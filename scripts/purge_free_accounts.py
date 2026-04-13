#!/usr/bin/env python3
"""One-shot mass purge of FREE codex accounts across all channels.

This script:
  1. Backs up all currently VALID (not 401) free codex accounts with their full
     credentials (access_token + refresh_token) so they can be restored later.
  2. Deletes ALL free codex accounts from every channel.

Channels processed:
  - CPA (each comma-separated URL in CPA_BASE_URL)
  - Sub2API "codex free" group

Channels NEVER touched:
  - Sub2API anthropic accounts (or any non-codex group)
  - Sub2API "codex team" accounts
  - CPA "team" plan_type accounts

Usage:
    python scripts/purge_free_accounts.py --dry-run    # preview only
    python scripts/purge_free_accounts.py              # backup + interactive confirmation
    python scripts/purge_free_accounts.py --yes        # backup + delete (no prompt)

Backup layout (created under $BACKUP_DIR/purge_<YYYYMMDD_HHMMSS>/):
  cpa_<host>_codex_free.json   — list of {"name", "content"}, content = full auth file JSON
  sub2api_codex_free.json      — list of full sub2api account dicts (already include credentials)
  manifest.json                — counts + source URLs + restore hints
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

CPA_BASE_URL = os.environ.get("CPA_BASE_URL", "")
CPA_TOKEN = os.environ.get("CPA_TOKEN", "")
SUB2API_URL = os.environ.get("SUB2API_URL", "")
SUB2API_ADMIN_EMAIL = os.environ.get("SUB2API_ADMIN_EMAIL", "")
SUB2API_ADMIN_PASSWORD = os.environ.get("SUB2API_ADMIN_PASSWORD", "")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(Path(__file__).resolve().parent.parent / "backups")))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def cpa_headers() -> dict:
    return {"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# CPA helpers
# ---------------------------------------------------------------------------

def cpa_list_codex(url: str) -> list[dict]:
    r = requests.get(f"{url.rstrip('/')}/v0/management/auth-files", headers=cpa_headers(), timeout=30)
    r.raise_for_status()
    data = r.json()
    files = data.get("files", data) if isinstance(data, dict) else data
    return [f for f in files if f.get("type") == "codex"]


def cpa_get_plan_type(item: dict) -> str:
    """Extract plan_type from id_token.

    Handles three shapes:
      1. id_token is a dict with plan_type key
      2. id_token is a JSON string of such a dict
      3. id_token is a raw JWT string (gpt-team heartbeat uploads this way) —
         decode the payload and look at `https://api.openai.com/auth.chatgpt_plan_type`
    """
    import base64 as _b64
    id_tok = item.get("id_token")
    # Form 1: dict
    if isinstance(id_tok, dict):
        return str(id_tok.get("plan_type") or "").strip().lower()
    if not isinstance(id_tok, str) or not id_tok:
        return ""
    # Form 2: JSON-encoded dict
    try:
        parsed = json.loads(id_tok)
        if isinstance(parsed, dict) and parsed.get("plan_type"):
            return str(parsed["plan_type"]).strip().lower()
    except Exception:
        pass
    # Form 3: raw JWT (three dot-separated base64 parts)
    parts = id_tok.split(".")
    if len(parts) >= 2:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = json.loads(_b64.urlsafe_b64decode(payload))
            auth = decoded.get("https://api.openai.com/auth", {})
            plan = auth.get("chatgpt_plan_type") or decoded.get("plan_type")
            if plan:
                return str(plan).strip().lower()
        except Exception:
            pass
    return ""


def cpa_probe_status(url: str, item: dict) -> int:
    """Probe an account via CPA's api-call endpoint, return upstream status code.

    Returns 0 on network error.
    """
    name = item.get("name", "")
    auth_index = item.get("auth_index", "")
    id_tok = item.get("id_token")
    if isinstance(id_tok, str):
        try:
            id_tok = json.loads(id_tok)
        except Exception:
            id_tok = {}
    account_id = (id_tok or {}).get("chatgpt_account_id", "")
    if not auth_index:
        return 0
    payload = {
        "authIndex": auth_index,
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": {
            "Authorization": "Bearer $TOKEN$",
            "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
            **({"Chatgpt-Account-Id": account_id} if account_id else {}),
        },
    }
    try:
        resp = requests.post(
            f"{url.rstrip('/')}/v0/management/api-call",
            headers=cpa_headers(), json=payload, timeout=20,
        )
        upstream = resp.status_code
        if resp.status_code == 200:
            try:
                upstream = resp.json().get("status_code", resp.status_code)
            except Exception:
                pass
        return int(upstream)
    except Exception:
        return 0


def cpa_download_auth(url: str, name: str) -> dict | None:
    enc = urllib.parse.quote(name, safe="")
    try:
        r = requests.get(
            f"{url.rstrip('/')}/v0/management/auth-files/download?name={enc}",
            headers=cpa_headers(), timeout=15,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def cpa_delete_auth(url: str, name: str) -> bool:
    enc = urllib.parse.quote(name, safe="")
    try:
        r = requests.delete(
            f"{url.rstrip('/')}/v0/management/auth-files?name={enc}",
            headers=cpa_headers(), timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sub2API helpers
# ---------------------------------------------------------------------------

def sub2api_login() -> str:
    r = requests.post(
        f"{SUB2API_URL.rstrip('/')}/api/v1/auth/login",
        json={"email": SUB2API_ADMIN_EMAIL, "password": SUB2API_ADMIN_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"sub2api login failed: {body.get('message')}")
    return body["data"]["access_token"]


def sub2api_list_all(jwt: str) -> list[dict]:
    base = SUB2API_URL.rstrip("/")
    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    out: list[dict] = []
    page = 1
    while True:
        r = requests.get(f"{base}/api/v1/admin/accounts?page={page}&page_size=100", headers=hdrs, timeout=30)
        r.raise_for_status()
        d = r.json().get("data", {})
        items = d.get("items", [])
        out.extend(items)
        total = d.get("total", 0)
        if len(out) >= total or not items:
            break
        page += 1
    return out


def sub2api_delete(jwt: str, account_id: int) -> bool:
    base = SUB2API_URL.rstrip("/")
    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    try:
        r = requests.delete(f"{base}/api/v1/admin/accounts/{account_id}", headers=hdrs, timeout=15)
        return r.status_code == 200 and r.json().get("code") == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def is_sub2api_401(acct: dict) -> bool:
    """True if a sub2api account is in 401/token_invalidated state."""
    if acct.get("status") != "error":
        return False
    err = str(acct.get("error_message") or "")
    return ("token_invalidated" in err) or ('"status": 401' in err) or ('"status":401' in err)


def process_cpa(url: str, backup_dir: Path, dry_run: bool, assume_yes: bool, probe: bool) -> dict:
    print(f"\n=== CPA: {url} ===")
    codex = cpa_list_codex(url)
    free = [f for f in codex if cpa_get_plan_type(f) == "free"]
    team = [f for f in codex if cpa_get_plan_type(f) == "team"]
    other = [f for f in codex if cpa_get_plan_type(f) not in {"free", "team"}]
    print(f"  Codex: total={len(codex)}  free={len(free)}  team={len(team)}  other_plan={len(other)}")

    # --- Probe to identify which free accounts are healthy (non-401) ---
    healthy_free: list[dict] = []
    if probe and free:
        print(f"  Probing {len(free)} free accounts via wham/usage...")
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(cpa_probe_status, url, f): f for f in free}
            done = 0
            for fut in as_completed(futs):
                f = futs[fut]
                try:
                    code = fut.result()
                except Exception:
                    code = 0
                # Healthy = not 401 and not 0 (network err treated as unknown)
                if code and code != 401:
                    healthy_free.append(f)
                done += 1
                if done % 100 == 0:
                    print(f"    probed {done}/{len(free)}")
        print(f"  Healthy (non-401) free: {len(healthy_free)}/{len(free)}")
    else:
        # No probe — assume all currently-not-disabled free accounts are healthy
        healthy_free = [f for f in free if not f.get("disabled")]
        print(f"  Skipping probe; treating {len(healthy_free)} non-disabled free as healthy")

    # --- Backup healthy accounts with credentials ---
    backups: list[dict] = []
    if healthy_free:
        print(f"  Downloading credentials for {len(healthy_free)} healthy free accounts...")
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(cpa_download_auth, url, f["name"]): f for f in healthy_free}
            for fut in as_completed(futs):
                f = futs[fut]
                content = fut.result()
                if content:
                    backups.append({"name": f["name"], "content": content,
                                    "email": f.get("email") or f.get("account") or ""})
        host = url.replace("https://", "").replace("http://", "").rstrip("/").replace("/", "_")
        bp = backup_dir / f"cpa_{host}_codex_free.json"
        bp.write_text(json.dumps(backups, ensure_ascii=False, indent=2))
        print(f"  Backup written: {bp} ({len(backups)} accounts)")

    # --- Delete ALL free codex accounts (healthy + 401) ---
    if not free:
        return {"channel": url, "free_total": 0, "backup_saved": 0, "deleted_ok": 0, "deleted_fail": 0}

    if dry_run:
        print(f"  [DRY-RUN] Would delete {len(free)} free codex accounts")
        return {"channel": url, "free_total": len(free), "backup_saved": len(backups), "deleted_ok": 0, "deleted_fail": 0}

    if not assume_yes:
        ans = input(f"  Delete {len(free)} free codex accounts from {url}? Type DELETE: ").strip()
        if ans != "DELETE":
            print("  Aborted by user")
            return {"channel": url, "free_total": len(free), "backup_saved": len(backups), "deleted_ok": 0, "deleted_fail": 0}

    print(f"  Deleting {len(free)} free codex accounts...")
    deleted_ok = 0
    deleted_fail = 0
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(cpa_delete_auth, url, f["name"]): f["name"] for f in free}
        for fut in as_completed(futs):
            if fut.result():
                deleted_ok += 1
            else:
                deleted_fail += 1
    print(f"  Deleted: ok={deleted_ok} fail={deleted_fail}")
    return {"channel": url, "free_total": len(free), "backup_saved": len(backups),
            "deleted_ok": deleted_ok, "deleted_fail": deleted_fail}


def process_sub2api(backup_dir: Path, dry_run: bool, assume_yes: bool) -> dict:
    if not SUB2API_URL or not SUB2API_ADMIN_EMAIL or not SUB2API_ADMIN_PASSWORD:
        print("\n=== Sub2API: not configured, skipping ===")
        return {}

    print(f"\n=== Sub2API: {SUB2API_URL} ===")
    jwt = sub2api_login()
    all_accts = sub2api_list_all(jwt)
    free_accts = [
        a for a in all_accts
        if a.get("platform") == "openai" and a.get("type") == "oauth"
        and any(g.get("name") == "codex free" for g in a.get("groups", []))
    ]
    team_accts = [
        a for a in all_accts
        if a.get("platform") == "openai" and a.get("type") == "oauth"
        and any(g.get("name") == "codex team" for g in a.get("groups", []))
    ]
    print(f"  Total: {len(all_accts)}  codex-free: {len(free_accts)}  codex-team: {len(team_accts)} (UNTOUCHED)")

    healthy_free = [a for a in free_accts if not is_sub2api_401(a)]
    print(f"  Healthy (non-401) free: {len(healthy_free)}/{len(free_accts)}")

    if healthy_free:
        bp = backup_dir / "sub2api_codex_free.json"
        bp.write_text(json.dumps(healthy_free, ensure_ascii=False, indent=2, default=str))
        print(f"  Backup written: {bp} ({len(healthy_free)} accounts with credentials)")

    if not free_accts:
        return {"channel": "sub2api", "free_total": 0, "backup_saved": 0, "deleted_ok": 0, "deleted_fail": 0}

    if dry_run:
        print(f"  [DRY-RUN] Would delete {len(free_accts)} codex-free accounts")
        return {"channel": "sub2api", "free_total": len(free_accts), "backup_saved": len(healthy_free),
                "deleted_ok": 0, "deleted_fail": 0}

    if not assume_yes:
        ans = input(f"  Delete {len(free_accts)} codex-free accounts from sub2api? Type DELETE: ").strip()
        if ans != "DELETE":
            print("  Aborted by user")
            return {"channel": "sub2api", "free_total": len(free_accts), "backup_saved": len(healthy_free),
                    "deleted_ok": 0, "deleted_fail": 0}

    print(f"  Deleting {len(free_accts)} codex-free accounts...")
    deleted_ok = 0
    deleted_fail = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(sub2api_delete, jwt, a["id"]): a for a in free_accts}
        for fut in as_completed(futs):
            if fut.result():
                deleted_ok += 1
            else:
                deleted_fail += 1
    print(f"  Deleted: ok={deleted_ok} fail={deleted_fail}")
    return {"channel": "sub2api", "free_total": len(free_accts), "backup_saved": len(healthy_free),
            "deleted_ok": deleted_ok, "deleted_fail": deleted_fail}


def main() -> int:
    parser = argparse.ArgumentParser(description="Mass purge of free codex accounts")
    parser.add_argument("--dry-run", action="store_true", help="Preview only — backup but no deletions")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    parser.add_argument("--no-probe", action="store_true",
                        help="Skip wham/usage probing for CPA (treat non-disabled as healthy)")
    parser.add_argument("--skip-cpa", action="store_true", help="Skip CPA channels")
    parser.add_argument("--skip-sub2api", action="store_true", help="Skip sub2api channel")
    args = parser.parse_args()

    if not CPA_TOKEN and not args.skip_cpa:
        print("ERROR: CPA_TOKEN not set", file=sys.stderr)
        return 1

    cpa_urls = [u.strip() for u in CPA_BASE_URL.split(",") if u.strip()]
    if not cpa_urls and not args.skip_cpa:
        print("ERROR: CPA_BASE_URL not set", file=sys.stderr)
        return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_DIR / f"purge_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Free Codex Accounts Mass Purge — {utc_now()} ===")
    print(f"Backup dir: {backup_dir}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    if args.no_probe:
        print("CPA probing: DISABLED (using disabled flag instead)")
    print()

    results: list[dict] = []
    if not args.skip_cpa:
        for url in cpa_urls:
            results.append(process_cpa(url, backup_dir, args.dry_run, args.yes, probe=not args.no_probe))

    if not args.skip_sub2api:
        results.append(process_sub2api(backup_dir, args.dry_run, args.yes))

    # Manifest
    manifest = {
        "timestamp": utc_now(),
        "dry_run": args.dry_run,
        "results": results,
        "restore_hint": "Use scripts/restore_free_accounts.py with this backup dir to re-upload accounts.",
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    print("\n=== Summary ===")
    for r in results:
        if r:
            print(f"  {r.get('channel')}: free={r.get('free_total',0)} "
                  f"backup={r.get('backup_saved',0)} "
                  f"deleted={r.get('deleted_ok',0)}/{r.get('deleted_ok',0)+r.get('deleted_fail',0)}")
    print(f"\nBackup directory: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

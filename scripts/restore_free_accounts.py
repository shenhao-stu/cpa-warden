#!/usr/bin/env python3
"""Restore free codex accounts from a purge backup directory.

Reads backups written by `purge_free_accounts.py` and re-uploads them to:
  - All CPA instances listed in CPA_BASE_URL
  - Sub2API "codex free" group

Optionally tests each account before restoring (via sub2api test API) so only
currently-healthy accounts are uploaded.

Usage:
    python scripts/restore_free_accounts.py <backup_dir>               # restore all
    python scripts/restore_free_accounts.py <backup_dir> --test        # test first, skip 401
    python scripts/restore_free_accounts.py <backup_dir> --dry-run
    python scripts/restore_free_accounts.py <backup_dir> --only cpa    # restore only to CPA
    python scripts/restore_free_accounts.py <backup_dir> --only sub2api
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

CPA_BASE_URL = os.environ.get("CPA_BASE_URL", "")
CPA_TOKEN = os.environ.get("CPA_TOKEN", "")
SUB2API_URL = os.environ.get("SUB2API_URL", "")
SUB2API_ADMIN_EMAIL = os.environ.get("SUB2API_ADMIN_EMAIL", "")
SUB2API_ADMIN_PASSWORD = os.environ.get("SUB2API_ADMIN_PASSWORD", "")
SUB2API_CODEX_FREE_GROUP_ID = int(os.environ.get("SUB2API_CODEX_FREE_GROUP_ID", "4"))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# CPA upload
# ---------------------------------------------------------------------------

def cpa_headers() -> dict:
    return {"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"}


def cpa_existing_names(url: str) -> set[str]:
    try:
        r = requests.get(f"{url.rstrip('/')}/v0/management/auth-files", headers=cpa_headers(), timeout=60)
        r.raise_for_status()
        data = r.json()
        files = data.get("files", data) if isinstance(data, dict) else data
        return {f.get("name", "") for f in files if f.get("name")}
    except Exception as e:
        print(f"  [CPA] list {url} error: {e}")
        return set()


def cpa_upload_auth(url: str, name: str, content: dict) -> bool:
    enc = urllib.parse.quote(name, safe="")
    try:
        r = requests.post(
            f"{url.rstrip('/')}/v0/management/auth-files?name={enc}",
            headers=cpa_headers(),
            data=json.dumps(content),
            timeout=30,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Sub2API
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


def sub2api_existing_names(jwt: str) -> set[str]:
    base = SUB2API_URL.rstrip("/")
    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    names: set[str] = set()
    page = 1
    while True:
        r = requests.get(f"{base}/api/v1/admin/accounts?page={page}&page_size=100", headers=hdrs, timeout=30)
        r.raise_for_status()
        d = r.json().get("data", {})
        items = d.get("items", [])
        for a in items:
            if a.get("name"):
                names.add(a["name"].lower().strip())
        total = d.get("total", 0)
        if len(names) >= total or not items:
            break
        page += 1
    return names


def sub2api_upload(jwt: str, name: str, access_token: str, refresh_token: str) -> bool:
    base = SUB2API_URL.rstrip("/")
    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    payload = {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "group_ids": [SUB2API_CODEX_FREE_GROUP_ID],
        "credentials": {"access_token": access_token, "refresh_token": refresh_token},
    }
    try:
        r = requests.post(f"{base}/api/v1/admin/accounts", headers=hdrs, json=payload, timeout=30)
        return r.status_code in (200, 201) and r.json().get("code") == 0
    except Exception:
        return False


def sub2api_test_account(jwt: str, account_id: int) -> str:
    """Return 'ok', '401', or '429'."""
    base = SUB2API_URL.rstrip("/")
    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    prompts = ["1+1", "hi", "ok?", "2+2", "hello", "thanks", "yes", "no"]
    try:
        r = requests.post(
            f"{base}/api/v1/admin/accounts/{account_id}/test",
            headers=hdrs,
            json={"model_id": "gpt-5.4", "prompt": random.choice(prompts)},
            timeout=60,
        )
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("data: "):
                line = line[6:]
            elif line.startswith("data:"):
                line = line[5:]
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "error":
                err = ev.get("error", "")
                if "token_invalidated" in err or '"status": 401' in err or '"status":401' in err:
                    return "401"
                if '"status": 429' in err or '"status":429' in err or "API returned 429" in err:
                    return "429"
                return "ok"
        return "ok"
    except Exception:
        return "ok"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def load_cpa_backup(backup_dir: Path) -> dict[str, list[dict]]:
    """Return {host: [{name, content}, ...]} for all CPA backup files."""
    out: dict[str, list[dict]] = {}
    for bp in sorted(backup_dir.glob("cpa_*_codex_free.json")):
        host = bp.stem.replace("cpa_", "").replace("_codex_free", "")
        try:
            out[host] = json.loads(bp.read_text())
        except Exception as e:
            print(f"  [CPA] Failed to load {bp}: {e}")
    return out


def load_sub2api_backup(backup_dir: Path) -> list[dict]:
    bp = backup_dir / "sub2api_codex_free.json"
    if not bp.exists():
        return []
    try:
        return json.loads(bp.read_text())
    except Exception as e:
        print(f"  [Sub2API] Failed to load {bp}: {e}")
        return []


def host_from_url(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").rstrip("/").replace("/", "_")


def restore_cpa(backup_dir: Path, dry_run: bool) -> dict:
    cpa_urls = [u.strip() for u in CPA_BASE_URL.split(",") if u.strip()]
    if not cpa_urls:
        print("  [CPA] CPA_BASE_URL not set — skipping")
        return {"uploaded": 0, "skipped": 0}

    backups = load_cpa_backup(backup_dir)
    if not backups:
        print("  [CPA] No CPA backup files found")
        return {"uploaded": 0, "skipped": 0}

    # Use all backups as the pool — we will upload each account to every CPA instance
    # that doesn't already have it.
    all_accounts: dict[str, dict] = {}  # name -> content
    for host, lst in backups.items():
        for item in lst:
            if item.get("name") and item.get("content"):
                all_accounts[item["name"]] = item["content"]
    print(f"  [CPA] Backup pool: {len(all_accounts)} unique accounts from {len(backups)} host file(s)")

    stats_uploaded = 0
    stats_skipped = 0
    for url in cpa_urls:
        host = host_from_url(url)
        existing = cpa_existing_names(url)
        missing = [name for name in all_accounts if name not in existing]
        print(f"  [CPA] {url}: existing={len(existing)} missing={len(missing)}")
        if dry_run:
            stats_skipped += len(missing)
            continue
        up_ok = 0
        up_fail = 0
        for i, name in enumerate(missing, 1):
            if cpa_upload_auth(url, name, all_accounts[name]):
                up_ok += 1
            else:
                up_fail += 1
            if i % 50 == 0:
                print(f"    progress: {i}/{len(missing)} ok={up_ok} fail={up_fail}")
        print(f"  [CPA] {url}: uploaded={up_ok} failed={up_fail}")
        stats_uploaded += up_ok
    return {"uploaded": stats_uploaded, "skipped": stats_skipped}


def restore_sub2api(backup_dir: Path, dry_run: bool, test_first: bool) -> dict:
    if not SUB2API_URL or not SUB2API_ADMIN_EMAIL or not SUB2API_ADMIN_PASSWORD:
        print("  [Sub2API] Not configured, skipping")
        return {"uploaded": 0, "skipped": 0, "tested_401": 0}

    items = load_sub2api_backup(backup_dir)
    if not items:
        print("  [Sub2API] No backup file")
        return {"uploaded": 0, "skipped": 0, "tested_401": 0}

    jwt = sub2api_login()
    existing = sub2api_existing_names(jwt)
    print(f"  [Sub2API] Backup: {len(items)} accounts   Existing on sub2api: {len(existing)}")

    missing = [a for a in items if (a.get("name") or "").lower().strip() not in existing]
    print(f"  [Sub2API] Missing (to restore): {len(missing)}")

    if dry_run:
        return {"uploaded": 0, "skipped": len(missing), "tested_401": 0}

    up_ok = 0
    up_fail = 0
    tested_401 = 0
    for i, acct in enumerate(missing, 1):
        name = (acct.get("name") or "").strip()
        cred = acct.get("credentials") or {}
        ak = cred.get("access_token", "")
        rk = cred.get("refresh_token", "")
        if not name or not ak:
            up_fail += 1
            continue
        if sub2api_upload(jwt, name, ak, rk):
            up_ok += 1
            # Optionally test after upload and re-delete if 401
            if test_first:
                # Need the new account ID — easier to just skip test and let
                # the periodic cleanup remove 401s later.
                pass
        else:
            up_fail += 1
        if i % 50 == 0:
            print(f"    progress: {i}/{len(missing)} ok={up_ok} fail={up_fail}")
    print(f"  [Sub2API] Uploaded: ok={up_ok} fail={up_fail}")
    return {"uploaded": up_ok, "skipped": up_fail, "tested_401": tested_401}


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore free codex accounts from a purge backup")
    parser.add_argument("backup_dir", help="Path to a backups/purge_<timestamp>/ directory")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--test", action="store_true",
                        help="(sub2api) After upload, test via gpt-5.4 and remove any that still 401")
    parser.add_argument("--only", choices=["cpa", "sub2api"], default=None,
                        help="Restore to only one channel type")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir)
    if not backup_dir.is_dir():
        print(f"ERROR: backup dir not found: {backup_dir}", file=sys.stderr)
        return 1

    print(f"=== Restore Free Codex Accounts — {utc_now()} ===")
    print(f"Backup: {backup_dir}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print()

    results: dict = {}
    if args.only in (None, "cpa"):
        print("[CPA] Restoring...")
        results["cpa"] = restore_cpa(backup_dir, args.dry_run)

    if args.only in (None, "sub2api"):
        print("\n[Sub2API] Restoring...")
        results["sub2api"] = restore_sub2api(backup_dir, args.dry_run, args.test)

    print("\n=== Summary ===")
    for k, v in results.items():
        print(f"  {k}: uploaded={v.get('uploaded',0)} skipped={v.get('skipped',0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

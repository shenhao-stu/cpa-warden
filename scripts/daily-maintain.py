#!/usr/bin/env python3
"""Local daily CPA maintenance: scan → delete bad accounts → sync Git → Feishu notify.

Extends cpa_warden.py maintain mode with:
  - 5xx error account deletion (cpa_warden only handles 401)
  - Git repo sync: delete auth files from GitHub that were removed from CPA
  - Feishu webhook notification

Usage:
    python scripts/daily-maintain.py          # run maintain + git sync + notify
    python scripts/daily-maintain.py --dry-run  # preview only
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------

CPA_BASE_URL = os.environ.get("CPA_BASE_URL", "https://ohmyapi-2api.hf.space")
CPA_TOKEN = os.environ.get("CPA_TOKEN", "")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GIT_REPO = os.environ.get("CPA_GIT_REPO", "shenhao-stu/ohmyapi-2api")
GIT_BRANCH = os.environ.get("CPA_GIT_BRANCH", "master")
GIT_AUTH_DIR = os.environ.get("CPA_GIT_AUTH_DIR", "auths")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK_URL", "")
WARDEN_PY = os.environ.get("WARDEN_PY", str(Path(__file__).resolve().parent.parent / "cpa_warden.py"))
PYTHON = os.environ.get("PYTHON", sys.executable)

# grok2api PostgreSQL DSN
GROK_PG_DSN = os.environ.get("GROK_PG_DSN", "")
# Max age (hours) for grok SSO tokens before they are considered expired
GROK_TOKEN_MAX_AGE_H = int(os.environ.get("GROK_TOKEN_MAX_AGE_H", "48"))

# Which upstream API status codes trigger account deletion (in addition to 401).
DELETE_STATUSES = {401, 403, 500, 502, 503}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# CPA Management API helpers
# ---------------------------------------------------------------------------

def cpa_headers() -> dict:
    return {
        "Authorization": f"Bearer {CPA_TOKEN}",
        "Content-Type": "application/json",
    }


def cpa_list_auth_files() -> list[dict]:
    """GET /v0/management/auth-files — return the list of auth file objects."""
    resp = requests.get(
        f"{CPA_BASE_URL.rstrip('/')}/v0/management/auth-files",
        headers=cpa_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("files", data) if isinstance(data, dict) else data


def cpa_delete_auth_file(name: str) -> bool:
    """DELETE /v0/management/auth-files?name={name}"""
    encoded = urllib.parse.quote(name, safe="")
    resp = requests.delete(
        f"{CPA_BASE_URL.rstrip('/')}/v0/management/auth-files?name={encoded}",
        headers=cpa_headers(),
        timeout=15,
    )
    return resp.status_code == 200


def cpa_probe_account(name: str) -> dict | None:
    """POST /v0/management/api-call to probe a single account's wham/usage.

    Returns the parsed JSON body, or None on failure.
    """
    payload = {
        "auth_name": name,
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "headers": {
            "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
        },
    }
    try:
        resp = requests.post(
            f"{CPA_BASE_URL.rstrip('/')}/v0/management/api-call",
            headers=cpa_headers(),
            json=payload,
            timeout=20,
        )
        return {"http_status": resp.status_code, "body": resp.text[:500]}
    except Exception as e:
        return {"http_status": 0, "body": str(e)}


# ---------------------------------------------------------------------------
# GitHub REST API helpers
# ---------------------------------------------------------------------------

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_list_auth_files() -> dict[str, str]:
    """List files in auths/ dir, returning {filename: sha}."""
    api_url = f"https://api.github.com/repos/{GIT_REPO}/contents/{GIT_AUTH_DIR}"
    resp = requests.get(api_url, headers=gh_headers(), params={"ref": GIT_BRANCH}, timeout=15)
    if resp.status_code != 200:
        return {}
    return {item["name"]: item["sha"] for item in resp.json() if item["type"] == "file"}


def gh_delete_file(filename: str, sha: str) -> bool:
    """Delete a file from the GitHub repo."""
    api_url = f"https://api.github.com/repos/{GIT_REPO}/contents/{GIT_AUTH_DIR}/{urllib.parse.quote(filename, safe='')}"
    payload = {
        "message": f"Remove invalid auth: {filename}",
        "sha": sha,
        "branch": GIT_BRANCH,
    }
    resp = requests.delete(api_url, headers=gh_headers(), json=payload, timeout=15)
    return resp.status_code == 200


# ---------------------------------------------------------------------------
# Run cpa_warden maintain
# ---------------------------------------------------------------------------

def run_warden_maintain(tmpdir: str, dry_run: bool = False, base_url: str = "", token: str = "") -> dict:
    """Run cpa_warden.py --mode maintain and return parsed stats."""
    config_path = os.path.join(tmpdir, "config.json")
    db_path = os.path.join(tmpdir, "state.sqlite3")
    invalid_path = os.path.join(tmpdir, "invalid.json")
    quota_path = os.path.join(tmpdir, "quota.json")
    log_path = os.path.join(tmpdir, "run.log")

    config = {
        "base_url": base_url or CPA_BASE_URL,
        "token": token or CPA_TOKEN,
        "target_type": "codex",
        "probe_workers": 40,
        "action_workers": 20,
        "timeout": 20,
        "retries": 2,
        "delete_retries": 2,
        "quota_action": "disable",
        "delete_401": True,
        "auto_reenable": True,
        "db_path": db_path,
        "invalid_output": invalid_path,
        "quota_output": quota_path,
        "log_file": log_path,
        "debug": False,
    }
    with open(config_path, "w") as f:
        json.dump(config, f)

    start = time.monotonic()
    cmd = [PYTHON, WARDEN_PY, "--mode", "scan" if dry_run else "maintain", "--config", config_path, "--yes"]
    proc = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=600,
    )
    elapsed = round(time.monotonic() - start, 1)

    # Parse stats from log
    stats = parse_log_stats(log_path)
    stats["elapsed"] = elapsed
    stats["returncode"] = proc.returncode
    stats["stdout"] = proc.stdout[-500:] if proc.stdout else ""
    stats["stderr"] = proc.stderr[-500:] if proc.stderr else ""

    # Read deleted account names from the invalid output
    deleted_names = []
    if os.path.isfile(invalid_path):
        try:
            with open(invalid_path) as f:
                for item in json.load(f):
                    if item.get("name"):
                        deleted_names.append(item["name"])
        except Exception:
            pass
    stats["deleted_401_names"] = deleted_names

    return stats


def parse_log_stats(log_path: str) -> dict:
    stats = {
        "total": 0, "filtered": 0, "invalid_401": 0,
        "quota_limited": 0, "recovered": 0,
        "delete_401_ok": 0, "delete_401_fail": 0,
        "quota_action_ok": 0, "quota_action_fail": 0,
        "reenable_ok": 0, "reenable_fail": 0,
    }
    try:
        content = Path(log_path).read_text()
    except FileNotFoundError:
        return stats

    def extract_int(line, prefix):
        try:
            idx = line.index(prefix) + len(prefix)
            num = ""
            for ch in line[idx:].strip():
                if ch.isdigit():
                    num += ch
                else:
                    break
            return int(num) if num else 0
        except (ValueError, IndexError):
            return 0

    for line in content.split("\n"):
        if "总认证文件数:" in line:
            stats["total"] = extract_int(line, "总认证文件数:")
        elif "符合过滤条件账号数:" in line:
            stats["filtered"] = extract_int(line, "符合过滤条件账号数:")
        elif "401 账号数:" in line:
            stats["invalid_401"] = extract_int(line, "401 账号数:")
        elif "限额账号数:" in line:
            stats["quota_limited"] = extract_int(line, "限额账号数:")
        elif "恢复候选账号数:" in line:
            stats["recovered"] = extract_int(line, "恢复候选账号数:")
        elif "删除 401:" in line:
            stats["delete_401_ok"] = extract_int(line, "成功=")
            stats["delete_401_fail"] = extract_int(line, "失败=")
        elif "处理限额:" in line:
            stats["quota_action_ok"] = extract_int(line, "成功=")
            stats["quota_action_fail"] = extract_int(line, "失败=")
        elif "恢复启用:" in line:
            stats["reenable_ok"] = extract_int(line, "成功=")
            stats["reenable_fail"] = extract_int(line, "失败=")

    return stats


# ---------------------------------------------------------------------------
# Extended cleanup: delete 5xx accounts + Git sync
# ---------------------------------------------------------------------------

def find_and_delete_error_accounts(auth_files: list[dict], dry_run: bool = False, base_url: str = "", token: str = "") -> dict:
    """Probe accounts for 5xx/error statuses and delete them via CPA API.

    Returns {"probed": N, "deleted_ok": N, "deleted_fail": N, "names": [...]}.
    """
    _url = (base_url or CPA_BASE_URL).rstrip("/")
    _tok = token or CPA_TOKEN
    hdrs = {"Authorization": f"Bearer {_tok}", "Content-Type": "application/json"}

    candidates = [
        f for f in auth_files
        if f.get("type", "") == "codex" and not f.get("disabled", False)
    ]

    error_accounts = []
    for af in candidates:
        name = af.get("name", "")
        if not name:
            continue
        payload = {
            "auth_name": name, "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "headers": {"User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"},
        }
        try:
            resp = requests.post(f"{_url}/v0/management/api-call", headers=hdrs, json=payload, timeout=20)
            if resp.status_code in DELETE_STATUSES:
                error_accounts.append({"name": name, "status": resp.status_code})
        except Exception:
            pass

    print(f"    Probed {len(candidates)}, found {len(error_accounts)} errors")

    if dry_run:
        return {"probed": len(candidates), "deleted_ok": 0, "deleted_fail": 0, "names": []}

    deleted_ok, deleted_fail = 0, 0
    deleted_names = []
    for ea in error_accounts:
        encoded = urllib.parse.quote(ea["name"], safe="")
        try:
            resp = requests.delete(f"{_url}/v0/management/auth-files?name={encoded}", headers=hdrs, timeout=15)
            if resp.status_code == 200:
                deleted_ok += 1
                deleted_names.append(ea["name"])
            else:
                deleted_fail += 1
        except Exception:
            deleted_fail += 1

    return {"probed": len(candidates), "deleted_ok": deleted_ok, "deleted_fail": deleted_fail, "names": deleted_names}


def sync_git_deletions(deleted_names: list[str], dry_run: bool = False) -> dict:
    """Delete auth files from the GitHub repo that were removed from CPA.

    Returns {"synced": N, "skipped": N, "not_in_git": N}.
    """
    if not GH_TOKEN:
        print("  [Git] GH_TOKEN not set, skipping Git sync")
        return {"synced": 0, "skipped": 0, "not_in_git": 0}
    if not deleted_names:
        print("  [Git] No deletions to sync")
        return {"synced": 0, "skipped": 0, "not_in_git": 0}

    git_files = gh_list_auth_files()
    synced, skipped, not_in_git = 0, 0, 0

    for name in deleted_names:
        sha = git_files.get(name)
        if not sha:
            not_in_git += 1
            continue

        if dry_run:
            print(f"    [DRY-RUN] Would delete from Git: {name}")
            skipped += 1
            continue

        if gh_delete_file(name, sha):
            synced += 1
        else:
            skipped += 1
            print(f"    [Git] FAILED to delete: {name}")

    print(f"  [Git] Synced {synced}, skipped {skipped}, not in Git {not_in_git}")
    return {"synced": synced, "skipped": skipped, "not_in_git": not_in_git}


# ---------------------------------------------------------------------------
# Feishu notification
# ---------------------------------------------------------------------------

def send_feishu(warden_stats: dict, error_stats: dict, git_stats: dict, grok_stats: dict | None = None) -> None:
    if not FEISHU_WEBHOOK:
        print("  [Feishu] Webhook not set, skipping")
        return

    ts = utc_now()
    total = warden_stats.get("total", 0)
    filtered = warden_stats.get("filtered", 0)
    inv401 = warden_stats.get("invalid_401", 0)
    quota = warden_stats.get("quota_limited", 0)
    active = filtered - inv401 - quota
    del_ok = warden_stats.get("delete_401_ok", 0)
    del_fail = warden_stats.get("delete_401_fail", 0)
    err_del_ok = error_stats.get("deleted_ok", 0)
    err_del_fail = error_stats.get("deleted_fail", 0)
    elapsed = warden_stats.get("elapsed", 0)

    all_ok = del_fail == 0 and err_del_fail == 0
    header_color = "green" if all_ok else "yellow"
    header_icon = "✅" if all_ok else "⚠️"

    lines = [
        f"🕐 {ts}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📍 Codex — Scan ({CPA_BASE_URL})",
        f"   📦 Total: {total}  |  🎯 Filtered: {filtered}",
        f"   ✅ Active: {active}  |  🚫 401: {inv401}  |  ⚠️ Quota: {quota}",
        f"📍 Codex — Actions",
        f"   🗑️ Delete 401: ✅ {del_ok}  ❌ {del_fail}",
        f"   🗑️ Delete 5xx: ✅ {err_del_ok}  ❌ {err_del_fail}",
    ]

    if grok_stats:
        lines.extend([
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📍 Grok — Token Maintenance",
            f"   📦 Before: {grok_stats.get('total_before', 0)}  →  Active: {grok_stats.get('active_after', 0)}",
            f"   🗑️ Expired: {grok_stats.get('expired_deleted', 0)}  |  Disabled: {grok_stats.get('disabled_deleted', 0)}",
            f"   🔄 Migrated: {grok_stats.get('migrated_to_ssoBasic', 0)}  |  NSFW: {grok_stats.get('nsfw_enabled', 0)}",
        ])

    lines.extend([
        f"━━━━━━━━━━━━━━━━━━━━",
        f"⏱️ Completed in {elapsed}s",
    ])

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{header_icon} CPA Warden — Local Maintenance"},
                "template": header_color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
                {"tag": "hr"},
                {"tag": "note", "elements": [{"tag": "lark_md", "content": "🛡️ CPA Warden (local cron)"}]},
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=card, timeout=15)
        body = resp.json()
        if body.get("code") == 0 or body.get("StatusCode") == 0:
            print("  [Feishu] Notification sent")
        else:
            print(f"  [Feishu] API error: {body}")
    except Exception as e:
        print(f"  [Feishu] Failed: {e}")


# ---------------------------------------------------------------------------
# grok2api PostgreSQL maintenance
# ---------------------------------------------------------------------------

def _grok_pg_connect():
    """Connect to grok2api PostgreSQL with IPv4 forcing."""
    import socket
    import psycopg2
    from urllib.parse import urlparse

    parsed = urlparse(GROK_PG_DSN)
    host = parsed.hostname
    kwargs = {"connect_timeout": 15}
    if host:
        try:
            ipv4 = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
            kwargs["hostaddr"] = ipv4
        except socket.gaierror:
            pass
    return psycopg2.connect(GROK_PG_DSN, **kwargs)


def maintain_grok_tokens(dry_run: bool = False) -> dict:
    """Clean expired grok tokens and enable NSFW for all.

    - Delete tokens older than GROK_TOKEN_MAX_AGE_H
    - Delete tokens with status='disabled'
    - Migrate 'default' pool tokens to 'ssoBasic' with NSFW
    - Enable NSFW tags for all tokens missing it
    Returns stats dict.
    """
    if not GROK_PG_DSN:
        print("  [Grok] GROK_PG_DSN not set, skipping")
        return {}

    conn = _grok_pg_connect()
    cur = conn.cursor()
    now_ts = int(time.time())
    cutoff_ts = now_ts - GROK_TOKEN_MAX_AGE_H * 3600

    # Count before
    cur.execute("SELECT count(*) FROM tokens")
    total_before = cur.fetchone()[0]

    # 1. Delete expired tokens (older than max age)
    cur.execute("SELECT count(*) FROM tokens WHERE created_at > 0 AND created_at < %s", (cutoff_ts,))
    expired_count = cur.fetchone()[0]

    # 2. Count disabled tokens
    cur.execute("SELECT count(*) FROM tokens WHERE status = 'disabled'")
    disabled_count = cur.fetchone()[0]

    if not dry_run:
        # Delete expired
        cur.execute("DELETE FROM tokens WHERE created_at > 0 AND created_at < %s", (cutoff_ts,))
        # Delete disabled
        cur.execute("DELETE FROM tokens WHERE status = 'disabled'")
        # Migrate default pool → ssoBasic with active + nsfw
        cur.execute("""
            UPDATE tokens SET pool_name = 'ssoBasic', status = 'active', tags = '["nsfw"]'
            WHERE pool_name = 'default'
        """)
        migrated = cur.rowcount
        # Enable NSFW for any ssoBasic tokens missing it
        cur.execute("""
            UPDATE tokens SET tags = '["nsfw"]'
            WHERE pool_name = 'ssoBasic' AND (tags IS NULL OR tags = '[]' OR tags NOT LIKE '%%nsfw%%')
        """)
        nsfw_fixed = cur.rowcount
        conn.commit()
    else:
        cur.execute("SELECT count(*) FROM tokens WHERE pool_name = 'default'")
        migrated = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tokens WHERE pool_name = 'ssoBasic' AND (tags IS NULL OR tags = '[]' OR tags NOT LIKE '%%nsfw%%')")
        nsfw_fixed = cur.fetchone()[0]

    # Count after
    cur.execute("SELECT count(*) FROM tokens WHERE status IN ('active', 'normal')")
    active_after = cur.fetchone()[0]

    conn.close()

    stats = {
        "total_before": total_before,
        "expired_deleted": expired_count,
        "disabled_deleted": disabled_count,
        "migrated_to_ssoBasic": migrated,
        "nsfw_enabled": nsfw_fixed,
        "active_after": active_after,
    }
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="CPA Warden local daily maintenance")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no deletions")
    parser.add_argument("--skip-warden", action="store_true", help="Skip cpa_warden maintain (only do 5xx + git sync)")
    args = parser.parse_args()

    if not CPA_TOKEN:
        print("ERROR: CPA_TOKEN not set")
        return 1

    # Support comma-separated CPA_BASE_URL for multi-instance
    cpa_urls = [u.strip() for u in CPA_BASE_URL.split(",") if u.strip()]

    print(f"=== CPA Warden Local Maintenance — {utc_now()} ===")
    print(f"  Targets: {', '.join(cpa_urls)}")
    print()

    # Step 1-2: CPA maintenance for each instance
    all_warden: dict = {}
    all_error: dict = {"probed": 0, "deleted_ok": 0, "deleted_fail": 0}

    for idx, url in enumerate(cpa_urls, 1):
        print(f"[CPA {idx}/{len(cpa_urls)}] {url}")

        # Step 1: cpa_warden maintain
        warden_stats: dict = {}
        if not args.skip_warden:
            print("  [Warden] Running maintain...")
            with tempfile.TemporaryDirectory() as tmpdir:
                warden_stats = run_warden_maintain(tmpdir, dry_run=args.dry_run, base_url=url, token=CPA_TOKEN)
                print(f"    Total: {warden_stats.get('total', 0)} | 401: {warden_stats.get('invalid_401', 0)}")
                print(f"    Delete: ok={warden_stats.get('delete_401_ok', 0)} fail={warden_stats.get('delete_401_fail', 0)}")

        # Step 2: 5xx probing
        print("  [5xx] Probing for error accounts...")
        hdrs = {"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"}
        try:
            resp = requests.get(f"{url.rstrip('/')}/v0/management/auth-files", headers=hdrs, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            auth_files = data.get("files", data) if isinstance(data, dict) else data
        except Exception as e:
            print(f"    List failed: {e}")
            auth_files = []

        error_stats = find_and_delete_error_accounts(auth_files, dry_run=args.dry_run, base_url=url, token=CPA_TOKEN)

        # Aggregate
        if warden_stats:
            if not all_warden:
                all_warden = dict(warden_stats)
            else:
                for k in ("total", "filtered", "invalid_401", "quota_limited", "delete_401_ok", "delete_401_fail"):
                    all_warden[k] = all_warden.get(k, 0) + warden_stats.get(k, 0)
                all_warden["elapsed"] = all_warden.get("elapsed", 0) + warden_stats.get("elapsed", 0)
        all_error["probed"] += error_stats.get("probed", 0)
        all_error["deleted_ok"] += error_stats.get("deleted_ok", 0)
        all_error["deleted_fail"] += error_stats.get("deleted_fail", 0)
        print()

    # Step 3: Grok token maintenance
    print("[Step 3] Grok token maintenance...")
    grok_stats = maintain_grok_tokens(dry_run=args.dry_run)
    if grok_stats:
        print(f"  Before: {grok_stats['total_before']} | Expired deleted: {grok_stats['expired_deleted']}")
        print(f"  Disabled deleted: {grok_stats['disabled_deleted']} | Migrated to ssoBasic: {grok_stats['migrated_to_ssoBasic']}")
        print(f"  NSFW enabled: {grok_stats['nsfw_enabled']} | Active after: {grok_stats['active_after']}")

    # Step 4: Feishu notification
    print("\n[Step 4] Sending Feishu notification...")
    git_stats: dict = {}
    if not args.dry_run:
        send_feishu(all_warden, all_error, git_stats, grok_stats)
    else:
        print("  [DRY-RUN] Skipping Feishu notification")

    print(f"\n=== Done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

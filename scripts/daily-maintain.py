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
import re
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

# sub2api (Codex account cleanup)
SUB2API_URL = os.environ.get("SUB2API_URL", "")
SUB2API_ADMIN_EMAIL = os.environ.get("SUB2API_ADMIN_EMAIL", "")
SUB2API_ADMIN_PASSWORD = os.environ.get("SUB2API_ADMIN_PASSWORD", "")
SUB2API_PG_DSN = os.environ.get("SUB2API_PG_DSN", "")

# Backup directory for pre-cleanup snapshots (one per day)
BACKUP_DIR = os.environ.get("BACKUP_DIR", str(Path(__file__).resolve().parent.parent / "backups"))

# Which upstream API status codes trigger account DELETION (permanent, unrecoverable).
# 401 is now included — CPA already exhausted refresh attempts before surfacing 401,
#   so an upstream 401 means the token is permanently invalidated (token_invalidated).
# 500/502/503 are temporary server errors, NOT account problems.
DELETE_STATUSES = {401, 403}

# Status codes that should trigger DISABLE (temporary, kept around for future use).
DISABLE_STATUSES: set[int] = set()

# Plan types that should NEVER be disabled when hitting quota (429).
# Team plans get reset by OpenAI on their schedule, so leave them alone.
QUOTA_SKIP_PLAN_TYPES = ["team"]

# Maximum percentage of accounts that can be deleted in a single run (safety valve).
MAX_DELETE_RATIO = 0.30  # Abort if >30% of accounts would be deleted


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


def cpa_probe_account(name: str, auth_index: str = "", account_id: str = "") -> dict | None:
    """POST /v0/management/api-call to probe a single account's wham/usage.

    Returns the parsed JSON body, or None on failure.
    """
    payload = {
        "authIndex": auth_index or name,
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
        "quota_skip_plan_types": QUOTA_SKIP_PLAN_TYPES,  # Never disable team plans on 429
        "delete_401": True,  # 401 now means token_invalidated → delete
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
# Backup: snapshot channel data before cleanup (daily rotation)
# ---------------------------------------------------------------------------

def _ensure_backup_dir() -> Path:
    """Create backup directory if needed."""
    p = Path(BACKUP_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def backup_cpa_accounts(url: str, token: str) -> str | None:
    """Backup CPA auth-files list to a daily JSON file. Returns path or None."""
    try:
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.get(f"{url.rstrip('/')}/v0/management/auth-files", headers=hdrs, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        auth_files = data.get("files", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"  [Backup] Failed to list CPA {url}: {e}")
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    host = url.replace("https://", "").replace("http://", "").rstrip("/").replace("/", "_").replace(".", "_")
    backup_path = _ensure_backup_dir() / f"cpa_{host}_{date_str}.json"
    with open(backup_path, "w") as f:
        json.dump(auth_files, f, ensure_ascii=False, indent=2)
    print(f"  [Backup] CPA {url} -> {backup_path} ({len(auth_files)} accounts)")
    return str(backup_path)


def backup_sub2api_accounts() -> str | None:
    """Backup sub2api accounts to a daily JSON file. Returns path or None."""
    if not SUB2API_URL or not SUB2API_ADMIN_EMAIL or not SUB2API_ADMIN_PASSWORD:
        return None
    try:
        jwt = _sub2api_login()
        hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
        resp = requests.get(
            f"{SUB2API_URL.rstrip('/')}/api/v1/admin/accounts?page_size=100",
            headers=hdrs, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        items = data.get("items", [])
    except Exception as e:
        print(f"  [Backup] Failed to list sub2api: {e}")
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_path = _ensure_backup_dir() / f"sub2api_{date_str}.json"
    with open(backup_path, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"  [Backup] sub2api -> {backup_path} ({len(items)} accounts)")
    return str(backup_path)


def backup_grok_tokens() -> str | None:
    """Backup grok tokens to a daily JSON file. Returns path or None."""
    if not GROK_PG_DSN:
        return None
    try:
        conn = _grok_pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT token, pool_name, status, tags, created_at FROM tokens")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        print(f"  [Backup] Failed to read grok tokens: {e}")
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_path = _ensure_backup_dir() / f"grok_tokens_{date_str}.json"
    with open(backup_path, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    print(f"  [Backup] grok -> {backup_path} ({len(rows)} tokens)")
    return str(backup_path)


def cleanup_old_backups(max_days: int = 7) -> None:
    """Remove backup files older than max_days."""
    backup_dir = Path(BACKUP_DIR)
    if not backup_dir.exists():
        return
    cutoff = time.time() - max_days * 86400
    for f in backup_dir.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            print(f"  [Backup] Removed old backup: {f.name}")


# ---------------------------------------------------------------------------
# sub2api: health-check + cleanup codex accounts
# ---------------------------------------------------------------------------

def _sub2api_login() -> str:
    """Login to sub2api and return JWT token."""
    resp = requests.post(
        f"{SUB2API_URL.rstrip('/')}/api/v1/auth/login",
        json={"email": SUB2API_ADMIN_EMAIL, "password": SUB2API_ADMIN_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"sub2api login failed: {data.get('message', 'unknown')}")
    return data["data"]["access_token"]


def _sub2api_set_status(account_id: int, status: str, hdrs: dict) -> bool:
    """Set sub2api account status via PUT /api/v1/admin/accounts/{id}.

    `status` should be "active" or "inactive".
    Returns True on success.
    """
    base = SUB2API_URL.rstrip("/")
    try:
        resp = requests.put(
            f"{base}/api/v1/admin/accounts/{account_id}",
            headers=hdrs,
            json={"status": status},
            timeout=15,
        )
        return resp.status_code == 200 and resp.json().get("code") == 0
    except Exception:
        return False


def _sub2api_test_account(account_id: int, hdrs: dict) -> str:
    """Test a sub2api account via the test API (model gpt-5.4).

    Returns:
      "ok"  — account is healthy (or inconclusive)
      "401" — token permanently invalidated, should be deleted
      "429" — weekly quota limit reached, should be disabled
    """
    import random
    test_prompts = ["1+1", "hi", "ok?", "2+2", "hello", "thanks", "yes", "no",
                    "3*3", "good", "fine", "done", "next", "go", "cool"]
    base = SUB2API_URL.rstrip("/")
    try:
        resp = requests.post(
            f"{base}/api/v1/admin/accounts/{account_id}/test",
            headers=hdrs,
            json={"model_id": "gpt-5.4", "prompt": random.choice(test_prompts)},
            timeout=60,
        )
        body = resp.text
        for raw_line in body.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if raw_line.startswith("data: "):
                raw_line = raw_line[6:]
            elif raw_line.startswith("data:"):
                raw_line = raw_line[5:]
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "error":
                error_msg = event.get("error", "")
                if re.search(r'"status"\s*:\s*401', error_msg) or \
                   re.search(r'API returned 401', error_msg) or \
                   "token_invalidated" in error_msg:
                    return "401"
                if re.search(r'"status"\s*:\s*429', error_msg) or \
                   re.search(r'API returned 429', error_msg):
                    return "429"
                return "ok"  # other errors — don't delete
        return "ok"  # no error events = healthy
    except Exception:
        return "ok"  # network error — don't delete on uncertainty


def maintain_sub2api(dry_run: bool = False) -> dict:
    """Health-check and cleanup codex (openai oauth) accounts on sub2api.

    Phases:
      0. Dedup: remove duplicate names, keep newest
      1. Status sweep: disable already-rate-limited & delete already-401 accounts
         (uses the account's existing status/error_message — no probe needed)
      2. Cross-reference: delete sub2api accounts whose emails are NOT in any CPA instance
      3. Test API probe: test remaining accounts via sub2api test endpoint (gpt-5.4)
         - 401 (token_invalidated) → DELETE
         - 429 (quota limit reached) → DISABLE (status=inactive)
      4. Re-enable any active "codex free" account whose status is wrongly inactive
         (this is intentionally narrow — only re-enable if test reports "ok")
    Only touches openai/oauth accounts in "codex free" group.
    "codex team" accounts are NEVER touched.
    Returns stats dict.
    """
    if not SUB2API_URL or not SUB2API_ADMIN_EMAIL or not SUB2API_ADMIN_PASSWORD:
        print("  [Sub2API] Not configured, skipping")
        return {}

    try:
        jwt = _sub2api_login()
    except Exception as e:
        print(f"  [Sub2API] Login failed: {e}")
        return {"error": str(e)}

    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    base = SUB2API_URL.rstrip("/")

    # List all sub2api accounts (paginated — may have thousands due to duplicates)
    all_accounts: list[dict] = []
    try:
        page = 1
        while True:
            resp = requests.get(f"{base}/api/v1/admin/accounts?page={page}&page_size=100", headers=hdrs, timeout=30)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            items = data.get("items", [])
            all_accounts.extend(items)
            total = data.get("total", 0)
            if len(all_accounts) >= total or not items:
                break
            page += 1
    except Exception as e:
        print(f"  [Sub2API] List accounts failed: {e}")
        return {"error": str(e)}

    # Include both codex free AND codex team openai/oauth accounts.
    # Anthropic and any other groups are NEVER touched.
    def _account_group_name(a: dict) -> str:
        for g in a.get("groups", []):
            gn = str(g.get("name") or "").lower().strip()
            if gn in {"codex free", "codex team"}:
                return gn
        return ""

    codex_accounts = [
        a for a in all_accounts
        if a.get("platform") == "openai" and a.get("type") == "oauth"
        and _account_group_name(a) in {"codex free", "codex team"}
    ]
    free_count = sum(1 for a in codex_accounts if _account_group_name(a) == "codex free")
    team_count = sum(1 for a in codex_accounts if _account_group_name(a) == "codex team")
    print(f"  [Sub2API] Total accounts: {len(all_accounts)}, "
          f"Codex free: {free_count}, Codex team: {team_count}")

    if not codex_accounts:
        return {"total": len(all_accounts), "codex": 0, "dedup_deleted": 0, "cross_ref_deleted": 0,
                "test_deleted": 0, "quota_disabled": 0, "reenabled": 0, "deleted_ok": 0, "deleted_fail": 0}

    # --- Phase 0: Deduplicate — keep newest (highest id) per name, delete rest ---
    from collections import defaultdict
    name_groups: dict[str, list[dict]] = defaultdict(list)
    for acct in codex_accounts:
        acct_name = (acct.get("name") or "").lower().strip()
        if acct_name:
            name_groups[acct_name].append(acct)

    dedup_to_delete: list[dict] = []
    deduplicated_accounts: list[dict] = []
    for name, group in name_groups.items():
        if len(group) > 1:
            # Sort by id descending — keep the newest
            group.sort(key=lambda a: a.get("id", 0), reverse=True)
            deduplicated_accounts.append(group[0])  # keep newest
            for dup in group[1:]:
                dedup_to_delete.append({"id": dup["id"], "name": dup.get("name", ""), "reason": "duplicate"})
        else:
            deduplicated_accounts.append(group[0])

    dedup_deleted_ok = 0
    if dedup_to_delete:
        print(f"  [Sub2API] Dedup: {len(dedup_to_delete)} duplicates to remove (keeping newest per name)")
        if not dry_run:
            for ea in dedup_to_delete:
                try:
                    resp = requests.delete(f"{base}/api/v1/admin/accounts/{ea['id']}", headers=hdrs, timeout=15)
                    rdata = resp.json()
                    if rdata.get("code") == 0:
                        dedup_deleted_ok += 1
                except Exception:
                    pass
            print(f"  [Sub2API] Dedup deleted: {dedup_deleted_ok}/{len(dedup_to_delete)}")
    else:
        print(f"  [Sub2API] No duplicates found")

    # Use deduplicated list for subsequent phases
    codex_accounts = deduplicated_accounts

    # --- Phase 1: Status sweep (no probe needed) ---
    # Accounts with status=error and 401/token_invalidated in error_message → delete
    # Accounts already inactive → leave alone (stays disabled)
    status_401_to_delete: list[dict] = []
    for acct in codex_accounts:
        if acct.get("status") == "error":
            err = str(acct.get("error_message") or "")
            if "token_invalidated" in err or '"status": 401' in err or '"status":401' in err:
                status_401_to_delete.append({
                    "id": acct["id"], "name": acct.get("name", ""), "reason": "status_401",
                })
    print(f"  [Sub2API] Status sweep: {len(status_401_to_delete)} accounts already in 401 error state")

    # --- Phase 2: Cross-reference with CPA active emails ---
    cpa_urls = [u.strip() for u in CPA_BASE_URL.split(",") if u.strip()]
    cpa_active_emails: set[str] = set()
    for cpa_url in cpa_urls:
        try:
            cpa_hdrs = {"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"}
            r = requests.get(f"{cpa_url.rstrip('/')}/v0/management/auth-files", headers=cpa_hdrs, timeout=30)
            r.raise_for_status()
            files_data = r.json()
            files = files_data.get("files", files_data) if isinstance(files_data, dict) else files_data
            for f in files:
                if f.get("type") == "codex" and not f.get("disabled", False):
                    email = f.get("account") or f.get("email") or f.get("name", "")
                    if email:
                        cpa_active_emails.add(email.lower().strip())
        except Exception as e:
            print(f"  [Sub2API] Failed to list CPA {cpa_url}: {e}")

    print(f"  [Sub2API] CPA active codex emails: {len(cpa_active_emails)}")

    # Accounts in sub2api but not in any CPA instance → stale, delete.
    # Only apply to "codex free" accounts — team accounts may live only in sub2api.
    # Skip accounts already marked for deletion in Phase 1.
    phase1_ids = {ea["id"] for ea in status_401_to_delete}
    stale_accounts = []
    remaining_accounts = []
    for acct in codex_accounts:
        if acct.get("id") in phase1_ids:
            continue
        acct_email = (acct.get("name") or "").lower().strip()
        grp = _account_group_name(acct)
        if grp == "codex free" and cpa_active_emails and acct_email and acct_email not in cpa_active_emails:
            stale_accounts.append({"id": acct["id"], "name": acct.get("name", ""), "reason": "not_in_cpa"})
        else:
            remaining_accounts.append(acct)

    print(f"  [Sub2API] Stale (not in CPA): {len(stale_accounts)}, remaining for test: {len(remaining_accounts)}")

    # --- Phase 3: Test API probe (gpt-5.4) for remaining accounts ---
    # Uses sub2api's own test endpoint — no proxy needed, no direct chatgpt.com access
    # Team accounts: only delete 401; their 429 is expected to reset, so leave status alone.
    # Free accounts: delete 401; disable 429 (free quotas may stay at 0 for a long time).
    test_401_accounts: list[dict] = []   # delete (free + team)
    test_429_accounts: list[dict] = []   # disable (free only)
    test_ok_accounts: list[dict] = []    # candidate for re-enable if currently inactive
    for acct in remaining_accounts:
        acct_id = acct.get("id")
        acct_name = acct.get("name", str(acct_id))
        if not acct_id:
            continue
        grp = _account_group_name(acct)
        result = _sub2api_test_account(acct_id, hdrs)
        cur_status = acct.get("status", "")
        if result == "401":
            test_401_accounts.append({"id": acct_id, "name": acct_name, "reason": "test_401", "group": grp})
            print(f"    [Test] {acct_name} ({grp}): 401 token_invalidated → will delete")
        elif result == "429":
            if grp == "codex team":
                # Team quota resets automatically — leave it alone
                print(f"    [Test] {acct_name} (team): 429 quota limit → skip (will auto-reset)")
            elif cur_status != "inactive":
                test_429_accounts.append({"id": acct_id, "name": acct_name, "reason": "test_429"})
                print(f"    [Test] {acct_name} (free): 429 quota limit → will disable")
        else:
            # ok — if currently inactive, this is a candidate for re-enable
            if cur_status == "inactive":
                test_ok_accounts.append({"id": acct_id, "name": acct_name, "reason": "recovered"})

    print(f"  [Sub2API] Tested {len(remaining_accounts)}: "
          f"401={len(test_401_accounts)} 429={len(test_429_accounts)} "
          f"recoverable={len(test_ok_accounts)}")

    # --- Apply actions ---
    all_to_delete = status_401_to_delete + stale_accounts + test_401_accounts

    if dry_run:
        return {"total": len(all_accounts), "codex": len(codex_accounts),
                "dedup_deleted": len(dedup_to_delete),
                "cross_ref_deleted": len(stale_accounts),
                "test_deleted": len(test_401_accounts) + len(status_401_to_delete),
                "quota_disabled": len(test_429_accounts),
                "reenabled": len(test_ok_accounts),
                "deleted_ok": 0, "deleted_fail": 0}

    # Delete 401 + stale
    deleted_ok, deleted_fail = 0, 0
    for ea in all_to_delete:
        try:
            resp = requests.delete(f"{base}/api/v1/admin/accounts/{ea['id']}", headers=hdrs, timeout=15)
            rdata = resp.json()
            if rdata.get("code") == 0:
                deleted_ok += 1
                print(f"    Deleted: {ea['name']} ({ea['reason']})")
            else:
                deleted_fail += 1
        except Exception:
            deleted_fail += 1

    # Disable 429 (free tier exhausted quota)
    disabled_ok, disabled_fail = 0, 0
    for ea in test_429_accounts:
        if _sub2api_set_status(ea["id"], "inactive", hdrs):
            disabled_ok += 1
            print(f"    Disabled (429): {ea['name']}")
        else:
            disabled_fail += 1

    # Re-enable any that recovered (inactive → active when test reports ok)
    reenabled_ok = 0
    for ea in test_ok_accounts:
        if _sub2api_set_status(ea["id"], "active", hdrs):
            reenabled_ok += 1
            print(f"    Re-enabled: {ea['name']}")

    print(f"  [Sub2API] Deleted: ok={deleted_ok} fail={deleted_fail}  "
          f"Disabled: ok={disabled_ok} fail={disabled_fail}  Re-enabled: {reenabled_ok}")
    return {
        "total": len(all_accounts),
        "codex": len(codex_accounts),
        "dedup_deleted": dedup_deleted_ok,
        "cross_ref_deleted": len(stale_accounts),
        "test_deleted": len(test_401_accounts) + len(status_401_to_delete),
        "quota_disabled": disabled_ok,
        "quota_disable_fail": disabled_fail,
        "reenabled": reenabled_ok,
        "deleted_ok": deleted_ok,
        "deleted_fail": deleted_fail,
    }


# ---------------------------------------------------------------------------
# Cross-sync: validated CPA accounts → other CPA instances + sub2api
# ---------------------------------------------------------------------------

def sync_validated_accounts(dry_run: bool = False) -> dict:
    """Sync validated auth files across CPA instances and to sub2api.

    After cleanup, CPA instances may have different account sets. This function:
      1. Reads active account lists from each CPA instance
      2. Gets full auth file content from the GitHub repo for accounts missing in each target
      3. Uploads validated accounts to target CPA instances
      4. Syncs to sub2api

    Returns stats dict.
    """
    cpa_urls = [u.strip() for u in CPA_BASE_URL.split(",") if u.strip()]
    if len(cpa_urls) < 2 and not SUB2API_URL:
        print("  [Sync] Only one CPA target and no sub2api, skipping")
        return {}

    # Collect active account names+emails from each CPA instance, with validation
    cpa_accounts: dict[str, dict[str, set]] = {}  # url → {"names": set, "emails": set}
    for url in cpa_urls:
        try:
            hdrs = {"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"}
            resp = requests.get(f"{url.rstrip('/')}/v0/management/auth-files", headers=hdrs, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files", data) if isinstance(data, dict) else data
            active = [f for f in files if f.get("type") == "codex" and not f.get("disabled", False)]

            # Validate each active account via wham/usage probe
            validated = []
            invalid_names = []
            for af in active:
                name = af.get("name", "")
                auth_index = af.get("auth_index", "")
                account_id = (af.get("id_token") or {}).get("chatgpt_account_id", "")
                if not name:
                    continue

                # Try CPA api-call probe if auth_index + account_id available
                if auth_index and account_id:
                    payload = {
                        "authIndex": auth_index,
                        "method": "GET",
                        "url": "https://chatgpt.com/backend-api/wham/usage",
                        "header": {
                            "Authorization": "Bearer $TOKEN$",
                            "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
                            "Chatgpt-Account-Id": account_id,
                        },
                    }
                    try:
                        pr = requests.post(f"{url.rstrip('/')}/v0/management/api-call", headers=hdrs, json=payload, timeout=20)
                        if pr.status_code == 200:
                            pr_data = pr.json()
                            status = pr_data.get("status_code", 0)
                            if status in DELETE_STATUSES:  # Only 403 (permanent)
                                invalid_names.append(name)
                                continue
                            # 401 = token expired, don't delete — just skip validation
                            # 500/502/503 = temporary, trust CPA's active status
                    except Exception:
                        pass
                # If we can't probe or probe returned 401/5xx, trust the CPA's active status
                validated.append(af)

            # Delete only truly invalid accounts (403)
            for inv_name in invalid_names:
                if not dry_run:
                    encoded = urllib.parse.quote(inv_name, safe="")
                    requests.delete(f"{url.rstrip('/')}/v0/management/auth-files?name={encoded}", headers=hdrs, timeout=15)
            if invalid_names:
                print(f"  [Sync] {url}: validated {len(validated)}, removed {len(invalid_names)} invalid (403 only)")

            names = {f.get("name", "") for f in validated if f.get("name")}
            emails = {(f.get("email") or f.get("account") or "").lower().strip() for f in validated}
            emails.discard("")
            cpa_accounts[url] = {"names": names, "emails": emails}
            print(f"  [Sync] {url}: {len(names)} validated codex")
        except Exception as e:
            print(f"  [Sync] Failed to list {url}: {e}")
            cpa_accounts[url] = {"names": set(), "emails": set()}

    # Build union of all active names across all CPA instances
    all_active_names: set[str] = set()
    for info in cpa_accounts.values():
        all_active_names |= info["names"]

    if not all_active_names:
        print("  [Sync] No active accounts found, skipping")
        return {"synced_cpa": 0, "synced_sub2api": 0}

    # Get list of auth files from GitHub repo
    if not GH_TOKEN:
        print("  [Sync] GH_TOKEN not set, skipping GitHub-based sync")
        return {"synced_cpa": 0, "synced_sub2api": 0}

    gh_files = gh_list_auth_files()  # {name: sha}
    print(f"  [Sync] GitHub repo: {len(gh_files)} auth files")

    # For each CPA instance, find accounts missing from it but present in another
    total_synced_cpa = 0
    for target_url in cpa_urls:
        target_names = cpa_accounts[target_url]["names"]
        # Accounts active in OTHER instances but not in this one
        missing = all_active_names - target_names
        # Only sync files that exist in GitHub
        to_sync = [name for name in missing if name in gh_files]

        if not to_sync:
            continue

        print(f"  [Sync] {target_url}: {len(to_sync)} accounts to sync from GitHub")
        if dry_run:
            continue

        synced = 0
        for name in to_sync[:200]:  # Cap at 200 per run
            try:
                # Read auth file content from GitHub
                file_url = f"https://api.github.com/repos/{GIT_REPO}/contents/{GIT_AUTH_DIR}/{urllib.parse.quote(name, safe='')}"
                resp = requests.get(file_url, headers=gh_headers(), params={"ref": GIT_BRANCH}, timeout=15)
                if resp.status_code != 200:
                    continue
                content = base64.b64decode(resp.json()["content"]).decode("utf-8")

                # Upload to target CPA
                encoded = urllib.parse.quote(name, safe="")
                upload_url = f"{target_url.rstrip('/')}/v0/management/auth-files?name={encoded}"
                up_resp = requests.post(
                    upload_url,
                    headers={"Authorization": f"Bearer {CPA_TOKEN}", "Content-Type": "application/json"},
                    data=content, timeout=15,
                )
                if up_resp.status_code == 200:
                    synced += 1
            except Exception:
                pass

        print(f"  [Sync] {target_url}: synced {synced}/{len(to_sync)}")
        total_synced_cpa += synced

    # NOTE: Sub2API cross-sync is intentionally NOT performed here.
    #   After the free-account purge, all CPA accounts are `team`, but this function
    #   has no cheap way to distinguish team vs free before upload. Blind uploads
    #   would land in the wrong sub2api group (codex free instead of codex team)
    #   and would be immediately re-deleted by the next maintenance run.
    #
    #   Sub2API team accounts are managed separately by the gpt-team heartbeat
    #   (uploads from its own DB to the correct group). Free accounts, if ever
    #   restored, should be pushed via `scripts/restore_free_accounts.py`.
    return {"synced_cpa": total_synced_cpa, "synced_sub2api": 0}


# ---------------------------------------------------------------------------
# Extended cleanup: delete 5xx accounts + Git sync
# ---------------------------------------------------------------------------

def find_and_delete_error_accounts(auth_files: list[dict], dry_run: bool = False, base_url: str = "", token: str = "") -> dict:
    """Probe accounts and delete those returning permanent errors (403).

    401 (token_invalidated) is now handled by cpa_warden's delete_401 path —
    we skip it here to avoid duplicate work. 5xx is treated as transient.

    Safety valve: abort deletion if >MAX_DELETE_RATIO of accounts would be affected.

    Returns {"probed": N, "deleted_ok": N, "deleted_fail": N, "names": [...]}.
    """
    _url = (base_url or CPA_BASE_URL).rstrip("/")
    _tok = token or CPA_TOKEN
    hdrs = {"Authorization": f"Bearer {_tok}", "Content-Type": "application/json"}

    candidates = [
        f for f in auth_files
        if f.get("type", "") == "codex" and not f.get("disabled", False)
    ]

    to_delete = []     # 403: permanently forbidden
    skipped_5xx = 0    # 500/502/503: temporary server errors
    skipped_401 = 0    # handled by cpa_warden delete_401 path

    for af in candidates:
        name = af.get("name", "")
        auth_index = af.get("auth_index", "")
        account_id = (af.get("id_token") or {}).get("chatgpt_account_id", "")
        if not name or not auth_index:
            continue
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
            resp = requests.post(f"{_url}/v0/management/api-call", headers=hdrs, json=payload, timeout=20)
            upstream_status = resp.status_code
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    upstream_status = data.get("status_code", resp.status_code)
                except Exception:
                    pass
            if upstream_status == 403:
                to_delete.append({"name": name, "status": upstream_status})
            elif upstream_status == 401:
                skipped_401 += 1  # cpa_warden handles deletion
            elif upstream_status in {500, 502, 503}:
                skipped_5xx += 1
        except Exception:
            pass

    print(f"    Probed {len(candidates)}: {len(to_delete)} to delete (403), "
          f"{skipped_401} 401 (handled by warden), {skipped_5xx} skipped (5xx)")

    # Safety valve: abort DELETION if too many accounts would be deleted
    if candidates and len(to_delete) > 0 and len(to_delete) / len(candidates) > MAX_DELETE_RATIO:
        print(f"    SAFETY VALVE: {len(to_delete)}/{len(candidates)} ({len(to_delete)/len(candidates):.0%}) "
              f"deletions exceeds {MAX_DELETE_RATIO:.0%} threshold, aborting deletions")
        to_delete = []

    if dry_run:
        return {"probed": len(candidates), "deleted_ok": 0, "deleted_fail": 0, "names": []}

    # Delete only truly forbidden accounts (403)
    deleted_ok, deleted_fail = 0, 0
    deleted_names = []
    for ea in to_delete:
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

    return {"probed": len(candidates), "deleted_ok": deleted_ok, "deleted_fail": deleted_fail,
            "names": deleted_names}


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

def send_feishu(warden_stats: dict, error_stats: dict, git_stats: dict, grok_stats: dict | None = None, sub2api_stats: dict | None = None, sync_stats: dict | None = None) -> None:
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

    if sub2api_stats and not sub2api_stats.get("error"):
        s2a_del_ok = sub2api_stats.get("deleted_ok", 0)
        s2a_del_fail = sub2api_stats.get("deleted_fail", 0)
        s2a_dedup = sub2api_stats.get("dedup_deleted", 0)
        s2a_xref = sub2api_stats.get("cross_ref_deleted", 0)
        s2a_test = sub2api_stats.get("test_deleted", 0)
        s2a_disable = sub2api_stats.get("quota_disabled", 0)
        s2a_disable_fail = sub2api_stats.get("quota_disable_fail", 0)
        s2a_reenable = sub2api_stats.get("reenabled", 0)
        if s2a_del_fail > 0 or s2a_disable_fail > 0:
            all_ok = False
        lines.extend([
            f"━━━━━━━━━━━━━━━━━━━━",
            f"📍 Sub2API — Codex Cleanup ({SUB2API_URL})",
            f"   📦 Total: {sub2api_stats.get('total', 0)}  |  🎯 Codex free: {sub2api_stats.get('codex', 0)}",
            f"   🔄 Dedup: {s2a_dedup}  |  🔗 Stale: {s2a_xref}  |  🚫 401: {s2a_test}",
            f"   ⏸️ Disabled (429): ✅ {s2a_disable}  ❌ {s2a_disable_fail}  |  ▶️ Re-enabled: {s2a_reenable}",
            f"   🗑️ Deleted: ✅ {s2a_del_ok}  ❌ {s2a_del_fail}",
        ])

    if git_stats:
        g_synced = git_stats.get("synced", 0)
        g_skipped = git_stats.get("skipped", 0)
        g_not_in_git = git_stats.get("not_in_git", 0)
        if g_synced + g_skipped + g_not_in_git > 0:
            lines.extend([
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📍 Git Sync — Repo Cleanup",
                f"   🗑️ Removed: {g_synced}  |  Skipped: {g_skipped}  |  Not in Git: {g_not_in_git}",
            ])

    if sync_stats:
        s_cpa = sync_stats.get("synced_cpa", 0)
        s_sub = sync_stats.get("synced_sub2api", 0)
        if s_cpa + s_sub > 0:
            lines.extend([
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📍 Cross-Sync — Validated Accounts",
                f"   ↗️ CPA: {s_cpa}  |  Sub2API: {s_sub}",
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

    # Step 0: Backup all channel data before cleanup
    print("[Step 0] Backing up account data...")
    for url in cpa_urls:
        backup_cpa_accounts(url, CPA_TOKEN)
    backup_sub2api_accounts()
    backup_grok_tokens()
    cleanup_old_backups(max_days=7)
    print()

    # Step 1-2: CPA maintenance for each instance
    all_warden: dict = {}
    all_error: dict = {"probed": 0, "deleted_ok": 0, "deleted_fail": 0}
    all_deleted_names: list[str] = []  # Track all deleted names for Git sync

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
            all_deleted_names.extend(warden_stats.get("deleted_401_names", []))
        all_error["probed"] += error_stats.get("probed", 0)
        all_error["deleted_ok"] += error_stats.get("deleted_ok", 0)
        all_error["deleted_fail"] += error_stats.get("deleted_fail", 0)
        all_deleted_names.extend(error_stats.get("names", []))
        print()

    # Step 3: Grok token maintenance
    print("[Step 3] Grok token maintenance...")
    grok_stats = maintain_grok_tokens(dry_run=args.dry_run)
    if grok_stats:
        print(f"  Before: {grok_stats['total_before']} | Expired deleted: {grok_stats['expired_deleted']}")
        print(f"  Disabled deleted: {grok_stats['disabled_deleted']} | Migrated to ssoBasic: {grok_stats['migrated_to_ssoBasic']}")
        print(f"  NSFW enabled: {grok_stats['nsfw_enabled']} | Active after: {grok_stats['active_after']}")

    # Step 4: Sub2API codex cleanup
    print("\n[Step 4] Sub2API codex cleanup...")
    sub2api_stats = maintain_sub2api(dry_run=args.dry_run)
    if sub2api_stats and not sub2api_stats.get("error"):
        print(f"  Total: {sub2api_stats.get('total', 0)} | Codex: {sub2api_stats.get('codex', 0)}")
        print(f"  Deleted: ok={sub2api_stats.get('deleted_ok', 0)} fail={sub2api_stats.get('deleted_fail', 0)}")

    # Step 5: Git sync — remove deleted auth files from GitHub repo
    print("\n[Step 5] Git sync (delete from repo)...")
    unique_deleted = list(set(all_deleted_names))
    git_stats = sync_git_deletions(unique_deleted, dry_run=args.dry_run)

    # Step 6: Cross-sync validated accounts across CPA instances + sub2api
    print("\n[Step 6] Cross-sync validated accounts...")
    sync_stats = sync_validated_accounts(dry_run=args.dry_run)
    if sync_stats:
        print(f"  CPA synced: {sync_stats.get('synced_cpa', 0)} | Sub2API synced: {sync_stats.get('synced_sub2api', 0)}")

    # Step 7: Feishu notification
    print("\n[Step 7] Sending Feishu notification...")
    if not args.dry_run:
        send_feishu(all_warden, all_error, git_stats, grok_stats, sub2api_stats, sync_stats)
    else:
        print("  [DRY-RUN] Skipping Feishu notification")

    print(f"\n=== Done ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Scheduled CPA maintenance runner for GitHub Actions.

Reads CPA_INSTANCES from environment (JSON array), runs scan+maintain
on each instance. Optionally cleans expired grok tokens and enables NSFW.

Environment variables:
  CPA_INSTANCES: JSON array of {"name", "url", "token"} objects.
  GROK_PG_DSN: PostgreSQL DSN for grok2api (optional).
  GROK_TOKEN_MAX_AGE_H: Max token age in hours (default: 48).
  GITHUB_OUTPUT: GitHub Actions output file path.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run_instance(instance: dict) -> dict:
    """Run cpa_warden maintain on a single CPA instance."""
    name = instance.get("name", "unnamed")
    url = instance.get("url", "")
    token = instance.get("token", "")

    if not url or not token:
        return {
            "name": name,
            "url": url,
            "success": False,
            "error": "Missing url or token",
            "total": 0,
            "filtered": 0,
            "invalid_401": 0,
            "quota_limited": 0,
            "recovered": 0,
            "delete_401_ok": 0,
            "delete_401_fail": 0,
            "quota_action_ok": 0,
            "quota_action_fail": 0,
            "reenable_ok": 0,
            "reenable_fail": 0,
        }

    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = os.path.join(tmpdir, "config.json")
        db_path = os.path.join(tmpdir, "state.sqlite3")
        invalid_path = os.path.join(tmpdir, "invalid.json")
        quota_path = os.path.join(tmpdir, "quota.json")
        log_path = os.path.join(tmpdir, "run.log")

        config = {
            "base_url": url,
            "token": token,
            "target_type": "codex",
            "probe_workers": 40,
            "action_workers": 20,
            "timeout": 20,
            "retries": 2,
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
        try:
            proc = subprocess.run(
                [
                    sys.executable, "cpa_warden.py",
                    "--mode", "maintain",
                    "--config", config_path,
                    "--yes",
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            elapsed = round(time.monotonic() - start, 1)
            output = proc.stdout + proc.stderr

            result = {
                "name": name,
                "url": mask_url(url),
                "success": proc.returncode == 0,
                "error": "" if proc.returncode == 0 else f"exit code {proc.returncode}",
                "elapsed_seconds": elapsed,
            }

            result.update(parse_log_stats(log_path))

            if proc.returncode != 0:
                tail_lines = output.strip().split("\n")[-10:]
                result["error_detail"] = "\n".join(tail_lines)

            return result

        except subprocess.TimeoutExpired:
            return {
                "name": name,
                "url": mask_url(url),
                "success": False,
                "error": "Timeout (600s)",
                "elapsed_seconds": 600,
                "total": 0,
                "filtered": 0,
                "invalid_401": 0,
                "quota_limited": 0,
                "recovered": 0,
            }
        except Exception as exc:
            return {
                "name": name,
                "url": mask_url(url),
                "success": False,
                "error": str(exc),
                "total": 0,
                "filtered": 0,
                "invalid_401": 0,
                "quota_limited": 0,
                "recovered": 0,
            }


def parse_log_stats(log_path: str) -> dict:
    """Parse key statistics from the cpa_warden log file."""
    stats = {
        "total": 0,
        "filtered": 0,
        "invalid_401": 0,
        "quota_limited": 0,
        "recovered": 0,
        "delete_401_ok": 0,
        "delete_401_fail": 0,
        "quota_action_ok": 0,
        "quota_action_fail": 0,
        "reenable_ok": 0,
        "reenable_fail": 0,
        "timeout_count": 0,
        "failure_count": 0,
    }

    try:
        log_content = Path(log_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return stats

    for line in log_content.split("\n"):
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
            ok, fail = extract_action_result(line)
            stats["delete_401_ok"] = ok
            stats["delete_401_fail"] = fail
        elif "处理限额:" in line:
            ok, fail = extract_action_result(line)
            stats["quota_action_ok"] = ok
            stats["quota_action_fail"] = fail
        elif "恢复启用:" in line:
            ok, fail = extract_action_result(line)
            stats["reenable_ok"] = ok
            stats["reenable_fail"] = fail
        elif "请求超时:" in line:
            stats["timeout_count"] = extract_int(line, "请求超时:")

    return stats


def extract_int(line: str, prefix: str) -> int:
    try:
        idx = line.index(prefix) + len(prefix)
        num_str = ""
        for ch in line[idx:].strip():
            if ch.isdigit():
                num_str += ch
            else:
                break
        return int(num_str) if num_str else 0
    except (ValueError, IndexError):
        return 0


def extract_action_result(line: str) -> tuple[int, int]:
    ok = 0
    fail = 0
    if "成功=" in line:
        ok = extract_int(line, "成功=")
    if "失败=" in line:
        fail = extract_int(line, "失败=")
    return ok, fail


def mask_url(url: str) -> str:
    """Remove protocol for display without masking the hostname."""
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def maintain_grok_tokens() -> dict | None:
    """Clean expired grok tokens and enable NSFW. Returns stats or None."""
    dsn = os.environ.get("GROK_PG_DSN", "")
    if not dsn:
        return None

    max_age_h = int(os.environ.get("GROK_TOKEN_MAX_AGE_H", "48"))

    try:
        import psycopg2
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "psycopg2-binary", "-q"], check=True)
        import psycopg2

    parsed = urlparse(dsn)
    host = parsed.hostname
    kwargs = {"connect_timeout": 15}
    if host:
        try:
            ipv4 = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
            kwargs["hostaddr"] = ipv4
        except socket.gaierror:
            pass

    conn = psycopg2.connect(dsn, **kwargs)
    cur = conn.cursor()
    now_ts = int(time.time())
    cutoff_ts = now_ts - max_age_h * 3600

    cur.execute("SELECT count(*) FROM tokens")
    total_before = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM tokens WHERE created_at > 0 AND created_at < %s", (cutoff_ts,))
    expired_count = cur.fetchone()[0]

    cur.execute("SELECT count(*) FROM tokens WHERE status = 'disabled'")
    disabled_count = cur.fetchone()[0]

    # Delete expired
    cur.execute("DELETE FROM tokens WHERE created_at > 0 AND created_at < %s", (cutoff_ts,))
    # Delete disabled
    cur.execute("DELETE FROM tokens WHERE status = 'disabled'")
    # Migrate default → ssoBasic
    cur.execute("UPDATE tokens SET pool_name = 'ssoBasic', status = 'active', tags = '[\"nsfw\"]' WHERE pool_name = 'default'")
    migrated = cur.rowcount
    # Enable NSFW
    cur.execute("UPDATE tokens SET tags = '[\"nsfw\"]' WHERE pool_name = 'ssoBasic' AND (tags IS NULL OR tags = '[]' OR tags NOT LIKE '%%nsfw%%')")
    nsfw_fixed = cur.rowcount

    conn.commit()

    cur.execute("SELECT count(*) FROM tokens WHERE status IN ('active', 'normal')")
    active_after = cur.fetchone()[0]
    conn.close()

    return {
        "total_before": total_before,
        "expired_deleted": expired_count,
        "disabled_deleted": disabled_count,
        "migrated": migrated,
        "nsfw_enabled": nsfw_fixed,
        "active_after": active_after,
    }


def maintain_sub2api() -> dict | None:
    """Health-check and cleanup codex (openai/oauth) accounts on sub2api.

    Two-phase approach (CI version — GitHub runners can reach chatgpt.com directly):
      1. Cross-reference: delete sub2api accounts not in any CPA instance
      2. Direct probe: check remaining accounts via wham/usage
    Returns stats dict or None if not configured.
    """
    base_url = os.environ.get("SUB2API_URL", "")
    admin_email = os.environ.get("SUB2API_ADMIN_EMAIL", "")
    admin_password = os.environ.get("SUB2API_ADMIN_PASSWORD", "")
    if not base_url or not admin_email or not admin_password:
        return None

    import requests

    base_url = base_url.rstrip("/")

    # Login
    resp = requests.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": admin_email, "password": admin_password},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"sub2api login failed: {data.get('message', 'unknown')}")
    jwt = data["data"]["access_token"]

    hdrs = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

    # List accounts
    resp = requests.get(f"{base_url}/api/v1/admin/accounts?page_size=500", headers=hdrs, timeout=30)
    resp.raise_for_status()
    all_accounts = resp.json().get("data", {}).get("items", [])

    codex_accounts = [a for a in all_accounts if a.get("platform") == "openai" and a.get("type") == "oauth"]

    if not codex_accounts:
        return {"total": len(all_accounts), "codex": 0, "cross_ref_deleted": 0, "probe_deleted": 0, "deleted_ok": 0, "deleted_fail": 0}

    # --- Phase 1: Cross-reference with CPA ---
    raw_instances = os.environ.get("CPA_INSTANCES", "")
    cpa_active_emails: set = set()
    if raw_instances:
        try:
            instances = json.loads(raw_instances)
            for inst in instances:
                try:
                    cpa_hdrs = {"Authorization": f"Bearer {inst['token']}", "Content-Type": "application/json"}
                    r = requests.get(f"{inst['url'].rstrip('/')}/v0/management/auth-files", headers=cpa_hdrs, timeout=30)
                    r.raise_for_status()
                    files_data = r.json()
                    files = files_data.get("files", files_data) if isinstance(files_data, dict) else files_data
                    for f in files:
                        if f.get("type") == "codex" and not f.get("disabled", False):
                            email = f.get("account") or f.get("email") or f.get("name", "")
                            if email:
                                cpa_active_emails.add(email.lower().strip())
                except Exception as e:
                    print(f"  Failed to list CPA {inst.get('name', '?')}: {e}")
        except json.JSONDecodeError:
            pass

    stale_accounts = []
    remaining_accounts = []
    for acct in codex_accounts:
        acct_email = (acct.get("name") or "").lower().strip()
        if cpa_active_emails and acct_email and acct_email not in cpa_active_emails:
            stale_accounts.append({"id": acct["id"], "name": acct.get("name", ""), "reason": "not_in_cpa"})
        else:
            remaining_accounts.append(acct)

    # --- Phase 2: Direct probe (GitHub runners not Cloudflare-blocked) ---
    delete_statuses = {401, 403, 500, 502, 503}
    probe_error_accounts = []
    probe_timeout_count = 0
    for acct in remaining_accounts:
        creds = acct.get("credentials", {})
        access_token = creds.get("access_token", "")
        if not access_token:
            continue
        try:
            probe_resp = requests.get(
                "https://chatgpt.com/backend-api/wham/usage",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
                },
                timeout=20,
            )
            if probe_resp.status_code in delete_statuses:
                probe_error_accounts.append({"id": acct["id"], "name": acct.get("name", ""), "reason": f"probe_{probe_resp.status_code}"})
        except requests.exceptions.Timeout:
            probe_timeout_count += 1
        except Exception:
            pass

    if probe_timeout_count > 0:
        print(f"  Sub2API probe: {probe_timeout_count} timeouts")

    # --- Delete ---
    all_to_delete = stale_accounts + probe_error_accounts
    deleted_ok, deleted_fail = 0, 0
    for ea in all_to_delete:
        try:
            resp = requests.delete(f"{base_url}/api/v1/admin/accounts/{ea['id']}", headers=hdrs, timeout=15)
            if resp.json().get("code") == 0:
                deleted_ok += 1
            else:
                deleted_fail += 1
        except Exception:
            deleted_fail += 1

    return {
        "total": len(all_accounts),
        "codex": len(codex_accounts),
        "cross_ref_deleted": len(stale_accounts),
        "probe_deleted": len(probe_error_accounts),
        "deleted_ok": deleted_ok,
        "deleted_fail": deleted_fail,
    }


def main() -> int:
    raw = os.environ.get("CPA_INSTANCES", "")
    if not raw:
        print("ERROR: CPA_INSTANCES environment variable is not set.", file=sys.stderr)
        print("Please configure it as a GitHub repository secret.", file=sys.stderr)
        return 1

    try:
        instances = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: CPA_INSTANCES is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(instances, list) or len(instances) == 0:
        print("ERROR: CPA_INSTANCES must be a non-empty JSON array.", file=sys.stderr)
        return 1

    timestamp = utc_now_iso()
    print(f"CPA Warden Scheduled Maintenance - {timestamp}")
    print(f"Instances to check: {len(instances)}")
    print("=" * 60)

    results = []
    all_success = True

    for i, instance in enumerate(instances, 1):
        name = instance.get("name", f"instance-{i}")
        print(f"\n[{i}/{len(instances)}] Processing: {name}")
        print("-" * 40)

        result = run_instance(instance)
        results.append(result)

        if result["success"]:
            print(f"  Total: {result['total']} | Filtered: {result['filtered']}")
            print(f"  401: {result['invalid_401']} | Quota: {result['quota_limited']} | Recovered: {result['recovered']}")
            if result.get("elapsed_seconds"):
                print(f"  Elapsed: {result['elapsed_seconds']}s")
        else:
            all_success = False
            print(f"  FAILED: {result['error']}")

    print("\n" + "=" * 60)
    success_count = sum(1 for r in results if r["success"])
    print(f"Results: {success_count}/{len(results)} succeeded")

    # Grok token maintenance
    grok_result = None
    if os.environ.get("GROK_PG_DSN"):
        print("\n" + "=" * 60)
        print("Grok token maintenance...")
        try:
            grok_result = maintain_grok_tokens()
            if grok_result:
                print(f"  Before: {grok_result['total_before']} -> Active: {grok_result['active_after']}")
                print(f"  Expired: {grok_result['expired_deleted']} | Disabled: {grok_result['disabled_deleted']}")
                print(f"  Migrated: {grok_result['migrated']} | NSFW: {grok_result['nsfw_enabled']}")
        except Exception as exc:
            print(f"  Grok maintenance failed: {exc}")

    # Sub2API codex cleanup
    sub2api_result = None
    if os.environ.get("SUB2API_URL"):
        print("\n" + "=" * 60)
        print("Sub2API codex cleanup...")
        try:
            sub2api_result = maintain_sub2api()
            if sub2api_result:
                print(f"  Total: {sub2api_result['total']} | Codex: {sub2api_result['codex']}")
                print(f"  Probed: {sub2api_result['probed']} | Deleted: ok={sub2api_result['deleted_ok']} fail={sub2api_result['deleted_fail']}")
        except Exception as exc:
            print(f"  Sub2API maintenance failed: {exc}")

    # Write output for notification step
    output_data = {
        "timestamp": timestamp,
        "results": results,
        "grok": grok_result,
        "sub2api": sub2api_result,
        "success_count": success_count,
        "total_count": len(results),
        "all_success": all_success,
    }

    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            json_str = json.dumps(output_data, ensure_ascii=False)
            f.write(f"result={json_str}\n")

    return 0 if all_success else 1


if __name__ == "__main__":
    raise SystemExit(main())

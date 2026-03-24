#!/usr/bin/env python3
"""Send CPA Warden maintenance results to Feishu via webhook.

Sends a rich interactive card message with Claude-style aesthetics.

Environment variables:
  FEISHU_WEBHOOK: Feishu bot webhook URL.
  SCAN_RESULT: JSON string with scan results from scheduled_maintain.py.
  SCAN_STATUS: "success" or "failure" from GitHub Actions step outcome.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import requests


def build_card(result_data: dict | None, scan_status: str) -> dict:
    """Build a Feishu interactive card message."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if result_data is None:
        # Fallback when no scan result is available
        return build_error_card(timestamp, scan_status)

    results = result_data.get("results", [])
    all_success = result_data.get("all_success", False)
    success_count = result_data.get("success_count", 0)
    total_count = result_data.get("total_count", 0)
    run_timestamp = result_data.get("timestamp", timestamp)

    # Header color
    header_color = "green" if all_success else "red"
    header_icon = "✅" if all_success else "🔴"

    elements = []

    # Timestamp section
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"🕐 Execution Time: {run_timestamp}",
        },
    })

    elements.append({"tag": "hr"})

    # Overall summary
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": f"📊 Overall: {success_count}/{total_count} instances succeeded",
        },
    })

    elements.append({"tag": "hr"})

    # Per-instance results
    for r in results:
        name = r.get("name", "unknown")
        url = r.get("url", "")
        success = r.get("success", False)

        status_icon = "✅" if success else "❌"
        status_text = "Success" if success else "Failed"

        if success:
            total = r.get("total", 0)
            filtered = r.get("filtered", 0)
            invalid_401 = r.get("invalid_401", 0)
            quota_limited = r.get("quota_limited", 0)
            recovered = r.get("recovered", 0)
            elapsed = r.get("elapsed_seconds", 0)

            delete_ok = r.get("delete_401_ok", 0)
            delete_fail = r.get("delete_401_fail", 0)
            quota_ok = r.get("quota_action_ok", 0)
            quota_fail = r.get("quota_action_fail", 0)
            reenable_ok = r.get("reenable_ok", 0)
            reenable_fail = r.get("reenable_fail", 0)

            active_count = filtered - invalid_401 - quota_limited

            content_lines = [
                f"{status_icon} {name}  {url}",
                f"━━━━━━━━━━━━━━━━━━━━",
                f"📍 Scan Results",
                f"   📦 Total: {total}  |  🎯 Filtered: {filtered}",
                f"   ✅ Active: {active_count}  |  🚫 401: {invalid_401}  |  ⚠️ Quota: {quota_limited}",
                f"   🔄 Recovered: {recovered}",
            ]

            # Always show 401 deletion status so the report makes it explicit
            has_actions = (delete_ok + delete_fail + quota_ok + quota_fail + reenable_ok + reenable_fail) > 0
            content_lines.append(f"📍 Maintenance Actions")
            content_lines.append(f"   🗑️ Delete 401: ✅ {delete_ok}  ❌ {delete_fail}")
            if quota_ok + quota_fail > 0:
                content_lines.append(f"   ⏸️ Quota Action: ✅ {quota_ok}  ❌ {quota_fail}")
            if reenable_ok + reenable_fail > 0:
                content_lines.append(f"   ▶️ Re-enable: ✅ {reenable_ok}  ❌ {reenable_fail}")

            content_lines.append(f"━━━━━━━━━━━━━━━━━━━━")

            if invalid_401 == 0 and quota_limited == 0:
                content_lines.append(f"ℹ️  All accounts healthy, no maintenance needed")
            elif has_actions:
                content_lines.append(f"⏱️  Completed in {elapsed}s")
            else:
                content_lines.append(f"⏱️  Scanned in {elapsed}s")

        else:
            error = r.get("error", "Unknown error")
            content_lines = [
                f"{status_icon} {name}  {url}",
                f"━━━━━━━━━━━━━━━━━━━━",
                f"❌ Status: {status_text}",
                f"📝 Error: {error}",
                f"━━━━━━━━━━━━━━━━━━━━",
            ]

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(content_lines),
            },
        })

        elements.append({"tag": "hr"})

    # Grok section
    grok = result_data.get("grok")
    if grok:
        grok_lines = [
            "🎮 Grok Token Maintenance",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"   📦 Before: {grok.get('total_before', 0)}  →  Active: {grok.get('active_after', 0)}",
            f"   🗑️ Expired: {grok.get('expired_deleted', 0)}  |  Disabled: {grok.get('disabled_deleted', 0)}",
            f"   🔄 Migrated: {grok.get('migrated', 0)}  |  NSFW: {grok.get('nsfw_enabled', 0)}",
            f"━━━━━━━━━━━━━━━━━━━━",
        ]
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(grok_lines)},
        })
        elements.append({"tag": "hr"})

    # Footer
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "lark_md",
                "content": "🛡️ CPA Warden  |  Automated Maintenance Report",
            }
        ],
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{header_icon} CPA Warden Maintenance Report",
                },
                "template": header_color,
            },
            "elements": elements,
        },
    }

    return card


def build_error_card(timestamp: str, scan_status: str) -> dict:
    """Build an error notification card when scan results are unavailable."""
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🔴 CPA Warden Maintenance Failed",
                },
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"🕐 Time: {timestamp}\n\n"
                            f"❌ Status: {scan_status}\n\n"
                            "The scheduled maintenance job failed to produce results.\n"
                            "Please check the GitHub Actions logs for details."
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "lark_md",
                            "content": "🛡️ CPA Warden  |  Automated Maintenance Report",
                        }
                    ],
                },
            ],
        },
    }


def main() -> int:
    webhook_url = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook_url:
        print("WARNING: FEISHU_WEBHOOK not set, skipping notification.", file=sys.stderr)
        return 0

    scan_result_raw = os.environ.get("SCAN_RESULT", "")
    scan_status = os.environ.get("SCAN_STATUS", "unknown")

    result_data = None
    if scan_result_raw:
        try:
            result_data = json.loads(scan_result_raw)
        except json.JSONDecodeError:
            print("WARNING: SCAN_RESULT is not valid JSON, sending error card.", file=sys.stderr)

    card = build_card(result_data, scan_status)

    try:
        resp = requests.post(
            webhook_url,
            json=card,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("code") == 0 or body.get("StatusCode") == 0:
                print("Feishu notification sent successfully.")
                return 0
            print(f"Feishu API returned: {body}", file=sys.stderr)
            return 1
        print(f"Feishu webhook returned HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed to send Feishu notification: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

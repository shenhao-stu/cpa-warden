# cpa-warden

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/fantasticjoe/cpa-warden)](https://github.com/fantasticjoe/cpa-warden/releases/latest)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![uv](https://img.shields.io/badge/deps-uv-6f42c1)

[简体中文](README.zh-CN.md)

`cpa-warden` is an interactive [CLIProxyAPI (CPA)](https://github.com/router-for-me/CLIProxyAPI) auth inventory scanner and maintenance tool for local operations against a specific CPA management environment.

It currently relies on two management flows: `GET /v0/management/auth-files` for inventory and `POST /v0/management/api-call` against `https://chatgpt.com/backend-api/wham/usage` for usage probing.

## What It Does

From a user perspective, the script:

- fetches the current auth inventory
- stores local state in SQLite
- probes usage concurrently
- exports current `401` and quota-limited results
- optionally deletes, disables, or re-enables accounts in maintenance mode

## Key Capabilities

- Interactive mode by default when no `--mode` is provided in a TTY
- Non-interactive `scan` and `maintain` workflows for repeatable runs
- External JSON configuration for sensitive values such as `base_url` and `token`
- Concurrent usage probing through the CLIProxyAPI `api-call` endpoint
- Local SQLite state tracking across runs
- JSON exports for invalid and quota-limited accounts
- Short production output with optional Rich progress display in TTY sessions
- Full debug-level logs written to a file on every run

## Safety And Scope

`cpa-warden` is built for a specific CPA management setup, not as a generic account-management platform. It is already usable for real local operations, but it is still an early-stage open-source project. Treat it as an operator-focused maintenance tool for known CPA environments, not a universal solution for arbitrary auth systems.

Maintenance mode can delete or disable remote accounts. Review your configuration carefully before running destructive actions, especially when using `--quota-action delete` or `--yes`.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
uv sync
```

## Configuration

Copy the example configuration first:

```bash
cp config.example.json config.json
```

At minimum, set:

- `base_url`
- `token`

`config.json` is ignored by git and should never be committed.

Example configuration:

```json
{
  "base_url": "https://your-cpa.example.com",
  "token": "replace-with-your-management-token",
  "target_type": "codex",
  "provider": "",
  "probe_workers": 40,
  "action_workers": 20,
  "timeout": 15,
  "retries": 1,
  "quota_action": "disable",
  "delete_401": true,
  "auto_reenable": true,
  "db_path": "cpa_warden_state.sqlite3",
  "invalid_output": "cpa_warden_401_accounts.json",
  "quota_output": "cpa_warden_quota_accounts.json",
  "log_file": "cpa_warden.log",
  "debug": false,
  "user_agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
}
```

Important configuration keys:

- `base_url`: CLIProxyAPI management base URL
- `token`: CLIProxyAPI management token
- `target_type`: filter records by `files[].type`
- `provider`: filter records by the `provider` field
- `probe_workers`: concurrency for usage probing
- `action_workers`: concurrency for delete / disable / enable actions
- `timeout`: request timeout in seconds
- `retries`: retry count for probe failures
- `quota_action`: action for quota-limited accounts, either `disable` or `delete`
- `delete_401`: whether maintenance mode deletes `401` accounts
- `auto_reenable`: whether recovered accounts are re-enabled automatically
- `db_path`: local SQLite state database path
- `invalid_output`: JSON export path for invalid `401` accounts
- `quota_output`: JSON export path for quota-limited accounts
- `log_file`: runtime log file path
- `debug`: enable more verbose terminal logging
- `user_agent`: User-Agent used for `wham/usage` probing

## Usage

Interactive mode:

```bash
uv run python cpa_warden.py
```

Non-interactive examples:

```bash
uv run python cpa_warden.py --mode scan
uv run python cpa_warden.py --mode scan --debug
uv run python cpa_warden.py --mode scan --target-type codex --provider openai
uv run python cpa_warden.py --mode maintain
uv run python cpa_warden.py --mode maintain --no-delete-401 --no-auto-reenable
uv run python cpa_warden.py --mode maintain --quota-action delete
uv run python cpa_warden.py --mode maintain --quota-action delete --yes
```

Available CLI options:

- `--config`
- `--mode`
- `--target-type`
- `--provider`
- `--probe-workers`
- `--action-workers`
- `--timeout`
- `--retries`
- `--user-agent`
- `--quota-action`
- `--db-path`
- `--invalid-output`
- `--quota-output`
- `--log-file`
- `--debug`
- `--delete-401`
- `--no-delete-401`
- `--auto-reenable`
- `--no-auto-reenable`
- `--yes`

## Modes

### `scan`

`scan` only reads inventory, probes usage, updates the local SQLite state, and exports current results. It does not change remote account state.

### `maintain`

`maintain` always runs a full `scan` first, then applies configured actions:

- delete `401` accounts if enabled
- disable or delete quota-limited accounts
- re-enable recovered accounts if enabled

Deletion flows require confirmation unless `--yes` is provided.

## Filters And Classification Rules

Filtering behavior:

- `target_type` matches `files[].type`
- `provider` is a case-insensitive exact match against the `provider` field

Classification rules:

- `401`: `unavailable == true` or `api-call.status_code == 401`
- `quota limited`: `api-call.status_code == 200` and `body.rate_limit.limit_reached == true`
- `recovered`: previously marked as `quota_disabled`, and now `allowed == true` and `limit_reached == false`

## Output Files

Default output artifacts:

- `cpa_warden_state.sqlite3`: local SQLite state database
- `cpa_warden_401_accounts.json`: exported invalid `401` accounts
- `cpa_warden_quota_accounts.json`: exported quota-limited accounts
- `cpa_warden.log`: runtime log file

## Logging And Debug Behavior

- Production terminal output stays short
- If the session is a TTY, production mode prefers a Rich progress display
- `--debug` or `debug: true` enables more verbose terminal logging
- The log file always keeps full debug-level details

## Web UI

The `web` branch includes a browser-based dashboard for managing CPA auth files. It is a pure HTML/CSS/JS application with no build step — deployable to GitHub Pages or any static hosting service.

Features:

- Overview dashboard with account statistics and visual charts
- Searchable, sortable, paginated accounts table with bulk actions
- Upload, download, and delete auth files
- OAuth login flows for Codex, Claude, Gemini, Qwen, iFlow
- Remote CPA configuration editor and log viewer
- Dark/light theme with system preference detection
- Saved connections stored locally in browser `localStorage`

### Deploy to GitHub Pages

1. Go to **Settings > Pages** in your GitHub repository
2. Under **Build and deployment > Source**, select **GitHub Actions**
3. Click **Save**
4. Push any change to the `web` branch, or manually trigger the workflow at **Actions > Deploy Web UI to GitHub Pages > Run workflow**
5. Your dashboard will be live at `https://<username>.github.io/<repo>/`

### Deploy with Docker

```bash
docker build -t cpa-warden-web web/
docker run -p 8080:80 cpa-warden-web
```

### Run locally

```bash
python3 -m http.server 8080 --directory web
# Open http://localhost:8080
```

### CORS note

The Web UI makes cross-origin requests from the browser to your CPA instance. This requires the CPA server to have `allow-remote-management: true` and send appropriate CORS headers. If CORS is blocked, either:

- Deploy the Web UI on the same domain as your CPA instance
- Use the Docker/nginx deployment with the reverse proxy config in `web/nginx.conf`

## Scheduled Maintenance (GitHub Actions)

The `maintain.yml` workflow runs automated scan + maintenance on multiple CPA instances and sends results to Feishu.

### Setup

1. Go to **Settings > Secrets and variables > Actions** in your GitHub repository

2. Add the following **Repository secrets**:

   **`CPA_INSTANCES`** — JSON array of CPA instances:
   ```json
   [
     {"name": "instance-1", "url": "https://your-cpa-1.example.com", "token": "your-mgmt-token-1"},
     {"name": "instance-2", "url": "https://your-cpa-2.example.com", "token": "your-mgmt-token-2"}
   ]
   ```

   **`FEISHU_WEBHOOK`** — Feishu bot webhook URL:
   ```
   https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id
   ```

3. The workflow runs automatically every 6 hours. You can also trigger it manually from **Actions > Scheduled Maintenance > Run workflow**.

### What it does

For each CPA instance, the workflow:

- Fetches the auth file inventory
- Probes usage quotas concurrently
- Deletes accounts returning `401`
- Disables accounts that have hit their quota limit
- Re-enables accounts that have recovered
- Sends a rich Feishu card notification with per-instance results

### Customizing the schedule

Edit the cron expression in `.github/workflows/maintain.yml`:

```yaml
on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours
```

## Project Structure

- `cpa_warden.py`: main entrypoint
- `clean_codex_accounts.py`: compatibility wrapper for the old command name
- `config.example.json`: example configuration
- `pyproject.toml`: project metadata and dependencies
- `web/`: browser-based dashboard (HTML/CSS/JS)
- `.github/workflows/ci.yml`: basic CI checks
- `.github/workflows/pages.yml`: GitHub Pages deployment
- `.github/workflows/maintain.yml`: scheduled maintenance with Feishu notification
- `.github/scripts/scheduled_maintain.py`: multi-instance maintenance runner
- `.github/scripts/feishu_notify.py`: Feishu card notification sender

## Compatibility Note

`clean_codex_accounts.py` is kept as a compatibility wrapper around `cpa_warden.main()`. Use `cpa_warden.py` as the primary documented entrypoint for new usage.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Security

See [SECURITY.md](SECURITY.md) for responsible disclosure guidance.

## License

MIT. See [LICENSE](LICENSE).

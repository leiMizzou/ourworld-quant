# OurWorlds Quant deployment runbook

This project currently runs as a single stdlib Python HTTP service behind Cloudflare Tunnel.

Registration uses email verification plus password login. `/register` first records explicit acceptance of the current terms, privacy notice, and risk disclosure on a pending email session, then sends a one-time verification link. Confirmed links open a token-free setup page where the user sets a username and password; the app then creates or reuses the account, joins the active contest, and redirects to `/login`. The verification link itself does not create a browser session.

## Public beta command

```bash
screen -dmS ourworld-quant-app zsh -lc 'cd /Volumes/EXTDISK/QUANT/QUANT/ourworld-quant && OWQ_EMAIL_DEV_AUTH=1 OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=0 OWQ_ADMIN_USER_IDS=<real-admin-user-id> deploy/run-public-app.sh'
```

`OWQ_EMAIL_DEV_AUTH=1` is only for a short public beta before transactional email is ready. The committed example keeps it disabled; formal production should configure either Cloudflare Email Service over REST:

```bash
OWQ_EMAIL_PROVIDER=cloudflare
OWQ_EMAIL_FROM=noreply@ourworlds.app
OWQ_EMAIL_TEST_MAX_AGE_HOURS=72
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_API_TOKEN=...
OWQ_EMAIL_DEV_AUTH=0
```

`OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=1` makes the app display one-time test verification links on registration pages. Keep it unset or `0` on public deployments; it is intended for local development only.

The built-in `cloudflare` provider uses the Email Service REST API endpoint
`POST https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send`
with JSON fields `to`, `from`, `subject`, `text`, and `html`.
Cloudflare Email Routing/forwarding handles inbound mail only; it is not an outbound SMTP service. Registration verification links need Cloudflare Email Sending, a verified sending domain, and a plan that includes outbound sending to arbitrary recipients.
`OWQ_EMAIL_FROM` must be a single sender address such as `noreply@ourworlds.app`; keep the display name in `OWQ_EMAIL_FROM_NAME`. When `OWQ_EMAIL_PROVIDER` is explicitly set to `cloudflare` or `smtp`, the app validates and uses only that provider instead of silently falling back to the other path.

or SMTP:

```bash
OWQ_EMAIL_PROVIDER=smtp
OWQ_EMAIL_FROM=noreply@ourworlds.app
OWQ_SMTP_HOST=smtp.example.com
OWQ_SMTP_PORT=587
OWQ_SMTP_USER=...
OWQ_SMTP_PASSWORD=...
OWQ_EMAIL_DEV_AUTH=0
```

For Google/Gmail SMTP, use `smtp.gmail.com` with TLS port `587` or SSL port `465`, authenticate with the full Google email address, and use a Google App Password rather than the account password. In `deploy/public.env`, set `OWQ_EMAIL_PROVIDER=smtp`, `OWQ_EMAIL_FROM=<your Google address>`, `OWQ_SMTP_HOST=smtp.gmail.com`, `OWQ_SMTP_USER=<your Google address>`, and `OWQ_SMTP_PASSWORD=<Google App Password>`.
For Cloudflare Email Service SMTP, use `smtp.mx.cloudflare.net` with SSL port `465`, set `OWQ_SMTP_USER=api_token`, and put the Cloudflare API token in `OWQ_SMTP_PASSWORD`.

After configuring real email sending, log in as an admin, open `/admin`, and use `邮件发信诊断` to send a test message before opening registration broadly.
You can also validate the same sending path from the command line before logging in:

```bash
.venv/bin/python -m src.app.server --env-file deploy/public.env --send-test-email you@example.com
```

The CLI test records `cli.email_test` or `cli.email_test_failed` in the audit log and returns non-zero on failure without printing API tokens or SMTP passwords. Public registration failures show users a generic retry/contact-admin message; detailed provider errors are kept only as redacted audit diagnostics. Formal `/readyz` also requires a recent successful `cli.email_test` or `admin.email_test`; tune the freshness window with `OWQ_EMAIL_TEST_MAX_AGE_HOURS` (default 72).

## launchd service

For a durable Mac deployment, run the app under launchd instead of a detached `screen` session.

One-time setup:

```bash
cd /Volumes/EXTDISK/QUANT/QUANT/ourworld-quant
mkdir -p data/logs
cp deploy/public.env.example deploy/public.env
chmod 600 deploy/public.env
# edit deploy/public.env and fill email/Tushare/admin settings
deploy/install-launchd.sh
# after deploy/public.env has data-source credentials:
OWQ_INSTALL_MARKET_SYNC=1 deploy/install-launchd.sh
```

Operational commands:

```bash
launchctl print "gui/$(id -u)/com.ourworlds.quant.app"
launchctl kickstart -k "gui/$(id -u)/com.ourworlds.quant.app"
tail -f ~/Library/Logs/OurWorldsQuant/app.err.log
tail -f ~/Library/Logs/OurWorldsQuant/market-sync.err.log
```

Public deployment check:

```bash
# Formal production: every release gate must be clean.
deploy/check-public.sh

# Current public beta: accept only the documented email-sending warnings.
OWQ_ALLOW_PUBLIC_BETA=1 deploy/check-public.sh
```

The check script verifies launchd state, the market-sync last exit code when it has run, local and public `/livez`, local and public `/healthz`, public `/readyz`, `/metrics`, `/data-status`, `/robots.txt`, `/sitemap.xml`, public registration pages, and the app stderr log. `/livez` must return 200; `/healthz` may return 200 or 503 as a diagnostic when required release prerequisites are missing. `OWQ_ALLOW_PUBLIC_BETA=1` still fails if any required readiness check fails, if warnings appear outside `email_sending` and `email_dev_auth_public`, or if the public registration page exposes a test verification link. In public beta without real sending credentials, the recent email delivery probe is marked OK with a beta-only detail; once credentials are configured, formal readiness requires a recent successful diagnostic email.

Unload:

```bash
deploy/uninstall-launchd.sh
```

The install script copies small launcher scripts plus private copies of `deploy/public.env` and `data/app.secret` to `~/Library/Application Support/OurWorldsQuant`, copies LaunchAgent plists to `~/Library/LaunchAgents`, rewrites launchd stdout/stderr paths to `~/Library/Logs/OurWorldsQuant`, injects `OWQ_ROOT_DIR` and `OWQ_SECRET_FILE`, then loads them with `launchctl bootstrap`. Keep the real `deploy/public.env` out of git; rerun `deploy/install-launchd.sh` after editing it so launchd receives the updated copy. During public beta it may contain `OWQ_EMAIL_DEV_AUTH=1`; formal production must set `OWQ_EMAIL_DEV_AUTH=0` and configure Cloudflare Email Sending or SMTP.

`deploy/public.env` is sourced by `zsh`, so treat it as a shell env file: quote values that contain spaces, keep secrets on their own assignment lines, and run `zsh -n deploy/run-public-app.sh deploy/sync-market-public.sh` plus the deploy artifact tests after editing the example.

## Health gate

```bash
.venv/bin/python -m src.app.server --env-file deploy/public.env --doctor
.venv/bin/python -m src.app.server --env-file deploy/public.env --doctor-strict
```

`/livez` is the narrow liveness probe for process and application database connectivity. `/healthz` and `--doctor` are health/readiness diagnostics: required checks must be `OK` for a 200 response. `/readyz` and `--doctor-strict` are formal release gates: every warning must be cleared. Public requests receive a summary by default; local requests receive full `checks`, and remote operators can set `OWQ_HEALTH_DETAIL_TOKEN` and send `X-OWQ-Health-Token` to retrieve full details. Optional warnings are acceptable only for a documented public beta, not for commercial launch. Public or production deployments must set `OWQ_ADMIN_USER_IDS` or `OWQ_ADMIN_EMAILS`; the local "first registered user is admin" fallback is disabled whenever `OWQ_PUBLIC_BASE_URL`, `OWQ_ENV=production`, or `OWQ_ENV_PRODUCTION=1` marks the runtime as public. Before disabling beta email verification, readiness also requires a recoverable admin access path: either an `OWQ_ADMIN_EMAILS` address with real email sending configured, or an already-configured admin user with username/email + password login.

If real email sending is not ready yet, set a temporary password for an existing admin user without printing or auditing the password itself:

```bash
OWQ_SET_PASSWORD='temporary-strong-password' \
.venv/bin/python -m src.app.server --env-file deploy/public.env \
  --set-user-password USER_ID --login-name admin-user
```

The admin should log in and immediately change it from `/account`; that password update invalidates the old session.

Public `/metrics` returns only `{"status":"ok","detail":"summary"}` by default. Local requests, or remote requests with the configured `X-OWQ-Health-Token`, expose low-sensitivity aggregate runtime counters for monitoring: uptime, request totals, status-code buckets, method buckets, error count, average duration, and max duration. It intentionally does not expose IPs, emails, cookies, URL parameters, or user identifiers.

`/data-status` is the public human-readable data transparency page for market source coverage, latest trading date, prediction candidate matching, and arena activity. `/support` is a public noindex support-request form for registration, login, data, community, or business issues when email delivery or account access needs human handling. The support form is protected by IP throttling plus same-email cooldown, hourly, and open-request limits; rate-limit hits write redacted `security.rate_limited` audit events. `/livez`, `/healthz`, `/readyz`, and `/metrics` remain machine/ops endpoints.

`/robots.txt` and `/sitemap.xml` are public discovery files. The sitemap includes only the landing page, data transparency page, public leaderboard, forum, public posts, public profile pages, and legal pages. It must not list login, account, admin, support, monitoring, or auth endpoints. Private, auth, support, monitoring, download, redirect, and error responses also send `X-Robots-Tag: noindex, nofollow`.

Production health also verifies that application prices are backed by non-demo market rows and that the latest `as_of` date is not stale. The default freshness threshold is 10 days and can be tuned with `OWQ_MARKET_MAX_STALENESS_DAYS`. The strict release gate also warns when the real market universe is too small; the default minimum is 300 non-demo symbols and can be tuned with `OWQ_MARKET_MIN_REAL_CODES`. It also checks that `reports/predictions.csv` exists, is fresh, and has enough candidates matching current app market prices; the default minimum is 10 candidates and can be tuned with `OWQ_PREDICTIONS_MIN_CODES`. `/readyz` also reports the most recent production market-sync script success/failure; tune the allowed age with `OWQ_MARKET_SYNC_MAX_AGE_HOURS` (default 36). Demo or development participants (`demo-*`, `dev-wechat-*`, or `模拟用户*`) are tolerated while `OWQ_EMAIL_DEV_AUTH=1` is explicitly running a public beta, but formal readiness warns if they remain in the active public contest after the beta test entry is closed. Formal production also blocks `/admin/demo-seed` unless `OWQ_ALLOW_DEMO_SEED=1` is deliberately set for a controlled drill.
The strict gate additionally checks open support requests and pending content reports; if the oldest item is older than `OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS` (default 72), `/readyz` warns so user-facing queues do not silently accumulate. It also warns when `server.error` audit events were recorded within `OWQ_SERVER_ERROR_WINDOW_HOURS` (default 24), so recent 500s are visible before a formal release.

The app database is opened with SQLite WAL mode, `synchronous=NORMAL`, foreign keys, and a configurable lock wait (`OWQ_SQLITE_BUSY_TIMEOUT_MS`, default 5000). `/healthz` also runs `PRAGMA quick_check`, `PRAGMA foreign_key_check`, reports the runtime SQLite settings, and warns when the WAL file exceeds `OWQ_SQLITE_MAX_WAL_MB` (default 256 MB). Run `--sqlite-maintenance` after large write batches to execute `PRAGMA optimize` and `PRAGMA wal_checkpoint(TRUNCATE)`; the production market sync script runs it automatically after refreshing market rows and prediction reports.

Disk free space is part of the release gate. By default the app checks the disk containing `OWQ_APP_DB` and warns below 1024 MB free. Tune this with `OWQ_DISK_CHECK_PATH` and `OWQ_MIN_FREE_DISK_MB`.

POST form bodies are capped by `OWQ_MAX_FORM_BYTES` to protect the single-process service from oversized form or CSV submissions. The default is 1MB; keep it at or below 5MB for this deployment model. Registration/auth endpoints and logged-in write actions are also rate limited in memory. Keep `OWQ_RATE_LIMITS_DISABLED=0` in public deployments; `/readyz` treats disabled rate limits as a required production warning.

CSRF failures and authenticated non-admin attempts to access admin endpoints are recorded as minimal audit events (`security.csrf_failed` and `security.admin_forbidden`). These events include path/method metadata and actor ID where available, but not submitted form bodies. Logout is a CSRF-protected POST action and records `auth.logout` before clearing the session cookie. Public support requests create `support.request_create`; admin support handling and CSV exports for account overview, content reports, support requests, and audit logs also create audit events, so operational downloads are visible during incident review. Audit logs default to a 400-day retention window (`OWQ_AUDIT_RETENTION_DAYS`, allowed 30-3650). `/healthz` and `/readyz` report expired audit rows; admins can prune them in `/admin`, CLI operators can run `python3 -m src.app.server --env-file deploy/public.env --prune-audit-log`, and `deploy/sync-market-public.sh` does this by default unless `OWQ_SYNC_PRUNE_AUDIT=0`. Email magic-link session records default to 30-day retention (`OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS`, allowed 1-365); admins can prune them in `/admin`, CLI operators can run `--prune-email-login-sessions`, and the sync script does this by default unless `OWQ_SYNC_PRUNE_EMAIL_LOGIN=0`.

Unhandled route exceptions return a generic 500 page and write a `server.error` audit event with method, path, and exception type. The response includes an `X-OurWorlds-Error-Id` header and a user-visible error number that matches the audit event ID, without exposing exception text. Do not rely on terminal tracebacks as the primary production signal.

When launchd unloads or restarts the app it sends `SIGTERM`; the server handles that signal through the normal shutdown path, closes the HTTP listener, and closes the SQLite connection before exiting.

## Backup

CLI:

```bash
.venv/bin/python -m src.app.server --backup-app-db
.venv/bin/python -m src.app.server --verify-app-backup data/backups/app-YYYYMMDD-HHMMSS.sqlite
.venv/bin/python -m src.app.server --restore-app-backup data/backups/app-YYYYMMDD-HHMMSS.sqlite data/restore-drill.sqlite
.venv/bin/python -m src.app.server --sqlite-maintenance
```

Admin UI:

1. Log in as an admin user.
2. Open `/admin`.
3. Click `立即备份应用数据库`.

Backups are written to `OWQ_APP_BACKUP_DIR` or `data/backups/`. Automatically named `app-*.sqlite` backups keep the newest 30 files by default; tune this with `OWQ_APP_BACKUP_KEEP`. `/readyz` verifies that the latest automatic backup is no older than `OWQ_APP_BACKUP_MAX_AGE_HOURS` and passes `PRAGMA quick_check`; the default threshold is 48 hours. `--verify-app-backup` opens a backup read-only and validates quick_check, core tables, foreign keys, and row-count visibility without touching the live DB. `--restore-app-backup BACKUP DEST` restores to a target SQLite file and refuses to overwrite unless `--restore-overwrite` is set; run it into a drill path first, then stop the app before replacing a live DB. Backups written to an explicit path are never pruned automatically.

## Market sync job

Run the production market sync script manually after a data refresh, or from cron/launchd after the A-share close:

```bash
cd /Volumes/EXTDISK/QUANT/QUANT/ourworld-quant
OWQ_MARKET_SOURCE=tushare \
OWQ_MARKET_LIMIT=500 \
OWQ_MARKET_MIN_REAL_CODES=300 \
OWQ_SYNC_DATA_FIRST=1 \
OWQ_SYNC_REPORTS=1 \
OWQ_ADMIN_USER_IDS=<real-admin-user-id> \
OWQ_EMAIL_DEV_AUTH=1 \
TUSHARE_TOKEN=... \
deploy/sync-market-public.sh
```

The script:

1. Optionally refreshes the DuckDB data layer when `OWQ_SYNC_DATA_FIRST=1`.
2. Creates a SQLite app backup before replacing market rows.
3. Syncs unadjusted (`none`) latest prices into the paper-trading app.
4. Refreshes `reports/real-data-report.md` and `reports/predictions.csv` by default; set `OWQ_SYNC_REPORTS=0` to skip.
5. Records `cli.market_sync_started` at launch and records `cli.market_sync_succeeded` or `cli.market_sync_failed` on exit, so `/readyz` can report whether the scheduled job itself is healthy.
6. Runs SQLite maintenance to optimize planner statistics and truncate the WAL file after write-heavy refreshes.
7. Runs `--doctor` as a diagnostic by default without marking an otherwise successful market sync failed; set `OWQ_SYNC_STRICT_READY=1` after real email sending is configured to run the formal release gate and fail the job on readiness warnings.

When `OWQ_MARKET_SOURCE=tushare`, the script defaults `OWQ_SLEEP` to `1.3` seconds if you do not set it, keeping the free-account request rate below the common 50 calls/minute limit. Use the same admin and email environment as the running public service. During a temporary public beta, keep `OWQ_EMAIL_DEV_AUTH=1` only if needed and leave `OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=0`; formal production should replace the test login flag with real email sending variables.

Recommended launchd schedule after setup:

```xml
<key>ProgramArguments</key>
<array>
  <string>/bin/zsh</string>
  <string>/Volumes/EXTDISK/QUANT/QUANT/ourworld-quant/deploy/sync-market-public.sh</string>
</array>
<key>StartCalendarInterval</key>
<dict>
  <key>Hour</key><integer>18</integer>
  <key>Minute</key><integer>30</integer>
</dict>
```

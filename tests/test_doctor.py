from __future__ import annotations

import os
import signal
import time
import tempfile
import unittest
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.app import db, doctor
from src.app import services
from src.app import server
from src.app.server import main

# The strict-readiness happy path is not hermetic: the disk check and the DuckDB quant
# check default to the repo's local `data/` dir (which exists on the maintainer's host
# but not on a fresh CI checkout). Run it only where that local data layout is present.
_LOCAL_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class DoctorTest(unittest.TestCase):
    def test_doctor_reports_core_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"] for row in checks}
        self.assertIn("python", names)
        self.assertIn("app_db", names)
        self.assertIn("app_db_integrity", names)
        self.assertIn("app_db_foreign_keys", names)
        self.assertIn("sqlite_runtime", names)
        self.assertIn("sqlite_wal_size", names)
        self.assertIn("market", names)
        self.assertIn("market_real_data", names)
        self.assertIn("market_coverage", names)
        self.assertIn("market_freshness", names)
        self.assertIn("prediction_results", names)
        self.assertIn("app_secret", names)
        self.assertIn("cookie_secure", names)
        self.assertIn("request_body_limit", names)
        self.assertIn("rate_limits", names)
        self.assertIn("legal_consent_gate", names)
        self.assertIn("disk_space", names)
        self.assertIn("app_backup", names)
        self.assertIn("session_ttl", names)
        self.assertIn("audit_retention", names)
        self.assertIn("email_login_session_retention", names)
        self.assertIn("operational_queue", names)
        self.assertIn("recent_server_errors", names)
        self.assertIn("admin_config", names)
        self.assertIn("admin_access", names)
        self.assertIn("email_login", names)
        self.assertIn("email_sending", names)
        self.assertIn("email_delivery_probe", names)
        self.assertIn("demo_contest_participants", names)
        self.assertTrue(all("required" in row for row in checks))

    def test_shutdown_signal_handler_is_restored(self):
        if not hasattr(signal, "SIGTERM"):
            self.skipTest("SIGTERM is not available on this platform")

        original = signal.getsignal(signal.SIGTERM)
        installed = server.install_shutdown_signal_handlers()
        try:
            self.assertIn((signal.SIGTERM, original), installed)
            self.assertIs(signal.getsignal(signal.SIGTERM), server.raise_keyboard_interrupt)
            with self.assertRaises(KeyboardInterrupt):
                server.raise_keyboard_interrupt(signal.SIGTERM, None)
        finally:
            server.restore_signal_handlers(installed)

        self.assertEqual(signal.getsignal(signal.SIGTERM), original)

    def test_health_treats_optional_production_checks_as_non_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                health = doctor.health(con)
                strict = doctor.health(con, strict=True)
            finally:
                con.close()

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["ok"])
        self.assertFalse(health["strict"])
        self.assertEqual(health["required_warnings"], 0)
        self.assertGreaterEqual(health["optional_warnings"], 1)
        self.assertIn("checks", health)
        self.assertEqual(strict["status"], "degraded")
        self.assertFalse(strict["ok"])
        self.assertTrue(strict["strict"])
        self.assertEqual(strict["required_warnings"], 0)
        self.assertGreaterEqual(strict["optional_warnings"], 1)

    def test_production_readiness_checks_are_blocking_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "",
                        "OWQ_ADMIN_USER_IDS": "",
                        "OWQ_ADMIN_OPENIDS": "",
                        "OWQ_ADMIN_EMAILS": "",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_FROM": "",
                        "CLOUDFLARE_ACCOUNT_ID": "",
                        "CLOUDFLARE_API_TOKEN": "",
                        "OWQ_SMTP_HOST": "",
                    },
                    clear=False,
                ):
                    health = doctor.health(con)
            finally:
                con.close()

        self.assertEqual(health["status"], "degraded")
        self.assertFalse(health["ok"])
        names = {row["name"]: row for row in health["checks"]}
        self.assertEqual(names["app_secret"]["status"], "warn")
        self.assertEqual(names["admin_config"]["status"], "warn")
        self.assertEqual(names["email_login"]["status"], "warn")
        self.assertEqual(names["market_real_data"]["status"], "warn")

    def test_app_db_bootstrap_configures_sqlite_for_concurrent_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                journal_mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                busy_timeout = con.execute("PRAGMA busy_timeout").fetchone()[0]
                foreign_keys = con.execute("PRAGMA foreign_keys").fetchone()[0]
                checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertGreaterEqual(int(busy_timeout), 5000)
        self.assertEqual(int(foreign_keys), 1)
        self.assertEqual(names["app_db_integrity"]["status"], "ok")
        self.assertEqual(names["app_db_foreign_keys"]["status"], "ok")
        self.assertEqual(names["sqlite_runtime"]["status"], "ok")
        self.assertEqual(names["sqlite_wal_size"]["status"], "ok")

    def test_app_db_foreign_key_check_warns_on_orphaned_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                con.commit()
                con.execute("PRAGMA foreign_keys = OFF")
                con.execute(
                    """
                    INSERT INTO orders(account_id, code, side, qty, price, fee, amount)
                    VALUES (9999, '000001.SZ', 'buy', 100, 10, 1, 1001)
                    """
                )
                con.commit()
                con.execute("PRAGMA foreign_keys = ON")
                checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["app_db_foreign_keys"]["status"], "warn")
        self.assertIn("外键错误", names["app_db_foreign_keys"]["detail"])

    def test_sqlite_wal_size_check_warns_when_file_is_too_large(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_db = Path(tmp) / "app.sqlite"
            fake_db.write_text("", encoding="utf-8")
            wal_path = Path(str(fake_db) + "-wal")
            wal_path.write_bytes(b"x" * (2 * 1024 * 1024))
            with patch("src.app.doctor.sqlite_main_db_path", return_value=fake_db), patch.dict(
                "os.environ",
                {"OWQ_SQLITE_MAX_WAL_MB": "1"},
                clear=False,
            ):
                con = db.bootstrap(Path(tmp) / "real.sqlite")
                try:
                    ok, detail = doctor.sqlite_wal_size_check(con)
                finally:
                    con.close()

        self.assertFalse(ok)
        self.assertIn("WAL 2.0 MB", detail)

    def test_sqlite_wal_size_check_warns_on_invalid_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_SQLITE_MAX_WAL_MB": "huge"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["sqlite_wal_size"]["status"], "warn")
        self.assertIn("OWQ_SQLITE_MAX_WAL_MB", names["sqlite_wal_size"]["detail"])

    def test_session_ttl_check_warns_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_SESSION_TTL_SECONDS": "forever"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["session_ttl"]["status"], "warn")
        self.assertEqual(names["session_ttl"]["required"], "false")

    def test_request_body_limit_check_warns_on_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_MAX_FORM_BYTES": "unbounded"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["request_body_limit"]["status"], "warn")
        self.assertEqual(names["request_body_limit"]["required"], "false")

    def test_operational_queue_check_warns_when_open_work_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                author_id = services.get_or_create_user(con, "queue-author-openid", "QueueAuthor")
                reporter_id = services.get_or_create_user(con, "queue-reporter-openid", "QueueReporter")
                post_id = services.create_post(con, author_id, "待处理举报帖子", "这是一条用于运营队列检查的帖子。", "ops")
                services.create_content_report(con, reporter_id, "post", post_id, "待处理时间过长")
                services.create_support_request(
                    con,
                    "queue@example.com",
                    "注册确认邮件异常",
                    "这条支持请求用于验证运营队列 SLA 检查。",
                    category="registration",
                )
                con.execute("UPDATE content_reports SET created_at=datetime('now', '-4 hours') WHERE status='pending'")
                con.execute("UPDATE support_requests SET created_at=datetime('now', '-5 hours') WHERE status='open'")
                con.commit()
                with patch.dict("os.environ", {"OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS": "3"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["operational_queue"]["status"], "warn")
        self.assertEqual(names["operational_queue"]["required"], "false")
        self.assertIn("支持请求 1 条", names["operational_queue"]["detail"])
        self.assertIn("内容举报 1 条", names["operational_queue"]["detail"])
        self.assertIn("阈值 3 小时", names["operational_queue"]["detail"])

    def test_operational_queue_check_warns_on_invalid_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS": "soon"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["operational_queue"]["status"], "warn")
        self.assertIn("OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS", names["operational_queue"]["detail"])

    def test_recent_server_errors_warns_when_error_is_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "server.error", target_type="http", target_id="/app")
                checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["recent_server_errors"]["status"], "warn")
        self.assertEqual(names["recent_server_errors"]["required"], "false")
        self.assertIn("server.error", names["recent_server_errors"]["detail"])
        self.assertIn("/app", names["recent_server_errors"]["detail"])

    def test_recent_server_errors_ignores_old_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "server.error", target_type="http", target_id="/old")
                con.execute("UPDATE audit_events SET created_at=datetime('now', '-48 hours') WHERE action='server.error'")
                con.commit()
                with patch.dict("os.environ", {"OWQ_SERVER_ERROR_WINDOW_HOURS": "24"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["recent_server_errors"]["status"], "ok")
        self.assertIn("未记录 server.error", names["recent_server_errors"]["detail"])

    def test_recent_server_errors_warns_on_invalid_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_SERVER_ERROR_WINDOW_HOURS": "soon"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["recent_server_errors"]["status"], "warn")
        self.assertIn("OWQ_SERVER_ERROR_WINDOW_HOURS", names["recent_server_errors"]["detail"])

    def test_disk_space_check_warns_when_free_space_is_below_threshold(self):
        class Usage:
            total = 1024 * 1024 * 1024
            used = 1023 * 1024 * 1024
            free = 1 * 1024 * 1024

        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {"OWQ_DISK_CHECK_PATH": tmp, "OWQ_MIN_FREE_DISK_MB": "1024"},
                    clear=False,
                ), patch("src.app.doctor.shutil.disk_usage", return_value=Usage()):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["disk_space"]["status"], "warn")
        self.assertEqual(names["disk_space"]["required"], "false")
        self.assertIn("可用 1 MB", names["disk_space"]["detail"])

    def test_app_backup_check_accepts_recent_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_dir / "app-20260624-120000.sqlite")
                with patch.dict("os.environ", {"OWQ_APP_BACKUP_DIR": str(backup_dir)}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["app_backup"]["status"], "ok")
        self.assertEqual(names["app_backup"]["required"], "false")
        self.assertIn("quick_check=ok", names["app_backup"]["detail"])

    def test_app_backup_check_warns_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_APP_BACKUP_DIR": str(Path(tmp) / "missing-backups")}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["app_backup"]["status"], "warn")
        self.assertIn("备份目录不存在", names["app_backup"]["detail"])

    def test_app_backup_check_warns_when_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            backup_path = backup_dir / "app-20260624-120000.sqlite"
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_path)
                old = time.time() - 3 * 3600
                os.utime(backup_path, (old, old))
                with patch.dict(
                    "os.environ",
                    {"OWQ_APP_BACKUP_DIR": str(backup_dir), "OWQ_APP_BACKUP_MAX_AGE_HOURS": "1"},
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["app_backup"]["status"], "warn")
        self.assertIn("阈值 1 小时", names["app_backup"]["detail"])

    def test_audit_retention_warns_when_events_exceed_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                event_id = services.record_audit_event(con, None, "old.audit", target_type="unit")
                con.execute("UPDATE audit_events SET created_at=datetime('now', '-45 days') WHERE id=?", (event_id,))
                con.commit()
                with patch.dict("os.environ", {"OWQ_AUDIT_RETENTION_DAYS": "30"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["audit_retention"]["status"], "warn")
        self.assertEqual(names["audit_retention"]["required"], "false")
        self.assertIn("1 条超过保留期", names["audit_retention"]["detail"])

    def test_audit_retention_warns_on_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_AUDIT_RETENTION_DAYS": "forever"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["audit_retention"]["status"], "warn")
        self.assertIn("OWQ_AUDIT_RETENTION_DAYS", names["audit_retention"]["detail"])

    def test_email_login_session_retention_warns_when_cleanup_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                token = services.create_email_login_session(
                    con,
                    "cleanup-needed@example.com",
                    "2026-06-24",
                    "2026-06-24",
                    "2026-06-24",
                    enforce_rate_limit=False,
                )
                services.confirm_email_login_session(con, token)
                con.execute(
                    "UPDATE email_login_sessions SET created_at=datetime('now', '-45 days') WHERE email=?",
                    ("cleanup-needed@example.com",),
                )
                con.commit()
                with patch.dict("os.environ", {"OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS": "30"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_login_session_retention"]["status"], "warn")
        self.assertEqual(names["email_login_session_retention"]["required"], "false")
        self.assertIn("1 条可清理", names["email_login_session_retention"]["detail"])

    def test_email_login_session_retention_allows_recent_expired_pending_challenges(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.create_email_login_session(
                    con,
                    "expired-pending@example.com",
                    "2026-06-24",
                    "2026-06-24",
                    "2026-06-24",
                    enforce_rate_limit=False,
                )
                con.execute(
                    "UPDATE email_login_sessions SET expires_at=datetime('now', '-1 minute') WHERE email=?",
                    ("expired-pending@example.com",),
                )
                con.commit()
                checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_login_session_retention"]["status"], "ok")
        self.assertIn("1 条待过期标记", names["email_login_session_retention"]["detail"])

    def test_email_login_session_retention_warns_on_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict("os.environ", {"OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS": "forever"}, clear=False):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_login_session_retention"]["status"], "warn")
        self.assertIn("OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS", names["email_login_session_retention"]["detail"])

    def test_production_readiness_warns_when_rate_limits_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_RATE_LIMITS_DISABLED": "1",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["rate_limits"]["status"], "warn")
        self.assertEqual(names["rate_limits"]["required"], "true")
        self.assertIn("OWQ_RATE_LIMITS_DISABLED=1", names["rate_limits"]["detail"])

    def test_production_readiness_warns_when_legal_consent_gate_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_LEGAL_CONSENT_REQUIRED": "0",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["legal_consent_gate"]["status"], "warn")
        self.assertEqual(names["legal_consent_gate"]["required"], "true")
        self.assertIn("OWQ_LEGAL_CONSENT_REQUIRED=0", names["legal_consent_gate"]["detail"])

    def test_production_market_freshness_warns_on_stale_real_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                con.execute("UPDATE market_prices SET source='csv', as_of='2020-01-01'")
                con.commit()
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "x" * 32,
                        "OWQ_ADMIN_USER_IDS": "",
                        "OWQ_ADMIN_EMAILS": "admin@example.com",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.example.com",
                        "OWQ_MARKET_MAX_STALENESS_DAYS": "10",
                    },
                    clear=False,
                ):
                    health = doctor.health(con)
            finally:
                con.close()

        names = {row["name"]: row for row in health["checks"]}
        self.assertEqual(names["market_real_data"]["status"], "ok")
        self.assertEqual(names["market_freshness"]["status"], "warn")
        self.assertEqual(names["market_freshness"]["required"], "true")
        self.assertEqual(health["status"], "degraded")

    def test_production_market_coverage_warns_when_real_universe_is_too_small(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                con.execute("DELETE FROM market_prices")
                con.execute(
                    """
                    INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
                    VALUES ('000001.SZ', '平安银行', 10, 9.8, 'csv', date('now'))
                    """
                )
                con.commit()
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "x" * 32,
                        "OWQ_ADMIN_USER_IDS": "",
                        "OWQ_ADMIN_EMAILS": "admin@example.com",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.example.com",
                        "OWQ_MARKET_MIN_REAL_CODES": "50",
                    },
                    clear=False,
                ):
                    health = doctor.health(con, strict=True)
            finally:
                con.close()

        names = {row["name"]: row for row in health["checks"]}
        self.assertEqual(names["market_real_data"]["status"], "ok")
        self.assertEqual(names["market_coverage"]["status"], "warn")
        self.assertEqual(names["market_coverage"]["required"], "false")
        self.assertEqual(health["status"], "degraded")

    def test_market_sync_job_check_accepts_recent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "cli.market_sync_succeeded", target_type="market_sync", detail={"status": "succeeded"})
                ok, detail = doctor.market_sync_job_check(con)
            finally:
                con.close()

        self.assertTrue(ok)
        self.assertIn("最近成功生产同步脚本", detail)

    def test_market_sync_job_check_warns_when_latest_run_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "cli.market_sync_succeeded", target_type="market_sync", detail={"status": "succeeded"})
                services.record_audit_event(con, None, "cli.market_sync_failed", target_type="market_sync", detail={"status": "failed", "exit_code": 2})
                ok, detail = doctor.market_sync_job_check(con)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("最近市场同步失败", detail)

    def test_market_sync_job_check_warns_when_success_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                event_id = services.record_audit_event(con, None, "cli.market_sync_succeeded", target_type="market_sync", detail={"status": "succeeded"})
                con.execute("UPDATE audit_events SET created_at=datetime('now', '-72 hours') WHERE id=?", (event_id,))
                con.commit()
                with patch.dict("os.environ", {"OWQ_MARKET_SYNC_MAX_AGE_HOURS": "24"}, clear=False):
                    ok, detail = doctor.market_sync_job_check(con)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("阈值 24 小时", detail)

    def test_prediction_results_warn_when_candidates_do_not_match_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            pred_path = Path(tmp) / "predictions.csv"
            today = date.today().isoformat()
            pred_path.write_text(
                "code,prediction,date\n"
                f"000001.SZ,0.01,{today}\n"
                f"600519.SH,0.02,{today}\n",
                encoding="utf-8",
            )
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                con.execute("DELETE FROM market_prices")
                con.execute(
                    """
                    INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
                    VALUES ('000001.SZ', '平安银行', 10, 9.8, 'csv', date('now'))
                    """
                )
                con.commit()
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_PREDICTIONS_CSV": str(pred_path),
                        "OWQ_PREDICTIONS_MIN_CODES": "2",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["prediction_results"]["status"], "warn")
        self.assertEqual(names["prediction_results"]["required"], "false")
        self.assertIn("可交易匹配 1 个", names["prediction_results"]["detail"])

    def test_production_readiness_accepts_explicit_security_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                con.execute("UPDATE market_prices SET source='csv', as_of=date('now')")
                con.commit()
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "x" * 32,
                        "OWQ_ADMIN_USER_IDS": "",
                        "OWQ_ADMIN_EMAILS": "admin@example.com",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_COOKIE_SECURE": "1",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.example.com",
                    },
                    clear=False,
                ):
                    health = doctor.health(con)
            finally:
                con.close()

        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["ok"])
        self.assertEqual(health["required_warnings"], 0)

    def test_production_readiness_requires_recoverable_admin_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                user_id = services.get_or_create_user(con, "legacy-admin-openid", "Legacy Admin")
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "x" * 32,
                        "OWQ_ADMIN_USER_IDS": str(user_id),
                        "OWQ_ADMIN_EMAILS": "",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_COOKIE_SECURE": "1",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.example.com",
                    },
                    clear=False,
                ):
                    health = doctor.health(con)
            finally:
                con.close()

        names = {row["name"]: row for row in health["checks"]}
        self.assertEqual(names["admin_config"]["status"], "ok")
        self.assertEqual(names["admin_access"]["status"], "warn")
        self.assertEqual(names["admin_access"]["required"], "true")
        self.assertIn("缺少账号密码登录", names["admin_access"]["detail"])
        self.assertFalse(health["ok"])

    def test_admin_access_accepts_email_password_login_without_login_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                user_id = services.get_or_create_email_user(con, "admin@example.com")
                con.execute(
                    "UPDATE users SET login_name='', password_hash=? WHERE id=?",
                    (services.password_hash("Password1234"), user_id),
                )
                con.commit()
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ADMIN_USER_IDS": str(user_id),
                        "OWQ_ADMIN_EMAILS": "",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                    },
                    clear=False,
                ):
                    ok, detail = doctor.configured_admin_access_check(con, email_configured=False, email_dev_auth=False)
            finally:
                con.close()

        self.assertTrue(ok)
        self.assertIn("用户名/邮箱 + 密码", detail)

    def test_admin_access_does_not_treat_email_dev_auth_as_recoverable_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                user_id = services.get_or_create_user(con, "legacy-admin-openid", "Legacy Admin")
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ADMIN_USER_IDS": str(user_id),
                        "OWQ_ADMIN_EMAILS": "",
                        "OWQ_EMAIL_DEV_AUTH": "1",
                    },
                    clear=False,
                ):
                    ok, detail = doctor.configured_admin_access_check(con, email_configured=False, email_dev_auth=True)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("测试验证入口不能替代", detail)

    def test_production_readiness_warns_when_demo_participants_remain_after_beta(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.seed_demo_competition(con)
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["demo_contest_participants"]["status"], "warn")
        self.assertEqual(names["demo_contest_participants"]["required"], "false")
        self.assertIn("演示/开发参赛账户", names["demo_contest_participants"]["detail"])

    def test_public_beta_allows_demo_participants_with_explicit_warning_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.seed_demo_competition(con)
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "1",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["demo_contest_participants"]["status"], "ok")
        self.assertIn("beta 测试可保留", names["demo_contest_participants"]["detail"])
        self.assertEqual(names["email_dev_auth_public_links"]["status"], "ok")

    def test_public_beta_warns_if_email_dev_auth_links_are_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "1",
                        "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "1",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_dev_auth_public_links"]["status"], "warn")
        self.assertIn("OWQ_EMAIL_DEV_AUTH_SHOW_LINKS", names["email_dev_auth_public_links"]["detail"])

    def test_email_sending_check_rejects_invalid_sender_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply",
                        "OWQ_SMTP_HOST": "smtp.example.com",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_sending"]["status"], "warn")
        self.assertIn("OWQ_EMAIL_FROM", names["email_sending"]["detail"])
        self.assertEqual(names["email_login"]["status"], "warn")

    def test_email_sending_check_requires_auth_for_cloudflare_smtp(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.mx.cloudflare.net",
                        "OWQ_SMTP_PORT": "465",
                        "OWQ_SMTP_SSL": "1",
                        "OWQ_SMTP_TLS": "0",
                        "OWQ_SMTP_USER": "",
                        "OWQ_SMTP_PASSWORD": "",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_sending"]["status"], "warn")
        self.assertIn("SMTP 认证", names["email_sending"]["detail"])
        self.assertIn("OWQ_SMTP_PASSWORD", names["email_sending"]["detail"])

    def test_email_sending_check_requires_auth_for_gmail_smtp(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_EMAIL_PROVIDER": "smtp",
                        "OWQ_EMAIL_FROM": "noreply@example.com",
                        "OWQ_SMTP_HOST": "smtp.gmail.com",
                        "OWQ_SMTP_PORT": "587",
                        "OWQ_SMTP_TLS": "1",
                        "OWQ_SMTP_SSL": "0",
                        "OWQ_SMTP_USER": "noreply@example.com",
                        "OWQ_SMTP_PASSWORD": "",
                    },
                    clear=False,
                ):
                    checks = doctor.check(con)
            finally:
                con.close()

        names = {row["name"]: row for row in checks}
        self.assertEqual(names["email_sending"]["status"], "warn")
        self.assertIn("smtp.gmail.com", names["email_sending"]["detail"])
        self.assertIn("OWQ_SMTP_PASSWORD", names["email_sending"]["detail"])

    def test_email_delivery_probe_skips_public_beta_test_login_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                ok, detail = doctor.recent_email_delivery_check(con, email_configured=False, email_dev_auth=True)
            finally:
                con.close()

        self.assertTrue(ok)
        self.assertIn("公测测试登录", detail)

    def test_email_delivery_probe_warns_when_configured_without_successful_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "cli.email_test_failed", target_type="email", target_id="ops@example.com")
                ok, detail = doctor.recent_email_delivery_check(con, email_configured=True, email_dev_auth=False)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("未找到成功发信诊断记录", detail)

    def test_email_delivery_probe_accepts_recent_successful_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(
                    con,
                    None,
                    "admin.email_test",
                    target_type="email",
                    target_id="ops@example.com",
                    detail={"provider": "smtp"},
                )
                ok, detail = doctor.recent_email_delivery_check(con, email_configured=True, email_dev_auth=False)
            finally:
                con.close()

        self.assertTrue(ok)
        self.assertNotIn("ops@example.com", detail)
        self.assertIn("收件哈希", detail)
        self.assertIn(services.email_token_hash("ops@example.com")[:16], detail)
        self.assertIn("provider=smtp", detail)

    def test_email_delivery_probe_warns_when_successful_test_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                event_id = services.record_audit_event(
                    con,
                    None,
                    "cli.email_test",
                    target_type="email",
                    target_id="ops@example.com",
                    detail={"provider": "smtp"},
                )
                con.execute("UPDATE audit_events SET created_at=datetime('now', '-3 hours') WHERE id=?", (event_id,))
                con.commit()
                with patch.dict("os.environ", {"OWQ_EMAIL_TEST_MAX_AGE_HOURS": "1"}, clear=False):
                    ok, detail = doctor.recent_email_delivery_check(con, email_configured=True, email_dev_auth=False)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("阈值 1 小时", detail)

    def test_email_delivery_probe_warns_on_invalid_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.bootstrap(Path(tmp) / "app.sqlite")
            try:
                services.record_audit_event(con, None, "cli.email_test", target_type="email", target_id="ops@example.com")
                with patch.dict("os.environ", {"OWQ_EMAIL_TEST_MAX_AGE_HOURS": "soon"}, clear=False):
                    ok, detail = doctor.recent_email_delivery_check(con, email_configured=True, email_dev_auth=False)
            finally:
                con.close()

        self.assertFalse(ok)
        self.assertIn("OWQ_EMAIL_TEST_MAX_AGE_HOURS", detail)

    def test_strict_doctor_cli_fails_until_optional_warnings_are_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            status = main(["--db", str(app_path), "--doctor-strict"])

        self.assertEqual(status, 1)

    @unittest.skipUnless(_LOCAL_DATA_DIR.is_dir(), "needs local data/ dir (disk + market.duckdb checks)")
    def test_strict_doctor_cli_passes_when_formal_readiness_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            pred_path = Path(tmp) / "predictions.csv"
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            con = db.bootstrap(app_path)
            try:
                con.execute("DELETE FROM market_prices")
                market_rows = [(f"{i:06d}.SZ", f"测试标的{i}") for i in range(1, 301)]
                con.executemany(
                    """
                    INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
                    VALUES (?, ?, 10, 9.8, 'csv', date('now'))
                    """,
                    market_rows,
                )
                con.commit()
                services.record_audit_event(
                    con,
                    None,
                    "cli.email_test",
                    target_type="email",
                    target_id="ops@example.com",
                    detail={"provider": "smtp"},
                )
                services.record_audit_event(
                    con,
                    None,
                    "cli.market_sync_succeeded",
                    target_type="market_sync",
                    target_id="public",
                    detail={"status": "succeeded"},
                )
                db.backup_database(con, backup_dir / "app-20260624-120000.sqlite")
            finally:
                con.close()
            today = date.today().isoformat()
            pred_path.write_text(
                "code,prediction,date\n"
                + "".join(f"{code},0.01,{today}\n" for code, _ in market_rows[:10]),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "OWQ_ENV": "production",
                        "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                        "OWQ_SECRET": "x" * 32,
                        "OWQ_ADMIN_USER_IDS": "",
                        "OWQ_ADMIN_EMAILS": "admin@example.com",
                        "OWQ_EMAIL_DEV_AUTH": "0",
                        "OWQ_COOKIE_SECURE": "1",
                    "OWQ_EMAIL_PROVIDER": "smtp",
                    "OWQ_EMAIL_FROM": "noreply@example.com",
                    "OWQ_SMTP_HOST": "smtp.example.com",
                    "OWQ_PREDICTIONS_CSV": str(pred_path),
                    "OWQ_APP_BACKUP_DIR": str(backup_dir),
                },
                clear=False,
            ):
                status = main(["--db", str(app_path), "--doctor-strict"])

        self.assertEqual(status, 0)

    def test_app_database_backup_writes_consistent_sqlite_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "backup.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute(
                    "INSERT INTO users(nickname, wechat_openid) VALUES (?, ?)",
                    ("备份用户", "backup-openid"),
                )
                con.commit()
                written = db.backup_database(con, backup_path)
            finally:
                con.close()

            self.assertEqual(written, backup_path)
            backup = sqlite3.connect(backup_path)
            try:
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                row = backup.execute("SELECT nickname FROM users WHERE wechat_openid='backup-openid'").fetchone()
            finally:
                backup.close()
            self.assertEqual(row[0], "备份用户")

    def test_app_database_backup_verify_reports_restore_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "backup.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute(
                    "INSERT INTO users(nickname, wechat_openid) VALUES (?, ?)",
                    ("验证备份用户", "verify-backup-openid"),
                )
                con.commit()
                db.backup_database(con, backup_path)
            finally:
                con.close()

            result = db.verify_backup_file(backup_path)
            self.assertEqual(result["quick_check"], "ok")
            self.assertGreaterEqual(result["row_counts"]["users"], 1)
            self.assertIn("market_prices", result["row_counts"])

    def test_app_database_backup_verify_rejects_incomplete_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.sqlite"
            con = sqlite3.connect(broken)
            try:
                con.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
                con.commit()
            finally:
                con.close()

            with self.assertRaises(sqlite3.DatabaseError):
                db.verify_backup_file(broken)

    def test_app_database_backup_can_restore_to_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "backup.sqlite"
            restore_path = Path(tmp) / "restore.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute(
                    "INSERT INTO users(nickname, wechat_openid) VALUES (?, ?)",
                    ("恢复用户", "restore-openid"),
                )
                con.commit()
                db.backup_database(con, backup_path)
            finally:
                con.close()

            result = db.restore_backup_file(backup_path, restore_path)

            self.assertEqual(Path(result["path"]), restore_path)
            restored = sqlite3.connect(restore_path)
            try:
                row = restored.execute("SELECT nickname FROM users WHERE wechat_openid='restore-openid'").fetchone()
                self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                restored.close()
            self.assertEqual(row[0], "恢复用户")

    def test_app_database_restore_refuses_to_overwrite_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "backup.sqlite"
            restore_path = Path(tmp) / "restore.sqlite"
            restore_path.write_text("existing", encoding="utf-8")
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_path)
            finally:
                con.close()

            with self.assertRaises(FileExistsError):
                db.restore_backup_file(backup_path, restore_path)

            self.assertEqual(restore_path.read_text(encoding="utf-8"), "existing")

    def test_app_database_restore_can_overwrite_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "backup.sqlite"
            restore_path = Path(tmp) / "restore.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute(
                    "INSERT INTO users(nickname, wechat_openid) VALUES (?, ?)",
                    ("覆盖恢复用户", "overwrite-restore-openid"),
                )
                con.commit()
                db.backup_database(con, backup_path)
            finally:
                con.close()
            sqlite3.connect(restore_path).close()

            result = db.restore_backup_file(backup_path, restore_path, overwrite=True)

            self.assertEqual(result["quick_check"], "ok")
            restored = sqlite3.connect(restore_path)
            try:
                row = restored.execute("SELECT nickname FROM users WHERE wechat_openid='overwrite-restore-openid'").fetchone()
            finally:
                restored.close()
            self.assertEqual(row[0], "覆盖恢复用户")

    def test_app_database_auto_backup_prunes_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            for index in range(5):
                path = backup_dir / f"app-20250101-00000{index}.sqlite"
                path.write_text("old", encoding="utf-8")
                path.touch()
            con = db.bootstrap(app_path)
            try:
                with patch.dict("os.environ", {"OWQ_APP_BACKUP_KEEP": "3"}, clear=False), patch.object(db, "DEFAULT_BACKUP_DIR", backup_dir):
                    written = db.backup_database(con)
            finally:
                con.close()

            backups = sorted(path.name for path in backup_dir.glob("app-*.sqlite"))
            self.assertIn(written.name, backups)
            self.assertLessEqual(len(backups), 3)

    def test_explicit_app_database_backup_does_not_prune_default_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_dir = Path(tmp) / "backups"
            backup_dir.mkdir()
            old_path = backup_dir / "app-20250101-000000.sqlite"
            old_path.write_text("old", encoding="utf-8")
            explicit_path = Path(tmp) / "manual.sqlite"
            con = db.bootstrap(app_path)
            try:
                with patch.dict("os.environ", {"OWQ_APP_BACKUP_KEEP": "1"}, clear=False), patch.object(db, "DEFAULT_BACKUP_DIR", backup_dir):
                    written = db.backup_database(con, explicit_path)
            finally:
                con.close()

            self.assertEqual(written, explicit_path)
            self.assertTrue(old_path.exists())

    def test_bootstrap_migrates_existing_users_table_without_email_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "old-app.sqlite"
            old = sqlite3.connect(app_path)
            try:
                old.execute(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        nickname TEXT NOT NULL,
                        wechat_openid TEXT NOT NULL UNIQUE,
                        avatar_url TEXT DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                old.commit()
            finally:
                old.close()

            con = db.bootstrap(app_path)
            try:
                user_cols = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
                email_sessions = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='email_login_sessions'"
                ).fetchone()
            finally:
                con.close()

        self.assertIn("email", user_cols)
        self.assertIn("login_name", user_cols)
        self.assertIn("password_hash", user_cols)
        self.assertIn("password_updated_at", user_cols)
        self.assertIn("session_version", user_cols)
        self.assertIsNotNone(email_sessions)

    def test_backup_cli_writes_requested_path_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "cli-backup.sqlite"
            status = main(["--db", str(app_path), "--backup-app-db", str(backup_path)])

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.backup")
            self.assertEqual(events[0]["target_id"], backup_path.name)
            self.assertIn('"file":', events[0]["detail"])
            self.assertNotIn(str(backup_path.parent), events[0]["detail"])
            backup = sqlite3.connect(backup_path)
            try:
                self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertGreater(backup.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0], 0)
            finally:
                backup.close()

    def test_backup_verify_cli_checks_backup_without_touching_live_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "cli-verify.sqlite"
            live_path = Path(tmp) / "should-not-be-created.sqlite"
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_path)
            finally:
                con.close()

            status = main(["--db", str(live_path), "--verify-app-backup", str(backup_path)])

            self.assertEqual(status, 0)
            self.assertFalse(live_path.exists())

    def test_backup_verify_cli_fails_for_missing_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.sqlite"
            status = main(["--verify-app-backup", str(missing)])

            self.assertEqual(status, 1)

    def test_backup_restore_cli_restores_without_touching_live_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "cli-restore-source.sqlite"
            restore_path = Path(tmp) / "cli-restore-target.sqlite"
            live_path = Path(tmp) / "should-not-be-created.sqlite"
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_path)
            finally:
                con.close()

            status = main(["--db", str(live_path), "--restore-app-backup", str(backup_path), str(restore_path)])

            self.assertEqual(status, 0)
            self.assertFalse(live_path.exists())
            result = db.verify_backup_file(restore_path)
            self.assertEqual(result["quick_check"], "ok")

    def test_backup_restore_cli_requires_overwrite_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            backup_path = Path(tmp) / "cli-restore-source.sqlite"
            restore_path = Path(tmp) / "cli-restore-target.sqlite"
            restore_path.write_text("existing", encoding="utf-8")
            con = db.bootstrap(app_path)
            try:
                db.backup_database(con, backup_path)
            finally:
                con.close()

            status = main(["--restore-app-backup", str(backup_path), str(restore_path)])
            self.assertEqual(status, 1)
            status = main(["--restore-app-backup", str(backup_path), str(restore_path), "--restore-overwrite"])
            self.assertEqual(status, 0)

    def test_sqlite_maintenance_returns_checkpoint_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute("INSERT INTO users(nickname, wechat_openid) VALUES (?, ?)", ("维护用户", "maintenance-openid"))
                con.commit()
                result = db.sqlite_maintenance(con)
            finally:
                con.close()

        self.assertEqual(os.path.realpath(result["db_path"]), os.path.realpath(app_path))
        self.assertIn("wal_before_bytes", result)
        self.assertIn("wal_after_bytes", result)
        self.assertIsInstance(result["checkpoint"], tuple)

    def test_sqlite_maintenance_cli_records_audit_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            status = main(["--db", str(app_path), "--sqlite-maintenance"])

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.sqlite_maintenance")

    def test_audit_prune_cli_deletes_expired_events_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                old_id = services.record_audit_event(con, None, "old.audit", target_type="unit")
                services.record_audit_event(con, None, "recent.audit", target_type="unit")
                con.execute("UPDATE audit_events SET created_at=datetime('now', '-45 days') WHERE id=?", (old_id,))
                con.commit()
            finally:
                con.close()

            status = main(["--db", str(app_path), "--prune-audit-log", "--audit-retention-days", "30"])

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.audit_prune")
            self.assertIn('"deleted": "1"', events[0]["detail"])
            self.assertNotIn("old.audit", [event["action"] for event in events])
            self.assertIn("recent.audit", [event["action"] for event in events])

    def test_email_login_prune_cli_deletes_expired_sessions_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                token = services.create_email_login_session(
                    con,
                    "old-login@example.com",
                    "2026-06-24",
                    "2026-06-24",
                    "2026-06-24",
                    enforce_rate_limit=False,
                )
                services.confirm_email_login_session(con, token)
                con.execute(
                    "UPDATE email_login_sessions SET created_at=datetime('now', '-45 days') WHERE email=?",
                    ("old-login@example.com",),
                )
                con.commit()
            finally:
                con.close()

            status = main(
                [
                    "--db",
                    str(app_path),
                    "--prune-email-login-sessions",
                    "--email-login-session-retention-days",
                    "30",
                ]
            )

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
                remaining = con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.email_login_prune")
            self.assertIn('"deleted": "1"', events[0]["detail"])
            self.assertEqual(remaining, 0)

    def test_demo_contest_clean_cli_removes_demo_participants_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                services.seed_demo_competition(con)
            finally:
                con.close()

            status = main(["--db", str(app_path), "--remove-demo-contest-participants"])

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
                summary = services.demo_contest_participant_summary(con)
                demo_users = con.execute("SELECT COUNT(*) FROM users WHERE wechat_openid LIKE 'demo-%'").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.demo_contest_clean")
            self.assertIn('"participants_removed": "3"', events[0]["detail"])
            self.assertEqual(summary["participants"], 0)
            self.assertEqual(demo_users, 3)

    def test_set_user_password_cli_reads_password_from_env_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                user_id = services.get_or_create_email_user(con, "ops@example.com")
                con.commit()
            finally:
                con.close()

            with patch.dict("os.environ", {"OWQ_TEST_PASSWORD": "Password1234"}, clear=False):
                status = main(
                    [
                        "--db",
                        str(app_path),
                        "--set-user-password",
                        str(user_id),
                        "--login-name",
                        "ops-admin",
                        "--password-env",
                        "OWQ_TEST_PASSWORD",
                    ]
                )

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
                authed = services.authenticate_user(con, "ops-admin", "Password1234")
            finally:
                con.close()
            self.assertEqual(authed, user_id)
            self.assertEqual(events[0]["action"], "cli.user_password_set")
            self.assertIn('"login_name": "ops-admin"', events[0]["detail"])
            self.assertNotIn("Password1234", events[0]["detail"])

    def test_record_market_sync_status_cli_records_audit_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            status = main(
                [
                    "--db",
                    str(app_path),
                    "--record-market-sync-status",
                    "failed",
                    "--market-sync-exit-code",
                    "7",
                    "--market-sync-message",
                    "unit-test",
                ]
            )

            self.assertEqual(status, 0)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.market_sync_failed")
            self.assertIn('"exit_code": "7"', events[0]["detail"])
            self.assertIn('"message": "unit-test"', events[0]["detail"])

    def test_email_test_cli_sends_diagnostic_and_records_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            with patch.dict(
                "os.environ",
                {
                    "OWQ_EMAIL_PROVIDER": "smtp",
                    "OWQ_EMAIL_FROM": "noreply@example.com",
                    "OWQ_SMTP_HOST": "smtp.example.com",
                    "OWQ_EMAIL_DEV_AUTH": "0",
                },
                clear=False,
            ), patch("src.app.server.AppHandler.send_transactional_email", return_value="smtp") as sender:
                status = main(["--db", str(app_path), "--send-test-email", "Ops@Test.Example"])

            self.assertEqual(status, 0)
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0], "ops@test.example")
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.email_test")
            recipient_hash = services.email_token_hash("ops@test.example")[:16]
            self.assertEqual(events[0]["target_id"], recipient_hash)
            self.assertIn(f'"recipient_hash": "{recipient_hash}"', events[0]["detail"])
            self.assertNotIn("ops@test.example", events[0]["detail"])

    def test_email_test_cli_reports_failure_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            with patch.dict(
                "os.environ",
                {
                    "OWQ_EMAIL_PROVIDER": "smtp",
                    "OWQ_EMAIL_FROM": "noreply@example.com",
                    "OWQ_SMTP_HOST": "smtp.example.com",
                    "OWQ_SMTP_PASSWORD": "super-secret-password",
                    "OWQ_EMAIL_DEV_AUTH": "0",
                },
                clear=False,
            ), patch("src.app.server.AppHandler.send_transactional_email", side_effect=RuntimeError("smtp down super-secret-password")):
                status = main(["--db", str(app_path), "--send-test-email", "ops@example.com"])

            self.assertEqual(status, 1)
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.email_test_failed")
            self.assertEqual(events[0]["target_id"], services.email_token_hash("ops@example.com")[:16])
            self.assertNotIn("ops@example.com", events[0]["detail"])
            self.assertNotIn("super-secret-password", events[0]["detail"])
            self.assertIn("[redacted]", events[0]["detail"])

    def test_env_file_loader_parses_quoted_values_and_secret_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "app.secret"
            secret_path.write_text("loaded-secret\n", encoding="utf-8")
            env_path = Path(tmp) / "public.env"
            env_path.write_text(
                f"""
OWQ_EMAIL_FROM_NAME="OurWorlds Quant"
OWQ_SECRET_FILE={secret_path}
OWQ_PORT=9090
""",
                encoding="utf-8",
            )

            original_secret = server.SECRET
            with patch.dict(os.environ, {}, clear=True):
                try:
                    loaded = server.load_env_file(env_path)
                    self.assertEqual(loaded["OWQ_EMAIL_FROM_NAME"], "OurWorlds Quant")
                    self.assertEqual(os.environ["OWQ_SECRET"], "loaded-secret")
                    self.assertEqual(server.SECRET, "loaded-secret")
                finally:
                    server.SECRET = original_secret

    def test_email_test_cli_can_load_configuration_from_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "env-app.sqlite"
            env_path = Path(tmp) / "public.env"
            env_path.write_text(
                f"""
OWQ_APP_DB={app_path}
OWQ_EMAIL_PROVIDER=smtp
OWQ_EMAIL_FROM=noreply@example.com
OWQ_EMAIL_FROM_NAME="OurWorlds Quant"
OWQ_SMTP_HOST=smtp.example.com
OWQ_EMAIL_DEV_AUTH=0
""",
                encoding="utf-8",
            )

            def fake_send(email, subject, text, html):
                self.assertEqual(os.environ["OWQ_EMAIL_FROM_NAME"], "OurWorlds Quant")
                return os.environ["OWQ_EMAIL_PROVIDER"]

            original_secret = server.SECRET
            with patch.dict(os.environ, {}, clear=True), patch(
                "src.app.server.AppHandler.send_transactional_email",
                side_effect=fake_send,
            ) as sender:
                try:
                    status = main(["--env-file", str(env_path), "--send-test-email", "ops@example.com"])
                finally:
                    server.SECRET = original_secret

            self.assertEqual(status, 0)
            sender.assert_called_once()
            con = db.connect(app_path)
            try:
                events = services.audit_events(con)
            finally:
                con.close()
            self.assertEqual(events[0]["action"], "cli.email_test")
            self.assertEqual(events[0]["target_id"], services.email_token_hash("ops@example.com")[:16])
            self.assertIn('"provider": "smtp"', events[0]["detail"])
            self.assertIn('"recipient_hash":', events[0]["detail"])
            self.assertNotIn("ops@example.com", events[0]["detail"])


if __name__ == "__main__":
    unittest.main()

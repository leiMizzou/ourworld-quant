"""Operational readiness checks for the local paper-trading app."""
from __future__ import annotations

import os
import sqlite3
import sys
import csv
import json
import shutil
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import db, email_config, services


DEFAULT_SECRET = "local-dev-secret-change-me"
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
DEFAULT_MARKET_MAX_STALENESS_DAYS = 10
DEFAULT_MARKET_MIN_REAL_CODES = 300
DEFAULT_PREDICTIONS_MIN_CODES = 10
DEFAULT_MAX_FORM_BYTES = 1024 * 1024
DEFAULT_MIN_FREE_DISK_MB = 1024
DEFAULT_BACKUP_MAX_AGE_HOURS = 48
DEFAULT_MARKET_SYNC_MAX_AGE_HOURS = 36
DEFAULT_SQLITE_MAX_WAL_MB = 256
DEFAULT_EMAIL_TEST_MAX_AGE_HOURS = 72
DEFAULT_OPERATIONAL_QUEUE_MAX_AGE_HOURS = 72
DEFAULT_SERVER_ERROR_WINDOW_HOURS = 24
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
MIN_BUSY_TIMEOUT_MS = 1000


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def production_mode() -> bool:
    public_base = os.getenv("OWQ_PUBLIC_BASE_URL", "").strip().lower()
    env = os.getenv("OWQ_ENV", "").strip().lower()
    return env in {"prod", "production"} or public_base.startswith("https://")


def parse_market_date(value: str | None) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date()
    except ValueError:
        return None


def parse_audit_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def market_max_staleness_days() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_MARKET_MAX_STALENESS_DAYS", "").strip()
    if not raw:
        return DEFAULT_MARKET_MAX_STALENESS_DAYS, True, f"{DEFAULT_MARKET_MAX_STALENESS_DAYS} 天"
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_MARKET_MAX_STALENESS_DAYS, False, "OWQ_MARKET_MAX_STALENESS_DAYS 必须是整数天数"
    if days < 1 or days > 90:
        return DEFAULT_MARKET_MAX_STALENESS_DAYS, False, "行情新鲜度阈值应在 1 到 90 天之间"
    return days, True, f"{days} 天"


def market_min_real_codes() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_MARKET_MIN_REAL_CODES", "").strip()
    if not raw:
        return DEFAULT_MARKET_MIN_REAL_CODES, True, f"{DEFAULT_MARKET_MIN_REAL_CODES} 个标的"
    try:
        count = int(raw)
    except ValueError:
        return DEFAULT_MARKET_MIN_REAL_CODES, False, "OWQ_MARKET_MIN_REAL_CODES 必须是整数"
    if count < 1 or count > 10000:
        return DEFAULT_MARKET_MIN_REAL_CODES, False, "真实行情覆盖阈值应在 1 到 10000 个标的之间"
    return count, True, f"{count} 个标的"


def predictions_min_codes() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_PREDICTIONS_MIN_CODES", "").strip()
    if not raw:
        return DEFAULT_PREDICTIONS_MIN_CODES, True, f"{DEFAULT_PREDICTIONS_MIN_CODES} 个候选"
    try:
        count = int(raw)
    except ValueError:
        return DEFAULT_PREDICTIONS_MIN_CODES, False, "OWQ_PREDICTIONS_MIN_CODES 必须是整数"
    if count < 1 or count > 1000:
        return DEFAULT_PREDICTIONS_MIN_CODES, False, "预测候选覆盖阈值应在 1 到 1000 个候选之间"
    return count, True, f"{count} 个候选"


def market_sync_max_age_hours() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_MARKET_SYNC_MAX_AGE_HOURS", "").strip()
    if not raw:
        return DEFAULT_MARKET_SYNC_MAX_AGE_HOURS, True, f"{DEFAULT_MARKET_SYNC_MAX_AGE_HOURS} 小时"
    try:
        hours = int(raw)
    except ValueError:
        return DEFAULT_MARKET_SYNC_MAX_AGE_HOURS, False, "OWQ_MARKET_SYNC_MAX_AGE_HOURS 必须是整数小时"
    if hours < 1 or hours > 24 * 14:
        return DEFAULT_MARKET_SYNC_MAX_AGE_HOURS, False, "市场同步任务新鲜度阈值应在 1 到 336 小时之间"
    return hours, True, f"{hours} 小时"


def request_body_limit() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_MAX_FORM_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_FORM_BYTES, True, f"表单请求体上限 {DEFAULT_MAX_FORM_BYTES} 字节"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_FORM_BYTES, False, "OWQ_MAX_FORM_BYTES 必须是整数"
    if value < 4096 or value > 5 * 1024 * 1024:
        return DEFAULT_MAX_FORM_BYTES, False, "表单请求体上限应在 4096 字节到 5MB 之间"
    return value, True, f"表单请求体上限 {value} 字节"


def legal_consent_gate_check(prod: bool | None = None) -> tuple[bool, str]:
    production = production_mode() if prod is None else bool(prod)
    raw = os.getenv("OWQ_LEGAL_CONSENT_REQUIRED", "").strip().lower()
    if raw in TRUE_VALUES:
        enabled = True
    elif raw in FALSE_VALUES:
        enabled = False
    elif raw:
        return False, "OWQ_LEGAL_CONSENT_REQUIRED 必须是 1/0、true/false、yes/no 或 on/off"
    else:
        enabled = production
    if production and not enabled:
        return False, "OWQ_LEGAL_CONSENT_REQUIRED=0 会允许用户绕过当前服务条款确认"
    if enabled:
        return True, "公网/生产会强制用户确认当前服务条款、隐私说明和风险提示"
    return True, "本地开发未强制法律条款补签"


def min_free_disk_mb() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_MIN_FREE_DISK_MB", "").strip()
    if not raw:
        return DEFAULT_MIN_FREE_DISK_MB, True, f"{DEFAULT_MIN_FREE_DISK_MB} MB"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MIN_FREE_DISK_MB, False, "OWQ_MIN_FREE_DISK_MB 必须是整数 MB"
    if value < 64 or value > 10 * 1024 * 1024:
        return DEFAULT_MIN_FREE_DISK_MB, False, "磁盘剩余空间阈值应在 64 MB 到 10485760 MB 之间"
    return value, True, f"{value} MB"


def backup_max_age_hours() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_APP_BACKUP_MAX_AGE_HOURS", "").strip()
    if not raw:
        return DEFAULT_BACKUP_MAX_AGE_HOURS, True, f"{DEFAULT_BACKUP_MAX_AGE_HOURS} 小时"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BACKUP_MAX_AGE_HOURS, False, "OWQ_APP_BACKUP_MAX_AGE_HOURS 必须是整数小时"
    if value < 1 or value > 24 * 30:
        return DEFAULT_BACKUP_MAX_AGE_HOURS, False, "备份新鲜度阈值应在 1 到 720 小时之间"
    return value, True, f"{value} 小时"


def sqlite_max_wal_mb() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_SQLITE_MAX_WAL_MB", "").strip()
    if not raw:
        return DEFAULT_SQLITE_MAX_WAL_MB, True, f"{DEFAULT_SQLITE_MAX_WAL_MB} MB"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SQLITE_MAX_WAL_MB, False, "OWQ_SQLITE_MAX_WAL_MB 必须是整数 MB"
    if value < 1 or value > 1024 * 100:
        return DEFAULT_SQLITE_MAX_WAL_MB, False, "WAL 大小阈值应在 1 MB 到 102400 MB 之间"
    return value, True, f"{value} MB"


def email_test_max_age_hours() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_EMAIL_TEST_MAX_AGE_HOURS", "").strip()
    if not raw:
        return DEFAULT_EMAIL_TEST_MAX_AGE_HOURS, True, f"{DEFAULT_EMAIL_TEST_MAX_AGE_HOURS} 小时"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_EMAIL_TEST_MAX_AGE_HOURS, False, "OWQ_EMAIL_TEST_MAX_AGE_HOURS 必须是整数小时"
    if value < 1 or value > 24 * 30:
        return DEFAULT_EMAIL_TEST_MAX_AGE_HOURS, False, "邮件发信诊断新鲜度阈值应在 1 到 720 小时之间"
    return value, True, f"{value} 小时"


def operational_queue_max_age_hours() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS", "").strip()
    if not raw:
        return DEFAULT_OPERATIONAL_QUEUE_MAX_AGE_HOURS, True, f"{DEFAULT_OPERATIONAL_QUEUE_MAX_AGE_HOURS} 小时"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OPERATIONAL_QUEUE_MAX_AGE_HOURS, False, "OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS 必须是整数小时"
    if value < 1 or value > 24 * 30:
        return DEFAULT_OPERATIONAL_QUEUE_MAX_AGE_HOURS, False, "运营待处理队列阈值应在 1 到 720 小时之间"
    return value, True, f"{value} 小时"


def operational_queue_check(con: sqlite3.Connection) -> tuple[bool, str]:
    max_age_hours, threshold_ok, threshold_detail = operational_queue_max_age_hours()
    if not threshold_ok:
        return False, threshold_detail
    summary = services.operational_queue_summary(con)
    support = summary["support_open"]
    reports = summary["content_reports_pending"]
    max_age = max(float(support["oldest_age_hours"]), float(reports["oldest_age_hours"]))
    ok = max_age <= max_age_hours
    support_detail = (
        f"支持请求 {support['count']} 条"
        + (
            f", 最久 {float(support['oldest_age_hours']):.1f} 小时, 最早 {support['oldest_at']}"
            if support["count"]
            else ""
        )
    )
    report_detail = (
        f"内容举报 {reports['count']} 条"
        + (
            f", 最久 {float(reports['oldest_age_hours']):.1f} 小时, 最早 {reports['oldest_at']}"
            if reports["count"]
            else ""
        )
    )
    return ok, f"{support_detail}; {report_detail}; 阈值 {threshold_detail}"


def server_error_window_hours() -> tuple[int, bool, str]:
    raw = os.getenv("OWQ_SERVER_ERROR_WINDOW_HOURS", "").strip()
    if not raw:
        return DEFAULT_SERVER_ERROR_WINDOW_HOURS, True, f"{DEFAULT_SERVER_ERROR_WINDOW_HOURS} 小时"
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SERVER_ERROR_WINDOW_HOURS, False, "OWQ_SERVER_ERROR_WINDOW_HOURS 必须是整数小时"
    if value < 1 or value > 24 * 14:
        return DEFAULT_SERVER_ERROR_WINDOW_HOURS, False, "服务端异常观察窗口应在 1 到 336 小时之间"
    return value, True, f"{value} 小时"


def recent_server_errors_check(con: sqlite3.Connection) -> tuple[bool, str]:
    hours, threshold_ok, threshold_detail = server_error_window_hours()
    if not threshold_ok:
        return False, threshold_detail
    window = f"-{hours} hours"
    row = con.execute(
        """
        SELECT COUNT(*) AS count,
               MAX(created_at) AS latest_at
        FROM audit_events
        WHERE action='server.error'
          AND created_at >= datetime('now', ?)
        """,
        (window,),
    ).fetchone()
    count = int(row["count"] or 0)
    if count == 0:
        return True, f"近 {threshold_detail} 未记录 server.error"
    latest = str(row["latest_at"] or "")
    latest_row = con.execute(
        """
        SELECT target_id, detail, ip_address
        FROM audit_events
        WHERE action='server.error'
          AND created_at >= datetime('now', ?)
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (window,),
    ).fetchone()
    target = str(latest_row["target_id"] or "unknown") if latest_row else "unknown"
    return False, f"近 {threshold_detail} 记录 {count} 次 server.error, 最近 {latest}, 目标 {target[:80]}"


def existing_disk_check_path() -> Path:
    configured = os.getenv("OWQ_DISK_CHECK_PATH", "").strip()
    if configured:
        path = Path(configured)
    else:
        db_path = Path(os.getenv("OWQ_APP_DB", "data/app.sqlite"))
        path = db_path.parent
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def disk_space_check() -> tuple[bool, str]:
    threshold_mb, threshold_ok, threshold_detail = min_free_disk_mb()
    if not threshold_ok:
        return False, threshold_detail
    path = existing_disk_check_path()
    try:
        usage = shutil.disk_usage(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法读取磁盘空间: {path} ({exc})"
    free_mb = int(usage.free / (1024 * 1024))
    total_mb = int(usage.total / (1024 * 1024))
    ok = free_mb >= threshold_mb
    return ok, f"{path} 可用 {free_mb} MB / 总计 {total_mb} MB, 阈值 {threshold_detail}"


def app_db_foreign_key_check(con: sqlite3.Connection) -> tuple[bool, str]:
    errors = con.execute("PRAGMA foreign_key_check").fetchall()
    if not errors:
        return True, "ok"
    first = errors[0]
    table = first[0] if len(first) > 0 else "unknown"
    rowid = first[1] if len(first) > 1 else "unknown"
    parent = first[2] if len(first) > 2 else "unknown"
    return False, f"{len(errors)} 条外键错误,首个: table={table}, rowid={rowid}, parent={parent}"


def sqlite_main_db_path(con: sqlite3.Connection) -> Path | None:
    row = con.execute("PRAGMA database_list").fetchone()
    if not row:
        return None
    path = row[2]
    if not path:
        return None
    return Path(path)


def sqlite_wal_size_check(con: sqlite3.Connection) -> tuple[bool, str]:
    threshold_mb, threshold_ok, threshold_detail = sqlite_max_wal_mb()
    if not threshold_ok:
        return False, threshold_detail
    db_path = sqlite_main_db_path(con)
    if db_path is None or str(db_path) == ":memory:":
        return True, "内存数据库无 WAL 文件"
    wal_path = Path(str(db_path) + "-wal")
    if not wal_path.exists():
        return True, f"WAL 文件不存在,阈值 {threshold_detail}"
    try:
        size_bytes = wal_path.stat().st_size
    except OSError as exc:
        return False, f"无法读取 WAL 文件: {wal_path.name} ({exc})"
    size_mb = size_bytes / (1024 * 1024)
    ok = size_mb <= threshold_mb
    return ok, f"WAL {size_mb:.1f} MB / 阈值 {threshold_detail}"


def app_backup_check() -> tuple[bool, str]:
    max_age_hours, threshold_ok, threshold_detail = backup_max_age_hours()
    if not threshold_ok:
        return False, threshold_detail
    backup_dir = Path(os.getenv("OWQ_APP_BACKUP_DIR", "data/backups"))
    if not backup_dir.exists():
        return False, f"备份目录不存在: {backup_dir}"
    try:
        backups = sorted(
            backup_dir.glob("app-*.sqlite"),
            key=lambda item: (item.stat().st_mtime, item.name),
            reverse=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"无法读取备份目录: {backup_dir} ({exc})"
    if not backups:
        return False, f"备份目录没有 app-*.sqlite: {backup_dir}"
    latest = backups[0]
    try:
        stat = latest.stat()
    except OSError as exc:
        return False, f"无法读取最新备份: {latest.name} ({exc})"
    if stat.st_size <= 0:
        return False, f"最新备份为空: {latest.name}"
    age_hours = max(0.0, (time.time() - stat.st_mtime) / 3600)
    try:
        result = db.verify_backup_file(latest)
    except Exception as exc:  # noqa: BLE001
        return False, f"最新备份无法打开: {latest.name} ({type(exc).__name__})"
    quick_detail = result["quick_check"]
    ok = quick_detail == "ok" and age_hours <= max_age_hours
    detail = (
        f"最新备份 {latest.name}, {age_hours:.1f} 小时前, "
        f"quick_check={quick_detail}, 核心表 {len(result['row_counts'])} 个, 阈值 {threshold_detail}"
    )
    return ok, detail


def parse_audit_created_at(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recent_email_delivery_check(con: sqlite3.Connection, email_configured: bool, email_dev_auth: bool) -> tuple[bool, str]:
    if not email_configured:
        if email_dev_auth:
            return True, "公测测试登录模式未要求近期真实发信诊断;正式运营需配置真实发信后完成测试"
        return False, "未配置真实发信服务,无法验证近期发信诊断"
    max_age_hours, threshold_ok, threshold_detail = email_test_max_age_hours()
    if not threshold_ok:
        return False, threshold_detail
    row = con.execute(
        """
        SELECT action, target_id, detail, created_at
        FROM audit_events
        WHERE action IN ('cli.email_test', 'admin.email_test')
          AND target_type='email'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return False, "未找到成功发信诊断记录;配置后运行 --send-test-email 或在后台发送邮件发信诊断"
    created_at = parse_audit_created_at(row["created_at"])
    if created_at is None:
        return False, f"最近发信诊断时间不可解析: {row['created_at']}"
    age_hours = max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
    provider = ""
    recipient_hash = ""
    try:
        detail = json.loads(row["detail"] or "{}")
        provider = str(detail.get("provider") or "")
        recipient_hash = str(detail.get("recipient_hash") or "")[:16]
    except ValueError:
        provider = ""
        recipient_hash = ""
    target = str(row["target_id"] or "").strip()
    if not recipient_hash and target:
        recipient_hash = services.email_token_hash(target.lower())[:16] if "@" in target else target[:16]
    target_text = f"收件哈希 {recipient_hash}" if recipient_hash else "未记录收件标识"
    provider_text = f", provider={provider}" if provider else ""
    ok = age_hours <= max_age_hours
    return ok, f"最近成功发信诊断 {age_hours:.1f} 小时前,{target_text}{provider_text},阈值 {threshold_detail}"


def prediction_results_check(con: sqlite3.Connection, max_staleness_days: int, threshold_ok: bool) -> tuple[bool, str]:
    min_codes, min_ok, min_detail = predictions_min_codes()
    if not min_ok:
        return False, min_detail
    csv_path = Path(os.getenv("OWQ_PREDICTIONS_CSV", "reports/predictions.csv"))
    if not csv_path.exists():
        return False, f"预测结果不存在: {csv_path}"
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as exc:  # noqa: BLE001
        return False, f"预测结果无法读取: {exc}"
    if not rows:
        return False, f"预测结果为空: {csv_path}"
    codes = []
    dates = []
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        if not code:
            continue
        try:
            float(row.get("prediction", ""))
        except ValueError:
            continue
        codes.append(code)
        parsed = parse_market_date(row.get("date"))
        if parsed:
            dates.append(parsed)
    if not codes:
        return False, "预测结果缺少 code/prediction/date 可用行"
    placeholders = ",".join("?" for _ in codes)
    matched = con.execute(
        f"""
        SELECT COUNT(DISTINCT code)
        FROM market_prices
        WHERE source <> 'demo' AND price > 0 AND prev_close > 0 AND code IN ({placeholders})
        """,
        codes,
    ).fetchone()[0]
    latest = max(dates) if dates else None
    if latest is None:
        return False, "预测结果缺少可解析日期"
    age_days = (date.today() - latest).days
    if not threshold_ok:
        return False, "预测新鲜度阈值配置无效"
    ok = matched >= min_codes and age_days <= max_staleness_days
    detail = f"预测候选 {len(codes)} 行 / 可交易匹配 {matched} 个, 最新预测 {latest.isoformat()}, 阈值 {min_detail}, 新鲜度 {max_staleness_days} 天"
    return ok, detail


def _latest_audit_for_actions(con: sqlite3.Connection, actions: tuple[str, ...]) -> sqlite3.Row | None:
    placeholders = ",".join("?" for _ in actions)
    return con.execute(
        f"""
        SELECT id, created_at, action, detail
        FROM audit_events
        WHERE action IN ({placeholders})
        ORDER BY id DESC
        LIMIT 1
        """,
        actions,
    ).fetchone()


def market_sync_job_check(con: sqlite3.Connection) -> tuple[bool, str]:
    max_age_hours, threshold_ok, threshold_detail = market_sync_max_age_hours()
    if not threshold_ok:
        return False, threshold_detail
    success = _latest_audit_for_actions(con, ("cli.market_sync_succeeded",))
    legacy_success = _latest_audit_for_actions(con, ("cli.market_duckdb_sync",))
    failure = _latest_audit_for_actions(con, ("cli.market_sync_failed",))
    if success is None and legacy_success is not None:
        success = legacy_success
    if success is None:
        return False, "未找到成功市场同步记录;生产环境应运行 deploy/sync-market-public.sh"
    success_at = parse_audit_datetime(success["created_at"])
    if success_at is None:
        return False, f"市场同步记录时间不可解析: {success['created_at']}"
    if failure is not None and int(failure["id"]) > int(success["id"]):
        return False, f"最近市场同步失败: {failure['created_at']} {failure['detail']}"
    age_hours = (datetime.now(timezone.utc) - success_at).total_seconds() / 3600
    ok = age_hours <= max_age_hours
    source = "生产同步脚本" if success["action"] == "cli.market_sync_succeeded" else "应用行情同步"
    detail = f"最近成功{source} {age_hours:.1f} 小时前,阈值 {threshold_detail}"
    return ok, detail


def configured_admin_access_check(con: sqlite3.Connection, email_configured: bool, email_dev_auth: bool) -> tuple[bool, str]:
    admin_ids = [item.strip() for item in os.getenv("OWQ_ADMIN_USER_IDS", "").replace(";", ",").split(",") if item.strip()]
    admin_openids = [item.strip() for item in os.getenv("OWQ_ADMIN_OPENIDS", "").replace(";", ",").split(",") if item.strip()]
    admin_emails = [item.strip().lower() for item in os.getenv("OWQ_ADMIN_EMAILS", "").replace(";", ",").split(",") if item.strip()]
    if not (admin_ids or admin_openids or admin_emails):
        return False, "未配置管理员身份"

    clauses = []
    params: list[str] = []
    if admin_ids:
        clauses.append("CAST(id AS TEXT) IN ({})".format(",".join("?" for _ in admin_ids)))
        params.extend(admin_ids)
    if admin_openids:
        clauses.append("wechat_openid IN ({})".format(",".join("?" for _ in admin_openids)))
        params.extend(admin_openids)
    if admin_emails:
        clauses.append("LOWER(email) IN ({})".format(",".join("?" for _ in admin_emails)))
        params.extend(admin_emails)
    if not clauses:
        return False, "管理员邮箱已配置但发信服务不可用"
    rows = con.execute(
        f"""
        SELECT id, email, login_name, password_hash
        FROM users
        WHERE {" OR ".join(clauses)}
        """,
        params,
    ).fetchall()
    password_ready = [
        row
        for row in rows
        if (str(row["login_name"] or "").strip() or str(row["email"] or "").strip())
        and str(row["password_hash"] or "").startswith("pbkdf2_sha256$")
    ]
    if password_ready:
        return True, f"{len(password_ready)} 个已配置管理员账号可使用用户名/邮箱 + 密码登录"
    if admin_emails and email_configured:
        return True, f"已配置 {len(admin_emails)} 个管理员邮箱,可通过邮箱验证注册或重置密码"
    if rows:
        suffix = "测试验证入口不能替代可恢复的管理员登录。" if email_dev_auth else ""
        return False, ("已配置管理员用户存在,但缺少账号密码登录;请用邮箱重置密码或配置 OWQ_ADMIN_EMAILS。" + suffix).rstrip("。") + "。"
    return False, "未找到已配置的管理员用户;请先创建管理员邮箱账户或更新 OWQ_ADMIN_USER_IDS/OWQ_ADMIN_EMAILS"


def check(con: sqlite3.Connection) -> list[dict[str, str]]:
    """Return readiness checks as dictionaries with name/status/detail."""
    rows: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
        rows.append(
            {
                "name": name,
                "status": "ok" if ok else "warn",
                "detail": detail,
                "required": "true" if required else "false",
            }
        )

    add("python", sys.version_info >= (3, 11), sys.version.split()[0])
    add("app_db", True, "SQLite 已连接")

    try:
        quick_check = con.execute("PRAGMA quick_check").fetchone()
        quick_detail = quick_check[0] if quick_check else "no result"
        add("app_db_integrity", quick_detail == "ok", str(quick_detail))
        fk_ok, fk_detail = app_db_foreign_key_check(con)
        add("app_db_foreign_keys", fk_ok, fk_detail)
        journal_mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        busy_timeout = int(con.execute("PRAGMA busy_timeout").fetchone()[0])
        add(
            "sqlite_runtime",
            ((not production_mode()) or journal_mode == "wal") and busy_timeout >= MIN_BUSY_TIMEOUT_MS,
            f"journal_mode={journal_mode}, busy_timeout={busy_timeout}ms",
            required=production_mode(),
        )
        wal_ok, wal_detail = sqlite_wal_size_check(con)
        add("sqlite_wal_size", wal_ok, wal_detail, required=False)
        user_count = con.execute("SELECT count(*) FROM users").fetchone()[0]
        account_count = con.execute("SELECT count(*) FROM accounts").fetchone()[0]
        market_count = con.execute("SELECT count(*) FROM market_prices").fetchone()[0]
        real_market_count = con.execute(
            """
            SELECT COUNT(DISTINCT code)
            FROM market_prices
            WHERE price > 0 AND prev_close > 0 AND source <> 'demo'
            """
        ).fetchone()[0]
        demo_market_count = con.execute(
            """
            SELECT COUNT(DISTINCT code)
            FROM market_prices
            WHERE price > 0 AND prev_close > 0 AND source = 'demo'
            """
        ).fetchone()[0]
        latest_market_as_of = con.execute(
            """
            SELECT MAX(as_of)
            FROM market_prices
            WHERE price > 0 AND prev_close > 0
            """
        ).fetchone()[0]
        contest_count = con.execute("SELECT count(*) FROM contests WHERE is_active=1").fetchone()[0]
        add("users", user_count >= 0, f"{user_count} 个用户 / {account_count} 个账户")
        add("market", market_count > 0, f"{market_count} 条行情")
        add(
            "market_real_data",
            (not production_mode()) or real_market_count > 0,
            f"{real_market_count} 个真实行情标的 / {demo_market_count} 个演示标的",
            required=production_mode(),
        )
        min_real_codes, coverage_threshold_ok, coverage_threshold_detail = market_min_real_codes()
        coverage_ok = coverage_threshold_ok and ((not production_mode()) or real_market_count >= min_real_codes)
        coverage_detail = (
            f"真实行情覆盖 {real_market_count} 个标的,正式发布建议不少于 {coverage_threshold_detail}"
            if coverage_threshold_ok
            else coverage_threshold_detail
        )
        add("market_coverage", coverage_ok, coverage_detail, required=False)
        max_staleness_days, threshold_ok, threshold_detail = market_max_staleness_days()
        market_date = parse_market_date(latest_market_as_of)
        if market_date is None:
            freshness_ok = False
            freshness_detail = "行情缺少可解析的 as_of 日期"
        else:
            age_days = (date.today() - market_date).days
            freshness_ok = threshold_ok and age_days <= max_staleness_days
            freshness_detail = f"最新行情 {market_date.isoformat()}, 距今天 {age_days} 天, 阈值 {threshold_detail}"
        if not threshold_ok:
            freshness_detail = threshold_detail
        add("market_freshness", freshness_ok, freshness_detail, required=production_mode())
        prediction_ok, prediction_detail = prediction_results_check(con, max_staleness_days, threshold_ok)
        add("prediction_results", prediction_ok, prediction_detail, required=False)
        sync_ok, sync_detail = market_sync_job_check(con)
        add("market_sync_job", sync_ok, sync_detail, required=False)
        add("contest", contest_count > 0, f"{contest_count} 个启用比赛")
    except Exception as exc:  # noqa: BLE001
        add("app_tables", False, str(exc))

    public_base = os.getenv("OWQ_PUBLIC_BASE_URL", "").strip()
    app_secret = os.getenv("OWQ_SECRET", "").strip()
    admin_ids = os.getenv("OWQ_ADMIN_USER_IDS", "").strip()
    admin_openids = os.getenv("OWQ_ADMIN_OPENIDS", "").strip()
    admin_emails = os.getenv("OWQ_ADMIN_EMAILS", "").strip()
    prod = production_mode()
    email_status = email_config.status()
    email_configured = bool(email_status["configured"])
    email_dev_auth = env_flag("OWQ_EMAIL_DEV_AUTH", default=(not prod and not email_configured))
    email_dev_auth_show_links = env_flag("OWQ_EMAIL_DEV_AUTH_SHOW_LINKS", default=not prod)
    demo_participants = services.demo_contest_participant_summary(con)
    demo_participant_count = int(demo_participants["participants"])
    demo_participants_ok = (not prod) or email_dev_auth or demo_participant_count == 0
    if demo_participant_count:
        demo_participants_detail = (
            f"发现 {demo_participant_count} 个演示/开发参赛账户"
            + (f" (user_ids={demo_participants['user_ids']})" if demo_participants["user_ids"] else "")
            + ("; beta 测试可保留,正式关闭测试入口前应移出公开赛或清理演示数据" if email_dev_auth else "; 正式发布前应移出公开赛或清理演示数据")
        )
    else:
        demo_participants_detail = "公开赛未发现演示/开发参赛账户"

    add(
        "app_secret",
        bool(app_secret and app_secret != DEFAULT_SECRET and len(app_secret) >= 24),
        "OWQ_SECRET 已配置" if app_secret and app_secret != DEFAULT_SECRET else "生产环境必须设置非默认 OWQ_SECRET",
        required=prod,
    )
    add(
        "public_base_url",
        (not prod) or public_base.startswith("https://"),
        public_base or "生产环境需要设置 https:// 开头的 OWQ_PUBLIC_BASE_URL",
        required=prod,
    )
    add(
        "cookie_secure",
        (not prod) or public_base.startswith("https://") or env_flag("OWQ_COOKIE_SECURE"),
        "公网 HTTPS 会启用 Secure Cookie" if public_base.startswith("https://") else "生产环境需要 HTTPS 或 OWQ_COOKIE_SECURE=1",
        required=prod,
    )
    _, body_limit_ok, body_limit_detail = request_body_limit()
    add("request_body_limit", body_limit_ok, body_limit_detail, required=False)
    rate_limits_disabled = env_flag("OWQ_RATE_LIMITS_DISABLED")
    add(
        "rate_limits",
        not (prod and rate_limits_disabled),
        "限流已启用" if not rate_limits_disabled else "OWQ_RATE_LIMITS_DISABLED=1 会关闭注册、认证和登录用户写入节流",
        required=prod,
    )
    legal_consent_ok, legal_consent_detail = legal_consent_gate_check(prod)
    add("legal_consent_gate", legal_consent_ok, legal_consent_detail, required=prod)
    disk_ok, disk_detail = disk_space_check()
    add("disk_space", disk_ok, disk_detail, required=False)
    backup_ok, backup_detail = app_backup_check()
    add("app_backup", backup_ok, backup_detail, required=False)
    raw_session_ttl = os.getenv("OWQ_SESSION_TTL_SECONDS", "").strip()
    session_ttl_ok = True
    session_ttl_detail = f"会话默认 {DEFAULT_SESSION_TTL_SECONDS} 秒后过期"
    if raw_session_ttl:
        try:
            session_ttl = int(raw_session_ttl)
            session_ttl_ok = 300 <= session_ttl <= 60 * 60 * 24 * 365
            session_ttl_detail = f"会话 {session_ttl} 秒后过期" if session_ttl_ok else "会话有效期应在 300 秒到 365 天之间"
        except ValueError:
            session_ttl_ok = False
            session_ttl_detail = "OWQ_SESSION_TTL_SECONDS 必须是整数秒数"
    add("session_ttl", session_ttl_ok, session_ttl_detail, required=False)
    try:
        audit_summary = services.audit_retention_summary(con)
        audit_expired = int(audit_summary["expired"])
        audit_total = int(audit_summary["total"])
        audit_ok = bool(audit_summary["ok"]) and audit_expired == 0
        if audit_summary["ok"]:
            audit_detail = (
                f"保留 {audit_summary['detail']}, 共 {audit_total} 条, "
                f"{audit_expired} 条超过保留期, 截止 {audit_summary['cutoff']}"
            )
        else:
            audit_detail = audit_summary["detail"]
    except Exception as exc:  # noqa: BLE001
        audit_ok = False
        audit_detail = f"审计日志保留检查失败: {type(exc).__name__}: {exc}"
    add("audit_retention", audit_ok, audit_detail, required=False)
    try:
        email_session_summary = services.email_login_session_retention_summary(con)
        email_session_ok = (
            bool(email_session_summary["ok"])
            and int(email_session_summary["deletable"]) == 0
        )
        if email_session_summary["ok"]:
            email_session_detail = (
                f"保留 {email_session_summary['detail']}, 共 {email_session_summary['total']} 条, "
                f"{email_session_summary['expired_pending']} 条待过期标记, "
                f"{email_session_summary['deletable']} 条可清理, 截止 {email_session_summary['cutoff']}"
            )
        else:
            email_session_detail = email_session_summary["detail"]
    except Exception as exc:  # noqa: BLE001
        email_session_ok = False
        email_session_detail = f"邮箱登录临时会话保留检查失败: {type(exc).__name__}: {exc}"
    add("email_login_session_retention", email_session_ok, email_session_detail, required=False)
    try:
        operational_queue_ok, operational_queue_detail = operational_queue_check(con)
    except Exception as exc:  # noqa: BLE001
        operational_queue_ok = False
        operational_queue_detail = f"运营待处理队列检查失败: {type(exc).__name__}: {exc}"
    add("operational_queue", operational_queue_ok, operational_queue_detail, required=False)
    try:
        recent_errors_ok, recent_errors_detail = recent_server_errors_check(con)
    except Exception as exc:  # noqa: BLE001
        recent_errors_ok = False
        recent_errors_detail = f"服务端异常检查失败: {type(exc).__name__}: {exc}"
    add("recent_server_errors", recent_errors_ok, recent_errors_detail, required=False)
    add(
        "admin_config",
        (not prod) or bool(admin_ids or admin_openids or admin_emails),
        "已显式配置管理员" if admin_ids or admin_openids or admin_emails else "生产环境应配置 OWQ_ADMIN_USER_IDS / OWQ_ADMIN_EMAILS",
        required=prod,
    )
    admin_access_ok, admin_access_detail = configured_admin_access_check(con, email_configured, email_dev_auth)
    add(
        "admin_access",
        (not prod) or admin_access_ok,
        admin_access_detail,
        required=prod,
    )
    add(
        "email_login",
        email_configured or email_dev_auth,
        "邮箱登录可用" if email_configured or email_dev_auth else "需要配置 Cloudflare Email Sending 或 SMTP",
        required=prod,
    )
    add(
        "email_sending",
        email_configured,
        str(email_status["detail"]),
        required=False,
    )
    email_probe_ok, email_probe_detail = recent_email_delivery_check(con, email_configured, email_dev_auth)
    add("email_delivery_probe", email_probe_ok, email_probe_detail, required=False)
    add(
        "email_dev_auth_public",
        not (prod and email_dev_auth),
        "公网环境仍启用邮箱测试登录;正式运营前应关闭并配置真实发信服务"
        if prod and email_dev_auth
        else "公网测试邮箱验证入口已关闭",
        required=False,
    )
    add(
        "email_dev_auth_public_links",
        not (prod and email_dev_auth and email_dev_auth_show_links),
        "公网注册页会展示邮箱测试验证链接;请关闭 OWQ_EMAIL_DEV_AUTH_SHOW_LINKS"
        if prod and email_dev_auth and email_dev_auth_show_links
        else "公网注册页未展示邮箱测试验证链接",
        required=False,
    )
    add("demo_contest_participants", demo_participants_ok, demo_participants_detail, required=False)

    data_db = Path(os.getenv("OWQ_DB_PATH", "data/market.duckdb"))
    add("quant_duckdb", data_db.exists(), str(data_db), required=False)
    return rows


def health(con: sqlite3.Connection, strict: bool = False) -> dict:
    rows = check(con)
    required_warnings = [row for row in rows if row["required"] == "true" and row["status"] != "ok"]
    optional_warnings = [row for row in rows if row["required"] == "false" and row["status"] != "ok"]
    ok = not required_warnings and (not strict or not optional_warnings)
    return {
        "status": "ok" if ok else "degraded",
        "ok": ok,
        "strict": bool(strict),
        "required_warnings": len(required_warnings),
        "optional_warnings": len(optional_warnings),
        "checks": rows,
    }


def print_report(con: sqlite3.Connection) -> None:
    for row in check(con):
        marker = "OK" if row["status"] == "ok" else "WARN"
        scope = "required" if row["required"] == "true" else "optional"
        print(f"[{marker}] {row['name']} ({scope}): {row['detail']}")

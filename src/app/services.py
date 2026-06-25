"""Domain services for registration, paper trading, contests, and forum posts."""
from __future__ import annotations

import csv
import base64
import hashlib
import hmac
import io
import secrets
import sqlite3
import os
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


INITIAL_CASH = 1_000_000.0
PASSWORD_HASH_ITERATIONS = 210_000
DEFAULT_AUDIT_RETENTION_DAYS = 400
MIN_AUDIT_RETENTION_DAYS = 30
MAX_AUDIT_RETENTION_DAYS = 3650
DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS = 30
MIN_EMAIL_LOGIN_SESSION_RETENTION_DAYS = 1
MAX_EMAIL_LOGIN_SESSION_RETENTION_DAYS = 365

SUPPORT_REQUEST_COOLDOWN_SECONDS = 300
SUPPORT_REQUEST_HOURLY_LIMIT = 3
SUPPORT_REQUEST_OPEN_LIMIT = 5


class RateLimitExceeded(ValueError):
    """Raised when a user-facing write is throttled before persistence."""

DEMO_PLAYERS = [
    {
        "openid": "demo-low-vol",
        "nickname": "低波动练习生",
        "code": "510300.SH",
        "qty": 18000,
        "avg_multiplier": 0.965,
        "title": "低波动 ETF 轮动复盘",
        "body": "用低波动 ETF 做月度轮动,重点观察回撤和换手成本。",
        "tag": "low-vol",
    },
    {
        "openid": "demo-reversal",
        "nickname": "反转策略样本",
        "code": "000001.SZ",
        "qty": 12000,
        "avg_multiplier": 0.93,
        "title": "短期反转模拟盘记录",
        "body": "选择近期回撤较大的流动性标的做小仓位反转演练,发帖时保留战绩快照。",
        "tag": "reversal",
    },
    {
        "openid": "demo-growth",
        "nickname": "成长波动样本",
        "code": "300750.SZ",
        "qty": 900,
        "avg_multiplier": 1.035,
        "title": "成长股高波动持仓复盘",
        "body": "高波动标的更容易拉开榜单表现,也更需要看最大回撤和仓位控制。",
        "tag": "growth",
    },
]


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def record_audit_event(
    con: sqlite3.Connection,
    actor_user_id: int | None,
    action: str,
    target_type: str = "",
    target_id: str | int | None = "",
    detail: dict | str | None = None,
    ip_address: str = "",
) -> int:
    action = (action or "").strip()[:80]
    if not action:
        raise ValueError("审计动作不能为空")
    target_type = (target_type or "").strip()[:80]
    target_id = str(target_id or "").strip()[:120]
    if isinstance(detail, dict):
        safe_detail = {}
        for key, value in detail.items():
            if value is None:
                continue
            safe_detail[str(key)[:60]] = str(value)[:300]
        detail_text = json.dumps(safe_detail, ensure_ascii=False, sort_keys=True)
    elif detail is None:
        detail_text = "{}"
    else:
        detail_text = json.dumps({"message": str(detail)[:500]}, ensure_ascii=False)
    cur = con.execute(
        """
        INSERT INTO audit_events(actor_user_id, action, target_type, target_id, detail, ip_address)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(actor_user_id) if actor_user_id is not None else None,
            action,
            target_type,
            target_id,
            detail_text[:2000],
            (ip_address or "").strip()[:80],
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def audit_events(con: sqlite3.Connection, limit: int = 80):
    limit = max(1, min(int(limit), 5000))
    return con.execute(
        """
        SELECT e.*, u.nickname
        FROM audit_events e
        LEFT JOIN users u ON u.id=e.actor_user_id
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


SECURITY_AUDIT_ACTIONS = (
    "server.error",
    "auth.email_send_failed",
    "auth.password_reset_email_failed",
    "admin.backup_failed",
    "admin.email_test_failed",
    "cli.email_test_failed",
    "cli.market_sync_failed",
)


def security_audit_summary(con: sqlite3.Connection, hours: int = 24, recent_limit: int = 12) -> dict:
    hours = max(1, min(int(hours), 168))
    recent_limit = max(1, min(int(recent_limit), 50))
    window = f"-{hours} hours"
    exact_placeholders = ",".join("?" for _ in SECURITY_AUDIT_ACTIONS)
    security_where = f"(e.action LIKE 'security.%' OR e.action IN ({exact_placeholders}))"
    params = list(SECURITY_AUDIT_ACTIONS)
    total_window = int(
        con.execute(
            f"""
            SELECT COUNT(*)
            FROM audit_events e
            WHERE {security_where}
              AND e.created_at >= datetime('now', ?)
            """,
            (*params, window),
        ).fetchone()[0]
    )
    total_7d = int(
        con.execute(
            f"""
            SELECT COUNT(*)
            FROM audit_events e
            WHERE {security_where}
              AND e.created_at >= datetime('now', '-7 days')
            """,
            params,
        ).fetchone()[0]
    )
    by_action = con.execute(
        f"""
        SELECT e.action, COUNT(*) AS count
        FROM audit_events e
        WHERE {security_where}
          AND e.created_at >= datetime('now', ?)
        GROUP BY e.action
        ORDER BY count DESC, e.action ASC
        """,
        (*params, window),
    ).fetchall()
    recent = con.execute(
        f"""
        SELECT e.*, u.nickname
        FROM audit_events e
        LEFT JOIN users u ON u.id=e.actor_user_id
        WHERE {security_where}
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (*params, recent_limit),
    ).fetchall()
    return {
        "hours": hours,
        "total_window": total_window,
        "total_7d": total_7d,
        "by_action": row_dicts(by_action),
        "recent": recent,
    }


def audit_retention_config(days: int | str | None = None) -> tuple[int, bool, str]:
    raw = str(days).strip() if days is not None else os.getenv("OWQ_AUDIT_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_AUDIT_RETENTION_DAYS, True, f"{DEFAULT_AUDIT_RETENTION_DAYS} 天"
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_AUDIT_RETENTION_DAYS, False, "OWQ_AUDIT_RETENTION_DAYS 必须是整数天数"
    if parsed < MIN_AUDIT_RETENTION_DAYS or parsed > MAX_AUDIT_RETENTION_DAYS:
        return (
            DEFAULT_AUDIT_RETENTION_DAYS,
            False,
            f"审计日志保留期应在 {MIN_AUDIT_RETENTION_DAYS} 到 {MAX_AUDIT_RETENTION_DAYS} 天之间",
        )
    return parsed, True, f"{parsed} 天"


def audit_retention_cutoff(days: int, now: datetime | None = None) -> str:
    base = now or utc_now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    cutoff = base.astimezone(timezone.utc) - timedelta(days=int(days))
    return cutoff.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def audit_retention_summary(con: sqlite3.Connection, days: int | str | None = None, now: datetime | None = None) -> dict:
    retention_days, ok, detail = audit_retention_config(days)
    cutoff = audit_retention_cutoff(retention_days, now)
    total = int(con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    expired = int(con.execute("SELECT COUNT(*) FROM audit_events WHERE created_at < ?", (cutoff,)).fetchone()[0])
    return {
        "ok": ok,
        "retention_days": retention_days,
        "detail": detail,
        "cutoff": cutoff,
        "total": total,
        "expired": expired,
    }


def prune_audit_events(con: sqlite3.Connection, days: int | str | None = None, now: datetime | None = None) -> dict:
    summary = audit_retention_summary(con, days=days, now=now)
    if not summary["ok"]:
        raise ValueError(summary["detail"])
    deleted = int(summary["expired"])
    con.execute("DELETE FROM audit_events WHERE created_at < ?", (summary["cutoff"],))
    con.commit()
    remaining = int(con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    return {
        "deleted": deleted,
        "remaining": remaining,
        "retention_days": summary["retention_days"],
        "cutoff": summary["cutoff"],
    }


def record_user_consent(
    con: sqlite3.Connection,
    user_id: int,
    terms_version: str,
    privacy_version: str,
    risk_version: str,
    source: str = "",
    ip_address: str = "",
    user_agent: str = "",
) -> int:
    if get_user(con, user_id) is None:
        raise ValueError("用户不存在")
    versions = [terms_version, privacy_version, risk_version]
    if any(not str(item or "").strip() for item in versions):
        raise ValueError("同意版本不能为空")
    cur = con.execute(
        """
        INSERT INTO user_consents(
            user_id, terms_version, privacy_version, risk_version, source, ip_address, user_agent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            str(terms_version).strip()[:40],
            str(privacy_version).strip()[:40],
            str(risk_version).strip()[:40],
            str(source or "").strip()[:80],
            str(ip_address or "").strip()[:80],
            str(user_agent or "").strip()[:300],
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def latest_user_consent(con: sqlite3.Connection, user_id: int):
    return con.execute(
        """
        SELECT *
        FROM user_consents
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(user_id),),
    ).fetchone()


def user_consent_summary(con: sqlite3.Connection, limit: int = 200):
    limit = max(1, min(int(limit), 500))
    return con.execute(
        """
        SELECT u.id AS user_id, u.nickname, u.created_at AS user_created_at,
               c.terms_version, c.privacy_version, c.risk_version,
               c.source, c.ip_address, c.created_at AS consent_at
        FROM users u
        LEFT JOIN user_consents c ON c.id = (
            SELECT id
            FROM user_consents latest
            WHERE latest.user_id=u.id
            ORDER BY latest.id DESC
            LIMIT 1
        )
        ORDER BY u.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def row_dict(row) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def row_dicts(rows) -> list[dict]:
    return [row_dict(row) for row in rows]


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_LOGIN_COOLDOWN_SECONDS = 60
EMAIL_LOGIN_HOURLY_LIMIT = 5


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if len(value) > 254 or not EMAIL_RE.match(value):
        raise ValueError("请输入有效的邮箱地址")
    return value


def email_token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def email_login_session_retention_config(days: int | str | None = None) -> tuple[int, bool, str]:
    raw = str(days).strip() if days is not None else os.getenv("OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS", "").strip()
    if not raw:
        return DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS, True, f"{DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS} 天"
    try:
        parsed = int(raw)
    except ValueError:
        return (
            DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS,
            False,
            "OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS 必须是整数天数",
        )
    if parsed < MIN_EMAIL_LOGIN_SESSION_RETENTION_DAYS or parsed > MAX_EMAIL_LOGIN_SESSION_RETENTION_DAYS:
        return (
            DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS,
            False,
            f"邮箱登录临时会话保留期应在 {MIN_EMAIL_LOGIN_SESSION_RETENTION_DAYS} 到 {MAX_EMAIL_LOGIN_SESSION_RETENTION_DAYS} 天之间",
        )
    return parsed, True, f"{parsed} 天"


def email_login_session_retention_cutoff(days: int, now: datetime | None = None) -> str:
    base = now or utc_now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    cutoff = base.astimezone(timezone.utc) - timedelta(days=int(days))
    return cutoff.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _email_login_expires_at_expired(value: str, now: datetime | None = None) -> bool:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) < current.astimezone(timezone.utc)


def _expired_pending_email_login_hashes(con: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    rows = con.execute(
        "SELECT token_hash, expires_at FROM email_login_sessions WHERE status='pending'"
    ).fetchall()
    expired = []
    for row in rows:
        try:
            if _email_login_expires_at_expired(row["expires_at"], now=now):
                expired.append(row["token_hash"])
        except ValueError:
            expired.append(row["token_hash"])
    return expired


def email_login_session_retention_summary(
    con: sqlite3.Connection,
    days: int | str | None = None,
    now: datetime | None = None,
) -> dict:
    retention_days, ok, detail = email_login_session_retention_config(days)
    cutoff = email_login_session_retention_cutoff(retention_days, now)
    total = int(con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0])
    expired_pending = len(_expired_pending_email_login_hashes(con, now=now))
    deletable = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM email_login_sessions
            WHERE status IN ('confirmed', 'expired')
              AND created_at < ?
            """,
            (cutoff,),
        ).fetchone()[0]
    )
    return {
        "ok": ok,
        "retention_days": retention_days,
        "detail": detail,
        "cutoff": cutoff,
        "total": total,
        "expired_pending": expired_pending,
        "deletable": deletable,
    }


def cleanup_email_login_sessions(
    con: sqlite3.Connection,
    days: int | str | None = None,
    now: datetime | None = None,
) -> dict:
    """Expire pending magic links and prune short-lived authentication records."""
    expired = _expired_pending_email_login_hashes(con, now=now)
    if expired:
        con.executemany(
            "UPDATE email_login_sessions SET status='expired' WHERE token_hash=?",
            [(item,) for item in expired],
        )
        con.commit()
    summary = email_login_session_retention_summary(con, days=days, now=now)
    deleted = 0
    if summary["ok"]:
        deleted = int(summary["deletable"])
        con.execute(
            """
            DELETE FROM email_login_sessions
            WHERE status IN ('confirmed', 'expired')
              AND created_at < ?
            """,
            (summary["cutoff"],),
        )
        con.commit()
    remaining = int(con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0])
    return {
        "expired": len(expired),
        "deleted": deleted,
        "remaining": remaining,
        "retention_days": summary["retention_days"],
        "cutoff": summary["cutoff"],
        "retention_ok": summary["ok"],
        "detail": summary["detail"],
    }


def prune_email_login_sessions(
    con: sqlite3.Connection,
    days: int | str | None = None,
    now: datetime | None = None,
) -> dict:
    retention_days, ok, detail = email_login_session_retention_config(days)
    if not ok:
        raise ValueError(detail)
    return cleanup_email_login_sessions(con, days=retention_days, now=now)


def enforce_email_login_request_limit(
    con: sqlite3.Connection,
    email: str,
    cooldown_seconds: int = EMAIL_LOGIN_COOLDOWN_SECONDS,
    hourly_limit: int = EMAIL_LOGIN_HOURLY_LIMIT,
) -> None:
    normalized = normalize_email(email)
    cooldown_seconds = max(1, int(cooldown_seconds))
    hourly_limit = max(1, int(hourly_limit))
    recent = con.execute(
        """
        SELECT COUNT(*)
        FROM email_login_sessions
        WHERE email=? AND created_at >= datetime('now', ?)
        """,
        (normalized, f"-{cooldown_seconds} seconds"),
    ).fetchone()[0]
    if int(recent) > 0:
        raise ValueError("登录邮件请求过于频繁,请稍后再试。")
    hourly = con.execute(
        """
        SELECT COUNT(*)
        FROM email_login_sessions
        WHERE email=? AND created_at >= datetime('now', '-1 hour')
        """,
        (normalized,),
    ).fetchone()[0]
    if int(hourly) >= hourly_limit:
        raise ValueError("该邮箱登录邮件请求过多,请稍后再试。")


def create_email_login_session(
    con: sqlite3.Connection,
    email: str,
    terms_version: str,
    privacy_version: str,
    risk_version: str,
    ttl_minutes: int = 15,
    enforce_rate_limit: bool = True,
) -> str:
    normalized = normalize_email(email)
    versions = [terms_version, privacy_version, risk_version]
    if any(not str(item or "").strip() for item in versions):
        raise ValueError("同意版本不能为空")
    cleanup_email_login_sessions(con)
    if enforce_rate_limit:
        enforce_email_login_request_limit(con, normalized)
    token = secrets.token_urlsafe(32)
    now = utc_now()
    expires = iso(now + timedelta(minutes=ttl_minutes))
    con.execute(
        """
        INSERT INTO email_login_sessions(
            token_hash, email, status, expires_at,
            accepted_terms_version, accepted_privacy_version, accepted_risk_version, accepted_at
        )
        VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            email_token_hash(token),
            normalized,
            expires,
            str(terms_version).strip()[:40],
            str(privacy_version).strip()[:40],
            str(risk_version).strip()[:40],
            iso(now),
        ),
    )
    con.commit()
    return token


def delete_email_login_session(con: sqlite3.Connection, token: str) -> None:
    con.execute("DELETE FROM email_login_sessions WHERE token_hash=?", (email_token_hash(token),))
    con.commit()


def mark_email_login_sent(con: sqlite3.Connection, token: str) -> None:
    con.execute(
        "UPDATE email_login_sessions SET sent_at=? WHERE token_hash=?",
        (iso(utc_now()), email_token_hash(token)),
    )
    con.commit()


def email_login_session_status(con: sqlite3.Connection, token: str) -> dict:
    row = con.execute(
        "SELECT * FROM email_login_sessions WHERE token_hash=?",
        (email_token_hash(token),),
    ).fetchone()
    if row is None:
        return {"status": "missing", "user_id": None, "email": ""}
    status = row["status"]
    if status == "pending" and _email_login_expires_at_expired(row["expires_at"]):
        con.execute("UPDATE email_login_sessions SET status='expired' WHERE token_hash=?", (row["token_hash"],))
        con.commit()
        status = "expired"
    return {"status": status, "user_id": row["user_id"], "email": row["email"]}


def email_login_legal_acceptance(con: sqlite3.Connection, token: str) -> dict | None:
    row = con.execute(
        """
        SELECT accepted_terms_version, accepted_privacy_version, accepted_risk_version, accepted_at
        FROM email_login_sessions
        WHERE token_hash=?
        """,
        (email_token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    if not (row["accepted_terms_version"] and row["accepted_privacy_version"] and row["accepted_risk_version"]):
        return None
    return dict(row)


def confirm_email_login_session(con: sqlite3.Connection, token: str) -> int:
    row = con.execute(
        "SELECT * FROM email_login_sessions WHERE token_hash=? AND status='pending'",
        (email_token_hash(token),),
    ).fetchone()
    if row is None:
        raise ValueError("登录链接不存在或已使用")
    if _email_login_expires_at_expired(row["expires_at"]):
        con.execute("UPDATE email_login_sessions SET status='expired' WHERE token_hash=?", (row["token_hash"],))
        con.commit()
        raise ValueError("登录链接已过期,请重新获取")
    user_id = get_or_create_email_user(con, row["email"])
    con.execute(
        "UPDATE email_login_sessions SET status='confirmed', user_id=? WHERE token_hash=?",
        (user_id, row["token_hash"]),
    )
    join_active_contest(con, user_id)
    record_equity_snapshot(con, user_id, source="email")
    con.commit()
    return user_id


def create_wechat_session(
    con: sqlite3.Connection,
    ttl_minutes: int = 10,
    terms_version: str = "",
    privacy_version: str = "",
    risk_version: str = "",
) -> str:
    token = secrets.token_urlsafe(24)
    expires = iso(utc_now() + timedelta(minutes=ttl_minutes))
    accepted_at = iso(utc_now()) if terms_version and privacy_version and risk_version else ""
    con.execute(
        """
        INSERT INTO wechat_sessions(
            token, status, expires_at,
            accepted_terms_version, accepted_privacy_version, accepted_risk_version, accepted_at
        )
        VALUES (?, 'pending', ?, ?, ?, ?, ?)
        """,
        (token, expires, terms_version, privacy_version, risk_version, accepted_at),
    )
    con.commit()
    return token


def wechat_session_legal_acceptance(con: sqlite3.Connection, token: str) -> dict | None:
    row = con.execute(
        """
        SELECT accepted_terms_version, accepted_privacy_version, accepted_risk_version, accepted_at
        FROM wechat_sessions
        WHERE token=?
        """,
        (token,),
    ).fetchone()
    if row is None:
        return None
    if not (row["accepted_terms_version"] and row["accepted_privacy_version"] and row["accepted_risk_version"]):
        return None
    return dict(row)


def confirm_wechat_session(con: sqlite3.Connection, token: str, nickname: str | None = None) -> int:
    row = con.execute(
        "SELECT * FROM wechat_sessions WHERE token=? AND status='pending'",
        (token,),
    ).fetchone()
    if row is None:
        raise ValueError("二维码会话不存在或已使用")
    if datetime.fromisoformat(row["expires_at"]) < utc_now():
        con.execute("UPDATE wechat_sessions SET status='expired' WHERE token=?", (token,))
        con.commit()
        raise ValueError("二维码已过期,请刷新注册页")

    nick = (nickname or "").strip() or f"微信用户{token[:4]}"
    openid = f"dev-wechat-{token}"
    user_id = get_or_create_user(con, openid, nick)
    con.execute(
        "UPDATE wechat_sessions SET status='confirmed', user_id=? WHERE token=?",
        (user_id, token),
    )
    join_active_contest(con, user_id)
    record_equity_snapshot(con, user_id, source="register")
    con.commit()
    return user_id


def wechat_session_status(con: sqlite3.Connection, token: str) -> dict:
    row = con.execute("SELECT * FROM wechat_sessions WHERE token=?", (token,)).fetchone()
    if row is None:
        return {"status": "missing", "user_id": None}
    status = row["status"]
    if status == "pending" and datetime.fromisoformat(row["expires_at"]) < utc_now():
        con.execute("UPDATE wechat_sessions SET status='expired' WHERE token=?", (token,))
        con.commit()
        status = "expired"
    return {"status": status, "user_id": row["user_id"]}


def confirm_wechat_oauth_code(con: sqlite3.Connection, state: str, code: str) -> int:
    """Confirm a real WeChat Open Platform OAuth callback.

    This is only active when WECHAT_APP_ID and WECHAT_APP_SECRET are configured.
    The state parameter must be a pending QR session token created by this app.
    """
    app_id = os.getenv("WECHAT_APP_ID", "").strip()
    app_secret = os.getenv("WECHAT_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ValueError("未配置 WECHAT_APP_ID / WECHAT_APP_SECRET")
    session = con.execute(
        "SELECT * FROM wechat_sessions WHERE token=? AND status='pending'",
        (state,),
    ).fetchone()
    if session is None:
        raise ValueError("二维码会话不存在或已使用")
    if datetime.fromisoformat(session["expires_at"]) < utc_now():
        con.execute("UPDATE wechat_sessions SET status='expired' WHERE token=?", (state,))
        con.commit()
        raise ValueError("二维码已过期,请刷新注册页")

    token_url = "https://api.weixin.qq.com/sns/oauth2/access_token?" + urllib.parse.urlencode(
        {
            "appid": app_id,
            "secret": app_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    )
    payload = _json_get(token_url)
    if "errcode" in payload:
        raise ValueError(f"微信授权失败: {payload.get('errmsg', payload['errcode'])}")
    openid = payload.get("openid")
    access_token = payload.get("access_token")
    if not openid or not access_token:
        raise ValueError("微信授权返回缺少 openid/access_token")

    profile = _json_get(
        "https://api.weixin.qq.com/sns/userinfo?"
        + urllib.parse.urlencode(
            {
                "access_token": access_token,
                "openid": openid,
                "lang": "zh_CN",
            }
        )
    )
    nickname = profile.get("nickname") or f"微信用户{openid[-4:]}"
    avatar_url = profile.get("headimgurl") or ""
    user_id = get_or_create_user(con, f"wechat-{openid}", nickname, avatar_url)
    con.execute(
        "UPDATE wechat_sessions SET status='confirmed', user_id=? WHERE token=?",
        (user_id, state),
    )
    join_active_contest(con, user_id)
    record_equity_snapshot(con, user_id, source="wechat")
    con.commit()
    return user_id


def _json_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=8) as resp:  # noqa: S310 - fixed WeChat endpoints
        return json.loads(resp.read().decode("utf-8"))


def get_or_create_user(
    con: sqlite3.Connection,
    wechat_openid: str,
    nickname: str,
    avatar_url: str = "",
) -> int:
    nickname = (nickname or "").strip() or "微信用户"
    avatar_url = (avatar_url or "").strip()
    row = con.execute("SELECT id, avatar_url FROM users WHERE wechat_openid=?", (wechat_openid,)).fetchone()
    if row:
        con.execute(
            "UPDATE users SET nickname=?, avatar_url=? WHERE id=?",
            (nickname, avatar_url or row["avatar_url"] or "", row["id"]),
        )
        return int(row["id"])
    cur = con.execute(
        "INSERT INTO users(nickname, wechat_openid, avatar_url) VALUES (?, ?, ?)",
        (nickname, wechat_openid, avatar_url),
    )
    user_id = int(cur.lastrowid)
    con.execute(
        "INSERT INTO accounts(user_id, initial_cash, cash) VALUES (?, ?, ?)",
        (user_id, INITIAL_CASH, INITIAL_CASH),
    )
    return user_id


def get_or_create_email_user(con: sqlite3.Connection, email: str) -> int:
    normalized = normalize_email(email)
    row = con.execute("SELECT id, nickname FROM users WHERE email=?", (normalized,)).fetchone()
    if row:
        return int(row["id"])
    local = normalized.split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    nickname = local[:40] or "邮箱用户"
    identity = "email-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    cur = con.execute(
        "INSERT INTO users(nickname, wechat_openid, email, avatar_url) VALUES (?, ?, ?, '')",
        (nickname, identity, normalized),
    )
    user_id = int(cur.lastrowid)
    con.execute(
        "INSERT INTO accounts(user_id, initial_cash, cash) VALUES (?, ?, ?)",
        (user_id, INITIAL_CASH, INITIAL_CASH),
    )
    return user_id


def get_user_by_email(con: sqlite3.Connection, email: str):
    return con.execute("SELECT * FROM users WHERE email=?", (normalize_email(email),)).fetchone()


def suggest_login_name(email: str) -> str:
    local = normalize_email(email).split("@", 1)[0].lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", local).strip("-_")
    if not cleaned:
        cleaned = "user"
    if not cleaned[0].isalnum():
        cleaned = "u" + cleaned
    cleaned = cleaned[:32].strip("-_")
    if len(cleaned) < 3:
        cleaned = (cleaned + "user")[:5]
    return cleaned


def normalize_login_name(login_name: str) -> str:
    value = (login_name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", value):
        raise ValueError("用户名需为 3-32 位小写字母、数字、下划线或短横线,且以字母或数字开头")
    return value


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("密码不能为空")
    if len(password) < 10:
        raise ValueError("密码至少需要 10 位")
    if len(password) > 128:
        raise ValueError("密码最多 128 位")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("密码需要同时包含字母和数字")


def password_hash(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    if not encoded:
        return False
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
    except Exception:  # noqa: BLE001 - malformed stored password hashes should fail closed
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def ensure_login_name_available(con: sqlite3.Connection, login_name: str, user_id: int | None = None) -> str:
    normalized = normalize_login_name(login_name)
    row = con.execute("SELECT id FROM users WHERE login_name=?", (normalized,)).fetchone()
    if row and (user_id is None or int(row["id"]) != int(user_id)):
        raise ValueError("用户名已被占用")
    return normalized


def set_user_password(
    con: sqlite3.Connection,
    user_id: int,
    login_name: str,
    password: str,
    update_nickname: bool = True,
) -> None:
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    normalized = ensure_login_name_available(con, login_name, user_id=user_id)
    hashed = password_hash(password)
    if update_nickname:
        con.execute(
            """
            UPDATE users
            SET login_name=?,
                password_hash=?,
                password_updated_at=CURRENT_TIMESTAMP,
                nickname=?,
                session_version=COALESCE(session_version, 1) + 1
            WHERE id=?
            """,
            (normalized, hashed, normalized, int(user_id)),
        )
    else:
        con.execute(
            """
            UPDATE users
            SET login_name=?,
                password_hash=?,
                password_updated_at=CURRENT_TIMESTAMP,
                session_version=COALESCE(session_version, 1) + 1
            WHERE id=?
            """,
            (normalized, hashed, int(user_id)),
        )
    con.commit()


def authenticate_user(con: sqlite3.Connection, identifier: str, password: str) -> int | None:
    value = (identifier or "").strip()
    if not value or not password:
        return None
    if "@" in value:
        try:
            lookup = normalize_email(value)
        except ValueError:
            return None
        row = con.execute("SELECT * FROM users WHERE email=?", (lookup,)).fetchone()
    else:
        try:
            lookup = normalize_login_name(value)
        except ValueError:
            return None
        row = con.execute("SELECT * FROM users WHERE login_name=?", (lookup,)).fetchone()
    if row is None:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return int(row["id"])


def update_user_profile(con: sqlite3.Connection, user_id: int, nickname: str, avatar_url: str = "") -> None:
    nickname = (nickname or "").strip()
    avatar_url = (avatar_url or "").strip()
    if not nickname:
        raise ValueError("昵称不能为空")
    if avatar_url and not (avatar_url.startswith("https://") or avatar_url.startswith("http://")):
        raise ValueError("头像 URL 必须以 http:// 或 https:// 开头")
    if get_user(con, user_id) is None:
        raise ValueError("用户不存在")
    con.execute(
        "UPDATE users SET nickname=?, avatar_url=? WHERE id=?",
        (nickname[:80], avatar_url[:500], user_id),
    )
    con.commit()


def user_status(user) -> str:
    if user is None:
        return "missing"
    try:
        status = str(user["status"] or "active").strip().lower()
    except Exception:  # noqa: BLE001 - compatibility with rows created before migration
        status = "active"
    return status or "active"


def ensure_user_active(user) -> None:
    if user is None:
        raise ValueError("用户不存在")
    if user_status(user) != "active":
        reason = ""
        try:
            reason = str(user["status_reason"] or "").strip()
        except Exception:  # noqa: BLE001
            reason = ""
        suffix = f": {reason}" if reason else ""
        raise ValueError(f"账户已被暂停,暂不能提交交易、演练计划或社区内容{suffix}")


def update_user_status(con: sqlite3.Connection, user_id: int, status: str, reason: str = "") -> None:
    status = (status or "").strip().lower()
    if status not in {"active", "suspended"}:
        raise ValueError("用户状态无效")
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    reason = "" if status == "active" else (reason or "").strip()[:300]
    con.execute(
        """
        UPDATE users
        SET status=?, status_reason=?, status_updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, reason, int(user_id)),
    )
    con.commit()


def seed_demo_competition(con: sqlite3.Connection) -> dict:
    """Create deterministic demo participants, positions, snapshots, and forum posts."""
    update_active_contest(con, "OurWorlds 模拟盘公开赛", "演示账户展示不同策略风格的模拟盘表现。")
    created_users = 0
    posts = 0
    for player in DEMO_PLAYERS:
        existed = con.execute("SELECT id FROM users WHERE wechat_openid=?", (player["openid"],)).fetchone()
        user_id = get_or_create_user(con, player["openid"], player["nickname"])
        if existed is None:
            created_users += 1
        join_active_contest(con, user_id)
        account = account_for_user(con, user_id)
        price_row = get_price(con, player["code"])
        if account is None or price_row is None:
            continue
        price = float(price_row["price"])
        avg_price = round(price * float(player["avg_multiplier"]), 4)
        qty = int(player["qty"])
        invested = round(avg_price * qty, 2)
        cash = max(float(account["initial_cash"]) - invested, 0.0)

        con.execute("DELETE FROM holdings WHERE account_id=?", (account["id"],))
        con.execute("DELETE FROM orders WHERE account_id=?", (account["id"],))
        con.execute("DELETE FROM equity_snapshots WHERE account_id=?", (account["id"],))
        con.execute("UPDATE accounts SET cash=? WHERE id=?", (cash, account["id"]))
        con.execute(
            "INSERT INTO holdings(account_id, code, qty, available_qty, avg_price) VALUES (?, ?, ?, ?, ?)",
            (account["id"], player["code"], qty, qty, avg_price),
        )
        con.execute(
            """
            INSERT INTO orders(account_id, code, side, qty, price, fee, amount)
            VALUES (?, ?, 'buy', ?, ?, 0, ?)
            """,
            (account["id"], player["code"], qty, avg_price, invested),
        )
        record_equity_snapshot(con, user_id, source="demo_seed")
        if con.execute(
            "SELECT 1 FROM forum_posts WHERE user_id=? AND title=?",
            (user_id, player["title"]),
        ).fetchone() is None:
            create_post(con, user_id, player["title"], player["body"], player["tag"], attach_snapshot=True)
            posts += 1
    con.commit()
    return {"users_created": created_users, "players": len(DEMO_PLAYERS), "posts_created": posts}


def demo_contest_participant_summary(con: sqlite3.Connection) -> dict:
    row = con.execute(
        """
        SELECT COUNT(DISTINCT u.id) AS participants,
               GROUP_CONCAT(DISTINCT u.id) AS user_ids
        FROM contest_participants cp
        JOIN contests c ON c.id=cp.contest_id AND c.is_active=1
        JOIN users u ON u.id=cp.user_id
        WHERE u.wechat_openid LIKE 'demo-%'
           OR u.wechat_openid LIKE 'dev-wechat-%'
           OR COALESCE(u.email, '') LIKE 'demo-%'
           OR u.nickname LIKE '模拟用户%'
        """
    ).fetchone()
    return {
        "participants": int(row["participants"] or 0) if row else 0,
        "user_ids": str(row["user_ids"] or "") if row else "",
    }


def remove_demo_contest_participants(con: sqlite3.Connection) -> dict:
    summary = demo_contest_participant_summary(con)
    user_ids = [int(item) for item in str(summary["user_ids"] or "").split(",") if item.strip().isdigit()]
    if not user_ids:
        return {"participants_removed": 0, "user_ids": ""}
    placeholders = ",".join("?" for _ in user_ids)
    cur = con.execute(
        f"""
        DELETE FROM contest_participants
        WHERE user_id IN ({placeholders})
          AND contest_id IN (SELECT id FROM contests WHERE is_active=1)
        """,
        user_ids,
    )
    con.commit()
    return {"participants_removed": int(cur.rowcount if cur.rowcount is not None else 0), "user_ids": ",".join(str(item) for item in user_ids)}


def get_user(con: sqlite3.Connection, user_id: int):
    return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def user_session_version(user) -> int:
    if user is None:
        return 1
    try:
        version = int(user["session_version"] or 1)
    except Exception:  # noqa: BLE001 - rows from older databases may not expose the column in tests
        version = 1
    return max(1, version)


def bump_user_session_version(con: sqlite3.Connection, user_id: int) -> int:
    cur = con.execute(
        "UPDATE users SET session_version=COALESCE(session_version, 1) + 1 WHERE id=?",
        (int(user_id),),
    )
    if cur.rowcount == 0:
        raise ValueError("用户不存在")
    row = con.execute("SELECT session_version FROM users WHERE id=?", (int(user_id),)).fetchone()
    con.commit()
    return user_session_version(row)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _first_user_admin_fallback_allowed() -> bool:
    env = os.getenv("OWQ_ENV", "").strip().lower()
    public_base = os.getenv("OWQ_PUBLIC_BASE_URL", "").strip()
    if env in {"prod", "production"} or _env_flag("OWQ_ENV_PRODUCTION"):
        return False
    if public_base:
        return False
    return True


def is_admin(con: sqlite3.Connection, user) -> bool:
    if user is None:
        return False
    admin_ids = {
        item.strip()
        for item in os.getenv("OWQ_ADMIN_USER_IDS", "").replace(";", ",").split(",")
        if item.strip()
    }
    admin_openids = {
        item.strip()
        for item in os.getenv("OWQ_ADMIN_OPENIDS", "").replace(";", ",").split(",")
        if item.strip()
    }
    admin_emails = {
        item.strip().lower()
        for item in os.getenv("OWQ_ADMIN_EMAILS", "").replace(";", ",").split(",")
        if item.strip()
    }
    if admin_ids or admin_openids or admin_emails:
        return (
            str(user["id"]) in admin_ids
            or str(user["wechat_openid"]) in admin_openids
            or str(user["email"] or "").lower() in admin_emails
        )

    if not _first_user_admin_fallback_allowed():
        return False

    first = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    return first is not None and int(user["id"]) == int(first["id"])


def account_for_user(con: sqlite3.Connection, user_id: int):
    return con.execute("SELECT * FROM accounts WHERE user_id=?", (user_id,)).fetchone()


def active_contest(con: sqlite3.Connection):
    return con.execute("SELECT * FROM contests WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()


def update_active_contest(con: sqlite3.Connection, title: str, description: str) -> int:
    title = title.strip()
    description = description.strip()
    if not title:
        raise ValueError("比赛名称不能为空")
    contest = active_contest(con)
    if contest is None:
        cur = con.execute(
            "INSERT INTO contests(title, description, is_active) VALUES (?, ?, 1)",
            (title, description),
        )
        contest_id = int(cur.lastrowid)
    else:
        contest_id = int(contest["id"])
        con.execute(
            "UPDATE contests SET title=?, description=? WHERE id=?",
            (title, description, contest_id),
        )
    con.commit()
    return contest_id


def join_active_contest(con: sqlite3.Connection, user_id: int) -> None:
    contest = active_contest(con)
    account = account_for_user(con, user_id)
    if contest is None or account is None:
        return
    con.execute(
        """
        INSERT OR IGNORE INTO contest_participants(contest_id, user_id, account_id)
        VALUES (?, ?, ?)
        """,
        (contest["id"], user_id, account["id"]),
    )
    con.commit()


def market_rows(con: sqlite3.Connection, real_only: bool = False, limit: int | None = None):
    where = "WHERE price > 0 AND prev_close > 0"
    params: list = []
    if real_only:
        where += " AND source <> 'demo'"
    q = f"SELECT * FROM market_prices {where} ORDER BY code"
    if limit is not None:
        q += " LIMIT ?"
        params.append(int(limit))
    return con.execute(q, params).fetchall()


def market_source_summary(con: sqlite3.Connection):
    return con.execute(
        """
        SELECT source,
               COUNT(*) AS rows,
               COUNT(DISTINCT code) AS codes,
               MIN(as_of) AS date_min,
               MAX(as_of) AS date_max,
               MAX(updated_at) AS updated_at
        FROM market_prices
        GROUP BY source
        ORDER BY source
        """
    ).fetchall()


def landing_summary(con: sqlite3.Connection) -> dict:
    """Return live public-homepage data from the application database."""
    board = leaderboard(con)
    contest = active_contest(con)
    posts = forum_posts(con, limit=3)
    sources = market_source_summary(con)
    counts = con.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS user_count,
            (SELECT COUNT(*) FROM contest_participants) AS participant_count,
            (SELECT COUNT(*) FROM orders) AS order_count,
            (SELECT COUNT(*) FROM practice_signals) AS signal_count,
            (SELECT COUNT(*) FROM forum_posts) AS post_count,
            (SELECT COUNT(*) FROM forum_comments) AS comment_count,
            (
                SELECT COUNT(DISTINCT code)
                FROM market_prices
                WHERE price > 0 AND prev_close > 0
            ) AS market_code_count,
            (
                SELECT COUNT(DISTINCT code)
                FROM market_prices
                WHERE price > 0 AND prev_close > 0 AND source <> 'demo'
            ) AS real_market_code_count,
            (
                SELECT MAX(as_of)
                FROM market_prices
                WHERE price > 0 AND prev_close > 0
            ) AS market_as_of
        """
    ).fetchone()
    return {
        "contest": contest,
        "leaderboard": board[:5],
        "latest_posts": posts,
        "sources": sources,
        "user_count": int(counts["user_count"] or 0),
        "participant_count": int(counts["participant_count"] or 0),
        "order_count": int(counts["order_count"] or 0),
        "signal_count": int(counts["signal_count"] or 0),
        "post_count": int(counts["post_count"] or 0),
        "comment_count": int(counts["comment_count"] or 0),
        "market_code_count": int(counts["market_code_count"] or 0),
        "real_market_code_count": int(counts["real_market_code_count"] or 0),
        "market_as_of": counts["market_as_of"] or "",
    }


def get_price(con: sqlite3.Connection, code: str):
    return con.execute("SELECT * FROM market_prices WHERE code=?", (code,)).fetchone()


def normalize_side(side: str) -> str:
    raw = (side or "").strip().lower()
    mapping = {
        "buy": "buy",
        "b": "buy",
        "买": "buy",
        "买入": "buy",
        "sell": "sell",
        "s": "sell",
        "卖": "sell",
        "卖出": "sell",
    }
    return mapping.get(raw, raw)


def _parse_qty(value) -> int:
    text = str(value or "").strip().replace(",", "").replace("股", "")
    if not text:
        raise ValueError("数量必须大于 0")
    return int(text)


def _validated_practice_signal(
    con: sqlite3.Connection,
    user_id: int,
    strategy_name: str,
    code: str,
    side: str,
    qty,
    rationale: str = "",
) -> dict:
    ensure_user_active(get_user(con, user_id))
    strategy_name = (strategy_name or "").strip()
    rationale = (rationale or "").strip()
    code = (code or "").strip().upper()
    side = normalize_side(side)
    qty = _parse_qty(qty)
    if not strategy_name:
        raise ValueError("策略名称不能为空")
    if side not in {"buy", "sell"}:
        raise ValueError("交易方向必须是 buy 或 sell")
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    if side == "buy" and qty % 100 != 0:
        raise ValueError("A 股买入数量必须为 100 股整数倍")
    if account_for_user(con, user_id) is None:
        raise ValueError("账户不存在")
    if get_price(con, code) is None:
        raise ValueError("标的不存在")
    return {
        "strategy_name": strategy_name[:80],
        "code": code,
        "side": side,
        "qty": qty,
        "rationale": rationale[:1000],
    }


def create_practice_signal(
    con: sqlite3.Connection,
    user_id: int,
    strategy_name: str,
    code: str,
    side: str,
    qty: int,
    rationale: str = "",
) -> int:
    """Create a paper-trading practice plan that can later be executed."""
    payload = _validated_practice_signal(con, user_id, strategy_name, code, side, qty, rationale)
    cur = con.execute(
        """
        INSERT INTO practice_signals(user_id, code, side, qty, strategy_name, rationale)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            payload["code"],
            payload["side"],
            payload["qty"],
            payload["strategy_name"],
            payload["rationale"],
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def parse_practice_signal_batch(text: str) -> list[dict[str, str]]:
    """Parse a pasted strategy basket into practice-signal rows.

    Accepted CSV columns are code,side,qty,rationale. A header row is optional.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("策略篮子不能为空")
    clean = "\n".join(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    reader = csv.reader(io.StringIO(clean))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError("策略篮子不能为空")
    header = [cell.strip().lower() for cell in rows[0]]
    has_header = {"code", "side", "qty"}.issubset(set(header))
    parsed: list[dict[str, str]] = []
    data_rows = rows[1:] if has_header else rows
    for idx, row in enumerate(data_rows, start=2 if has_header else 1):
        if has_header:
            item = {header[i]: row[i].strip() if i < len(row) else "" for i in range(len(header))}
        else:
            if len(row) < 3:
                raise ValueError(f"第 {idx} 行至少需要 code,side,qty")
            item = {
                "code": row[0].strip(),
                "side": row[1].strip(),
                "qty": row[2].strip(),
                "rationale": row[3].strip() if len(row) > 3 else "",
            }
        parsed.append(
            {
                "code": item.get("code", ""),
                "side": item.get("side", ""),
                "qty": item.get("qty", ""),
                "rationale": item.get("rationale", ""),
            }
        )
    if not parsed:
        raise ValueError("策略篮子不能为空")
    return parsed


def create_practice_signal_batch(
    con: sqlite3.Connection,
    user_id: int,
    strategy_name: str,
    text: str,
    default_rationale: str = "",
) -> int:
    rows = parse_practice_signal_batch(text)
    if len(rows) > 50:
        raise ValueError("一次最多导入 50 条演练计划")
    payloads = []
    for idx, row in enumerate(rows, start=1):
        rationale = row.get("rationale") or default_rationale
        try:
            payloads.append(
                _validated_practice_signal(
                    con,
                    user_id,
                    strategy_name,
                    row.get("code", ""),
                    row.get("side", ""),
                    row.get("qty", ""),
                    rationale,
                )
            )
        except ValueError as exc:
            raise ValueError(f"第 {idx} 行: {exc}") from exc

    for payload in payloads:
        con.execute(
            """
            INSERT INTO practice_signals(user_id, code, side, qty, strategy_name, rationale)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                payload["code"],
                payload["side"],
                payload["qty"],
                payload["strategy_name"],
                payload["rationale"],
            ),
        )
    con.commit()
    return len(payloads)


def market_signal_basket_rows(
    con: sqlite3.Connection,
    mode: str = "reversal",
    side: str = "buy",
    qty=100,
    limit: int = 5,
    real_only: bool = False,
) -> list[dict]:
    mode = (mode or "reversal").strip().lower()
    if mode not in {"reversal", "momentum"}:
        raise ValueError("行情篮子模式必须是 reversal 或 momentum")
    side = normalize_side(side)
    if side not in {"buy", "sell"}:
        raise ValueError("交易方向必须是 buy 或 sell")
    qty = _parse_qty(qty)
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    if side == "buy" and qty % 100 != 0:
        raise ValueError("A 股买入数量必须为 100 股整数倍")
    limit = int(limit)
    if limit <= 0:
        raise ValueError("候选数量必须大于 0")
    if limit > 50:
        raise ValueError("一次最多生成 50 条演练计划")

    order = "ASC" if mode == "reversal" else "DESC"
    where = "WHERE price > 0 AND prev_close > 0"
    if real_only:
        where += " AND source <> 'demo'"
    rows = con.execute(
        f"""
        SELECT code, name, price, prev_close, source, as_of,
               (price / prev_close - 1.0) * 100.0 AS change_pct
        FROM market_prices
        {where}
        ORDER BY change_pct {order}, code
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        raise ValueError("基础行情为空,请先同步真实行情")
    label = "反转候选" if mode == "reversal" else "动量候选"
    out = []
    for row in rows:
        out.append(
            {
                "code": row["code"],
                "name": row["name"],
                "side": side,
                "qty": qty,
                "rationale": (
                    f"{label}: {row['name']} 当前价 {_money(row['price'])}, "
                    f"涨跌幅 {_pct(float(row['change_pct']))}, 来源 {row['source']}"
                    f"{(' / ' + row['as_of']) if row['as_of'] else ''}"
                ),
                "change_pct": float(row["change_pct"]),
            }
        )
    return out


def create_practice_signals_from_market(
    con: sqlite3.Connection,
    user_id: int,
    strategy_name: str,
    mode: str = "reversal",
    side: str = "buy",
    qty=100,
    limit: int = 5,
    real_only: bool = False,
) -> int:
    if account_for_user(con, user_id) is None:
        raise ValueError("账户不存在")
    mode = (mode or "reversal").strip().lower()
    default_name = "基础行情反转篮子" if mode == "reversal" else "基础行情动量篮子"
    strategy_name = (strategy_name or default_name).strip()
    rows = market_signal_basket_rows(con, mode=mode, side=side, qty=qty, limit=limit, real_only=real_only)
    payloads = [
        _validated_practice_signal(
            con,
            user_id,
            strategy_name,
            row["code"],
            row["side"],
            row["qty"],
            row["rationale"],
        )
        for row in rows
    ]
    for payload in payloads:
        con.execute(
            """
            INSERT INTO practice_signals(user_id, code, side, qty, strategy_name, rationale)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                payload["code"],
                payload["side"],
                payload["qty"],
                payload["strategy_name"],
                payload["rationale"],
            ),
        )
    con.commit()
    return len(payloads)


def prediction_basket_rows(
    con: sqlite3.Connection,
    path: str | Path | None = None,
    side: str = "buy",
    qty=100,
    limit: int = 5,
) -> list[dict]:
    side = normalize_side(side)
    if side not in {"buy", "sell"}:
        raise ValueError("交易方向必须是 buy 或 sell")
    qty = _parse_qty(qty)
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    if side == "buy" and qty % 100 != 0:
        raise ValueError("A 股买入数量必须为 100 股整数倍")
    limit = int(limit)
    if limit <= 0:
        raise ValueError("候选数量必须大于 0")
    if limit > 50:
        raise ValueError("一次最多生成 50 条演练计划")

    csv_path = Path(path or os.getenv("OWQ_PREDICTIONS_CSV", "reports/predictions.csv"))
    if not csv_path.exists():
        raise ValueError(f"预测结果不存在: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    parsed = []
    for row in rows:
        code = (row.get("code") or "").strip().upper()
        if not code:
            continue
        try:
            pred = float(row.get("prediction", ""))
        except ValueError:
            continue
        price = get_price(con, code)
        if price is None:
            continue
        parsed.append(
            {
                "code": code,
                "name": price["name"],
                "side": side,
                "qty": qty,
                "prediction": pred,
                "last_close": float(price["price"]),
                "rationale": (
                    f"预测候选: 模型下一期预测 {_pct(pred * 100)}, "
                    f"当前价 {_money(price['price'])}, 来源 {price['source']}"
                    f"{(' / ' + price['as_of']) if price['as_of'] else ''}"
                ),
            }
        )
    parsed.sort(key=lambda item: item["prediction"], reverse=True)
    if not parsed:
        raise ValueError("预测结果里没有可用标的,请先同步行情并生成预测")
    return parsed[:limit]


def create_practice_signals_from_predictions(
    con: sqlite3.Connection,
    user_id: int,
    strategy_name: str,
    qty=100,
    limit: int = 5,
    path: str | Path | None = None,
) -> int:
    if account_for_user(con, user_id) is None:
        raise ValueError("账户不存在")
    strategy_name = (strategy_name or "模型预测候选篮子").strip()
    rows = prediction_basket_rows(con, path=path, side="buy", qty=qty, limit=limit)
    payloads = [
        _validated_practice_signal(
            con,
            user_id,
            strategy_name,
            row["code"],
            row["side"],
            row["qty"],
            row["rationale"],
        )
        for row in rows
    ]
    for payload in payloads:
        con.execute(
            """
            INSERT INTO practice_signals(user_id, code, side, qty, strategy_name, rationale)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                payload["code"],
                payload["side"],
                payload["qty"],
                payload["strategy_name"],
                payload["rationale"],
            ),
        )
    con.commit()
    return len(payloads)


def practice_signals(con: sqlite3.Connection, user_id: int, status: str | None = None, limit: int = 20):
    params: list = [user_id]
    where = "s.user_id=?"
    if status:
        where += " AND s.status=?"
        params.append(status)
    params.append(int(limit))
    return con.execute(
        f"""
        SELECT s.*, m.name, m.price, o.price AS executed_price, o.created_at AS executed_at
        FROM practice_signals s
        LEFT JOIN market_prices m ON m.code=s.code
        LEFT JOIN orders o ON o.id=s.order_id
        WHERE {where}
        ORDER BY s.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _signal_for_user(con: sqlite3.Connection, user_id: int, signal_id: int):
    return con.execute(
        "SELECT * FROM practice_signals WHERE id=? AND user_id=?",
        (int(signal_id), user_id),
    ).fetchone()


def execute_practice_signal(con: sqlite3.Connection, user_id: int, signal_id: int) -> int:
    signal = _signal_for_user(con, user_id, signal_id)
    if signal is None:
        raise ValueError("演练计划不存在")
    if signal["status"] != "pending":
        raise ValueError("只能执行待执行的演练计划")
    order_id = place_order(con, user_id, signal["code"], signal["side"], int(signal["qty"]))
    con.execute(
        """
        UPDATE practice_signals
        SET status='executed', order_id=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND user_id=?
        """,
        (order_id, signal_id, user_id),
    )
    con.commit()
    return order_id


def execute_pending_practice_signals(con: sqlite3.Connection, user_id: int, limit: int = 20) -> dict:
    limit = int(limit)
    if limit <= 0:
        raise ValueError("执行数量必须大于 0")
    if limit > 50:
        raise ValueError("一次最多执行 50 条演练计划")
    rows = con.execute(
        """
        SELECT id, code
        FROM practice_signals
        WHERE user_id=? AND status='pending'
        ORDER BY id
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    executed = []
    failed = []
    for row in rows:
        signal_id = int(row["id"])
        try:
            order_id = execute_practice_signal(con, user_id, signal_id)
            executed.append({"signal_id": signal_id, "order_id": order_id, "code": row["code"]})
        except Exception as exc:  # noqa: BLE001 - return per-signal failure detail to caller
            failed.append({"signal_id": signal_id, "code": row["code"], "error": str(exc)})
    return {"total": len(rows), "executed": executed, "failed": failed}


def cancel_practice_signal(con: sqlite3.Connection, user_id: int, signal_id: int) -> None:
    signal = _signal_for_user(con, user_id, signal_id)
    if signal is None:
        raise ValueError("演练计划不存在")
    if signal["status"] != "pending":
        raise ValueError("只能取消待执行的演练计划")
    con.execute(
        """
        UPDATE practice_signals
        SET status='cancelled', updated_at=CURRENT_TIMESTAMP
        WHERE id=? AND user_id=?
        """,
        (signal_id, user_id),
    )
    con.commit()


def fee(amount: float, side: str) -> float:
    commission = max(amount * 0.00025, 5.0)
    transfer = amount * 0.00001
    stamp = amount * 0.0005 if side == "sell" else 0.0
    return round(commission + transfer + stamp, 2)


def place_order(con: sqlite3.Connection, user_id: int, code: str, side: str, qty: int) -> int:
    ensure_user_active(get_user(con, user_id))
    code = code.strip().upper()
    side = side.strip().lower()
    qty = int(qty)
    if side not in {"buy", "sell"}:
        raise ValueError("交易方向必须是 buy 或 sell")
    if qty <= 0:
        raise ValueError("数量必须大于 0")
    if side == "buy" and qty % 100 != 0:
        raise ValueError("A 股买入数量必须为 100 股整数倍")

    account = account_for_user(con, user_id)
    if account is None:
        raise ValueError("账户不存在")
    price_row = get_price(con, code)
    if price_row is None:
        raise ValueError("标的不存在")
    price = float(price_row["price"])
    gross = round(price * qty, 2)
    trade_fee = fee(gross, side)
    cash = float(account["cash"])
    holding = con.execute(
        "SELECT * FROM holdings WHERE account_id=? AND code=?",
        (account["id"], code),
    ).fetchone()

    if side == "buy":
        total = gross + trade_fee
        if total > cash:
            raise ValueError("现金不足")
        old_qty = int(holding["qty"]) if holding else 0
        old_cost = old_qty * float(holding["avg_price"]) if holding else 0.0
        new_qty = old_qty + qty
        new_avg = round((old_cost + gross) / new_qty, 4)
        con.execute("UPDATE accounts SET cash=cash-? WHERE id=?", (total, account["id"]))
        con.execute(
            """
            INSERT INTO holdings(account_id, code, qty, available_qty, avg_price)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(account_id, code) DO UPDATE SET
                qty=excluded.qty,
                avg_price=excluded.avg_price,
                available_qty=holdings.available_qty
            """,
            (account["id"], code, new_qty, new_avg),
        )
    else:
        if holding is None or int(holding["qty"]) < qty:
            raise ValueError("持仓不足")
        if int(holding["available_qty"]) < qty:
            raise ValueError("可卖数量不足,A 股买入需等下一交易日后才能卖出")
        new_qty = int(holding["qty"]) - qty
        con.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (gross - trade_fee, account["id"]))
        if new_qty == 0:
            con.execute("DELETE FROM holdings WHERE account_id=? AND code=?", (account["id"], code))
        else:
            con.execute(
                "UPDATE holdings SET qty=?, available_qty=available_qty-? WHERE account_id=? AND code=?",
                (new_qty, qty, account["id"], code),
            )

    cur = con.execute(
        """
        INSERT INTO orders(account_id, code, side, qty, price, fee, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (account["id"], code, side, qty, price, trade_fee, gross),
    )
    record_equity_snapshot(con, user_id, source=f"order:{side}")
    con.commit()
    return int(cur.lastrowid)


def settle_account(con: sqlite3.Connection, user_id: int) -> int:
    """Advance the paper account to the next trading day and release T+1 lots."""
    account = account_for_user(con, user_id)
    if account is None:
        raise ValueError("账户不存在")
    cur = con.execute(
        """
        UPDATE holdings
        SET available_qty=qty
        WHERE account_id=? AND available_qty<>qty
        """,
        (account["id"],),
    )
    record_equity_snapshot(con, user_id, source="settle")
    con.commit()
    return int(cur.rowcount)


def reset_paper_account(con: sqlite3.Connection, user_id: int) -> None:
    """Reset a user's paper-trading account while keeping forum posts intact."""
    account = account_for_user(con, user_id)
    if account is None:
        raise ValueError("账户不存在")
    con.execute("DELETE FROM practice_signals WHERE user_id=?", (user_id,))
    con.execute("DELETE FROM holdings WHERE account_id=?", (account["id"],))
    con.execute("DELETE FROM orders WHERE account_id=?", (account["id"],))
    con.execute("DELETE FROM equity_snapshots WHERE account_id=?", (account["id"],))
    con.execute(
        "UPDATE accounts SET cash=initial_cash WHERE id=?",
        (account["id"],),
    )
    record_equity_snapshot(con, user_id, source="reset")
    con.commit()


def portfolio_snapshot(con: sqlite3.Connection, user_id: int) -> dict:
    account = account_for_user(con, user_id)
    if account is None:
        raise ValueError("账户不存在")
    rows = con.execute(
        """
        SELECT h.code, m.name, h.qty, h.available_qty, h.avg_price, m.price,
               h.qty * m.price AS market_value,
               (m.price - h.avg_price) * h.qty AS pnl
        FROM holdings h
        JOIN market_prices m ON m.code=h.code
        WHERE h.account_id=?
        ORDER BY h.code
        """,
        (account["id"],),
    ).fetchall()
    market_value = sum(float(r["market_value"]) for r in rows)
    cash = float(account["cash"])
    equity = cash + market_value
    initial = float(account["initial_cash"])
    return {
        "account": account,
        "holdings": rows,
        "cash": cash,
        "market_value": market_value,
        "equity": equity,
        "return_pct": (equity / initial - 1.0) * 100 if initial else 0.0,
    }


def record_equity_snapshot(con: sqlite3.Connection, user_id: int, source: str = "manual") -> int:
    snap = portfolio_snapshot(con, user_id)
    account = snap["account"]
    cur = con.execute(
        """
        INSERT INTO equity_snapshots(account_id, cash, market_value, equity, return_pct, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            account["id"],
            float(snap["cash"]),
            float(snap["market_value"]),
            float(snap["equity"]),
            float(snap["return_pct"]),
            source[:48],
        ),
    )
    return int(cur.lastrowid)


def equity_history(con: sqlite3.Connection, user_id: int, limit: int = 30):
    account = account_for_user(con, user_id)
    if account is None:
        return []
    rows = con.execute(
        """
        SELECT *
        FROM equity_snapshots
        WHERE account_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (account["id"], int(limit)),
    ).fetchall()
    return list(reversed(rows))


def equity_snapshots(con: sqlite3.Connection, user_id: int):
    account = account_for_user(con, user_id)
    if account is None:
        return []
    return con.execute(
        """
        SELECT *
        FROM equity_snapshots
        WHERE account_id=?
        ORDER BY id
        """,
        (account["id"],),
    ).fetchall()


def record_all_equity_snapshots(con: sqlite3.Connection, source: str = "market") -> int:
    rows = con.execute("SELECT user_id FROM accounts ORDER BY user_id").fetchall()
    count = 0
    for row in rows:
        record_equity_snapshot(con, int(row["user_id"]), source=source)
        count += 1
    con.commit()
    return count


def leaderboard(con: sqlite3.Connection):
    rows = con.execute(
        """
        SELECT u.id AS user_id, u.nickname, a.initial_cash, a.cash,
               COALESCE(SUM(h.qty * m.price), 0) AS market_value,
               a.cash + COALESCE(SUM(h.qty * m.price), 0) AS equity
        FROM contest_participants cp
        JOIN users u ON u.id=cp.user_id
        JOIN accounts a ON a.id=cp.account_id
        LEFT JOIN holdings h ON h.account_id=a.id
        LEFT JOIN market_prices m ON m.code=h.code
        GROUP BY u.id, u.nickname, a.initial_cash, a.cash
        ORDER BY (a.cash + COALESCE(SUM(h.qty * m.price), 0)) / a.initial_cash DESC
        """
    ).fetchall()
    out = []
    for rank, row in enumerate(rows, start=1):
        ret = (float(row["equity"]) / float(row["initial_cash"]) - 1.0) * 100
        out.append({"rank": rank, "row": row, "return_pct": ret})
    return out


def account_overview(con: sqlite3.Connection):
    rows = con.execute(
        """
        SELECT u.id AS user_id, u.nickname, u.email, u.status, u.status_reason, u.status_updated_at, u.created_at, a.id AS account_id,
               a.initial_cash, a.cash,
               COALESCE(SUM(h.qty * m.price), 0) AS market_value,
               a.cash + COALESCE(SUM(h.qty * m.price), 0) AS equity,
               COUNT(DISTINCT o.id) AS order_count,
               COUNT(DISTINCT p.id) AS post_count
        FROM users u
        JOIN accounts a ON a.user_id=u.id
        LEFT JOIN holdings h ON h.account_id=a.id
        LEFT JOIN market_prices m ON m.code=h.code
        LEFT JOIN orders o ON o.account_id=a.id
        LEFT JOIN forum_posts p ON p.user_id=u.id
        GROUP BY u.id, u.nickname, u.email, u.status, u.status_reason, u.status_updated_at, u.created_at, a.id, a.initial_cash, a.cash
        ORDER BY u.id DESC
        """
    ).fetchall()
    out = []
    ranks = {int(item["row"]["user_id"]): item["rank"] for item in leaderboard(con)}
    for row in rows:
        ret = (float(row["equity"]) / float(row["initial_cash"]) - 1.0) * 100
        out.append({"row": row, "return_pct": ret, "rank": ranks.get(int(row["user_id"]))})
    return out


def rank_for_user(con: sqlite3.Connection, user_id: int) -> int | None:
    for row in leaderboard(con):
        if int(row["row"]["user_id"]) == int(user_id):
            return int(row["rank"])
    return None


def public_profile(con: sqlite3.Connection, user_id: int) -> dict:
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    snapshot = portfolio_snapshot(con, user_id)
    history = equity_history(con, user_id, limit=12)
    posts = con.execute(
        """
        SELECT id, title, strategy_tag, snapshot_equity, snapshot_return_pct, snapshot_rank, created_at
        FROM forum_posts
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 8
        """,
        (user_id,),
    ).fetchall()
    return {
        "user": user,
        "snapshot": snapshot,
        "rank": rank_for_user(con, user_id),
        "posts": posts,
        "history": history,
        "orders": recent_orders(con, user_id, limit=8),
    }


def recent_orders(con: sqlite3.Connection, user_id: int, limit: int = 10):
    account = account_for_user(con, user_id)
    if account is None:
        return []
    return con.execute(
        "SELECT * FROM orders WHERE account_id=? ORDER BY id DESC LIMIT ?",
        (account["id"], limit),
    ).fetchall()


def order_history(con: sqlite3.Connection, user_id: int):
    account = account_for_user(con, user_id)
    if account is None:
        return []
    return con.execute(
        "SELECT * FROM orders WHERE account_id=? ORDER BY id",
        (account["id"],),
    ).fetchall()


def account_data_export(con: sqlite3.Connection, user_id: int) -> dict:
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    account = account_for_user(con, user_id)
    snapshot = portfolio_snapshot(con, user_id)
    signals = con.execute(
        """
        SELECT s.*, m.name AS market_name, o.created_at AS executed_at
        FROM practice_signals s
        LEFT JOIN market_prices m ON m.code=s.code
        LEFT JOIN orders o ON o.id=s.order_id
        WHERE s.user_id=?
        ORDER BY s.id
        """,
        (int(user_id),),
    ).fetchall()
    posts = con.execute(
        """
        SELECT *
        FROM forum_posts
        WHERE user_id=?
        ORDER BY id
        """,
        (int(user_id),),
    ).fetchall()
    comments = con.execute(
        """
        SELECT c.*, p.title AS post_title
        FROM forum_comments c
        LEFT JOIN forum_posts p ON p.id=c.post_id
        WHERE c.user_id=?
        ORDER BY c.id
        """,
        (int(user_id),),
    ).fetchall()
    consents = con.execute(
        """
        SELECT *
        FROM user_consents
        WHERE user_id=?
        ORDER BY id
        """,
        (int(user_id),),
    ).fetchall()
    contests = con.execute(
        """
        SELECT cp.contest_id, cp.joined_at, c.title, c.description, c.start_at, c.end_at, c.is_active
        FROM contest_participants cp
        JOIN contests c ON c.id=cp.contest_id
        WHERE cp.user_id=?
        ORDER BY cp.joined_at
        """,
        (int(user_id),),
    ).fetchall()
    reports = con.execute(
        """
        SELECT *
        FROM content_reports
        WHERE reporter_user_id=?
        ORDER BY id
        """,
        (int(user_id),),
    ).fetchall()
    support_where = "requester_user_id=?"
    support_params: list = [int(user_id)]
    if str(user["email"] or "").strip():
        support_where += " OR email=?"
        support_params.append(str(user["email"]).strip().lower())
    support_requests_rows = con.execute(
        f"""
        SELECT id, email, category, subject, message, status, resolution_note, created_at, resolved_at
        FROM support_requests
        WHERE {support_where}
        ORDER BY id
        """,
        support_params,
    ).fetchall()
    audits = con.execute(
        """
        SELECT id, action, target_type, target_id, detail, ip_address, created_at
        FROM audit_events
        WHERE actor_user_id=?
        ORDER BY id
        """,
        (int(user_id),),
    ).fetchall()
    return {
        "exported_at": iso(utc_now()),
        "user": row_dict(user),
        "account": row_dict(account),
        "portfolio": {
            "cash": snapshot["cash"],
            "market_value": snapshot["market_value"],
            "equity": snapshot["equity"],
            "return_pct": snapshot["return_pct"],
            "holdings": row_dicts(snapshot["holdings"]),
        },
        "orders": row_dicts(order_history(con, user_id)),
        "equity_snapshots": row_dicts(equity_snapshots(con, user_id)),
        "practice_signals": row_dicts(signals),
        "consents": row_dicts(consents),
        "contests": row_dicts(contests),
        "forum_posts": row_dicts(posts),
        "forum_comments": row_dicts(comments),
        "content_reports": row_dicts(reports),
        "support_requests": row_dicts(support_requests_rows),
        "audit_events": row_dicts(audits),
    }


def delete_user_account(con: sqlite3.Connection, user_id: int) -> dict:
    """Delete a user's account and directly identifying application data."""
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    account = account_for_user(con, user_id)
    email = str(user["email"] or "").strip()
    post_ids = [
        int(row["id"])
        for row in con.execute("SELECT id FROM forum_posts WHERE user_id=?", (int(user_id),)).fetchall()
    ]
    comment_rows = con.execute(
        """
        SELECT id
        FROM forum_comments
        WHERE user_id=?
           OR post_id IN (SELECT id FROM forum_posts WHERE user_id=?)
        """,
        (int(user_id), int(user_id)),
    ).fetchall()
    comment_ids = [int(row["id"]) for row in comment_rows]

    summary = {
        "user_id": int(user_id),
        "account_id": int(account["id"]) if account else None,
        "orders": con.execute(
            "SELECT COUNT(*) FROM orders WHERE account_id=?",
            (int(account["id"]) if account else -1,),
        ).fetchone()[0],
        "practice_signals": con.execute(
            "SELECT COUNT(*) FROM practice_signals WHERE user_id=?",
            (int(user_id),),
        ).fetchone()[0],
        "forum_posts": len(post_ids),
        "forum_comments": len(comment_ids),
        "consents": con.execute("SELECT COUNT(*) FROM user_consents WHERE user_id=?", (int(user_id),)).fetchone()[0],
        "support_requests": con.execute(
            "SELECT COUNT(*) FROM support_requests WHERE requester_user_id=? OR email=?",
            (int(user_id), email),
        ).fetchone()[0],
    }

    if post_ids:
        placeholders = ",".join("?" for _ in post_ids)
        con.execute(f"DELETE FROM content_reports WHERE target_type='post' AND target_id IN ({placeholders})", post_ids)
    if comment_ids:
        placeholders = ",".join("?" for _ in comment_ids)
        con.execute(f"DELETE FROM content_reports WHERE target_type='comment' AND target_id IN ({placeholders})", comment_ids)
    if email:
        con.execute("DELETE FROM email_login_sessions WHERE email=? OR user_id=?", (email, int(user_id)))
    else:
        con.execute("DELETE FROM email_login_sessions WHERE user_id=?", (int(user_id),))
    con.execute("DELETE FROM support_requests WHERE requester_user_id=? OR email=?", (int(user_id), email))
    con.execute("DELETE FROM wechat_sessions WHERE user_id=?", (int(user_id),))
    con.execute("DELETE FROM users WHERE id=?", (int(user_id),))
    con.commit()
    return summary


def performance_post_draft(con: sqlite3.Connection, user_id: int, profile_url: str = "") -> dict[str, str]:
    """Build an editable forum-post draft from a user's paper-trading record."""
    user = get_user(con, user_id)
    if user is None:
        raise ValueError("用户不存在")
    snap = portfolio_snapshot(con, user_id)
    rank = rank_for_user(con, user_id)
    holdings = snap["holdings"]
    orders = recent_orders(con, user_id, limit=5)
    signals = practice_signals(con, user_id, limit=5)

    rank_text = f"#{rank}" if rank else "未参赛"
    holding_lines = [
        (
            f"- {r['code']} {r['name']}: 持仓 {r['qty']} 股, 可卖 {r['available_qty']} 股, 成本 {_money(r['avg_price'])}, "
            f"现价 {_money(r['price'])}, 市值 {_money(r['market_value'])}, 盈亏 {_money(r['pnl'])}"
        )
        for r in holdings
    ] or ["- 暂无持仓"]
    order_lines = [
        f"- {o['created_at']} {o['code']} {'买入' if o['side'] == 'buy' else '卖出'} {o['qty']} 股 @ {_money(o['price'])}, 费用 {_money(o['fee'])}"
        for o in orders
    ] or ["- 暂无成交"]
    signal_lines = [
        f"- {s['strategy_name']}: {s['code']} {'买入' if s['side'] == 'buy' else '卖出'} {s['qty']} 股, 状态 {s['status']}, 依据: {s['rationale'] or '未填写'}"
        for s in signals
    ] or ["- 暂无演练计划"]

    profile = f"\n个人战绩页: {profile_url}\n" if profile_url else ""
    body = "\n".join(
        [
            f"当前模拟盘战绩: 总资产 {_money(snap['equity'])}, 收益率 {_pct(snap['return_pct'])}, 公开赛排名 {rank_text}。",
            f"现金 {_money(snap['cash'])}, 持仓市值 {_money(snap['market_value'])}。",
            profile,
            "持仓:",
            *holding_lines,
            "",
            "最近成交:",
            *order_lines,
            "",
            "策略演练计划:",
            *signal_lines,
            "",
            "复盘:",
            "1. 策略假设:",
            "2. 风险控制:",
            "3. 下一步观察:",
        ]
    )
    title = f"模拟盘战绩复盘: {_pct(snap['return_pct'])} / 排名 {rank_text}"
    return {"title": title, "tag": "performance", "body": body}


def create_post(
    con: sqlite3.Connection,
    user_id: int,
    title: str,
    body: str,
    tag: str,
    attach_snapshot: bool = True,
) -> int:
    ensure_user_active(get_user(con, user_id))
    title = title.strip()
    body = body.strip()
    tag = (tag or "general").strip()[:32] or "general"
    if not title or not body:
        raise ValueError("标题和内容不能为空")
    equity = ret = rank = None
    if attach_snapshot:
        snap = portfolio_snapshot(con, user_id)
        equity = float(snap["equity"])
        ret = float(snap["return_pct"])
        rank = rank_for_user(con, user_id)
    cur = con.execute(
        """
        INSERT INTO forum_posts(
            user_id, title, body, strategy_tag, snapshot_equity, snapshot_return_pct, snapshot_rank
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, title, body, tag, equity, ret, rank),
    )
    con.commit()
    return int(cur.lastrowid)


def forum_posts(
    con: sqlite3.Connection,
    tag: str = "",
    q: str = "",
    sort: str = "latest",
    limit: int = 100,
):
    tag = (tag or "").strip()
    q = (q or "").strip()
    sort = (sort or "latest").strip().lower()
    order_by = {
        "latest": "p.id DESC",
        "performance": "p.snapshot_return_pct IS NULL, p.snapshot_return_pct DESC, p.id DESC",
        "comments": "comments DESC, p.id DESC",
    }.get(sort, "p.id DESC")
    where = []
    params: list = []
    if tag:
        where.append("p.strategy_tag=?")
        params.append(tag)
    if q:
        where.append("(p.title LIKE ? OR p.body LIKE ? OR u.nickname LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    return con.execute(
        f"""
        SELECT p.*, u.nickname,
               (SELECT COUNT(*) FROM forum_comments c WHERE c.post_id=p.id) AS comments
        FROM forum_posts p
        JOIN users u ON u.id=p.user_id
        {where_sql}
        ORDER BY {order_by}
        LIMIT ?
        """,
        params,
    ).fetchall()


def forum_tags(con: sqlite3.Connection):
    return con.execute(
        """
        SELECT strategy_tag AS tag, COUNT(*) AS count
        FROM forum_posts
        GROUP BY strategy_tag
        ORDER BY count DESC, strategy_tag
        """
    ).fetchall()


def get_post(con: sqlite3.Connection, post_id: int):
    return con.execute(
        """
        SELECT p.*, u.nickname
        FROM forum_posts p
        JOIN users u ON u.id=p.user_id
        WHERE p.id=?
        """,
        (post_id,),
    ).fetchone()


def delete_post(con: sqlite3.Connection, actor_user_id: int, post_id: int) -> None:
    post = get_post(con, post_id)
    if post is None:
        raise ValueError("帖子不存在")
    actor = get_user(con, actor_user_id)
    if actor is None:
        raise ValueError("用户不存在")
    if int(post["user_id"]) != int(actor_user_id) and not is_admin(con, actor):
        raise ValueError("无权删除帖子")
    con.execute("DELETE FROM forum_posts WHERE id=?", (int(post_id),))
    con.commit()


def post_comments(con: sqlite3.Connection, post_id: int):
    return con.execute(
        """
        SELECT c.*, u.nickname
        FROM forum_comments c
        JOIN users u ON u.id=c.user_id
        WHERE c.post_id=?
        ORDER BY c.id
        """,
        (post_id,),
    ).fetchall()


def get_comment(con: sqlite3.Connection, comment_id: int):
    return con.execute("SELECT * FROM forum_comments WHERE id=?", (int(comment_id),)).fetchone()


def add_comment(con: sqlite3.Connection, user_id: int, post_id: int, body: str) -> int:
    ensure_user_active(get_user(con, user_id))
    body = body.strip()
    if not body:
        raise ValueError("评论不能为空")
    if get_post(con, post_id) is None:
        raise ValueError("帖子不存在")
    cur = con.execute(
        "INSERT INTO forum_comments(post_id, user_id, body) VALUES (?, ?, ?)",
        (post_id, user_id, body),
    )
    con.commit()
    return int(cur.lastrowid)


def delete_comment(con: sqlite3.Connection, actor_user_id: int, comment_id: int) -> int:
    comment = get_comment(con, comment_id)
    if comment is None:
        raise ValueError("评论不存在")
    actor = get_user(con, actor_user_id)
    if actor is None:
        raise ValueError("用户不存在")
    if int(comment["user_id"]) != int(actor_user_id) and not is_admin(con, actor):
        raise ValueError("无权删除评论")
    post_id = int(comment["post_id"])
    con.execute("DELETE FROM forum_comments WHERE id=?", (int(comment_id),))
    con.commit()
    return post_id


def create_content_report(
    con: sqlite3.Connection,
    reporter_user_id: int,
    target_type: str,
    target_id: int,
    reason: str,
) -> int:
    ensure_user_active(get_user(con, reporter_user_id))
    target_type = (target_type or "").strip().lower()
    target_id = int(target_id)
    reason = (reason or "").strip()
    if target_type not in {"post", "comment"}:
        raise ValueError("举报对象类型无效")
    if not reason:
        raise ValueError("举报原因不能为空")
    if get_user(con, reporter_user_id) is None:
        raise ValueError("用户不存在")
    if target_type == "post" and get_post(con, target_id) is None:
        raise ValueError("帖子不存在")
    if target_type == "comment" and get_comment(con, target_id) is None:
        raise ValueError("评论不存在")
    existing = con.execute(
        """
        SELECT id
        FROM content_reports
        WHERE reporter_user_id=? AND target_type=? AND target_id=? AND status='pending'
        """,
        (reporter_user_id, target_type, target_id),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = con.execute(
        """
        INSERT INTO content_reports(reporter_user_id, target_type, target_id, reason)
        VALUES (?, ?, ?, ?)
        """,
        (reporter_user_id, target_type, target_id, reason[:1000]),
    )
    con.commit()
    return int(cur.lastrowid)


def content_reports(con: sqlite3.Connection, status: str = "", limit: int = 100):
    status = (status or "").strip().lower()
    where = ""
    params: list = []
    if status:
        where = "WHERE r.status=?"
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    return con.execute(
        f"""
        SELECT r.*,
               reporter.nickname AS reporter_nickname,
               resolver.nickname AS resolver_nickname,
               p.title AS post_title,
               c.body AS comment_body,
               c.post_id AS comment_post_id,
               cp.title AS comment_post_title
        FROM content_reports r
        JOIN users reporter ON reporter.id=r.reporter_user_id
        LEFT JOIN users resolver ON resolver.id=r.resolver_user_id
        LEFT JOIN forum_posts p ON r.target_type='post' AND p.id=r.target_id
        LEFT JOIN forum_comments c ON r.target_type='comment' AND c.id=r.target_id
        LEFT JOIN forum_posts cp ON cp.id=c.post_id
        {where}
        ORDER BY r.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def resolve_content_report(
    con: sqlite3.Connection,
    resolver_user_id: int,
    report_id: int,
    status: str,
    note: str = "",
) -> None:
    resolver = get_user(con, resolver_user_id)
    if resolver is None:
        raise ValueError("用户不存在")
    if not is_admin(con, resolver):
        raise ValueError("无权处理举报")
    status = (status or "").strip().lower()
    if status not in {"resolved", "dismissed"}:
        raise ValueError("处理状态必须是 resolved 或 dismissed")
    row = con.execute("SELECT * FROM content_reports WHERE id=?", (int(report_id),)).fetchone()
    if row is None:
        raise ValueError("举报不存在")
    con.execute(
        """
        UPDATE content_reports
        SET status=?, resolver_user_id=?, resolution_note=?, resolved_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, resolver_user_id, (note or "").strip()[:1000], int(report_id)),
    )
    con.commit()


SUPPORT_CATEGORIES = {"registration", "account", "data", "community", "business", "other"}


def normalize_support_category(category: str) -> str:
    value = (category or "").strip().lower()
    return value if value in SUPPORT_CATEGORIES else "other"


def enforce_support_request_limit(con: sqlite3.Connection, email: str) -> None:
    normalized = normalize_email(email)
    open_count = int(
        con.execute(
            "SELECT COUNT(*) FROM support_requests WHERE email=? AND status='open'",
            (normalized,),
        ).fetchone()[0]
    )
    if open_count >= SUPPORT_REQUEST_OPEN_LIMIT:
        raise RateLimitExceeded("该邮箱仍有较多未处理支持请求,请等待管理员处理。")
    recent = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM support_requests
            WHERE email=? AND created_at >= datetime('now', ?)
            """,
            (normalized, f"-{SUPPORT_REQUEST_COOLDOWN_SECONDS} seconds"),
        ).fetchone()[0]
    )
    if recent > 0:
        raise RateLimitExceeded("支持请求提交过于频繁,请稍后再试。")
    hourly = int(
        con.execute(
            """
            SELECT COUNT(*)
            FROM support_requests
            WHERE email=? AND created_at >= datetime('now', '-1 hour')
            """,
            (normalized,),
        ).fetchone()[0]
    )
    if hourly >= SUPPORT_REQUEST_HOURLY_LIMIT:
        raise RateLimitExceeded("该邮箱支持请求提交过多,请稍后再试。")


def create_support_request(
    con: sqlite3.Connection,
    email: str,
    subject: str,
    message: str,
    category: str = "other",
    requester_user_id: int | None = None,
    ip_address: str = "",
    user_agent: str = "",
) -> int:
    normalized_email = normalize_email(email)
    enforce_support_request_limit(con, normalized_email)
    clean_subject = (subject or "").strip()
    clean_message = (message or "").strip()
    if len(clean_subject) < 3:
        raise ValueError("主题至少需要 3 个字符")
    if len(clean_subject) > 120:
        raise ValueError("主题不能超过 120 个字符")
    if len(clean_message) < 10:
        raise ValueError("问题描述至少需要 10 个字符")
    if len(clean_message) > 3000:
        raise ValueError("问题描述不能超过 3000 个字符")
    requester_id = int(requester_user_id) if requester_user_id else None
    if requester_id is not None and get_user(con, requester_id) is None:
        raise ValueError("用户不存在")
    cur = con.execute(
        """
        INSERT INTO support_requests(
            requester_user_id, email, category, subject, message, ip_address, user_agent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            requester_id,
            normalized_email,
            normalize_support_category(category),
            clean_subject[:120],
            clean_message[:3000],
            (ip_address or "").strip()[:120],
            (user_agent or "").strip()[:300],
        ),
    )
    con.commit()
    return int(cur.lastrowid)


def support_requests(con: sqlite3.Connection, status: str = "", limit: int = 100):
    status = (status or "").strip().lower()
    where = ""
    params: list = []
    if status:
        where = "WHERE s.status=?"
        params.append(status)
    params.append(max(1, min(int(limit), 500)))
    return con.execute(
        f"""
        SELECT s.*,
               requester.nickname AS requester_nickname,
               handler.nickname AS handler_nickname
        FROM support_requests s
        LEFT JOIN users requester ON requester.id=s.requester_user_id
        LEFT JOIN users handler ON handler.id=s.handler_user_id
        {where}
        ORDER BY s.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _queue_age_summary(con: sqlite3.Connection, table: str, status_col: str, status_value: str) -> dict:
    row = con.execute(
        f"""
        SELECT COUNT(*) AS count,
               MIN(created_at) AS oldest_at,
               MAX((julianday('now') - julianday(created_at)) * 24.0) AS oldest_age_hours
        FROM {table}
        WHERE {status_col}=?
        """,
        (status_value,),
    ).fetchone()
    count = int(row["count"] or 0)
    oldest_age = row["oldest_age_hours"] if count else None
    return {
        "count": count,
        "oldest_at": row["oldest_at"] or "",
        "oldest_age_hours": float(oldest_age) if oldest_age is not None else 0.0,
    }


def operational_queue_summary(con: sqlite3.Connection) -> dict:
    return {
        "support_open": _queue_age_summary(con, "support_requests", "status", "open"),
        "content_reports_pending": _queue_age_summary(con, "content_reports", "status", "pending"),
    }


def resolve_support_request(
    con: sqlite3.Connection,
    handler_user_id: int,
    request_id: int,
    status: str,
    note: str = "",
) -> None:
    handler = get_user(con, handler_user_id)
    if handler is None:
        raise ValueError("用户不存在")
    if not is_admin(con, handler):
        raise ValueError("无权处理支持请求")
    status = (status or "").strip().lower()
    if status not in {"resolved", "dismissed"}:
        raise ValueError("处理状态必须是 resolved 或 dismissed")
    row = con.execute("SELECT * FROM support_requests WHERE id=?", (int(request_id),)).fetchone()
    if row is None:
        raise ValueError("支持请求不存在")
    con.execute(
        """
        UPDATE support_requests
        SET status=?, handler_user_id=?, resolution_note=?, resolved_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (status, int(handler_user_id), (note or "").strip()[:1000], int(request_id)),
    )
    con.commit()

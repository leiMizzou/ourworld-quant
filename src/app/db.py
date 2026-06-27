"""SQLite persistence for the local paper-trading community app."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = Path(os.getenv("OWQ_APP_DB", REPO_ROOT / "data" / "app.sqlite"))
DEFAULT_BACKUP_DIR = Path(os.getenv("OWQ_APP_BACKUP_DIR", REPO_ROOT / "data" / "backups"))
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_BACKUP_KEEP = 30
CORE_BACKUP_TABLES = (
    "users",
    "accounts",
    "holdings",
    "orders",
    "learning_tasks",
    "practice_signals",
    "equity_snapshots",
    "market_prices",
    "contests",
    "contest_participants",
    "forum_posts",
    "forum_comments",
    "email_login_sessions",
    "audit_events",
    "content_reports",
    "support_requests",
    "user_consents",
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT NOT NULL,
    wechat_openid TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    login_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    password_updated_at TEXT NOT NULL DEFAULT '',
    avatar_url TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    status_reason TEXT NOT NULL DEFAULT '',
    status_updated_at TEXT NOT NULL DEFAULT '',
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    initial_cash REAL NOT NULL DEFAULT 1000000.0,
    cash REAL NOT NULL DEFAULT 1000000.0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    qty INTEGER NOT NULL,
    available_qty INTEGER NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL,
    PRIMARY KEY (account_id, code)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS learning_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'beginner',
    template TEXT NOT NULL DEFAULT 'reversal',
    coach_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signals_saved', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS practice_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'executed', 'cancelled')),
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    learning_task_id INTEGER REFERENCES learning_tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learning_tasks_user_time ON learning_tasks(user_id, created_at);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    equity REAL NOT NULL,
    return_pct REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_prices (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price REAL NOT NULL,
    prev_close REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'demo',
    as_of TEXT DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    start_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    end_at TEXT DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contest_participants (
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (contest_id, user_id)
);

CREATE TABLE IF NOT EXISTS forum_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    strategy_tag TEXT NOT NULL DEFAULT 'general',
    snapshot_equity REAL,
    snapshot_return_pct REAL,
    snapshot_rank INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS forum_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES forum_posts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wechat_sessions (
    token TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'expired')),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    accepted_terms_version TEXT NOT NULL DEFAULT '',
    accepted_privacy_version TEXT NOT NULL DEFAULT '',
    accepted_risk_version TEXT NOT NULL DEFAULT '',
    accepted_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS email_login_sessions (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL DEFAULT '',
    code_attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'expired')),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    accepted_terms_version TEXT NOT NULL,
    accepted_privacy_version TEXT NOT NULL,
    accepted_risk_version TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_email_login_sessions_email ON email_login_sessions(email, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}',
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor ON audit_events(actor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS content_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK (target_type IN ('post', 'comment')),
    target_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'dismissed')),
    resolver_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolution_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_content_reports_status ON content_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_content_reports_target ON content_reports(target_type, target_id);

CREATE TABLE IF NOT EXISTS support_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    email TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'other',
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    handler_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    resolution_note TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_support_requests_status ON support_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_requests_email ON support_requests(email, created_at DESC);

CREATE TABLE IF NOT EXISTS user_consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    terms_version TEXT NOT NULL,
    privacy_version TEXT NOT NULL,
    risk_version TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_consents_user ON user_consents(user_id, created_at DESC);
"""


DEMO_MARKET = [
    ("000001.SZ", "平安银行", 10.82, 10.74),
    ("600519.SH", "贵州茅台", 1448.00, 1432.10),
    ("300750.SZ", "宁德时代", 198.35, 196.20),
    ("510300.SH", "沪深300ETF", 3.73, 3.70),
    ("159915.SZ", "创业板ETF", 1.85, 1.82),
    ("588000.SH", "科创50ETF", 0.93, 0.92),
]


def sqlite_busy_timeout_ms() -> int:
    raw = os.getenv("OWQ_SQLITE_BUSY_TIMEOUT_MS", "").strip()
    if not raw:
        return DEFAULT_BUSY_TIMEOUT_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BUSY_TIMEOUT_MS
    return max(1000, min(value, 60000))


def configure_connection(con: sqlite3.Connection, db_path: Path) -> sqlite3.Connection:
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute(f"PRAGMA busy_timeout = {sqlite_busy_timeout_ms()}")
    if str(db_path) != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    return con


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else DEFAULT_DB_PATH
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    return configure_connection(con, db_path)


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    migrate(con)
    con.commit()


def migrate(con: sqlite3.Connection) -> None:
    """Apply lightweight additive migrations for local SQLite databases."""
    user_cols = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_url" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT DEFAULT ''")
    if "email" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "login_name" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN login_name TEXT NOT NULL DEFAULT ''")
    if "password_hash" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if "password_updated_at" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN password_updated_at TEXT NOT NULL DEFAULT ''")
    if "status" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "status_reason" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN status_reason TEXT NOT NULL DEFAULT ''")
    if "status_updated_at" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN status_updated_at TEXT NOT NULL DEFAULT ''")
    if "session_version" not in user_cols:
        con.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email) WHERE email <> ''")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_name_unique ON users(login_name) WHERE login_name <> ''")

    cols = {row["name"] for row in con.execute("PRAGMA table_info(forum_posts)").fetchall()}
    additions = {
        "snapshot_equity": "ALTER TABLE forum_posts ADD COLUMN snapshot_equity REAL",
        "snapshot_return_pct": "ALTER TABLE forum_posts ADD COLUMN snapshot_return_pct REAL",
        "snapshot_rank": "ALTER TABLE forum_posts ADD COLUMN snapshot_rank INTEGER",
    }
    for name, sql in additions.items():
        if name not in cols:
            con.execute(sql)
    market_cols = {row["name"] for row in con.execute("PRAGMA table_info(market_prices)").fetchall()}
    market_additions = {
        "source": "ALTER TABLE market_prices ADD COLUMN source TEXT NOT NULL DEFAULT 'demo'",
        "as_of": "ALTER TABLE market_prices ADD COLUMN as_of TEXT DEFAULT ''",
    }
    for name, sql in market_additions.items():
        if name not in market_cols:
            con.execute(sql)
    holding_cols = {row["name"] for row in con.execute("PRAGMA table_info(holdings)").fetchall()}
    if "available_qty" not in holding_cols:
        con.execute("ALTER TABLE holdings ADD COLUMN available_qty INTEGER NOT NULL DEFAULT 0")
        con.execute("UPDATE holdings SET available_qty=qty")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal TEXT NOT NULL,
            difficulty TEXT NOT NULL DEFAULT 'beginner',
            template TEXT NOT NULL DEFAULT 'reversal',
            coach_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'signals_saved', 'archived')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_learning_tasks_user_time ON learning_tasks(user_id, created_at);
        """
    )
    signal_cols = {row["name"] for row in con.execute("PRAGMA table_info(practice_signals)").fetchall()}
    if "learning_task_id" not in signal_cols:
        con.execute("ALTER TABLE practice_signals ADD COLUMN learning_task_id INTEGER REFERENCES learning_tasks(id) ON DELETE SET NULL")
    session_cols = {row["name"] for row in con.execute("PRAGMA table_info(wechat_sessions)").fetchall()}
    session_additions = {
        "accepted_terms_version": "ALTER TABLE wechat_sessions ADD COLUMN accepted_terms_version TEXT NOT NULL DEFAULT ''",
        "accepted_privacy_version": "ALTER TABLE wechat_sessions ADD COLUMN accepted_privacy_version TEXT NOT NULL DEFAULT ''",
        "accepted_risk_version": "ALTER TABLE wechat_sessions ADD COLUMN accepted_risk_version TEXT NOT NULL DEFAULT ''",
        "accepted_at": "ALTER TABLE wechat_sessions ADD COLUMN accepted_at TEXT NOT NULL DEFAULT ''",
    }
    for name, sql in session_additions.items():
        if name not in session_cols:
            con.execute(sql)

    email_session_cols = {row["name"] for row in con.execute("PRAGMA table_info(email_login_sessions)").fetchall()}
    email_session_additions = {
        "code_hash": "ALTER TABLE email_login_sessions ADD COLUMN code_hash TEXT NOT NULL DEFAULT ''",
        "code_attempts": "ALTER TABLE email_login_sessions ADD COLUMN code_attempts INTEGER NOT NULL DEFAULT 0",
    }
    for name, sql in email_session_additions.items():
        if name not in email_session_cols:
            con.execute(sql)

    # AI co-pilot tables (additive; safe on existing prod DBs). The user's API key is
    # stored only as AES-GCM ciphertext+nonce — never plaintext, never in audit/export.
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_user_keys (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            ciphertext BLOB NOT NULL,
            nonce BLOB NOT NULL,
            key_version INTEGER NOT NULL DEFAULT 1,
            base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
            model TEXT NOT NULL DEFAULT 'deepseek-chat',
            masked_hint TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT '',
            daily_token_cap INTEGER NOT NULL DEFAULT 200000,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_validated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_kind TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ok',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ai_usage_user_time ON ai_usage(user_id, created_at);
        CREATE TABLE IF NOT EXISTS ai_interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_kind TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            raw_response TEXT NOT NULL DEFAULT '',
            filtered_response TEXT NOT NULL DEFAULT '',
            blocked INTEGER NOT NULL DEFAULT 0,
            reasons TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_ai_interactions_user_time ON ai_interactions(user_id, created_at);
        """
    )


def seed_demo_market(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT COUNT(*) FROM market_prices").fetchone()
    if row and int(row[0]) > 0:
        con.execute(
            """
            INSERT INTO contests(title, description, is_active)
            SELECT ?, ?, 1
            WHERE NOT EXISTS (SELECT 1 FROM contests WHERE is_active=1)
            """,
            ("OurWorlds 模拟盘公开赛", "用统一 100 万模拟本金展示策略表现、交流复盘。"),
        )
        con.commit()
        return
    con.executemany(
        """
        INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
        VALUES (?, ?, ?, ?, 'demo', date('now'))
        ON CONFLICT(code) DO NOTHING
        """,
        DEMO_MARKET,
    )
    con.execute(
        """
        INSERT INTO contests(title, description, is_active)
        SELECT ?, ?, 1
        WHERE NOT EXISTS (SELECT 1 FROM contests WHERE is_active=1)
        """,
        ("OurWorlds 模拟盘公开赛", "用统一 100 万模拟本金展示策略表现、交流复盘。"),
    )
    con.commit()


def bootstrap(path: str | Path | None = None) -> sqlite3.Connection:
    con = connect(path)
    init_db(con)
    seed_demo_market(con)
    return con


def backup_keep_count() -> int:
    raw = os.getenv("OWQ_APP_BACKUP_KEEP", "").strip()
    if not raw:
        return DEFAULT_BACKUP_KEEP
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BACKUP_KEEP
    return max(1, min(value, 1000))


def prune_backup_dir(backup_dir: str | Path | None = None, keep: int | None = None) -> list[Path]:
    """Delete old automatic app backups, keeping the newest N files."""
    directory = Path(backup_dir or DEFAULT_BACKUP_DIR)
    if not directory.exists():
        return []
    keep_count = backup_keep_count() if keep is None else max(1, int(keep))
    backups = sorted(
        directory.glob("app-*.sqlite"),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    )
    removed: list[Path] = []
    for path in backups[keep_count:]:
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
    return removed


def backup_database(con: sqlite3.Connection, dest: str | Path | None = None) -> Path:
    """Create a consistent SQLite backup of the running app database."""
    auto_dest = dest is None
    if dest is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_path = DEFAULT_BACKUP_DIR / f"app-{stamp}.sqlite"
    else:
        dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(dest_path)
    try:
        con.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"备份校验失败: {result[0] if result else 'no result'}")
    finally:
        target.close()
    if auto_dest:
        prune_backup_dir(dest_path.parent)
    return dest_path


def verify_backup_file(path: str | Path) -> dict:
    """Open an app backup read-only and verify it can support a restore."""
    backup_path = Path(path)
    if not backup_path.exists():
        raise FileNotFoundError(f"备份文件不存在: {backup_path}")
    stat = backup_path.stat()
    if stat.st_size <= 0:
        raise sqlite3.DatabaseError(f"备份文件为空: {backup_path}")
    con = sqlite3.connect(f"file:{backup_path.resolve()}?mode=ro", uri=True)
    try:
        quick_check = con.execute("PRAGMA quick_check").fetchone()
        quick_detail = quick_check[0] if quick_check else "no result"
        if quick_detail != "ok":
            raise sqlite3.DatabaseError(f"备份 quick_check 失败: {quick_detail}")

        existing_tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = [name for name in CORE_BACKUP_TABLES if name not in existing_tables]
        if missing:
            raise sqlite3.DatabaseError("备份缺少核心表: " + ", ".join(missing))

        fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise sqlite3.DatabaseError(f"备份外键检查失败: {len(fk_errors)} 条")

        row_counts = {
            name: int(con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in CORE_BACKUP_TABLES
        }
    finally:
        con.close()
    return {
        "path": str(backup_path),
        "size_bytes": int(stat.st_size),
        "quick_check": quick_detail,
        "table_count": len(existing_tables),
        "row_counts": row_counts,
    }


def sqlite_sidecar_paths(path: Path) -> tuple[Path, Path]:
    text = str(path)
    return Path(text + "-wal"), Path(text + "-shm")


def sqlite_wal_size_bytes(path: str | Path | None) -> int:
    if not path:
        return 0
    db_path = Path(path)
    if str(db_path) == ":memory:":
        return 0
    wal_path = Path(str(db_path) + "-wal")
    try:
        return wal_path.stat().st_size
    except FileNotFoundError:
        return 0


def sqlite_maintenance(con: sqlite3.Connection) -> dict:
    """Run lightweight SQLite maintenance for the app database."""
    db_row = con.execute("PRAGMA database_list").fetchone()
    db_path = db_row[2] if db_row and len(db_row) > 2 else ""
    wal_before = sqlite_wal_size_bytes(db_path)
    optimize_rows = con.execute("PRAGMA optimize").fetchall()
    checkpoint = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    wal_after = sqlite_wal_size_bytes(db_path)
    checkpoint_values = tuple(int(value) for value in checkpoint) if checkpoint else ()
    return {
        "db_path": str(db_path or ":memory:"),
        "wal_before_bytes": int(wal_before),
        "wal_after_bytes": int(wal_after),
        "checkpoint": checkpoint_values,
        "optimize_rows": len(optimize_rows),
    }


def restore_backup_file(source: str | Path, dest: str | Path, overwrite: bool = False) -> dict:
    """Restore a verified backup into a target SQLite file without mutating the source."""
    source_path = Path(source)
    dest_path = Path(dest)
    if dest_path.exists() and not overwrite:
        raise FileExistsError(f"目标数据库已存在: {dest_path}")
    verify_backup_file(source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(f"{dest_path.name}.restore-{int(time.time() * 1000)}.tmp")
    for sidecar in sqlite_sidecar_paths(tmp_path):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
    source_con = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    target_con = sqlite3.connect(tmp_path)
    try:
        source_con.backup(target_con)
    finally:
        target_con.close()
        source_con.close()
    try:
        result = verify_backup_file(tmp_path)
        if overwrite:
            for sidecar in sqlite_sidecar_paths(dest_path):
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
        os.replace(tmp_path, dest_path)
        result = verify_backup_file(dest_path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        for sidecar in sqlite_sidecar_paths(tmp_path):
            try:
                sidecar.unlink()
            except FileNotFoundError:
                pass
    return result

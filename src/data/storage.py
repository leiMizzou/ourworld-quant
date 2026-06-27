"""DuckDB 存储层。表:stock_basic、daily_bars(主键去重,可重复跑不重复)。"""
from __future__ import annotations

import duckdb
import pandas as pd

from . import config
from .utils import log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_basic (
    code VARCHAR PRIMARY KEY,
    name VARCHAR, list_date VARCHAR, delist_date VARCHAR,
    status VARCHAR, source VARCHAR
);
CREATE TABLE IF NOT EXISTS daily_bars (
    code VARCHAR, date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume DOUBLE, amount DOUBLE,
    adjust VARCHAR, source VARCHAR,
    PRIMARY KEY (code, date, adjust)
);
"""


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    config.ensure_dirs()
    return duckdb.connect(str(config.DB_PATH), read_only=read_only)


def init_db() -> None:
    with connect() as con:
        con.execute(_SCHEMA)
    log.info("DuckDB 已就绪: %s", config.DB_PATH)


def upsert_stock_basic(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    with connect() as con:
        con.execute(_SCHEMA)
        con.register("t", df)
        con.execute(
            "INSERT OR REPLACE INTO stock_basic "
            "SELECT code,name,list_date,delist_date,status,source FROM t"
        )
    return len(df)


def upsert_bars(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    with connect() as con:
        con.execute(_SCHEMA)
        con.register("t", df)
        con.execute(
            "INSERT OR REPLACE INTO daily_bars "
            "SELECT code,date,open,high,low,close,volume,amount,adjust,source FROM t"
        )
    return len(df)


def latest_date(code: str, adjust: str = "hfq"):
    """某只股票已落库的最新日期,用于增量更新。无则返回 None。"""
    if not config.DB_PATH.exists():
        return None
    with connect(read_only=True) as con:
        row = con.execute(
            """
            SELECT date
            FROM daily_bars
            WHERE code=? AND adjust=?
            ORDER BY date DESC
            LIMIT 1
            """,
            [code, adjust],
        ).fetchone()
    return row[0] if row else None


def load_bars(codes=None, start=None, end=None, adjust: str = "hfq") -> pd.DataFrame:
    """从库里读日线。codes 可为单个代码或列表。"""
    from .sources.base import BAR_COLUMNS
    if not config.DB_PATH.exists():
        return pd.DataFrame(columns=BAR_COLUMNS)
    q = "SELECT * FROM daily_bars WHERE adjust=?"
    params = [adjust]
    if codes:
        codes = [codes] if isinstance(codes, str) else list(codes)
        q += f" AND code IN ({','.join(['?'] * len(codes))})"
        params += codes
    if start:
        q += " AND date>=?"
        params.append(pd.to_datetime(start))
    if end:
        q += " AND date<=?"
        params.append(pd.to_datetime(end))
    q += " ORDER BY code, date"
    with connect(read_only=True) as con:
        return con.execute(q, params).df()


def table_counts() -> dict[str, int]:
    if not config.DB_PATH.exists():
        return {"stock_basic": 0, "daily_bars": 0}
    with connect(read_only=True) as con:
        try:
            sb = con.execute("SELECT count(*) FROM stock_basic").fetchone()[0]
            db = con.execute("SELECT count(*) FROM daily_bars").fetchone()[0]
        except duckdb.Error:
            sb = db = 0
    return {"stock_basic": sb, "daily_bars": db}

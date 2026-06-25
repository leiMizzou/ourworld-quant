"""Bridge market data from the research data layer into the paper-trading app."""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from typing import Iterable


class MarketSyncError(RuntimeError):
    """Raised when a market data sync cannot be completed."""


def upsert_market_rows(con: sqlite3.Connection, rows: Iterable[dict], source: str, replace: bool = False) -> int:
    payload = []
    for row in rows:
        code = str(row.get("code", "")).strip().upper()
        if not code:
            continue
        name = str(row.get("name") or code).strip()
        price = float(row["price"])
        prev_close = float(row.get("prev_close") or price)
        as_of = str(row.get("as_of") or row.get("date") or "")
        if price <= 0 or prev_close <= 0:
            continue
        payload.append((code, name, price, prev_close, source, as_of))
    if not payload:
        return 0
    if replace:
        con.execute("DELETE FROM market_prices")
    con.executemany(
        """
        INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            price=excluded.price,
            prev_close=excluded.prev_close,
            source=excluded.source,
            as_of=excluded.as_of,
            updated_at=CURRENT_TIMESTAMP
        """,
        payload,
    )
    con.commit()
    return len(payload)


def sync_market_from_csv(con: sqlite3.Connection, path: str | Path, source: str = "csv", replace: bool = False) -> int:
    """Load market prices from a CSV with code,name,price,prev_close[,as_of]."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise MarketSyncError(f"CSV 不存在: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    try:
        return upsert_market_rows(con, rows, source=source, replace=replace)
    except (KeyError, ValueError) as exc:
        raise MarketSyncError("CSV 字段需要包含 code,name,price,prev_close,as_of") from exc


def sync_market_from_csv_text(con: sqlite3.Connection, text: str, source: str = "csv_text", replace: bool = False) -> int:
    """Load market prices from pasted CSV text."""
    text = text.strip()
    if not text:
        raise MarketSyncError("CSV 内容为空")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise MarketSyncError("CSV 没有数据行")
    try:
        return upsert_market_rows(con, rows, source=source, replace=replace)
    except (KeyError, ValueError) as exc:
        raise MarketSyncError("CSV 字段需要包含 code,name,price,prev_close,as_of") from exc


def sync_market_from_quant_db(
    con: sqlite3.Connection,
    adjust: str = "none",
    limit: int = 500,
    replace: bool = False,
) -> int:
    """Sync latest close/previous close from src.data DuckDB daily_bars.

    This function is intentionally optional. If DuckDB or the local market database is
    unavailable, callers receive a clear MarketSyncError and can keep using demo data.
    """
    try:
        from src.data import config, storage
    except Exception as exc:  # noqa: BLE001
        raise MarketSyncError(f"数据层不可用,请先安装 data 依赖: {exc}") from exc

    if not config.DB_PATH.exists():
        raise MarketSyncError(f"行情库不存在: {config.DB_PATH}")

    query = """
    WITH latest AS (
        SELECT code, max(date) AS date
        FROM daily_bars
        WHERE adjust = ?
        GROUP BY code
    ),
    latest_bars AS (
        SELECT b.code, b.date, b.close AS price, b.source AS upstream_source
        FROM daily_bars b
        JOIN latest l ON l.code = b.code AND l.date = b.date
        WHERE b.adjust = ?
    ),
    prev_bars AS (
        SELECT p.code, max(p.date) AS prev_date
        FROM daily_bars p
        JOIN latest_bars lb ON lb.code = p.code
        WHERE p.adjust = ? AND p.date < lb.date
        GROUP BY p.code
    )
    SELECT lb.code,
           COALESCE(sb.name, lb.code) AS name,
           lb.date AS as_of,
           lb.price,
           COALESCE(pb.close, lb.price) AS prev_close,
           COALESCE(lb.upstream_source, 'unknown') AS upstream_source
    FROM latest_bars lb
    LEFT JOIN prev_bars pbd ON pbd.code = lb.code
    LEFT JOIN daily_bars pb ON pb.code = pbd.code AND pb.date = pbd.prev_date AND pb.adjust = ?
    LEFT JOIN stock_basic sb ON sb.code = lb.code
    WHERE lb.price > 0
    ORDER BY lb.code
    LIMIT ?
    """
    try:
        with storage.connect(read_only=True) as duck:
            df = duck.execute(query, [adjust, adjust, adjust, adjust, int(limit)]).df()
    except Exception as exc:  # noqa: BLE001
        raise MarketSyncError(f"读取行情库失败: {exc}") from exc
    if df.empty:
        raise MarketSyncError("行情库没有可同步的日线数据")
    rows = [
        {
            "code": r["code"],
            "name": r["name"],
            "price": r["price"],
            "prev_close": r["prev_close"],
            "as_of": str(r["as_of"]),
            "upstream_source": r["upstream_source"],
        }
        for _, r in df.iterrows()
    ]
    if replace:
        con.execute("DELETE FROM market_prices")
    count = 0
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        label = f"duckdb:{adjust}:{row.get('upstream_source') or 'unknown'}"
        by_source.setdefault(label, []).append(row)
    for label, source_rows in by_source.items():
        count += upsert_market_rows(con, source_rows, source=label)
    return count

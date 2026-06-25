"""编排:取股票列表 → 取日线(增量/重试/限流)→ 清洗 → 落库 DuckDB。"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from . import clean, config, storage
from .sources import get_source
from .utils import log


def sync_stock_list(source_name: str = "akshare") -> pd.DataFrame:
    """拉取并入库股票列表(含退市,缓解幸存者偏差)。"""
    storage.init_db()
    src = get_source(source_name)
    try:
        df = src.get_stock_list()
    finally:
        src.close()
    n = storage.upsert_stock_basic(df)
    by_status = df["status"].value_counts().to_dict() if not df.empty else {}
    log.info("股票列表[%s]:取得 %d,入库 %d,分布=%s", source_name, len(df), n, by_status)
    return df


def codes_from_db(limit: int | None = None, status: str | None = "L") -> list[str]:
    """从已入库的 stock_basic 取代码列表。"""
    if not config.DB_PATH.exists():
        return []
    q, params = "SELECT code FROM stock_basic", []
    if status:
        q += " WHERE status=?"
        params.append(status)
    q += " ORDER BY code"
    if limit:
        q += f" LIMIT {int(limit)}"
    with storage.connect(read_only=True) as con:
        try:
            rows = con.execute(q, params).fetchall()
        except Exception:  # noqa: BLE001 - 表可能还没建
            return []
    return [r[0] for r in rows]


def _progress(seq):
    try:
        from tqdm import tqdm
        return tqdm(list(seq), ncols=80)
    except Exception:  # noqa: BLE001
        return seq


def sync_daily(
    codes: Iterable[str],
    source_name: str = "akshare",
    start: str | None = None,
    adjust: str | None = None,
    incremental: bool = True,
    flush_rows: int = 20000,
) -> dict:
    """同步一批代码的日线。incremental=True 时只补每只股票库里最新日期之后的数据。"""
    start = start or config.DEFAULT_START
    adjust = adjust or config.DEFAULT_ADJUST
    storage.init_db()
    src = get_source(source_name)
    buf: list[pd.DataFrame] = []
    rows_in_buf = 0
    stats = {"ok": 0, "empty": 0, "fail": 0, "rows": 0}
    today = pd.Timestamp.today().strftime("%Y%m%d")
    try:
        for code in _progress(codes):
            s = start
            if incremental:
                last = storage.latest_date(code, adjust)
                if last is not None:
                    s = (pd.Timestamp(last) + pd.Timedelta(days=1)).strftime("%Y%m%d")
                    if s > today:  # 已是最新
                        stats["ok"] += 1
                        continue
            try:
                df = clean.standardize_bars(src.get_daily_bars(code, start=s, adjust=adjust))
                if df.empty:
                    stats["empty"] += 1
                else:
                    buf.append(df)
                    rows_in_buf += len(df)
                    stats["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["fail"] += 1
                log.warning("取 %s 失败: %s", code, exc)
            if rows_in_buf >= flush_rows:
                stats["rows"] += storage.upsert_bars(pd.concat(buf, ignore_index=True))
                buf, rows_in_buf = [], 0
        if buf:
            stats["rows"] += storage.upsert_bars(pd.concat(buf, ignore_index=True))
    finally:
        src.close()
    log.info("日线[%s/%s]完成:成功 %d 空 %d 失败 %d,入库 %d 行",
             source_name, adjust, stats["ok"], stats["empty"], stats["fail"], stats["rows"])
    return stats

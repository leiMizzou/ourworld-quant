"""编排:取股票列表 → 取日线(增量/重试/限流)→ 清洗 → 落库 DuckDB。"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path
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


def _stable_code_rank(code: str) -> str:
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def normalize_codes(codes: Iterable[str]) -> list[str]:
    seen = set()
    normalized = []
    for raw in codes:
        code = str(raw or "").strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)
    return normalized


def codes_from_csv(path: str | Path, column: str = "code") -> list[str]:
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"CSV 需要包含 {column} 字段")
        return normalize_codes(row.get(column, "") for row in reader)


def _universe_bucket(code: str, status: str) -> str:
    num, _, suffix = str(code).partition(".")
    if suffix == "SZ" and num.startswith(("300", "301")):
        board = "SZ_CHINEXT"
    elif suffix == "SZ":
        board = "SZ_MAIN"
    elif suffix == "SH" and num.startswith(("688", "689")):
        board = "SH_STAR"
    elif suffix == "SH":
        board = "SH_MAIN"
    elif suffix == "BJ":
        board = "BJ"
    else:
        board = suffix or "UNKNOWN"
    return f"{status or 'UNKNOWN'}:{board}"


def _representative_codes(rows: list[tuple[str, str]], limit: int | None = None) -> list[str]:
    """Deterministic stratified sample across status/exchange/board buckets."""
    if not rows:
        return []
    dedup = {}
    for code, status in rows:
        dedup[str(code)] = str(status or "")
    items = [(code, dedup[code]) for code in sorted(dedup)]
    if not limit or limit >= len(items):
        return normalize_codes(code for code, _ in sorted(items, key=lambda item: (_stable_code_rank(item[0]), item[0])))

    limit = max(1, int(limit))
    buckets: dict[str, list[str]] = {}
    for code, status_value in items:
        buckets.setdefault(_universe_bucket(code, status_value), []).append(code)
    for codes in buckets.values():
        codes.sort(key=lambda c: (_stable_code_rank(c), c))

    total = sum(len(codes) for codes in buckets.values())
    allocs: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for bucket, codes in buckets.items():
        exact = limit * len(codes) / total
        base = min(len(codes), int(exact))
        allocs[bucket] = base
        remainders.append((exact - base, bucket))

    remaining = limit - sum(allocs.values())
    for _, bucket in sorted(remainders, reverse=True):
        if remaining <= 0:
            break
        if allocs[bucket] < len(buckets[bucket]):
            allocs[bucket] += 1
            remaining -= 1

    statuses = sorted({status_value for _, status_value in items if status_value})
    if limit >= len(statuses):
        for status_value in statuses:
            status_buckets = [bucket for bucket in buckets if bucket.startswith(f"{status_value}:")]
            if not status_buckets or sum(allocs[bucket] for bucket in status_buckets) > 0:
                continue
            donor = max(
                (bucket for bucket in buckets if allocs[bucket] > 1 and not bucket.startswith(f"{status_value}:")),
                key=lambda bucket: allocs[bucket],
                default="",
            )
            if donor:
                target = max(status_buckets, key=lambda bucket: len(buckets[bucket]))
                allocs[donor] -= 1
                allocs[target] += 1

    selected: list[str] = []
    for bucket in sorted(buckets):
        selected.extend(buckets[bucket][: allocs[bucket]])
    if len(selected) < limit:
        chosen = set(selected)
        rest = [code for codes in buckets.values() for code in codes if code not in chosen]
        selected.extend(sorted(rest, key=lambda c: (_stable_code_rank(c), c))[: limit - len(selected)])
    return sorted(selected[:limit], key=lambda c: (_stable_code_rank(c), c))


def codes_from_db(
    limit: int | None = None,
    status: str | None = "L",
    universe_mode: str = "ordered",
) -> list[str]:
    """从已入库的 stock_basic 取代码列表。"""
    if not config.DB_PATH.exists():
        return []
    q, params = "SELECT code, status FROM stock_basic", []
    normalized_status = (status or "").strip().upper()
    if normalized_status in {"", "ALL", "*"}:
        normalized_status = ""
    if normalized_status:
        q += " WHERE status=?"
        params.append(normalized_status)
    q += " ORDER BY code"
    if limit:
        q += f" LIMIT {int(limit)}"
    with storage.connect(read_only=True) as con:
        try:
            rows = con.execute(q, params).fetchall()
        except Exception:  # noqa: BLE001 - 表可能还没建
            return []
    if universe_mode == "representative":
        if limit:
            q = "SELECT code, status FROM stock_basic"
            if normalized_status:
                q += " WHERE status=?"
            q += " ORDER BY code"
            with storage.connect(read_only=True) as con:
                rows = con.execute(q, params).fetchall()
        return _representative_codes([(r[0], r[1]) for r in rows], limit=limit)
    return normalize_codes(r[0] for r in rows)


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

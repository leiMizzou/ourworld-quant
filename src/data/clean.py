"""清洗与质检。回测可信度的第一道关:类型、排序、去重、停牌缺口、涨跌停标记。"""
from __future__ import annotations

import pandas as pd

from .sources.base import BAR_COLUMNS

_NUMERIC = ["open", "high", "low", "close", "volume", "amount"]


def standardize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """统一类型、排序、去重、剔除无效行(价格<=0 或缺失)。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in _NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]                     # 剔除异常价
    df = df.drop_duplicates(["code", "date", "adjust"], keep="last")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    return df[BAR_COLUMNS]


def add_flags(df: pd.DataFrame, limit_threshold: float = 0.095, susp_gap_days: int = 7) -> pd.DataFrame:
    """给【单只股票】的日线加派生标记(回测里很关键):
    - prev_close / ret:昨收与日收益
    - limit_hit:近似涨跌停(|ret|>=阈值)。注意:主板≈10%、创业板/科创板≈20%、ST≈5%,
      这里用统一阈值做*近似*标记,精确判定需结合板块与 ST 状态。
    - susp_gap:与上一根的自然日间隔过大(疑似停牌复牌)。
    """
    if df.empty:
        return df.assign(prev_close=pd.NA, ret=pd.NA, limit_hit=False, susp_gap=False)
    df = df.sort_values("date").copy()
    df["prev_close"] = df["close"].shift(1)
    df["ret"] = df["close"] / df["prev_close"] - 1
    df["limit_hit"] = df["ret"].abs() >= limit_threshold
    gap = df["date"].diff().dt.days
    df["susp_gap"] = gap > susp_gap_days
    return df


def quality_report(df: pd.DataFrame) -> dict:
    """快速体检:行数、日期范围、重复、缺失、非正价格、负收益异常等。"""
    if df is None or df.empty:
        return {"rows": 0}
    rep = {
        "rows": int(len(df)),
        "codes": int(df["code"].nunique()) if "code" in df else 1,
        "date_min": str(pd.to_datetime(df["date"]).min().date()),
        "date_max": str(pd.to_datetime(df["date"]).max().date()),
        "dup_rows": int(df.duplicated(["code", "date", "adjust"]).sum()) if "adjust" in df else int(df.duplicated(["code", "date"]).sum()),
        "null_close": int(df["close"].isna().sum()),
        "nonpos_close": int((pd.to_numeric(df["close"], errors="coerce") <= 0).sum()),
    }
    return rep

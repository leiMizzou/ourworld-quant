"""横截面排序策略:按过去 N 日收益排序,等权持有 top-N,定期轮动。

- signal='reversal':买过去 N 日**跌**得最多的(短期反转,A 股历史上较强)
- signal='momentum':买过去 N 日**涨**得最多的(动量)

权重在调仓日**收盘后**用截至当日的数据算出;引擎在**次日开盘**执行(T+1,无前视)。
"""
from __future__ import annotations

import pandas as pd


def rebalance_dates(dates, freq: str = "M") -> list[pd.Timestamp]:
    """每个周期(默认月)的最后一个交易日。"""
    idx = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(dates)))))
    if len(idx) == 0:
        return []
    s = pd.Series(idx, index=idx.to_period(freq))
    last = s.groupby(level=0).max()
    return list(pd.to_datetime(last.values))


def cross_sectional_weights(
    panel: pd.DataFrame,
    signal: str = "reversal",
    lookback: int = 20,
    top_n: int = 20,
    freq: str = "M",
) -> pd.DataFrame:
    """返回 index=调仓日, columns=code, 值=目标权重(等权 top-N)。"""
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    close = panel.pivot(index="date", columns="code", values="close").sort_index()

    rows: dict[pd.Timestamp, pd.Series] = {}
    for d in rebalance_dates(close.index, freq):
        hist = close.loc[:d]
        if len(hist) <= lookback:
            continue
        past_ret = hist.iloc[-1] / hist.iloc[-1 - lookback] - 1.0
        # 仅在当日有有效收盘价(在市、未停牌)的股票里选
        valid = close.loc[d].dropna().index
        past_ret = past_ret.reindex(valid).dropna()
        if past_ret.empty:
            continue
        ascending = signal == "reversal"   # 反转选最低收益,动量选最高
        picks = past_ret.sort_values(ascending=ascending).head(top_n).index
        if len(picks) == 0:
            continue
        rows[d] = pd.Series(1.0 / len(picks), index=picks)

    if not rows:
        return pd.DataFrame()
    w = pd.DataFrame(rows).T.fillna(0.0)
    w.index = pd.to_datetime(w.index)
    return w.sort_index()

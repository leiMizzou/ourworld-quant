"""单因子检验:前向收益对齐、IC 序列、ICIR/t 值、分层回测、多空组合。

无前视:因子在调仓日 d 用截至 d 的数据算出,前向收益是 d→下一个调仓日。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def forward_returns(close: pd.DataFrame, rebal_dates) -> pd.DataFrame:
    """每个调仓日到下一个调仓日的收益。index=rebal_dates[:-1], columns=code。"""
    rd = [d for d in pd.to_datetime(list(rebal_dates)) if d in close.index]
    rows = {d0: close.loc[d1] / close.loc[d0] - 1.0 for d0, d1 in zip(rd[:-1], rd[1:])}
    return pd.DataFrame(rows).T


def _avg_period_days(index) -> float:
    idx = pd.DatetimeIndex(sorted(index))
    if len(idx) < 2:
        return 21.0
    return float(np.median(np.diff(idx.values).astype("timedelta64[D]").astype(int)))


def ic_series(factor_s: pd.DataFrame, fwd_ret: pd.DataFrame, method: str = "spearman") -> pd.Series:
    """逐调仓日的截面 IC(默认 Spearman 秩相关 = RankIC)。"""
    common = factor_s.index.intersection(fwd_ret.index)
    ics = {}
    for d in common:
        df = pd.concat([factor_s.loc[d].rename("f"), fwd_ret.loc[d].rename("r")], axis=1).dropna()
        if len(df) >= 5:
            f, r = (df["f"].rank(), df["r"].rank()) if method == "spearman" else (df["f"], df["r"])
            ics[d] = f.corr(r)   # 秩相关 = 对排名做 Pearson,免 scipy 依赖
    return pd.Series(ics).sort_index()


def summarize_ic(ic: pd.Series) -> dict:
    ic = ic.dropna()
    n = len(ic)
    if n < 2:
        return {"n_periods": n}
    mean, std = ic.mean(), ic.std()
    icir = mean / std if std > 0 else np.nan
    return {
        "ic_mean": round(float(mean), 4),
        "ic_std": round(float(std), 4),
        "icir": round(float(icir), 3),
        "t_stat": round(float(icir * np.sqrt(n)), 2),
        "ic_pos_rate": round(float((ic > 0).mean()), 3),
        "n_periods": int(n),
    }


def quantile_returns(factor_s: pd.DataFrame, fwd_ret: pd.DataFrame, q: int = 5) -> pd.DataFrame:
    """每个调仓日按因子分 q 组,算各组前向收益均值。返回 date×group。"""
    common = factor_s.index.intersection(fwd_ret.index)
    rows = {}
    for d in common:
        df = pd.concat([factor_s.loc[d].rename("f"), fwd_ret.loc[d].rename("r")], axis=1).dropna()
        if len(df) < q * 2:
            continue
        try:
            df["g"] = pd.qcut(df["f"], q, labels=False, duplicates="drop")
        except ValueError:
            continue
        rows[d] = df.groupby("g")["r"].mean()
    return pd.DataFrame(rows).T.sort_index()


def evaluate_factor(close: pd.DataFrame, factor: pd.DataFrame, rebal_dates,
                    q: int = 5, method: str = "spearman") -> dict:
    """完整单因子检验,返回 {report, ic_series, quantile_returns}。"""
    fwd = forward_returns(close, rebal_dates)
    fs = factor.loc[factor.index.intersection(fwd.index)]
    ic = ic_series(fs, fwd, method)
    report = {"ic": summarize_ic(ic)}

    qr = quantile_returns(fs, fwd, q)
    if not qr.empty:
        qmean = qr.mean().sort_index()
        report["quantile_mean_return"] = {int(k): round(float(v), 4) for k, v in qmean.items()}
        ls = qr[qr.columns.max()] - qr[qr.columns.min()]      # 最高组 - 最低组
        periods_per_year = TRADING_DAYS / _avg_period_days(qr.index)
        report["long_short_mean"] = round(float(ls.mean()), 4)
        report["long_short_ann"] = round(float(ls.mean() * periods_per_year), 4)
        report["long_short_cum"] = round(float((1 + ls).prod() - 1.0), 4)
        # 单调性:分位顺序 与 各组平均收益 的秩相关(免 scipy)
        report["monotonicity"] = round(
            float(pd.Series(qmean.values).rank().corr(pd.Series(range(len(qmean))))), 3)
    return {"report": report, "ic_series": ic, "quantile_returns": qr}

"""绩效指标。输入逐日净值序列,输出年化/夏普/最大回撤等。"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def drawdown_series(equity: pd.Series) -> pd.Series:
    equity = equity.astype(float)
    return equity / equity.cummax() - 1.0


def compute_metrics(equity: pd.Series, turnover: float | None = None, rf: float = 0.0) -> dict:
    """equity:逐日净值(index 为日期)。turnover:年化换手(可选)。"""
    equity = equity.dropna().astype(float)
    if len(equity) < 2:
        return {"error": "净值序列过短"}
    rets = equity.pct_change().dropna()
    years = len(equity) / TRADING_DAYS
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0 if years > 0 else float("nan")
    ann_vol = rets.std() * np.sqrt(TRADING_DAYS)
    excess = rets.mean() * TRADING_DAYS - rf
    sharpe = excess / ann_vol if ann_vol > 0 else float("nan")
    max_dd = drawdown_series(equity).min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else float("nan")
    out = {
        "total_return": round(float(total_return), 4),
        "cagr": round(float(cagr), 4),
        "ann_vol": round(float(ann_vol), 4),
        "sharpe": round(float(sharpe), 3),
        "max_drawdown": round(float(max_dd), 4),
        "calmar": round(float(calmar), 3) if calmar == calmar else None,
        "win_rate": round(float((rets > 0).mean()), 3),
        "days": int(len(equity)),
    }
    if turnover is not None:
        out["annual_turnover"] = round(float(turnover), 2)
    return out

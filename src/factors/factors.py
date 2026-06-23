"""因子计算。输入宽表(index=date, columns=code),输出同形状的日频因子面板。

只用到 OHLCV 能算的因子(数据层已有);市值/财务类因子待数据层补充后扩展。
约定:因子值越大代表"越偏向买入"的方向由评估端用 IC 符号判断,不在此处强行规定。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def momentum(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """过去 window 日收益(动量)。"""
    return close / close.shift(window) - 1.0


def reversal(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """短期反转 = 负动量(A 股历史上较强)。"""
    return -(close / close.shift(window) - 1.0)


def volatility(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """过去 window 日日收益波动率(低波动常有正溢价 → IC 多为负)。"""
    return close.pct_change().rolling(window).std()


def amihud_illiq(close: pd.DataFrame, amount: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Amihud 非流动性 = 平均(|日收益| / 成交额)。越大越不流动。"""
    ret = close.pct_change().abs()
    illiq = ret / amount.replace(0, np.nan)
    return illiq.rolling(window).mean()


def ma_bias(close: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """均线乖离 = 收盘/均线 - 1。"""
    return close / close.rolling(window).mean() - 1.0


# 因子注册表:name -> (函数, 需要的面板)
REGISTRY = {
    "momentum": ("close",),
    "reversal": ("close",),
    "volatility": ("close",),
    "amihud": ("close", "amount"),
    "ma_bias": ("close",),
}

_FUNCS = {
    "momentum": momentum,
    "reversal": reversal,
    "volatility": volatility,
    "amihud": amihud_illiq,
    "ma_bias": ma_bias,
}


def compute(name: str, panels: dict[str, pd.DataFrame], window: int = 20) -> pd.DataFrame:
    """按名字计算因子。panels 至少含 'close',amihud 还需 'amount'。"""
    if name not in _FUNCS:
        raise ValueError(f"未知因子 {name!r},可选: {list(_FUNCS)}")
    needs = REGISTRY[name]
    args = [panels[p] for p in needs]
    return _FUNCS[name](*args, window=window)

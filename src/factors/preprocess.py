"""因子预处理:去极值、标准化、中性化。全部按【截面】(每个交易日一行)处理。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def winsorize_mad(row: pd.Series, k: float = 3.0) -> pd.Series:
    """MAD 去极值:中位数 ± k×(1.4826×MAD) 截断。比按分位更稳健。"""
    med = row.median()
    mad = (row - med).abs().median()
    if mad == 0 or np.isnan(mad):
        return row
    lo, hi = med - k * 1.4826 * mad, med + k * 1.4826 * mad
    return row.clip(lo, hi)


def zscore(row: pd.Series) -> pd.Series:
    """截面 z-score 标准化。"""
    sd = row.std()
    if sd == 0 or np.isnan(sd):
        return row * 0.0
    return (row - row.mean()) / sd


def standardize(factor: pd.DataFrame, k: float = 3.0) -> pd.DataFrame:
    """对每个交易日(每行)先 MAD 去极值再 z-score。"""
    return factor.apply(lambda r: zscore(winsorize_mad(r.dropna(), k)).reindex(r.index), axis=1)


def neutralize(factor: pd.DataFrame, exposures: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """对暴露(如市值对数、行业哑变量)做截面 OLS,取残差作为中性化后因子。

    exposures: {名称: 面板(date×code)}。行业可先做成 0/1 哑变量面板传入。
    无暴露数据时可跳过本步(数据层补充市值/行业后再用)。
    """
    if not exposures:
        return factor
    names = list(exposures)
    out = pd.DataFrame(index=factor.index, columns=factor.columns, dtype=float)
    for d in factor.index:
        y = factor.loc[d]
        X = pd.DataFrame({n: exposures[n].loc[d] for n in names if d in exposures[n].index})
        df = pd.concat([y.rename("y"), X], axis=1).dropna()
        if len(df) < len(names) + 2:
            out.loc[d] = y
            continue
        A = np.column_stack([np.ones(len(df)), df[names].values])
        beta, *_ = np.linalg.lstsq(A, df["y"].values, rcond=None)
        resid = df["y"].values - A @ beta
        out.loc[d, df.index] = resid
    return out

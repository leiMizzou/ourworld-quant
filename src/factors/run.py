"""单因子检验入口。从 DuckDB 读日线 → 算因子 → 预处理 → 评估 → 打印报告。

用法(仓库根目录,需先用 src.data 取数):
    python -m src.factors.run --factor reversal --window 20 --q 5 --start 20200101
    python -m src.factors.run --factor amihud --window 20
    python -m src.factors.run --factor momentum --raw      # 不做去极值/标准化
"""
from __future__ import annotations

import argparse

import pandas as pd

from ..backtest.strategies.cross_sectional import rebalance_dates
from ..data import storage
from . import factors as F
from .evaluate import evaluate_factor
from .preprocess import standardize


def load_panels(codes=None, start=None, adjust="hfq") -> dict:
    df = storage.load_bars(codes=codes, start=start, adjust=adjust)
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    return {
        "close": df.pivot(index="date", columns="code", values="close").sort_index(),
        "amount": df.pivot(index="date", columns="code", values="amount").sort_index(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("owq-factors", description="OurWorlds Quant Lab 单因子检验")
    p.add_argument("--factor", default="reversal", choices=list(F.REGISTRY))
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--q", type=int, default=5, help="分层组数")
    p.add_argument("--freq", default="M", help="调仓频率(M=月,W=周)")
    p.add_argument("--start", default="20180101")
    p.add_argument("--adjust", default="hfq", choices=["hfq", "qfq", "none"])
    p.add_argument("--codes", nargs="*")
    p.add_argument("--raw", action="store_true", help="跳过去极值/标准化")
    a = p.parse_args(argv)

    panels = load_panels(codes=a.codes, start=a.start, adjust=a.adjust)
    if not panels:
        print("⚠️ 库内无数据。先取数:python -m src.data.cli daily --limit 300")
        return 1
    close = panels["close"]
    print(f"载入 {close.shape[1]} 只 / {close.index.min().date()}~{close.index.max().date()}")

    fac = F.compute(a.factor, panels, window=a.window)
    if not a.raw:
        fac = standardize(fac)
    rebal = rebalance_dates(close.index, a.freq)
    out = evaluate_factor(close, fac, rebal, q=a.q)
    rep = out["report"]

    print(f"\n=== 因子检验: {a.factor} (window={a.window}, q={a.q}, freq={a.freq}) ===")
    print("IC:")
    for k, v in rep["ic"].items():
        print(f"  {k:>12}: {v}")
    if "quantile_mean_return" in rep:
        print("分层平均收益(低→高分位):")
        print("  " + "  ".join(f"Q{k}:{v:+.4f}" for k, v in rep["quantile_mean_return"].items()))
        print(f"  多空(高-低)单期均值: {rep['long_short_mean']:+.4f} | 年化: {rep['long_short_ann']:+.4f} "
              f"| 累计: {rep['long_short_cum']:+.4f} | 单调性: {rep['monotonicity']}")
    print("\n判读:|t|>2 且 IC 方向稳定、分层单调,才算有效因子。务必再做样本外验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""回测运行入口。从 DuckDB 读日线 → 生成横截面信号 → 跑引擎 → 打印指标。

用法(仓库根目录,需先用 src.data 取数):
    python -m src.backtest.run --signal reversal --lookback 20 --top 20 --start 20200101
    python -m src.backtest.run --signal momentum --save data/equity.csv
"""
from __future__ import annotations

import argparse

import pandas as pd

from ..data import storage
from .costs import CostModel
from .engine import run_backtest
from .strategies.cross_sectional import cross_sectional_weights


def load_panel(codes=None, start=None, end=None, adjust="hfq") -> pd.DataFrame:
    df = storage.load_bars(codes=codes, start=start, end=end, adjust=adjust)
    if df.empty:
        return df
    return df[["date", "code", "open", "close"]]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("owq-backtest", description="OurWorlds Quant Lab 回测")
    p.add_argument("--signal", default="reversal", choices=["reversal", "momentum"])
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--start", default="20180101")
    p.add_argument("--adjust", default="hfq", choices=["hfq", "qfq", "none"])
    p.add_argument("--cash", type=float, default=1_000_000.0)
    p.add_argument("--codes", nargs="*", help="限定股票池;不填用库内全部")
    p.add_argument("--save", help="把净值序列保存为 csv")
    a = p.parse_args(argv)

    panel = load_panel(codes=a.codes, start=a.start, adjust=a.adjust)
    if panel.empty:
        print("⚠️ 库内无数据。请先取数,例如:python -m src.data.cli daily --limit 200")
        return 1
    n_codes = panel["code"].nunique()
    print(f"载入 {len(panel)} 行 / {n_codes} 只 / {panel['date'].min().date()}~{panel['date'].max().date()}")

    weights = cross_sectional_weights(panel, signal=a.signal, lookback=a.lookback, top_n=a.top)
    if weights.empty:
        print("⚠️ 信号为空(数据太短或股票太少)。")
        return 1

    res = run_backtest(panel, weights, cost_model=CostModel(), init_cash=a.cash)
    print(f"\n策略={a.signal} lookback={a.lookback} top={a.top} 调仓次数={len(weights)}")
    print("绩效:")
    for k, v in res["metrics"].items():
        print(f"  {k:>16}: {v}")
    if a.save:
        res["equity"].rename("equity").to_csv(a.save, header=True)
        print(f"净值已保存: {a.save}")
    print("\n提示:把 CostModel(slippage_bps=0, commission_rate=0, stamp_rate=0) 再跑一次,"
          "对比『有无成本』的差距——高换手策略的真实损耗就藏在这里。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

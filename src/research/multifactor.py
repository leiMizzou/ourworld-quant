"""多因子合成 → 组合构建 → 回测,一条龙。

闭环:
  data.storage.load_bars  → 价/量面板
  factors.compute + standardize → 各因子(截面标准化)
  composite_score         → 按方向与权重合成总分(越大越买)
  to_target_weights       → 月度等权 top-N 目标权重
  backtest.run_backtest   → 扣费/滑点/T+1/涨跌停 下的净值与指标

注意:默认**等权合成**(无前视)。IC 加权用的是全样本 IC,**有前视**,仅作演示;
真实使用应改为滚动 IC。
"""
from __future__ import annotations

import argparse

import pandas as pd

from ..backtest.costs import CostModel
from ..backtest.engine import run_backtest
from ..backtest.strategies.cross_sectional import rebalance_dates
from ..data import storage
from ..factors import factors as F
from ..factors.evaluate import evaluate_factor
from ..factors.preprocess import standardize
from ..metrics_glossary import glossary_markdown

# Metrics the CLI explains; the drift test asserts every key exists in the glossary.
CLI_GLOSSARY_KEYS = ("cagr", "sharpe", "max_drawdown", "turnover")

# (因子名, 窗口, 方向)。方向 +1=值越大越买;-1=值越小越买(合成时统一成"越大越买")
DEFAULT_SPECS = [
    ("reversal", 20, +1),     # 短期反转:跌得多的买
    ("volatility", 20, -1),   # 低波动溢价:波动低的买
]


def load_panels(codes=None, start=None, adjust="hfq") -> dict:
    df = storage.load_bars(codes=codes, start=start, adjust=adjust)
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    return {
        "close": df.pivot(index="date", columns="code", values="close").sort_index(),
        "amount": df.pivot(index="date", columns="code", values="amount").sort_index(),
    }


def composite_score(panels, specs=DEFAULT_SPECS, ic_weight=False, rebal=None):
    """把多个标准化因子按方向与权重合成为总分(date×code,越大越买)。"""
    close = panels["close"]
    comps, raw_w, ic_log = [], [], {}
    for name, window, sign in specs:
        fac = standardize(F.compute(name, panels, window=window)) * sign
        comps.append(fac)
        w = 1.0
        if ic_weight and rebal is not None:
            ic = evaluate_factor(close, fac, rebal)["report"]["ic"].get("ic_mean", 0.0)
            ic_log[name] = ic
            w = abs(ic)
        raw_w.append(w)
    s = sum(raw_w) or 1.0
    weights = [w / s for w in raw_w]
    composite = sum(c * w for c, w in zip(comps, weights))
    return composite, dict(zip([x[0] for x in specs], [round(w, 3) for w in weights])), ic_log


def to_target_weights(composite, close, rebal_dates, top_n=30) -> pd.DataFrame:
    """每个调仓日取合成分最高的 top_n 等权。只选当日在市(有收盘价)的。"""
    rows = {}
    for d in rebal_dates:
        if d not in composite.index:
            continue
        s = composite.loc[d].dropna()
        s = s[s.index.isin(close.loc[d].dropna().index)]
        if s.empty:
            continue
        picks = s.sort_values(ascending=False).head(top_n).index
        rows[d] = pd.Series(1.0 / len(picks), index=picks)
    return pd.DataFrame(rows).T.fillna(0.0).sort_index() if rows else pd.DataFrame()


def run(codes=None, start=None, adjust="hfq", specs=DEFAULT_SPECS, top_n=30, freq="M",
        ic_weight=False, init_cash=1_000_000.0, cost_model=None) -> dict:
    """完整闭环(从 DuckDB 读数)。返回 composite_weights / ic_log / metrics / equity。"""
    panels = load_panels(codes, start, adjust)
    if not panels:
        return {"error": "库内无数据"}
    close = panels["close"]
    rebal = rebalance_dates(close.index, freq)
    composite, comp_w, ic_log = composite_score(panels, specs, ic_weight=ic_weight, rebal=rebal)
    target = to_target_weights(composite, close, rebal, top_n=top_n)
    if target.empty:
        return {"error": "目标权重为空(数据太短或股票太少)"}
    res = run_backtest(_long_panel(codes, start, adjust), target,
                       cost_model=cost_model or CostModel(), init_cash=init_cash)
    return {"composite_weights": comp_w,
            "ic_log": {k: round(float(v), 4) for k, v in ic_log.items()},
            "n_rebalance": len(target), "metrics": res["metrics"],
            "equity": res["equity"], "backtest": res}


def _long_panel(codes=None, start=None, adjust="hfq") -> pd.DataFrame:
    df = storage.load_bars(codes=codes, start=start, adjust=adjust)
    return df[["date", "code", "open", "close"]] if not df.empty else df


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("owq-multifactor", description="多因子合成 → 组合 → 回测")
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--freq", default="M")
    p.add_argument("--start", default="20180101")
    p.add_argument("--adjust", default="hfq", choices=["hfq", "qfq", "none"])
    p.add_argument("--ic-weight", action="store_true", help="按|IC|加权(全样本,有前视,仅演示)")
    p.add_argument("--cash", type=float, default=1_000_000.0)
    p.add_argument("--codes", nargs="*")
    p.add_argument("--save", help="净值保存 csv")
    a = p.parse_args(argv)

    panels = load_panels(codes=a.codes, start=a.start, adjust=a.adjust)
    if not panels:
        print("⚠️ 库内无数据。先取数:python -m src.data.cli daily --limit 300")
        return 1
    close = panels["close"]
    rebal = rebalance_dates(close.index, a.freq)
    composite, comp_w, ic_log = composite_score(panels, ic_weight=a.ic_weight, rebal=rebal)
    target = to_target_weights(composite, close, rebal, top_n=a.top)
    if target.empty:
        print("⚠️ 目标权重为空。")
        return 1

    panel_long = _long_panel(codes=a.codes, start=a.start, adjust=a.adjust)
    res = run_backtest(panel_long, target, cost_model=CostModel(), init_cash=a.cash)

    print(f"\n=== 多因子组合 (top={a.top}, freq={a.freq}, ic_weight={a.ic_weight}) ===")
    print("因子权重:", comp_w, ("| 全样本IC: " + str({k: round(v, 4) for k, v in ic_log.items()})) if ic_log else "")
    print(f"调仓次数: {len(target)}")
    print("组合绩效:")
    for k, v in res["metrics"].items():
        print(f"  {k:>16}: {v}")
    surv = res.get("survivorship") or {}
    if surv:
        print(f"  {'退市强制平仓':>16}: {surv.get('delisted_positions_closed', 0)} 笔")
    if a.save:
        res["equity"].rename("equity").to_csv(a.save, header=True)
        print("净值已保存:", a.save)
    print("\n指标说明(与 App / 报告同源 src/metrics_glossary.py):")
    print(glossary_markdown(CLI_GLOSSARY_KEYS))
    print("\n判读:对比单因子与组合的夏普/回撤;再切样本内外。别只看年化;好看的数字先怀疑数据(幸存者/前视)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

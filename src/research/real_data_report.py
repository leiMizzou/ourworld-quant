"""Real-data verification, regression, backtest, and prediction report.

The report intentionally reads only persisted market data from DuckDB and the
app SQLite database. It does not treat docs/demo values as research evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..app import db as app_db
from ..backtest.costs import CostModel
from ..backtest.engine import run_backtest
from ..backtest.strategies.cross_sectional import rebalance_dates
from ..data import clean, config, storage
from ..data.sources import get_source
from ..factors import factors as F
from ..factors.evaluate import evaluate_factor, forward_returns
from ..factors.preprocess import standardize
from .multifactor import DEFAULT_SPECS, composite_score, to_target_weights
from ..metrics_glossary import glossary_markdown

# Metrics this report surfaces; the drift test asserts every key exists in the glossary.
REPORT_GLOSSARY_KEYS = ("cagr", "sharpe", "max_drawdown", "ic", "icir", "turnover")


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    factor: str
    window: int


# 复权研究低于这个标的数就视为样本过小、不具代表性(通常是 hfq 日线没同步够)。
MIN_REPRESENTATIVE_CODES = 30

FEATURE_SPECS = [
    FeatureSpec("reversal_20", "reversal", 20),
    FeatureSpec("momentum_60", "momentum", 60),
    FeatureSpec("volatility_20", "volatility", 20),
    FeatureSpec("amihud_20", "amihud", 20),
    FeatureSpec("ma_bias_20", "ma_bias", 20),
]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _fmt_pct(x) -> str:
    if x is None or pd.isna(x):
        return "-"
    return f"{float(x) * 100:.2f}%"


def _fmt_num(x, digits: int = 4) -> str:
    if x is None or pd.isna(x):
        return "-"
    return f"{float(x):.{digits}f}"


def load_panels(codes=None, start=None, adjust="hfq") -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    df = storage.load_bars(codes=codes, start=start, adjust=adjust)
    if df.empty:
        return {}, df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    panels = {
        "open": df.pivot(index="date", columns="code", values="open").sort_index(),
        "close": df.pivot(index="date", columns="code", values="close").sort_index(),
        "amount": df.pivot(index="date", columns="code", values="amount").sort_index(),
    }
    return panels, df


def source_counts() -> pd.DataFrame:
    if not config.DB_PATH.exists():
        return pd.DataFrame(columns=["source", "rows", "codes", "date_min", "date_max"])
    with storage.connect(read_only=True) as con:
        try:
            return con.execute(
                """
                SELECT source,
                       count(*) AS rows,
                       count(DISTINCT code) AS codes,
                       min(date) AS date_min,
                       max(date) AS date_max
                FROM daily_bars
                GROUP BY source
                ORDER BY source
                """
            ).df()
        except Exception:  # noqa: BLE001 - empty/uninitialized DB
            return pd.DataFrame(columns=["source", "rows", "codes", "date_min", "date_max"])


def app_market_counts(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["source", "rows", "codes", "date_min", "date_max"])
    con = sqlite3.connect(path)
    try:
        return pd.read_sql_query(
            """
            SELECT source,
                   count(*) AS rows,
                   count(DISTINCT code) AS codes,
                   min(as_of) AS date_min,
                   max(as_of) AS date_max
            FROM market_prices
            GROUP BY source
            ORDER BY source
            """,
            con,
        )
    finally:
        con.close()


def probe_sources(codes: list[str], start: str, adjust: str, include_tushare: bool = True) -> list[dict]:
    probes = []
    names = ["akshare", "baostock"]
    if include_tushare:
        names.append("tushare")
    for name in names:
        src = None
        try:
            src = get_source(name)
            df = src.get_daily_bars(codes[0], start=start, adjust=adjust)
            df = clean.standardize_bars(df)
            probes.append(
                {
                    "source": name,
                    "status": "ok" if not df.empty else "empty",
                    "rows": int(len(df)),
                    "date_min": str(df["date"].min().date()) if not df.empty else "",
                    "date_max": str(df["date"].max().date()) if not df.empty else "",
                    "detail": "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            probes.append(
                {
                    "source": name,
                    "status": "missing_config" if name == "tushare" and not os.getenv("TUSHARE_TOKEN") else "error",
                    "rows": 0,
                    "date_min": "",
                    "date_max": "",
                    "detail": str(exc),
                }
            )
        finally:
            if src is not None:
                src.close()
    return probes


def feature_panels(panels: dict[str, pd.DataFrame], specs: list[FeatureSpec] = FEATURE_SPECS) -> dict[str, pd.DataFrame]:
    out = {}
    for spec in specs:
        out[spec.name] = standardize(F.compute(spec.factor, panels, window=spec.window))
    return out


def regression_frame(features: dict[str, pd.DataFrame], close: pd.DataFrame, rebal: list[pd.Timestamp]) -> pd.DataFrame:
    fwd = forward_returns(close, rebal)
    rows = []
    for d in fwd.index:
        pieces = [features[name].loc[d].rename(name) for name in features if d in features[name].index]
        if len(pieces) != len(features):
            continue
        frame = pd.concat(pieces + [fwd.loc[d].rename("target_return")], axis=1).dropna()
        for code, row in frame.iterrows():
            item = {"date": pd.Timestamp(d), "code": code}
            item.update({k: float(row[k]) for k in features})
            item["target_return"] = float(row["target_return"])
            rows.append(item)
    return pd.DataFrame(rows).sort_values(["date", "code"]).reset_index(drop=True) if rows else pd.DataFrame()


def fit_regression(frame: pd.DataFrame, feature_names: list[str]) -> dict:
    if frame.empty or len(frame["date"].unique()) < 4:
        return {"error": "回归样本不足"}
    dates = sorted(pd.to_datetime(frame["date"].unique()))
    split = max(1, int(len(dates) * 0.7))
    if split >= len(dates):
        split = len(dates) - 1
    train_dates = set(dates[:split])
    train = frame[pd.to_datetime(frame["date"]).isin(train_dates)]
    test = frame[~pd.to_datetime(frame["date"]).isin(train_dates)]
    if len(train) <= len(feature_names) + 1 or test.empty:
        return {"error": "训练或测试样本不足"}

    x_train = train[feature_names].to_numpy(dtype=float)
    y_train = train["target_return"].to_numpy(dtype=float)
    x_test = test[feature_names].to_numpy(dtype=float)
    y_test = test["target_return"].to_numpy(dtype=float)

    x_train_i = np.column_stack([np.ones(len(x_train)), x_train])
    beta = np.linalg.lstsq(x_train_i, y_train, rcond=None)[0]
    pred = np.column_stack([np.ones(len(x_test)), x_test]) @ beta

    denom = float(np.sum((y_test - y_test.mean()) ** 2))
    r2 = float(1 - np.sum((y_test - pred) ** 2) / denom) if denom > 0 else np.nan
    pearson = float(pd.Series(pred).corr(pd.Series(y_test))) if len(test) > 1 else np.nan
    spearman = float(pd.Series(pred).rank().corr(pd.Series(y_test).rank())) if len(test) > 1 else np.nan
    direction = float((np.sign(pred) == np.sign(y_test)).mean()) if len(test) else np.nan

    test_eval = test[["date", "code", "target_return"]].copy()
    test_eval["prediction"] = pred
    per_date_ic = (
        test_eval.groupby("date")
        .apply(lambda g: g["prediction"].rank().corr(g["target_return"].rank()) if len(g) >= 3 else np.nan)
        .dropna()
    )
    coefs = {"intercept": float(beta[0])}
    coefs.update({name: float(value) for name, value in zip(feature_names, beta[1:])})
    return {
        "coefs": coefs,
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train_periods": int(len(train_dates)),
        "test_periods": int(len(dates) - len(train_dates)),
        "oos_r2": r2,
        "oos_pearson": pearson,
        "oos_rank_ic_mean": float(per_date_ic.mean()) if len(per_date_ic) else spearman,
        "oos_direction_acc": direction,
        "test_predictions": test_eval,
    }


def latest_predictions(
    features: dict[str, pd.DataFrame],
    close: pd.DataFrame,
    model: dict,
    top_n: int,
) -> pd.DataFrame:
    if "coefs" not in model:
        return pd.DataFrame()
    latest_date = max(set.intersection(*(set(f.dropna(how="all").index) for f in features.values())))
    rows = pd.concat([features[k].loc[latest_date].rename(k) for k in features], axis=1).dropna()
    rows = rows.loc[rows.index.isin(close.loc[latest_date].dropna().index)]
    if rows.empty:
        return pd.DataFrame()
    pred = np.full(len(rows), model["coefs"]["intercept"], dtype=float)
    for name in features:
        pred += rows[name].to_numpy(dtype=float) * model["coefs"].get(name, 0.0)
    out = rows.copy()
    out["prediction"] = pred
    out["date"] = latest_date
    out["last_close"] = close.loc[latest_date].reindex(out.index)
    return (
        out.reset_index(names="code")
        .sort_values("prediction", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )


def factor_reports(features: dict[str, pd.DataFrame], close: pd.DataFrame, rebal: list[pd.Timestamp]) -> list[dict]:
    reports = []
    for name, panel in features.items():
        result = evaluate_factor(close, panel, rebal)
        ic = result["report"].get("ic", {})
        reports.append(
            {
                "factor": name,
                "ic_mean": ic.get("ic_mean"),
                "icir": ic.get("icir"),
                "t_stat": ic.get("t_stat"),
                "ic_pos_rate": ic.get("ic_pos_rate"),
                "n_periods": ic.get("n_periods", 0),
                "long_short_ann": result["report"].get("long_short_ann"),
                "monotonicity": result["report"].get("monotonicity"),
            }
        )
    return reports


def backtest_report(panels: dict[str, pd.DataFrame], long_panel: pd.DataFrame, top_n: int, freq: str) -> dict:
    close = panels["close"]
    rebal = rebalance_dates(close.index, freq)
    composite, weights, _ = composite_score(panels, DEFAULT_SPECS, ic_weight=False, rebal=rebal)
    target = to_target_weights(composite, close, rebal, top_n=top_n)
    if target.empty:
        return {"error": "目标权重为空"}
    res = run_backtest(
        long_panel[["date", "code", "open", "close"]],
        target,
        cost_model=CostModel(),
        init_cash=1_000_000.0,
    )
    return {
        "factor_weights": weights,
        "n_rebalance": len(target),
        "metrics": res["metrics"],
        "survivorship": res.get("survivorship", {}),
    }


def survivorship_comparison(panels, long_panel, top_n: int = 10, freq: str = "M", survivor_tail: int = 10) -> dict:
    """量化幸存者偏差:同一策略在两个票池上的绩效差。

    - 真实池(full):包含中途退市的标的(它们的行情会在退市日结束)。
    - 有偏池(survivors_only):只保留'活到样本期末'的标的——这是新手最容易犯的错。
    只在 survivors 上回测会系统性高估收益/夏普;delta = survivors_only − full 就是被高估的幅度。
    用 none(不复权)票池,因为退市股通常只有不复权行情;比较在同一口径下进行。
    """
    close = panels["close"]
    if close.shape[1] < 6 or len(close) < 60:
        return {"error": "样本不足,无法量化幸存者偏差"}
    tail_idx = close.index[-survivor_tail:]
    survivors = [c for c in close.columns if bool(close.loc[tail_idx, c].notna().any())]
    delisted = [c for c in close.columns if c not in survivors]
    if not delisted:
        return {"error": "票池中没有中途退市的标的(需先补采退市行情)", "n_delisted": 0, "n_full": int(close.shape[1])}

    def _metrics(cols: list[str]):
        sub = {"close": close[cols], "amount": panels["amount"][cols]}
        rebal = rebalance_dates(sub["close"].index, freq)
        comp, _, _ = composite_score(sub, DEFAULT_SPECS, ic_weight=False, rebal=rebal)
        tgt = to_target_weights(comp, sub["close"], rebal, top_n=min(top_n, len(cols)))
        if tgt.empty:
            return None
        lp = long_panel[long_panel["code"].isin(cols)][["date", "code", "open", "close"]]
        res = run_backtest(lp, tgt, cost_model=CostModel(), init_cash=1_000_000.0)
        return res["metrics"]

    full = _metrics(list(close.columns))      # realistic (includes delisted)
    surv = _metrics(survivors)                # biased (survivors only)
    if not full or not surv:
        return {"error": "目标权重为空", "n_delisted": len(delisted)}
    keys = ("total_return", "cagr", "sharpe", "max_drawdown")
    delta = {k: round(float(surv.get(k, 0.0)) - float(full.get(k, 0.0)), 4) for k in keys if k in full and k in surv}
    return {
        "n_full": int(close.shape[1]),
        "n_survivors": len(survivors),
        "n_delisted": len(delisted),
        "full": {k: full.get(k) for k in keys},          # 真实
        "survivors_only": {k: surv.get(k) for k in keys},  # 有偏
        "delta_survivors_minus_full": delta,
    }


def build_preview_payload(panels, long_panel, top_n: int = 10, freq: str = "M", max_points: int = 60) -> dict:
    """Precompute the public /preview artifact: a real, honestly-labeled backtest equity curve
    plus the survivorship comparison (the teaching hook). Saved to reports/preview.json so the
    page renders fast and fully server-side — no per-request backtest, no JS dependency."""
    comparison = survivorship_comparison(panels, long_panel, top_n=top_n, freq=freq)
    close = panels["close"]
    rebal = rebalance_dates(close.index, freq)
    comp_score, _, _ = composite_score(panels, DEFAULT_SPECS, ic_weight=False, rebal=rebal)
    target = to_target_weights(comp_score, close, rebal, top_n=min(top_n, close.shape[1]))
    equity_points: list[dict] = []
    metrics: dict = {}
    if not target.empty:
        res = run_backtest(long_panel[["date", "code", "open", "close"]], target, cost_model=CostModel(), init_cash=1_000_000.0)
        eq = res["equity"]
        metrics = res["metrics"]
        n = len(eq)
        if n:
            step = max(1, n // max_points)
            for i in range(0, n, step):
                equity_points.append({"date": str(eq.index[i])[:10], "equity": round(float(eq.iloc[i]), 2)})
            last = {"date": str(eq.index[-1])[:10], "equity": round(float(eq.iloc[-1]), 2)}
            if not equity_points or equity_points[-1] != last:
                equity_points.append(last)
    return {
        "as_of": str(close.index.max())[:10] if len(close) else "",
        "freq": freq,
        "top_n": top_n,
        "n_codes": int(close.shape[1]),
        "metrics": {k: metrics.get(k) for k in ("total_return", "cagr", "sharpe", "max_drawdown")},
        "equity_points": equity_points,
        "survivorship": comparison,
    }


def _markdown_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "_无数据_\n"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = ["| " + " | ".join(str(row.get(c, "")) for c in columns) + " |" for row in rows]
    return "\n".join([header, sep] + body) + "\n"


def write_predictions_csv(path: Path, pred: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pred.to_csv(path, index=False)


def write_report(
    path: Path,
    *,
    source_df: pd.DataFrame,
    app_df: pd.DataFrame,
    probes: list[dict],
    quality: dict,
    factors: list[dict],
    model: dict,
    predictions: pd.DataFrame,
    bt: dict,
    app_db_path: Path,
    survivorship: dict | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    source_rows = source_df.astype(str).to_dict("records") if not source_df.empty else []
    app_rows = app_df.astype(str).to_dict("records") if not app_df.empty else []
    factor_rows = [
        {
            "factor": r["factor"],
            "IC": _fmt_num(r.get("ic_mean")),
            "ICIR": _fmt_num(r.get("icir"), 3),
            "t": _fmt_num(r.get("t_stat"), 2),
            "pos_rate": _fmt_pct(r.get("ic_pos_rate")),
            "LS_ann": _fmt_pct(r.get("long_short_ann")),
            "n": r.get("n_periods", 0),
        }
        for r in factors
    ]
    pred_rows = [
        {
            "rank": i + 1,
            "code": row["code"],
            "prediction": _fmt_pct(row["prediction"]),
            "last_close": _fmt_num(row["last_close"], 3),
        }
        for i, row in predictions.iterrows()
    ]
    lines = [
        "# OurWorlds Quant Real Data Report",
        "",
        "本报告由 `python -m src.research.real_data_report` 生成,只读取 DuckDB/App SQLite 中的真实行情同步结果。",
        "结果用于研究验证,不构成投资建议。",
        "",
        "## Data Sources",
        "",
        f"- DuckDB: `{config.DB_PATH}`",
        f"- App SQLite: `{app_db_path}`",
        f"- Quality: `{quality}`",
        "",
        "### Persisted Daily Bars",
        "",
        _markdown_table(source_rows, ["source", "rows", "codes", "date_min", "date_max"]),
        "### App Market Prices",
        "",
        _markdown_table(app_rows, ["source", "rows", "codes", "date_min", "date_max"]),
        "### Live Source Probe",
        "",
        _markdown_table(probes, ["source", "status", "rows", "date_min", "date_max", "detail"]),
        "## Factor IC",
        "",
        _markdown_table(factor_rows, ["factor", "IC", "ICIR", "t", "pos_rate", "LS_ann", "n"]),
        "## Cross-Sectional Regression",
        "",
    ]
    if "error" in model:
        lines.append(f"- Error: {model['error']}")
    else:
        lines += [
            f"- Train rows / periods: {model['train_rows']} / {model['train_periods']}",
            f"- Test rows / periods: {model['test_rows']} / {model['test_periods']}",
            f"- OOS R2: `{_fmt_num(model['oos_r2'])}`",
            f"- OOS Pearson: `{_fmt_num(model['oos_pearson'])}`",
            f"- OOS RankIC mean: `{_fmt_num(model['oos_rank_ic_mean'])}`",
            f"- Direction accuracy: `{_fmt_pct(model['oos_direction_acc'])}`",
            f"- Coefficients: `{ {k: round(v, 6) for k, v in model['coefs'].items()} }`",
        ]
    lines += [
        "",
        "## Next-Period Prediction Candidates",
        "",
        _markdown_table(pred_rows, ["rank", "code", "prediction", "last_close"]),
        "## Backtest",
        "",
    ]
    if "error" in bt:
        lines.append(f"- Error: {bt['error']}")
    else:
        metrics = bt["metrics"]
        surv = bt.get("survivorship") or {}
        lines += [
            f"- Rebalances: {bt['n_rebalance']}",
            f"- Factor weights: `{bt['factor_weights']}`",
            f"- Total return: `{_fmt_pct(metrics.get('total_return'))}`",
            f"- CAGR: `{_fmt_pct(metrics.get('cagr'))}`",
            f"- Sharpe: `{_fmt_num(metrics.get('sharpe'), 3)}`",
            f"- Max drawdown: `{_fmt_pct(metrics.get('max_drawdown'))}`",
            f"- Annual turnover: `{_fmt_num(metrics.get('annual_turnover'), 2)}`",
            f"- 退市强制平仓: {surv.get('delisted_positions_closed', 0)} 笔"
            + (f"({surv['note']})" if surv.get("note") else ""),
        ]
    sv = survivorship or {}
    lines += ["", "## 幸存者偏差实测", ""]
    if sv.get("error"):
        lines.append(f"- {sv['error']}" + (f"(全部 {sv['n_full']} 只)" if sv.get("n_full") else ""))
    else:
        f = sv["full"]
        s = sv["survivors_only"]
        d = sv["delta_survivors_minus_full"]
        lines += [
            "同一策略,只换票池:**含中途退市的真实池** vs **只保留活到期末的有偏池**(不复权 none 口径)。",
            "",
            f"- 票池: 全部 {sv['n_full']} 只,其中中途退市 {sv['n_delisted']} 只、活到期末 {sv['n_survivors']} 只",
            f"- 真实(含退市): 总收益 `{_fmt_pct(f.get('total_return'))}` · CAGR `{_fmt_pct(f.get('cagr'))}` · Sharpe `{_fmt_num(f.get('sharpe'), 3)}` · 最大回撤 `{_fmt_pct(f.get('max_drawdown'))}`",
            f"- 有偏(只回测活下来的): 总收益 `{_fmt_pct(s.get('total_return'))}` · CAGR `{_fmt_pct(s.get('cagr'))}` · Sharpe `{_fmt_num(s.get('sharpe'), 3)}` · 最大回撤 `{_fmt_pct(s.get('max_drawdown'))}`",
            f"- **被高估的幅度(有偏 − 真实)**: 总收益 `{_fmt_pct(d.get('total_return'))}` · CAGR `{_fmt_pct(d.get('cagr'))}` · Sharpe `{_fmt_num(d.get('sharpe'), 3)}`",
            "",
            "解读: 只在'活下来的'股票上回测,把退市股造成的亏损排除在外,会系统性高估策略表现。差值就是幸存者偏差的真实代价——这也是为什么回测票池必须含退市股。",
        ]
    lines += [
        "",
        "## 指标说明",
        "",
        "（与 App 内 tooltip、`/api/glossary` 同源,见 `src/metrics_glossary.py`。判读偏保守:好看的数字往往是数据缺陷,不是 alpha。）",
        "",
        glossary_markdown(REPORT_GLOSSARY_KEYS),
        "",
        "## Notes",
        "",
        "- 样本股票太少时,回归和回测只能验证链路,不能作为稳健结论。",
        "- **幸存者偏差**:本数据集只覆盖少量退市股(大量退市名单没有行情),回测把'活下来的'当成全部,"
        "结果系统性偏乐观;退市持仓已按最后收盘价强制平仓,但真实偏差仍被低估。",
        "- 线上预测模型用前 70% 训练、后 30% 留作 OOS 评估;报告里的 OOS 指标只读这部分留出集,不要据此对最新预测过度自信。",
        "- Tushare 需要 `TUSHARE_TOKEN`;未配置时报告会标记为 missing_config。",
        "- 后复权价格适合收益连续性研究,不适合直接当作真实可成交价格展示。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser("owq-real-data-report")
    parser.add_argument("--codes", nargs="*", help="限制代码范围;默认读取 DuckDB 中全部代码")
    parser.add_argument("--start", default="20230101")
    parser.add_argument("--adjust", default="hfq", choices=["hfq", "qfq", "none"])
    parser.add_argument("--freq", default="M")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--app-db", default=str(app_db.DEFAULT_DB_PATH))
    parser.add_argument("--probe", action="store_true", help="主动探测 AkShare/BaoStock/Tushare")
    parser.add_argument("--probe-start", default="20260101")
    parser.add_argument("--probe-adjust", choices=["hfq", "qfq", "none"], help="source probe adjust, defaults to --adjust")
    parser.add_argument("--out", default="reports/real-data-report.md")
    parser.add_argument("--predictions-csv", default="reports/predictions.csv")
    parser.add_argument("--preview-out", default="reports/preview.json")
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="只生成公开 /preview 用的 JSON 工件(none 票池,真实回测+幸存者对比),不生成完整报告/预测",
    )
    parser.add_argument(
        "--min-representative-codes",
        type=int,
        default=_env_int("OWQ_REPORT_MIN_REPRESENTATIVE_CODES", MIN_REPRESENTATIVE_CODES),
        help="复权研究最少标的数;低于该值视为样本不具代表性。",
    )
    parser.add_argument(
        "--strict-representative-codes",
        action="store_true",
        default=_env_flag("OWQ_REPORT_STRICT_REPRESENTATIVE_CODES"),
        help="复权研究标的数低于阈值时直接失败,用于生产同步。",
    )
    parser.add_argument(
        "--allow-unadjusted",
        action="store_true",
        help="允许用不复权(none)价做研究——仅用于调试。不复权价含分红除权跳空,"
        "会使 IC/回归/回测失真,正式研究必须用 hfq。",
    )
    args = parser.parse_args(argv)

    # 公开 /preview 工件:用 none 票池(退市股只有不复权行情),生成真实回测曲线 + 幸存者对比。
    # 与主研究报告解耦,不改写 real-data-report.md / predictions.csv。
    if args.preview_only:
        preview_panels, preview_long = load_panels(codes=args.codes, start=args.start, adjust="none")
        if not preview_panels:
            print("No none data in DuckDB for preview.", file=sys.stderr)
            return 1
        payload = build_preview_payload(preview_panels, preview_long, top_n=args.top, freq=args.freq)
        preview_path = Path(args.preview_out)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Preview written: {preview_path}")
        return 0

    # 不复权价反映的是除权除息跳空而非真实收益,会让因子/IC/回测全部失真。研究默认
    # 必须用 hfq;模拟盘成交价才用 none。这里硬性拦截,避免再次生成"看似真实却无效"的报告。
    if args.adjust == "none" and not args.allow_unadjusted:
        print(
            "拒绝执行:--adjust none 会用不复权价做研究,IC/回归/回测结果无效。\n"
            "请改用 --adjust hfq(需先同步 hfq 日线);确需调试不复权数据再显式加 --allow-unadjusted。",
            file=sys.stderr,
        )
        return 2

    panels, long_panel = load_panels(codes=args.codes, start=args.start, adjust=args.adjust)
    if not panels:
        print("No real data in DuckDB. Run: python -m src.data.cli daily --source akshare --codes 000001.SZ 600519.SH")
        return 1

    close = panels["close"]
    # 复权研究但股票池过小,通常意味着 hfq 日线尚未按代表性股票池同步。响亮提示,
    # 避免把"只有几只票"的结果当成可信结论。
    min_representative_codes = max(1, int(args.min_representative_codes))
    sample_too_small = args.adjust in {"hfq", "qfq"} and close.shape[1] < min_representative_codes
    if sample_too_small:
        print(
            f"警告:adjust={args.adjust} 仅加载到 {close.shape[1]} 只标的"
            f"(< {min_representative_codes}),样本过小、不具代表性,结论不可信。\n"
            "请先用 deploy/sync-market-public.sh 或 src.data.cli 同步 hfq 日线"
            "(含退市股票池)再生成报告。",
            file=sys.stderr,
        )
        if args.strict_representative_codes:
            return 2
    rebal = rebalance_dates(close.index, args.freq)
    features = feature_panels(panels)
    frame = regression_frame(features, close, rebal)
    feature_names = list(features)
    model = fit_regression(frame, feature_names)
    predictions = latest_predictions(features, close, model, args.top)
    factors = factor_reports(features, close, rebal)
    bt = backtest_report(panels, long_panel, top_n=min(args.top, close.shape[1]), freq=args.freq)

    # 幸存者偏差实测:用 none 票池(退市股一般只有不复权行情),同一策略在 含退市 vs 仅存活
    # 两个池上的绩效差。与上面 hfq 研究分开,仅用于量化幸存者偏差这一个问题。
    try:
        surv_panels, surv_long = load_panels(codes=args.codes, start=args.start, adjust="none")
        survivorship = (
            survivorship_comparison(surv_panels, surv_long, top_n=args.top, freq=args.freq)
            if surv_panels
            else {"error": "无 none 行情,无法量化幸存者偏差"}
        )
    except Exception as exc:  # noqa: BLE001 - 对比失败不应阻断主报告
        survivorship = {"error": f"幸存者偏差对比失败: {type(exc).__name__}"}

    probe_codes = args.codes or list(close.columns)
    probes = probe_sources(probe_codes, args.probe_start, args.probe_adjust or args.adjust) if args.probe else []
    quality = clean.quality_report(long_panel)
    source_df = source_counts()
    app_path = Path(args.app_db)
    app_df = app_market_counts(app_path)

    out_path = Path(args.out)
    pred_path = Path(args.predictions_csv)
    write_predictions_csv(pred_path, predictions)
    write_report(
        out_path,
        source_df=source_df,
        app_df=app_df,
        probes=probes,
        quality=quality,
        factors=factors,
        model=model,
        predictions=predictions,
        bt=bt,
        app_db_path=app_path,
        survivorship=survivorship,
    )
    print(f"Report written: {out_path}")
    print(f"Predictions written: {pred_path}")
    if "error" not in model:
        print(
            "Regression OOS:",
            {
                "r2": round(float(model["oos_r2"]), 4) if pd.notna(model["oos_r2"]) else None,
                "rank_ic": round(float(model["oos_rank_ic_mean"]), 4)
                if pd.notna(model["oos_rank_ic_mean"])
                else None,
                "direction_acc": round(float(model["oos_direction_acc"]), 4)
                if pd.notna(model["oos_direction_acc"])
                else None,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

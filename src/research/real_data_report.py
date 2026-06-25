"""Real-data verification, regression, backtest, and prediction report.

The report intentionally reads only persisted market data from DuckDB and the
app SQLite database. It does not treat docs/demo values as research evidence.
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
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


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    factor: str
    window: int


FEATURE_SPECS = [
    FeatureSpec("reversal_20", "reversal", 20),
    FeatureSpec("momentum_60", "momentum", 60),
    FeatureSpec("volatility_20", "volatility", 20),
    FeatureSpec("amihud_20", "amihud", 20),
    FeatureSpec("ma_bias_20", "ma_bias", 20),
]


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
    return {"factor_weights": weights, "n_rebalance": len(target), "metrics": res["metrics"]}


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
        lines += [
            f"- Rebalances: {bt['n_rebalance']}",
            f"- Factor weights: `{bt['factor_weights']}`",
            f"- Total return: `{_fmt_pct(metrics.get('total_return'))}`",
            f"- CAGR: `{_fmt_pct(metrics.get('cagr'))}`",
            f"- Sharpe: `{_fmt_num(metrics.get('sharpe'), 3)}`",
            f"- Max drawdown: `{_fmt_pct(metrics.get('max_drawdown'))}`",
            f"- Annual turnover: `{_fmt_num(metrics.get('annual_turnover'), 2)}`",
        ]
    lines += [
        "",
        "## Notes",
        "",
        "- 样本股票太少时,回归和回测只能验证链路,不能作为稳健结论。",
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
    args = parser.parse_args(argv)

    panels, long_panel = load_panels(codes=args.codes, start=args.start, adjust=args.adjust)
    if not panels:
        print("No real data in DuckDB. Run: python -m src.data.cli daily --source akshare --codes 000001.SZ 600519.SH")
        return 1

    close = panels["close"]
    rebal = rebalance_dates(close.index, args.freq)
    features = feature_panels(panels)
    frame = regression_frame(features, close, rebal)
    feature_names = list(features)
    model = fit_regression(frame, feature_names)
    predictions = latest_predictions(features, close, model, args.top)
    factors = factor_reports(features, close, rebal)
    bt = backtest_report(panels, long_panel, top_n=min(args.top, close.shape[1]), freq=args.freq)

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

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from src.factors.evaluate import forward_returns
from src.metrics_glossary import GLOSSARY_FIELDS, METRIC_GLOSSARY
from src.research.multifactor import CLI_GLOSSARY_KEYS, to_target_weights
from src.research.real_data_report import REPORT_GLOSSARY_KEYS, fit_regression, survivorship_comparison


class ResearchPipelineTest(unittest.TestCase):
    def test_forward_returns_use_next_rebalance_close(self):
        dates = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
        close = pd.DataFrame({"000001.SZ": [10.0, 11.0, 12.0], "600000.SH": [8.0, 8.0, 7.2]}, index=dates)

        fwd = forward_returns(close, [dates[0], dates[2]])

        self.assertEqual(list(fwd.index), [dates[0]])
        self.assertAlmostEqual(fwd.loc[dates[0], "000001.SZ"], 0.2)
        self.assertAlmostEqual(fwd.loc[dates[0], "600000.SH"], -0.1)

    def test_target_weights_select_top_scores_that_are_tradable_on_rebalance_date(self):
        date = pd.Timestamp("2026-01-02")
        composite = pd.DataFrame(
            {"000001.SZ": [0.3], "600000.SH": [1.2], "300001.SZ": [0.8]},
            index=[date],
        )
        close = pd.DataFrame(
            {"000001.SZ": [10.0], "600000.SH": [None], "300001.SZ": [20.0]},
            index=[date],
        )

        weights = to_target_weights(composite, close, [date], top_n=2)

        self.assertEqual(set(weights.columns), {"000001.SZ", "300001.SZ"})
        self.assertAlmostEqual(weights.loc[date, "000001.SZ"], 0.5)
        self.assertAlmostEqual(weights.loc[date, "300001.SZ"], 0.5)


    def test_regression_coefs_are_train_only_not_full_sample(self):
        # Guard the train/test boundary: latest_predictions is fed fit_regression()'s coefs,
        # and the report's OOS metrics read the held-out split. If anyone "refits on full
        # history" by wiring the full-sample model into the SAME object that reports OOS, it
        # would leak test data into the reported metrics. This locks coefs to the train split.
        dates = pd.to_datetime([f"2026-01-{d:02d}" for d in range(1, 11)])  # 10 dates -> split 7/3
        feats = [-2.0, -1.0, 0.0, 1.0, 2.0]
        rows = []
        for di, d in enumerate(dates):
            slope = 2.0 if di < 7 else -5.0  # train slope 2, test slope flips to -5
            for j, f in enumerate(feats):
                rows.append({"date": d, "code": f"C{j}", "f": f, "target_return": slope * f})
        frame = pd.DataFrame(rows)

        model = fit_regression(frame, ["f"])

        self.assertEqual(model["train_periods"], 7)
        self.assertEqual(model["test_periods"], 3)
        self.assertIn("oos_r2", model)  # OOS computed on the held-out split
        # Coefs match the TRAIN-only OLS (slope 2), not the full-sample fit.
        self.assertAlmostEqual(model["coefs"]["f"], 2.0, places=6)
        x = frame[["f"]].to_numpy(float)
        full = np.linalg.lstsq(np.column_stack([np.ones(len(x)), x]), frame["target_return"].to_numpy(float), rcond=None)[0]
        self.assertGreater(abs(full[1] - 2.0), 0.5)  # full-sample slope clearly differs

    def test_metric_glossary_has_no_consumer_drift(self):
        # One source of truth, three consumers: the app dashboard (metric_label), the markdown
        # report, and the CLI. Every consumer key must resolve, and every entry well-formed.
        for key, info in METRIC_GLOSSARY.items():
            for field in GLOSSARY_FIELDS:
                self.assertIn(field, info, f"{key} missing {field}")
            for field in ("term", "short", "formula", "band"):  # unit may be "" (e.g. sharpe)
                self.assertTrue(str(info[field]).strip(), f"{key}.{field} is empty")
        # Keys referenced by the dashboard's metric_label() calls in src/app/server.py.
        app_keys = {"equity", "cash", "return_pct"}
        for keyset in (app_keys, set(REPORT_GLOSSARY_KEYS), set(CLI_GLOSSARY_KEYS)):
            missing = keyset - set(METRIC_GLOSSARY)
            self.assertEqual(missing, set(), f"glossary missing keys referenced by a consumer: {missing}")


    def test_survivorship_comparison_partitions_universe_and_returns_delta(self):
        # 6+ codes, 60+ dates: one name delists at row 40 (NaN through the tail) → must be
        # classed as delisted; the rest survive. The comparison returns full vs survivors-only
        # metrics and a delta. This locks the partition + contract (magnitude is data-specific).
        dates = pd.bdate_range("2024-01-01", periods=70)
        codes = [f"60000{i}.SH" for i in range(1, 8)]  # 7 codes
        gone = codes[-1]
        close, amount, long_rows = {}, {}, []
        for j, c in enumerate(codes):
            col = []
            for i, d in enumerate(dates):
                if c == gone and i >= 40:
                    px = float("nan")  # delisted: no more bars
                else:
                    px = (10 + j) * (1 + 0.002 * i) * (0.6 if (c == gone and i >= 30) else 1.0)
                col.append(px)
                if not pd.isna(px):
                    long_rows.append({"date": d, "code": c, "open": px * 0.999, "close": px})
            close[c] = col
            amount[c] = [(p * 1e6 if not pd.isna(p) else float("nan")) for p in col]
        panels = {"close": pd.DataFrame(close, index=dates), "amount": pd.DataFrame(amount, index=dates)}
        res = survivorship_comparison(panels, pd.DataFrame(long_rows), top_n=3, freq="M")

        self.assertEqual(res["n_full"], 7)
        self.assertEqual(res["n_delisted"], 1)
        self.assertEqual(res["n_survivors"], 6)
        self.assertIn("delta_survivors_minus_full", res)
        for k in ("total_return", "cagr", "sharpe", "max_drawdown"):
            self.assertIn(k, res["full"])
            self.assertIn(k, res["survivors_only"])


if __name__ == "__main__":
    unittest.main()

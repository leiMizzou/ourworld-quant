from __future__ import annotations

import unittest

import pandas as pd

from src.backtest.engine import run_backtest
from src.backtest.strategies.cross_sectional import cross_sectional_weights


class BacktestSmokeTest(unittest.TestCase):
    def test_cross_sectional_strategy_runs_on_synthetic_panel(self):
        dates = pd.bdate_range("2024-01-01", periods=80)
        rows = []
        for j, code in enumerate(["000001.SZ", "600000.SH", "300001.SZ", "688001.SH", "000002.SZ"]):
            for i, date in enumerate(dates):
                close = (10 + j) * (1 + 0.001 * (i + 1) + 0.01 * j)
                rows.append({"date": date, "code": code, "open": close * 0.999, "close": close})
        panel = pd.DataFrame(rows)

        weights = cross_sectional_weights(panel, signal="momentum", lookback=20, top_n=2, freq="M")
        result = run_backtest(panel, weights, init_cash=100_000)

        self.assertFalse(weights.empty)
        self.assertEqual(len(result["equity"]), len(dates))
        self.assertIn("total_return", result["metrics"])


if __name__ == "__main__":
    unittest.main()


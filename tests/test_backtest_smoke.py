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

    def test_delisted_holding_is_force_closed_not_zeroed(self):
        # A held name whose price series ends mid-backtest must be liquidated at its last
        # valid close (carried into cash), NOT silently marked to 0 by fillna(0). Otherwise
        # equity collapses to a phantom value and survivorship bias is hidden.
        dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
        rows = []
        for i, d in enumerate(dates):
            rows.append({"date": d, "code": "STAY.SZ", "open": 10.0, "close": 10.0})
            if i <= 2:  # GONE.SZ trades through 2024-01-03 (last close 20) then delists
                rows.append({"date": d, "code": "GONE.SZ", "open": 20.0, "close": 20.0})
            else:
                rows.append({"date": d, "code": "GONE.SZ", "open": float("nan"), "close": float("nan")})
        panel = pd.DataFrame(rows)
        weights = pd.DataFrame({"STAY.SZ": [0.0], "GONE.SZ": [1.0]}, index=[dates[0]])

        result = run_backtest(panel, weights, init_cash=100_000.0, cash_buffer=0.0)

        surv = result["survivorship"]
        self.assertEqual(surv["delisted_positions_closed"], 1)
        self.assertGreater(surv["delist_proceeds"], 0)
        self.assertTrue((result["trades"]["side"] == "delist").any())
        self.assertTrue(result["positions_end"].empty)  # nothing lingering as a phantom
        # Equity after delisting stays at the cashed-out value, it does not collapse to ~0.
        self.assertGreater(result["equity"].iloc[-1], 90_000.0)


if __name__ == "__main__":
    unittest.main()


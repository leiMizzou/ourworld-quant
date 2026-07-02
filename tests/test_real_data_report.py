from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.app import db
from src.research import real_data_report


class RealDataReportTest(unittest.TestCase):
    def test_unadjusted_research_is_rejected_by_default(self):
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            status = real_data_report.main(["--adjust", "none"])

        self.assertEqual(status, 2)
        self.assertIn("--adjust none", stderr.getvalue())

    def test_strict_representative_codes_rejects_tiny_adjusted_universe(self):
        dates = pd.bdate_range("2026-01-01", periods=5)
        close = pd.DataFrame(
            {
                "000001.SZ": [10, 11, 12, 13, 14],
                "600000.SH": [8, 8.1, 8.2, 8.3, 8.4],
                "300001.SZ": [20, 20.1, 20.2, 20.3, 20.4],
            },
            index=dates,
        )
        panels = {"close": close}
        long_panel = pd.DataFrame()
        stderr = io.StringIO()

        with patch("src.research.real_data_report.load_panels", return_value=(panels, long_panel)):
            with redirect_stderr(stderr):
                status = real_data_report.main(
                    [
                        "--adjust",
                        "hfq",
                        "--min-representative-codes",
                        "30",
                        "--strict-representative-codes",
                    ]
                )

        self.assertEqual(status, 2)
        self.assertIn("样本过小", stderr.getvalue())
        self.assertIn("仅加载到 3 只标的", stderr.getvalue())

    def test_predictions_are_limited_to_app_tradeable_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "app.sqlite"
            con = db.bootstrap(app_path)
            try:
                con.execute("DELETE FROM market_prices")
                con.executemany(
                    """
                    INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
                    VALUES (?, ?, 10, 9.8, 'csv', date('now'))
                    """,
                    [
                        ("000001.SZ", "平安银行"),
                        ("000002.SZ", "万科A"),
                    ],
                )
                con.commit()
            finally:
                con.close()

            pred = pd.DataFrame(
                [
                    {"code": "920001.SH", "prediction": 0.10},
                    {"code": "000001.SZ", "prediction": 0.05},
                    {"code": "600000.SH", "prediction": 0.04},
                    {"code": "000002.SZ", "prediction": 0.03},
                ]
            )

            eligible = real_data_report.app_tradeable_codes(app_path)
            filtered = real_data_report.filter_predictions_for_app_market(pred, eligible, top_n=2)

        self.assertEqual(list(filtered["code"]), ["000001.SZ", "000002.SZ"])


if __name__ == "__main__":
    unittest.main()

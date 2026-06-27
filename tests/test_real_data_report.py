from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import pandas as pd

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


if __name__ == "__main__":
    unittest.main()

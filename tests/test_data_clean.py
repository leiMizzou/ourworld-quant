from __future__ import annotations

import unittest

import pandas as pd

from src.data.clean import quality_report, standardize_bars
from src.data.utils import normalize_code


class DataCleanTest(unittest.TestCase):
    def test_normalize_code(self):
        self.assertEqual(normalize_code("1"), "000001.SZ")
        self.assertEqual(normalize_code("sh600000"), "600000.SH")
        self.assertEqual(normalize_code("430047"), "430047.BJ")

    def test_standardize_bars_drops_bad_rows_and_duplicates(self):
        raw = pd.DataFrame(
            [
                {
                    "code": "000001.SZ",
                    "date": "2024-01-02",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 1000,
                    "adjust": "hfq",
                    "source": "test",
                },
                {
                    "code": "000001.SZ",
                    "date": "2024-01-02",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 1000,
                    "adjust": "hfq",
                    "source": "test",
                },
                {
                    "code": "000001.SZ",
                    "date": "bad",
                    "open": 0,
                    "high": 0,
                    "low": 0,
                    "close": 0,
                    "volume": 0,
                    "amount": 0,
                    "adjust": "hfq",
                    "source": "test",
                },
            ]
        )
        cleaned = standardize_bars(raw)
        report = quality_report(cleaned)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(report["dup_rows"], 0)
        self.assertEqual(report["nonpos_close"], 0)


if __name__ == "__main__":
    unittest.main()


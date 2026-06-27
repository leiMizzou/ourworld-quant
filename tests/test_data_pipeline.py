from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.data import config, pipeline, storage


class DataPipelineTest(unittest.TestCase):
    def test_latest_date_returns_none_for_missing_adjust_without_duckdb_aggregate_bug(self):
        stock_basic = pd.DataFrame(
            [("600532.SH", "测试股票", "", "", "L", "test")],
            columns=["code", "name", "list_date", "delist_date", "status", "source"],
        )
        bars = pd.DataFrame(
            [
                {
                    "code": "600532.SH",
                    "date": "2026-01-02",
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "volume": 1000,
                    "amount": 10000,
                    "adjust": "none",
                    "source": "test",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "DB_PATH", Path(tmp) / "market.duckdb"):
                storage.init_db()
                storage.upsert_stock_basic(stock_basic)
                storage.upsert_bars(bars)

                self.assertIsNone(storage.latest_date("600532.SH", "hfq"))
                self.assertEqual(str(storage.latest_date("600532.SH", "none")), "2026-01-02")

    def test_representative_universe_avoids_code_prefix_limit_bias(self):
        stock_basic = pd.DataFrame(
            [
                ("000001.SZ", "深主 1", "", "", "L", "test"),
                ("000002.SZ", "深主 2", "", "", "L", "test"),
                ("300001.SZ", "创业 1", "", "", "L", "test"),
                ("300002.SZ", "创业 2", "", "", "L", "test"),
                ("600000.SH", "沪主 1", "", "", "L", "test"),
                ("600001.SH", "沪主 2", "", "", "L", "test"),
                ("688001.SH", "科创 1", "", "", "L", "test"),
                ("688002.SH", "科创 2", "", "", "L", "test"),
                ("430047.BJ", "北交 1", "", "", "L", "test"),
                ("830001.BJ", "北交 2", "", "", "L", "test"),
                ("000003.SZ", "退深", "", "", "D", "test"),
                ("600002.SH", "退沪", "", "", "D", "test"),
            ],
            columns=["code", "name", "list_date", "delist_date", "status", "source"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "DB_PATH", Path(tmp) / "market.duckdb"):
                storage.init_db()
                storage.upsert_stock_basic(stock_basic)

                ordered = pipeline.codes_from_db(limit=4, status="L", universe_mode="ordered")
                representative = pipeline.codes_from_db(limit=7, status="all", universe_mode="representative")

        self.assertEqual(ordered, ["000001.SZ", "000002.SZ", "300001.SZ", "300002.SZ"])
        self.assertEqual(len(representative), 7)
        self.assertTrue(any(code.endswith(".BJ") for code in representative))
        self.assertTrue(any(code.startswith("688") for code in representative))
        self.assertTrue(any(code in {"000003.SZ", "600002.SH"} for code in representative))
        self.assertFalse(all(code.startswith(("000", "300")) for code in representative))

    def test_codes_from_csv_normalizes_and_deduplicates_prediction_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "predictions.csv"
            path.write_text(
                "date,code,score\n"
                "2026-01-03,000001.sz,0.2\n"
                "2026-01-03,000001.SZ,0.1\n"
                "2026-01-03,600000.SH,0.3\n",
                encoding="utf-8",
            )

            codes = pipeline.codes_from_csv(path)

        self.assertEqual(codes, ["000001.SZ", "600000.SH"])


if __name__ == "__main__":
    unittest.main()

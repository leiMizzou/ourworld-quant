from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.data import config as data_config
from src.data import storage
from src.app import data_bridge, db


class DataBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.con = db.bootstrap(Path(self.tmpdir.name) / "app.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmpdir.cleanup()

    def test_sync_market_from_csv_upserts_prices(self):
        csv_path = Path(self.tmpdir.name) / "market.csv"
        csv_path.write_text(
            "code,name,price,prev_close,as_of\n"
            "000001.SZ,平安银行,11.2,10.9,2026-06-23\n"
            "600000.SH,浦发银行,8.1,8.0,2026-06-23\n",
            encoding="utf-8",
        )

        count = data_bridge.sync_market_from_csv(self.con, csv_path)
        rows = self.con.execute(
            "SELECT code, name, price, prev_close, source, as_of FROM market_prices WHERE code IN ('000001.SZ','600000.SH') ORDER BY code"
        ).fetchall()

        self.assertEqual(count, 2)
        self.assertEqual(rows[0]["price"], 11.2)
        self.assertEqual(rows[0]["source"], "csv")
        self.assertEqual(rows[1]["name"], "浦发银行")

    def test_sync_market_from_csv_can_replace_demo_prices(self):
        csv_path = Path(self.tmpdir.name) / "market.csv"
        csv_path.write_text(
            "code,name,price,prev_close,as_of\n"
            "000001.SZ,平安银行真实,10.71,10.65,2026-06-23\n",
            encoding="utf-8",
        )

        count = data_bridge.sync_market_from_csv(self.con, csv_path, replace=True)
        rows = self.con.execute("SELECT code, name, source FROM market_prices ORDER BY code").fetchall()

        self.assertEqual(count, 1)
        self.assertEqual([(r["code"], r["name"], r["source"]) for r in rows], [("000001.SZ", "平安银行真实", "csv")])

    def test_demo_seed_does_not_overwrite_imported_prices(self):
        csv_path = Path(self.tmpdir.name) / "market.csv"
        csv_path.write_text(
            "code,name,price,prev_close,as_of\n"
            "000001.SZ,平安银行CSV,11.2,10.9,2026-06-23\n",
            encoding="utf-8",
        )
        data_bridge.sync_market_from_csv(self.con, csv_path)

        db.seed_demo_market(self.con)
        row = self.con.execute("SELECT name, price, source FROM market_prices WHERE code='000001.SZ'").fetchone()

        self.assertEqual(row["name"], "平安银行CSV")
        self.assertEqual(row["price"], 11.2)
        self.assertEqual(row["source"], "csv")

    def test_sync_market_from_pasted_csv_text(self):
        count = data_bridge.sync_market_from_csv_text(
            self.con,
            "code,name,price,prev_close,as_of\n"
            "000001.SZ,平安银行文本,12.1,11.9,2026-06-24\n",
        )
        row = self.con.execute(
            "SELECT name, price, prev_close, source, as_of FROM market_prices WHERE code='000001.SZ'"
        ).fetchone()

        self.assertEqual(count, 1)
        self.assertEqual(row["name"], "平安银行文本")
        self.assertEqual(row["price"], 12.1)
        self.assertEqual(row["prev_close"], 11.9)
        self.assertEqual(row["source"], "csv_text")
        self.assertEqual(row["as_of"], "2026-06-24")

    def test_sync_market_from_pasted_csv_text_rejects_empty_text(self):
        with self.assertRaisesRegex(data_bridge.MarketSyncError, "CSV 内容为空"):
            data_bridge.sync_market_from_csv_text(self.con, "")

    def test_sync_market_from_csv_rejects_missing_file(self):
        with self.assertRaisesRegex(data_bridge.MarketSyncError, "CSV 不存在"):
            data_bridge.sync_market_from_csv(self.con, Path(self.tmpdir.name) / "missing.csv")

    def test_sync_market_from_quant_db_limit_uses_stable_hash_order_not_code_prefix(self):
        market_db = Path(self.tmpdir.name) / "market.duckdb"
        codes = ["000001.SZ", "000002.SZ", "300001.SZ", "600000.SH"]
        stock_basic = pd.DataFrame(
            [(code, code, "", "", "L", "test") for code in codes],
            columns=["code", "name", "list_date", "delist_date", "status", "source"],
        )
        bars = pd.DataFrame(
            [
                {
                    "code": code,
                    "date": date,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1000,
                    "amount": 10000,
                    "adjust": "none",
                    "source": "test",
                }
                for code_index, code in enumerate(codes)
                for date, price in [
                    ("2026-01-02", 10 + code_index),
                    ("2026-01-03", 11 + code_index),
                ]
            ]
        )

        with patch.object(data_config, "DB_PATH", market_db):
            storage.init_db()
            storage.upsert_stock_basic(stock_basic)
            storage.upsert_bars(bars)
            with storage.connect(read_only=True) as duck:
                expected = {
                    row[0]
                    for row in duck.execute(
                        """
                        SELECT code
                        FROM daily_bars
                        WHERE adjust='none' AND date='2026-01-03'
                        ORDER BY hash(code), code
                        LIMIT 2
                        """
                    ).fetchall()
                }
            count = data_bridge.sync_market_from_quant_db(self.con, adjust="none", limit=2, replace=True)

        rows = self.con.execute("SELECT code FROM market_prices").fetchall()
        self.assertEqual(count, 2)
        self.assertEqual({row["code"] for row in rows}, expected)

    def test_sync_market_from_quant_db_prioritizes_included_codes_within_limit(self):
        market_db = Path(self.tmpdir.name) / "market.duckdb"
        codes = ["000001.SZ", "000002.SZ", "300001.SZ", "600000.SH"]
        stock_basic = pd.DataFrame(
            [(code, code, "", "", "L", "test") for code in codes],
            columns=["code", "name", "list_date", "delist_date", "status", "source"],
        )
        bars = pd.DataFrame(
            [
                {
                    "code": code,
                    "date": date,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1000,
                    "amount": 10000,
                    "adjust": "none",
                    "source": "test",
                }
                for code_index, code in enumerate(codes)
                for date, price in [
                    ("2026-01-02", 10 + code_index),
                    ("2026-01-03", 11 + code_index),
                ]
            ]
        )

        with patch.object(data_config, "DB_PATH", market_db):
            storage.init_db()
            storage.upsert_stock_basic(stock_basic)
            storage.upsert_bars(bars)
            with storage.connect(read_only=True) as duck:
                stable_order = [
                    row[0]
                    for row in duck.execute(
                        """
                        SELECT code
                        FROM daily_bars
                        WHERE adjust='none' AND date='2026-01-03'
                        ORDER BY hash(code), code
                        """
                    ).fetchall()
                ]
            priority_code = stable_order[-1]
            count = data_bridge.sync_market_from_quant_db(
                self.con,
                adjust="none",
                limit=2,
                replace=True,
                include_codes=[priority_code],
            )

        rows = self.con.execute("SELECT code FROM market_prices").fetchall()
        self.assertEqual(count, 2)
        self.assertIn(priority_code, {row["code"] for row in rows})

    def test_codes_from_csv_normalizes_and_deduplicates_codes(self):
        csv_path = Path(self.tmpdir.name) / "predictions.csv"
        csv_path.write_text(
            "date,code,score\n"
            "2026-01-03,000001.sz,0.2\n"
            "2026-01-03,000001.SZ,0.1\n"
            "2026-01-03,600000.SH,0.3\n",
            encoding="utf-8",
        )

        codes = data_bridge.codes_from_csv(csv_path)

        self.assertEqual(codes, ["000001.SZ", "600000.SH"])


if __name__ == "__main__":
    unittest.main()

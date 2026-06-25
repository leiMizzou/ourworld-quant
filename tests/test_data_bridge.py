from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

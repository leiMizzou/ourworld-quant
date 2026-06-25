"""并发写入安全测试。

回归点:此前 ThreadingHTTPServer 的所有线程共享同一个 sqlite 连接
(AppHandler.con,check_same_thread=False),并发的多语句事务会相互交错,
一个线程的 commit 可能把另一个线程写了一半的订单刷盘(扣了现金却没记持仓)。
修复方式是每个请求开自己的连接(server.AppHandler.setup/finish + db_path)。

本文件验证:
1. 并发写入者(各自独立连接)不会破坏模拟盘账本的一致性;
2. 设置 db_path 后,运行中的服务确实按"每请求一连接"工作,既能并发只读、
   也能把写入正确落库。
"""
from __future__ import annotations

import http.client
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from src.app import db, services
from src.app.server import DB_WRITE_LOCK, AppHandler


class ConcurrentLedgerTest(unittest.TestCase):
    """直接在服务层用多连接并发下单,断言账本守恒。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmpdir.name) / "app.sqlite"
        self.con = db.bootstrap(self.db_file)
        token = services.create_wechat_session(self.con)
        self.user_id = services.confirm_wechat_session(self.con, token, "并发用户")

    def tearDown(self):
        self.con.close()
        self.tmpdir.cleanup()

    def _place_orders(self, n_orders: int, successes: list, lock: threading.Lock):
        # 每个线程一条独立连接,模拟"每请求一连接"模型;并按服务端 do_POST 的契约
        # 持有 DB_WRITE_LOCK 串行化写入,使 place_order 的读-改-写保持原子。
        con = db.connect(self.db_file)
        try:
            local = 0
            for _ in range(n_orders):
                for _attempt in range(5):  # busy_timeout 已足够,这里只防极少数锁等待
                    try:
                        with DB_WRITE_LOCK:
                            services.place_order(con, self.user_id, "000001.SZ", "buy", 100)
                        local += 1
                        break
                    except sqlite3.OperationalError:
                        continue
            with lock:
                successes.append(local)
        finally:
            con.close()

    def test_concurrent_orders_keep_ledger_consistent(self):
        n_threads, per_thread = 10, 5
        successes: list[int] = []
        lock = threading.Lock()
        threads = [
            threading.Thread(target=self._place_orders, args=(per_thread, successes, lock))
            for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total_success = sum(successes)
        # 期望全部成功(1,000,000 本金远够 50 笔 ~1082 元的订单)。
        self.assertEqual(total_success, n_threads * per_thread)

        check = db.connect(self.db_file)
        try:
            order_count = len(services.recent_orders(check, self.user_id, limit=10_000))
            snap = services.portfolio_snapshot(check, self.user_id)
        finally:
            check.close()

        holdings_qty = snap["holdings"][0]["qty"] if snap["holdings"] else 0
        # 核心一致性不变量:订单数 == 成交数,且每笔订单都恰好落了 100 股持仓。
        # 旧的共享连接 bug 会丢订单行或丢持仓更新,使下面两条断言失败。
        self.assertEqual(order_count, total_success)
        self.assertEqual(holdings_qty, 100 * total_success)
        # 现金被扣减且未透支(扣了现金却没记持仓的腐蚀会破坏这个关系)。
        self.assertLess(snap["cash"], services.INITIAL_CASH)
        self.assertGreater(snap["cash"], 0)


class PerRequestConnectionTest(unittest.TestCase):
    """通过真实 HTTP 验证服务端按"每请求一连接"工作。"""

    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_PROVIDER": "",
                "OWQ_EMAIL_FROM": "",
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "",
            },
            clear=False,
        )
        self.env_patcher.start()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmpdir.name) / "app.sqlite"
        self.con = db.bootstrap(self.db_file)
        AppHandler.con = self.con
        # 关键:设置 db_path 让每个请求开自己的连接(被测的生产路径)。
        AppHandler.db_path = self.db_file
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        AppHandler.db_path = None  # 不要把 db_path 泄漏给其它测试
        self.con.close()
        self.tmpdir.cleanup()
        self.env_patcher.stop()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, payload

    def test_concurrent_reads_succeed_on_per_request_connections(self):
        results: list[int] = []
        lock = threading.Lock()

        def hit():
            status, _ = self._request("GET", "/livez")
            with lock:
                results.append(status)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 每请求独立连接;20 个并发只读请求都应成功,不会出现跨线程连接错误。
        self.assertEqual(len(results), 20)
        self.assertTrue(all(status == 200 for status in results), results)

    def test_write_through_per_request_connection_is_persisted(self):
        # 注册起始流程会写入一条 email_login_session(经由每请求连接)。
        status, _ = self._request("GET", "/register")
        self.assertEqual(status, 200)
        status, payload = self._request(
            "POST",
            "/register",
            body=urlencode({"email": "concurrency@example.com", "accept_terms": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 200)
        self.assertIn("测试邮箱验证链接已生成", payload)
        self.assertIsNotNone(re.search(r"/auth/email/confirm\?token=", payload))

        # 用一条全新连接确认写入已落库(每请求连接 + WAL 对其它连接可见)。
        check = db.connect(self.db_file)
        try:
            count = check.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0]
        finally:
            check.close()
        self.assertGreaterEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

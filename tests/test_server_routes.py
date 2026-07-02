from __future__ import annotations

import csv
import http.client
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import urllib.error
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlencode
from unittest.mock import patch

from src.app import db, services
from src.app import server as app_server
from src.app.server import AppHandler, RATE_LIMIT_BUCKETS, SECRET, csrf_token, reset_http_metrics, sign_user, verify_cookie


class ServerRoutesTest(unittest.TestCase):
    def setUp(self):
        RATE_LIMIT_BUCKETS.clear()
        reset_http_metrics()
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
        self.con = db.bootstrap(Path(self.tmpdir.name) / "app.sqlite")
        AppHandler.con = self.con
        # These routes assert state via self.con, so keep the handler on the shared
        # connection; ensure no other test left a per-request db_path on the class.
        AppHandler.db_path = None
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), AppHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.con.close()
        self.tmpdir.cleanup()
        self.env_patcher.stop()

    def request(self, method: str, path: str, body: str | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8", errors="replace")
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def start_registration(self, email: str = "route@example.com", headers: dict | None = None):
        status, _, intro = self.request("GET", "/register", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("注册后先完成 3 分钟学习闭环", intro)
        self.assertIn("第一屏先懂一句话,再点蓝色推荐按钮", intro)
        self.assertIn("点预设目标", intro)
        self.assertIn("现在只做一件事:收注册码", intro)
        self.assertIn("现在填什么</b><p>只填邮箱,再勾选同意条款", intro)
        self.assertIn("点完去哪</b><p>去邮箱收 8 位注册码", intro)
        self.assertIn("还不用做什么</b><p>不用配置 AI key,不用看模拟盘,不用自己写提示词", intro)
        self.assertIn("下一步:输入注册码并设置密码", intro)
        self.assertIn("3/6、4/6、5/6、6/6", intro)
        self.assertIn('id="email-register-form"', intro)
        self.assertIn('name="accept_terms"', intro)
        self.assertIn('name="email"', intro)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机注册下一步提示">', intro)
        self.assertIn("<span>现在只做一件事</span><b>填写邮箱并发送注册码</b>", intro)
        self.assertIn('<a class="btn blue" href="#email-register-form">去填写</a>', intro)

        post_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            post_headers.update(headers)
        status, _, payload = self.request(
            "POST",
            "/register",
            body=urlencode({"email": email, "accept_terms": "1"}),
            headers=post_headers,
        )
        self.assertEqual(status, 200)
        self.assertIn("测试邮箱验证链接已生成", payload)
        self.assertIn("现在去邮箱复制 8 位注册码", payload)
        self.assertIn("不用重新注册,也不用找模拟盘入口", payload)
        self.assertIn("现在做什么</b><span>去邮箱复制 8 位数字注册码", payload)
        self.assertIn("回来填哪里</b><span>点击“输入注册码”", payload)
        self.assertIn("完成后去哪</b><span>设置密码后自动进入学习工作台", payload)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机邮箱验证下一步提示">', payload)
        self.assertIn("<span>下一步</span><b>输入邮箱里的 8 位注册码</b>", payload)
        self.assertIn('<a class="btn blue" href="/auth/email/confirm">去输入</a>', payload)
        match = re.search(r"/auth/email/confirm\?token=([^\"&]+)", payload)
        self.assertIsNotNone(match)
        return match.group(1), payload

    def extract_dev_code(self, payload: str) -> str:
        match = re.search(r"<code>(\d{8})</code>", payload)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_healthz_reports_readiness_without_login(self):
        status, headers, payload = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        body = json.loads(payload)
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["ok"])
        self.assertEqual(body["required_warnings"], 0)
        self.assertGreaterEqual(len(body["checks"]), 1)

        status, headers, payload = self.request("HEAD", "/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(payload, "")

    def test_public_healthz_and_readyz_return_summary_without_internal_details(self):
        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, _, health_payload = self.request("GET", "/healthz", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 503)
            health = json.loads(health_payload)
            self.assertEqual(health["status"], "degraded")
            self.assertIn("required_warnings", health)
            self.assertIn("warnings", health)
            self.assertNotIn("checks", health)
            self.assertNotIn("users", health_payload)
            self.assertNotIn("SQLite", health_payload)

            status, _, ready_payload = self.request("GET", "/readyz", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 503)
            ready = json.loads(ready_payload)
            self.assertTrue(ready["strict"])
            self.assertIn("warnings", ready)
            self.assertNotIn("checks", ready)
            self.assertTrue(any(item["name"] in {"email_login", "email_sending", "email_dev_auth_public"} for item in ready["warnings"]))
            self.assertNotIn("需要配置 Cloudflare", ready_payload)

    def test_usage_guide_and_demo_are_public(self):
        status, headers, guide = self.request("GET", "/guide")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)
        self.assertIn("网站使用流程", guide)
        self.assertIn("当前不足", guide)
        self.assertIn("本次优化", guide)
        self.assertIn("/guide/demo", guide)
        self.assertIn("邮箱注册", guide)
        self.assertIn("选择预设学习目标", guide)
        self.assertIn("生成今日练习", guide)
        self.assertIn("保存三问复盘", guide)
        self.assertIn("设置密码后先完成第一次学习闭环", guide)
        self.assertNotIn("设置密码并进入模拟盘", guide)

        missing_voice = Path(self.tmpdir.name) / "missing-guide.mp3"
        with patch.dict(os.environ, {"OWQ_DEMO_VOICE_PATH": str(missing_voice)}, clear=False):
            status, headers, demo = self.request("GET", "/guide/demo")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)
        self.assertIn("自动演示", demo)
        self.assertIn("EdgeTTS 语音解说", demo)
        self.assertIn("--generate-demo-voice", demo)
        self.assertIn("demo-frame", demo)
        self.assertIn("选择预设学习目标", demo)
        self.assertIn("生成今日练习", demo)
        self.assertIn("生成观察记录", demo)
        self.assertIn("保存三问复盘", demo)
        self.assertIn("第一次学习闭环", demo)
        self.assertNotIn("模拟交易、组合设计、公开展示和论坛复盘", demo)

        status, _, sitemap = self.request("GET", "/sitemap.xml")
        self.assertEqual(status, 200)
        self.assertIn("/guide", sitemap)
        self.assertIn("/guide/demo", sitemap)
        self.assertIn("/learn/demo", sitemap)

    def test_usage_demo_audio_serves_generated_edge_tts_file(self):
        voice_path = Path(self.tmpdir.name) / "guide.mp3"
        voice_path.write_bytes(b"ID3demo")
        with patch.dict(os.environ, {"OWQ_DEMO_VOICE_PATH": str(voice_path)}, clear=False):
            status, headers, payload = self.request("GET", "/guide/demo/audio.mp3")

        self.assertEqual(status, 200)
        self.assertIn("audio/mpeg", headers.get("Content-Type", ""))
        self.assertEqual(payload, "ID3demo")

    def test_generate_demo_voice_uses_edge_tts_command(self):
        out_path = Path(self.tmpdir.name) / "generated.mp3"
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            self.assertTrue(kwargs["check"])
            media_path = Path(cmd[cmd.index("--write-media") + 1])
            media_path.write_bytes(b"ID3generated")

        with patch("src.app.server.edge_tts_command", return_value=["edge-tts"]):
            with patch("src.app.server.subprocess.run", side_effect=fake_run):
                result = app_server.generate_usage_demo_voice(out_path, voice="zh-CN-TestNeural")

        self.assertEqual(result, out_path)
        self.assertEqual(out_path.read_bytes(), b"ID3generated")
        self.assertIn("zh-CN-TestNeural", seen["cmd"])
        self.assertIn("--write-media", seen["cmd"])
        narration = seen["cmd"][seen["cmd"].index("--text") + 1]
        self.assertIn("AI 量化学习工作台", narration)
        self.assertIn("生成一条今日练习", narration)
        self.assertIn("三问复盘", narration)

    def test_public_healthz_detail_requires_ops_token(self):
        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app", "OWQ_HEALTH_DETAIL_TOKEN": "ops-token"}, clear=False):
            status, _, public_payload = self.request("GET", "/healthz", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 503)
            self.assertNotIn("checks", json.loads(public_payload))

            status, _, detailed_payload = self.request(
                "GET",
                "/healthz",
                headers={"Host": "quant.ourworlds.app", "X-OWQ-Health-Token": "ops-token"},
            )
            self.assertEqual(status, 503)
            detailed = json.loads(detailed_payload)
            self.assertIn("checks", detailed)
            self.assertIn("app_db", detailed_payload)

    def test_livez_reports_process_and_database_liveness(self):
        status, headers, payload = self.request("GET", "/livez")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")
        body = json.loads(payload)
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["ok"])
        self.assertEqual(body["database"], "ok")
        self.assertGreaterEqual(body["uptime_seconds"], 0)
        self.assertNotIn("required_warnings", body)

        status, headers, payload = self.request("HEAD", "/livez")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertEqual(payload, "")

    def test_readyz_is_strict_and_blocks_public_beta_warnings(self):
        status, headers, payload = self.request("GET", "/readyz")

        self.assertEqual(status, 503)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        body = json.loads(payload)
        self.assertFalse(body["ok"])
        self.assertTrue(body["strict"])
        self.assertEqual(body["required_warnings"], 0)
        self.assertGreaterEqual(body["optional_warnings"], 1)

        status, headers, payload = self.request("HEAD", "/readyz")
        self.assertEqual(status, 503)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(payload, "")

    def test_metrics_reports_aggregate_runtime_counters(self):
        self.request("GET", "/healthz")
        self.request("GET", "/missing-route")
        status, headers, payload = self.request("GET", "/metrics")

        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        body = json.loads(payload)
        self.assertEqual(body["status"], "ok")
        self.assertGreaterEqual(body["uptime_seconds"], 0)
        self.assertGreaterEqual(body["requests_total"], 2)
        self.assertGreaterEqual(body["in_flight"], 1)
        self.assertGreaterEqual(body["by_method"].get("GET", 0), 2)
        self.assertGreaterEqual(body["by_status"].get("200", 0), 1)
        self.assertGreaterEqual(body["by_status"].get("404", 0), 1)
        self.assertIn("2xx", body["by_status_class"])
        self.assertIn("4xx", body["by_status_class"])
        self.assertNotIn("route@example.com", payload)
        self.assertNotIn("127.0.0.1", payload)

        status, headers, payload = self.request("HEAD", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(payload, "")

    def test_public_metrics_returns_summary_without_runtime_counters(self):
        self.request("GET", "/healthz")
        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, headers, payload = self.request("GET", "/metrics", headers={"Host": "quant.ourworlds.app"})

        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        body = json.loads(payload)
        self.assertEqual(body, {"status": "ok", "detail": "summary"})
        self.assertNotIn("requests_total", payload)
        self.assertNotIn("by_status", payload)

    def test_public_metrics_detail_requires_ops_token(self):
        with patch.dict(
            os.environ,
            {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app", "OWQ_HEALTH_DETAIL_TOKEN": "ops-token"},
            clear=False,
        ):
            status, _, public_payload = self.request("GET", "/metrics", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 200)
            self.assertNotIn("requests_total", json.loads(public_payload))

            status, _, detailed_payload = self.request(
                "GET",
                "/metrics",
                headers={"Host": "quant.ourworlds.app", "X-OWQ-Health-Token": "ops-token"},
            )

        self.assertEqual(status, 200)
        detailed = json.loads(detailed_payload)
        self.assertEqual(detailed["status"], "ok")
        self.assertIn("requests_total", detailed)
        self.assertIn("by_status", detailed)

    def test_root_serves_public_landing_page(self):
        status, headers, payload = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertNotIn("X-Robots-Tag", headers)
        self.assertIn("OurWorlds Quant Arena", payload)
        self.assertIn("AI quant learning workspace", payload)
        self.assertIn("先学会一圈，再看模拟盘", payload)
        self.assertIn("先体验 3 分钟学习闭环", payload)
        self.assertIn("第一次先不要研究榜单、参数或下单", payload)
        self.assertIn("学习闭环", payload)
        self.assertIn("第一次学习闭环", payload)
        self.assertIn("First learning loop", payload)
        self.assertIn("3-5 MIN", payload)
        self.assertIn("理解一个量化概念", payload)
        self.assertIn("选择一个学习目标", payload)
        self.assertIn("生成一条模拟练习", payload)
        self.assertIn("保存三问复盘", payload)
        self.assertIn("第一圈怎么走", payload)
        self.assertIn("先看 3 分钟示例,知道 AI 教练会怎样拆解目标", payload)
        self.assertIn("注册后点蓝色按钮,生成 1 条小数量模拟练习", payload)
        self.assertIn("最后保存三问复盘,看到 6/6 就完成第一圈", payload)
        self.assertIn("先完成第一圈，再看赛场", payload)
        self.assertNotIn("当前赛场", payload)
        self.assertNotIn("当前榜首收益", payload)
        self.assertIn("策略论坛", payload)
        self.assertNotIn("微信扫码注册", payload)
        self.assertIn("注册进入学习工作台", payload)
        self.assertIn("先进入学习工作台完成第一次学习闭环", payload)
        self.assertIn("3 分钟学习体验", payload)
        self.assertIn("免登录看完目标、教练拆解、模拟练习和复盘问题", payload)
        self.assertLess(
            payload.index('<a class="btn blue" href="/learn/demo">先体验 3 分钟学习闭环</a>'),
            payload.index('<a class="btn" href="/register">注册进入学习工作台</a>'),
        )
        self.assertLess(
            payload.index('<a class="link-tile" href="/learn/demo"><strong>3 分钟学习体验</strong>'),
            payload.index('<a class="link-tile" href="/register"><strong>注册进入学习工作台</strong>'),
        )
        self.assertIn('/data-status', payload)

        status, headers, payload = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(payload, "")

    def test_public_landing_cta_switches_to_support_when_registration_is_closed(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "",
                "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
                "OWQ_EMAIL_PROVIDER": "",
                "OWQ_EMAIL_FROM": "",
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "",
            },
            clear=False,
        ):
            status, _, payload = self.request("GET", "/", headers={"Host": "quant.ourworlds.app"})

        self.assertEqual(status, 200)
        self.assertIn("申请加入", payload)
        self.assertIn('href="/support"', payload)
        self.assertIn("当前新用户注册暂未开放", payload)
        self.assertIn("先体验 3 分钟学习闭环", payload)
        self.assertIn("注册暂未开放时,也可以先免登录看完第一次闭环", payload)
        self.assertLess(
            payload.index('<a class="btn blue" href="/learn/demo">先体验 3 分钟学习闭环</a>'),
            payload.index('<a class="btn" href="/support">申请加入</a>'),
        )
        self.assertNotIn("邮箱验证注册", payload)
        self.assertNotIn('href="/register">邮箱注册', payload)

    def test_head_public_and_auth_pages_are_available(self):
        for path in ["/register", "/forgot-password", "/login", "/showcase/public", "/forum", "/terms", "/privacy", "/risk", "/support"]:
            with self.subTest(path=path):
                status, headers, payload = self.request("HEAD", path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers.get("Content-Type", ""))
                self.assertEqual(payload, "")
                if path in {"/register", "/forgot-password", "/login", "/support"}:
                    self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")
                else:
                    self.assertNotIn("X-Robots-Tag", headers)

    def test_public_https_responses_include_strict_security_headers(self):
        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, headers, _ = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Strict-Transport-Security"), "max-age=15552000")
        csp = headers.get("Content-Security-Policy", "")
        # Progressive enhancement: scripts are served from /static (same-origin) only.
        # 'self' WITHOUT 'unsafe-inline' keeps inline scripts and inline event handlers
        # blocked, so injected markup still cannot execute JS. Lock that in here.
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src", 1)[1].split(";", 1)[0])
        self.assertIn("object-src 'none'", csp)
        self.assertIn("upgrade-insecure-requests", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_static_assets_serve_and_block_traversal(self):
        status, headers, payload = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", headers.get("Content-Type", ""))
        self.assertIn("OurWorlds Quant", payload)
        # Traversal / absolute / disallowed-extension are all rejected before disk access.
        for bad in ["/static/../app/server.py", "/static/..%2fserver.py", "/static/nope.js", "/static/app.py"]:
            with self.subTest(path=bad):
                status, _, _ = self.request("GET", bad)
                self.assertEqual(status, 404)

    def test_glossary_api_returns_metric_definitions(self):
        status, headers, payload = self.request("GET", "/api/glossary")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        data = json.loads(payload)
        self.assertIn("metrics", data)
        for key in ("return_pct", "sharpe", "max_drawdown"):
            self.assertIn(key, data["metrics"])
            self.assertTrue(data["metrics"][key]["band"])  # conservative guidance present

    def test_glossary_page_guides_to_learning_before_simulator(self):
        status, _, payload = self.request("GET", "/glossary")

        self.assertEqual(status, 200)
        self.assertIn("术语表", payload)
        self.assertIn("先体验学习闭环", payload)
        self.assertIn("高级模拟盘(看完术语后再看)", payload)
        self.assertNotIn(">进入模拟盘</a>", payload)
        self.assertLess(payload.index("先体验学习闭环"), payload.index("高级模拟盘(看完术语后再看)"))

    def test_dashboard_metric_labels_have_no_js_fallback(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "MetricLabel")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        status, _, payload = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        # The metric is interactive (data-metric hook) AND carries a title fallback that
        # works with JS disabled, and the global enhancement script is wired in.
        self.assertIn('data-metric="return_pct"', payload)
        self.assertIn("title=", payload)
        self.assertIn('/static/app.js', payload)
        # Provenance chip (server-rendered, works without JS) tells the user this is their
        # simulated account priced off demo / real-but-non-realtime data.
        self.assertIn("你的模拟训练账户", payload)
        self.assertIn("行情:", payload)
        # Weekly-review retention card (Phase 3) with its review-post CTA.
        self.assertIn("本周复盘", payload)
        self.assertIn("/forum/new?template=performance", payload)

    def test_equity_curve_api_and_dashboard_container(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "EquityCurve")
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)  # extra snapshot
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, headers, payload = self.request("GET", "/api/equity-curve", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        data = json.loads(payload)
        self.assertIn("points", data)
        self.assertGreaterEqual(len(data["points"]), 1)
        for pt in data["points"]:
            self.assertIn("date", pt)
            self.assertIn("equity", pt)
        # Dashboard ships the (hidden) container that app.js fills; no-JS users keep the cards.
        _, _, dash = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertIn("data-equity-curve", dash)
        self.assertIn("data-equity-section", dash)

    def test_preview_page_is_public_indexable_and_no_js_safe(self):
        payload_json = {
            "as_of": "2026-06-26",
            "n_codes": 618,
            "metrics": {"total_return": -0.4325, "cagr": -0.1561, "sharpe": -0.628, "max_drawdown": -0.5185},
            "equity_points": [
                {"date": "2023-01-03", "equity": 1000000.0},
                {"date": "2024-01-03", "equity": 820000.0},
                {"date": "2025-01-03", "equity": 700000.0},
                {"date": "2026-06-26", "equity": 567488.89},
            ],
            "survivorship": {
                "n_delisted": 49, "n_survivors": 569, "n_full": 618,
                "full": {"total_return": -0.4325, "sharpe": -0.628},
                "survivors_only": {"total_return": -0.1081, "sharpe": -0.028},
                "delta_survivors_minus_full": {"total_return": 0.3244, "sharpe": 0.6},
            },
        }
        preview_file = Path(self.tmpdir.name) / "preview.json"
        preview_file.write_text(json.dumps(payload_json), encoding="utf-8")
        with patch.dict(os.environ, {"OWQ_PREVIEW_JSON": str(preview_file)}, clear=False):
            status, headers, payload = self.request("GET", "/preview")  # no login

        self.assertEqual(status, 200)
        # Public + indexable (it's an acquisition page; must NOT be noindexed).
        self.assertNotIn("X-Robots-Tag", headers)
        # Honesty badge + survivorship teaching + server-rendered SVG all present without JS.
        self.assertIn("真实历史 A 股数据", payload)
        self.assertIn("幸存者偏差", payload)
        self.assertIn("<svg", payload)
        self.assertIn('data-metric="sharpe"', payload)
        self.assertIn("总收益被高估", payload)

    def test_learning_demo_is_public_and_runs_without_key(self):
        status, headers, payload = self.request("GET", "/learn/demo?preset=2")  # no login, no key

        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)
        self.assertIn("3 分钟体验一次 AI 量化学习闭环", payload)
        self.assertIn("免 DeepSeek key", payload)
        self.assertIn("AI 能帮我做什么?", payload)
        self.assertIn("真实学习页会一路告诉你下一步", payload)
        self.assertIn("刚完成 3/6", payload)
        self.assertIn("刚完成 4/6", payload)
        self.assertIn("刚完成 5/6", payload)
        self.assertIn("完成 6/6", payload)
        self.assertIn("第一圈可以停在这里", payload)
        self.assertIn("示例教练会这样拆解", payload)
        self.assertIn("系统会生成这样的练习草稿", payload)
        self.assertIn('<table class="learning-mobile-table">', payload)
        self.assertIn('data-label="练习对象"', payload)
        self.assertIn('data-label="这一步学习什么"', payload)
        self.assertIn("第一次复盘只回答 3 个问题", payload)
        self.assertIn("不会写入你的模拟盘", payload)
        self.assertIn("看完后只做下一步", payload)
        self.assertIn("注册后不会把你丢进复杂模拟盘", payload)
        self.assertIn("注册后去哪里", payload)
        self.assertIn("先进入学习工作台,不是高级模拟盘", payload)
        self.assertIn("第一下点什么", payload)
        self.assertIn("点“一键开始第一关”,不用自己写提示词", payload)
        self.assertIn("不用先配置 AI key", payload)
        self.assertIn("第一圈用示例教练,不调用 DeepSeek", payload)
        self.assertIn("/register", payload)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机示例下一步提示">', payload)
        self.assertIn("<span>看完示例后</span><b>注册后创建自己的练习</b>", payload)
        self.assertIn('<a class="btn blue" href="/register">注册开始</a>', payload)
        self.assertNotIn('action="/learn/coach"', payload)
        self.assertNotIn("配置 AI 教练", payload)
        self.assertNotIn("看看公开榜单", payload)

        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "DemoLoggedIn")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        status, _, logged_in = self.request("GET", "/learn/demo?preset=2", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("回到我的学习工作台", logged_in)
        self.assertIn("看完后只回学习工作台", logged_in)
        self.assertIn("你还没完成第一次 6/6 闭环", logged_in)
        self.assertIn("不要配置 key、不要看榜单、不要进高级模拟盘", logged_in)
        self.assertIn("第一屏看什么", logged_in)
        self.assertIn("学习工作台会把下一步放在最上面", logged_in)
        self.assertIn("第一圈可以用示例教练完成,不调用 DeepSeek", logged_in)
        self.assertIn("<span>看完示例后</span><b>回到自己的学习工作台</b>", logged_in)
        self.assertIn('<a class="btn blue" href="/learn">回学习工作台</a>', logged_in)
        self.assertIn("完成 6/6 后再看高级模拟盘", logged_in)
        self.assertNotIn('href="/app"', logged_in)
        self.assertNotIn("高级模拟盘(完成第一圈后再看)", logged_in)
        self.assertNotIn(">进入模拟盘</a>", logged_in)
        self.assertNotIn("配置 AI 教练", logged_in)
        self.assertNotIn("看看公开榜单", logged_in)
        self.assertLess(logged_in.index("回到我的学习工作台"), logged_in.index("完成 6/6 后再看高级模拟盘"))

        self.con.execute(
            """
            INSERT OR REPLACE INTO market_prices(code, name, price, prev_close, source, as_of)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("000001.SZ", "平安银行", 10.0, 9.8, "demo", "2026-06-24"),
        )
        task_id = services.create_learning_task(
            self.con,
            uid,
            "我想先完成第一圈学习闭环",
            "starter",
            "reversal",
            "示例教练拆解",
        )
        services.create_practice_signals_from_learning_task(
            self.con,
            uid,
            task_id,
            "演示页完成态验证",
            "reversal",
            qty="100",
            limit=1,
        )
        signal = services.practice_signals(self.con, uid, status="pending", limit=1)[0]
        services.execute_practice_signal(self.con, uid, int(signal["id"]))
        services.save_learning_reflection(self.con, uid, int(signal["id"]), "我想练流程", "我按小数量执行", "下次先看边界")

        status, _, completed = self.request("GET", "/learn/demo?preset=2", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("看完后可以继续第二关", completed)
        self.assertIn("你已经完成过一次 6/6 闭环", completed)
        self.assertIn("高级模拟盘只用于深入查看模拟记录", completed)
        self.assertIn('<a class="btn secondary" href="/app">高级模拟盘</a>', completed)
        self.assertLess(completed.index("回到我的学习工作台"), completed.index('<a class="btn secondary" href="/app">高级模拟盘</a>'))

    def test_lessons_page_is_public_indexable_and_covers_three_pitfalls(self):
        payload_json = {
            "survivorship": {
                "n_delisted": 49,
                "full": {"total_return": -0.4325, "sharpe": -0.628},
                "survivors_only": {"total_return": -0.1081, "sharpe": -0.028},
                "delta_survivors_minus_full": {"total_return": 0.3244, "sharpe": 0.6},
            }
        }
        preview_file = Path(self.tmpdir.name) / "preview.json"
        preview_file.write_text(json.dumps(payload_json), encoding="utf-8")
        with patch.dict(os.environ, {"OWQ_PREVIEW_JSON": str(preview_file)}, clear=False):
            status, headers, payload = self.request("GET", "/lessons")  # no login

        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)  # indexable public education page
        self.assertIn("幸存者偏差", payload)
        self.assertIn("前视偏差", payload)
        self.assertIn("复权口径", payload)
        self.assertIn("被高估了", payload)  # real survivorship numbers wired in

    def test_public_pages_reflect_login_session(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "LoggedIn")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        # Logged-in user on a PUBLIC page: logged-in nav + learning-first CTA, not a signup pitch.
        _, _, prev = self.request("GET", "/preview", headers={"Cookie": cookie})
        self.assertIn("退出", prev)            # logged-in nav (logout button) present
        self.assertIn("进入学习工作台", prev)
        self.assertIn("高级模拟盘(完成第一圈后再看)", prev)
        self.assertLess(
            prev.index('<a class="btn blue" href="/learn">进入学习工作台</a>'),
            prev.index('<a class="btn secondary" href="/app">高级模拟盘(完成第一圈后再看)</a>'),
        )
        self.assertNotIn("免费注册,拿 10 万模拟本金", prev)
        # Landing page (bypasses send_html) also reflects login state.
        _, _, home = self.request("GET", "/", headers={"Cookie": cookie})
        self.assertIn("学习工作台", home)
        self.assertIn("进入学习工作台", home)
        self.assertIn("继续学习工作台", home)
        self.assertIn("一键开始第一关", home)
        self.assertIn("看 3 分钟示例", home)
        self.assertLess(
            home.index('<a class="btn blue" href="/learn">进入学习工作台</a>'),
            home.index('<a class="btn" href="/learn#learn-presets">一键开始第一关</a>'),
        )
        self.assertNotIn('<a class="btn" href="/app">高级模拟盘(完成第一圈后再看)</a>', home)
        # A logged-in user hitting /login or /register is redirected into the app, not asked
        # to authenticate again (the "点击之后还要重新登录" complaint).
        for p in ("/login", "/register"):
            st, _, _ = self.request("GET", p, headers={"Cookie": cookie})
            self.assertIn(st, {302, 303}, f"{p} should redirect a logged-in user")
        # Logged-out regression: public nav still offers login, and /login renders the form.
        _, _, anon = self.request("GET", "/preview")
        self.assertIn("登录", anon)
        self.assertIn("免费注册,进入学习工作台", anon)
        self.assertIn("先体验 3 分钟学习闭环", anon)
        self.assertNotIn("免费注册,拿 10 万模拟本金", anon)
        self.assertNotIn("退出", anon)
        st_login, _, _ = self.request("GET", "/login")
        self.assertEqual(st_login, 200)  # anonymous still gets the login form

    def test_research_page_is_public_indexable_builder_tier(self):
        status, headers, payload = self.request("GET", "/research")  # no login
        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)  # indexable (engineer-persona acquisition)
        self.assertIn("研究闭环", payload)
        self.assertIn("从模拟盘毕业", payload)
        self.assertIn("src.research.real_data_report", payload)  # CLI run instructions
        self.assertIn("reports/predictions.csv", payload)  # bridge back to paper trading

    def test_lessons_page_works_without_artifact(self):
        missing = Path(self.tmpdir.name) / "none.json"
        with patch.dict(os.environ, {"OWQ_PREVIEW_JSON": str(missing)}, clear=False):
            status, _, payload = self.request("GET", "/lessons")
        self.assertEqual(status, 200)  # lessons still render without the live numbers
        self.assertIn("幸存者偏差", payload)

    def test_preview_page_falls_back_without_artifact(self):
        missing = Path(self.tmpdir.name) / "nope.json"
        with patch.dict(os.environ, {"OWQ_PREVIEW_JSON": str(missing)}, clear=False):
            status, _, payload = self.request("GET", "/preview")
        self.assertEqual(status, 200)  # graceful fallback, still public, still has CTAs
        self.assertIn("免费注册,进入学习工作台", payload)

    def test_showcase_share_card_renders(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "ShareCard")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        status, _, payload = self.request("GET", "/showcase", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("分享我的战绩", payload)
        self.assertIn(f"/u/{user_id}/card.svg", payload)  # embeds the existing shareable card
        self.assertIn("data-copy", payload)               # copy-link affordance (app.js enhances)
        self.assertIn("模拟训练账户", payload)             # honest framing

    def test_equity_curve_api_requires_login(self):
        status, _, _ = self.request("GET", "/api/equity-curve")
        self.assertIn(status, {302, 303})  # redirect to login, not a JSON leak

    def test_private_and_machine_routes_send_noindex_headers(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "NoIndexRoute")
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        for path in ["/app", "/account", "/market", "/portfolio-lab"]:
            with self.subTest(path=path):
                status, headers, _ = self.request("GET", path, headers={"Cookie": cookie})
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")

        # /login and /register are noindex too, but render the form only when ANONYMOUS
        # (a logged-in user is redirected into the app — see test_public_pages_reflect_login_session).
        for path in ["/register", "/login"]:
            with self.subTest(path=path):
                status, headers, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")

        for path in ["/livez", "/healthz", "/readyz", "/metrics", "/account/export/orders.csv", "/account/export/data.json"]:
            with self.subTest(path=path):
                status, headers, _ = self.request("GET", path, headers={"Cookie": cookie})
                self.assertIn(status, {200, 503})
                self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")

        status, headers, _ = self.request("GET", "/showcase/public")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Robots-Tag", headers)

    def test_unhandled_route_error_returns_500_and_records_audit(self):
        with patch.object(AppHandler, "render_landing", side_effect=RuntimeError("sensitive boom")):
            status, headers, payload = self.request("GET", "/")

        self.assertEqual(status, 500)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertIn("服务暂时不可用", payload)
        self.assertIn("错误编号", payload)
        self.assertNotIn("sensitive boom", payload)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "server.error")
        self.assertEqual(event["target_type"], "http")
        self.assertEqual(event["target_id"], "/")
        self.assertEqual(headers.get("X-OurWorlds-Error-Id"), str(event["id"]))
        self.assertIn(f"#{event['id']}", payload)
        self.assertIn("RuntimeError", event["detail"])
        self.assertNotIn("sensitive boom", event["detail"])

        with patch.object(AppHandler, "render_landing", side_effect=RuntimeError("sensitive head boom")):
            status, headers, payload = self.request("HEAD", "/")
        self.assertEqual(status, 500)
        self.assertEqual(payload, "")
        self.assertEqual(headers.get("X-OurWorlds-Error-Id"), str(services.audit_events(self.con)[0]["id"]))
        self.assertNotIn("sensitive head boom", str(headers))

    def test_public_legal_pages_are_available(self):
        for path, expected in {
            "/legal": "法律与风险",
            "/terms": "服务条款",
            "/privacy": "隐私说明",
            "/risk": "风险提示",
        }.items():
            status, _, payload = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(expected, payload)
            self.assertIn("2026-06-24", payload)

    def test_auth_rate_limit_returns_429(self):
        last_status = 0
        last_payload = ""
        for _ in range(31):
            last_status, _, last_payload = self.request("GET", "/auth/email/confirm?token=test-rate-limit")

        self.assertEqual(last_status, 429)
        self.assertIn("请求过于频繁", last_payload)

    def test_oversized_post_body_returns_413_before_form_handling(self):
        body = urlencode({"email": "huge@example.com", "accept_terms": "1", "padding": "x" * 5000})

        with patch.dict(os.environ, {"OWQ_MAX_FORM_BYTES": "4096"}, clear=False):
            status, _, payload = self.request(
                "POST",
                "/register",
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        self.assertEqual(status, 413)
        self.assertIn("请求内容过大", payload)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0], 0)

    def test_root_landing_injects_live_contest_and_forum_data(self):
        user_id = services.get_or_create_user(self.con, "dev-live-openid", "LiveArenaUser")
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        services.create_post(self.con, user_id, "实时赛场复盘", "这是一条首页可见的讨论记录", "live")

        status, _, payload = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("当前公开赛排名", payload)
        self.assertIn("LiveArenaUser", payload)
        self.assertIn("实时赛场复盘", payload)
        self.assertIn("DATA PROOF", payload)
        self.assertIn("1 人", payload)

    def test_public_discovery_files_expose_only_public_routes(self):
        user_id = services.get_or_create_user(self.con, "dev-discovery-openid", "DiscoveryUser")
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        post_id = services.create_post(self.con, user_id, "Sitemap 策略复盘", "这是一条可公开索引的复盘。", "sitemap")

        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, headers, robots = self.request("GET", "/robots.txt")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", headers.get("Content-Type", ""))
            self.assertIn("Sitemap: https://quant.ourworlds.app/sitemap.xml", robots)
            self.assertIn("Disallow: /admin", robots)
            self.assertIn("Disallow: /register", robots)
            self.assertIn("Disallow: /support", robots)
            self.assertIn("Disallow: /livez", robots)
            self.assertIn("Disallow: /metrics", robots)

            status, headers, payload = self.request("HEAD", "/robots.txt")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", headers.get("Content-Type", ""))
            self.assertEqual(payload, "")

            status, headers, sitemap = self.request("GET", "/sitemap.xml")
            self.assertEqual(status, 200)
            self.assertIn("application/xml", headers.get("Content-Type", ""))
            self.assertIn("<urlset", sitemap)
            self.assertIn("<loc>https://quant.ourworlds.app/</loc>", sitemap)
            self.assertIn("<loc>https://quant.ourworlds.app/data-status</loc>", sitemap)
            self.assertIn("<loc>https://quant.ourworlds.app/showcase/public</loc>", sitemap)
            self.assertIn("<loc>https://quant.ourworlds.app/forum</loc>", sitemap)
            self.assertIn(f"<loc>https://quant.ourworlds.app/u/{user_id}</loc>", sitemap)
            self.assertIn(f"<loc>https://quant.ourworlds.app/forum/{post_id}</loc>", sitemap)
            self.assertIn("<loc>https://quant.ourworlds.app/terms</loc>", sitemap)
            self.assertNotIn("/admin", sitemap)
            self.assertNotIn("/register", sitemap)
            self.assertNotIn("/support", sitemap)
            self.assertNotIn("/livez", sitemap)
            self.assertNotIn("/metrics", sitemap)
            self.assertNotIn("route@example.com", sitemap)

            status, headers, payload = self.request("HEAD", "/sitemap.xml")
            self.assertEqual(status, 200)
            self.assertIn("application/xml", headers.get("Content-Type", ""))
            self.assertEqual(payload, "")

    def test_public_data_status_summarizes_safe_live_data(self):
        pred_path = Path(self.tmpdir.name) / "predictions.csv"
        pred_path.write_text(
            "code,prediction,date\n"
            "000001.SZ,0.031,2026-06-24\n"
            "600519.SH,0.022,2026-06-24\n"
            "999999.SZ,0.099,2026-06-24\n",
            encoding="utf-8",
        )
        self.con.execute("DELETE FROM market_prices")
        self.con.executemany(
            """
            INSERT INTO market_prices(code, name, price, prev_close, source, as_of)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001.SZ", "平安银行", 10.0, 9.8, "duckdb:none:tushare", "2026-06-24"),
                ("600519.SH", "贵州茅台", 1600.0, 1580.0, "duckdb:none:tushare", "2026-06-24"),
            ],
        )
        user_id = services.get_or_create_user(self.con, "dev-data-openid", "DataStatusUser")
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        services.create_post(self.con, user_id, "数据状态复盘", "围绕真实行情复盘", "data")
        self.con.commit()

        with patch.dict(os.environ, {"OWQ_PREDICTIONS_CSV": str(pred_path)}, clear=False):
            status, headers, payload = self.request("GET", "/data-status")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("数据透明度", payload)
            self.assertIn("Tushare / DuckDB", payload)
            self.assertIn("2026-06-24", payload)
            self.assertIn("2 个", payload)
            self.assertIn("000001.SZ", payload)
            self.assertIn("平安银行", payload)
            self.assertIn("参赛账户", payload)
            self.assertIn("讨论记录", payload)
            self.assertIn('property="og:title"', payload)
            self.assertNotIn("OWQ_SECRET", payload)
            self.assertNotIn("CLOUDFLARE_API_TOKEN", payload)
            self.assertNotIn(str(pred_path), payload)

            status, headers, payload = self.request("HEAD", "/data-status")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertEqual(payload, "")

    def test_head_protected_routes_match_get_auth_redirects(self):
        status, headers, payload = self.request("HEAD", "/app")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")
        self.assertEqual(payload, "")

        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "HeadUser")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, headers, payload = self.request("HEAD", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertEqual(payload, "")

    def test_public_register_showcase_and_forum_routes(self):
        user_id = services.get_or_create_user(self.con, "dev-test-openid", "RouteUser")
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        post_id = services.create_post(self.con, user_id, "公开复盘", "公开阅读内容", "paper")

        status, _, register = self.request("GET", "/register")
        self.assertEqual(status, 200)
        self.assertIn("注册后先完成 3 分钟学习闭环", register)
        self.assertIn("没有 DeepSeek key 也能用内置示例教练跑通流程", register)
        self.assertIn("注册后第一屏先看一句话,再点蓝色推荐按钮", register)
        self.assertIn("现在只做一件事:收注册码", register)
        self.assertIn("只填邮箱,再勾选同意条款", register)
        self.assertIn("手机注册下一步提示", register)
        self.assertIn('name="accept_terms"', register)
        token, register_dev = self.start_registration()
        self.assertIn(f"/auth/email/confirm?token={token}", register_dev)
        self.assertIn("完成后自动进入学习工作台", register_dev)
        self.assertIn("第一屏先懂一句话,再点蓝色推荐按钮", register_dev)

        status, _, showcase = self.request("GET", "/showcase/public")
        self.assertEqual(status, 200)
        self.assertIn("公开排行榜", showcase)
        self.assertIn("RouteUser", showcase)
        self.assertIn("赛场讨论", showcase)
        self.assertIn("数据和组合设计", showcase)
        self.assertIn('href="/data-status"', showcase)
        self.assertIn('href="/forum"', showcase)
        self.assertIn('property="og:title"', showcase)
        self.assertIn('property="og:url"', showcase)

        status, _, forum = self.request("GET", "/forum")
        self.assertEqual(status, 200)
        self.assertIn("策略论坛", forum)
        self.assertIn("筛选", forum)
        self.assertIn('property="og:title"', forum)
        self.assertIn("登录后发帖", forum)
        self.assertIn("公开复盘", forum)

        status, _, post = self.request("GET", f"/forum/{post_id}")
        self.assertEqual(status, 200)
        self.assertIn("公开阅读内容", post)
        self.assertIn("登录后评论", post)
        self.assertIn("分享链接", post)
        self.assertIn('property="og:type" content="article"', post)
        self.assertIn(f"/u/{user_id}/card.svg", post)

    def test_public_support_request_can_be_submitted_and_admin_resolved(self):
        status, headers, page = self.request("GET", "/support")
        self.assertEqual(status, 200)
        self.assertIn("联系支持", page)
        self.assertIn('name="email"', page)
        self.assertIn('name="accept_terms"', page)
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")

        status, headers, _ = self.request(
            "POST",
            "/support",
            body=urlencode({"email": "help@example.com", "subject": "缺少同意", "message": "这条请求缺少法律同意。"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM support_requests").fetchone()[0], 0)

        status, headers, _ = self.request(
            "POST",
            "/support",
            body=urlencode(
                {
                    "email": "help@example.com",
                    "category": "registration",
                    "subject": "无法收到确认邮件",
                    "message": "我无法收到注册确认邮件,请帮忙检查注册链路。",
                    "accept_terms": "1",
                }
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn("msg=", headers.get("Location", ""))
        request = self.con.execute("SELECT * FROM support_requests WHERE email='help@example.com'").fetchone()
        self.assertIsNotNone(request)
        self.assertEqual(request["category"], "registration")
        self.assertEqual(request["status"], "open")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "support.request_create")

        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "SupportAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        user_id = services.get_or_create_user(self.con, "dev-support-normal", "SupportNormal")
        user_cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("支持请求", admin)
        self.assertIn("无法收到确认邮件", admin)
        self.assertIn("/admin/support.csv", admin)

        status, headers, _ = self.request(
            "POST",
            f"/admin/support/{request['id']}/resolve",
            body=self.form_body(admin_id, {"status": "resolved", "note": "已联系用户"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
        )
        self.assertEqual(status, 303)
        resolved = self.con.execute("SELECT * FROM support_requests WHERE id=?", (request["id"],)).fetchone()
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution_note"], "已联系用户")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.support_resolve")

        status, headers, csv_body = self.request("GET", "/admin/support.csv", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("support-requests.csv", headers.get("Content-Disposition", ""))
        rows = list(csv.DictReader(io.StringIO(csv_body.lstrip("\ufeff"))))
        exported = next(row for row in rows if row["id"] == str(request["id"]))
        self.assertEqual(exported["email"], "help@example.com")
        self.assertEqual(exported["status"], "resolved")
        self.assertEqual(exported["resolution_note"], "已联系用户")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.support_export")

        status, _, forbidden = self.request("GET", "/admin/support.csv", headers={"Cookie": user_cookie})
        self.assertEqual(status, 403)
        self.assertIn("当前用户没有管理权限", forbidden)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.admin_forbidden")
        self.assertEqual(event["target_id"], "/admin/support.csv")

    def test_support_page_becomes_join_application_when_registration_is_closed(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "",
                "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
                "OWQ_EMAIL_PROVIDER": "",
                "OWQ_EMAIL_FROM": "",
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "",
            },
            clear=False,
        ):
            status, headers, page = self.request("GET", "/support", headers={"Host": "quant.ourworlds.app"})

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Robots-Tag"), "noindex, nofollow")
        self.assertIn("申请加入", page)
        self.assertIn("当前新用户注册暂未开放", page)
        self.assertIn('value="registration" selected', page)
        self.assertIn("申请加入模拟盘公开赛", page)
        self.assertIn("提交申请", page)
        self.assertNotIn('value="other" selected', page)

    def test_support_join_application_posts_as_registration_when_registration_is_closed(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "",
                "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
                "OWQ_EMAIL_PROVIDER": "",
                "OWQ_EMAIL_FROM": "",
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "",
            },
            clear=False,
        ):
            status, headers, _ = self.request(
                "POST",
                "/support",
                body=urlencode(
                    {
                        "email": "join-application@example.com",
                        "category": "other",
                        "subject": "申请加入模拟盘公开赛",
                        "message": "我希望申请加入公开赛,请管理员联系我开通测试账号。",
                        "accept_terms": "1",
                    }
                ),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "quant.ourworlds.app"},
            )

        self.assertEqual(status, 303)
        self.assertIn("msg=", headers.get("Location", ""))
        request = self.con.execute(
            "SELECT * FROM support_requests WHERE email='join-application@example.com'"
        ).fetchone()
        self.assertIsNotNone(request)
        self.assertEqual(request["category"], "registration")
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "support.request_create")
        self.assertIn('"category": "registration"', event["detail"])

    def test_public_forms_do_not_prefill_sensitive_query_values(self):
        for path in [
            "/login?identifier=leak@example.com&email=other@example.com",
            "/register?email=leak@example.com",
            "/forgot-password?email=leak@example.com",
            "/support?email=leak@example.com&subject=SecretSubject",
        ]:
            status, _, page = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertNotIn("leak@example.com", page)
            self.assertNotIn("other@example.com", page)
            self.assertNotIn("SecretSubject", page)

    def test_support_validation_redirect_does_not_leak_submitted_fields(self):
        status, headers, _ = self.request(
            "POST",
            "/support",
            body=urlencode(
                {
                    "email": "support-leak@example.com",
                    "category": "account",
                    "subject": "S",
                    "message": "这条描述足够长,但主题太短。",
                    "accept_terms": "1",
                }
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn("support-leak", location)
        self.assertNotIn("category=account", location)
        self.assertNotIn("subject=", location)

    def test_public_support_request_is_rate_limited_by_email_without_leaking_email(self):
        first = {
            "email": "repeat-help@example.com",
            "category": "registration",
            "subject": "第一次支持请求",
            "message": "第一次支持请求用于验证重复提交限制。",
            "accept_terms": "1",
        }
        status, headers, _ = self.request(
            "POST",
            "/support",
            body=urlencode(first),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn("msg=", headers.get("Location", ""))

        second = dict(first)
        second["subject"] = "第二次支持请求"
        second["message"] = "第二次支持请求应该因为同邮箱冷却被拦截。"
        status, headers, _ = self.request(
            "POST",
            "/support",
            body=urlencode(second),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM support_requests WHERE email='repeat-help@example.com'").fetchone()[0], 1)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.rate_limited")
        self.assertEqual(event["target_id"], "support.request.email")
        self.assertIn('"email_hash":', event["detail"])
        self.assertNotIn("repeat-help@example.com", event["detail"])

    def test_support_request_open_limit_blocks_unbounded_queue_growth(self):
        for index in range(services.SUPPORT_REQUEST_OPEN_LIMIT):
            request_id = services.create_support_request(
                self.con,
                "queue-limit@example.com",
                f"队列上限请求 {index}",
                "这条支持请求用于验证未处理队列数量限制。",
                category="account",
            )
            self.con.execute(
                "UPDATE support_requests SET created_at=datetime('now', '-2 hours') WHERE id=?",
                (request_id,),
            )
            self.con.commit()

        with self.assertRaises(services.RateLimitExceeded):
            services.create_support_request(
                self.con,
                "queue-limit@example.com",
                "超过队列上限",
                "这一条应该因为未处理支持请求数量过多而被拒绝。",
                category="account",
            )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM support_requests WHERE email='queue-limit@example.com'").fetchone()[0],
            services.SUPPORT_REQUEST_OPEN_LIMIT,
        )

    def test_public_forum_can_filter_strategy_discussions(self):
        user_id = services.get_or_create_user(self.con, "dev-filter-openid", "FilterUser")
        services.join_active_contest(self.con, user_id)
        services.create_post(self.con, user_id, "低波动策略", "低波动 ETF 复盘", "low-vol")
        services.create_post(self.con, user_id, "反转策略", "短期反转复盘", "reversal")

        status, _, forum = self.request("GET", "/forum?tag=low-vol&sort=comments")

        self.assertEqual(status, 200)
        self.assertIn("低波动策略", forum)
        self.assertNotIn("反转策略", forum)
        self.assertIn('value="low-vol" selected', forum)
        self.assertIn('value="comments" selected', forum)

    def test_comment_author_can_delete_comment_from_post_page(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "CommentRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        post_id = services.create_post(self.con, user_id, "评论删除帖", "用于验证评论删除", "forum")
        comment_id = services.add_comment(self.con, user_id, post_id, "这条评论会被删除")

        status, _, post = self.request("GET", f"/forum/{post_id}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("删除评论", post)
        self.assertIn('name="csrf"', post)
        self.assertIn("这条评论会被删除", post)

        body = self.form_body(user_id)
        status, headers, _ = self.request(
            "POST",
            f"/forum/{post_id}/comments/{comment_id}/delete",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn(f"/forum/{post_id}", headers.get("Location", ""))
        self.assertEqual(services.post_comments(self.con, post_id), [])

    def test_post_author_can_delete_post_from_post_page(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "PostDeleteRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        post_id = services.create_post(self.con, user_id, "准备删除的策略帖", "这篇复盘稍后撤回", "forum")
        services.add_comment(self.con, user_id, post_id, "随帖删除的评论")

        status, _, post = self.request("GET", f"/forum/{post_id}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("删除帖子", post)
        self.assertIn('name="csrf"', post)
        self.assertIn("准备删除的策略帖", post)

        body = self.form_body(user_id)
        status, headers, _ = self.request(
            "POST",
            f"/forum/{post_id}/delete",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/forum", headers.get("Location", ""))
        self.assertIsNone(services.get_post(self.con, post_id))
        self.assertEqual(services.post_comments(self.con, post_id), [])

        status, _, forum = self.request("GET", "/forum")
        self.assertEqual(status, 200)
        self.assertNotIn("准备删除的策略帖", forum)

    def test_logged_in_user_can_report_content_and_admin_can_resolve(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "ReportAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        reporter_token = services.create_wechat_session(self.con)
        reporter_id = services.confirm_wechat_session(self.con, reporter_token, "ReporterRoute")
        reporter_cookie = f"owq_session={self.sign_cookie(reporter_id)}"
        post_id = services.create_post(self.con, admin_id, "可举报策略帖", "这篇帖子用于举报流程测试", "forum")
        comment_id = services.add_comment(self.con, admin_id, post_id, "可举报评论")

        status, _, post = self.request("GET", f"/forum/{post_id}", headers={"Cookie": reporter_cookie})
        self.assertEqual(status, 200)
        self.assertIn("举报帖子", post)
        self.assertIn("举报评论", post)

        status, headers, _ = self.request(
            "POST",
            f"/forum/{post_id}/report",
            body=self.form_body(reporter_id, {"reason": "疑似诱导交易"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": reporter_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn(f"/forum/{post_id}", headers.get("Location", ""))

        status, headers, _ = self.request(
            "POST",
            f"/forum/{post_id}/comments/{comment_id}/report",
            body=self.form_body(reporter_id, {"reason": "评论不友善"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": reporter_cookie},
        )
        self.assertEqual(status, 303)
        reports = services.content_reports(self.con)
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["target_type"], "comment")

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("内容举报", admin)
        self.assertIn("疑似诱导交易", admin)
        self.assertIn("评论不友善", admin)

        report_id = reports[0]["id"]
        status, headers, _ = self.request(
            "POST",
            f"/admin/reports/{report_id}/resolve",
            body=self.form_body(admin_id, {"status": "resolved", "note": "已处理"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
        )
        self.assertEqual(status, 303)
        resolved = [r for r in services.content_reports(self.con) if r["id"] == report_id][0]
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolution_note"], "已处理")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.report_resolve")

    def test_account_ai_page_renders_education_banner(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "AIRoute")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        status, _, page = self.request("GET", "/account/ai", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("AI 教练", page)
        self.assertIn("AI 教练是可选升级", page)
        self.assertIn("不构成投资建议", page)  # mandatory education banner
        self.assertIn("尚未配置 API key", page)
        self.assertIn("第一次不用配置 key", page)
        self.assertIn("直接回学习工作台点蓝色推荐按钮", page)
        self.assertIn("不调用 DeepSeek,不产生 AI 费用", page)
        self.assertIn('href="/learn#learn-presets">回学习工作台继续第一圈</a>', page)
        self.assertIn('href="/learn/demo">先看 3 分钟示例</a>', page)
        self.assertIn("学习工作台的预设示例任务", page)
        self.assertIn("基础模拟练习和复盘都不需要 key", page)
        self.assertIn("deepseek-v4-flash", page)

    def test_account_ai_save_key_is_encrypted_and_masked(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "AIKeyRoute")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        with patch("src.app.ai.client.test_api_key", return_value={"ok": True, "detail": "key 可用"}):
            status, headers, _ = self.request(
                "POST", "/account/ai",
                body=self.form_body(uid, {"action": "save", "api_key": "sk-routekey1234567890",
                                          "base_url": "https://api.deepseek.com", "model": "deepseek-chat"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )
        self.assertEqual(status, 303)
        row = self.con.execute("SELECT * FROM ai_user_keys WHERE user_id=?", (uid,)).fetchone()
        self.assertEqual(row["masked_hint"], "sk-…7890")
        self.assertNotIn(b"sk-routekey", bytes(row["ciphertext"]))  # ciphertext, not plaintext

    def test_account_ai_review_without_key_prompts_config(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "AINoKey")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        status, _, page = self.request(
            "POST", "/account/ai-review",
            body=self.form_body(uid, {"question": "复盘"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertIn("DeepSeek API key", page)

    def test_account_ai_review_blocks_model_stock_tip(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "AITip")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        from src.app.ai import service as ai_service
        ai_service.save_key(self.con, uid, SECRET, "sk-routekey1234567890",
                            "https://api.deepseek.com", "deepseek-chat")
        services.place_order(self.con, uid, "000001.SZ", "buy", 100)
        tip = {"text": "建议买入 000001.SZ,目标价 15 元。", "usage": {"total_tokens": 9}, "model": "x"}
        with patch("src.app.ai.client.chat_completion", return_value=tip):
            status, _, page = self.request(
                "POST", "/account/ai-review",
                body=self.form_body(uid, {"question": "我该买啥"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )
        self.assertEqual(status, 200)
        self.assertIn("触发了合规过滤", page)  # blocked message shown
        self.assertNotIn("目标价 15", page)    # the tip itself is never shown

    def test_learn_page_without_key_shows_course_and_config_entry(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnNoKey")
        cookie = f"owq_session={self.sign_cookie(uid)}"

        status, _, page = self.request("GET", "/learn", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertIn('class="nav learn-nav"', page)
        self.assertIn('<a href="/learn">学习首页</a>', page)
        self.assertIn('<a href="/learn#learn-presets">一键开始</a>', page)
        self.assertIn('<a href="/learn/demo">示例体验</a>', page)
        self.assertNotIn('<a href="/account/ai">AI教练(稍后)</a>', page)
        self.assertNotIn('<a href="/learn#learn-presets">选目标</a>', page)
        self.assertNotIn('<a href="/learn#learning-journey">学习轨迹</a>', page)
        self.assertNotIn('<a href="/app">高级模拟盘</a>', page)
        self.assertNotIn('href="/app"', page)
        self.assertNotIn("组合设计", page)
        self.assertNotIn("研究引擎", page)
        self.assertNotIn("比赛展示", page)
        self.assertIn("新手 AI 学习工作台", page)
        self.assertIn("量化投资是什么", page)
        self.assertIn("先记住一句话", page)
        self.assertIn("量化学习不是让 AI 告诉你买什么", page)
        self.assertIn("AI 在这里做什么", page)
        self.assertIn("第一次只做什么", page)
        self.assertIn("第一步:先懂一句话,再一键开始", page)
        self.assertIn("30 秒先懂一句话", page)
        self.assertIn("量化投资不是猜涨跌", page)
        self.assertIn("AI 在这里像教练", page)
        self.assertIn("第一圈只求完成", page)
        self.assertIn("img,svg,video,canvas{max-width:100%;height:auto}", page)
        self.assertIn("先用 30 秒知道这里不是荐股工具", page)
        self.assertIn("再点蓝色按钮开始第一次学习闭环", page)
        self.assertIn("现在只做一件事", page)
        self.assertIn("先记住一句话:量化投资不是猜涨跌", page)
        self.assertIn("一键开始第一关", page)
        self.assertIn(".starter-fast-path form,.starter-fast-path button", page)
        self.assertIn(".next-action-cta form,.next-action-cta button", page)
        self.assertIn("@media(max-width:560px){body{font-size:14px;overflow-x:hidden}", page)
        self.assertIn("body:has(.mobile-next-bar){padding-bottom:calc(86px + env(safe-area-inset-bottom))}", page)
        self.assertIn(".nav{width:100%;gap:8px;flex-wrap:nowrap;overflow-x:auto", page)
        self.assertIn("scroll-snap-type:x proximity", page)
        self.assertIn("mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent)", page)
        self.assertIn(".card p>.btn,.card p>button{width:100%;margin-top:8px}", page)
        self.assertIn(".practice-detail-grid", page)
        self.assertIn("table{min-width:640px}", page)
        self.assertIn(".learning-mobile-table,.mobile-card-table{min-width:0;border-collapse:separate", page)
        self.assertIn(".learning-mobile-table td::before,.mobile-card-table td::before{content:attr(data-label)", page)
        self.assertIn(".mobile-card-table td:empty::after{content:", page)
        self.assertIn(".return-mission-head,.return-mission-grid{grid-template-columns:1fr}", page)
        self.assertIn(".return-mission-action .btn,.return-mission-action button,.return-mission-action form{width:100%}", page)
        self.assertIn(".badge,.pill,.demo-pill{white-space:normal;max-width:100%}", page)
        self.assertIn("input,select,textarea{font-size:16px}", page)
        self.assertIn(".starter-choice,.preset-card,.quest-card{min-height:auto;padding:14px}", page)
        self.assertIn(".loop-step{display:grid;grid-template-columns:auto 1fr;gap:8px", page)
        self.assertIn(".top,.card,.msg,.advanced-practice,.manual-reflection,.practice-detail{max-width:100%}", page)
        self.assertIn(".markdown-body code{white-space:normal;overflow-wrap:anywhere}", page)
        self.assertIn(".voice-command,pre{white-space:pre-wrap;overflow-wrap:anywhere}", page)
        self.assertIn(".identity{align-items:flex-start;flex-direction:column}", page)
        self.assertIn(".learning-notice{display:grid;grid-template-columns:1fr auto", page)
        self.assertIn(".learning-notice .btn{width:100%}", page)
        self.assertIn(
            ".mobile-next-bar{bottom:max(10px,env(safe-area-inset-bottom));padding-bottom:max(10px,env(safe-area-inset-bottom))}",
            page,
        )
        self.assertIn(".mobile-next-bar{position:fixed;left:12px;right:12px;bottom:12px", page)
        self.assertIn(".mobile-next-bar{grid-template-columns:minmax(0,1fr) minmax(88px,auto)}", page)
        self.assertIn("@media(max-width:380px){.wrap{padding:0 10px 22px}", page)
        self.assertIn(".mobile-next-bar{left:8px;right:8px;grid-template-columns:1fr}", page)
        self.assertIn(".mobile-next-spacer{height:calc(128px + env(safe-area-inset-bottom))}", page)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机下一步提示">', page)
        self.assertIn("<span>现在只做一件事</span><b>先开始第一关</b>", page)
        self.assertIn('<a class="btn blue" href="#learn-presets">去开始</a>', page)
        self.assertIn('class="mobile-next-spacer"', page)
        self.assertIn('class="loop-promise"', page)
        self.assertIn("3-5 分钟", page)
        self.assertIn("只跑通第一次闭环", page)
        self.assertIn("无 key 也能开始", page)
        self.assertIn("没有 DeepSeek key 时使用示例教练", page)
        self.assertIn("不会真实交易", page)
        self.assertIn("完成有反馈", page)
        self.assertIn("保存复盘后会解锁 6/6 和第一枚学习徽章", page)
        self.assertIn('id="beginner-focus"', page)
        self.assertIn("小白模式:现在只点一个按钮", page)
        self.assertIn("去一键开始第一关", page)
        self.assertIn("不用会术语", page)
        self.assertIn("不会下单", page)
        self.assertIn('<div class="starter-fast-path">', page)
        self.assertIn('action="/learn/sample-task"', page)
        self.assertIn('name="preset" value="0"', page)
        self.assertNotIn('href="#starter-choice-recommended"', page)
        self.assertNotIn('id="starter-choice-recommended"', page)
        self.assertIn('class="starter-selected"', page)
        self.assertIn("我完全不懂,先从概念开始", page)
        self.assertIn("推荐第一关", page)
        self.assertIn("点击后发生什么", page)
        self.assertIn("安全边界", page)
        self.assertIn("创建示例教练任务,并直接准备 1 条今日练习", page)
        self.assertIn("不调用 DeepSeek、不产生 AI 费用", page)
        self.assertIn("不会扣 AI 费用,不会自动成交,下一步只确认观察材料", page)
        self.assertNotIn('class="starter-choice recommended"', page)
        self.assertIn("想换关? 更多新手关卡", page)
        self.assertIn('<details class="advanced-practice preset-library">', page)
        self.assertIn("可选:展开新手关卡地图", page)
        self.assertIn("第一次先不用展开", page)
        self.assertIn("直接点上面的蓝色按钮", page)
        self.assertNotIn('<details class="advanced-practice preset-library" open>', page)
        self.assertNotIn('id="learning-journey"', page)
        self.assertIn("不知道问什么", page)
        self.assertIn('class="quest-ladder" aria-label="分级学习任务地图"', page)
        self.assertIn("分级关卡地图", page)
        self.assertIn("没完成第一圈前先不要跨到第二关", page)
        self.assertIn("第 1 关 · 先懂概念", page)
        self.assertIn("完全不懂也从这里开始", page)
        self.assertIn("适合 0-3 分钟", page)
        self.assertIn("推荐第一关:完全不懂就点这个", page)
        self.assertIn("点这里开始第 1 关", page)
        self.assertIn("第 2 关 · 做一次观察", page)
        self.assertIn("完成第一关后", page)
        self.assertIn("建议完成当前 6/6 后再点", page)
        self.assertIn("完成第一圈后再创建", page)
        self.assertIn("第 3 关 · 风险和复盘", page)
        self.assertIn("有复盘后", page)
        self.assertLess(page.index("第 1 关 · 先懂概念"), page.index("第 2 关 · 做一次观察"))
        self.assertLess(page.index("第 2 关 · 做一次观察"), page.index("第 3 关 · 风险和复盘"))
        self.assertIn("量化投资到底是什么?", page)
        self.assertIn("AI 能帮我做什么?", page)
        self.assertIn("点这里创建学习任务", page)
        self.assertIn("创建示例任务", page)
        self.assertIn("/learn/sample-task", page)
        self.assertIn("创建内置示例教练任务", page)
        self.assertIn("3 分钟示例体验", page)
        self.assertIn("6/6 前先不要进入高级模拟盘", page)
        self.assertIn("完成后页面会再给你高级入口", page)
        self.assertIn("高级模拟盘入口会在完成 6/6 后出现", page)
        self.assertIn("AI 教练配置(可选,以后再看)", page)
        self.assertNotIn("高级模拟盘(完成第一圈后再看)", page)
        self.assertNotIn("高级模拟盘(稍后再看)", page)
        self.assertNotIn("想直接上手?进入模拟盘", page)
        self.assertIn("第一圈不用 DeepSeek key", page)
        self.assertIn("回到蓝色推荐按钮", page)
        self.assertIn("以后再配置 AI key", page)
        self.assertIn("示例教练不调用 DeepSeek", page)
        self.assertNotIn("避免你填完后不能提交", page)
        self.assertNotIn("<h2>学习记录</h2>", page)
        self.assertNotIn("还没有学习轨迹", page)
        self.assertNotIn("查看高级任务记录", page)
        self.assertNotIn("新手第一次闭环可以先不展开", page)
        self.assertNotIn("暂无学习任务。先看上面的 30 秒概念,再点蓝色推荐按钮即可开始", page)
        self.assertNotIn("最近学习任务", page)
        self.assertNotIn('textarea name="goal"', page)
        self.assertNotIn("让 AI 拆解目标</button>", page)
        self.assertIn('class="preset-card"', page)
        self.assertIn("/account/ai", page)
        self.assertLess(page.index('id="learn-presets"'), page.index('id="learning-loop"'))
        self.assertLess(page.index('id="learn-presets"'), page.index('id="beginner-focus"'))
        self.assertLess(page.index('id="beginner-focus"'), page.index('id="learning-loop"'))
        self.assertLess(page.index("现在只做一件事"), page.index("30 秒先懂一句话"))
        self.assertLess(page.index("一键开始第一关"), page.index("30 秒先懂一句话"))
        self.assertLess(page.index("第一步:先懂一句话,再一键开始"), page.index("第一次学习闭环"))
        self.assertLess(page.index("一键开始第一关"), page.index("可选:展开新手关卡地图"))

    def test_learning_welcome_notice_starts_first_task_directly(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnWelcome")
        cookie = f"owq_session={self.sign_cookie(uid)}"

        status, _, page = self.request(
            "GET",
            "/learn?msg=%E7%99%BB%E5%BD%95%E6%88%90%E5%8A%9F%2C%E5%85%88%E4%BB%8E%E5%AD%A6%E4%B9%A0%E5%B7%A5%E4%BD%9C%E5%8F%B0%E5%BC%80%E5%A7%8B%E3%80%82",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', page)
        self.assertIn("欢迎来到学习工作台", page)
        self.assertIn('method="post" action="/learn/sample-task"', page)
        self.assertIn('name="preset" value="0"', page)
        self.assertIn('name="quick_start" value="1"', page)
        self.assertIn('class="btn blue" type="submit">一键开始第一关</button>', page)
        self.assertIn(".learning-notice form,.learning-notice .btn{width:100%}", page)
        self.assertLess(page.index('class="msg learning-notice"'), page.index('id="learn-presets"'))

        with patch("src.app.ai.client.chat_completion", side_effect=AssertionError("welcome first action should not call AI")):
            status, headers, _ = self.request(
                "POST",
                "/learn/sample-task",
                body=self.form_body(uid, {"preset": "0", "quick_start": "1"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#today-practice", headers.get("Location", ""))
        self.assertIn("第一关已准备好", unquote(headers.get("Location", "")))
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(task)
        self.assertIn("示例教练拆解", task["coach_text"])
        self.assertEqual(task["status"], "signals_saved")
        signals = services.practice_signals(self.con, uid, status="pending")
        self.assertEqual(len(signals), 1)
        self.assertEqual(int(signals[0]["learning_task_id"]), int(task["id"]))
        self.assertIn("第一关一键开始", signals[0]["rationale"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0], 0)
        status, _, learn = self.request("GET", headers.get("Location", "").split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第一关已准备好", learn)
        self.assertIn("刚完成 4/6:确认练习后生成观察记录", learn)
        self.assertIn("确认这 1 条模拟练习", learn)
        self.assertIn("生成模拟观察记录,去复盘", learn)

    def test_learning_sample_task_creates_task_without_deepseek_key(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnSample")
        cookie = f"owq_session={self.sign_cookie(uid)}"

        status, headers, _ = self.request(
            "POST",
            "/learn/sample-task",
            body=self.form_body(uid, {"preset": "3"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn/tasks/", headers.get("Location", ""))
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["template"], "reversal")
        self.assertIn("示例教练拆解", task["coach_text"])
        self.assertIn("先点击“一键生成今日练习”", task["coach_text"])
        self.assertNotIn("在下面预览草稿", task["coach_text"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0], 0)
        status, _, detail = self.request("GET", headers.get("Location", ""), headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', detail)
        self.assertIn("3/6", detail)
        self.assertIn("目标已经建好", detail)
        self.assertIn("刚才已经完成选目标和教练拆解", detail)
        self.assertIn(f'method="post" action="/learn/tasks/{int(task["id"])}/quick-save"', detail)
        self.assertIn('class="btn blue" type="submit">一键生成今日练习</button>', detail)
        self.assertLess(detail.index('class="msg learning-notice"'), detail.index('id="task-next-action"'))
        self.assertIn('class="nav learn-nav"', detail)
        self.assertIn('<a href="/learn">学习首页</a>', detail)
        self.assertIn('<a href="/learn#learning-loop">当前进度</a>', detail)
        self.assertIn('<a href="/learn#learning-journey">学习轨迹</a>', detail)
        self.assertNotIn('<a href="/account/ai">AI教练(稍后)</a>', detail)
        self.assertNotIn('<a href="/learn#learn-presets">选目标</a>', detail)
        self.assertNotIn('<a href="/app">高级模拟盘</a>', detail)
        self.assertNotIn("组合设计", detail)
        self.assertNotIn("研究引擎", detail)
        self.assertNotIn("比赛展示", detail)
        self.assertIn("这一关的学习目标", detail)
        self.assertIn("待生成今日练习", detail)
        self.assertIn(f"任务编号 {int(task['id'])}", detail)
        self.assertNotIn(f"<h2>学习任务 #{int(task['id'])}</h2>", detail)
        self.assertNotIn('<span class="badge">draft</span>', detail)
        self.assertIn("刚完成 3/6:点蓝色按钮进入下一步", detail)
        self.assertIn("你已经选了目标,也拿到了示例教练拆解", detail)
        self.assertIn("第一次闭环进度: 3/6", detail)
        self.assertIn("已完成:理解概念、选择目标、获得教练拆解", detail)
        self.assertIn("还差 3 步:生成练习、生成观察记录、保存复盘", detail)
        self.assertIn('id="task-shortcut"', detail)
        self.assertIn("10 秒读懂这一页", detail)
        self.assertIn("预计 30 秒完成", detail)
        self.assertIn("第一遍不用读完整教练拆解", detail)
        self.assertIn("只点蓝色按钮", detail)
        self.assertIn("一键生成今日练习", detail)
        self.assertIn("下一屏", detail)
        self.assertIn("回到学习工作台看“今日练习”", detail)
        self.assertIn("不会自动成交", detail)
        self.assertIn('class="next-action-cta"', detail)
        self.assertIn('class="task-action-head task-first-action-head"', detail)
        self.assertIn(
            ".task-first-action-head .next-action-cta,.review-first-action-head .review-primary-cta{order:-1}",
            detail,
        )
        self.assertIn("第一次只按这个按钮", detail)
        self.assertIn("不用看完下面长文", detail)
        self.assertIn("不用展开进阶草稿", detail)
        self.assertIn("已完成 1", detail)
        self.assertIn("你已经从蓝色推荐按钮或入门问题选了一个学习目标", detail)
        self.assertIn("已完成 2", detail)
        self.assertIn("系统已经给出示例教练拆解", detail)
        self.assertIn("先点击“一键生成今日练习”", detail)
        self.assertIn('class="coach-digest"', detail)
        self.assertIn("30 秒摘要", detail)
        self.assertIn("用反转观察理解“规则如何变成练习”", detail)
        self.assertIn("先看这三件事", detail)
        self.assertIn("下面是把示例教练长文压缩成的新手读法", detail)
        self.assertIn("这一关目标", detail)
        self.assertIn("系统会做", detail)
        self.assertIn("用反转观察生成小数量模拟练习,不自动成交", detail)
        self.assertIn("你下一步", detail)
        self.assertIn("先点页面上方蓝色按钮生成 1 条今日练习", detail)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机学习任务下一步提示">', detail)
        self.assertIn("<span>下一步</span><b>生成今日练习</b>", detail)
        self.assertIn('<a class="btn blue" href="#task-next-action">继续</a>', detail)
        self.assertLess(detail.index("刚完成 3/6"), detail.index("可选:展开教练拆解全文"))
        self.assertIn("接下来 3 步", detail)
        self.assertIn("NOW · 1/3", detail)
        self.assertIn("生成练习", detail)
        self.assertIn("TODO · 2/3", detail)
        self.assertIn("生成观察记录", detail)
        self.assertIn("TODO · 3/3", detail)
        self.assertIn("保存复盘", detail)
        self.assertIn("教练拆解怎么看", detail)
        self.assertIn("第一次不用逐字读完", detail)
        self.assertIn("看不懂也能继续", detail)
        self.assertIn("始终不是投资建议", detail)
        self.assertIn("可选:展开教练拆解全文", detail)
        self.assertIn("第一次先按上方按钮一键生成今日练习", detail)
        self.assertIn('<details class="advanced-practice coach-breakdown">', detail)
        self.assertNotIn("<h3>教练拆解</h3>", detail)
        self.assertIn("示例教练拆解", detail)
        self.assertIn("一键生成今日练习", detail)
        self.assertIn("会回到学习工作台继续", detail)
        self.assertEqual(detail.count('class="blue" type="submit">一键生成今日练习</button>'), 1)
        self.assertIn('id="task-next-action"', detail)
        self.assertLess(detail.index('id="task-shortcut"'), detail.index('class="task-flow"'))
        self.assertLess(detail.index("刚完成 3/6"), detail.index("可选:展开教练拆解全文"))
        self.assertIn('<details class="card advanced-practice task-advanced-settings">', detail)
        self.assertIn("可选:进阶草稿设置", detail)
        self.assertIn("第一次不用展开;上方“一键生成今日练习”已经够用", detail)
        self.assertIn("第一次可以跳过这里", detail)
        self.assertIn("回到上方生成按钮", detail)
        self.assertNotIn("已经看懂流程? 展开预览和调参数", detail)
        self.assertNotIn("第一次使用建议先不要展开", detail)
        self.assertNotIn('<details class="card advanced-practice task-advanced-settings" open>', detail)
        self.assertIn("保存当前草稿到今日练习", detail)
        self.assertIn("还没保存草稿时不要跳到高级模拟盘", detail)
        self.assertIn("高级模拟盘入口会在完成 6/6 后出现", detail)
        self.assertNotIn('href="/app"', detail)
        self.assertNotIn(">进入高级模拟盘</a>", detail)
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("继续当前学习任务", learn)
        self.assertIn("小白模式:现在只生成今日练习", learn)
        self.assertIn("去一键生成今日练习", learn)
        self.assertIn("不用调参数", learn)
        self.assertIn("会回这里", learn)
        self.assertIn("预计 30 秒", learn)
        self.assertIn("只点一次蓝色按钮,不用改参数", learn)
        self.assertIn("点完去哪", learn)
        self.assertIn("自动回到学习工作台的今日练习", learn)
        self.assertIn("不会成交", learn)
        self.assertIn('class="task-loop-hint resume-checkpoint"', learn)
        self.assertIn("回访定位:已完成 3/6", learn)
        self.assertIn("概念、目标和教练拆解已经完成", learn)
        self.assertIn("现在只差生成今日练习、生成观察记录、保存复盘", learn)
        self.assertIn("已经完成 3/6", learn)
        self.assertIn("学习记录", learn)
        self.assertIn("日常只看上面的「我的学习轨迹」就够了", learn)
        self.assertIn("查看高级任务记录", learn)
        self.assertNotIn("<h2>最近学习任务</h2>", learn)
        self.assertIn("一键生成今日练习", learn)
        self.assertIn("回看教练拆解", learn)
        self.assertIn("下一步不用改参数", learn)
        self.assertIn('class="next-action-cta"', learn)
        self.assertIn("只会生成待观察练习", learn)
        self.assertIn("不会自动成交", learn)
        self.assertIn("然后回到学习工作台继续", learn)
        self.assertIn("先完成当前第一圈:生成练习、生成观察记录、保存复盘", learn)
        self.assertIn("可选:完成当前第一圈后再换关", learn)
        self.assertIn("当前任务完成复盘前,新题会分散注意力", learn)
        self.assertIn("当前第一圈先不新建任务", learn)
        self.assertIn("你已经开始第一次学习闭环", learn)
        self.assertIn("回到当前任务", learn)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机下一步提示">', learn)
        self.assertIn("<span>下一步</span><b>回到当前任务</b>", learn)
        self.assertIn('<a class="btn blue" href="#continue-learning-task">继续</a>', learn)
        self.assertIn("不要再开新题", learn)
        self.assertIn("没有 DeepSeek key 也能完成当前第一圈", learn)
        self.assertNotIn("现在怎么开始</b><p>先点上面的预设卡片", learn)
        self.assertIn('<details class="advanced-practice preset-library">', learn)
        self.assertNotIn('<details class="advanced-practice preset-library" open>', learn)
        self.assertIn(f'/learn/tasks/{int(task["id"])}', learn)
        self.assertIn(f'/learn/tasks/{int(task["id"])}/quick-save', learn)
        self.assertLess(learn.index('id="continue-learning-task"'), learn.index('id="learning-loop"'))
        self.assertLess(learn.index('id="continue-learning-task"'), learn.index('id="beginner-focus"'))
        self.assertLess(learn.index('id="beginner-focus"'), learn.index('id="learning-loop"'))
        self.assertLess(learn.index('id="continue-learning-task"'), learn.index('id="learning-journey"'))
        self.assertLess(learn.index('id="continue-learning-task"'), learn.index("<h2>新手 AI 学习工作台</h2>"))
        self.assertLess(learn.index('id="continue-learning-task"'), learn.index("可选:完成当前第一圈后再换关"))
        self.assertNotIn('id="today-practice"', learn)

        status, headers, _ = self.request(
            "POST",
            f"/learn/tasks/{int(task['id'])}/quick-save",
            body=self.form_body(uid, {}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#today-practice", headers.get("Location", ""))
        status, _, notice_page = self.request("GET", headers.get("Location", "").split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', notice_page)
        self.assertIn("4/6", notice_page)
        self.assertIn("今日练习已经生成", notice_page)
        self.assertIn("刚才只是保存了待观察练习,还没有成交", notice_page)
        self.assertIn('href="#today-practice">去生成观察记录</a>', notice_page)
        signals = services.practice_signals(self.con, uid)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["status"], "pending")
        self.assertEqual(int(signals[0]["learning_task_id"]), int(task["id"]))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        status, _, pending_detail = self.request("GET", f"/learn/tasks/{int(task['id'])}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第一次闭环进度: 4/6", pending_detail)
        self.assertIn("预计 60 秒完成", pending_detail)
        self.assertIn("今日练习已经生成。不要再保存新草稿", pending_detail)
        self.assertIn("回学习页", pending_detail)
        self.assertIn("确认观察材料、练习规模和依据", pending_detail)
        self.assertIn("生成观察记录后会进入三问复盘", pending_detail)
        self.assertIn("这关已经生成过今日练习。现在先回学习工作台", pending_detail)
        self.assertIn("<span>下一步</span><b>生成观察记录</b>", pending_detail)
        self.assertIn('<a class="btn blue" href="/learn#today-practice">继续</a>', pending_detail)
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("已完成 4/6", learn)
        self.assertIn("还剩 2 步,先做当前这一步", learn)
        self.assertNotIn("继续当前学习任务", learn)
        self.assertIn("今日练习", learn)
        self.assertIn("小白模式:现在只生成观察记录", learn)
        self.assertIn("去生成观察记录", learn)
        self.assertIn("只点一条", learn)
        self.assertIn("系统会带你保存三问复盘", learn)
        self.assertIn("预计 60 秒", learn)
        self.assertIn("只确认 1 条最上面的练习", learn)
        self.assertIn("点完去哪", learn)
        self.assertIn("自动跳到三问复盘,完成最后一步", learn)
        self.assertIn("可选:完成当前第一圈后再换关", learn)
        self.assertIn("当前第一圈先不新建任务", learn)
        self.assertIn('<a class="btn blue" href="#first-practice-card">继续</a>', learn)
        self.assertIn("<span>下一步</span><b>生成观察记录</b>", learn)
        self.assertIn("看到 6/6 和一条复盘记录", learn)
        self.assertNotIn("现在怎么开始</b><p>先点上面的预设卡片", learn)
        self.assertNotIn('<details class="advanced-practice preset-library" open>', learn)
        self.assertLess(learn.index('id="today-practice"'), learn.index('id="learning-journey"'))
        self.assertLess(learn.index('id="today-practice"'), learn.index('id="beginner-focus"'))
        self.assertLess(learn.index('id="beginner-focus"'), learn.index('id="learning-loop"'))
        self.assertLess(learn.index('id="today-practice"'), learn.index("<h2>新手 AI 学习工作台</h2>"))
        self.assertLess(learn.index('id="today-practice"'), learn.index("可选:完成当前第一圈后再换关"))
        self.assertIn("刚完成 4/6:确认练习后生成观察记录", learn)
        self.assertIn("今日练习已经生成,但还没有生成观察记录", learn)
        self.assertIn("学习目标和教练拆解已经变成今日练习", learn)
        self.assertIn("系统还没有生成模拟观察记录", learn)
        self.assertLess(learn.index("刚完成 4/6"), learn.index("确认这 1 条模拟练习"))
        self.assertIn("确认这 1 条模拟练习", learn)
        self.assertIn("推荐先做这一条", learn)
        self.assertIn('id="first-practice-card"', learn)
        self.assertIn('class="practice-focus"', learn)
        self.assertIn("一句话任务", learn)
        self.assertIn("看依据 -> 生成观察记录 -> 三问复盘", learn)
        self.assertIn("不是判断它会不会涨", learn)
        self.assertIn("第一次只点这一条的蓝色按钮", learn)
        self.assertIn("回看和取消入口都收在可选区", learn)
        self.assertIn("观察材料", learn)
        self.assertIn("练习规模", learn)
        self.assertIn("观察 100", learn)
        self.assertIn("小数量,只为训练流程", learn)
        self.assertIn("可选:查看代码和模拟价", learn)
        self.assertIn("第一次可以先不展开", learn)
        self.assertNotIn("参考价格", learn)
        self.assertNotIn("<strong>买入 100</strong>", learn)
        self.assertIn("第一次只看三件事", learn)
        self.assertIn("代码和价格已经收进可选详情", learn)
        self.assertIn("我为什么观察它?", learn)
        self.assertLess(learn.index("我为什么观察它?"), learn.index('<button class="blue" type="submit">生成模拟观察记录,去复盘</button>'))
        self.assertIn('class="practice-ready" aria-label="点按钮前只核对三件事"', learn)
        self.assertIn("点前只核对 3 件事", learn)
        self.assertIn("<strong>材料</strong><small>知道这是练习样本", learn)
        self.assertIn("<strong>规模</strong><small>确认数量很小", learn)
        self.assertIn("<strong>依据</strong><small>能读懂一句理由", learn)
        self.assertIn("点完会发生什么?", learn)
        self.assertIn("这不是现实交易", learn)
        self.assertIn("确认 1", learn)
        self.assertIn("我能看懂“为什么观察它”的一句话", learn)
        self.assertIn("确认 2", learn)
        self.assertIn("不是现实委托", learn)
        self.assertIn("确认 3", learn)
        self.assertIn("自动跳到三问复盘", learn)
        self.assertIn("第一次只建议生成 1 条观察记录", learn)
        self.assertIn("其他先留着不动", learn)
        self.assertIn("生成观察记录", learn)
        self.assertIn('class="actions practice-primary-action"', learn)
        self.assertIn(".practice-primary-action form,.practice-primary-action button{width:100%}", learn)
        self.assertIn(".practice-card .actions,.review-done-actions{display:grid;grid-template-columns:1fr}", learn)
        self.assertIn(".practice-card .actions form,.practice-card .actions button,.review-done-actions form,.review-done-actions button,.review-next-recommend form,.review-next-recommend button{width:100%}", learn)
        self.assertIn('<button class="blue" type="submit">生成模拟观察记录,去复盘</button>', learn)
        self.assertIn('class="practice-action-note">只生成模拟学习记录,不是现实交易。</small>', learn)
        self.assertIn(".practice-action-note{display:block;margin-top:6px", learn)
        self.assertLess(learn.index('<button class="blue" type="submit">生成模拟观察记录,去复盘</button>'), learn.index("可选:查看代码和模拟价"))
        self.assertLess(learn.index('<button class="blue" type="submit">生成模拟观察记录,去复盘</button>'), learn.index("<li><b>确认 1</b>"))
        self.assertIn('<details class="practice-detail practice-optional-actions">', learn)
        self.assertIn("可选:回看教练或管理这条练习", learn)
        self.assertIn("暂不练这条", learn)
        self.assertNotIn('type="submit">暂不练习</button>', learn)
        self.assertIn("系统只会生成一条模拟观察记录", learn)
        self.assertIn("先回答三问并保存复盘,看到 6/6 后再看高级页面", learn)
        self.assertNotIn("再决定是否进入高级模拟盘看细节", learn)
        self.assertNotIn("确认,开始模拟观察", learn)
        self.assertNotIn("系统只会生成模拟成交", learn)
        self.assertLess(learn.index('id="today-practice"'), learn.index('id="learning-loop"'))
        self.assertNotIn("我看懂了,开始观察", learn)
        self.assertNotIn("先点一个就能开始", learn)
        status, _, detail = self.request("GET", f"/learn/tasks/{int(task['id'])}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("今日练习已生成", detail)
        self.assertIn("第一次闭环进度: 4/6", detail)
        self.assertIn("还差 2 步:生成观察记录、保存复盘", detail)
        self.assertIn("回学习页生成观察记录", detail)
        self.assertIn("回学习页确认后才会生成模拟观察记录", detail)
        self.assertIn("还没生成记录", detail)
        self.assertIn("DONE · 1/3", detail)
        self.assertIn("NOW · 2/3", detail)

    def test_learning_cancelled_practice_returns_to_generate_step(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnCancelPractice")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        task_id = services.create_learning_task(self.con, uid, "学习反转观察", "beginner", "reversal", "教练说明")
        count = services.create_practice_signals_from_learning_task(
            self.con,
            uid,
            task_id,
            "学习反转",
            "reversal",
            qty="100",
            limit=1,
            rationale_note="记录风险",
        )
        self.assertEqual(count, 1)
        signal = services.practice_signals(self.con, uid, status="pending")[0]

        status, headers, _ = self.request(
            "POST",
            f"/practice-signals/{int(signal['id'])}/cancel",
            body=self.form_body(uid, {"next": "/learn#today-practice"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        cancelled = services.practice_signals(self.con, uid)[0]
        self.assertEqual(cancelled["status"], "cancelled")
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("已完成 3/6", learn)
        self.assertIn("还剩 3 步,先做当前这一步", learn)
        self.assertIn("下一步:生成一个练习", learn)
        self.assertIn("一键生成今日练习", learn)
        self.assertIn("待生成练习", learn)
        self.assertIn("练习 0", learn)
        self.assertNotIn('id="today-practice"', learn)
        self.assertNotIn("下一步:保存第一次复盘", learn)

    def test_learning_first_button_with_key_uses_sample_task_without_ai_call(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnKeyFast")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        from src.app.ai import service as ai_service
        ai_service.save_key(
            self.con,
            uid,
            SECRET,
            "sk-routekey1234567890",
            "https://api.deepseek.com",
            "deepseek-v4-flash",
        )

        with patch("src.app.ai.client.chat_completion", side_effect=AssertionError("first loop should not call AI")):
            status, headers, _ = self.request(
                "POST",
                "/learn/sample-task",
                body=self.form_body(uid, {"preset": "0", "quick_start": "1"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#today-practice", headers.get("Location", ""))
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(task)
        self.assertIn("示例教练拆解", task["coach_text"])
        self.assertEqual(task["status"], "signals_saved")
        self.assertEqual(len(services.practice_signals(self.con, uid, status="pending")), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0], 0)

    def test_learning_coach_creates_task_with_mock_deepseek(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnCoach")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        from src.app.ai import service as ai_service
        ai_service.save_key(self.con, uid, SECRET, "sk-routekey1234567890",
                            "https://api.deepseek.com", "deepseek-v4-flash")
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("AI key 已配置,第一圈仍建议用推荐任务", learn)
        self.assertNotIn('<a href="/account/ai">AI教练</a>', learn)
        self.assertNotIn('<a href="/account/ai">AI教练(稍后)</a>', learn)
        self.assertIn("AI 教练已配置,但第一圈蓝色按钮仍先用内置示例教练", learn)
        self.assertIn("更快完成闭环", learn)
        self.assertIn("第一次先不要自己写提示词", learn)
        self.assertIn("可选:自己写学习目标", learn)
        self.assertIn('<details class="advanced-practice custom-ai-goal">', learn)
        self.assertNotIn('<details class="advanced-practice custom-ai-goal" open>', learn)
        self.assertNotIn("创建自定义 AI 学习任务", learn)
        self.assertIn("点击后发生什么", learn)
        self.assertIn("创建示例教练任务,并直接准备 1 条今日练习", learn)
        self.assertIn("安全边界", learn)
        self.assertIn("现在只做一件事", learn)
        self.assertIn("一键开始第一关", learn)
        self.assertIn('action="/learn/sample-task"', learn)
        self.assertIn('name="preset" value="0"', learn)
        self.assertIn('action="/learn/coach"', learn)
        self.assertIn("推荐第一关", learn)
        self.assertIn("点这里创建学习任务", learn)
        self.assertIn('textarea name="goal"', learn)
        self.assertIn("让 AI 拆解目标</button>", learn)
        self.assertLess(learn.index("一键开始第一关"), learn.index("可选:自己写学习目标"))
        self.assertLess(learn.index('action="/learn/sample-task"'), learn.index("可选:自己写学习目标"))
        answer = {
            "text": "### 目标拆解\n1. 先理解数据。\n2. 再做模拟盘草稿。\n\n|概念|用途|\n|---|---|\n|数据|练习材料|\n\n> 先做模拟,不要当成买卖建议。",
            "usage": {"total_tokens": 18},
            "model": "deepseek-v4-flash",
        }

        with patch("src.app.ai.client.chat_completion", return_value=answer):
            status, headers, _ = self.request(
                "POST",
                "/learn/coach",
                body=self.form_body(uid, {"goal": "我想学习低风险量化练习", "difficulty": "beginner", "template": "reversal"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/learn/tasks/", headers.get("Location", ""))
        self.assertIn("AI 教练已完成目标拆解", unquote(headers.get("Location", "")))
        self.assertIn("下一步点击“一键生成今日练习”", unquote(headers.get("Location", "")))
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["template"], "reversal")
        self.assertIn("目标拆解", task["coach_text"])
        usage = self.con.execute("SELECT request_kind FROM ai_usage WHERE user_id=?", (uid,)).fetchone()
        self.assertEqual(usage["request_kind"], "learning_coach")

        task_id = int(task["id"])
        status, _, detail = self.request("GET", f"/learn/tasks/{task_id}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("markdown-body", detail)
        self.assertIn("<h3>目标拆解</h3>", detail)
        self.assertIn("<ol>", detail)
        self.assertIn('class="markdown-table"', detail)
        self.assertIn('class="learning-mobile-table markdown-mobile-table"', detail)
        self.assertIn("<th>概念</th>", detail)
        self.assertIn('data-label="用途">练习材料</td>', detail)
        self.assertIn("<blockquote>先做模拟,不要当成买卖建议。</blockquote>", detail)
        self.assertIn("30 秒摘要", detail)
        self.assertIn("下面是把AI 教练长文压缩成的新手读法", detail)
        self.assertIn("你已经选了目标,也拿到了AI 教练拆解", detail)
        self.assertIn("系统已经给出AI 教练拆解", detail)
        self.assertNotIn("你已经选了目标,也拿到了示例教练拆解", detail)

    def test_learning_coach_provider_failure_falls_back_to_sample_task(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnCoachFallback")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        from src.app.ai import client as ai_client
        from src.app.ai import service as ai_service
        ai_service.save_key(self.con, uid, SECRET, "sk-routekey1234567890",
                            "https://api.deepseek.com", "deepseek-v4-flash")

        with patch("src.app.ai.client.chat_completion", side_effect=ai_client.ProviderError("network", category="network")):
            status, headers, _ = self.request(
                "POST",
                "/learn/coach",
                body=self.form_body(uid, {"goal": "我想学习低风险量化练习", "difficulty": "beginner", "template": "reversal"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/learn/tasks/", headers.get("Location", ""))
        self.assertIn("AI%20%E6%95%99%E7%BB%83%E6%9A%82%E6%97%B6%E4%B8%8D%E5%8F%AF%E7%94%A8", headers.get("Location", ""))
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["template"], "reversal")
        self.assertIn("AI 教练暂时不可用,先用示例教练继续", task["coach_text"])
        self.assertIn("示例教练拆解", task["coach_text"])
        usage = self.con.execute("SELECT status FROM ai_usage WHERE user_id=?", (uid,)).fetchone()
        self.assertEqual(usage["status"], "error:network")

        status, _, detail = self.request("GET", f"/learn/tasks/{int(task['id'])}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("AI 教练暂时不可用,先用示例教练继续", detail)
        self.assertIn("你已经选了目标,也拿到了示例教练拆解", detail)
        self.assertIn("刚完成 3/6:点蓝色按钮进入下一步", detail)
        self.assertIn("一键生成今日练习", detail)
        self.assertIn("第一次只按这个按钮", detail)

    def test_learning_coach_blocks_model_stock_tip(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnTip")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        from src.app.ai import service as ai_service
        ai_service.save_key(self.con, uid, SECRET, "sk-routekey1234567890",
                            "https://api.deepseek.com", "deepseek-v4-flash")
        tip = {"text": "建议买入 000001.SZ,目标价 15 元。", "usage": {"total_tokens": 9}, "model": "x"}

        with patch("src.app.ai.client.chat_completion", return_value=tip):
            status, headers, _ = self.request(
                "POST",
                "/learn/coach",
                body=self.form_body(uid, {"goal": "我该买啥", "difficulty": "beginner", "template": "reversal"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )
        self.assertEqual(status, 303)
        task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=?", (uid,)).fetchone()
        status, _, page = self.request("GET", f"/learn/tasks/{task['id']}", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertIn("触发了合规过滤", page)
        self.assertNotIn("目标价 15", page)

    def test_learn_page_shows_beginner_loop_progress_for_new_user(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnProgressNew")
        cookie = f"owq_session={self.sign_cookie(uid)}"

        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertIn("第一次学习闭环", learn)
        self.assertIn("已完成 0/6", learn)
        self.assertIn("预计 3-5 分钟跑完第一次闭环", learn)
        self.assertIn("NOW · 1/6", learn)
        self.assertIn("下一步:先记住一句话,再点按钮", learn)
        self.assertIn("量化学习不是让 AI 告诉你买什么", learn)
        self.assertIn("AI 在这里做什么", learn)
        self.assertIn("第一次只做什么", learn)
        self.assertIn("看完,点蓝色推荐按钮", learn)
        self.assertIn("第一步:先懂一句话,再一键开始", learn)
        self.assertIn("30 秒先懂一句话", learn)
        self.assertIn("量化投资不是猜涨跌", learn)
        self.assertIn("AI 在这里像教练", learn)
        self.assertIn("现在只做一件事", learn)
        self.assertIn("先记住一句话:量化投资不是猜涨跌", learn)
        self.assertIn(".starter-fast-path form,.starter-fast-path button", learn)
        self.assertIn('action="/learn/sample-task"', learn)
        self.assertIn('name="preset" value="0"', learn)
        self.assertIn('name="quick_start" value="1"', learn)
        self.assertIn("创建示例教练任务,并直接准备 1 条今日练习", learn)
        self.assertIn("下一步只确认观察材料", learn)
        self.assertNotIn('href="#starter-choice-recommended"', learn)
        self.assertIn('<details class="advanced-practice preset-library">', learn)
        self.assertIn("可选:展开新手关卡地图", learn)
        self.assertIn("第 1 关 · 先懂概念", learn)
        self.assertIn("第 2 关 · 做一次观察", learn)
        self.assertIn("第 3 关 · 风险和复盘", learn)
        self.assertLess(learn.index("一键开始第一关"), learn.index("30 秒先懂一句话"))
        self.assertLess(learn.index("一键开始第一关"), learn.index("可选:展开新手关卡地图"))
        self.assertIn("推荐第一关", learn)
        self.assertLess(learn.index('id="learn-presets"'), learn.index('id="learning-loop"'))

    def test_learning_task_preview_does_not_write_trading_state(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnPreview")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        task_id = services.create_learning_task(self.con, uid, "学习反转观察", "beginner", "reversal", "教练说明")
        before_cash = services.portfolio_snapshot(self.con, uid)["cash"]

        status, _, page = self.request(
            "POST",
            f"/learn/tasks/{task_id}/preview",
            body=self.form_body(uid, {"template": "reversal", "qty": "100", "limit": "2", "strategy_name": "学习反转", "rationale_note": "记录风险"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 200)
        self.assertIn("草稿预览", page)
        self.assertIn('<details class="card advanced-practice task-advanced-settings" open>', page)
        self.assertIn("观察动作", page)
        self.assertIn('<table class="learning-mobile-table">', page)
        self.assertIn('data-label="代码"', page)
        self.assertIn('data-label="依据"', page)
        self.assertIn("模拟观察数量", page)
        self.assertIn("观察 100", page)
        self.assertIn("不是买卖指令", page)
        self.assertIn("这只是模拟观察依据,不是买卖建议", page)
        self.assertNotIn("duckdb:none:tushare", page)
        self.assertNotIn("来源 duckdb", page)
        self.assertNotIn("<th>方向</th>", page)
        self.assertNotIn("<th>数量</th>", page)
        self.assertNotIn("<td>买入</td>", page)
        self.assertIn("保存当前草稿到今日练习", page)
        self.assertIn("还没保存草稿时不要跳到高级模拟盘", page)
        self.assertIn("高级模拟盘入口会在完成 6/6 后出现", page)
        self.assertNotIn('href="/app"', page)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM practice_signals WHERE user_id=?", (uid,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        self.assertEqual(services.portfolio_snapshot(self.con, uid)["cash"], before_cash)

    def test_learning_task_save_signals_creates_pending_linked_signals(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnSave")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        task_id = services.create_learning_task(self.con, uid, "学习动量观察", "beginner", "momentum", "教练说明")

        status, headers, _ = self.request(
            "POST",
            f"/learn/tasks/{task_id}/save-signals",
            body=self.form_body(uid, {"template": "momentum", "qty": "100", "limit": "2", "strategy_name": "学习动量", "rationale_note": "记录想练什么"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#today-practice", headers.get("Location", ""))
        signals = services.practice_signals(self.con, uid)
        self.assertEqual(len(signals), 2)
        self.assertTrue(all(s["status"] == "pending" for s in signals))
        self.assertTrue(all(int(s["learning_task_id"]) == task_id for s in signals))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("今日练习", learn)
        self.assertIn("刚完成 4/6:确认练习后生成观察记录", learn)
        self.assertIn("今日练习已经生成,但还没有生成观察记录", learn)
        self.assertIn("确认这 1 条模拟练习", learn)
        self.assertIn('id="first-practice-card"', learn)
        self.assertIn("一句话任务", learn)
        self.assertIn("点前只核对 3 件事", learn)
        self.assertIn("推荐先做这一条", learn)
        self.assertIn("第一次只点这一条的蓝色按钮", learn)
        self.assertIn("回看和取消入口都收在可选区", learn)
        self.assertIn("备用练习", learn)
        self.assertIn("这是备用练习", learn)
        self.assertIn("第一次闭环可以先留着不动", learn)
        self.assertIn("练习规模", learn)
        self.assertIn("观察 100", learn)
        self.assertIn("小数量,只为训练流程", learn)
        self.assertIn("学习重点", learn)
        self.assertIn("进入三问复盘", learn)
        self.assertIn("可选:查看代码和模拟价", learn)
        self.assertIn("第一次可以先不展开", learn)
        self.assertNotIn("参考价格", learn)
        self.assertIn("这只是模拟观察依据,不是买卖建议", learn)
        self.assertLess(learn.index("我为什么观察它?"), learn.index('<button class="blue" type="submit">生成模拟观察记录,去复盘</button>'))
        self.assertNotIn("duckdb:none:tushare", learn)
        self.assertNotIn("来源 duckdb", learn)
        self.assertNotIn("<strong>买入 100</strong>", learn)
        self.assertIn("第一次只看三件事", learn)
        self.assertIn("代码和价格已经收进可选详情", learn)
        self.assertIn("第一次只建议生成 1 条观察记录", learn)
        self.assertIn("点蓝色按钮后会自动跳到三问复盘", learn)
        self.assertIn("生成观察记录", learn)
        self.assertIn("其他先留着不动", learn)
        self.assertIn("可选:回看教练或管理这条练习", learn)
        self.assertIn("暂不练这条", learn)
        self.assertNotIn('type="submit">暂不练习</button>', learn)
        self.assertIn("来自你的学习任务", learn)
        self.assertIn("已完成 4/6", learn)
        self.assertIn("下一步:生成观察记录", learn)
        self.assertIn("去今日练习", learn)
        self.assertLess(learn.index('id="today-practice"'), learn.index('id="learning-loop"'))
        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("来自学习任务", app)

    def test_learning_today_practice_can_execute_from_learn_page(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnExecute")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        task_id = services.create_learning_task(self.con, uid, "学习反转观察", "beginner", "reversal", "教练说明")
        count = services.create_practice_signals_from_learning_task(
            self.con,
            uid,
            task_id,
            "学习反转",
            "reversal",
            qty="100",
            limit=1,
            rationale_note="记录风险",
        )
        self.assertEqual(count, 1)
        signal = services.practice_signals(self.con, uid, status="pending")[0]

        status, headers, _ = self.request(
            "POST",
            f"/practice-signals/{int(signal['id'])}/execute",
            body=self.form_body(uid, {"next": "/learn#learning-review"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("/learn?msg=", location)
        self.assertIn("#learning-review", location)
        decoded_location = unquote(location)
        self.assertIn("模拟观察记录已生成", decoded_location)
        self.assertIn("不是现实交易", decoded_location)
        self.assertNotIn("系统已生成一笔模拟成交", decoded_location)
        status, _, notice_page = self.request("GET", location.split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', notice_page)
        self.assertIn("5/6", notice_page)
        self.assertIn("观察记录已经生成", notice_page)
        self.assertIn("现在不用判断赚亏,先一键完成 6/6 并保存示例复盘", notice_page)
        self.assertIn("系统只记录了一次模拟观察,不是现实交易", notice_page)
        self.assertNotIn("系统已生成一笔模拟成交", notice_page)
        self.assertIn('href="#learning-review">完成 6/6</a>', notice_page)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        executed = services.practice_signals(self.con, uid)[0]
        self.assertEqual(executed["status"], "executed")
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertNotIn('type="submit">开始观察', learn)
        self.assertIn("观察复盘", learn)
        self.assertEqual(learn.count("<h2>观察复盘</h2>"), 1)
        self.assertNotIn("复盘已经保存。你已经完成 6/6", learn)
        self.assertLess(learn.index('id="learning-review"'), learn.index('id="learning-journey"'))
        self.assertLess(learn.index('id="learning-review"'), learn.index("<h2>新手 AI 学习工作台</h2>"))
        self.assertIn("你已经生成了一条模拟观察记录", learn)
        self.assertIn("小白模式:最后 30 秒完成 6/6", learn)
        self.assertIn("去完成 6/6", learn)
        self.assertIn("完成标志", learn)
        self.assertIn("页面显示 6/6 和学习徽章", learn)
        self.assertIn("刚完成 5/6:最后一步保存复盘", learn)
        self.assertIn("模拟观察记录已经生成。现在不用判断赚亏", learn)
        self.assertIn("目标、教练拆解、今日练习和观察记录已经串起来", learn)
        self.assertIn("保存“想练什么、有没有按规则做、下次改哪里”三句话,就解锁 6/6", learn)
        self.assertIn("预计 30 秒", learn)
        self.assertIn("直接点一键完成 6/6 也可以", learn)
        self.assertIn("完成标志", learn)
        self.assertIn("页面出现 6/6 和第一枚学习徽章", learn)
        self.assertIn("可以停下", learn)
        self.assertIn('class="review-unlock" aria-label="保存复盘后会看到什么"', learn)
        self.assertIn("点完马上看到", learn)
        self.assertIn("已完成 6/6、第一枚学习徽章和学习轨迹", learn)
        self.assertIn("不用会分析", learn)
        self.assertIn("示例复盘会先帮你留下三句大白话", learn)
        self.assertIn("今天可停", learn)
        self.assertIn("第一圈已经达标,不必马上进高级模拟盘", learn)
        self.assertIn('class="review-template-note"', learn)
        self.assertIn("系统会先保存这三句话", learn)
        self.assertIn("我想练什么、有没有按小数量规则做、下次先改哪一点", learn)
        self.assertIn("之后可以改成自己的话", learn)
        self.assertLess(learn.index("最后 30 秒"), learn.index("刚完成 5/6"))
        self.assertIn("先完成第一次复盘", learn)
        self.assertIn("已生成观察记录", learn)
        self.assertIn("第一次复盘不用判断涨跌", learn)
        self.assertIn("不需要 AI key", learn)
        self.assertIn("第一次复盘不用写专业分析", learn)
        self.assertIn("点击后就完成 6/6", learn)
        self.assertIn("最后 30 秒:点一下完成 6/6", learn)
        self.assertIn("先点“一键完成 6/6 并保存示例复盘”留下示例记录", learn)
        self.assertNotIn("先点右侧按钮", learn)
        self.assertIn('class="review-primary-cta"', learn)
        self.assertIn('class="review-focus-head review-first-action-head"', learn)
        self.assertIn(".review-first-action-head .review-primary-cta{order:-1}", learn)
        self.assertIn("点这个按钮就完成 6/6", learn)
        self.assertIn("不调用 AI", learn)
        self.assertIn("一键完成 6/6 并保存示例复盘", learn)
        self.assertLess(learn.index("一键完成 6/6 并保存示例复盘"), learn.index("刚完成 5/6"))
        self.assertLess(learn.index("一键完成 6/6 并保存示例复盘"), learn.index("想自己写? 展开手写三句话"))
        self.assertIn("想自己写? 展开手写三句话", learn)
        self.assertIn("第一次可以直接点上面的示例复盘", learn)
        self.assertIn('<details class="manual-reflection">', learn)
        self.assertNotIn('<details class="manual-reflection" open>', learn)
        self.assertIn("只写三句大白话就够了", learn)
        self.assertIn("观察材料", learn)
        self.assertIn("练习规模", learn)
        self.assertIn("观察 100", learn)
        self.assertIn("可选:查看代码和模拟记录价", learn)
        self.assertIn("复盘先写三句大白话", learn)
        self.assertIn("模拟记录价", learn)
        self.assertNotIn("模拟生成价", learn)
        self.assertIn("这只是模拟观察依据,不是买卖建议", learn)
        self.assertNotIn("duckdb:none:tushare", learn)
        self.assertNotIn("来源 duckdb", learn)
        self.assertIn("复盘焦点", learn)
        self.assertIn("先写三问", learn)
        self.assertIn("先不判断涨跌", learn)
        self.assertNotIn("模拟动作", learn)
        self.assertNotIn("<strong>买入 100</strong>", learn)
        self.assertNotIn("价格变化", learn)
        self.assertIn("我这次到底想练什么", learn)
        self.assertIn("我有没有按小数量和边界规则做", learn)
        self.assertIn("下次我先改哪一个小动作", learn)
        self.assertIn("1. 我这次想练什么", learn)
        self.assertIn("2. 我有没有按小数量规则做", learn)
        self.assertIn("3. 下次先改哪一点", learn)
        self.assertNotIn("1. 我的观察假设", learn)
        self.assertNotIn("2. 我是否按计划执行", learn)
        self.assertIn('<details class="advanced-practice review-optional-actions">', learn)
        self.assertIn("可选:AI 复盘和高级入口", learn)
        self.assertIn("第一次先不用展开;先点上方蓝色复盘按钮", learn)
        self.assertIn("AI 复盘和高级模拟盘都不是完成第一圈的前置条件", learn)
        self.assertNotIn("查看高级模拟盘细节", learn)
        self.assertNotIn('href="/app"', learn)
        self.assertIn("可选:配置 AI 教练复盘", learn)
        self.assertIn("配置 AI 教练复盘", learn)
        self.assertIn("保存我的复盘", learn)
        self.assertIn("已完成 5/6", learn)
        self.assertIn("还剩 1 步,先做当前这一步", learn)
        self.assertIn("下一步:保存第一次复盘", learn)
        self.assertIn("去完成 6/6", learn)
        self.assertIn("<span>下一步</span><b>保存复盘</b>", learn)
        self.assertIn('<a class="btn blue" href="#learning-review">继续</a>', learn)
        self.assertLess(learn.index('id="learning-review"'), learn.index('id="learning-loop"'))
        self.assertLess(learn.index('id="learning-review"'), learn.index('id="beginner-focus"'))
        self.assertLess(learn.index('id="beginner-focus"'), learn.index('id="learning-loop"'))
        status, _, detail = self.request("GET", f"/learn/tasks/{task_id}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第一次闭环进度: 5/6", detail)
        self.assertIn("还差 1 步:保存三问复盘", detail)
        self.assertIn("预计 30 秒完成", detail)
        self.assertIn("现在不用判断赚亏,也不用读完教练全文", detail)
        self.assertIn("只回学习页保存三问复盘", detail)
        self.assertIn("不用看涨跌", detail)
        self.assertIn("保存后会出现 6/6 和学习徽章", detail)

        status, headers, _ = self.request(
            "POST",
            "/learn/reflections/quick-save",
            body=self.form_body(uid, {"practice_signal_id": str(int(signal["id"]))}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#learning-review", headers.get("Location", ""))
        self.assertIn("6/6", headers.get("Location", ""))
        status, _, notice_page = self.request("GET", headers.get("Location", "").split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', notice_page)
        self.assertIn("6/6", notice_page)
        self.assertIn("第一次学习闭环完成", notice_page)
        self.assertIn("今天可以停在这里", notice_page)
        self.assertIn('href="#learning-journey">查看学习轨迹</a>', notice_page)
        reflection = services.learning_reflection_for_signal(self.con, uid, int(signal["id"]))
        self.assertIn("示例:我这次想练的是", reflection["hypothesis"])
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第一次学习闭环完成", learn)
        self.assertIn('<a href="/learn#learning-loop">当前进度</a>', learn)
        self.assertIn('<a href="/learn#learning-journey">学习轨迹</a>', learn)
        self.assertIn('<a href="/app">高级模拟盘</a>', learn)
        self.assertIn("学习成果", learn)
        self.assertEqual(learn.count("<h2>学习成果</h2>"), 1)
        self.assertIn("复盘已经保存。你已经完成 6/6", learn)
        self.assertNotIn("你已经生成了一条模拟观察记录。第一次复盘不用判断涨跌", learn)
        self.assertIn("你已经解锁 6/6", learn)
        self.assertIn("第一枚学习徽章已解锁", learn)
        self.assertNotIn('<div class="mobile-next-bar" role="navigation" aria-label="手机下一步提示">', learn)
        self.assertNotIn('class="mobile-next-spacer"', learn)
        self.assertIn("第一枚学习徽章:把想法变成可复盘练习", learn)
        self.assertIn("提出目标、生成练习、生成观察记录、保存复盘", learn)
        self.assertIn("查看我的学习轨迹", learn)
        self.assertIn("已保存复盘", learn)
        self.assertIn("第一次复盘已保存", learn)
        self.assertIn("已完成复盘", learn)
        self.assertNotIn("已生成观察记录", learn)
        self.assertNotIn("先完成第一次复盘", learn)
        self.assertIn("练习规模", learn)
        self.assertIn("观察 100", learn)
        self.assertIn("复盘焦点", learn)
        self.assertNotIn("<strong>买入 100</strong>", learn)
        self.assertNotIn("价格变化", learn)
        self.assertIn("示例:我这次想练的是", learn)
        self.assertIn("想修改自己的三句话? 展开手写复盘", learn)
        self.assertIn("你刚完成了第一次学习闭环", learn)
        self.assertIn("今天的第一圈已经达标,现在可以停在这里", learn)
        self.assertLess(learn.index("你刚完成了第一次学习闭环"), learn.index("观察材料"))
        self.assertLess(learn.index("你刚完成了第一次学习闭环"), learn.index("模拟记录价"))
        self.assertLess(learn.index("今天的第一圈已经达标"), learn.index("查看 6/6 成就"))
        self.assertIn("你完成了什么", learn)
        self.assertIn("复盘不是猜对错", learn)
        self.assertIn("想练什么、有没有按规则做、下次改哪一点", learn)
        self.assertIn("查看 6/6 成就", learn)
        self.assertIn('<details class="advanced-practice review-next-recommend">', learn)
        self.assertIn("可选:展开第二关建议", learn)
        self.assertIn("今天可以先停在这里;想巩固时再打开", learn)
        self.assertIn('id="next-visit-mission"', learn)
        self.assertIn("下次回来只做 3 分钟第二关", learn)
        self.assertIn("下次回来不要重新研究菜单", learn)
        self.assertIn("同样的小数量,换一个观察角度", learn)
        self.assertIn("可选任务,不影响今天已经完成的 6/6", learn)
        self.assertIn("回来先点哪里", learn)
        self.assertIn("学习轨迹会多一条对照记录", learn)
        self.assertLess(learn.index("现在可以停在这里"), learn.index("下次回来只做 3 分钟第二关"))
        mission_idx = learn.index('id="next-visit-mission"')
        self.assertLess(mission_idx, learn.index("<summary>可选:展开第二关建议", mission_idx))
        self.assertNotIn('<details class="advanced-practice review-next-recommend" open>', learn)
        self.assertIn("可选第二关: 做一次动量观察", learn)
        self.assertIn("今天第一圈已经达标,不用马上继续", learn)
        self.assertIn("可选:创建第二关", learn)
        self.assertNotIn("开始推荐下一关", learn)
        self.assertIn("第一圈已完成,AI key 是可选升级", learn)
        self.assertIn("配置 AI key 只是为了写自定义目标和 AI 复盘", learn)
        self.assertIn("不配置 key 也能继续使用示例教练和模拟练习", learn)
        self.assertIn("配置 AI 教练(可选)", learn)
        self.assertNotIn("现在怎么开始</b><p>先点上面的预设卡片", learn)
        self.assertNotIn("第一圈不用 DeepSeek key", learn)
        self.assertIn('name="preset" value="4"', learn)
        self.assertIn("已完成 6/6", learn)
        self.assertIn("第一次闭环已完成", learn)
        self.assertNotIn("刚完成 5/6:最后一步保存复盘", learn)
        status, _, detail = self.request("GET", f"/learn/tasks/{task_id}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第一次闭环进度: 6/6", detail)
        self.assertIn("第一次闭环已经完成", detail)
        self.assertIn("10 秒回看这一关", detail)
        self.assertIn("这一关已经完成。现在不用继续点交易相关按钮", detail)
        self.assertIn("今天可以停在这里", detail)
        self.assertIn("第一圈已经达标,不需要马上开新题", detail)

        status, headers, _ = self.request(
            "POST",
            "/learn/reflections",
            body=self.form_body(
                uid,
                {
                    "practice_signal_id": str(int(signal["id"])),
                    "hypothesis": "我想验证反转观察只是一条学习假设。",
                    "execution_check": "数量按计划执行,没有临时加仓。",
                    "adjustment": "下次先比较两个候选再观察。",
                },
            ),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn?msg=", headers.get("Location", ""))
        self.assertIn("#learning-review", headers.get("Location", ""))
        self.assertIn("6/6", headers.get("Location", ""))
        reflection = services.learning_reflection_for_signal(self.con, uid, int(signal["id"]))
        self.assertEqual(reflection["hypothesis"], "我想验证反转观察只是一条学习假设。")
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("已保存复盘", learn)
        self.assertIn("第一次复盘已保存", learn)
        self.assertIn("已完成复盘", learn)
        self.assertNotIn("已生成观察记录", learn)
        self.assertNotIn("先完成第一次复盘", learn)
        self.assertIn("我想验证反转观察只是一条学习假设", learn)
        self.assertIn("学习成果", learn)
        self.assertIn("复盘已经保存。你已经完成 6/6", learn)
        self.assertNotIn("你已经开始了一次模拟观察。第一次复盘不用判断涨跌", learn)
        self.assertIn("更新复盘", learn)
        self.assertIn("已完成 6/6", learn)
        self.assertNotIn("刚完成 5/6:最后一步保存复盘", learn)
        self.assertIn("今天的第一圈已经达标,现在可以停在这里", learn)

        self.assertIn("第一次学习闭环完成", learn)
        self.assertIn("第一圈已经达标", learn)
        self.assertIn("现在可以停在这里", learn)
        self.assertIn("查看我的学习轨迹", learn)
        self.assertIn("<summary>可选:展开第二关建议", learn)
        self.assertIn("今天不用继续;想巩固时再打开", learn)
        self.assertIn('id="next-visit-mission"', learn)
        self.assertIn("下次回来只做 3 分钟第二关", learn)
        self.assertIn("回来先点哪里", learn)
        self.assertIn("这次只比什么", learn)
        self.assertIn("继续给你什么反馈", learn)
        self.assertLess(learn.index("现在可以停在这里"), learn.index("下次回来只做 3 分钟第二关"))
        mission_idx = learn.index('id="next-visit-mission"')
        self.assertLess(mission_idx, learn.index("<summary>可选:展开第二关建议", mission_idx))
        self.assertLess(learn.index("查看 6/6 成就"), learn.index("<summary>可选:展开第二关建议", mission_idx))
        self.assertIn("你刚解锁的不是收益,而是一种学习能力", learn)
        self.assertIn("会提问题", learn)
        self.assertIn("会留证据", learn)
        self.assertIn("会做修正", learn)
        self.assertIn("可选:开始第二关", learn)
        self.assertIn("为什么是第二关?", learn)
        self.assertIn("这次重点看什么?", learn)
        self.assertIn("第一圈已完成,可以先停在这里", learn)
        self.assertIn("想巩固时再开第二关动量对照", learn)
        self.assertNotIn("开始第二关动量对照实验", learn)
        self.assertIn("第二关怎么比较?", learn)
        self.assertIn("上一关是什么?", learn)
        self.assertIn("下一关比什么?", learn)
        self.assertIn("带走一句话", learn)
        self.assertIn("你已经掌握的 3 个动作", learn)
        self.assertIn("下一关不是从零开始", learn)
        self.assertIn("把问题变成目标", learn)
        self.assertIn("把目标变成模拟练习", learn)
        self.assertIn("把结果变成复盘", learn)
        self.assertIn("我的学习轨迹", learn)
        self.assertIn("学习轨迹摘要", learn)
        self.assertIn("下次回来先看这里", learn)
        self.assertIn("journey-summary-takeaway", learn)
        self.assertIn("下一步建议", learn)
        self.assertIn("可选:查看第二关建议", learn)
        self.assertIn('href="#learning-loop">可选:查看第二关建议</a>', learn)
        self.assertIn("已完成复盘", learn)
        self.assertIn("练过的模板", learn)
        self.assertIn("累计练习计划", learn)
        self.assertIn("第 1 关", learn)
        self.assertIn("已复盘", learn)
        self.assertIn("回看拆解", learn)
        self.assertIn("这一关下次想改", learn)
        self.assertIn("下次先比较两个候选再观察", learn)
        self.assertIn("下一关推荐", learn)
        self.assertIn("可选第二关: 换一种策略练习", learn)
        self.assertIn("第三关: 先补风险边界", learn)
        self.assertIn("第四关: 学会复盘模拟盘", learn)
        self.assertIn("创建示例任务", learn)
        self.assertIn('action="/learn/next-task/quick-start"', learn)
        self.assertIn("一键开始第二关并生成练习", learn)
        self.assertIn('name="preset" value="4"', learn)

        before_ai_usage = self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0]
        status, headers, _ = self.request(
            "POST",
            "/learn/next-task/quick-start",
            body=self.form_body(uid, {"preset": "4"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        next_location = headers.get("Location", "")
        self.assertIn("/learn?msg=", next_location)
        self.assertIn("#today-practice", next_location)
        self.assertIn("第二关已准备好", unquote(next_location))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0], before_ai_usage)
        latest_task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        self.assertEqual(latest_task["template"], "momentum")
        second_signal = next(
            s for s in services.practice_signals(self.con, uid, status="pending")
            if int(s["learning_task_id"]) == int(latest_task["id"])
        )
        status, _, learn = self.request("GET", next_location.split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("我的学习轨迹", learn)
        self.assertIn("学习轨迹摘要", learn)
        self.assertIn("第二关已准备好", learn)
        self.assertIn("刚完成 4/6:确认练习后生成观察记录", learn)
        self.assertIn("今日练习已经生成", learn)
        self.assertNotIn("把最新目标生成今日练习", learn)
        self.assertNotIn(f'href="/learn/tasks/{int(latest_task["id"])}">生成今日练习</a>', learn)
        self.assertIn("第 2 关", learn)
        self.assertIn("待观察", learn)
        self.assertIn("生成观察记录", learn)
        self.assertIn("下一关已经创建", learn)
        self.assertIn("可选:继续第二关", learn)
        self.assertNotIn("回访继续:第 2 关待生成练习", learn)
        self.assertIn("推荐先做这一条", learn)
        self.assertIn("确认这 1 条模拟练习", learn)
        self.assertIn("生成模拟观察记录,去复盘", learn)
        self.assertIn("不会自动成交", learn)
        self.assertLess(learn.index('id="today-practice"'), learn.index('id="learning-loop"'))
        self.assertIn("复盘后的“下次改什么”会留在这里", learn)
        self.assertIn("这一关下次想改", learn)
        status, headers, _ = self.request(
            "POST",
            f"/practice-signals/{int(second_signal['id'])}/execute",
            body=self.form_body(uid, {"next": "/learn#learning-review"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("先完成待复盘的观察", learn)
        self.assertIn("第二关对照复盘:先比较,再保存", learn)
        self.assertIn("一键保存对照复盘", learn)
        self.assertIn("点这个按钮会保存示例复盘", learn)
        self.assertIn("不会产生现实交易", learn)
        self.assertIn('class="review-unlock" aria-label="保存对照复盘后会看到什么"', learn)
        self.assertIn("这一关变成已复盘,学习轨迹会多一条对照记录", learn)
        self.assertIn("只比较观察角度、数量边界和上次想改的小动作", learn)
        self.assertIn("示例复盘只是先占位,之后可以展开改成自己的话", learn)
        self.assertIn("系统会先保存这三句话", learn)
        self.assertIn("这次复盘要和上一关比较", learn)
        self.assertIn("上一关: 反转观察; 这一关: 动量观察", learn)
        self.assertIn("只比较三件事:候选来源、数量边界、上次想改的小动作有没有用上", learn)
        self.assertIn("候选怎么来?", learn)
        self.assertIn("边界是否一样?", learn)
        self.assertIn("上次想改的小动作", learn)
        self.assertIn("下次先比较两个候选再观察", learn)
        self.assertIn("完成第二关对照复盘", learn)
        self.assertIn("第二关先做对照复盘", learn)
        self.assertIn("先用对照示例复盘", learn)
        self.assertIn("想自己写对照? 展开三问", learn)
        self.assertIn("这一关和上一关的观察角度哪里不同", learn)
        self.assertIn("上次想改的小动作这次有没有用上", learn)

        status, headers, _ = self.request(
            "POST",
            "/learn/reflections/quick-save",
            body=self.form_body(uid, {"practice_signal_id": str(int(second_signal["id"]))}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("#learning-review", headers.get("Location", ""))
        second_reflection = services.learning_reflection_for_signal(self.con, uid, int(second_signal["id"]))
        self.assertIn("和上一关 反转观察 做对照", second_reflection["hypothesis"])
        self.assertIn("上次提醒自己", second_reflection["adjustment"])
        self.assertIn("比较候选来源和边界", second_reflection["adjustment"])
        status, _, learn = self.request("GET", "/learn", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("策略视角对照已解锁", learn)
        self.assertIn("2 VIEW", learn)
        self.assertIn("第一种视角: 反转观察", learn)
        self.assertIn("第二种视角: 动量观察", learn)
        self.assertIn("进入第三关风险边界", learn)
        self.assertIn("学习才不会只停留在看涨跌", learn)
        self.assertIn("下次回来只做 3 分钟风险边界", learn)
        self.assertIn("一键开始第三关风险练习", learn)
        self.assertIn('action="/learn/next-task/quick-start"', learn)
        self.assertIn('name="preset" value="6"', learn)
        self.assertIn("可选:展开第三关建议", learn)
        self.assertIn("可选第三关: 先补风险边界", learn)
        self.assertNotIn("一键开始第二关并生成练习", learn)

        before_third_ai_usage = self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0]
        status, headers, _ = self.request(
            "POST",
            "/learn/next-task/quick-start",
            body=self.form_body(uid, {"preset": "6"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        third_location = headers.get("Location", "")
        self.assertIn("/learn?msg=", third_location)
        self.assertIn("#today-practice", third_location)
        self.assertIn("第三关风险边界已准备好", unquote(third_location))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM ai_usage WHERE user_id=?", (uid,)).fetchone()[0], before_third_ai_usage)
        latest_task = self.con.execute("SELECT * FROM learning_tasks WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        self.assertEqual(latest_task["template"], "risk_review")
        third_signal = next(
            s for s in services.practice_signals(self.con, uid, status="pending")
            if int(s["learning_task_id"]) == int(latest_task["id"])
        )
        self.assertIn("风险边界一键开始", third_signal["rationale"])
        status, _, risk_learn = self.request("GET", third_location.split("#", 1)[0], headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("第三关风险边界已准备好", risk_learn)
        self.assertIn("刚完成 4/6:确认练习后生成观察记录", risk_learn)
        self.assertIn("风险控制复盘", risk_learn)
        self.assertIn("生成模拟观察记录,去复盘", risk_learn)
        status, _, risk_task = self.request("GET", f"/learn/tasks/{int(latest_task['id'])}", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("风险控制复盘", risk_task)
        self.assertIn("风险复盘不直接等于买入卖出", risk_task)
        self.assertIn("第三关只看 3 个风险边界", risk_task)
        self.assertIn("今日练习已生成", risk_task)
        self.assertIn("回学习页生成观察记录", risk_task)
        self.assertIn("这关已经生成过今日练习", risk_task)
        self.assertIn("不用展开进阶设置", risk_task)
        self.assertIn("数量边界", risk_task)
        self.assertIn("回撤边界", risk_task)
        self.assertIn("停止条件", risk_task)
        self.assertIn("每个对象只用小数量", risk_task)
        self.assertIn("系统仍会用一条小数量观察作为材料", risk_task)

    def test_app_guides_zero_beginner_back_to_learning_workspace(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "AppLearnFirst")
        cookie = f"owq_session={self.sign_cookie(uid)}"

        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertIn('id="app-learning-first"', app)
        self.assertIn("你现在在高级模拟盘,第一次建议先回学习工作台", app)
        self.assertIn("回学习工作台选第一个目标", app)
        self.assertIn('/learn#learn-presets', app)
        self.assertIn("先不手动下单", app)
        self.assertIn("学习页会带路", app)
        self.assertIn("模拟交易", app)
        self.assertIn('<table class="mobile-card-table"><thead><tr><th>代码</th><th>名称</th><th>价格</th><th>涨跌</th></tr></thead>', app)
        self.assertIn('data-label="价格"', app)
        self.assertIn('data-label="状态" colspan="8" class="muted">暂无持仓', app)

        task_id = services.create_learning_task(self.con, uid, "学习反转观察", "beginner", "reversal", "教练说明")
        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})

        self.assertEqual(status, 200)
        self.assertIn("你已经有学习任务,先回学习页生成练习", app)
        self.assertIn("回学习页生成今日练习", app)
        self.assertIn('/learn#continue-learning-task', app)
        self.assertIn(f"/learn/tasks/{task_id}", self.request("GET", "/learn", headers={"Cookie": cookie})[2])

    def test_learning_signal_execute_from_app_redirects_to_review(self):
        uid = services.confirm_wechat_session(self.con, services.create_wechat_session(self.con), "LearnExecuteFromApp")
        cookie = f"owq_session={self.sign_cookie(uid)}"
        task_id = services.create_learning_task(self.con, uid, "学习反转观察", "beginner", "reversal", "教练说明")
        count = services.create_practice_signals_from_learning_task(
            self.con,
            uid,
            task_id,
            "学习反转",
            "reversal",
            qty="100",
            limit=1,
            rationale_note="记录风险",
        )
        self.assertEqual(count, 1)
        signal = services.practice_signals(self.con, uid, status="pending")[0]

        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("来自学习任务的待执行计划", app)
        self.assertIn("回学习页生成观察记录", app)
        self.assertIn('name="next" value="/learn#learning-review"', app)

        status, headers, _ = self.request(
            "POST",
            f"/practice-signals/{int(signal['id'])}/execute",
            body=self.form_body(uid, {}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("/learn?msg=", location)
        self.assertIn("#learning-review", location)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        executed = services.practice_signals(self.con, uid)[0]
        self.assertEqual(executed["status"], "executed")
        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("学习观察没复盘", app)
        self.assertIn("回学习页复盘", app)

    def test_public_profile_shows_current_holdings_and_recent_orders(self):
        user_id = services.get_or_create_user(self.con, "dev-profile-openid", "ProfileRoute")
        services.join_active_contest(self.con, user_id)
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)

        status, _, profile = self.request("GET", f"/u/{user_id}")
        self.assertEqual(status, 200)
        self.assertIn("当前持仓", profile)
        self.assertIn("最近成交", profile)
        self.assertIn("打开战绩卡", profile)
        self.assertIn('property="og:image"', profile)
        self.assertIn(f"/u/{user_id}/card.svg", profile)
        self.assertIn("000001.SZ", profile)
        self.assertIn("买入", profile)

        status, headers, card = self.request("GET", f"/u/{user_id}/card.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", headers.get("Content-Type", ""))
        self.assertIn("OurWorlds Quant 模拟盘", card)
        self.assertIn("ProfileRoute", card)
        self.assertIn("000001.SZ", card)

    def test_email_magic_link_sets_session_cookie(self):
        token, _ = self.start_registration(email="routelogin@example.com")

        status, headers, _ = self.request("GET", f"/auth/email/confirm?token={token}")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/auth/email/confirm")
        confirm_cookie = headers.get("Set-Cookie", "")
        self.assertIn("owq_email_confirm=", confirm_cookie)
        self.assertNotIn("owq_session=", confirm_cookie)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE email='routelogin@example.com'").fetchone()[0], 0)
        self.assertEqual(services.email_login_session_status(self.con, token)["status"], "pending")

        confirm_cookie = confirm_cookie.split(";", 1)[0]
        status, headers, payload = self.request("GET", "/auth/email/confirm", headers={"Cookie": confirm_cookie})
        self.assertEqual(status, 200)
        self.assertIn("设置登录账号", payload)
        self.assertIn("先进入学习工作台", payload)
        self.assertIn("第一屏先懂一句话,再点蓝色推荐按钮", payload)
        self.assertIn("不用配置 DeepSeek key", payload)
        self.assertIn("设置密码后自动进入学习工作台", payload)
        self.assertIn("现在填什么</b><span>用户名已自动填好,先输入两遍密码", payload)
        self.assertIn("点完去哪</b><span>自动登录并进入学习工作台", payload)
        self.assertIn("用户名</b><p>系统已经按邮箱自动填好", payload)
        self.assertIn("第一次看不懂规则时,先不要改", payload)
        self.assertIn("密码怎么写</b><p>至少 10 位,同时包含字母和数字", payload)
        self.assertIn("英文词 + 数字", payload)
        self.assertIn("确认密码</b><p>把同一个密码再输入一遍", payload)
        self.assertIn('aria-describedby="login-name-help"', payload)
        self.assertIn('aria-describedby="password-help"', payload)
        self.assertIn("已按邮箱自动生成,一般不用改", payload)
        self.assertIn("不要直接使用页面里的示例格式", payload)
        self.assertIn('id="email-password-form"', payload)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机设置密码下一步提示">', payload)
        self.assertIn("<span>最后一步</span><b>设置密码后自动进入学习工作台</b>", payload)
        self.assertIn('<a class="btn blue" href="#email-password-form">去设置</a>', payload)
        self.assertIn("设置密码并进入学习工作台", payload)
        self.assertNotIn("自动登录进入模拟盘", payload)
        self.assertIn('method="post" action="/auth/email/confirm"', payload)
        self.assertNotIn(token, payload)
        self.assertNotIn("owq_session=", headers.get("Set-Cookie", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE email='routelogin@example.com'").fetchone()[0], 0)
        self.assertEqual(services.email_login_session_status(self.con, token)["status"], "pending")

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "route-login", "password": "Password1234", "password_confirm": "Password5678"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/auth/email/confirm?err=", headers.get("Location", ""))
        self.assertIn("#email-password-form", headers.get("Location", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE email='routelogin@example.com'").fetchone()[0], 0)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "route-login", "password": "Password1234", "password_confirm": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        # Auto-login: the set-password response now establishes a session (the session cookie is
        # the last Set-Cookie header, shadowing the email_confirm-clear cookie in this dict helper).
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))
        self.assertNotIn("/login", headers.get("Location", ""))
        self.assertNotIn("routelogin", headers.get("Location", ""))
        self.assertNotIn("email=", headers.get("Location", ""))
        user = self.con.execute("SELECT id, login_name, password_hash FROM users WHERE email='routelogin@example.com'").fetchone()
        self.assertEqual(user["login_name"], "route-login")
        self.assertTrue(user["password_hash"].startswith("pbkdf2_sha256$"))
        session_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        status, _, learn_after_register = self.request("GET", headers.get("Location", ""), headers={"Cookie": session_cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', learn_after_register)
        self.assertIn("0/6", learn_after_register)
        self.assertIn("欢迎来到学习工作台", learn_after_register)
        self.assertIn("现在不用找菜单、配置 key 或写提示词", learn_after_register)
        self.assertIn('method="post" action="/learn/sample-task"', learn_after_register)
        self.assertIn('name="preset" value="0"', learn_after_register)
        self.assertIn('name="quick_start" value="1"', learn_after_register)
        self.assertIn('class="btn blue" type="submit">一键开始第一关</button>', learn_after_register)
        self.assertLess(learn_after_register.index('class="msg learning-notice"'), learn_after_register.index('id="learn-presets"'))
        consent = services.latest_user_consent(self.con, int(user["id"]))
        self.assertIsNotNone(consent)
        self.assertEqual(consent["terms_version"], "2026-06-24")
        self.assertEqual(consent["source"], "email_login")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "legal.consent")

        status, headers, _ = self.request(
            "POST",
            "/login",
            body=urlencode({"identifier": "route-login", "password": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn("/learn", headers.get("Location", ""))
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))

        cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        status, _, learn_after_login = self.request("GET", headers.get("Location", ""), headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="msg learning-notice" role="status"', learn_after_login)
        self.assertIn("0/6", learn_after_login)
        self.assertIn("欢迎来到学习工作台", learn_after_login)
        self.assertIn('method="post" action="/learn/sample-task"', learn_after_login)
        self.assertIn('name="quick_start" value="1"', learn_after_login)
        self.assertIn('class="btn blue" type="submit">一键开始第一关</button>', learn_after_login)
        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("用户同意记录", admin)
        self.assertIn("routelogin", admin)

    def test_email_registration_code_sets_password_without_magic_link(self):
        _, payload = self.start_registration(email="code-route@example.com")
        code = self.extract_dev_code(payload)

        status, _, code_entry = self.request("GET", "/auth/email/confirm")
        self.assertEqual(status, 200)
        self.assertIn("先输入邮箱里的 8 位注册码", code_entry)
        self.assertIn("下一页只需要设置用户名和密码", code_entry)
        self.assertIn("自动进入学习工作台", code_entry)
        self.assertIn('id="email-code-form"', code_entry)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机注册码下一步提示">', code_entry)
        self.assertIn("<span>下一步</span><b>输入邮箱和 8 位注册码</b>", code_entry)
        self.assertIn('<a class="btn blue" href="#email-code-form">去输入</a>', code_entry)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/code",
            body=urlencode({"email": "code-route@example.com", "code": code}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/auth/email/confirm")
        confirm_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.assertIn("owq_email_confirm=", confirm_cookie)

        status, _, confirm = self.request("GET", "/auth/email/confirm", headers={"Cookie": confirm_cookie})
        self.assertEqual(status, 200)
        self.assertIn("设置登录账号", confirm)
        self.assertIn("邮箱已确认", confirm)
        self.assertIn("先进入学习工作台", confirm)
        self.assertIn("第一屏先懂一句话,再点蓝色推荐按钮", confirm)
        self.assertIn("不用配置 DeepSeek key", confirm)
        self.assertIn("设置密码后自动进入学习工作台", confirm)
        self.assertIn("点蓝色推荐按钮开始第一关", confirm)
        self.assertIn('id="email-password-form"', confirm)
        self.assertIn("设置密码并进入学习工作台", confirm)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "code-route", "password": "Password1234", "password_confirm": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        # New behavior: set-password auto-logs the user in (no bounce back to /login).
        loc = headers.get("Location", "")
        self.assertNotIn("/login", loc)
        new_cookie = headers.get("Set-Cookie", "")
        self.assertIn("owq_session=", new_cookie)
        user = self.con.execute("SELECT id FROM users WHERE email='code-route@example.com'").fetchone()
        self.assertIsNotNone(user)
        self.assertEqual(services.authenticate_user(self.con, "code-route@example.com", "Password1234"), user["id"])
        # The freshly issued session cookie actually works.
        session_cookie = new_cookie.split(";", 1)[0]
        st2, _, _ = self.request("GET", "/account", headers={"Cookie": session_cookie})
        self.assertEqual(st2, 200)

    def test_email_code_page_rejects_wrong_code_without_leaking_email(self):
        _, payload = self.start_registration(email="wrong-code-route@example.com")
        code = self.extract_dev_code(payload)
        wrong = "00000000" if code != "00000000" else "11111111"

        status, headers, _ = self.request(
            "POST",
            "/auth/email/code",
            body=urlencode({"email": "wrong-code-route@example.com", "code": wrong}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        self.assertIn("/auth/email/confirm?err=", headers.get("Location", ""))
        self.assertNotIn("wrong-code-route@example.com", headers.get("Location", ""))
        row = self.con.execute(
            "SELECT code_attempts, status FROM email_login_sessions WHERE email='wrong-code-route@example.com'"
        ).fetchone()
        self.assertEqual(row["code_attempts"], 1)
        self.assertEqual(row["status"], "pending")

    def test_failed_password_login_does_not_echo_identifier_in_url(self):
        user_id = services.get_or_create_email_user(self.con, "login-leak@example.com")
        services.set_user_password(self.con, user_id, "login-leak", "Password1234")

        status, headers, _ = self.request(
            "POST",
            "/login",
            body=urlencode({"identifier": "login-leak@example.com", "password": "WrongPassword1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn("login-leak", location)
        self.assertNotIn("identifier=", location)

    def test_login_identifier_rate_limit_blocks_repeated_attempts_without_leaking_identifier(self):
        user_id = services.get_or_create_email_user(self.con, "victim@example.com")
        services.set_user_password(self.con, user_id, "victim-user", "Password1234")
        last_status = 0
        last_headers = {}
        for _ in range(9):
            last_status, last_headers, _ = self.request(
                "POST",
                "/login",
                body=urlencode({"identifier": "victim@example.com", "password": "WrongPassword1234"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        self.assertEqual(last_status, 303)
        self.assertIn("err=", last_headers.get("Location", ""))
        self.assertNotIn("identifier=victim", last_headers.get("Location", ""))
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.rate_limited")
        self.assertEqual(event["target_id"], "auth.login.identifier")
        self.assertIn('"identifier_type": "email"', event["detail"])
        self.assertIn('"identifier_hash":', event["detail"])
        self.assertNotIn("victim@example.com", event["detail"])

    def test_successful_login_clears_identifier_rate_limit_bucket(self):
        user_id = services.get_or_create_email_user(self.con, "clear-limit@example.com")
        services.set_user_password(self.con, user_id, "clear-limit", "Password1234")
        digest = hmac.new(SECRET.encode(), b"clear-limit", hashlib.sha256).hexdigest()
        bucket_key = ("auth:login:identifier", f"identifier:{digest}")

        for _ in range(2):
            status, _, _ = self.request(
                "POST",
                "/login",
                body=urlencode({"identifier": "clear-limit", "password": "WrongPassword1234"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 303)
        self.assertIn(bucket_key, RATE_LIMIT_BUCKETS)

        status, headers, _ = self.request(
            "POST",
            "/login",
            body=urlencode({"identifier": "clear-limit", "password": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        self.assertIn("/learn", headers.get("Location", ""))
        self.assertNotIn(bucket_key, RATE_LIMIT_BUCKETS)

    def test_logout_requires_post_csrf_and_clears_cookie(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "LogoutRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, account = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('method="post" action="/logout"', account)
        self.assertNotIn('href="/logout"', account)

        status, headers, _ = self.request("GET", "/logout", headers={"Cookie": cookie})
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))
        self.assertNotIn("Max-Age=0", headers.get("Set-Cookie", ""))

        status, headers, _ = self.request(
            "POST",
            "/logout",
            body=urlencode({}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))
        self.assertNotIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertEqual(services.audit_events(self.con)[0]["action"], "security.csrf_failed")

        status, headers, _ = self.request(
            "POST",
            "/logout",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertEqual(services.audit_events(self.con)[0]["action"], "auth.logout")
        self.assertEqual(services.user_session_version(services.get_user(self.con, user_id)), 2)

        status, headers, _ = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))

        legacy_sig = hmac.new(SECRET.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
        status, headers, _ = self.request("GET", "/account", headers={"Cookie": f"owq_session={user_id}:{legacy_sig}"})
        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))

    def test_signed_session_cookie_expires_server_side(self):
        with patch("src.app.server.time.time", return_value=1000):
            cookie = sign_user(42, ttl_seconds=10)

        with patch("src.app.server.time.time", return_value=1009):
            self.assertEqual(verify_cookie(cookie), 42)
        with patch("src.app.server.time.time", return_value=1011):
            self.assertIsNone(verify_cookie(cookie))

    def test_legacy_session_cookie_still_verifies_during_transition(self):
        sig = hmac.new(SECRET.encode(), b"42", hashlib.sha256).hexdigest()

        self.assertEqual(verify_cookie(f"42:{sig}"), 42)

    def test_email_magic_link_can_only_be_used_once(self):
        token, _ = self.start_registration(email="once@example.com")

        status, headers, _ = self.request("GET", f"/auth/email/confirm?token={token}")
        self.assertEqual(status, 303)
        confirm_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.assertIn("owq_email_confirm=", confirm_cookie)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "once-user", "password": "Password1234", "password_confirm": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertNotIn("/login", headers.get("Location", ""))  # auto-login
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "once-user", "password": "Password1234", "password_confirm": "Password1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE email='once@example.com'").fetchone()[0], 1)

    def test_existing_email_user_can_reset_password_without_renaming_profile(self):
        user_id = services.get_or_create_email_user(self.con, "reset-route@example.com")
        services.set_user_password(self.con, user_id, "reset-route", "Password1234")
        services.update_user_profile(self.con, user_id, "Route Public Name", "https://img.example/route.png")
        old_cookie = f"owq_session={self.sign_cookie(user_id)}"

        token, _ = self.start_registration(email="reset-route@example.com")
        status, headers, _ = self.request("GET", f"/auth/email/confirm?token={token}")
        self.assertEqual(status, 303)
        confirm_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]

        status, _, payload = self.request("GET", "/auth/email/confirm", headers={"Cookie": confirm_cookie})
        self.assertEqual(status, 200)
        self.assertIn("重置登录密码", payload)
        self.assertIn('value="reset-route"', payload)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "reset-route", "password": "NewPassword1234", "password_confirm": "NewPassword1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        # Auto-login after reset too: lands in-app with a fresh session, not on /login.
        self.assertNotIn("/login", headers.get("Location", ""))
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))
        user = services.get_user(self.con, user_id)
        self.assertEqual(user["login_name"], "reset-route")
        self.assertEqual(user["nickname"], "Route Public Name")
        self.assertEqual(user["avatar_url"], "https://img.example/route.png")
        self.assertIsNone(services.authenticate_user(self.con, "reset-route", "Password1234"))
        self.assertEqual(services.authenticate_user(self.con, "reset-route", "NewPassword1234"), user_id)

        status, headers, _ = self.request("GET", "/account", headers={"Cookie": old_cookie})
        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))

    def test_forgot_password_resets_existing_email_user(self):
        user_id = services.get_or_create_email_user(self.con, "forgot-route@example.com")
        services.set_user_password(self.con, user_id, "forgot-route", "Password1234")

        status, _, page = self.request("GET", "/forgot-password")
        self.assertEqual(status, 200)
        self.assertIn("重置登录密码", page)
        self.assertIn('action="/forgot-password"', page)

        status, _, payload = self.request(
            "POST",
            "/forgot-password",
            body=urlencode({"email": "forgot-route@example.com", "accept_terms": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 200)
        self.assertIn("测试重置密码链接已生成", payload)
        match = re.search(r"/auth/email/confirm\?token=([^\"&]+)", payload)
        self.assertIsNotNone(match)
        token = match.group(1)

        status, headers, _ = self.request("GET", f"/auth/email/confirm?token={token}")
        self.assertEqual(status, 303)
        confirm_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]

        status, _, confirm = self.request("GET", "/auth/email/confirm", headers={"Cookie": confirm_cookie})
        self.assertEqual(status, 200)
        self.assertIn("重置登录密码", confirm)
        self.assertIn('href="/forgot-password"', confirm)
        self.assertNotIn(token, confirm)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "forgot-route", "password": "NewPassword1234", "password_confirm": "NewPassword1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertNotIn("/login", headers.get("Location", ""))  # auto-login after reset
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))
        self.assertIsNone(services.authenticate_user(self.con, "forgot-route", "Password1234"))
        self.assertEqual(services.authenticate_user(self.con, "forgot-route", "NewPassword1234"), user_id)

    def test_forgot_password_code_resets_existing_email_user(self):
        user_id = services.get_or_create_email_user(self.con, "forgot-code@example.com")
        services.set_user_password(self.con, user_id, "forgot-code", "Password1234")

        status, _, payload = self.request(
            "POST",
            "/forgot-password",
            body=urlencode({"email": "forgot-code@example.com", "accept_terms": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 200)
        code = self.extract_dev_code(payload)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/code",
            body=urlencode({"email": "forgot-code@example.com", "code": code}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        confirm_cookie = headers.get("Set-Cookie", "").split(";", 1)[0]

        status, _, confirm = self.request("GET", "/auth/email/confirm", headers={"Cookie": confirm_cookie})
        self.assertEqual(status, 200)
        self.assertIn("重置登录密码", confirm)

        status, headers, _ = self.request(
            "POST",
            "/auth/email/confirm",
            body=urlencode({"login_name": "forgot-code", "password": "NewPassword1234", "password_confirm": "NewPassword1234"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": confirm_cookie},
        )
        self.assertEqual(status, 303)
        self.assertNotIn("/login", headers.get("Location", ""))  # auto-login after reset
        self.assertIn("owq_session=", headers.get("Set-Cookie", ""))
        self.assertIsNone(services.authenticate_user(self.con, "forgot-code", "Password1234"))
        self.assertEqual(services.authenticate_user(self.con, "forgot-code@example.com", "NewPassword1234"), user_id)

    def test_forgot_password_does_not_create_session_for_unknown_email(self):
        status, _, payload = self.request(
            "POST",
            "/forgot-password",
            body=urlencode({"email": "missing-route@example.com", "accept_terms": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 200)
        self.assertIn("重置密码邮件已处理", payload)
        self.assertNotIn("missing-route@example.com", payload)
        self.assertNotIn("/auth/email/confirm?token=", payload)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='missing-route@example.com'").fetchone()[0],
            0,
        )
        self.assertEqual(services.audit_events(self.con)[0]["action"], "auth.password_reset_requested")

    def test_register_sends_email_when_sender_is_configured(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_login_email", return_value="smtp") as sender:
                status, _, payload = self.request(
                    "POST",
                    "/register",
                    body=urlencode({"email": "send@example.com", "accept_terms": "1"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        self.assertEqual(status, 200)
        self.assertIn("验证邮件已发送", payload)
        self.assertIn("现在去邮箱复制 8 位注册码", payload)
        self.assertIn("去邮箱复制 8 位数字注册码", payload)
        self.assertIn("设置密码后自动进入学习工作台", payload)
        self.assertIn('<div class="mobile-next-bar" role="navigation" aria-label="手机邮箱验证下一步提示">', payload)
        self.assertIn('<a class="btn blue" href="/auth/email/confirm">去输入</a>', payload)
        sender.assert_called_once()
        row = self.con.execute("SELECT email, sent_at FROM email_login_sessions WHERE email='send@example.com'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["sent_at"])

    def test_forgot_password_sends_reset_email_when_sender_is_configured(self):
        user_id = services.get_or_create_email_user(self.con, "mail-reset@example.com")
        services.set_user_password(self.con, user_id, "mail-reset", "Password1234")
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_password_reset_email", return_value="smtp") as sender:
                status, _, payload = self.request(
                    "POST",
                    "/forgot-password",
                    body=urlencode({"email": "mail-reset@example.com", "accept_terms": "1"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        self.assertEqual(status, 200)
        self.assertIn("重置密码邮件已处理", payload)
        sender.assert_called_once()
        row = self.con.execute("SELECT email, sent_at FROM email_login_sessions WHERE email='mail-reset@example.com'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["sent_at"])

    def test_forgot_password_sends_setup_email_for_existing_user_without_password(self):
        user_id = services.get_or_create_email_user(self.con, "mail-setup@example.com")
        row = self.con.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        self.assertFalse(row["password_hash"])
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_password_reset_email", return_value="smtp") as sender:
                status, _, payload = self.request(
                    "POST",
                    "/forgot-password",
                    body=urlencode({"email": "mail-setup@example.com", "accept_terms": "1"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        self.assertEqual(status, 200)
        self.assertIn("重置密码邮件已处理", payload)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], "mail-setup@example.com")
        self.assertRegex(sender.call_args.args[2], r"^\d{8}$")
        row = self.con.execute("SELECT email, sent_at FROM email_login_sessions WHERE email='mail-setup@example.com'").fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row["sent_at"])
        event = next(item for item in services.audit_events(self.con) if item["action"] == "auth.password_reset_requested")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["known_account"], "1")
        self.assertEqual(detail["has_password"], "0")

    def test_invalid_smtp_sender_config_disables_email_login(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            status, _, payload = self.request("GET", "/register")
            self.assertEqual(status, 200)
            self.assertIn("邮箱注册暂未开放", payload)

            status, headers, _ = self.request(
                "POST",
                "/register",
                body=urlencode({"email": "bad-sender@example.com", "accept_terms": "1"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertNotIn("bad-sender", headers.get("Location", ""))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='bad-sender@example.com'").fetchone()[0], 0)

    def test_explicit_cloudflare_provider_does_not_fall_back_to_smtp(self):
        handler = object.__new__(AppHandler)
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_PROVIDER": "cloudflare",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "CLOUDFLARE_ACCOUNT_ID": "acct_123",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            self.assertEqual(handler.email_sender_provider(), "")

    def test_cloudflare_email_sender_posts_official_rest_payload(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @staticmethod
            def read():
                return b'{"success": true}'

        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["authorization"] = req.get_header("Authorization")
            captured["content_type"] = req.get_header("Content-type")
            captured["timeout"] = timeout
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        handler = object.__new__(AppHandler)
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_PROVIDER": "cloudflare",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "CLOUDFLARE_ACCOUNT_ID": "acct_123",
                "CLOUDFLARE_API_TOKEN": "token_456",
            },
            clear=False,
        ):
            with patch("src.app.server.urllib.request.urlopen", side_effect=fake_urlopen):
                handler.send_login_email_cloudflare("to@example.com", "Subject", "Text", "<p>HTML</p>")

        self.assertEqual(
            captured["url"],
            "https://api.cloudflare.com/client/v4/accounts/acct_123/email/sending/send",
        )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["authorization"], "Bearer token_456")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["timeout"], 10)
        self.assertEqual(
            captured["payload"],
            {
                "to": "to@example.com",
                "from": "noreply@example.com",
                "subject": "Subject",
                "text": "Text",
                "html": "<p>HTML</p>",
            },
        )

    def test_cloudflare_email_sender_raises_on_api_failure(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            @staticmethod
            def read():
                return b'{"success": false, "errors": [{"message": "bad sender"}]}'

        handler = object.__new__(AppHandler)
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_PROVIDER": "cloudflare",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "CLOUDFLARE_ACCOUNT_ID": "acct_123",
                "CLOUDFLARE_API_TOKEN": "token_456",
            },
            clear=False,
        ):
            with patch("src.app.server.urllib.request.urlopen", return_value=FakeResponse()):
                with self.assertRaisesRegex(RuntimeError, "Cloudflare Email Sending"):
                    handler.send_login_email_cloudflare("to@example.com", "Subject", "Text", "<p>HTML</p>")

    def test_cloudflare_email_sender_redacts_http_error_detail(self):
        handler = object.__new__(AppHandler)
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_PROVIDER": "cloudflare",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "CLOUDFLARE_ACCOUNT_ID": "acct_123",
                "CLOUDFLARE_API_TOKEN": "token_456",
            },
            clear=False,
        ):
            error = urllib.error.HTTPError(
                "https://api.cloudflare.com/client/v4/accounts/acct_123/email/sending/send",
                403,
                "Forbidden token_456",
                {},
                io.BytesIO(b'{"success": false, "errors": [{"message": "bad token_456 sender"}]}'),
            )
            with patch("src.app.server.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    handler.send_login_email_cloudflare("to@example.com", "Subject", "Text", "<p>HTML</p>")

        message = str(raised.exception)
        self.assertIn("HTTP 403", message)
        self.assertIn("[redacted]", message)
        self.assertNotIn("token_456", message)

    def test_register_rate_limits_repeated_email_requests(self):
        self.start_registration(email="repeat@example.com")

        status, headers, _ = self.request(
            "POST",
            "/register",
            body=urlencode({"email": "repeat@example.com", "accept_terms": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='repeat@example.com'").fetchone()[0],
            1,
        )

    def test_failed_email_send_removes_pending_session(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
                "OWQ_SMTP_PASSWORD": "super-secret-password",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_login_email", side_effect=RuntimeError("smtp down super-secret-password")):
                status, headers, _ = self.request(
                    "POST",
                    "/register",
                    body=urlencode({"email": "fail@example.com", "accept_terms": "1"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn("smtp down", location)
        self.assertNotIn("super-secret-password", location)
        self.assertNotIn("fail@example.com", location)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='fail@example.com'").fetchone()[0],
            0,
        )
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "auth.email_send_failed")
        self.assertNotIn("super-secret-password", event["detail"])
        self.assertIn("[redacted]", event["detail"])

    def test_register_requires_terms_before_email_session(self):
        status, headers, _ = self.request(
            "POST",
            "/register",
            body=urlencode({"email": "noconsent@example.com"}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        count = self.con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_wechat_routes_are_not_exposed_by_default(self):
        status, _, payload = self.request("GET", "/auth/wechat/dev-confirm?token=legacy")

        self.assertEqual(status, 404)
        self.assertIn("404", payload)

    def test_public_request_sets_secure_session_cookie(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "1",
                "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
            },
            clear=False,
        ):
            token, _ = self.start_registration(email="secure@example.com", headers={"Host": "quant.ourworlds.app"})

            status, headers, _ = self.request("GET", f"/auth/email/confirm?token={token}", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 303)
            confirm_cookie = headers.get("Set-Cookie", "")
            self.assertIn("owq_email_confirm=", confirm_cookie)
            self.assertNotIn("owq_session=", confirm_cookie)
            self.assertIn("Secure", confirm_cookie)
            confirm_cookie = confirm_cookie.split(";", 1)[0]

            status, headers, _ = self.request(
                "POST",
                "/auth/email/confirm",
                body=urlencode({"login_name": "secure-user", "password": "Password1234", "password_confirm": "Password1234"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "quant.ourworlds.app", "Cookie": confirm_cookie},
            )
            self.assertEqual(status, 303)
            # Auto-login on set-password also issues a Secure session cookie on public requests.
            self.assertIn("owq_session=", headers.get("Set-Cookie", ""))
            self.assertIn("Secure", headers.get("Set-Cookie", ""))

            status, headers, _ = self.request(
                "POST",
                "/login",
                body=urlencode({"identifier": "secure-user", "password": "Password1234"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "quant.ourworlds.app"},
            )
            self.assertEqual(status, 303)
            cookie = headers.get("Set-Cookie", "")
            self.assertIn("owq_session=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("Max-Age=", cookie)
            self.assertIn("Secure", cookie)

    def test_public_user_must_accept_current_legal_terms_before_private_app(self):
        user_id = services.get_or_create_email_user(self.con, "consent-route@example.com")
        services.set_user_password(self.con, user_id, "consent-route", "Password1234")
        services.record_user_consent(self.con, user_id, "2026-01-01", "2026-01-01", "2026-01-01", source="old")
        cookie = f"owq_session={sign_user(user_id, session_version=services.user_session_version(services.get_user(self.con, user_id)))}"

        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, headers, _ = self.request("GET", "/app", headers={"Cookie": cookie, "Host": "quant.ourworlds.app"})
            self.assertEqual(status, 303)
            self.assertIn("/account/consent", headers.get("Location", ""))
            self.assertIn("next=/app", headers.get("Location", ""))

            status, _, page = self.request(
                "GET",
                "/account/consent?next=/app",
                headers={"Cookie": cookie, "Host": "quant.ourworlds.app"},
            )
            self.assertEqual(status, 200)
            self.assertIn("确认服务条款", page)
            self.assertIn('name="accept_terms"', page)
            self.assertIn('value="/app"', page)

            status, headers, _ = self.request(
                "POST",
                "/account/consent",
                body=self.form_body(user_id, {"accept_terms": "1", "next": "/app"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie, "Host": "quant.ourworlds.app"},
            )
            self.assertEqual(status, 303)
            self.assertIn("/app", headers.get("Location", ""))

            latest = services.latest_user_consent(self.con, user_id)
            self.assertEqual(latest["terms_version"], "2026-06-24")
            self.assertEqual(latest["source"], "legal_update")
            self.assertEqual(services.audit_events(self.con)[0]["action"], "legal.consent")

            status, _, dashboard = self.request("GET", "/app", headers={"Cookie": cookie, "Host": "quant.ourworlds.app"})
            self.assertEqual(status, 200)
            self.assertIn("模拟交易", dashboard)

    def test_public_user_can_export_data_before_legal_reconsent(self):
        user_id = services.get_or_create_email_user(self.con, "export-before-consent@example.com")
        services.set_user_password(self.con, user_id, "export-before-consent", "Password1234")
        cookie = f"owq_session={sign_user(user_id, session_version=services.user_session_version(services.get_user(self.con, user_id)))}"

        with patch.dict(os.environ, {"OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app"}, clear=False):
            status, _, payload = self.request(
                "GET",
                "/account/export/data.json",
                headers={"Cookie": cookie, "Host": "quant.ourworlds.app"},
            )

        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data["user"]["email"], "export-before-consent@example.com")

    def test_public_email_dev_auth_does_not_expose_registration_link_by_default(self):
        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "1",
                "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "",
                "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
                "OWQ_EMAIL_PROVIDER": "",
                "OWQ_EMAIL_FROM": "",
                "CLOUDFLARE_ACCOUNT_ID": "",
                "CLOUDFLARE_API_TOKEN": "",
                "OWQ_SMTP_HOST": "",
            },
            clear=False,
        ):
            status, _, register = self.request("GET", "/register", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 200)
            self.assertIn("邮箱注册暂未开放", register)
            self.assertIn("真实发信服务", register)
            self.assertIn("不会用注册申请创建登录态", register)
            self.assertIn("进入学习工作台", register)
            self.assertIn("第一屏先懂一句话,再点蓝色推荐按钮", register)
            self.assertNotIn("进入模拟盘", register)
            self.assertIn('href="/support"', register)
            self.assertNotIn('name="email"', register)
            self.assertNotIn("发送验证邮件", register)
            self.assertNotIn("当前启用本地邮箱测试注册", register)

            status, _, forgot = self.request("GET", "/forgot-password", headers={"Host": "quant.ourworlds.app"})
            self.assertEqual(status, 200)
            self.assertIn("暂不能通过页面自助重置密码", forgot)
            self.assertIn('href="/support"', forgot)
            self.assertNotIn("/auth/email/confirm", forgot)

            status, headers, payload = self.request(
                "POST",
                "/register",
                body=urlencode({"email": "public-beta@example.com", "accept_terms": "1"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "quant.ourworlds.app"},
            )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertNotIn("public-beta", headers.get("Location", ""))
        self.assertNotIn("/auth/email/confirm?token=", payload)
        pending = self.con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0]
        self.assertEqual(pending, 0)

    def test_public_registration_without_auth_mode_is_closed(self):
        with patch.dict(
            os.environ,
            {"OWQ_EMAIL_DEV_AUTH": "0", "OWQ_PUBLIC_BASE_URL": "", "OWQ_EMAIL_PROVIDER": "", "OWQ_EMAIL_FROM": "", "CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": "", "OWQ_SMTP_HOST": ""},
            clear=False,
        ):
            status, _, register = self.request("GET", "/register", headers={"Host": "quant.ourworlds.app"})

        self.assertEqual(status, 200)
        self.assertIn("邮箱注册暂未开放", register)
        self.assertIn("不会发送确认邮件", register)
        self.assertNotIn("/auth/email/confirm", register)

    def test_public_pages_offer_support_cta_when_registration_is_closed(self):
        user_id = services.get_or_create_user(self.con, "dev-closed-cta", "ClosedCtaRoute")
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        env = {
            "OWQ_EMAIL_DEV_AUTH": "1",
            "OWQ_EMAIL_DEV_AUTH_SHOW_LINKS": "",
            "OWQ_PUBLIC_BASE_URL": "https://quant.ourworlds.app",
            "OWQ_EMAIL_PROVIDER": "",
            "OWQ_EMAIL_FROM": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_API_TOKEN": "",
            "OWQ_SMTP_HOST": "",
        }

        with patch.dict(os.environ, env, clear=False):
            for path in ["/data-status", "/showcase/public", f"/u/{user_id}"]:
                with self.subTest(path=path):
                    status, _, payload = self.request("GET", path, headers={"Host": "quant.ourworlds.app"})

                    self.assertEqual(status, 200)
                    self.assertIn('href="/support"', payload)
                    self.assertIn("申请加入", payload)
                    self.assertNotIn('href="/register">邮箱注册', payload)
                    self.assertNotIn("首次完成邮箱验证会自动加入公开赛", payload)

    def test_login_explains_password_path_for_legacy_test_accounts(self):
        status, _, login = self.request("GET", "/login")

        self.assertEqual(status, 200)
        self.assertIn("登录后先进入学习工作台", login)
        self.assertIn("新用户不用先找模拟盘菜单", login)
        self.assertIn("懂一句话、点一个目标、完成一次模拟练习和三问复盘", login)
        self.assertIn("已有模拟记录的用户会自动回到高级模拟盘", login)
        self.assertIn("登录账号", login)
        self.assertIn("手机上也只填两项", login)
        self.assertIn("点第一关", login)
        self.assertIn("无 DeepSeek key 也能开始", login)
        self.assertIn("完成 6/6", login)
        self.assertIn("先看 3 分钟示例", login)
        self.assertIn("还没有账号?邮箱注册", login)
        self.assertLess(login.index("登录后先进入学习工作台"), login.index("账号密码登录"))
        self.assertIn("账号密码登录", login)
        self.assertIn("新用户登录后会先进入学习工作台", login)
        self.assertIn("登录学习工作台", login)
        self.assertNotIn("登录模拟盘", login)
        self.assertIn("早期测试账号", login)
        self.assertIn('href="/support"', login)

    def test_account_and_profile_render_safe_avatar(self):
        user_id = services.get_or_create_user(
            self.con,
            "dev-avatar-openid",
            "AvatarRoute",
            "https://img.example/avatar.jpg",
        )
        services.join_active_contest(self.con, user_id)
        services.record_equity_snapshot(self.con, user_id, source="test")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, account = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn('class="avatar"', account)
        self.assertIn("https://img.example/avatar.jpg", account)
        self.assertIn("身份标识", account)

        status, _, profile = self.request("GET", f"/u/{user_id}")
        self.assertEqual(status, 200)
        self.assertIn('class="avatar"', profile)
        self.assertIn("https://img.example/avatar.jpg", profile)

        unsafe_id = services.get_or_create_user(self.con, "dev-unsafe-avatar", "UnsafeAvatar", "javascript:alert(1)")
        services.join_active_contest(self.con, unsafe_id)
        services.record_equity_snapshot(self.con, unsafe_id, source="test")
        status, _, unsafe_profile = self.request("GET", f"/u/{unsafe_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("javascript:alert", unsafe_profile)

    def test_logged_in_user_can_update_account_profile(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "OldProfile")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, account = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("资料设置", account)
        self.assertIn('name="csrf"', account)

        body = self.form_body(user_id, {"nickname": "NewProfile", "avatar_url": "https://img.example/new.jpg"})
        status, headers, _ = self.request(
            "POST",
            "/account/profile",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))

        user = services.get_user(self.con, user_id)
        self.assertEqual(user["nickname"], "NewProfile")
        self.assertEqual(user["avatar_url"], "https://img.example/new.jpg")

        status, _, profile = self.request("GET", f"/u/{user_id}")
        self.assertEqual(status, 200)
        self.assertIn("NewProfile", profile)
        self.assertIn("https://img.example/new.jpg", profile)

        bad_body = self.form_body(user_id, {"nickname": "NewProfile", "avatar_url": "javascript:alert(1)"})
        status, headers, _ = self.request(
            "POST",
            "/account/profile",
            body=bad_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))

    def test_logged_in_user_can_update_password_and_relogin(self):
        user_id = services.get_or_create_email_user(self.con, "password-route@example.com")
        services.set_user_password(self.con, user_id, "password-route", "Password1234")
        cookie = f"owq_session={sign_user(user_id, session_version=services.user_session_version(services.get_user(self.con, user_id)))}"

        status, _, account = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("登录密码", account)
        self.assertIn('action="/account/password"', account)

        bad_body = self.form_body(
            user_id,
            {
                "login_name": "password-route",
                "current_password": "WrongPassword1234",
                "password": "NewPassword1234",
                "password_confirm": "NewPassword1234",
            },
        )
        status, headers, _ = self.request(
            "POST",
            "/account/password",
            body=bad_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertIsNone(services.authenticate_user(self.con, "password-route", "NewPassword1234"))

        body = self.form_body(
            user_id,
            {
                "login_name": "password-route",
                "current_password": "Password1234",
                "password": "NewPassword1234",
                "password_confirm": "NewPassword1234",
            },
        )
        status, headers, _ = self.request(
            "POST",
            "/account/password",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))
        self.assertIsNone(services.authenticate_user(self.con, "password-route", "Password1234"))
        self.assertEqual(services.authenticate_user(self.con, "password-route", "NewPassword1234"), user_id)
        self.assertEqual(services.audit_events(self.con)[0]["action"], "account.password_update")

    def test_logged_in_post_requires_csrf_token(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "CsrfRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        missing_body = urlencode({"code": "000001.SZ", "side": "buy", "qty": "100"})
        status, headers, _ = self.request(
            "POST",
            "/orders",
            body=missing_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(services.recent_orders(self.con, user_id), [])
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.csrf_failed")
        self.assertEqual(event["actor_user_id"], user_id)
        self.assertEqual(event["target_id"], "/orders")
        self.assertIn('"redirect": "/app"', event["detail"])
        self.assertNotIn("000001.SZ", event["detail"])

        valid_body = self.form_body(user_id, {"code": "000001.SZ", "side": "buy", "qty": "100"})
        status, headers, _ = self.request(
            "POST",
            "/orders",
            body=valid_body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))
        self.assertEqual(len(services.recent_orders(self.con, user_id)), 1)

    def test_logged_in_write_rate_limit_blocks_repeated_mutations(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "WriteLimitRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        RATE_LIMIT_BUCKETS[("write:orders", f"user:{user_id}")] = [1_000_000_000.0] * 30

        body = self.form_body(user_id, {"code": "000001.SZ", "side": "buy", "qty": "100"})
        status, headers, _ = self.request(
            "POST",
            "/orders",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(services.recent_orders(self.con, user_id), [])
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.rate_limited")
        self.assertEqual(event["actor_user_id"], user_id)
        self.assertEqual(event["target_id"], "orders")
        self.assertIn('"path": "/orders"', event["detail"])

    def test_mutating_routes_write_audit_events_and_admin_shows_them(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "AuditRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        body = self.form_body(user_id, {"code": "000001.SZ", "side": "buy", "qty": "100"})
        status, _, _ = self.request(
            "POST",
            "/orders",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        events = services.audit_events(self.con)
        self.assertEqual(events[0]["action"], "order.place")
        self.assertEqual(events[0]["actor_user_id"], user_id)
        self.assertIn("000001.SZ", events[0]["detail"])

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("审计日志", admin)
        self.assertIn("order.place", admin)
        self.assertIn("AuditRoute", admin)

    def test_logged_in_user_can_reset_paper_account(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "ResetRoute")
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, account_page = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("重新演练", account_page)
        self.assertIn("T+1 结算", account_page)
        self.assertIn("数据导出", account_page)
        self.assertIn("输入 RESET 确认重置模拟账户", account_page)
        self.assertIn('name="csrf"', account_page)

        status, headers, _ = self.request(
            "POST",
            "/account/settle",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))
        snapshot = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(snapshot["holdings"][0]["available_qty"], 100)

        status, headers, _ = self.request(
            "POST",
            "/account/reset",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))
        self.assertIn("err=", headers.get("Location", ""))
        self.assertNotEqual(services.recent_orders(self.con, user_id), [])

        status, headers, _ = self.request(
            "POST",
            "/account/reset",
            body=self.form_body(user_id, {"confirm": "RESET"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/account", headers.get("Location", ""))

        snapshot = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(services.recent_orders(self.con, user_id), [])
        self.assertEqual(snapshot["holdings"], [])
        self.assertAlmostEqual(snapshot["cash"], services.INITIAL_CASH)

    def test_logged_in_user_can_export_account_csv_files(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "ExportRoute")
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        services.record_user_consent(self.con, user_id, "2026-06-24", "2026-06-24", "2026-06-24", source="test")
        post_id = services.create_post(self.con, user_id, "导出复盘帖", "这条帖子应进入 JSON 导出", "export")
        services.add_comment(self.con, user_id, post_id, "这条评论应进入 JSON 导出")
        services.create_support_request(
            self.con,
            "export-route@example.com",
            "导出前支持请求",
            "这条支持请求应进入 JSON 导出。",
            category="account",
            requester_user_id=user_id,
        )
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, headers, orders = self.request("GET", "/account/export/orders.csv", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("orders.csv", headers.get("Content-Disposition", ""))
        self.assertIn("created_at,code,side,qty,price,fee,amount", orders.lstrip("\ufeff"))
        self.assertIn("000001.SZ,buy,100", orders)

        status, headers, holdings = self.request("GET", "/account/export/holdings.csv", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("holdings.csv", headers.get("Content-Disposition", ""))
        self.assertIn("available_qty", holdings)
        self.assertIn("000001.SZ", holdings)

        status, headers, equity = self.request("GET", "/account/export/equity.csv", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("equity.csv", headers.get("Content-Disposition", ""))
        self.assertIn("return_pct", equity)
        self.assertIn("order:buy", equity)

        status, headers, exported = self.request("GET", "/account/export/data.json", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIn(f"ourworld-quant-user-{user_id}.json", headers.get("Content-Disposition", ""))
        data = json.loads(exported)
        self.assertEqual(data["user"]["id"], user_id)
        self.assertEqual(data["user"]["nickname"], "ExportRoute")
        self.assertEqual(data["orders"][0]["code"], "000001.SZ")
        self.assertEqual(data["consents"][0]["source"], "test")
        self.assertEqual(data["forum_posts"][0]["title"], "导出复盘帖")
        self.assertEqual(data["forum_comments"][0]["body"], "这条评论应进入 JSON 导出")
        self.assertEqual(data["support_requests"][0]["subject"], "导出前支持请求")
        self.assertIn("portfolio", data)
        self.assertTrue(any(event["action"] == "account.export" and "data.json" in event["detail"] for event in data["audit_events"]))

        export_events = [event for event in services.audit_events(self.con, limit=20) if event["action"] == "account.export"]
        exported_files = {json.loads(event["detail"])["file"] for event in export_events}
        self.assertEqual(exported_files, {"orders.csv", "holdings.csv", "equity.csv", "data.json"})
        for event in export_events:
            self.assertEqual(event["actor_user_id"], user_id)
            self.assertEqual(event["target_type"], "user_data_export")

    def test_logged_in_user_can_close_account_after_confirmation(self):
        token = services.create_email_login_session(
            self.con,
            "close-route@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            enforce_rate_limit=False,
        )
        user_id = services.confirm_email_login_session(self.con, token)
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        services.create_post(self.con, user_id, "注销前帖子", "关闭账户时会删除", "privacy")
        services.create_support_request(
            self.con,
            "close-route@example.com",
            "注销前支持请求",
            "关闭账户时这条支持请求也应该删除。",
            category="account",
            requester_user_id=user_id,
        )
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, account = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("关闭账户", account)
        self.assertIn('action="/account/delete"', account)

        status, headers, _ = self.request(
            "POST",
            "/account/delete",
            body=self.form_body(user_id, {"confirm": "WRONG"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertIsNotNone(services.get_user(self.con, user_id))

        status, headers, _ = self.request(
            "POST",
            "/account/delete",
            body=self.form_body(user_id, {"confirm": "DELETE"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/register", headers.get("Location", ""))
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertIsNone(services.get_user(self.con, user_id))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM forum_posts WHERE user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='close-route@example.com'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM support_requests WHERE email='close-route@example.com'").fetchone()[0], 0)
        delete_event = services.audit_events(self.con)[0]
        self.assertEqual(delete_event["action"], "account.delete")
        self.assertEqual(json.loads(delete_event["detail"])["support_requests"], "1")

        status, headers, _ = self.request("GET", "/account", headers={"Cookie": cookie})
        self.assertEqual(status, 303)
        self.assertIn("/login", headers.get("Location", ""))

    def test_logged_in_user_can_create_and_execute_practice_signal(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "PracticeRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, app = self.request("GET", "/app", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("策略演练计划", app)
        self.assertIn("策略篮子导入", app)
        self.assertIn("从基础行情生成篮子", app)
        self.assertIn("执行全部待执行计划", app)
        self.assertIn('name="csrf"', app)

        body = self.form_body(
            user_id,
            {
                "strategy_name": "反转演练",
                "code": "000001.SZ",
                "side": "buy",
                "qty": "100",
                "rationale": "回撤后观察反弹",
            }
        )
        status, headers, _ = self.request(
            "POST",
            "/practice-signals",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))

        signal = services.practice_signals(self.con, user_id)[0]
        status, headers, _ = self.request(
            "POST",
            f"/practice-signals/{signal['id']}/execute",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))

        signal = services.practice_signals(self.con, user_id)[0]
        snapshot = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(signal["status"], "executed")
        self.assertEqual(len(services.recent_orders(self.con, user_id)), 1)
        self.assertEqual(snapshot["holdings"][0]["qty"], 100)

    def test_logged_in_user_can_batch_import_practice_signals(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "BasketRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        body = self.form_body(
            user_id,
            {
                "strategy_name": "研究篮子",
                "batch_text": "code,side,qty,rationale\n000001.SZ,buy,100,反转候选\n510300.SH,买入,1000,ETF 配置\n",
            },
        )
        status, headers, _ = self.request(
            "POST",
            "/practice-signals/batch",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))
        signals = services.practice_signals(self.con, user_id)
        self.assertEqual(len(signals), 2)
        self.assertEqual({s["code"] for s in signals}, {"000001.SZ", "510300.SH"})

    def test_practice_signal_batch_failure_does_not_leak_submitted_text(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "BasketFailureRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        secret_text = "SECRET-BASKET-CONTENT"

        status, headers, _ = self.request(
            "POST",
            "/practice-signals/batch",
            body=self.form_body(user_id, {"strategy_name": "失败篮子", "batch_text": secret_text}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn(secret_text, location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "practice_signal.batch_failed")
        self.assertNotIn(secret_text, event["detail"])

    def test_logged_in_user_can_generate_practice_signals_from_market_data(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "MarketBasketRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        body = self.form_body(
            user_id,
            {
                "strategy_name": "行情反转观察",
                "mode": "reversal",
                "qty": "100",
                "limit": "2",
            },
        )
        status, headers, _ = self.request(
            "POST",
            "/practice-signals/from-market",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))
        signals = services.practice_signals(self.con, user_id)
        self.assertEqual(len(signals), 2)
        self.assertEqual({s["code"] for s in signals}, {"000001.SZ", "510300.SH"})
        self.assertTrue(all("反转候选" in s["rationale"] for s in signals))

    def test_logged_in_user_can_use_portfolio_lab_predictions(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "PortfolioLabRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        pred_path = Path(self.tmpdir.name) / "predictions.csv"
        pred_path.write_text(
            "code,prediction,last_close\n"
            "600519.SH,0.031,1222.45\n"
            "000001.SZ,0.012,10.71\n",
            encoding="utf-8",
        )

        with patch.dict("os.environ", {"OWQ_PREDICTIONS_CSV": str(pred_path)}):
            status, _, lab = self.request("GET", "/portfolio-lab", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertIn("组合设计", lab)
            self.assertIn("模型预测篮子", lab)
            self.assertIn("600519.SH", lab)

            body = self.form_body(user_id, {"strategy_name": "预测组合", "qty": "100", "limit": "1"})
            status, headers, _ = self.request(
                "POST",
                "/practice-signals/from-predictions",
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/portfolio-lab", headers.get("Location", ""))
        signals = services.practice_signals(self.con, user_id)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["code"], "600519.SH")
        self.assertIn("预测候选", signals[0]["rationale"])

    def test_practice_signal_prediction_failure_does_not_leak_prediction_path(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "PredictionFailureRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        missing_path = Path(self.tmpdir.name) / "secret-predictions.csv"

        with patch.dict("os.environ", {"OWQ_PREDICTIONS_CSV": str(missing_path)}):
            status, headers, _ = self.request(
                "POST",
                "/practice-signals/from-predictions",
                body=self.form_body(user_id, {"strategy_name": "预测失败", "qty": "100", "limit": "1"}),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn(str(missing_path), location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "practice_signal.prediction_failed")
        self.assertNotIn(str(missing_path), event["detail"])

    def test_logged_in_user_can_execute_pending_practice_signals(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "ExecutePendingRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        services.create_practice_signal(self.con, user_id, "批量执行", "000001.SZ", "buy", 100, "第一笔")
        services.create_practice_signal(self.con, user_id, "批量执行", "510300.SH", "buy", 1000, "第二笔")

        status, headers, _ = self.request(
            "POST",
            "/practice-signals/execute-pending",
            body=self.form_body(user_id, {"limit": "20"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/app", headers.get("Location", ""))
        self.assertEqual(len(services.recent_orders(self.con, user_id)), 2)
        self.assertTrue(all(s["status"] == "executed" for s in services.practice_signals(self.con, user_id)))

    def test_showcase_links_to_prefilled_performance_forum_post(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "ShareRoute")
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, showcase = self.request("GET", "/showcase", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("生成战绩复盘帖", showcase)
        self.assertIn('name="csrf"', showcase)

        status, _, draft = self.request("GET", "/forum/new?template=performance", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("模拟盘战绩复盘", draft)
        self.assertIn("当前模拟盘战绩", draft)
        self.assertIn("个人战绩页", draft)
        self.assertIn("performance", draft)

    def test_logged_in_user_can_sync_market_from_pasted_csv(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "MarketRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, market = self.request("GET", "/market", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("粘贴 CSV", market)
        self.assertIn("CSV 内容", market)
        self.assertIn('name="csrf"', market)

        body = self.form_body(
            user_id,
            {
                "source": "csv_text",
                "csv_text": "code,name,price,prev_close,as_of\n000001.SZ,平安银行文本,12.1,11.9,2026-06-24\n",
            }
        )
        status, headers, _ = self.request(
            "POST",
            "/market/sync",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/market", headers.get("Location", ""))

        row = self.con.execute("SELECT name, price, source, as_of FROM market_prices WHERE code='000001.SZ'").fetchone()
        history = services.equity_history(self.con, user_id)
        self.assertEqual(row["name"], "平安银行文本")
        self.assertEqual(row["price"], 12.1)
        self.assertEqual(row["source"], "csv_text")
        self.assertEqual(row["as_of"], "2026-06-24")
        self.assertEqual(history[-1]["source"], "market_sync")

    def test_market_sync_failure_does_not_leak_csv_path(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "MarketFailureRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        missing_path = Path(self.tmpdir.name) / "private-market.csv"

        status, headers, _ = self.request(
            "POST",
            "/market/sync",
            body=self.form_body(user_id, {"source": "csv", "csv_path": str(missing_path), "replace_market": "1"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn(str(missing_path), location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "market.sync_failed")
        self.assertIn('"source": "csv"', event["detail"])
        self.assertNotIn(str(missing_path), event["detail"])

    def test_admin_can_seed_demo_competition(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "AdminRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("生成演示比赛数据", admin)
        self.assertIn("移出演示/开发参赛账户", admin)
        self.assertIn('name="csrf"', admin)

        status, headers, _ = self.request(
            "POST",
            "/admin/demo-seed",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/admin", headers.get("Location", ""))

        status, _, showcase = self.request("GET", "/showcase/public")
        self.assertEqual(status, 200)
        self.assertIn("低波动练习生", showcase)
        self.assertIn("反转策略样本", showcase)

    def test_admin_can_remove_demo_participants_from_public_contest(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "DemoCleanAdmin")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        services.seed_demo_competition(self.con)

        status, headers, _ = self.request(
            "POST",
            "/admin/demo-contest-clean",
            body=self.form_body(user_id),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        self.assertIn("/admin", headers.get("Location", ""))
        self.assertEqual(services.demo_contest_participant_summary(self.con)["participants"], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE wechat_openid LIKE 'demo-%'").fetchone()[0], 3)
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.demo_contest_clean")

    def test_formal_production_blocks_demo_competition_seed_by_default(self):
        user_id = services.get_or_create_email_user(self.con, "demo-block-admin@example.com")
        services.set_user_password(self.con, user_id, "demo-block-admin", "Password1234")
        services.record_user_consent(self.con, user_id, "2026-06-24", "2026-06-24", "2026-06-24", source="test")
        cookie = f"owq_session={sign_user(user_id, session_version=services.user_session_version(services.get_user(self.con, user_id)))}"

        with patch.dict(
            os.environ,
            {
                "OWQ_ENV": "production",
                "OWQ_PUBLIC_BASE_URL": "https://quant.example",
                "OWQ_ADMIN_USER_IDS": str(user_id),
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_ALLOW_DEMO_SEED": "0",
            },
            clear=False,
        ):
            status, headers, _ = self.request(
                "POST",
                "/admin/demo-seed",
                body=self.form_body(user_id),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM users WHERE wechat_openid LIKE 'demo-%'").fetchone()[0],
            0,
        )

    def test_admin_can_create_app_database_backup(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "BackupAdmin")
        cookie = f"owq_session={self.sign_cookie(user_id)}"
        backup_dir = Path(self.tmpdir.name) / "admin-backups"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("应用数据备份", admin)

        with patch.object(db, "DEFAULT_BACKUP_DIR", backup_dir):
            status, headers, _ = self.request(
                "POST",
                "/admin/backup",
                body=self.form_body(user_id),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("/admin", location)
        self.assertNotIn(str(backup_dir), location)
        backups = list(backup_dir.glob("app-*.sqlite"))
        self.assertEqual(len(backups), 1)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "admin.backup")
        self.assertEqual(event["target_id"], backups[0].name)
        self.assertIn('"file":', event["detail"])
        self.assertNotIn(str(backup_dir), event["detail"])
        backup = sqlite3.connect(backups[0])
        try:
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertGreaterEqual(backup.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            backup.close()

    def test_admin_backup_failure_redirect_is_generic_and_audited(self):
        token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, token, "BackupFailAdmin")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        with patch.object(db, "backup_database", side_effect=RuntimeError(f"backup failed in {self.tmpdir.name}")):
            status, headers, _ = self.request(
                "POST",
                "/admin/backup",
                body=self.form_body(user_id),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
            )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn(self.tmpdir.name, location)
        self.assertNotIn("RuntimeError", location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "admin.backup_failed")
        self.assertIn("RuntimeError", event["detail"])

    def test_admin_can_export_audit_log_csv(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "AuditExportAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        user_id = services.get_or_create_user(self.con, "dev-audit-normal", "NormalAuditUser")
        user_cookie = f"owq_session={self.sign_cookie(user_id)}"
        services.record_audit_event(
            self.con,
            admin_id,
            "test.audit_export_seed",
            target_type="unit",
            target_id="42",
            detail={"scope": "csv"},
            ip_address="127.0.0.1",
        )

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("/admin/audit.csv", admin)

        status, headers, csv_body = self.request("GET", "/admin/audit.csv", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("audit-events.csv", headers.get("Content-Disposition", ""))
        self.assertIn("created_at,action,actor_user_id,actor,target_type,target_id,detail,ip_address", csv_body.lstrip("\ufeff"))
        self.assertIn("test.audit_export_seed", csv_body)
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.audit_export")

        status, _, forbidden = self.request("GET", "/admin/audit.csv", headers={"Cookie": user_cookie})
        self.assertEqual(status, 403)
        self.assertIn("当前用户没有管理权限", forbidden)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "security.admin_forbidden")
        self.assertEqual(event["actor_user_id"], user_id)
        self.assertEqual(event["target_id"], "/admin/audit.csv")

    def test_admin_can_export_operational_csvs(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "OpsExportAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        reporter_token = services.create_wechat_session(self.con)
        reporter_id = services.confirm_wechat_session(self.con, reporter_token, "OpsReporter")
        reporter_cookie = f"owq_session={self.sign_cookie(reporter_id)}"
        post_id = services.create_post(self.con, admin_id, "导出举报策略帖", "这篇帖子用于后台导出测试", "ops")
        report_id = services.create_content_report(self.con, reporter_id, "post", post_id, "运营导出测试原因")

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("/admin/accounts.csv", admin)
        self.assertIn("/admin/reports.csv", admin)

        status, headers, accounts_csv = self.request("GET", "/admin/accounts.csv", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("admin-accounts.csv", headers.get("Content-Disposition", ""))
        account_rows = list(csv.DictReader(io.StringIO(accounts_csv.lstrip("\ufeff"))))
        self.assertTrue(any(row["user_id"] == str(admin_id) and row["nickname"] == "OpsExportAdmin" for row in account_rows))
        self.assertTrue(any(row["user_id"] == str(reporter_id) and row["status"] == "active" for row in account_rows))
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.accounts_export")

        status, headers, reports_csv = self.request("GET", "/admin/reports.csv", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers.get("Content-Type", ""))
        self.assertIn("content-reports.csv", headers.get("Content-Disposition", ""))
        report_rows = list(csv.DictReader(io.StringIO(reports_csv.lstrip("\ufeff"))))
        exported_report = next(row for row in report_rows if row["id"] == str(report_id))
        self.assertEqual(exported_report["status"], "pending")
        self.assertEqual(exported_report["reporter"], "OpsReporter")
        self.assertEqual(exported_report["target"], "导出举报策略帖")
        self.assertEqual(exported_report["reason"], "运营导出测试原因")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.reports_export")

        for path in ["/admin/accounts.csv", "/admin/reports.csv"]:
            status, _, forbidden = self.request("GET", path, headers={"Cookie": reporter_cookie})
            self.assertEqual(status, 403)
            self.assertIn("当前用户没有管理权限", forbidden)
            event = services.audit_events(self.con)[0]
            self.assertEqual(event["action"], "security.admin_forbidden")
            self.assertEqual(event["actor_user_id"], reporter_id)
            self.assertEqual(event["target_id"], path)

    def test_admin_dashboard_shows_security_event_summary(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "SecurityAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        services.record_audit_event(self.con, None, "security.login_failed", target_type="auth", target_id="password", ip_address="203.0.113.1")
        services.record_audit_event(self.con, admin_id, "server.error", target_type="http", target_id="/app", ip_address="127.0.0.1")
        services.record_audit_event(self.con, admin_id, "account.profile_update", target_type="user")

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})

        self.assertEqual(status, 200)
        self.assertIn("安全和异常事件", admin)
        self.assertIn("近 24 小时按类型", admin)
        self.assertIn("security.login_failed", admin)
        self.assertIn("server.error", admin)
        self.assertIn("203.0.113.1", admin)
        self.assertNotIn("account.profile_update</td><td>1</td>", admin)

    def test_admin_can_prune_expired_audit_log_events(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "AuditPruneAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        old_id = services.record_audit_event(self.con, admin_id, "old.audit", target_type="unit")
        services.record_audit_event(self.con, admin_id, "recent.audit", target_type="unit")
        self.con.execute("UPDATE audit_events SET created_at=datetime('now', '-45 days') WHERE id=?", (old_id,))
        self.con.commit()

        with patch.dict(os.environ, {"OWQ_AUDIT_RETENTION_DAYS": "30"}, clear=False):
            status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
            self.assertEqual(status, 200)
            self.assertIn("清理超期审计日志", admin)
            self.assertIn("1 条超过保留期", admin)

            status, headers, _ = self.request(
                "POST",
                "/admin/audit-prune",
                body=self.form_body(admin_id),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/admin", headers.get("Location", ""))
        events = services.audit_events(self.con)
        self.assertEqual(events[0]["action"], "admin.audit_prune")
        self.assertIn('"deleted": "1"', events[0]["detail"])
        self.assertNotIn("old.audit", [event["action"] for event in events])
        self.assertIn("recent.audit", [event["action"] for event in events])

    def test_admin_can_prune_email_login_sessions(self):
        admin_token = services.create_wechat_session(self.con)
        admin_id = services.confirm_wechat_session(self.con, admin_token, "EmailLoginPruneAdmin")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        token = services.create_email_login_session(
            self.con,
            "old-email-login@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            enforce_rate_limit=False,
        )
        services.confirm_email_login_session(self.con, token)
        self.con.execute(
            "UPDATE email_login_sessions SET created_at=datetime('now', '-45 days') WHERE email=?",
            ("old-email-login@example.com",),
        )
        self.con.commit()

        with patch.dict(os.environ, {"OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS": "30"}, clear=False):
            status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
            self.assertEqual(status, 200)
            self.assertIn("邮箱登录临时会话", admin)
            self.assertIn("1 条可清理", admin)

            status, headers, _ = self.request(
                "POST",
                "/admin/email-login-prune",
                body=self.form_body(admin_id),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
            )

        self.assertEqual(status, 303)
        self.assertIn("/admin", headers.get("Location", ""))
        events = services.audit_events(self.con)
        self.assertEqual(events[0]["action"], "admin.email_login_prune")
        self.assertIn('"deleted": "1"', events[0]["detail"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM email_login_sessions").fetchone()[0], 0)

    def test_admin_can_send_email_diagnostic(self):
        user_id = services.get_or_create_email_user(self.con, "admin@example.com")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("发布闸门", admin)
        self.assertIn("正式发布前仍有待处理项", admin)
        self.assertIn("注册发信", admin)
        self.assertIn("当前发信状态", admin)
        self.assertIn("邮件发信诊断", admin)
        self.assertIn('action="/admin/email-test"', admin)

        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_transactional_email", return_value="smtp") as sender:
                status, headers, _ = self.request(
                    "POST",
                    "/admin/email-test",
                    body=self.form_body(user_id, {"email": "ops@example.com"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
                )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("/admin", location)
        self.assertNotIn("ops@example.com", location)
        self.assertNotIn("smtp", location)
        sender.assert_called_once()
        event = services.audit_events(self.con)[0]
        recipient_hash = services.email_token_hash("ops@example.com")[:16]
        self.assertEqual(event["action"], "admin.email_test")
        self.assertEqual(event["target_id"], recipient_hash)
        self.assertIn(f'"recipient_hash": "{recipient_hash}"', event["detail"])
        self.assertNotIn("ops@example.com", event["detail"])

    def test_admin_email_diagnostic_reports_missing_sender(self):
        user_id = services.get_or_create_email_user(self.con, "admin2@example.com")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, headers, _ = self.request(
            "POST",
            "/admin/email-test",
            body=self.form_body(user_id, {"email": "ops@example.com"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
        )

        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("err=", location)
        self.assertNotIn("ops@example.com", location)
        self.assertNotIn("邮箱发信服务未配置", location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "admin.email_test_failed")
        self.assertEqual(event["target_id"], services.email_token_hash("ops@example.com")[:16])
        self.assertNotIn("ops@example.com", event["detail"])

    def test_admin_email_diagnostic_redacts_provider_failure(self):
        user_id = services.get_or_create_email_user(self.con, "admin3@example.com")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        with patch.dict(
            os.environ,
            {
                "OWQ_EMAIL_DEV_AUTH": "0",
                "OWQ_EMAIL_PROVIDER": "smtp",
                "OWQ_EMAIL_FROM": "noreply@example.com",
                "OWQ_SMTP_HOST": "smtp.example.com",
                "OWQ_SMTP_PASSWORD": "super-secret-password",
            },
            clear=False,
        ):
            with patch.object(AppHandler, "send_transactional_email", side_effect=RuntimeError("smtp failed super-secret-password")):
                status, headers, _ = self.request(
                    "POST",
                    "/admin/email-test",
                    body=self.form_body(user_id, {"email": "ops@example.com"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
                )

        location = headers.get("Location", "")
        self.assertEqual(status, 303)
        self.assertIn("err=", location)
        self.assertNotIn("RuntimeError", location)
        self.assertNotIn("ops@example.com", location)
        self.assertNotIn("super-secret-password", location)
        event = services.audit_events(self.con)[0]
        self.assertEqual(event["action"], "admin.email_test_failed")
        self.assertEqual(event["target_id"], services.email_token_hash("ops@example.com")[:16])
        self.assertNotIn("ops@example.com", event["detail"])
        self.assertNotIn("super-secret-password", event["detail"])
        self.assertIn("[redacted]", event["detail"])

    def test_admin_can_suspend_restore_user_and_block_mutations_only(self):
        admin_id = services.get_or_create_email_user(self.con, "status-admin@example.com")
        target_id = services.get_or_create_email_user(self.con, "status-user@example.com")
        admin_cookie = f"owq_session={self.sign_cookie(admin_id)}"
        target_cookie = f"owq_session={self.sign_cookie(target_id)}"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": admin_cookie})
        self.assertEqual(status, 200)
        self.assertIn("用户账户概览", admin)
        self.assertIn('action="/admin/users/', admin)
        self.assertIn("暂停", admin)
        self.assertIn('<table class="mobile-card-table"><thead><tr><th>ID</th><th>用户</th><th>排名</th>', admin)
        self.assertIn('data-label="操作"', admin)

        status, headers, _ = self.request(
            "POST",
            f"/admin/users/{admin_id}/status",
            body=self.form_body(admin_id, {"status": "suspended", "reason": "self"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(services.get_user(self.con, admin_id)["status"], "active")

        status, headers, _ = self.request(
            "POST",
            f"/admin/users/{target_id}/status",
            body=self.form_body(admin_id, {"status": "suspended", "reason": "异常刷屏"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("/admin", headers.get("Location", ""))
        self.assertEqual(services.get_user(self.con, target_id)["status"], "suspended")
        self.assertEqual(services.audit_events(self.con)[0]["action"], "admin.user_status")

        status, headers, _ = self.request(
            "POST",
            "/orders",
            body=self.form_body(target_id, {"code": "000001.SZ", "side": "buy", "qty": "100"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": target_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(services.recent_orders(self.con, target_id), [])

        status, headers, _ = self.request(
            "POST",
            "/forum/new",
            body=self.form_body(target_id, {"title": "暂停发帖", "body": "应该被拦截", "tag": "forum"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": target_cookie},
        )
        self.assertEqual(status, 303)
        self.assertIn("err=", headers.get("Location", ""))
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM forum_posts WHERE user_id=?", (target_id,)).fetchone()[0],
            0,
        )

        status, headers, exported = self.request("GET", "/account/export/data.json", headers={"Cookie": target_cookie})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(json.loads(exported)["user"]["id"], target_id)

        status, headers, _ = self.request(
            "POST",
            f"/admin/users/{target_id}/status",
            body=self.form_body(admin_id, {"status": "active"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin_cookie},
        )
        self.assertEqual(status, 303)
        self.assertEqual(services.get_user(self.con, target_id)["status"], "active")

        status, _, _ = self.request(
            "POST",
            "/orders",
            body=self.form_body(target_id, {"code": "000001.SZ", "side": "buy", "qty": "100"}),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": target_cookie},
        )
        self.assertEqual(status, 303)
        self.assertEqual(len(services.recent_orders(self.con, target_id)), 1)

    def test_non_admin_cannot_access_management_actions(self):
        admin_token = services.create_wechat_session(self.con)
        services.confirm_wechat_session(self.con, admin_token, "FirstAdmin")
        user_token = services.create_wechat_session(self.con)
        user_id = services.confirm_wechat_session(self.con, user_token, "NormalRoute")
        cookie = f"owq_session={self.sign_cookie(user_id)}"

        status, _, admin = self.request("GET", "/admin", headers={"Cookie": cookie})
        self.assertEqual(status, 403)
        self.assertIn("当前用户没有管理权限", admin)
        self.assertNotIn('href="/admin">管理</a>', admin)

        status, _, _ = self.request("POST", "/admin/demo-seed", headers={"Cookie": cookie})
        self.assertEqual(status, 403)

        showcase_users = [row["row"]["nickname"] for row in services.leaderboard(self.con)]
        self.assertNotIn("低波动练习生", showcase_users)

    @staticmethod
    def sign_cookie(user_id: int) -> str:
        from src.app.server import sign_user

        return sign_user(user_id)

    @staticmethod
    def csrf(user_id: int) -> str:
        return csrf_token(user_id)

    def form_body(self, user_id: int, fields: dict[str, str] | None = None) -> str:
        body = {"csrf": self.csrf(user_id)}
        body.update(fields or {})
        return urlencode(body)


if __name__ == "__main__":
    unittest.main()

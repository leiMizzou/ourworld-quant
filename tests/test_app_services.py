from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.app import db, services


class AppServicesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.con = db.bootstrap(Path(self.tmpdir.name) / "app.sqlite")

    def tearDown(self):
        self.con.close()
        self.tmpdir.cleanup()

    def register_user(self):
        token = services.create_wechat_session(self.con)
        return services.confirm_wechat_session(self.con, token, "测试用户")

    def test_wechat_registration_creates_account_and_contest_entry(self):
        user_id = self.register_user()
        user = services.get_user(self.con, user_id)
        account = services.account_for_user(self.con, user_id)
        board = services.leaderboard(self.con)
        history = services.equity_history(self.con, user_id)

        self.assertEqual(user["nickname"], "测试用户")
        self.assertAlmostEqual(account["cash"], services.INITIAL_CASH)
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["row"]["user_id"], user_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "register")

    def test_email_magic_link_creates_account_and_contest_entry(self):
        token = services.create_email_login_session(
            self.con,
            "USER@Example.COM",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
        )

        user_id = services.confirm_email_login_session(self.con, token)

        user = services.get_user(self.con, user_id)
        account = services.account_for_user(self.con, user_id)
        board = services.leaderboard(self.con)
        history = services.equity_history(self.con, user_id)
        self.assertEqual(user["email"], "user@example.com")
        self.assertTrue(user["wechat_openid"].startswith("email-"))
        self.assertIsNotNone(account)
        self.assertEqual(len(board), 1)
        self.assertEqual(history[0]["source"], "email")

        with self.assertRaisesRegex(ValueError, "不存在或已使用"):
            services.confirm_email_login_session(self.con, token)

    def test_bump_user_session_version_increments_login_generation(self):
        user_id = self.register_user()

        self.assertEqual(services.user_session_version(services.get_user(self.con, user_id)), 1)
        self.assertEqual(services.bump_user_session_version(self.con, user_id), 2)
        self.assertEqual(services.user_session_version(services.get_user(self.con, user_id)), 2)

    def test_email_user_can_set_and_verify_password_login(self):
        user_id = services.get_or_create_email_user(self.con, "Login@Example.COM")

        services.set_user_password(self.con, user_id, "login-user", "Password1234")

        user = services.get_user(self.con, user_id)
        self.assertEqual(user["login_name"], "login-user")
        self.assertTrue(user["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertEqual(services.authenticate_user(self.con, "login-user", "Password1234"), user_id)
        self.assertEqual(services.authenticate_user(self.con, "login@example.com", "Password1234"), user_id)
        self.assertIsNone(services.authenticate_user(self.con, "login-user", "wrong-password"))
        self.assertEqual(services.user_session_version(user), 2)

        with self.assertRaisesRegex(ValueError, "用户名已被占用"):
            other_id = services.get_or_create_email_user(self.con, "other@example.com")
            services.set_user_password(self.con, other_id, "login-user", "Password1234")

    def test_password_reset_can_preserve_existing_public_profile(self):
        user_id = services.get_or_create_email_user(self.con, "ResetProfile@Example.COM")
        services.set_user_password(self.con, user_id, "reset-user", "Password1234")
        services.update_user_profile(self.con, user_id, "公开昵称", "https://img.example/avatar.png")
        before = services.get_user(self.con, user_id)

        services.set_user_password(self.con, user_id, "reset-user", "NewPassword1234", update_nickname=False)

        user = services.get_user(self.con, user_id)
        self.assertEqual(user["login_name"], "reset-user")
        self.assertEqual(user["nickname"], "公开昵称")
        self.assertEqual(user["avatar_url"], "https://img.example/avatar.png")
        self.assertEqual(services.user_session_version(user), services.user_session_version(before) + 1)
        self.assertIsNone(services.authenticate_user(self.con, "reset-user", "Password1234"))
        self.assertEqual(services.authenticate_user(self.con, "reset-user", "NewPassword1234"), user_id)

    def test_email_login_sessions_are_rate_limited_per_email(self):
        services.create_email_login_session(
            self.con,
            "limit@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
        )

        with self.assertRaisesRegex(ValueError, "请求过于频繁"):
            services.create_email_login_session(
                self.con,
                "limit@example.com",
                "2026-06-24",
                "2026-06-24",
                "2026-06-24",
            )

    def test_email_login_cleanup_expires_old_pending_sessions(self):
        token = services.create_email_login_session(
            self.con,
            "expired@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            ttl_minutes=-1,
            enforce_rate_limit=False,
        )

        result = services.cleanup_email_login_sessions(self.con)

        self.assertEqual(result["expired"], 1)
        self.assertEqual(services.email_login_session_status(self.con, token)["status"], "expired")

    def test_email_login_cleanup_prunes_old_completed_sessions(self):
        confirmed = services.create_email_login_session(
            self.con,
            "old-confirmed@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            enforce_rate_limit=False,
        )
        services.confirm_email_login_session(self.con, confirmed)
        recent = services.create_email_login_session(
            self.con,
            "recent@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            enforce_rate_limit=False,
        )
        expired = services.create_email_login_session(
            self.con,
            "old-expired@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            ttl_minutes=-1,
            enforce_rate_limit=False,
        )
        services.email_login_session_status(self.con, expired)
        self.con.execute(
            "UPDATE email_login_sessions SET created_at=datetime('now', '-45 days') WHERE email IN (?, ?)",
            ("old-confirmed@example.com", "old-expired@example.com"),
        )
        self.con.commit()

        summary = services.email_login_session_retention_summary(self.con, days=30)
        self.assertEqual(summary["deletable"], 2)
        result = services.prune_email_login_sessions(self.con, days=30)

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["remaining"], 1)
        remaining = self.con.execute("SELECT email FROM email_login_sessions").fetchall()
        self.assertEqual([row["email"] for row in remaining], ["recent@example.com"])

    def test_email_login_retention_rejects_unsafe_window(self):
        days, ok, detail = services.email_login_session_retention_config(0)

        self.assertFalse(ok)
        self.assertEqual(days, services.DEFAULT_EMAIL_LOGIN_SESSION_RETENTION_DAYS)
        self.assertIn("邮箱登录临时会话保留期", detail)
        with self.assertRaisesRegex(ValueError, "邮箱登录临时会话保留期"):
            services.prune_email_login_sessions(self.con, days=0)

    def test_audit_events_record_actor_action_and_safe_detail(self):
        user_id = self.register_user()
        event_id = services.record_audit_event(
            self.con,
            user_id,
            "test.action",
            target_type="unit",
            target_id="abc",
            detail={"long": "x" * 500, "count": 2},
            ip_address="127.0.0.1",
        )

        events = services.audit_events(self.con)
        self.assertEqual(events[0]["id"], event_id)
        self.assertEqual(events[0]["actor_user_id"], user_id)
        self.assertEqual(events[0]["action"], "test.action")
        self.assertEqual(events[0]["target_type"], "unit")
        self.assertEqual(events[0]["target_id"], "abc")
        self.assertIn('"count": "2"', events[0]["detail"])
        self.assertLessEqual(len(events[0]["detail"]), 2000)
        self.assertEqual(events[0]["ip_address"], "127.0.0.1")

    def test_security_audit_summary_counts_recent_operational_risks(self):
        user_id = self.register_user()
        login_failed = services.record_audit_event(self.con, None, "security.login_failed", target_type="auth")
        services.record_audit_event(self.con, user_id, "server.error", target_type="http")
        services.record_audit_event(self.con, user_id, "auth.email_send_failed", target_type="email")
        old_security = services.record_audit_event(self.con, None, "security.rate_limited", target_type="rate_limit")
        services.record_audit_event(self.con, user_id, "account.profile_update", target_type="user")
        self.con.execute("UPDATE audit_events SET created_at=datetime('now', '-2 days') WHERE id=?", (old_security,))
        self.con.commit()

        summary = services.security_audit_summary(self.con, hours=24, recent_limit=10)

        self.assertEqual(summary["total_window"], 3)
        self.assertEqual(summary["total_7d"], 4)
        by_action = {row["action"]: row["count"] for row in summary["by_action"]}
        self.assertEqual(by_action["security.login_failed"], 1)
        self.assertEqual(by_action["server.error"], 1)
        self.assertEqual(by_action["auth.email_send_failed"], 1)
        self.assertNotIn("account.profile_update", by_action)
        recent_actions = [row["action"] for row in summary["recent"]]
        self.assertIn("security.rate_limited", recent_actions)
        self.assertNotIn("account.profile_update", recent_actions)
        self.assertEqual(summary["recent"][-1]["id"], login_failed)

    def test_audit_retention_prunes_only_expired_events(self):
        old_id = services.record_audit_event(self.con, None, "old.event", target_type="unit")
        recent_id = services.record_audit_event(self.con, None, "recent.event", target_type="unit")
        self.con.execute("UPDATE audit_events SET created_at=datetime('now', '-45 days') WHERE id=?", (old_id,))
        self.con.commit()

        summary = services.audit_retention_summary(self.con, days=30)
        self.assertEqual(summary["expired"], 1)
        result = services.prune_audit_events(self.con, days=30)

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["remaining"], 1)
        events = services.audit_events(self.con)
        self.assertEqual([event["id"] for event in events], [recent_id])

    def test_audit_retention_rejects_unsafe_window(self):
        days, ok, detail = services.audit_retention_config(0)

        self.assertFalse(ok)
        self.assertEqual(days, services.DEFAULT_AUDIT_RETENTION_DAYS)
        self.assertIn("审计日志保留期", detail)
        with self.assertRaisesRegex(ValueError, "审计日志保留期"):
            services.prune_audit_events(self.con, days=0)

    def test_user_consent_records_versions_and_latest_summary(self):
        user_id = self.register_user()
        first = services.record_user_consent(
            self.con,
            user_id,
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            source="test",
            ip_address="127.0.0.1",
            user_agent="UnitTest",
        )
        second = services.record_user_consent(
            self.con,
            user_id,
            "2026-07-01",
            "2026-07-01",
            "2026-07-01",
            source="renewal",
        )

        latest = services.latest_user_consent(self.con, user_id)
        self.assertEqual(latest["id"], second)
        self.assertEqual(latest["terms_version"], "2026-07-01")
        summary = services.user_consent_summary(self.con)
        self.assertEqual(summary[0]["user_id"], user_id)
        self.assertEqual(summary[0]["terms_version"], "2026-07-01")
        self.assertGreater(second, first)

    def test_wechat_session_records_prelogin_legal_acceptance(self):
        token = services.create_wechat_session(
            self.con,
            terms_version="2026-06-24",
            privacy_version="2026-06-24",
            risk_version="2026-06-24",
        )

        acceptance = services.wechat_session_legal_acceptance(self.con, token)

        self.assertIsNotNone(acceptance)
        self.assertEqual(acceptance["accepted_terms_version"], "2026-06-24")
        self.assertEqual(acceptance["accepted_privacy_version"], "2026-06-24")
        self.assertEqual(acceptance["accepted_risk_version"], "2026-06-24")
        self.assertTrue(acceptance["accepted_at"])

    def test_content_reports_can_be_created_and_resolved(self):
        admin_id = self.register_user()
        reporter_id = services.get_or_create_user(self.con, "dev-reporter", "举报用户")
        post_id = services.create_post(self.con, admin_id, "待举报复盘", "内容需要管理员看看", "forum")
        comment_id = services.add_comment(self.con, admin_id, post_id, "评论也可以被举报")

        post_report = services.create_content_report(self.con, reporter_id, "post", post_id, "疑似广告")
        duplicate = services.create_content_report(self.con, reporter_id, "post", post_id, "重复举报")
        comment_report = services.create_content_report(self.con, reporter_id, "comment", comment_id, "不友善")

        self.assertEqual(post_report, duplicate)
        reports = services.content_reports(self.con)
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0]["id"], comment_report)
        self.assertEqual(reports[0]["target_type"], "comment")
        self.assertEqual(reports[1]["target_type"], "post")

        services.resolve_content_report(self.con, admin_id, post_report, "resolved", "已提醒作者")
        resolved = [r for r in services.content_reports(self.con) if r["id"] == post_report][0]
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["resolver_user_id"], admin_id)
        self.assertEqual(resolved["resolution_note"], "已提醒作者")

    def test_real_wechat_callback_stores_profile_avatar(self):
        token = services.create_wechat_session(self.con)
        with patch.dict("os.environ", {"WECHAT_APP_ID": "appid", "WECHAT_APP_SECRET": "secret"}, clear=True):
            with patch(
                "src.app.services._json_get",
                side_effect=[
                    {"openid": "real-openid", "access_token": "access-token"},
                    {"nickname": "真实微信用户", "headimgurl": "https://wx.example/avatar.jpg"},
                ],
            ):
                user_id = services.confirm_wechat_oauth_code(self.con, token, "oauth-code")

        user = services.get_user(self.con, user_id)
        account = services.account_for_user(self.con, user_id)
        history = services.equity_history(self.con, user_id)

        self.assertEqual(user["nickname"], "真实微信用户")
        self.assertEqual(user["wechat_openid"], "wechat-real-openid")
        self.assertEqual(user["avatar_url"], "https://wx.example/avatar.jpg")
        self.assertIsNotNone(account)
        self.assertEqual(history[0]["source"], "wechat")

        services.get_or_create_user(self.con, "wechat-real-openid", "新昵称")
        updated = services.get_user(self.con, user_id)
        self.assertEqual(updated["nickname"], "新昵称")
        self.assertEqual(updated["avatar_url"], "https://wx.example/avatar.jpg")

    def test_update_user_profile_validates_nickname_and_avatar(self):
        user_id = self.register_user()

        services.update_user_profile(self.con, user_id, "新资料名", "https://img.example/me.jpg")
        user = services.get_user(self.con, user_id)
        self.assertEqual(user["nickname"], "新资料名")
        self.assertEqual(user["avatar_url"], "https://img.example/me.jpg")

        with self.assertRaisesRegex(ValueError, "昵称不能为空"):
            services.update_user_profile(self.con, user_id, "", "")
        with self.assertRaisesRegex(ValueError, "头像 URL"):
            services.update_user_profile(self.con, user_id, "新资料名", "javascript:alert(1)")

    def test_admin_defaults_to_first_user_and_can_be_configured(self):
        first = self.register_user()
        second = services.get_or_create_user(self.con, "dev-second", "第二用户")
        first_user = services.get_user(self.con, first)
        second_user = services.get_user(self.con, second)

        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(services.is_admin(self.con, first_user))
            self.assertFalse(services.is_admin(self.con, second_user))

        with patch.dict("os.environ", {"OWQ_ADMIN_USER_IDS": str(second)}, clear=True):
            self.assertFalse(services.is_admin(self.con, first_user))
            self.assertTrue(services.is_admin(self.con, second_user))

        with patch.dict("os.environ", {"OWQ_ADMIN_OPENIDS": "dev-second"}, clear=True):
            self.assertFalse(services.is_admin(self.con, first_user))
            self.assertTrue(services.is_admin(self.con, second_user))

    def test_first_user_admin_fallback_is_development_only(self):
        first = self.register_user()
        second = services.get_or_create_user(self.con, "dev-second", "第二用户")
        first_user = services.get_user(self.con, first)
        second_user = services.get_user(self.con, second)

        with patch.dict("os.environ", {"OWQ_PUBLIC_BASE_URL": "https://quant.example"}, clear=True):
            self.assertFalse(services.is_admin(self.con, first_user))
            self.assertFalse(services.is_admin(self.con, second_user))

        with patch.dict("os.environ", {"OWQ_ENV": "production"}, clear=True):
            self.assertFalse(services.is_admin(self.con, first_user))
            self.assertFalse(services.is_admin(self.con, second_user))

        with patch.dict(
            "os.environ",
            {"OWQ_PUBLIC_BASE_URL": "https://quant.example", "OWQ_ADMIN_USER_IDS": str(second)},
            clear=True,
        ):
            self.assertFalse(services.is_admin(self.con, first_user))
            self.assertTrue(services.is_admin(self.con, second_user))

    def test_user_status_can_suspend_and_restore_account(self):
        user_id = self.register_user()

        services.update_user_status(self.con, user_id, "suspended", "异常刷屏")

        user = services.get_user(self.con, user_id)
        self.assertEqual(user["status"], "suspended")
        self.assertEqual(user["status_reason"], "异常刷屏")
        self.assertTrue(user["status_updated_at"])
        with self.assertRaisesRegex(ValueError, "账户已被暂停.*异常刷屏"):
            services.ensure_user_active(user)
        overview = services.account_overview(self.con)
        self.assertEqual(overview[0]["row"]["status"], "suspended")

        services.update_user_status(self.con, user_id, "active", "ignored")

        restored = services.get_user(self.con, user_id)
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["status_reason"], "")
        services.ensure_user_active(restored)

    def test_suspended_user_cannot_write_trading_or_forum_content(self):
        author_id = self.register_user()
        blocked_id = services.get_or_create_user(self.con, "dev-blocked", "暂停用户")
        post_id = services.create_post(self.con, author_id, "正常讨论帖", "用于验证暂停账户拦截", "forum")
        comment_id = services.add_comment(self.con, author_id, post_id, "正常评论")

        services.update_user_status(self.con, blocked_id, "suspended", "风控复核")

        with self.assertRaisesRegex(ValueError, "账户已被暂停.*风控复核"):
            services.place_order(self.con, blocked_id, "000001.SZ", "buy", 100)
        with self.assertRaisesRegex(ValueError, "账户已被暂停"):
            services.create_practice_signal(self.con, blocked_id, "暂停演练", "000001.SZ", "buy", 100, "")
        with self.assertRaisesRegex(ValueError, "账户已被暂停"):
            services.create_post(self.con, blocked_id, "暂停发帖", "应该被拦截", "forum")
        with self.assertRaisesRegex(ValueError, "账户已被暂停"):
            services.add_comment(self.con, blocked_id, post_id, "应该被拦截")
        with self.assertRaisesRegex(ValueError, "账户已被暂停"):
            services.create_content_report(self.con, blocked_id, "comment", comment_id, "应该被拦截")

    def test_buy_and_sell_updates_portfolio(self):
        user_id = self.register_user()
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        after_buy = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(after_buy["holdings"][0]["qty"], 100)
        self.assertEqual(after_buy["holdings"][0]["available_qty"], 0)
        self.assertLess(after_buy["cash"], services.INITIAL_CASH)

        with self.assertRaisesRegex(ValueError, "可卖数量不足"):
            services.place_order(self.con, user_id, "000001.SZ", "sell", 50)

        settled = services.settle_account(self.con, user_id)
        self.assertEqual(settled, 1)
        after_settle = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(after_settle["holdings"][0]["available_qty"], 100)

        services.place_order(self.con, user_id, "000001.SZ", "sell", 50)
        after_sell = services.portfolio_snapshot(self.con, user_id)
        self.assertEqual(after_sell["holdings"][0]["qty"], 50)
        self.assertEqual(after_sell["holdings"][0]["available_qty"], 50)
        self.assertEqual(len(services.recent_orders(self.con, user_id)), 2)
        self.assertEqual(len(services.equity_history(self.con, user_id)), 4)

    def test_buy_rejects_non_lot_size(self):
        user_id = self.register_user()
        with self.assertRaisesRegex(ValueError, "100 股整数倍"):
            services.place_order(self.con, user_id, "000001.SZ", "buy", 1)

    def test_practice_signal_can_be_executed_or_cancelled(self):
        user_id = self.register_user()
        signal_id = services.create_practice_signal(
            self.con,
            user_id,
            "ETF 轮动演练",
            "510300.SH",
            "buy",
            100,
            "突破均线后小仓位验证",
        )

        pending = services.practice_signals(self.con, user_id, status="pending")
        self.assertEqual(pending[0]["id"], signal_id)
        self.assertEqual(pending[0]["strategy_name"], "ETF 轮动演练")

        order_id = services.execute_practice_signal(self.con, user_id, signal_id)
        signals = services.practice_signals(self.con, user_id)
        snapshot = services.portfolio_snapshot(self.con, user_id)

        self.assertEqual(signals[0]["status"], "executed")
        self.assertEqual(signals[0]["order_id"], order_id)
        self.assertEqual(snapshot["holdings"][0]["code"], "510300.SH")
        self.assertEqual(snapshot["holdings"][0]["qty"], 100)
        with self.assertRaisesRegex(ValueError, "只能取消"):
            services.cancel_practice_signal(self.con, user_id, signal_id)

        cancel_id = services.create_practice_signal(self.con, user_id, "退出观察", "510300.SH", "sell", 50, "")
        services.cancel_practice_signal(self.con, user_id, cancel_id)
        cancelled = services.practice_signals(self.con, user_id)
        self.assertEqual(cancelled[0]["status"], "cancelled")

    def test_pending_practice_signals_can_be_executed_in_batch(self):
        user_id = self.register_user()
        services.create_practice_signal(self.con, user_id, "批量演练", "000001.SZ", "buy", 100, "第一笔")
        services.create_practice_signal(self.con, user_id, "批量演练", "510300.SH", "buy", 1000, "第二笔")

        result = services.execute_pending_practice_signals(self.con, user_id)
        signals = services.practice_signals(self.con, user_id)
        snapshot = services.portfolio_snapshot(self.con, user_id)

        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["executed"]), 2)
        self.assertEqual(result["failed"], [])
        self.assertTrue(all(s["status"] == "executed" for s in signals))
        self.assertEqual(len(services.recent_orders(self.con, user_id)), 2)
        self.assertEqual({h["code"] for h in snapshot["holdings"]}, {"000001.SZ", "510300.SH"})

    def test_pending_practice_signal_batch_keeps_failed_items_pending(self):
        user_id = self.register_user()
        services.create_practice_signal(self.con, user_id, "大额演练", "600519.SH", "buy", 100000, "现金不足")
        services.create_practice_signal(self.con, user_id, "小额演练", "000001.SZ", "buy", 100, "可以成交")

        result = services.execute_pending_practice_signals(self.con, user_id)
        signals = sorted(services.practice_signals(self.con, user_id), key=lambda row: int(row["id"]))

        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["executed"]), 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("现金不足", result["failed"][0]["error"])
        self.assertEqual(signals[0]["status"], "pending")
        self.assertEqual(signals[1]["status"], "executed")

    def test_practice_signal_batch_imports_strategy_basket_atomically(self):
        user_id = self.register_user()
        count = services.create_practice_signal_batch(
            self.con,
            user_id,
            "多因子篮子",
            "code,side,qty,rationale\n000001.SZ,buy,100,反转得分靠前\n510300.SH,买入,1000,低波动配置\n",
        )

        signals = services.practice_signals(self.con, user_id)
        self.assertEqual(count, 2)
        self.assertEqual([s["code"] for s in signals], ["510300.SH", "000001.SZ"])
        self.assertEqual(signals[0]["side"], "buy")
        self.assertEqual(signals[0]["qty"], 1000)
        self.assertEqual(signals[0]["strategy_name"], "多因子篮子")

        with self.assertRaisesRegex(ValueError, "第 2 行: 标的不存在"):
            services.create_practice_signal_batch(
                self.con,
                user_id,
                "非法篮子",
                "000001.SZ,buy,100\nNOPE,buy,100\n",
            )
        self.assertEqual(len(services.practice_signals(self.con, user_id)), 2)

    def test_practice_signals_can_be_generated_from_market_data(self):
        user_id = self.register_user()
        reversal = services.market_signal_basket_rows(self.con, mode="reversal", qty=100, limit=2)
        self.assertEqual([row["code"] for row in reversal], ["000001.SZ", "510300.SH"])
        self.assertIn("反转候选", reversal[0]["rationale"])

        count = services.create_practice_signals_from_market(
            self.con,
            user_id,
            "基础行情动量篮子",
            mode="momentum",
            qty=100,
            limit=2,
        )
        signals = services.practice_signals(self.con, user_id)

        self.assertEqual(count, 2)
        self.assertEqual({s["code"] for s in signals}, {"159915.SZ", "600519.SH"})
        self.assertTrue(all(s["strategy_name"] == "基础行情动量篮子" for s in signals))
        self.assertTrue(all("动量候选" in s["rationale"] for s in signals))

    def test_prediction_csv_can_generate_practice_signals(self):
        user_id = self.register_user()
        pred_path = Path(self.tmpdir.name) / "predictions.csv"
        pred_path.write_text(
            "code,prediction,last_close\n"
            "600519.SH,0.031,1222.45\n"
            "000001.SZ,0.012,10.71\n",
            encoding="utf-8",
        )

        rows = services.prediction_basket_rows(self.con, path=pred_path, qty=100, limit=1)
        count = services.create_practice_signals_from_predictions(
            self.con,
            user_id,
            "预测篮子",
            qty=100,
            limit=1,
            path=pred_path,
        )
        signals = services.practice_signals(self.con, user_id)

        self.assertEqual(rows[0]["code"], "600519.SH")
        self.assertEqual(rows[0]["last_close"], 1448.0)
        self.assertEqual(count, 1)
        self.assertEqual(signals[0]["code"], "600519.SH")
        self.assertIn("预测候选", signals[0]["rationale"])

    def test_reset_paper_account_clears_trading_state_only(self):
        user_id = self.register_user()
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        services.create_practice_signal(self.con, user_id, "待清空计划", "000001.SZ", "buy", 100, "")
        post_id = services.create_post(self.con, user_id, "重新演练前复盘", "保留社区内容", "practice")
        account = services.account_for_user(self.con, user_id)

        services.reset_paper_account(self.con, user_id)

        snapshot = services.portfolio_snapshot(self.con, user_id)
        orders = services.recent_orders(self.con, user_id)
        history = services.equity_history(self.con, user_id)
        posts = services.forum_posts(self.con)
        holdings = self.con.execute("SELECT COUNT(*) AS n FROM holdings WHERE account_id=?", (account["id"],)).fetchone()

        self.assertEqual(holdings["n"], 0)
        self.assertEqual(orders, [])
        self.assertAlmostEqual(snapshot["cash"], services.INITIAL_CASH)
        self.assertEqual(snapshot["market_value"], 0)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "reset")
        self.assertEqual(services.practice_signals(self.con, user_id), [])
        self.assertEqual(posts[0]["id"], post_id)

    def test_delete_user_account_removes_personal_app_data(self):
        token = services.create_email_login_session(
            self.con,
            "delete-me@example.com",
            "2026-06-24",
            "2026-06-24",
            "2026-06-24",
            enforce_rate_limit=False,
        )
        user_id = services.confirm_email_login_session(self.con, token)
        other_id = services.get_or_create_user(self.con, "dev-delete-other", "保留用户")
        account_id = services.account_for_user(self.con, user_id)["id"]
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        services.create_practice_signal(self.con, user_id, "删除前计划", "000001.SZ", "buy", 100, "")
        services.record_user_consent(self.con, user_id, "2026-06-24", "2026-06-24", "2026-06-24", source="test")
        post_id = services.create_post(self.con, user_id, "准备注销的帖子", "会随账户删除", "privacy")
        own_comment_id = services.add_comment(self.con, user_id, post_id, "自己的评论")
        other_post_id = services.create_post(self.con, other_id, "其他用户帖子", "应该保留", "forum")
        services.add_comment(self.con, user_id, other_post_id, "注销用户在别人帖子下的评论")
        services.create_content_report(self.con, user_id, "post", other_post_id, "由注销用户举报")
        services.create_content_report(self.con, other_id, "post", post_id, "举报注销用户帖子")
        services.create_content_report(self.con, other_id, "comment", own_comment_id, "举报注销用户评论")

        summary = services.delete_user_account(self.con, user_id)

        self.assertEqual(summary["user_id"], user_id)
        self.assertEqual(summary["account_id"], account_id)
        self.assertIsNone(services.get_user(self.con, user_id))
        self.assertIsNone(services.account_for_user(self.con, user_id))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM accounts WHERE id=?", (account_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM orders WHERE account_id=?", (account_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM practice_signals WHERE user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM user_consents WHERE user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM email_login_sessions WHERE email='delete-me@example.com'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM forum_posts WHERE user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM forum_comments WHERE user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertIsNotNone(services.get_post(self.con, other_post_id))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM content_reports WHERE reporter_user_id=?", (user_id,)).fetchone()[0], 0)
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM content_reports WHERE target_type='post' AND target_id=?", (post_id,)).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM content_reports WHERE target_type='comment' AND target_id=?", (own_comment_id,)).fetchone()[0],
            0,
        )

    def test_forum_post_and_comment(self):
        user_id = self.register_user()
        post_id = services.create_post(self.con, user_id, "反转策略复盘", "本周模拟盘表现稳定", "reversal")
        comment_id = services.add_comment(self.con, user_id, post_id, "补充一下换手和成本。")

        post = services.get_post(self.con, post_id)
        comments = services.post_comments(self.con, post_id)
        self.assertEqual(post["title"], "反转策略复盘")
        self.assertIsNotNone(post["snapshot_equity"])
        self.assertIsNotNone(post["snapshot_return_pct"])
        self.assertEqual(comments[0]["id"], comment_id)

    def test_forum_posts_can_be_filtered_and_sorted(self):
        user_id = self.register_user()
        low_vol_id = services.create_post(self.con, user_id, "低波动组合复盘", "低波动 ETF 组合", "low-vol")
        reversal_id = services.create_post(self.con, user_id, "反转观察", "短期反转观察记录", "reversal")
        alpha_id = services.create_post(self.con, user_id, "Alpha 组合周记", "多因子 alpha 观察", "low-vol")
        services.add_comment(self.con, user_id, low_vol_id, "第一条评论")
        services.add_comment(self.con, user_id, low_vol_id, "第二条评论")
        services.add_comment(self.con, user_id, reversal_id, "反转评论")

        low_vol_posts = services.forum_posts(self.con, tag="low-vol")
        search_posts = services.forum_posts(self.con, q="Alpha")
        commented_posts = services.forum_posts(self.con, sort="comments")
        tags = {row["tag"]: row["count"] for row in services.forum_tags(self.con)}

        self.assertEqual({p["id"] for p in low_vol_posts}, {low_vol_id, alpha_id})
        self.assertEqual([p["id"] for p in search_posts], [alpha_id])
        self.assertEqual(commented_posts[0]["id"], low_vol_id)
        self.assertEqual(tags["low-vol"], 2)
        self.assertEqual(tags["reversal"], 1)

    def test_comment_can_be_deleted_by_author_or_admin_only(self):
        admin_id = self.register_user()
        author_id = services.get_or_create_user(self.con, "dev-comment-author", "评论作者")
        outsider_id = services.get_or_create_user(self.con, "dev-comment-outsider", "旁观者")
        post_id = services.create_post(self.con, admin_id, "讨论帖", "测试评论删除权限", "forum")
        comment_id = services.add_comment(self.con, author_id, post_id, "准备删除的评论")

        with self.assertRaisesRegex(ValueError, "无权删除评论"):
            services.delete_comment(self.con, outsider_id, comment_id)

        deleted_post_id = services.delete_comment(self.con, author_id, comment_id)
        self.assertEqual(deleted_post_id, post_id)
        self.assertEqual(services.post_comments(self.con, post_id), [])

        admin_delete_id = services.add_comment(self.con, author_id, post_id, "管理员可删除")
        deleted_post_id = services.delete_comment(self.con, admin_id, admin_delete_id)
        self.assertEqual(deleted_post_id, post_id)
        self.assertEqual(services.post_comments(self.con, post_id), [])

    def test_post_can_be_deleted_by_author_or_admin_only(self):
        admin_id = self.register_user()
        author_id = services.get_or_create_user(self.con, "dev-post-author", "发帖作者")
        outsider_id = services.get_or_create_user(self.con, "dev-post-outsider", "旁观者")
        post_id = services.create_post(self.con, author_id, "待删除复盘", "写错后撤回", "forum")
        services.add_comment(self.con, admin_id, post_id, "会随帖子删除")

        with self.assertRaisesRegex(ValueError, "无权删除帖子"):
            services.delete_post(self.con, outsider_id, post_id)

        services.delete_post(self.con, author_id, post_id)
        self.assertIsNone(services.get_post(self.con, post_id))
        self.assertEqual(services.post_comments(self.con, post_id), [])

        admin_delete_id = services.create_post(self.con, author_id, "管理员删除帖", "管理员可维护论坛", "forum")
        services.delete_post(self.con, admin_id, admin_delete_id)
        self.assertIsNone(services.get_post(self.con, admin_delete_id))

    def test_public_profile_contains_rank_snapshot_and_posts(self):
        user_id = self.register_user()
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        post_id = services.create_post(self.con, user_id, "实盘演练记录", "买入后观察成本影响", "paper")

        profile = services.public_profile(self.con, user_id)

        self.assertEqual(profile["user"]["id"], user_id)
        self.assertEqual(profile["rank"], 1)
        self.assertEqual(profile["posts"][0]["id"], post_id)
        self.assertGreaterEqual(len(profile["history"]), 2)
        self.assertEqual(profile["orders"][0]["code"], "000001.SZ")
        self.assertEqual(profile["snapshot"]["holdings"][0]["available_qty"], 0)
        self.assertLess(profile["snapshot"]["equity"], services.INITIAL_CASH)

    def test_performance_post_draft_contains_trading_context(self):
        user_id = self.register_user()
        signal_id = services.create_practice_signal(self.con, user_id, "反转演练", "000001.SZ", "buy", 100, "回撤后观察")
        services.execute_practice_signal(self.con, user_id, signal_id)

        draft = services.performance_post_draft(self.con, user_id, "http://example.test/u/1")

        self.assertEqual(draft["tag"], "performance")
        self.assertIn("模拟盘战绩复盘", draft["title"])
        self.assertIn("公开赛排名 #1", draft["body"])
        self.assertIn("000001.SZ", draft["body"])
        self.assertIn("可卖 0 股", draft["body"])
        self.assertIn("最近成交", draft["body"])
        self.assertIn("策略演练计划", draft["body"])
        self.assertIn("http://example.test/u/1", draft["body"])

    def test_update_contest_and_account_overview(self):
        user_id = self.register_user()
        services.place_order(self.con, user_id, "000001.SZ", "buy", 100)
        services.create_post(self.con, user_id, "策略记录", "记录一次模拟交易", "paper")

        contest_id = services.update_active_contest(self.con, "六月模拟赛", "面向公开分享的模拟盘比赛")
        contest = services.active_contest(self.con)
        overview = services.account_overview(self.con)

        self.assertEqual(contest["id"], contest_id)
        self.assertEqual(contest["title"], "六月模拟赛")
        self.assertEqual(overview[0]["row"]["user_id"], user_id)
        self.assertEqual(overview[0]["rank"], 1)
        self.assertEqual(overview[0]["row"]["order_count"], 1)
        self.assertEqual(overview[0]["row"]["post_count"], 1)

    def test_record_all_equity_snapshots(self):
        user_id = self.register_user()
        count = services.record_all_equity_snapshots(self.con, source="manual_test")
        history = services.equity_history(self.con, user_id)

        self.assertEqual(count, 1)
        self.assertEqual(history[-1]["source"], "manual_test")

    def test_seed_demo_competition_is_idempotent(self):
        first = services.seed_demo_competition(self.con)
        second = services.seed_demo_competition(self.con)
        board = services.leaderboard(self.con)
        posts = services.forum_posts(self.con)

        self.assertEqual(first["players"], 3)
        self.assertEqual(second["players"], 3)
        self.assertGreaterEqual(len(board), 3)
        self.assertEqual(first["posts_created"], 3)
        self.assertEqual(second["posts_created"], 0)
        self.assertEqual(len([p for p in posts if str(p["strategy_tag"]) in {"low-vol", "reversal", "growth"}]), 3)

    def test_remove_demo_contest_participants_keeps_accounts_but_cleans_leaderboard(self):
        services.seed_demo_competition(self.con)
        before = services.demo_contest_participant_summary(self.con)

        result = services.remove_demo_contest_participants(self.con)

        after = services.demo_contest_participant_summary(self.con)
        self.assertEqual(before["participants"], 3)
        self.assertEqual(result["participants_removed"], 3)
        self.assertEqual(after["participants"], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM users WHERE wechat_openid LIKE 'demo-%'").fetchone()[0], 3)
        self.assertEqual(len(services.leaderboard(self.con)), 0)


if __name__ == "__main__":
    unittest.main()

"""AI orchestration tests against a mocked provider (no network, no real key)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.app import db, services
from src.app.ai import guardrail, service

SECRET = "unit-test-server-secret"
LEAK = ["unit-test-server-secret", "sk-realserversecret"]


class AiServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.bootstrap(Path(self.tmp.name) / "app.sqlite")
        token = services.create_wechat_session(self.con)
        self.uid = services.confirm_wechat_session(self.con, token, "AI用户")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _save_key(self, key="sk-userkey1234567890"):
        service.save_key(self.con, self.uid, SECRET, key, "https://api.deepseek.com", "deepseek-chat")

    def test_key_roundtrip_and_masking(self):
        self._save_key("sk-abcdef1234567890")
        row = service.get_key_row(self.con, self.uid)
        self.assertEqual(row["masked_hint"], "sk-…7890")
        self.assertNotIn(b"sk-abcdef", bytes(row["ciphertext"]))  # never plaintext at rest
        plaintext, base, model, cap = service.resolve_key(self.con, self.uid, SECRET)
        self.assertEqual(plaintext, "sk-abcdef1234567890")

    def test_no_key_returns_friendly_prompt(self):
        out = service.ai_complete(self.con, self.uid, kind="review", user_message="hi",
                                  secret=SECRET, leak_check_secrets=LEAK)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "no_key")

    def test_kill_switch(self):
        self._save_key()
        with patch.dict("os.environ", {"OWQ_AI_DISABLED": "1"}, clear=False):
            out = service.ai_complete(self.con, self.uid, kind="review", user_message="hi",
                                      secret=SECRET, leak_check_secrets=LEAK)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "ai_disabled")

    def test_success_appends_disclaimer_and_records(self):
        self._save_key()
        fake = {"text": "反转因子要注意流动性风险。", "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}, "model": "deepseek-v4-flash"}
        with patch("src.app.ai.client.chat_completion", return_value=fake):
            out = service.ai_complete(self.con, self.uid, kind="review", user_message="复盘",
                                      secret=SECRET, leak_check_secrets=LEAK)
        self.assertTrue(out["ok"])
        self.assertFalse(out["blocked"])
        self.assertIn("不构成任何投资建议", out["text"])  # disclaimer appended
        self.assertEqual(service.daily_tokens(self.con, self.uid), 18)
        inter = self.con.execute("SELECT * FROM ai_interactions WHERE user_id=?", (self.uid,)).fetchone()
        self.assertEqual(int(inter["blocked"]), 0)

    def test_guardrail_blocks_stock_tip_from_model(self):
        self._save_key()
        fake = {"text": "建议买入 000001.SZ,目标价 15 元。", "usage": {"total_tokens": 20}, "model": "x"}
        with patch("src.app.ai.client.chat_completion", return_value=fake):
            out = service.ai_complete(self.con, self.uid, kind="review", user_message="买啥",
                                      secret=SECRET, leak_check_secrets=LEAK)
        self.assertTrue(out["ok"])
        self.assertTrue(out["blocked"])
        self.assertEqual(out["text"], guardrail.BLOCKED_MESSAGE)
        inter = self.con.execute("SELECT * FROM ai_interactions WHERE user_id=?", (self.uid,)).fetchone()
        self.assertEqual(int(inter["blocked"]), 1)
        self.assertIn("ticker_with_intent", inter["reasons"])

    def test_daily_quota_enforced(self):
        self._save_key()
        self.con.execute("UPDATE ai_user_keys SET daily_token_cap=10 WHERE user_id=?", (self.uid,))
        self.con.execute("INSERT INTO ai_usage(user_id, total_tokens) VALUES (?, 50)", (self.uid,))
        self.con.commit()
        out = service.ai_complete(self.con, self.uid, kind="review", user_message="hi",
                                  secret=SECRET, leak_check_secrets=LEAK)
        self.assertEqual(out["error"], "quota")

    def test_secret_leak_is_blocked_before_network(self):
        self._save_key()
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            return {"text": "x", "usage": {}, "model": "x"}

        with patch("src.app.ai.client.chat_completion", side_effect=boom):
            with self.assertRaises(ValueError):
                # context_text carrying a server secret must be refused pre-send
                service.ai_complete(self.con, self.uid, kind="review",
                                    user_message="hi", context_text="leak " + SECRET,
                                    secret=SECRET, leak_check_secrets=LEAK)
        self.assertEqual(called["n"], 0)  # never reached the provider

    def test_explain_my_result_requires_data(self):
        self._save_key()
        out = service.explain_my_result(self.con, self.uid, secret=SECRET, leak_check_secrets=LEAK)
        self.assertEqual(out["error"], "no_data")

    def test_explain_my_result_with_a_trade(self):
        self._save_key()
        services.place_order(self.con, self.uid, "000001.SZ", "buy", 100)
        fake = {"text": "你这笔交易的依据可以更明确。", "usage": {"total_tokens": 12}, "model": "x"}
        with patch("src.app.ai.client.chat_completion", return_value=fake):
            out = service.explain_my_result(self.con, self.uid, secret=SECRET, leak_check_secrets=LEAK)
        self.assertTrue(out["ok"])
        self.assertIn("不构成任何投资建议", out["text"])


if __name__ == "__main__":
    unittest.main()

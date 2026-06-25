"""AI client egress/SSRF guard tests (no network)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from src.app.ai import client


class EgressAllowlistTest(unittest.TestCase):
    def test_accepts_default_deepseek_https(self):
        self.assertEqual(client.validate_base_url("https://api.deepseek.com"), "https://api.deepseek.com")
        self.assertEqual(client.validate_base_url("https://api.deepseek.com/"), "https://api.deepseek.com")

    def test_rejects_http(self):
        with self.assertRaises(client.EgressError):
            client.validate_base_url("http://api.deepseek.com")

    def test_rejects_ip_and_localhost(self):
        for url in [
            "https://127.0.0.1",
            "https://169.254.169.254",  # cloud metadata endpoint
            "https://[::1]",
            "https://localhost",
            "https://evil.localhost",
        ]:
            with self.subTest(url=url), self.assertRaises(client.EgressError):
                client.validate_base_url(url)

    def test_rejects_non_allowlisted_host(self):
        with self.assertRaises(client.EgressError):
            client.validate_base_url("https://attacker.example.com")

    def test_allowlist_is_env_extendable(self):
        with patch.dict("os.environ", {"OWQ_AI_EGRESS_ALLOWLIST": "api.openai.com, my.proxy.example"}, clear=False):
            self.assertEqual(client.validate_base_url("https://api.openai.com"), "https://api.openai.com")
            self.assertEqual(client.validate_base_url("https://my.proxy.example"), "https://my.proxy.example")

    def test_no_key_raises_before_network(self):
        with self.assertRaises(client.ProviderError):
            client.chat_completion("", [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()

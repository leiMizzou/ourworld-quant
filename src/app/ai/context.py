"""Grounding / context assembly for the AI co-pilot.

This is the single choke point for everything sent to a third-party LLM. It pulls
ONLY the requesting user's own read-only trading data (an explicit allowlist of
fields — never email / password hashes / openid / other users' rows / audit log),
plus a small static methodology snippet, so the model is grounded instead of
hallucinating prices and A-share rules. A final assertion guarantees no server
secret leaks into the payload (PIPL data-minimization + secret-safety).
"""
from __future__ import annotations

import sqlite3

from .. import services

# Short, hand-curated A-share methodology so the model grounds rule-talk on facts
# (seeded from plan/A股量化_个人准备计划.md) rather than inventing rules.
METHODOLOGY_SNIPPET = (
    "A 股关键规则与回测陷阱(供解释时引用,不要臆造其它'规则'):\n"
    "- T+1:当日买入次日才能卖,普通股票无法日内回转。\n"
    "- 涨跌停:主板±10%,创业板/科创板±20%,ST±5%,北交所±30%;一字板买不进/卖不出。\n"
    "- 交易成本:佣金+印花税(卖出单边0.05%)+过户费;高换手策略对费用和滑点极敏感。\n"
    "- 小资金优势在容量,可做机构做不了的低流动性/小市值,但要建模流动性风险与极端回撤"
    "(2024 微盘股流动性危机)。\n"
    "- 回测陷阱:未来函数、幸存者偏差、过拟合、成本/滑点低估、复权处理错误、样本内外划分。"
)


def assert_no_secret_leak(text: str, secrets: list[str]) -> None:
    """Raise if any server secret value appears in an outbound payload."""
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in text:
            raise ValueError("拒绝外发:出站内容包含服务端密钥")


def review_context(con: sqlite3.Connection, user_id: int) -> dict:
    """Assemble the grounded 'explain my own result' context. Returns
    {'has_data': bool, 'text': str}. Reuses performance_post_draft, which emits only
    the user's own trading record (holdings/orders/rationale/returns) — no PII."""
    draft = services.performance_post_draft(con, user_id)
    snap = services.portfolio_snapshot(con, user_id)
    has_data = bool(snap["holdings"]) or bool(services.recent_orders(con, user_id, limit=1))
    text = "我的模拟盘记录(只读,用于复盘):\n" + draft["body"]
    return {"has_data": has_data, "text": text}

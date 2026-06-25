"""AI orchestration: the single entry point every AI feature calls.

Flow: resolve the user's encrypted key -> enforce kill-switch / enabled / daily cap
-> assemble grounded context -> assert no secret leak -> call provider -> run the
deterministic guardrail filter -> record usage + a full interaction row for audit.

Append-only writes (ai_usage / ai_interactions) are concurrency-safe without the
global POST write lock, so the slow network call must NOT be made under it.
"""
from __future__ import annotations

import os
import sqlite3
import time

from . import client, context, crypto, guardrail

DEFAULT_DAILY_TOKEN_CAP = 200_000
MAX_USER_MESSAGE_CHARS = 2000


def ai_disabled() -> bool:
    return os.getenv("OWQ_AI_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def realdata_enabled() -> bool:
    """Gate for AI features that cite real IC/backtest/prediction numbers. Default OFF
    until the hfq + delisted-universe data rebuild lands."""
    return os.getenv("OWQ_AI_REALDATA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


# ---- key storage ---------------------------------------------------------

def get_key_row(con: sqlite3.Connection, user_id: int):
    return con.execute("SELECT * FROM ai_user_keys WHERE user_id=?", (int(user_id),)).fetchone()


def save_key(con: sqlite3.Connection, user_id: int, secret: str, plaintext: str,
             base_url: str, model: str, status: str = "") -> None:
    base_url = client.validate_base_url(base_url or client.DEFAULT_BASE_URL)
    model = (model or client.DEFAULT_MODEL).strip()[:64]
    ciphertext, nonce = crypto.encrypt_api_key(secret, user_id, plaintext)
    hint = crypto.mask_key(plaintext)
    con.execute(
        """
        INSERT INTO ai_user_keys(user_id, ciphertext, nonce, key_version, base_url, model,
                                 masked_hint, enabled, status, updated_at, last_validated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP, CASE WHEN ?<>'' THEN CURRENT_TIMESTAMP ELSE '' END)
        ON CONFLICT(user_id) DO UPDATE SET
            ciphertext=excluded.ciphertext, nonce=excluded.nonce, key_version=excluded.key_version,
            base_url=excluded.base_url, model=excluded.model, masked_hint=excluded.masked_hint,
            enabled=1, status=excluded.status, updated_at=CURRENT_TIMESTAMP,
            last_validated_at=excluded.last_validated_at
        """,
        (int(user_id), ciphertext, nonce, crypto.KEY_VERSION, base_url, model, hint, status, status),
    )
    con.commit()


def set_enabled(con: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    con.execute("UPDATE ai_user_keys SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (1 if enabled else 0, int(user_id)))
    con.commit()


def delete_key(con: sqlite3.Connection, user_id: int) -> None:
    con.execute("DELETE FROM ai_user_keys WHERE user_id=?", (int(user_id),))
    con.commit()


def resolve_key(con: sqlite3.Connection, user_id: int, secret: str):
    """Return (plaintext, base_url, model, cap) for an enabled key, else None."""
    row = get_key_row(con, user_id)
    if row is None or not int(row["enabled"]):
        return None
    plaintext = crypto.decrypt_api_key(secret, user_id, row["ciphertext"], row["nonce"], int(row["key_version"]))
    cap = int(row["daily_token_cap"] or DEFAULT_DAILY_TOKEN_CAP)
    return plaintext, row["base_url"], row["model"], cap


# ---- accounting ----------------------------------------------------------

def daily_tokens(con: sqlite3.Connection, user_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(total_tokens),0) AS t FROM ai_usage WHERE user_id=? AND created_at >= date('now')",
        (int(user_id),),
    ).fetchone()
    return int(row["t"] if row else 0)


def _record_usage(con, user_id, kind, model, usage, status, latency_ms):
    con.execute(
        """INSERT INTO ai_usage(user_id, request_kind, model, prompt_tokens, completion_tokens,
               total_tokens, status, latency_ms) VALUES (?,?,?,?,?,?,?,?)""",
        (int(user_id), kind[:32], (model or "")[:64], int(usage.get("prompt_tokens", 0) or 0),
         int(usage.get("completion_tokens", 0) or 0), int(usage.get("total_tokens", 0) or 0),
         status[:16], int(latency_ms)),
    )
    con.commit()


def _record_interaction(con, user_id, kind, model, prompt, raw, filtered, blocked, reasons):
    con.execute(
        """INSERT INTO ai_interactions(user_id, request_kind, model, prompt, raw_response,
               filtered_response, blocked, reasons) VALUES (?,?,?,?,?,?,?,?)""",
        (int(user_id), kind[:32], (model or "")[:64], prompt, raw, filtered,
         1 if blocked else 0, ",".join(reasons)[:300]),
    )
    con.commit()


# ---- the single entry point ----------------------------------------------

def ai_complete(
    con: sqlite3.Connection,
    user_id: int,
    *,
    kind: str,
    user_message: str,
    secret: str,
    leak_check_secrets: list[str],
    context_text: str = "",
) -> dict:
    """Run one grounded, guarded completion. Returns:
    {'ok', 'text', 'blocked', 'reasons', 'error'}. Never raises on provider errors."""
    if ai_disabled():
        return {"ok": False, "error": "ai_disabled", "text": "AI 功能当前已关闭。", "blocked": False, "reasons": []}

    resolved = resolve_key(con, user_id, secret)
    if resolved is None:
        return {"ok": False, "error": "no_key",
                "text": "请先在 账户 → AI 配置 里填入你自己的 DeepSeek API key 并启用。", "blocked": False, "reasons": []}
    api_key, base_url, model, cap = resolved

    if daily_tokens(con, user_id) >= cap:
        return {"ok": False, "error": "quota", "text": "今日 AI 用量已达上限,请明天再试或调高额度。", "blocked": False, "reasons": []}

    user_message = (user_message or "").strip()[:MAX_USER_MESSAGE_CHARS]
    parts = [context.METHODOLOGY_SNIPPET]
    if context_text:
        parts.append(guardrail.wrap_untrusted("我的记录", context_text))
    parts.append("我的问题:" + (user_message or "请帮我复盘我的模拟盘表现,指出方法上的问题和可以学习的点。"))
    composed = "\n\n".join(parts)

    # Hard stop: never let a server secret go out to the third-party provider.
    context.assert_no_secret_leak(composed, leak_check_secrets)

    messages = [{"role": "system", "content": guardrail.SYSTEM_PROMPT},
                {"role": "user", "content": composed}]

    started = time.monotonic()
    try:
        out = client.chat_completion(api_key, messages, model=model, base_url=base_url)
    except client.EgressError as exc:
        _record_usage(con, user_id, kind, model, {}, "egress_error", (time.monotonic() - started) * 1000)
        return {"ok": False, "error": "egress", "text": str(exc), "blocked": False, "reasons": []}
    except client.ProviderError as exc:
        _record_usage(con, user_id, kind, model, {}, "error:" + exc.category, (time.monotonic() - started) * 1000)
        msg = "AI 服务暂时不可用,请稍后再试。" if exc.category in {"transient", "network"} else "AI 调用失败,请检查你的 API key 与额度。"
        return {"ok": False, "error": exc.category, "text": msg, "blocked": False, "reasons": []}

    latency_ms = (time.monotonic() - started) * 1000
    raw = out["text"]
    verdict = guardrail.filter_output(raw)
    safe_text = verdict["text"]
    if not verdict["blocked"]:
        safe_text = safe_text.rstrip() + "\n\n— " + guardrail.DISCLAIMER

    status = "blocked" if verdict["blocked"] else "ok"
    _record_usage(con, user_id, kind, out.get("model", model), out.get("usage", {}), status, latency_ms)
    _record_interaction(con, user_id, kind, out.get("model", model), composed, raw, safe_text,
                        verdict["blocked"], verdict["reasons"])
    return {"ok": True, "text": safe_text, "blocked": verdict["blocked"], "reasons": verdict["reasons"], "error": ""}


def explain_my_result(con, user_id, *, secret, leak_check_secrets, question: str = "") -> dict:
    """Phase-2 feature: grounded 'explain MY own paper-trading result' (data-free safe)."""
    ctx = context.review_context(con, user_id)
    if not ctx["has_data"]:
        return {"ok": False, "error": "no_data",
                "text": "你还没有模拟盘记录。先去下单或记录一个策略演练计划,我再帮你复盘。", "blocked": False, "reasons": []}
    return ai_complete(con, user_id, kind="review", user_message=question, secret=secret,
                       leak_check_secrets=leak_check_secrets, context_text=ctx["text"])

"""Minimal OpenAI-compatible chat client over stdlib urllib (no SDK).

Security:
- HTTPS-only egress allowlist (default {api.deepseek.com}, extendable via
  OWQ_AI_EGRESS_ALLOWLIST). Rejects http, IPs, and localhost so a user-configured
  base_url cannot redirect the Authorization: Bearer header to an attacker (SSRF /
  key exfiltration).
- The API key is only ever placed in the Authorization header; it is never included
  in any exception message (errors carry status/category only).
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_ALLOWLIST = {"api.deepseek.com"}
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
MODEL_OPTIONS = (
    ("deepseek-v4-flash", "DeepSeek V4 Flash"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
)


class EgressError(ValueError):
    """base_url failed the egress allowlist / SSRF checks."""


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, category: str = "provider"):
        super().__init__(message)
        self.status = status
        self.category = category


def allowed_hosts() -> set[str]:
    hosts = set(DEFAULT_ALLOWLIST)
    for raw in os.getenv("OWQ_AI_EGRESS_ALLOWLIST", "").split(","):
        host = raw.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def _is_ip_literal(host: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    return False


def validate_base_url(base_url: str) -> str:
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme != "https":
        raise EgressError("AI base_url 必须使用 https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise EgressError("AI base_url 缺少主机名")
    if host == "localhost" or host.endswith(".localhost") or _is_ip_literal(host):
        raise EgressError("AI base_url 不允许指向 IP 或本地地址")
    if host not in allowed_hosts():
        raise EgressError(f"AI base_url 不在出站白名单内: {host}")
    return base_url.rstrip("/")


def chat_completion(
    api_key: str,
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    temperature: float = 0.4,
    max_tokens: int = 900,
    timeout: float = 20.0,
    max_retries: int = 2,
) -> dict:
    """Blocking chat completion. Returns {'text', 'usage', 'model'}.
    Raises EgressError (bad base_url) or ProviderError (HTTP/network)."""
    if not api_key:
        raise ProviderError("未配置 API key", category="no_key")
    base = validate_base_url(base_url)
    url = base + "/chat/completions"
    body = json.dumps(
        {"model": model, "messages": messages, "temperature": temperature,
         "max_tokens": max_tokens, "stream": False}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    last_error: ProviderError | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content")) or ""
            return {"text": text, "usage": data.get("usage") or {}, "model": data.get("model", model)}
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status == 429 or 500 <= status < 600:
                last_error = ProviderError(f"provider HTTP {status}", status=status, category="transient")
                if attempt < max_retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise last_error
            # 4xx (e.g. 401 invalid key, 402 insufficient balance): do not retry, never leak key.
            raise ProviderError(f"provider HTTP {status}", status=status, category="client")
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            last_error = ProviderError("网络错误,无法连接 AI 服务", category="network")
            if attempt < max_retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise last_error
    raise last_error or ProviderError("未知错误")


def test_api_key(api_key: str, *, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL) -> dict:
    """Validate a key with one cheap call. Returns {'ok': bool, 'detail': str}."""
    try:
        out = chat_completion(
            api_key,
            [{"role": "user", "content": "回复 OK"}],
            model=model, base_url=base_url, max_tokens=5, timeout=15.0, max_retries=1,
        )
        return {"ok": True, "detail": "key 可用,模型=" + str(out.get("model", model))}
    except EgressError as exc:
        return {"ok": False, "detail": str(exc)}
    except ProviderError as exc:
        if exc.status == 401:
            return {"ok": False, "detail": "key 无效(401)"}
        if exc.status == 402:
            return {"ok": False, "detail": "余额不足(402)"}
        return {"ok": False, "detail": f"调用失败({exc.category})"}

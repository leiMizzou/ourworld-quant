"""Email sender configuration validation shared by HTTP and readiness checks."""
from __future__ import annotations

import os
from email.utils import parseaddr
from typing import Mapping


TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def _env(environ: Mapping[str, str] | None, name: str, default: str = "") -> str:
    source = os.environ if environ is None else environ
    return str(source.get(name, default) or "").strip()


def _valid_bool(environ: Mapping[str, str] | None, name: str) -> bool:
    raw = _env(environ, name).lower()
    return not raw or raw in TRUE_VALUES or raw in FALSE_VALUES


def _bool_value(environ: Mapping[str, str] | None, name: str, default: bool) -> bool:
    raw = _env(environ, name).lower()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return default


def _valid_email_address(value: str) -> bool:
    if not value or any(ch in value for ch in "\r\n\t<>"):
        return False
    if " " in value:
        return False
    _, parsed = parseaddr(value)
    if parsed != value:
        return False
    local, sep, domain = parsed.partition("@")
    if not sep or not local or not domain or "." not in domain:
        return False
    return True


def _smtp_port(environ: Mapping[str, str] | None) -> tuple[int, bool, str]:
    raw = _env(environ, "OWQ_SMTP_PORT", "587")
    try:
        port = int(raw)
    except ValueError:
        return 587, False, "OWQ_SMTP_PORT 必须是 1-65535 之间的整数"
    if port < 1 or port > 65535:
        return 587, False, "OWQ_SMTP_PORT 必须是 1-65535 之间的整数"
    return port, True, ""


def cloudflare_ready(environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    missing = [
        name
        for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "OWQ_EMAIL_FROM")
        if not _env(environ, name)
    ]
    if missing:
        return False, "Cloudflare Email Sending 未配置完整: 缺少 " + ", ".join(missing)
    if not _valid_email_address(_env(environ, "OWQ_EMAIL_FROM")):
        return False, "OWQ_EMAIL_FROM 必须是单个有效邮箱地址"
    return True, "Cloudflare Email Sending 已配置"


def smtp_ready(environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    missing = [name for name in ("OWQ_SMTP_HOST", "OWQ_EMAIL_FROM") if not _env(environ, name)]
    if missing:
        return False, "SMTP 未配置完整: 缺少 " + ", ".join(missing)
    host = _env(environ, "OWQ_SMTP_HOST").lower()
    if any(ch.isspace() for ch in host):
        return False, "OWQ_SMTP_HOST 不能包含空白字符"
    if not _valid_email_address(_env(environ, "OWQ_EMAIL_FROM")):
        return False, "OWQ_EMAIL_FROM 必须是单个有效邮箱地址"
    _, port_ok, port_detail = _smtp_port(environ)
    if not port_ok:
        return False, port_detail
    if not _valid_bool(environ, "OWQ_SMTP_SSL"):
        return False, "OWQ_SMTP_SSL 必须是 1/0、true/false、yes/no 或 on/off"
    if not _valid_bool(environ, "OWQ_SMTP_TLS"):
        return False, "OWQ_SMTP_TLS 必须是 1/0、true/false、yes/no 或 on/off"
    use_ssl = _bool_value(environ, "OWQ_SMTP_SSL", default=(_smtp_port(environ)[0] == 465))
    use_tls = _bool_value(environ, "OWQ_SMTP_TLS", default=(not use_ssl))
    if use_ssl and use_tls:
        return False, "OWQ_SMTP_SSL 和 OWQ_SMTP_TLS 不能同时启用"
    auth_required_hosts = {"smtp.gmail.com", "smtp.mx.cloudflare.net"}
    if host in auth_required_hosts:
        missing_auth = [name for name in ("OWQ_SMTP_USER", "OWQ_SMTP_PASSWORD") if not _env(environ, name)]
        if missing_auth:
            return False, f"{host} 需要 SMTP 认证: 缺少 " + ", ".join(missing_auth)
    return True, "SMTP 已配置"


def selected_provider(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    requested = _env(environ, "OWQ_EMAIL_PROVIDER").lower()
    cf_ok, cf_detail = cloudflare_ready(environ)
    smtp_ok, smtp_detail = smtp_ready(environ)
    if requested in {"cloudflare", "cf"}:
        return ("cloudflare", cf_detail) if cf_ok else ("", cf_detail)
    if requested == "smtp":
        return ("smtp", smtp_detail) if smtp_ok else ("", smtp_detail)
    if requested:
        return "", "OWQ_EMAIL_PROVIDER 仅支持 cloudflare 或 smtp"
    if cf_ok:
        return "cloudflare", cf_detail
    if smtp_ok:
        return "smtp", smtp_detail
    return "", "未配置真实发信服务"


def status(environ: Mapping[str, str] | None = None) -> dict[str, str | bool]:
    provider, detail = selected_provider(environ)
    cf_ok, cf_detail = cloudflare_ready(environ)
    smtp_ok, smtp_detail = smtp_ready(environ)
    return {
        "provider": provider,
        "configured": bool(provider),
        "detail": detail,
        "cloudflare_ok": cf_ok,
        "cloudflare_detail": cf_detail,
        "smtp_ok": smtp_ok,
        "smtp_detail": smtp_detail,
    }

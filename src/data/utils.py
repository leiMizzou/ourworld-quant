"""通用工具:股票代码规范化、重试、日志。"""
from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Callable, TypeVar

from . import config

T = TypeVar("T")


def get_logger(name: str = "owq.data") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger


log = get_logger()


def normalize_code(code: str) -> str:
    """把各种写法的 A 股代码统一成 '600000.SH' / '000001.SZ' / '430047.BJ'。

    规则(够用即可):6/9 开头 → 上交所;0/2/3 开头 → 深交所;4/8 开头 → 北交所。
    已带后缀的(600000.SH / sh600000 / SH600000)也能识别。
    """
    c = str(code).strip().upper().replace(" ", "")
    # 已是标准后缀
    if "." in c:
        num, suf = c.split(".", 1)
        suf = {"XSHG": "SH", "XSHE": "SZ"}.get(suf, suf)
        return f"{num.zfill(6)}.{suf}"
    # 前缀写法 sh600000 / SZ000001
    if c[:2] in ("SH", "SZ", "BJ"):
        return f"{c[2:].zfill(6)}.{c[:2]}"
    num = c.zfill(6)
    head = num[0]
    if head in ("6", "9"):
        suf = "SH"
    elif head in ("0", "2", "3"):
        suf = "SZ"
    else:  # 4, 8
        suf = "BJ"
    return f"{num}.{suf}"


def bare_code(code: str) -> str:
    """取 6 位数字部分(AkShare 等很多接口只要数字)。"""
    return normalize_code(code).split(".")[0]


def to_baostock_code(code: str) -> str:
    """'000001.SZ' -> 'sz.000001'(BaoStock 格式)。"""
    num, suf = normalize_code(code).split(".")
    return f"{suf.lower()}.{num}"


def to_tushare_code(code: str) -> str:
    """'000001.SZ' -> '000001.SZ'(Tushare 即标准后缀,北交所为 .BJ)。"""
    return normalize_code(code)


def retry(fn: Callable[..., T]) -> Callable[..., T]:
    """简单指数退避重试,用于包裹会触发限流/抖动的网络调用。"""

    @wraps(fn)
    def wrapper(*args, **kwargs) -> T:
        last_exc: Exception | None = None
        for attempt in range(1, config.MAX_RETRY + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
                last_exc = exc
                wait = config.RETRY_BACKOFF ** attempt
                log.warning("%s 第 %d 次失败: %s — %.1fs 后重试", fn.__name__, attempt, exc, wait)
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    return wrapper


def polite_sleep() -> None:
    """每次请求后礼貌 sleep,降低被限流概率。"""
    time.sleep(config.REQUEST_SLEEP)

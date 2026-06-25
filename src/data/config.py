"""全局配置。敏感信息(如 Tushare token)只从环境变量读,绝不写进代码。"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 路径 ----
# 数据默认放在仓库根的 data/ 下(已在 .gitignore 中忽略,不会误传行情数据)
_REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("OWQ_DATA_DIR", _REPO_ROOT / "data"))
DB_PATH = Path(os.getenv("OWQ_DB_PATH", DATA_DIR / "market.duckdb"))
PARQUET_DIR = DATA_DIR / "parquet"

# ---- 凭据 ----
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "").strip()

# ---- 数据默认 ----
DEFAULT_START = os.getenv("OWQ_START", "20180101")
# 后复权(hfq):历史价格不随新分红重算,回测无前视偏差 —— 作为落库默认。
# 需要前复权/不复权时,按 adjust 参数另取。
DEFAULT_ADJUST = os.getenv("OWQ_ADJUST", "hfq")

# ---- 限流 / 重试(免费源都有频率限制,务必保留)----
REQUEST_SLEEP = float(os.getenv("OWQ_SLEEP", "0.4"))   # 每次请求后 sleep 秒数
MAX_RETRY = int(os.getenv("OWQ_RETRY", "3"))
RETRY_BACKOFF = float(os.getenv("OWQ_BACKOFF", "1.6"))  # 指数退避底数

# ---- 规范 ----
# 落库统一口径:成交量 = 股,成交额 = 元,价格 = 元
VOLUME_UNIT = "shares"
AMOUNT_UNIT = "yuan"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)

"""数据源统一接口。所有源都把数据归一到同一套列与单位。"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

# 日线标准列(落库口径:volume=股,amount=元,价格=元)
BAR_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount", "adjust", "source"]
# 股票列表标准列
STOCK_COLUMNS = ["code", "name", "list_date", "delist_date", "status", "source"]

# 复权口径
ADJUST_NONE = "none"
ADJUST_QFQ = "qfq"   # 前复权
ADJUST_HFQ = "hfq"   # 后复权(回测推荐)
VALID_ADJUST = {ADJUST_NONE, ADJUST_QFQ, ADJUST_HFQ}


class DataSource(ABC):
    """每个源实现取『股票列表』和『日线』。返回值列名/单位必须符合上面的标准。"""

    name: str = "base"

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """返回 STOCK_COLUMNS。status: 'L'=上市, 'D'=退市, 'P'=暂停。

        注意:为缓解幸存者偏差,应尽量包含已退市股票。
        """

    @abstractmethod
    def get_daily_bars(
        self, code: str, start: str, end: str | None = None, adjust: str = ADJUST_HFQ
    ) -> pd.DataFrame:
        """返回 BAR_COLUMNS 的标准化日线。start/end 形如 'YYYYMMDD'。"""

    def close(self) -> None:  # 可选:有的源需要登出(BaoStock)
        pass

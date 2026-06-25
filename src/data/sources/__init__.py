"""数据源工厂。"""
from __future__ import annotations

from .base import (
    ADJUST_HFQ, ADJUST_NONE, ADJUST_QFQ, BAR_COLUMNS, STOCK_COLUMNS, DataSource,
)

_REGISTRY = {
    "akshare": "AkShareSource",
    "baostock": "BaoStockSource",
    "tushare": "TushareSource",
}


def get_source(name: str) -> DataSource:
    """按名字构造一个数据源实例(延迟导入,缺包/缺 token 时只在用到的源上报错)。"""
    name = name.lower()
    if name not in _REGISTRY:
        raise ValueError(f"未知数据源 {name!r},可选: {list(_REGISTRY)}")
    if name == "akshare":
        from .akshare_source import AkShareSource
        return AkShareSource()
    if name == "baostock":
        from .baostock_source import BaoStockSource
        return BaoStockSource()
    from .tushare_source import TushareSource
    return TushareSource()


__all__ = [
    "get_source", "DataSource", "BAR_COLUMNS", "STOCK_COLUMNS",
    "ADJUST_HFQ", "ADJUST_QFQ", "ADJUST_NONE",
]

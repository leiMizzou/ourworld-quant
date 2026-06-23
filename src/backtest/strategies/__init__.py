"""示例策略:生成『调仓日 × 股票』的目标权重表,交给 engine 执行。"""
from .cross_sectional import cross_sectional_weights, rebalance_dates

__all__ = ["cross_sectional_weights", "rebalance_dates"]

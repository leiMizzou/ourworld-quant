"""OurWorlds Quant Lab — 研究层。

把三层接成一条闭环:数据(data) → 因子(factors) → 组合(此处) → 回测(backtest)。

multifactor:多因子标准化合成 → 月度 top-N 目标权重 → 回测引擎 → 因子IC + 组合净值。
"""

__version__ = "0.1.0"

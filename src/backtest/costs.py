"""A 股交易成本模型。默认值贴近 2024+ 现实,可按你的券商调整。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    commission_rate: float = 0.00025   # 佣金 万2.5(双边),按券商可低至万1.x
    min_commission: float = 5.0        # 单笔最低 5 元
    stamp_rate: float = 0.0005         # 印花税 0.05%(2023-08 起,仅卖出单边)
    transfer_rate: float = 0.00001     # 过户费 万0.1(双边)
    slippage_bps: float = 5.0          # 滑点(单边,基点)。高换手策略对此极敏感

    def buy_cost(self, amount: float) -> float:
        """买入费用(佣金 + 过户费)。amount 为成交金额(元)。"""
        return max(amount * self.commission_rate, self.min_commission) + amount * self.transfer_rate

    def sell_cost(self, amount: float) -> float:
        """卖出费用(佣金 + 印花税 + 过户费)。"""
        return (max(amount * self.commission_rate, self.min_commission)
                + amount * self.stamp_rate + amount * self.transfer_rate)

    def fill_price(self, price: float, side: str) -> float:
        """加滑点后的成交价。买入上滑、卖出下滑。"""
        s = self.slippage_bps / 1e4
        return price * (1 + s) if side == "buy" else price * (1 - s)

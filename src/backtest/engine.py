"""事件驱动回测引擎(组合级,日频)。

核心约束(A 股):
- **T+1**:权重在某日收盘决定,**次一交易日开盘**成交;不卖出当日买入。
- **涨跌停**:开盘价 ≥ 昨收×(1+阈值) 视为接近涨停→**买不进**;≤ 昨收×(1-阈值)→**卖不出**。
  阈值按板块近似:创业板(300/301)/科创板(688/689)=20%,其余=10%。**ST 的 5% 未识别(近似)**。
- **停牌**:当日无开盘价→不可交易,持仓按收盘估值。
- **成本/滑点/整手**:见 costs.CostModel,100 股为单位。

用法见 strategies/ 与 run.py。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .costs import CostModel


def default_limit(code: str) -> float:
    """按代码前缀给涨跌停阈值(近似)。"""
    num = str(code).split(".")[0]
    if num[:3] in ("300", "301") or num[:3] in ("688", "689"):
        return 0.20
    if num.startswith(("4", "8")):  # 北交所 30%
        return 0.30
    return 0.10


def run_backtest(
    panel: pd.DataFrame,
    weights: pd.DataFrame,
    cost_model: CostModel | None = None,
    init_cash: float = 1_000_000.0,
    lot: int = 100,
    cash_buffer: float = 0.005,
    get_limit=default_limit,
) -> dict:
    """
    panel:   长表,列 [date, code, open, close]。
    weights: index=调仓日(交易日),columns=code,值=目标权重(>=0,行和<=1)。
             权重在该日**收盘后**决定,**次一交易日开盘**执行(T+1,无前视)。
    返回 dict:equity(Series)、trades(DataFrame)、metrics(dict)、positions_end(Series)。
    """
    from .metrics import compute_metrics

    cost_model = cost_model or CostModel()
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    opens = panel.pivot(index="date", columns="code", values="open").sort_index()
    closes = panel.pivot(index="date", columns="code", values="close").sort_index()
    prev_close = closes.shift(1)

    dates = list(closes.index)
    codes = list(closes.columns)
    thr = pd.Series({c: get_limit(c) for c in codes})
    weights = weights.reindex(columns=codes).fillna(0.0)
    rebal = set(pd.to_datetime(weights.index))

    cash = float(init_cash)
    holdings = pd.Series(0.0, index=codes)
    equity_curve: dict[pd.Timestamp, float] = {}
    trades: list[dict] = []
    pending: pd.Series | None = None
    traded_notional = 0.0

    for i, d in enumerate(dates):
        op, cl, pc = opens.loc[d], closes.loc[d], prev_close.loc[d]

        # ---- 1) 次日开盘执行昨日收盘决定的目标(T+1)----
        if pending is not None:
            tradable = op.notna()                       # 停牌(无开盘)不可交易
            up_block = op >= pc * (1 + thr) - 1e-6       # 近涨停买不进
            down_block = op <= pc * (1 - thr) + 1e-6     # 近跌停卖不出
            # 以开盘市值为基准定目标股数(持仓按开盘价,停牌按昨收)
            mark = op.where(tradable, pc)
            eq_open = cash + float((holdings * mark).fillna(0).sum())
            tgt_val = pending * eq_open
            with np.errstate(invalid="ignore", divide="ignore"):
                tgt_shares = np.floor(tgt_val / (op * lot)) * lot
            tgt_shares = tgt_shares.where(tradable, holdings)  # 不可交易则维持原仓

            delta = tgt_shares - holdings

            # 先卖(回笼现金):跌停/停牌不可卖
            for c in codes:
                dq = delta[c]
                if dq < 0 and op.notna()[c] and not down_block[c]:
                    qty = -dq
                    px = cost_model.fill_price(op[c], "sell")
                    amt = qty * px
                    cash += amt - cost_model.sell_cost(amt)
                    holdings[c] += dq
                    traded_notional += amt
                    trades.append({"date": d, "code": c, "side": "sell", "qty": qty, "price": px})

            # 再买:涨停/停牌不可买;现金不足则按比例缩量
            buy_codes = [c for c in codes if delta[c] > 0 and op.notna()[c] and not up_block[c]]
            if buy_codes:
                want = {c: delta[c] for c in buy_codes}
                est = sum(want[c] * cost_model.fill_price(op[c], "buy") for c in buy_codes)
                est *= (1 + cost_model.commission_rate + cost_model.transfer_rate)
                budget = cash * (1 - cash_buffer)
                scale = min(1.0, budget / est) if est > 0 else 0.0
                for c in buy_codes:
                    qty = np.floor(want[c] * scale / lot) * lot
                    if qty <= 0:
                        continue
                    px = cost_model.fill_price(op[c], "buy")
                    amt = qty * px
                    fee = cost_model.buy_cost(amt)
                    if amt + fee > cash:
                        continue
                    cash -= amt + fee
                    holdings[c] += qty
                    traded_notional += amt
                    trades.append({"date": d, "code": c, "side": "buy", "qty": qty, "price": px})
            pending = None

        # ---- 2) 收盘盯市 ----
        equity_curve[d] = cash + float((holdings * cl).fillna(0).sum())

        # ---- 3) 调仓日:用截至今日收盘的信息定目标,挂到次日执行 ----
        if d in rebal and i < len(dates) - 1:
            pending = weights.loc[d]

    equity = pd.Series(equity_curve).sort_index()
    mean_eq = equity.mean()
    years = max(len(equity) / 252, 1e-9)
    annual_turnover = (traded_notional / mean_eq / years) if mean_eq > 0 else 0.0

    return {
        "equity": equity,
        "trades": pd.DataFrame(trades),
        "positions_end": holdings[holdings != 0],
        "metrics": compute_metrics(equity, turnover=annual_turnover),
        "final_cash": cash,
    }

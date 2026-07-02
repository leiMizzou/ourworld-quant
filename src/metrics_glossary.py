"""Single source of truth for quant-metric explanations — the "explain every number" layer.

Consumed by the web app (dashboard tooltips + ``GET /api/glossary``). From Phase 1 the
markdown report writer (``src/research/real_data_report.py``) and the CLI
(``src/research/multifactor.py``) read from here too, and a drift test asserts the
consumers stay in sync with these keys.

Keep this module DEPENDENCY-FREE so every layer can import it without cycles
(``from ..metrics_glossary import METRIC_GLOSSARY``).

Editorial rule — bands are deliberately CONSERVATIVE and frame a good-looking number as a
reason for suspicion, not a trophy. The platform's own backtests still carry known biases
(survivorship / lookahead / 复权口径), so a high number is more likely a data flaw than
real alpha. When the Phase-1 survivorship fix drops everyone's Sharpe, these bands should
read as *confirmed*, never contradicted.
"""
from __future__ import annotations

# key -> {term, short, formula, unit, band}
#   term:    display name shown to the user
#   short:   one-line plain-language definition
#   formula: how it is computed (plain, not LaTeX)
#   unit:    display unit ("%", "倍", "" ...)
#   band:    conservative "what good / red-flag looks like" guidance
METRIC_GLOSSARY: dict[str, dict[str, str]] = {
    "return_pct": {
        "term": "收益率",
        "short": "账户当前总资产相对初始本金的盈亏百分比。",
        "formula": "(总资产 − 初始本金) ÷ 初始本金 × 100%",
        "unit": "%",
        "band": "模拟盘里的短期高收益大多是运气或选股偏差,别据此加仓——看长期、看回撤、看是否可复现。",
    },
    "equity": {
        "term": "总资产",
        "short": "现金加上所有持仓按现价折算的市值。",
        "formula": "现金 + Σ(持仓数量 × 现价)",
        "unit": "元",
        "band": "总资产只是某一时刻的快照;真正重要的是这条曲线的形状和回撤,而不是某天的高点。",
    },
    "cash": {
        "term": "现金",
        "short": "账户里尚未买入持仓的可用资金。",
        "formula": "初始本金 − 买入成本 + 卖出回款 − 费用",
        "unit": "元",
        "band": "现金过低意味着仓位过满、抗风险能力差;留足现金是风险控制的一部分,不是收益的浪费。",
    },
    "sharpe": {
        "term": "夏普比率",
        "short": "每承担一单位波动,策略获得的超额收益。衡量收益的'性价比'。",
        "formula": "(年化收益 − 无风险利率) ÷ 年化波动率",
        "unit": "",
        "band": "夏普 > 1 看起来很美,但小样本或有偏回测最容易刷出高夏普——先怀疑数据(幸存者/前视/复权),再相信策略。",
    },
    "max_drawdown": {
        "term": "最大回撤",
        "short": "净值从历史最高点跌到随后最低点的最大跌幅。衡量最坏情况下你要忍受多大亏损。",
        "formula": "min(净值 ÷ 净值历史最高 − 1)",
        "unit": "%",
        "band": "回撤比收益更诚实。−30% 意味着真金白银时多数人会割肉离场;能不能拿得住,取决于你能接受多大回撤。",
    },
    "total_return": {
        "term": "总收益率",
        "short": "整段回测从开始到结束的累计盈亏百分比(不年化)。",
        "formula": "期末净值 ÷ 期初净值 − 1",
        "unit": "%",
        "band": "总收益不区分时间长短,3 年涨 30% 和 1 年涨 30% 完全不同;比较时务必看年化和回撤。",
    },
    "cagr": {
        "term": "年化收益率",
        "short": "把整段回测的总收益折算成'每年平均增长'的复利速度。",
        "formula": "(期末净值 ÷ 期初净值) ^ (1 ÷ 年数) − 1",
        "unit": "%",
        "band": "年化对样本区间极度敏感:换个起止日期可能天差地别。短回测里的高年化几乎没有参考价值。",
    },
    "ic": {
        "term": "IC(信息系数)",
        "short": "因子值与未来收益的截面相关性,衡量一个因子'预测力'的强弱。",
        "formula": "每个调仓日 corr(因子排名, 下期收益排名) 的均值",
        "unit": "",
        "band": "A 股月频因子 |IC| 能稳定在 0.03~0.05 已属不错;若看到很高的 IC,八成是前视或全样本拟合带来的假象。",
    },
    "icir": {
        "term": "ICIR(IC 信息比率)",
        "short": "IC 的稳定性:平均 IC 除以 IC 的波动。比单看 IC 更能反映因子是否'持续'有效。",
        "formula": "IC 均值 ÷ IC 标准差",
        "unit": "",
        "band": "稳定为正比偶尔很高更值钱。样本期短时 ICIR 不可信;别用一段牛市的 ICIR 推断未来。",
    },
    "turnover": {
        "term": "换手率",
        "short": "每次调仓时组合变动的比例,衡量交易频繁程度。",
        "formula": "每次调仓 Σ|目标权重 − 当前权重| ÷ 2 的均值",
        "unit": "%",
        "band": "换手越高,手续费/滑点/冲击成本吃掉的收益越多。回测里漂亮的高换手策略,实盘常被成本磨平。",
    },
}


# Every entry must define these fields; the drift test enforces it across consumers.
GLOSSARY_FIELDS = ("term", "short", "formula", "unit", "band")


# key -> {term, short, band}
#   Concept/jargon terms a beginner meets in the UI that are NOT numbers with a formula.
#   Kept SEPARATE from METRIC_GLOSSARY so the metric drift-test and the report/CLI consumers
#   stay untouched. Surfaced the same way: ``/api/glossary`` merges both, app.js renders the
#   ``band`` line if present, and ``term_label()`` wraps a label into a tappable tooltip.
TERM_GLOSSARY: dict[str, dict[str, str]] = {
    "instrument": {
        "term": "标的",
        "short": "可以买卖的对象——一只股票或一只 ETF 基金。",
        "band": "代码后缀 .SH=上交所、.SZ=深交所;ETF 是一篮子股票打包成的基金,适合新手分散风险。",
    },
    "available_qty": {
        "term": "可卖",
        "short": "当前真正能卖出的数量。",
        "band": "A 股 T+1:当天买入的部分要到下一交易日才计入可卖,所以买入当天这里常是 0,不是出错。",
    },
    "avg_cost": {
        "term": "成本",
        "short": "你持有这只标的的平均买入价(均价)。",
        "band": "现价高于成本是浮盈,低于是浮亏;加仓会重新摊薄或抬高这个均价。",
    },
    "fee": {
        "term": "费用",
        "short": "一笔交易的交易成本。",
        "band": "买入含佣金+过户费;卖出再多一道印花税。交易越频繁,费用越会慢慢磨掉收益。",
    },
    "pnl": {
        "term": "盈亏",
        "short": "这只持仓当前的浮动盈亏(还没卖出兑现)。",
        "band": "≈(现价 − 成本)× 数量;没卖出前都只是账面数字,会随行情上下波动。",
    },
    "market_value": {
        "term": "市值",
        "short": "持仓按现价折算的价值。",
        "band": "= 现价 × 持有数量,随行情波动;总资产 = 现金 + 所有持仓市值。",
    },
    "adjust": {
        "term": "复权",
        "short": "对历史价格做分红/拆股调整的口径,让价格在分红除权时不出现假跳空。",
        "band": "不复权(none)最接近真实可成交价,模拟成交用它;后复权(hfq)保持收益连续,研究回测用它。不确定就用 none。",
    },
    "backtest": {
        "term": "回测",
        "short": "用历史行情模拟『如果当时按这个策略交易,结果会怎样』。",
        "band": "回测好≠未来赚钱:最容易被幸存者偏差、前视、过拟合骗到。看到漂亮数字,先怀疑数据,再相信策略。",
    },
    "factor": {
        "term": "因子",
        "short": "一个能给股票打分、用来选股的可量化特征(如反转、动量、波动率)。",
        "band": "单个因子预测力很弱(月频 IC 能稳定在 0.03~0.05 已算不错);因子会失效,需要持续验证。",
    },
    "survivorship": {
        "term": "幸存者偏差",
        "short": "只用『活到今天』的股票做回测,漏掉了已退市的失败者,从而高估收益。",
        "band": "正确做法是把当时在市的退市股也纳入票池。不含退市股的回测数字通常虚高、不可信。",
    },
    "lookahead": {
        "term": "前视(未来函数)",
        "short": "回测里不小心用到了『当时还不知道』的未来信息,导致成绩虚高。",
        "band": "例如用全样本统计量、或用收盘后才有的数据在收盘前下单。一旦有前视,回测就不可信。",
    },
    "price_limit": {
        "term": "涨跌停",
        "short": "A 股单日涨跌幅有上限(主板 ±10%、创业板/科创板 ±20%、ST ±5%)。",
        "band": "涨停常买不进、跌停常卖不出;回测若忽略涨跌停会高估可成交性。",
    },
    "t_plus_1": {
        "term": "T+1",
        "short": "A 股的交易规则:当天买入的股票,要到下一个交易日才能卖出。",
        "band": "所以买入当天『可卖』是 0;在模拟盘点『进入下一交易日』即可解锁。",
    },
    "tokens": {
        "term": "tokens(令牌)",
        "short": "大模型计量用量的单位,大致 1 个汉字≈1~2 tokens。",
        "band": "AI 调用按 tokens 计费、也按 tokens 限额;本站每位用户每天有用量上限,超出后次日恢复。",
    },
    "growth_score": {
        "term": "成长分",
        "short": "榜单默认排名依据:复盘质量(50) + 交易纪律(30) + 稳健收益(20),满分 100。",
        "band": "短期收益率排名主要反映运气和风险敞口,所以本站不按收益排名;完整三问复盘每条 +5(每周最多计 5 条),计分规则全部公开。",
    },
    "plan_ratio": {
        "term": "计划内交易",
        "short": "先写演练计划再下单的订单占比——衡量『按计划交易』的纪律。",
        "band": "占比越高说明越少冲动下单;累计满 4 笔订单后才开始计算,避免样本太小失真。",
    },
}


def _lookup(key: str) -> dict | None:
    """Resolve a key against either glossary (metrics first, then concept terms)."""
    return METRIC_GLOSSARY.get(key) or TERM_GLOSSARY.get(key)


def glossary_payload() -> dict:
    """Merged view served by ``GET /api/glossary`` so tooltips cover metrics AND concept terms."""
    return {**METRIC_GLOSSARY, **TERM_GLOSSARY}


def tooltip_text(key: str) -> str:
    """Plain-language fallback string for a term (used as the no-JS ``title`` attribute)."""
    info = _lookup(key)
    if not info:
        return ""
    band = info.get("band", "")
    return f"{info['short']} {band}".strip()


def glossary_markdown(keys=None) -> str:
    """Render selected metrics as a markdown definition list (shared by the report and CLI)."""
    selected = list(keys) if keys else list(METRIC_GLOSSARY)
    out: list[str] = []
    for key in selected:
        info = METRIC_GLOSSARY.get(key)
        if not info:
            continue
        unit = f"（{info['unit']}）" if info["unit"] else ""
        out.append(f"- **{info['term']}**{unit} — {info['short']}")
        out.append(f"  - 计算: `{info['formula']}`")
        out.append(f"  - 判读: {info['band']}")
    return "\n".join(out)

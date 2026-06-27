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


def tooltip_text(key: str) -> str:
    """Plain-language fallback string for a metric (used as the no-JS ``title`` attribute)."""
    info = METRIC_GLOSSARY.get(key)
    if not info:
        return ""
    return f"{info['short']} {info['band']}"


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

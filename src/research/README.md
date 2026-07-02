# 研究层 · src/research

> ⚠️ **回测数字可信度（先读这条）**：复权口径用**后复权（hfq）**，研究股票池已是代表性、多板块覆盖
> （现 **440 只 hfq 日线，2023→2026**），并已**纳入窗口内退市股**（45 只经 akshare 补齐，hfq 池含 48 只退市；
> 主回测对退市持仓按最后收盘价强制平仓）——所以窗口内的幸存者偏差已基本处理。`none`（不复权）全市场池
> 上的**幸存者偏差实测**仍保留作教学：只测存活股会把总收益高估约 **32 个百分点**、夏普高估约 **0.6**
> （见 `/research` 与下方报告）。**残留**：极老/无行情的退市名单仍缺、preview 用 none 口径、样本仅约 3.5 年。
> **结论**：可信度已显著提升，但仍是**短窗口演示**——绝对收益数字别当真实业绩对外引用。

把三层接成一条**研究闭环**:数据 → 因子 → 组合 → 回测。

```
data.load_bars → factors.compute+standardize → composite_score(多因子合成)
              → to_target_weights(月度 top-N) → backtest.run_backtest → 净值 + 指标
```

## multifactor.py

- `composite_score(panels, specs, ic_weight=False)` — 把多个标准化因子按方向(±1)与权重合成为"总分"(越大越买)。默认**等权**(无前视);`ic_weight=True` 用全样本 \|IC\| 加权(**有前视,仅演示**,真实应改滚动 IC)。
- `to_target_weights(composite, close, rebal_dates, top_n)` — 每个调仓日取总分最高 top-N 等权。
- `run(codes, start, ...)` — 从 DuckDB 一键跑完整闭环,返回因子权重 + 组合指标 + 净值。
- CLI `python -m src.research.multifactor`

默认因子组合:**非流动性溢价(amihud,20)+ 低波动(volatility,20,反向)**。

选择依据(2026-07,数据 2018→2026 hfq 440 只):在前 70% 调仓期(2018→2023)上比较 5 个候选组合,
amihud+低波动 训练窗最优(Sharpe 0.76、回撤最小、换手最低),再用后 30%(2024→2026)一次性验证
(Sharpe 0.74)——权重决策只看训练窗,避免"全样本挑最好"的过拟合。旧默认 反转+低波动 在两个窗口
都亏损(训练 −31%/测试 −31%),已废弃。两点诚实提醒:(1) amihud 买的是低流动性股票,纸面收益受
真实容量约束,教学/模拟用途可接受;(2) 2024→2026 小盘牛市里该组合跑输全池等权基准(报告里的
基准行),长多因子组合在普涨行情跑输等权是常态,不代表因子失效。

## 用法

```bash
# 先取数(见 src/data),再跑组合:
python -m src.research.multifactor --top 30 --freq M --start 20200101
python -m src.research.multifactor --top 30 --ic-weight        # |IC| 加权(演示)
python -m src.research.multifactor --top 30 --save data/equity.csv
```

## 在代码里

```python
from src.research.multifactor import run
res = run(start="20200101", top_n=30)
print(res["composite_weights"], res["metrics"])
```

## ⚠️ 注意

- 这是把各层**接通**的脚手架,不是"能赚钱的策略"。判断有效性要:对比单因子 vs 组合、看回撤/换手/成本、**切样本内外**、考虑因子拥挤与容量。
- `ic_weight` 的全样本 IC 有前视,实盘前务必改成滚动窗口。
- 后续:滚动 IC 加权、行业/市值中性化(需数据层补市值与行业)、风险模型与组合优化。

> 本项目仅为技术研究,不构成投资建议。

---

## 🚧 研究数据重建清单(P0,做任何研究结论前先完成)

**进度(实测 `data/market.duckdb`)**:基本完成——`hfq` 后复权 **440 只**(含 **48 只退市**)、`none` 约 **618 只**,
覆盖 002/300/600/688/603/301/920/000 等多板块;报告已按 hfq 重生成,回测对退市持仓强制平仓。
**退市股覆盖已补齐(窗口内)**:与 2023→2026 回测窗口相关的 45 只退市股(窗口内有行情、原先缺 hfq)已用 **akshare**
补齐 hfq 日线(Tushare 的 `adj_factor` 接口被限速到 1 次/小时,不可行;akshare 直接返回 hfq、无此限制)。
**残留**:`stock_basic` 里 100 多只 2016 年前的老退市在窗口外、无行情,取不到也与本窗口无关;`stock_basic.delist_date`
全为空,做严格 point-in-time 需先从源刷新退市日期。`none` 全市场池(含 49 只退市)的实测仍显示只测存活股高估总收益约 32pp。

代码侧已加好护栏:`real_data_report` 默认 `--adjust hfq`,显式 `--adjust none` 会被拒绝
(除非加 `--allow-unadjusted` 调试);hfq 标的数 < 30 会响亮告警;同步脚本与 env 模板
的 `OWQ_REPORT_ADJUST` 默认已改为 `hfq`。**剩下需要你带 `TUSHARE_TOKEN` 跑的部分:**

- [x] **1. 定义代表性股票池**:已完成——hfq 池覆盖 002/300/600/688/603/301/920/000 等多板块,
      不再是"前 300 个 000 开头"。
- [x] **2. 纳入退市股(窗口内已补齐)**:与回测窗口(2023→2026)相关的 **45 只**退市股(窗口内有行情、原先缺 hfq)
      已补齐;hfq 池现含 **48 只退市**。`Tushare` 的 `adj_factor` 接口被限速到 **1 次/小时**、44 小时不可行,
      改用 **akshare**(直接返回 hfq、无该限速)拉取:
      `python -m src.data.cli daily --source akshare --adjust hfq --codes-csv <delisted.csv>`(几分钟跑完)。
      其余 100 多只 2016 年前老退市在窗口外、无行情,取不到也与本窗口无关。`stock_basic.delist_date` 仍全空,做严格
      point-in-time 需先刷新退市日期。
- [x] **3. 同步 `hfq` 日线**:在市股 + 窗口内退市股均已完成(共 440 只,2023→2026)。
- [ ] **4. 校验**:`src/data` 的双源互备(akshare/baostock 校验 tushare),检查除权除息跳空、
      停复牌、缺失;确认 `daily_bars` 中 `hfq` 标的数达到代表性规模(≥300,现 440 ✓)。
- [x] **5. 重生成报告**:已用 `python -m src.research.real_data_report --adjust hfq`(主报告 + predictions)和
      `--preview-only`(/research 工件)在补齐退市股**之后**重生成;回测对退市持仓强制平仓(6 笔)。
- [ ] **6.(后续)补市值/行业**,接通 `factors/preprocess.neutralize`(当前是死代码),做市值/行业中性化。

> 现状:复权、代表性股票池、窗口内退市股均已就绪,报告已在补齐退市股后重生成、回测含退市强制平仓并自带偏差量化。
> **仍是约 3.5 年的短窗口演示**——绝对收益数字别当作真实业绩写进简历或对外材料。

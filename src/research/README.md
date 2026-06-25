# 研究层 · src/research

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

默认因子组合:**短期反转(reversal,20)+ 低波动(volatility,20,反向)**,都是 A 股有文献支持的效应。

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

**为什么**:当前 `data/market.duckdb` 只有 3 只 `hfq`(后复权)、302 只 `none`(不复权),
且这 302 只 99% 是单一 `000` 板块、**不含任何退市股**。历史 `reports/real-data-report.md`
是用**不复权价 + 幸存者偏差**股票池跑出来的,IC/回归/回测**全部无效**(这也是它出现
-43% 回撤的原因)。在重建数据前,任何回测/因子结论都不可信、不可对外展示。

代码侧已加好护栏:`real_data_report` 默认 `--adjust hfq`,显式 `--adjust none` 会被拒绝
(除非加 `--allow-unadjusted` 调试);hfq 标的数 < 30 会响亮告警;同步脚本与 env 模板
的 `OWQ_REPORT_ADJUST` 默认已改为 `hfq`。**剩下需要你带 `TUSHARE_TOKEN` 跑的部分:**

- [ ] **1. 定义代表性股票池**:用指数成分(如沪深300/中证500/中证全指)的**历史成分**,
      或全市场按市值/流动性筛选,而不是"前 300 个 000 开头"。
- [ ] **2. 纳入退市股**:`stock_basic` 里已有 146 只 `D`(退市)。按**当时在市**的 point-in-time
      口径把它们一并取数,消除幸存者偏差。
- [ ] **3. 同步 `hfq` 日线**:配置 `TUSHARE_TOKEN` 后,
      `python -m src.data.cli daily --source tushare --adjust hfq --codes <universe> --start 20180101`
      (或用 `deploy/sync-market-public.sh`,其 `OWQ_ADJUST=hfq` 已用于研究取数)。
- [ ] **4. 校验**:`src/data` 的双源互备(akshare/baostock 校验 tushare),检查除权除息跳空、
      停复牌、缺失;确认 `daily_bars` 中 `hfq` 标的数达到代表性规模(≥300)。
- [ ] **5. 重生成报告**:`python -m src.research.real_data_report --adjust hfq`(不再触发告警),
      并**切样本内外**重看 IC/ICIR/分层/多空与回测。
- [ ] **6.(后续)补市值/行业**,接通 `factors/preprocess.neutralize`(当前是死代码),做市值/行业中性化。

> 完成 1–5 前,请不要在站点、简历或对外材料里引用任何回测数字。

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

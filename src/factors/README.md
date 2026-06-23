# 因子层 · src/factors

阶段 2 的产出:**单因子检验流水线**——因子计算 → 预处理 → 评估(IC/ICIR/分层/多空)。

## 流程

```
日线(DuckDB) → 计算因子 → 截面去极值+标准化(可选中性化) → 取调仓日采样
            → 前向收益对齐 → RankIC 序列 / 分层回测 / 多空组合 → 报告
```

**无前视**:因子在调仓日 d 用截至 d 的数据算出,前向收益是 d→下一个调仓日。

## 模块

- `factors.py` — 动量 / 反转 / 波动率 / Amihud 非流动性 / 均线乖离(均可从 OHLCV 算)
- `preprocess.py` — MAD 去极值、截面 z-score、OLS 中性化(对市值/行业暴露)
- `evaluate.py` — `evaluate_factor()`:RankIC、ICIR、t 值、IC>0 胜率、分层收益、多空、单调性
- `run.py` — 命令行入口

## 用法

```bash
# 先取数(见 src/data),例如 300 只:
python -m src.data.cli stocks && python -m src.data.cli daily --limit 300 --start 20200101

# 检验因子:
python -m src.factors.run --factor reversal --window 20 --q 5
python -m src.factors.run --factor amihud  --window 20
python -m src.factors.run --factor volatility --window 60
```

## 怎么读结果

| 指标 | 含义 | 经验阈值 |
|---|---|---|
| `ic_mean` | RankIC 均值(因子与未来收益的秩相关) | \|IC\| ≳ 0.03 有意义 |
| `icir` | IC 均值/IC 标准差(稳定性) | ≳ 0.3 较好 |
| `t_stat` | ICIR×√N | **\|t\|>2** 才算显著 |
| `monotonicity` | 分层收益与分位的秩相关 | 接近 ±1 = 单调 |
| `long_short_ann` | 多空组合年化 | 越高越好,但看回撤与换手 |

## ⚠️ 注意

- 单因子显著 ≠ 能赚钱:还要看**换手/成本**(交给回测层)、**拥挤度**、**样本外**表现。
- 中性化需要市值/行业数据(数据层后续补充);现仅做去极值+标准化。
- 默认全样本,**务必自己切样本内外**,否则就是过拟合自欺。

> 本项目仅为技术研究,不构成投资建议。

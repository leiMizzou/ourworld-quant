# 回测层 · src/backtest

阶段 1 的产出:一个**显式建模 A 股摩擦**的事件驱动回测引擎,以及一个横截面排序示例策略。

## 为什么自己写引擎

回测最大的坑不在"跑得快",而在"是否骗自己"。这个引擎把最容易作弊的环节显式化:

| 摩擦 | 处理 |
|---|---|
| **T+1** | 信号在 t 日收盘产生,**t+1 日开盘**成交;当日买入不可当日卖出 |
| **涨跌停** | 开盘≈涨停**买不进**、≈跌停**卖不出**(创业板/科创板 20%、北交所 30%、其余 10%) |
| **停牌** | 当日无开盘价→不可交易,持仓按收盘估值 |
| **成本** | 佣金(万2.5/最低5元)+ 印花税(0.05% 卖出)+ 过户费 + 滑点 |
| **整手** | 100 股为单位,买入受现金约束(不足按比例缩量) |

> 这些就是回测与实盘差距的主要来源。先把它们建对,收益数字才有意义。

## 模块

- `costs.py` — `CostModel`,可调佣金/印花税/滑点
- `metrics.py` — 年化、夏普、最大回撤、Calmar、胜率、年化换手
- `engine.py` — `run_backtest(panel, weights, ...)`,事件驱动逐日撮合
- `strategies/cross_sectional.py` — 按过去 N 日收益排序、月度等权 top-N(反转/动量)
- `run.py` — 命令行入口(从 DuckDB 读数 → 跑回测 → 打印指标)

## 用法

```bash
# 先用数据层取数(见 src/data/README.md),例如同步前 200 只:
python -m src.data.cli stocks && python -m src.data.cli daily --limit 200 --start 20200101

# 跑回测:
python -m src.backtest.run --signal reversal --lookback 20 --top 20 --start 20200101
python -m src.backtest.run --signal momentum --save data/equity.csv
```

## 在代码里调用

```python
from src.data import storage
from src.backtest.engine import run_backtest
from src.backtest.strategies.cross_sectional import cross_sectional_weights

panel = storage.load_bars(start="20200101", adjust="hfq")[["date","code","open","close"]]
weights = cross_sectional_weights(panel, signal="reversal", lookback=20, top_n=20)
res = run_backtest(panel, weights)
print(res["metrics"]); print(res["equity"].tail())
```

## ⚠️ 已知近似(继续改进的方向)

- 涨跌停按**前缀**近似阈值;**ST 的 5%** 未识别。精确需股票状态表。
- 用**开盘价**近似判断当日能否成交;真实涨跌停是盘中动态。
- 无冲击成本模型(只有固定滑点);小市值/大额时需补建。
- 未扣分红税、未处理配股/转增的现金流(后复权价已含价格调整)。
- **务必做样本内外划分**,本脚本默认全样本,易过拟合参数。

> 本项目仅为技术研究,不构成投资建议。

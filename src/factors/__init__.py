"""OurWorlds Quant Lab — 因子层(阶段 2)。

单因子检验流水线:因子计算 → 预处理(去极值/标准化/中性化)→ 评估(IC/ICIR/分层/多空)。

- factors:   从日线 panel 计算常见因子(动量/反转/波动率/Amihud/均线乖离)
- preprocess: MAD 去极值、截面 z-score、OLS 中性化(对市值/行业等暴露)
- evaluate:  前向收益对齐、Spearman IC 序列、ICIR/t 值、分层回测、多空组合
- run:       命令行入口

方法论参考《因子投资:方法与实践》。本项目仅为研究,不构成投资建议。
"""

__version__ = "0.1.0"

"""OurWorlds Quant Lab — 数据层。

阶段 0:把 A 股日线数据从多个源拉下来,做复权/停牌/退市处理,落地到本地 DuckDB。

子模块:
- config:   路径、token、复权默认值、限流参数
- utils:    代码规范化、重试、日志
- sources:  AkShare / BaoStock / Tushare 三源适配器(统一接口)
- storage:  DuckDB 读写
- clean:    标准化 / 质检 / 停牌缺口 / 涨跌停近似标记
- pipeline: 编排(取列表 → 取日线 → 清洗 → 落库)
- cli:      命令行入口
"""

__version__ = "0.1.0"

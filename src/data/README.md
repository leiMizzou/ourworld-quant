# 数据层 · src/data

阶段 0 的产出:把 A 股日线从多个源拉下来,做复权/退市/停牌处理,落地到本地 **DuckDB**。

## 设计要点

- **三源互备**:`AkShare`(主,免费免 token)、`BaoStock`(校验,免费)、`Tushare`(可选,需 token)。统一接口 `get_stock_list()` / `get_daily_bars()`,列名与单位全部归一。
- **统一口径**:成交量=股、成交额=元、价格=元(各源原始单位在适配器内换算)。
- **复权默认 `hfq`(后复权)**:历史价格不随新分红重算,回测**无前视偏差**;需要前复权/不复权用 `--adjust qfq|none`。
- **缓解幸存者偏差**:股票列表尽量含**已退市**股票(AkShare 退市接口 / Tushare `list_status=D,P`)。
- **可重复跑**:DuckDB 主键 `(code,date,adjust)`,重复同步不产生重复行;`daily` 默认**增量**(只补每只股票最新日期之后)。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r src/data/requirements.txt
```

## 用法(在仓库根目录)

```bash
python -m src.data.cli init                              # 建表
python -m src.data.cli sample                            # 演示:取 3 只样本股入库并体检
python -m src.data.cli stocks --source akshare           # 同步股票列表(含退市)
python -m src.data.cli daily --source akshare --start 20200101 --limit 50   # 同步前 50 只
python -m src.data.cli daily --codes 000001.SZ 600519.SH # 指定代码
python -m src.data.cli info                              # 查看库内行数
```

Tushare 源需先 `export TUSHARE_TOKEN=你的token`。

## 在代码里读数据

```python
from src.data import storage
df = storage.load_bars(["000001.SZ", "600519.SH"], start="20230101", adjust="hfq")
```

## 配置(环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `OWQ_DATA_DIR` | `<repo>/data` | 数据目录(已 gitignore) |
| `OWQ_DB_PATH` | `<data>/market.duckdb` | DuckDB 文件 |
| `OWQ_START` | `20180101` | 默认起始日 |
| `OWQ_ADJUST` | `hfq` | 默认复权口径 |
| `OWQ_SLEEP` | `0.4` | 每次请求间隔(限流) |
| `TUSHARE_TOKEN` | — | Tushare token |

## ⚠️ 已知近似与后续

- `clean.add_flags` 的涨跌停为**统一阈值近似**;精确判定需结合主板/创业板/科创板/ST 与停牌状态。
- BaoStock 的 `get_stock_list` 仅取当前可交易标的(退市覆盖用 AkShare/Tushare)。
- qfq 用区间内最新因子做近似;严格前复权需全历史最新复权因子。
- 下一步(阶段 1):基于本表搭含**真实扣费/滑点/涨跌停不可成交**的回测框架。

> 本项目仅为技术研究,不构成投资建议。

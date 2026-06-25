"""命令行入口。

用法(在仓库根目录):
    python -m src.data.cli init                         # 初始化 DuckDB
    python -m src.data.cli stocks --source akshare      # 同步股票列表(含退市)
    python -m src.data.cli sample                       # 演示:取几只样本股并体检
    python -m src.data.cli daily --source akshare --start 20200101 --limit 50
    python -m src.data.cli daily --codes 000001.SZ 600519.SH --adjust hfq
    python -m src.data.cli info                         # 查看库内行数

Tushare 需先 export TUSHARE_TOKEN=xxxx。
"""
from __future__ import annotations

import argparse

from . import clean, config, pipeline, storage
from .utils import log


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser("owq-data", description="OurWorlds Quant Lab 数据管道")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化 DuckDB 表")

    sp = sub.add_parser("stocks", help="同步股票列表(含退市)")
    sp.add_argument("--source", default="akshare", choices=["akshare", "baostock", "tushare"])

    dp = sub.add_parser("daily", help="同步日线")
    dp.add_argument("--source", default="akshare", choices=["akshare", "baostock", "tushare"])
    dp.add_argument("--start", default=config.DEFAULT_START)
    dp.add_argument("--adjust", default=config.DEFAULT_ADJUST, choices=["hfq", "qfq", "none"])
    dp.add_argument("--codes", nargs="*", help="指定代码(如 000001.SZ);不填则用库内股票列表")
    dp.add_argument("--limit", type=int, default=None, help="最多同步多少只(调试)")
    dp.add_argument("--full", action="store_true", help="非增量,从 start 全量重取")

    smp = sub.add_parser("sample", help="演示:取 3 只样本股入库并打印体检")
    smp.add_argument("--source", default="akshare", choices=["akshare", "baostock", "tushare"])

    sub.add_parser("info", help="查看库内表行数")

    args = p.parse_args(argv)

    if args.cmd == "init":
        storage.init_db()

    elif args.cmd == "stocks":
        pipeline.sync_stock_list(args.source)

    elif args.cmd == "daily":
        codes = args.codes or pipeline.codes_from_db(limit=args.limit)
        if not codes:
            log.error("没有代码可同步。请先 `stocks` 同步列表,或用 --codes 指定。")
            return 1
        if args.limit and args.codes:
            codes = codes[: args.limit]
        pipeline.sync_daily(codes, source_name=args.source, start=args.start,
                            adjust=args.adjust, incremental=not args.full)

    elif args.cmd == "sample":
        codes = ["000001.SZ", "600519.SH", "300750.SZ"]
        log.info("演示样本: %s", codes)
        pipeline.sync_daily(codes, source_name=args.source, start="20230101", adjust="hfq")
        df = storage.load_bars(codes, adjust="hfq")
        print("\n=== 体检 quality_report ===")
        print(clean.quality_report(df))
        if not df.empty:
            print("\n=== 样例(尾部 5 行)===")
            print(df.tail(5).to_string(index=False))

    elif args.cmd == "info":
        print(storage.table_counts())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

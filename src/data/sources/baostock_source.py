"""BaoStock 适配器 —— 免费、无需 token,作为校验源(数据干净,适合对账)。

复权 adjustflag:1=后复权(hfq),2=前复权(qfq),3=不复权(none)。
单位:成交量 股、成交额 元(无需换算)。
说明:BaoStock 取『全量含退市』不便,这里 get_stock_list 用 query_all_stock(交易日)取当前可交易标的;
退市覆盖请优先用 AkShare/Tushare。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from ..utils import log, normalize_code, polite_sleep, retry, to_baostock_code
from .base import BAR_COLUMNS, STOCK_COLUMNS, DataSource

_ADJ_FLAG = {"hfq": "1", "qfq": "2", "none": "3"}


class BaoStockSource(DataSource):
    name = "baostock"

    def __init__(self) -> None:
        import baostock as bs
        self._bs = bs
        r = bs.login()
        if r.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败: {r.error_msg}")
        log.info("BaoStock 已登录")

    def close(self) -> None:
        try:
            self._bs.logout()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _rs_to_df(rs) -> pd.DataFrame:
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    @retry
    def get_stock_list(self) -> pd.DataFrame:
        # 取最近交易日的可交易标的
        day = dt.date.today().strftime("%Y-%m-%d")
        rs = self._bs.query_all_stock(day=day)
        df = self._rs_to_df(rs)
        if df.empty:  # 今天非交易日,回退 10 天
            day = (dt.date.today() - dt.timedelta(days=10)).strftime("%Y-%m-%d")
            df = self._rs_to_df(self._bs.query_all_stock(day=day))
        # 只留股票(代码 sh.6/sz.0/sz.3 等),滤掉指数
        df = df[df["code"].str.contains(r"\.(?:6|0|3|4|8|9)")]
        out = pd.DataFrame({
            "code": df["code"].map(normalize_code),
            "name": df.get("code_name", ""),
            "status": "L",
        })
        out["list_date"] = ""
        out["delist_date"] = ""
        out["source"] = self.name
        return out[STOCK_COLUMNS].drop_duplicates("code").reset_index(drop=True)

    @retry
    def get_daily_bars(self, code, start, end=None, adjust="hfq") -> pd.DataFrame:
        end = end or dt.date.today().strftime("%Y%m%d")
        fmt = lambda s: f"{s[:4]}-{s[4:6]}-{s[6:8]}"  # noqa: E731
        rs = self._bs.query_history_k_data_plus(
            to_baostock_code(code),
            "date,open,high,low,close,volume,amount",
            start_date=fmt(start), end_date=fmt(end),
            frequency="d", adjustflag=_ADJ_FLAG.get(adjust, "1"),
        )
        polite_sleep()
        df = self._rs_to_df(rs)
        if df.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["code"] = normalize_code(code)
        df["adjust"] = adjust
        df["source"] = self.name
        return df[BAR_COLUMNS]

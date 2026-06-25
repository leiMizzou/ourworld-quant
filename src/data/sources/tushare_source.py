"""Tushare 适配器 —— 需 token(环境变量 TUSHARE_TOKEN),可选高质量源。

复权:pro.daily 给不复权价 + pro.adj_factor 复权因子,自己算 hfq/qfq。
  hfq = price * adj_factor
  qfq = price * adj_factor / 最新 adj_factor(本区间近似;严格 qfq 需全历史最新因子)
单位:成交量 手→股(×100),成交额 千元→元(×1000)。
幸存者偏差:pro.stock_basic(list_status='L'/'D'/'P') 可取在市/退市/暂停。
"""
from __future__ import annotations

import pandas as pd

from .. import config
from ..utils import log, normalize_code, polite_sleep, retry, to_tushare_code
from .base import BAR_COLUMNS, STOCK_COLUMNS, DataSource


class TushareSource(DataSource):
    name = "tushare"

    def __init__(self) -> None:
        if not config.TUSHARE_TOKEN:
            raise RuntimeError("未设置 TUSHARE_TOKEN 环境变量,无法使用 Tushare 源。")
        import tushare as ts
        ts.set_token(config.TUSHARE_TOKEN)
        self._pro = ts.pro_api()

    @retry
    def get_stock_list(self) -> pd.DataFrame:
        frames = []
        for status in ("L", "D", "P"):  # 在市 / 退市 / 暂停 —— 含退市以缓解幸存者偏差
            d = self._pro.stock_basic(list_status=status,
                                      fields="ts_code,name,list_date,delist_date")
            polite_sleep()
            if d is not None and not d.empty:
                d["status"] = status
                frames.append(d)
        if not frames:
            return pd.DataFrame(columns=STOCK_COLUMNS)
        out = pd.concat(frames, ignore_index=True)
        out = out.rename(columns={"ts_code": "code"})
        out["code"] = out["code"].map(normalize_code)
        out["list_date"] = out.get("list_date", "").fillna("")
        out["delist_date"] = out.get("delist_date", "").fillna("")
        out["source"] = self.name
        return out[STOCK_COLUMNS].drop_duplicates("code").reset_index(drop=True)

    @retry
    def get_daily_bars(self, code, start, end=None, adjust="hfq") -> pd.DataFrame:
        ts_code = to_tushare_code(code)
        end = end or "20991231"
        daily = self._pro.daily(ts_code=ts_code, start_date=start, end_date=end)
        polite_sleep()
        if daily is None or daily.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        daily = daily.sort_values("trade_date")
        if adjust != "none":
            adj = self._pro.adj_factor(ts_code=ts_code, start_date=start, end_date=end)
            polite_sleep()
            daily = daily.merge(adj[["trade_date", "adj_factor"]], on="trade_date", how="left")
            f = daily["adj_factor"].astype(float)
            if adjust == "qfq":
                f = f / f.iloc[-1]
            for c in ["open", "high", "low", "close"]:
                daily[c] = daily[c].astype(float) * f
        df = pd.DataFrame({
            "code": normalize_code(code),
            "date": pd.to_datetime(daily["trade_date"], format="%Y%m%d"),
            "open": daily["open"].astype(float),
            "high": daily["high"].astype(float),
            "low": daily["low"].astype(float),
            "close": daily["close"].astype(float),
            "volume": daily["vol"].astype(float) * 100,        # 手 → 股
            "amount": daily["amount"].astype(float) * 1000,    # 千元 → 元
            "adjust": adjust,
            "source": self.name,
        })
        return df[BAR_COLUMNS]

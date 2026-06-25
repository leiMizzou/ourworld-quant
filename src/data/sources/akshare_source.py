"""AkShare 适配器 —— 免费、无需 token,作为主源。

注意:AkShare 底层接口随版本变动较大,这里对列名/函数做了防御性处理。
单位归一:成交量 手→股(×100),成交额本就为元。
"""
from __future__ import annotations

import pandas as pd

from ..utils import bare_code, log, normalize_code, polite_sleep, retry
from .base import BAR_COLUMNS, STOCK_COLUMNS, DataSource

_ADJ_MAP = {"hfq": "hfq", "qfq": "qfq", "none": ""}

# AkShare 中文列 → 标准列
_COL_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
}


class AkShareSource(DataSource):
    name = "akshare"

    def __init__(self) -> None:
        import akshare as ak  # 延迟导入,未装也能 import 本包
        self._ak = ak

    @retry
    def get_stock_list(self) -> pd.DataFrame:
        frames = []
        # 在市
        live = self._ak.stock_info_a_code_name()  # columns: code, name
        live = live.rename(columns={"code": "code", "name": "name"})
        live["status"] = "L"
        frames.append(live[["code", "name", "status"]])
        # 退市(缓解幸存者偏差;不同 akshare 版本签名不同,失败则跳过)
        for fn_name, args in (
            ("stock_info_sh_delist", {}),
            ("stock_info_sz_delist", {"symbol": "终止上市公司"}),
        ):
            try:
                fn = getattr(self._ak, fn_name)
                d = fn(**args)
                col = next((c for c in d.columns if "代码" in c or c.lower() == "code"), None)
                ncol = next((c for c in d.columns if "名称" in c or "简称" in c), None)
                if col:
                    dd = pd.DataFrame({"code": d[col], "name": d[ncol] if ncol else ""})
                    dd["status"] = "D"
                    frames.append(dd)
            except Exception as exc:  # noqa: BLE001
                log.warning("AkShare 退市列表 %s 获取失败(可忽略): %s", fn_name, exc)

        out = pd.concat(frames, ignore_index=True)
        out["code"] = out["code"].map(normalize_code)
        out = out.drop_duplicates("code", keep="first")
        out["list_date"] = ""
        out["delist_date"] = ""
        out["source"] = self.name
        return out[STOCK_COLUMNS].reset_index(drop=True)

    @retry
    def get_daily_bars(self, code, start, end=None, adjust="hfq") -> pd.DataFrame:
        end = end or "20991231"
        raw = self._ak.stock_zh_a_hist(
            symbol=bare_code(code), period="daily",
            start_date=start, end_date=end, adjust=_ADJ_MAP.get(adjust, "hfq"),
        )
        polite_sleep()
        if raw is None or raw.empty:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df = raw.rename(columns=_COL_MAP)
        keep = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
        df = df[keep].copy()
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100  # 手 → 股
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")        # 元
        df["code"] = normalize_code(code)
        df["adjust"] = adjust
        df["source"] = self.name
        return df[BAR_COLUMNS]

<div align="center">

# OurWorlds Quant Lab · 量化实验室

**一个从零开始、公开构建的 A 股个人量化交易/研究项目**
*Building a personal A-share quant trading & research stack — in public.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-building-orange.svg)]()
[![Made with](https://img.shields.io/badge/made%20with-Python-blue.svg)]()

🌐 **在线站点 / Site:** https://quant.ourworlds.app &nbsp;·&nbsp; 📓 **构建日志 / Build Log:** [站点内](https://quant.ourworlds.app/#log)

</div>

## 这是什么

这是一个**边做边公开**的个人量化项目。目标不是炫技或喊单,而是把"一个有工程能力的个人,如何从零搭起一套 A 股量化研究到实盘的完整闭环"这件事,**完整、透明、可复现地记录下来**——代码开源、过程上站、心得做自媒体。

- 🧱 **可复现**:数据管道、回测框架、因子研究、策略,全部开源可跑。
- 📖 **透明**:每个阶段的进展、踩的坑、改的错,都写进[构建日志](docs/index.html)。
- 🎯 **务实**:适配小资金(≤10万)、中低频、纯多头的现实约束,不碰拼不过机构的高频。

> 💡 本项目所有内容仅为技术研究与学习记录,**不构成任何投资建议**。量化有风险,实盘需谨慎。

---

## 为什么公开做

| 动机 | 说明 |
|---|---|
| **内容即资产** | 研究笔记 + 实盘曲线是投递量化私募/资管最值钱的作品集 |
| **公开倒逼质量** | Build in public 逼自己把代码和方法论做扎实 |
| **运营/自媒体** | 站点 + 仓库 + 日志天然就是自媒体素材,顺手就把个人品牌做了 |
| **回馈社区** | 国内个人量化的完整开源闭环不多,做一个真实样本 |

---

## 路线图(6 个月跑通闭环)

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| **0** | 环境与数据管道 | 可靠的本地数据管道 | ✅ 完成 |
| **1** | 回测引擎 + 复现经典策略 | 可信任的回测框架 | ✅ 完成 |
| **2** | 因子研究 + 多因子组合 | 多因子选股策略 + 研究笔记 | 🟡 进行中 |
| **3** | 模拟盘验证 | 回测/模拟一致的策略 | ⬜ 未开始 |
| **4** | 小资金实盘 | 真实实盘曲线 | ⬜ 未开始 |

> 完整路线图与方法论见 [`plan/A股量化_个人准备计划.md`](plan/A股量化_个人准备计划.md)。

---

## 技术栈

- **语言**:Python 3.11+
- **数据**:AkShare + Tushare(双源互备)、BaoStock(校验)
- **存储**:Parquet + DuckDB(轻量),量大上 ClickHouse
- **回测**:backtrader(入门)→ vectorbt(提速)→ qlib(机器学习)
- **实盘**:vnpy / QMT / Ptrade(中后期)
- **站点**:自包含静态 HTML,经 Cloudflare Tunnel + 反向代理对外发布

---

## 仓库结构

```
ourworld-quant/
├── docs/            # 公开站点(index.html)— 主站 + 构建日志
├── plan/            # 总体计划与方法论
├── deploy/          # Cloudflare Tunnel + 反代部署配置
├── src/
│   ├── data/        # 数据管道(取数/清洗/存储 → DuckDB)
│   ├── factors/     # 因子计算 + 单因子检验(IC/ICIR/分层/多空)
│   ├── backtest/    # 回测引擎(T+1/涨跌停/费用/滑点)+ 策略
│   └── research/    # 多因子合成 → 组合 → 回测 闭环
├── notebooks/       # 研究 notebook
├── README.md
└── LICENSE
```

---

## 部署

站点经 **Cloudflare Tunnel + 反向代理**对外发布,详见 [`deploy/cloudflare-tunnel.md`](deploy/cloudflare-tunnel.md)。
本地预览:

```bash
cd docs && python -m http.server 8080   # 浏览器打开 http://localhost:8080
```

---

## 跟进 · Follow

- 🌐 站点:https://quant.ourworlds.app
- 🐙 GitHub:https://github.com/leiMizzou/ourworld-quant
- ⭐ 如果这个项目对你有帮助,欢迎 Star / Watch 跟进进度。

---

## License

[MIT](LICENSE) © 2026 OurWorlds Quant Lab

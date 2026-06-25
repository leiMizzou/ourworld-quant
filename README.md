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

## 本地快速启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[app,data,test]"

# 启动社区模拟盘:邮箱验证注册 + 账号密码登录 + 模拟交易 + 公开赛 + 论坛
python3 -m src.app.server --host 127.0.0.1 --port 8081
```

打开 http://127.0.0.1:8081 。本地开发可用 `OWQ_EMAIL_DEV_AUTH=1` 显示一次性测试邮箱验证链接;生产接入需配置 Cloudflare Email Sending 或 SMTP,并关闭测试验证入口。Cloudflare Email Routing/转发只处理入站邮件,不能替代确认邮件发信;如果使用 Gmail SMTP,需要 Google App Password,不能使用普通邮箱密码。用户通过邮箱验证后设置用户名和密码,之后从 `/login` 用用户名/邮箱 + 密码进入模拟盘;忘记密码时从 `/forgot-password` 请求一次性重置链接。`/support` 提供公开支持请求入口,可在邮件未配置、注册异常、数据问题或商务合作场景下提交站内工单。公开传播页为 `/showcase/public`,公开数据透明页为 `/data-status`,个人战绩页为 `/u/<user_id>` 并展示收益、持仓、最近成交和策略帖,`/u/<user_id>/card.svg` 可生成公开战绩卡,论坛帖子 `/forum/<post_id>` 可公开阅读。公开榜单、数据状态、个人页、论坛和帖子页带分享标题/描述/图片 meta。论坛列表支持按标签、关键词和最新/战绩/评论数筛选排序。登录后的 Showcase 页面可一键生成带排名、持仓、成交和演练计划的论坛战绩复盘草稿;作者和管理员可以删除帖子/评论。

邮箱注册页会先要求用户明确勾选服务条款、隐私说明和风险提示,然后发送一次性邮箱验证链接。用户点击邮件链接后会先把 token 换成 15 分钟短期 HttpOnly 确认 Cookie,再打开不带 token 的账号设置页;页面不会直接下发登录态。用户设置用户名和密码后,系统才会创建或复用账户、自动加入公开赛、记录法律同意版本,并跳转 `/login` 让用户用账号密码登录。`/forgot-password` 复用同一套短期确认 Cookie 和一次性 token,但只会给已存在且已有密码的邮箱账号生成重置链接;未知邮箱只显示泛化提示,避免账号枚举或把密码重置变成隐式注册。公网/生产环境会要求登录用户具备当前 `LEGAL_VERSION` 的条款、隐私和风险同意记录;版本更新或历史用户缺失同意记录时,会先跳转 `/account/consent` 补签,但仍允许导出个人数据。这样既避免邮件网关预取提前登录,也避免原始 token 长时间留在地址栏或页面 HTML 中。

模拟盘首次空库会使用演示行情;生产环境应先用 `src.data` 同步真实日线,再用 `python3 -m src.app.server --sync-market --market-adjust none --replace-market --sync-only` 或 `deploy/sync-market-public.sh` 把不复权最新收盘价同步为模拟成交价格。研究回测继续使用 `hfq`,模拟盘成交使用 `none`,避免后复权价格被当作真实成交价。也支持 `--market-csv /path/to/market.csv` 导入 `code,name,price,prev_close,as_of` CSV,并可直接在网页粘贴同格式 CSV 行情。`/healthz` 会检查生产环境是否仍只有 demo 行情,并按 `OWQ_MARKET_MAX_STALENESS_DAYS` 判断最新行情日期是否过旧;`/readyz` 还会检查真实行情覆盖度,默认正式发布建议不少于 300 个非 demo 标的,可用 `OWQ_MARKET_MIN_REAL_CODES` 调整。生产同步脚本默认会刷新 `reports/real-data-report.md` 和 `reports/predictions.csv`;严格发布还会检查预测候选能匹配当前真实行情,默认至少 10 个候选,可用 `OWQ_PREDICTIONS_MIN_CODES` 调整。注册、交易、行情同步都会记录资产快照,用于账户复盘、公开战绩页和论坛战绩分享。模拟盘首页支持先记录“策略演练计划”,也可以粘贴 `code,side,qty,rationale` 策略篮子批量导入待执行计划,或按当前基础行情涨跌幅一键生成反转/动量候选篮子;`/portfolio-lab` 可直接用真实行情和 `reports/predictions.csv` 模型预测候选生成组合演练计划。待执行计划可逐条执行,也可批量执行为模拟成交。交易遵循 A 股 T+1,当天买入不可卖,账户页可“进入下一交易日”释放可卖数量。账户页支持导出完整账户数据 JSON、成交记录 CSV、当前持仓 CSV 和资产曲线 CSV,每次导出会记录 `account.export` 审计事件但不会记录导出正文;也支持维护公开昵称/头像、重置模拟账户或关闭账户。重置模拟账户需要输入 `RESET` 确认;关闭账户会删除登录身份、模拟盘和社区内容并清除登录 Cookie。

系统自检:

```bash
python3 -m src.app.server --doctor
python3 -m src.app.server --doctor-strict
python3 -m src.app.server --send-test-email you@example.com
python3 -m src.app.server --prune-audit-log
python3 -m src.app.server --prune-email-login-sessions
python3 -m src.app.server --env-file deploy/public.env --doctor-strict
python3 -m src.app.server --env-file deploy/public.env --send-test-email you@example.com
```

运行中服务提供只读存活检查 `GET /livez` / `HEAD /livez`、基础健康/就绪诊断 `GET /healthz` / `HEAD /healthz`、严格发布检查 `GET /readyz` / `HEAD /readyz`,以及运行指标 `GET /metrics` / `HEAD /metrics`。`/livez` 只检查进程和应用数据库能否响应;`/healthz` 会在必需项异常时返回 503;`/readyz` 会在任何 warning 存在时返回 503,适合作为正式发布闸门。公网请求默认只返回摘要和失败检查项名称;本机请求会返回完整诊断,远程运维可配置 `OWQ_HEALTH_DETAIL_TOKEN` 并通过 `X-OWQ-Health-Token` 请求头获取完整 `checks`。公网发布时会检查 `OWQ_SECRET`、HTTPS `OWQ_PUBLIC_BASE_URL`、Secure Cookie、管理员配置、法律条款补签门禁、真实行情覆盖、行情覆盖度、行情新鲜度、预测候选结果、磁盘剩余空间、邮箱发信/测试验证模式、近期成功发信诊断、支持请求/内容举报待处理队列是否积压以及近期是否发生服务端异常;队列阈值默认 72 小时,可用 `OWQ_OPERATIONAL_QUEUE_MAX_AGE_HOURS` 调整,`server.error` 观察窗口默认 24 小时,可用 `OWQ_SERVER_ERROR_WINDOW_HOURS` 调整。公网 `/metrics` 默认只返回 `{"status":"ok","detail":"summary"}`;本机请求或携带正确 `X-OWQ-Health-Token` 的运维请求才返回请求总数、状态码分布、方法分布、运行时长和耗时等聚合指标,并且不记录 IP、邮箱、Cookie、URL 参数或用户标识。

公开站点同时提供 `GET /data-status` / `HEAD /data-status`、`GET /support` / `HEAD /support`、`GET /robots.txt` / `HEAD /robots.txt` 和 `GET /sitemap.xml` / `HEAD /sitemap.xml`。`/data-status` 面向用户展示行情来源、最新交易日、预测候选匹配数和赛场活跃度,不展示内部密钥、管理员、登录会话或邮箱发信配置。`/support` 用于提交注册、登录、数据、社区或商务支持请求,会返回 noindex 并不会进入 sitemap;公开支持入口同时有 IP 限流、同邮箱提交冷却、每小时上限和未处理请求数量上限,触发时会写入脱敏 `security.rate_limited` 审计事件。sitemap 只列出首页、数据透明页、公开榜单、论坛、公开帖子、公开用户战绩页和法律说明页;登录、账户、管理、监控、支持和认证端点不会进入 sitemap,并会返回 `X-Robots-Tag: noindex, nofollow`。

应用数据备份:

```bash
python3 -m src.app.server --backup-app-db
python3 -m src.app.server --backup-app-db data/backups/app-before-release.sqlite
python3 -m src.app.server --verify-app-backup data/backups/app-before-release.sqlite
python3 -m src.app.server --restore-app-backup data/backups/app-before-release.sqlite data/restore-drill.sqlite
python3 -m src.app.server --sqlite-maintenance
```

应用数据库连接会启用 SQLite WAL、`synchronous=NORMAL`、外键约束和写锁等待,默认 `OWQ_SQLITE_BUSY_TIMEOUT_MS=5000`;`/healthz` 会检查 `PRAGMA quick_check`、`PRAGMA foreign_key_check`、journal mode、busy timeout、WAL 文件大小和磁盘剩余空间。WAL 文件默认超过 `OWQ_SQLITE_MAX_WAL_MB=256` 会在严格发布检查中告警。`--sqlite-maintenance` 会执行 `PRAGMA optimize` 和 `PRAGMA wal_checkpoint(TRUNCATE)`,生产同步脚本会在行情和预测刷新后自动运行一次。磁盘检查默认读取 `data` 所在磁盘,要求至少 1024 MB 可用,可用 `OWQ_DISK_CHECK_PATH` 和 `OWQ_MIN_FREE_DISK_MB` 调整。备份使用 SQLite online backup API,会复制用户、账户、模拟盘、比赛、论坛和登录会话等应用数据;默认写入 `OWQ_APP_BACKUP_DIR` 或 `data/backups/`。自动命名的 `app-*.sqlite` 备份默认只保留最近 30 份,可用 `OWQ_APP_BACKUP_KEEP` 调整;`/readyz` 还会检查最近备份是否在 `OWQ_APP_BACKUP_MAX_AGE_HOURS` 小时内且 `quick_check=ok`,默认 48 小时。`--verify-app-backup` 会只读打开备份,验证 quick_check、核心表和外键一致性,不会创建或修改当前应用数据库。`--restore-app-backup BACKUP DEST` 会先验证备份,再恢复到指定目标文件;默认拒绝覆盖已有文件,确需覆盖时必须显式加 `--restore-overwrite`。手动指定路径的备份不会触发清理。

登录密码使用 PBKDF2 哈希存储,不会保存明文。登录态使用带服务端过期时间和服务端会话版本的 HMAC 签名 Cookie,默认 30 天有效,可用 `OWQ_SESSION_TTL_SECONDS` 调整;Cookie 带 HttpOnly + SameSite=Lax,公网 HTTPS 或 `OWQ_COOKIE_SECURE=1` 时会自动添加 Secure。退出登录、账户页改密或邮箱重置密码都会递增服务端会话版本,旧 Cookie 会失效。退出登录必须通过 POST 表单提交 CSRF token,成功后清除 Cookie 并记录 `auth.logout`。所有登录后的 POST 表单都会带 CSRF token 并在服务端校验;CSRF 失败会写入 `security.csrf_failed` 审计事件,只记录路径和重定向目标,不记录表单正文。服务端会统一下发 `X-Frame-Options`、`X-Content-Type-Options`、`Referrer-Policy`、`Permissions-Policy`、CSP,并在 HTTPS 场景下下发 HSTS;HSTS 默认 180 天,可用 `OWQ_HSTS_MAX_AGE_SECONDS` 调整或设为 `0` 关闭。服务端会对注册、忘记密码、登录、邮件确认、旧兼容认证端点和登录用户写入操作做轻量限流;登录用户写入节流覆盖下单、演练计划、手工行情同步、发帖、评论和举报,超限会写入 `security.rate_limited` 审计事件。`OWQ_MAX_FORM_BYTES` 限制 POST 表单请求体大小,默认 1MB,最大 5MB;确需压测时可临时设置 `OWQ_RATE_LIMITS_DISABLED=1`,正式发布闸门会阻止生产环境关闭限流。

所有 HTML/JSON/CSV/SVG/跳转响应都会输出基础安全头,包括 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、私有端点 `X-Robots-Tag` 和 HTML `Content-Security-Policy`。未捕获的路由异常会统一返回 500 页面,并写入 `server.error` 审计事件;响应不会暴露异常详情,只会显示可和审计日志对应的错误编号。

管理员访问 `/admin` 可配置比赛信息、查看系统状态、查看用户账户概览、查看用户同意记录、暂停/恢复用户、处理内容举报、处理支持请求、查看安全/异常事件摘要,并导出用户账户概览、内容举报、支持请求和审计日志 CSV。后台处理和导出会写入审计事件,便于运营、客服和合规复盘。未配置管理员环境变量时,仅本地开发默认第一个注册用户为管理员;公网或生产环境不会启用该兜底,必须显式配置 `OWQ_ADMIN_USER_IDS` 或 `OWQ_ADMIN_EMAILS`。正式关闭测试验证入口前,还需要确保至少一个管理员可通过邮箱验证/重置密码或既有账号密码登录恢复后台访问。
管理页可一键生成演示参赛账户与策略帖,用于 beta 阶段快速展示 Showcase 和论坛传播效果;也可一键创建应用数据库一致性备份。正式生产关闭测试验证入口后默认禁止生成演示比赛数据,`/readyz` 也会提示仍留在公开赛中的 demo/dev/模拟用户参赛账户,避免测试战绩混入正式榜单。被暂停用户仍可登录查看账户和导出个人数据,但不能提交交易、演练计划或社区写入。用户可在账户页自助关闭账户并删除个人应用数据;审计日志会保留登录、交易、数据导出、账户关闭、后台操作、后台越权访问和 CSRF 失败等最小操作记录,用于排查和运营追踪。审计日志默认保留 400 天,可用 `OWQ_AUDIT_RETENTION_DAYS` 调整到 30-3650 天;后台和 `--prune-audit-log` 可清理超期记录,生产同步脚本默认会执行一次并写入 `admin.audit_prune` 或 `cli.audit_prune` 审计事件。邮箱验证临时会话默认保留 30 天,可用 `OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS` 调整到 1-365 天;后台、`--prune-email-login-sessions` 和生产同步脚本会过期未使用链接并清理超期 confirmed/expired 会话。`/readyz` 还会检查未处理支持请求和内容举报的最久等待时间,默认超过 72 小时告警;近 24 小时发生过未捕获服务端异常时也会告警,直到运营人员排查并确认窗口滚动清零。

公开合规说明页:

- `/terms`:服务条款。
- `/privacy`:隐私说明。
- `/risk`:风险提示。
- `/legal`:以上说明的汇总入口。
- `/data-status`:公开数据透明页,展示行情来源、预测候选和赛场活跃度。
- `/robots.txt`、`/sitemap.xml`:公开传播和搜索引擎发现文件。

运行测试:

```bash
python3 -m unittest discover -s tests
```

公网运行脚本、launchd 守护进程模板、行情同步定时任务、发布检查和日志路径见 `deploy/run-public-app.sh`、`deploy/check-public.sh`、`deploy/launchd/` 与 `deploy/README.md`。

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
│   ├── app/         # 本地社区模拟盘(邮箱验证/账号登录/账户/模拟交易/公开赛/论坛)
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

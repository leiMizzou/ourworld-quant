# 社区模拟盘 · src/app

这是一个不依赖 Web 框架的本地 MVP,用于把当前量化研究系统变成可交互产品:

- 邮箱验证注册:用户先明确同意服务条款、隐私说明和风险提示,系统再发送一次性邮箱验证链接;链接 GET 只设置短期确认 Cookie 并进入无 token 的设置页,不会直接登录。
- 账号密码登录:邮箱确认后设置用户名和密码,之后通过 `/login` 用用户名/邮箱 + 密码进入模拟盘。密码使用 PBKDF2 哈希存储,不会保存明文。
- 模拟账户:注册后自动创建 100 万模拟资金账户;账户页可维护公开昵称/头像,也可输入 `RESET` 确认后重置模拟账户,清空持仓/成交/资产快照并保留论坛内容。
- 基础行情:首次空库会写入一组演示行情;生产可从 `src.data` DuckDB、CSV 文件或粘贴 CSV 同步真实价格并替换演示标的。
- 模拟交易:买入按 100 股整数倍,扣佣金/过户费/卖出印花税,并按 A 股 T+1 规则限制当天买入不可卖。
- T+1 结算:账户页可进入下一交易日,把当前持仓释放为可卖数量。
- 策略演练计划:用户可以先保存策略名称、标的、方向、数量和依据,再一键按当前行情执行成模拟成交。
- 策略篮子导入:用户可以把研究结果或组合目标粘贴为 `code,side,qty,rationale` CSV,一次生成多条待执行演练计划。
- 基础行情篮子:用户可以按当前行情涨跌幅一键生成反转/动量候选演练计划,用于快速把数据观察转成模拟盘测试。
- 组合设计:登录后访问 `/portfolio-lab`,可直接用真实同步行情和 `reports/predictions.csv` 预测结果生成待执行演练计划。
- 批量执行:待执行演练计划可以逐条执行,也可以一次批量执行;失败计划会保留为待执行并返回失败原因。
- 资产快照:注册、交易、行情同步后记录账户权益,用于账户复盘和公开战绩页展示。
- 数据导出:账户页可导出完整账户数据 JSON,也可导出成交记录、当前持仓和资产曲线 CSV,便于用户在表格或研究 notebook 中复盘和留档。
- 公开赛 Showcase:用户自动加入默认公开赛,按模拟盘收益率排行。
- 策略论坛:用户可以发布策略复盘和评论。

## 启动

```bash
python3 -m src.app.server --host 127.0.0.1 --port 8081
```

访问 http://127.0.0.1:8081 。

自检:

```bash
python3 -m src.app.server --doctor
```

运行中服务提供 `GET /livez` / `HEAD /livez`、`GET /healthz` / `HEAD /healthz`、严格发布检查 `GET /readyz` / `HEAD /readyz` 和运行指标 `GET /metrics` / `HEAD /metrics`。`/livez` 只检查进程和应用数据库能否响应;`/healthz` 会在缺少必需项时返回 503;`/readyz` 会在任何 warning 存在时返回 503,适合作为正式发布闸门。公网请求默认只返回摘要和失败检查项名称;本机请求会返回完整诊断,远程运维可配置 `OWQ_HEALTH_DETAIL_TOKEN` 并通过 `X-OWQ-Health-Token` 请求头获取完整 `checks` 和完整 `/metrics` 聚合计数。真实行情覆盖、真实行情覆盖度、行情新鲜度、预测候选结果、磁盘剩余空间、邮箱发信、近期成功发信诊断和 DuckDB 行情库等配置会在体检中展示。正式发布默认建议不少于 300 个非 demo 行情标的,可用 `OWQ_MARKET_MIN_REAL_CODES` 调整;预测候选默认要求至少 10 个能匹配当前真实行情的候选,可用 `OWQ_PREDICTIONS_MIN_CODES` 调整;磁盘默认要求至少 1024 MB 可用,可用 `OWQ_DISK_CHECK_PATH` 和 `OWQ_MIN_FREE_DISK_MB` 调整。公网 `/metrics` 默认只返回 `{"status":"ok","detail":"summary"}`;完整指标只包含请求总数、状态码分布、方法分布、运行时长和耗时等聚合值。
公开传播发现文件提供 `GET /robots.txt` / `HEAD /robots.txt` 和 `GET /sitemap.xml` / `HEAD /sitemap.xml`;sitemap 只列出无需登录的首页、公开榜单、论坛、公开帖子、公开用户战绩页和法律说明页。登录、账户、后台、认证、监控和下载端点还会返回 `X-Robots-Tag: noindex, nofollow`。
注册、登录、邮件确认、旧兼容认证端点和登录用户写入操作带内存限流;登录用户写入节流覆盖下单、演练计划、手工行情同步、发帖、评论和举报,超限会写入 `security.rate_limited` 审计事件。默认开启,可用 `OWQ_RATE_LIMITS_DISABLED=1` 临时关闭,但生产发布检查会把关闭限流视为 required warning。退出登录必须通过 POST 表单提交 CSRF token,成功后清除 Cookie、递增服务端会话版本并记录 `auth.logout`。POST 表单请求体默认限制为 1MB,可用 `OWQ_MAX_FORM_BYTES` 调整到 4096 字节到 5MB 之间;超限会返回 413。

应用数据可用 SQLite online backup API 备份:

```bash
python3 -m src.app.server --backup-app-db
python3 -m src.app.server --backup-app-db data/backups/app-before-release.sqlite
python3 -m src.app.server --verify-app-backup data/backups/app-before-release.sqlite
python3 -m src.app.server --restore-app-backup data/backups/app-before-release.sqlite data/restore-drill.sqlite
python3 -m src.app.server --sqlite-maintenance
```

应用数据库连接会启用 SQLite WAL、`synchronous=NORMAL`、外键约束和写锁等待,默认 `OWQ_SQLITE_BUSY_TIMEOUT_MS=5000`;`/healthz` 会检查 `PRAGMA quick_check`、`PRAGMA foreign_key_check`、journal mode、busy timeout 和 WAL 文件大小。WAL 文件默认超过 `OWQ_SQLITE_MAX_WAL_MB=256` 会在严格发布检查中告警。`--sqlite-maintenance` 会执行 `PRAGMA optimize` 和 `PRAGMA wal_checkpoint(TRUNCATE)`,生产同步脚本会在行情和预测刷新后自动运行一次。默认备份目录是 `OWQ_APP_BACKUP_DIR` 或 `data/backups/`。自动命名的 `app-*.sqlite` 备份默认保留最近 30 份,可用 `OWQ_APP_BACKUP_KEEP` 调整;`/readyz` 会检查最近备份是否在 `OWQ_APP_BACKUP_MAX_AGE_HOURS` 小时内且 `quick_check=ok`,默认 48 小时。`--verify-app-backup` 会只读验证备份的 quick_check、核心表和外键一致性,不会创建或修改当前应用数据库。`--restore-app-backup BACKUP DEST` 会恢复到指定目标文件,默认拒绝覆盖已有文件,覆盖必须显式加 `--restore-overwrite`。手动指定路径的备份不会触发清理。管理员也可以在 `/admin` 点击“立即备份应用数据库”触发同一套一致性备份。

登录态使用带服务端过期时间的 HMAC 签名 Cookie,默认 30 天有效,可用 `OWQ_SESSION_TTL_SECONDS` 调整;Cookie 带 HttpOnly + SameSite=Lax,公网 HTTPS 或 `OWQ_COOKIE_SECURE=1` 时会自动加 Secure。所有登录后的 POST 表单都会带 CSRF token 并在服务端校验;CSRF 失败会写入 `security.csrf_failed` 审计事件,只记录路径和重定向目标,不记录表单正文。服务端统一下发点击劫持、MIME 嗅探、Referrer、权限策略和 CSP 安全头,公网 HTTPS 下还会下发 HSTS;HSTS 默认 180 天,可用 `OWQ_HSTS_MAX_AGE_SECONDS` 调整或设为 `0` 关闭。
HTML 响应会输出基础安全头和 CSP;JSON/SVG/CSV/跳转响应会输出 no-store 与 nosniff 等基础安全头。私有、运维、下载和错误响应会额外输出 `X-Robots-Tag: noindex, nofollow`。
未捕获的路由异常会统一返回 500 页面,并写入 `server.error` 审计事件;响应不会暴露异常详情,只会显示可和审计日志对应的错误编号。

管理员访问 `/admin` 可查看系统自检、配置比赛名称/说明、备份应用数据库、查看用户账户概览、查看用户同意记录、暂停/恢复用户、处理内容举报和查看/导出审计日志。
管理页还可以一键生成 demo 参赛账户、持仓、资产快照和策略帖,用于 beta 阶段快速展示公开榜单与论坛传播页。正式生产关闭测试验证入口后默认禁止生成演示比赛数据;`/readyz` 也会提示仍留在公开赛中的演示/开发参赛账户,避免测试战绩混入正式榜单。
未配置 `OWQ_ADMIN_USER_IDS` / `OWQ_ADMIN_EMAILS` 时,仅本地开发默认第一个注册用户为管理员;设置 `OWQ_PUBLIC_BASE_URL`、`OWQ_ENV=production` 或 `OWQ_ENV_PRODUCTION=1` 后不会启用该兜底,生产环境必须显式配置管理员。正式关闭测试验证入口前,还必须确保至少有一个管理员能通过 `OWQ_ADMIN_EMAILS` + 真实发信完成邮箱验证/重置,或已有配置管理员具备用户名/邮箱 + 密码登录,避免后台被锁死。
注册页会在发送验证邮件前要求用户勾选服务条款、隐私说明和风险提示,并把当时的法律版本绑定到邮箱验证会话。系统会保存条款版本、同意时间、来源和 IP。邮箱确认后用户需要设置用户名和密码,再通过 `/login` 登录;密码重置会递增服务端会话版本,使旧 Cookie 失效。被暂停用户仍可登录查看账户和导出个人数据,但不能提交交易、演练计划、参赛提交、发帖、评论或举报。用户可在账户页自助关闭账户,删除登录身份、模拟盘、论坛内容、同意记录和登录会话;安全审计日志保留最小操作记录。用户登录后可以举报帖子和评论;管理员可以在内容举报队列中标记已处理或驳回。审计日志会记录注册/登录、条款同意、交易、演练计划、行情同步、个人数据导出、账户、论坛、备份、举报、管理操作、后台越权访问和 CSRF 失败摘要;不记录密钥,也不记录帖子/评论、表单正文或导出文件完整正文。审计日志默认保留 400 天,可用 `OWQ_AUDIT_RETENTION_DAYS` 调整到 30-3650 天;后台、`--prune-audit-log` 和生产同步脚本可以清理超期记录。邮箱登录临时会话默认保留 30 天,可用 `OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS` 调整到 1-365 天;后台、`--prune-email-login-sessions` 和生产同步脚本可以清理超期 confirmed/expired 会话。

## 行情来源

首次空库会写入一组演示行情,用于没有本地行情库时也能演练交易流程。一旦同步真实行情,后续启动不会再自动补回 demo 标的。

模拟盘成交应使用不复权 `none` 最新收盘价;研究回测可以继续使用后复权 `hfq`。如果已经用 `src.data` 同步过 DuckDB 日线,可以启动时同步真实价格并替换演示行情:

```bash
python3 -m src.app.server --sync-market --market-adjust none --replace-market --sync-only
```

也可以导入 CSV:

```bash
python3 -m src.app.server --market-csv /path/to/market.csv
```

CSV 字段:`code,name,price,prev_close,as_of`。登录后也可以在“基础数据”页面点击“同步行情”,既可以填写本机 CSV 路径,也可以直接粘贴同格式 CSV 内容;勾选“替换现有行情”会清除演示行情。行情同步后会刷新所有账户的资产快照,公开榜单、个人战绩页、模拟成交和组合设计页都会按最新价格展示。生产环境 `/healthz` 会要求至少有一组非 demo 行情,并按 `OWQ_MARKET_MAX_STALENESS_DAYS` 检查 `as_of` 是否过旧;严格发布 `/readyz` 还会按 `OWQ_MARKET_MIN_REAL_CODES` 检查真实标的覆盖度。

生成研究预测候选:

```bash
python3 -m src.research.real_data_report --start 20230101 --adjust hfq --top 20
```

报告写入 `reports/real-data-report.md`,候选写入 `reports/predictions.csv`,组合设计页会读取该 CSV 并按 app 当前不复权价格创建演练计划。生产同步脚本 `deploy/sync-market-public.sh` 默认会在行情同步后刷新报告和预测候选;如需关闭可设置 `OWQ_SYNC_REPORTS=0`。同步脚本会记录 `cli.market_sync_started`、`cli.market_sync_succeeded` 或 `cli.market_sync_failed`,`/readyz` 会按 `OWQ_MARKET_SYNC_MAX_AGE_HOURS` 检查最近一次生产同步任务是否按时成功。

## 生产接入邮箱验证与账号登录

本地开发可以设置 `OWQ_EMAIL_DEV_AUTH=1` 使用页面测试链接验证完整流程;公网默认不会展示测试确认链接,除非显式设置 `OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=1`。公网正式运营前应关闭测试模式,并完成:

1. 可公网访问的 HTTPS `OWQ_PUBLIC_BASE_URL`。
2. 非默认 `OWQ_SECRET`、显式管理员 `OWQ_ADMIN_USER_IDS` 或 `OWQ_ADMIN_EMAILS`,并确保管理员具备可恢复的邮箱/密码登录路径。
3. Cloudflare Email Service REST API:配置 `OWQ_EMAIL_PROVIDER=cloudflare`、`OWQ_EMAIL_FROM`、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_API_TOKEN`。应用会调用 `POST /accounts/{account_id}/email/sending/send`。单独的 Cloudflare Email Routing/邮件转发只解决收信转发,不等于可以给任意报名用户发验证邮件;正式注册确认邮件需要 Email Sending、已验证发信域名和支持出站发信的 Cloudflare 计划。
4. 或 SMTP:配置 `OWQ_EMAIL_PROVIDER=smtp`、`OWQ_EMAIL_FROM`、`OWQ_SMTP_HOST`、`OWQ_SMTP_PORT`、`OWQ_SMTP_USER`、`OWQ_SMTP_PASSWORD`。Cloudflare Email Service SMTP 可使用 `smtp.mx.cloudflare.net`,SSL 465,用户名 `api_token`,密码填 Cloudflare API token。Google/Gmail SMTP 可使用 `smtp.gmail.com`,TLS 587 或 SSL 465,账号填完整 Google 邮箱,密码使用 Google App Password。

`OWQ_EMAIL_FROM` 必须是单个发信邮箱地址,不要填展示名或 `Name <mail@example.com>` 格式;展示名单独放在 `OWQ_EMAIL_FROM_NAME`。如果显式设置 `OWQ_EMAIL_PROVIDER=cloudflare` 或 `smtp`,系统只会校验并使用该 provider,不会在配置不完整时静默降级到另一条发信路径。`/readyz` 会报告缺失项、发信地址格式、SMTP 端口和 TLS/SSL 取值问题。

当前版本已经实现 `/auth/email/confirm` 的一次性邮箱验证流程。邮件链接的 GET 请求只校验 token,写入 15 分钟短期 HttpOnly 确认 Cookie,再跳转到不带 token 的设置页;页面 HTML 不包含原始 token,也不会设置登录 Cookie。用户提交用户名和密码后才会创建或复用邮箱用户、自动加入公开赛、记录法律同意版本并跳转 `/login`;之后必须用用户名/邮箱和密码登录。`/forgot-password` 复用同一套确认页和短期 Cookie,但只会对已存在且已有密码的邮箱账号发送重置链接;未知邮箱只显示泛化结果页,不会创建临时会话或隐式注册。公网/生产默认要求登录用户具备当前 `LEGAL_VERSION` 的条款、隐私和风险同意记录;缺失或旧版本会跳转 `/account/consent` 补签,`OWQ_LEGAL_CONSENT_REQUIRED=0` 只应用于明确的本地调试。链接 token 只保存哈希;发信密钥只从环境变量读取,不会写入仓库。正式发布前还应确保 `demo-*`、`dev-wechat-*` 或“模拟用户”账号没有留在公开赛榜单中。

已登录用户可以在账户页更新用户名和密码;更新后旧会话会失效并要求重新登录。未登录用户可以从 `/forgot-password` 请求一次性重置邮件;提交新密码后同样会让旧会话失效。运维也可以用一次性环境变量给指定用户设置临时密码,命令只记录变量名和用户名,不会把明文密码写入审计日志:

```bash
OWQ_SET_PASSWORD='临时强密码' python3 -m src.app.server --env-file deploy/public.env --set-user-password USER_ID --login-name admin-user
```

验证邮件有邮箱维度的反滥用限制:同一邮箱默认 60 秒内只能请求一次,1 小时最多 5 次;过期的 pending 验证会话会自动标记为 expired。若真实发信失败,系统会删除刚创建的验证会话,避免用户被失败请求占用冷却窗口。已确认或已过期的邮箱登录临时会话只短期留存用于排查和限流,法律同意记录会单独写入 `user_consents`。

配置真实发信服务后,管理员可以在 `/admin` 的“邮件发信诊断”里发送测试邮件,先验证发信服务、DNS 和收件链路,再关闭 `OWQ_EMAIL_DEV_AUTH` 对外开放。
也可以先在命令行验证同一套发信链路:

```bash
python3 -m src.app.server --env-file deploy/public.env --send-test-email you@example.com
```

命令成功会写入 `cli.email_test` 审计事件;失败会返回非 0 并写入 `cli.email_test_failed`,不会输出 API token 或 SMTP 密码。普通报名用户只会看到通用重试/联系管理员提示;后台和 CLI 会记录脱敏后的服务商错误,方便排查域名验证、API 权限、SMTP 认证或收件限制。正式发布闸门还会检查最近一次成功发信诊断是否仍在 `OWQ_EMAIL_TEST_MAX_AGE_HOURS` 内,默认 72 小时。

后台 `/admin` 会把 `security.*`、`server.error`、发信失败和市场同步失败汇总为“安全和异常事件”,显示近 24 小时按类型计数、近 7 天总数和最近事件。原始审计日志仍可在同页查看并导出 CSV,用于进一步排查。

公网运行时 `/healthz` 和 `/readyz` 会把默认密钥、HTTPS 基础地址、Secure Cookie、管理员配置、真实发信服务、近期成功发信诊断和邮箱测试验证模式纳入体检。若要短期公测且暂无发信凭据,可以显式设置 `OWQ_EMAIL_DEV_AUTH=1`,但不应在公网设置 `OWQ_EMAIL_DEV_AUTH_SHOW_LINKS=1`;严格发布检查 `/readyz` 会保留 503,直到真实发信配置完成、成功发送诊断邮件并关闭测试验证。

## 公开传播

- `/showcase/public`:无需登录的公开排行榜。
- `/data-status`:无需登录的公开数据透明页,展示行情来源、最新交易日、预测候选匹配情况和赛场活跃度。
- `/u/<user_id>`:无需登录的个人模拟盘战绩页,展示收益、当前持仓、最近成交、资产曲线和策略分享。
- `/u/<user_id>/card.svg`:无需登录的公开战绩卡,可用于分享或嵌入。
- `/forum` 和 `/forum/<post_id>`:无需登录即可阅读,便于传播策略复盘;列表页支持标签、关键词和最新/战绩/评论数排序筛选。
- `/robots.txt` 和 `/sitemap.xml`:搜索引擎和分享爬虫的公开发现入口;会列出数据透明页,不会列出登录、账户、管理、监控或认证端点。
- 公开榜单、个人页、论坛和帖子页会输出基础 Open Graph/Twitter Card 分享 meta。
- 论坛发帖和评论需要登录;发帖默认附带当时的模拟盘收益、总资产和比赛排名快照,方便围绕结果讨论策略。帖子作者/管理员可以删除帖子,评论作者/管理员可以删除评论。
- 登录后的 `/showcase` 提供“生成战绩复盘帖”,会预填当前排名、账户权益、持仓、最近成交和策略演练计划,用户确认后即可发布到论坛。账户页关闭账户需要输入 `DELETE` 确认。
- `/terms`、`/privacy`、`/risk`、`/legal`:公开服务条款、隐私说明和风险提示。

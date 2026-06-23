# 部署:Cloudflare Tunnel + 反向代理

把 `docs/index.html` 这个静态站点,通过你已有的 **Cloudflare Tunnel + 反代** 发布到二级域名 `quant.ourworlds.app`。

> 需要替换的占位符:`ourworlds.app`、`<TUNNEL_ID_OR_NAME>`、端口 `8088`(可改)、路径 `/srv/ourworld-quant`。

## 架构

```
浏览器  →  Cloudflare 边缘(TLS/CDN)  →  cloudflared 隧道  →  反向代理 / 静态服务器  →  docs/ 静态文件
        quant.ourworlds.app                                  (localhost:8088)
```

TLS 证书由 Cloudflare 边缘自动处理,源站只需提供 HTTP 即可,不必在本地配证书。

---

## 第 1 步:本地把静态站跑起来(三选一)

### 方式 A — Docker + Caddy(最省事,推荐)
见同目录 [`docker-compose.yml`](docker-compose.yml)。官方 caddy 镜像默认就会把 `/usr/share/caddy` 以 file_server 发布在 80 端口:

```bash
cd deploy
docker compose up -d        # 站点在 http://localhost:8088
```

### 方式 B — 已有 Nginx,加一个 server 块
把 [`nginx.conf`](nginx.conf) 的内容并入你的 nginx 配置(或放进 `conf.d/`),`nginx -t && nginx -s reload`。站点在 `http://localhost:8088`。

### 方式 C — 已有 Caddy,加一个站点块
把 [`Caddyfile`](Caddyfile) 的内容并入你的 Caddyfile,reload 即可。

> 把仓库放在服务器上(如 `/srv/ourworld-quant`),让上面的配置指向 `/srv/ourworld-quant/docs`。更新站点 = `git pull`,静态文件即时生效。

---

## 第 2 步:用 Tunnel 暴露子域名(二选一)

### 方式一 — 仪表盘(最简单)
1. 进入 **Cloudflare Zero Trust → Networks → Tunnels**,选中你正在用的隧道。
2. **Public Hostname → Add a public hostname**:
   - Subdomain:`quant`
   - Domain:`ourworlds.app`
   - Service:`HTTP` → `localhost:8088`(若 cloudflared 与站点都在 docker 同一网络,填 `quant-site:80`)
3. 保存。Cloudflare 会**自动创建** `quant` 的 DNS CNAME 记录,无需手动加。

### 方式二 — 配置文件
编辑 `~/.cloudflared/config.yml`,在 `ingress` 顶部加一条(保留你已有的规则和结尾的 404):

```yaml
tunnel: <TUNNEL_ID_OR_NAME>
credentials-file: /home/USER/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: quant.ourworlds.app
    service: http://localhost:8088
  # —— 你已有的其它 hostname 规则放这里 ——
  - service: http_status:404
```

创建 DNS 路由并重启:

```bash
cloudflared tunnel route dns <TUNNEL_ID_OR_NAME> quant.ourworlds.app
sudo systemctl restart cloudflared        # 或 cloudflared tunnel run <NAME>
```

---

## 第 3 步:验证

```bash
curl -I https://quant.ourworlds.app        # 期望 200 OK
```
浏览器打开 `https://quant.ourworlds.app`,应看到站点首页。

---

## 更新工作流

```bash
cd /srv/ourworld-quant && git pull          # 拉取最新(站点/日志更新)
# 静态文件,改完即生效;Docker 方式也无需重启
```

## 备注

- **缓存**:Cloudflare 可能缓存 HTML。改动后若没刷新,在 CF 仪表盘 Caching → Purge 一下,或开发期对该子域名设 Cache Rule 为 Bypass。
- **安全**:源站只暴露给隧道、不开公网端口;反代/容器只监听 `127.0.0.1:8088` 更稳妥。
- **合规**:经 Cloudflare 隧道发布、源站隐藏;若你的域名/业务面向国内且涉及备案要求,请按自身情况确认(纯静态个人项目通常风险低,但以实际为准)。

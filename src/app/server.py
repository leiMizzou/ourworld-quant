"""Stdlib HTTP server for the local paper-trading community MVP.

Run from the repository root:
    python3 -m src.app.server --host 127.0.0.1 --port 8081
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import shlex
import signal
import smtplib
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from . import data_bridge, db, doctor, email_config, services
from .ai import service as ai_service
from ..metrics_glossary import METRIC_GLOSSARY, tooltip_text


SESSION_COOKIE = "owq_session"
EMAIL_CONFIRM_COOKIE = "owq_email_confirm"
DEFAULT_SECRET = "local-dev-secret-change-me"
DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
DEFAULT_EMAIL_CONFIRM_COOKIE_SECONDS = 60 * 15
SECRET = os.getenv("OWQ_SECRET", DEFAULT_SECRET)
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
RATE_LIMIT_BUCKETS: dict[tuple[str, str], list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()
# Serializes all state-changing (POST) request handling. Several service writes do a
# read-modify-write (e.g. place_order reads holdings.qty then writes the new absolute
# qty), which separate per-request connections + WAL do NOT make atomic — concurrent
# writers would lose updates. Holding this lock for the duration of each POST keeps the
# whole read-modify-write-commit sequence atomic across worker threads.
DB_WRITE_LOCK = threading.RLock()
SERVER_STARTED_AT = time.time()
METRICS_LOCK = threading.Lock()
HTTP_METRICS = {
    "requests_total": 0,
    "responses_total": 0,
    "in_flight": 0,
    "errors_total": 0,
    "duration_total_ms": 0.0,
    "duration_max_ms": 0.0,
    "by_method": {},
    "by_status": {},
    "by_status_class": {},
    "last_request_at": "",
}
LEGAL_VERSION = "2026-06-24"
DEFAULT_MAX_FORM_BYTES = 1024 * 1024
DEFAULT_HSTS_MAX_AGE_SECONDS = 60 * 60 * 24 * 180
SHUTDOWN_SIGNALS = ("SIGTERM",)
SENSITIVE_ENV_NAMES = (
    "OWQ_SECRET",
    "CLOUDFLARE_API_TOKEN",
    "OWQ_SMTP_PASSWORD",
    "WECHAT_APP_SECRET",
    "TUSHARE_TOKEN",
    "OWQ_DEEPSEEK_API_KEY",
)
DEFAULT_DEMO_TTS_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_DEMO_VOICE_PATH = db.REPO_ROOT / "data" / "demo" / "ourworld-quant-guide.mp3"
STATIC_DIR = db.REPO_ROOT / "src" / "app" / "static"
USAGE_FLOW_STEPS = (
    {
        "title": "公开了解",
        "path": "/",
        "summary": "先看首页、公开榜单、数据透明页和论坛,确认这是模拟盘训练系统。",
        "detail": "未登录用户可以查看赛场状态、行情覆盖、公开战绩和策略复盘,但不能提交交易或发帖。",
    },
    {
        "title": "邮箱注册",
        "path": "/register",
        "summary": "填写邮箱并同意条款,收到注册码后到确认页设置用户名和密码。",
        "detail": "注册码和备用链接 15 分钟内有效。完成后仍需回到登录页使用账号密码登录。",
    },
    {
        "title": "登录进入模拟盘",
        "path": "/login",
        "summary": "使用用户名或邮箱加密码进入模拟盘,系统自动创建 100 万模拟资金账户。",
        "detail": "登录后可以查看总资产、现金、收益率、持仓、行情和最近成交。",
    },
    {
        "title": "制定演练计划",
        "path": "/app",
        "summary": "先保存策略演练计划,再逐条或批量执行为模拟成交。",
        "detail": "可以手动选择标的,也可以从行情反转/动量候选或研究篮子导入待执行计划。",
    },
    {
        "title": "组合与数据",
        "path": "/portfolio-lab",
        "summary": "组合设计页读取真实行情和预测候选,把研究结果转成可执行演练。",
        "detail": "基础数据页负责同步行情,数据透明页公开展示当前行情覆盖、最新交易日和预测匹配状态。",
    },
    {
        "title": "公开展示和讨论",
        "path": "/showcase",
        "summary": "加入公开赛后,榜单、个人战绩页和战绩卡会展示模拟盘结果。",
        "detail": "可以从比赛页生成战绩复盘帖,再到论坛围绕策略、回撤和执行偏差讨论。",
    },
    {
        "title": "账户和运维",
        "path": "/account",
        "summary": "账户页负责资料、导出、重置模拟账户和关闭账户;管理员在后台处理系统运维。",
        "detail": "管理员可备份数据库、查看体检、处理举报和支持请求,正式发布前必须完成真实发信和严格体检。",
    },
)
USAGE_GAPS = (
    "新用户以前需要在多个页面之间猜路径,注册、确认、登录、模拟盘和公开赛关系不够集中。",
    "数据状态、组合设计和模拟交易虽然已经打通,但缺少一页把“先看数据、再生成计划、再执行复盘”的闭环讲清楚。",
    "演示主要依赖管理员生成 demo 数据,普通访客无法在不登录的情况下快速理解完整操作。",
    "没有语音解说入口,对录屏、路演和非技术用户讲解不够友好。",
)
USAGE_IMPROVEMENTS = (
    "新增公开使用指南,把访客、注册用户、参赛用户和管理员的路径合并到一页。",
    "新增自动演示页,用纯 HTML/CSS 自动轮播核心步骤,在当前安全 CSP 下不启用脚本。",
    "新增 EdgeTTS 语音生成命令,可把固定演示文案生成 MP3 并由演示页播放。",
    "导航、首页入口和 sitemap 增加指南/演示入口,降低新用户第一次使用的路径成本。",
)
DEMO_NARRATION_TEXT = (
    "欢迎使用 OurWorlds Quant 模拟盘。第一步,先通过首页、公开榜单、数据透明页和论坛了解赛场。"
    "第二步,使用邮箱注册。系统会发送一次性注册码,确认邮箱后设置用户名和密码。"
    "第三步,回到登录页,使用用户名或邮箱和密码进入模拟盘。"
    "第四步,在模拟盘里先保存策略演练计划,再把计划执行成模拟成交。"
    "第五步,到组合设计页使用真实行情和研究预测候选,把研究结果转成待执行计划。"
    "第六步,加入公开赛,查看排名、个人战绩页和战绩卡。"
    "第七步,把战绩复盘发布到论坛,围绕策略逻辑、执行偏差和风险控制继续讨论。"
    "所有交易都是模拟训练,不构成投资建议,也不产生真实证券委托。"
)
LEARNING_PRESETS = (
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "reversal",
        "title": "量化投资到底是什么?",
        "summary": "先弄清楚数据、规则、策略和模拟盘分别在做什么。",
        "goal": "我是零基础,想先理解量化投资到底是什么,它和主观投资、AI聊天选股有什么区别,并用一个最简单的模拟盘练习建立基本概念。",
    },
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "reversal",
        "title": "我该先学哪些基础?",
        "summary": "按最短路径梳理行情、交易规则、回测陷阱和复盘。",
        "goal": "我是新手,想知道学习量化投资最先应该掌握哪些基础知识,每个知识点为什么重要,以及如何用模拟盘做一次安全的小练习。",
    },
    {
        "level": "零基础",
        "difficulty": "beginner",
        "template": "momentum",
        "title": "AI 能帮我做什么?",
        "summary": "理解 AI 适合做拆解、解释、记录,不适合替你下判断。",
        "goal": "我想学习 AI 在量化投资学习中能帮我做什么,哪些事情不能让 AI 替我决定,并设计一个用 AI 辅助记录和复盘的入门练习。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "reversal",
        "title": "做一次反转观察",
        "summary": "观察短期跌幅靠前的候选,学习假设、仓位和复盘记录。",
        "goal": "我想通过一次反转观察练习,学习什么是策略假设、候选筛选、仓位控制和复盘记录,不要追求收益,重点学习如何验证想法。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "momentum",
        "title": "做一次动量观察",
        "summary": "观察短期强势候选,理解趋势、回撤和过拟合风险。",
        "goal": "我想做一次动量观察练习,学习如何看趋势信号、如何避免追涨冲动、如何设置观察指标和复盘问题。",
    },
    {
        "level": "入门练习",
        "difficulty": "balanced",
        "template": "prediction",
        "title": "理解模型预测候选",
        "summary": "把预测结果当成学习材料,而不是买卖指令。",
        "goal": "我想学习如何理解模型预测候选,知道预测值、行情、风险记录和模拟盘验证之间是什么关系,并设计一次只用于学习的预测候选观察练习。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "risk_review",
        "title": "如何控制风险?",
        "summary": "从仓位、回撤、交易成本和停止条件建立风险框架。",
        "goal": "我想系统学习量化练习里的风险控制,包括仓位、回撤、交易成本、停止继续练习的条件,并形成一套可以复盘的检查清单。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "risk_review",
        "title": "如何复盘模拟盘?",
        "summary": "把成交、持仓和演练依据转成可学习的复盘问题。",
        "goal": "我已经知道模拟盘只是训练,想学习如何复盘自己的成交、持仓、演练依据和结果,找出方法上的问题而不是只看赚亏。",
    },
    {
        "level": "进阶复盘",
        "difficulty": "advanced",
        "template": "prediction",
        "title": "怎么避免过拟合?",
        "summary": "学习样本内外、未来函数、幸存者偏差和成本低估。",
        "goal": "我想学习量化研究里常见的过拟合和回测陷阱,包括未来函数、幸存者偏差、样本内外和成本低估,并设计一个模拟盘层面的检查练习。",
    },
)


CSS = """
:root{color-scheme:light;--ink:#101217;--muted:#59616f;--soft:#eef1f5;--paper:#f7f8fa;--panel:#ffffff;--line:#d8dee8;--blue:#1d4ed8;--green:#087f5b;--amber:#b45309;--red:#b91c1c}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:'Space Grotesk','Noto Sans SC',-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}a:hover{text-decoration:underline}
h1,h2,h3,p{letter-spacing:0}p{margin:0;color:var(--muted)}
.wrap{max-width:1180px;margin:0 auto;padding:0 36px 36px}
.top{display:flex;align-items:center;justify-content:space-between;gap:20px;border-bottom:2px solid var(--ink);padding:24px 0;margin-bottom:28px}
.brand{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:800;letter-spacing:0;color:var(--ink)}
.brand::before{content:"";width:12px;height:12px;background:var(--blue);display:inline-block;flex:0 0 auto}
.nav{display:flex;gap:22px;align-items:center;flex-wrap:wrap;font-size:14px;color:var(--muted)}
.nav a,.nav button{color:var(--muted)}.nav a:hover,.nav button:hover{color:var(--ink)}
.nav span{color:var(--ink);font-weight:700}.nav form{margin:0}
.nav .primary{background:var(--ink);color:#fff;border:1px solid var(--ink);border-radius:7px;padding:8px 14px;font-weight:700}
.grid{display:grid;grid-template-columns:1.3fr .9fr;gap:16px;margin-bottom:16px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);padding:22px;border-radius:8px;margin-bottom:16px;overflow-x:auto}.cards .card,.grid .card,.live-grid .card{margin-bottom:0}
.card h2,.card h3{margin:0 0 12px;font-weight:800;line-height:1.15;color:var(--ink)}.card h2{font-size:22px}.card h3{font-size:18px}
.card p{margin-top:10px;color:var(--muted)}.card>p:first-child{margin-top:0}
.card a:not(.btn):not(.link-tile),td a,.post a,.mini-post a,.rank-row a{color:var(--blue);font-weight:600}
.metric{font-size:32px;font-weight:800;line-height:1.05;color:var(--ink);word-break:break-word}.metric strong{display:block;font-size:clamp(26px,4vw,42px);line-height:1;font-weight:800}.metric span{display:block;margin-top:8px;font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--muted)}
.identity .metric{font-size:22px}.muted{color:var(--muted)}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}
[data-equity-curve] svg{width:100%;height:auto;display:block;overflow:visible}
.provenance{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 18px;font-size:12px}.provenance b{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border:1px solid var(--line);border-radius:999px;font-weight:600;color:var(--ink)}.provenance b.real{color:var(--green);border-color:var(--green)}.provenance b.demo{color:var(--amber);border-color:var(--amber)}
.metric-info{border-bottom:1px dashed var(--muted);cursor:help}.metric-info:focus-visible{outline:2px solid var(--blue);outline-offset:2px}.metric-info[aria-expanded=true]{color:var(--blue);border-bottom-color:var(--blue)}
.owq-tip{position:absolute;z-index:60;max-width:320px;background:var(--ink);color:#fff;padding:13px 15px;border-radius:9px;font-size:13px;line-height:1.55;box-shadow:0 10px 30px rgba(0,0,0,.28)}.owq-tip h4{margin:0 0 4px;font-size:13px;color:#fff}.owq-tip .owq-tip-f{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#cbd5e1;margin-top:7px;word-break:break-word}.owq-tip .owq-tip-b{margin-top:9px;color:#fde68a}
.badge,.pill{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:3px 8px;font-size:12px;font-weight:700;background:#fff;color:var(--muted);white-space:nowrap}
.card-title{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px;font-size:13px;color:var(--muted)}
.flow-map{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin:16px 0}.flow-step{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px}.flow-step span{display:inline-block;color:var(--blue);font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:8px}.flow-step strong{display:block;font-size:18px;line-height:1.2;margin-bottom:8px}.flow-step p{margin:0 0 10px}.flow-step a{font-weight:700;color:var(--blue)}
.preset-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:16px 0}.preset-form{margin:0}.preset-card{display:block;width:100%;min-height:178px;text-align:left;background:#fff;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:18px;white-space:normal}.preset-card:hover{border-color:var(--ink);background:#fbfcfe}.preset-card strong{display:block;font-size:18px;line-height:1.2;margin:8px 0}.preset-card span{display:inline-flex;margin-right:6px}.preset-card p{margin:8px 0 0;color:var(--muted);font-weight:400}
.markdown-body{background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px;overflow:auto}.markdown-body h3,.markdown-body h4{margin:18px 0 8px;font-weight:800;line-height:1.2}.markdown-body h3:first-child,.markdown-body h4:first-child{margin-top:0}.markdown-body p{margin:10px 0;color:var(--ink)}.markdown-body ul,.markdown-body ol{margin:10px 0 10px 22px;padding:0;color:var(--ink)}.markdown-body li{margin:6px 0}.markdown-body code{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;background:var(--soft);border:1px solid var(--line);border-radius:5px;padding:1px 5px}.markdown-body strong{font-weight:800}
.guide-list{margin:0;padding-left:20px;color:var(--muted)}.guide-list li{margin:7px 0}
.demo-board{display:grid;grid-template-columns:1fr .8fr;gap:18px;align-items:stretch}.demo-stage{display:grid;position:relative;min-height:360px;overflow:hidden;background:#fff;border:1px solid var(--line);border-radius:8px}.demo-frame{grid-area:1/1;padding:22px;opacity:0;transform:translateY(10px);animation:demo-frame 49s infinite}.demo-frame:nth-child(1){animation-delay:0s}.demo-frame:nth-child(2){animation-delay:7s}.demo-frame:nth-child(3){animation-delay:14s}.demo-frame:nth-child(4){animation-delay:21s}.demo-frame:nth-child(5){animation-delay:28s}.demo-frame:nth-child(6){animation-delay:35s}.demo-frame:nth-child(7){animation-delay:42s}.demo-frame h3{font-size:22px;margin:0 0 8px}.demo-path{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:4px 10px;background:#fff;font-weight:700;color:var(--blue)}.demo-screen{margin-top:16px;border:1px solid var(--line);border-radius:8px;padding:14px;background:var(--paper)}.demo-screen .bar{height:9px;border-radius:999px;background:var(--blue);margin:10px 0}.demo-progress{height:8px;background:#fff;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:12px 0}.demo-progress span{display:block;height:100%;background:var(--blue);animation:demo-progress 49s linear infinite}.demo-steps{display:grid;gap:8px}.demo-steps a{display:block;border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:#fff;color:var(--ink)}.voice-box audio{width:100%;margin:8px 0}.voice-command{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto}
@keyframes demo-frame{0%,12%{opacity:1;transform:translateY(0)}14%,100%{opacity:0;transform:translateY(10px)}}@keyframes demo-progress{from{width:0}to{width:100%}}
table{width:100%;border-collapse:collapse;background:transparent}th,td{text-align:left;border-bottom:1px solid var(--line);padding:10px 8px;vertical-align:top}th{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-weight:500}tr:hover td{background:#fbfcfe}
input,select,textarea,button{font:inherit}label{display:block;margin:12px 0 6px;font-weight:700;color:var(--ink)}input,select,textarea{width:100%;min-height:42px;border:1px solid var(--line);background:#fff;border-radius:7px;padding:9px 10px;color:var(--ink)}textarea{min-height:150px;resize:vertical}input[type=checkbox],input[type=radio]{width:auto;min-height:auto}input:focus,select:focus,textarea:focus{outline:2px solid rgba(29,78,216,.18);border-color:var(--blue)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.formline{display:grid;grid-template-columns:1.1fr .8fr .8fr auto;gap:12px;align-items:end}
td form{display:flex;align-items:center;gap:8px;flex-wrap:wrap}td form input:not([type=hidden]),td form select{width:auto;min-width:150px;flex:1 1 150px}
button,.btn{display:inline-flex;align-items:center;justify-content:center;border:1.5px solid var(--ink);background:var(--ink);color:#fff;border-radius:7px;padding:10px 15px;min-height:42px;font-weight:700;cursor:pointer;text-decoration:none;white-space:nowrap}.btn:hover,button:hover{text-decoration:none}.btn.blue{background:var(--blue);border-color:var(--blue);color:#fff}.btn.dark{background:var(--ink);color:#fff}.btn.secondary,button.secondary{background:transparent;color:var(--ink);border-color:var(--ink)}
.nav button{border:0;background:transparent;color:var(--muted);padding:0;min-height:auto;font-weight:500}.nav button:hover{color:var(--ink)}
.msg{border:1px solid #bfdbfe;background:#eff6ff;color:#1e3a8a;padding:11px 13px;border-radius:8px;margin-bottom:16px}.err{border-color:#fecaca;background:#fff1f2;color:#991b1b}
.qr{display:grid;grid-template-columns:220px 1fr;gap:22px;align-items:center}.qr img{width:220px;height:220px;border:1px solid var(--line);background:#fff;padding:10px;border-radius:8px}
.post{border-top:1px solid var(--line);padding:14px 0}.tag{display:inline-flex;align-items:center;font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--blue);letter-spacing:1px;text-transform:uppercase}
.avatar{width:56px;height:56px;border-radius:50%;object-fit:cover;border:1px solid var(--line);background:#fff}.identity{display:flex;align-items:center;gap:12px}
.rank-list,.post-list{display:grid;gap:10px;margin-top:16px}.rank-row{display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:center;border-top:1px solid var(--line);padding:12px 0}.rank-row span{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--blue)}.rank-row strong{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--green)}
.mini-post{border-top:1px solid var(--line);padding:12px 0}.mini-post strong{display:block;margin-bottom:6px}.mini-post p{margin:0 0 8px;font-size:14px}.mini-post span{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--muted)}
.data-proof{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:16px 0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:20px 22px}.data-proof strong{display:block;font-size:22px;line-height:1.2;margin:6px 0}.data-proof p{max-width:72ch}
.link-tile{display:block;background:#fff;border:1px solid var(--line);border-radius:8px;padding:18px;min-height:120px;color:var(--ink)}.link-tile strong{display:block;margin-bottom:8px}.link-tile p{margin-top:0}
.live-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:16px;align-items:start}
.score{background:#fff;padding:18px 20px;min-height:106px}.score b{display:block;font-size:30px;line-height:1;color:var(--ink);margin-bottom:8px}.score span{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;text-transform:uppercase;color:var(--muted);letter-spacing:1px}
.step{background:var(--ink);color:#fff;border-radius:8px;padding:22px;min-height:178px}.step p{color:#d7dce5;margin-top:12px}.step span{font-family:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#8fb4ff}
.footer{border-top:2px solid var(--ink);margin-top:76px;padding:30px 0 58px;color:var(--muted);display:flex;justify-content:space-between;gap:24px;flex-wrap:wrap;font-size:13px}
@media(prefers-reduced-motion:reduce){.demo-frame,.demo-progress span{animation:none}.demo-frame{position:static;opacity:1;transform:none}.demo-stage{display:block}}
@media(max-width:880px){.grid,.cards,.qr,.formline,.row,.flow-map,.preset-grid,.demo-board,.live-grid,.data-proof{grid-template-columns:1fr}.wrap{padding:0 20px 28px}.top{align-items:flex-start;gap:12px;flex-direction:column}.data-proof{align-items:flex-start;flex-direction:column}.nav{gap:14px 18px}}
@media(max-width:560px){.card{padding:18px}.metric{font-size:28px}.rank-row{grid-template-columns:40px 1fr}.score{min-height:auto}}
"""


def markdown_inline(text: str) -> str:
    parts = str(text or "").split("`")
    rendered: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2:
            rendered.append(f"<code>{escape(part)}</code>")
            continue
        safe = escape(part)
        safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
        rendered.append(safe)
    return "".join(rendered)


def render_markdown(text: str) -> str:
    """Render a small safe Markdown subset produced by the AI coach."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    html: list[str] = []
    list_type = ""

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            html.append(f"</{list_type}>")
            list_type = ""

    for raw in lines:
        line = raw.strip()
        if not line:
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            close_list()
            level = 3 if len(heading.group(1)) <= 2 else 4
            html.append(f"<h{level}>{markdown_inline(heading.group(2))}</h{level}>")
            continue
        numbered = re.match(r"^\d+[\.)]\s+(.+)$", line)
        if numbered:
            if list_type != "ol":
                close_list()
                html.append("<ol>")
                list_type = "ol"
            html.append(f"<li>{markdown_inline(numbered.group(1))}</li>")
            continue
        bulleted = re.match(r"^[-*]\s+(.+)$", line)
        if bulleted:
            if list_type != "ul":
                close_list()
                html.append("<ul>")
                list_type = "ul"
            html.append(f"<li>{markdown_inline(bulleted.group(1))}</li>")
            continue
        close_list()
        html.append(f"<p>{markdown_inline(line)}</p>")
    close_list()
    return "".join(html) or '<p class="muted">暂无内容</p>'


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def sanitize_diagnostic_message(value: object, limit: int = 220) -> str:
    text = str(value)
    for name in SENSITIVE_ENV_NAMES:
        secret = os.getenv(name, "")
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    text = " ".join(text.split())
    return text[:limit]


def exception_diagnostic(exc: Exception, limit: int = 220) -> dict:
    return {"error": type(exc).__name__, "message": sanitize_diagnostic_message(exc, limit=limit)}


def email_audit_metadata(email: str) -> tuple[str, dict[str, str]]:
    normalized = services.normalize_email(email)
    digest = services.email_token_hash(normalized)[:16]
    domain = normalized.rsplit("@", 1)[-1]
    return digest, {"recipient_hash": digest, "recipient_domain": domain}


def email_public_failure_message() -> str:
    return "登录邮件暂时发送失败,请稍后重试或联系管理员。"


def max_form_bytes() -> int:
    raw = os.getenv("OWQ_MAX_FORM_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_FORM_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_FORM_BYTES
    return max(4096, min(value, 5 * 1024 * 1024))


def usage_demo_voice_path(path: str | Path | None = None) -> Path:
    raw = str(path or "").strip() or os.getenv("OWQ_DEMO_VOICE_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_DEMO_VOICE_PATH


def edge_tts_command() -> list[str]:
    configured = os.getenv("OWQ_EDGE_TTS_BIN", "edge-tts").strip() or "edge-tts"
    resolved = shutil.which(configured)
    if resolved:
        return [resolved]
    configured_path = Path(configured).expanduser()
    if configured_path.exists():
        return [str(configured_path)]
    if importlib.util.find_spec("edge_tts") is not None:
        return [sys.executable, "-m", "edge_tts"]
    raise RuntimeError("未找到 edge-tts。请先安装 edge-tts 或设置 OWQ_EDGE_TTS_BIN。")


def generate_usage_demo_voice(path: str | Path | None = None, voice: str | None = None) -> Path:
    output = usage_demo_voice_path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    selected_voice = (voice or os.getenv("OWQ_DEMO_TTS_VOICE", "") or DEFAULT_DEMO_TTS_VOICE).strip()
    rate = os.getenv("OWQ_DEMO_TTS_RATE", "+0%").strip()
    cmd = edge_tts_command() + [
        "--voice",
        selected_voice,
        "--text",
        DEMO_NARRATION_TEXT,
        "--write-media",
        str(tmp),
    ]
    if rate:
        cmd.extend(["--rate", rate])
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180)
    except subprocess.CalledProcessError as exc:
        detail = sanitize_diagnostic_message((exc.stderr or exc.stdout or str(exc)), limit=300)
        raise RuntimeError(f"EdgeTTS 生成失败: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("EdgeTTS 生成超时。") from exc
    if not tmp.exists() or tmp.stat().st_size <= 0:
        raise RuntimeError("EdgeTTS 未生成有效音频文件。")
    tmp.replace(output)
    return output


def hsts_max_age_seconds() -> int:
    raw = os.getenv("OWQ_HSTS_MAX_AGE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_HSTS_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_HSTS_MAX_AGE_SECONDS
    return max(0, min(value, 60 * 60 * 24 * 365 * 2))


class RequestBodyTooLarge(ValueError):
    pass


def raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def install_shutdown_signal_handlers():
    installed = []
    for name in SHUTDOWN_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, raise_keyboard_interrupt)
        except (OSError, ValueError):
            continue
        installed.append((sig, previous))
    return installed


def restore_signal_handlers(installed) -> None:
    for sig, previous in reversed(installed):
        try:
            signal.signal(sig, previous)
        except (OSError, ValueError):
            pass


def load_env_file(path: str | os.PathLike | None) -> dict[str, str]:
    """Load simple KEY=VALUE env files without executing shell code."""
    if not path:
        return {}
    env_path = str(path).strip()
    if not env_path:
        return {}
    loaded: dict[str, str] = {}
    with open(env_path, encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                raise ValueError(f"{env_path}:{lineno}: env 行必须是 KEY=VALUE")
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                raise ValueError(f"{env_path}:{lineno}: env 变量名无效")
            value = value.strip()
            if value:
                parts = shlex.split(value, comments=False, posix=True)
                if len(parts) > 1:
                    raise ValueError(f"{env_path}:{lineno}: 包含空格的值需要用引号包裹")
                value = parts[0] if parts else ""
            os.environ[key] = value
            loaded[key] = value
    refresh_runtime_secret()
    return loaded


def refresh_runtime_secret() -> None:
    global SECRET
    if not os.getenv("OWQ_SECRET", "").strip():
        secret_file = os.getenv("OWQ_SECRET_FILE", "").strip()
        if secret_file:
            try:
                secret = Path(secret_file).read_text(encoding="utf-8").strip()
            except OSError:
                secret = ""
            if secret:
                os.environ["OWQ_SECRET"] = secret
    SECRET = os.getenv("OWQ_SECRET", DEFAULT_SECRET)


def iso_timestamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def reset_http_metrics() -> None:
    with METRICS_LOCK:
        HTTP_METRICS.update(
            {
                "requests_total": 0,
                "responses_total": 0,
                "in_flight": 0,
                "errors_total": 0,
                "duration_total_ms": 0.0,
                "duration_max_ms": 0.0,
                "by_method": {},
                "by_status": {},
                "by_status_class": {},
                "last_request_at": "",
            }
        )


def metrics_request_started() -> None:
    with METRICS_LOCK:
        HTTP_METRICS["in_flight"] = int(HTTP_METRICS["in_flight"]) + 1


def metrics_request_finished(method: str, status: int, duration_ms: float) -> None:
    status = int(status or 0)
    method = (method or "UNKNOWN").upper()
    status_key = str(status)
    class_key = f"{status // 100}xx" if status >= 100 else "unknown"
    with METRICS_LOCK:
        by_method = dict(HTTP_METRICS["by_method"])
        by_status = dict(HTTP_METRICS["by_status"])
        by_status_class = dict(HTTP_METRICS["by_status_class"])
        by_method[method] = int(by_method.get(method, 0)) + 1
        by_status[status_key] = int(by_status.get(status_key, 0)) + 1
        by_status_class[class_key] = int(by_status_class.get(class_key, 0)) + 1
        HTTP_METRICS["requests_total"] = int(HTTP_METRICS["requests_total"]) + 1
        HTTP_METRICS["responses_total"] = int(HTTP_METRICS["responses_total"]) + 1
        HTTP_METRICS["in_flight"] = max(0, int(HTTP_METRICS["in_flight"]) - 1)
        HTTP_METRICS["errors_total"] = int(HTTP_METRICS["errors_total"]) + (1 if status >= 500 else 0)
        HTTP_METRICS["duration_total_ms"] = float(HTTP_METRICS["duration_total_ms"]) + max(0.0, duration_ms)
        HTTP_METRICS["duration_max_ms"] = max(float(HTTP_METRICS["duration_max_ms"]), max(0.0, duration_ms))
        HTTP_METRICS["by_method"] = by_method
        HTTP_METRICS["by_status"] = by_status
        HTTP_METRICS["by_status_class"] = by_status_class
        HTTP_METRICS["last_request_at"] = iso_timestamp()


def metrics_snapshot() -> dict:
    with METRICS_LOCK:
        requests_total = int(HTTP_METRICS["requests_total"])
        duration_total = float(HTTP_METRICS["duration_total_ms"])
        return {
            "status": "ok",
            "started_at": iso_timestamp(SERVER_STARTED_AT),
            "uptime_seconds": int(time.time() - SERVER_STARTED_AT),
            "requests_total": requests_total,
            "responses_total": int(HTTP_METRICS["responses_total"]),
            "in_flight": int(HTTP_METRICS["in_flight"]),
            "errors_total": int(HTTP_METRICS["errors_total"]),
            "avg_duration_ms": round(duration_total / requests_total, 3) if requests_total else 0.0,
            "max_duration_ms": round(float(HTTP_METRICS["duration_max_ms"]), 3),
            "by_method": dict(HTTP_METRICS["by_method"]),
            "by_status": dict(HTTP_METRICS["by_status"]),
            "by_status_class": dict(HTTP_METRICS["by_status_class"]),
            "last_request_at": str(HTTP_METRICS["last_request_at"]),
        }


def session_ttl_seconds() -> int:
    raw = os.getenv("OWQ_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS
    return max(300, min(ttl, 60 * 60 * 24 * 365))


def sign_user(user_id: int, ttl_seconds: int | None = None, session_version: int = 1) -> str:
    ttl = session_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    expires_at = int(time.time()) + max(1, ttl)
    version = max(1, int(session_version or 1))
    msg = f"v3:{int(user_id)}:{expires_at}:{version}".encode()
    sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"v3:{int(user_id)}:{expires_at}:{version}:{sig}"


def verify_session_cookie(value: str | None) -> dict[str, int | bool] | None:
    if not value or ":" not in value:
        return None
    parts = value.split(":")
    if len(parts) == 5 and parts[0] == "v3":
        _, raw_id, raw_expires, raw_version, sig = parts
        if not (raw_id.isdigit() and raw_expires.isdigit() and raw_version.isdigit()):
            return None
        if int(raw_expires) < int(time.time()):
            return None
        user_id = int(raw_id)
        session_version = max(1, int(raw_version))
        msg = f"v3:{user_id}:{int(raw_expires)}:{session_version}".encode()
        expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return {"user_id": user_id, "session_version": session_version, "legacy": False}
    if len(parts) == 4 and parts[0] == "v2":
        _, raw_id, raw_expires, sig = parts
        if not (raw_id.isdigit() and raw_expires.isdigit()):
            return None
        if int(raw_expires) < int(time.time()):
            return None
        msg = f"v2:{int(raw_id)}:{int(raw_expires)}".encode()
        expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return {"user_id": int(raw_id), "session_version": 1, "legacy": True}
    if len(parts) == 2:
        raw_id, sig = parts
        if not raw_id.isdigit():
            return None
        expected = hmac.new(SECRET.encode(), raw_id.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return {"user_id": int(raw_id), "session_version": 1, "legacy": True}
    return None


def verify_cookie(value: str | None) -> int | None:
    session = verify_session_cookie(value)
    if not session:
        return None
    return int(session["user_id"])


def sign_email_confirm_token(token: str, ttl_seconds: int = DEFAULT_EMAIL_CONFIRM_COOKIE_SECONDS) -> str:
    expires_at = int(time.time()) + max(1, int(ttl_seconds))
    token = str(token or "")
    msg = f"email-confirm:v1:{expires_at}:{token}".encode()
    sig = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"v1:{expires_at}:{token}:{sig}"


def verify_email_confirm_cookie(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) < 4 or parts[0] != "v1":
        return None
    raw_expires = parts[1]
    sig = parts[-1]
    token = ":".join(parts[2:-1])
    if not raw_expires.isdigit() or int(raw_expires) < int(time.time()) or not token:
        return None
    msg = f"email-confirm:v1:{int(raw_expires)}:{token}".encode()
    expected = hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return token


def csrf_token(user_id: int) -> str:
    msg = f"csrf:{user_id}".encode()
    return hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()


def verify_csrf(user_id: int, token: str | None) -> bool:
    return hmac.compare_digest(csrf_token(user_id), token or "")


def csrf_input(user) -> str:
    return f'<input type="hidden" name="csrf" value="{csrf_token(int(user["id"]))}">'


def money(value: float) -> str:
    return f"{value:,.2f}"


def pct(value: float) -> str:
    if abs(value) < 0.005:
        value = 0.0
    return f"{value:+.2f}%"


def side_cn(side: str) -> str:
    return "买入" if side == "buy" else "卖出"


def signal_status_cn(status: str) -> str:
    return {"pending": "待执行", "executed": "已执行", "cancelled": "已取消"}.get(status, status)


def preview_equity_svg(points: list) -> str:
    """Server-rendered equity-curve SVG for the public /preview page (works with NO JS)."""
    pts = [p for p in points if p.get("equity") is not None]
    if len(pts) < 2:
        return ""
    W, H, pad = 640, 220, 32
    eq = [float(p["equity"]) for p in pts]
    lo, hi = min(eq), max(eq)
    if hi == lo:
        hi = lo + 1.0
    n = len(eq)
    px = lambda i: pad + i * (W - 2 * pad) / (n - 1)  # noqa: E731
    py = lambda v: H - pad - (v - lo) / (hi - lo) * (H - 2 * pad)  # noqa: E731
    peak, cur, dd_peak, dd_trough, worst = eq[0], 0, 0, 0, 0.0
    for i, v in enumerate(eq):
        if v > peak:
            peak, cur = v, i
        drop = v / peak - 1
        if drop < worst:
            worst, dd_trough, dd_peak = drop, i, cur
    base = eq[0]
    poly = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(eq))
    stroke = "var(--green)" if eq[-1] >= base else "var(--red)"
    band = ""
    if worst < -0.0001 and dd_trough > dd_peak:
        band = (
            f'<rect x="{px(dd_peak):.1f}" y="{pad}" width="{px(dd_trough) - px(dd_peak):.1f}" '
            f'height="{H - 2 * pad}" fill="rgba(220,38,38,0.10)"></rect>'
        )
    baseline = (
        f'<line x1="{pad}" y1="{py(base):.1f}" x2="{W - pad}" y2="{py(base):.1f}" '
        'stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1" opacity="0.6"></line>'
    )
    return (
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="真实回测净值曲线" '
        'style="width:100%;height:auto;display:block;margin-top:8px">'
        f"{band}{baseline}"
        f'<polyline fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round" points="{poly}"></polyline>'
        "</svg>"
    )


def metric_label(key: str, text: str) -> str:
    """Render a metric label that teaches what the number means.

    The ``title`` attribute is the no-JS fallback (it shows the plain-language definition on
    hover/long-press even with scripts disabled); ``app.js`` upgrades any ``[data-metric]``
    node into a tap/focus rich tooltip sourced from ``/api/glossary``. Falls back to the bare
    escaped label if the key is unknown, so a typo can never blank out a heading.
    """
    info = METRIC_GLOSSARY.get(key)
    if not info:
        return escape(text)
    return (
        f'<span class="metric-info" data-metric="{escape(key)}" '
        f'title="{escape(tooltip_text(key))}" tabindex="0" role="button" '
        f'aria-label="{escape(text)} — 点击查看含义">{escape(text)}</span>'
    )


def avatar_html(user, size: int = 56) -> str:
    url = str(user["avatar_url"] or "").strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        return ""
    return f'<img class="avatar" src="{escape(url)}" alt="{escape(display_nickname(user))}" width="{size}" height="{size}">'


def display_nickname(row) -> str:
    nickname = str(row["nickname"] or "").strip()
    if not nickname.startswith("模拟用户"):
        return nickname or "参赛用户"
    user_id = None
    for key in ("user_id", "id"):
        try:
            user_id = row[key]
            break
        except Exception:  # noqa: BLE001 - sqlite rows raise for missing keys
            continue
    return f"参赛用户 #{user_id}" if user_id else "参赛用户"


def audit_actor_name(row) -> str:
    actor_id = row["actor_user_id"]
    if actor_id is None:
        return "系统"
    nickname = str(row["nickname"] or "").strip()
    if nickname.startswith("模拟用户"):
        return f"参赛用户 #{actor_id}"
    return nickname or f"用户 #{actor_id}"


def report_user_name(row, key_id: str, key_name: str) -> str:
    user_id = row[key_id]
    nickname = str(row[key_name] or "").strip()
    if not user_id:
        return "-"
    if nickname.startswith("模拟用户"):
        return f"参赛用户 #{user_id}"
    return nickname or f"用户 #{user_id}"


def support_request_user_name(row, key_id: str, key_name: str) -> str:
    user_id = row[key_id]
    nickname = str(row[key_name] or "").strip()
    if not user_id:
        return "未登录访客"
    if nickname.startswith("模拟用户"):
        return f"参赛用户 #{user_id}"
    return nickname or f"用户 #{user_id}"


def history_rows(rows) -> str:
    if not rows:
        return '<tr><td colspan="5" class="muted">暂无资产快照</td></tr>'
    return "".join(
        f"<tr><td>{escape(r['created_at'])}</td><td>{money(r['equity'])}</td>"
        f"<td>{money(r['cash'])}</td><td>{money(r['market_value'])}</td><td>{pct(r['return_pct'])}</td></tr>"
        for r in rows
    )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "OurWorldQuantApp/0.1"
    # Fallback shared connection (used by tests that assign AppHandler.con directly).
    con = None
    # When set (production main()), each request opens its own SQLite connection so that
    # ThreadingHTTPServer worker threads never share one connection's transaction state.
    db_path = None
    _owns_con = False

    def setup(self):
        super().setup()
        # Per-request connection: avoids interleaving transactions on a single shared
        # connection across threads, which could otherwise commit another request's
        # half-applied write (e.g. a cash debit without the matching holdings insert).
        if self.db_path is not None:
            self.con = db.connect(self.db_path)
            self._owns_con = True

    def finish(self):
        try:
            super().finish()
        finally:
            if self._owns_con:
                try:
                    self.con.close()
                except Exception:
                    pass
                self._owns_con = False

    def log_message(self, fmt, *args):  # noqa: D401
        """Keep the default server quieter."""
        if os.getenv("OWQ_HTTP_LOG"):
            super().log_message(fmt, *args)

    def client_ip(self) -> str:
        cf_ip = self.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded
        return self.client_address[0] if self.client_address else "unknown"

    def rate_limit(self, scope: str, max_hits: int, window_seconds: int) -> bool:
        return self.rate_limit_subject(scope, self.client_ip(), max_hits, window_seconds)

    def rate_limit_subject(self, scope: str, subject: str, max_hits: int, window_seconds: int) -> bool:
        if env_flag("OWQ_RATE_LIMITS_DISABLED"):
            return True
        now = time.monotonic()
        key = (scope, subject)
        cutoff = now - float(window_seconds)
        with RATE_LIMIT_LOCK:
            hits = [item for item in RATE_LIMIT_BUCKETS.get(key, []) if item >= cutoff]
            if len(hits) >= int(max_hits):
                RATE_LIMIT_BUCKETS[key] = hits
                return False
            hits.append(now)
            RATE_LIMIT_BUCKETS[key] = hits
        return True

    def clear_rate_limit_subject(self, scope: str, subject: str) -> None:
        with RATE_LIMIT_LOCK:
            RATE_LIMIT_BUCKETS.pop((scope, subject), None)

    def require_rate_limit(self, scope: str, max_hits: int, window_seconds: int) -> bool:
        if self.rate_limit(scope, max_hits=max_hits, window_seconds=window_seconds):
            return True
        self.too_many_requests()
        return False

    def require_user_write_limit(
        self,
        user,
        scope: str,
        max_hits: int,
        window_seconds: int,
        redirect_to: str,
    ) -> bool:
        target = (scope or "write").strip()[:80]
        user_scope = f"write:{target}"
        user_subject = f"user:{int(user['id'])}"
        if self.rate_limit_subject(user_scope, user_subject, max_hits=max_hits, window_seconds=window_seconds):
            return True
        self.audit_security_event(
            "security.rate_limited",
            user=user,
            target_type="rate_limit",
            target_id=target,
            detail={
                "method": self.command,
                "path": urlparse(self.path).path[:300],
                "limit": max_hits,
                "window_seconds": window_seconds,
            },
        )
        separator = "&" if "?" in redirect_to else "?"
        self.redirect(redirect_to + separator + "err=" + quote("操作过于频繁,请稍后再试。"))
        return False

    def login_identifier_rate_limit_subject(self, identifier: str) -> str:
        normalized = (identifier or "").strip().lower()
        if not normalized:
            normalized = "empty"
        digest = hmac.new(SECRET.encode(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"identifier:{digest}"

    def require_login_identifier_limit(self, identifier: str, max_hits: int = 8, window_seconds: int = 600) -> bool:
        subject = self.login_identifier_rate_limit_subject(identifier)
        if self.rate_limit_subject("auth:login:identifier", subject, max_hits=max_hits, window_seconds=window_seconds):
            return True
        digest = subject.rsplit(":", 1)[-1]
        self.audit_security_event(
            "security.rate_limited",
            target_type="rate_limit",
            target_id="auth.login.identifier",
            detail={
                "method": self.command,
                "path": urlparse(self.path).path[:300],
                "limit": max_hits,
                "window_seconds": window_seconds,
                "identifier_type": "email" if "@" in (identifier or "") else "login_name",
                "identifier_hash": digest[:16],
            },
        )
        self.redirect("/login?err=" + quote("登录尝试过于频繁,请稍后再试。"))
        return False

    def do_GET(self):
        self.safe_dispatch(self.handle_get)

    def handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            self.render_landing()
        elif path == "/robots.txt":
            self.render_robots()
        elif path == "/sitemap.xml":
            self.render_sitemap()
        elif path == "/preview":
            self.render_preview()
        elif path == "/lessons":
            self.render_lessons()
        elif path == "/research":
            self.render_research()
        elif path == "/data-status":
            self.render_data_status()
        elif path == "/guide":
            self.render_usage_guide(query)
        elif path == "/guide/demo":
            self.render_usage_demo(query)
        elif path == "/guide/demo/audio.mp3":
            self.render_usage_demo_audio()
        elif path.startswith("/static/"):
            self.render_static_asset(path)
        elif path == "/api/glossary":
            self.send_json({"metrics": METRIC_GLOSSARY})
        elif path == "/api/equity-curve":
            self.require_user(self.api_equity_curve, enforce_consent=False)
        elif path == "/support":
            if not self.require_rate_limit("support:view", 120, 60):
                return
            self.render_support(query)
        elif path == "/livez":
            self.render_livez()
        elif path == "/healthz":
            self.render_health()
        elif path == "/readyz":
            self.render_ready()
        elif path == "/metrics":
            self.render_metrics()
        elif path in {"/legal", "/terms", "/privacy", "/risk"}:
            self.render_legal(path)
        elif path == "/register":
            if not self.require_rate_limit("auth:register", 30, 60):
                return
            self.render_register(query)
        elif path == "/forgot-password":
            if not self.require_rate_limit("auth:forgot-password", 30, 60):
                return
            self.render_forgot_password(query)
        elif path == "/login":
            if not self.require_rate_limit("auth:login", 60, 60):
                return
            self.render_login(query)
        elif path == "/auth/email/confirm":
            if not self.require_rate_limit("auth:email-confirm", 30, 60):
                return
            self.render_email_confirm(query)
        elif path.startswith("/auth/wechat/qr/"):
            if not self.legacy_wechat_enabled():
                self.not_found()
                return
            if not self.require_rate_limit("auth:qr", 90, 60):
                return
            token = path.rsplit("/", 1)[-1].split(".")[0]
            self.render_qr(token)
        elif path == "/auth/wechat/status":
            if not self.legacy_wechat_enabled():
                self.not_found()
                return
            if not self.require_rate_limit("auth:status", 180, 60):
                return
            self.render_wechat_status(query)
        elif path == "/auth/wechat/dev-confirm":
            if not self.legacy_wechat_enabled():
                self.not_found()
                return
            if not self.require_rate_limit("auth:dev-confirm:get", 30, 60):
                return
            self.render_dev_confirm(query)
        elif path == "/auth/wechat/callback":
            if not self.legacy_wechat_enabled():
                self.not_found()
                return
            if not self.require_rate_limit("auth:wechat-callback", 30, 60):
                return
            self.render_wechat_callback(query)
        elif path == "/logout":
            user = self.current_user()
            self.redirect("/account?err=" + quote("请使用页面上的退出按钮退出登录。") if user else "/login")
        elif path == "/app":
            self.require_user(lambda user: self.render_dashboard(user, query))
        elif path == "/learn":
            self.require_user(lambda user: self.render_learn(user, query))
        elif path.startswith("/learn/tasks/") and path.count("/") == 3:
            self.require_user(lambda user: self.render_learning_task(user, path, query))
        elif path == "/market":
            self.require_user(lambda user: self.render_market(user, query))
        elif path == "/portfolio-lab":
            self.require_user(lambda user: self.render_portfolio_lab(user, query))
        elif path == "/account/export/orders.csv":
            self.require_user(self.export_orders_csv, enforce_consent=False)
        elif path == "/account/export/holdings.csv":
            self.require_user(self.export_holdings_csv, enforce_consent=False)
        elif path == "/account/export/equity.csv":
            self.require_user(self.export_equity_csv, enforce_consent=False)
        elif path == "/account/export/data.json":
            self.require_user(self.export_account_json, enforce_consent=False)
        elif path == "/account/consent":
            self.require_user(lambda user: self.render_account_consent(user, query), enforce_consent=False)
        elif path == "/account":
            self.require_user(lambda user: self.render_account(user, query))
        elif path == "/account/ai":
            self.require_user(lambda user: self.render_account_ai(user, query))
        elif path == "/admin":
            self.require_admin(lambda user: self.render_admin(user, query))
        elif path == "/admin/accounts.csv":
            self.require_admin(self.export_admin_accounts_csv)
        elif path == "/admin/reports.csv":
            self.require_admin(self.export_admin_reports_csv)
        elif path == "/admin/support.csv":
            self.require_admin(self.export_admin_support_csv)
        elif path == "/admin/audit.csv":
            self.require_admin(self.export_admin_audit_csv)
        elif path in {"/contest", "/showcase"}:
            self.require_user(lambda user: self.render_showcase(user, query))
        elif path == "/showcase/public":
            self.render_public_showcase(query)
        elif path.startswith("/u/") and path.endswith("/card.svg"):
            self.render_public_profile_card(path)
        elif path.startswith("/u/") and path.count("/") == 2:
            self.render_public_profile(path, query)
        elif path == "/forum":
            self.render_forum(self.current_user(), query)
        elif path == "/forum/new":
            self.require_user(lambda user: self.render_new_post(user, query))
        elif path.startswith("/forum/") and path.count("/") == 2:
            self.render_post(self.current_user(), path, query)
        else:
            self.not_found()

    def do_HEAD(self):
        self.safe_dispatch(self.handle_head, head=True)

    def handle_head(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.render_landing(head=True)
        elif path == "/robots.txt":
            self.render_robots(head=True)
        elif path == "/sitemap.xml":
            self.render_sitemap(head=True)
        elif path == "/preview":
            self.render_preview(head=True)
        elif path == "/lessons":
            self.render_lessons(head=True)
        elif path == "/research":
            self.render_research(head=True)
        elif path == "/data-status":
            self.render_data_status(head=True)
        elif path == "/guide":
            self.render_usage_guide(parse_qs(""), head=True)
        elif path == "/guide/demo":
            self.render_usage_demo(parse_qs(""), head=True)
        elif path == "/guide/demo/audio.mp3":
            self.render_usage_demo_audio(head=True)
        elif path.startswith("/static/"):
            self.render_static_asset(path, head=True)
        elif path == "/support":
            self.send_html("OK", "", head=True)
        elif path == "/livez":
            self.render_livez(head=True)
        elif path == "/healthz":
            self.render_health(head=True)
        elif path == "/readyz":
            self.render_ready(head=True)
        elif path == "/metrics":
            self.render_metrics(head=True)
        elif path in {"/register", "/forgot-password", "/login", "/auth/email/confirm", "/guide", "/guide/demo", "/showcase/public", "/forum", "/legal", "/terms", "/privacy", "/risk", "/support"}:
            self.send_html("OK", "", head=True)
        elif path.startswith("/forum/") and path.count("/") == 2:
            self.send_html("OK", "", head=True)
        elif path.startswith("/u/") and path.endswith("/card.svg"):
            self.send_text("", "image/svg+xml; charset=utf-8", head=True)
        elif path.startswith("/u/") and path.count("/") == 2:
            self.send_html("OK", "", head=True)
        elif path in {"/learn", "/app", "/market", "/portfolio-lab", "/account", "/account/ai", "/account/consent", "/contest", "/showcase", "/forum/new"} or (path.startswith("/learn/tasks/") and path.count("/") == 3):
            user = self.current_user()
            if not user:
                self.redirect("/login")
                return
            self.send_html("OK", "", user=user, head=True)
        elif path == "/admin":
            user = self.current_user()
            if not user:
                self.redirect("/login")
                return
            if not services.is_admin(self.con, user):
                self.send_html("无权限", "", 403, user=user, head=True)
                return
            self.send_html("OK", "", user=user, head=True)
        else:
            self.send_response(404)
            self.send_security_headers("asset")
            self.end_headers()

    # AI routes make a slow (up to ~20s) third-party network call and only do append-only
    # writes (ai_usage / ai_interactions / a per-user-PK key upsert), which are
    # concurrency-safe without the global lock. Holding DB_WRITE_LOCK across that network
    # call would block every other write, so these paths are dispatched outside it.
    AI_UNLOCKED_POST_PATHS = {"/account/ai", "/account/ai-review", "/learn/coach"}

    def do_POST(self):
        # Serialize writes: POST is the only state-changing verb in this app, so holding
        # DB_WRITE_LOCK here makes every read-modify-write service call atomic and prevents
        # concurrent orders from losing holdings/cash updates.
        if urlparse(self.path).path in self.AI_UNLOCKED_POST_PATHS:
            self.safe_dispatch(self.handle_post)
        else:
            with DB_WRITE_LOCK:
                self.safe_dispatch(self.handle_post)

    def handle_post(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            form = self.read_form()
        except RequestBodyTooLarge as exc:
            self.payload_too_large(str(exc))
            return
        except ValueError as exc:
            self.bad_request(str(exc))
            return
        if path == "/register":
            if not self.require_rate_limit("auth:register:start", 20, 60):
                return
            self.handle_register_start(form)
        elif path == "/support":
            if not self.require_rate_limit("support:create", 6, 3600):
                return
            self.handle_support_request(form)
        elif path == "/forgot-password":
            if not self.require_rate_limit("auth:forgot-password:start", 20, 60):
                return
            self.handle_forgot_password_start(form)
        elif path == "/login":
            if not self.require_rate_limit("auth:login:post", 20, 60):
                return
            self.handle_login(form)
        elif path == "/auth/email/confirm":
            if not self.require_rate_limit("auth:email-confirm:post", 30, 60):
                return
            self.handle_email_confirm(form)
        elif path == "/auth/email/code":
            if not self.require_rate_limit("auth:email-code:post", 20, 60):
                return
            self.handle_email_code_confirm(form)
        elif path == "/auth/wechat/dev-confirm":
            if not self.legacy_wechat_enabled():
                self.not_found()
                return
            if not self.require_rate_limit("auth:dev-confirm:post", 12, 60):
                return
            self.handle_dev_confirm(form)
        elif path == "/logout":
            self.handle_logout(form)
        elif path == "/orders":
            self.require_active_user(lambda user: self.handle_order(user, form), form=form)
        elif path == "/practice-signals":
            self.require_active_user(lambda user: self.handle_practice_signal_create(user, form), form=form)
        elif path == "/practice-signals/batch":
            self.require_active_user(lambda user: self.handle_practice_signal_batch(user, form), form=form)
        elif path == "/practice-signals/from-market":
            self.require_active_user(lambda user: self.handle_practice_signal_from_market(user, form), form=form)
        elif path == "/practice-signals/from-predictions":
            self.require_active_user(lambda user: self.handle_practice_signal_from_predictions(user, form), form=form)
        elif path == "/practice-signals/execute-pending":
            self.require_active_user(lambda user: self.handle_practice_signal_execute_pending(user, form), form=form)
        elif path.startswith("/practice-signals/") and path.endswith("/execute"):
            self.require_active_user(lambda user: self.handle_practice_signal_execute(user, path), form=form)
        elif path.startswith("/practice-signals/") and path.endswith("/cancel"):
            self.require_active_user(lambda user: self.handle_practice_signal_cancel(user, path), form=form)
        elif path == "/account/reset":
            self.require_user(lambda user: self.handle_account_reset(user, form), form=form, csrf_redirect="/account")
        elif path == "/account/settle":
            self.require_user(lambda user: self.handle_account_settle(user), form=form, csrf_redirect="/account")
        elif path == "/account/profile":
            self.require_user(lambda user: self.handle_account_profile(user, form), form=form, csrf_redirect="/account")
        elif path == "/account/ai":
            self.require_user(lambda user: self.handle_account_ai(user, form), form=form, csrf_redirect="/account/ai")
        elif path == "/account/ai-review":
            self.require_active_user(lambda user: self.handle_account_ai_review(user, form), form=form, csrf_redirect="/account/ai")
        elif path == "/learn/coach":
            self.require_active_user(lambda user: self.handle_learning_coach(user, form), form=form, csrf_redirect="/learn")
        elif path.startswith("/learn/tasks/") and path.endswith("/preview"):
            self.require_active_user(lambda user: self.handle_learning_task_preview(user, path, form), form=form, csrf_redirect="/learn")
        elif path.startswith("/learn/tasks/") and path.endswith("/save-signals"):
            self.require_active_user(lambda user: self.handle_learning_task_save_signals(user, path, form), form=form, csrf_redirect="/learn")
        elif path == "/account/password":
            self.require_user(lambda user: self.handle_account_password(user, form), form=form, csrf_redirect="/account")
        elif path == "/account/delete":
            self.require_user(lambda user: self.handle_account_delete(user, form), form=form, csrf_redirect="/account")
        elif path == "/account/consent":
            self.require_user(
                lambda user: self.handle_account_consent(user, form),
                form=form,
                csrf_redirect="/account/consent",
                enforce_consent=False,
            )
        elif path == "/market/sync":
            self.require_user(lambda user: self.handle_market_sync(user, form), form=form, csrf_redirect="/market")
        elif path == "/admin/contest":
            self.require_admin(lambda user: self.handle_admin_contest(user, form), form=form)
        elif path == "/admin/backup":
            self.require_admin(lambda user: self.handle_admin_backup(user), form=form)
        elif path == "/admin/email-test":
            self.require_admin(lambda user: self.handle_admin_email_test(user, form), form=form)
        elif path == "/admin/email-login-prune":
            self.require_admin(lambda user: self.handle_admin_email_login_prune(user), form=form)
        elif path == "/admin/audit-prune":
            self.require_admin(lambda user: self.handle_admin_audit_prune(user), form=form)
        elif path.startswith("/admin/users/") and path.endswith("/status"):
            self.require_admin(lambda user: self.handle_admin_user_status(user, path, form), form=form)
        elif path.startswith("/admin/reports/") and path.endswith("/resolve"):
            self.require_admin(lambda user: self.handle_admin_report_resolve(user, path, form), form=form)
        elif path.startswith("/admin/support/") and path.endswith("/resolve"):
            self.require_admin(lambda user: self.handle_admin_support_resolve(user, path, form), form=form)
        elif path == "/admin/demo-seed":
            self.require_admin(lambda user: self.handle_admin_demo_seed(user), form=form)
        elif path == "/admin/demo-contest-clean":
            self.require_admin(lambda user: self.handle_admin_demo_contest_clean(user), form=form)
        elif path == "/contest/join":
            self.require_active_user(lambda user: self.handle_join_contest(user), form=form, csrf_redirect="/showcase")
        elif path == "/forum/new":
            self.require_active_user(lambda user: self.handle_new_post(user, form), form=form, csrf_redirect="/forum/new")
        elif path.startswith("/forum/") and "/comments/" in path and path.endswith("/delete"):
            self.require_user(lambda user: self.handle_delete_comment(user, path), form=form, csrf_redirect="/forum")
        elif path.startswith("/forum/") and "/comments/" in path and path.endswith("/report"):
            self.require_active_user(lambda user: self.handle_report_comment(user, path, form), form=form, csrf_redirect="/forum")
        elif path.startswith("/forum/") and path.count("/") == 3 and path.endswith("/delete"):
            self.require_user(lambda user: self.handle_delete_post(user, path), form=form, csrf_redirect="/forum")
        elif path.startswith("/forum/") and path.count("/") == 3 and path.endswith("/report"):
            self.require_active_user(lambda user: self.handle_report_post(user, path, form), form=form, csrf_redirect="/forum")
        elif path.startswith("/forum/") and path.endswith("/comment"):
            self.require_active_user(lambda user: self.handle_comment(user, path, form), form=form, csrf_redirect="/forum")
        else:
            self.not_found()

    def safe_dispatch(self, callback, head: bool = False):
        started = time.monotonic()
        self._response_status = 0
        metrics_request_started()
        try:
            callback()
        except (BrokenPipeError, ConnectionResetError):
            self._response_status = self._response_status or 499
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            incident_id = self.audit_server_error(exc)
            self.server_error(head=head, incident_id=incident_id)
        finally:
            duration_ms = (time.monotonic() - started) * 1000
            metrics_request_finished(self.command, int(getattr(self, "_response_status", 0) or 0), duration_ms)

    def send_response(self, code, message=None):  # noqa: D401
        """Record the final status code for aggregate runtime metrics."""
        self._response_status = int(code)
        super().send_response(code, message)

    def read_form(self) -> dict[str, str]:
        raw_size = self.headers.get("Content-Length", "0") or "0"
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise ValueError("Content-Length 无效。") from exc
        limit = max_form_bytes()
        if size > limit:
            raise RequestBodyTooLarge(f"表单内容过大,最大允许 {limit} 字节。")
        try:
            body = self.rfile.read(size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("表单编码无效,请使用 UTF-8。") from exc
        return {k: v[-1] for k, v in parse_qs(body).items()}

    def current_user(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(SESSION_COOKIE)
        session = verify_session_cookie(morsel.value if morsel else None)
        if not session:
            return None
        user = services.get_user(self.con, int(session["user_id"]))
        if user is None:
            return None
        user_version = services.user_session_version(user)
        cookie_version = int(session["session_version"])
        if bool(session.get("legacy")):
            return user if user_version <= cookie_version else None
        return user if cookie_version == user_version else None

    def require_user(
        self,
        callback,
        form: dict[str, str] | None = None,
        csrf_redirect: str = "/app",
        enforce_consent: bool = True,
    ):
        user = self.current_user()
        if not user:
            self.redirect("/login")
            return
        if form is not None and not verify_csrf(int(user["id"]), form.get("csrf")):
            self.audit_csrf_failed(user, csrf_redirect)
            self.redirect(csrf_redirect + "?err=" + quote("表单已过期,请刷新后重试。"))
            return
        if enforce_consent and not self.ensure_current_legal_consent(user):
            return
        callback(user)

    def require_active_user(self, callback, form: dict[str, str] | None = None, csrf_redirect: str = "/app"):
        user = self.current_user()
        if not user:
            self.redirect("/login")
            return
        if form is not None and not verify_csrf(int(user["id"]), form.get("csrf")):
            self.audit_csrf_failed(user, csrf_redirect)
            self.redirect(csrf_redirect + "?err=" + quote("表单已过期,请刷新后重试。"))
            return
        try:
            services.ensure_user_active(user)
        except ValueError as exc:
            self.redirect(csrf_redirect + "?err=" + quote(str(exc)))
            return
        if not self.ensure_current_legal_consent(user):
            return
        callback(user)

    def require_admin(self, callback, form: dict[str, str] | None = None):
        user = self.current_user()
        if not user:
            self.redirect("/login")
            return
        if not services.is_admin(self.con, user):
            self.audit_security_event(
                "security.admin_forbidden",
                user=user,
                target_type="http",
                target_id=urlparse(self.path).path[:120],
                detail={"method": self.command, "path": urlparse(self.path).path[:300]},
            )
            self.forbidden(user)
            return
        if form is not None and not verify_csrf(int(user["id"]), form.get("csrf")):
            self.audit_csrf_failed(user, "/admin")
            self.redirect("/admin?err=" + quote("表单已过期,请刷新后重试。"))
            return
        if not self.ensure_current_legal_consent(user):
            return
        callback(user)

    def legal_consent_required(self) -> bool:
        raw = os.getenv("OWQ_LEGAL_CONSENT_REQUIRED", "").strip().lower()
        if raw in TRUE_VALUES:
            return True
        if raw in FALSE_VALUES:
            return False
        return self.is_public_request()

    def has_current_legal_consent(self, user) -> bool:
        if not user:
            return False
        consent = services.latest_user_consent(self.con, int(user["id"]))
        if consent is None:
            return False
        return (
            consent["terms_version"] == LEGAL_VERSION
            and consent["privacy_version"] == LEGAL_VERSION
            and consent["risk_version"] == LEGAL_VERSION
        )

    def safe_next_path(self, value: str | None, default: str = "/app") -> str:
        candidate = str(value or "").strip()
        if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
            return default
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc or parsed.path == "/account/consent":
            return default
        return candidate[:300]

    def ensure_current_legal_consent(self, user) -> bool:
        if not self.legal_consent_required() or self.has_current_legal_consent(user):
            return True
        current_path = self.safe_next_path(self.path, default="/app")
        self.redirect("/account/consent?next=" + quote(current_path))
        return False

    def is_admin_user(self, user) -> bool:
        return bool(user and services.is_admin(self.con, user))

    def request_host(self) -> str:
        return self.headers.get("Host", "127.0.0.1:8081").strip()

    def request_hostname(self) -> str:
        host = self.request_host().split(",", 1)[0].strip()
        if host.startswith("[") and "]" in host:
            return host[1 : host.index("]")].lower()
        return host.split(":", 1)[0].lower()

    def request_scheme(self) -> str:
        public = os.getenv("OWQ_PUBLIC_BASE_URL", "").strip().lower()
        if public.startswith("https://"):
            return "https"
        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        if forwarded in {"http", "https"}:
            return forwarded
        cf_visitor = self.headers.get("CF-Visitor", "").lower()
        if '"scheme":"https"' in cf_visitor or '"scheme": "https"' in cf_visitor:
            return "https"
        return "https" if self.is_public_request() else "http"

    def is_local_request(self) -> bool:
        host = self.request_hostname()
        return host in {"", "localhost", "127.0.0.1", "::1"} or host.startswith("127.")

    def is_public_request(self) -> bool:
        if os.getenv("OWQ_PUBLIC_BASE_URL", "").strip():
            return True
        if env_flag("OWQ_ENV_PRODUCTION") or os.getenv("OWQ_ENV", "").strip().lower() in {"prod", "production"}:
            return True
        return not self.is_local_request()

    def health_detail_allowed(self) -> bool:
        if self.is_local_request():
            return True
        token = os.getenv("OWQ_HEALTH_DETAIL_TOKEN", "").strip()
        if not token:
            return False
        provided = self.headers.get("X-OWQ-Health-Token", "").strip()
        return hmac.compare_digest(provided, token)

    def cookie_secure_enabled(self) -> bool:
        return env_flag("OWQ_COOKIE_SECURE") or self.request_scheme() == "https" or self.is_public_request()

    def cookie_attrs(self, max_age: int | None = None, path: str = "/") -> str:
        attrs = ["HttpOnly", "SameSite=Lax", f"Path={path or '/'}"]
        if max_age is not None:
            attrs.append(f"Max-Age={int(max_age)}")
        if self.cookie_secure_enabled():
            attrs.append("Secure")
        return "; ".join(attrs)

    def session_cookie_header(self, user_id: int | None = None, clear: bool = False) -> str:
        if clear:
            value = ""
        else:
            user = services.get_user(self.con, int(user_id))
            value = sign_user(int(user_id), session_version=services.user_session_version(user))
        max_age = 0 if clear else session_ttl_seconds()
        return f"{SESSION_COOKIE}={value}; {self.cookie_attrs(max_age=max_age)}"

    def email_confirm_cookie_header(self, token: str | None = None, clear: bool = False) -> str:
        value = "" if clear else sign_email_confirm_token(str(token or ""))
        max_age = 0 if clear else DEFAULT_EMAIL_CONFIRM_COOKIE_SECONDS
        return f"{EMAIL_CONFIRM_COOKIE}={value}; {self.cookie_attrs(max_age=max_age, path='/auth/email/confirm')}"

    def current_email_confirm_token(self) -> str | None:
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(EMAIL_CONFIRM_COOKIE)
        return verify_email_confirm_cookie(morsel.value if morsel else None)

    def wechat_login_configured(self) -> bool:
        return bool(
            os.getenv("WECHAT_APP_ID", "").strip()
            and os.getenv("WECHAT_APP_SECRET", "").strip()
            and os.getenv("OWQ_PUBLIC_BASE_URL", "").strip()
        )

    def email_sender_provider(self) -> str:
        provider, _ = email_config.selected_provider()
        return provider

    def email_login_configured(self) -> bool:
        return bool(self.email_sender_provider())

    def email_dev_auth_enabled(self) -> bool:
        raw = os.getenv("OWQ_EMAIL_DEV_AUTH", "").strip().lower()
        if raw in TRUE_VALUES:
            return True
        if raw in FALSE_VALUES:
            return False
        return not self.email_login_configured() and self.is_local_request()

    def email_dev_auth_show_links(self) -> bool:
        if not self.email_dev_auth_enabled():
            return False
        raw = os.getenv("OWQ_EMAIL_DEV_AUTH_SHOW_LINKS", "").strip().lower()
        if raw in TRUE_VALUES:
            return True
        if raw in FALSE_VALUES:
            return False
        return self.is_local_request() and not self.is_public_request()

    def legacy_wechat_enabled(self) -> bool:
        return env_flag("OWQ_LEGACY_WECHAT_AUTH")

    def dev_auth_enabled(self) -> bool:
        raw = os.getenv("OWQ_DEV_AUTH", "").strip().lower()
        if raw in TRUE_VALUES:
            return True
        if raw in FALSE_VALUES:
            return False
        return not self.wechat_login_configured() and self.is_local_request()

    def auth_mode(self) -> str:
        if self.email_login_configured():
            return "email"
        if self.email_dev_auth_enabled():
            return "email_dev"
        return "disabled"

    def audit(self, action: str, user=None, target_type: str = "", target_id=None, detail: dict | str | None = None):
        actor_id = int(user["id"]) if user else None
        return services.record_audit_event(
            self.con,
            actor_id,
            action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            ip_address=self.client_ip(),
        )

    def audit_security_event(self, action: str, user=None, target_type: str = "http", target_id=None, detail: dict | None = None):
        try:
            return self.audit(action, user=user, target_type=target_type, target_id=target_id, detail=detail or {})
        except Exception:  # noqa: BLE001
            return None

    def redirect_operation_failed(
        self,
        redirect_to: str,
        message: str,
        action: str,
        exc: Exception,
        user=None,
        target_type: str = "operation",
        detail: dict | None = None,
    ):
        safe_detail = {"error": type(exc).__name__}
        for key, value in (detail or {}).items():
            if value is not None:
                safe_detail[str(key)[:60]] = str(value)[:120]
        self.audit(action, user=user, target_type=target_type, detail=safe_detail)
        separator = "&" if "?" in redirect_to else "?"
        self.redirect(redirect_to + separator + "err=" + quote(message))

    def audit_csrf_failed(self, user, redirect_to: str):
        path = urlparse(self.path).path
        return self.audit_security_event(
            "security.csrf_failed",
            user=user,
            target_type="http",
            target_id=path[:120],
            detail={"method": self.command, "path": path[:300], "redirect": redirect_to[:120]},
        )

    def audit_server_error(self, exc: Exception):
        try:
            return services.record_audit_event(
                self.con,
                None,
                "server.error",
                target_type="http",
                target_id=urlparse(self.path).path[:200],
                detail={
                    "method": self.command,
                    "error_type": type(exc).__name__,
                    "path": urlparse(self.path).path[:500],
                },
                ip_address=self.client_ip(),
            )
        except Exception:  # noqa: BLE001
            return None

    def record_current_consent(self, user_id: int, source: str) -> int:
        return services.record_user_consent(
            self.con,
            int(user_id),
            LEGAL_VERSION,
            LEGAL_VERSION,
            LEGAL_VERSION,
            source=source,
            ip_address=self.client_ip(),
            user_agent=self.headers.get("User-Agent", ""),
        )

    def wechat_session_has_current_legal_acceptance(self, token: str) -> bool:
        acceptance = services.wechat_session_legal_acceptance(self.con, token)
        if not acceptance:
            return False
        return (
            acceptance.get("accepted_terms_version") == LEGAL_VERSION
            and acceptance.get("accepted_privacy_version") == LEGAL_VERSION
            and acceptance.get("accepted_risk_version") == LEGAL_VERSION
        )

    def base_url(self) -> str:
        public = os.getenv("OWQ_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public:
            return public
        return f"{self.request_scheme()}://{self.request_host()}"

    def email_login_url(self, token: str) -> str:
        return f"{self.base_url()}/auth/email/confirm?token={quote(token)}"

    def send_login_email(self, email: str, token: str, code: str) -> str:
        login_url = self.email_login_url(token)
        subject = "OurWorlds Quant 注册码"
        text = (
            "请使用下面的注册码完成 OurWorlds Quant 模拟盘公开赛邮箱确认。\n\n"
            f"注册码: {code}\n\n"
            f"确认页: {self.base_url()}/auth/email/confirm\n"
            "你也可以直接打开下面的备用确认链接:\n\n"
            f"{login_url}\n\n"
            "确认后设置用户名和密码,再使用账号密码登录。注册码和链接 15 分钟内有效,且只能使用一次。"
            "如果不是你本人操作,请忽略这封邮件。"
        )
        html = (
            "<p>请使用下面的注册码完成 OurWorlds Quant 模拟盘公开赛邮箱确认。</p>"
            f'<p style="font-size:24px;font-weight:700">{escape(code)}</p>'
            f'<p><a href="{escape(self.base_url() + "/auth/email/confirm", quote=True)}">打开邮箱确认页</a></p>'
            f'<p>备用链接: <a href="{escape(login_url, quote=True)}">直接确认邮箱</a></p>'
            "<p>确认后设置用户名和密码,再使用账号密码登录。注册码和链接 15 分钟内有效,且只能使用一次。"
            "如果不是你本人操作,请忽略这封邮件。</p>"
        )
        return self.send_transactional_email(email, subject, text, html)

    def send_password_reset_email(self, email: str, token: str, code: str) -> str:
        reset_url = self.email_login_url(token)
        subject = "OurWorlds Quant 设置/重置密码注册码"
        text = (
            "请使用下面的注册码打开 OurWorlds Quant 登录密码设置/重置页。\n\n"
            f"注册码: {code}\n\n"
            f"确认页: {self.base_url()}/auth/email/confirm\n"
            "你也可以直接打开下面的备用重置链接:\n\n"
            f"{reset_url}\n\n"
            "确认后设置新密码,再使用用户名或邮箱和新密码登录。注册码和链接 15 分钟内有效,且只能使用一次。"
            "如果不是你本人操作,请忽略这封邮件。"
        )
        html = (
            "<p>请使用下面的注册码打开 OurWorlds Quant 登录密码设置/重置页。</p>"
            f'<p style="font-size:24px;font-weight:700">{escape(code)}</p>'
            f'<p><a href="{escape(self.base_url() + "/auth/email/confirm", quote=True)}">打开验证码确认页</a></p>'
            f'<p>备用链接: <a href="{escape(reset_url, quote=True)}">直接打开重置密码页</a></p>'
            "<p>确认后设置新密码,再使用用户名或邮箱和新密码登录。注册码和链接 15 分钟内有效,且只能使用一次。"
            "如果不是你本人操作,请忽略这封邮件。</p>"
        )
        return self.send_transactional_email(email, subject, text, html)

    def send_transactional_email(self, email: str, subject: str, text: str, html: str) -> str:
        provider = self.email_sender_provider()
        if not provider:
            raise RuntimeError("邮箱发信服务未配置")
        if provider == "cloudflare":
            self.send_login_email_cloudflare(email, subject, text, html)
        elif provider == "smtp":
            self.send_login_email_smtp(email, subject, text, html)
        else:
            raise RuntimeError("邮箱发信服务未配置")
        return provider

    def send_login_email_cloudflare(self, email: str, subject: str, text: str, html: str) -> None:
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        from_addr = os.getenv("OWQ_EMAIL_FROM", "").strip()
        payload = json.dumps(
            {
                "to": email,
                "from": from_addr,
                "subject": subject,
                "text": text,
                "html": html,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/email/sending/send",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed Cloudflare API endpoint
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            detail = self.cloudflare_email_error_detail(raw) or sanitize_diagnostic_message(exc.reason or exc)
            raise RuntimeError(f"Cloudflare Email Sending HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cloudflare Email Sending 网络错误: {sanitize_diagnostic_message(exc.reason)}") from exc
        body = self.parse_cloudflare_email_response(raw)
        if body.get("success") is not True:
            detail = self.cloudflare_email_error_detail(raw) or "API 未返回 success=true"
            raise RuntimeError(f"Cloudflare Email Sending 返回失败: {detail}")

    def parse_cloudflare_email_response(self, raw: bytes) -> dict:
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise RuntimeError(f"Cloudflare Email Sending 返回不可解析响应: {type(exc).__name__}") from exc
        return body if isinstance(body, dict) else {}

    def cloudflare_email_error_detail(self, raw: bytes) -> str:
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return sanitize_diagnostic_message(raw.decode("utf-8", errors="replace"))
        errors = body.get("errors") if isinstance(body, dict) else None
        messages = []
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict):
                    msg = item.get("message") or item.get("code")
                else:
                    msg = item
                if msg:
                    messages.append(sanitize_diagnostic_message(msg, limit=120))
        if messages:
            return "; ".join(messages)[:240]
        if isinstance(body, dict) and body.get("messages"):
            return sanitize_diagnostic_message(body.get("messages"), limit=240)
        return ""

    def send_login_email_smtp(self, email: str, subject: str, text: str, html: str) -> None:
        host = os.getenv("OWQ_SMTP_HOST", "").strip()
        port = int(os.getenv("OWQ_SMTP_PORT", "587") or "587")
        username = os.getenv("OWQ_SMTP_USER", "").strip()
        password = os.getenv("OWQ_SMTP_PASSWORD", "")
        from_addr = os.getenv("OWQ_EMAIL_FROM", "").strip()
        from_name = os.getenv("OWQ_EMAIL_FROM_NAME", "OurWorlds Quant").strip()
        use_ssl = env_flag("OWQ_SMTP_SSL", default=(port == 465))
        use_tls = env_flag("OWQ_SMTP_TLS", default=(not use_ssl))
        msg = EmailMessage()
        msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
        msg["To"] = email
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        context = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                if use_tls:
                    smtp.starttls(context=context)
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)

    def auth_target_url(self, token: str) -> str:
        app_id = os.getenv("WECHAT_APP_ID", "").strip()
        if self.wechat_login_configured():
            params = {
                "appid": app_id,
                "redirect_uri": f"{self.base_url()}/auth/wechat/callback",
                "response_type": "code",
                "scope": "snsapi_login",
                "state": token,
            }
            return "https://open.weixin.qq.com/connect/qrconnect?" + urlencode(params) + "#wechat_redirect"
        if self.dev_auth_enabled():
            return f"{self.base_url()}/auth/wechat/dev-confirm?token={quote(token)}"
        return f"{self.base_url()}/register?err={quote('注册暂未开放')}"

    def public_registration_available(self) -> bool:
        mode = self.auth_mode()
        return mode == "email" or (mode == "email_dev" and self.email_dev_auth_show_links())

    def public_join_href(self) -> str:
        return "/register" if self.public_registration_available() else "/support"

    def public_join_label(self, primary: bool = True) -> str:
        if self.public_registration_available():
            return "邮箱验证注册" if primary else "邮箱注册"
        return "申请加入" if primary else "联系支持"

    def public_join_button(self, class_name: str = "btn", primary: bool = True) -> str:
        return f'<a class="{escape(class_name, quote=True)}" href="{self.public_join_href()}">{escape(self.public_join_label(primary))}</a>'

    def public_join_hint(self) -> str:
        if self.public_registration_available():
            return "首次完成邮箱验证会自动加入公开赛"
        return "注册开放前可先提交支持请求,由管理员联系处理"

    def social_meta_html(self, title: str, meta: dict | None = None) -> str:
        if not meta:
            return ""
        meta_title = str(meta.get("title") or title)
        description = str(meta.get("description") or "")
        url = str(meta.get("url") or "")
        image = str(meta.get("image") or "")
        og_type = str(meta.get("type") or "website")
        tags = [
            f'<meta name="description" content="{escape(description, quote=True)}">',
            f'<meta property="og:title" content="{escape(meta_title, quote=True)}">',
            f'<meta property="og:description" content="{escape(description, quote=True)}">',
            f'<meta property="og:type" content="{escape(og_type, quote=True)}">',
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{escape(meta_title, quote=True)}">',
            f'<meta name="twitter:description" content="{escape(description, quote=True)}">',
        ]
        if url:
            tags.append(f'<meta property="og:url" content="{escape(url, quote=True)}">')
        if image:
            image = escape(image, quote=True)
            tags.append(f'<meta property="og:image" content="{image}">')
            tags.append(f'<meta name="twitter:image" content="{image}">')
        return "\n  " + "\n  ".join(tags)

    def send_html(
        self,
        title: str,
        body: str,
        status: int = 200,
        user=None,
        meta: dict | None = None,
        head: bool = False,
        extra_headers: dict[str, str] | None = None,
    ):
        if user is None:
            # Public/marketing pages don't pass a user; detect the session here so the nav (and
            # anything keyed off `user`) reflects login state CONSISTENTLY on every page, not
            # just on auth-gated ones. Callers that pass a real user skip this.
            try:
                user = self.current_user()
            except Exception:  # noqa: BLE001 - nav detection must never break a response
                user = None
        nav = ""
        if user:
            admin_link = '<a href="/admin">管理</a>' if self.is_admin_user(user) else ""
            nav = (
                '<div class="nav">'
                '<a href="/learn">学习</a><a href="/app">高级模拟盘</a><a href="/market">基础数据</a><a href="/portfolio-lab">组合设计</a><a href="/research">研究引擎</a><a href="/showcase">比赛展示</a>'
                f'<a href="/forum">论坛</a><a href="/guide">指南</a><a href="/account/ai">AI教练</a><a href="/support">支持</a><a href="/account">账户</a>{admin_link}'
                f'<span>{escape(user["nickname"])}</span>'
                f'<form method="post" action="/logout">{csrf_input(user)}<button type="submit">退出</button></form>'
                "</div>"
            )
        else:
            nav_join = self.public_join_button("primary", primary=False if self.public_registration_available() else True)
            nav = (
                '<div class="nav">'
                '<a href="/">首页</a><a href="/preview">试一试</a><a href="/lessons">三大坑</a><a href="/showcase/public">排行榜</a><a href="/forum">论坛</a>'
                f'<a href="/data-status">数据状态</a><a href="/guide">指南</a><a href="/support">支持</a><a href="/login">登录</a>{nav_join}'
                "</div>"
            )
        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{escape(title)} · OurWorlds Quant</title>
  {self.social_meta_html(title, meta)}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;700;900&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{CSS}</style>
  <script src="/static/app.js" defer></script>
</head>
<body>
  <main class="wrap">
    <header class="top">
      <a class="brand" href="/">OurWorlds Quant Arena</a>
      {nav}
    </header>
    {body}
    <footer class="footer">
      <span>所有内容仅用于技术研究与模拟训练，不构成投资建议。</span>
      <a href="/terms">服务条款</a>
      <a href="/privacy">隐私说明</a>
      <a href="/risk">风险提示</a>
      <a href="/support">支持</a>
    </footer>
  </main>
</body>
</html>"""
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_security_headers("html")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for name, value in (extra_headers or {}).items():
            self.send_header(str(name), str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def render_landing(self, head: bool = False):
        path = db.REPO_ROOT / "docs" / "index.html"
        if not path.exists():
            self.redirect("/register")
            return
        html = path.read_text(encoding="utf-8")
        html = self.inject_landing_runtime(html, services.landing_summary(self.con), self.current_user())
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_security_headers("html")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if not head:
            self.wfile.write(payload)

    def inject_landing_runtime(self, html: str, summary: dict, user=None) -> str:
        replacements = {
            "PUBLIC_NAV_CTA": self.landing_nav_cta(user),
            "HERO_ACTIONS": self.landing_hero_actions(user),
            "HERO_SCORES": self.landing_hero_scores(summary),
            "HERO_FEED": self.landing_hero_feed(summary),
            "LIVE_STRIP": self.landing_metric_strip(summary),
            "LIVE_SECTION": self.landing_live_section(summary),
            "FLOW_STEP_1": self.landing_flow_step_one(),
            "LINK_TILES": self.landing_link_tiles(user),
        }
        for name, content in replacements.items():
            html = self.replace_landing_block(html, name, content)
        return html

    def replace_landing_block(self, html: str, name: str, content: str) -> str:
        start = f"<!-- OWQ:{name}:START -->"
        end = f"<!-- OWQ:{name}:END -->"
        before, found, rest = html.partition(start)
        if not found:
            return html
        _, found_end, after = rest.partition(end)
        if not found_end:
            return html
        return f"{before}{start}\n{content}\n{end}{after}"

    def landing_source_label(self, summary: dict) -> str:
        sources = [str(row["source"]) for row in summary.get("sources", []) if row["source"]]
        real_sources = [src for src in sources if src != "demo"]
        source_text = " ".join(real_sources or sources).lower()
        if "tushare" in source_text:
            return "Tushare / DuckDB"
        if "akshare" in source_text:
            return "AkShare / DuckDB"
        if "baostock" in source_text:
            return "BaoStock / DuckDB"
        if real_sources:
            return "真实行情库"
        return "演示行情"

    def landing_market_count_text(self, summary: dict) -> str:
        real_count = int(summary.get("real_market_code_count") or 0)
        total_count = int(summary.get("market_code_count") or 0)
        count = real_count or total_count
        return f"{count} 只" if count else "待同步"

    def market_provenance(self) -> dict:
        """Where the prices that value the account come from: demo vs real, and how stale."""
        rows = [r for r in services.market_source_summary(self.con) if r["source"]]
        real = [r for r in rows if r["source"] != "demo"]
        pool = real or rows
        as_of = max((str(r["date_max"]) for r in pool if r["date_max"]), default="")
        return {"is_real": bool(real), "as_of": as_of[:10]}  # date only, drop any time part

    def provenance_chip(self) -> str:
        """A server-rendered chip (works without JS) telling the user what they're looking at:
        their own simulated account, priced off demo or real-but-non-realtime market data."""
        prov = self.market_provenance()
        if prov["is_real"]:
            label = f"真实行情(截至 {escape(prov['as_of'])},非实时)" if prov["as_of"] else "真实行情(非实时)"
            cls = "real"
        else:
            label = "演示数据"
            cls = "demo"
        return (
            '<div class="provenance">'
            "<b>你的模拟训练账户</b>"
            f'<b class="{cls}">行情: {label}</b>'
            '<span class="muted">所有数字均为模拟训练,不产生真实委托</span>'
            "</div>"
        )

    def landing_date_text(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return "待同步"
        return text.split(" ")[0]

    def public_prediction_status(self) -> dict:
        csv_path = Path(os.getenv("OWQ_PREDICTIONS_CSV", "reports/predictions.csv"))
        status = {
            "available": False,
            "row_count": 0,
            "valid_count": 0,
            "matched_count": 0,
            "latest_date": "",
            "top": [],
            "detail": "预测候选文件暂未生成",
        }
        if not csv_path.exists():
            return status
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:  # noqa: BLE001 - public page should degrade without exposing internals
            status["detail"] = f"预测候选暂时不可读取: {type(exc).__name__}"
            return status
        parsed = []
        dates = []
        for row in rows:
            code = str(row.get("code") or "").strip().upper()
            if not code:
                continue
            try:
                prediction = float(row.get("prediction", ""))
            except ValueError:
                continue
            parsed_date = doctor.parse_market_date(row.get("date"))
            if parsed_date:
                dates.append(parsed_date)
            parsed.append({"code": code, "prediction": prediction, "date": parsed_date.isoformat() if parsed_date else ""})
        status["row_count"] = len(rows)
        status["valid_count"] = len(parsed)
        if not parsed:
            status["detail"] = "预测候选文件没有可用的 code/prediction/date 行"
            return status
        codes = sorted({row["code"] for row in parsed})
        placeholders = ",".join("?" for _ in codes)
        market_rows = self.con.execute(
            f"""
            SELECT code, name, source, as_of, price
            FROM market_prices
            WHERE source <> 'demo' AND price > 0 AND prev_close > 0 AND code IN ({placeholders})
            """,
            codes,
        ).fetchall()
        market_by_code = {row["code"]: row for row in market_rows}
        matched = [row for row in parsed if row["code"] in market_by_code]
        matched.sort(key=lambda row: row["prediction"], reverse=True)
        status.update(
            {
                "available": bool(matched),
                "matched_count": len({row["code"] for row in matched}),
                "latest_date": max(dates).isoformat() if dates else "",
                "top": [
                    {
                        "code": row["code"],
                        "prediction": row["prediction"],
                        "date": row["date"],
                        "name": market_by_code[row["code"]]["name"],
                        "source": market_by_code[row["code"]]["source"],
                        "as_of": market_by_code[row["code"]]["as_of"],
                    }
                    for row in matched[:5]
                ],
            }
        )
        status["detail"] = (
            f"预测候选 {status['valid_count']} 行 / 可交易匹配 {status['matched_count']} 个"
            + (f"，最新预测 {status['latest_date']}" if status["latest_date"] else "")
        )
        return status

    def landing_display_name(self, row) -> str:
        return display_nickname(row)

    def landing_nav_cta(self, user=None) -> str:
        # This block now owns the whole login-area of the landing nav (the static 登录 link was
        # folded in here) so it flips fully with login state — no stray 登录 link when signed in.
        if user:
            return '<a href="/account">账户</a><a class="primary" href="/app">进入模拟盘</a>'
        label = self.public_join_label(primary=False if self.public_registration_available() else True)
        return f'<a href="/login">登录</a><a class="primary" href="{self.public_join_href()}">{escape(label)}</a>'

    def landing_hero_actions(self, user=None) -> str:
        if user:
            # Already logged in: the marketing CTAs become "enter the product", not "sign up".
            return "\n".join(
                [
                    '<a class="btn blue" href="/app">进入模拟盘</a>',
                    '<a class="btn" href="/learn">继续学习</a>',
                    '<a class="btn" href="/research">研究引擎</a>',
                    '<a class="btn" href="/showcase/public">查看公开榜单</a>',
                    '<a class="btn" href="/forum">进入策略论坛</a>',
                ]
            )
        join = self.public_join_button("btn blue", primary=True)
        return "\n".join(
            [
                join,
                '<a class="btn" href="/login">账号密码登录</a>',
                '<a class="btn" href="/guide">使用指南</a>',
                '<a class="btn" href="/showcase/public">查看公开榜单</a>',
                '<a class="btn" href="/forum">进入策略论坛</a>',
                '<a class="btn" href="/support">联系支持</a>',
            ]
        )

    def landing_flow_step_one(self) -> str:
        if self.public_registration_available():
            title = "邮箱验证"
            detail = "确认邮箱后设置用户名和密码，再登录参赛。"
        else:
            title = "申请加入"
            detail = "当前新用户注册暂未开放，请先提交支持请求，等待管理员联系开通。"
        return f'<div class="step"><span>STEP 1</span><h3>{title}</h3><p>{detail}</p></div>'

    def landing_link_tiles(self, user=None) -> str:
        if user:
            first = '<a class="link-tile" href="/app"><strong>进入模拟盘</strong><p>查看你的账户、持仓、净值曲线和本周复盘。</p></a>'
        elif self.public_registration_available():
            first = '<a class="link-tile" href="/register"><strong>邮箱注册</strong><p>验证邮箱、设置账号密码并加入公开赛。</p></a>'
        else:
            first = '<a class="link-tile" href="/support"><strong>申请加入</strong><p>提交注册或登录支持请求，等待管理员联系开通。</p></a>'
        return "\n".join(
            [
                first,
                '<a class="link-tile" href="/showcase/public"><strong>公开榜单</strong><p>查看排名和参赛者战绩。</p></a>',
                '<a class="link-tile" href="/forum"><strong>策略论坛</strong><p>阅读复盘，参与讨论。</p></a>',
                '<a class="link-tile" href="/data-status"><strong>数据状态</strong><p>查看行情来源、预测候选和赛场活跃度。</p></a>',
                '<a class="link-tile" href="/guide"><strong>使用指南</strong><p>按流程了解注册、模拟交易、组合设计和公开复盘。</p></a>',
            ]
        )

    def landing_hero_scores(self, summary: dict) -> str:
        participant_count = int(summary.get("participant_count") or 0)
        post_count = int(summary.get("post_count") or 0)
        market_count = self.landing_market_count_text(summary)
        board = summary.get("leaderboard") or []
        top_return = pct(board[0]["return_pct"]) if board else "待开赛"
        return "\n".join(
            [
                f'<div class="score"><b>{participant_count} 人</b><span>当前参赛账户</span></div>',
                f'<div class="score"><b>{market_count}</b><span>已接入行情标的</span></div>',
                f'<div class="score"><b>{post_count} 篇</b><span>公开策略复盘</span></div>',
                f'<div class="score"><b>{top_return}</b><span>当前榜首收益</span></div>',
            ]
        )

    def landing_hero_feed(self, summary: dict) -> str:
        rows = []
        board = summary.get("leaderboard") or []
        if board:
            leader = board[0]
            rows.append(
                '<div class="feed-row"><span class="tag">LEADER</span>'
                f'<span>当前榜首: {escape(self.landing_display_name(leader["row"]))}，收益 {pct(leader["return_pct"])}</span>'
                '<span class="pill">公开榜单</span></div>'
            )
        else:
            leader_text = "公开赛已开放报名，等待第一批参赛账户形成榜单" if self.public_registration_available() else "公开赛暂未开放新注册，等待第一批申请账户形成榜单"
            rows.append(
                '<div class="feed-row"><span class="tag">LEADER</span>'
                f'<span>{leader_text}</span>'
                '<span class="pill">公开榜单</span></div>'
            )
        latest_posts = summary.get("latest_posts") or []
        if latest_posts:
            post = latest_posts[0]
            rows.append(
                '<div class="feed-row"><span class="tag">DISCUSS</span>'
                f'<span>最新复盘: {escape(post["title"])}</span>'
                '<span class="pill">策略论坛</span></div>'
            )
        else:
            rows.append(
                '<div class="feed-row"><span class="tag">DISCUSS</span>'
                '<span>论坛等待第一篇带战绩快照的策略复盘</span>'
                '<span class="pill">策略论坛</span></div>'
            )
        source = self.landing_source_label(summary)
        as_of = self.landing_date_text(summary.get("market_as_of"))
        rows.append(
            '<div class="feed-row"><span class="tag">DATA</span>'
            f'<span>行情源: {escape(source)}，最新交易日 {escape(str(as_of))}</span>'
            '<span class="pill">真实数据</span></div>'
        )
        return "\n".join(rows)

    def landing_metric_strip(self, summary: dict) -> str:
        discussion_count = int(summary.get("post_count") or 0) + int(summary.get("comment_count") or 0)
        as_of = self.landing_date_text(summary.get("market_as_of"))
        return "\n".join(
            [
                f'<div class="metric"><strong>{int(summary.get("participant_count") or 0)} 人</strong><span>Contest players</span></div>',
                f'<div class="metric"><strong>{int(summary.get("order_count") or 0)} 笔</strong><span>Paper trades</span></div>',
                f'<div class="metric"><strong>{discussion_count} 条</strong><span>Posts and comments</span></div>',
                f'<div class="metric"><strong>{escape(str(as_of))}</strong><span>Latest market date</span></div>',
            ]
        )

    def landing_live_section(self, summary: dict) -> str:
        board = summary.get("leaderboard") or []
        rank_rows = "".join(
            '<div class="rank-row">'
            f'<span>#{item["rank"]}</span>'
            f'<a href="/u/{item["row"]["user_id"]}">{escape(self.landing_display_name(item["row"]))}</a>'
            f'<strong>{pct(item["return_pct"])}</strong>'
            '</div>'
            for item in board[:5]
        ) or f'<p class="muted">暂无参赛账户。{escape(self.public_join_hint())}。</p>'
        latest_posts = summary.get("latest_posts") or []
        post_rows = "".join(
            '<div class="mini-post">'
            f'<a href="/forum/{post["id"]}"><strong>{escape(post["title"])}</strong></a>'
            f'<p>{escape(str(post["body"])[:92])}</p>'
            f'<span>{escape(display_nickname(post))} · {escape(post["strategy_tag"])}</span>'
            '</div>'
            for post in latest_posts
        ) or '<p class="muted">暂无复盘帖。参赛后可以从比赛页一键生成带战绩快照的复盘草稿。</p>'
        source = self.landing_source_label(summary)
        market_count = self.landing_market_count_text(summary)
        as_of = self.landing_date_text(summary.get("market_as_of"))
        return f"""
<div class="live-grid">
  <div class="card">
    <div class="card-title">
      <span class="tag">LEADERBOARD</span>
      <a href="/showcase/public">查看完整榜单</a>
    </div>
    <h3>当前公开赛排名</h3>
    <div class="rank-list">{rank_rows}</div>
  </div>
  <div class="card">
    <div class="card-title">
      <span class="tag">FORUM</span>
      <a href="/forum">进入论坛</a>
    </div>
    <h3>最新策略讨论</h3>
    <div class="post-list">{post_rows}</div>
  </div>
</div>
<div class="data-proof">
  <div><span class="tag">DATA PROOF</span><strong>{escape(source)}</strong><p>当前首页读取应用数据库中的真实赛场状态，行情标的 {escape(market_count)}，最新交易日 {escape(str(as_of))}。</p></div>
  <a class="btn" href="/data-status">查看数据状态</a>
</div>
"""

    def render_data_status(self, head: bool = False):
        summary = services.landing_summary(self.con)
        prediction = self.public_prediction_status()
        source = self.landing_source_label(summary)
        market_count = self.landing_market_count_text(summary)
        as_of = self.landing_date_text(summary.get("market_as_of"))
        discussion_count = int(summary.get("post_count") or 0) + int(summary.get("comment_count") or 0)
        join_secondary = self.public_join_button("btn secondary", primary=True)
        source_rows = "".join(
            "<tr>"
            f"<td>{escape(str(row['source']))}</td>"
            f"<td>{int(row['codes'] or 0)}</td>"
            f"<td>{int(row['rows'] or 0)}</td>"
            f"<td>{escape(str(row['date_max'] or '-'))}</td>"
            f"<td>{escape(str(row['updated_at'] or '-'))}</td>"
            "</tr>"
            for row in summary.get("sources", [])
        ) or '<tr><td colspan="5" class="muted">暂无行情来源记录</td></tr>'
        prediction_rows = "".join(
            "<tr>"
            f"<td>{escape(row['code'])}</td>"
            f"<td>{escape(str(row['name'] or '-'))}</td>"
            f"<td>{pct(float(row['prediction']) * 100)}</td>"
            f"<td>{escape(str(row['as_of'] or '-'))}</td>"
            f"<td>{escape(str(row['source'] or '-'))}</td>"
            "</tr>"
            for row in prediction.get("top", [])
        ) or '<tr><td colspan="5" class="muted">暂无可展示的可交易预测候选</td></tr>'
        body = f"""
<section class="card">
  <h2>数据透明度</h2>
  <p>这里展示公开赛和组合设计当前正在使用的公开数据状态。页面只展示可公开信息:行情覆盖、最新交易日、预测候选匹配情况和赛场活跃度;内部密钥、管理员配置、登录会话和邮箱发信配置不会在这里展示。</p>
  <p><a class="btn" href="/showcase/public">公开榜单</a> <a class="btn secondary" href="/forum">策略论坛</a> {join_secondary}</p>
</section>
<div class="cards">
  <div class="card"><p>行情来源</p><div class="metric">{escape(source)}</div><p>{escape(market_count)}可用标的</p></div>
  <div class="card"><p>最新交易日</p><div class="metric">{escape(str(as_of))}</div><p>模拟成交使用当前不复权价格</p></div>
  <div class="card"><p>预测候选</p><div class="metric">{int(prediction['matched_count'])} 个</div><p>{escape(prediction['detail'])}</p></div>
</div>
<div class="cards">
  <div class="card"><p>参赛账户</p><div class="metric">{int(summary.get('participant_count') or 0)} 人</div><p>{escape(self.public_join_hint())}</p></div>
  <div class="card"><p>模拟成交</p><div class="metric">{int(summary.get('order_count') or 0)} 笔</div><p>交易记录用于公开排名和复盘</p></div>
  <div class="card"><p>讨论记录</p><div class="metric">{discussion_count} 条</div><p>帖子和评论围绕战绩、持仓和策略复盘展开</p></div>
</div>
<section class="card">
  <h2>行情来源明细</h2>
  <table><thead><tr><th>来源</th><th>标的数</th><th>记录数</th><th>最新日期</th><th>更新时间</th></tr></thead><tbody>{source_rows}</tbody></table>
</section>
<section class="card">
  <h2>模型候选状态</h2>
  <p>预测候选来自服务端生成的 CSV,并且只展示能匹配当前真实行情的标的。候选结果用于组合演练,不构成投资建议或收益承诺。</p>
  <table><thead><tr><th>代码</th><th>名称</th><th>预测</th><th>行情日期</th><th>行情来源</th></tr></thead><tbody>{prediction_rows}</tbody></table>
</section>
<section class="card">
  <h2>使用边界</h2>
  <p>本站模拟盘不产生真实证券委托。公开数据状态只能说明系统当前可用的数据覆盖和更新时间,不能代表数据无误、策略有效或未来收益。正式运营前仍需完成真实邮件发信配置并关闭测试验证入口。</p>
</section>
"""
        self.send_html(
            "数据透明度",
            body,
            meta={
                "title": "OurWorlds Quant 数据透明度",
                "description": "查看模拟盘公开赛当前行情来源、最新交易日、预测候选和赛场活跃度。",
                "url": self.public_url("/data-status"),
            },
            head=head,
        )

    def usage_flow_cards(self) -> str:
        cards = []
        for idx, step in enumerate(USAGE_FLOW_STEPS, start=1):
            cards.append(
                '<div class="flow-step">'
                f"<span>STEP {idx}</span>"
                f"<strong>{escape(step['title'])}</strong>"
                f"<p>{escape(step['summary'])}</p>"
                f"<p class=\"muted\">{escape(step['detail'])}</p>"
                f"<a href=\"{escape(step['path'], quote=True)}\">进入 {escape(step['path'])}</a>"
                "</div>"
            )
        return "".join(cards)

    def render_usage_guide(self, query, head: bool = False):
        gap_items = "".join(f"<li>{escape(item)}</li>" for item in USAGE_GAPS)
        improvement_items = "".join(f"<li>{escape(item)}</li>" for item in USAGE_IMPROVEMENTS)
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>网站使用流程</h2>
  <p>这套系统从公开了解、邮箱注册、密码登录、模拟交易、组合设计、公开展示到论坛复盘形成闭环。下面按真实用户路径展开。</p>
  <div class="flow-map">{self.usage_flow_cards()}</div>
  <p><a class="btn" href="/guide/demo">观看自动演示</a> <a class="btn secondary" href="/register">开始注册</a> <a class="btn secondary" href="/showcase/public">查看榜单</a></p>
</section>
<section class="grid">
  <div class="card">
    <h2>当前不足</h2>
    <ul class="guide-list">{gap_items}</ul>
  </div>
  <div class="card">
    <h2>本次优化</h2>
    <ul class="guide-list">{improvement_items}</ul>
  </div>
</section>
<section class="card">
  <h2>角色路径</h2>
  <table>
    <thead><tr><th>角色</th><th>第一步</th><th>核心页面</th><th>完成目标</th></tr></thead>
    <tbody>
      <tr><td>访客</td><td>看首页和指南</td><td><a href="/data-status">数据状态</a> / <a href="/showcase/public">公开榜单</a> / <a href="/forum">论坛</a></td><td>判断赛场、数据和讨论是否值得加入</td></tr>
      <tr><td>新用户</td><td>邮箱注册并输入注册码</td><td><a href="/register">注册</a> / <a href="/auth/email/confirm">注册码确认</a> / <a href="/login">登录</a></td><td>设置密码并进入模拟盘</td></tr>
      <tr><td>参赛用户</td><td>制定演练计划</td><td><a href="/app">模拟盘</a> / <a href="/portfolio-lab">组合设计</a> / <a href="/showcase/public">公开榜单</a></td><td>执行模拟交易、跟踪收益、公开复盘</td></tr>
      <tr><td>管理员</td><td>完成发布体检</td><td><a href="/admin">管理后台</a> / <a href="/readyz">严格体检</a> / <a href="/support">支持请求</a></td><td>保障发信、行情、备份和内容治理可用</td></tr>
    </tbody>
  </table>
</section>
"""
        self.send_html(
            "使用指南",
            body,
            meta={
                "title": "OurWorlds Quant 使用指南",
                "description": "按流程了解邮箱注册、模拟交易、组合设计、公开榜单、论坛复盘和后台运维。",
                "url": self.public_url("/guide"),
            },
            head=head,
        )

    def render_usage_demo(self, query, head: bool = False):
        frames = []
        step_links = []
        for idx, step in enumerate(USAGE_FLOW_STEPS, start=1):
            width = 24 + idx * 9
            frames.append(
                '<div class="demo-frame">'
                f'<span class="demo-path">{escape(step["path"])}</span>'
                f"<h3>{idx}. {escape(step['title'])}</h3>"
                f"<p>{escape(step['summary'])}</p>"
                '<div class="demo-screen">'
                f"<strong>{escape(step['title'])}</strong>"
                f'<div class="bar" style="width:{min(width, 92)}%"></div>'
                f"<p>{escape(step['detail'])}</p>"
                "</div>"
                "</div>"
            )
            step_links.append(f'<a href="{escape(step["path"], quote=True)}">第 {idx} 站: {escape(step["title"])}</a>')
        voice_path = usage_demo_voice_path()
        if voice_path.exists() and voice_path.stat().st_size > 0:
            voice_html = """
<div class="voice-box">
  <h3>EdgeTTS 语音解说</h3>
  <audio controls preload="metadata" src="/guide/demo/audio.mp3"></audio>
  <p class="muted">音频来自预生成的 EdgeTTS MP3,可用于现场演示或录屏。</p>
</div>
"""
        else:
            command = ".venv/bin/python -m src.app.server --env-file deploy/public.env --generate-demo-voice"
            voice_html = f"""
<div class="voice-box">
  <h3>EdgeTTS 语音解说</h3>
  <p>当前还没有生成演示 MP3。安装 edge-tts 后运行下面命令,页面会自动显示播放器。</p>
  <div class="voice-command">{escape(command)}</div>
  <p class="muted">默认 voice: {escape(DEFAULT_DEMO_TTS_VOICE)}。也可通过 OWQ_DEMO_TTS_VOICE 或 --demo-voice 指定其他 EdgeTTS 声音。</p>
</div>
"""
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>自动演示</h2>
  <p>演示会每 7 秒切换一站,串起公开了解、注册确认、密码登录、模拟交易、组合设计、公开展示和论坛复盘。浏览器偏好减少动画时会直接展开全部步骤。</p>
  <div class="demo-progress"><span></span></div>
  <div class="demo-board">
    <div class="demo-stage">{''.join(frames)}</div>
    <div>
      {voice_html}
      <div class="demo-steps">
        {''.join(step_links)}
      </div>
    </div>
  </div>
  <p><a class="btn" href="/guide">返回使用指南</a> <a class="btn secondary" href="/register">开始注册</a> <a class="btn secondary" href="/showcase/public">查看公开榜单</a></p>
</section>
"""
        self.send_html(
            "自动演示",
            body,
            meta={
                "title": "OurWorlds Quant 自动演示",
                "description": "自动演示 OurWorlds Quant 从注册、模拟交易、组合设计到公开复盘的完整使用流程。",
                "url": self.public_url("/guide/demo"),
            },
            head=head,
        )

    def render_usage_demo_audio(self, head: bool = False):
        voice_path = usage_demo_voice_path()
        if not voice_path.exists() or voice_path.stat().st_size <= 0:
            self.send_text("演示语音尚未生成。请运行 --generate-demo-voice。", "text/plain; charset=utf-8", status=404, head=head)
            return
        self.send_binary_file(voice_path, "audio/mpeg", head=head)

    STATIC_CONTENT_TYPES = {
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".map": "application/json; charset=utf-8",
    }

    def render_static_asset(self, path: str, head: bool = False):
        # Serve client assets from STATIC_DIR for the progressive-enhancement layer.
        # The CSP allows script-src 'self', so only same-origin files run; never any
        # attacker-writable location. Reject traversal/absolute paths before touching disk.
        rel = path[len("/static/"):]
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            self.not_found()
            return
        content_type = self.STATIC_CONTENT_TYPES.get(Path(rel).suffix.lower())
        if content_type is None:
            self.not_found()
            return
        target = (STATIC_DIR / rel).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self.not_found()
            return
        self.send_binary_file(target, content_type, head=head)

    def send_json(self, payload: dict, status: int = 200, user_id: int | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_security_headers("json")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if user_id is not None:
            self.send_header(
                "Set-Cookie",
                self.session_cookie_header(user_id=user_id),
            )
        self.end_headers()
        self.wfile.write(body)

    def send_json_headers(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_security_headers("json")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def send_text(self, payload: str, content_type: str, status: int = 200, head: bool = False):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_security_headers("asset")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_binary_file(self, path: Path, content_type: str, status: int = 200, head: bool = False):
        body = b"" if head else path.read_bytes()
        self.send_response(status)
        self.send_security_headers("asset")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_svg(self, payload: str, status: int = 200):
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_security_headers("asset")
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_csv(self, filename: str, headers: list[str], rows: list[list]):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(rows)
        body = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_security_headers("download")
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json_download(self, filename: str, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_security_headers("download")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(
        self,
        location: str,
        user_id: int | None = None,
        clear_cookie: bool = False,
        extra_cookies: list[str] | None = None,
    ):
        self.send_response(303)
        self.send_security_headers("redirect")
        self.send_header("Location", location)
        for cookie_header in extra_cookies or []:
            self.send_header("Set-Cookie", cookie_header)
        if user_id is not None:
            self.send_header(
                "Set-Cookie",
                self.session_cookie_header(user_id=user_id),
            )
        if clear_cookie:
            self.send_header(
                "Set-Cookie",
                self.session_cookie_header(clear=True),
            )
        self.end_headers()

    def public_indexable_path(self, path: str | None = None) -> bool:
        clean = path if path is not None else urlparse(self.path).path
        if clean in {
            "/",
            "/preview",
            "/lessons",
            "/research",
            "/data-status",
            "/guide",
            "/guide/demo",
            "/showcase/public",
            "/forum",
            "/legal",
            "/terms",
            "/privacy",
            "/risk",
            "/robots.txt",
            "/sitemap.xml",
        }:
            return True
        parts = clean.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "u" and parts[1].isdigit():
            return True
        if len(parts) == 3 and parts[0] == "u" and parts[1].isdigit() and parts[2] == "card.svg":
            return True
        if len(parts) == 2 and parts[0] == "forum" and parts[1].isdigit():
            return True
        return False

    def should_noindex_response(self, kind: str) -> bool:
        status = int(getattr(self, "_response_status", 0) or 0)
        if status >= 400:
            return True
        if kind in {"json", "download", "redirect"}:
            return True
        if kind in {"html", "asset"}:
            return not self.public_indexable_path()
        return False

    def send_security_headers(self, kind: str = "html") -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if self.should_noindex_response(kind):
            self.send_header("X-Robots-Tag", "noindex, nofollow")
        if self.request_scheme() == "https":
            max_age = hsts_max_age_seconds()
            if max_age > 0:
                self.send_header("Strict-Transport-Security", f"max-age={max_age}")
        if kind == "html":
            directives = [
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
                "font-src 'self' https://fonts.gstatic.com data:",
                "img-src 'self' data: https: http:",
                "connect-src 'self'",
                "form-action 'self'",
                "base-uri 'self'",
                "object-src 'none'",
                "frame-ancestors 'none'",
            ]
            if self.request_scheme() == "https":
                directives.append("upgrade-insecure-requests")
            csp = "; ".join(directives)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store")
        elif kind in {"json", "asset", "download", "redirect"}:
            self.send_header("Cache-Control", "no-store")

    def too_many_requests(self):
        self.send_html(
            "请求过于频繁",
            '<section class="card"><h2>请求过于频繁</h2><p>请稍后再试。</p></section>',
            status=429,
        )

    def payload_too_large(self, message: str):
        self.close_connection = True
        self.send_html(
            "请求内容过大",
            f'<section class="card"><h2>请求内容过大</h2><p>{escape(message)}</p></section>',
            status=413,
        )

    def bad_request(self, message: str):
        self.send_html(
            "请求无效",
            f'<section class="card"><h2>请求无效</h2><p>{escape(message)}</p></section>',
            status=400,
        )

    def server_error(self, head: bool = False, incident_id: int | None = None):
        incident_html = (
            f'<p class="muted">错误编号: #{int(incident_id)}</p>'
            if incident_id is not None
            else '<p class="muted">错误已记录,但暂时没有生成编号。</p>'
        )
        extra_headers = {"X-OurWorlds-Error-Id": str(int(incident_id))} if incident_id is not None else None
        if head:
            self.send_response(500)
            self.send_security_headers("html")
            for name, value in (extra_headers or {}).items():
                self.send_header(str(name), str(value))
            self.end_headers()
            return
        self.send_html(
            "服务暂时不可用",
            (
                '<section class="card"><h2>服务暂时不可用</h2>'
                '<p>请求处理失败,请稍后重试。若问题持续,请把错误编号发给管理员。</p>'
                f"{incident_html}</section>"
            ),
            status=500,
            extra_headers=extra_headers,
        )

    def not_found(self):
        self.send_html("未找到", '<div class="card"><h2>404</h2><p>页面不存在。</p></div>', 404)

    def forbidden(self, user=None):
        self.send_html("无权限", '<div class="card"><h2>403</h2><p>当前用户没有管理权限。</p></div>', 403, user=user)

    def message_html(self, query) -> str:
        msg = query.get("msg", [""])[0]
        err = query.get("err", [""])[0]
        if err:
            return f'<div class="msg err">{escape(err)}</div>'
        if msg:
            return f'<div class="msg">{escape(msg)}</div>'
        return ""

    def release_gate_html(self, checks: list[dict]) -> str:
        by_name = {str(row.get("name")): row for row in checks}
        groups = [
            (
                "真实数据",
                ("market_real_data", "market_freshness", "prediction_results", "market_sync_job"),
                "真实行情覆盖、行情新鲜度、预测候选匹配和自动同步任务状态达标。",
            ),
            (
                "注册发信",
                ("email_sending", "email_delivery_probe"),
                "配置真实事务邮件服务,并完成最近一次成功发信诊断。",
            ),
            (
                "测试入口",
                ("email_dev_auth_public", "email_dev_auth_public_links", "demo_contest_participants"),
                "公网正式运营必须关闭测试邮箱验证入口,隐藏测试链接,并移除演示/开发参赛账户。",
            ),
            (
                "安全配置",
                ("app_secret", "cookie_secure", "rate_limits", "legal_consent_gate", "admin_config", "admin_access", "request_body_limit"),
                "生产密钥、Secure Cookie、限流、法律条款补签、管理员和表单大小限制均已配置。",
            ),
            (
                "数据安全",
                ("app_db_integrity", "app_db_foreign_keys", "app_backup", "audit_retention", "email_login_session_retention"),
                "数据库一致性、备份和运营记录保留策略可验证。",
            ),
            (
                "运营处理",
                ("operational_queue", "recent_server_errors"),
                "支持请求、内容举报和近期服务端异常都处于可控状态。",
            ),
        ]
        rows = []
        ready = True
        for label, names, success_detail in groups:
            failures = []
            missing = []
            for name in names:
                row = by_name.get(name)
                if row is None:
                    missing.append(name)
                elif row.get("status") != "ok":
                    failures.append(f"{name}: {row.get('detail')}")
            ok = not failures and not missing
            ready = ready and ok
            if ok:
                status = '<span class="badge ok">通过</span>'
                detail = success_detail
            else:
                status = '<span class="badge bad">待处理</span>'
                parts = failures[:2]
                if len(failures) > 2:
                    parts.append(f"另有 {len(failures) - 2} 项未通过")
                if missing:
                    parts.append("缺少检查项: " + ", ".join(missing))
                detail = "；".join(str(part) for part in parts)
            rows.append(
                "<tr>"
                f"<td>{escape(label)}</td>"
                f"<td>{status}</td>"
                f"<td>{escape(detail)}</td>"
                "</tr>"
            )
        headline = "可进入正式发布" if ready else "正式发布前仍有待处理项"
        headline_class = "ok" if ready else "bad"
        guide = (
            "全部关键闸门已通过。上线前再运行 deploy/check-public.sh 做外网回归。"
            if ready
            else "按待处理项修复后,运行发信诊断和 deploy/check-public.sh。当前状态可用于 beta,但不应作为正式运营状态。"
        )
        return f"""
<section class="card">
  <h2>发布闸门 <span class="{headline_class}">{headline}</span></h2>
  <p>{escape(guide)}</p>
  <table><thead><tr><th>范围</th><th>状态</th><th>依据 / 下一步</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
	</section>
	"""

    def render_livez(self, head: bool = False):
        payload = {
            "status": "ok",
            "ok": True,
            "database": "ok",
            "started_at": iso_timestamp(SERVER_STARTED_AT),
            "uptime_seconds": int(time.time() - SERVER_STARTED_AT),
        }
        status = 200
        try:
            self.con.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - liveness should fail when DB access breaks
            payload["status"] = "degraded"
            payload["ok"] = False
            payload["database"] = type(exc).__name__
            status = 503
        if head:
            self.send_json_headers(payload, status=status)
        else:
            self.send_json(payload, status=status)

    def public_health_payload(self, payload: dict) -> dict:
        warnings = []
        for row in payload.get("checks", []):
            if not isinstance(row, dict) or row.get("status") == "ok":
                continue
            warnings.append(
                {
                    "name": str(row.get("name") or "")[:80],
                    "status": str(row.get("status") or "warn")[:20],
                    "required": str(row.get("required") or "false"),
                }
            )
        return {
            "status": payload.get("status", "degraded"),
            "ok": bool(payload.get("ok")),
            "strict": bool(payload.get("strict")),
            "required_warnings": int(payload.get("required_warnings") or 0),
            "optional_warnings": int(payload.get("optional_warnings") or 0),
            "warnings": warnings,
        }

    def health_payload_for_response(self, payload: dict) -> dict:
        return payload if self.health_detail_allowed() else self.public_health_payload(payload)

    def render_health(self, head: bool = False):
        payload = doctor.health(self.con)
        status = 200 if payload["ok"] else 503
        payload = self.health_payload_for_response(payload)
        if head:
            self.send_json_headers(payload, status=status)
        else:
            self.send_json(payload, status=status)

    def render_ready(self, head: bool = False):
        payload = doctor.health(self.con, strict=True)
        status = 200 if payload["ok"] else 503
        payload = self.health_payload_for_response(payload)
        if head:
            self.send_json_headers(payload, status=status)
        else:
            self.send_json(payload, status=status)

    def render_metrics(self, head: bool = False):
        payload = metrics_snapshot()
        if not self.health_detail_allowed():
            payload = {"status": payload.get("status", "ok"), "detail": "summary"}
        if head:
            self.send_json_headers(payload)
        else:
            self.send_json(payload)

    def public_url(self, path: str) -> str:
        clean = "/" + path.lstrip("/")
        return f"{self.base_url()}{clean}"

    def sitemap_date(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        candidate = text.split("T", 1)[0].split(" ", 1)[0]
        return candidate if len(candidate) == 10 and candidate.count("-") == 2 else ""

    def sitemap_entries(self) -> list[dict[str, str]]:
        entries = [
            {"path": "/", "changefreq": "daily", "priority": "1.0"},
            {"path": "/preview", "changefreq": "daily", "priority": "0.9"},
            {"path": "/lessons", "changefreq": "monthly", "priority": "0.8"},
            {"path": "/research", "changefreq": "weekly", "priority": "0.7"},
            {"path": "/data-status", "changefreq": "hourly", "priority": "0.8"},
            {"path": "/guide", "changefreq": "monthly", "priority": "0.7"},
            {"path": "/guide/demo", "changefreq": "monthly", "priority": "0.6"},
            {"path": "/showcase/public", "changefreq": "hourly", "priority": "0.9"},
            {"path": "/forum", "changefreq": "hourly", "priority": "0.8"},
            {"path": "/legal", "lastmod": LEGAL_VERSION, "changefreq": "monthly", "priority": "0.4"},
            {"path": "/terms", "lastmod": LEGAL_VERSION, "changefreq": "monthly", "priority": "0.4"},
            {"path": "/privacy", "lastmod": LEGAL_VERSION, "changefreq": "monthly", "priority": "0.4"},
            {"path": "/risk", "lastmod": LEGAL_VERSION, "changefreq": "monthly", "priority": "0.4"},
        ]
        for row in self.con.execute(
            """
            SELECT u.id, MAX(COALESCE(e.created_at, u.created_at)) AS lastmod
            FROM users u
            JOIN accounts a ON a.user_id=u.id
            LEFT JOIN equity_snapshots e ON e.account_id=a.id
            WHERE COALESCE(u.status, 'active')='active'
            GROUP BY u.id
            ORDER BY u.id DESC
            LIMIT 500
            """
        ).fetchall():
            item = {"path": f"/u/{int(row['id'])}", "changefreq": "daily", "priority": "0.6"}
            lastmod = self.sitemap_date(row["lastmod"])
            if lastmod:
                item["lastmod"] = lastmod
            entries.append(item)
        for row in self.con.execute(
            """
            SELECT p.id, p.created_at
            FROM forum_posts p
            JOIN users u ON u.id=p.user_id
            WHERE COALESCE(u.status, 'active')='active'
            ORDER BY p.id DESC
            LIMIT 500
            """
        ).fetchall():
            item = {"path": f"/forum/{int(row['id'])}", "changefreq": "weekly", "priority": "0.7"}
            lastmod = self.sitemap_date(row["created_at"])
            if lastmod:
                item["lastmod"] = lastmod
            entries.append(item)
        return entries

    def render_robots(self, head: bool = False):
        lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin",
            "Disallow: /account",
            "Disallow: /app",
            "Disallow: /auth/",
            "Disallow: /forgot-password",
            "Disallow: /login",
            "Disallow: /logout",
            "Disallow: /market",
            "Disallow: /portfolio-lab",
            "Disallow: /register",
            "Disallow: /livez",
            "Disallow: /metrics",
            "Disallow: /healthz",
            "Disallow: /readyz",
            "Disallow: /forum/new",
            "Disallow: /support",
            f"Sitemap: {self.public_url('/sitemap.xml')}",
            "",
        ]
        self.send_text("\n".join(lines), "text/plain; charset=utf-8", head=head)

    def render_sitemap(self, head: bool = False):
        rows = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for entry in self.sitemap_entries():
            rows.append("  <url>")
            rows.append(f"    <loc>{escape(self.public_url(entry['path']))}</loc>")
            if entry.get("lastmod"):
                rows.append(f"    <lastmod>{escape(entry['lastmod'])}</lastmod>")
            if entry.get("changefreq"):
                rows.append(f"    <changefreq>{escape(entry['changefreq'])}</changefreq>")
            if entry.get("priority"):
                rows.append(f"    <priority>{escape(entry['priority'])}</priority>")
            rows.append("  </url>")
        rows.append("</urlset>")
        self.send_text("\n".join(rows) + "\n", "application/xml; charset=utf-8", head=head)

    def render_legal(self, path: str):
        updated = LEGAL_VERSION
        pages = {
            "/terms": (
                "服务条款",
                f"""
<section class="card">
  <h2>服务条款</h2>
  <p class="muted">最后更新: {updated}</p>
  <p>OurWorlds Quant 提供量化研究、模拟交易、公开赛和策略讨论工具。使用本站即表示你理解:站内交易为模拟盘演练,不产生真实证券委托,也不代表任何收益承诺。</p>
  <h3>用户内容</h3>
  <p>你在论坛、个人页和比赛页发布的昵称、战绩、持仓快照、帖子和评论可能被公开展示。请不要发布违法、侵权、骚扰、广告、诱导交易或未经授权的个人信息。</p>
  <h3>服务边界</h3>
  <p>本站不提供证券投资顾问服务,不承诺数据实时性、完整性、连续可用性或模型有效性。运营方可以为了安全、合规、运维或产品调整暂停功能、删除违规内容或限制异常账户。</p>
  <h3>账户责任</h3>
  <p>你需要对账户行为和公开发言负责。发现异常登录、数据错误或需要删除公开内容时,应及时联系运营方处理。</p>
</section>
""",
            ),
            "/privacy": (
                "隐私说明",
                f"""
<section class="card">
  <h2>隐私说明</h2>
  <p class="muted">最后更新: {updated}</p>
  <p>本站只收集运行模拟盘和社区功能所需的数据,包括邮箱登录身份、用户名、密码哈希、昵称、头像、模拟账户、持仓、订单、资产快照、演练计划、论坛帖子、评论和支持请求。</p>
  <h3>数据用途</h3>
  <p>这些数据用于登录识别、模拟交易结算、公开赛排名、个人战绩页、论坛互动、系统安全、备份和故障排查。公开榜单、个人战绩页、战绩卡、论坛内容会被公开访问。</p>
  <h3>邮箱验证与账号登录</h3>
  <p>本站通过一次性邮件注册码或备用链接完成邮箱验证,会保存邮箱地址用于识别账户、发送验证邮件和处理必要的安全审计。用户设置的密码只保存哈希,不保存明文。发信密钥只应通过环境变量配置,不得写入代码仓库。</p>
  <h3>支持请求</h3>
  <p>通过联系支持页提交的邮箱、主题和问题描述会进入站内后台,仅用于处理注册、登录、数据、社区或商务请求。管理员处理和导出支持请求会写入审计日志。</p>
  <h3>导出与删除</h3>
  <p>登录后可以导出自己的完整账户数据、订单、持仓、资产曲线和关联支持请求。账户页提供自助关闭账户入口;关闭后会删除登录身份、模拟盘、社区内容和关联支持请求,安全审计日志会保留最小操作记录。</p>
</section>
""",
            ),
            "/risk": (
                "风险提示",
                f"""
<section class="card">
  <h2>风险提示</h2>
  <p class="muted">最后更新: {updated}</p>
  <p>本站内容仅用于技术研究、模拟训练和策略交流,不构成任何投资建议、收益承诺或买卖依据。</p>
  <h3>模拟盘风险</h3>
  <p>模拟交易无法完整反映真实市场冲击、滑点、流动性、停牌、涨跌停、委托排队、账户限制和心理因素。模拟盈利不代表实盘盈利。</p>
  <h3>数据与模型风险</h3>
  <p>行情数据、预测结果和研究报告可能存在延迟、缺失、错误、复权口径差异或样本偏差。任何模型结果都需要独立验证,不能直接作为真实交易依据。</p>
  <h3>交流边界</h3>
  <p>论坛讨论应围绕规则、数据、仓位、执行和复盘展开。请不要把他人的观点理解为投资建议,也不要发布诱导交易或承诺收益的内容。</p>
</section>
""",
            ),
        }
        if path == "/legal":
            title = "法律与风险"
            body = f"""
<section class="card">
  <h2>法律与风险</h2>
  <p class="muted">最后更新: {updated}</p>
  <p>正式运营前需要让用户明确理解服务边界、数据使用方式和模拟交易风险。以下页面构成站内公开说明。</p>
  <p><a class="btn" href="/terms">服务条款</a> <a class="btn secondary" href="/privacy">隐私说明</a> <a class="btn secondary" href="/risk">风险提示</a></p>
</section>
"""
        else:
            title, body = pages.get(path, pages["/risk"])
        self.send_html(
            title,
            body,
            user=self.current_user(),
            meta={
                "title": title,
                "description": "OurWorlds Quant 的服务条款、隐私说明和风险提示。",
                "url": f"{self.base_url()}{path}",
            },
        )

    def render_login(self, query):
        existing = self.current_user()
        if existing:  # already logged in — don't ask them to log in again
            self.redirect(services.post_auth_landing(self.con, existing["id"]))
            return
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>账号密码登录</h2>
  <p>使用邮箱验证后设置的用户名或邮箱登录。退出后再次进入模拟盘,从这里登录。</p>
  <p>如果你是早期测试账号且还没有设置密码,当前不能靠旧测试入口再次进入;请在邮箱发信开通后通过邮箱验证或重置密码设置登录方式,也可以先提交支持请求让管理员补账号密码。</p>
  <form method="post" action="/login">
    <label>用户名或邮箱</label>
    <input name="identifier" autocomplete="username" required value="" placeholder="username 或 you@example.com">
    <label>密码</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <p><button type="submit">登录模拟盘</button> <a class="btn secondary" href="/forgot-password">忘记密码</a> <a class="btn secondary" href="/register">邮箱注册</a> <a class="btn secondary" href="/support">联系支持</a></p>
  </form>
</section>
"""
        self.send_html("登录", body)

    def support_category_options(self, selected: str = "") -> str:
        labels = {
            "registration": "注册/登录",
            "account": "账户与数据",
            "data": "行情/预测数据",
            "community": "论坛/比赛",
            "business": "商务合作",
            "other": "其他",
        }
        current = services.normalize_support_category(selected)
        return "".join(
            f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
            for value, label in labels.items()
        )

    def default_support_category(self) -> str:
        return "other" if self.public_registration_available() else "registration"

    def render_support(self, query):
        user = self.current_user()
        email_value = str(user["email"] or "") if user else ""
        subject_value = ""
        join_mode = not self.public_registration_available()
        category_value = self.default_support_category()
        title = "申请加入" if join_mode else "联系支持"
        description = (
            "当前新用户注册暂未开放。请留下联系邮箱和参赛申请说明,管理员会在后台处理并联系你。"
            if join_mode
            else "注册、登录、数据、比赛、社区或商务问题都可以在这里提交。请求会进入站内后台,管理员处理后会保留状态和审计记录。"
        )
        subject_placeholder = "例如: 申请加入模拟盘公开赛" if join_mode else "例如: 无法收到注册确认邮件"
        message_placeholder = (
            "请简单说明你希望申请加入公开赛、需要开通的邮箱账号,以及是否已有测试账号。"
            if join_mode
            else "请写清楚你遇到的问题、相关页面和希望我们处理的事项。"
        )
        submit_label = "提交申请" if join_mode else "提交支持请求"
        csrf = csrf_input(user) if user else ""
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>{title}</h2>
  <p>{description}</p>
  <form method="post" action="/support">
    {csrf}
    <label>联系邮箱</label>
    <input name="email" type="email" required placeholder="you@example.com" value="{escape(email_value)}">
    <label>问题类型</label>
    <select name="category">{self.support_category_options(category_value)}</select>
    <label>主题</label>
    <input name="subject" required maxlength="120" value="{escape(subject_value)}" placeholder="{escape(subject_placeholder)}">
    <label>问题描述</label>
    <textarea name="message" required maxlength="3000" placeholder="{escape(message_placeholder)}"></textarea>
    <p><label><input type="checkbox" name="accept_terms" value="1" style="width:auto"> 我已阅读并同意 <a href="/terms">服务条款</a>、<a href="/privacy">隐私说明</a> 和 <a href="/risk">风险提示</a></label></p>
    <p><button type="submit">{submit_label}</button> <a class="btn secondary" href="/login">返回登录</a></p>
  </form>
</section>
"""
        self.send_html(
            title,
            body,
            user=user,
            meta={
                "title": f"{title} · OurWorlds Quant",
                "description": "提交注册、登录、数据、比赛、社区或商务支持请求。",
                "url": f"{self.base_url()}/support",
            },
        )

    def handle_support_request(self, form):
        user = self.current_user()
        if user and not verify_csrf(int(user["id"]), form.get("csrf")):
            self.audit_csrf_failed(user, "/support")
            self.redirect("/support?err=" + quote("表单已过期,请刷新后重试。"))
            return
        if form.get("accept_terms") != "1":
            self.redirect("/support?err=" + quote("请先阅读并同意服务条款、隐私说明和风险提示。"))
            return
        if self.public_registration_available():
            category = form.get("category", "other")
        else:
            category = self.default_support_category()
        try:
            request_id = services.create_support_request(
                self.con,
                form.get("email", ""),
                form.get("subject", ""),
                form.get("message", ""),
                category=category,
                requester_user_id=int(user["id"]) if user else None,
                ip_address=self.client_ip(),
                user_agent=self.headers.get("User-Agent", ""),
            )
        except services.RateLimitExceeded as exc:
            email_hash = ""
            try:
                email_hash = self.login_identifier_rate_limit_subject(form.get("email", "")).rsplit(":", 1)[-1][:16]
            except Exception:  # noqa: BLE001
                email_hash = ""
            self.audit_security_event(
                "security.rate_limited",
                user=user,
                target_type="rate_limit",
                target_id="support.request.email",
                detail={
                    "method": self.command,
                    "path": urlparse(self.path).path[:300],
                    "email_hash": email_hash,
                },
            )
            self.redirect("/support?err=" + quote(str(exc)))
            return
        except ValueError as exc:
            self.redirect("/support?err=" + quote(str(exc)))
            return
        self.audit(
            "support.request_create",
            user=user,
            target_type="support_request",
            target_id=request_id,
            detail={"category": services.normalize_support_category(category)},
        )
        self.redirect("/support?msg=" + quote("支持请求已提交,管理员会在后台处理。"))

    def handle_login(self, form):
        identifier = (form.get("identifier") or "").strip()
        password = form.get("password") or ""
        identifier_subject = self.login_identifier_rate_limit_subject(identifier)
        if not self.require_login_identifier_limit(identifier):
            return
        user_id = services.authenticate_user(self.con, identifier, password)
        if not user_id:
            self.audit_security_event(
                "security.login_failed",
                target_type="auth",
                target_id="password",
                detail={"identifier_type": "email" if "@" in identifier else "login_name"},
            )
            self.redirect("/login?err=" + quote("用户名/邮箱或密码不正确。"))
            return
        user = services.get_user(self.con, user_id)
        self.clear_rate_limit_subject("auth:login:identifier", identifier_subject)
        self.audit("auth.password_login", user=user, target_type="user", target_id=user_id)
        # Returning active users (have traded / saved a plan) go to the dashboard; brand-new
        # users get the guided learn home. Activity-derived → existing users auto-grandfathered.
        landing = services.post_auth_landing(self.con, user_id)
        msg = "登录成功。" if landing == "/app" else "登录成功,先从学习工作台开始(随时可进模拟盘)。"
        self.redirect(f"{landing}?msg=" + quote(msg), user_id=user_id)

    def render_register(self, query):
        existing = self.current_user()
        if existing:  # already logged in — send them into the product, not a signup form
            self.redirect(services.post_auth_landing(self.con, existing["id"]))
            return
        mode = self.auth_mode()
        if mode == "disabled":
            body = f"""
{self.message_html(query)}
<section class="card">
  <h2>邮箱注册暂未开放</h2>
  <p>当前环境没有配置可用的出站邮件服务,系统不会发送确认邮件,也不会用注册申请创建登录态。正式开放报名需要配置 Cloudflare Email Sending 或 SMTP。</p>
  <p>已有账号可以继续使用用户名/邮箱和密码登录;早期测试账号如果还没有设置密码,请先联系管理员补登录方式。</p>
  <p><a class="btn" href="/login">去登录</a> <a class="btn secondary" href="/support">联系支持</a> <a class="btn secondary" href="/">返回首页</a> <a class="btn secondary" href="/legal">查看服务说明</a></p>
</section>
"""
            self.send_html("注册", body)
            return
        if mode == "email_dev":
            if self.email_dev_auth_show_links():
                mode_note = "<p>当前启用本地邮箱测试注册:系统会在页面上显示一次性注册码和备用验证链接。正式运营必须配置真实发信服务并关闭 OWQ_EMAIL_DEV_AUTH。</p>"
            else:
                body = f"""
{self.message_html(query)}
<section class="card">
  <h2>邮箱注册暂未开放</h2>
  <p>当前公网环境尚未配置真实发信服务,新用户邮箱注册暂未开放;系统不会展示测试链接,也不会用注册申请创建登录态。已有账号可以继续使用用户名/邮箱和密码登录。</p>
  <p>正式开放报名后,系统会向邮箱发送一次性注册码;确认后设置用户名和密码,再回到登录页进入模拟盘。</p>
  <p><a class="btn" href="/login">去登录</a> <a class="btn secondary" href="/support">联系支持</a> <a class="btn secondary" href="/">先看看赛场</a> <a class="btn secondary" href="/legal">查看服务说明</a></p>
</section>
"""
                self.send_html("注册", body)
                return
        else:
            provider = self.email_sender_provider()
            provider_text = "Cloudflare Email Sending" if provider == "cloudflare" else "SMTP"
            mode_note = f"<p>邮箱注册码会发送到你的邮箱。当前发信服务: {provider_text}。</p>"
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>邮箱验证注册</h2>
  <p>输入邮箱后会收到一次性注册码。确认后需要设置用户名和密码,未来使用账号密码登录模拟盘。</p>
  {mode_note}
  <form method="post" action="/register">
    <label>邮箱</label>
    <input name="email" type="email" required placeholder="you@example.com" value="">
    <p><label><input type="checkbox" name="accept_terms" value="1" style="width:auto"> 我已阅读并同意 <a href="/terms">服务条款</a>、<a href="/privacy">隐私说明</a> 和 <a href="/risk">风险提示</a></label></p>
    <p><button type="submit">发送注册码</button> <a class="btn secondary" href="/login">已有账号登录</a> <a class="btn secondary" href="/">先看看赛场</a></p>
  </form>
</section>
"""
        self.send_html("注册", body)

    def render_forgot_password(self, query):
        mode = self.auth_mode()
        if mode == "disabled" or (mode == "email_dev" and not self.email_dev_auth_show_links()):
            reason = (
                "当前环境没有配置真实邮箱发信服务,暂不能自助重置密码。已有账号可以继续使用用户名/邮箱和密码登录;忘记密码时请联系管理员。"
                if mode == "disabled"
                else "当前公网环境尚未配置真实邮箱发信服务,暂不能通过页面自助重置密码。已有账号可以继续登录。"
            )
            body = f"""
{self.message_html(query)}
<section class="card">
  <h2>重置登录密码</h2>
  <p>{reason}</p>
  <p><a class="btn" href="/login">去登录</a> <a class="btn secondary" href="/support">联系支持</a> <a class="btn secondary" href="/register">邮箱注册</a></p>
</section>
"""
            self.send_html("忘记密码", body)
            return
        if mode == "email_dev":
            mode_note = "<p>当前启用本地邮箱测试重置:已存在账号会在页面上显示一次性重置注册码和备用链接。正式运营必须配置真实发信服务。</p>"
        else:
            provider = self.email_sender_provider()
            provider_text = "Cloudflare Email Sending" if provider == "cloudflare" else "SMTP"
            mode_note = f"<p>如果邮箱已注册,系统会发送一次性设置/重置密码注册码。当前发信服务: {provider_text}。</p>"
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>重置登录密码</h2>
  <p>输入注册邮箱后,如果该邮箱已有账号,会收到一封一次性设置/重置密码邮件。注册码 15 分钟内有效,且只能使用一次。</p>
  {mode_note}
  <form method="post" action="/forgot-password">
    <label>注册邮箱</label>
    <input name="email" type="email" required placeholder="you@example.com" value="">
    <p><label><input type="checkbox" name="accept_terms" value="1" style="width:auto"> 我已阅读并同意 <a href="/terms">服务条款</a>、<a href="/privacy">隐私说明</a> 和 <a href="/risk">风险提示</a></label></p>
    <p><button type="submit">发送重置码</button> <a class="btn secondary" href="/login">返回登录</a> <a class="btn secondary" href="/register">邮箱注册</a></p>
  </form>
</section>
"""
        self.send_html("忘记密码", body)

    def render_password_reset_request_result(self, email: str):
        body = """
<section class="card">
  <h2>重置密码邮件已处理</h2>
  <p>如果该邮箱已经注册,我们会发送一封一次性设置/重置密码邮件。请在 15 分钟内使用注册码确认并设置新密码。</p>
  <p><a class="btn" href="/login">返回登录</a> <a class="btn secondary" href="/forgot-password">重新填写邮箱</a></p>
</section>
"""
        self.send_html("重置密码", body)

    def handle_register_start(self, form):
        mode = self.auth_mode()
        if mode == "disabled":
            self.redirect("/register?err=" + quote("邮箱注册暂未开放。"))
            return
        if form.get("accept_terms") != "1":
            self.redirect("/register?err=" + quote("请先阅读并同意服务条款、隐私说明和风险提示。"))
            return
        try:
            email = services.normalize_email(form.get("email", ""))
            if mode == "email_dev" and not self.email_dev_auth_show_links():
                self.redirect("/register?err=" + quote("公网测试验证链接已关闭。请等待真实发信服务配置完成后再注册。"))
                return
            token, code = services.create_email_login_session(
                self.con,
                email,
                terms_version=LEGAL_VERSION,
                privacy_version=LEGAL_VERSION,
                risk_version=LEGAL_VERSION,
                return_code=True,
            )
        except ValueError as exc:
            self.redirect("/register?err=" + quote(str(exc)))
            return
        self.audit(
            "legal.accept_before_email",
            target_type="email_login_session",
            target_id=services.email_token_hash(token)[:16],
            detail={"version": LEGAL_VERSION, "mode": mode},
        )
        if mode == "email_dev":
            login_url = self.email_login_url(token)
            body = f"""
<section class="card">
  <h2>测试邮箱验证链接已生成</h2>
  <p>邮箱: {escape(email)}</p>
  <p>测试注册码: <code>{escape(code)}</code></p>
  <p>正式运营配置发信服务后,这里会改为发送邮件,不会展示注册码和链接。确认后需要设置用户名和密码,再使用账号密码登录。</p>
  <p><a class="btn" href="/auth/email/confirm">输入注册码</a> <a class="btn secondary" href="{escape(login_url, quote=True)}">打开备用确认链接</a></p>
</section>
"""
            self.send_html("邮箱验证", body)
            return
        try:
            provider = self.send_login_email(email, token, code)
            services.mark_email_login_sent(self.con, token)
        except Exception as exc:  # noqa: BLE001
            services.delete_email_login_session(self.con, token)
            self.audit(
                "auth.email_send_failed",
                target_type="email_login_session",
                target_id=services.email_token_hash(token)[:16],
                detail=exception_diagnostic(exc),
            )
            self.redirect("/register?err=" + quote(email_public_failure_message()))
            return
        self.audit(
            "auth.email_sent",
            target_type="email_login_session",
            target_id=services.email_token_hash(token)[:16],
            detail={"provider": provider},
        )
        body = f"""
<section class="card">
  <h2>验证邮件已发送</h2>
  <p>我们已经向 {escape(email)} 发送了一封一次性邮箱验证邮件。注册码 15 分钟内有效,确认后需要设置用户名和密码,再使用账号密码登录。</p>
  <p><a class="btn" href="/auth/email/confirm">输入注册码</a> <a class="btn secondary" href="/register">换一个邮箱</a></p>
</section>
"""
        self.send_html("邮箱验证", body)

    def handle_forgot_password_start(self, form):
        mode = self.auth_mode()
        if mode == "disabled" or (mode == "email_dev" and not self.email_dev_auth_show_links()):
            self.redirect("/forgot-password?err=" + quote("当前暂不能自助重置密码,请使用已有密码登录或联系管理员。"))
            return
        if form.get("accept_terms") != "1":
            self.redirect("/forgot-password?err=" + quote("请先阅读并同意服务条款、隐私说明和风险提示。"))
            return
        try:
            email = services.normalize_email(form.get("email", ""))
        except ValueError as exc:
            self.redirect("/forgot-password?err=" + quote(str(exc)))
            return
        email_hash = services.email_token_hash(email)[:16]
        existing_user = services.get_user_by_email(self.con, email)
        known_account = bool(existing_user)
        has_password = bool(existing_user and str(existing_user["password_hash"] or "").strip())
        self.audit(
            "auth.password_reset_requested",
            target_type="email",
            target_id=email_hash,
            detail={"known_account": "1" if known_account else "0", "has_password": "1" if has_password else "0", "mode": mode},
        )
        if not known_account:
            self.render_password_reset_request_result(email)
            return
        try:
            token, code = services.create_email_login_session(
                self.con,
                email,
                terms_version=LEGAL_VERSION,
                privacy_version=LEGAL_VERSION,
                risk_version=LEGAL_VERSION,
                return_code=True,
            )
        except ValueError as exc:
            self.redirect("/forgot-password?err=" + quote(str(exc)))
            return
        if mode == "email_dev":
            reset_url = self.email_login_url(token)
            body = f"""
<section class="card">
  <h2>测试重置密码链接已生成</h2>
  <p>邮箱: {escape(email)}</p>
  <p>测试重置码: <code>{escape(code)}</code></p>
  <p>正式运营配置发信服务后,这里会改为发送邮件,不会展示注册码和链接。确认后设置新密码,再使用账号密码登录。</p>
  <p><a class="btn" href="/auth/email/confirm">输入重置码</a> <a class="btn secondary" href="{escape(reset_url, quote=True)}">打开备用重置链接</a> <a class="btn secondary" href="/login">返回登录</a></p>
</section>
"""
            self.audit(
                "auth.password_reset_link_generated",
                target_type="email_login_session",
                target_id=services.email_token_hash(token)[:16],
                detail={"mode": mode},
            )
            self.send_html("重置密码", body)
            return
        try:
            provider = self.send_password_reset_email(email, token, code)
            services.mark_email_login_sent(self.con, token)
        except Exception as exc:  # noqa: BLE001
            services.delete_email_login_session(self.con, token)
            self.audit(
                "auth.password_reset_email_failed",
                target_type="email_login_session",
                target_id=services.email_token_hash(token)[:16],
                detail=exception_diagnostic(exc),
            )
            self.redirect("/forgot-password?err=" + quote(email_public_failure_message()))
            return
        self.audit(
            "auth.password_reset_email_sent",
            target_type="email_login_session",
            target_id=services.email_token_hash(token)[:16],
            detail={"provider": provider},
        )
        self.render_password_reset_request_result(email)

    def handle_logout(self, form):
        user = self.current_user()
        if not user:
            self.redirect("/login", clear_cookie=True)
            return
        if not verify_csrf(int(user["id"]), form.get("csrf")):
            self.audit_csrf_failed(user, "/account")
            self.redirect("/account?err=" + quote("表单已过期,请刷新后重试。"))
            return
        services.bump_user_session_version(self.con, int(user["id"]))
        self.audit("auth.logout", user=user, target_type="user", target_id=user["id"])
        self.redirect("/login?msg=" + quote("已退出登录。"), clear_cookie=True)

    def render_email_confirm(self, query):
        token = query.get("token", [""])[0]
        if token:
            state, error = self.email_confirm_state(token)
            if error:
                self.redirect(
                    "/register?err=" + quote(error),
                    extra_cookies=[self.email_confirm_cookie_header(clear=True)],
                )
                return
            self.redirect(
                "/auth/email/confirm",
                extra_cookies=[self.email_confirm_cookie_header(token)],
            )
            return
        handle = self.current_email_confirm_token()
        if not handle:
            self.render_email_code_entry(query)
            return
        state, error = self.email_confirm_state(handle)
        if error:
            self.redirect(
                "/auth/email/confirm?err=" + quote(error),
                extra_cookies=[self.email_confirm_cookie_header(clear=True)],
            )
            return
        existing_user = services.get_user_by_email(self.con, state.get("email") or "")
        suggested = (
            str(existing_user["login_name"] or "").strip()
            if existing_user and existing_user["login_name"]
            else services.suggest_login_name(state.get("email") or "")
        )
        is_reset = bool(existing_user and existing_user["password_hash"])
        title = "重置登录密码" if is_reset else "设置登录账号"
        description = (
            "邮箱已确认。请确认用户名并设置新密码;完成后需要回到登录页使用新密码进入模拟盘。"
            if is_reset
            else "邮箱已确认。请设置用户名和密码;完成后需要回到登录页使用账号密码进入模拟盘。"
        )
        button = "重置密码并去登录" if is_reset else "设置密码并去登录"
        retry_path = "/forgot-password" if is_reset else "/register"
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>{title}</h2>
  <p>邮箱: {escape(state.get("email") or "")}</p>
  <p>{escape(description)}</p>
  <form method="post" action="/auth/email/confirm">
    <label>用户名</label>
    <input name="login_name" autocomplete="username" required pattern="[a-z0-9][a-z0-9_-]{{2,31}}" value="{escape(suggested)}" placeholder="3-32 位小写字母、数字、_ 或 -">
    <label>密码</label>
    <input name="password" type="password" autocomplete="new-password" required minlength="10">
    <label>确认密码</label>
    <input name="password_confirm" type="password" autocomplete="new-password" required minlength="10">
    <p><button type="submit">{button}</button> <a class="btn secondary" href="{retry_path}">重新获取邮件</a></p>
  </form>
</section>
"""
        self.send_html("设置登录账号", body)

    def render_email_code_entry(self, query):
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>输入邮箱注册码</h2>
  <p>请输入邮件里的 8 位注册码。确认后即可设置登录密码;忘记密码时也可以用邮件里的重置码进入同一个确认流程。</p>
  <form method="post" action="/auth/email/code">
    <label>邮箱</label>
    <input name="email" type="email" required autocomplete="email" placeholder="you@example.com">
    <label>注册码</label>
    <input name="code" inputmode="numeric" autocomplete="one-time-code" required pattern="[0-9 ]{{8,15}}" placeholder="8 位数字">
    <p><button type="submit">确认注册码</button> <a class="btn secondary" href="/register">重新注册</a> <a class="btn secondary" href="/forgot-password">忘记密码</a></p>
  </form>
</section>
"""
        self.send_html("邮箱注册码", body)

    def email_confirm_handle_hash(self, handle: str | None) -> str:
        value = str(handle or "").strip()
        if not value:
            return ""
        if value.startswith("hash:"):
            try:
                return services.normalize_email_login_session_hash(value[5:])
            except ValueError:
                return ""
        return services.email_token_hash(value)

    def email_confirm_state(self, handle: str | None) -> tuple[dict, str]:
        token_hash = self.email_confirm_handle_hash(handle)
        if not token_hash:
            return {}, "验证会话无效。"
        acceptance = services.email_login_legal_acceptance_by_hash(self.con, token_hash)
        if not acceptance:
            return {}, "请先阅读并同意服务条款、隐私说明和风险提示后再登录。"
        if (
            acceptance.get("accepted_terms_version") != LEGAL_VERSION
            or acceptance.get("accepted_privacy_version") != LEGAL_VERSION
            or acceptance.get("accepted_risk_version") != LEGAL_VERSION
        ):
            return {}, "服务条款已更新,请重新阅读并获取注册码。"
        state = services.email_login_session_status_by_hash(self.con, token_hash)
        if state["status"] != "pending":
            message = {
                "confirmed": "验证码已使用,请重新获取邮件。",
                "expired": "验证码已过期,请重新获取邮件。",
                "missing": "验证码无效,请重新获取邮件。",
            }.get(state["status"], "验证码不可用,请重新获取邮件。")
            return {}, message
        return state, ""

    def handle_email_code_confirm(self, form):
        try:
            email = services.normalize_email(form.get("email", ""))
            code_result = services.verify_email_login_code(self.con, email, form.get("code", ""))
        except ValueError as exc:
            self.audit_security_event(
                "security.email_code_failed",
                target_type="email_login_session",
                detail={"reason": str(exc)[:80]},
            )
            self.redirect("/auth/email/confirm?err=" + quote(str(exc)))
            return
        token_hash = code_result["token_hash"]
        self.audit(
            "auth.email_code_verified",
            target_type="email_login_session",
            target_id=token_hash[:16],
            detail={"email_hash": services.email_token_hash(email)[:16]},
        )
        self.redirect(
            "/auth/email/confirm",
            extra_cookies=[self.email_confirm_cookie_header(f"hash:{token_hash}")],
        )

    def handle_email_confirm(self, form):
        handle = form.get("token", "") or self.current_email_confirm_token()
        token_hash = self.email_confirm_handle_hash(handle)
        state, error = self.email_confirm_state(handle)
        if error:
            self.redirect(
                "/register?err=" + quote(error),
                extra_cookies=[self.email_confirm_cookie_header(clear=True)],
            )
            return
        login_name = (form.get("login_name") or "").strip()
        password = form.get("password") or ""
        if password != (form.get("password_confirm") or ""):
            self.redirect("/auth/email/confirm?err=" + quote("两次输入的密码不一致。"))
            return
        existing_user = services.get_user_by_email(self.con, state.get("email") or "")
        try:
            normalized_login_name = services.ensure_login_name_available(
                self.con,
                login_name,
                user_id=int(existing_user["id"]) if existing_user else None,
            )
            services.validate_password(password)
        except ValueError as exc:
            self.redirect("/auth/email/confirm?err=" + quote(str(exc)))
            return
        try:
            user_id = services.confirm_email_login_session_by_hash(self.con, token_hash)
            services.set_user_password(
                self.con,
                user_id,
                normalized_login_name,
                password,
                update_nickname=existing_user is None,
            )
        except ValueError as exc:
            self.redirect(
                "/register?err=" + quote(str(exc)),
                extra_cookies=[self.email_confirm_cookie_header(clear=True)],
            )
            return
        user = services.get_user(self.con, user_id)
        consent_id = self.record_current_consent(user_id, "email_login")
        self.audit("auth.email_confirm", user=user, target_type="user", target_id=user_id)
        self.audit("auth.password_set", user=user, target_type="user", target_id=user_id)
        self.audit("legal.consent", user=user, target_type="user_consent", target_id=consent_id, detail={"version": LEGAL_VERSION, "source": "email_login"})
        self.redirect(
            "/login?msg=" + quote("邮箱已验证,请用用户名/邮箱和密码登录。"),
            extra_cookies=[self.email_confirm_cookie_header(clear=True)],
        )

    def render_wechat_status(self, query):
        token = query.get("token", [""])[0]
        if not token:
            self.send_json({"status": "missing"})
            return
        state = services.wechat_session_status(self.con, token)
        user_id = state.get("user_id") if state["status"] == "confirmed" else None
        self.send_json({"status": state["status"]}, user_id=user_id)

    def render_qr(self, token: str):
        state = services.wechat_session_status(self.con, token)
        if state["status"] != "pending" or not self.wechat_session_has_current_legal_acceptance(token):
            self.not_found()
            return
        target = self.auth_target_url(token)
        try:
            import qrcode

            image = qrcode.make(target)
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            payload = buf.getvalue()
            self.send_response(200)
            self.send_security_headers("asset")
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except Exception:
            pass

        text = escape(target)
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="220" height="220" viewBox="0 0 220 220">
<rect width="220" height="220" fill="#fff"/>
<rect x="18" y="18" width="56" height="56" fill="#111"/><rect x="30" y="30" width="32" height="32" fill="#fff"/>
<rect x="146" y="18" width="56" height="56" fill="#111"/><rect x="158" y="30" width="32" height="32" fill="#fff"/>
<rect x="18" y="146" width="56" height="56" fill="#111"/><rect x="30" y="158" width="32" height="32" fill="#fff"/>
<text x="110" y="108" text-anchor="middle" font-size="12" fill="#111">Install qrcode</text>
<text x="110" y="126" text-anchor="middle" font-size="10" fill="#555">or open link</text>
<text x="110" y="142" text-anchor="middle" font-size="8" fill="#555">{text[:42]}</text>
        </svg>""".encode()
        self.send_response(200)
        self.send_security_headers("asset")
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(svg)))
        self.end_headers()
        self.wfile.write(svg)

    def render_dev_confirm(self, query):
        if not self.dev_auth_enabled():
            self.send_html(
                "注册暂未开放",
                '<section class="card"><h2>注册暂未开放</h2><p>当前环境未开启测试扫码确认页。</p><p><a href="/register">返回注册页</a></p></section>',
                status=403,
            )
            return
        token = query.get("token", [""])[0]
        body = f"""
<section class="card">
  <h2>确认微信扫码注册</h2>
  <p>这是测试确认页,用于在没有微信开放平台凭据时验证完整参赛流程。正式运营应关闭该入口。</p>
  <form method="post" action="/auth/wechat/dev-confirm">
    <input type="hidden" name="token" value="{escape(token)}">
    <label>昵称</label>
    <input name="nickname" value="参赛用户">
    <p><label><input type="checkbox" name="accept_terms" value="1" style="width:auto"> 我已阅读并同意 <a href="/terms">服务条款</a>、<a href="/privacy">隐私说明</a> 和 <a href="/risk">风险提示</a></label></p>
    <p><button type="submit">确认注册并进入模拟盘</button></p>
  </form>
</section>
"""
        self.send_html("扫码确认", body)

    def handle_dev_confirm(self, form):
        if not self.dev_auth_enabled():
            self.redirect("/register?err=" + quote("当前环境未开启测试扫码确认页。"))
            return
        if form.get("accept_terms") != "1":
            self.redirect("/register?err=" + quote("请先阅读并同意服务条款、隐私说明和风险提示。"))
            return
        try:
            user_id = services.confirm_wechat_session(
                self.con,
                form.get("token", ""),
                form.get("nickname", ""),
            )
        except ValueError as exc:
            self.redirect("/register?err=" + quote(str(exc)))
            return
        consent_id = self.record_current_consent(user_id, "dev_confirm")
        self.audit("auth.dev_confirm", user=services.get_user(self.con, user_id), target_type="user", target_id=user_id)
        self.audit("legal.consent", user=services.get_user(self.con, user_id), target_type="user_consent", target_id=consent_id, detail={"version": LEGAL_VERSION, "source": "dev_confirm"})
        self.redirect("/learn?msg=" + quote("注册成功,先从学习工作台开始。"), user_id=user_id)

    def render_wechat_callback(self, query):
        code = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        if not code or not state:
            body = """
<section class="card">
  <h2>微信回调参数缺失</h2>
  <p>缺少 code 或 state。请从注册页重新扫码。</p>
  <p><a href="/register">返回注册页</a></p>
</section>
"""
            self.send_html("微信回调", body)
            return
        if not self.wechat_session_has_current_legal_acceptance(state):
            self.redirect("/register?err=" + quote("请先阅读并同意服务条款、隐私说明和风险提示后再扫码登录。"))
            return
        try:
            user_id = services.confirm_wechat_oauth_code(self.con, state, code)
        except ValueError as exc:
            self.redirect("/register?err=" + quote(str(exc)))
            return
        consent_id = self.record_current_consent(user_id, "wechat_callback")
        self.audit("auth.wechat_callback", user=services.get_user(self.con, user_id), target_type="user", target_id=user_id)
        self.audit("legal.consent", user=services.get_user(self.con, user_id), target_type="user_consent", target_id=consent_id, detail={"version": LEGAL_VERSION, "source": "wechat_callback"})
        self.redirect("/learn?msg=" + quote("微信扫码注册成功,先从学习工作台开始。"), user_id=user_id)

    def learning_difficulty_options(self, selected: str = "beginner") -> str:
        current = services.normalize_learning_difficulty(selected)
        return "".join(
            f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
            for value, label in services.LEARNING_DIFFICULTIES.items()
        )

    def learning_template_options(self, selected: str = "reversal", include_risk: bool = True) -> str:
        current = services.normalize_learning_template(selected)
        items = services.LEARNING_TEMPLATES.items()
        if not include_risk:
            items = [(value, label) for value, label in items if value != "risk_review"]
        return "".join(
            f'<option value="{escape(value)}"{" selected" if value == current else ""}>{escape(label)}</option>'
            for value, label in items
        )

    def learning_preset_cards(self, user, ai_ready: bool) -> str:
        cards = []
        for preset in LEARNING_PRESETS:
            chips = (
                f'<span class="badge">{escape(preset["level"])}</span>'
                f'<span class="badge">{escape(services.LEARNING_TEMPLATES[preset["template"]])}</span>'
            )
            content = (
                f"{chips}"
                f"<strong>{escape(preset['title'])}</strong>"
                f"<p>{escape(preset['summary'])}</p>"
            )
            if ai_ready:
                cards.append(
                    '<form class="preset-form" method="post" action="/learn/coach">'
                    f"{csrf_input(user)}"
                    f'<input type="hidden" name="goal" value="{escape(preset["goal"], quote=True)}">'
                    f'<input type="hidden" name="difficulty" value="{escape(preset["difficulty"])}">'
                    f'<input type="hidden" name="template" value="{escape(preset["template"])}">'
                    f'<button type="submit" class="preset-card">{content}</button>'
                    "</form>"
                )
            else:
                cards.append(
                    '<a class="preset-card" href="/account/ai">'
                    f"{content}"
                    '<p><strong>先配置 DeepSeek API key 后可一键创建。</strong></p>'
                    "</a>"
                )
        return '<div class="preset-grid">' + "".join(cards) + "</div>"

    def learning_task_id_from_path(self, path: str) -> int:
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "learn" or parts[1] != "tasks":
            raise ValueError("学习任务路径无效")
        return int(parts[2])

    def render_learn(self, user, query):
        key_row = ai_service.get_key_row(self.con, user["id"])
        ai_ready = key_row is not None and bool(int(key_row["enabled"]))
        task_rows = services.learning_tasks(self.con, user["id"], limit=6)
        tasks_html = "".join(
            '<tr>'
            f'<td><a href="/learn/tasks/{int(task["id"])}">#{int(task["id"])}</a></td>'
            f'<td>{escape(task["goal"])}</td>'
            f'<td>{escape(services.LEARNING_TEMPLATES.get(task["template"], task["template"]))}</td>'
            f'<td>{escape(task["status"])}</td>'
            f'<td>{escape(task["created_at"])}</td>'
            '</tr>'
            for task in task_rows
        ) or '<tr><td colspan="5" class="muted">暂无学习任务。先写一个目标,让 AI 教练帮你拆解。</td></tr>'
        ai_box = (
            """
<div class="msg">
  DeepSeek API key 已配置。你可以直接创建学习任务,AI 会先做方法拆解,不会替你下单。
</div>
"""
            if ai_ready
            else """
<div class="msg err">
  还没有配置 DeepSeek API key。课程可以先看,AI 教练需要先到“账户 → AI 教练配置”填入你自己的 key。
</div>
"""
        )
        submit = (
            '<button type="submit">让 AI 拆解目标</button>'
            if ai_ready
            else '<a class="btn" href="/account/ai">配置 DeepSeek API key</a>'
        )
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>新手 AI 学习工作台</h2>
  <p>这里先帮你理解量化投资的基本环节,再把一个学习目标拆成可演练、可记录、可复盘的模拟盘任务。</p>
  <p><a class="btn secondary" href="/lessons">先看:量化三大坑(免登录、免 key)</a> <a class="btn secondary" href="/app">想直接上手?进入模拟盘 →</a></p>
  <div class="flow-map">
    <div class="flow-step"><span>STEP 1</span><strong>量化投资是什么</strong><p>用数据、规则和程序把投资想法转成可检验流程。它不是 AI 替你预测涨跌,也不是自动稳赚。</p></div>
    <div class="flow-step"><span>STEP 2</span><strong>你需要理解哪些环节</strong><p>数据、交易规则、策略假设、回测陷阱、模拟盘执行和复盘,分别解决不同问题。</p></div>
    <div class="flow-step"><span>STEP 3</span><strong>目标如何拆成任务</strong><p>把“我想学习量化”拆成一个观察模板、几个参数、一组记录问题和一次可复盘练习。</p></div>
    <div class="flow-step"><span>STEP 4</span><strong>以练代学</strong><p>AI 先解释方法,系统生成候选草稿,你确认后保存为待执行计划,再用模拟结果复盘。</p></div>
  </div>
</section>
<section class="card">
  <h2>不知道问什么? 从这些任务开始</h2>
  <p>按你的基础选择一个常见问题,系统会直接把它送给 AI 教练拆解成学习任务。</p>
  {self.learning_preset_cards(user, ai_ready)}
</section>
<section class="grid">
  <div class="card">
    <h2>创建学习任务</h2>
    {ai_box}
    <form method="post" action="/learn/coach">
      {csrf_input(user)}
      <label>我想学习或练习的目标</label>
      <textarea name="goal" maxlength="500" required placeholder="例如: 我想学习如何用 AI 帮我设计一个低风险的量化练习,并知道应该看哪些指标。"></textarea>
      <div class="row">
        <div><label>当前基础</label><select name="difficulty">{self.learning_difficulty_options()}</select></div>
        <div><label>练习模板</label><select name="template">{self.learning_template_options()}</select></div>
      </div>
      <p>{submit}</p>
    </form>
  </div>
  <div class="card">
    <h2>新手边界</h2>
    <ul class="guide-list">
      <li>AI 只做方法教学、目标拆解和草稿说明。</li>
      <li>具体候选由系统按行情/预测数据确定,不是模型荐股。</li>
      <li>保存后只是待执行计划,不会自动成交。</li>
      <li>执行、取消、复盘都由你自己确认。</li>
    </ul>
    <p><a class="btn secondary" href="/app">进入高级模拟盘</a> <a class="btn secondary" href="/account/ai">AI 教练配置</a></p>
  </div>
</section>
<section class="card">
  <h2>最近学习任务</h2>
  <table><thead><tr><th>ID</th><th>目标</th><th>模板</th><th>状态</th><th>创建时间</th></tr></thead><tbody>{tasks_html}</tbody></table>
</section>
"""
        self.send_html("学习工作台", body, user=user)

    def learning_preview_rows_html(self, rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td>{escape(row['code'])}</td>"
            f"<td>{escape(row.get('name') or '-')}</td>"
            f"<td>{side_cn(row['side'])}</td>"
            f"<td>{int(row['qty'])}</td>"
            f"<td>{escape(row.get('rationale') or '-')}</td>"
            "</tr>"
            for row in rows
        )

    def render_learning_task_page(
        self,
        user,
        task,
        query,
        preview_rows: list[dict] | None = None,
        preview_error: str = "",
        preview_params: dict | None = None,
    ):
        preview_params = preview_params or {}
        template = services.normalize_learning_template(preview_params.get("template") or task["template"])
        qty = escape(str(preview_params.get("qty") or "100"))
        limit = escape(str(preview_params.get("limit") or "3"))
        strategy_name = escape(str(preview_params.get("strategy_name") or f"学习任务 {int(task['id'])} · {services.LEARNING_TEMPLATES[template]}"))
        rationale_note = escape(str(preview_params.get("rationale_note") or "按 AI 教练建议做小仓位观察,记录假设、风险和复盘问题。"))
        coach_text = render_markdown(task["coach_text"] or "AI 教练暂无输出。")
        saved_count = services.learning_task_signal_count(self.con, user["id"], int(task["id"]))
        preview_html = ""
        if preview_error:
            preview_html = f'<div class="msg err">{escape(preview_error)}</div>'
        elif preview_rows is not None:
            rows_html = self.learning_preview_rows_html(preview_rows) or '<tr><td colspan="5" class="muted">暂无候选</td></tr>'
            preview_html = f"""
<div class="msg">以下只是草稿预览,尚未写入模拟盘。确认后会保存为待执行演练计划。</div>
<table><thead><tr><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>依据</th></tr></thead><tbody>{rows_html}</tbody></table>
"""
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>学习任务 #{int(task['id'])}</h2>
  <p><span class="badge">{escape(services.LEARNING_DIFFICULTIES.get(task['difficulty'], task['difficulty']))}</span> <span class="badge">{escape(services.LEARNING_TEMPLATES.get(task['template'], task['template']))}</span> <span class="badge">{escape(task['status'])}</span></p>
  <h3>目标</h3>
  <p>{escape(task['goal'])}</p>
  <h3>AI 教练拆解</h3>
  <div class="markdown-body">{coach_text}</div>
  <p class="muted">已从该任务保存 {saved_count} 条演练计划。保存后仍需到高级模拟盘手动执行。</p>
</section>
<section class="card">
  <h2>演练计划草稿</h2>
  <form method="post" action="/learn/tasks/{int(task['id'])}/preview">
    {csrf_input(user)}
    <div class="formline">
      <div><label>练习模板</label><select name="template">{self.learning_template_options(template, include_risk=False)}</select></div>
      <div><label>数量/标的</label><input name="qty" type="number" min="100" step="100" value="{qty}"></div>
      <div><label>候选数</label><input name="limit" type="number" min="1" max="10" step="1" value="{limit}"></div>
      <button type="submit">预览草稿</button>
    </div>
    <label>策略名称</label>
    <input name="strategy_name" value="{strategy_name}" maxlength="80">
    <label>附加记录要求</label>
    <input name="rationale_note" value="{rationale_note}" maxlength="300">
  </form>
  {preview_html}
  <form method="post" action="/learn/tasks/{int(task['id'])}/save-signals">
    {csrf_input(user)}
    <input type="hidden" name="template" value="{escape(template)}">
    <input type="hidden" name="qty" value="{qty}">
    <input type="hidden" name="limit" value="{limit}">
    <input type="hidden" name="strategy_name" value="{strategy_name}">
    <input type="hidden" name="rationale_note" value="{rationale_note}">
    <p><button type="submit">确认保存为待执行计划</button> <a class="btn secondary" href="/app">去高级模拟盘</a> <a class="btn secondary" href="/learn">返回学习工作台</a></p>
  </form>
</section>
"""
        self.send_html("学习任务", body, user=user)

    def render_learning_task(self, user, path, query):
        try:
            task_id = self.learning_task_id_from_path(path)
        except ValueError:
            self.not_found()
            return
        task = services.learning_task(self.con, user["id"], task_id)
        if task is None:
            self.not_found()
            return
        self.render_learning_task_page(user, task, query)

    def handle_learning_coach(self, user, form):
        if not self.require_user_write_limit(user, "learning_coach", 8, 3600, "/learn"):
            return
        difficulty = services.normalize_learning_difficulty(form.get("difficulty", "beginner"))
        template = services.normalize_learning_template(form.get("template", "reversal"))
        result = ai_service.coach_learning_goal(
            self.con,
            user["id"],
            secret=SECRET,
            leak_check_secrets=self.sensitive_secret_values(),
            goal=form.get("goal", ""),
            difficulty=difficulty,
            template=template,
        )
        self.audit(
            "ai.learning_coach",
            user=user,
            target_type="ai",
            detail={"ok": result["ok"], "blocked": result.get("blocked", False), "error": result.get("error", "")},
        )
        if not result["ok"]:
            self.redirect("/learn?err=" + quote(result["text"]))
            return
        try:
            task_id = services.create_learning_task(
                self.con,
                user["id"],
                form.get("goal", ""),
                difficulty,
                template,
                result["text"],
            )
        except ValueError as exc:
            self.redirect("/learn?err=" + quote(str(exc)))
            return
        self.audit("learning.task_create", user=user, target_type="learning_task", target_id=task_id, detail={"template": template})
        self.redirect(f"/learn/tasks/{task_id}?msg=" + quote("AI 教练已完成目标拆解。"))

    def handle_learning_task_preview(self, user, path, form):
        try:
            task_id = self.learning_task_id_from_path(path)
        except ValueError:
            self.not_found()
            return
        task = services.learning_task(self.con, user["id"], task_id)
        if task is None:
            self.not_found()
            return
        params = {
            "template": form.get("template", task["template"]),
            "qty": form.get("qty", "100"),
            "limit": form.get("limit", "3"),
            "strategy_name": form.get("strategy_name", ""),
            "rationale_note": form.get("rationale_note", ""),
        }
        try:
            rows = services.learning_template_rows(
                self.con,
                user["id"],
                params["template"],
                qty=params["qty"],
                limit=int(params["limit"] or "3"),
            )
        except Exception as exc:  # noqa: BLE001 - show preview errors inline
            self.render_learning_task_page(user, task, parse_qs(""), preview_error=str(exc), preview_params=params)
            return
        self.render_learning_task_page(user, task, parse_qs(""), preview_rows=rows, preview_params=params)

    def handle_learning_task_save_signals(self, user, path, form):
        task_id = 0
        try:
            task_id = self.learning_task_id_from_path(path)
            count = services.create_practice_signals_from_learning_task(
                self.con,
                user["id"],
                task_id,
                form.get("strategy_name", ""),
                form.get("template", ""),
                qty=form.get("qty", "100"),
                limit=int(form.get("limit", "3") or "3"),
                rationale_note=form.get("rationale_note", ""),
            )
        except Exception as exc:  # noqa: BLE001
            target = f"/learn/tasks/{task_id}" if task_id else "/learn"
            self.redirect(target + "?err=" + quote(str(exc)))
            return
        self.audit("learning.signals_saved", user=user, target_type="learning_task", target_id=task_id, detail={"count": count, "template": form.get("template", "")})
        self.redirect(f"/learn/tasks/{task_id}?msg=" + quote(f"已保存 {count} 条待执行演练计划。请到高级模拟盘确认是否执行。"))

    def _load_preview_data(self):
        """Load the offline /preview + /lessons artifact (path overridable via OWQ_PREVIEW_JSON)."""
        path = Path(os.getenv("OWQ_PREVIEW_JSON") or (db.REPO_ROOT / "reports" / "preview.json"))
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 工件损坏时退化
                return None
        return None

    def render_lessons(self, head: bool = False):
        """Public, no-login, no-key '坑即课程': the three biases turned into lessons, the
        survivorship one backed by the platform's own real numbers."""
        data = self._load_preview_data() or {}
        sv = data.get("survivorship") or {}

        def pctf(x):
            return pct(float(x) * 100) if x is not None else "—"

        delta = sv.get("delta_survivors_minus_full") if not sv.get("error") else None
        if delta:
            full = sv.get("full") or {}
            only = sv.get("survivors_only") or {}
            surv_real = (
                f'<p class="muted">用真实数据实测:把 {int(sv.get("n_delisted", 0))} 只退市股放回票池,同一策略总收益从 '
                f'{pctf(only.get("total_return"))}(只测存活)掉到 {pctf(full.get("total_return"))}(含退市),'
                f'{metric_label("sharpe", "夏普")}从 {float(only.get("sharpe") or 0):.2f} 掉到 {float(full.get("sharpe") or 0):.2f}——'
                f'被高估了 {pct(float(delta.get("total_return", 0)) * 100)}。</p>'
                '<p><a class="btn secondary" href="/preview">在 /preview 看这条实测曲线</a></p>'
            )
        else:
            surv_real = '<p class="muted">(运行 <code>--preview-only</code> 生成实测数据后,这里会显示平台自己的真实对比。)</p>'

        lessons = f"""
<section class="card">
  <div class="card-title"><span>坑 1 · 幸存者偏差</span><span class="pill warn">最常见</span></div>
  <p><strong>症状</strong>:回测看起来很赚,实盘却不行。</p>
  <p><strong>真相</strong>:你的票池只剩"活下来的"股票,退市的那些(连同它们的亏损)被悄悄删掉了。回测于是把"幸存者"当成了全部。</p>
  {surv_real}
  <p><strong>怎么避免</strong>:回测票池必须包含退市股。本平台的回测<strong>默认含退市股</strong>,并对消失的持仓按最后收盘价强制平仓,而不是当它没发生过。</p>
</section>
<section class="card">
  <div class="card-title"><span>坑 2 · 前视偏差(用了未来的信息)</span></div>
  <p><strong>症状</strong>:样本内{metric_label("sharpe", "夏普")}高得离谱,换个时间段就崩。</p>
  <p><strong>真相</strong>:你不小心用了"当时还不知道"的信息——比如用<strong>全样本 IC</strong> 给因子加权、用整段历史拟合回归系数,再拿去"预测"同一段。模型偷看了答案,自然好看。</p>
  <p><strong>怎么避免</strong>:任何调参/加权只能用<strong>截至当下</strong>的信息(滚动 / walk-forward);上线模型与上报的 OOS 指标要用留出集分开评估。本平台里 <code>--ic-weight</code> 这种全样本加权被明确标注"仅演示、有前视",绝不喂给对外展示的数字。</p>
</section>
<section class="card">
  <div class="card-title"><span>坑 3 · 复权口径(拆股看起来像暴跌)</span></div>
  <p><strong>症状</strong>:某只票某天"暴跌 50%",但持有人其实没亏。</p>
  <p><strong>真相</strong>:那天它分红或拆股了。<strong>不复权价(none)</strong>会在除权日跳空,看起来像暴跌,其实只是价格口径问题,不是真实涨跌。</p>
  <p><strong>怎么避免</strong>:研究/回测用<strong>后复权(hfq)</strong>价才能反映真实连续收益;而你下单看到的成交价是不复权现价。本平台在"预测→演练"交接处会提示这两套口径可能背离。</p>
</section>
"""
        body = f"""
<section class="card">
  <p><span class="pill ok">免登录 · 免 API key</span></p>
  <h2>三个让回测"看起来很赚"的坑</h2>
  <p class="muted">大多数"稳赚回测"不是骗子,而是踩了这三个坑。看懂它们,你就比多数散户更懂量化了。下面每一课都对应本平台在代码里真实做的处理。</p>
</section>
{lessons}
{self._preview_ctas()}
"""
        self.send_html(
            "量化三大坑 · 免登录科普",
            body,
            head=head,
            meta={"description": "幸存者偏差、前视偏差、复权口径——三个最常见的量化回测陷阱,用真实A股数据讲清楚,免登录免API key。"},
        )

    def render_research(self, head: bool = False):
        """Builder tier (public, educational): surface the research engine so engaged users can
        graduate from the paper-trading sim to building their own strategies. Reuses the offline
        preview artifact for a real backtest snapshot; explains the pipeline and how to run it."""
        data = self._load_preview_data() or {}
        m = data.get("metrics") or {}
        sv = data.get("survivorship") or {}

        def pctf(x):
            return pct(float(x) * 100) if x is not None else "—"

        snapshot = ""
        if m.get("total_return") is not None:
            delta = sv.get("delta_survivors_minus_full") if not sv.get("error") else None
            surv_line = (
                f'<p class="muted">幸存者偏差实测:只测存活股会把总收益高估 {pct(float(delta.get("total_return", 0)) * 100)}、'
                f'夏普高估 {float(delta.get("sharpe", 0)):+.2f}——所以这里的票池<strong>默认含退市股</strong>。</p>'
                if delta else ""
            )
            snapshot = f"""
<section class="card">
  <div class="card-title"><span>最新研究快照(真实回测,含退市)</span><span class="muted">截至 {escape(str(data.get('as_of') or ''))}</span></div>
  <div class="cards">
    <div class="card"><p>{metric_label('total_return', '总收益率')}</p><div class="metric">{pctf(m.get('total_return'))}</div></div>
    <div class="card"><p>{metric_label('cagr', '年化')}</p><div class="metric">{pctf(m.get('cagr'))}</div></div>
    <div class="card"><p>{metric_label('sharpe', '夏普')}</p><div class="metric">{(f"{float(m.get('sharpe')):.3f}" if m.get('sharpe') is not None else '—')}</div></div>
    <div class="card"><p>{metric_label('max_drawdown', '最大回撤')}</p><div class="metric bad">{pctf(m.get('max_drawdown'))}</div></div>
  </div>
  {surv_line}
  <p class="muted">完整报告(因子 IC、截面回归、预测候选、回测明细)在本机运行 <code>python -m src.research.real_data_report</code> 生成 <code>reports/real-data-report.md</code>。</p>
</section>
"""
        cli = (
            "# 1) 取数落库(含退市股票池,缓解幸存者偏差)\n"
            "python -m src.data.cli stock-list --source akshare\n"
            "python -m src.data.cli daily --source akshare --adjust hfq --limit 300\n\n"
            "# 2) 单因子评估(IC / 分层收益)\n"
            "python -m src.factors.run --factor reversal --window 20\n\n"
            "# 3) 单策略回测(T+1 / 费用 / 涨跌停 / 退市强制平仓)\n"
            "python -m src.backtest.run --signal reversal --lookback 20 --top 20\n\n"
            "# 4) 多因子合成 → 组合 → 回测(默认等权,无前视)\n"
            "python -m src.research.multifactor --top 30 --freq M\n\n"
            "# 5) 真实数据报告 + 预测候选(reports/ 下)\n"
            "python -m src.research.real_data_report\n"
        )
        body = f"""
<section class="card">
  <p><span class="pill">Builder 层</span> <span class="pill ok">真实数据 · 含退市股</span></p>
  <h2>从模拟盘毕业:用研究引擎自己造策略</h2>
  <p class="muted">模拟盘帮你建立手感和纪律;研究引擎让你像工程师一样,从数据出发自己设计、回测、迭代策略。这套引擎全部开源、可在本机跑通,下面是它的全貌。</p>
</section>
<section class="card">
  <div class="card-title"><span>研究闭环</span></div>
  <div class="cards">
    <div class="card"><p>① 数据</p><p class="muted">akshare/tushare/baostock → DuckDB,统一口径(量=股、额=元),含退市股票池。</p></div>
    <div class="card"><p>② 因子</p><p class="muted">reversal/momentum/volatility/amihud/ma_bias,截面 winsorize+zscore 标准化。</p></div>
    <div class="card"><p>③ 回测</p><p class="muted">事件驱动、T+1、费用/滑点/涨跌停、退市按最后收盘价强制平仓。</p></div>
    <div class="card"><p>④ 合成</p><p class="muted">多因子按方向合成总分,月度等权 top-N(默认无前视;IC 加权仅演示)。</p></div>
    <div class="card"><p>⑤ 预测</p><p class="muted">截面回归(前 70% 训练、后 30% OOS)产出下一期候选 → 可一键导入模拟盘。</p></div>
  </div>
  <p class="muted">每一步都对应 <code>src/</code> 下一个可独立运行的模块,结果可复现、可复盘。</p>
</section>
{snapshot}
<section class="card">
  <div class="card-title"><span>在本机跑起来</span></div>
  <pre style="background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;line-height:1.6;white-space:pre">{escape(cli)}</pre>
</section>
<section class="card">
  <div class="card-title"><span>接回模拟盘</span></div>
  <p>研究产出的<strong>预测候选</strong>(<code>reports/predictions.csv</code>)可以一键导入模拟盘当作演练计划——研究和实操在同一套真实行情上闭环。</p>
  <p><a class="btn" href="/app">回模拟盘 · 从模型预测生成篮子</a> <a class="btn secondary" href="/lessons">先复习量化三大坑</a></p>
</section>
"""
        self.send_html(
            "研究引擎 · 从模拟盘毕业",
            body,
            head=head,
            meta={"description": "OurWorlds Quant 研究引擎:数据→因子→回测→多因子→预测的开源闭环,含退市股、可复现,帮你从模拟盘毕业到自己造策略。"},
        )

    def _preview_ctas(self) -> str:
        if self.current_user():
            # Already logged in: don't show a "sign up" CTA — point into the product.
            return (
                '<section class="card"><div class="card-title"><span>继续</span></div>'
                '<p><a class="btn blue" href="/app">进入模拟盘</a> '
                '<a class="btn secondary" href="/learn">继续学习</a> '
                '<a class="btn secondary" href="/research">研究引擎</a> '
                '<a class="btn secondary" href="/showcase/public">公开排行榜</a></p></section>'
            )
        return (
            '<section class="card"><div class="card-title"><span>想自己上手?</span></div>'
            '<p><a class="btn blue" href="/register">免费注册,拿 10 万模拟本金</a> '
            '<a class="btn secondary" href="/lessons">量化三大坑(免登录)</a> '
            '<a class="btn secondary" href="/research">研究引擎</a> '
            '<a class="btn secondary" href="/showcase/public">看公开排行榜</a></p></section>'
        )

    def render_preview(self, head: bool = False):
        """Public, no-signup, no-JS preview: a real backtest + the survivorship teaching."""
        data = self._load_preview_data()
        if not data or not data.get("equity_points"):
            body = (
                '<section class="card"><h2>真实回测预览</h2>'
                '<p class="muted">预览数据尚未生成。可运行 '
                "<code>python -m src.research.real_data_report --preview-only</code> 生成。</p></section>"
                f"{self._preview_ctas()}"
            )
            self.send_html("真实回测预览", body, head=head)
            return

        def pctf(x):
            return pct(float(x) * 100) if x is not None else "—"

        def numf(x, d=2):
            return f"{float(x):.{d}f}" if x is not None else "—"

        m = data.get("metrics") or {}
        sv = data.get("survivorship") or {}
        as_of = escape(str(data.get("as_of") or ""))
        n_codes = int(data.get("n_codes") or 0)
        svg = preview_equity_svg(data["equity_points"])
        cards = (
            f'<div class="card"><p>{metric_label("total_return", "总收益率")}</p><div class="metric">{pctf(m.get("total_return"))}</div></div>'
            f'<div class="card"><p>{metric_label("cagr", "年化收益率")}</p><div class="metric">{pctf(m.get("cagr"))}</div></div>'
            f'<div class="card"><p>{metric_label("sharpe", "夏普比率")}</p><div class="metric">{numf(m.get("sharpe"), 3)}</div></div>'
            f'<div class="card"><p>{metric_label("max_drawdown", "最大回撤")}</p><div class="metric bad">{pctf(m.get("max_drawdown"))}</div></div>'
        )
        surv_html = ""
        delta = sv.get("delta_survivors_minus_full") if not sv.get("error") else None
        if delta:
            full = sv.get("full") or {}
            only = sv.get("survivors_only") or {}
            surv_html = (
                '<section class="card">'
                '<div class="card-title"><span>为什么很多"稳赚回测"是假的</span></div>'
                f"<p>同一个策略,只要把<strong>已经退市的 {int(sv.get('n_delisted', 0))} 只股票</strong>从票池里去掉"
                "(很多人就是这么干的,因为退市数据不好拿),绩效就会凭空变好:</p>"
                '<div class="cards">'
                f'<div class="card"><p>总收益被高估</p><div class="metric warn">{pct(float(delta.get("total_return", 0)) * 100)}</div></div>'
                f'<div class="card"><p>夏普被高估</p><div class="metric warn">{float(delta.get("sharpe", 0)):+.2f}</div></div>'
                "</div>"
                f'<p class="muted">真实(含退市): 总收益 {pctf(full.get("total_return"))} · 夏普 {numf(full.get("sharpe"))}　|　'
                f'有偏(只测存活): 总收益 {pctf(only.get("total_return"))} · 夏普 {numf(only.get("sharpe"))}</p>'
                "<p>这就是<strong>幸存者偏差</strong>。我们的回测<strong>默认含退市股</strong>——数字也许不好看,但它是真的。</p>"
                "</section>"
            )
        body = f"""
<section class="card">
  <p><span class="pill ok">真实历史 A 股数据</span> <span class="pill">含退市股回测</span> <span class="pill">截至 {as_of}</span></p>
  <h2>免注册,先看一个策略在真实数据上的真实表现</h2>
  <p class="muted">不喊单、不炫技。下面是一个多因子策略在 {n_codes} 只 A 股(含中途退市)上的真实回测,以及我们如何诚实地标注它的缺陷。所有内容仅用于模拟训练与方法演示,不构成投资建议。</p>
</section>
<section class="card">
  <div class="card-title"><span>真实回测绩效(含退市股)</span><span class="muted">点按指标名看含义</span></div>
  <div class="cards">{cards}</div>
  {svg}
  <p class="muted">阴影=区间最大回撤,虚线=初始本金。曲线向下也照样展示——因为这才是真实的。</p>
</section>
{surv_html}
{self._preview_ctas()}
"""
        self.send_html(
            "真实回测预览 · 免注册",
            body,
            head=head,
            meta={"description": "免注册查看一个多因子策略在真实A股数据(含退市股)上的真实回测,以及幸存者偏差如何让大多数回测看起来更好。"},
        )

    def api_equity_curve(self, user):
        """Read-only time series for the dashboard equity chart (progressive enhancement)."""
        rows = services.equity_history(self.con, user["id"], limit=120)
        points = [
            {
                "date": str(r["created_at"])[:10],
                "equity": float(r["equity"] or 0.0),
                "return_pct": float(r["return_pct"] or 0.0),
            }
            for r in rows
        ]
        self.send_json({"points": points})

    def render_dashboard(self, user, query):
        snap = services.portfolio_snapshot(self.con, user["id"])
        market = services.market_rows(self.con)
        orders = services.recent_orders(self.con, user["id"])
        signals = services.practice_signals(self.con, user["id"], limit=8)
        holdings = snap["holdings"]
        hold_rows = "".join(
            f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{r['qty']}</td><td>{r['available_qty']}</td>"
            f"<td>{money(r['avg_price'])}</td><td>{money(r['price'])}</td>"
            f"<td>{money(r['market_value'])}</td><td>{money(r['pnl'])}</td></tr>"
            for r in holdings
        ) or '<tr><td colspan="8" class="muted">暂无持仓</td></tr>'
        market_options = "".join(
            f"<option value=\"{escape(r['code'])}\">{escape(r['code'])} · {escape(r['name'])} · {money(r['price'])}</option>"
            for r in market
        )
        market_rows = "".join(
            f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{money(r['price'])}</td>"
            f"<td>{pct((r['price']/r['prev_close']-1)*100)}</td></tr>"
            for r in market
        )
        order_rows = "".join(
            f"<tr><td>{escape(o['created_at'])}</td><td>{escape(o['code'])}</td><td>{side_cn(o['side'])}</td>"
            f"<td>{o['qty']}</td><td>{money(o['price'])}</td><td>{money(o['fee'])}</td></tr>"
            for o in orders
        ) or '<tr><td colspan="6" class="muted">暂无交易</td></tr>'
        signal_rows = "".join(
            f"<tr><td>{escape(s['created_at'])}</td><td>{escape(s['strategy_name'])}</td>"
            f"<td>{escape(s['code'])}</td><td>{side_cn(s['side'])}</td><td>{s['qty']}</td>"
            f"<td>{money(s['price']) if s['price'] is not None else '-'}</td>"
            f"<td>{escape(s['rationale'] or '-')}</td><td>{signal_status_cn(s['status'])}</td>"
            f"<td>{self.practice_signal_actions(s, user)}</td></tr>"
            for s in signals
        ) or '<tr><td colspan="9" class="muted">暂无演练计划</td></tr>'
        ret_class = "ok" if snap["return_pct"] >= 0 else "bad"
        wr = services.weekly_review(self.con, user["id"])
        if wr:
            wk_cls = "ok" if wr["week_change_pct"] >= 0 else "bad"
            wk_nudge = (
                "本周还没有交易。复盘的第一步是先有记录——在上面下一两笔模拟单,或保存一个演练计划。"
                if wr["trades"] == 0
                else "记录这周的假设、风险控制和执行偏差,公开复盘比闷头交易学得快。"
            )
            weekly_html = f"""
<section class="card">
  <div class="card-title"><span>本周复盘</span><span class="muted">最近 7 天</span></div>
  <div class="cards">
    <div class="card"><p>{metric_label('return_pct', '本周净值变化')}</p><div class="metric {wk_cls}">{pct(wr['week_change_pct'])}</div></div>
    <div class="card"><p>本周成交</p><div class="metric">{wr['trades']} 笔</div></div>
  </div>
  <p class="muted">{wk_nudge}</p>
  <p><a class="btn" href="/forum/new?template=performance">生成战绩复盘帖</a> <a class="btn secondary" href="/account/ai">问 AI 教练复盘</a></p>
</section>
"""
        else:
            weekly_html = ""
        learning_pending = self.con.execute(
            """
            SELECT COUNT(*) AS count
            FROM practice_signals
            WHERE user_id=? AND status='pending' AND learning_task_id IS NOT NULL
            """,
            (int(user["id"]),),
        ).fetchone()
        learning_notice = (
            f'<div class="msg">你有 {int(learning_pending["count"])} 条来自学习任务的待执行计划。它们只是草稿保存结果,执行前请确认数量、依据和风险记录。</div>'
            if learning_pending and int(learning_pending["count"])
            else ""
        )
        body = f"""
{self.message_html(query)}
{learning_notice}
{self.provenance_chip()}
<section class="cards">
  <div class="card"><p>{metric_label('equity', '总资产')}</p><div class="metric">{money(snap['equity'])}</div></div>
  <div class="card"><p>{metric_label('cash', '现金')}</p><div class="metric">{money(snap['cash'])}</div></div>
  <div class="card"><p>{metric_label('return_pct', '收益率')}</p><div class="metric {ret_class}">{pct(snap['return_pct'])}</div></div>
</section>
<section class="card" data-equity-section hidden>
  <div class="card-title"><span>资产曲线</span><span class="muted">模拟账户净值,阴影为最大回撤区间</span></div>
  <div data-equity-curve></div>
</section>
{weekly_html}
<section class="card">
  <h2>模拟交易</h2>
  <form class="formline" method="post" action="/orders">
    {csrf_input(user)}
    <div><label>标的</label><select name="code">{market_options}</select></div>
    <div><label>方向</label><select name="side"><option value="buy">买入</option><option value="sell">卖出</option></select></div>
    <div><label>数量</label><input name="qty" type="number" min="1" step="1" value="100"></div>
    <button type="submit">提交</button>
  </form>
</section>
<section class="card">
  <h2>策略演练计划</h2>
  <form method="post" action="/practice-signals">
    {csrf_input(user)}
    <div class="formline">
      <div><label>策略名称</label><input name="strategy_name" placeholder="例如: ETF 轮动 / 反转观察"></div>
      <div><label>标的</label><select name="code">{market_options}</select></div>
      <div><label>方向</label><select name="side"><option value="buy">买入</option><option value="sell">卖出</option></select></div>
      <div><label>数量</label><input name="qty" type="number" min="1" step="1" value="100"></div>
    </div>
    <p><label>演练依据</label><textarea name="rationale" placeholder="记录入场条件、预期、止损/观察点"></textarea></p>
    <p><button type="submit">保存演练计划</button></p>
  </form>
  <h3>策略篮子导入</h3>
  <form method="post" action="/practice-signals/batch">
    {csrf_input(user)}
    <div class="row">
      <div><label>策略名称</label><input name="strategy_name" value="研究篮子"></div>
      <div><label>默认依据</label><input name="rationale" placeholder="例如: 多因子 top 组合 / ETF 轮动"></div>
    </div>
    <p><label>篮子 CSV</label><textarea name="batch_text" placeholder="code,side,qty,rationale&#10;000001.SZ,buy,100,反转得分靠前&#10;510300.SH,buy,1000,低波动配置"></textarea></p>
    <p><button type="submit">导入演练计划</button></p>
  </form>
  <h3>从基础行情生成篮子</h3>
  <form method="post" action="/practice-signals/from-market">
    {csrf_input(user)}
    <div class="formline">
      <div><label>策略名称</label><input name="strategy_name" value="基础行情反转篮子"></div>
      <div><label>模式</label><select name="mode"><option value="reversal">反转候选</option><option value="momentum">动量候选</option></select></div>
      <div><label>数量/标的</label><input name="qty" type="number" min="1" step="1" value="100"></div>
      <div><label>候选数</label><input name="limit" type="number" min="1" max="50" step="1" value="3"></div>
    </div>
    <p><label><input type="checkbox" name="real_only" value="1" style="width:auto"> 只使用真实同步行情</label></p>
    <p><button type="submit">生成待执行计划</button></p>
  </form>
  <form method="post" action="/practice-signals/execute-pending">
    {csrf_input(user)}
    <input type="hidden" name="limit" value="20">
    <p><button type="submit">执行全部待执行计划</button></p>
  </form>
  <table><thead><tr><th>时间</th><th>策略</th><th>代码</th><th>方向</th><th>数量</th><th>现价</th><th>依据</th><th>状态</th><th>操作</th></tr></thead><tbody>{signal_rows}</tbody></table>
</section>
<div class="grid">
  <section class="card">
    <h2>持仓</h2>
    <table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>可卖</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th></tr></thead><tbody>{hold_rows}</tbody></table>
  </section>
  <section class="card">
    <h2>基础行情</h2>
    <table><thead><tr><th>代码</th><th>名称</th><th>价格</th><th>涨跌</th></tr></thead><tbody>{market_rows}</tbody></table>
  </section>
</div>
<section class="card">
  <h2>最近成交</h2>
  <table><thead><tr><th>时间</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th></tr></thead><tbody>{order_rows}</tbody></table>
</section>
"""
        self.send_html("模拟盘", body, user=user)

    def practice_signal_actions(self, signal, user) -> str:
        if signal["status"] != "pending":
            return "-"
        signal_id = int(signal["id"])
        return (
            f'<form method="post" action="/practice-signals/{signal_id}/execute" style="display:inline">'
            f'{csrf_input(user)}<button type="submit">执行</button></form> '
            f'<form method="post" action="/practice-signals/{signal_id}/cancel" style="display:inline">'
            f'{csrf_input(user)}<button class="secondary" type="submit">取消</button></form>'
        )

    def render_market(self, user, query):
        market = services.market_rows(self.con)
        summary = services.market_source_summary(self.con)
        summary_rows = "".join(
            f"<tr><td>{escape(r['source'])}</td><td>{r['rows']}</td><td>{r['codes']}</td>"
            f"<td>{escape(r['date_min'] or '-')}</td><td>{escape(r['date_max'] or '-')}</td>"
            f"<td>{escape(r['updated_at'] or '-')}</td></tr>"
            for r in summary
        ) or '<tr><td colspan="6" class="muted">暂无行情</td></tr>'
        rows = "".join(
            f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{money(r['prev_close'])}</td>"
            f"<td>{money(r['price'])}</td><td>{pct((r['price']/r['prev_close']-1)*100)}</td>"
            f"<td>{escape(r['source'])}</td><td>{escape(r['as_of'] or '-')}</td><td>{escape(r['updated_at'])}</td></tr>"
            for r in market
        )
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>基础行情数据</h2>
  <p>系统会把真实日线库同步为模拟盘可用行情。模拟成交应优先使用不复权 none 价格;后复权 hfq 主要用于研究回测。</p>
  <form method="post" action="/market/sync">
    {csrf_input(user)}
    <div class="formline">
      <div><label>来源</label><select name="source"><option value="duckdb">src.data DuckDB</option><option value="csv_text">粘贴 CSV</option><option value="csv">CSV 文件路径</option></select></div>
      <div><label>复权</label><select name="adjust"><option value="none">none</option><option value="qfq">qfq</option><option value="hfq">hfq</option></select></div>
      <div><label>数量上限</label><input name="limit" type="number" min="1" max="10000" step="1" value="500"></div>
      <div><label>CSV 路径</label><input name="csv_path" placeholder="可选: /path/market.csv"></div>
      <button type="submit">同步行情</button>
    </div>
    <p><label><input type="checkbox" name="replace_market" value="1" checked style="width:auto"> 同步成功后替换现有行情,清除演示标的</label></p>
    <p><label>CSV 内容</label><textarea name="csv_text" placeholder="code,name,price,prev_close,as_of&#10;000001.SZ,平安银行,10.90,10.82,2026-06-23"></textarea></p>
  </form>
</section>
<section class="card">
  <h2>来源覆盖</h2>
  <table><thead><tr><th>来源</th><th>行数</th><th>标的数</th><th>最早日期</th><th>最新日期</th><th>更新时间</th></tr></thead><tbody>{summary_rows}</tbody></table>
</section>
<section class="card">
  <h2>行情列表</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>昨收</th><th>现价</th><th>涨跌</th><th>来源</th><th>日期</th><th>更新时间</th></tr></thead><tbody>{rows}</tbody></table>
</section>
"""
        self.send_html("基础数据", body, user=user)

    def render_portfolio_lab(self, user, query):
        summary = services.market_source_summary(self.con)
        summary_rows = "".join(
            f"<tr><td>{escape(r['source'])}</td><td>{r['rows']}</td><td>{r['codes']}</td>"
            f"<td>{escape(r['date_max'] or '-')}</td></tr>"
            for r in summary
        ) or '<tr><td colspan="4" class="muted">暂无行情,请先到基础数据同步。</td></tr>'
        def candidate_rows(mode: str) -> str:
            try:
                rows = services.market_signal_basket_rows(self.con, mode=mode, qty=100, limit=10, real_only=True)
            except Exception as exc:  # noqa: BLE001
                return f'<tr><td colspan="6" class="muted">{escape(str(exc))}</td></tr>'
            return "".join(
                f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{side_cn(r['side'])}</td>"
                f"<td>{r['qty']}</td><td>{pct(r['change_pct'])}</td><td>{escape(r['rationale'])}</td></tr>"
                for r in rows
            )
        try:
            pred = services.prediction_basket_rows(self.con, qty=100, limit=10)
            pred_rows = "".join(
                f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{pct(r['prediction'] * 100)}</td>"
                f"<td>{money(r['last_close'])}</td><td>{escape(r['rationale'])}</td></tr>"
                for r in pred
            )
            pred_note = ""
        except Exception as exc:  # noqa: BLE001
            pred_rows = f'<tr><td colspan="5" class="muted">{escape(str(exc))}</td></tr>'
            pred_note = "请先运行研究报告命令生成 reports/predictions.csv。"
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>组合设计</h2>
  <p>这里直接使用系统已同步的真实行情和研究预测结果生成模拟盘演练计划。用户不需要自己准备 CSV。</p>
  <table><thead><tr><th>来源</th><th>行数</th><th>标的数</th><th>最新日期</th></tr></thead><tbody>{summary_rows}</tbody></table>
</section>
<section class="card">
  <h2>真实行情篮子</h2>
  <form method="post" action="/practice-signals/from-market">
    {csrf_input(user)}
    <input type="hidden" name="real_only" value="1">
    <div class="formline">
      <div><label>策略名称</label><input name="strategy_name" value="真实行情反转篮子"></div>
      <div><label>模式</label><select name="mode"><option value="reversal">反转候选</option><option value="momentum">动量候选</option></select></div>
      <div><label>数量/标的</label><input name="qty" type="number" min="1" step="1" value="100"></div>
      <div><label>候选数</label><input name="limit" type="number" min="1" max="50" step="1" value="5"></div>
    </div>
    <p><button type="submit">生成演练计划</button></p>
  </form>
  <h3>反转候选</h3>
  <table><thead><tr><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>涨跌</th><th>依据</th></tr></thead><tbody>{candidate_rows("reversal")}</tbody></table>
  <h3>动量候选</h3>
  <table><thead><tr><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>涨跌</th><th>依据</th></tr></thead><tbody>{candidate_rows("momentum")}</tbody></table>
</section>
<section class="card">
  <h2>模型预测篮子</h2>
  <p class="muted">{escape(pred_note)}</p>
  <form method="post" action="/practice-signals/from-predictions">
    {csrf_input(user)}
    <div class="formline">
      <div><label>策略名称</label><input name="strategy_name" value="模型预测候选篮子"></div>
      <div><label>数量/标的</label><input name="qty" type="number" min="1" step="1" value="100"></div>
      <div><label>候选数</label><input name="limit" type="number" min="1" max="50" step="1" value="5"></div>
    </div>
    <p><button type="submit">导入预测候选</button></p>
  </form>
  <table><thead><tr><th>代码</th><th>名称</th><th>预测</th><th>收盘</th><th>依据</th></tr></thead><tbody>{pred_rows}</tbody></table>
</section>
"""
        self.send_html("组合设计", body, user=user)

    def handle_order(self, user, form):
        if not self.require_user_write_limit(user, "orders", 30, 60, "/app"):
            return
        code = form.get("code", "")
        side = form.get("side", "")
        qty = form.get("qty", "0")
        try:
            order_id = services.place_order(self.con, user["id"], code, side, int(qty))
        except Exception as exc:  # noqa: BLE001
            self.redirect("/app?err=" + quote(str(exc)))
            return
        self.audit("order.place", user=user, target_type="order", target_id=order_id, detail={"code": code, "side": side, "qty": qty})
        self.redirect("/app?msg=" + quote("委托已按当前基础行情成交。"))

    def handle_practice_signal_create(self, user, form):
        if not self.require_user_write_limit(user, "practice_signal.create", 60, 60, "/app"):
            return
        code = form.get("code", "")
        side = form.get("side", "")
        qty = form.get("qty", "0")
        try:
            signal_id = services.create_practice_signal(
                self.con,
                user["id"],
                form.get("strategy_name", ""),
                code,
                side,
                int(qty),
                form.get("rationale", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/app?err=" + quote(str(exc)))
            return
        self.audit("practice_signal.create", user=user, target_type="practice_signal", target_id=signal_id, detail={"code": code, "side": side, "qty": qty})
        self.redirect("/app?msg=" + quote("演练计划已保存。"))

    def handle_practice_signal_batch(self, user, form):
        if not self.require_user_write_limit(user, "practice_signal.batch", 20, 60, "/app"):
            return
        try:
            count = services.create_practice_signal_batch(
                self.con,
                user["id"],
                form.get("strategy_name", ""),
                form.get("batch_text", ""),
                form.get("rationale", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect_operation_failed(
                "/app",
                "批量导入失败,请检查篮子格式。",
                "practice_signal.batch_failed",
                exc,
                user=user,
                target_type="practice_signal",
            )
            return
        self.audit("practice_signal.batch_create", user=user, target_type="practice_signal", detail={"count": count})
        self.redirect("/app?msg=" + quote(f"已导入 {count} 条演练计划。"))

    def handle_practice_signal_from_market(self, user, form):
        if not self.require_user_write_limit(user, "practice_signal.market", 20, 60, "/app"):
            return
        try:
            count = services.create_practice_signals_from_market(
                self.con,
                user["id"],
                form.get("strategy_name", ""),
                form.get("mode", "reversal"),
                "buy",
                form.get("qty", "100"),
                int(form.get("limit", "3") or "3"),
                real_only=form.get("real_only") == "1",
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect_operation_failed(
                "/app",
                "行情候选生成失败,请检查基础行情数据。",
                "practice_signal.market_failed",
                exc,
                user=user,
                target_type="practice_signal",
                detail={"mode": form.get("mode", "reversal"), "real_only": form.get("real_only") == "1"},
            )
            return
        self.audit(
            "practice_signal.market_create",
            user=user,
            target_type="practice_signal",
            detail={"count": count, "mode": form.get("mode", "reversal"), "real_only": form.get("real_only") == "1"},
        )
        self.redirect("/app?msg=" + quote(f"已从基础行情生成 {count} 条演练计划。"))

    def handle_practice_signal_from_predictions(self, user, form):
        if not self.require_user_write_limit(user, "practice_signal.predictions", 20, 60, "/portfolio-lab"):
            return
        try:
            count = services.create_practice_signals_from_predictions(
                self.con,
                user["id"],
                form.get("strategy_name", ""),
                form.get("qty", "100"),
                int(form.get("limit", "5") or "5"),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect_operation_failed(
                "/portfolio-lab",
                "预测候选导入失败,请检查预测结果和基础行情。",
                "practice_signal.prediction_failed",
                exc,
                user=user,
                target_type="practice_signal",
            )
            return
        self.audit("practice_signal.prediction_create", user=user, target_type="practice_signal", detail={"count": count})
        self.redirect("/portfolio-lab?msg=" + quote(f"已导入 {count} 条预测演练计划。"))

    def handle_practice_signal_execute_pending(self, user, form):
        if not self.require_user_write_limit(user, "practice_signal.execute_pending", 20, 60, "/app"):
            return
        try:
            result = services.execute_pending_practice_signals(
                self.con,
                user["id"],
                int(form.get("limit", "20") or "20"),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/app?err=" + quote(str(exc)))
            return
        executed = len(result["executed"])
        failed = len(result["failed"])
        if failed:
            first = result["failed"][0]
            msg = f"已执行 {executed} 条,{failed} 条失败;首个失败 {first['code']}: {first['error']}"
        else:
            msg = f"已执行 {executed} 条待执行计划。"
        self.audit("practice_signal.execute_pending", user=user, target_type="practice_signal", detail={"executed": executed, "failed": failed})
        self.redirect("/app?msg=" + quote(msg))

    def handle_practice_signal_execute(self, user, path):
        if not self.require_user_write_limit(user, "practice_signal.execute", 60, 60, "/app"):
            return
        try:
            signal_id = int(path.split("/")[2])
            services.execute_practice_signal(self.con, user["id"], signal_id)
        except Exception as exc:  # noqa: BLE001
            self.redirect("/app?err=" + quote(str(exc)))
            return
        self.audit("practice_signal.execute", user=user, target_type="practice_signal", target_id=signal_id)
        self.redirect("/app?msg=" + quote("演练计划已执行并生成成交。"))

    def handle_practice_signal_cancel(self, user, path):
        if not self.require_user_write_limit(user, "practice_signal.cancel", 60, 60, "/app"):
            return
        try:
            signal_id = int(path.split("/")[2])
            services.cancel_practice_signal(self.con, user["id"], signal_id)
        except Exception as exc:  # noqa: BLE001
            self.redirect("/app?err=" + quote(str(exc)))
            return
        self.audit("practice_signal.cancel", user=user, target_type="practice_signal", target_id=signal_id)
        self.redirect("/app?msg=" + quote("演练计划已取消。"))

    def handle_market_sync(self, user, form):
        if not self.require_user_write_limit(user, "market.sync", 5, 300, "/market"):
            return
        try:
            replace = form.get("replace_market") == "1"
            if form.get("source") == "csv":
                n = data_bridge.sync_market_from_csv(self.con, form.get("csv_path", ""), replace=replace)
            elif form.get("source") == "csv_text":
                n = data_bridge.sync_market_from_csv_text(self.con, form.get("csv_text", ""), replace=replace)
            else:
                n = data_bridge.sync_market_from_quant_db(
                    self.con,
                    adjust=form.get("adjust", "none"),
                    limit=int(form.get("limit", "500") or "500"),
                    replace=replace,
                )
        except Exception as exc:  # noqa: BLE001
            self.redirect_operation_failed(
                "/market",
                "行情同步失败,请检查数据来源或 CSV 格式。",
                "market.sync_failed",
                exc,
                user=user,
                target_type="market_prices",
                detail={"source": form.get("source", "duckdb"), "replace": replace},
            )
            return
        snap_count = services.record_all_equity_snapshots(self.con, source="market_sync")
        self.audit(
            "market.sync",
            user=user,
            target_type="market_prices",
            detail={"rows": n, "snapshots": snap_count, "source": form.get("source", "duckdb"), "replace": replace},
        )
        self.redirect("/market?msg=" + quote(f"已同步 {n} 条行情,刷新 {snap_count} 个账户资产快照。"))

    def render_account(self, user, query):
        snap = services.portfolio_snapshot(self.con, user["id"])
        history = services.equity_history(self.con, user["id"], limit=20)
        identity_label = "邮箱" if user["email"] else "身份标识"
        identity_value = user["email"] or user["wechat_openid"]
        suggested_login = str(user["login_name"] or "").strip() or (
            services.suggest_login_name(user["email"]) if user["email"] else f"user{user['id']}"
        )
        current_password_required = "required" if user["password_hash"] else ""
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>账户信息</h2>
  <div class="identity">{avatar_html(user)}<strong>{escape(user['nickname'])}</strong></div>
  <table>
    <tr><th>昵称</th><td>{escape(user['nickname'])}</td></tr>
    <tr><th>{identity_label}</th><td>{escape(identity_value)}</td></tr>
    <tr><th>模拟账户</th><td>{snap['account']['id']}</td></tr>
    <tr><th>初始资金</th><td>{money(snap['account']['initial_cash'])}</td></tr>
    <tr><th>当前权益</th><td>{money(snap['equity'])}</td></tr>
    <tr><th>收益率</th><td>{pct(snap['return_pct'])}</td></tr>
  </table>
</section>
<section class="card">
  <h2>资料设置</h2>
  <form method="post" action="/account/profile">
    {csrf_input(user)}
    <label>昵称</label><input name="nickname" value="{escape(user['nickname'])}">
    <label>头像 URL</label><input name="avatar_url" value="{escape(user['avatar_url'] or '')}" placeholder="https://...">
    <p><button type="submit">保存资料</button></p>
  </form>
</section>
<section class="card">
  <h2>登录密码</h2>
  <form method="post" action="/account/password">
    {csrf_input(user)}
    <label>用户名</label>
    <input name="login_name" autocomplete="username" required pattern="[a-z0-9][a-z0-9_-]{{2,31}}" value="{escape(suggested_login)}" placeholder="3-32 位小写字母、数字、_ 或 -">
    <label>当前密码</label>
    <input name="current_password" type="password" autocomplete="current-password" {current_password_required}>
    <label>新密码</label>
    <input name="password" type="password" autocomplete="new-password" required minlength="10">
    <label>确认新密码</label>
    <input name="password_confirm" type="password" autocomplete="new-password" required minlength="10">
    <p><button type="submit">更新登录密码</button></p>
  </form>
</section>
<section class="card">
  <h2>资产快照</h2>
  <table><thead><tr><th>时间</th><th>总资产</th><th>现金</th><th>持仓市值</th><th>收益率</th></tr></thead><tbody>{history_rows(history)}</tbody></table>
</section>
<section class="card">
  <h2>数据导出</h2>
  <p>导出自己的模拟盘和社区数据,用于复盘、表格分析、研究记录或个人留档。</p>
  <p><a class="btn secondary" href="/account/export/data.json">完整数据 JSON</a> <a class="btn secondary" href="/account/export/orders.csv">成交记录 CSV</a> <a class="btn secondary" href="/account/export/holdings.csv">当前持仓 CSV</a> <a class="btn secondary" href="/account/export/equity.csv">资产曲线 CSV</a></p>
</section>
<section class="card">
  <h2>T+1 结算</h2>
  <p>买入成交当天不可卖出。进入下一交易日后,当前持仓会变为可卖。</p>
  <form method="post" action="/account/settle">
    {csrf_input(user)}
    <button type="submit">进入下一交易日</button>
  </form>
</section>
<section class="card">
  <h2>重新演练</h2>
  <p>重置后会清空当前持仓、成交记录、演练计划和资产快照,现金恢复到初始资金;论坛帖子会保留。</p>
  <form method="post" action="/account/reset">
    {csrf_input(user)}
    <label>输入 RESET 确认重置模拟账户</label>
    <input name="confirm" autocomplete="off" placeholder="RESET">
    <button type="submit">重置我的模拟账户</button>
  </form>
</section>
<section class="card">
  <h2>关闭账户</h2>
  <p>关闭后会删除你的登录身份、模拟账户、交易、持仓、演练计划、论坛帖子/评论、同意记录和登录会话。安全审计日志会保留最小操作记录。</p>
  <form method="post" action="/account/delete">
    {csrf_input(user)}
    <label>输入 DELETE 确认关闭账户</label>
    <input name="confirm" autocomplete="off" placeholder="DELETE">
    <p><button class="secondary" type="submit">关闭并删除我的账户</button></p>
  </form>
</section>
"""
        self.send_html("账户", body, user=user)

    def render_account_consent(self, user, query):
        latest = services.latest_user_consent(self.con, int(user["id"]))
        next_path = self.safe_next_path(query.get("next", [""])[0] if query else "", default="/app")
        latest_text = (
            f"当前记录:条款 {escape(latest['terms_version'])},隐私 {escape(latest['privacy_version'])},风险 {escape(latest['risk_version'])}"
            if latest
            else "当前没有可用的法律同意记录。"
        )
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>确认服务条款</h2>
  <p>继续使用模拟盘、组合设计、公开赛和论坛前,请确认你已经阅读并同意当前版本的服务条款、隐私说明和风险提示。</p>
  <p>{latest_text}</p>
  <table>
    <tr><th>当前服务条款版本</th><td>{LEGAL_VERSION}</td><td><a href="/terms">查看服务条款</a></td></tr>
    <tr><th>当前隐私说明版本</th><td>{LEGAL_VERSION}</td><td><a href="/privacy">查看隐私说明</a></td></tr>
    <tr><th>当前风险提示版本</th><td>{LEGAL_VERSION}</td><td><a href="/risk">查看风险提示</a></td></tr>
  </table>
  <form method="post" action="/account/consent">
    {csrf_input(user)}
    <input type="hidden" name="next" value="{escape(next_path, quote=True)}">
    <p><label><input type="checkbox" name="accept_terms" value="1" style="width:auto"> 我已阅读并同意当前版本的服务条款、隐私说明和风险提示</label></p>
    <p><button type="submit">确认并继续</button> <a class="btn secondary" href="/account/export/data.json">导出我的数据</a></p>
  </form>
</section>
"""
        self.send_html("确认服务条款", body, user=user)

    def export_orders_csv(self, user):
        rows = services.order_history(self.con, user["id"])
        self.audit_account_export(user, "orders.csv", len(rows))
        self.send_csv(
            "orders.csv",
            ["created_at", "code", "side", "qty", "price", "fee", "amount"],
            [
                [r["created_at"], r["code"], r["side"], r["qty"], r["price"], r["fee"], r["amount"]]
                for r in rows
            ],
        )

    def export_holdings_csv(self, user):
        snap = services.portfolio_snapshot(self.con, user["id"])
        self.audit_account_export(user, "holdings.csv", len(snap["holdings"]))
        self.send_csv(
            "holdings.csv",
            ["code", "name", "qty", "available_qty", "avg_price", "price", "market_value", "pnl"],
            [
                [
                    r["code"],
                    r["name"],
                    r["qty"],
                    r["available_qty"],
                    r["avg_price"],
                    r["price"],
                    r["market_value"],
                    r["pnl"],
                ]
                for r in snap["holdings"]
            ],
        )

    def export_equity_csv(self, user):
        rows = services.equity_snapshots(self.con, user["id"])
        self.audit_account_export(user, "equity.csv", len(rows))
        self.send_csv(
            "equity.csv",
            ["created_at", "cash", "market_value", "equity", "return_pct", "source"],
            [
                [r["created_at"], r["cash"], r["market_value"], r["equity"], r["return_pct"], r["source"]]
                for r in rows
            ],
        )

    def export_account_json(self, user):
        self.audit_account_export(user, "data.json")
        payload = services.account_data_export(self.con, user["id"])
        self.send_json_download(f"ourworld-quant-user-{user['id']}.json", payload)

    def audit_account_export(self, user, filename: str, rows: int | None = None):
        detail = {"file": filename}
        if rows is not None:
            detail["rows"] = rows
        self.audit(
            "account.export",
            user=user,
            target_type="user_data_export",
            target_id=filename,
            detail=detail,
        )

    def sensitive_secret_values(self) -> list[str]:
        """Secret values that must never leave the system in an AI payload."""
        return [os.getenv(name, "") for name in SENSITIVE_ENV_NAMES]

    AI_BANNER = (
        '<section class="card" style="border-color:#f0c36d;background:#fff8e6">'
        '<p class="muted" style="margin:0">AI 教练仅用于<strong>量化方法学习、引导与模拟盘复盘</strong>,'
        '不提供针对具体标的的买卖建议或收益预测,不构成投资建议。每位用户使用自己配置的 DeepSeek API key,'
        'key 加密存储、仅调用时解密;复盘只读取你<strong>本人</strong>的模拟盘数据。</p></section>'
    )

    def render_account_ai(self, user, query):
        row = ai_service.get_key_row(self.con, user["id"])
        used = ai_service.daily_tokens(self.con, user["id"])
        if row is None:
            status_html = '<p class="muted">尚未配置 API key。</p>'
            enabled = False
        else:
            enabled = bool(int(row["enabled"]))
            status_html = (
                f'<table><tr><th>状态</th><td>{"已启用" if enabled else "已停用"}</td></tr>'
                f'<tr><th>Key</th><td>{escape(row["masked_hint"])}</td></tr>'
                f'<tr><th>Base URL</th><td>{escape(row["base_url"])}</td></tr>'
                f'<tr><th>模型</th><td>{escape(row["model"])}</td></tr>'
                f'<tr><th>今日用量</th><td>{used} tokens</td></tr>'
                f'<tr><th>上次校验</th><td>{escape(row["status"] or "未校验")}</td></tr></table>'
            )
        disabled_note = (
            '<div class="msg err">服务端已全局关闭 AI 功能(OWQ_AI_DISABLED)。</div>'
            if ai_service.ai_disabled() else ""
        )
        cur_base = escape(row["base_url"]) if row else "https://api.deepseek.com"
        cur_model_raw = str(row["model"] if row else ai_service.client.DEFAULT_MODEL)
        known_models = {value for value, _ in ai_service.client.MODEL_OPTIONS}
        model_options = "".join(
            f'<option value="{escape(value)}"{" selected" if value == cur_model_raw else ""}>{escape(label)}</option>'
            for value, label in ai_service.client.MODEL_OPTIONS
        )
        if cur_model_raw not in known_models:
            model_options += f'<option value="{escape(cur_model_raw)}" selected>{escape(cur_model_raw)} (当前保存)</option>'
        toggle_action = "disable" if enabled else "enable"
        toggle_label = "停用 AI" if enabled else "启用 AI"
        body = f"""
{self.message_html(query)}
{self.AI_BANNER}
{disabled_note}
<section class="card">
  <h2>AI 教练配置</h2>
  {status_html}
  <form method="post" action="/account/ai">
    {csrf_input(user)}
    <input type="hidden" name="action" value="save">
    <label>DeepSeek API Key</label>
    <input name="api_key" type="password" placeholder="sk-..." autocomplete="off">
    <div class="row">
      <div><label>Base URL</label><input name="base_url" value="{cur_base}"></div>
      <div><label>模型</label><select name="model">{model_options}</select></div>
    </div>
    <p class="muted">默认建议 DeepSeek V4 Flash;复杂复盘可切换 V4 Pro。key 用服务端密钥加密后存储,仅调用时解密,不显示明文、不进入导出或日志。</p>
    <p><button type="submit">保存并校验</button></p>
  </form>
  <div class="row">
    <form method="post" action="/account/ai">{csrf_input(user)}<input type="hidden" name="action" value="{toggle_action}"><button type="submit" class="secondary">{toggle_label}</button></form>
    <form method="post" action="/account/ai" onsubmit="return confirm('确定删除已保存的 API key?');">{csrf_input(user)}<input type="hidden" name="action" value="delete"><button type="submit" class="secondary">删除 key</button></form>
  </div>
</section>
<section class="card">
  <h2>AI 复盘:解释我自己的模拟盘结果</h2>
  <p class="muted">基于你<strong>本人</strong>的持仓、成交和演练计划,做方法层面的复盘和引导(不预测、不荐股)。</p>
  <form method="post" action="/account/ai-review">
    {csrf_input(user)}
    <label>想让 AI 重点看什么?(可选)</label>
    <input name="question" placeholder="例如:我这波操作的风险控制有什么问题?">
    <p><button type="submit">让 AI 复盘</button></p>
  </form>
</section>
"""
        self.send_html("AI 教练", body, user=user)

    def handle_account_ai(self, user, form):
        action = (form.get("action") or "save").strip()
        try:
            if action == "save":
                key = (form.get("api_key") or "").strip()
                if not key:
                    self.redirect("/account/ai?err=" + quote("请填入 API key。"))
                    return
                base_url = form.get("base_url") or ai_service.client.DEFAULT_BASE_URL
                model = form.get("model") or ai_service.client.DEFAULT_MODEL
                result = ai_service.client.test_api_key(key, base_url=base_url, model=model)
                ai_service.save_key(self.con, user["id"], SECRET, key, base_url, model, status=result["detail"][:120])
                self.audit("ai.key_saved", user=user, target_type="ai", detail={"ok": result["ok"]})
                tail = "key 已保存并校验通过。" if result["ok"] else "key 已保存,但校验失败:" + result["detail"]
                self.redirect("/account/ai?msg=" + quote(tail))
                return
            if action in {"enable", "disable"}:
                ai_service.set_enabled(self.con, user["id"], action == "enable")
                self.audit("ai.key_toggled", user=user, target_type="ai", detail={"enabled": action == "enable"})
                self.redirect("/account/ai?msg=" + quote("已更新 AI 启用状态。"))
                return
            if action == "delete":
                ai_service.delete_key(self.con, user["id"])
                self.audit("ai.key_deleted", user=user, target_type="ai")
                self.redirect("/account/ai?msg=" + quote("已删除 API key。"))
                return
            self.redirect("/account/ai?err=" + quote("未知操作。"))
        except ValueError as exc:
            self.redirect("/account/ai?err=" + quote(sanitize_diagnostic_message(exc)))

    def handle_account_ai_review(self, user, form):
        if not self.require_user_write_limit(user, "ai_review", 12, 3600, "/account/ai"):
            return
        result = ai_service.explain_my_result(
            self.con,
            user["id"],
            secret=SECRET,
            leak_check_secrets=self.sensitive_secret_values(),
            question=form.get("question", ""),
        )
        self.audit(
            "ai.review",
            user=user,
            target_type="ai",
            detail={"ok": result["ok"], "blocked": result.get("blocked", False), "error": result.get("error", "")},
        )
        answer = render_markdown(result["text"])
        klass = "msg" if result["ok"] and not result.get("blocked") else "msg err"
        body = f"""
{self.AI_BANNER}
<section class="card">
  <h2>AI 复盘结果</h2>
  <div class="{klass}"><div class="markdown-body">{answer}</div></div>
  <p><a class="btn secondary" href="/account/ai">返回 AI 教练</a></p>
</section>
"""
        self.send_html("AI 复盘结果", body, user=user)

    def handle_account_reset(self, user, form):
        if (form.get("confirm") or "").strip() != "RESET":
            self.redirect("/account?err=" + quote("请输入 RESET 确认重置模拟账户。"))
            return
        try:
            services.reset_paper_account(self.con, user["id"])
        except ValueError as exc:
            self.redirect("/account?err=" + quote(str(exc)))
            return
        self.audit("account.reset", user=user, target_type="account", target_id=user["id"])
        self.redirect("/account?msg=" + quote("模拟账户已重置,可以重新开始演练。"))

    def handle_account_settle(self, user):
        try:
            count = services.settle_account(self.con, user["id"])
        except ValueError as exc:
            self.redirect("/account?err=" + quote(str(exc)))
            return
        self.audit("account.settle", user=user, target_type="account", target_id=user["id"], detail={"released_holdings": count})
        self.redirect("/account?msg=" + quote(f"已进入下一交易日,{count} 个持仓标的变为可卖。"))

    def handle_account_profile(self, user, form):
        try:
            services.update_user_profile(
                self.con,
                user["id"],
                form.get("nickname", ""),
                form.get("avatar_url", ""),
            )
        except ValueError as exc:
            self.redirect("/account?err=" + quote(str(exc)))
            return
        self.audit("account.profile_update", user=user, target_type="user", target_id=user["id"])
        self.redirect("/account?msg=" + quote("账户资料已保存。"))

    def handle_account_password(self, user, form):
        existing_hash = str(user["password_hash"] or "")
        if existing_hash and not services.verify_password(form.get("current_password") or "", existing_hash):
            self.redirect("/account?err=" + quote("当前密码不正确。"))
            return
        password = form.get("password") or ""
        if password != (form.get("password_confirm") or ""):
            self.redirect("/account?err=" + quote("两次输入的新密码不一致。"))
            return
        try:
            login_name = services.ensure_login_name_available(self.con, form.get("login_name", ""), user_id=int(user["id"]))
            services.validate_password(password)
            services.set_user_password(self.con, int(user["id"]), login_name, password, update_nickname=False)
        except ValueError as exc:
            self.redirect("/account?err=" + quote(str(exc)))
            return
        self.audit("account.password_update", user=user, target_type="user", target_id=user["id"])
        self.redirect(
            "/login?msg=" + quote("登录密码已更新,请重新登录。"),
            clear_cookie=True,
        )

    def handle_account_consent(self, user, form):
        next_path = self.safe_next_path(form.get("next"), default="/app")
        if form.get("accept_terms") != "1":
            self.redirect(
                "/account/consent?next="
                + quote(next_path)
                + "&err="
                + quote("请先阅读并同意服务条款、隐私说明和风险提示。")
            )
            return
        consent_id = self.record_current_consent(int(user["id"]), "legal_update")
        self.audit(
            "legal.consent",
            user=user,
            target_type="user_consent",
            target_id=consent_id,
            detail={"version": LEGAL_VERSION, "source": "legal_update"},
        )
        separator = "&" if "?" in next_path else "?"
        self.redirect(next_path + separator + "msg=" + quote("已确认当前服务条款。"))

    def handle_account_delete(self, user, form):
        if (form.get("confirm") or "").strip() != "DELETE":
            self.redirect("/account?err=" + quote("请输入 DELETE 确认关闭账户。"))
            return
        user_id = int(user["id"])
        try:
            summary = services.delete_user_account(self.con, user_id)
        except ValueError as exc:
            self.redirect("/account?err=" + quote(str(exc)))
            return
        self.audit("account.delete", target_type="user", target_id=user_id, detail=summary)
        self.redirect("/register?msg=" + quote("账户已关闭,相关个人数据已删除。"), clear_cookie=True)

    def render_admin(self, user, query):
        contest = services.active_contest(self.con)
        checks = doctor.check(self.con)
        accounts = services.account_overview(self.con)
        consents = services.user_consent_summary(self.con, limit=100)
        consent_rows = "".join(
            f"<tr><td>{c['user_id']}</td><td>{escape(display_nickname(c))}</td>"
            f"<td>{escape(c['terms_version'] or '-')}</td><td>{escape(c['privacy_version'] or '-')}</td>"
            f"<td>{escape(c['risk_version'] or '-')}</td><td>{escape(c['source'] or '-')}</td>"
            f"<td>{escape(c['consent_at'] or '-')}</td><td>{escape(c['ip_address'] or '-')}</td></tr>"
            for c in consents
        ) or '<tr><td colspan="8" class="muted">暂无用户</td></tr>'
        reports = services.content_reports(self.con, limit=50)
        report_rows = "".join(self.admin_report_row(r, user) for r in reports) or '<tr><td colspan="8" class="muted">暂无举报</td></tr>'
        support_requests = services.support_requests(self.con, limit=50)
        support_rows = (
            "".join(self.admin_support_request_row(r, user) for r in support_requests)
            or '<tr><td colspan="9" class="muted">暂无支持请求</td></tr>'
        )
        security_summary = services.security_audit_summary(self.con)
        security_action_rows = "".join(
            f"<tr><td>{escape(row['action'])}</td><td>{int(row['count'])}</td></tr>"
            for row in security_summary["by_action"]
        ) or '<tr><td colspan="2" class="muted">近 24 小时暂无安全或异常事件</td></tr>'
        security_recent_rows = "".join(
            f"<tr><td>{escape(e['created_at'])}</td><td>{escape(e['action'])}</td>"
            f"<td>{escape(audit_actor_name(e))}</td><td>{escape(e['target_type'] or '-')}</td>"
            f"<td>{escape(e['target_id'] or '-')}</td><td>{escape(e['ip_address'] or '-')}</td></tr>"
            for e in security_summary["recent"]
        ) or '<tr><td colspan="6" class="muted">暂无安全或异常事件</td></tr>'
        email_session_summary = services.email_login_session_retention_summary(self.con)
        email_session_retention_text = (
            f"保留 {email_session_summary['detail']}; 当前 {email_session_summary['total']} 条, "
            f"{email_session_summary['expired_pending']} 条待过期标记, "
            f"{email_session_summary['deletable']} 条可清理, 截止 {email_session_summary['cutoff']}。"
            if email_session_summary["ok"]
            else email_session_summary["detail"]
        )
        audit_summary = services.audit_retention_summary(self.con)
        audit_retention_text = (
            f"保留 {audit_summary['detail']}; 当前 {audit_summary['total']} 条, "
            f"{audit_summary['expired']} 条超过保留期, 截止 {audit_summary['cutoff']}。"
            if audit_summary["ok"]
            else audit_summary["detail"]
        )
        audit_rows = "".join(
            f"<tr><td>{escape(e['created_at'])}</td><td>{escape(e['action'])}</td>"
            f"<td>{escape(audit_actor_name(e))}</td>"
            f"<td>{escape(e['target_type'] or '-')}</td><td>{escape(e['target_id'] or '-')}</td>"
            f"<td>{escape(e['detail'])}</td><td>{escape(e['ip_address'] or '-')}</td></tr>"
            for e in services.audit_events(self.con, limit=50)
        ) or '<tr><td colspan="7" class="muted">暂无审计事件</td></tr>'
        check_rows = "".join(
            f"<tr><td>{escape(c['name'])}</td><td>{escape(c['status'])}</td><td>{escape(c['detail'])}</td></tr>"
            for c in checks
        )
        release_gate = self.release_gate_html(checks)
        email_status = email_config.status()
        email_provider = str(email_status["provider"] or "未配置")
        email_provider_label = {"cloudflare": "Cloudflare Email Sending", "smtp": "SMTP"}.get(email_provider, email_provider)
        email_status_class = "ok" if email_status["configured"] else "bad"
        email_status_detail = str(email_status["detail"])
        demo_participants = services.demo_contest_participant_summary(self.con)
        demo_participant_count = int(demo_participants["participants"])
        demo_participant_note = (
            f"当前启用公开赛中有 {demo_participant_count} 个演示/开发参赛账户"
            + (f" (user_ids={escape(demo_participants['user_ids'])})" if demo_participants["user_ids"] else "")
            + "。正式发布前应移出公开赛。"
            if demo_participant_count
            else "当前启用公开赛未发现演示/开发参赛账户。"
        )
        account_rows = "".join(
            f"<tr><td>{a['row']['user_id']}</td><td><a href=\"/u/{a['row']['user_id']}\">{escape(display_nickname(a['row']))}</a></td>"
            f"<td>{a['rank'] or '-'}</td><td>{money(a['row']['equity'])}</td><td>{pct(a['return_pct'])}</td>"
            f"<td>{a['row']['order_count']}</td><td>{a['row']['post_count']}</td>"
            f"<td>{escape(a['row']['status'] or 'active')}</td><td>{self.admin_user_status_action(a['row'], user)}</td></tr>"
            for a in accounts
        ) or '<tr><td colspan="9" class="muted">暂无用户</td></tr>'
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>运营管理</h2>
  <p>本地 MVP 的管理入口:检查系统状态、配置比赛展示、查看用户账户概览。</p>
</section>
{release_gate}
<div class="grid">
  <section class="card">
    <h2>系统自检</h2>
    <table><thead><tr><th>项目</th><th>状态</th><th>说明</th></tr></thead><tbody>{check_rows}</tbody></table>
  </section>
  <section class="card">
    <h2>比赛配置</h2>
    <form method="post" action="/admin/contest">
      {csrf_input(user)}
      <label>比赛名称</label>
      <input name="title" value="{escape(contest['title']) if contest else '模拟盘公开赛'}">
      <label>说明</label>
      <textarea name="description">{escape(contest['description']) if contest else ''}</textarea>
      <p><button type="submit">保存比赛配置</button></p>
    </form>
  </section>
</div>
<section class="card">
  <h2>应用数据备份</h2>
  <p>立即使用 SQLite online backup API 生成一致性备份,会包含用户、账户、模拟盘、比赛、论坛和登录会话。</p>
  <form method="post" action="/admin/backup">
    {csrf_input(user)}
    <button type="submit">立即备份应用数据库</button>
  </form>
</section>
<section class="card">
  <h2>邮件发信诊断</h2>
  <p>当前发信状态: <strong class="{email_status_class}">{escape(email_provider_label)}</strong> · {escape(email_status_detail)}</p>
  <p>配置 Cloudflare Email Sending 或 SMTP 后,先发送一封测试邮件验证发信、DNS 和收件链路。</p>
  <form method="post" action="/admin/email-test">
    {csrf_input(user)}
    <label>测试收件邮箱</label>
    <input name="email" type="email" value="{escape(user['email'] or '')}" placeholder="you@example.com">
    <p><button type="submit">发送测试邮件</button></p>
  </form>
</section>
<section class="card">
  <h2>邮箱登录临时会话</h2>
  <p>{escape(email_session_retention_text)}</p>
  <form method="post" action="/admin/email-login-prune">
    {csrf_input(user)}
    <button class="secondary" type="submit">清理登录临时会话</button>
  </form>
</section>
<section class="card">
  <h2>演示数据</h2>
  <p>一键创建 3 个 demo 参赛账户、持仓、资产快照和论坛策略帖,方便 beta 阶段展示公开榜单和论坛传播效果。只会覆盖 demo-* 用户。</p>
  <p>{demo_participant_note}</p>
  <form method="post" action="/admin/demo-seed">
    {csrf_input(user)}
    <button type="submit">生成演示比赛数据</button>
  </form>
  <form method="post" action="/admin/demo-contest-clean">
    {csrf_input(user)}
    <button class="secondary" type="submit">移出演示/开发参赛账户</button>
  </form>
</section>
<section class="card">
  <h2>用户账户概览</h2>
  <p><a class="btn secondary" href="/admin/accounts.csv">导出用户账户 CSV</a></p>
  <table><thead><tr><th>ID</th><th>用户</th><th>排名</th><th>总资产</th><th>收益率</th><th>成交</th><th>帖子</th><th>状态</th><th>操作</th></tr></thead><tbody>{account_rows}</tbody></table>
</section>
<section class="card">
  <h2>用户同意记录</h2>
  <p>展示每个用户最近一次确认的服务条款、隐私说明和风险提示版本。</p>
  <table><thead><tr><th>ID</th><th>用户</th><th>条款</th><th>隐私</th><th>风险</th><th>来源</th><th>时间</th><th>IP</th></tr></thead><tbody>{consent_rows}</tbody></table>
</section>
<section class="card">
  <h2>内容举报</h2>
  <p>用户提交的帖子和评论举报。处理后会保留记录,用于社区治理和后续追踪。</p>
  <p><a class="btn secondary" href="/admin/reports.csv">导出内容举报 CSV</a></p>
  <table><thead><tr><th>时间</th><th>状态</th><th>举报人</th><th>目标</th><th>原因</th><th>处理人</th><th>备注</th><th>操作</th></tr></thead><tbody>{report_rows}</tbody></table>
</section>
<section class="card">
  <h2>支持请求</h2>
  <p>公开联系支持页提交的注册、登录、数据、社区和商务请求。处理记录会留在后台审计链路中。</p>
  <p><a class="btn secondary" href="/admin/support.csv">导出支持请求 CSV</a></p>
  <table><thead><tr><th>时间</th><th>状态</th><th>类型</th><th>提交人</th><th>邮箱</th><th>主题</th><th>处理人</th><th>备注</th><th>操作</th></tr></thead><tbody>{support_rows}</tbody></table>
</section>
<section class="card">
  <h2>安全和异常事件</h2>
  <p>近 {security_summary['hours']} 小时 {security_summary['total_window']} 条;近 7 天 {security_summary['total_7d']} 条。覆盖登录失败、限流、CSRF、越权、服务端错误、发信失败和同步失败等事件。</p>
  <div class="grid">
    <div>
      <h3>近 24 小时按类型</h3>
      <table><thead><tr><th>事件类型</th><th>次数</th></tr></thead><tbody>{security_action_rows}</tbody></table>
    </div>
    <div>
      <h3>最近事件</h3>
      <table><thead><tr><th>时间</th><th>动作</th><th>操作者</th><th>目标类型</th><th>目标 ID</th><th>IP</th></tr></thead><tbody>{security_recent_rows}</tbody></table>
    </div>
  </div>
</section>
<section class="card">
  <h2>审计日志</h2>
  <p>记录注册、交易、行情同步、账户、论坛、备份和管理操作,用于排查和运营追踪。</p>
  <p>{escape(audit_retention_text)}</p>
  <form method="post" action="/admin/audit-prune">
    {csrf_input(user)}
    <button class="secondary" type="submit">清理超期审计日志</button>
    <a class="btn secondary" href="/admin/audit.csv">导出审计日志 CSV</a>
  </form>
  <table><thead><tr><th>时间</th><th>动作</th><th>操作者</th><th>目标类型</th><th>目标 ID</th><th>摘要</th><th>IP</th></tr></thead><tbody>{audit_rows}</tbody></table>
</section>
"""
        self.send_html("管理", body, user=user)

    def export_admin_audit_csv(self, user):
        rows = services.audit_events(self.con, limit=5000)
        self.audit("admin.audit_export", user=user, target_type="audit_events", detail={"rows": len(rows)})
        self.send_csv(
            "audit-events.csv",
            ["created_at", "action", "actor_user_id", "actor", "target_type", "target_id", "detail", "ip_address"],
            [
                [
                    r["created_at"],
                    r["action"],
                    r["actor_user_id"] or "",
                    audit_actor_name(r),
                    r["target_type"] or "",
                    r["target_id"] or "",
                    r["detail"],
                    r["ip_address"] or "",
                ]
                for r in rows
            ],
        )

    def export_admin_accounts_csv(self, user):
        rows = services.account_overview(self.con)
        self.audit("admin.accounts_export", user=user, target_type="account_overview", detail={"rows": len(rows)})
        self.send_csv(
            "admin-accounts.csv",
            [
                "user_id",
                "nickname",
                "email",
                "status",
                "status_reason",
                "status_updated_at",
                "created_at",
                "rank",
                "account_id",
                "initial_cash",
                "cash",
                "market_value",
                "equity",
                "return_pct",
                "order_count",
                "post_count",
            ],
            [
                [
                    r["row"]["user_id"],
                    display_nickname(r["row"]),
                    r["row"]["email"] or "",
                    r["row"]["status"] or "active",
                    r["row"]["status_reason"] or "",
                    r["row"]["status_updated_at"] or "",
                    r["row"]["created_at"] or "",
                    r["rank"] or "",
                    r["row"]["account_id"],
                    r["row"]["initial_cash"],
                    r["row"]["cash"],
                    r["row"]["market_value"],
                    r["row"]["equity"],
                    round(float(r["return_pct"]), 6),
                    r["row"]["order_count"],
                    r["row"]["post_count"],
                ]
                for r in rows
            ],
        )

    def export_admin_reports_csv(self, user):
        rows = services.content_reports(self.con, limit=500)
        self.audit("admin.reports_export", user=user, target_type="content_reports", detail={"rows": len(rows)})
        self.send_csv(
            "content-reports.csv",
            [
                "id",
                "created_at",
                "status",
                "reporter_user_id",
                "reporter",
                "target_type",
                "target_id",
                "target",
                "reason",
                "resolver_user_id",
                "resolver",
                "resolved_at",
                "resolution_note",
            ],
            [
                [
                    r["id"],
                    r["created_at"],
                    r["status"],
                    r["reporter_user_id"],
                    report_user_name(r, "reporter_user_id", "reporter_nickname"),
                    r["target_type"],
                    r["target_id"],
                    self.admin_report_target(r)[0],
                    r["reason"],
                    r["resolver_user_id"] or "",
                    report_user_name(r, "resolver_user_id", "resolver_nickname"),
                    r["resolved_at"] or "",
                    r["resolution_note"] or "",
                ]
                for r in rows
            ],
        )

    def export_admin_support_csv(self, user):
        rows = services.support_requests(self.con, limit=500)
        self.audit("admin.support_export", user=user, target_type="support_requests", detail={"rows": len(rows)})
        self.send_csv(
            "support-requests.csv",
            [
                "id",
                "created_at",
                "status",
                "category",
                "requester_user_id",
                "requester",
                "email",
                "subject",
                "message",
                "handler_user_id",
                "handler",
                "resolved_at",
                "resolution_note",
            ],
            [
                [
                    r["id"],
                    r["created_at"],
                    r["status"],
                    r["category"],
                    r["requester_user_id"] or "",
                    support_request_user_name(r, "requester_user_id", "requester_nickname"),
                    r["email"],
                    r["subject"],
                    r["message"],
                    r["handler_user_id"] or "",
                    support_request_user_name(r, "handler_user_id", "handler_nickname") if r["handler_user_id"] else "",
                    r["resolved_at"] or "",
                    r["resolution_note"] or "",
                ]
                for r in rows
            ],
        )

    def handle_admin_audit_prune(self, user):
        try:
            result = services.prune_audit_events(self.con)
        except ValueError as exc:
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.audit_prune", user=user, target_type="audit_events", detail=result)
        self.redirect(
            "/admin?msg="
            + quote(
                f"审计日志清理完成: 删除 {result['deleted']} 条, "
                f"保留 {result['remaining']} 条, 截止 {result['cutoff']}。"
            )
        )

    def handle_admin_email_login_prune(self, user):
        try:
            result = services.prune_email_login_sessions(self.con)
        except ValueError as exc:
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.email_login_prune", user=user, target_type="email_login_sessions", detail=result)
        self.redirect(
            "/admin?msg="
            + quote(
                f"邮箱登录临时会话清理完成: 过期标记 {result['expired']} 条, "
                f"删除 {result['deleted']} 条, 保留 {result['remaining']} 条。"
            )
        )

    def admin_user_status_action(self, row, admin_user) -> str:
        target_id = int(row["user_id"])
        status = str(row["status"] or "active")
        reason = str(row["status_reason"] or "").strip()
        if target_id == int(admin_user["id"]):
            return '<span class="muted">当前管理员</span>'
        if status == "suspended":
            return (
                f'<form method="post" action="/admin/users/{target_id}/status">'
                f'{csrf_input(admin_user)}'
                '<input type="hidden" name="status" value="active">'
                f'<span class="muted">{escape(reason or "已暂停")}</span> '
                '<button type="submit">解除暂停</button></form>'
            )
        return (
            f'<form method="post" action="/admin/users/{target_id}/status">'
            f'{csrf_input(admin_user)}'
            '<input type="hidden" name="status" value="suspended">'
            '<input name="reason" placeholder="暂停原因">'
            '<button type="submit">暂停</button></form>'
        )

    def admin_report_target(self, report) -> tuple[str, str]:
        if report["target_type"] == "post":
            target_label = report["post_title"] or f"帖子 #{report['target_id']}"
            target_href = f"/forum/{report['target_id']}"
        else:
            target_label = report["comment_post_title"] or f"评论 #{report['target_id']}"
            target_href = f"/forum/{report['comment_post_id']}" if report["comment_post_id"] else "/forum"
        return str(target_label), str(target_href)

    def admin_report_row(self, report, user) -> str:
        reporter = report_user_name(report, "reporter_user_id", "reporter_nickname")
        resolver = report_user_name(report, "resolver_user_id", "resolver_nickname")
        target_label, target_href = self.admin_report_target(report)
        if report["status"] == "pending":
            action = (
                f'<form method="post" action="/admin/reports/{report["id"]}/resolve">'
                f'{csrf_input(user)}'
                '<select name="status"><option value="resolved">已处理</option><option value="dismissed">驳回</option></select>'
                '<input name="note" placeholder="处理备注">'
                '<button type="submit">保存</button></form>'
            )
        else:
            action = "-"
        return (
            f"<tr><td>{escape(report['created_at'])}</td><td>{escape(report['status'])}</td>"
            f"<td>{escape(reporter)}</td><td><a href=\"{escape(target_href)}\">{escape(target_label)}</a></td>"
            f"<td>{escape(report['reason'])}</td><td>{escape(resolver)}</td>"
            f"<td>{escape(report['resolution_note'] or '-')}</td><td>{action}</td></tr>"
        )

    def admin_support_request_row(self, request, user) -> str:
        requester = support_request_user_name(request, "requester_user_id", "requester_nickname")
        handler = support_request_user_name(request, "handler_user_id", "handler_nickname") if request["handler_user_id"] else "-"
        if request["status"] == "open":
            action = (
                f'<form method="post" action="/admin/support/{request["id"]}/resolve">'
                f'{csrf_input(user)}'
                '<select name="status"><option value="resolved">已处理</option><option value="dismissed">无需处理</option></select>'
                '<input name="note" placeholder="处理备注">'
                '<button type="submit">保存</button></form>'
            )
        else:
            action = "-"
        return (
            f"<tr><td>{escape(request['created_at'])}</td><td>{escape(request['status'])}</td>"
            f"<td>{escape(request['category'])}</td><td>{escape(requester)}</td><td>{escape(request['email'])}</td>"
            f"<td>{escape(request['subject'])}</td><td>{escape(handler)}</td>"
            f"<td>{escape(request['resolution_note'] or '-')}</td><td>{action}</td></tr>"
        )

    def handle_admin_contest(self, user, form):
        try:
            services.update_active_contest(
                self.con,
                form.get("title", ""),
                form.get("description", ""),
            )
        except ValueError as exc:
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.contest_update", user=user, target_type="contest", detail={"title": form.get("title", "")})
        self.redirect("/admin?msg=" + quote("比赛配置已保存。"))

    def handle_admin_backup(self, user):
        try:
            path = db.backup_database(self.con)
        except Exception as exc:  # noqa: BLE001
            self.audit("admin.backup_failed", user=user, target_type="app_db", detail=exception_diagnostic(exc))
            self.redirect("/admin?err=" + quote("备份失败,请查看安全和异常事件。"))
            return
        self.audit("admin.backup", user=user, target_type="app_db", target_id=path.name, detail={"file": path.name})
        self.redirect("/admin?msg=" + quote("应用数据库备份已写入。"))

    def handle_admin_email_test(self, user, form):
        email_hash = ""
        email_detail: dict[str, str] = {}
        try:
            email = services.normalize_email(form.get("email", ""))
            email_hash, email_detail = email_audit_metadata(email)
            provider = self.send_transactional_email(
                email,
                "OurWorlds Quant 邮件发信测试",
                (
                    "这是一封 OurWorlds Quant 后台发信诊断邮件。\n\n"
                    "如果你收到这封邮件,说明当前发信服务可以从应用服务器发出邮件。"
                ),
                (
                    "<p>这是一封 OurWorlds Quant 后台发信诊断邮件。</p>"
                    "<p>如果你收到这封邮件,说明当前发信服务可以从应用服务器发出邮件。</p>"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            detail = exception_diagnostic(exc)
            detail.update(email_detail)
            self.audit("admin.email_test_failed", user=user, target_type="email", target_id=email_hash, detail=detail)
            self.redirect("/admin?err=" + quote("测试邮件发送失败,请查看安全和异常事件。"))
            return
        self.audit("admin.email_test", user=user, target_type="email", target_id=email_hash, detail={"provider": provider, **email_detail})
        self.redirect("/admin?msg=" + quote("测试邮件已发送。"))

    def handle_admin_user_status(self, user, path, form):
        try:
            user_id = int(path.strip("/").split("/")[2])
            if user_id == int(user["id"]):
                raise ValueError("不能在后台暂停当前管理员账号")
            status = form.get("status", "")
            reason = form.get("reason", "")
            services.update_user_status(self.con, user_id, status, reason)
        except Exception as exc:  # noqa: BLE001
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.user_status", user=user, target_type="user", target_id=user_id, detail={"status": status, "reason": reason})
        self.redirect("/admin?msg=" + quote("用户状态已更新。"))

    def handle_admin_report_resolve(self, user, path, form):
        try:
            report_id = int(path.strip("/").split("/")[2])
            services.resolve_content_report(
                self.con,
                user["id"],
                report_id,
                form.get("status", ""),
                form.get("note", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.report_resolve", user=user, target_type="content_report", target_id=report_id, detail={"status": form.get("status", "")})
        self.redirect("/admin?msg=" + quote("举报处理结果已保存。"))

    def handle_admin_support_resolve(self, user, path, form):
        try:
            request_id = int(path.strip("/").split("/")[2])
            services.resolve_support_request(
                self.con,
                user["id"],
                request_id,
                form.get("status", ""),
                form.get("note", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/admin?err=" + quote(str(exc)))
            return
        self.audit("admin.support_resolve", user=user, target_type="support_request", target_id=request_id, detail={"status": form.get("status", "")})
        self.redirect("/admin?msg=" + quote("支持请求处理结果已保存。"))

    def handle_admin_demo_seed(self, user):
        if doctor.production_mode() and not self.email_dev_auth_enabled() and not env_flag("OWQ_ALLOW_DEMO_SEED"):
            self.redirect(
                "/admin?err="
                + quote("正式生产环境默认禁止生成演示比赛数据。如确需演练,请临时设置 OWQ_ALLOW_DEMO_SEED=1。")
            )
            return
        result = services.seed_demo_competition(self.con)
        self.audit("admin.demo_seed", user=user, target_type="contest", detail=result)
        self.redirect(
            "/admin?msg="
            + quote(
                f"已生成 {result['players']} 个演示参赛账户,新增 {result['posts_created']} 条策略帖。"
            )
        )

    def handle_admin_demo_contest_clean(self, user):
        result = services.remove_demo_contest_participants(self.con)
        self.audit("admin.demo_contest_clean", user=user, target_type="contest_participants", detail=result)
        self.redirect(
            "/admin?msg="
            + quote(
                f"已移出 {result['participants_removed']} 个演示/开发参赛账户。"
            )
        )

    def render_showcase(self, user, query):
        board = services.leaderboard(self.con)
        contest = services.active_contest(self.con)
        rows = "".join(
            f"<tr><td>{r['rank']}</td><td><a href=\"/u/{r['row']['user_id']}\">{escape(display_nickname(r['row']))}</a></td><td>{money(r['row']['equity'])}</td>"
            f"<td>{pct(r['return_pct'])}</td></tr>"
            for r in board
        ) or '<tr><td colspan="4" class="muted">暂无参赛用户</td></tr>'
        share_url = f"{self.base_url()}/showcase/public"
        profile_url = f"{self.base_url()}/u/{user['id']}"
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>{escape(contest['title']) if contest else '模拟盘公开赛'}</h2>
  <p>{escape(contest['description']) if contest else '展示参赛者的模拟盘收益表现。'}</p>
  <form method="post" action="/contest/join">{csrf_input(user)}<button type="submit">加入公开赛</button></form>
</section>
<section class="card">
  <h2>排行榜 Showcase</h2>
  <table><thead><tr><th>排名</th><th>用户</th><th>总资产</th><th>收益率</th></tr></thead><tbody>{rows}</tbody></table>
  <p class="muted">公开榜单: <a href="/showcase/public">{escape(share_url)}</a></p>
</section>
<section class="card">
  <div class="card-title"><span>分享我的战绩</span></div>
  <img src="/u/{int(user['id'])}/card.svg" alt="我的模拟战绩卡" loading="lazy" style="max-width:100%;border:1px solid var(--line);border-radius:10px;margin:6px 0">
  <p class="muted">这是<strong>模拟训练账户</strong>的战绩卡(估值/回测已含退市股,但仍是模拟、非真实委托)。把下面的链接分享到社媒,会自动带上这张卡片预览。</p>
  <p><a class="btn" href="/u/{int(user['id'])}">打开我的公开战绩页 →</a>
     <button type="button" class="btn secondary" data-copy="{escape(profile_url, quote=True)}">复制分享链接</button>
     <a class="btn secondary" href="/forum/new?template=performance">生成战绩复盘帖</a></p>
  <p class="muted">链接: <a href="/u/{int(user['id'])}">{escape(profile_url)}</a></p>
</section>
"""
        self.send_html("比赛展示", body, user=user)

    def render_public_showcase(self, query):
        board = services.leaderboard(self.con)
        contest = services.active_contest(self.con)
        summary = services.landing_summary(self.con)
        prediction = self.public_prediction_status()
        rows = "".join(
            f"<tr><td>{r['rank']}</td><td><a href=\"/u/{r['row']['user_id']}\">{escape(display_nickname(r['row']))}</a></td>"
            f"<td>{money(r['row']['equity'])}</td><td>{pct(r['return_pct'])}</td></tr>"
            for r in board
        ) or '<tr><td colspan="4" class="muted">暂无参赛用户</td></tr>'
        contest_title = contest['title'] if contest else '模拟盘公开赛'
        description = contest['description'] if contest else '展示参赛者的模拟盘收益表现。'
        participant_count = int(summary.get("participant_count") or 0)
        order_count = int(summary.get("order_count") or 0)
        discussion_count = int(summary.get("post_count") or 0) + int(summary.get("comment_count") or 0)
        top_return = pct(board[0]["return_pct"]) if board else "待开赛"
        market_count = self.landing_market_count_text(summary)
        as_of = self.landing_date_text(summary.get("market_as_of"))
        source = self.landing_source_label(summary)
        latest_posts = services.forum_posts(self.con, limit=3)
        post_rows = "".join(
            '<div class="post">'
            f'<a href="/forum/{post["id"]}"><strong>{escape(post["title"])}</strong></a>'
            f'<p>{escape(str(post["body"])[:120])}</p>'
            f'<span class="tag">{escape(display_nickname(post))} · {escape(post["strategy_tag"])}</span>'
            '</div>'
            for post in latest_posts
        ) or '<p class="muted">暂无复盘帖。参赛后可以从比赛页一键生成带战绩快照的复盘草稿。</p>'
        top_image = (
            f"{self.base_url()}/u/{board[0]['row']['user_id']}/card.svg"
            if board
            else ""
        )
        join_primary = self.public_join_button("btn", primary=True)
        join_secondary = self.public_join_button("btn", primary=True)
        body = f"""
<section class="cards">
  <div class="card"><p>参赛账户</p><div class="metric">{participant_count} 人</div><p>{escape(self.public_join_hint())}</p></div>
  <div class="card"><p>榜首收益</p><div class="metric">{top_return}</div><p>按当前模拟账户权益实时排序</p></div>
  <div class="card"><p>真实行情</p><div class="metric">{escape(market_count)}</div><p>{escape(source)} · {escape(str(as_of))}</p></div>
</section>
<section class="card">
  <h2>{escape(contest_title)}</h2>
  <p>{escape(description)}</p>
  <p>{join_primary} <a class="btn secondary" href="/login">账号密码登录</a> <a class="btn secondary" href="/forum">进入策略论坛</a> <a class="btn secondary" href="/data-status">查看数据状态</a></p>
</section>
<section class="grid">
  <div class="card">
    <h2>公开排行榜</h2>
    <table><thead><tr><th>排名</th><th>用户</th><th>总资产</th><th>收益率</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
  <div class="card">
    <h2>赛场讨论</h2>
    <p>复盘帖会附带发帖时的收益、总资产和比赛排名快照,方便围绕结果讨论策略。</p>
    {post_rows}
  </div>
</section>
<section class="card">
  <h2>数据和组合设计</h2>
  <p>当前模拟盘成交、公开榜单和组合设计共用同一套已同步行情。预测候选状态: {escape(str(prediction["detail"]))}。</p>
  <p>登录后可以直接用真实行情和模型候选生成组合演练计划,不需要自己准备 CSV。</p>
  <p>{join_secondary} <a class="btn secondary" href="/data-status">公开数据透明页</a></p>
</section>
<section class="cards">
  <div class="card"><p>模拟成交</p><div class="metric">{order_count} 笔</div><p>交易记录用于排名和复盘</p></div>
  <div class="card"><p>讨论互动</p><div class="metric">{discussion_count} 条</div><p>帖子和评论围绕公开战绩展开</p></div>
  <div class="card"><p>预测匹配</p><div class="metric">{int(prediction.get("matched_count") or 0)} 个</div><p>候选可匹配当前真实行情</p></div>
</section>
"""
        self.send_html(
            "公开榜单",
            body,
            meta={
                "title": f"{contest_title} · 公开排行榜",
                "description": description,
                "url": f"{self.base_url()}/showcase/public",
                "image": top_image,
            },
        )

    def render_public_profile(self, path, query):
        try:
            user_id = int(path.rsplit("/", 1)[-1])
            profile = services.public_profile(self.con, user_id)
        except Exception:
            self.not_found()
            return
        user = profile["user"]
        public_name = display_nickname(user)
        snap = profile["snapshot"]
        posts = profile["posts"]
        history = profile["history"]
        orders = profile["orders"]
        holding_rows = "".join(
            f"<tr><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td><td>{r['qty']}</td><td>{r['available_qty']}</td>"
            f"<td>{money(r['avg_price'])}</td><td>{money(r['price'])}</td><td>{money(r['market_value'])}</td><td>{money(r['pnl'])}</td></tr>"
            for r in snap["holdings"]
        ) or '<tr><td colspan="8" class="muted">暂无持仓</td></tr>'
        order_rows = "".join(
            f"<tr><td>{escape(o['created_at'])}</td><td>{escape(o['code'])}</td><td>{side_cn(o['side'])}</td>"
            f"<td>{o['qty']}</td><td>{money(o['price'])}</td><td>{money(o['fee'])}</td></tr>"
            for o in orders
        ) or '<tr><td colspan="6" class="muted">暂无成交</td></tr>'
        post_rows = "".join(
            f'<div class="post"><a href="/forum/{p["id"]}"><strong>{escape(p["title"])}</strong></a> '
            f'<span class="tag">{escape(p["strategy_tag"])}</span>'
            f'<p class="muted">发帖快照: 收益 {pct(p["snapshot_return_pct"] or 0)}'
            f'{(" · 排名 #" + str(p["snapshot_rank"])) if p["snapshot_rank"] else ""}</p></div>'
            for p in posts
        ) or '<p class="muted">暂无公开策略分享。</p>'
        rank_text = f"#{profile['rank']}" if profile["rank"] else "未参赛"
        card_url = f"{self.base_url()}/u/{user_id}/card.svg"
        profile_url = f"{self.base_url()}/u/{user_id}"
        description = (
            f"收益率 {pct(snap['return_pct'])}, 总资产 {money(snap['equity'])}, "
            f"公开赛排名 {rank_text}。"
        )
        join_primary = self.public_join_button("btn", primary=True)
        body = f"""
<section class="cards">
  <div class="card"><p>用户</p><div class="identity">{avatar_html(user)}<div class="metric">{escape(public_name)}</div></div></div>
  <div class="card"><p>公开赛排名</p><div class="metric">{rank_text}</div></div>
  <div class="card"><p>收益率</p><div class="metric">{pct(snap['return_pct'])}</div></div>
</section>
<section class="card">
  <h2>战绩概览</h2>
  <table>
    <tr><th>总资产</th><td>{money(snap['equity'])}</td></tr>
    <tr><th>现金</th><td>{money(snap['cash'])}</td></tr>
    <tr><th>持仓市值</th><td>{money(snap['market_value'])}</td></tr>
  </table>
  <p>{join_primary} <a class="btn secondary" href="/showcase/public">查看公开榜单</a> <a class="btn secondary" href="/u/{user_id}/card.svg">打开战绩卡</a></p>
  <p class="muted">战绩卡: <a href="/u/{user_id}/card.svg">{escape(card_url)}</a></p>
</section>
<section class="card">
  <h2>当前持仓</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>可卖</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th></tr></thead><tbody>{holding_rows}</tbody></table>
</section>
<section class="card">
  <h2>最近成交</h2>
  <table><thead><tr><th>时间</th><th>代码</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th></tr></thead><tbody>{order_rows}</tbody></table>
</section>
<section class="card">
  <h2>最近资产曲线</h2>
  <table><thead><tr><th>时间</th><th>总资产</th><th>现金</th><th>持仓市值</th><th>收益率</th></tr></thead><tbody>{history_rows(history)}</tbody></table>
</section>
<section class="card">
  <h2>策略分享</h2>
  {post_rows}
</section>
"""
        self.send_html(
            f"{public_name} 的战绩",
            body,
            meta={
                "title": f"{public_name} 的模拟盘战绩",
                "description": description,
                "url": profile_url,
                "image": card_url,
            },
        )

    def render_public_profile_card(self, path):
        try:
            parts = path.strip("/").split("/")
            user_id = int(parts[1])
            profile = services.public_profile(self.con, user_id)
        except Exception:
            self.not_found()
            return
        user = profile["user"]
        snap = profile["snapshot"]
        rank_text = f"#{profile['rank']}" if profile["rank"] else "未参赛"
        ret = float(snap["return_pct"])
        ret_color = "#15803d" if ret >= 0 else "#b91c1c"
        holdings = snap["holdings"]
        if holdings:
            holding_text = " / ".join(f"{r['code']} {r['qty']}股" for r in holdings[:3])
        else:
            holding_text = "暂无持仓"
        holding_text = holding_text[:72]
        title = display_nickname(user)[:18]
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" role="img" aria-labelledby="title desc">
  <title id="title">{escape(title)} 的模拟盘战绩卡</title>
  <desc id="desc">OurWorlds Quant 模拟盘公开赛战绩卡</desc>
  <rect width="960" height="540" fill="#fbfaf6"/>
  <rect x="36" y="36" width="888" height="468" rx="18" fill="#f0ede6" stroke="#d9d3c8"/>
  <text x="72" y="96" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="30" font-weight="700" fill="#171510">OurWorlds Quant 模拟盘</text>
  <text x="72" y="150" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="48" font-weight="750" fill="#171510">{escape(title)}</text>
  <text x="72" y="205" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="24" fill="#645f55">公开赛排名 {escape(rank_text)} · 统一 100 万模拟本金</text>
  <text x="72" y="300" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="26" fill="#645f55">收益率</text>
  <text x="72" y="370" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="68" font-weight="800" fill="{ret_color}">{pct(ret)}</text>
  <text x="492" y="300" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="26" fill="#645f55">总资产</text>
  <text x="492" y="370" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="52" font-weight="750" fill="#171510">{money(snap['equity'])}</text>
  <text x="72" y="440" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="22" fill="#645f55">持仓: {escape(holding_text)}</text>
  <text x="72" y="476" font-family="-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif" font-size="18" fill="#645f55">邮箱验证并登录后可加入公开赛、模拟交易、发布策略复盘</text>
</svg>"""
        self.send_svg(svg)

    def handle_join_contest(self, user):
        services.join_active_contest(self.con, user["id"])
        self.audit("contest.join", user=user, target_type="contest")
        self.redirect("/showcase?msg=" + quote("已加入公开赛。"))

    def render_forum(self, user, query):
        tag_filter = query.get("tag", [""])[0].strip()
        q_filter = query.get("q", [""])[0].strip()
        sort = query.get("sort", ["latest"])[0].strip() or "latest"
        posts = services.forum_posts(self.con, tag=tag_filter, q=q_filter, sort=sort)
        tags = services.forum_tags(self.con)
        tag_options = '<option value="">全部标签</option>' + "".join(
            f'<option value="{escape(t["tag"])}"{" selected" if t["tag"] == tag_filter else ""}>'
            f'{escape(t["tag"])} ({t["count"]})</option>'
            for t in tags
        )
        sort_labels = {"latest": "最新发布", "performance": "战绩快照", "comments": "评论最多"}
        sort_options = "".join(
            f'<option value="{key}"{" selected" if key == sort else ""}>{label}</option>'
            for key, label in sort_labels.items()
        )
        rows = "".join(
            f'<div class="post"><a href="/forum/{p["id"]}"><strong>{escape(p["title"])}</strong></a> '
            f'<a class="tag" href="/forum?{urlencode({"tag": p["strategy_tag"]})}">{escape(p["strategy_tag"])}</a>'
            f'<p>{escape(p["body"][:140])}</p><p class="muted">作者 {escape(display_nickname(p))}'
            f'{(" · 发帖收益 " + pct(p["snapshot_return_pct"])) if p["snapshot_return_pct"] is not None else ""}'
            f'{(" · 排名 #" + str(p["snapshot_rank"])) if p["snapshot_rank"] else ""}'
            f' · 评论 {p["comments"]} · {escape(p["created_at"])}</p></div>'
            for p in posts
        ) or '<p class="muted">没有匹配的帖子。</p>'
        action = '<a class="btn" href="/forum/new">发布策略分享</a>' if user else '<a class="btn" href="/login">登录后发帖</a>'
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>策略论坛</h2>
  <p>围绕模拟盘战绩、策略思路和复盘结论自由讨论。</p>
  <p>{action}</p>
  <form method="get" action="/forum">
    <div class="formline">
      <div><label>关键词</label><input name="q" value="{escape(q_filter)}" placeholder="标题 / 内容 / 作者"></div>
      <div><label>标签</label><select name="tag">{tag_options}</select></div>
      <div><label>排序</label><select name="sort">{sort_options}</select></div>
      <button type="submit">筛选</button>
    </div>
  </form>
  {rows}
</section>
"""
        meta_title = "策略论坛"
        if tag_filter:
            meta_title = f"{tag_filter} 策略讨论"
        if q_filter:
            meta_title = f"{q_filter} · 策略论坛"
        self.send_html(
            "论坛",
            body,
            user=user,
            meta={
                "title": meta_title,
                "description": "围绕模拟盘战绩、策略思路和复盘结论自由讨论。",
                "url": f"{self.base_url()}/forum",
            },
        )

    def render_new_post(self, user, query):
        draft = {"title": "", "tag": "strategy", "body": ""}
        if query.get("template", [""])[0] == "performance":
            draft = services.performance_post_draft(
                self.con,
                user["id"],
                profile_url=f"{self.base_url()}/u/{user['id']}",
            )
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>发布策略分享</h2>
  <form method="post" action="/forum/new">
    {csrf_input(user)}
    <label>标题</label><input name="title" value="{escape(draft['title'])}" placeholder="例如：低波动 + 反转月度轮动复盘">
    <div class="row">
      <div><label>标签</label><input name="tag" value="{escape(draft['tag'])}"></div>
    </div>
    <label>内容</label><textarea name="body" placeholder="写下规则、回测区间、持仓、风险和模拟盘表现。">{escape(draft['body'])}</textarea>
    <p><label><input type="checkbox" name="attach_snapshot" value="1" checked style="width:auto"> 附带当前模拟盘收益/排名快照</label></p>
    <p><button type="submit">发布</button></p>
  </form>
</section>
"""
        self.send_html("发帖", body, user=user)

    def handle_new_post(self, user, form):
        if not self.require_user_write_limit(user, "forum.post", 6, 600, "/forum/new"):
            return
        try:
            post_id = services.create_post(
                self.con,
                user["id"],
                form.get("title", ""),
                form.get("body", ""),
                form.get("tag", ""),
                form.get("attach_snapshot") == "1",
            )
        except ValueError as exc:
            self.redirect("/forum/new?err=" + quote(str(exc)))
            return
        self.audit("forum.post_create", user=user, target_type="forum_post", target_id=post_id, detail={"tag": form.get("tag", "")})
        self.redirect(f"/forum/{post_id}?msg=" + quote("帖子已发布。"))

    def render_post(self, user, path, query):
        post_id = int(path.rsplit("/", 1)[-1])
        post = services.get_post(self.con, post_id)
        if post is None:
            self.not_found()
            return
        comments = services.post_comments(self.con, post_id)
        comment_rows = "".join(
            f'<div class="post"><p>{escape(c["body"])}</p><p class="muted">{escape(display_nickname(c))} · {escape(c["created_at"])}</p>'
            f'{self.comment_actions(c, user)}{self.comment_report_form(c, user)}</div>'
            for c in comments
        ) or '<p class="muted">暂无评论</p>'
        comment_form = (
            f"""
  <form method="post" action="/forum/{post_id}/comment">
    {csrf_input(user)}
    <label>评论</label><textarea name="body"></textarea>
    <p><button type="submit">提交评论</button></p>
  </form>
"""
            if user
            else '<p><a class="btn" href="/login">登录后评论</a></p>'
        )
        body = f"""
{self.message_html(query)}
<section class="card">
  <h2>{escape(post['title'])}</h2>
  <p><span class="tag">{escape(post['strategy_tag'])}</span> <span class="muted">作者 {escape(display_nickname(post))} · {escape(post['created_at'])}</span></p>
  {self.post_snapshot_html(post)}
  <p>{escape(post['body']).replace(chr(10), '<br>')}</p>
  <p class="muted">分享链接: {escape(self.base_url())}/forum/{post_id}</p>
  {self.post_actions(post, user)}
  {self.post_report_form(post, user)}
</section>
<section class="card">
  <h2>讨论</h2>
  {comment_rows}
  {comment_form}
</section>
"""
        description = post["body"].replace("\n", " ").strip()[:140]
        if post["snapshot_return_pct"] is not None:
            description = f"发帖收益 {pct(post['snapshot_return_pct'])}。{description}"
        self.send_html(
            "帖子",
            body,
            user=user,
            meta={
                "title": post["title"],
                "description": description,
                "url": f"{self.base_url()}/forum/{post_id}",
                "image": f"{self.base_url()}/u/{post['user_id']}/card.svg",
                "type": "article",
            },
        )

    def handle_comment(self, user, path, form):
        try:
            post_id = int(path.split("/")[2])
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        if not self.require_user_write_limit(user, "forum.comment", 30, 600, f"/forum/{post_id}"):
            return
        try:
            comment_id = services.add_comment(self.con, user["id"], post_id, form.get("body", ""))
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        self.audit("forum.comment_create", user=user, target_type="forum_comment", target_id=comment_id, detail={"post_id": post_id})
        self.redirect(f"/forum/{post_id}?msg=" + quote("评论已发布。"))

    def handle_delete_post(self, user, path):
        try:
            post_id = int(path.split("/")[2])
            services.delete_post(self.con, user["id"], post_id)
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        self.audit("forum.post_delete", user=user, target_type="forum_post", target_id=post_id)
        self.redirect("/forum?msg=" + quote("帖子已删除。"))

    def handle_delete_comment(self, user, path):
        try:
            parts = path.strip("/").split("/")
            post_id = int(parts[1])
            comment_id = int(parts[3])
            deleted_post_id = services.delete_comment(self.con, user["id"], comment_id)
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        self.audit("forum.comment_delete", user=user, target_type="forum_comment", target_id=comment_id, detail={"post_id": deleted_post_id or post_id})
        self.redirect(f"/forum/{deleted_post_id or post_id}?msg=" + quote("评论已删除。"))

    def handle_report_post(self, user, path, form):
        try:
            post_id = int(path.split("/")[2])
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        if not self.require_user_write_limit(user, "content.report", 20, 3600, f"/forum/{post_id}"):
            return
        try:
            report_id = services.create_content_report(
                self.con,
                user["id"],
                "post",
                post_id,
                form.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        self.audit("content.report_create", user=user, target_type="content_report", target_id=report_id, detail={"target": "post", "target_id": post_id})
        self.redirect(f"/forum/{post_id}?msg=" + quote("举报已提交,管理员会处理。"))

    def handle_report_comment(self, user, path, form):
        try:
            parts = path.strip("/").split("/")
            post_id = int(parts[1])
            comment_id = int(parts[3])
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        if not self.require_user_write_limit(user, "content.report", 20, 3600, f"/forum/{post_id}"):
            return
        try:
            report_id = services.create_content_report(
                self.con,
                user["id"],
                "comment",
                comment_id,
                form.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            self.redirect("/forum?err=" + quote(str(exc)))
            return
        self.audit("content.report_create", user=user, target_type="content_report", target_id=report_id, detail={"target": "comment", "target_id": comment_id, "post_id": post_id})
        self.redirect(f"/forum/{post_id}?msg=" + quote("举报已提交,管理员会处理。"))

    def post_actions(self, post, user) -> str:
        if not user:
            return ""
        if int(post["user_id"]) != int(user["id"]) and not services.is_admin(self.con, user):
            return ""
        return (
            f'<form method="post" action="/forum/{post["id"]}/delete">'
            f'{csrf_input(user)}<button class="secondary" type="submit">删除帖子</button></form>'
        )

    def post_report_form(self, post, user) -> str:
        if not user:
            return ""
        return (
            f'<form method="post" action="/forum/{post["id"]}/report" class="row">'
            f'{csrf_input(user)}'
            '<input name="reason" placeholder="举报原因: 广告 / 诱导交易 / 侵权 / 其他">'
            '<button class="secondary" type="submit">举报帖子</button></form>'
        )

    def comment_actions(self, comment, user) -> str:
        if not user:
            return ""
        if int(comment["user_id"]) != int(user["id"]) and not services.is_admin(self.con, user):
            return ""
        return (
            f'<form method="post" action="/forum/{comment["post_id"]}/comments/{comment["id"]}/delete" style="display:inline">'
            f'{csrf_input(user)}<button class="secondary" type="submit">删除评论</button></form>'
        )

    def comment_report_form(self, comment, user) -> str:
        if not user:
            return ""
        return (
            f'<form method="post" action="/forum/{comment["post_id"]}/comments/{comment["id"]}/report" class="row">'
            f'{csrf_input(user)}'
            '<input name="reason" placeholder="举报原因">'
            '<button class="secondary" type="submit">举报评论</button></form>'
        )

    def post_snapshot_html(self, post) -> str:
        if post["snapshot_equity"] is None:
            return ""
        rank = f" · 排名 #{post['snapshot_rank']}" if post["snapshot_rank"] else ""
        return (
            '<p class="msg">'
            f"发帖时战绩快照: 总资产 {money(post['snapshot_equity'])} · "
            f"收益 {pct(post['snapshot_return_pct'])}{rank}"
            "</p>"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser("owq-app", description="OurWorlds Quant local web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--db", default=None, help="SQLite path, default=data/app.sqlite")
    parser.add_argument("--env-file", default=os.getenv("OWQ_ENV_FILE", ""), help="load a simple KEY=VALUE env file before running")
    parser.add_argument("--sync-market", action="store_true", help="start by syncing latest prices from src.data DuckDB")
    parser.add_argument("--sync-only", action="store_true", help="sync market data/import CSV and exit without starting the server")
    parser.add_argument("--market-csv", help="start by importing market prices from CSV")
    parser.add_argument("--market-adjust", default="none", choices=["hfq", "qfq", "none"])
    parser.add_argument("--market-limit", type=int, default=500)
    parser.add_argument("--market-include-codes-csv", default="", help="prioritize codes from this CSV when syncing DuckDB market prices")
    parser.add_argument("--replace-market", action="store_true", help="delete existing market prices before importing synced rows")
    parser.add_argument("--doctor", action="store_true", help="print readiness checks and exit")
    parser.add_argument("--doctor-strict", action="store_true", help="fail if any readiness warning remains")
    parser.add_argument(
        "--generate-demo-voice",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="generate the public guide demo narration MP3 with EdgeTTS and exit",
    )
    parser.add_argument("--demo-voice", default="", help=f"EdgeTTS voice for --generate-demo-voice, default {DEFAULT_DEMO_TTS_VOICE}")
    parser.add_argument("--sqlite-maintenance", action="store_true", help="run PRAGMA optimize and WAL checkpoint, then exit")
    parser.add_argument("--prune-audit-log", action="store_true", help="delete audit events older than the configured retention window and exit")
    parser.add_argument("--audit-retention-days", type=int, default=None, help="override OWQ_AUDIT_RETENTION_DAYS for --prune-audit-log")
    parser.add_argument("--prune-email-login-sessions", action="store_true", help="expire and delete short-lived email login session records, then exit")
    parser.add_argument("--remove-demo-contest-participants", action="store_true", help="remove demo/dev users from active public contest participants, then exit")
    parser.add_argument("--set-user-password", type=int, metavar="USER_ID", help="set a user's login password from an environment variable, then exit")
    parser.add_argument("--login-name", default="", help="login name for --set-user-password")
    parser.add_argument("--password-env", default="OWQ_SET_PASSWORD", help="environment variable containing the password for --set-user-password")
    parser.add_argument("--record-market-sync-status", choices=["started", "succeeded", "failed"], help="record production market-sync script status, then exit")
    parser.add_argument("--market-sync-exit-code", type=int, default=None, help="exit code for --record-market-sync-status failed")
    parser.add_argument("--market-sync-message", default="", help="short detail for --record-market-sync-status")
    parser.add_argument(
        "--email-login-session-retention-days",
        type=int,
        default=None,
        help="override OWQ_EMAIL_LOGIN_SESSION_RETENTION_DAYS for --prune-email-login-sessions",
    )
    parser.add_argument("--send-test-email", metavar="EMAIL", help="send a transactional email diagnostic and exit")
    parser.add_argument("--email-subject", default="OurWorlds Quant 邮件发信诊断", help="subject for --send-test-email")
    parser.add_argument(
        "--backup-app-db",
        nargs="?",
        const="",
        default=None,
        help="backup the app SQLite database to an optional path and exit",
    )
    parser.add_argument("--verify-app-backup", metavar="PATH", help="verify an app SQLite backup and exit without touching the live DB")
    parser.add_argument(
        "--restore-app-backup",
        nargs=2,
        metavar=("BACKUP", "DEST"),
        help="restore an app backup into a target SQLite file and exit; refuses to overwrite unless --restore-overwrite is set",
    )
    parser.add_argument("--restore-overwrite", action="store_true", help="allow --restore-app-backup to overwrite an existing target file")
    args = parser.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    if args.host == "127.0.0.1" and os.getenv("OWQ_HOST"):
        args.host = os.getenv("OWQ_HOST", args.host)
    if args.port == 8081 and os.getenv("OWQ_PORT"):
        try:
            args.port = int(os.getenv("OWQ_PORT", str(args.port)))
        except ValueError:
            pass
    if args.db is None and os.getenv("OWQ_APP_DB"):
        args.db = os.getenv("OWQ_APP_DB")

    if args.generate_demo_voice is not None:
        try:
            path = generate_usage_demo_voice(args.generate_demo_voice or None, voice=args.demo_voice or None)
        except Exception as exc:  # noqa: BLE001 - CLI should print optional TTS setup failures clearly
            print(f"演示语音生成失败: {sanitize_diagnostic_message(exc)}")
            return 1
        print(f"演示语音已生成: {path}")
        return 0

    if args.verify_app_backup:
        try:
            result = db.verify_backup_file(args.verify_app_backup)
        except Exception as exc:  # noqa: BLE001 - CLI should print restore-readiness failures cleanly
            print(f"备份校验失败: {type(exc).__name__}: {exc}")
            return 1
        counts = result["row_counts"]
        print(
            "备份校验通过: "
            f"{result['path']} "
            f"quick_check={result['quick_check']} "
            f"size={result['size_bytes']} "
            f"users={counts.get('users', 0)} "
            f"accounts={counts.get('accounts', 0)} "
            f"orders={counts.get('orders', 0)} "
            f"market_prices={counts.get('market_prices', 0)}"
        )
        return 0

    if args.restore_app_backup:
        backup_path, dest_path = args.restore_app_backup
        try:
            result = db.restore_backup_file(backup_path, dest_path, overwrite=args.restore_overwrite)
        except Exception as exc:  # noqa: BLE001 - restore CLI should fail cleanly
            print(f"备份恢复失败: {type(exc).__name__}: {exc}")
            return 1
        counts = result["row_counts"]
        print(
            "备份恢复完成: "
            f"{result['path']} "
            f"quick_check={result['quick_check']} "
            f"users={counts.get('users', 0)} "
            f"accounts={counts.get('accounts', 0)} "
            f"orders={counts.get('orders', 0)} "
            f"market_prices={counts.get('market_prices', 0)}"
        )
        return 0

    con = db.bootstrap(args.db)
    if args.market_csv:
        n = data_bridge.sync_market_from_csv(con, args.market_csv, replace=args.replace_market)
        services.record_audit_event(
            con,
            None,
            "cli.market_csv_sync",
            target_type="market_prices",
            detail={"rows": n, "path": args.market_csv, "replace": args.replace_market},
        )
        print(f"Imported {n} market rows from {args.market_csv}")
    if args.sync_market:
        try:
            include_codes = data_bridge.codes_from_csv(args.market_include_codes_csv) if args.market_include_codes_csv else []
            n = data_bridge.sync_market_from_quant_db(
                con,
                adjust=args.market_adjust,
                limit=args.market_limit,
                replace=args.replace_market,
                include_codes=include_codes,
            )
            services.record_audit_event(
                con,
                None,
                "cli.market_duckdb_sync",
                target_type="market_prices",
                detail={
                    "rows": n,
                    "adjust": args.market_adjust,
                    "limit": args.market_limit,
                    "replace": args.replace_market,
                    "priority_codes": len(include_codes),
                },
            )
            print(f"Synced {n} market rows from src.data DuckDB")
        except data_bridge.MarketSyncError as exc:
            print(f"Market sync skipped: {exc}")
    if args.sync_only:
        con.close()
        return 0
    if args.doctor or args.doctor_strict:
        doctor.print_report(con)
        status = doctor.health(con, strict=args.doctor_strict)
        con.close()
        return 0 if status["ok"] else 1
    if args.backup_app_db is not None:
        path = db.backup_database(con, args.backup_app_db or None)
        services.record_audit_event(con, None, "cli.backup", target_type="app_db", target_id=path.name, detail={"file": path.name})
        print(f"应用数据库备份已写入: {path}")
        con.close()
        return 0
    if args.sqlite_maintenance:
        services.record_audit_event(con, None, "cli.sqlite_maintenance", target_type="app_db")
        result = db.sqlite_maintenance(con)
        print(
            "SQLite 维护完成: "
            f"checkpoint={result['checkpoint']} "
            f"wal_before={result['wal_before_bytes']} "
            f"wal_after={result['wal_after_bytes']} "
            f"db={result['db_path']}"
        )
        con.close()
        return 0
    if args.prune_audit_log:
        try:
            result = services.prune_audit_events(con, days=args.audit_retention_days)
        except ValueError as exc:
            print(f"审计日志清理失败: {exc}")
            con.close()
            return 1
        services.record_audit_event(con, None, "cli.audit_prune", target_type="audit_events", detail=result)
        print(
            "审计日志清理完成: "
            f"deleted={result['deleted']} "
            f"remaining={result['remaining']} "
            f"retention_days={result['retention_days']} "
            f"cutoff={result['cutoff']}"
        )
        con.close()
        return 0
    if args.prune_email_login_sessions:
        try:
            result = services.prune_email_login_sessions(con, days=args.email_login_session_retention_days)
        except ValueError as exc:
            print(f"邮箱登录临时会话清理失败: {exc}")
            con.close()
            return 1
        services.record_audit_event(con, None, "cli.email_login_prune", target_type="email_login_sessions", detail=result)
        print(
            "邮箱登录临时会话清理完成: "
            f"expired={result['expired']} "
            f"deleted={result['deleted']} "
            f"remaining={result['remaining']} "
            f"retention_days={result['retention_days']} "
            f"cutoff={result['cutoff']}"
        )
        con.close()
        return 0
    if args.remove_demo_contest_participants:
        result = services.remove_demo_contest_participants(con)
        services.record_audit_event(con, None, "cli.demo_contest_clean", target_type="contest_participants", detail=result)
        print(
            "演示/开发参赛账户清理完成: "
            f"removed={result['participants_removed']} "
            f"user_ids={result['user_ids'] or '-'}"
        )
        con.close()
        return 0
    if args.set_user_password is not None:
        password = os.getenv(args.password_env, "")
        if not password:
            print(f"用户密码设置失败: 环境变量 {args.password_env} 为空")
            con.close()
            return 1
        if not args.login_name.strip():
            print("用户密码设置失败: 缺少 --login-name")
            con.close()
            return 1
        try:
            services.set_user_password(con, int(args.set_user_password), args.login_name, password, update_nickname=False)
        except ValueError as exc:
            print(f"用户密码设置失败: {exc}")
            con.close()
            return 1
        services.record_audit_event(
            con,
            None,
            "cli.user_password_set",
            target_type="user",
            target_id=int(args.set_user_password),
            detail={"login_name": services.normalize_login_name(args.login_name), "password_env": args.password_env},
        )
        print(f"用户密码已更新: user_id={int(args.set_user_password)} login_name={services.normalize_login_name(args.login_name)}")
        con.close()
        return 0
    if args.record_market_sync_status:
        action = {
            "started": "cli.market_sync_started",
            "succeeded": "cli.market_sync_succeeded",
            "failed": "cli.market_sync_failed",
        }[args.record_market_sync_status]
        detail = {
            "status": args.record_market_sync_status,
            "source": os.getenv("OWQ_MARKET_SOURCE", ""),
            "limit": os.getenv("OWQ_MARKET_LIMIT", ""),
            "sync_data_first": os.getenv("OWQ_SYNC_DATA_FIRST", ""),
            "sync_reports": os.getenv("OWQ_SYNC_REPORTS", ""),
        }
        if args.market_sync_exit_code is not None:
            detail["exit_code"] = args.market_sync_exit_code
        if args.market_sync_message.strip():
            detail["message"] = args.market_sync_message.strip()[:180]
        services.record_audit_event(con, None, action, target_type="market_sync", target_id="public", detail=detail)
        print(f"市场同步状态已记录: {args.record_market_sync_status}")
        con.close()
        return 0
    if args.send_test_email:
        subject = args.email_subject.strip() or "OurWorlds Quant 邮件发信诊断"
        body = (
            "这是一封 OurWorlds Quant 邮件发信诊断邮件。\n\n"
            "如果你收到这封邮件,说明当前环境的事务邮件发送链路可用。"
        )
        html = (
            "<p>这是一封 OurWorlds Quant 邮件发信诊断邮件。</p>"
            "<p>如果你收到这封邮件,说明当前环境的事务邮件发送链路可用。</p>"
        )
        handler = object.__new__(AppHandler)
        email_hash = ""
        email_detail: dict[str, str] = {}
        try:
            email = services.normalize_email(args.send_test_email)
            email_hash, email_detail = email_audit_metadata(email)
            provider = handler.send_transactional_email(email, subject, body, html)
        except Exception as exc:  # noqa: BLE001 - CLI should report provider diagnostics cleanly
            detail = exception_diagnostic(exc)
            detail.update(email_detail)
            services.record_audit_event(
                con,
                None,
                "cli.email_test_failed",
                target_type="email",
                target_id=email_hash,
                detail=detail,
            )
            print(f"测试邮件发送失败: {detail['error']}: {detail['message']}")
            con.close()
            return 1
        services.record_audit_event(con, None, "cli.email_test", target_type="email", target_id=email_hash, detail={"provider": provider, **email_detail})
        print(f"测试邮件已通过 {provider} 发送到 {email}")
        con.close()
        return 0
    AppHandler.con = con
    # Serve with one SQLite connection per request (thread-per-request server). The
    # bootstrap connection above has already created/migrated the schema; each worker
    # opens its own connection to this path so transactions never interleave.
    AppHandler.db_path = args.db if args.db is not None else db.DEFAULT_DB_PATH
    httpd = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Serving OurWorlds Quant app at http://{args.host}:{args.port}")
    signal_handlers = install_shutdown_signal_handlers()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        restore_signal_handlers(signal_handlers)
        httpd.server_close()
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

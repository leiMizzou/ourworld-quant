#!/bin/bash
# 双击即启动:把 docs/ 作为静态站发布在 http://localhost:8088
# 逻辑:Docker 真在运行就用容器(后台常驻);否则自动退回 python3(前台,需保持窗口)。
cd "$(dirname "$0")"
DOCS="$(cd ../docs && pwd)"
PORT=8088

echo "=============================================="
echo " OurWorlds Quant Lab — 启动本地静态站"
echo " 目录: $DOCS"
echo " 地址: http://localhost:$PORT"
echo "=============================================="
echo ""

start_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "==> 用 python3 启动(请保持本窗口开启,关闭=停止服务;Ctrl+C 也可停止)..."
    exec python3 -m http.server "$PORT" --directory "$DOCS"
  else
    echo "!! 未找到 python3。"
    return 1
  fi
}

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "==> 检测到 Docker 正在运行,用 caddy 容器启动..."
  if docker compose up -d; then
    echo "==> 已启动(容器 ourworld-quant-site,后台常驻)。可关闭本窗口。"
  else
    echo "!! docker compose 启动失败,改用 python3..."
    start_python || true
  fi
else
  echo "==> 未检测到正在运行的 Docker,改用 python3..."
  start_python || echo "!! 既无 Docker 也无 python3,无法启动。请先启动 Docker Desktop 或安装 python3。"
fi

echo ""
read -p "按回车键关闭此窗口..."

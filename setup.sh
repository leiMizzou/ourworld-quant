#!/usr/bin/env bash
# 一键把仓库里的占位符替换成你的真实值。
# 用法:
#   ./setup.sh <你的域名> [二级域名前缀] <GitHub用户名> ["署名"]
# 例:
#   ./setup.sh ourworlds.app quant myname "Lei"
#   -> 站点域名 quant.ourworlds.app, GitHub 用户 myname
set -euo pipefail

DOMAIN="${1:?请提供主域名,如 ourworlds.app}"
SUB="${2:-quant}"
GH="${3:?请提供 GitHub 用户名}"
NAME="${4:-OurWorlds Quant Lab}"
FQDN="${SUB}.${DOMAIN}"

echo "将替换为 → 站点: ${FQDN} | GitHub: ${GH} | 署名: ${NAME}"

# 找出含占位符的文件并替换(排除 .git)
grep -rlZ --exclude-dir=.git -e 'YOURDOMAIN\.com' -e 'leiMizzou' -e 'Lei' . \
| while IFS= read -r -d '' f; do
    sed -i.bak \
      -e "s#quant\.YOURDOMAIN\.com#${FQDN}#g" \
      -e "s#YOURDOMAIN\.com#${DOMAIN}#g" \
      -e "s#leiMizzou#${GH}#g" \
      -e "s#Lei#${NAME}#g" \
      "$f"
    rm -f "$f.bak"
    echo "  ✓ $f"
done

echo ""
echo "占位符替换完成。接下来初始化 git 并推送:"
cat <<EOF

  git init
  git add .
  git commit -m "chore: initial commit — OurWorlds Quant Lab"
  git branch -M main
  # 在 GitHub 新建空仓库 ourworld-quant 后:
  git remote add origin git@github.com:${GH}/ourworld-quant.git
  git push -u origin main

EOF

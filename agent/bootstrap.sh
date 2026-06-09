#!/bin/bash
# 方式二：无 MDM / 无 root，per-user 一键安装（curl | bash）。
# 适合没有飞连/MDM 的企业：员工执行一次，之后静默后台跑，零感知；身份自动用 git email。
#
# 用法（IT 把文件托管到内网某 BASE_URL 后，发这一行给员工）：
#   curl -fsSL https://intranet/tok/bootstrap.sh | \
#     COLLECTOR_URL=https://collector.example.com COLLECTOR_TOKEN=xxx \
#     BASE_URL=https://intranet/tok bash
#
# 可选环境变量：COLLECTORS(默认 claude_code，免二进制) / EMAIL_DOMAIN / EMPLOYEE_EMAIL
set -euo pipefail

: "${COLLECTOR_URL:?need COLLECTOR_URL}"
: "${COLLECTOR_TOKEN:?need COLLECTOR_TOKEN}"
: "${BASE_URL:?need BASE_URL (where agent files are hosted)}"
COLLECTORS="${COLLECTORS:-tokscale}"   # 默认 tokscale，一把覆盖 25+ 工具

LIB="$HOME/.local/share/tokreport"
CONF="$HOME/.config/tokreport.conf"
PLIST="$HOME/Library/LaunchAgents/com.example.tokreport.plist"
UID_NUM=$(id -u)

mkdir -p "$LIB/collectors" "$(dirname "$CONF")" "$(dirname "$PLIST")"

echo "downloading agent from $BASE_URL ..."
curl -fsSL "$BASE_URL/tokreport.py" -o "$LIB/tokreport.py"
curl -fsSL "$BASE_URL/identity.py"  -o "$LIB/identity.py"
for f in __init__ base tokscale_collector claude_code_collector; do
  curl -fsSL "$BASE_URL/collectors/$f.py" -o "$LIB/collectors/$f.py"
done
# 默认 claude_code 免二进制；若用 tokscale 采集源，再拉二进制
if [[ "$COLLECTORS" == *tokscale* ]]; then
  curl -fsSL "$BASE_URL/tokscale" -o "$LIB/tokscale" && chmod +x "$LIB/tokscale"
  xattr -dr com.apple.quarantine "$LIB/tokscale" 2>/dev/null || true  # 过 Gatekeeper
fi

cat > "$CONF" <<EOF
COLLECTOR_URL=$COLLECTOR_URL
COLLECTOR_TOKEN=$COLLECTOR_TOKEN
COLLECTORS=$COLLECTORS
TOKSCALE_BIN=$LIB/tokscale
EMPLOYEE_EMAIL=${EMPLOYEE_EMAIL:-}
EMAIL_DOMAIN=${EMAIL_DOMAIN:-}
LOOKBACK_DAYS=3
EOF
chmod 0600 "$CONF"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.tokreport</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$LIB/tokreport.py</string></array>
  <key>EnvironmentVariables</key><dict><key>TOKREPORT_CONF</key><string>$CONF</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- 每隔 3600s 跑一次；以加载时刻为锚，天然错峰 -->
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>/tmp/tokreport.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/tokreport.err.log</string>
</dict></plist>
EOF

launchctl bootout   "gui/$UID_NUM" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl kickstart -k "gui/$UID_NUM/com.example.tokreport"
echo "tokreport installed for $(id -un). Identity auto-resolved; reporting daily."

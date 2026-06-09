#!/bin/bash
# 单机手动安装（调试/单台用）。批量上线请用 mdm_bootstrap.sh 经飞连 MDM 下发（幂等）。
# 装的是 tokscale 采集 shell 脚本（打 /v1/tokscale/report，与线上 collector 对齐），
# 以 LaunchAgent 形式跑在登录用户域（每天 19:00 + 加载即跑）。
# 用法: sudo ./install.sh [下发包目录]   （默认取脚本同目录）
set -euo pipefail

PKG_DIR="${1:-$(dirname "$0")}"
LIB_DIR="/usr/local/lib/tokreport"
LABEL="com.example.tokreport"

CONSOLE_USER=$(stat -f%Su /dev/console)
UID_NUM=$(id -u "$CONSOLE_USER")
USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')
AGENTS_DIR="$USER_HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"

echo "installing for user=$CONSOLE_USER uid=$UID_NUM"

# 1) 采集脚本（单一真源；也可直接 curl collector 的 /tokreport.sh 覆盖）
install -d "$LIB_DIR"
install -m 0755 "$PKG_DIR/remote_tokscale_report.sh" "$LIB_DIR/tokreport.sh"
# 可选：随包附带 tokscale 二进制则一并装上，去隔离属性避免 Gatekeeper 拦截
[ -f "$PKG_DIR/tokscale" ] && {
  install -m 0755 "$PKG_DIR/tokscale" /usr/local/bin/tokscale
  xattr -dr com.apple.quarantine /usr/local/bin/tokscale 2>/dev/null || true
} || true

# 2) LaunchAgent（用户域才能读到该用户的 ~/.claude、~/.codex）
install -d -o "$CONSOLE_USER" "$AGENTS_DIR"
install -m 0644 -o "$CONSOLE_USER" "$PKG_DIR/com.example.tokreport.plist" "$PLIST"

launchctl bootout   "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl kickstart -k "gui/$UID_NUM/$LABEL"
echo "tokreport installed and started (LaunchAgent → $LIB_DIR/tokreport.sh)."

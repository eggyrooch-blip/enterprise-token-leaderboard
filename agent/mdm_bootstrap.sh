#!/bin/bash
# ============================================================
# tokreport 幂等自举（飞连 MDM「执行脚本」下发用，root 执行）
# ------------------------------------------------------------
# 一次下发即可：把 tokscale 采集脚本装成 LaunchAgent，之后由本机 launchd
# 每天自动采集上报，飞连无需再次触发。
#
# 幂等（解决「重复下发别重建定时任务、别给员工机带来无谓消耗」）：
#   已是当前版本且 LaunchAgent 已加载  → 秒退，不重写、不重载、不跑采集
#   版本升级 / 任务缺失 / 脚本丢失      → 才真正安装或更新
#
# 员工无感：纯后台，无弹窗/通知；读的是用户自己家目录日志，不触发隐私授权。
# 始终 exit 0：不污染 MDM 批量执行结果。
# ============================================================
set -u

VERSION=4                                  # 改采集逻辑/计划时 +1，触发员工机平滑更新
LABEL="com.example.tokreport"
COLLECTOR="${COLLECTOR:-https://collector.example.com}"  # collector endpoint
LIB="${TOKREPORT_LIB:-/usr/local/lib/tokreport}"   # 可覆盖(测试用);生产默认不变
SCRIPT="$LIB/tokreport.sh"
VFILE="$LIB/.version"

log() { echo "[tokreport-bootstrap] $*"; }

# ── COLLECTOR 未配置（仍是脱敏占位）→ 直接拒绝，避免「版本涨了但下载失败」的静默坑 ──
case "$COLLECTOR" in
  *example.com*|"") log "COLLECTOR 未配置($COLLECTOR);请在飞连任务里设 COLLECTOR=https://<真实域名>"; exit 0;;
esac

# ── 必须有真实登录用户（LaunchAgent 要装进其 GUI 域）。登录窗口/无人登录时跳过 ──
CONSOLE_USER=$(stat -f%Su /dev/console 2>/dev/null)
if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ] || [ "$CONSOLE_USER" = "_mbsetupuser" ]; then
  log "no real console user (login window?), skip"; exit 0
fi
UID_NUM=$(id -u "$CONSOLE_USER" 2>/dev/null) || { log "cannot resolve uid"; exit 0; }
USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
[ -z "$USER_HOME" ] && USER_HOME="/Users/$CONSOLE_USER"
PLIST="$USER_HOME/Library/LaunchAgents/$LABEL.plist"

is_loaded() { launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; }

# ── 幂等闸门：版本一致 + 脚本在 + 任务已加载 → 直接退出 ──
if [ -f "$VFILE" ] && [ "$(cat "$VFILE" 2>/dev/null)" = "$VERSION" ] \
     && [ -f "$SCRIPT" ] && is_loaded; then
  log "already v$VERSION and loaded for $CONSOLE_USER — no-op"
  exit 0
fi

log "installing/updating to v$VERSION for $CONSOLE_USER (uid=$UID_NUM)"

# ── 取最新采集脚本（单一真源：collector 暴露的 /tokreport.sh，已内置域名+token）──
# FRESH=1 仅当本次确实下载并校验通过、装上了新脚本；下载/校验失败保留旧脚本但 FRESH=0，
# 从而「不落新版本号」→ 下次下发自动重试，绝不出现「版本涨了却跑着旧脚本」的假成功。
FRESH=0
mkdir -p "$LIB"
TMP="$(mktemp "$LIB/.dl.XXXXXX" 2>/dev/null || echo "$LIB/.dl.tmp")"
if curl -fsSL --connect-timeout 5 --max-time 30 "$COLLECTOR/tokreport.sh" -o "$TMP" \
     && head -1 "$TMP" | grep -q '^#!' && grep -q "v1/tokscale/report" "$TMP"; then
  mv -f "$TMP" "$SCRIPT"; chmod 755 "$SCRIPT"; FRESH=1
else
  rm -f "$TMP"
  log "download/validate $COLLECTOR/tokreport.sh failed"
  [ -f "$SCRIPT" ] || { log "no local script to fall back on; abort"; exit 0; }
  log "keep existing script; will retry version bump on next push"
fi

# ── LaunchAgent plist（每隔 1 小时 + 加载即跑一次）──
install -d -o "$CONSOLE_USER" "$(dirname "$PLIST")" 2>/dev/null || mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>/bin/bash</string><string>$SCRIPT</string></array>
    <key>RunAtLoad</key><true/>
    <!-- 每隔 3600s 跑一次;以各机加载时刻为锚,天然错峰,不会全员整点齐打 collector -->
    <key>StartInterval</key><integer>3600</integer>
    <!-- 后台低优先级:CPU/IO 让路给用户前台,尽量零感知 -->
    <key>ProcessType</key><string>Background</string>
    <key>LowPriorityIO</key><true/>
    <key>Nice</key><integer>10</integer>
    <key>StandardOutPath</key><string>/tmp/tokreport.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/tokreport.err.log</string>
</dict>
</plist>
PLIST_EOF
chown "$CONSOLE_USER" "$PLIST" 2>/dev/null || true

# ── 重新加载（先卸再装，保证用新 plist）+ 立即跑一次让数据当天出现 ──
launchctl bootout   "gui/$UID_NUM/$LABEL" 2>/dev/null || true
if launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
  launchctl kickstart "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  # 只有「确实装上了新脚本」才落版本号；保留旧脚本时不落 → 下次下发会重试
  if [ "$FRESH" = 1 ]; then
    echo "$VERSION" > "$VFILE"
    log "installed v$VERSION (fresh script); LaunchAgent active for $CONSOLE_USER"
  else
    log "LaunchAgent active for $CONSOLE_USER, but script not refreshed (version NOT bumped)"
  fi
else
  log "bootstrap failed; will retry on next push"
fi

exit 0

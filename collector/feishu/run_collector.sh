#!/bin/bash
# 每日采集主流程(launchd 调用):
#   1) 确保带调试端口的自动化 Chrome(拷贝 profile,headless)在跑——没有就拉起
#   2) 跑采集器(CDP 连上去抓 → 归一化 → 上报看板)
#   3) 登录态失效(退出码 3)→ 飞书机器人告警,请你 refresh_profile 重登
# 配置从 ~/.feishu/collector.env 读(COLLECTOR_URL/COLLECTOR_TOKEN/FEILIAN_*/LARK_ALERT_WEBHOOK 等)。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="$HOME/.feishu/collector.log"
mkdir -p "$HOME/.feishu"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UDD="$HOME/.feishu/auto_udd"
PORT="${FEISHU_PORT:-9223}"
PY="${FEISHU_PY:-$HERE/../../.venv-feishu/bin/python}"
PROFILE="$(cat "$UDD/.profile_name" 2>/dev/null || echo 'Profile 1')"

[ -f "$HOME/.feishu/collector.env" ] && set -a && . "$HOME/.feishu/collector.env" && set +a
ts() { date '+%Y-%m-%d %H:%M:%S'; }
echo "[$(ts)] === run start ===" >> "$LOG"

# 1) 确保 Chrome 调试端口
if ! curl -s --max-time 3 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  if [ ! -d "$UDD" ]; then
    echo "[$(ts)] auto_udd 不存在,先 refresh_profile" >> "$LOG"
    "$HERE/refresh_profile.sh" >> "$LOG" 2>&1 || { echo "[$(ts)] refresh 失败" >> "$LOG"; exit 1; }
  fi
  echo "[$(ts)] 拉起 headless Chrome (profile=$PROFILE port=$PORT)" >> "$LOG"
  "$CHROME" --user-data-dir="$UDD" --profile-directory="$PROFILE" \
    --remote-debugging-port="$PORT" --headless=new --no-first-run \
    --no-default-browser-check >/tmp/feishu_chrome.log 2>&1 &
  sleep 6
fi

# 2) 跑采集器
FEISHU_CDP="http://127.0.0.1:$PORT" "$PY" "$HERE/feishu_collector.py" >> "$LOG" 2>&1
rc=$?
echo "[$(ts)] collector exit=$rc" >> "$LOG"

# 3) 失效告警
if [ "$rc" -eq 3 ]; then
  msg="飞书 AI 用量采集:登录态失效。请在 Mac 日常 Chrome 里确认飞书后台仍登录,然后跑 refresh_profile.sh 重导。"
  echo "[$(ts)] ALERT: $msg" >> "$LOG"
  if [ -n "${LARK_ALERT_WEBHOOK:-}" ]; then
    curl -s --max-time 8 -X POST "$LARK_ALERT_WEBHOOK" \
      -H 'Content-Type: application/json' \
      -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$msg\"}}" >> "$LOG" 2>&1 || true
  fi
  # 同时尝试 PAI 本地语音/通知(可选)
  curl -s --max-time 4 -X POST http://localhost:31337/notify \
    -H 'Content-Type: application/json' \
    -d "{\"message\":\"$msg\",\"voice_enabled\":false}" >/dev/null 2>&1 || true
fi
echo "[$(ts)] === run end (rc=$rc) ===" >> "$LOG"
exit "$rc"

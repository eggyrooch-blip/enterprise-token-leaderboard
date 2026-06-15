#!/bin/bash
# 每日采集主流程(launchd 调用):
#   1) 拉起带调试端口的自动化 Chrome(拷贝 profile,headless)——本轮专用,用完即关
#   2) 跑采集器(CDP 连上去抓 → 归一化 → 上报看板)
#   3) 登录态失效(退出码 3)→ 飞书机器人告警,请你 refresh_profile 重登
# 注意:自动化 Chrome 与日常 Chrome 共用同一个 Google Chrome.app —— 若让它常驻,
# macOS LaunchServices 会认为"Chrome 已在运行",把孙可日常的启动吞掉(打不开)。
# 因此本脚本绝不让它残留:启动前清旧、EXIT 时必杀,且只按 auto_udd 作用域匹配,不碰日常 Chrome。
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

# 只杀本自动化的 headless Chrome(按 auto_udd 用户目录精确匹配)。
# 日常 Chrome 不带 --user-data-dir=auto_udd,绝不会命中 —— 这是不误伤的关键。
cleanup_chrome() { pkill -f "user-data-dir=$UDD" >/dev/null 2>&1 || true; }
# 任何退出路径(正常 exit / 被 launchd SIGTERM / Ctrl-C)都必杀,绝不残留堵塞日常 Chrome。
trap cleanup_chrome EXIT INT TERM

# 1) 拉起本轮专用 headless Chrome —— 先清掉上一轮异常崩溃可能留下的残留
cleanup_chrome
if [ ! -d "$UDD" ]; then
  echo "[$(ts)] auto_udd 不存在,先 refresh_profile" >> "$LOG"
  "$HERE/refresh_profile.sh" >> "$LOG" 2>&1 || { echo "[$(ts)] refresh 失败" >> "$LOG"; exit 1; }
fi
echo "[$(ts)] 拉起 headless Chrome (profile=$PROFILE port=$PORT) —— 本轮用完即关" >> "$LOG"
"$CHROME" --user-data-dir="$UDD" --profile-directory="$PROFILE" \
  --remote-debugging-port="$PORT" --headless=new --no-first-run \
  --no-default-browser-check >/tmp/feishu_chrome.log 2>&1 &
# 等 CDP 端口就绪(最多 ~20s),就绪即继续
for _i in $(seq 1 20); do
  curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  sleep 1
done

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

#!/bin/bash
# 一键部署(在你 Mac 上跑一次):
#   1) 建 venv + 装 playwright + 下 chromium
#   2) 拷贝你登录了飞书的 Chrome profile → 自动化专用目录
#   3) 生成 collector.env(需你填 COLLECTOR_URL/COLLECTOR_TOKEN)
#   4) 生成并加载 launchd 定时任务(每天 08:30 跑一次)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"          # worktree / repo 根
VENV="$ROOT/.venv-feishu"
ENVF="$HOME/.feishu/collector.env"
PLIST="$HOME/Library/LaunchAgents/com.example.feishu-collector.plist"
mkdir -p "$HOME/.feishu" "$HOME/Library/LaunchAgents"

echo "1/4 venv + playwright + chromium…"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install --upgrade pip >/dev/null
"$VENV/bin/pip" -q install playwright >/dev/null
"$VENV/bin/python" -m playwright install chromium >/dev/null
echo "   ok"

echo "2/4 拷贝飞书登录 profile…"
bash "$HERE/refresh_profile.sh"

echo "3/4 配置文件 $ENVF"
if [ ! -f "$ENVF" ]; then
  cp "$HERE/collector.env.example" "$ENVF"
  echo "   已生成模板,请编辑填 COLLECTOR_URL / COLLECTOR_TOKEN(看板地址与 Bearer)"
else
  echo "   已存在,跳过"
fi

echo "4/4 生成 launchd 定时(每天 08:30)…"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.feishu-collector</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$HERE/run_collector.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$HOME/.feishu/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/.feishu/launchd.err.log</string>
</dict></plist>
PLIST
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" && echo "   已加载: com.example.feishu-collector"

echo
echo "✅ 部署完成。手动试跑一次:  bash $HERE/run_collector.sh"
echo "   日志:  tail -f $HOME/.feishu/collector.log"
echo "   登录态失效时:  bash $HERE/refresh_profile.sh"

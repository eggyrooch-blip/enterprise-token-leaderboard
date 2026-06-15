#!/bin/bash
# 回归测试:run_collector.sh 必须"用完即关"自己拉起的 headless Chrome,
# 且只按 auto_udd 作用域匹配 —— 绝不误杀孙可日常的 Google Chrome。
#
# 背景(根因):自动化 Chrome 与日常 Chrome 共用同一个 Google Chrome.app。
# 旧脚本让 headless Chrome 常驻复用,macOS LaunchServices 因此认为
# "Chrome 已在运行",把日常启动吞掉 → 用户"打不开 Chrome"。
#
# 本测试在【未修复】的旧脚本上必然失败(无 trap / 无 cleanup_chrome),
# 在【已修复】脚本上通过。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/run_collector.sh"
fail=0

# --- 1) 结构:必须装 EXIT trap 且有作用域清理函数(旧脚本没有 → 失败) ---
grep -Eq 'trap[[:space:]]+cleanup_chrome[[:space:]]+EXIT' "$SCRIPT" \
  || { echo "FAIL: 缺少 'trap cleanup_chrome EXIT'(headless Chrome 会残留堵塞日常 Chrome)"; fail=1; }
grep -Eq 'cleanup_chrome\(\)' "$SCRIPT" \
  || { echo "FAIL: 缺少 cleanup_chrome 清理函数"; fail=1; }
grep -q 'user-data-dir=$UDD' "$SCRIPT" \
  || { echo "FAIL: 清理未按 \$UDD(auto_udd)作用域匹配,可能误伤日常 Chrome"; fail=1; }

# --- 2) 行为:清理逻辑必须杀掉 auto_udd 进程,但放过日常 Chrome ---
UDD="/tmp/test_auto_udd_$$"
cleanup_chrome() { pkill -f "user-data-dir=$UDD" >/dev/null 2>&1 || true; }

# 模拟自动化 headless Chrome(命令行带 auto_udd 标记)。
# 用 'sleep 30; true' 两条语句,阻止 sh 的 exec 优化丢掉 argv 标记(真实 Chrome argv 必带该标记)。
sh -c 'sleep 30; true' "chrome --user-data-dir=$UDD --headless=new" &
victim=$!
# 模拟日常 Chrome(不带 --user-data-dir)
sh -c 'sleep 30; true' "Google Chrome --profile-directory=Default" &
bystander=$!
sleep 0.5

cleanup_chrome
sleep 0.5

if kill -0 "$victim" 2>/dev/null; then
  echo "FAIL: 自动化 Chrome 未被清理(仍残留)"; fail=1; kill "$victim" 2>/dev/null || true
else
  echo "ok: 自动化 headless Chrome 已被清理"
fi
if kill -0 "$bystander" 2>/dev/null; then
  echo "ok: 日常 Chrome 安然无恙(未被误杀)"; kill "$bystander" 2>/dev/null || true
else
  echo "FAIL: 日常 Chrome 被误杀了!作用域匹配有问题"; fail=1
fi

if [ "$fail" -eq 0 ]; then echo "PASS: run_collector.sh 用完即关 + 作用域安全"; fi
exit "$fail"

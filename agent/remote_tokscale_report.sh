#!/bin/bash
# ============================================================
# AI 编程 Token 用量采集脚本 (macOS)
# 适用于：飞连 MDM「执行脚本」远程批量下发
# 结果上报：内网 collector（按序列号经飞连反解身份，机器侧零配置）
#
# 采集范围：tokscale 覆盖的全部 AI 编程工具
#   Claude Code / Codex CLI / Cursor / Gemini CLI / Kimi / OpenCode ...
#
# 安全保证（参考 openclaw 检测脚本的工程实践）：
#   - 纯只读：只读 token 计数/成本，绝不读取/上传 prompt 或代码内容
#   - 整体超时自杀：最多跑 SCRIPT_TIMEOUT 秒（当前 600），超时自动退出，绝不拖死 MDM 批量执行链
#   - 逐命令超时 + 2>/dev/null 容错：单条命令卡住不影响整体
#   - 始终 exit 0：不污染 MDM 执行结果
# ============================================================

# ── 整体超时保护（防止 npx/tokscale 卡住拖死 MDM/SSH 连接）──────────────────
# 关键：后台看门狗的 FD 全部重定向到 /dev/null，否则残留进程会让 MDM 连接挂起
SCRIPT_TIMEOUT=600
( sleep "$SCRIPT_TIMEOUT" && kill -9 $$ 2>/dev/null ) </dev/null >/dev/null 2>&1 &
WATCHDOG_PID=$!
trap 'kill -9 "$WATCHDOG_PID" 2>/dev/null' EXIT

# ── macOS 兼容的单命令超时（macOS 无 GNU timeout，用 perl alarm 实现）────────
# 用法：tmout 秒数 命令 参数...（仅单条命令，不支持管道）
tmout() { perl -e 'alarm shift; exec @ARGV' "$@" 2>/dev/null; }

# ── 配置 ────────────────────────────────────────────────────────────────────
COLLECTOR="${COLLECTOR:-https://collector.example.com}"   # collector endpoint
TOKEN="${TOKEN:-}"    # collector 鉴权 token，由 MDM/环境变量注入；不要写进脚本
# 上报来源标记:mdm=飞连 MDM 自动下发(默认) / manual=员工自己终端补跑。
# 手工补报命令显式传第一个参数 'manual',收集端据此打「手工」角标 + 记审计行。
VIA="${1:-mdm}"
if [ -z "$TOKEN" ]; then
  echo "tokreport SKIP: TOKEN is required"
  exit 0
fi

# ── 健壮的设备信息采集（多级兜底，任一步失败不影响整体）─────────────────────
SERIAL=$(/usr/sbin/ioreg -c IOPlatformExpertDevice -d 2 2>/dev/null | awk -F\" '/IOPlatformSerialNumber/{print $(NF-1)}')
HOSTNAME=$(/usr/sbin/scutil --get ComputerName 2>/dev/null || hostname 2>/dev/null || echo "unknown")
CONSOLE_USER=$(stat -f "%Su" /dev/console 2>/dev/null)
[ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ] && CONSOLE_USER=$(ls -l /dev/console 2>/dev/null | awk '{print $3}')
[ -z "$CONSOLE_USER" ] && CONSOLE_USER="unknown"
OS_VERSION=$(sw_vers -productVersion 2>/dev/null || echo "unknown")
IP_ADDR=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || ifconfig 2>/dev/null | grep 'inet ' | grep -v 127.0.0.1 | head -1 | awk '{print $2}' || echo "unknown")

# 以登录用户身份执行（tokscale 要读登录用户的本地日志），整体限时。
# 上下文自适应：root 跑（MDM 一次性下发 / LaunchDaemon）→ sudo 切到登录用户；
# 已是普通用户跑（LaunchAgent 用户态）→ 直接跑，不再 sudo（否则会卡密码提示）。
run_as_user() {
  if [ "$(id -u)" -eq 0 ] && [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ] \
       && [ "$CONSOLE_USER" != "unknown" ]; then
    tmout 120 sudo -u "$CONSOLE_USER" -H /bin/bash -lc "$1"
  else
    tmout 120 /bin/bash -lc "$1"
  fi
}

# 身份完全交给 collector（按序列号→飞连反解）。不再用 git 邮箱兜底：
# 一是本企业有飞连/MDM，序列号反解已足够；二是裸调 git 会在没装 Xcode 命令行工具的
# 员工机上弹“git 命令需要命令行开发者工具，是否安装”对话框，定时任务反复骚扰。
EMAIL=""

# tokscale 调用前缀：补 PATH；装了就用本地二进制,没装则【直接用 npx 运行】。
# 注意:不能只 `npx ... --version` 再裸跑 tokscale —— 那只下到缓存,tokscale 不进 PATH,
# 会导致没装 tokscale 的机器最终命令找不到、上报 0 条。必须让 npx 直接执行子命令。
TOKSCALE_CMD='export PATH="$HOME/.bun/bin:$HOME/.npm-global/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; if command -v tokscale >/dev/null 2>&1; then TS=tokscale; elif command -v npx >/dev/null 2>&1; then TS="npx -y tokscale@latest"; elif command -v bunx >/dev/null 2>&1; then TS="bunx tokscale@latest"; else TS=""; fi; [ -z "$TS" ] && exit 0; $TS'

# 取数临时目录(tokscale 输出落文件,不走管道)。
TMPD="$(mktemp -d 2>/dev/null || echo "/tmp/tokreport.$$")"
mkdir -p "$TMPD"
trap 'kill -9 "$WATCHDOG_PID" 2>/dev/null; rm -rf "$TMPD" 2>/dev/null' EXIT

# 容错取数：tokscale 输出【重定向到文件】而非命令替换捕获 —— tokscale 是 Node/bun CLI,
# process.exit() 在 stdout 走管道($()/| )时会把没刷完的缓冲砍掉(实测 graph >64KB 正好截到
# 64KB → 残缺 JSON → collector json.loads 崩 → 502)。写文件不走管道,完整落盘;再用 sed 读文件
# (sed 是 C 程序,正常 flush,$() 读它不会触发该截断)。--no-spinner 必带;最多重试 3 次。
fetch_json() {  # $1=子命令(models|monthly) $2=输出文件
  local out="" _t
  for _t in 1 2 3; do
    run_as_user "$TOKSCALE_CMD $1 --json --no-spinner > '$2' 2>/dev/null"
    # 登录 shell 可能在 JSON 前打印 banner，从第一个 '{' 起截取，剔除污染
    out=$(sed -n '/{/,$p' "$2" 2>/dev/null)
    if [ -n "$out" ] && case "$out" in *'"entries"'*) true;; *) false;; esac; then
      printf '%s' "$out"
      return 0
    fi
    sleep 2
  done
  printf '{"entries":[]}'
}

# 取数串行(不并行):三条 tokscale 调用是扫同一批本地日志的 IO/CPU 密集型,并行只会互相抢
# IO/CPU 反而不快(实测 97s vs 串行 107s)。看门狗 SCRIPT_TIMEOUT 已 180→600s 留足余量。
# ── lifetime 快照 + 月度时间序列（两者都是可重复上报的幂等快照）───────────────
MODELS=$(fetch_json models "$TMPD/models")
MONTHLY=$(fetch_json monthly "$TMPD/monthly")

# ── 日粒度(tokscale graph)：支持按区间(1/2/3周)看榜。只取近 100 天,够覆盖 3 周区间。──
# 同样落文件再读 —— graph 体积最大(可达近百 KB),正是 Node 管道截断的重灾区。
SINCE=$(date -v-100d +%Y-%m-%d 2>/dev/null || date -d '100 days ago' +%Y-%m-%d 2>/dev/null)
GRAPH='{"contributions":[]}'
for _t in 1 2 3; do
  run_as_user "$TOKSCALE_CMD graph --since $SINCE > '$TMPD/graph' 2>/dev/null"
  _g=$(sed -n '/{/,$p' "$TMPD/graph" 2>/dev/null)
  if [ -n "$_g" ] && case "$_g" in *'"contributions"'*) true;; *) false;; esac; then GRAPH="$_g"; break; fi
  sleep 2
done

# ── 上报（带连接/总超时;校验返回）──────────────────────────────────────────
PAYLOAD=$(cat <<EOF
{"serial":"$SERIAL","email":"$EMAIL","hostname":"$HOSTNAME","os":"$OS_VERSION","ip":"$IP_ADDR","via":"$VIA","models":$MODELS,"monthly":$MONTHLY,"graph":$GRAPH}
EOF
)
RESP=$(curl -s --connect-timeout 5 --max-time 20 -X POST "$COLLECTOR/v1/tokscale/report" \
  -H "Content-Type: application/json" -H "Authorization: Bearer $TOKEN" \
  -d "$PAYLOAD" 2>/dev/null)

# 用 case 模式匹配判断返回，不走管道 —— 避免 grep 命中即退、管道提前关闭产生
# "printf: write error: Broken pipe" 的 stderr 噪声。
case "$RESP" in
  *'"ok"'*) echo "tokreport OK: serial=$SERIAL user=$CONSOLE_USER resp=$RESP" ;;
  *)        echo "tokreport SENT (resp unverified): serial=$SERIAL user=$CONSOLE_USER resp=$RESP" ;;
esac

# 始终正常退出，不影响 MDM 执行链
exit 0

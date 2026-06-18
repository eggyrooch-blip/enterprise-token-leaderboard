#!/usr/bin/env bash
# deploy.sh — 把 dev_collector 部署到远端 ELK 服务器
# 幂等：可反复执行。不启动服务（交由 systemd unit）。
#
# 用法：
#   bash deploy/deploy.sh                          # 使用默认参数
#   REMOTE_HOST=collector.example.com bash deploy/deploy.sh   # 覆盖主机
#
# 前提：
#   1. ssh 密钥免密已配置（it@REMOTE_HOST）
#   2. pipeline/.env 已在本地填好（含 FEILIAN_* + COLLECTOR_API_TOKENS）
#   3. 本机已安装 rsync（macOS 自带）

set -euo pipefail

# ── 参数（可通过环境变量覆盖） ──────────────────────────────────────
REMOTE_HOST="${REMOTE_HOST:-collector.example.com}"
REMOTE_USER="${REMOTE_USER:-it}"
REMOTE_DIR="${REMOTE_DIR:-/home/it/tokreport}"
PORT="${PORT:-8090}"
SERVICE_NAME="${SERVICE_NAME:-tokreport-collector}"
PYTHON="${PYTHON:-/usr/bin/python3}"
LOCAL_ENV="${LOCAL_ENV:-pipeline/.env}"   # 本地凭证文件路径（相对于 repo 根）
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no"

# ── 颜色 ────────────────────────────────────────────────────────────
GRN='\033[0;32m'; YEL='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GRN}[deploy]${NC} $*"; }
warn()  { echo -e "${YEL}[warn]${NC}  $*"; }
die()   { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── 工作目录锁定到 repo 根 ──────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ── 前置检查 ────────────────────────────────────────────────────────
[[ -f "$LOCAL_ENV" ]] || die "凭证文件 '$LOCAL_ENV' 不存在。请先复制 deploy/.env.example 并填写真实值。"
[[ -f "collector/dev_collector.py" ]] || die "collector/dev_collector.py 不存在，请确认 repo 结构。"
[[ -f "collector/feilian_client.py" ]] || die "collector/feilian_client.py 不存在。"
[[ -f "collector/subscriptions_sync.py" ]] || die "collector/subscriptions_sync.py 不存在。"
[[ -f "collector/feishu_directory_sync.py" ]] || die "collector/feishu_directory_sync.py 不存在。"
[[ -f "agent/remote_tokscale_report.sh" ]] || die "agent/remote_tokscale_report.sh 不存在。"
[[ -f "agent/tokreport_windows.ps1" ]] || die "agent/tokreport_windows.ps1 不存在。"
command -v rsync >/dev/null 2>&1 || die "本机未安装 rsync。"

info "目标: ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}  port=${PORT}"

# ── 1. 远端建目录 ───────────────────────────────────────────────────
info "[1/5] 远端建目录 ${REMOTE_DIR}"
# shellcheck disable=SC2029
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "mkdir -p '${REMOTE_DIR}'"

# ── 2. 同步 collector 源码 ──────────────────────────────────────────
info "[2/5] rsync collector/*.py → 远端"
rsync -az \
    -e "ssh $SSH_OPTS" \
    collector/dev_collector.py \
    collector/feilian_client.py \
    collector/litellm_collector.py \
    collector/subscriptions_sync.py \
    collector/feishu_directory_sync.py \
    collector/dashboard.html \
    collector/help.html \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

info "[2b/6] rsync reporter scripts → 远端 (remote_tokscale_report.sh, /tokreport.ps1)"
rsync -az \
    -e "ssh $SSH_OPTS" \
    agent/remote_tokscale_report.sh \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/remote_tokscale_report.sh"
rsync -az \
    -e "ssh $SSH_OPTS" \
    agent/tokreport_windows.ps1 \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/tokreport.ps1"

# ── 3. 上传 env 文件（含真实凭证，走加密 ssh 通道，不经明文） ──────
info "[3/5] 上传凭证 env 文件 → 远端 ${REMOTE_DIR}/.env"
# 先上传，再 chmod 600（两步保证权限在内容写入后立即锁定）
scp $SSH_OPTS "$LOCAL_ENV" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/.env"
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "chmod 600 '${REMOTE_DIR}/.env'"

# ── 4. 上传并 install systemd service ───────────────────────────────
info "[4/5] 安装 systemd unit"

# 渲染 service 模板（替换变量后上传）
SERVICE_CONTENT="$(sed \
    -e "s|__REMOTE_DIR__|${REMOTE_DIR}|g" \
    -e "s|__PYTHON__|${PYTHON}|g" \
    -e "s|__PORT__|${PORT}|g" \
    -e "s|__SERVICE_NAME__|${SERVICE_NAME}|g" \
    "$SCRIPT_DIR/tokreport-collector.service")"

# 写到远端 /etc/systemd/system/（需要 sudo；经侦察 it 有 sudo systemctl）
echo "$SERVICE_CONTENT" | ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null"

ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl daemon-reload && sudo systemctl enable ${SERVICE_NAME}.service"

info "systemd unit 已安装并 enable，服务尚未启动（由主控决定何时 start）"
warn "启动命令（主控执行）: sudo systemctl start ${SERVICE_NAME}"

# ── 4b. LiteLLM 同步 timer（每小时把网关 token 灌进同一个 tok.db） ───
# 前提：pipeline/.env 里需含 LITELLM_BASE_URL + LITELLM_MASTER_KEY（已随 .env 上传）。
info "[4b/6] 安装 litellm-sync service + timer（hourly）"
for _unit in litellm-sync.service litellm-sync.timer; do
    sed \
        -e "s|__REMOTE_DIR__|${REMOTE_DIR}|g" \
        -e "s|__PYTHON__|${PYTHON}|g" \
        "$SCRIPT_DIR/${_unit}" \
    | ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
        "sudo tee /etc/systemd/system/${_unit} > /dev/null"
done
# enable --now 让 timer 立即开始计时（service 本身是 oneshot，由 timer 触发）
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl daemon-reload && sudo systemctl enable --now litellm-sync.timer"
info "litellm-sync.timer 已 enable --now（开机 3min 后首跑，之后每个整点）"
warn "立即手动跑一次（主控执行）: ssh it@${REMOTE_HOST} 'sudo systemctl start litellm-sync.service'"

# ── 4c. 订阅名单同步 timer（每天 03:30 从飞书表整表覆盖 subscriptions） ──
# 前提：.env 里需含 FEISHU_APP_ID + FEISHU_APP_SECRET（bot 凭证，已随 .env 上传）。
info "[4c/6] 安装 subscriptions-sync service + timer（daily 03:30）"
for _unit in subscriptions-sync.service subscriptions-sync.timer; do
    sed \
        -e "s|__REMOTE_DIR__|${REMOTE_DIR}|g" \
        -e "s|__PYTHON__|${PYTHON}|g" \
        "$SCRIPT_DIR/${_unit}" \
    | ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
        "sudo tee /etc/systemd/system/${_unit} > /dev/null"
done
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl daemon-reload && sudo systemctl enable --now subscriptions-sync.timer"
info "subscriptions-sync.timer 已 enable --now（每天 03:30 触发，oneshot）"
warn "立即手动跑一次（主控执行）: ssh it@${REMOTE_HOST} 'sudo systemctl start subscriptions-sync.service'"

# ── 4d. 飞书通讯录同步 timer（每天 02:10 同步 people/departments/roles） ──
# 前提：.env 里需含 FEISHU_APP_ID + FEISHU_APP_SECRET，且 bot 有 contact-read 权限。
info "[4d/6] 安装 feishu-directory-sync service + timer（daily 02:10）"
for _unit in feishu-directory-sync.service feishu-directory-sync.timer; do
    sed \
        -e "s|__REMOTE_DIR__|${REMOTE_DIR}|g" \
        -e "s|__PYTHON__|${PYTHON}|g" \
        "$SCRIPT_DIR/${_unit}" \
    | ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
        "sudo tee /etc/systemd/system/${_unit} > /dev/null"
done
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl daemon-reload && sudo systemctl enable --now feishu-directory-sync.timer"
info "feishu-directory-sync.timer 已 enable --now（每天 02:10 触发，oneshot）"
warn "立即手动跑一次（主控执行）: ssh it@${REMOTE_HOST} 'sudo systemctl start feishu-directory-sync.service'"

# ── 5. 幂等验证：检查 unit 是否被 systemd 识别 ──────────────────────
info "[5/6] 验证 collector unit 注册"
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl status ${SERVICE_NAME}.service --no-pager 2>&1 | head -8 || true"

info "[6/6] 验证 litellm-sync.timer"
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl list-timers litellm-sync.timer --no-pager 2>&1 | head -5 || true"

info "[6b/6] 验证 feishu-directory-sync.timer"
ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "sudo systemctl list-timers feishu-directory-sync.timer --no-pager 2>&1 | head -5 || true"

echo ""
info "部署 kit 传输完成。collector 待主控 start；litellm-sync.timer 已自动计时。"
info "主控启动 collector: ssh it@${REMOTE_HOST} 'sudo systemctl start ${SERVICE_NAME}'"
info "litellm 立即首跑:    ssh it@${REMOTE_HOST} 'sudo systemctl start litellm-sync.service'"
info "飞书通讯录首跑:      ssh it@${REMOTE_HOST} 'sudo systemctl start feishu-directory-sync.service'"
info "冒烟测试: bash deploy/smoke.sh"

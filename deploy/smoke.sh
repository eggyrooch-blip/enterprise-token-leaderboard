#!/usr/bin/env bash
# smoke.sh — 部署后冒烟测试
# 用法：bash deploy/smoke.sh [host] [token]
#   host  默认 collector.example.com
#   token 默认从 pipeline/.env 读取 COLLECTOR_API_TOKENS 第一个值

set -euo pipefail

HOST="${1:-${REMOTE_HOST:-collector.example.com}}"
PORT="${PORT:-8090}"
BASE="http://${HOST}:${PORT}"

# 读取 token
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_ENV="${REPO_ROOT}/pipeline/.env"

if [[ -n "${2:-}" ]]; then
    TOKEN="$2"
elif [[ -f "$LOCAL_ENV" ]]; then
    TOKEN="$(grep -E '^COLLECTOR_API_TOKENS=' "$LOCAL_ENV" | head -1 | cut -d= -f2- | cut -d, -f1 | tr -d '[:space:]')"
else
    TOKEN="devtoken"
fi

GRN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
pass() { echo -e "${GRN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; FAILURES=$((FAILURES+1)); }
FAILURES=0

echo "=== smoke test: ${BASE}  token=${TOKEN:0:6}*** ==="

# ── 1. GET / 健康检查 ────────────────────────────────────────────────
echo ""
echo "[1/3] GET ${BASE}/"
RESP=$(curl -sf -m 8 "${BASE}/" 2>&1) && pass "服务响应: $RESP" || fail "GET / 失败: $RESP"

# ── 2. POST /v1/tokscale/report 模拟飞连脚本上报 ────────────────────
echo ""
echo "[2/3] POST ${BASE}/v1/tokscale/report"
PAYLOAD='{"serial":"SMOKE-TEST-001","email":"smoke@example.com","models":{"entries":[{"client":"claude","model":"claude-sonnet-4-5","input":1000,"output":500,"cacheRead":0,"cacheWrite":0,"reasoning":0,"cost":0.01,"messageCount":3}]}}'
RESP=$(curl -sf -m 8 \
    -X POST "${BASE}/v1/tokscale/report" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1) \
    && pass "tokscale/report 响应: $RESP" \
    || fail "POST /v1/tokscale/report 失败: $RESP"

# ── 3. GET /v1/leaderboard 验证数据入库 ─────────────────────────────
echo ""
echo "[3/3] GET ${BASE}/v1/leaderboard"
RESP=$(curl -sf -m 8 "${BASE}/v1/leaderboard" 2>&1) && pass "leaderboard: $RESP" || fail "GET /v1/leaderboard 失败: $RESP"

echo ""
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GRN}=== 全部通过 ===${NC}"
else
    echo -e "${RED}=== ${FAILURES} 项失败 ===${NC}"
    exit 1
fi

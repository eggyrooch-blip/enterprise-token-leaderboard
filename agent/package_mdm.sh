#!/bin/bash
# 组装 MDM 下发包：把 agent 全部文件 + tokscale 二进制 + 填好的 conf 打成一个目录/压缩包，
# 交给飞连/JAMF 下发后以 root 执行包内 install.sh 即可。
#
# 用法:
#   ./package_mdm.sh <tokscale二进制路径> <收集端URL> <BearerToken> [输出目录]
# 例:
#   ./package_mdm.sh ./tokscale https://collector.example.com xxxxx ./dist
set -euo pipefail

TOKSCALE_BIN="${1:?need path to tokscale binary}"
COLLECTOR_URL="${2:?need collector url}"
COLLECTOR_TOKEN="${3:?need collector token}"
OUT="${4:-./dist/tokreport-mdm}"
HERE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$OUT/collectors"
cp "$HERE/tokreport.py" "$HERE/identity.py" "$HERE/install.sh" "$HERE/com.example.tokreport.plist" "$OUT/"
cp "$HERE"/collectors/*.py "$OUT/collectors/"
cp "$TOKSCALE_BIN" "$OUT/tokscale"
chmod +x "$OUT/install.sh" "$OUT/tokscale"

# 身份留空 -> 客户端自动用 git email；如需强归属，MDM 可在每台机覆盖 EMPLOYEE_EMAIL
cat > "$OUT/tokreport.conf" <<EOF
COLLECTOR_URL=$COLLECTOR_URL
COLLECTOR_TOKEN=$COLLECTOR_TOKEN
COLLECTORS=tokscale
TOKSCALE_BIN=/usr/local/bin/tokscale
EMPLOYEE_EMAIL=
EMAIL_DOMAIN=
LOOKBACK_DAYS=3
EOF

( cd "$(dirname "$OUT")" && tar czf "$(basename "$OUT").tar.gz" "$(basename "$OUT")" )
echo "MDM package ready:"
echo "  dir:    $OUT"
echo "  tar:    $OUT.tar.gz"
echo "MDM 下发后执行： sudo ./install.sh ."

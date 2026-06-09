#!/bin/bash
# 把你日常 Chrome 里登录了飞书后台的 Profile 拷成独立 user-data-dir(自动化专用)。
# 为什么要拷:Chrome 136+ 禁止在默认 profile 上开调试端口,且一次性登录攒不齐全套
# cookie——只有你日常 profile 的完整会话才工作。拷一份独立目录即可带调试端口跑、headless。
#
# 何时跑:首次部署一次;之后采集器报 LOGIN_EXPIRED 时,你在日常 Chrome 里确认飞书后台
# 还登着(打开 keep.feishu.cn/admin 看一眼),再跑本脚本刷新拷贝即可。绝不天天跑。
#
# 用法: ./refresh_profile.sh [Profile 名(默认自动探测含 feishu cookie 最多的)]
set -euo pipefail
SRC="$HOME/Library/Application Support/Google/Chrome"
DEST="$HOME/.feishu/auto_udd"
PROFILE="${1:-}"

# 自动探测:哪个 Profile 的 feishu cookie 最多
if [ -z "$PROFILE" ]; then
  best=""; bestn=-1
  for d in "$SRC"/Profile* "$SRC/Default"; do
    [ -d "$d" ] || continue
    ck="$d/Network/Cookies"; [ -f "$ck" ] || ck="$d/Cookies"
    [ -f "$ck" ] || continue
    n=$(sqlite3 "file:$ck?mode=ro" "SELECT count(*) FROM cookies WHERE host_key LIKE '%feishu%'" 2>/dev/null || echo 0)
    if [ "${n:-0}" -gt "$bestn" ]; then bestn=$n; best="$(basename "$d")"; fi
  done
  PROFILE="$best"
  echo "自动探测到含飞书 cookie 最多的 profile: 「$PROFILE」($bestn 个)"
fi
[ -n "$PROFILE" ] || { echo "❌ 找不到任何登录了飞书的 Chrome profile"; exit 1; }

echo "拷贝 「$PROFILE」 → $DEST (排除大缓存)…"
rm -rf "$DEST"; mkdir -p "$DEST/$PROFILE"
cp "$SRC/Local State" "$DEST/Local State" 2>/dev/null || true
rsync -a \
  --exclude 'Cache' --exclude 'Code Cache' --exclude 'GPUCache' \
  --exclude 'Service Worker/CacheStorage' --exclude 'Service Worker/ScriptCache' \
  --exclude 'DawnGraphiteCache' --exclude 'DawnWebGPUCache' --exclude 'Application Cache' \
  --exclude 'File System' --exclude 'IndexedDB' \
  "$SRC/$PROFILE/" "$DEST/$PROFILE/"

# 记录用了哪个 profile,供 run_collector.sh 启动 Chrome 时带 --profile-directory
echo "$PROFILE" > "$DEST/.profile_name"
n=$(sqlite3 "file:$DEST/$PROFILE/Network/Cookies?mode=ro" "SELECT count(*) FROM cookies WHERE host_key LIKE '%feishu%'" 2>/dev/null || echo '?')
echo "✅ 完成。拷贝后 feishu cookie: $n。大小: $(du -sh "$DEST" | cut -f1)"

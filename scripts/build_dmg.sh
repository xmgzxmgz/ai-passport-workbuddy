#!/bin/bash
# ---------------------------------------------------------------------------
# 打包 WBBridge.app 并生成可分发的 DMG
#
#   ./scripts/build_dmg.sh              # 出 dist/WBBridge-<版本>.dmg
#   ./scripts/build_dmg.sh --no-icon    # 跳过图标重绘
#
# 产物布局（DMG 里）：
#   WBBridge.app        后台服务（内含 bridge/*.py，自包含）
#   Applications -> /Applications      拖拽安装用
#   安装与使用.txt      说明书
#   wbctl               命令行开关
#
# 说明：
#   · app 用 ad-hoc 签名（codesign -s -）。本机自建自用没问题；拷到别的
#     机器会被 Gatekeeper 拦，需要右键 → 打开，或 xattr -dr com.apple.quarantine。
#   · 不打包 Python 运行时（venv 带 faster-whisper 有好几百 MB）。启动器
#     按需探测解释器，缺失时弹窗告知怎么装。
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PLIST="packaging/Info.plist"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST")"
DIST="$ROOT/dist"
STAGE="$DIST/stage"
APP="$STAGE/WBBridge.app"
DMG="$DIST/WBBridge-$VERSION.dmg"
PY="${WB_PYTHON:-$HOME/.workbuddy/binaries/python/envs/default/bin/python}"
MAKE_ICON=1
[[ "${1:-}" == "--no-icon" ]] && MAKE_ICON=0

echo "▸ 版本 $VERSION"

# --- 1. 图标 ---------------------------------------------------------------
ICON="packaging/AppIcon.icns"
if (( MAKE_ICON )) || [[ ! -f "$ICON" ]]; then
  if "$PY" -c 'import PIL' 2>/dev/null; then
    "$PY" packaging/make_icon.py
  else
    echo "  ! 没有 PIL，跳过图标（沿用旧文件或留空）"
  fi
fi

# --- 2. 组装 .app ----------------------------------------------------------
rm -rf "$STAGE"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/bridge"

# 只带运行需要的代码：录音目录、缓存、旧 bundle、pyc 都不进包
rsync -a \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'voice_out' --exclude '.gw_cache.json' \
  --exclude 'WBBridge.app' --exclude '.DS_Store' \
  bridge/ "$APP/Contents/Resources/bridge/"

cp "$PLIST" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

cp packaging/launcher/wb-bridge "$APP/Contents/MacOS/wb-bridge"
chmod +x "$APP/Contents/MacOS/wb-bridge"

[[ -f "$ICON" ]] && cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"
cp packaging/安装与使用.txt "$APP/Contents/Resources/安装与使用.txt"

# 签名（内容变了必须重签，否则 TCC 会把蓝牙授权判成另一个应用）
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
  && echo "▸ 已 ad-hoc 签名" \
  || echo "  ! 签名失败，蓝牙授权可能每次都要重新确认"

# --- 3. 打包 DMG ----------------------------------------------------------
mkdir -p "$STAGE"
ln -sfn /Applications "$STAGE/Applications"
cp packaging/wbctl "$STAGE/wbctl"; chmod +x "$STAGE/wbctl"
cp packaging/安装与使用.txt "$STAGE/安装与使用.txt"

rm -f "$DMG"
hdiutil create -quiet -volname "WBBridge" -srcfolder "$STAGE" \
  -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$DMG"

# --- 4. 自检 --------------------------------------------------------------
hdiutil verify -quiet "$DMG"
SIZE="$(du -h "$DMG" | awk '{print $1}')"
echo
echo "✅ $DMG  ($SIZE)"
echo
echo "内容："
hdiutil attach -quiet -nobrowse -mountpoint /tmp/wbdmg.$$ "$DMG"
ls -1 /tmp/wbdmg.$$ | sed 's/^/    /'
du -sh /tmp/wbdmg.$$/WBBridge.app/Contents/Resources/bridge | awk '{print "    代码体积："$1}'
hdiutil detach -quiet /tmp/wbdmg.$$ || true

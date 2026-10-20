#!/bin/bash
# ---------------------------------------------------------------------------
# 生成文档用渲染图（工牌三态 / 主窗口五页 / 头图）
#
#   ./scripts/make_renders.sh
#
# 做法：docs/render/*.html 用 CSS 画工牌机身与屏内 UI（坐标照 firmware/main/
# wb_app.c 还原），再用无头 Chrome 按 2 倍像素密度截图。改样式后重跑即可，
# 不用手工 P 图。
#
# 依赖：Google Chrome（只用到 --headless 截图，不需要装 puppeteer）
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -x "$CHROME" ]] || { echo "找不到 Chrome，装一个再跑（或改 CHROME 变量）"; exit 1; }

OUT="docs/render"
shoot () {                    # shoot <html> <宽> <高> <输出png>
  local src="$1" w="$2" h="$3" dst="$4"
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --allow-file-access-from-files --force-device-scale-factor=2 \
    --window-size="$w,$h" --virtual-time-budget=3000 \
    --screenshot="$dst" "file://$ROOT/$OUT/$src" >/dev/null 2>&1
  printf '  %-26s %s\n' "$(basename "$dst")" "$(du -h "$dst" | awk '{print $1}')"
}

echo "▸ 渲染中（2x）"
shoot badges.html 1180 700  "$OUT/badge-states.png"
shoot hero.html   1440 880  "$OUT/hero.png"
shoot pages.html  1560 1060 "$OUT/app-pages.png"

echo
echo "✅ 产出在 $OUT/"

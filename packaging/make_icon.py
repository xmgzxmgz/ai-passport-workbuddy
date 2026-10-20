#!/usr/bin/env python3
"""生成 WBBridge 的 .icns 图标（打包时调用，一次性产物可提交进仓库）。

画一个深色圆角方块 + 青色蓝牙环 + 白色话筒，和工牌金属外壳的观感对齐。
输出 packaging/AppIcon.icns（iconset 中间产物自动清理）。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / "AppIcon.icns"

SIZE = 1024
BG_TOP = (28, 36, 52)
BG_BOT = (12, 16, 24)
ACCENT = (64, 208, 214)
INK = (245, 249, 255)


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size - 1, size - 1), radius, fill=255)
    return m


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 渐变底（逐行插值，够用且无依赖）
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel((0, y), tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((SIZE, SIZE))
    img.paste(grad, (0, 0), rounded_mask(SIZE, 230))

    d = ImageDraw.Draw(img)

    # 外圈光晕
    d.ellipse((150, 150, 874, 874), outline=(ACCENT[0], ACCENT[1], ACCENT[2], 90), width=6)
    d.ellipse((190, 190, 834, 834), outline=(ACCENT[0], ACCENT[1], ACCENT[2], 45), width=3)

    # 话筒头
    d.rounded_rectangle((452, 300, 572, 566), radius=60, fill=INK)
    # 话筒支架（开口朝上的弧）
    d.arc((384, 396, 640, 652), start=0, end=180, fill=INK, width=26)
    d.line((512, 652, 512, 726), fill=INK, width=26)
    d.line((446, 726, 578, 726), fill=INK, width=26)

    # 蓝牙符文（右上角点缀）
    bx, by = 700, 300
    pts = [(bx, by), (bx, by + 150), (bx + 62, by + 75)]
    d.line(pts, fill=ACCENT, width=14, joint="curve")
    d.line([(bx + 62, by + 75), (bx - 46, by + 150)], fill=ACCENT, width=14)
    d.line([(bx + 62, by + 75), (bx - 46, by)], fill=ACCENT, width=14)

    return img


def main() -> int:
    img = draw_icon()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        specs = [(16, "icon_16x16"), (32, "icon_16x16@2x"), (32, "icon_32x32"),
                 (64, "icon_32x32@2x"), (128, "icon_128x128"), (256, "icon_128x128@2x"),
                 (256, "icon_256x256"), (512, "icon_256x256@2x"), (512, "icon_512x512"),
                 (1024, "icon_512x512@2x")]
        for px, name in specs:
            img.resize((px, px), Image.LANCZOS).save(iconset / f"{name}.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)], check=True)
    print(f"✅ {OUT}  ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

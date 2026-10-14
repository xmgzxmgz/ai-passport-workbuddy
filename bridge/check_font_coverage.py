#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 LVGL 内置 CJK 字体对真实对话数据的汉字覆盖率。"""
import os
import re
import sqlite3
import sys
from pathlib import Path

# 固件工程根目录（需含 managed_components/lvgl__lvgl —— 编译过一次就会有）。
ENGINE_ROOT = Path(os.environ.get("WB_ENGINE_ROOT")
                   or Path(__file__).resolve().parent.parent / "firmware")
FONT_DIR = ENGINE_ROOT / "managed_components" / "lvgl__lvgl" / "src" / "font"
DB = Path.home() / ".workbuddy" / "workbuddy.db"
PROJECTS = Path.home() / ".workbuddy" / "projects"

HAN = lambda ch: "\u3400" <= ch <= "\u9fff"          # noqa: E731


def font_codepoints(fname):
    """字形注释 /* U+XXXX "c" */ 是最准的码点来源（bitmap/dsc 重复，用 set 去重）。"""
    src = (FONT_DIR / fname).read_text(encoding="utf-8", errors="ignore")
    return set(int(h, 16) for h in re.findall(r"/\* U\+([0-9A-Fa-f]{2,6})", src))


def han_of(text):
    return set(ch for ch in text if HAN(ch))


def gather_real_text():
    titles, bodies = set(), set()
    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
            for (t,) in con.execute(
                "SELECT title FROM sessions WHERE title IS NOT NULL"
            ):
                titles |= han_of(t or "")
            con.close()
        except sqlite3.Error as e:
            print(f"  [warn] db: {e}", file=sys.stderr)

    if PROJECTS.exists():
        for n, jf in enumerate(PROJECTS.rglob("*.jsonl")):
            if n >= 30:
                break
            try:
                bodies |= han_of(
                    jf.read_text(encoding="utf-8", errors="ignore")
                )
            except OSError:
                pass
    return titles, bodies


def main():
    for fname in ("lv_font_source_han_sans_sc_14_cjk.c",
                  "lv_font_source_han_sans_sc_16_cjk.c"):
        cps = font_codepoints(fname)
        han = set(c for c in cps if HAN(chr(c)))
        ascii_ok = all((0x20 <= c <= 0x7E) for c in range(0x20, 0x7F))
        ascii_have = sum(1 for c in range(0x20, 0x7F) if c in cps)
        print(f"{fname}")
        print(f"  字形总数 : {len(cps)}")
        print(f"  含汉字   : {len(han)}")
        print(f"  ASCII    : {ascii_have}/95")
        for c in (0x2014, 0x2018, 0x201C, 0x2026, 0x00B7, 0x3002, 0xFF0C, 0xFF1F):
            print(f"    U+{c:04X} {chr(c)}: {'有' if c in cps else '缺'}")
        print()

    titles, bodies = gather_real_text()
    # 统一转成码点再比较（字符集 - 码点集永远为空）
    titles = set(ord(c) for c in titles)
    bodies = set(ord(c) for c in bodies)
    print(f"真实标题汉字数 : {len(titles)}")
    print(f"真实正文汉字数 : {len(bodies)}")
    print()

    font16 = font_codepoints("lv_font_source_han_sans_sc_16_cjk.c")

    # 自建字库（GB2312 全集），生成前先看 symbols.txt
    # FONT_DIR = <proj>/ai-passport/managed_components/lvgl__lvgl/src/font
    symbols_path = FONT_DIR.parents[3] / "fonts" / "symbols.txt"
    custom = set()
    if symbols_path.exists():
        custom = set(ord(c) for c in symbols_path.read_text(encoding="utf-8"))
        h = len([c for c in custom if 0x3400 <= c <= 0x9FFF])
        print(f"自建字库 symbols.txt : {len(custom)} 字符（汉字 {h}）")
    else:
        print(f"自建字库缺失: {symbols_path}")

    for label, fontset in (("LVGL内置", font16), ("自建GB2312", custom)):
        if not fontset:
            continue
        print(f"\n===== {label} =====")
        for name, real in (("标题", titles), ("正文", bodies)):
            if not real:
                print(f"[{name}] 无数据")
                continue
            miss = real - fontset
            hit = real & fontset
            print(f"[{name}] 覆盖 {len(hit)}/{len(real)} = "
                  f"{len(hit)*100.0/len(real):.1f}%")
            if miss:
                print(f"       缺 {len(miss)} 字: "
                      f"{''.join(chr(c) for c in sorted(miss))[:160]}")
        print()

    return


if __name__ == "__main__":
    main()

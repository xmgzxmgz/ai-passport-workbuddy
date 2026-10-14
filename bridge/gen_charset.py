#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 LVGL 字体字符集文件。

覆盖范围（去重后输出到 symbols.txt）：
  1. ASCII 可视区 0x20-0x7E
  2. GB2312 全集（6763 汉字：一级 3755 + 二级 3008）
  3. 中文标点与常用符号（LVGL 内置字体缺的那批）
  4. WorkBuddy 真实数据里出现过的全部字符（兜底，可能有 GB2312 之外的字）
"""
import os
import sqlite3
import sys
import unicodedata
from pathlib import Path

# 固件工程根目录。默认取本仓库的 firmware/；若你把固件放在别处（比如
# 从 FoloToy/ai-passport clone 出来的完整工程），用 WB_ENGINE_ROOT 指过去。
ENGINE_ROOT = Path(os.environ.get("WB_ENGINE_ROOT")
                   or Path(__file__).resolve().parent.parent / "firmware")
OUT_DIR = ENGINE_ROOT / "fonts"
DB = Path.home() / ".workbuddy" / "workbuddy.db"
PROJECTS = Path.home() / ".workbuddy" / "projects"
SESSIONS = Path.home() / ".workbuddy" / "sessions"

# 标点：中文全角 + 通用符号（LVGL 内置 CJK 字体只带 。和，其它全缺）
PUNCT = (
    "、。〈〉《》「」『』【】〔〕〖〗！＂＃＄％＆＇（）＊＋，－．／"
    "０１２３４５６７８９：；＜＝＞？＠［＼］＾＿｀｛｜｝～"
    "…—–‘’“”·×÷°±≤≥≠→←↑↓№℃￥$€£¥§¶†‡•‰′″"
    "　"
)
# 全角数字字母（某些标题会混用）
FULLWIDTH = "".join(chr(c) for c in range(0xFF01, 0xFF5F))


def gb2312_chars():
    """GB2312 全部汉字（一级 16-55 区 + 二级 56-87 区）。"""
    out = []
    for q in range(16, 88):
        for w in range(1, 95):
            try:
                out.append(bytes([q + 0xA0, w + 0xA0]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return out


def real_chars():
    """从真实数据采集字符，兜住 GB2312 之外的字（如 emoji 外的生僻字）。"""
    got = set()

    def eat(s):
        for ch in s or "":
            cat = unicodedata.category(ch)
            # 只留字母/数字/标点/符号，丢掉控制字符与 emoji 代理
            if cat[0] in ("L", "N", "P", "S", "Z") and ord(ch) > 0x1F:
                got.add(ch)

    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
            for (t,) in con.execute("SELECT title FROM sessions WHERE title IS NOT NULL"):
                eat(t)
            con.close()
        except sqlite3.Error as e:
            print(f"[warn] db: {e}", file=sys.stderr)

    for base in (PROJECTS, SESSIONS):
        if not base.exists():
            continue
        for n, f in enumerate(base.rglob("*")):
            if n >= 60:
                break
            if f.suffix not in (".jsonl", ".json"):
                continue
            try:
                eat(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return got


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ascii_set = set(chr(c) for c in range(0x20, 0x7F))
    gb = set(gb2312_chars())
    pun = set(PUNCT + FULLWIDTH)
    real = real_chars()

    allset = ascii_set | gb | pun | real
    # 过滤掉 TTF 里大概率没有的：emoji、私用区、非常用符号
    def keep(ch):
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            return True
        if 0x2000 <= o <= 0x206F and ch in pun:      # 通用标点，仅取白名单
            return True
        if 0x2190 <= o <= 0x21FF and ch in pun:
            return True
        if 0x2460 <= o <= 0x24FF:                    # ① ② ③
            return True
        if 0x3000 <= o <= 0x303F:                    # CJK 标点
            return True
        if 0x3400 <= o <= 0x9FFF:                    # CJK 扩展A + 基本区
            return True
        if 0xFF00 <= o <= 0xFFEF:                    # 全角
            return True
        if o in (0x00A5, 0x00B0, 0x00B1, 0x00D7, 0x00F7):
            return True
        return False

    final = sorted(ch for ch in allset if keep(ch))

    symbols_path = OUT_DIR / "symbols.txt"
    with open(symbols_path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(final))
    # 再产一份 range 形式，lv_font_conv 可直接用
    ranges_path = OUT_DIR / "symbols.txt"
    print(f"字符集总计 : {len(final)} 字符")
    print(f"  ASCII    : {len([c for c in final if ord(c) < 0x80])}")
    print(f"  汉字     : {len([c for c in final if 0x3400 <= ord(c) <= 0x9FFF])}")
    print(f"  其它符号 : {len([c for c in final if not (ord(c) < 0x80) and not (0x3400 <= ord(c) <= 0x9FFF)])}")
    print(f"  来自真实数据: {len([c for c in real if c in final])}")
    print(f"输出       : {symbols_path}")


if __name__ == "__main__":
    main()

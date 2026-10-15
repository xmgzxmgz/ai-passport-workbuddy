#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-bridge v3 —— WorkBuddy <-> AI Passport 双向中转服务

AI Passport（ESP32-C3）自己读不到 PC 上的 WorkBuddy 数据，也不会跑 ASR。
本服务跑在 PC 侧，做三件事：

  1. 下行：把 WorkBuddy 本机数据归约成固件能消费的小帧，经 BLE FF01 推送
     （状态 / 对话列表 / 消息流 / 额度 / 发送结果）
  2. 上行：接收工牌的按键、选择、语音帧、发送请求（BLE FF02 Notify）
  3. 语音闭环：拼 WAV -> 本地 Whisper 转写 -> 发送到指定对话 -> 回执

数据源（全部免认证、只读）：
  A. ~/.workbuddy/sessions/<pid>.json          会话心跳
  B. ~/.workbuddy/workbuddy.db                 sessions / session_usage 表
  C. ~/.workbuddy/projects/<slug>/<sid>.jsonl  对话消息流

协议（与固件 main/wb_proto.h 一一对应）：
  下行帧  AA cmd seq total payload
  上行帧  BB cmd payload
"""

import glob
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _wb_home() -> Path:
    """定位 ~/.workbuddy。

    注意：`Path("~")` **不会**展开成家目录。原实现写的是
        Path(os.environ.get("USERPROFILE", "~")) / ".workbuddy"
    Windows 上正常（USERPROFILE 一定存在），但在 macOS / Linux 上会退化成
    字面量相对路径 "~/.workbuddy" —— 于是所有数据源都查不到东西，
    只读功能静默返回空，看起来像"服务跑起来了但什么都没数据"。
    """
    env = os.environ.get("WB_HOME")
    if env:
        return Path(env).expanduser()
    # Windows 用 USERPROFILE；macOS / Linux 用真实家目录
    base = os.environ.get("USERPROFILE")
    return (Path(base) if base else Path.home()) / ".workbuddy"


WB_HOME      = _wb_home()
SESSIONS_DIR = WB_HOME / "sessions"
WORKBUDDY_DB = WB_HOME / "workbuddy.db"
PROJECTS_DIR = WB_HOME / "projects"
MAIN_LOG     = WB_HOME / "logs" / "main.log"

BRIDGE_DIR   = Path(__file__).resolve().parent
VOICE_DIR    = BRIDGE_DIR / "voice_out"

HTTP_PORT     = 8765
POLL_INTERVAL = 2

OFFLINE_AFTER_MS = 60_000

# 协议限制（必须与 wb_proto.h 一致）
TITLE_MAX     = 32
LIST_MAX      = 5         # 下发给工牌的对话条数；屏幕一屏 5 行，不做翻页
MSG_PAGE_MAX  = 10        # 固件 MSG_SLOTS
MSG_BODY_MAX  = 120       # WB_TEXT_LEN
ASR_BODY_MAX  = 170       # 转写回显 ≤ WB_ASR_LEN(200)，留帧头 + UTF-8 余量
VOICE_MAX_SEC = 60        # 语音上限，防呆（固件侧同样有独立上限）

# 语音 WAV 保留策略：每次录音落一个文件，不清理会一直堆
VOICE_KEEP_MAX = 200      # 最多保留多少个
VOICE_KEEP_DAYS = 30      # 或者最多保留多少天

# 转写全文会进日志。默认只打长度，设 WB_LOG_ASR=1 才打全文。
LOG_ASR_TEXT = os.environ.get("WB_LOG_ASR", "0") not in ("", "0", "false", "False")

# WorkBuddy 本地 CLI —— **兜底路径**。
# 正常发送走下面的网关 REST（见 _discover / send_to_workbuddy）；只有在网关
# 发现失败时才退回 CLI 子进程，而那条路在独立进程里拿不到登录态，基本不可用。
# 两个路径都可以用环境变量覆盖。
def _default_cli_dir() -> Path:
    """WorkBuddy 随包 CLI 的默认位置（按平台取）。"""
    if sys.platform == "darwin":
        return Path("/Applications/WorkBuddy.app/Contents/Resources"
                    "/app.asar.unpacked/cli")
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else (Path.home() / "AppData" / "Local")
    return (base / "Programs" / "WorkBuddy" / "resources"
            / "app.asar.unpacked" / "cli")


CLI_DIR  = Path(os.environ.get("WB_CLI_DIR") or _default_cli_dir())


def _find_node():
    """定位随 WorkBuddy 一起安装的 node；找不到就信 PATH 里的 node。"""
    env = os.environ.get("WB_NODE_EXE")
    if env:
        return Path(env)
    versions = Path.home() / ".workbuddy" / "binaries" / "node" / "versions"
    if versions.is_dir():
        # Windows 装的是 node.exe，macOS / Linux 是 bin/node
        found = sorted(versions.glob("*/node.exe")) or \
            sorted(versions.glob("*/bin/node"))
        if found:
            return found[-1]
    from shutil import which
    return Path(which("node") or "node")


NODE_EXE = _find_node()

# Whisper
WHISPER_MODEL = os.environ.get("WB_WHISPER_MODEL", "small")
WHISPER_LANG  = os.environ.get("WB_WHISPER_LANG", "zh")

# huggingface_hub 新版默认走 xet 原生后端下载，实测在 Windows 上会把进程
# 打成段错误（下完 model.bin 就崩）。这里强制退回传统 HTTP 下载。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 语音负载编码（与固件 main/wb_proto.h 的 WB_VOICE_CODEC_* 对齐）
VOICE_CODEC_PCM16 = 0
VOICE_CODEC_ADPCM = 1

# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

STATE_IDLE, STATE_BUSY, STATE_OFFLINE = 0, 1, 2
NAME_OF = {STATE_IDLE: "IDLE", STATE_BUSY: "BUSY", STATE_OFFLINE: "OFFLINE"}

CONV_WORKING, CONV_DONE, CONV_ERROR = 0, 1, 2

# 下行 cmd
D_STATUS, D_CONV, D_MSG, D_USAGE, D_INFO, D_VOICE, D_SENT, D_CLEAR = range(1, 9)
D_ASR = 9
# 上行 cmd
U_KEY, U_SELECT, U_REQ, U_VOICE_START, U_VOICE_DATA, U_VOICE_END, \
    U_VOICE_ABORT, U_SEND = range(1, 9)
U_CONFIRM = 9

# 语音提交模式（WB_U_SEND payload[1]）
SEND_AUTO, SEND_CONFIRM = 0, 1

REQ_ALL, REQ_LIST, REQ_MSG, REQ_USAGE = 0, 1, 2, 3


# ===========================================================================
# 工具
# ===========================================================================

# 固件字库白名单（由 ai-passport/fonts/gen_font.js 与 symbols.txt 同源）
# 不在这个集合里的字符固件渲染不出来（会变豆腐块），下发前必须替换掉。
FONT_SYMBOLS = BRIDGE_DIR.parent / "ai-passport" / "fonts" / "symbols.txt"


def _load_font_chars():
    try:
        return set(FONT_SYMBOLS.read_text(encoding="utf-8"))
    except OSError as e:
        print(f"[warn] 字库白名单读取失败({e})，降级为范围判断")
        return None


_FONT_CHARS = _load_font_chars()


def clean(s):
    """净化文本，使其能被固件字库完整渲染。

    - 控制字符（\\r \\n \\t）→ 空格
    - 字库外的字符（emoji / 生僻符号）→ 空格
    - 压缩连续空格、去掉首尾空白

    替换成空格而不是 '?'，避免标题里出现一串问号。
    """
    if not s:
        return ""
    out = []
    prev_sp = True                      # 抑制开头空格
    for ch in s:
        if ch in "\r\n\t":
            ch = " "
        elif _FONT_CHARS is not None:
            if ch not in _FONT_CHARS:
                ch = " "
        elif not ch.isprintable() or ord(ch) > 0x9FFF:
            ch = " "                    # 无白名单时的降级判断
        if ch == " ":
            if prev_sp:
                continue
            prev_sp = True
        else:
            prev_sp = False
        out.append(ch)
    return "".join(out).strip()


def cut_utf8(s, n):
    """按 UTF-8 安全截断到 <= n 字节，不切断多字节字符。"""
    b = (s or "").encode("utf-8")
    if len(b) <= n:
        return b
    b = b[:n]
    while b and (b[-1] & 0xC0) == 0x80:
        b = b[:-1]
    if b and (b[-1] & 0xC0) == 0xC0:
        b = b[:-1]
    return b


def frame(cmd, seq=0, total=1, payload=b""):
    """下行帧：AA cmd seq total payload"""
    return bytes([0xAA, cmd, seq & 0xFF, total & 0xFF]) + payload


def status_frame(st):
    name = cut_utf8(clean(st.get("task") or ""), TITLE_MAX)
    up = min(int(st.get("uptime_s", 0)), 65535)
    flags = (0x01 if name else 0) | (0x02 if st.get("online") else 0)
    pl = bytes([st.get("state", STATE_OFFLINE), flags, up & 0xFF, (up >> 8) & 0xFF,
                len(name)]) + name
    return frame(D_STATUS, 0, 1, pl)


def conv_frame(i, total, c):
    t = cut_utf8(clean(c.get("title") or ""), TITLE_MAX)
    st = c.get("status") or ""
    code = CONV_WORKING if st == "working" else CONV_ERROR if st in ("error", "failed") \
        else CONV_DONE
    return frame(D_CONV, i, total, bytes([code, len(t)]) + t)


def msg_frame(i, total, m):
    t = cut_utf8(clean(m.get("text") or ""), MSG_BODY_MAX)
    role = 1 if m.get("role") == "assistant" else 0
    return frame(D_MSG, i, total, bytes([role, len(t)]) + t)


def usage_frame(used_x10, total_x10):
    return frame(D_USAGE, 0, 1, f"{int(used_x10)}|{int(total_x10)}".encode())


def info_frame(text):
    return frame(D_INFO, 0, 1, cut_utf8(clean(text), 26))


def sent_frame(ok, text):
    return frame(D_SENT, 0, 1, bytes([0 if ok else 1]) + cut_utf8(clean(text), 36))


def asr_frame(ok, text):
    """转写回显：摊到工牌屏幕上等人确认，本身不投递。"""
    return frame(D_ASR, 0, 1,
                 bytes([0 if ok else 1]) + cut_utf8(clean(text), ASR_BODY_MAX))


def clear_frame(what=0):
    return frame(D_CLEAR, 0, 1, bytes([what]))


# ===========================================================================
# 数据源 A：sessions 心跳
# ===========================================================================

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def latest_heartbeat():
    """最新的 GUI 交互会话心跳，返回 (lastHeartbeat_ms, cwd, sessionId)。

    实测踩坑（本机 4 个心跳文件）：
      * CLI host 子进程也会写心跳，cwd 含 cli_host、sessionId 根本不是
        对话 id —— 不排除会把消息投进一个不存在的会话，屏幕毫无反应；
      * 会话空闲时心跳间隔会拉长到 ~2 分钟，不能只认"几秒内"的心跳。
    所以这里按 UUID 格式过滤 sessionId、排除 cli_host，取最新的那个。
    """
    best = (None, None, None)
    for f in glob.glob(str(SESSIONS_DIR / "*.json")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("kind") != "interactive":
            continue
        sid = d.get("sessionId") or ""
        if not _UUID_RE.match(str(sid)):
            continue
        if "cli_host" in str(d.get("cwd", "")):
            continue
        hb = d.get("lastHeartbeat")
        if isinstance(hb, (int, float)) and (best[0] is None or hb > best[0]):
            best = (hb, d.get("cwd", ""), sid)
    return best


# ===========================================================================
# 数据源 B：workbuddy.db
# ===========================================================================

def _read_db():
    """开一个只读连接。

    优先直连只读 URI —— SQLite 本身支持多进程只读并发，不需要拷贝文件。
    原实现是反的：先 `shutil.copy2` 把整个库拷到临时目录，只在拷贝失败时
    才退到只读 URI。而 `_build_all_frames()` 一轮要调它 3 次（状态/列表/额度），
    主循环 2 秒一轮 —— 稳态下每秒约 1.5 次整库拷贝，纯属无谓磁盘 IO。
    """
    if not WORKBUDDY_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{WORKBUDDY_DB}?mode=ro", uri=True,
                              timeout=1.0)
        con.execute("SELECT 1").fetchone()      # 确认连接真的可用
        return con
    except sqlite3.Error:
        pass
    # 只读 URI 走不通（Windows 上偶发文件锁）才退回快照拷贝
    tmp = Path(tempfile.gettempdir()) / "wb_bridge_snapshot.db"
    try:
        shutil.copy2(WORKBUDDY_DB, tmp)
    except (OSError, PermissionError):
        return None
    try:
        return sqlite3.connect(str(tmp))
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# 查询结果短缓存
# ---------------------------------------------------------------------------
# 一次 _build_all_frames() 会调 compute_state() + list_conversations() +
# get_usage()，而 compute_state() 内部又会调 get_running_conversation() 和
# get_usage()。缓存 1 秒刚够同一轮复用，又不会让屏幕上的数据变旧
# （推帧间隔本来就是 2 秒）。

_QUERY_CACHE: dict = {}
_QUERY_TTL = 1.0
_query_lock = threading.Lock()


def _cached(key, producer):
    now = time.monotonic()
    with _query_lock:
        hit = _QUERY_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = producer()
    with _query_lock:
        _QUERY_CACHE[key] = (now + _QUERY_TTL, val)
    return val


def list_conversations(limit=LIST_MAX, offset=0):
    # 返回浅拷贝：调用方（BleCenter.convs）会长期持有，别让缓存对象被改
    return list(_cached(("convs", limit, offset),
                        lambda: _query_conversations(limit, offset)))


def _query_conversations(limit, offset):
    con = _read_db()
    if con is None:
        return []
    try:
        rows = con.execute(
            """
            SELECT id, COALESCE(custom_title, title, '(untitled)'),
                   status, model, last_activity_at, unread
            FROM sessions
            WHERE deleted_at IS NULL
            ORDER BY last_activity_at DESC
            LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
    except sqlite3.Error:
        con.close()
        return []
    con.close()
    return [{
        "id": sid,
        "title": (title or "")[:TITLE_MAX],
        "status": status or "unknown",
        "model": model or "",
        "updated_at": ts or 0,
        "unread": unread or 0,
    } for sid, title, status, model, ts, unread in rows]


def get_running_conversation():
    con = _read_db()
    if con is None:
        return None
    try:
        row = con.execute(
            """
            SELECT id, COALESCE(custom_title, title, ''), model, last_activity_at
            FROM sessions
            WHERE deleted_at IS NULL AND status = 'working'
            ORDER BY last_activity_at DESC LIMIT 1
            """).fetchone()
    except sqlite3.Error:
        con.close()
        return None
    con.close()
    if not row:
        return None
    return {"id": row[0], "title": (row[1] or "")[:TITLE_MAX],
            "model": row[2] or "", "updated_at": row[3] or 0}


def get_usage(session_id=None):
    con = _read_db()
    if con is None:
        return {"used": 0, "size": 0, "percent": 0, "credits": {}}
    try:
        if session_id:
            row = con.execute(
                "SELECT used, size, credit_json FROM session_usage WHERE session_id = ?",
                (session_id,)).fetchone()
        else:
            row = con.execute(
                """SELECT used, size, credit_json FROM session_usage
                   ORDER BY updated_at DESC LIMIT 1""").fetchone()
    except sqlite3.Error:
        con.close()
        return {"used": 0, "size": 0, "percent": 0, "credits": {}}
    con.close()
    if not row:
        return {"used": 0, "size": 0, "percent": 0, "credits": {}}

    used, size, credit_json = row
    credits = {}
    try:
        raw = json.loads(credit_json or "{}")
        items = sorted(raw.items(), key=lambda kv: kv[1], reverse=True)[:5]
        credits = {k[:16]: round(float(v), 2) for k, v in items}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    pct = int(used * 100 / size) if size else 0
    return {"used": used or 0, "size": size or 0,
            "percent": min(pct, 100), "credits": credits}


# ===========================================================================
# 数据源 C：对话消息流
# ===========================================================================

def _find_jsonl(session_id):
    hits = glob.glob(str(PROJECTS_DIR / "**" / f"{session_id}.jsonl"), recursive=True)
    return hits[0] if hits else None


# 思考过程不上工牌：2.4 寸屏只放一问一答，中间那段推理链既占地方，
# 又会让人误以为是要读的正文。块类型和行内标签两条路都堵掉。
_SKIP_BLOCK = {"thinking", "reasoning", "analysis", "redacted_thinking"}
_SKIP_TAG = re.compile(r"<(thinking|reasoning|analysis)\b[^>]*>.*?</\1>",
                       re.S | re.I)


def _extract_text(content):
    if isinstance(content, str):
        return _SKIP_TAG.sub("", content).strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") in _SKIP_BLOCK:
            continue
        if blk.get("type") in ("input_text", "output_text", "text"):
            t = blk.get("text") or ""
            if "<system-reminder" in t or "<user_info>" in t:
                continue
            t = _SKIP_TAG.sub("", t).strip()
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


def read_messages(session_id, page=0, page_size=MSG_PAGE_MAX):
    """page 0 = 最新一页"""
    path = _find_jsonl(session_id)
    empty = {"total": 0, "page": page, "page_size": page_size,
             "messages": [], "has_more": False}
    if not path:
        return empty

    msgs = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") != "message":
                    continue
                role = o.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _extract_text(o.get("content"))
                if not text:
                    continue
                msgs.append({"role": role, "text": text[:MSG_BODY_MAX],
                             "ts": o.get("timestamp", 0)})
    except OSError:
        return empty

    total = len(msgs)
    end = total - page * page_size
    start = max(0, end - page_size)
    if end <= 0:
        return empty
    return {"total": total, "page": page, "page_size": page_size,
            "messages": msgs[start:end], "has_more": start > 0}


# ===========================================================================
# 归约
# ===========================================================================

_busy_since = None
_prev_key = None


def compute_state():
    global _busy_since, _prev_key
    now_ms = int(time.time() * 1000)

    running = get_running_conversation()
    # 只认近期还在活动的 working 记录：进程异常退出会留下僵尸 working 行，
    # 不设时限会让工牌永远显示 BUSY。
    if running and now_ms - (running.get("updated_at") or 0) > 30 * 60 * 1000:
        running = None

    # 这里永远不返回 OFFLINE：本函数能被调用本身就证明中转服务活着，
    # 而"PC 侧不可用"只可能是 BLE 断开或服务死掉 —— 两种情况工牌都收不到本帧，
    # 由工牌侧按 连接状态 + 收帧超时 自行判离线（wb_app.c 的 home_refresh）。
    #
    # 曾经拿 ~/.workbuddy/sessions/*.json 心跳当在线依据，结果常驻 OFFLINE：
    # 那个文件只由交互式会话在活动时写，闲置几分钟就过期，而服务一直在推帧。
    busy = running is not None

    key = running["id"] if running else None
    if busy and key != _prev_key:
        _busy_since = time.time()
    elif not busy:
        _busy_since = None
    _prev_key = key

    usage = get_usage(running["id"] if running else None)
    return {
        "state": STATE_BUSY if busy else STATE_IDLE,
        "task": running["title"] if running else None,
        "uptime_s": int(time.time() - _busy_since) if _busy_since else 0,
        "model": running["model"] if running else "",
        "online": True,
        "percent": usage.get("percent", 0),
        "conv_id": running["id"] if running else None,
    }


# ===========================================================================
# 语音：WAV + Whisper
# ===========================================================================

_whisper = None
_whisper_lock = threading.Lock()

# ---- IMA/DVI 4bit ADPCM 解码（与固件 main/wb_adpcm.c 同表同式）----
_ADPCM_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209,
    230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876,
    963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
]
_ADPCM_IDX = (-1, -1, -1, -1, 2, 4, 6, 8)


def adpcm_decode(nib_data: bytes, seed: int) -> bytes:
    """把 ADPCM 半字节流解回 16bit 小端 PCM。

    每个包的预测器都用 seed 重置（固件端发包时同样重置），所以包与包
    之间没有耦合 —— 丢一包只坏那 40ms，不会连锁。
    """
    step_tbl = _ADPCM_STEP
    idx_tbl = _ADPCM_IDX
    pred = seed & 0xFFFF
    if pred >= 0x8000:
        pred -= 0x10000
    index = 0
    out = bytearray()
    ext = out.extend
    for b in nib_data:
        for nib in (b & 0x0F, (b >> 4) & 0x0F):
            step = step_tbl[index]
            vp = step >> 3
            if nib & 4:
                vp += step
            if nib & 2:
                vp += step >> 1
            if nib & 1:
                vp += step >> 2
            pred = pred - vp if nib & 8 else pred + vp
            if pred < -32768:
                pred = -32768
            elif pred > 32767:
                pred = 32767
            index += idx_tbl[nib & 7]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            ext(int(pred).to_bytes(2, "little", signed=True))
    return bytes(out)


def save_wav(pcm: bytes, path: Path, rate=8000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def diag_audio(pcm: bytes, rate: int, tag=""):
    """打印一段 PCM 的客观指标，用来判断「录到的到底是不是语音」。

    只做统计不做判断 —— 阈值靠人看，避免脚本自己骗自己。
    """
    import numpy as np

    a = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    if len(a) < 2:
        print(f"[DIAG] {tag} empty", flush=True)
        return
    rms = float(np.sqrt((a ** 2).mean()))
    peak = float(np.abs(a).max())
    dc = float(a.mean())
    clip = int((np.abs(a) >= 32700).sum())
    # 16bit 满幅 -32768 与 +32767 是不对称的，单边贴边也算削顶
    per = max(1, int(rate * 0.04))              # 40ms 一块 = 一个 ADPCM 包
    nb = len(a) // per
    brms = np.sqrt((a[:nb * per].reshape(nb, per) ** 2).mean(axis=1))
    quiet = float((brms < 100).mean()) * 100.0

    print(f"[DIAG] {tag} dur={len(a)/rate:.2f}s rms={rms:.0f} "
          f"peak={peak:.0f} dc={dc:+.0f} clip={clip} quiet={quiet:.0f}%",
          flush=True)
    if nb:
        top = np.argsort(brms)[::-1][:16]
        print("[DIAG] 最响的块 (序号:40ms块的RMS) "
              + " ".join(f"{int(i)}:{int(brms[i])}" for i in sorted(top)),
              flush=True)
        # 前 8 块单独列出：codec 上电瞬态就藏在这里
        print("[DIAG] 开头8块RMS "
              + " ".join(str(int(v)) for v in brms[:8]), flush=True)


def load_wav_16k(path: Path):
    """读 WAV -> 16kHz 单声道 float32。

    刻意不用 PyAV：PyAV 19 删掉了 av.open 的 metadata_errors 参数，
    而 faster-whisper 1.2.1 还在传它，一走 PyAV 就 TypeError。
    我们的 WAV 是自己写的（单声道 16bit），直接解最省事也最稳。
    """
    import numpy as np

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if sw != 2:
        raise ValueError(f"unsupported sampwidth {sw}")
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        n = int(round(len(a) * 16000 / rate))
        if n < 1:
            return a
        a = np.interp(np.arange(n) * (rate / 16000.0),
                      np.arange(len(a)), a).astype(np.float32)
    return a


def _hf_cached(name: str) -> bool:
    d = (Path.home() / ".cache" / "huggingface" / "hub"
         / ("models--Systran--faster-whisper-" + name) / "snapshots")
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def get_whisper():
    """懒加载 faster-whisper。返回模型或 None（未安装/加载失败）。"""
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                return None
            try:
                _whisper = WhisperModel(
                    WHISPER_MODEL, device="cpu", compute_type="int8",
                    cpu_threads=4, num_workers=1,
                    local_files_only=_hf_cached(WHISPER_MODEL))
                print(f"[ASR] model {WHISPER_MODEL} ready", flush=True)
            except Exception as e:            # noqa: BLE001
                print(f"[ASR] load failed: {e}", flush=True)
                print(f"[ASR] hint: WB_WHISPER_MODEL={WHISPER_MODEL} "
                      f"cached={_hf_cached(WHISPER_MODEL)}", flush=True)
                return None
        return _whisper


# Whisper 是"看着上文猜下文"的：喂一句普通话 + 简体的语域提示，
# 它就会往简体上靠。实测能把大部分繁体输出掰回来，剩下几个犄角旮旯的字
# 再由 _to_simplified 兜底 —— 固件字库是 GB2312 简体子集，
# 繁体字过不了 clean()，会在屏幕上直接变成空格（缺字）。
_ASR_PROMPT = "以下是普通话的日常对话，请用简体中文转写。"


def _to_simplified(s):
    """繁体 -> 简体兜底。zhconv 是纯 Python，没装也不影响主流程。"""
    if not s:
        return s
    try:
        from zhconv import convert
    except ImportError:
        return s
    try:
        return convert(s, "zh-cn")
    except Exception:                         # noqa: BLE001
        return s


def transcribe(wav_path: Path):
    m = get_whisper()
    if m is None:
        return None, "ASR offline"
    try:
        audio = load_wav_16k(wav_path)
        # beam_size 给到 5：一句就两三秒，多花的几十毫秒换更稳的字，值。
        segs, _info = m.transcribe(audio, language=WHISPER_LANG,
                                   beam_size=5, vad_filter=True,
                                   initial_prompt=_ASR_PROMPT,
                                   condition_on_previous_text=False)
        return _to_simplified("".join(s.text for s in segs).strip()), None
    except Exception as e:                    # noqa: BLE001
        return None, str(e)[:60]


# ===========================================================================
# 发送：WorkBuddy 本地网关 REST API
# ===========================================================================
# WorkBuddy GUI 自带一个本地 HTTP 网关（就是 `codebuddy --serve` 那一套）：
#   POST /api/v1/runs               发起一次 Agent 运行
#   GET  /api/v1/runs/:id/stream    SSE 读结果
# 认证用 GUI 进程内随机生成的密钥 —— 它只通过环境变量注入给对话子进程，
# **不落盘**，所以靠 gw_discover 从活着的子进程环境块里读出来，再扫端口定位。
#
# 为什么不用 `codebuddy -p`：那个是独立进程，没有账号登录态，
# 会一直停在 "Authentication required" 上（实测永久卡死，无超时价值）。

_GW = {"base": None, "password": None, "at": 0.0, "active": None}
_GW_TTL = 1800.0          # GUI 不重启则端口/密钥不变，缓存半小时
_SENDER_ID = "aipassport"  # 网关限流按 sender.id 分桶，默认桶名


def _device_sender(ble) -> str:
    """按设备 BLE 地址派生 sender id —— 每台设备独占一个限流桶。"""
    peer = (getattr(ble, "peer", "") or "").replace(":", "").replace("-", "")
    tail = peer[-6:]
    return f"aipassport-{tail}" if tail else _SENDER_ID
_gw_lock = threading.Lock()


def _discover(force=False):
    """惰性发现网关，缓存 5 分钟。返回 (base, password)。

    注意 find_gateway() 读不到密钥（如 GUI 正在重启、网关 sidecar 尚未
    拉起）时 discover_gateway() 返回 None 且不抛异常 —— 必须打日志，
    否则就是静默失败（实测踩过：用户按 OK 时撞上 GUI 重启窗口期）。
    """
    with _gw_lock:
        if (not force) and _GW["base"] and (time.time() - _GW["at"] < _GW_TTL):
            return _GW["base"], _GW["password"]
        try:
            import gw_discover
            # 多窗口场景（2026-10-15 实机踩坑）：两个 WorkBuddy 窗口 = 两个
            # GUI 网关实例，live 都非空，按端口序会抓到另一个窗口的网关。
            # 把心跳侧最新会话 id 传过去，让发现逻辑优先选 live 匹配的实例。
            hb_at, _cwd, hb_sid = latest_heartbeat()
            prefer = (str(hb_sid) if hb_sid and hb_at
                      and (time.time() * 1000 - hb_at) < 180_000 else None)
            info = gw_discover.discover_gateway(prefer_sid=prefer)
        except Exception as e:                        # noqa: BLE001
            print(f"[GW] discover failed: {type(e).__name__}: {e}", flush=True)
            info = None
        if info:
            _GW.update(base=info["base"], password=info["password"],
                       at=time.time(), active=None)
            print(f"[GW] {info['base']} pid={info['pid']} "
                  f"cached={info.get('cached')}", flush=True)
            return _GW["base"], _GW["password"]
        print("[GW] discover: no gateway found "
              "(GUI 重启中或 sidecar 未拉起，下轮重试)", flush=True)
        _GW.update(base=None, password=None, at=time.time())
        return None, None


_GW_ACTIVE = {"sid": None, "at": 0.0}
_GW_ACTIVE_TTL = 10.0          # 网关活跃会话的短缓存（秒）


def gateway_active_session():
    """当前 GUI 活会话 id（带 10s TTL 缓存）。

    权威来源是网关的 GET /api/v1/sessions/live（返回 {sessionId,
    writerOccupied}）——逆向自 WorkBuddy 网关内嵌 OpenAPI。
    历史教训：/status 的 activeSessionId 语义是"最近有 run 的会话"
    而非屏幕上开着的；本地心跳则会被 CLI host 子进程污染。live 端点
    三者皆无，实测返回的就是眼前窗口的会话。
    """
    now = time.monotonic()
    if now - _GW_ACTIVE["at"] < _GW_ACTIVE_TTL:
        return _GW_ACTIVE["sid"]
    sid = None
    base, pw = _discover()
    if base:
        req = urllib.request.Request(
            f"{base}/api/v1/sessions/live",
            headers={"Authorization": f"Bearer {pw}",
                     "X-CodeBuddy-Request": "1"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            sid = ((body or {}).get("data") or {}).get("sessionId") or None
        except Exception:                             # noqa: BLE001
            sid = None
    _GW_ACTIVE.update(sid=sid, at=now)
    return sid


_GW_SESS_CACHE = {"rows": None, "at": 0.0}
_GW_SESS_TTL = 5.0


_ROUTE = {"sid": None, "at": 0.0, "route": None}
_ROUTE_TTL = 15.0


def _route_to_instance(sid, force=False):
    """找到 live 恰好等于 sid 的网关实例，返回 (base, pw) 或 (None, None)。

    2026-10-15 实机踩坑：两个 WorkBuddy 窗口 = 两个网关实例，默认发现
    会抓错实例 —— 用户选了 A 对话，消息却投进 B 窗口的会话。多实例下
    必须按「对话 -> live 匹配的实例」路由，而不是随便挑一个网关。
    """
    now = time.monotonic()
    if (not force) and _ROUTE["sid"] == sid and now - _ROUTE["at"] < _ROUTE_TTL:
        return _ROUTE["route"]
    route = (None, None)
    try:
        import gw_discover
        for inst in gw_discover.discover_instances(prefer_sid=sid):
            if inst["live_sid"] == sid:
                route = (inst["base"], inst["password"])
                break
    except Exception as e:                        # noqa: BLE001
        print(f"[GW] route failed: {type(e).__name__}: {e}", flush=True)
    _ROUTE.update(sid=sid, at=now, route=route)
    if route[0]:
        print(f"[GW] routed {str(sid)[:8]} -> {route[0]}", flush=True)
    else:
        print(f"[GW] no instance live=={str(sid)[:8]}", flush=True)
    return route


def gateway_sessions(limit=10):
    """从网关读会话列表（比翻 sqlite 更准，含 GUI 刚建的会话）。带 5s 缓存。"""
    now = time.monotonic()
    if now - _GW_SESS_CACHE["at"] < _GW_SESS_TTL:
        rows = _GW_SESS_CACHE["rows"] or []
        return rows[:limit]
    rows = []
    base, pw = _discover()
    if base:
        req = urllib.request.Request(
            f"{base}/api/v1/sessions",
            headers={"Authorization": f"Bearer {pw}",
                     "X-CodeBuddy-Request": "1"})
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            rows = ((body or {}).get("data") or {}).get("sessions") or []
        except Exception:                             # noqa: BLE001
            rows = []
    _GW_SESS_CACHE.update(rows=rows, at=now)
    return rows[:limit]


def _post_run(base, pw, body, timeout=25):
    req = urllib.request.Request(
        f"{base}/api/v1/runs",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {pw}",
                 "X-CodeBuddy-Request": "1",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class _NotLive(Exception):
    """目标会话不是当前活会话（reply 端点 409）。"""


def _send_via_gateway(base, pw, session_id, text, sender_id, timeout=25):
    """把文本投进指定会话。走 POST /api/v1/sessions/{id}/reply。

    逆向结论（app.asar 内嵌 OpenAPI）：
      * POST /api/v1/runs = "发起 Agent 执行" —— 每次创建一个新后台任务，
        语音文本全变成任务名，消息永远不进对话（实机踩坑两轮）；
      * POST /api/v1/sessions/{id}/reply = "向当前活会话投递回复"，
        body {"text": ...}，返回 {"delivered": bool}，GUI 实时渲染。
    只接受"当前活会话"，投给别的会话返回 409 —— 抛 _NotLive 让上层
    重新解析目标。忙碌会话不受影响（writerOccupied=true 实测照常投递）。
    """
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/v1/sessions/{session_id}/reply",
        data=body,
        headers={"Authorization": f"Bearer {pw}",
                 "X-CodeBuddy-Request": "1",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as he:
        if he.code == 409:
            raise _NotLive(session_id) from None
        raise
    delivered = ((payload or {}).get("data") or {}).get("delivered")
    if not delivered:
        raise RuntimeError(f"not delivered: {str(payload)[:60]}")
    print(f"[GW] reply -> conv={str(session_id)[:8]} "
          f"sender={sender_id}", flush=True)
    return True


def send_to_workbuddy(session_id, text, timeout=25, sender_id=None,
                      route=None, strict=False):
    """把文本发给指定对话。返回 (ok, 回执文本)。

    route=(base,pw)：按对话路由到的网关实例（多窗口场景必须，
    2026-10-15 踩坑）。strict=True：工牌明确选中的对话 —— 409 时
    **不许**重定目标改投别的对话，如实报错。
    """
    if not text or not text.strip():
        return False, "empty text"
    sender = sender_id or _SENDER_ID
    if route and route[0]:
        base, pw = route
    else:
        base, pw = _discover()
        if not base:
            # 撞上 GUI 重启窗口期（sidecar 未拉起）时短暂等待再试一次，
            # 别把 "Gateway offline" 直接甩给用户（实测踩过）。
            for _ in range(3):
                time.sleep(3.0)
                base, pw = _discover(force=True)
                if base:
                    break
            if not base:
                return False, "Gateway offline"

    def attempt(b, p, target):
        try:
            _send_via_gateway(b, p, target, text, sender, timeout=timeout)
            return True, "Sent"
        except _NotLive:
            if strict:
                # 用户明确选中的对话不是当前 live 窗口 —— 绝不改投
                # 别的对话（2026-10-15：静默重定目标把消息投进了另
                # 一个窗口的会话）。
                return False, "Not live (picked conv)"
            # 目标不是当前活会话 -> 用 live 端点重定目标（缓存立即失效）
            _GW_ACTIVE.update(sid=None, at=0.0)
            live = gateway_active_session()
            if not live:
                # live 为空可能是抓到了 CLI 网关实例（本机多实例并存，
                # CLI 实例 live 恒 null）—— 强制重发现，发现逻辑会挑
                # live 非空的 GUI 实例
                b2, p2 = _discover(force=True)
                if not b2:
                    return False, "No active session"
                b, p = b2, p2
                _GW_ACTIVE.update(sid=None, at=0.0)
                live = gateway_active_session()
            if not live:
                return False, "No active session"
            if live == target:
                return False, "Not live (live==target)"
            print(f"[GW] 409, retarget {str(target)[:8]} -> {str(live)[:8]}",
                  flush=True)
            try:
                _send_via_gateway(b, p, live, text, sender, timeout=timeout)
                return True, "Sent"
            except _NotLive:
                return False, "Retarget still 409"
        except urllib.error.HTTPError as he:
            if he.code == 429:
                return False, "busy"
            if he.code == 401:
                return None, "auth expired"      # 密钥过期 -> 触发重发现
            return False, f"HTTP {he.code}"
        except urllib.error.URLError:
            # 连接层失败（拒绝/超时）：WorkBuddy 重启后网关会换端口，
            # 而内存缓存还抱着旧端口 —— 实测踩过：缓存 5 分钟内一直打死
            # 端口报 URLError。当成"需要重发现"处理，和 401 同路径。
            return None, "URLError"
        except Exception as e:                    # noqa: BLE001
            return False, f"{type(e).__name__}"[:36]

    last = (False, "unknown")
    for i, wait in enumerate((0.0, 2.5, 5.0)):    # 退避：立刻 -> 2.5s -> 5s
        if wait:
            time.sleep(wait)
        ok, msg = attempt(base, pw, session_id)
        if ok is None:                            # 401/URLError：重新发现/重路由
            if route and route[0]:
                nb, npw = _route_to_instance(session_id, force=True)
                if not nb:
                    return False, "Gateway offline"
                base, pw = nb, npw
            else:
                base, pw = _discover(force=True)
            if not base:
                return False, "Gateway offline"
            ok, msg = attempt(base, pw, session_id)
        if ok:
            return True, "Sent"
        last = (ok, msg)
        if msg != "busy":                         # 非并发问题就别重试了
            break
    return last[0], ("Busy, retry" if last[1] == "busy" else last[1])


def send_to_workbuddy_cli(session_id, text, timeout=90):
    """旧路径：本地 CLI。保留作兜底，但需要独立登录态，通常不可用。"""
    cli = CLI_DIR / "bin" / "codebuddy"
    if not cli.exists():
        return False, "CLI missing"
    if not NODE_EXE.exists():
        return False, "node missing"

    cmd = [str(NODE_EXE), str(cli), "-p", text, "--output-format", "text"]
    if session_id:
        cmd += ["-r", session_id]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, cwd=str(BRIDGE_DIR),
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as e:
        return False, str(e)[:36]

    out = (r.stdout or "").strip()
    if r.returncode == 0 and out:
        return True, out[:36]
    err = (r.stderr or "").strip() or out or f"exit {r.returncode}"
    return False, err[:36]


# ===========================================================================
# HTTP 调试端点
# ===========================================================================

class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        def qs(name, default=None):
            v = q.get(name)
            return v[0] if v else default

        try:
            if p in ("/", "/status"):
                st = compute_state()
                self._send({**st, "state_name": NAME_OF.get(st["state"], "?")})

            elif p == "/conversations":
                limit = int(qs("limit", LIST_MAX))
                offset = int(qs("offset", 0))
                self._send({"items": list_conversations(limit, offset),
                            "limit": limit, "offset": offset})

            elif p == "/messages":
                sid = qs("id")
                if not sid:
                    return self._send({"error": "missing id"}, 400)
                self._send(read_messages(sid, int(qs("page", 0))))

            elif p == "/usage":
                self._send(get_usage(qs("id")))

            elif p == "/health":
                try:
                    import faster_whisper  # noqa: F401
                    asr_installed = True
                except ImportError:
                    asr_installed = False
                self._send({"ok": True, "ts": int(time.time() * 1000),
                            "sources": {
                                "sessions": SESSIONS_DIR.exists(),
                                "db": WORKBUDDY_DB.exists(),
                                "projects": PROJECTS_DIR.exists(),
                            },
                            # loaded=模型已在内存；cached=权重已在本地
                            # （没缓存的话首次转写要拉 483MB）
                            "whisper": {"loaded": _whisper is not None,
                                        "installed": asr_installed,
                                        "model": WHISPER_MODEL,
                                        "cached": _hf_cached(WHISPER_MODEL)}})

            elif p == "/gw":
                info = dict(_GW)
                if info.get("password"):
                    info["password"] = "***"
                try:
                    import gw_discover
                    info["cached_port"] = gw_discover._load_cached_port()
                except Exception:                     # noqa: BLE001
                    info["cached_port"] = None
                self._send({"ok": bool(_GW.get("base")), "gateway": info})

            elif p == "/ble":
                b = _BLE
                self._send({
                    "available": bool(b and b.available),
                    "error": (b.last_error if b else "not_started"),
                    "connected": bool(b and b.connected),
                    "peer": (b.peer if b else None),
                    "mtu": (b.mtu if b else 0),
                    "pushed": (b.pushed if b else 0),
                    "received": (b.received if b else 0),
                    "voice_bytes": (len(b.voice_buf) if b else 0),
                    "selected": (b.selected if b else -1),
                })

            elif p == "/voice":
                files = sorted(VOICE_DIR.glob("*.wav"))[-8:] if VOICE_DIR.exists() else []
                self._send({"dir": str(VOICE_DIR),
                            "files": [f.name for f in files]})

            elif p == "/font":
                # 字库白名单诊断：确认哪些字符固件能渲染，以及净化效果
                samples = [
                    "介绍 AI Passport 有趣项目",
                    "🚀 修复登录 bug ✅ 完成",
                    "评估算力券补贴平台需求清单",
                    "带\t制表符\n和换行   多余空格",
                    "全角标点：测试。，！？——……",
                ]
                han = (len([c for c in _FONT_CHARS if 0x3400 <= ord(c) <= 0x9FFF])
                       if _FONT_CHARS else 0)
                self._send({
                    "loaded": _FONT_CHARS is not None,
                    "path": str(FONT_SYMBOLS),
                    "chars": len(_FONT_CHARS) if _FONT_CHARS else 0,
                    "han": han,
                    "ascii": (len([c for c in _FONT_CHARS
                                   if ord(c) < 0x80]) if _FONT_CHARS else 0),
                    "samples": [{"in": s, "out": clean(s)} for s in samples],
                })

            elif p == "/push":
                # 手动触发一次全量下发，便于调试
                if _BLE:
                    _BLE.push_all()
                self._send({"ok": bool(_BLE and _BLE.connected)})

            elif p == "/send":
                # 诊断：跳过 ASR，直接把文本经网关发给指定对话
                #   /send?text=hello[&conv=<sessionId>][&sender=<bucket>]
                text = (q.get("text") or [""])[0]
                conv = (q.get("conv") or [None])[0]
                sender = (q.get("sender") or [None])[0]
                t0 = time.time()
                ok, msg = send_to_workbuddy(conv, text, sender_id=sender)
                self._send({"ok": ok, "message": msg,
                            "elapsed": round(time.time() - t0, 2),
                            "conv": conv, "gateway": _GW.get("base")})

            else:
                self._send({"error": "not_found", "path": p}, 404)
        except Exception as e:                # noqa: BLE001
            self._send({"error": "internal", "message": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass


def run_http():
    # ThreadingHTTPServer：/send 内部有最长 ~7.5s 的退避重试，单线程
    # HTTPServer 会让诊断请求排队堵死（浏览器 tab 卡住表现为一直转圈）。
    srv = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), BridgeHandler)
    srv.daemon_threads = True
    srv.serve_forever()


# ===========================================================================
# BLE 中心（工牌做外设，本服务做中心）
# ===========================================================================

class BleCenter(threading.Thread):
    SVC_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
    CHR_DOWN = "0000ff01-0000-1000-8000-00805f9b34fb"   # write
    CHR_UP   = "0000ff02-0000-1000-8000-00805f9b34fb"   # notify
    DEV_NAME = "AI-Passport"

    def __init__(self):
        super().__init__(daemon=True)
        self.available = False
        self.last_error = None
        self.connected = False
        self.peer = None
        self.mtu = 0
        self.pushed = 0
        self.received = 0

        self.client = None
        self.tx_queue = queue.Queue()
        self.rx_queue = queue.Queue()

        self.convs = []
        self.selected = -1
        self.msg_page = 0

        self.voice_buf = bytearray()
        self.voice_rate = 8000
        self.voice_codec = VOICE_CODEC_PCM16
        self.voice_last_seq = None
        self.voice_lost = 0
        self.voice_truncated = False     # 达到 VOICE_MAX_SEC 被静默截断过
        self._vbuf_lock = threading.Lock()

        # 已转写但还没投递的文本（工牌确认屏用的那份）
        self.pending_text = ""
        self.pending_idx = -1

        # 连接后要自动全量推一次
        self._push_all_pending = False

        try:
            import bleak  # noqa: F401
            self.available = True
        except ImportError:
            self.last_error = "bleak not installed"

    # ---- 对外接口（线程安全）----

    def send(self, data: bytes):
        self.tx_queue.put(data)

    def push_all(self):
        self._push_all_pending = True

    # ---- 下行组装 ----

    def _build_all_frames(self):
        frames = []
        st = compute_state()
        frames.append(status_frame(st))

        convs = list_conversations(LIST_MAX, 0)
        self.convs = convs
        total = len(convs)
        for i, c in enumerate(convs):
            frames.append(conv_frame(i, total, c))

        usage = get_usage(st.get("conv_id"))
        frames.append(usage_frame(round(usage["used"] * 10),
                                  round(usage["size"] * 10)))
        model = st.get("model") or "WorkBuddy"
        frames.append(info_frame(model))
        return frames

    def _build_msg_frames(self, page):
        if not (0 <= self.selected < len(self.convs)):
            return []
        sid = self.convs[self.selected]["id"]
        r = read_messages(sid, page, MSG_PAGE_MAX)
        msgs = r["messages"]
        total = len(msgs)
        return [msg_frame(i, total, m) for i, m in enumerate(msgs)]

    # ---- 上行处理 ----

    def _on_notify(self, _sender, data: bytearray):
        if len(data) < 2 or data[0] != 0xBB:
            return
        cmd = data[1]
        pl = bytes(data[2:])
        self.received += 1

        if cmd == U_VOICE_DATA:
            # ADPCM 帧：seq16 + seed16 + 半字节流；PCM16 帧：seq16 + pcm[]
            if self.voice_codec == VOICE_CODEC_ADPCM:
                if len(pl) <= 4:
                    return
                seq = pl[0] | (pl[1] << 8)
                seed = pl[2] | (pl[3] << 8)
                if seed >= 0x8000:
                    seed -= 0x10000
                pcm = adpcm_decode(pl[4:], seed)
            elif len(pl) > 2:
                seq = pl[0] | (pl[1] << 8)
                pcm = pl[2:]
            else:
                return

            with self._vbuf_lock:
                if self.voice_last_seq is not None:
                    expect = (self.voice_last_seq + 1) & 0xFFFF
                    if seq != expect:
                        self.voice_lost += (seq - expect) & 0xFFFF
                self.voice_last_seq = seq
                if len(self.voice_buf) < VOICE_MAX_SEC * self.voice_rate * 2:
                    self.voice_buf += pcm
                else:
                    # 固件侧没有时长上限（设备屏上照常计时），PC 侧到点就
                    # 不再收包 —— 必须把"截断"显式告诉用户，否则他会以为
                    # 是 Whisper 转写不准弄丢了后半段。
                    self.voice_truncated = True
            return

        self.rx_queue.put((cmd, pl))

    # ---- 主循环 ----

    def run(self):
        if not self.available:
            return
        import asyncio
        asyncio.run(self._loop())

    async def _loop(self):
        import asyncio
        from bleak import BleakScanner, BleakClient

        last_status = 0.0
        while True:
            try:
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, ad: self.DEV_NAME in (ad.local_name or "")
                    or (d.name or "").startswith(self.DEV_NAME),
                    timeout=8.0)
                if not dev:
                    await asyncio.sleep(3)
                    continue

                async with BleakClient(dev) as client:
                    self.client = client
                    self.connected = True
                    self.peer = dev.address
                    self.last_error = None
                    self.mtu = getattr(client, "mtu_size", 0) or 0
                    print(f"[BLE] connected {dev.address} mtu={self.mtu}", flush=True)

                    await client.start_notify(self.CHR_UP, self._on_notify)
                    self._push_all_pending = True

                    while client.is_connected:
                        if self._push_all_pending:
                            self._push_all_pending = False
                            for f in self._build_all_frames():
                                await self._write(f)
                                await asyncio.sleep(0.02)
                            last_status = time.time()

                        now = time.time()
                        if now - last_status >= POLL_INTERVAL:
                            await self._write(status_frame(compute_state()))
                            last_status = now

                        while not self.tx_queue.empty():
                            f = self.tx_queue.get_nowait()
                            await self._write(f)
                            await asyncio.sleep(0.01)

                        await asyncio.sleep(0.05)
            except Exception as e:            # noqa: BLE001
                self.connected = False
                self.peer = None
                self.client = None
                self.last_error = str(e)[:120]
                await asyncio.sleep(4)

    async def _write(self, data: bytes):
        try:
            await self.client.write_gatt_char(self.CHR_DOWN, data, response=False)
            self.pushed += 1
        except Exception as e:                # noqa: BLE001
            self.last_error = str(e)[:120]


# ===========================================================================
# 工作线程：消费上行事件（转写、发送都在这儿，避免阻塞 BLE 循环）
# ===========================================================================

def bridge_worker(ble: BleCenter):
    while True:
        cmd, pl = ble.rx_queue.get()
        # 整个 worker 包一层兜底：任何一条事件处理抛异常（numpy 缺失、
        # 数组越界、写盘失败……）都不能让线程死掉 —— 死了之后所有按键/
        # 录音/发送永久失效，而 /ble 还报 connected=true，极难排查。
        try:
            _dispatch(ble, cmd, pl)
        except Exception:                         # noqa: BLE001
            import traceback
            print(f"[RX] handler error cmd=0x{cmd:02x}:", flush=True)
            traceback.print_exc()


def _dispatch(ble: BleCenter, cmd: int, pl: bytes):
    if cmd == U_REQ:
        what = pl[0] if len(pl) >= 1 else REQ_ALL
        page = pl[1] if len(pl) >= 2 else 0
        print(f"[RX] req what={what} page={page}", flush=True)
        if what in (REQ_ALL, REQ_LIST):
            ble.push_all()
        elif what == REQ_MSG:
            ble.msg_page = page
            for f in ble._build_msg_frames(page):
                ble.send(f)
        elif what == REQ_USAGE:
            st = compute_state()
            u = get_usage(st.get("conv_id"))
            ble.send(usage_frame(round(u["used"] * 10), round(u["size"] * 10)))

    elif cmd == U_SELECT:
        if len(pl) >= 1:
            ble.selected = pl[0]
            print(f"[RX] select conv={ble.selected}", flush=True)

    elif cmd == U_VOICE_START:
        with ble._vbuf_lock:
            ble.voice_buf = bytearray()
        ble.voice_rate = (pl[0] | (pl[1] << 8)) if len(pl) >= 2 else 8000
        ble.voice_codec = pl[2] if len(pl) >= 3 else VOICE_CODEC_PCM16
        ble.voice_last_seq = None
        ble.voice_lost = 0
        ble.voice_truncated = False
        print(f"[RX] voice start @{ble.voice_rate}Hz "
              f"codec={'ADPCM' if ble.voice_codec else 'PCM16'}", flush=True)

    elif cmd == U_VOICE_END:
        with ble._vbuf_lock:
            n = len(ble.voice_buf)
        tag = " (TRUNCATED@%ds)" % VOICE_MAX_SEC if ble.voice_truncated else ""
        print(f"[RX] voice end, {n} bytes, seq_lost={ble.voice_lost}{tag}",
              flush=True)

    elif cmd == U_VOICE_ABORT:
        with ble._vbuf_lock:
            ble.voice_buf = bytearray()
        print("[RX] voice abort", flush=True)

    elif cmd == U_SEND:
        idx = pl[0] if len(pl) >= 1 else ble.selected
        mode = pl[1] if len(pl) >= 2 else SEND_AUTO
        handle_voice_send(ble, idx, mode)

    elif cmd == U_CONFIRM:
        idx = pl[0] if pl else ble.selected
        handle_confirm(ble, idx)

    elif cmd == U_KEY:
        pass  # 诊断用，忽略


def _pick_target_session(ble: BleCenter, idx: int):
    """决定这句话投到哪个对话、走哪个网关实例。

    返回 (session_id, 来源标签, route)；route=(base,pw) 为按对话路由到
    的实例，(None,None) 表示用默认发现。

    投递走 reply 端点，只接受该实例的"当前活会话"。多窗口下每个实例
    各有自己的 live —— 所以用户在工牌上明确选中的对话是最高意图：
    找 live 恰好等于它的实例路由过去；找不到就如实报错，**绝不偷偷
    改投别的对话**（2026-10-15 踩坑：409 重定目标把消息投进了另一
    个窗口的会话）。

    优先级：
      1. 工牌选中的 conv —— 显式意图，strict
      2. 本地心跳 —— 隐式跟随（latest_heartbeat 已过滤 CLI host）
      3. 网关 live —— 无任何意图时跟随
    """
    if 0 <= idx < len(ble.convs):
        sid = str(ble.convs[idx]["id"])
        return sid, "device", _route_to_instance(sid)
    hb_at, _cwd, hb_sid = latest_heartbeat()
    if hb_sid and hb_at and (time.time() * 1000 - hb_at) < 180_000:
        sid = str(hb_sid)
        return sid, "heartbeat", _route_to_instance(sid)
    active = gateway_active_session()
    if active:
        return str(active), "live", (None, None)
    if ble.convs:
        return str(ble.convs[0]["id"]), "fallback", (None, None)
    return None, "none", (None, None)


def _is_session_busy(sid):
    """会话是否正忙（agent 在跑）。查本地 DB，只读、零配额。"""
    if not sid:
        return False
    con = _read_db()
    if con is None:
        return False
    try:
        row = con.execute("SELECT status FROM sessions WHERE id = ?",
                          (sid,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return bool(row) and row[0] == "working"


_BUSY_WAIT_MAX = 120         # 最长等目标会话空闲的时间（秒）
_BUSY_POLL = 3.0


def _deliver(ble: BleCenter, idx: int, text: str):
    """真正投递一句话，并把回执推给工牌。

    ⚠️ 实测（11:44，有 jsonl 证据）：reply 端点对忙碌会话返回
    delivered:true 但消息**静默丢弃**（消息文件零痕迹）；只有空闲
    窗口才真正落库（「测试语音收录。」在我回复间隙投递成功落库，
    「测试语音输入。」在我处理中投递被丢）。GUI 自己的输入队列在
    内存里，外部够不着 —— 所以只能等空闲再投。
    """
    sid, how, route = _pick_target_session(ble, idx)
    if not sid:
        ble.send(sent_frame(False, "No active session"))
        print("[SEND] no target session", flush=True)
        ble.push_all()
        return
    if how == "device" and not route[0]:
        # 工牌明确选中的对话不是任何实例的 live（窗口没开着它）
        # —— 如实报错。绝不改投别的对话（2026-10-15 踩坑）。
        ble.send(sent_frame(False, "Conv not in live window"))
        print(f"[SEND] device-picked conv {str(sid)[:8]} is not live "
              f"on any instance", flush=True)
        ble.push_all()
        return

    waited = 0.0
    while _is_session_busy(sid) and waited < _BUSY_WAIT_MAX:
        if waited == 0.0:
            print(f"[SEND] target busy, waiting idle: {str(sid)[:8]}",
                  flush=True)
        time.sleep(_BUSY_POLL)
        waited += _BUSY_POLL

    ok, reply = send_to_workbuddy(sid, text, sender_id=_device_sender(ble),
                                  route=route, strict=(how == "device"))
    if ok:
        ble.send(sent_frame(True, reply or "Sent"))
        ble.selected = idx
        print(f"[SEND] ok -> conv={str(sid)[:8]} via {how} "
              f"(waited {waited:.0f}s)", flush=True)
    elif _is_session_busy(sid):
        print(f"[SEND] aborted, target still busy: {str(sid)[:8]}",
              flush=True)
        ble.send(sent_frame(False, "Session busy"))
    else:
        print(f"[SEND] failed ({how}): {reply}", flush=True)
        ble.send(sent_frame(False, reply))
    ble.push_all()


def handle_voice_send(ble: BleCenter, idx: int, mode: int = SEND_AUTO):
    """语音闭环：存 WAV -> Whisper -> (回显等确认 | 直接投递) -> 回执"""
    with ble._vbuf_lock:
        pcm = bytes(ble.voice_buf)
        ble.voice_buf = bytearray()

    if len(pcm) < 1600:            # 少于 0.1 秒
        ble.send(asr_frame(False, "Audio too short"))
        ble.send(sent_frame(False, "Audio too short"))
        return

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    wav = VOICE_DIR / f"{ts}.wav"
    try:
        save_wav(pcm, wav, ble.voice_rate)
    except OSError as e:
        ble.send(asr_frame(False, f"Save failed: {e}"[:36]))
        ble.send(sent_frame(False, f"Save failed: {e}"[:36]))
        return
    print(f"[VOICE] saved {wav.name} ({len(pcm)} bytes)", flush=True)
    diag_audio(pcm, ble.voice_rate, tag=wav.name)

    text, err = transcribe(wav)
    if err:
        ble.send(asr_frame(False, f"ASR: {err}"[:36]))
        ble.send(sent_frame(False, f"ASR: {err}"[:36]))
        print(f"[ASR] failed: {err}", flush=True)
        return
    if not text:
        ble.send(asr_frame(False, "No speech detected"))
        ble.send(sent_frame(False, "No speech detected"))
        return
    print(f"[ASR] {text}", flush=True)
    if ble.voice_truncated:
        print(f"[VOICE] 注意：录音超过 {VOICE_MAX_SEC}s，已截断", flush=True)

    # 留着给确认屏用：用户可能半小时后才按 OK
    ble.pending_text = text
    ble.pending_idx = idx

    if mode == SEND_CONFIRM:
        echo = (f"截断@{VOICE_MAX_SEC}s！{text}" if ble.voice_truncated
                else text)
        ble.send(asr_frame(True, echo))
        print(f"[ASR] echoed to device ({len(echo)} chars), awaiting OK",
              flush=True)
        return

    ble.pending_text = ""
    _deliver(ble, idx, text)


def handle_confirm(ble: BleCenter, idx: int):
    """工牌在确认屏上按了 OK：投递刚才缓存的那份转写。

    投递文本加 [语音] 前缀：GUI 对外部注入的消息不渲染实时气泡
    （逆向结论：用户气泡只由 GUI 自己发送时本地绘制，agent 侧
    runDefault 不向 GUI 流发 user_message_chunk），所以对话里
    只能看到回复看不到原话 —— 前缀让 Agent 与回放历史里都能
    一眼识别这是工牌语音。
    """
    text = ble.pending_text
    if not text:
        ble.send(sent_frame(False, "Nothing to send"))
        print("[CONFIRM] no pending transcript", flush=True)
        return
    ble.pending_text = ""
    if idx < 0:
        idx = ble.pending_idx
    print(f"[CONFIRM] accepted idx={idx}", flush=True)
    _deliver(ble, idx, f"[语音] {text}")


# ===========================================================================
# 主循环
# ===========================================================================

_BLE = None


def main():
    global _BLE
    VOICE_DIR.mkdir(parents=True, exist_ok=True)

    # 先在主线程把 ASR 加载完，再拉 BLE / HTTP 线程。
    # 踩过的坑：在子线程里 load ctranslate2 的模型，会在一个已经有多线程
    # （bleak 的 WinRT 线程 + HTTP 线程）的进程里 access violation ——
    # faulthandler 抓到的栈就是 WhisperModel.__init__ -> ctranslate2.models.Whisper。
    # 主线程加载则一直稳定，代价只是启动多等 ~1.4s。
    if _hf_cached(WHISPER_MODEL):
        t0 = time.time()
        if get_whisper() is not None:
            print(f"[ASR] warm-up {time.time() - t0:.1f}s（主线程）", flush=True)
    else:
        print("[ASR] 权重未缓存：首次「录音->发送」时会先下载再加载", flush=True)

    threading.Thread(target=run_http, daemon=True).start()
    ble = BleCenter()
    _BLE = ble
    ble.start()
    threading.Thread(target=bridge_worker, args=(ble,), daemon=True).start()

    print("wb-bridge v3 启动（双向）", flush=True)
    print(f"  心跳   : {SESSIONS_DIR}", flush=True)
    print(f"  数据库 : {WORKBUDDY_DB}", flush=True)
    print(f"  消息   : {PROJECTS_DIR}", flush=True)
    print(f"  语音   : {VOICE_DIR}", flush=True)
    print(f"  BLE    : {'就绪' if ble.available else '不可用 - ' + str(ble.last_error)}",
          flush=True)
    print(f"  ASR    : faster-whisper/{WHISPER_MODEL} "
          f"cached={_hf_cached(WHISPER_MODEL)}", flush=True)
    print(flush=True)
    print(f"  HTTP   : http://127.0.0.1:{HTTP_PORT}/", flush=True)
    print("    /status /conversations /messages?id= /usage /health /ble /voice",
          flush=True)
    print("    /gw   网关状态（端口/密钥/缓存）", flush=True)
    print("    /send?text=&conv=&sender=   直接发一条（跳过 ASR，诊断用）",
          flush=True)
    print("    /push 手动全量下发", flush=True)
    print(flush=True)

    try:
        while True:
            ts = datetime.now().strftime("%H:%M:%S")
            if ble.connected:
                print(f"[{ts}] BLE {ble.peer} mtu={ble.mtu} "
                      f"pushed={ble.pushed} recv={ble.received}", flush=True)
            else:
                st = compute_state()
                print(f"[{ts}] {NAME_OF[st['state']]:8s} "
                      f"task={(st['task'] or '-')[:28]}", flush=True)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()

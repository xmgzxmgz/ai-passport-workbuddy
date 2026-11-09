import json
import os
import re
import time

# 会话文件路径由环境变量提供，不再硬编码本机绝对路径：
#   WB_SESSION_JSONL=~/.workbuddy/projects/<project>/<session>.jsonl python session_probe.py
DEFAULT_PATH = os.environ.get("WB_SESSION_JSONL", "")


def user_msgs(path=None, tail=20):
    """列出会话里真实用户消息 (时间, 文本)，已剔除 system-reminder。"""
    path = os.path.expanduser(path or DEFAULT_PATH)
    if not path:
        raise SystemExit(
            "未指定会话文件。请用环境变量传入，例如：\n"
            "  WB_SESSION_JSONL=~/.workbuddy/projects/<project>/<session>.jsonl python session_probe.py"
        )
    out = []
    for ln in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("type") != "message" or d.get("role") != "user":
            continue
        c = d.get("content") or []
        txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        txt = re.sub(r"<system-reminder[\s\S]*?</system-reminder>", "", txt)
        txt = re.sub(r"<[^>]+>", "", txt).strip()
        out.append((d.get("timestamp", 0), txt))
    return out[-tail:]


if __name__ == "__main__":
    rows = user_msgs()
    print("真实 user 消息数:", len(rows))
    for ts, txt in rows:
        print(time.strftime("%H:%M:%S", time.localtime(ts / 1000)), "|",
              txt[:110].replace("\n", " ") or "(仅系统提示)")

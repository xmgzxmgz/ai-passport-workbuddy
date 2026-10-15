import json
import re
import time

P = ("/Users/xiamuguizhi/.workbuddy/projects/"
     "Users-xiamuguizhi-WorkBuddy-2026-10-15-08-59-10/"
     "fb0f0677-3969-4f4d-819a-25b9fe8b93fb.jsonl")


def user_msgs(path=P, tail=20):
    """列出会话里真实用户消息 (时间, 文本)，已剔除 system-reminder。"""
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

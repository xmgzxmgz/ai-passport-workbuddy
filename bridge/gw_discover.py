"""从运行中的 WorkBuddy 子进程里读取网关凭据。

WorkBuddy 的本地网关密钥是进程内随机生成、并**只通过环境变量注入给对话子进程**的
（`getGatewaySecret()` -> `CODEBUDDY_GATEWAY_PASSWORD`），不落盘。
所以想从外部（比如我们的 BLE 中转）调用网关，就必须从活着的子进程环境块里把它读出来。

实现：Win32 PEB 遍历
  OpenProcess -> NtQueryInformationProcess(ProcessBasicInformation)
  -> ReadProcessMemory(PEB+0x20 = ProcessParameters)
  -> ReadProcessMemory(ProcessParameters+0x80 = Environment)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# PEB 与 RTL_USER_PROCESS_PARAMETERS 在 x64 下的关键偏移
PEB_PROCESS_PARAMETERS_OFF = 0x20
RTL_ENVIRONMENT_OFF = 0x80
# Environment 上限，够放下几十个变量。
# 注意：必须**分块**读。环境块后面紧邻的页往往未映射，
# 一次性大块 ReadProcessMemory 会以 ERROR_PARTIAL_COPY(299) 整体失败。
ENV_MAX_BYTES = 256 * 1024
ENV_CHUNK = 4096


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def _read(handle, addr, size):
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf,
                                    ctypes.c_size_t(size), ctypes.byref(read))
    if not ok:
        return None
    return buf.raw[:read.value]


def _read_env_block(handle, addr, max_bytes=ENV_MAX_BYTES, chunk=ENV_CHUNK):
    """分块读取环境块，直到读到未映射页或遇到结束标记。

    环境块以 UTF-16LE 的空项（4 个 \\x00）结尾；紧邻的页通常未映射，
    所以一旦某块读失败就停，把已读到的部分返回。
    """
    out = bytearray()
    while len(out) < max_bytes:
        got = _read(handle, addr + len(out), chunk)
        if not got:
            break
        out += got
        if len(out) >= 4 and out[-4:] == b"\x00\x00\x00\x00":
            break
    return bytes(out) if out else None


def read_process_env(pid: int) -> dict[str, str] | None:
    """读取指定 pid 的环境变量。权限不足或进程已退出时返回 None。"""
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        status = ntdll.NtQueryInformationProcess(
            ctypes.c_void_p(handle), ctypes.c_int(0),
            ctypes.byref(pbi), ctypes.sizeof(pbi), None)
        if status != 0 or not pbi.PebBaseAddress:
            return None

        raw = _read(handle, pbi.PebBaseAddress + PEB_PROCESS_PARAMETERS_OFF, 8)
        if not raw:
            return None
        params_addr = int.from_bytes(raw, "little")
        if not params_addr:
            return None

        env_addr_raw = _read(handle, params_addr + RTL_ENVIRONMENT_OFF, 8)
        if not env_addr_raw:
            return None
        env_addr = int.from_bytes(env_addr_raw, "little")
        if not env_addr:
            return None

        blob = _read_env_block(handle, env_addr)
        if not blob:
            return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    # 环境块是 UTF-16LE，每项 "K=V\0"，整块以空项结尾
    text = blob.decode("utf-16-le", errors="ignore")
    env: dict[str, str] = {}
    for chunk in text.split("\x00"):
        if not chunk or "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        if k.startswith("="):        # Windows 的 "=C:" 驱动器变量，跳过
            continue
        env[k] = v
    return env


def list_workbuddy_subprocesses() -> list[dict]:
    """列出可能持有网关凭据的 WorkBuddy 进程（Electron 主/子进程）。"""
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match 'WorkBuddy' } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Depth 3"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        rows = json.loads(r.stdout or "[]")
    except Exception:                                # noqa: BLE001
        return []
    if isinstance(rows, dict):
        rows = [rows]
    return rows


def find_gateway():
    """在所有 WorkBuddy 进程里找网关凭据。返回 dict 或 None。"""
    for proc in list_workbuddy_subprocesses():
        pid = proc.get("ProcessId")
        if not pid:
            continue
        env = read_process_env(pid)
        if not env:
            continue
        pw = env.get("CODEBUDDY_GATEWAY_PASSWORD")
        proxy = env.get("CODEBUDDY_SERVICE_PROXY_URL", "")
        if not pw:
            continue
        base = ""
        if proxy:
            # http://127.0.0.1:55196/internal/hooks/services/invoke -> http://127.0.0.1:55196
            parts = proxy.split("/")
            if len(parts) >= 3:
                base = "/".join(parts[:3])
        return {"pid": pid, "password": pw, "base": base, "proxy": proxy,
                "host": env.get("CODEBUDDY_HOST", "")}
    return None


def listening_ports() -> list[int]:
    """本机所有 LISTENING 的 TCP 端口（去重）。"""
    import re
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, errors="replace", timeout=20).stdout
    except Exception:                                 # noqa: BLE001
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        m = re.match(r"^(?:127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[::\]):(\d+)$",
                     parts[1])
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def probe_gateway(base: str, password: str, timeout=1.5):
    """探测某个 base 是不是网关。返回 /api/v1/status 的 data 或 None。"""
    import urllib.request
    req = urllib.request.Request(
        f"{base}/api/v1/status",
        headers={"Authorization": f"Bearer {password}",
                 "X-CodeBuddy-Request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                 # noqa: BLE001
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and data.get("status") == "ok":
        return data
    return None


def find_gateway_port(timeout=1.2):
    """无认证扫描，定位网关端口。

    **必须不认证**：网关对需要认证的端点做了令牌桶限流
    （`RateLimiter(2, 60s)` + `RateLimiter(12, 1h)`，按 sender.id），
    拿密码挨个端口试会把配额烧光，真实请求就被 429 拒了。
    未认证访问 `/api/v1/status` 会得到唯一的 401 + `AUTH_REQUIRED`，
    用它认端口，零配额消耗。
    """
    import urllib.request
    import urllib.error
    from concurrent.futures import ThreadPoolExecutor

    ports = listening_ports()
    if not ports:
        return None

    def probe(port):
        url = f"http://127.0.0.1:{port}/api/v1/status"
        try:
            req = urllib.request.Request(
                url, headers={"X-CodeBuddy-Request": "1"})
            with urllib.request.urlopen(req, timeout=timeout):
                return None
        except urllib.error.HTTPError as e:
            if e.code != 401:
                return None
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:                         # noqa: BLE001
                return None
            if "AUTH_REQUIRED" in body or "Authentication required" in body:
                return port
            return None
        except Exception:                             # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as pool:
        for hit in pool.map(probe, ports):
            if hit:
                return hit
    return None


def _cache_path():
    return Path(__file__).resolve().parent / ".gw_cache.json"


def _load_cached_port():
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        port = int(data.get("port"))
        return port if port else None
    except Exception:                                 # noqa: BLE001
        return None


def _save_cached_port(port):
    try:
        _cache_path().write_text(
            json.dumps({"port": port, "at": time.time()}), encoding="utf-8")
    except Exception:                                 # noqa: BLE001
        pass


def discover_gateway(verbose=False):
    """完整发现：读凭据（PEB）-> 定位端口（无认证）-> 组装 base。

    返回 {"base","password","pid","host"} 或 None。
    端口每次 GUI 启动随机分配，但缓存后可直接命中，避免反复扫描。
    """
    cred = find_gateway()
    if not cred:
        if verbose:
            print("[gw] 未能从任何 WorkBuddy 进程读到网关密钥")
        return None
    pw = cred["password"]

    # 1) 缓存端口快路径：确认它还是那个网关（依旧不带认证）
    cached = _load_cached_port()

    def is_gateway(port):
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/status",
                headers={"X-CodeBuddy-Request": "1"})
            with urllib.request.urlopen(req, timeout=1.2):
                return False
        except urllib.error.HTTPError as e:
            return e.code == 401
        except Exception:                             # noqa: BLE001
            return False

    if cached and is_gateway(cached):
        if verbose:
            print(f"[gw] 端口缓存命中 {cached}")
        return {"base": f"http://127.0.0.1:{cached}", "password": pw,
                "pid": cred["pid"], "host": cred["host"], "cached": True}

    # 2) 全端口扫描
    if verbose:
        print(f"[gw] 扫描 {len(listening_ports())} 个本地端口（无认证）…")
    port = find_gateway_port()
    if not port:
        if verbose:
            print("[gw] 未发现网关")
        return None

    _save_cached_port(port)
    if verbose:
        print(f"[gw] 命中网关 127.0.0.1:{port}")
    return {"base": f"http://127.0.0.1:{port}", "password": pw,
            "pid": cred["pid"], "host": cred["host"], "cached": False}


if __name__ == "__main__":
    found = discover_gateway(verbose=True)
    if not found:
        print("NOT FOUND（可能需要管理员权限，或 WorkBuddy 未运行）")
        sys.exit(1)
    safe = dict(found)
    safe["password"] = found["password"][:6] + f"…(len={len(found['password'])})"
    print(json.dumps(safe, ensure_ascii=False, indent=2))


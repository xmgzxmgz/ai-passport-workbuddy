"""定位 WorkBuddy 本地网关（跨平台）。

WorkBuddy 的本地网关密钥是进程内随机生成、并**只通过环境变量注入给对话子进程**的
（`getGatewaySecret()` -> `CODEBUDDY_GATEWAY_PASSWORD`），不落盘。
所以想从外部（比如 BLE 中转）调用网关，就必须从活着的子进程环境里把它读出来。

各平台取进程环境的办法：

  Windows   PEB 遍历（OpenProcess -> NtQueryInformationProcess -> ReadProcessMemory）
  macOS     `ps -Eww -p <pid>`   —— 同用户免 root 可读（实测）
  Linux     /proc/<pid>/environ  —— 同用户可读

端口发现（无认证扫 401）本身是跨平台的，只有「列监听端口」这一步要用各平台命令：

  Windows   netstat -ano
  macOS     lsof -nP -iTCP -sTCP:LISTEN
  Linux     lsof 或 ss

顺带一提：如果本服务本身就是被 WorkBuddy 拉起来的子进程，那么它自己的环境里
就有密钥，直接读 `os.environ` 即可 —— 这是零成本的快路径，所有平台通用。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# 我们真正需要的键；其余环境变量读出来也没用
WANTED_KEYS = (
    "CODEBUDDY_GATEWAY_PASSWORD",
    "CODEBUDDY_SERVICE_PROXY_URL",
    "CODEBUDDY_HOST",
)

_GW_PASSWORD_KEY = "CODEBUDDY_GATEWAY_PASSWORD"

# Windows PEB 偏移与读取上限
PEB_PROCESS_PARAMETERS_OFF = 0x20
RTL_ENVIRONMENT_OFF = 0x80
ENV_MAX_BYTES = 256 * 1024
ENV_CHUNK = 4096


# ===========================================================================
# 进程环境读取
# ===========================================================================

def env_from_self() -> dict[str, str] | None:
    """快路径：本进程自己的环境里就有密钥（被 WorkBuddy 拉起时）。"""
    if not os.environ.get(_GW_PASSWORD_KEY):
        return None
    return {k: os.environ.get(k, "") for k in WANTED_KEYS}


# ---- Windows：PEB 遍历 ------------------------------------------------------
# 注意：ctypes.WinDLL / ctypes.wintypes 在非 Windows 上不可用，
# 所以这些 import 必须留在函数体内，不能在模块顶部 —— 否则 macOS 上 import 本模块就崩。

def _win_read_process_env(pid: int) -> dict[str, str] | None:
    import ctypes

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
        got = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf,
                                        ctypes.c_size_t(size), ctypes.byref(got))
        return buf.raw[:got.value] if ok else None

    def _read_env_block(handle, addr):
        """分块读，直到读到未映射页或遇到结束标记。

        环境块后面紧邻的页往往未映射，一次性大块 ReadProcessMemory 会以
        ERROR_PARTIAL_COPY(299) 整体失败，所以必须分块。
        """
        out = bytearray()
        while len(out) < ENV_MAX_BYTES:
            got = _read(handle, addr + len(out), ENV_CHUNK)
            if not got:
                break
            out += got
            if len(out) >= 4 and out[-4:] == b"\x00\x00\x00\x00":
                break
        return bytes(out) if out else None

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

        env_raw = _read(handle, params_addr + RTL_ENVIRONMENT_OFF, 8)
        if not env_raw:
            return None
        env_addr = int.from_bytes(env_raw, "little")
        if not env_addr:
            return None

        blob = _read_env_block(handle, env_addr)
        if not blob:
            return None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))

    text = blob.decode("utf-16-le", errors="ignore")
    env: dict[str, str] = {}
    for item in text.split("\x00"):
        if not item or "=" not in item:
            continue
        k, _, v = item.partition("=")
        if k.startswith("="):        # Windows 的 "=C:" 驱动器变量
            continue
        env[k] = v
    return env


# ---- macOS：ps -Eww --------------------------------------------------------

_ENV_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S*)")


def _mac_read_process_env(pid: int) -> dict[str, str] | None:
    """macOS 上 `ps -Eww -p <pid>` 会把环境变量追加在命令行之后，全部空格分隔。

    没法可靠地切分「参数」和「环境」，但我们只要几个固定的键，直接正则捞就够。
    实测同用户免 root 可读，不需要 sudo。
    """
    try:
        r = subprocess.run(["ps", "-Eww", "-p", str(pid)],
                           capture_output=True, text=True, errors="replace",
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    env = {k: v for k, v in _ENV_TOKEN.findall(r.stdout or "")}
    return env or None


# ---- Linux：/proc/<pid>/environ -------------------------------------------

def _linux_read_process_env(pid: int) -> dict[str, str] | None:
    try:
        blob = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    env: dict[str, str] = {}
    for item in blob.decode("utf-8", "replace").split("\x00"):
        if not item or "=" not in item:
            continue
        k, _, v = item.partition("=")
        env[k] = v
    return env or None


def read_process_env(pid: int) -> dict[str, str] | None:
    """读取指定 pid 的环境变量。权限不足或进程已退出时返回 None。"""
    if IS_WIN:
        try:
            return _win_read_process_env(pid)
        except Exception:                             # noqa: BLE001
            return None
    if IS_MAC:
        return _mac_read_process_env(pid)
    if IS_LINUX:
        return _linux_read_process_env(pid)
    return None


# ===========================================================================
# 进程枚举
# ===========================================================================

def list_workbuddy_subprocesses() -> list[dict]:
    """列出可能持有网关凭据的 WorkBuddy 进程。"""
    if IS_WIN:
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
        except Exception:                             # noqa: BLE001
            return []
        if isinstance(rows, dict):
            rows = [rows]
        return rows

    # macOS / Linux：ps -axww 拿 pid + 完整命令行，再按关键字过滤
    try:
        r = subprocess.run(["ps", "-axww", "-o", "pid=,command="],
                           capture_output=True, text=True, errors="replace",
                           timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or "WorkBuddy" not in line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        rows.append({"ProcessId": pid, "CommandLine": cmd.strip()})
    return rows


def find_gateway():
    """在所有 WorkBuddy 进程里找网关凭据。返回 dict 或 None。"""
    # 快路径：本进程环境（被 WorkBuddy 拉起时直接命中，零成本）
    env = env_from_self()
    if env:
        return {"pid": os.getpid(), "password": env[_GW_PASSWORD_KEY],
                "base": "", "proxy": env.get("CODEBUDDY_SERVICE_PROXY_URL", ""),
                "host": env.get("CODEBUDDY_HOST", "")}

    for proc in list_workbuddy_subprocesses():
        pid = proc.get("ProcessId")
        if not pid:
            continue
        env = read_process_env(pid)
        if not env:
            continue
        pw = env.get(_GW_PASSWORD_KEY)
        if not pw:
            continue
        proxy = env.get("CODEBUDDY_SERVICE_PROXY_URL", "")
        base = ""
        if proxy:
            # http://127.0.0.1:55196/internal/hooks/services/invoke -> http://127.0.0.1:55196
            parts = proxy.split("/")
            if len(parts) >= 3:
                base = "/".join(parts[:3])
        return {"pid": pid, "password": pw, "base": base, "proxy": proxy,
                "host": env.get("CODEBUDDY_HOST", "")}
    return None


# ===========================================================================
# 端口
# ===========================================================================

_NETSTAT_PORT = re.compile(r"^(?:127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[::\]):(\d+)$")
_LSOF_PORT = re.compile(
    r"((?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+\]|\*):(\d+))\s*\(LISTEN\)")


def listening_ports() -> list[int]:
    """本机所有 LISTENING 的 TCP 端口（去重、升序）。"""
    if IS_WIN:
        try:
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, errors="replace", timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        ports: set[int] = set()
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            m = _NETSTAT_PORT.match(parts[1])
            if m:
                ports.add(int(m.group(1)))
        return sorted(ports)

    # macOS / Linux：lsof
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                           capture_output=True, text=True, errors="replace",
                           timeout=25)
    except (OSError, subprocess.SubprocessError):
        return []
    ports = set()
    for line in (r.stdout or "").splitlines():
        if "(LISTEN)" not in line:
            continue
        m = _LSOF_PORT.search(line)
        if m:
            ports.add(int(m.group(2)))
    return sorted(ports)


def probe_gateway(base: str, password: str, timeout=1.5):
    """探测某个 base 是不是网关。返回 /api/v1/status 的 data 或 None。"""
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


def _is_gateway_unauthed(port: int, timeout=1.2) -> bool:
    """无认证探一个端口是不是网关。

    **必须不认证**：网关对需要认证的端点做了令牌桶限流
    （`RateLimiter(2, 60s)` + `RateLimiter(12, 1h)`，按 sender.id），
    拿密码挨个端口试会把配额烧光，真实请求就被 429 拒了。
    未认证访问 `/api/v1/status` 会得到唯一的 401 + `AUTH_REQUIRED`，
    用它认端口，零配额消耗。
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/status",
            headers={"X-CodeBuddy-Request": "1"})
        with urllib.request.urlopen(req, timeout=timeout):
            return False
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return False
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:                             # noqa: BLE001
            return False
        return "AUTH_REQUIRED" in body or "Authentication required" in body
    except Exception:                                 # noqa: BLE001
        return False


def find_gateway_ports(password=None, timeout=1.2):
    """返回**所有**网关实例端口。

    实机发现：GUI 和各 CLI 会话会各起一个网关实例，只返回一个会抓错
    （抓到 CLI 的，live=null，投递一律无效）。上层用 /sessions/live 甄别。
    """
    from concurrent.futures import ThreadPoolExecutor

    ports = listening_ports()
    if not ports:
        return []

    with ThreadPoolExecutor(max_workers=min(32, len(ports))) as pool:
        verdicts = list(pool.map(_is_gateway_unauthed, ports))
    candidates = sorted(p for p, ok in zip(ports, verdicts) if ok)

    if len(candidates) <= 1:
        return candidates
    if password:
        # 多候选时用密钥甄别（消耗一次配额，仅发现阶段发生）
        return [p for p in candidates
                if probe_gateway(f"http://127.0.0.1:{p}", password) is not None]
    return candidates


def find_gateway_port(password=None, timeout=1.2):
    """定位网关端口（返回第一个候选）。

    .. deprecated:: 多实例场景下请用 find_gateway_ports + live 甄别，
       见 discover_gateway。
    """
    ports = find_gateway_ports(password=password, timeout=timeout)
    return ports[0] if ports else None


# ===========================================================================
# 端口缓存
# ===========================================================================

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


# ===========================================================================
# 对外主入口
# ===========================================================================

def _live_session_on(port: int, password: str, timeout=1.5):
    """该端口网关的 /api/v1/sessions/live sessionId。

    非网关、密钥不符或当前无活会话都返回 None。
    实机发现：本机会同时存在多个网关实例（GUI 一个、各 CLI 会话各一个），
    只有 GUI 那个的 live 非空 —— 这是甄别实例的唯一可靠特征。
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/sessions/live",
        headers={"Authorization": f"Bearer {password}",
                 "X-CodeBuddy-Request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                 # noqa: BLE001
        return None
    return ((body or {}).get("data") or {}).get("sessionId") or None


def discover_gateway(verbose=False):
    """完整发现：读凭据 -> 定位端口 -> 组装 base。

    返回 {"base","password","pid","host"} 或 None。
    端口每次 GUI 启动随机分配，但缓存后可直接命中，避免反复扫描。

    实机踩坑：本机可能同时有多个网关实例（GUI + 各 CLI 会话），扫到的
    第一个很可能是 CLI 的（live=null），投递给它一律 409/无效。所以
    候选多于一个时，用 /sessions/live 非空来甄别 GUI 实例。
    """
    cred = find_gateway()
    if not cred:
        if verbose:
            print("[gw] 未能从任何 WorkBuddy 进程读到网关密钥")
            if IS_MAC:
                print("[gw] macOS 提示：需与 WorkBuddy 同一用户运行；"
                      "某些系统策略下还需给终端「完全磁盘访问」权限")
        return None
    pw = cred["password"]

    def pick(cands):
        """从候选端口里挑 GUI 实例（live 非空）；全空则返回第一个。"""
        for p in cands:
            sid = _live_session_on(p, pw)
            if sid:
                if verbose:
                    print(f"[gw] {p} live={str(sid)[:8]} <- GUI 网关")
                return p
        return cands[0] if cands else None

    cached = _load_cached_port()
    if cached and _is_gateway_unauthed(cached):
        port = pick([cached])
        if port and _live_session_on(port, pw):
            if verbose:
                print(f"[gw] 端口缓存命中 {cached}")
            return {"base": f"http://127.0.0.1:{port}", "password": pw,
                    "pid": cred["pid"], "host": cred["host"], "cached": True}
        # 缓存实例存在但 live 为空（可能是 CLI 网关）—— 继续扫描更好的

    if verbose:
        print(f"[gw] 扫描 {len(listening_ports())} 个本地端口（无认证）…")
    ports = find_gateway_ports(password=pw)
    port = pick(ports) if ports else None
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
        print("NOT FOUND（WorkBuddy 未运行，或权限不足）")
        sys.exit(1)
    safe = dict(found)
    safe["password"] = found["password"][:6] + f"…(len={len(found['password'])})"
    print(json.dumps(safe, ensure_ascii=False, indent=2))

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


def find_gateway(skip_self=False):
    """在所有 WorkBuddy 进程里找网关凭据。返回 dict 或 None。

    skip_self=True 时跳过本进程环境（用于继承密钥过期后的重扫）。
    """
    if not skip_self:
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
    """端口缓存文件位置。

    默认写在代码旁边；从 .app（可能在只读卷）启动时由启动器用
    WB_GW_CACHE 指到可写位置。缓存只是加速，坏掉也不影响正确性。
    """
    env = os.environ.get("WB_GW_CACHE")
    if env:
        return Path(env).expanduser()
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

def endpoint_for_session(sid, max_age=900.0):
    """会话 -> 网关地址：直接读会话心跳文件里写着的 endpoint。

    2026-10-15 17:40 决定性发现（"Conv not in live window" 的真根因）：
    空闲期**所有**网关实例对 /status 匿名（activeSessionId 不返回），
    /sessions/live 也是 null —— 于是 live/owner 两条路由键同时失效。
    而工牌按 OK 恰恰都发生在会话空闲期，所以路由十次九败。

    而 `~/.workbuddy/sessions/<pid>.json` 里本来就写着：

        {"sessionId": "fb0f0677-...", "url": "http://127.0.0.1:53403",
         "endpoint": "http://127.0.0.1:53403", "updatedAt": 1792056992339}

    这就是 sessionId -> 网关 的**权威映射，与忙闲无关**。实测
    fb0f0677 -> 53403，与 /status 忙时报告的实例完全一致。

    max_age：心跳新鲜度上限（秒），避免拿到早已退出会话的陈旧端口。
    """
    if not sid:
        return None
    now_ms = time.time() * 1000
    cands = []
    root = Path("~/.workbuddy/sessions").expanduser()
    try:
        files = list(root.glob("*.json"))
    except OSError:
        return None
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:                             # noqa: BLE001
            continue
        if d.get("sessionId") != sid:
            continue
        base = d.get("endpoint") or d.get("url")
        if not base:
            continue
        hb = d.get("lastHeartbeat") or d.get("updatedAt") or 0
        if hb and max_age and (now_ms - hb) > max_age * 1000:
            continue                                  # 陈旧心跳，别用
        # 同一个 sessionId 可能有多个心跳文件：GUI 窗口的（cwd 是项目目录）
        # 和 CLI host 子进程的（cwd 在 /private/var/.../workbuddy-host-cli/
        # 临时目录）。投递要走 GUI 那个，否则消息进了 CLI 自己的会话，
        # 屏幕上什么都不会出现。
        cwd = str(d.get("cwd") or "")
        is_cli_host = "workbuddy-host-cli" in cwd or cwd.startswith("/private/var/folders")
        cands.append((1 if is_cli_host else 0, -float(hb or 0), str(base).rstrip("/")))
    if not cands:
        return None
    cands.sort()                                      # 非 CLI host 优先，再取最新
    return cands[0][2]


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


def _owner_session_on(port: int, password: str, timeout=1.5):
    """该端口网关 /status 的 activeSessionId —— 网关所属窗口的会话。

    2026-10-15 实测关键发现：/sessions/live 只在有 writer 附着（agent
    run 进行中）时非空，**会话空闲时全实例 live=None** —— 按 live 路由
    在空闲期必然失联（工牌消息恰恰多发于空闲期）。/status 的
    activeSessionId 稳定标识该网关归属哪个会话，空闲时也在，作为兜底
    路由键。无该字段的实例（CLI host）返回 None。
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/status",
        headers={"Authorization": f"Bearer {password}",
                 "X-CodeBuddy-Request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                 # noqa: BLE001
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        return data.get("activeSessionId") or None
    return None


def discover_instances(verbose=False, prefer_sid=None):
    """发现**所有**认证可用的网关实例，返回实例列表。

    每项 {"base","password","pid","host","live_sid","owner_sid"}：
      * live_sid：/sessions/live（writer 附着时非空，权威）
      * owner_sid：/status 的 activeSessionId（空闲时也在，兜底）
    排序：live==prefer_sid > owner==prefer_sid > live 非空 > 其余。
    全部端口都认证不过时自动放弃自环境密钥、改扫活进程重试。
    """
    for skip_self in (False, True):
        cred = find_gateway(skip_self=skip_self)
        if not cred:
            if verbose:
                print(f"[gw] 无可用凭据 (skip_self={skip_self})")
            continue
        pw = cred["password"]
        ports = find_gateway_ports(password=pw)
        insts = []
        for p in ports:
            live = _live_session_on(p, pw)
            owner = live if live else _owner_session_on(p, pw)
            insts.append({"base": f"http://127.0.0.1:{p}", "password": pw,
                          "pid": cred["pid"], "host": cred["host"],
                          "live_sid": live, "owner_sid": owner})
        if not insts:
            # 这份密钥一个端口都认证不过 -> 下一轮 skip_self 扫活进程
            if verbose:
                print(f"[gw] 密钥(pid={cred['pid']})对 {len(ports)} 个候选"
                      f"全失败" if ports else f"[gw] 无候选端口")
            continue

        def rank(i):
            if prefer_sid:
                if i["live_sid"] == prefer_sid:
                    return 0
                if i["owner_sid"] == prefer_sid:
                    return 1
            return 2 if i["live_sid"] else 3

        insts.sort(key=rank)
        if insts and insts[0]["owner_sid"]:
            try:
                _save_cached_port(
                    int(insts[0]["base"].rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                pass
        if verbose:
            for i in insts:
                print(f"[gw] instance {i['base']} live="
                      f"{str(i['live_sid'])[:8]} owner={str(i['owner_sid'])[:8]}")
        return insts
    return []


def discover_gateway(verbose=False, prefer_sid=None):
    """完整发现：读凭据 -> 定位端口 -> 组装 base。返回单实例 dict 或 None。

    基于 discover_instances()：live 非空的排前，live==prefer_sid 的更前。
    """
    insts = discover_instances(verbose=verbose, prefer_sid=prefer_sid)
    if not insts:
        if verbose:
            print("[gw] 未发现网关")
        return None
    best = insts[0]
    return {"base": best["base"], "password": best["password"],
            "pid": best["pid"], "host": best["host"], "cached": False}


# ===========================================================================
# 投递候选：会话 -> 应该把 reply 发到哪个网关实例（2026-10-15 18:20 定稿）
# ===========================================================================
#
# 为什么需要这一层
# ----------------
# 逆向网关（app.asar 内嵌 OpenAPI + sessions 控制器源码）拿到权威语义：
#
#   async replySession(id, body) {
#     const cur = this.sessionManager.sessionSubject.value;   // 本实例装载的会话
#     if (!canDeliverFollowReply(cur?.id, id) || !cur)        // cur.id === id
#       409 SESSION_FOLLOW_NOT_LIVE;
#     await this.agentService.runDefault(buildFollowReplyItems(text, cur.id),
#                                        { context: cur, inputOrigin: "human" });
#   }
#
# 即：**只有把 reply 发到"恰好装载着目标会话"的那一个网关实例才会成功，
# 发到别的实例一律 409**（好在 409 是干净的 —— 不会污染别的对话）。
# 所以问题彻底归结为「sessionId -> 实例」的映射准不准。
#
# 单靠 live / owner 两条键不够：两者都来自网关的**运行时状态**，会话空闲
# 时都可能缺席或指向别处（工牌按 OK 绝大多数发生在空闲期，于是十投九败，
# 报 "Conv not in live window"）。
#
# 现在改用 5 条互相独立的键，键与键之间取"最先命中"，全部失败还有兜底：
#
#   0  heartbeat endpoint 且 owner==sid     心跳写死了 endpoint，最硬
#   1  /info.cwd == 会话 cwd 且 owner==sid  两个独立来源同时确认
#   2  /info.cwd == 会话 cwd                工作目录唯一，**与忙闲无关**
#   3  /status.activeSessionId == sid       运行时归属
#   4  /sessions/live.sessionId == sid      writer 附着时才有
#   5  /sessions 列表里含 sid               该实例至少认识这个会话
#   9  其余实例                             最后手段，靠 409 过滤 + 落库验真
#
# 其中 2 是这次新增、也是真正解决问题的一条：/api/v1/info 返回该实例的
# `cwd`（工作目录），而会话心跳文件里本来就写着同一个 cwd。两边都对得上，
# 就把"会话"钉在了"实例"上，跟会话忙不忙、writer 在不在都没关系。

def _api(port, password, path, timeout=1.5):
    """带认证 GET 一个网关端点，返回解析后的 JSON（dict）或 None。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/{path}",
        headers={"Authorization": f"Bearer {password}",
                 "X-CodeBuddy-Request": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:                                  # noqa: BLE001
        return None


def _data_of(obj):
    d = (obj or {}).get("data")
    return d if isinstance(d, dict) else {}


def session_cwd(sid):
    """会话的工作目录（cwd）—— 用于把会话钉到实例上，ACP 的 session/resume
    更是**必须**带上它（缺了直接 400 Invalid params: cwd 必填）。

    四个独立来源，按可靠性从高到低：
      1. ~/.workbuddy/sessions/*.json 心跳里的 cwd（快，带 kind 可排掉 CLI host）
      2. ~/.workbuddy/workbuddy.db 的 sessions.cwd（最准，会话关着也有）
      3. 会话 jsonl 前几行里的 cwd（首行是 session-meta，通常没有 cwd，
         真正的 cwd 在第 2~3 行）
      4. 都没有 -> None
    """
    if not sid:
        return None
    best = None
    root = Path("~/.workbuddy/sessions").expanduser()
    try:
        files = list(root.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if d.get("sessionId") != sid:
            continue
        cwd = str(d.get("cwd") or "")
        if not cwd:
            continue
        # CLI host 子进程会为同一个 sid 写另一份心跳，cwd 在临时目录里
        is_cli_host = ("workbuddy-host-cli" in cwd
                       or cwd.startswith("/private/var/folders"))
        if is_cli_host:
            if best is None:
                best = cwd                            # 只有 CLI 的也先记着
            continue
        return cwd
    if best:
        return best
    cwd = _cwd_from_db(sid)
    if cwd:
        return cwd
    # 心跳和 DB 都没有时退回 jsonl —— 注意首行是 session-meta（无 cwd），
    # 真实 cwd 在第一条 message 记录上，所以要前几行一起扫。
    try:
        hits = list(Path("~/.workbuddy/projects").expanduser()
                    .glob(f"*/{sid}.jsonl"))
    except OSError:
        hits = []
    for p in hits:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for _, line in zip(range(8), fh):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    cwd = d.get("cwd") or (d.get("meta") or {}).get("cwd")
                    if cwd:
                        return str(cwd)
        except Exception:                              # noqa: BLE001
            continue
    return None


def _cwd_from_db(sid):
    """从 workbuddy.db 的 sessions 表读 cwd（只读打开，失败一律 None）。"""
    import sqlite3
    home = Path(os.environ.get("WB_HOME") or "~/.workbuddy").expanduser()
    db = home / "workbuddy.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            row = con.execute("SELECT cwd FROM sessions WHERE id=?",
                              (sid,)).fetchone()
        finally:
            con.close()
        return str(row[0]) if row and row[0] else None
    except Exception:                                  # noqa: BLE001
        return None


def gateway_cwd(port, password, timeout=1.5):
    """该实例的工作目录（/api/v1/info 的 data.cwd）。实例强标识。"""
    return str(_data_of(_api(port, password, "info", timeout)).get("cwd") or "")


def gateway_status_of(port, password, timeout=1.5):
    """该实例 /status 的 data（含 activeSessionId / busy / runStatus）。"""
    return _data_of(_api(port, password, "status", timeout))


_CAND = {"sid": None, "at": 0.0, "rows": []}
_CAND_TTL = 4.0                 # 候选缓存：太短会打爆探测，太长会错过重绑定


def deliver_candidates(sid, force=False, verbose=False):
    """返回把 reply 投给 sid 的候选实例列表（已按可信度升序排序）。

    每项 {"base","password","why","score","owner","live","cwd"}。
    空列表 = 连一个网关都找不到。
    """
    now = time.monotonic()
    if (not force) and _CAND["sid"] == sid and now - _CAND["at"] < _CAND_TTL:
        return _CAND["rows"]
    rows = _candidates_impl(sid, verbose=verbose)
    _CAND.update(sid=sid, at=now, rows=rows)
    return rows


def _candidates_impl(sid, verbose=False):
    cred = find_gateway(skip_self=False) or find_gateway(skip_self=True)
    if not cred:
        if verbose:
            print("[gw] 无凭据，候选为空")
        return []
    pw = cred["password"]
    ports = find_gateway_ports(password=pw)
    if not ports:
        return []
    hb_cwd = session_cwd(sid)
    hb_ep = (endpoint_for_session(sid) or "").rstrip("/")
    rows = []
    for p in ports:
        base = f"http://127.0.0.1:{p}"
        cwd = gateway_cwd(p, pw)
        st = gateway_status_of(p, pw)
        owner = st.get("activeSessionId") or None
        live = _live_session_on(p, pw)
        score, why = 9, ["fallback"]
        if owner == sid:
            score, why = 3, ["owner"]
        if cwd and hb_cwd and cwd == hb_cwd:
            if owner == sid:
                score, why = 1, ["cwd+owner"]
            elif score > 2:
                score, why = 2, ["cwd"]
            else:
                why = why + ["cwd"]
        if live == sid and score > 4:
            score, why = 4, ["live"]
        if hb_ep and base == hb_ep:
            if not owner or owner == sid:
                score, why = 0, ["hb-endpoint"]
            else:
                score, why = 5, ["hb-endpoint(owner 不符)"]
        if score == 9:
            # 兜底键：该实例的会话列表里有这个会话（说明它至少认识）
            try:
                rowsj = _data_of(_api(p, pw, "sessions")).get("sessions") or []
                if any(str(r.get("id")) == sid for r in rowsj):
                    score, why = 6, ["listed"]
            except Exception:                          # noqa: BLE001
                pass
        is_cli_host = "workbuddy-host-cli" in cwd
        rows.append({"base": base, "password": pw, "score": score,
                     "why": "+".join(why), "owner": owner, "live": live,
                     "cwd": cwd, "port": p, "cli_host": is_cli_host})
    # CLI host 实例永远排在最后（它的会话属于 CLI，不是屏幕上的窗口）
    rows.sort(key=lambda r: (r["score"] + (5 if r["cli_host"] else 0), r["port"]))
    if verbose:
        print(f"[gw] 会话 {str(sid)[:8]} cwd={hb_cwd}")
        for r in rows:
            print(f"[gw]   {r['base']} score={r['score']} [{r['why']}] "
                  f"owner={str(r['owner'])[:8]} live={str(r['live'])[:8]}")
    return rows



# ---------------------------------------------------------------------------
# reply 端点的硬语义：**只服务该实例当前装载着的那一个会话**，投给别的会话
# 一律 409，且不会落到任何地方（干净）。所以"这条会话现在投不投得进去"只有
# 一个判据：有没有哪个实例把它当作 activeSessionId / live.sessionId。
#
# 反过来不成立 —— 窗口空闲时实例会匿名（activeSessionId=null），此时表里没有
# 它也未必真投不进去，所以这张表**只能用来判"投不进去"，不能判"投得进去"**。
# 后者永远以 reply 的 200 + 回读落库为准。
# ---------------------------------------------------------------------------

_LIVE = {"at": 0.0, "map": {}}
_LIVE_TTL = 5.0


def live_sessions(force=False):
    """{session_id: base} —— 当前真正装载在某个窗口里的会话。带 5s 缓存。

    面板 1.5s 轮询一次 /conversations，而这里每个端口要打两次 HTTP，所以
    必须缓存；投递失败判定时会传 force=True 拿当下真值。
    """
    now = time.monotonic()
    if (not force) and now - _LIVE["at"] < _LIVE_TTL:
        return _LIVE["map"]
    m = {}
    try:
        for inst in discover_instances():
            sid = inst.get("live_sid") or inst.get("owner_sid")
            if sid:
                m.setdefault(str(sid), inst["base"])
    except Exception:                                 # noqa: BLE001
        pass
    _LIVE.update(at=now, map=m)
    return m


def is_live(sid, force=False):
    """这条会话是否装载在某个窗口里（能投的前提）。"""
    if not sid:
        return False
    return str(sid) in live_sessions(force=force)


# ---------------------------------------------------------------------------
# ACP 通道 —— 唯一**不依赖"哪个窗口装载着这条会话"**的投递方式。
#
# 2026-10-20 从 app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs 内嵌的
# OpenAPI 里挖出来的官方协议：
#
#   POST /api/v1/acp/connect              -> {connectionId, sessionToken}
#   POST /api/v1/acp  (JSON-RPC, SSE 回)  header: acp-connection-id
#        initialize
#        session/resume {sessionId, cwd}  加载会话运行态（不回放历史，实测 0.0s）
#        session/prompt {sessionId, prompt:[{type:"text", text}]}
#                                         发用户消息，等本轮 Agent 跑完
#
# 实机证据（2026-10-20 10:48，会话 01a13d94，窗口**没有**打开它）：
#   reply 全实例 409 -> ACP session/resume 200 -> session/prompt 200
#   / end_turn 3.7s -> 会话 jsonl 从 1575B 涨到 4981B，出现 role=user 的
#   这条消息 + assistant 的回复。也就是说"窗口没开这个对话"不再是死路。
#
# 代价：session/prompt 同步等到 end_turn（小会话 3.7s，几十 MB 的大会话
# 更久），所以调用方要么放后台线程、要么用"回读落库"当判据而不是等它返回。
# ---------------------------------------------------------------------------


class AcpClient:
    """一个 ACP 连接：connect -> initialize -> resume -> prompt。"""

    def __init__(self, port, password, timeout=15):
        self.port = int(port)
        self.pw = password
        self.cid = None
        self.timeout = timeout

    # -- 底层 ---------------------------------------------------------
    def _headers(self, extra=None):
        h = {"Authorization": f"Bearer {self.pw}",
             "X-CodeBuddy-Request": "1",
             "Content-Type": "application/json",
             # 少这个头 initialize 直接 406：
             # "Client must accept both application/json and text/event-stream"
             "Accept": "application/json, text/event-stream"}
        if extra:
            h.update(extra)
        return h

    def connect(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/acp/connect",
            data=b"{}", method="POST", headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        self.cid = (body or {}).get("connectionId")
        if not self.cid:
            raise RuntimeError(f"no connectionId: {str(body)[:80]}")
        return self.cid

    def rpc(self, rid, method, params, timeout=90):
        """发一条 JSON-RPC，从 SSE 流里挑出 id 相同的 result/error。"""
        payload = {"jsonrpc": "2.0", "id": rid,
                   "method": method, "params": params}
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/acp",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers=self._headers({"acp-connection-id": self.cid}))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                for raw in r:                     # 逐行读，别整包 read
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        obj = json.loads(line[6:])
                    except ValueError:
                        continue
                    if obj.get("id") != rid:
                        continue                  # 期间的通知/流式块
                    if "error" in obj:
                        return False, obj["error"]
                    return True, obj.get("result")
        except urllib.error.HTTPError as he:
            return False, {"http": he.code,
                           "body": he.read().decode("utf-8", "replace")[:200]}
        except Exception as e:                    # noqa: BLE001
            return False, {"error": f"{type(e).__name__}: {e}"}
        return False, {"error": "no result in stream"}

    # -- 语义 ---------------------------------------------------------
    def open_session(self, sid, cwd):
        ok, err = self.rpc(1, "initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "wb-bridge", "version": "1.0"},
            "clientCapabilities": {"fs": {"readTextFile": False,
                                          "writeTextFile": False},
                                   "terminal": False}})
        if not ok:
            return False, f"initialize: {str(err)[:120]}"
        ok, err = self.rpc(2, "session/resume",
                           {"sessionId": sid, "cwd": cwd}, timeout=90)
        if not ok:
            # 老版本网关可能只认 session/load（会回放历史，慢但能用）
            ok, err = self.rpc(4, "session/load",
                               {"sessionId": sid, "cwd": cwd}, timeout=180)
            if not ok:
                return False, f"resume: {str(err)[:140]}"
        return True, "resumed"

    def prompt(self, sid, text, timeout=120):
        return self.rpc(3, "session/prompt",
                        {"sessionId": sid,
                         "prompt": [{"type": "text", "text": text}]},
                        timeout=timeout)


_ACP_CACHE = {"at": 0.0, "port": None}          # 上次跑通的端口，省一次探测


def _acp_pick_ports(prefer=None):
    """候选实例端口（真正能不能用由 acp_deliver 按 cwd 过滤）。"""
    ports = []
    if prefer:
        ports.append(int(prefer))
    try:
        for p in find_gateway_ports(password=find_gateway()["password"]):
            if p not in ports:
                ports.append(p)
    except Exception:                                 # noqa: BLE001
        pass
    cached = _ACP_CACHE.get("port")
    if cached and cached in ports:
        ports.remove(cached)
        ports.insert(0, cached)
    return ports


def acp_deliver(sid, text, cwd=None, prefer_port=None, password=None,
                prompt_timeout=120, verbose=False):
    """用 ACP 把一句话发进会话 —— **不需要窗口焦点**。

    返回 (ok, info)。ok=True 只说明 Agent 本轮跑完（result.stopReason），
    是否真落库仍建议调用方回读 jsonl 复核。

    ⚠️ 必须选 **cwd 与目标会话相同**的实例（2026-10-20 实测踩坑）：
    ACP 的 session/resume 虽然要求传 cwd，但真正决定"会话文件写到哪个
    projects/<slug>/ 下"的是**网关实例自己的 cwd**。传给 53584
    （cwd=2026-10-09 项目）去发一个属于 2026-10-19 项目的会话，它会在
    2026-10-09 目录下新建一个同名 sid 的 jsonl —— 消息确实进去了，但进的
    是"另一个项目里的同名会话"，用户在原来那个对话里一个字都看不到。
    所以宁可失败，也不能投错地方。
    """
    if not sid or not (text or "").strip():
        return False, "empty"
    cwd = cwd or session_cwd(sid)
    if not cwd:
        return False, "no cwd (无法定位会话所属项目)"
    pw = password or (find_gateway() or {}).get("password")
    if not pw:
        return False, "no gateway credential"

    matched, seen = [], []
    for port in _acp_pick_ports(prefer_port):
        try:
            inst_cwd = gateway_cwd(port, pw)
        except Exception:                             # noqa: BLE001
            continue
        seen.append(f"{port}={inst_cwd.rsplit('/', 1)[-1] or '?'}")
        if inst_cwd == cwd:
            matched.append(port)
        elif verbose:
            print(f"[ACP] skip {port}: 实例 cwd={inst_cwd} != 会话 {cwd}",
                  flush=True)
    if not matched:
        return False, f"该项目没有打开的窗口({','.join(seen[:3]) or '无实例'})"

    last = "no gateway port"
    for port in matched:
        try:
            client = AcpClient(port, pw)
            client.connect()
        except Exception as e:                        # noqa: BLE001
            last = f"{port}: connect {type(e).__name__}"
            continue
        ok, info = client.open_session(sid, cwd)
        if not ok:
            last = f"{port}: {info}"
            if verbose:
                print(f"[ACP] {last}", flush=True)
            continue
        _ACP_CACHE.update(at=time.monotonic(), port=port)
        ok, res = client.prompt(sid, text, timeout=prompt_timeout)
        if not ok:
            return False, f"prompt: {str(res)[:140]}"
        stop = (res or {}).get("stopReason")
        if verbose:
            print(f"[ACP] port={port} stop={stop} msgId="
                  f"{str((res or {}).get('userMessageId'))[:8]}", flush=True)
        return True, f"acp/{port}/{stop or 'ok'}"
    return False, last


if __name__ == "__main__":
    found = discover_gateway(verbose=True)
    if not found:
        print("NOT FOUND（WorkBuddy 未运行，或权限不足）")
        sys.exit(1)
    safe = dict(found)
    safe["password"] = found["password"][:6] + f"…(len={len(found['password'])})"
    print(json.dumps(safe, ensure_ascii=False, indent=2))

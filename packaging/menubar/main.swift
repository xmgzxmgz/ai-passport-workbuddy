// ---------------------------------------------------------------------------
// WBBridge 菜单栏应用
//
// 之前的启动器是 shell 脚本（LSUIElement 后台进程）：双击没反应、不知道
// 有没有在跑、也没法停 —— 用户视角就是"打不开"。这个版本换成真 NSStatusItem：
//
//   · 菜单栏常驻一个波形图标，形状/颜色反映服务与工牌状态
//   · 点开就是状态面板：服务 pid、工牌 peer/MTU、对话条数
//   · 菜单里能打开日志、重启、停止、退出
//   · 自己拉起 python 子进程 wb_bridge.py，并转发 SIGTERM
//   · 如果 8765 已被别的进程占用（例如终端里手动跑的），进入"接管"模式：
//     不重复启动，只做监控 + 提供停止
//
// 编译：swiftc -O -swift-version 5 -framework AppKit main.swift -o WBBridge
// ---------------------------------------------------------------------------

import AppKit
import Foundation

let HOME = NSHomeDirectory()
let WB_HOME = HOME + "/.workbuddy"
let LOG_PATH = WB_HOME + "/wb_bridge.log"
let PID_PATH = WB_HOME + "/wb_bridge.pid"
let PORT = 8765

// MARK: - 状态快照

struct Snapshot {
    var serviceUp = false
    var pid: Int32 = 0
    var adopted = false          // 端口被外部进程占用（不是我们拉起的）
    var peer: String = ""
    var bleConnected = false
    var mtu = 0
    var convCount = -1
    var checkedAt = Date.distantPast

    var bleLine: String {
        if bleConnected {
            let short = peer.count > 17 ? String(peer.prefix(17)) : peer
            return "工牌：已连接 \(short) · MTU \(mtu)"
        }
        return "工牌：未连接"
    }

    var serviceLine: String {
        if !serviceUp { return "服务：未运行" }
        let who = adopted ? "外部进程" : "本应用拉起"
        return "服务：运行中 · pid \(pid) · \(who)"
    }
}

// MARK: - 应用

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {

    private var statusItem: NSStatusItem!
    private var child: Process?
    private var logHandle: FileHandle?
    private var snapshot = Snapshot()
    private var refreshTimer: Timer?
    private var shownMissingDepsAlert = false

    // --- 路径 -------------------------------------------------------------

    /// 代码目录：打包后在 Resources/bridge；源码目录里直接跑时在 .app 上一级。
    private var bridgeDir: URL? {
        let fm = FileManager.default
        if let res = Bundle.main.resourceURL {
            let packed = res.appendingPathComponent("bridge")
            if fm.fileExists(atPath: packed.appendingPathComponent("wb_bridge.py").path) {
                return packed
            }
        }
        let dev = Bundle.main.bundleURL.deletingLastPathComponent()
        if fm.fileExists(atPath: dev.appendingPathComponent("wb_bridge.py").path) {
            return dev
        }
        return nil
    }

    // --- 生命周期 ---------------------------------------------------------

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        try? FileManager.default.createDirectory(atPath: WB_HOME + "/voice_out",
                                                withIntermediateDirectories: true)
        setupStatusItem()
        ensureWritableDirs()
        startService()
        refreshNow()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.refreshNow()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        // 重复双击 app 时把菜单弹出来，至少让用户看到"我在这儿"
        statusItem.button?.performClick(nil)
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        stopService(removePidFile: true)
    }

    // 关窗口不退出
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // --- 菜单栏 -----------------------------------------------------------

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "waveform.circle",
                                   accessibilityDescription: "WBBridge")
            button.image?.isTemplate = true
            button.toolTip = "WBBridge 工牌中转"
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        refreshSnapshotSync()

        menu.addItem(disabled(snapshot.serviceLine))
        if snapshot.serviceUp {
            menu.addItem(disabled(snapshot.bleLine))
        }
        if snapshot.convCount >= 0 {
            menu.addItem(disabled("对话：\(snapshot.convCount) 条"))
        }

        menu.addItem(.separator())

        if snapshot.serviceUp {
            menu.addItem(item("重启服务", #selector(restartService)))
            menu.addItem(item("停止服务", #selector(stopServiceAction)))
            menu.addItem(item("打开诊断页", #selector(openDiagPage)))
        } else {
            menu.addItem(item("启动服务", #selector(startServiceAction)))
        }
        menu.addItem(item("打开日志", #selector(openLogAction)))

        menu.addItem(.separator())
        menu.addItem(item("退出 WBBridge", #selector(quitApp), key: "q"))
    }

    private func item(_ title: String, _ action: Selector, key: String = "") -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        it.target = self
        return it
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        it.isEnabled = false
        return it
    }

    // --- 状态刷新 ---------------------------------------------------------

    /// 菜单打开时同步取一次（本地环回，几十毫秒）。用短超时，避免服务挂了
    /// 时候点菜单要转好几秒。
    private func refreshSnapshotSync() {
        let snap = probe(quick: true)
        DispatchQueue.main.async { self.apply(snap) }
    }

    private func refreshNow() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let snap = self.probe()
            DispatchQueue.main.async { self.apply(snap) }
        }
    }

    private func probe(quick: Bool = false) -> Snapshot {
        var s = Snapshot()
        s.checkedAt = Date()
        let timeout: TimeInterval = quick ? 0.5 : 1.2

        let ble = getJSON("/ble", timeout: timeout)
        let status = getJSON("/status", timeout: timeout)
        if ble != nil || status != nil {
            s.serviceUp = true
            s.pid = currentServicePid()
            s.adopted = (child == nil)
            if let b = ble {
                s.bleConnected = (b["connected"] as? Bool) ?? false
                s.peer = (b["peer"] as? String) ?? ""
                s.mtu = (b["mtu"] as? Int) ?? 0
            }
            if let c = getJSON("/conversations", timeout: timeout),
               let items = c["items"] as? [[String: Any]] {
                s.convCount = items.count
            }
        } else {
            s.serviceUp = false
            s.adopted = false
        }
        return s
    }

    private func apply(_ s: Snapshot) {
        snapshot = s
        guard let button = statusItem.button else { return }

        let symbol: String
        if !s.serviceUp {
            symbol = "waveform.slash"
        } else if s.bleConnected {
            symbol = "waveform.circle.fill"
        } else {
            symbol = "waveform.circle"
        }
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "WBBridge")
        button.image?.isTemplate = true
        button.toolTip = s.serviceUp
            ? "WBBridge · \(s.bleConnected ? "工牌已连接" : "等待工牌") · pid \(s.pid)"
            : "WBBridge · 服务未运行"
    }

    private func getJSON(_ path: String, timeout: TimeInterval = 1.2) -> [String: Any]? {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)\(path)") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var out: [String: Any]?
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let d = data,
               let obj = try? JSONSerialization.jsonObject(with: d),
               let dict = obj as? [String: Any] {
                out = dict
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.8)
        return out
    }

    // --- 服务的启停 -------------------------------------------------------

    private func startService() {
        guard child == nil else { return }
        if getJSON("/status", timeout: 0.5) != nil {
            // 已经有实例在跑（终端里起的 / 上一个 app 残留），不重复启动
            appendLog("menubar: 8765 已被占用，进入接管模式")
            snapshot.serviceUp = true
            snapshot.adopted = true
            return
        }
        guard let dir = bridgeDir else {
            alert("找不到服务代码",
                  "wb_bridge.py 不在 app 的 Resources/bridge 里，也不是源码目录布局。请重新打包。")
            return
        }
        guard let py = findPython() else {
            if !shownMissingDepsAlert {
                shownMissingDepsAlert = true
                alert("缺少 Python 依赖",
                      "没找到同时装有 bleak 和 numpy 的 Python。\n\n先安装依赖：\n  pip3 install -r \"\(dir.path)/requirements.txt\"\n\n或用环境变量指定解释器：\n  launchctl setenv WB_PYTHON /path/to/python3")
            }
            return
        }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: py)
        proc.arguments = ["-u", "wb_bridge.py"]
        proc.currentDirectoryURL = dir

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["WB_HOME"] = WB_HOME
        env["WB_VOICE_DIR"] = WB_HOME + "/voice_out"
        env["WB_GW_CACHE"] = WB_HOME + "/.gw_cache.json"
        proc.environment = env

        let fh = openLog()
        proc.standardOutput = fh
        proc.standardError = fh

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.child === p {
                    self.child = nil
                }
                self.appendLog("menubar: 服务进程退出 code=\(p.terminationStatus)")
                self.refreshNow()
            }
        }

        do {
            try proc.run()
            child = proc
            try? "\(proc.processIdentifier)\n".data(using: .utf8)?
                .write(to: URL(fileURLWithPath: PID_PATH))
            appendLog("menubar: 启动服务 pid=\(proc.processIdentifier) py=\(py)")
        } catch {
            alert("服务启动失败", error.localizedDescription)
        }
    }

    private func stopService(removePidFile: Bool = true) {
        guard let p = child else {
            // 接管模式下没有 child，按端口找进程
            let pid = currentServicePid()
            if pid > 0 { kill(pid, SIGTERM) }
            if removePidFile { try? FileManager.default.removeItem(atPath: PID_PATH) }
            return
        }
        child = nil
        p.terminate()
        // 给它 5 秒优雅退出
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
        if removePidFile { try? FileManager.default.removeItem(atPath: PID_PATH) }
        appendLog("menubar: 停止服务")
    }

    private func currentServicePid() -> Int32 {
        if let c = child, c.isRunning { return c.processIdentifier }
        if let txt = try? String(contentsOfFile: PID_PATH, encoding: .utf8),
           let pid = Int32(txt.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 0,
           processIsBridge(pid) {
            return pid
        }
        // 兜底：从监听端口反查
        if let out = runCapture("/usr/sbin/lsof", ["-nP", "-iTCP:\(PORT)", "-sTCP:LISTEN", "-t"]),
           let pid = Int32(out.components(separatedBy: .newlines).first ?? "") {
            return pid
        }
        return 0
    }

    private func processIsBridge(_ pid: Int32) -> Bool {
        guard let out = runCapture("/bin/ps", ["-p", "\(pid)", "-o", "command="]) else { return false }
        return out.contains("wb_bridge.py")
    }

    // --- 小工具 -----------------------------------------------------------

    private func findPython() -> String? {
        var candidates: [String] = []
        if let e = ProcessInfo.processInfo.environment["WB_PYTHON"], !e.isEmpty {
            candidates.append(e)
        }
        if let res = Bundle.main.resourceURL {
            candidates.append(res.appendingPathComponent("venv/bin/python3").path)
        }
        candidates.append(WB_HOME + "/binaries/python/envs/default/bin/python")
        candidates.append("/opt/homebrew/bin/python3")
        candidates.append("/usr/local/bin/python3")
        candidates.append("/usr/bin/python3")

        for py in candidates where FileManager.default.isExecutableFile(atPath: py) {
            if runSilent(py, ["-c", "import bleak, numpy"]) { return py }
        }
        return nil
    }

    private func runSilent(_ path: String, _ args: [String]) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus == 0
        } catch { return false }
    }

    private func runCapture(_ path: String, _ args: [String]) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func ensureWritableDirs() {
        try? FileManager.default.createDirectory(atPath: WB_HOME, withIntermediateDirectories: true)
    }

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: LOG_PATH) {
            fm.createFile(atPath: LOG_PATH, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: URL(fileURLWithPath: LOG_PATH)) else { return nil }
        h.seekToEndOfFile()
        logHandle = h
        return h
    }

    private func appendLog(_ msg: String) {
        let fmt = DateFormatter()
        fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let line = "=== \(fmt.string(from: Date())) [menubar] \(msg) ===\n"
        if let h = logHandle ?? openLog(), let d = line.data(using: .utf8) {
            h.seekToEndOfFile()
            h.write(d)
        }
    }

    private func alert(_ title: String, _ body: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = body
        a.alertStyle = .warning
        a.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        a.runModal()
    }

    // --- 菜单动作 ---------------------------------------------------------

    @objc private func startServiceAction() { startService(); refreshNow() }

    @objc private func stopServiceAction() {
        stopService()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { self.refreshNow() }
    }

    @objc private func restartService() {
        stopService()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.startService()
            self.refreshNow()
        }
    }

    @objc private func openLogAction() {
        NSWorkspace.shared.open(URL(fileURLWithPath: LOG_PATH))
    }

    @objc private func openDiagPage() {
        if let url = URL(string: "http://127.0.0.1:\(PORT)/status") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func quitApp() {
        stopService()
        NSApp.terminate(nil)
    }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

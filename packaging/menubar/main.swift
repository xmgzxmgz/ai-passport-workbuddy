import AppKit
import WebKit

// ---------------------------------------------------------------------------
// WBBridge 桌面壳
//
// 界面是服务自带的本地控制台（GET /ui），这里只负责：
//   · 一个原生窗口 + WKWebView 承载它（关窗不退出，缩到菜单栏）
//   · 菜单栏图标：服务/工牌状态一眼可见，带启停入口
//   · 拉起 / 停止 python 子进程，把设置（模型、语言、端口）通过环境变量注入
//   · 给网页提供原生桥（打开日志 / 打开目录 / 重启服务）
//
// 为什么不是原生控件界面：这台机器只有 Command Line Tools，没有 Xcode，
// SwiftUI 的 @State 等宏缺 libSwiftUIMacros.dylib 编译不过；纯 AppKit 手写
// 全部控件成本高、迭代慢。本地 Web 控制台既好写，又能直接在浏览器里用。
// ---------------------------------------------------------------------------

let HOME = NSHomeDirectory()
let WB_HOME = HOME + "/.workbuddy"
let LOG_PATH = WB_HOME + "/wb_bridge.log"
let PID_PATH = WB_HOME + "/wb_bridge.pid"

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         NSMenuDelegate, WKScriptMessageHandler {

    private var window: NSWindow!
    private var web: WKWebView!
    private var statusItem: NSStatusItem!
    private var child: Process?
    private var logHandle: FileHandle?
    private var timer: Timer?

    private var serviceUp = false
    private var bleConnected = false
    private var bleMtu = 0
    private var taskText = ""
    private var showingUI = false
    private var retryWork: DispatchWorkItem?

    /// 真实端口来自服务写的 ~/.workbuddy/http.port（8765 被占时服务会自动挪）。
    private var port: Int {
        if let s = try? String(contentsOfFile: WB_HOME + "/http.port", encoding: .utf8),
           let v = Int(s.trimmingCharacters(in: .whitespacesAndNewlines)), v > 1024 {
            return v
        }
        let v = UserDefaults.standard.integer(forKey: "port")
        return v > 1024 && v < 65536 ? v : 8765
    }

    // app=1 让前端知道自己在原生窗口里：留出标题栏高度、顶部加可拖拽条
    private var uiURL: URL { URL(string: "http://127.0.0.1:\(port)/ui?app=1")! }

    // MARK: - 生命周期

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.appearance = NSAppearance(named: .darkAqua)

        buildMainMenu()
        buildWindow()
        buildStatusItem()

        try? FileManager.default.createDirectory(atPath: WB_HOME + "/voice_out",
                                                withIntermediateDirectories: true)

        appendLog("app 启动，port=\(port)")
        if probeUp() {
            showingUI = true
            loadUI()
        } else {
            startService()
            loadPlaceholder(reason: "正在启动服务…")
            scheduleReload()
        }

        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func applicationWillTerminate(_ n: Notification) {
        timer?.invalidate()
        if let p = child, p.isRunning { p.terminate() }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows f: Bool) -> Bool {
        showWindow()
        return true
    }

    // MARK: - 窗口

    private func buildWindow() {
        let cfg = WKWebViewConfiguration()
        let ucc = WKUserContentController()
        ucc.add(self, name: "wb")
        cfg.userContentController = ucc

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable,
                                     .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = "WBBridge · 工牌中转"
        w.titlebarAppearsTransparent = true
        w.titleVisibility = .hidden
        w.backgroundColor = NSColor(srgbRed: 0.051, green: 0.067, blue: 0.090, alpha: 1)
        w.minSize = NSSize(width: 1020, height: 660)
        w.delegate = self

        let web = WKWebView(frame: w.contentView?.bounds ?? .zero, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.setValue(false, forKey: "drawsBackground")
        w.contentView = web

        self.web = web
        self.window = w
        w.center()
    }

    func windowShouldClose(_ s: NSWindow) -> Bool {
        s.orderOut(nil)          // 关窗不退出：留在菜单栏
        return false
    }

    private func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - 页面

    private func loadUI() {
        web.load(URLRequest(url: uiURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    /// 启动服务后端口要等几秒才可用 —— 轮询到能响应再切到控制台
    private func scheduleReload() {
        retryWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            if self.probeUp() {
                self.showingUI = true
                self.loadUI()
            } else {
                if self.child == nil || !(self.child?.isRunning ?? false) {
                    self.startService()
                }
                self.scheduleReload()
            }
        }
        retryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: work)
    }

    private func loadPlaceholder(reason: String) {
        showingUI = false
        let html = """
        <!doctype html><html><head><meta charset="utf-8">
        <style>
          :root { color-scheme: dark; }
          body { margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
                 background:#0d1117; color:#e6edf3;
                 font:13px/1.6 -apple-system,"PingFang SC",sans-serif; -webkit-user-select:none }
          .box { text-align:center; max-width:470px }
          .ring { width:52px; height:52px; margin:0 auto 18px; border-radius:50%;
                  border:2px solid rgba(64,208,214,.2); border-top-color:#40d0d6;
                  animation:spin 1s linear infinite }
          @keyframes spin { to { transform:rotate(360deg) } }
          h1 { font-size:16px; font-weight:600; margin:0 0 6px }
          p { color:#8b949e; font-size:12px; margin:0 0 18px }
          button { font:inherit; font-size:12px; padding:6px 14px; margin:0 4px;
                   border-radius:7px; cursor:pointer;
                   border:1px solid #252c38; background:#1b222c; color:#e6edf3 }
          button:hover { border-color:#38414f }
          .hint { margin-top:16px; font-size:10.5px; color:#5a6472 }
        </style></head><body>
        <div class="box">
          <div class="ring"></div>
          <h1>中转服务未就绪</h1>
          <p>\(reason)</p>
          <button onclick="send('start')">启动服务</button>
          <button onclick="send('openLog')">打开日志</button>
          <button onclick="location.reload()">重试</button>
          <div class="hint">日志：~/.workbuddy/wb_bridge.log</div>
        </div>
        <script>
          function send(cmd) {
            try { window.webkit.messageHandlers.wb.postMessage({ cmd: cmd }); } catch (e) {}
          }
        </script></body></html>
        """
        web.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - 轮询

    private func poll() {
        let up = probeUp()
        serviceUp = up
        if up {
            if let d = getJSON("/ble") {
                bleConnected = (d["connected"] as? Bool) ?? false
                bleMtu = (d["mtu"] as? Int) ?? 0
            }
            if let s = getJSON("/status") {
                taskText = (s["task"] as? String) ?? ""
            }
            if !showingUI {
                showingUI = true
                loadUI()
            }
        } else {
            bleConnected = false
            if showingUI {
                loadPlaceholder(reason: "服务进程已经不在了。点「启动服务」重新拉起。")
            }
        }
        updateIcon()
    }

    private func probeUp() -> Bool {
        getJSON("/health", timeout: 1.0) != nil || getJSON("/status", timeout: 1.0) != nil
    }

    private func getJSON(_ path: String, timeout: TimeInterval = 2.0) -> [String: Any]? {
        guard let u = URL(string: "http://127.0.0.1:\(port)\(path)") else { return nil }
        var req = URLRequest(url: u)
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        let sem = DispatchSemaphore(value: 0)
        var out: [String: Any]?
        URLSession.shared.dataTask(with: req) { d, _, _ in
            if let d, let o = try? JSONSerialization.jsonObject(with: d),
               let dict = o as? [String: Any] {
                out = dict
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.8)
        return out
    }

    // MARK: - 服务的启停

    private func resolveBridgeDir() -> URL? {
        let fm = FileManager.default
        if let env = ProcessInfo.processInfo.environment["WB_BRIDGE_DIR"],
           fm.fileExists(atPath: env + "/wb_bridge.py") {
            return URL(fileURLWithPath: env)
        }
        if let res = Bundle.main.resourceURL {
            let p = res.appendingPathComponent("bridge")
            if fm.fileExists(atPath: p.appendingPathComponent("wb_bridge.py").path) { return p }
        }
        let dev = Bundle.main.bundleURL.deletingLastPathComponent()
        if fm.fileExists(atPath: dev.appendingPathComponent("wb_bridge.py").path) { return dev }
        return nil
    }

    private func findPython() -> String? {
        var cands: [String] = []
        if let e = ProcessInfo.processInfo.environment["WB_PYTHON"], !e.isEmpty { cands.append(e) }
        if let res = Bundle.main.resourceURL {
            cands.append(res.appendingPathComponent("venv/bin/python3").path)
        }
        cands.append(WB_HOME + "/binaries/python/envs/default/bin/python")
        cands.append("/opt/homebrew/bin/python3")
        cands.append("/usr/local/bin/python3")
        cands.append("/usr/bin/python3")
        for py in cands where FileManager.default.isExecutableFile(atPath: py) {
            if runSilent(py, ["-c", "import bleak, numpy"]) { return py }
        }
        return nil
    }

    private func startService() {
        if let c = child, c.isRunning { return }
        guard let dir = resolveBridgeDir() else {
            alert("找不到服务代码", "app 的 Resources/bridge 里没有 wb_bridge.py。请重新打包，或用 WB_BRIDGE_DIR 指定。")
            loadPlaceholder(reason: "找不到 wb_bridge.py —— 请重新打包。")
            return
        }
        guard let py = findPython() else {
            alert("缺少 Python 依赖",
                  "没找到同时装有 bleak 和 numpy 的 Python。\n\n  pip3 install -r \"\(dir.path)/requirements.txt\"\n\n或指定解释器：\n  launchctl setenv WB_PYTHON /path/to/python3")
            loadPlaceholder(reason: "缺少 Python 依赖（bleak / numpy）—— 见弹出的说明。")
            return
        }

        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = ["-u", "wb_bridge.py"]
        p.currentDirectoryURL = dir

        var env = ProcessInfo.processInfo.environment
        let d = UserDefaults.standard
        env["PYTHONUNBUFFERED"] = "1"
        env["WB_HOME"] = WB_HOME
        env["WB_VOICE_DIR"] = WB_HOME + "/voice_out"
        env["WB_GW_CACHE"] = WB_HOME + "/.gw_cache.json"
        env["WB_HTTP_PORT"] = "\(port)"
        if let m = d.string(forKey: "whisperModel"), !m.isEmpty { env["WB_WHISPER_MODEL"] = m }
        if let l = d.string(forKey: "whisperLang"), !l.isEmpty { env["WB_WHISPER_LANG"] = l }
        p.environment = env

        if let h = openLog() {
            p.standardOutput = h
            p.standardError = h
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self else { return }
                if self.child === proc { self.child = nil }
                self.appendLog("服务退出 code=\(proc.terminationStatus)")
                self.serviceUp = false
                self.updateIcon()
            }
        }

        do {
            try? FileManager.default.removeItem(atPath: WB_HOME + "/http.port")
            try p.run()
            child = p
            try? "\(p.processIdentifier)\n".data(using: .utf8)?
                .write(to: URL(fileURLWithPath: PID_PATH))
            appendLog("启动服务 pid=\(p.processIdentifier) py=\(py)")
            serviceUp = true
            updateIcon()
        } catch {
            alert("服务启动失败", error.localizedDescription)
        }
    }

    private func stopService() {
        if let p = child, p.isRunning {
            p.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            }
        }
        child = nil
        try? FileManager.default.removeItem(atPath: PID_PATH)
        appendLog("停止服务")
        serviceUp = false
        updateIcon()
    }

    private func restartService() {
        stopService()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            self.startService()
            self.scheduleReload()
        }
    }

    // MARK: - 菜单栏

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "WBBridge")
            b.image?.isTemplate = true
        }
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        updateIcon()
    }

    private func updateIcon() {
        guard let b = statusItem?.button else { return }
        let name = !serviceUp ? "waveform.slash"
            : (bleConnected ? "waveform.circle.fill" : "waveform.circle")
        b.image = NSImage(systemSymbolName: name, accessibilityDescription: "WBBridge")
        b.image?.isTemplate = true
        b.toolTip = !serviceUp ? "WBBridge · 服务未运行"
            : (bleConnected ? "WBBridge · 工牌已连接 · MTU \(bleMtu)" : "WBBridge · 等待工牌")
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        add(menu, "打开控制台", #selector(openWindowAction))
        menu.addItem(.separator())
        add(menu, serviceUp ? "服务：运行中 · pid \(child?.processIdentifier ?? 0)" : "服务：未运行", enabled: false)
        if serviceUp {
            add(menu, bleConnected ? "工牌：已连接 · MTU \(bleMtu)" : "工牌：未连接", enabled: false)
            if !taskText.isEmpty {
                add(menu, "任务：\(taskText.prefix(26))", enabled: false)
            }
        }
        menu.addItem(.separator())
        add(menu, serviceUp ? "停止服务" : "启动服务", #selector(toggleService))
        add(menu, "重启服务", #selector(restartAction))
        add(menu, "打开日志", #selector(openLogAction))
        menu.addItem(.separator())
        add(menu, "退出 WBBridge", #selector(quitAction), key: "q")
    }

    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector? = nil,
                     key: String = "", enabled: Bool = true) {
        let it = NSMenuItem(title: title, action: sel, keyEquivalent: key)
        if sel != nil { it.target = self }
        it.isEnabled = enabled
        menu.addItem(it)
    }

    @objc private func openWindowAction() { showWindow() }
    @objc private func toggleService() { serviceUp ? stopService() : startService() }
    @objc private func restartAction() { restartService() }
    @objc private func openLogAction() { NSWorkspace.shared.open(URL(fileURLWithPath: LOG_PATH)) }
    @objc private func quitAction() { stopService(); NSApp.terminate(nil) }

    // MARK: - 网页原生桥

    func userContentController(_ u: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any],
              let cmd = body["cmd"] as? String else { return }
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            switch cmd {
            case "openLog":   NSWorkspace.shared.open(URL(fileURLWithPath: LOG_PATH))
            case "openVoice": NSWorkspace.shared.open(URL(fileURLWithPath: WB_HOME + "/voice_out"))
            case "openDiag":
                if let s = body["url"] as? String, let u = URL(string: s) { NSWorkspace.shared.open(u) }
            case "stop":      self.stopService()
            case "start":     self.startService(); self.scheduleReload()
            case "restart":   self.restartService()
            default:          break
            }
        }
    }

    // MARK: - 杂项

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        if !fm.fileExists(atPath: LOG_PATH) { fm.createFile(atPath: LOG_PATH, contents: nil) }
        guard let h = try? FileHandle(forWritingTo: URL(fileURLWithPath: LOG_PATH)) else { return nil }
        h.seekToEndOfFile()
        logHandle = h
        return h
    }

    private func appendLog(_ msg: String) {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let line = "=== \(f.string(from: Date())) [app] \(msg) ===\n"
        if let h = logHandle ?? openLog(), let d = line.data(using: .utf8) {
            h.seekToEndOfFile()
            h.write(d)
        }
    }

    private func runSilent(_ path: String, _ args: [String]) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); p.waitUntilExit(); return p.terminationStatus == 0 } catch { return false }
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

    private func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 WBBridge",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        let logItem = NSMenuItem(title: "打开日志", action: #selector(openLogAction), keyEquivalent: "l")
        logItem.target = self
        appMenu.addItem(logItem)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 WBBridge",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 WBBridge",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "显示")
        let reload = NSMenuItem(title: "重新载入控制台", action: #selector(reloadUI), keyEquivalent: "r")
        reload.target = self
        view.addItem(reload)
        viewItem.submenu = view
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }

    @objc private func reloadUI() {
        if serviceUp { loadUI() } else { loadPlaceholder(reason: "服务未就绪") }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

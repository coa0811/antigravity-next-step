import Cocoa
import Foundation
import WebKit

private struct SavedFrame: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

private struct RuntimeState: Codable {
    let pid: Int32
    let url: String
    let executable: String
}

private struct DismissState: Codable {
    let dismissedAt: Double
    let pid: Int32
    let reason: String
}

struct LaunchArgs {
    let url: URL
    let stateFile: URL?
    let frameFile: URL?
    let dismissFile: URL?
}

private func parseLaunchArgs() -> LaunchArgs? {
    let args = CommandLine.arguments
    var rawUrl: String?
    var statePath: String?
    var framePath: String?
    var dismissPath: String?

    var i = 1
    while i < args.count {
        let key = args[i]
        if key == "--url", i + 1 < args.count {
            rawUrl = args[i + 1]
            i += 2
        } else if key == "--state-file", i + 1 < args.count {
            statePath = args[i + 1]
            i += 2
        } else if key == "--frame-file", i + 1 < args.count {
            framePath = args[i + 1]
            i += 2
        } else if key == "--dismiss-file", i + 1 < args.count {
            dismissPath = args[i + 1]
            i += 2
        } else {
            i += 1
        }
    }

    guard let urlStr = rawUrl, let url = URL(string: urlStr) else {
        return nil
    }
    return LaunchArgs(
        url: url,
        stateFile: statePath.map { URL(fileURLWithPath: $0) },
        frameFile: framePath.map { URL(fileURLWithPath: $0) },
        dismissFile: dismissPath.map { URL(fileURLWithPath: $0) }
    )
}

@MainActor
private final class ClipboardMessageHandler: NSObject, WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let text = message.body as? String, !text.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }
}

@MainActor
private final class FloatingHUDPanel: NSPanel {
    static let panelStyleMask: NSWindow.StyleMask = [
        .titled, .closable, .resizable, .utilityWindow, .nonactivatingPanel,
    ]

    init(contentRect: NSRect) {
        super.init(
            contentRect: contentRect,
            styleMask: FloatingHUDPanel.panelStyleMask,
            backing: .buffered,
            defer: false
        )
        self.title = "Next Step HUD (只读悬浮窗)"
        self.level = .floating
        self.isFloatingPanel = true
        self.becomesKeyOnlyIfNeeded = true
        self.hidesOnDeactivate = false
        self.isReleasedWhenClosed = false
        self.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.minSize = NSSize(width: 340, height: 130)
    }
}

@MainActor
final class FloatingHUDController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let launchArgs: LaunchArgs
    private var panel: FloatingHUDPanel?
    private var webView: WKWebView?
    private var sigSource: DispatchSourceSignal?
    private let clipboardHandler = ClipboardMessageHandler()

    init(launchArgs: LaunchArgs) {
        self.launchArgs = launchArgs
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let initialFrame = resolveInitialFrame()
        let initialContentRect = NSWindow.contentRect(
            forFrameRect: initialFrame,
            styleMask: FloatingHUDPanel.panelStyleMask
        )
        let hudPanel = FloatingHUDPanel(contentRect: initialContentRect)
        hudPanel.setFrame(initialFrame, display: false)

        let config = WKWebViewConfiguration()
        config.userContentController.add(clipboardHandler, name: "copyText")

        let wv = WKWebView(frame: hudPanel.contentView?.bounds ?? initialContentRect, configuration: config)
        wv.autoresizingMask = [.width, .height]
        hudPanel.contentView = wv

        var req = URLRequest(url: launchArgs.url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        wv.load(req)

        hudPanel.delegate = self
        hudPanel.orderFrontRegardless()
        self.panel = hudPanel
        self.webView = wv

        writeStateFile()
        saveCurrentFrame()
        installSignalHandler()
    }

    func applicationWillTerminate(_ notification: Notification) {
        removeStateFile()
    }

    func windowDidMove(_ notification: Notification) {
        saveCurrentFrame()
    }

    func windowDidResize(_ notification: Notification) {
        saveCurrentFrame()
    }

    func windowWillClose(_ notification: Notification) {
        saveCurrentFrame()
        writeDismissFile()
        removeStateFile()
        NSApp.terminate(nil)
    }

    private func resolveInitialFrame() -> NSRect {
        let defaultW = 540.0
        let defaultH = 230.0

        if let frameFile = launchArgs.frameFile,
           let data = try? Data(contentsOf: frameFile),
           let saved = try? JSONDecoder().decode(SavedFrame.self, from: data),
           saved.width >= 340, saved.height >= 130 {
            let candidate = NSRect(x: saved.x, y: saved.y, width: saved.width, height: saved.height)
            let onScreen = NSScreen.screens.contains { $0.visibleFrame.intersects(candidate) }
            if onScreen {
                return candidate
            }
        }

        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 100, y: 100, width: 1200, height: 800)
        let x = visible.maxX - defaultW - 28.0
        let y = visible.minY + 36.0
        return NSRect(x: max(visible.minX + 16.0, x), y: y, width: defaultW, height: defaultH)
    }

    private func saveCurrentFrame() {
        guard let frameFile = launchArgs.frameFile, let frame = panel?.frame else { return }
        let payload = SavedFrame(x: frame.origin.x, y: frame.origin.y, width: frame.size.width, height: frame.size.height)
        if let encoded = try? JSONEncoder().encode(payload) {
            try? FileManager.default.createDirectory(at: frameFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? encoded.write(to: frameFile, options: .atomic)
        }
    }

    private func writeStateFile() {
        guard let stateFile = launchArgs.stateFile else { return }
        let exePath = Bundle.main.executablePath ?? CommandLine.arguments.first ?? ""
        let state = RuntimeState(
            pid: ProcessInfo.processInfo.processIdentifier,
            url: launchArgs.url.absoluteString,
            executable: exePath
        )
        if let encoded = try? JSONEncoder().encode(state) {
            try? FileManager.default.createDirectory(at: stateFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? encoded.write(to: stateFile, options: .atomic)
        }
    }

    private func writeDismissFile() {
        guard let dismissFile = launchArgs.dismissFile else { return }
        let dismiss = DismissState(
            dismissedAt: Date().timeIntervalSince1970,
            pid: ProcessInfo.processInfo.processIdentifier,
            reason: "window_closed"
        )
        if let encoded = try? JSONEncoder().encode(dismiss) {
            try? FileManager.default.createDirectory(at: dismissFile.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? encoded.write(to: dismissFile, options: .atomic)
        }
    }

    private func removeStateFile() {
        guard let stateFile = launchArgs.stateFile else { return }
        try? FileManager.default.removeItem(at: stateFile)
    }

    private func installSignalHandler() {
        signal(SIGUSR1, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        src.setEventHandler { [weak self] in
            guard let self else { return }
            self.panel?.orderFrontRegardless()
            if self.webView?.url == nil {
                var req = URLRequest(url: self.launchArgs.url)
                req.cachePolicy = .reloadIgnoringLocalCacheData
                self.webView?.load(req)
            }
        }
        src.resume()
        self.sigSource = src
    }
}

guard let parsedArgs = parseLaunchArgs() else {
    fputs("Usage: next-step-floating-hud --url <http://127.0.0.1:PORT/?mode=compact> [--state-file PATH] [--frame-file PATH] [--dismiss-file PATH]\n", stderr)
    exit(2)
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let controller = FloatingHUDController(launchArgs: parsedArgs)
    app.delegate = controller
    withExtendedLifetime(controller) {
        app.run()
    }
}

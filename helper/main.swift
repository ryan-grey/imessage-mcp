import AppKit
import Contacts

final class Delegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var label: NSTextField!
    var bridge: Process?
    let store = CNContactStore()

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 210),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Ryan Contacts"
        let intro = NSTextField(wrappingLabelWithString: "Share Contacts with your local MCP clients. Changes still require explicit confirmation; closing this window keeps the helper running.")
        intro.frame = NSRect(x: 24, y: 120, width: 430, height: 65)
        window.contentView!.addSubview(intro)
        label = NSTextField(labelWithString: "")
        label.frame = NSRect(x: 24, y: 85, width: 430, height: 24)
        window.contentView!.addSubview(label)
        let button = NSButton(title: "Request Contacts Access", target: self, action: #selector(request))
        button.frame = NSRect(x: 24, y: 30, width: 220, height: 35)
        window.contentView!.addSubview(button)
        let quit = NSButton(title: "Quit Helper", target: self, action: #selector(quitHelper))
        quit.frame = NSRect(x: 320, y: 30, width: 130, height: 35)
        window.contentView!.addSubview(quit)
        window.center()
        updateStatus()
        if CommandLine.arguments.contains("--settings") { showSettings() }
        let task = Process()
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        task.executableURL = URL(fileURLWithPath: home + "/.local/imessage-mcp/.venv/bin/python")
        task.arguments = [home + "/Developer/imessage-mcp/helper/bridge.py", "serve"]
        do { try task.run(); bridge = task }
        catch { label.stringValue = "Bridge could not start: \(error.localizedDescription)" }
    }
    func updateStatus() {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        let names = [0: "Not requested", 1: "Restricted", 2: "Denied — enable in System Settings", 3: "Authorized", 4: "Limited access"]
        label.stringValue = "Contacts: " + (names[status.rawValue] ?? "Unknown")
    }
    @objc func request() {
        store.requestAccess(for: .contacts) { _, _ in
            DispatchQueue.main.async { self.updateStatus() }
        }
    }
    @objc func quitHelper() { NSApp.terminate(nil) }
    func showSettings() {
        updateStatus()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationWillTerminate(_ notification: Notification) { bridge?.terminate() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        return false
    }
}
let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()

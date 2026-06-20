import AppKit
import SwiftUI
import Carbon.HIToolbox

/// A borderless panel that can become key (so its search field accepts typing).
final class KeyablePanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

/// Owns the Quick Search experience: shows the floating panel, remembers the app that had
/// focus, routes navigation keys, and on commit delivers the selected code into that app.
@MainActor
final class QuickSearchController: NSObject, NSWindowDelegate {
    let model = QuickSearchModel()

    private var panel: KeyablePanel?
    private var keyMonitor: Any?
    private var tickTimer: Timer?
    private var previousApp: NSRunningApplication?

    private let preferences: Preferences
    /// Supplies the current decrypted entries when the panel opens.
    var entriesProvider: () -> [AuthEntry] = { [] }
    /// Asked to present the login window when the user isn't signed in.
    var onNeedsLogin: () -> Void = {}
    var isSignedIn: () -> Bool = { false }
    /// Whether the app is locked, and how to ask for unlock instead of showing codes.
    var isLocked: () -> Bool = { false }
    var onLocked: () -> Void = {}

    init(preferences: Preferences) {
        self.preferences = preferences
        super.init()
    }

    var isVisible: Bool { panel?.isVisible == true }

    func toggle() {
        if isVisible { hide() } else { show() }
    }

    // MARK: Show / hide

    func show() {
        if isLocked() {
            onLocked()
            return
        }
        guard isSignedIn() else {
            onNeedsLogin()
            return
        }
        // Capture the app that currently has focus BEFORE we activate ourselves.
        previousApp = NSWorkspace.shared.frontmostApplication

        model.recentIDs = preferences.recentlyUsedIDs
        model.setEntries(entriesProvider())
        model.reset()

        let panel = self.panel ?? makePanel()
        self.panel = panel

        positionPanel(panel)
        installKeyMonitor()
        startTicking()

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
    }

    func hide() {
        panel?.orderOut(nil)
        removeKeyMonitor()
        stopTicking()
    }

    // MARK: Panel construction

    private func makePanel() -> KeyablePanel {
        let panel = KeyablePanel(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 80),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.animationBehavior = .utilityWindow
        panel.delegate = self

        let root = QuickSearchView(model: model) { [weak self] entry in
            self?.commit(entry)
        }
        let hosting = NSHostingView(rootView: root)
        hosting.translatesAutoresizingMaskIntoConstraints = false
        panel.contentView = hosting
        panel.setContentSize(hosting.fittingSize)
        return panel
    }

    private func positionPanel(_ panel: NSPanel) {
        panel.layoutIfNeeded()
        let size = panel.contentView?.fittingSize ?? NSSize(width: 560, height: 80)
        panel.setContentSize(size)

        let screen = screenWithMouse() ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { return }
        let x = frame.midX - size.width / 2
        // Sit a bit above vertical center, like Spotlight.
        let y = frame.midY + frame.height * 0.12 - size.height / 2
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func screenWithMouse() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
    }

    // MARK: Keyboard

    private func installKeyMonitor() {
        removeKeyMonitor()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }

            // ⌘1…⌘9 instantly commits the Nth visible result.
            if event.modifierFlags.contains(.command),
               let digit = event.charactersIgnoringModifiers.flatMap({ Int($0) }),
               (1...9).contains(digit) {
                let index = digit - 1
                if self.model.results.indices.contains(index) {
                    self.commit(self.model.results[index])
                }
                return nil
            }

            switch Int(event.keyCode) {
            case kVK_DownArrow:
                self.model.moveSelection(down: true); return nil
            case kVK_UpArrow:
                self.model.moveSelection(down: false); return nil
            case kVK_Return, kVK_ANSI_KeypadEnter:
                if let entry = self.model.selectedEntry { self.commit(entry) }
                return nil
            case kVK_Escape:
                if self.model.query.isEmpty { self.hide() } else { self.model.query = "" }
                return nil
            default:
                return event // let the text field handle typing
            }
        }
    }

    private func removeKeyMonitor() {
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor); self.keyMonitor = nil }
    }

    // MARK: Live code refresh

    private func startTicking() {
        stopTicking()
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.model.tick = Date()
        }
        RunLoop.main.add(timer, forMode: .common)
        tickTimer = timer
    }

    private func stopTicking() {
        tickTimer?.invalidate()
        tickTimer = nil
    }

    // MARK: Commit

    private func commit(_ entry: AuthEntry) {
        let code = entry.code()
        let target = previousApp
        preferences.recordUsage(entry.id)
        hide()
        CodeInjector.deliver(code: code, to: target,
                             mode: preferences.deliveryMode,
                             alsoCopy: preferences.alsoCopyWhenTyping,
                             clearClipboardAfter: preferences.clipboardClearDelay)
    }

    // MARK: NSWindowDelegate

    /// Dismiss like Spotlight when focus leaves the panel (click another app/window).
    func windowDidResignKey(_ notification: Notification) {
        guard isVisible else { return }
        hide()
    }
}

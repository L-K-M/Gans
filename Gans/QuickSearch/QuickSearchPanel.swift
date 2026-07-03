import AppKit
import SwiftUI
import Combine
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
    private var cancellables = Set<AnyCancellable>()

    /// Screen-anchored geometry, fixed for as long as the panel is up: the panel is
    /// pinned by its TOP edge and horizontal center, so the search field never jumps
    /// while the result list below it grows and shrinks.
    private var panelTop: CGFloat = 0
    private var panelCenterX: CGFloat = 0

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

        // Result changes resize the panel in place (top edge pinned).
        model.$results
            .receive(on: RunLoop.main)
            .sink { [weak self] results in
                guard let self, self.isVisible else { return }
                self.layoutPanel(rows: results.count)
            }
            .store(in: &cancellables)
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

        model.showCodes = preferences.showCodesInQuickSearch
        model.recentIDs = preferences.recentlyUsedIDs
        model.setEntries(entriesProvider())
        model.reset()

        let panel = self.panel ?? makePanel()
        self.panel = panel

        positionPanel()
        installKeyMonitor()
        startTicking()

        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
    }

    /// Dismisses the panel. `restoreFocus` hands activation back to the app that was
    /// frontmost before Quick Search opened — right for Esc/hotkey dismissal, wrong when
    /// the user clicked into another app (resign-key) or a commit is about to activate
    /// the target itself.
    func hide(restoreFocus: Bool = true) {
        let wasVisible = isVisible
        panel?.orderOut(nil)
        removeKeyMonitor()
        stopTicking()
        model.showIndices = false
        if restoreFocus, wasVisible {
            previousApp?.activate(options: [])
        }
    }

    // MARK: Panel construction

    private func makePanel() -> KeyablePanel {
        let panel = KeyablePanel(
            contentRect: NSRect(x: 0, y: 0,
                                width: QuickSearchMetrics.width,
                                height: QuickSearchMetrics.panelHeight(forRows: 0)),
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
        // The controller owns the panel frame (see layoutPanel): with the hosting view's
        // own sizing constraints active, SwiftUI would resize the borderless window with
        // an uncontrolled anchor and the panel would jump around while typing.
        hosting.sizingOptions = []
        panel.contentView = hosting
        return panel
    }

    private func positionPanel() {
        let screen = screenWithMouse() ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { return }
        panelCenterX = frame.midX
        // Sit a bit above vertical center, like Spotlight. This is the fixed TOP edge;
        // the panel grows downward from here.
        panelTop = frame.midY + frame.height * 0.25
        layoutPanel(rows: model.results.count)
    }

    /// Applies the deterministic frame for the current row count, top edge pinned.
    private func layoutPanel(rows: Int) {
        guard let panel else { return }
        let height = QuickSearchMetrics.panelHeight(forRows: rows)
        let frame = NSRect(x: panelCenterX - QuickSearchMetrics.width / 2,
                           y: panelTop - height,
                           width: QuickSearchMetrics.width,
                           height: height)
        panel.setFrame(frame, display: true)
    }

    private func screenWithMouse() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
    }

    // MARK: Keyboard

    private func installKeyMonitor() {
        removeKeyMonitor()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .flagsChanged]) { [weak self] event in
            guard let self else { return event }

            if event.type == .flagsChanged {
                // Holding ⌘ reveals the ⌘1…⌘9 quick-pick badges on the rows.
                self.model.showIndices = event.modifierFlags.contains(.command)
                return event
            }

            let modifiers = event.modifierFlags.intersection([.command, .option, .control, .shift])

            // ⌘1…⌘9 (exactly ⌘, so ⌘⇧1 etc. stay out of the way) commits the Nth result.
            if modifiers == .command,
               let digit = event.charactersIgnoringModifiers.flatMap({ Int($0) }),
               (1...9).contains(digit) {
                let index = digit - 1
                if self.model.results.indices.contains(index) {
                    self.commit(self.model.results[index])
                }
                return nil
            }

            // ⌘C copies the selected code instead of inserting it.
            if modifiers == .command,
               event.charactersIgnoringModifiers?.lowercased() == "c",
               let entry = self.model.selectedEntry {
                self.copyCommit(entry)
                return nil
            }

            switch Int(event.keyCode) {
            case kVK_DownArrow:
                self.model.moveSelection(down: true); return nil
            case kVK_UpArrow:
                self.model.moveSelection(down: false); return nil
            case kVK_Return, kVK_ANSI_KeypadEnter:
                if let entry = self.model.selectedEntry {
                    // ⌥Return copies without touching the previous app's focus.
                    if modifiers == .option { self.copyCommit(entry) } else { self.commit(entry) }
                }
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
        hide(restoreFocus: false) // the injector re-activates the target itself
        CodeInjector.deliver(code: code, to: target,
                             mode: preferences.deliveryMode,
                             alsoCopy: preferences.alsoCopyWhenTyping,
                             clearClipboardAfter: preferences.clipboardClearDelay) { result in
            // Without the Accessibility permission the code silently lands on the
            // clipboard instead of being typed — say so, or the commit feels broken.
            if case .copiedOnly = result {
                ToastPanel.show("Copied to the clipboard — grant Accessibility in Settings to insert codes directly")
            }
        }
    }

    /// Copies the code without injecting it (⌥Return / ⌘C), returning focus to where
    /// the user was.
    private func copyCommit(_ entry: AuthEntry) {
        let code = entry.code()
        preferences.recordUsage(entry.id)
        hide()
        CodeInjector.copyToClipboard(code, clearAfter: preferences.clipboardClearDelay)
        ToastPanel.show("Code copied")
    }

    // MARK: NSWindowDelegate

    /// Dismiss like Spotlight when focus leaves the panel (click another app/window).
    /// No focus restore — the user just chose somewhere else to be.
    func windowDidResignKey(_ notification: Notification) {
        guard isVisible else { return }
        hide(restoreFocus: false)
    }
}

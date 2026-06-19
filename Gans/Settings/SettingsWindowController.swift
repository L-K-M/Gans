import AppKit
import SwiftUI

/// Hosts `SettingsView` in a standard window, flipping the agent to `.regular` while it's
/// visible (like the login window) so it can take focus and show in the Dock.
@MainActor
final class SettingsWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?

    private let preferences: Preferences
    private let vault: EnteVault
    private let updateChecker: UpdateChecker

    var onSignIn: () -> Void = {}
    var onHotkeyChanged: () -> Void = {}

    init(preferences: Preferences, vault: EnteVault, updateChecker: UpdateChecker) {
        self.preferences = preferences
        self.vault = vault
        self.updateChecker = updateChecker
    }

    func show() {
        if let window {
            present(window)
            return
        }

        let view = SettingsView(
            preferences: preferences,
            vault: vault,
            updateChecker: updateChecker,
            onSignIn: { [weak self] in self?.onSignIn() },
            onSignOut: { [weak self] in self?.vault.signOut() },
            onHotkeyChanged: { [weak self] in self?.onHotkeyChanged() }
        )
        let hosting = NSHostingController(rootView: view)
        let window = NSWindow(contentViewController: hosting)
        window.title = "Gans Settings"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.setContentSize(hosting.view.fittingSize)
        window.center()
        self.window = window
        present(window)
    }

    private func present(_ window: NSWindow) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        ActivationPolicy.revertToAccessoryIfNoOrdinaryWindows(excluding: window)
    }
}

import AppKit
import SwiftUI

/// Hosts `LoginView` in a normal titled window. Because Gans is an `.accessory` app, we
/// flip to `.regular` while the window is up so it can take focus and show in the Dock,
/// then hand activation back when it closes.
@MainActor
final class LoginWindowController: NSObject, NSWindowDelegate {
    private var window: NSWindow?
    private let vault: EnteVault
    private let api: EnteAPI

    init(vault: EnteVault, api: EnteAPI) {
        self.vault = vault
        self.api = api
    }

    func show() {
        if let window {
            present(window)
            return
        }

        let model = LoginViewModel(vault: vault, api: api)
        model.onSignedIn = { [weak self] in
            // Pull focus back to Gans once login finishes — important after the passkey
            // flow, which leaves the browser frontmost.
            NSApp.activate(ignoringOtherApps: true)
            self?.close()
        }

        let hosting = NSHostingController(rootView: LoginView(model: model))
        let window = NSWindow(contentViewController: hosting)
        window.title = "Sign in to Ente"
        window.styleMask = [.titled, .closable]
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        self.window = window
        present(window)
    }

    private func present(_ window: NSWindow) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    private func close() {
        window?.close()
    }

    func windowWillClose(_ notification: Notification) {
        ActivationPolicy.revertToAccessoryIfNoOrdinaryWindows(excluding: window)
    }
}

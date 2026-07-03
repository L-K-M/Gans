import AppKit
import Combine

/// An `NSMenuItem` that runs a closure when chosen — keeps menu construction declarative.
final class ActionMenuItem: NSMenuItem {
    private let handler: () -> Void
    init(title: String, keyEquivalent: String = "", handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: keyEquivalent)
        self.target = self
    }
    required init(coder: NSCoder) { fatalError("not supported") }
    @objc private func fire() { handler() }
}

/// Owns the menu-bar item and builds its menu on demand. Each entry row copies that
/// entry's current code to the clipboard; the footer exposes Quick Search, refresh,
/// settings, updates, account, and quit.
@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let statusItem: NSStatusItem
    private let vault: EnteVault
    private let preferences: Preferences
    private let appLock: AppLock

    var onQuickSearch: () -> Void = {}
    var onSettings: () -> Void = {}
    var onLogin: () -> Void = {}
    var onCheckForUpdates: () -> Void = {}
    var onUnlock: () -> Void = {}

    private var cancellables = Set<AnyCancellable>()

    init(vault: EnteVault, preferences: Preferences, appLock: AppLock) {
        self.statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        self.vault = vault
        self.preferences = preferences
        self.appLock = appLock
        super.init()

        configureButton()
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        // Reflect the lock state in the menu-bar glyph.
        appLock.$isLocked
            .receive(on: RunLoop.main)
            .sink { [weak self] locked in self?.configureButton(locked: locked) }
            .store(in: &cancellables)
    }

    private func configureButton(locked: Bool = false) {
        let symbol = locked ? "lock.fill" : "key.fill"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Gans")
        image?.isTemplate = true
        statusItem.button?.image = image
    }

    // MARK: NSMenuDelegate

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if appLock.isLocked {
            buildLockedMenu(menu)
        } else if vault.isSignedIn {
            buildSignedInMenu(menu)
        } else {
            buildSignedOutMenu(menu)
        }
    }

    private func buildLockedMenu(_ menu: NSMenu) {
        let item = NSMenuItem(title: "Gans is locked", action: nil, keyEquivalent: "")
        item.isEnabled = false
        menu.addItem(item)
        menu.addItem(ActionMenuItem(title: "Unlock Gans…") { [weak self] in self?.onUnlock() })
        menu.addItem(.separator())
        addCommonFooter(menu)
    }

    private func buildSignedOutMenu(_ menu: NSMenu) {
        menu.addItem(ActionMenuItem(title: "Sign in to Ente…") { [weak self] in self?.onLogin() })
        menu.addItem(.separator())
        addCommonFooter(menu)
    }

    private func buildSignedInMenu(_ menu: NSMenu) {
        if let email = vault.accountEmail {
            let header = NSMenuItem(title: email, action: nil, keyEquivalent: "")
            header.isEnabled = false
            menu.addItem(header)
        }

        // A dead token would otherwise fail silently while stale cached codes keep
        // showing — surface it and offer the fix right here.
        if vault.sessionExpired {
            let warning = NSMenuItem(title: "⚠️ Session expired — codes no longer sync", action: nil, keyEquivalent: "")
            warning.isEnabled = false
            menu.addItem(warning)
            menu.addItem(ActionMenuItem(title: "Sign In Again…") { [weak self] in self?.onLogin() })
            menu.addItem(.separator())
        }

        let entries = vault.entries
        switch vault.state {
        case .loading where entries.isEmpty:
            let item = NSMenuItem(title: "Syncing…", action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        case .error(let message) where entries.isEmpty:
            let item = NSMenuItem(title: message, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        default:
            if entries.isEmpty {
                let item = NSMenuItem(title: "No entries", action: nil, keyEquivalent: "")
                item.isEnabled = false
                menu.addItem(item)
            } else {
                // Show only the name — never the live code — and copy it on click.
                for entry in entries {
                    menu.addItem(ActionMenuItem(title: entry.displayName) { [weak self] in
                        self?.copy(entry)
                    })
                }
            }
        }

        menu.addItem(.separator())
        menu.addItem(ActionMenuItem(title: "Quick Search…", keyEquivalent: "") { [weak self] in self?.onQuickSearch() })
        menu.addItem(ActionMenuItem(title: "Refresh Now") { [weak self] in
            Task { await self?.vault.refresh() }
        })
        menu.addItem(ActionMenuItem(title: "Lock Now") { [weak self] in self?.appLock.lock() })
        menu.addItem(ActionMenuItem(title: "Sign Out") { [weak self] in self?.vault.signOut() })
        menu.addItem(.separator())
        addCommonFooter(menu) // Settings, Check for Updates, then Quit last.
    }

    private func addCommonFooter(_ menu: NSMenu) {
        menu.addItem(ActionMenuItem(title: "Settings…", keyEquivalent: ",") { [weak self] in self?.onSettings() })
        menu.addItem(ActionMenuItem(title: "Check for Updates…") { [weak self] in self?.onCheckForUpdates() })
        menu.addItem(.separator())
        menu.addItem(ActionMenuItem(title: "Quit Gans", keyEquivalent: "q") { NSApp.terminate(nil) })
    }

    // MARK: Actions

    private func copy(_ entry: AuthEntry) {
        CodeInjector.copyToClipboard(entry.code(), clearAfter: preferences.clipboardClearDelay)
        preferences.recordUsage(entry.id)
        flashCopied()
    }

    /// Briefly swaps the menu-bar glyph to a checkmark to confirm a copy.
    private func flashCopied() {
        guard let button = statusItem.button else { return }
        let original = button.image
        button.image = NSImage(systemSymbolName: "checkmark.circle.fill", accessibilityDescription: "Copied")
        button.image?.isTemplate = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) {
            button.image = original
        }
    }
}

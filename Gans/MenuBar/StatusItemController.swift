import AppKit
import Combine

/// An `NSMenuItem` that runs a closure when chosen — keeps menu construction declarative.
///
/// The action goes through a separate trampoline object rather than `target = self`:
/// AppKit retains a menu item's target on modern macOS, so a self-targeting item is a
/// retain cycle that leaks every item of every menu open. Item → trampoline is a plain
/// one-way ownership and deallocates with the item under either retain semantic.
final class ActionMenuItem: NSMenuItem {
    private final class Trampoline: NSObject {
        let handler: () -> Void
        init(_ handler: @escaping () -> Void) { self.handler = handler }
        @objc func fire() { handler() }
    }

    private let trampoline: Trampoline

    init(title: String, keyEquivalent: String = "", handler: @escaping () -> Void) {
        self.trampoline = Trampoline(handler)
        super.init(title: title, action: #selector(Trampoline.fire), keyEquivalent: keyEquivalent)
        self.target = trampoline
    }
    required init(coder: NSCoder) { fatalError("not supported") }
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
                // Show every entry — name only (never the live code) — and copy it on
                // click. The list is intentionally not truncated: a subset would leave
                // the rest unreachable from the menu. AppKit makes an over-long NSMenu
                // scroll on its own, and Quick Search (⌃⌥Space) is the fast path for
                // large vaults, so a complete menu costs nothing but stays exhaustive.
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
        confirmCopy()
    }

    /// Monotonic token so overlapping flashes can't restore a stale glyph (two quick
    /// copies used to capture the checkmark as "original" and leave it stuck).
    private var flashGeneration = 0

    /// Confirms a copy or Quick Search commit: briefly swaps the menu-bar glyph — a 🪿
    /// goose in honk mode, otherwise a checkmark — and, in honk mode, plays the honk.
    /// Public so the Quick Search commit path can trigger the same confirmation.
    func confirmCopy() {
        let honk = preferences.honkOnCopy
        if honk { Honk.play() }
        guard let button = statusItem.button else { return }
        flashGeneration += 1
        let generation = flashGeneration
        // "bird.fill" stands in for the goose (SF Symbols has no goose, sadly).
        let symbol = honk ? "bird.fill" : "checkmark.circle.fill"
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Copied")
        button.image?.isTemplate = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in
            guard let self, self.flashGeneration == generation else { return }
            // Redraw from current state instead of a captured image, so a lock-state
            // change during the flash can't be clobbered either.
            self.configureButton(locked: self.appLock.isLocked)
        }
    }
}

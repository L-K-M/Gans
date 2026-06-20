import AppKit
import Combine

/// Boots and wires the app: initializes crypto, installs the menu-bar item and global
/// hotkey, restores the Ente session, and coordinates the windows.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    private let preferences = Preferences.shared
    private let vault = EnteVault()
    private let loginAPI = EnteAPI()

    private lazy var updateChecker = UpdateChecker(
        configuration: .init(owner: "L-K-M", repo: "Gans", appName: "Gans")
    )
    private lazy var statusItemController = StatusItemController(vault: vault, preferences: preferences)
    private lazy var quickSearch = QuickSearchController(preferences: preferences)
    private lazy var loginWindow = LoginWindowController(vault: vault, api: loginAPI)
    private lazy var settingsWindow = SettingsWindowController(preferences: preferences, vault: vault, updateChecker: updateChecker)
    private let hotkey = CarbonHotkey()

    private var cancellables = Set<AnyCancellable>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !Self.isRunningTests else { return }

        guard EnteCrypto.initialize() else {
            Log.app.fault("libsodium failed to initialize")
            return
        }

        // A menu-bar agent has no menu by default; install one so standard editing
        // shortcuts (⌘X/⌘C/⌘V/⌘A) work in our text fields.
        NSApp.mainMenu = MainMenu.build()

        wireStatusItem()
        wireQuickSearch()
        wireSettings()
        registerHotkey()

        updateChecker.start()
        observeVault()

        // Restore the session; if there's nobody signed in, open the sign-in window so a
        // fresh launch is actionable rather than a silent menu-bar icon.
        Task {
            await vault.restore()
            if !vault.isSignedIn { loginWindow.show() }
        }
    }

    // MARK: Wiring

    private func wireStatusItem() {
        statusItemController.onQuickSearch = { [weak self] in self?.quickSearch.show() }
        statusItemController.onSettings = { [weak self] in self?.settingsWindow.show() }
        statusItemController.onLogin = { [weak self] in self?.loginWindow.show() }
        statusItemController.onCheckForUpdates = { [weak self] in self?.updateChecker.checkNow() }
    }

    private func wireQuickSearch() {
        quickSearch.entriesProvider = { [weak self] in self?.vault.entries ?? [] }
        quickSearch.isSignedIn = { [weak self] in self?.vault.isSignedIn ?? false }
        quickSearch.onNeedsLogin = { [weak self] in self?.loginWindow.show() }
    }

    private func wireSettings() {
        settingsWindow.onSignIn = { [weak self] in self?.loginWindow.show() }
        settingsWindow.onHotkeyChanged = { [weak self] in self?.registerHotkey() }
    }

    private func registerHotkey() {
        hotkey.onPressed = { [weak self] in self?.quickSearch.toggle() }
        if !hotkey.register(preferences.hotkey) {
            Log.hotkey.error("Failed to register global hotkey \(self.preferences.hotkey.displayString, privacy: .public)")
        }
    }

    /// Keep the open Quick Search panel's list fresh when a sync lands.
    private func observeVault() {
        vault.$entries
            .receive(on: RunLoop.main)
            .sink { [weak self] entries in
                guard let self, self.quickSearch.isVisible else { return }
                self.quickSearch.model.setEntries(entries)
            }
            .store(in: &cancellables)
    }

    // MARK: Test guard

    private static var isRunningTests: Bool {
        NSClassFromString("XCTestCase") != nil ||
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
    }
}

import AppKit
import Combine

/// Boots and wires the app: initializes crypto, installs the menu-bar item and global
/// hotkey, restores the Ente session, and coordinates the windows.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {

    private let preferences = Preferences.shared
    private let vault = EnteVault()
    private let loginAPI = EnteAPI()
    private lazy var appLock = AppLock(preferences: preferences)

    private lazy var updateChecker = UpdateChecker(
        configuration: .init(owner: "L-K-M", repo: "Gans", appName: "Gans")
    )
    private lazy var statusItemController = StatusItemController(vault: vault, preferences: preferences, appLock: appLock)
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
        startAutoRefresh()

        startSession()
        presentOnboardingIfNeeded()
    }

    /// First launch only: a gentle, window-less welcome that names the hotkey and offers
    /// to open Quick Search — then never shown again.
    private func presentOnboardingIfNeeded() {
        guard !preferences.hasCompletedOnboarding else { return }
        preferences.hasCompletedOnboarding = true
        let hotkey = preferences.hotkey.displayString
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            ToastPanel.show(
                "Gans lives in your menu bar. Press \(hotkey) anywhere to search your 2FA codes and type them into whatever app you're in.",
                duration: 12,
                actionTitle: "Try it") { [weak self] in self?.quickSearch.show() }
        }
    }

    /// At launch: if the app lock is on and a session exists, start locked and prompt for
    /// unlock before touching the token; otherwise restore the session (and open sign-in if
    /// nobody's signed in).
    private func startSession() {
        if appLock.isEnabled && vault.isSignedIn {
            appLock.lockIfEnabled()
            promptUnlock()
        } else {
            Task {
                await vault.restore()
                if !vault.isSignedIn { loginWindow.show() }
            }
        }
    }

    /// Asks for Touch ID / password; on success, restores the (until-now untouched) session.
    private func promptUnlock() {
        appLock.authenticate { [weak self] success in
            guard let self, success else { return }
            NSApp.activate(ignoringOtherApps: true)
            Task { await self.vault.restore() }
        }
    }

    /// The passkey flow redirects the browser to `ente-cli://passkey` when the ceremony
    /// finishes. We register that scheme purely so this redirect brings Gans forward; the
    /// token is still retrieved by polling, so there's nothing to parse from the URL.
    func application(_ application: NSApplication, open urls: [URL]) {
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: Wiring

    private func wireStatusItem() {
        statusItemController.onQuickSearch = { [weak self] in self?.quickSearch.show() }
        statusItemController.onSettings = { [weak self] in self?.settingsWindow.show() }
        statusItemController.onLogin = { [weak self] in self?.loginWindow.show() }
        statusItemController.onCheckForUpdates = { [weak self] in self?.updateChecker.checkNow() }
        statusItemController.onUnlock = { [weak self] in self?.promptUnlock() }
    }

    private func wireQuickSearch() {
        quickSearch.entriesProvider = { [weak self] in
            guard let self else { return [] }
            // Opening Quick Search is the moment freshness matters: kick off a
            // (throttled) background sync while the current entries show immediately —
            // observeVault() live-updates the open panel if anything changed.
            self.refreshIfStale()
            return self.vault.entries
        }
        quickSearch.isSignedIn = { [weak self] in self?.vault.isSignedIn ?? false }
        quickSearch.onNeedsLogin = { [weak self] in self?.loginWindow.show() }
        quickSearch.isLocked = { [weak self] in self?.appLock.isLocked ?? false }
        quickSearch.onLocked = { [weak self] in self?.promptUnlock() }
        // A Quick Search commit gets the same confirmation as a menu copy (glyph blink,
        // and the honk in honk mode) — the status item owns the menu-bar glyph.
        quickSearch.onCommitted = { [weak self] in self?.statusItemController.confirmCopy() }
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

    // MARK: Auto refresh

    /// New codes added on other devices used to appear only after a manual
    /// "Refresh Now" (or a relaunch). Sync periodically, on wake from sleep, and when
    /// Quick Search opens — all funneled through a shared throttle.
    private var autoRefreshTimer: Timer?
    private var lastAutoRefresh = Date.distantPast
    /// Panel-open / wake refreshes are throttled to this; the timer refreshes anyway.
    private static let autoRefreshThrottle: TimeInterval = 60
    private static let autoRefreshInterval: TimeInterval = 15 * 60

    private func startAutoRefresh() {
        let timer = Timer(timeInterval: Self.autoRefreshInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshIfStale(ignoreThrottle: true) }
        }
        timer.tolerance = Self.autoRefreshInterval * 0.1
        RunLoop.main.add(timer, forMode: .common)
        autoRefreshTimer = timer

        // The 15-minute timer doesn't fire while the Mac sleeps; catch up on wake.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refreshIfStale() }
        }
    }

    private func refreshIfStale(ignoreThrottle: Bool = false) {
        guard vault.isSignedIn, !appLock.isLocked else { return }
        guard ignoreThrottle || Date().timeIntervalSince(lastAutoRefresh) > Self.autoRefreshThrottle else { return }
        lastAutoRefresh = Date()
        Task { await vault.refresh() }
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

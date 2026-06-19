import XCTest
@testable import Gans

final class PreferencesTests: XCTestCase {

    private func makeDefaults() -> UserDefaults {
        let suite = "GansTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    func testDefaults() {
        let prefs = Preferences(defaults: makeDefaults())
        XCTAssertEqual(prefs.hotkey, .default)
        XCTAssertEqual(prefs.deliveryMode, .type)
        XCTAssertTrue(prefs.alsoCopyWhenTyping)
    }

    func testHotkeyPersistsAndDecodes() {
        let defaults = makeDefaults()
        let custom = HotkeySpec(keyCode: 1, modifiers: 0x1000)
        do {
            let prefs = Preferences(defaults: defaults)
            prefs.hotkey = custom
        }
        let reloaded = Preferences(defaults: defaults)
        XCTAssertEqual(reloaded.hotkey, custom)
    }

    func testRecordUsageDeduplicatesOrdersAndPersists() {
        let defaults = makeDefaults()
        do {
            let prefs = Preferences(defaults: defaults)
            prefs.recordUsage("a")
            prefs.recordUsage("b")
            prefs.recordUsage("a") // re-using "a" moves it back to the front, no dupe
            XCTAssertEqual(prefs.recentlyUsedIDs, ["a", "b"])
        }
        // Survives a reload from the same defaults.
        XCTAssertEqual(Preferences(defaults: defaults).recentlyUsedIDs, ["a", "b"])
    }

    func testClipboardClearDefaultsAndDelay() {
        let prefs = Preferences(defaults: makeDefaults())
        XCTAssertTrue(prefs.clearClipboardEnabled)
        XCTAssertEqual(prefs.clearClipboardSeconds, 30)
        XCTAssertEqual(prefs.clipboardClearDelay, 30)
        prefs.clearClipboardEnabled = false
        XCTAssertNil(prefs.clipboardClearDelay)
    }

    func testDeliveryModePersists() {
        let defaults = makeDefaults()
        do {
            let prefs = Preferences(defaults: defaults)
            prefs.deliveryMode = .paste
            prefs.alsoCopyWhenTyping = false
        }
        let reloaded = Preferences(defaults: defaults)
        XCTAssertEqual(reloaded.deliveryMode, .paste)
        XCTAssertFalse(reloaded.alsoCopyWhenTyping)
    }
}

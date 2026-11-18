import XCTest
import Carbon.HIToolbox
@testable import Gans

final class HotkeySpecTests: XCTestCase {

    func testDefaultIsControlOptionSpace() {
        let spec = HotkeySpec.default
        XCTAssertEqual(spec.keyCode, UInt32(kVK_Space))
        XCTAssertEqual(spec.modifiers, UInt32(controlKey | optionKey))
    }

    func testCodableRoundTrip() throws {
        let spec = HotkeySpec(keyCode: 42, modifiers: 0x900)
        let data = try JSONEncoder().encode(spec)
        let decoded = try JSONDecoder().decode(HotkeySpec.self, from: data)
        XCTAssertEqual(decoded, spec)
    }

    /// The persisted payload must stay exactly `{"keyCode","modifiers"}`: it lives in
    /// `UserDefaults`, and a shape change would silently rewrite saved shortcuts.
    func testPersistedJSONKeys() throws {
        let data = try JSONEncoder().encode(HotkeySpec(keyCode: 42, modifiers: 0x900))
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        XCTAssertEqual(object?.keys.sorted(), ["keyCode", "modifiers"])
    }
}

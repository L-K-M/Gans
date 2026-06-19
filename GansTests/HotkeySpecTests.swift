import XCTest
import Carbon.HIToolbox
@testable import Gans

final class HotkeySpecTests: XCTestCase {

    func testDefaultIsControlOptionSpace() {
        let spec = HotkeySpec.default
        XCTAssertEqual(spec.keyCode, UInt32(kVK_Space))
        XCTAssertEqual(spec.displayString, "⌃⌥Space")
    }

    func testDisplayStringOrdersModifiers() {
        let spec = HotkeySpec(keyCode: UInt32(kVK_ANSI_E),
                              modifiers: UInt32(cmdKey | shiftKey | controlKey | optionKey))
        XCTAssertEqual(spec.displayString, "⌃⌥⇧⌘E")
    }

    func testCodableRoundTrip() throws {
        let spec = HotkeySpec(keyCode: 42, modifiers: 0x900)
        let data = try JSONEncoder().encode(spec)
        let decoded = try JSONDecoder().decode(HotkeySpec.self, from: data)
        XCTAssertEqual(decoded, spec)
    }

    func testCarbonModifierTranslation() {
        let carbon = KeyCodes.carbonModifiers(from: [.command, .shift])
        XCTAssertEqual(carbon & UInt32(cmdKey), UInt32(cmdKey))
        XCTAssertEqual(carbon & UInt32(shiftKey), UInt32(shiftKey))
        XCTAssertEqual(carbon & UInt32(optionKey), 0)
    }
}

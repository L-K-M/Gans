import XCTest
@testable import Gans

final class EnteAPIQueryTests: XCTestCase {

    func testPlusIsPercentEncoded() {
        // Ente's Go server form-decodes queries: a literal '+' arrives as a space, which
        // broke login for plus-addressed emails.
        XCTAssertEqual(EnteAPI.encodeQueryComponent("alice+ente@example.com"),
                       "alice%2Bente@example.com")
    }

    func testSeparatorsAndSpacesAreEncoded() {
        XCTAssertEqual(EnteAPI.encodeQueryComponent("a&b=c d"), "a%26b%3Dc%20d")
    }

    func testPlainValuesPassThrough() {
        XCTAssertEqual(EnteAPI.encodeQueryComponent("alice@example.com"), "alice@example.com")
        XCTAssertEqual(EnteAPI.encodeQueryComponent("12345"), "12345")
    }
}

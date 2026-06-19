import XCTest
@testable import Gans

final class SemanticVersionTests: XCTestCase {

    func testParsing() {
        XCTAssertEqual(SemanticVersion("v1.2.3")?.components, [1, 2, 3])
        XCTAssertEqual(SemanticVersion("1.4.0-beta.2")?.prerelease, "beta.2")
        XCTAssertNil(SemanticVersion("not-a-version"))
    }

    func testComparisonPadsComponents() {
        XCTAssertEqual(SemanticVersion("1.2")!, SemanticVersion("1.2.0")!)
        XCTAssertTrue(SemanticVersion("1.2.0")! < SemanticVersion("1.10.0")!)
        XCTAssertTrue(SemanticVersion("v0.9.0")! < SemanticVersion("0.10")!)
    }

    func testPrereleaseSortsBelowFinal() {
        XCTAssertTrue(SemanticVersion("1.2.0-beta")! < SemanticVersion("1.2.0")!)
    }
}

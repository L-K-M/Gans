import Foundation

/// Persists the *already-encrypted* authenticator entities Ente returns, plus the diff
/// cursor, in `~/Library/Application Support/Gans/`. These blobs are useless without the
/// authenticator key (kept in the Keychain), so the cache is safe at rest and lets the
/// menu populate instantly/offline at launch before a network refresh completes.
struct EntityCache {
    struct CachedEntity: Codable {
        let id: String
        let encryptedData: String
        let header: String
    }

    struct Snapshot: Codable {
        var entities: [CachedEntity] = []
        var sinceTime: Int64 = 0
    }

    private let fileURL: URL

    init() {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory,
                                                 in: .userDomainMask, appropriateFor: nil, create: true))
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        let directory = base.appendingPathComponent("Gans", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        self.fileURL = directory.appendingPathComponent("entities.json")
    }

    func load() -> Snapshot {
        guard let data = try? Data(contentsOf: fileURL),
              let snapshot = try? JSONDecoder().decode(Snapshot.self, from: data) else {
            return Snapshot()
        }
        return snapshot
    }

    func save(_ snapshot: Snapshot) {
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        try? data.write(to: fileURL, options: .atomic)
    }

    func clear() {
        try? FileManager.default.removeItem(at: fileURL)
    }
}

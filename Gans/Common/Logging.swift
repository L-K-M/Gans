import Foundation
import os

/// Namespaced loggers. We never log secrets, tokens, or codes.
enum Log {
    private static let subsystem = Bundle.main.bundleIdentifier ?? "ch.lkmc.Gans"
    static let app = Logger(subsystem: subsystem, category: "app")
    static let ente = Logger(subsystem: subsystem, category: "ente")
    static let hotkey = Logger(subsystem: subsystem, category: "hotkey")
    static let paste = Logger(subsystem: subsystem, category: "paste")
}

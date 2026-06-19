import Foundation

/// Minimal async URLSession client for the Ente API. Stateless apart from the optional
/// auth token, which is attached as `X-Auth-Token` on authenticated requests. Every
/// request advertises the Ente Auth client package, matching the official clients.
actor EnteAPI {
    /// Canonical production host. (`api.ente.com` is an alias of the same backend.)
    static let defaultBaseURL = URL(string: "https://api.ente.io")!
    static let clientPackage = "io.ente.auth"

    private let baseURL: URL
    private let session: URLSession
    private var authToken: String?

    init(baseURL: URL = EnteAPI.defaultBaseURL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    func setAuthToken(_ token: String?) { self.authToken = token }

    enum APIError: LocalizedError {
        case http(status: Int, body: String)
        case decoding(String)
        case transport(String)

        var errorDescription: String? {
            switch self {
            case .http(let status, let body):
                if status == 401 { return "Authentication failed or expired (HTTP 401)." }
                if status == 404 { return "Not found (HTTP 404). The account may not exist." }
                let trimmed = body.prefix(200)
                return "Ente returned HTTP \(status).\(trimmed.isEmpty ? "" : " \(trimmed)")"
            case .decoding(let what): return "Couldn't read the server response (\(what))."
            case .transport(let message): return "Network error: \(message)"
            }
        }
    }

    // MARK: Requests

    func get<T: Decodable>(_ type: T.Type, path: String, query: [URLQueryItem] = [], authenticated: Bool) async throws -> T {
        try await send(type, method: "GET", path: path, query: query, body: Optional<Data>.none, authenticated: authenticated)
    }

    func post<Body: Encodable, T: Decodable>(_ type: T.Type, path: String, body: Body, authenticated: Bool) async throws -> T {
        let encoded = try JSONEncoder().encode(body)
        return try await send(type, method: "POST", path: path, query: [], body: encoded, authenticated: authenticated)
    }

    private func send<T: Decodable>(_ type: T.Type, method: String, path: String,
                                    query: [URLQueryItem], body: Data?, authenticated: Bool) async throws -> T {
        var componentsURL = baseURL.appendingPathComponent(path)
        if !query.isEmpty {
            var comps = URLComponents(url: componentsURL, resolvingAgainstBaseURL: false)
            comps?.queryItems = query
            if let withQuery = comps?.url { componentsURL = withQuery }
        }

        var request = URLRequest(url: componentsURL)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(Self.clientPackage, forHTTPHeaderField: "X-Client-Package")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if authenticated, let authToken {
            request.setValue(authToken, forHTTPHeaderField: "X-Auth-Token")
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("No HTTP response.")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.http(status: http.statusCode, body: String(data: data, encoding: .utf8) ?? "")
        }

        if T.self == EmptyResponse.self, let empty = EmptyResponse() as? T {
            return empty
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }
}

/// Placeholder for endpoints that return no useful body (e.g. `/users/ott`).
struct EmptyResponse: Decodable { init() {} }

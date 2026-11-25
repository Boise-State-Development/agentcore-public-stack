import Foundation
import Synchronization

@testable import AgentCoreKit

/// A stub app-api: answers each request from a route table and records what it saw.
final class StubTransport: HTTPTransport {
    typealias Handler = @Sendable (URLRequest) throws -> (status: Int, body: String)

    private let handler: Handler
    private let seen = Mutex<[URLRequest]>([])

    init(_ handler: @escaping Handler) {
        self.handler = handler
    }

    var requests: [URLRequest] { seen.withLock { $0 } }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        seen.withLock { $0.append(request) }
        let (status, body) = try handler(request)
        let url = request.url ?? URL(fileURLWithPath: "/")
        guard let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil) else {
            throw URLError(.badServerResponse)
        }
        return (Data(body.utf8), response)
    }
}

/// A session store that lives only as long as the test.
final class MemorySessionStore: SessionStore {
    private let values = Mutex<[String: String]>([:])

    init(_ initial: [String: String] = [:]) {
        values.withLock { $0 = initial }
    }

    func load(for server: ServerConfiguration) -> String? {
        values.withLock { $0[server.baseURL.absoluteString] }
    }

    func save(_ sealed: String, for server: ServerConfiguration) throws {
        values.withLock { $0[server.baseURL.absoluteString] = sealed }
    }

    func delete(for server: ServerConfiguration) {
        _ = values.withLock { $0.removeValue(forKey: server.baseURL.absoluteString) }
    }
}

@MainActor
final class MemoryServerStore: ServerStore {
    var saved: ServerConfiguration?

    init(_ saved: ServerConfiguration? = nil) {
        self.saved = saved
    }

    func load() -> ServerConfiguration? { saved }
    func save(_ server: ServerConfiguration) { saved = server }
    func clear() { saved = nil }
}

enum Fixtures {
    static let base = "https://ai.example.edu/api"
    static let redirect = NativeRedirect(bundleIdentifier: "org.example.AgentCore")
    static let device = DeviceDescription(model: "iPhone", appVersion: "1.40.0")

    static var server: ServerConfiguration {
        get throws { try ServerConfiguration(validating: base) }
    }

    static let providers = """
        {"providers": [{"provider_id": "ms-entra-id", "display_name": "Example University",
        "logo_url": null, "button_color": null}], "cognito_login_visible": false}
        """

    static let user = """
        {"user_id": "u-1", "email": "member@example.edu", "name": "A Member",
        "roles": ["Faculty"], "picture": null, "csrf_token": "ignored"}
        """

    static let exchange = """
        {"session": "sealed-value", "expires_at": 1800000000, "user": \(user)}
        """
}

extension URLRequest {
    var path: String { url?.path() ?? "" }

    func query(_ name: String) -> String? {
        guard let url else { return nil }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == name })?.value
    }

    var jsonBody: [String: String] {
        guard let httpBody, let object = try? JSONSerialization.jsonObject(with: httpBody) else { return [:] }
        return object as? [String: String] ?? [:]
    }
}

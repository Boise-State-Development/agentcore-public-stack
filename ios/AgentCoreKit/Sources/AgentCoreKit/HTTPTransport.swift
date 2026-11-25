import Foundation

/// Sends one request and returns the body and the HTTP response.
///
/// A seam rather than a direct `URLSession` call so the client and the
/// sign-in flow can be tested against a stub server with no network.
public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

/// The real transport. Uses an ephemeral session: the app authenticates with
/// the `Authorization` header alone, so it must never pick up or keep a
/// cookie, least of all a web session cookie for the same host.
public struct URLSessionTransport: HTTPTransport {
    private let session: URLSession

    public init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.urlCache = nil
        self.session = URLSession(configuration: configuration)
    }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }
        return (data, http)
    }
}

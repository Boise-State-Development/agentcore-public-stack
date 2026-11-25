import Foundation

/// app-api, as the app reaches it.
///
/// Every authenticated call presents the session as `Authorization: BFF <sealed>`,
/// the transport the terminal client already uses (`SessionRefreshMiddleware`).
/// The scheme is `BFF`, not `Bearer`, because the value is a sealed session id
/// and not a token.
public struct AgentCoreClient: Sendable {
    public let server: ServerConfiguration
    private let transport: any HTTPTransport

    public init(server: ServerConfiguration, transport: any HTTPTransport = URLSessionTransport()) {
        self.server = server
        self.transport = transport
    }

    /// `GET /auth/providers`. Unauthenticated; also proves the server answers.
    public func signInOptions() async throws(AgentCoreClientError) -> SignInOptions {
        let (data, _) = try await send(request(path: "auth/providers"))
        return try decode(SignInOptions.self, from: data)
    }

    /// `GET /auth/session`: who the session belongs to.
    public func currentUser(session: String) async throws(AgentCoreClientError) -> SessionUser {
        let (data, _) = try await send(request(path: "auth/session", session: session))
        return try decode(SessionUser.self, from: data)
    }

    /// `POST /auth/native/exchange`: trade a handoff code and the verifier
    /// for a session. The code is single-use and dies after 120 seconds.
    public func exchange(
        handoff: String,
        verifier: String,
        device: DeviceDescription
    ) async throws(AgentCoreClientError) -> NativeSession {
        var request = request(path: "auth/native/exchange", method: "POST")
        let body = ExchangeRequest(
            handoff: handoff,
            codeVerifier: verifier,
            deviceModel: device.model,
            appVersion: device.appVersion
        )
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try encode(body)

        let (data, _) = try await send(request)
        let response = try decode(ExchangeResponse.self, from: data)
        return NativeSession(
            sealed: response.session,
            expiresAt: Date(timeIntervalSince1970: TimeInterval(response.expiresAt)),
            user: response.user
        )
    }

    /// `POST /auth/logout` with the session, so the server deletes it.
    public func signOut(session: String) async throws(AgentCoreClientError) {
        _ = try await send(request(path: "auth/logout", method: "POST", session: session))
    }

    // MARK: - Plumbing

    private func request(path: String, method: String = "GET", session: String? = nil) -> URLRequest {
        var request = URLRequest(url: server.baseURL.appending(path: path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let session {
            request.setValue("BFF \(session)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func send(_ request: URLRequest) async throws(AgentCoreClientError) -> (Data, HTTPURLResponse) {
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await transport.send(request)
        } catch {
            throw .unreachable
        }
        switch response.statusCode {
        case 200..<300:
            return (data, response)
        case 401:
            throw .unauthorized
        case 404:
            throw .notSupported
        case 400:
            throw .rejected
        default:
            throw .serverError(status: response.statusCode)
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws(AgentCoreClientError) -> T {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw .unexpectedResponse
        }
    }

    private func encode<T: Encodable>(_ value: T) throws(AgentCoreClientError) -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        do {
            return try encoder.encode(value)
        } catch {
            throw .unexpectedResponse
        }
    }
}

public enum AgentCoreClientError: Error, Equatable, Sendable {
    /// No HTTP answer: offline, wrong host, TLS failure.
    case unreachable
    /// The session is gone: expired, signed out elsewhere, or a rotated key.
    case unauthorized
    /// The route does not exist here, e.g. a server without native sign-in.
    case notSupported
    /// The server refused the request as made, e.g. a spent handoff code.
    case rejected
    case serverError(status: Int)
    case unexpectedResponse
}

private struct ExchangeRequest: Encodable {
    let handoff: String
    let codeVerifier: String
    let deviceModel: String
    let appVersion: String
}

private struct ExchangeResponse: Decodable {
    let session: String
    let expiresAt: Int
    let user: SessionUser
}

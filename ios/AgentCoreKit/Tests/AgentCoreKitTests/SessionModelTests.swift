import Foundation
import Testing

@testable import AgentCoreKit

@MainActor
@Suite("SessionModel")
struct SessionModelTests {
    /// A stub server that speaks the whole native sign-in contract.
    private static func server(sessionStatus: Int = 200, exchangeStatus: Int = 200) -> StubTransport {
        StubTransport { request in
            switch (request.httpMethod ?? "GET", request.path) {
            case ("GET", "/api/auth/providers"): (200, Fixtures.providers)
            case ("GET", "/api/auth/session"): (sessionStatus, Fixtures.user)
            case ("POST", "/api/auth/native/exchange"): (exchangeStatus, Fixtures.exchange)
            case ("POST", "/api/auth/logout"): (200, #"{"post_logout_url": null}"#)
            default: (404, "{}")
            }
        }
    }

    private static func model(
        transport: StubTransport,
        servers: MemoryServerStore = MemoryServerStore(),
        sessions: MemorySessionStore = MemorySessionStore()
    ) -> SessionModel {
        SessionModel(
            serverStore: servers,
            sessionStore: sessions,
            redirect: Fixtures.redirect,
            device: Fixtures.device,
            makeClient: { AgentCoreClient(server: $0, transport: transport) }
        )
    }

    /// Plays the sign-in sheet: checks what it was opened with, then lands on `callback`.
    private static func sheet(
        returning callback: String,
        opened: @escaping @MainActor (URL, String) -> Void = { _, _ in }
    ) -> WebAuthenticator {
        { url, scheme in
            opened(url, scheme)
            guard let result = URL(string: callback) else { throw URLError(.badURL) }
            return result
        }
    }

    @Test("a first launch asks for a server")
    func firstLaunch() async {
        let model = Self.model(transport: Self.server())
        await model.start()
        #expect(model.phase == .needsServer)
    }

    @Test("a mistyped address is refused before any request")
    func invalidServer() async {
        let transport = Self.server()
        let model = Self.model(transport: transport)
        await model.connect(to: "http://ai.example.edu")
        #expect(model.problem == .invalidServer(.insecureScheme(host: "ai.example.edu")))
        #expect(transport.requests.isEmpty)
    }

    @Test("connecting keeps the server and lists its sign-in options")
    func connect() async throws {
        let servers = MemoryServerStore()
        let model = Self.model(transport: Self.server(), servers: servers)
        await model.connect(to: Fixtures.base)
        #expect(model.phase == .signedOut)
        #expect(model.options.providers.map(\.id) == ["ms-entra-id"])
        #expect(servers.saved == (try Fixtures.server))
    }

    @Test("a server that does not answer is not kept")
    func unreachableServer() async {
        let servers = MemoryServerStore()
        let model = Self.model(transport: StubTransport { _ in throw URLError(.cannotFindHost) }, servers: servers)
        await model.connect(to: Fixtures.base)
        #expect(model.problem == .unreachable)
        #expect(servers.saved == nil)
    }

    @Test("signing in exchanges the handoff and keeps the session")
    func signIn() async throws {
        let transport = Self.server()
        let sessions = MemorySessionStore()
        let model = Self.model(transport: transport, sessions: sessions)
        await model.connect(to: Fixtures.base)

        var openedWith: (URL, String)?
        await model.signIn(
            with: model.options.providers.first,
            using: Self.sheet(returning: "org.example.agentcore://auth/callback?handoff=abc") { openedWith = ($0, $1) }
        )

        #expect(
            model.phase
                == .signedIn(
                    SessionUser(userId: "u-1", email: "member@example.edu", name: "A Member", roles: ["Faculty"])))
        #expect(sessions.load(for: try Fixtures.server) == "sealed-value")
        let opened = try #require(openedWith)
        #expect(opened.1 == "org.example.agentcore")
        #expect(opened.0.absoluteString.contains("provider=ms-entra-id"))

        let exchange = try #require(transport.requests.first { $0.path == "/api/auth/native/exchange" })
        let challenge = try #require(
            URLComponents(url: opened.0, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "code_challenge" }?.value)
        let verifier = try #require(exchange.jsonBody["code_verifier"])
        #expect(PKCEPair(verifier: verifier).challenge == challenge)
    }

    @Test("closing the sheet is not an error")
    func cancelled() async {
        let model = Self.model(transport: Self.server())
        await model.connect(to: Fixtures.base)
        await model.signIn(with: nil) { _, _ in throw WebAuthenticationCancelled() }
        #expect(model.phase == .signedOut)
        #expect(model.problem == nil)
    }

    @Test("the server's reason for a failed sign-in reaches the user")
    func serverReportedFailure() async {
        let model = Self.model(transport: Self.server())
        await model.connect(to: Fixtures.base)
        await model.signIn(
            with: nil, using: Self.sheet(returning: "org.example.agentcore://auth/callback?error=bad_nonce"))
        #expect(model.phase == .signedOut)
        #expect(model.problem == .signInFailed(reason: "bad_nonce"))
    }

    @Test("a server without native sign-in says so")
    func notSupported() async {
        let model = Self.model(transport: Self.server(exchangeStatus: 404))
        await model.connect(to: Fixtures.base)
        await model.signIn(with: nil, using: Self.sheet(returning: "org.example.agentcore://auth/callback?handoff=abc"))
        #expect(model.problem == .signInNotSupported)
    }

    @Test("a saved session is restored on launch")
    func restore() async throws {
        let server = try Fixtures.server
        let model = Self.model(
            transport: Self.server(),
            servers: MemoryServerStore(server),
            sessions: MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        )
        await model.start()
        guard case .signedIn(let user) = model.phase else {
            Issue.record("expected signed in, got \(model.phase)")
            return
        }
        #expect(user.userId == "u-1")
    }

    @Test("an expired saved session is dropped and the user is told")
    func expired() async throws {
        let server = try Fixtures.server
        let sessions = MemorySessionStore([server.baseURL.absoluteString: "stale"])
        let model = Self.model(
            transport: Self.server(sessionStatus: 401), servers: MemoryServerStore(server), sessions: sessions)
        await model.start()
        #expect(model.phase == .signedOut)
        #expect(model.problem == .sessionExpired)
        #expect(sessions.load(for: server) == nil)
    }

    @Test("being offline keeps a saved session rather than signing out")
    func offline() async throws {
        let server = try Fixtures.server
        let sessions = MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        let model = Self.model(
            transport: StubTransport { _ in throw URLError(.notConnectedToInternet) },
            servers: MemoryServerStore(server),
            sessions: sessions
        )
        await model.start()
        #expect(model.phase == .cannotReach)
        #expect(sessions.load(for: server) == "sealed-value")
    }

    @Test("signing out drops the local session and tells the server")
    func signOut() async throws {
        let server = try Fixtures.server
        let transport = Self.server()
        let sessions = MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        let model = Self.model(transport: transport, servers: MemoryServerStore(server), sessions: sessions)
        await model.start()
        await model.signOut()

        #expect(model.phase == .signedOut)
        #expect(sessions.load(for: server) == nil)
        let logout = try #require(transport.requests.first { $0.path == "/api/auth/logout" })
        #expect(logout.value(forHTTPHeaderField: "Authorization") == "BFF sealed-value")
    }

    @Test("signing out of a restored session offers the server's providers again")
    func signOutRestoresProviders() async throws {
        let server = try Fixtures.server
        let model = Self.model(
            transport: Self.server(),
            servers: MemoryServerStore(server),
            sessions: MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        )
        await model.start()
        await model.signOut()
        #expect(model.options.providers.map(\.id) == ["ms-entra-id"])
    }

    @Test("changing server forgets the old server's session")
    func changeServer() async throws {
        let server = try Fixtures.server
        let servers = MemoryServerStore(server)
        let sessions = MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        let model = Self.model(transport: Self.server(), servers: servers, sessions: sessions)
        await model.start()
        model.changeServer()

        #expect(model.phase == .needsServer)
        #expect(servers.saved == nil)
        #expect(sessions.load(for: server) == nil)
    }
}

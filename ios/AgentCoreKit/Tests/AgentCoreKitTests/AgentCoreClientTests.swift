import Foundation
import Testing

@testable import AgentCoreKit

@Suite("AgentCoreClient")
struct AgentCoreClientTests {
    @Test("reads the sign-in options")
    func signInOptions() async throws {
        let stub = StubTransport { _ in (200, Fixtures.providers) }
        let options = try await AgentCoreClient(server: try Fixtures.server, transport: stub).signInOptions()
        #expect(options.providers == [AuthProvider(id: "ms-entra-id", displayName: "Example University")])
        #expect(options.offersHostedSignIn == false)
        #expect(stub.requests.first?.path == "/api/auth/providers")
        #expect(stub.requests.first?.value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test("an older server that omits the hosted flag still offers hosted sign-in")
    func hostedDefault() async throws {
        let stub = StubTransport { _ in (200, #"{"providers": []}"#) }
        let options = try await AgentCoreClient(server: try Fixtures.server, transport: stub).signInOptions()
        #expect(options == .hostedOnly)
    }

    @Test("presents the session as Authorization: BFF, never Bearer")
    func sessionHeader() async throws {
        let stub = StubTransport { _ in (200, Fixtures.user) }
        let user = try await AgentCoreClient(server: try Fixtures.server, transport: stub)
            .currentUser(session: "sealed-value")
        #expect(user == SessionUser(userId: "u-1", email: "member@example.edu", name: "A Member", roles: ["Faculty"]))
        #expect(stub.requests.first?.path == "/api/auth/session")
        #expect(stub.requests.first?.value(forHTTPHeaderField: "Authorization") == "BFF sealed-value")
    }

    @Test("a 401 means the session is gone")
    func unauthorized() async throws {
        let stub = StubTransport { _ in (401, #"{"detail": "No active session."}"#) }
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        await #expect(throws: AgentCoreClientError.unauthorized) {
            try await client.currentUser(session: "stale")
        }
    }

    @Test("exchanges the handoff with the verifier and device facts")
    func exchange() async throws {
        let stub = StubTransport { _ in (200, Fixtures.exchange) }
        let session = try await AgentCoreClient(server: try Fixtures.server, transport: stub)
            .exchange(handoff: "abc123", verifier: "the-verifier", device: Fixtures.device)

        #expect(session.sealed == "sealed-value")
        #expect(session.expiresAt == Date(timeIntervalSince1970: 1_800_000_000))
        #expect(session.user.userId == "u-1")

        let request = try #require(stub.requests.first)
        #expect(request.httpMethod == "POST")
        #expect(request.path == "/api/auth/native/exchange")
        #expect(
            request.jsonBody == [
                "handoff": "abc123",
                "code_verifier": "the-verifier",
                "device_model": "iPhone",
                "app_version": "1.40.0",
            ]
        )
    }

    @Test("a refused exchange and a server without native sign-in are told apart")
    func exchangeFailures() async throws {
        let refused = AgentCoreClient(server: try Fixtures.server, transport: StubTransport { _ in (400, "{}") })
        await #expect(throws: AgentCoreClientError.rejected) {
            try await refused.exchange(handoff: "spent", verifier: "v", device: Fixtures.device)
        }
        let missing = AgentCoreClient(server: try Fixtures.server, transport: StubTransport { _ in (404, "{}") })
        await #expect(throws: AgentCoreClientError.notSupported) {
            try await missing.exchange(handoff: "h", verifier: "v", device: Fixtures.device)
        }
    }

    @Test("no answer at all is unreachable")
    func unreachable() async throws {
        let client = AgentCoreClient(
            server: try Fixtures.server, transport: StubTransport { _ in throw URLError(.timedOut) })
        await #expect(throws: AgentCoreClientError.unreachable) {
            try await client.signInOptions()
        }
    }
}

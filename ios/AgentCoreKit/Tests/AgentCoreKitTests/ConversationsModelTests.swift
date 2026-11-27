import Foundation
import Synchronization
import Testing

@testable import AgentCoreKit

@MainActor
@Suite("ConversationsModel")
struct ConversationsModelTests {
    /// Counts how often the source reported the session gone.
    private final class EndedCounter {
        var count = 0
    }

    private static func list(
        _ transport: StubTransport,
        ended: EndedCounter = EndedCounter()
    ) throws -> ConversationListModel {
        let client = AgentCoreClient(server: try Fixtures.server, transport: transport)
        return ConversationListModel(
            source: ConversationSource(client: client, sealed: "sealed-value") { ended.count += 1 })
    }

    /// Answers the first and second list pages by `next_token`.
    private static func pagedServer() -> StubTransport {
        StubTransport { request in
            switch request.query("next_token") {
            case nil: (200, ConversationFixtures.firstPage)
            case "page-2": (200, ConversationFixtures.secondPage)
            default: (500, "{}")
            }
        }
    }

    // MARK: - Client

    @Test("lists a page with the session header, the page size and the token")
    func listRequest() async throws {
        let stub = Self.pagedServer()
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        _ = try await client.conversations(session: "sealed-value", limit: 30)
        _ = try await client.conversations(session: "sealed-value", limit: 30, after: "page-2")

        let first = try #require(stub.requests.first)
        #expect(first.path == "/api/sessions")
        #expect(first.query("limit") == "30")
        #expect(first.query("next_token") == nil)
        #expect(first.value(forHTTPHeaderField: "Authorization") == "BFF sealed-value")
        #expect(stub.requests.last?.query("next_token") == "page-2")
    }

    @Test("a transcript follows the pages to the end, and escapes the id")
    func transcriptPages() async throws {
        let stub = StubTransport { request in
            switch request.query("next_token") {
            case nil:
                (
                    200,
                    #"{"messages": [{"id": "m-0", "role": "user", "content": [{"type": "text", "text": "Hi"}]}], "nextToken": "MQ=="}"#
                )
            default:
                (
                    200,
                    #"{"messages": [{"id": "m-1", "role": "assistant", "content": [{"type": "text", "text": "Hello"}]}]}"#
                )
            }
        }
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        let transcript = try await client.transcript(session: "sealed-value", conversationId: "a/b")

        #expect(transcript.messages.map(\.id) == ["m-0", "m-1"])
        #expect(stub.requests.count == 2)
        #expect(stub.requests.first?.url?.absoluteString.contains("/api/sessions/a%2Fb/messages") == true)
        #expect(stub.requests.last?.query("next_token") == "MQ==")
        #expect(stub.requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "BFF sealed-value" })
    }

    @Test("a server that hands back the same token doesn't loop")
    func repeatedToken() async throws {
        let stub = StubTransport { _ in (200, #"{"messages": [], "nextToken": "same"}"#) }
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        _ = try await client.transcript(session: "sealed-value", conversationId: "c-1")
        #expect(stub.requests.count == 2)
    }

    // MARK: - List

    @Test("the first page loads, and scrolling to the end loads the next without duplicates")
    func paging() async throws {
        let list = try Self.list(Self.pagedServer())
        #expect(list.hasLoaded == false)

        await list.refresh()
        #expect(list.hasLoaded)
        #expect(list.conversations.map(\.id) == ["c-3", "c-2", "c-1"])
        #expect(list.canLoadMore)

        // Only the last row asks for more.
        await list.loadMore(after: list.conversations[0])
        #expect(list.conversations.count == 3)

        await list.loadMore(after: list.conversations[2])
        #expect(list.conversations.map(\.id) == ["c-3", "c-2", "c-1", "c-0"])
        #expect(list.canLoadMore == false)
    }

    @Test("an empty account loads as empty, not as a problem")
    func empty() async throws {
        let list = try Self.list(StubTransport { _ in (200, ConversationFixtures.emptyPage) })
        await list.refresh()
        #expect(list.hasLoaded)
        #expect(list.conversations.isEmpty)
        #expect(list.problem == nil)
    }

    @Test("a failed refresh keeps the rows already shown")
    func refreshFailureKeepsRows() async throws {
        let online = Mutex(true)
        let list = try Self.list(
            StubTransport { _ in
                guard online.withLock({ $0 }) else { throw URLError(.notConnectedToInternet) }
                return (200, ConversationFixtures.firstPage)
            })
        await list.refresh()
        online.withLock { $0 = false }
        await list.refresh()

        #expect(list.problem == .unreachable)
        #expect(list.conversations.count == 3)
    }

    @Test("a failed next page is offered again and changes nothing else")
    func loadMoreFailure() async throws {
        let list = try Self.list(
            StubTransport { request in
                request.query("next_token") == nil ? (200, ConversationFixtures.firstPage) : (503, "{}")
            })
        await list.refresh()
        await list.loadMore()
        #expect(list.loadMoreFailed)
        #expect(list.canLoadMore)
        #expect(list.problem == nil)
    }

    @Test("a 401 while listing ends the session")
    func listUnauthorized() async throws {
        let ended = EndedCounter()
        let list = try Self.list(StubTransport { _ in (401, #"{"detail": "No active session."}"#) }, ended: ended)
        await list.refresh()
        #expect(ended.count == 1)
    }

    // MARK: - Transcript

    @Test("a transcript loads, and a missing conversation says so")
    func transcript() async throws {
        let list = try Self.list(
            StubTransport { request in
                request.path.contains("/c-1/") ? (200, ConversationFixtures.transcript) : (404, "{}")
            })
        let found = list.transcript(for: "c-1")
        await found.load()
        #expect(found.transcript?.pendingPrompts.count == 1)
        #expect(found.problem == nil)

        let missing = list.transcript(for: "c-9")
        await missing.load()
        #expect(missing.transcript == nil)
        #expect(missing.problem == .notFound)
    }

    @Test("a 401 while reading a transcript ends the session")
    func transcriptUnauthorized() async throws {
        let ended = EndedCounter()
        let list = try Self.list(StubTransport { _ in (401, "{}") }, ended: ended)
        await list.transcript(for: "c-1").load()
        #expect(ended.count == 1)
    }

    // MARK: - With SessionModel

    @Test("an expired session found while listing goes back to sign-in, like one found at launch")
    func expiredSessionSignsOut() async throws {
        let sessionStatus = Mutex(200)
        let transport = StubTransport { request in
            switch request.path {
            case "/api/auth/providers": (200, Fixtures.providers)
            case "/api/auth/session": (200, Fixtures.user)
            case "/api/sessions": (sessionStatus.withLock { $0 }, ConversationFixtures.firstPage)
            default: (404, "{}")
            }
        }
        let server = try Fixtures.server
        let sessions = MemorySessionStore([server.baseURL.absoluteString: "sealed-value"])
        let model = SessionModel(
            serverStore: MemoryServerStore(server),
            sessionStore: sessions,
            redirect: Fixtures.redirect,
            device: Fixtures.device,
            makeClient: { AgentCoreClient(server: $0, transport: transport) }
        )
        await model.start()
        let list = try #require(model.makeConversationList())

        await list.refresh()
        #expect(list.conversations.count == 3)

        sessionStatus.withLock { $0 = 401 }
        await list.refresh()
        #expect(model.phase == .signedOut)
        #expect(model.problem == .sessionExpired)
        #expect(sessions.load(for: server) == nil)
        #expect(model.makeConversationList() == nil)
    }
}

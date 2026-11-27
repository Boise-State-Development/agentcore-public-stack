import Foundation
import Synchronization
import Testing

@testable import AgentCoreKit

@MainActor
@Suite("InboxModel")
struct InboxModelTests {
    private final class EndedCounter {
        var count = 0
    }

    private static func api(_ transport: StubTransport, ended: EndedCounter = EndedCounter()) throws -> SignedInAPI {
        SignedInAPI(client: AgentCoreClient(server: try Fixtures.server, transport: transport), sealed: "sealed-value")
        {
            ended.count += 1
        }
    }

    /// A review model that follows a run without waiting between asks.
    private static func review(
        _ transport: StubTransport, giveUpAfter: Duration = .seconds(60)
    ) throws
        -> InboxReviewModel
    {
        InboxReviewModel(
            notificationId: InboxFixtures.waitingId, label: "Morning grant check", api: try api(transport),
            pollInterval: .zero, giveUpAfter: giveUpAfter)
    }

    /// A server for one waiting row: its prompts, the respond route and the run.
    private static func server(
        respondStatus: Int = 202, runStatuses: [String] = ["started", "completed"]
    )
        -> StubTransport
    {
        let remaining = Mutex(runStatuses)
        return StubTransport { request in
            switch (request.httpMethod ?? "GET", request.path) {
            case ("GET", "/api/notifications/\(InboxFixtures.waitingId)/prompts"):
                return (200, InboxFixtures.prompts)
            case ("POST", "/api/notifications/\(InboxFixtures.waitingId)/respond"):
                return (respondStatus, respondStatus == 202 ? InboxFixtures.respond() : #"{"detail": {"code": "x"}}"#)
            case ("GET", "/api/runs/run-bbbbbbbbbbbb"):
                let status = remaining.withLock { $0.count > 1 ? $0.removeFirst() : ($0.first ?? "started") }
                return (200, InboxFixtures.run(status))
            default:
                return (404, "{}")
            }
        }
    }

    /// Fills every fixture prompt with an answer that can be sent.
    private static func answerAll(_ review: InboxReviewModel) {
        review.drafts["i-approve"] = .approval(true)
        var questions = QuestionsDraft()
        questions.skipped = true
        review.drafts["i-ask"] = .questions(questions)
        guard case .form(var form) = review.drafts["i-form"] else { return }
        form.values["title"] = .text("New issue")
        review.drafts["i-form"] = .form(form)
    }

    // MARK: - Client

    @Test("lists the inbox with the session header, the page size and the cursor")
    func inboxRequest() async throws {
        let stub = StubTransport { _ in (200, InboxFixtures.firstPage) }
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        _ = try await client.inbox(session: "sealed-value", limit: 30)
        _ = try await client.inbox(session: "sealed-value", limit: 30, after: "cursor-2")
        let first = try #require(stub.requests.first)
        #expect(first.path == "/api/notifications")
        #expect(first.query("limit") == "30")
        #expect(first.query("cursor") == nil)
        #expect(first.value(forHTTPHeaderField: "Authorization") == "BFF sealed-value")
        #expect(stub.requests.last?.query("cursor") == "cursor-2")
    }

    @Test("sends answers in the respond route's shape, with the user's own keys untouched")
    func respondBody() async throws {
        let stub = StubTransport { _ in (202, InboxFixtures.respond()) }
        let client = AgentCoreClient(server: try Fixtures.server, transport: stub)
        let runId = try await client.respond(
            session: "sealed-value", notificationId: InboxFixtures.waitingId,
            answers: [
                ("i-approve", .string("approved")),
                ("i-ask", .object(["answers": .object(["Start_Date": .object(["selected": .array([])])])])),
            ])
        #expect(runId == "run-bbbbbbbbbbbb")

        let request = try #require(stub.requests.first)
        #expect(request.httpMethod == "POST")
        #expect(request.path == "/api/notifications/\(InboxFixtures.waitingId)/respond")
        let body = try #require(request.httpBody)
        let object = try #require(try JSONSerialization.jsonObject(with: body) as? [String: Any])
        let responses = try #require(object["responses"] as? [[String: Any]])
        #expect(responses.map { $0["interruptId"] as? String } == ["i-approve", "i-ask"])
        #expect(responses[0]["response"] as? String == "approved")
        let answers = (responses[1]["response"] as? [String: Any])?["answers"] as? [String: Any]
        #expect(answers?.keys.first == "Start_Date")
    }

    @Test("marking read posts to the row, and mark all read to read-all")
    func markRead() async throws {
        let stub = StubTransport { request in
            request.path == "/api/notifications" ? (200, InboxFixtures.firstPage) : (204, "")
        }
        let inbox = InboxModel(api: try Self.api(stub))
        await inbox.refresh()
        let first = try #require(inbox.items.first)

        await inbox.markRead(first)
        #expect(inbox.items.first?.isRead == true)
        #expect(inbox.unreadCount == 3)
        #expect(stub.requests.last?.path == "/api/notifications/\(first.id)/read")
        #expect(stub.requests.last?.httpMethod == "POST")

        // Already read: nothing sent.
        let count = stub.requests.count
        await inbox.markRead(try #require(inbox.items.first))
        #expect(stub.requests.count == count)

        await inbox.markAllRead()
        #expect(inbox.unreadCount == 0)
        #expect(inbox.items.allSatisfy { $0.isRead })
        #expect(stub.requests.last?.path == "/api/notifications/read-all")
    }

    // MARK: - List

    @Test("the inbox pages by cursor without duplicates")
    func paging() async throws {
        let stub = StubTransport { request in
            request.query("cursor") == nil ? (200, InboxFixtures.firstPage) : (200, InboxFixtures.secondPage)
        }
        let inbox = InboxModel(api: try Self.api(stub))
        await inbox.refresh()
        #expect(inbox.items.count == 6)
        #expect(inbox.unreadCount == 4)
        #expect(inbox.canLoadMore)
        await inbox.loadMore(after: try #require(inbox.items.last))
        #expect(inbox.items.count == 7)
        #expect(inbox.canLoadMore == false)
    }

    @Test("a 401 from the inbox ends the session")
    func unauthorized() async throws {
        let ended = EndedCounter()
        let inbox = InboxModel(api: try Self.api(StubTransport { _ in (401, "{}") }, ended: ended))
        await inbox.refresh()
        #expect(ended.count == 1)
    }

    // MARK: - Review

    @Test("answering sends everything once and follows the run until it finishes")
    func answerAndFollow() async throws {
        let stub = Self.server()
        let review = try Self.review(stub)
        await review.load()
        #expect(review.phase == .answering)
        #expect(review.prompts.count == 3)
        #expect(review.canSend == false)

        Self.answerAll(review)
        #expect(review.canSend)
        await review.send()

        #expect(review.phase == .ended(.finished))
        #expect(stub.requests.filter { $0.path.hasSuffix("/respond") }.count == 1)
        #expect(stub.requests.filter { $0.path.hasPrefix("/api/runs/") }.count == 2)
    }

    @Test("a run that pauses again says so")
    func waitingAgain() async throws {
        let review = try Self.review(Self.server(runStatuses: ["needs_user"]))
        await review.load()
        Self.answerAll(review)
        await review.send()
        #expect(review.phase == .ended(.waitingAgain))
    }

    @Test("a run that never reports back is given up on")
    func lost() async throws {
        let review = try Self.review(Self.server(runStatuses: ["started"]), giveUpAfter: .milliseconds(50))
        await review.load()
        Self.answerAll(review)
        await review.send()
        #expect(review.phase == .lost)
    }

    @Test(
        "answered elsewhere and expired are told apart",
        arguments: [(409, InboxReviewModel.Phase.alreadyAnswered), (410, .expired)])
    func settled(status: Int, phase: InboxReviewModel.Phase) async throws {
        let review = try Self.review(Self.server(respondStatus: status))
        await review.load()
        Self.answerAll(review)
        await review.send()
        #expect(review.phase == phase)
    }

    @Test("a send that fails for a passing reason keeps the answers on screen to send again")
    func sendFailure() async throws {
        let review = try Self.review(Self.server(respondStatus: 503))
        await review.load()
        Self.answerAll(review)
        await review.send()
        #expect(review.phase == .answering)
        #expect(review.sendProblem == .serverError)
        #expect(review.canSend)
    }

    @Test("a row whose prompts can't be read here sends the user to the conversation")
    func promptsGone() async throws {
        let review = try Self.review(StubTransport { _ in (404, #"{"detail": "Not found"}"#) })
        await review.load()
        #expect(review.phase == .openConversationInstead)
    }

    @Test("a pause with a prompt only the conversation can answer is not answered here")
    func notAnswerableHere() async throws {
        let review = try Self.review(
            StubTransport { _ in (200, #"{"prompts": [{"interruptId": "b", "kind": "browser_login"}]}"#) })
        await review.load()
        #expect(review.phase == .openConversationInstead)
    }
}

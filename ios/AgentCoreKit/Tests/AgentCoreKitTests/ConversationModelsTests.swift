import Foundation
import Testing

@testable import AgentCoreKit

@Suite("ConversationModels")
struct ConversationModelsTests {
    private func page(_ json: String) throws -> ConversationPage {
        try JSONDecoder().decode(ConversationPage.self, from: Data(json.utf8))
    }

    @Test("reads a page of conversations and ignores what the list doesn't show")
    func firstPage() throws {
        let page = try page(ConversationFixtures.firstPage)
        #expect(page.conversations.map(\.id) == ["c-3", "c-2", "c-1"])
        #expect(page.nextToken == "page-2")

        let newest = try #require(page.conversations.first)
        #expect(newest.title == "Draft the welcome email")
        #expect(newest.unread)
        #expect(newest.awaitingInput == nil)
        #expect(newest.messageCount == 6)
        let expected = try #require(ServerTimestamp.parse("2026-11-27T15:04:05Z"))
        let lastMessageAt = try #require(newest.lastMessageAt)
        #expect(abs(lastMessageAt.timeIntervalSince(expected)) < 1)
    }

    @Test("a row without an id is skipped, not the page")
    func rowWithoutId() throws {
        #expect(try page(ConversationFixtures.firstPage).conversations.count == 3)
    }

    @Test("awaiting input names the prompt, and an unknown kind still counts")
    func awaitingInput() throws {
        let rows = try page(ConversationFixtures.firstPage).conversations
        #expect(rows[1].awaitingInput == .toolApproval)
        #expect(rows[2].awaitingInput == .other("a_kind_from_the_future"))
    }

    @Test(
        "every awaiting-input kind the server sends has a name",
        arguments: [
            ("oauth", AwaitingInput.connectAccount),
            ("tool_approval", .toolApproval),
            ("user_question", .question),
            ("browser_login", .browserSignIn),
            ("mcp_elicitation", .form),
        ])
    func awaitingInputKinds(wire: String, expected: AwaitingInput) {
        #expect(AwaitingInput(wireValue: wire) == expected)
    }

    @Test("the last page has no next token, and an empty one reads as none")
    func lastPage() throws {
        #expect(try page(ConversationFixtures.secondPage).nextToken == nil)
        #expect(try page(#"{"sessions": [], "nextToken": ""}"#).nextToken == nil)
    }

    @Test("a body that isn't a page fails to decode")
    func notAPage() {
        #expect(throws: (any Error).self) {
            try page(#"{"detail": "nope"}"#)
        }
    }

    @Test(
        "reads every timestamp spelling the server has written",
        arguments: [
            "2026-11-27T05:09:55Z",
            "2026-11-27T05:09:55+00:00",
            "2026-11-27T05:09:55.853557+00:00",
            "2026-11-27T05:09:55.853557+00:00Z",
            "2026-11-27T05:09:55.853Z",
            "2026-11-27T05:09:55",
        ])
    func timestamps(raw: String) throws {
        let date = try #require(ServerTimestamp.parse(raw))
        #expect(abs(date.timeIntervalSince1970 - 1_795_756_195) < 1)
    }

    @Test("an empty or garbled timestamp is no date")
    func badTimestamps() {
        #expect(ServerTimestamp.parse(nil) == nil)
        #expect(ServerTimestamp.parse("") == nil)
        #expect(ServerTimestamp.parse("yesterday") == nil)
    }
}

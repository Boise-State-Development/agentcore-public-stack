import Foundation
import Testing

@testable import AgentCoreKit

@Suite("InboxModels")
struct InboxModelsTests {
    private func page(_ json: String) throws -> InboxPage {
        try JSONDecoder().decode(InboxPage.self, from: Data(json.utf8))
    }

    @Test("reads a page of every kind, skipping a row with no id")
    func firstPage() throws {
        let page = try page(InboxFixtures.firstPage)
        #expect(page.items.count == 6)
        #expect(page.unreadCount == 4)
        #expect(page.nextCursor == "cursor-2")
        #expect(try self.page(InboxFixtures.secondPage).nextCursor == nil)
    }

    @Test("a waiting run's row carries its label, prompt lines, expiry and conversation")
    func waitingRun() throws {
        let item = try #require(page(InboxFixtures.firstPage).items.first)
        let needsUser = try #require(item.needsUser)
        #expect(item.isRead == false)
        #expect(item.conversationId == "headless-1")
        #expect(needsUser.label == "Morning grant check")
        #expect(needsUser.state == .waiting)
        #expect(needsUser.prompts.map(\.kind) == ["tool_approval", "user_question"])
        #expect(needsUser.prompts[1].headline == "Which term? (+1 more)")
        #expect(needsUser.isOpen())
        #expect(needsUser.canAnswerHere())
    }

    @Test("an answered row says how, and offers no answer")
    func answeredRun() throws {
        let item = try page(InboxFixtures.firstPage).items[1]
        let needsUser = try #require(item.needsUser)
        #expect(item.isRead)
        #expect(needsUser.state == .answered(decision: "approved"))
        #expect(needsUser.canAnswerHere() == false)
    }

    @Test("a wait past its expiry is closed before the server says so")
    func lapsed() {
        let needsUser = NeedsUser(
            label: nil, prompts: [], expiresAt: Date(timeIntervalSince1970: 100), answerable: true, state: .waiting)
        #expect(needsUser.isOpen(now: Date(timeIntervalSince1970: 99)))
        #expect(needsUser.isOpen(now: Date(timeIntervalSince1970: 101)) == false)
    }

    @Test(
        "a row the inbox can't answer opens its conversation instead",
        arguments: [
            NeedsUser(
                label: nil, prompts: [.init(interruptId: "i", kind: "browser_login")], expiresAt: nil,
                answerable: true, state: .waiting),
            NeedsUser(
                label: nil, prompts: [.init(interruptId: "i", kind: "tool_approval")], expiresAt: nil,
                answerable: false, state: .waiting),
            NeedsUser(
                label: nil, prompts: [.init(interruptId: "i", kind: "tool_approval")], expiresAt: nil,
                answerable: true, state: .applying),
        ])
    func notAnswerableHere(needsUser: NeedsUser) {
        #expect(needsUser.canAnswerHere() == false)
    }

    @Test("a row given back after a failed answer can be answered again")
    func retry() {
        let needsUser = NeedsUser(
            label: nil, prompts: [.init(interruptId: "i", kind: "tool_approval")], expiresAt: nil,
            answerable: true, state: .retry)
        #expect(needsUser.canAnswerHere())
    }

    @Test("other kinds keep what their line needs, and an unknown kind is kept")
    func otherKinds() throws {
        let items = try page(InboxFixtures.firstPage).items
        #expect(items[2].kind == .watcherFound(label: "Grant watch", title: "A new call for proposals"))
        #expect(items[2].conversationId == "headless-3")
        #expect(items[3].kind == .handoff(.done, agentName: "Example Reviewer", sourceTitle: "Plan the field trip"))
        #expect(items[3].conversationId == "c-2")
        #expect(items[4].kind == .project("project_invited"))
        #expect(items[4].projectName == "Example Project")
        #expect(items[4].actorName == "A Colleague")
        #expect(items[5].kind == .other("a_kind_from_the_future"))
        #expect(items[5].conversationId == nil)
        #expect(try page(InboxFixtures.secondPage).items.first?.kind == .schedulePaused(label: "Daily summary"))
    }

    @Test("quota rows say who asked, or what was decided")
    func quota() throws {
        let body = """
            {"notifications": [
              {"notificationId": "1", "kind": "quota_request_submitted", "payload": {"userEmail": "user@example.edu"}},
              {"notificationId": "2", "kind": "quota_request_decided", "payload": {"outcome": "approved"}},
              {"notificationId": "3", "kind": "quota_request_decided", "payload": {"outcome": "declined"}},
              {"notificationId": "4", "kind": "quota_extension_revoked", "payload": {}},
              {"notificationId": "5", "kind": "quota_something_new", "payload": {}}
            ], "unreadCount": 0}
            """
        #expect(
            try page(body).items.map(\.kind) == [
                .quota(.requested(by: "user@example.edu")), .quota(.approved), .quota(.declined),
                .quota(.extensionEnded), .quota(.other("quota_something_new")),
            ])
    }

    @Test(
        "a run's status reads as an outcome",
        arguments: [
            ("started", RunStatus.Outcome.running),
            ("completed", .finished),
            ("needs_user", .waitingAgain),
            ("oauth_required", .needsConnection),
            ("timeout", .failed(nil)),
        ])
    func runStatus(wire: String, outcome: RunStatus.Outcome) throws {
        let status = try JSONDecoder().decode(RunStatus.self, from: Data(InboxFixtures.run(wire).utf8))
        #expect(status.outcome == outcome)
    }

    @Test("a failed run keeps its error")
    func runError() throws {
        let body = InboxFixtures.run("error", error: "The model was unavailable.")
        let status = try JSONDecoder().decode(RunStatus.self, from: Data(body.utf8))
        #expect(status.outcome == .failed("The model was unavailable."))
    }
}

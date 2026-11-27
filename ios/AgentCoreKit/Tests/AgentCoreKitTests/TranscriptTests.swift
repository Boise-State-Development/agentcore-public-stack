import Foundation
import Testing

@testable import AgentCoreKit

@Suite("Transcript")
struct TranscriptTests {
    private func transcript(_ json: String...) throws -> Transcript {
        let pages = try json.map { try JSONDecoder().decode(MessagesPage.self, from: Data($0.utf8)) }
        return Transcript(pages: pages)
    }

    private func message(_ id: String, in transcript: Transcript) throws -> TranscriptMessage {
        try #require(transcript.messages.first { $0.id == id })
    }

    @Test("shows the messages a person reads, in order, and folds the rest away")
    func order() throws {
        let transcript = try transcript(ConversationFixtures.transcript)
        #expect(transcript.messages.map(\.id) == ["msg-c-1-0", "msg-c-1-1", "msg-c-1-3", "unreadable-5", "msg-c-1-6"])
        #expect(transcript.isArchived == false)
    }

    @Test("a user message shows what the user typed, with its attachments as chips")
    func userMessage() throws {
        let user = try message("msg-c-1-0", in: transcript(ConversationFixtures.transcript))
        #expect(user.role == .user)
        #expect(
            user.items == [
                .text("What changed this week?"),
                .attachment(.image, name: nil),
                .attachment(.document, name: "agenda.pdf"),
            ])
        #expect(user.createdAt == ServerTimestamp.parse("2026-11-27T15:00:00Z"))
    }

    @Test("tool calls are one row each with how they ended, and reasoning isn't shown")
    func tools() throws {
        let assistant = try message("msg-c-1-1", in: transcript(ConversationFixtures.transcript))
        #expect(
            assistant.items == [
                .text("Let me look."),
                .tool(name: "web_search", outcome: .succeeded),
                .tool(name: "read_calendar", outcome: .failed),
            ])
    }

    @Test("an unknown block type, or a block with no type, is a neutral line, not a lost message")
    func unknownBlocks() throws {
        let assistant = try message("msg-c-1-3", in: transcript(ConversationFixtures.transcript))
        #expect(assistant.items == [.text("**Two** things changed."), .notShown, .notShown])
    }

    @Test("a message that doesn't decode is kept as a placeholder")
    func unreadableMessage() throws {
        let placeholder = try message("unreadable-5", in: transcript(ConversationFixtures.transcript))
        #expect(placeholder.role == .other)
        #expect(placeholder.items == [.notShown])
    }

    @Test("a Continue's prompt isn't shown as something the user said")
    func continuation() throws {
        let ids = try transcript(ConversationFixtures.transcript).messages.map(\.id)
        #expect(!ids.contains("msg-c-1-4"))
    }

    @Test("a paused prompt sits under the message that raised it, and its tool waits")
    func pendingPrompt() throws {
        let transcript = try transcript(ConversationFixtures.transcript)
        let paused = try message("msg-c-1-6", in: transcript)
        #expect(paused.items == [.tool(name: "send_email", outcome: .waiting)])
        #expect(
            paused.prompts == [
                PendingPrompt(id: "i-1", kind: .toolApproval(toolName: "send_email", message: "Send the email?"))
            ])
        #expect(transcript.pendingPrompts.count == 1)
    }

    @Test("every prompt kind is read from its breadcrumb")
    func promptKinds() throws {
        let questions = #"[{"header": "Scope", "question": "Which term?", "multiSelect": false, "options": []}]"#
        let form =
            #"{"toolName": "create_issue", "serverName": "Example Tracker", "message": "Pick a project", "fields": []}"#
        let body = """
            {"messages": [{"id": "m-0", "role": "assistant", "content": [{"type": "text", "text": "One moment."}]}],
             "pendingInterrupts": [
               {"interruptId": "q", "kind": "user_question", "questions": \(Self.quoted(questions)), "createdAt": "x"},
               {"interruptId": "f", "kind": "mcp_elicitation", "elicitation": \(Self.quoted(form)), "createdAt": "x"},
               {"interruptId": "o", "providerId": "example-drive", "createdAt": "x"},
               {"interruptId": "b", "kind": "browser_login", "reason": "Sign in to the portal", "createdAt": "x"},
               {"interruptId": "l", "kind": "client_tool", "toolName": "shell", "createdAt": "x"},
               {"interruptId": "u", "kind": "telepathy", "createdAt": "x"},
               {"kind": "tool_approval"}
             ]}
            """
        let prompts = try transcript(body).pendingPrompts
        #expect(
            prompts.map(\.kind) == [
                .question(["Which term?"]),
                .form(serverName: "Example Tracker", message: "Pick a project"),
                .connectAccount(providerId: "example-drive"),
                .browserSignIn(reason: "Sign in to the portal"),
                .localTool(toolName: "shell"),
                .other("telepathy"),
            ])
    }

    @Test("an encoded field that isn't valid JSON leaves the prompt with less, not gone")
    func garbledEncodedField() throws {
        let body = """
            {"messages": [], "pendingInterrupts": [
              {"interruptId": "q", "kind": "user_question", "questions": "not json", "createdAt": "x"}]}
            """
        let transcript = try transcript(body)
        #expect(transcript.pendingPrompts == [PendingPrompt(id: "q", kind: .question([]))])
        // No answer to put it under, so it stands on its own.
        #expect(transcript.messages.map(\.id) == ["pending-prompts"])
    }

    @Test("a quiet watcher's state block is stripped from the prompt it heads")
    func watchState() throws {
        let body = #"""
            {"messages": [{"id": "m-0", "role": "user", "content": [{"type": "text",
              "text": "<watch_state>\n{\"seen\": []}\n</watch_state>\n\nAny new grants?"}]}]}
            """#
        let user = try message("m-0", in: transcript(body))
        #expect(user.items == [.text("Any new grants?")])
    }

    @Test("a message typed mid-turn shows without the tags the model reads")
    func steering() throws {
        let body = #"""
            {"messages": [{"id": "m-0", "role": "user", "content": [
              {"type": "toolResult", "toolResult": {"toolUseId": "t-1", "status": "success", "content": []}},
              {"type": "text", "text": "<user_message_during_turn>\nOnly this week\n</user_message_during_turn>"}]}]}
            """#
        let user = try message("m-0", in: transcript(body))
        #expect(user.items == [.text("Only this week")])
    }

    @Test("a tool call with no stored result reads as cut off")
    func noResult() throws {
        let body = """
            {"messages": [{"id": "m-0", "role": "assistant", "content": [
              {"type": "toolUse", "toolUse": {"toolUseId": "t-1", "name": "web_search"}}]}]}
            """
        let assistant = try message("m-0", in: transcript(body))
        #expect(assistant.items == [.tool(name: "web_search", outcome: .noResult)])
    }

    @Test("pages join in order, results on a later page still close their calls")
    func pages() throws {
        let first = """
            {"messages": [{"id": "m-0", "role": "assistant", "content": [
              {"type": "toolUse", "toolUse": {"toolUseId": "t-1", "name": "web_search"}}]}],
             "nextToken": "MQ=="}
            """
        let second = """
            {"messages": [{"id": "m-1", "role": "user", "content": [
              {"type": "toolResult", "toolResult": {"toolUseId": "t-1", "status": "success"}}]},
              {"id": "m-2", "role": "assistant", "content": [{"type": "text", "text": "Done."}]}],
             "fromArchive": true}
            """
        let transcript = try transcript(first, second)
        #expect(transcript.messages.map(\.id) == ["m-0", "m-2"])
        #expect(transcript.messages[0].items == [.tool(name: "web_search", outcome: .succeeded)])
        #expect(transcript.isArchived)
    }

    /// `value` as a JSON string literal, the way the server stores encoded fields.
    private static func quoted(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value) else { return "\"\"" }
        return String(decoding: data, as: UTF8.self)
    }
}

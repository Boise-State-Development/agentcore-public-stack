import Foundation
import Testing

@testable import AgentCoreKit

@Suite("InboxPrompt")
struct InboxPromptTests {
    private func prompts(_ json: String = InboxFixtures.prompts) throws -> [InboxPrompt] {
        try JSONDecoder().decode(InboxPromptsPage.self, from: Data(json.utf8)).prompts
    }

    @Test("a tool approval carries its input, indented for reading")
    func approval() throws {
        let approval = try #require(prompts().first)
        guard case .toolApproval(let name, let message, let input?) = approval.kind else {
            Issue.record("expected an approval with input")
            return
        }
        #expect(name == "send_email")
        #expect(message == "Send this email?")
        #expect(input.contains("\n"))
        #expect(input.contains(#""subject" : "Hello""#))
    }

    @Test("questions are decoded from their encoded field")
    func questions() throws {
        guard case .questions(let questions) = try prompts()[1].kind else {
            Issue.record("expected questions")
            return
        }
        #expect(questions.map(\.header) == ["Term", "Topics"])
        #expect(questions[0].options.map(\.label) == ["Spring", "Fall"])
        #expect(questions[0].options[1].description == "Starts in August")
        #expect(questions[1].multiSelect)
    }

    @Test("a form's fields keep their kinds and rules")
    func form() throws {
        guard case .form(let form) = try prompts()[2].kind else {
            Issue.record("expected a form")
            return
        }
        #expect(form.serverName == "Example Tracker")
        #expect(form.message == "A few details for the new issue")
        #expect(form.hasUnsupportedField == false)
        #expect(form.fields.map(\.name) == ["title", "points", "urgent", "labels", "team"])
        #expect(form.fields[0].kind == .text(format: nil, minLength: 3, maxLength: 80))
        #expect(form.fields[1].kind == .integer(minimum: 1, maximum: 13))
        #expect(form.fields[1].defaultValue == .number(3))
        #expect(form.fields[2].defaultValue == nil)
        #expect(form.fields[4].defaultValue == .string("web"))
    }

    @Test("a field of a kind this build doesn't know marks the form, and the rest still decode")
    func unknownField() throws {
        let form =
            #"{"serverName": "S", "fields": [{"name": "a", "kind": "text", "title": "A"}, {"name": "b", "kind": "signature", "title": "B"}]}"#
        let body = """
            {"prompts": [{"interruptId": "i", "kind": "mcp_elicitation", "elicitation": \(InboxFixtures.quoted(form))}]}
            """
        guard case .form(let decoded) = try #require(prompts(body).first).kind else {
            Issue.record("expected a form")
            return
        }
        #expect(decoded.fields.map(\.name) == ["a"])
        #expect(decoded.hasUnsupportedField)
    }

    @Test("prompts the inbox can't answer, and garbled ones, say so rather than fail")
    func notAnswerable() throws {
        let body = """
            {"prompts": [
              {"interruptId": "b", "kind": "browser_login"},
              {"interruptId": "o", "kind": "oauth", "providerId": "example-drive"},
              {"interruptId": "f", "kind": "mcp_elicitation", "elicitation": "not json"},
              {"kind": "tool_approval"}
            ]}
            """
        let prompts = try prompts(body)
        #expect(prompts.map(\.id) == ["b", "o", "f"])
        #expect(prompts.allSatisfy { !$0.isAnswerableHere })
    }
}

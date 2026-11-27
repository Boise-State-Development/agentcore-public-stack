import AgentCoreKit
import Foundation

/// Example data for `#Preview`s. Example values only, and no network: the
/// transport answers every request from the bodies below.
enum PreviewData {
    static let user = SessionUser(userId: "u-1", email: "user@example.edu", name: "A Member", roles: [])

    @MainActor
    static func session() -> SessionModel {
        let bundleIdentifier = "org.example.agentcore.preview"
        return SessionModel(
            serverStore: UserDefaultsServerStore(),
            sessionStore: KeychainSessionStore(service: "\(bundleIdentifier).session"),
            redirect: NativeRedirect(bundleIdentifier: bundleIdentifier),
            device: DeviceDescription(model: "iPhone", appVersion: "0.0.0")
        )
    }

    @MainActor
    static func list(body: String = sessions) -> ConversationListModel {
        let client = AgentCoreClient(
            server: server,
            transport: PreviewTransport(sessions: body, messages: messages)
        )
        return ConversationListModel(source: ConversationSource(client: client, sealed: "preview") {})
    }

    static let transcript = Transcript(messages: [
        TranscriptMessage(
            id: "m-0", role: .user,
            items: [
                .text("Plan a field trip for the first week of March."), .attachment(.document, name: "agenda.pdf"),
            ]
        ),
        TranscriptMessage(
            id: "m-1", role: .assistant,
            items: [
                .text("I'll check the calendar first."),
                .tool(name: "read_calendar", outcome: .succeeded),
                .tool(name: "web_search", outcome: .failed),
                .text(
                    """
                    ## Options
                    - **Tuesday**: the museum is open late
                    - **Thursday**: buses are cheaper

                    Want me to book one?
                    """),
                .notShown,
            ]
        ),
        TranscriptMessage(
            id: "m-2", role: .assistant,
            items: [.tool(name: "send_email", outcome: .waiting)],
            prompts: [
                PendingPrompt(
                    id: "i-1", kind: .toolApproval(toolName: "send_email", message: "Send the booking email?"))
            ]
        ),
    ])

    private static var server: ServerConfiguration {
        do {
            return try ServerConfiguration(validating: "https://ai.example.edu/api")
        } catch {
            preconditionFailure("The example address is valid")
        }
    }

    private static let sessions = """
        {"sessions": [
          {"sessionId": "c-3", "title": "Plan the field trip", "lastMessageAt": "\(iso(-600))",
           "messageCount": 6, "unread": true},
          {"sessionId": "c-2", "title": "Weekly watch: new grants", "lastMessageAt": "\(iso(-86_400 * 2))",
           "messageCount": 2, "awaitingInput": "tool_approval"},
          {"sessionId": "c-1", "title": "Draft the welcome email", "lastMessageAt": "\(iso(-86_400 * 30))",
           "messageCount": 4}
        ]}
        """

    private static let messages = """
        {"messages": [
          {"id": "m-0", "role": "user", "content": [{"type": "text", "text": "Plan a field trip."}]},
          {"id": "m-1", "role": "assistant", "content": [{"type": "text", "text": "Here are **two** options."}]}
        ]}
        """

    private static func iso(_ offset: TimeInterval) -> String {
        Date.now.addingTimeInterval(offset).formatted(.iso8601)
    }
}

private struct PreviewTransport: HTTPTransport {
    let sessions: String
    let messages: String

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let url = request.url ?? URL(filePath: "/")
        let body = url.path().hasSuffix("/messages") ? messages : sessions
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else {
            throw URLError(.badServerResponse)
        }
        return (Data(body.utf8), response)
    }
}

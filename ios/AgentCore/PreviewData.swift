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
        return ConversationListModel(source: SignedInAPI(client: client, sealed: "preview") {})
    }

    @MainActor
    static func inbox() -> InboxModel {
        let client = AgentCoreClient(
            server: server,
            transport: PreviewTransport(sessions: sessions, messages: messages)
        )
        return InboxModel(api: SignedInAPI(client: client, sealed: "preview") {})
    }

    static let inboxItems = [
        InboxItem(
            id: "1795800000000-aaaaaaaaaaaa",
            kind: .needsUser(
                NeedsUser(
                    label: "Morning grant check",
                    prompts: [
                        .init(interruptId: "i-1", kind: "tool_approval", toolName: "send_email"),
                        .init(interruptId: "i-2", kind: "user_question", headline: "Which term?"),
                    ],
                    expiresAt: .now.addingTimeInterval(86_400 * 3), answerable: true, state: .waiting)),
            createdAt: .now.addingTimeInterval(-900), conversationId: "headless-1"),
        InboxItem(
            id: "1795700000000-bbbbbbbbbbbb",
            kind: .needsUser(
                NeedsUser(
                    label: "Weekly digest", prompts: [], expiresAt: nil, answerable: true,
                    state: .answered(decision: "approved"))),
            createdAt: .now.addingTimeInterval(-86_400), isRead: true),
        InboxItem(
            id: "1795600000000-cccccccccccc",
            kind: .watcherFound(label: "Grant watch", title: "A new call for proposals"),
            createdAt: .now.addingTimeInterval(-86_400 * 2), conversationId: "headless-3"),
        InboxItem(
            id: "1795400000000-eeeeeeeeeeee", kind: .project("project_invited"),
            createdAt: .now.addingTimeInterval(-86_400 * 9), isRead: true, projectName: "Example Project",
            actorName: "A Colleague"),
    ]

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

    fileprivate static let notifications = """
        {"notifications": [
          {"notificationId": "1795800000000-aaaaaaaaaaaa", "kind": "agent_needs_input", "createdAt": "\(iso(-900))",
           "payload": {"sessionId": "headless-1", "label": "Morning grant check", "answerable": true,
             "expiresAt": "\(iso(86_400 * 3))",
             "prompts": [{"interruptId": "i-1", "kind": "tool_approval", "toolName": "send_email"},
                         {"interruptId": "i-2", "kind": "user_question", "headline": "Which term?"}]}},
          {"notificationId": "1795600000000-cccccccccccc", "kind": "agent_item", "createdAt": "\(iso(-86_400 * 2))",
           "readAt": "\(iso(-86_400))",
           "payload": {"sessionId": "headless-3", "label": "Grant watch", "title": "A new call for proposals"}}
        ], "unreadCount": 1}
        """

    fileprivate static let prompts = """
        {"prompts": [
          {"interruptId": "i-1", "kind": "tool_approval", "toolName": "send_email",
           "message": "Send the weekly summary?", "toolInput": "{\\"to\\": \\"user@example.edu\\"}"},
          {"interruptId": "i-2", "kind": "user_question",
           "questions": "[{\\"header\\": \\"Term\\", \\"question\\": \\"Which term?\\", \\"options\\": [{\\"label\\": \\"Spring\\"}, {\\"label\\": \\"Fall\\"}]}]"}
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
        let path = url.path()
        let body =
            if path.hasSuffix("/messages") {
                messages
            } else if path.hasSuffix("/prompts") {
                PreviewData.prompts
            } else if path.hasSuffix("/notifications") {
                PreviewData.notifications
            } else {
                sessions
            }
        guard let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else {
            throw URLError(.badServerResponse)
        }
        return (Data(body.utf8), response)
    }
}

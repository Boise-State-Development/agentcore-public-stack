import Foundation

// Response bodies in the shapes app-api sends (`NotificationsResponse`,
// `InboxPromptsResponse`, `RespondResponse`, `RunStatusResponse`), with
// example values only. Notification ids match the route's pattern
// (`^[0-9]{13}-[0-9a-f]{12}$`).

enum InboxFixtures {
    static let waitingId = "1795800000000-aaaaaaaaaaaa"

    static let firstPage = """
        {
          "notifications": [
            {"notificationId": "\(waitingId)", "recipientEmail": "user@example.edu",
             "kind": "agent_needs_input", "projectId": null, "projectName": null, "actorEmail": null,
             "payload": {"scheduleId": "s-1", "runId": "run-aaaaaaaaaaaa", "sessionId": "headless-1",
               "label": "Morning grant check", "expiresAt": "2099-01-01T00:00:00Z", "answerable": true,
               "prompts": [
                 {"interruptId": "i-approve", "kind": "tool_approval", "toolName": "send_email"},
                 {"interruptId": "i-ask", "kind": "user_question", "headline": "Which term? (+1 more)"}
               ]},
             "createdAt": "2026-11-27T15:00:00.000000+00:00", "readAt": null, "resolution": null,
             "actorName": null},
            {"notificationId": "1795700000000-bbbbbbbbbbbb", "recipientEmail": "user@example.edu",
             "kind": "agent_needs_approval",
             "payload": {"sessionId": "headless-2", "label": "Weekly digest", "expiresAt": "2026-11-20T00:00:00Z",
               "answerable": true, "prompts": [{"interruptId": "i-1", "kind": "tool_approval"}]},
             "createdAt": "2026-11-19T15:00:00Z", "readAt": "2026-11-19T16:00:00Z",
             "resolution": {"state": "answered", "decision": "approved", "outcome": "finished",
               "at": "2026-11-19T16:00:00Z"}},
            {"notificationId": "1795600000000-cccccccccccc", "recipientEmail": "user@example.edu",
             "kind": "agent_item",
             "payload": {"scheduleId": "s-2", "runId": "run-x", "sessionId": "headless-3",
               "label": "Grant watch", "title": "A new call for proposals", "urgency": "normal", "key": "k"},
             "createdAt": "2026-11-18T15:00:00Z"},
            {"notificationId": "1795500000000-dddddddddddd", "recipientEmail": "user@example.edu",
             "kind": "handoff_done",
             "payload": {"handoffId": "h-1", "sourceSessionId": "c-1", "targetSessionId": "c-2",
               "agentId": "a-1", "agentName": "Example Reviewer", "sourceTitle": "Plan the field trip"},
             "createdAt": "2026-11-17T15:00:00Z"},
            {"notificationId": "1795400000000-eeeeeeeeeeee", "recipientEmail": "user@example.edu",
             "kind": "project_invited", "projectId": "p-1", "projectName": "Example Project",
             "actorEmail": "owner@example.edu", "payload": {"role": "editor"},
             "createdAt": "2026-11-16T15:00:00Z", "actorName": "A Colleague"},
            {"notificationId": "1795300000000-ffffffffffff", "recipientEmail": "user@example.edu",
             "kind": "a_kind_from_the_future", "payload": {"anything": [1, 2, 3]},
             "createdAt": "2026-11-15T15:00:00Z"},
            {"recipientEmail": "user@example.edu", "kind": "agent_item"}
          ],
          "unreadCount": 4,
          "nextCursor": "cursor-2"
        }
        """

    static let secondPage = """
        {"notifications": [
          {"notificationId": "1795200000000-aaaaaaaaaaab", "recipientEmail": "user@example.edu",
           "kind": "schedule_paused", "payload": {"scheduleId": "s-3", "label": "Daily summary",
           "reason": "quota_exceeded"}, "createdAt": "2026-11-14T15:00:00Z"}
         ],
         "unreadCount": 4, "nextCursor": null}
        """

    static let prompts = """
        {
          "notificationId": "\(waitingId)",
          "sessionId": "headless-1",
          "prompts": [
            {"interruptId": "i-approve", "kind": "tool_approval", "toolName": "send_email",
             "toolUseId": "t-1", "toolInput": "{\\"to\\": \\"user@example.edu\\", \\"subject\\": \\"Hello\\"}",
             "message": "Send this email?"},
            {"interruptId": "i-ask", "kind": "user_question",
             "questions": \(quoted(#"""
                [{"header": "Term", "question": "Which term?", "multiSelect": false,
                  "options": [{"label": "Spring"}, {"label": "Fall", "description": "Starts in August"}]},
                 {"header": "Topics", "question": "Which topics?", "multiSelect": true,
                  "options": [{"label": "Budget"}, {"label": "Travel"}, {"label": "Equipment"}]}]
                """#))},
            {"interruptId": "i-form", "kind": "mcp_elicitation",
             "elicitation": \(quoted(#"""
                {"type": "mcp_elicitation_required", "interruptId": "i-form", "toolName": "create_issue",
                 "serverName": "Example Tracker", "message": "A few details for the new issue",
                 "fields": [
                   {"name": "title", "kind": "text", "title": "Title", "required": true, "default": null,
                    "format": null, "minLength": 3, "maxLength": 80},
                   {"name": "points", "kind": "integer", "title": "Points", "required": false, "default": 3,
                    "minimum": 1, "maximum": 13},
                   {"name": "urgent", "kind": "boolean", "title": "Urgent", "required": false, "default": null},
                   {"name": "labels", "kind": "choice", "title": "Labels", "required": false, "default": null,
                    "options": [{"value": "bug", "label": "Bug"}, {"value": "docs", "label": "Docs"}],
                    "multiSelect": true, "minItems": null, "maxItems": 1},
                   {"name": "team", "kind": "choice", "title": "Team", "required": true, "default": "web",
                    "options": [{"value": "web", "label": "Web"}, {"value": "data", "label": "Data"}],
                    "multiSelect": false}
                 ]}
                """#))}
          ]
        }
        """

    static func respond(runId: String = "run-bbbbbbbbbbbb") -> String {
        #"{"resumeRunId": "\#(runId)", "resolution": {"state": "applying", "decision": "answered", "at": "2026-11-27T15:01:00Z"}}"#
    }

    static func run(_ status: String, error: String? = nil) -> String {
        let errorField = error.map { #", "error": "\#($0)""# } ?? ""
        return
            #"{"runId": "run-bbbbbbbbbbbb", "sessionId": "headless-1", "status": "\#(status)", "startedAt": "2026-11-27T15:01:00Z"\#(errorField)}"#
    }

    /// `value` as a JSON string literal, the way the server stores encoded fields.
    static func quoted(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value) else { return "\"\"" }
        return String(decoding: data, as: UTF8.self)
    }
}

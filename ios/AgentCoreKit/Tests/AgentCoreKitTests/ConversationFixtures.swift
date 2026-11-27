// Response bodies in the shapes app-api sends (`SessionsListResponse`,
// `MessagesListResponse` in apis/shared/sessions/models.py), with example
// values only. Each carries fields the app doesn't read, as the real
// responses do, so the tests prove those are ignored.

enum ConversationFixtures {
    static let firstPage = """
        {
          "sessions": [
            {"sessionId": "c-3", "title": "Draft the welcome email", "status": "active",
             "createdAt": "2026-11-20T09:00:00.000000+00:00", "lastMessageAt": "2026-11-27T15:04:05.123456+00:00",
             "messageCount": 6, "starred": false, "tags": [], "unread": true,
             "preferences": {"lastModel": "example-model", "enabledTools": ["web_search"]},
             "totalCost": 0.0123, "lastContextTokens": 1200, "exportReceipts": []},
            {"sessionId": "c-2", "title": "Weekly watch: new grants", "status": "active",
             "createdAt": "2026-11-19T09:00:00Z", "lastMessageAt": "2026-11-26T08:00:00+00:00Z",
             "messageCount": 2, "awaitingInput": "tool_approval",
             "spawnedFrom": {"sessionId": "c-1", "handoffId": "h-1", "settled": true}},
            {"title": "A row with no id is skipped", "status": "active"},
            {"sessionId": "c-1", "title": "Plan the field trip", "status": "active",
             "createdAt": "2026-11-18T09:00:00Z", "lastMessageAt": "2026-11-18T10:00:00Z",
             "messageCount": 4, "awaitingInput": "a_kind_from_the_future", "someNewField": {"nested": [1, 2]}}
          ],
          "nextToken": "page-2",
          "pinned": [
            {"sessionId": "c-1", "title": "Plan the field trip", "status": "active",
             "createdAt": "2026-11-18T09:00:00Z", "lastMessageAt": "2026-11-18T10:00:00Z",
             "messageCount": 4, "pinnedAt": "2026-11-21T10:00:00Z"}
          ]
        }
        """

    static let secondPage = """
        {
          "sessions": [
            {"sessionId": "c-1", "title": "Plan the field trip", "status": "active",
             "createdAt": "2026-11-18T09:00:00Z", "lastMessageAt": "2026-11-18T10:00:00Z", "messageCount": 4},
            {"sessionId": "c-0", "title": "Hello", "status": "active",
             "createdAt": "2026-11-01T09:00:00Z", "lastMessageAt": "2026-11-01T09:01:00Z", "messageCount": 2}
          ]
        }
        """

    static let emptyPage = #"{"sessions": []}"#

    /// A conversation that exercises every kind of block the transcript shows,
    /// plus the replay fields and block types it ignores.
    static let transcript = """
        {
          "messages": [
            {"id": "msg-c-1-0", "role": "user", "createdAt": "2026-11-27T15:00:00.000000+00:00",
             "content": [
               {"type": "text", "text": "Context the server added\\n\\nWhat changed this week?"},
               {"type": "image", "image": {"format": "png", "data": "aGVsbG8="}},
               {"type": "document", "document": {"format": "pdf", "name": "agenda.pdf", "data": "aGVsbG8="}}
             ],
             "metadata": {"displayText": "What changed this week?"}},
            {"id": "msg-c-1-1", "role": "assistant", "createdAt": "2026-11-27T15:00:05Z",
             "content": [
               {"type": "reasoningContent", "reasoningContent": {"reasoningText": {"text": "private"}}},
               {"type": "text", "text": "Let me look."},
               {"type": "toolUse", "toolUse": {"toolUseId": "t-1", "name": "web_search", "input": {"q": "x"}}},
               {"type": "toolUse", "toolUse": {"toolUseId": "t-2", "name": "read_calendar", "input": {}}}
             ],
             "metadata": {"tokenUsage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                          "cost": {"total": 0.001}, "contextBreakdown": {"partitions": []}},
             "citations": [{"assistantId": "a-1", "documentId": "d-1", "fileName": "notes.md", "text": "x"}]},
            {"id": "msg-c-1-2", "role": "user", "createdAt": "2026-11-27T15:00:06Z",
             "content": [
               {"type": "toolResult", "toolResult": {"toolUseId": "t-1", "status": "success",
                "content": [{"text": "result body the app never shows"}]}},
               {"type": "toolResult", "toolResult": {"toolUseId": "t-2", "status": "error",
                "content": [{"text": "boom"}]}}
             ]},
            {"id": "msg-c-1-3", "role": "assistant", "createdAt": "2026-11-27T15:00:09Z",
             "content": [
               {"type": "text", "text": "**Two** things changed."},
               {"type": "hologram", "hologram": {"depth": 3}},
               {"text": "a block with no type"}
             ]},
            {"id": "msg-c-1-4", "role": "user", "createdAt": "2026-11-27T15:01:00Z",
             "content": [{"type": "text", "text": "Please continue."}], "continuation": true},
            {"role": "assistant", "content": "not even a list"},
            {"id": "msg-c-1-6", "role": "assistant", "createdAt": "2026-11-27T15:02:00Z",
             "content": [{"type": "toolUse", "toolUse": {"toolUseId": "t-3", "name": "send_email", "input": {}}}]}
          ],
          "pendingInterrupts": [
            {"interruptId": "i-1", "kind": "tool_approval", "triggeringMessageId": "msg-c-1-6",
             "toolUseId": "t-3", "toolName": "send_email", "toolInput": "{\\"to\\": \\"user@example.edu\\"}",
             "message": "Send the email?", "createdAt": "2026-11-27T15:02:01Z"}
          ],
          "uiResources": [{"type": "ui_resource", "toolUseId": "t-1", "html": "<p>x</p>"}],
          "toolSummaries": [{"batchId": "b-1", "toolUseIds": ["t-1"], "summary": "Searched the web"}],
          "sourceProvenance": [{"toolUseId": "t-9", "claims": []}],
          "extractions": [{"toolUseId": "t-8", "fields": []}],
          "handoffs": [{"handoffId": "h-1", "kind": "mention", "status": "done"}],
          "deliveries": [],
          "workflowRuns": [],
          "fromArchive": false,
          "nextMessageIndex": 7
        }
        """
}

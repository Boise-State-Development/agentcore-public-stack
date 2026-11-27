import Foundation

/// A conversation as the transcript screen draws it: the messages a person
/// reads, with protocol-only messages (tool results) folded away and any
/// paused prompt placed under the answer that raised it.
public struct Transcript: Sendable, Equatable {
    public let messages: [TranscriptMessage]
    /// The archive's text-only copy: older than Memory keeps, so no tool calls
    /// or attachments, and the web app offers it read-only.
    public let isArchived: Bool

    public init(messages: [TranscriptMessage], isArchived: Bool = false) {
        self.messages = messages
        self.isArchived = isArchived
    }

    /// Every prompt still waiting on an answer, in transcript order.
    public var pendingPrompts: [PendingPrompt] {
        messages.flatMap(\.prompts)
    }
}

public struct TranscriptMessage: Sendable, Equatable, Identifiable {
    public enum Role: Sendable, Equatable {
        case user
        case assistant
        /// `system`, or a role this build doesn't know.
        case other
    }

    public let id: String
    public let role: Role
    public let createdAt: Date?
    public let items: [TranscriptItem]
    /// Paused prompts this message raised, waiting on the user.
    public let prompts: [PendingPrompt]

    public init(
        id: String,
        role: Role,
        createdAt: Date? = nil,
        items: [TranscriptItem],
        prompts: [PendingPrompt] = []
    ) {
        self.id = id
        self.role = role
        self.createdAt = createdAt
        self.items = items
        self.prompts = prompts
    }

    func with(prompts: [PendingPrompt]) -> TranscriptMessage {
        TranscriptMessage(id: id, role: role, createdAt: createdAt, items: items, prompts: self.prompts + prompts)
    }
}

public enum TranscriptItem: Sendable, Equatable {
    /// Markdown text, as the model or the user wrote it.
    case text(String)
    /// One tool call: its name and how it ended. Never its arguments or result.
    case tool(name: String, outcome: ToolOutcome)
    /// An attachment the app labels but doesn't render yet.
    case attachment(AttachmentKind, name: String?)
    /// A block this build doesn't know how to show.
    case notShown
}

public enum ToolOutcome: Sendable, Equatable {
    case succeeded
    case failed
    /// Paused on a prompt that is still waiting on the user.
    case waiting
    /// No result was stored: the turn was stopped or cut off mid-call.
    case noResult
}

public enum AttachmentKind: Sendable, Equatable {
    case image
    case document
}

/// A paused turn's prompt, shown read-only. Answering it is a later screen.
public struct PendingPrompt: Sendable, Equatable, Identifiable {
    public enum Kind: Sendable, Equatable {
        /// A tool asks to be allowed to run.
        case toolApproval(toolName: String?, message: String?)
        /// The agent asked clarifying questions; the text of each.
        case question([String])
        /// A connected service asked for details in a form.
        case form(serverName: String?, message: String?)
        /// A connector needs the user's consent.
        case connectAccount(providerId: String?)
        /// The agent handed the browser over for a sign-in.
        case browserSignIn(reason: String?)
        /// A tool the terminal client runs on the user's machine.
        case localTool(toolName: String?)
        /// A kind this build doesn't know.
        case other(String)
    }

    public let id: String
    public let kind: Kind

    public init(id: String, kind: Kind) {
        self.id = id
        self.kind = kind
    }

    init(_ wire: WireInterrupt) {
        self.id = wire.interruptId
        switch wire.kind {
        case "tool_approval": self.kind = .toolApproval(toolName: wire.toolName, message: wire.message)
        case "user_question": self.kind = .question(wire.questions)
        case "mcp_elicitation": self.kind = .form(serverName: wire.formServerName, message: wire.formMessage)
        case "oauth": self.kind = .connectAccount(providerId: wire.providerId)
        case "browser_login": self.kind = .browserSignIn(reason: wire.reason)
        case "client_tool": self.kind = .localTool(toolName: wire.toolName)
        default: self.kind = .other(wire.kind)
        }
    }
}

// MARK: - Building

extension Transcript {
    /// Build the transcript from every page of `GET /sessions/{id}/messages`, in order.
    init(pages: [MessagesPage]) {
        let wireMessages = pages.flatMap(\.messages)
        let interrupts = pages.flatMap(\.pendingInterrupts)
        let isArchived = pages.contains(where: \.fromArchive)

        var failedByToolUse: [String: Bool] = [:]
        for case let .toolResult(toolUseId?, failed) in wireMessages.flatMap({ $0?.content ?? [] }) {
            failedByToolUse[toolUseId] = failed
        }
        let waiting = Set(interrupts.compactMap(\.toolUseId))

        var messages: [TranscriptMessage] = []
        for (index, wire) in wireMessages.enumerated() {
            guard let wire else {
                messages.append(TranscriptMessage(id: "unreadable-\(index)", role: .other, items: [.notShown]))
                continue
            }
            if let message = Self.message(from: wire, failedByToolUse: failedByToolUse, waiting: waiting) {
                messages.append(message)
            }
        }

        self.init(
            messages: Self.placing(interrupts.map(PendingPrompt.init), from: interrupts, in: messages),
            isArchived: isArchived)
    }

    private static func message(
        from wire: WireMessage,
        failedByToolUse: [String: Bool],
        waiting: Set<String>
    ) -> TranscriptMessage? {
        let role: TranscriptMessage.Role =
            switch wire.role {
            case "user": .user
            case "assistant": .assistant
            default: .other
            }
        // A Continue's prompt: the web app shows the partial answer and its
        // continuation as one response, so there is nothing to show here.
        if role == .user && wire.continuation {
            return nil
        }

        let carriesToolResults = wire.content.contains {
            if case .toolResult = $0 { return true }
            return false
        }
        var items: [TranscriptItem] = []
        var usedDisplayText = false
        var isFirstText = true

        for block in wire.content {
            switch block {
            case .text(let raw):
                var text = raw
                if role == .user {
                    if let display = wire.displayText, !display.isEmpty {
                        // What the user typed replaces the stored text, once.
                        guard !usedDisplayText else { continue }
                        usedDisplayText = true
                        text = display
                    } else {
                        if isFirstText { text = UserText.withoutWatchState(text) }
                        if carriesToolResults { text = UserText.withoutSteeringTags(text) }
                    }
                }
                isFirstText = false
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty {
                    items.append(.text(trimmed))
                }
            case .toolUse(let id, let name):
                let outcome: ToolOutcome
                if let id, let failed = failedByToolUse[id] {
                    outcome = failed ? .failed : .succeeded
                } else if let id, waiting.contains(id) {
                    outcome = .waiting
                } else {
                    outcome = .noResult
                }
                items.append(.tool(name: name, outcome: outcome))
            case .toolResult, .reasoning:
                // Folded into the tool row; the model's reasoning isn't shown.
                continue
            case .image:
                items.append(.attachment(.image, name: nil))
            case .document(let name):
                items.append(.attachment(.document, name: name))
            case .unknown:
                items.append(.notShown)
            }
        }

        // A user message that only carried tool results is protocol, not speech.
        guard !items.isEmpty else { return nil }
        return TranscriptMessage(
            id: wire.id,
            role: role,
            createdAt: ServerTimestamp.parse(wire.createdAt),
            items: items
        )
    }

    /// Put each prompt under the message that raised it, else the last answer,
    /// else at the end on a message of its own.
    private static func placing(
        _ prompts: [PendingPrompt],
        from interrupts: [WireInterrupt],
        in messages: [TranscriptMessage]
    ) -> [TranscriptMessage] {
        guard !prompts.isEmpty else { return messages }
        var messages = messages
        let lastAssistant = messages.lastIndex { $0.role == .assistant }
        var unplaced: [PendingPrompt] = []

        for (prompt, interrupt) in zip(prompts, interrupts) {
            let anchor =
                interrupt.triggeringMessageId.flatMap { id in messages.firstIndex { $0.id == id } }
                ?? lastAssistant
            if let anchor {
                messages[anchor] = messages[anchor].with(prompts: [prompt])
            } else {
                unplaced.append(prompt)
            }
        }
        if !unplaced.isEmpty {
            messages.append(TranscriptMessage(id: "pending-prompts", role: .assistant, items: [], prompts: unplaced))
        }
        return messages
    }
}

/// What the server adds to a stored user message that the user didn't type.
enum UserText {
    /// A quiet watcher's run puts its state ahead of the schedule's prompt
    /// (`build_watch_prompt`); the web app strips it the same way (`watch-state.ts`).
    static func withoutWatchState(_ text: String) -> String {
        let trimmed = text.drop(while: \.isWhitespace)
        guard trimmed.hasPrefix("<watch_state>"), let end = trimmed.range(of: "</watch_state>") else {
            return text
        }
        return String(trimmed[end.upperBound...])
    }

    /// A message typed mid-turn rides on the tool results, wrapped for the
    /// model (`STEER_OPEN_TAG` in `session/hooks/steering.py`).
    static func withoutSteeringTags(_ text: String) -> String {
        text.replacing("<user_message_during_turn>", with: "")
            .replacing("</user_message_during_turn>", with: "")
    }
}

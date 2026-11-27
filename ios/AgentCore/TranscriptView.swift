import AgentCoreKit
import SwiftUI

/// One conversation, read-only. Loads every page, then opens at the newest message.
struct TranscriptView: View {
    @State private var model: TranscriptModel
    let title: String?

    init(model: TranscriptModel, title: String?) {
        self._model = State(initialValue: model)
        self.title = title
    }

    var body: some View {
        Group {
            if let transcript = model.transcript {
                TranscriptContent(transcript: transcript)
            } else if let problem = model.problem {
                ContentUnavailableView {
                    Label("Couldn't open this conversation", systemImage: "exclamationmark.bubble")
                } description: {
                    LoadProblemText(problem: problem)
                } actions: {
                    Button("Try again") {
                        Task { await model.load() }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isLoading)
                }
            } else {
                ProgressView()
            }
        }
        .navigationTitle(title ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            if model.transcript == nil {
                await model.load()
            }
        }
    }
}

struct TranscriptContent: View {
    let transcript: Transcript

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 20) {
                if transcript.isArchived {
                    Label(
                        "This is an archived copy: text only, without tool calls or attachments.",
                        systemImage: "archivebox"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
                if transcript.messages.isEmpty {
                    Text("No messages yet.")
                        .foregroundStyle(.secondary)
                }
                ForEach(transcript.messages) { message in
                    MessageView(message: message)
                }
            }
            .padding()
            .frame(maxWidth: 760)
            .frame(maxWidth: .infinity)
        }
        // Open at the newest message, but keep a short conversation at the top.
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .defaultScrollAnchor(.top, for: .alignment)
    }
}

struct MessageView: View {
    let message: TranscriptMessage

    var body: some View {
        VStack(alignment: message.role == .user ? .trailing : .leading, spacing: 8) {
            if message.role == .user {
                VStack(alignment: .leading, spacing: 8) {
                    items
                }
                .padding(12)
                .background(.tint.opacity(0.14), in: .rect(cornerRadius: 16))
                .frame(maxWidth: .infinity, alignment: .trailing)
                .padding(.leading, 40)
            } else {
                items
            }
            ForEach(message.prompts) { prompt in
                PromptCard(prompt: prompt)
            }
        }
        .frame(maxWidth: .infinity, alignment: message.role == .user ? .trailing : .leading)
    }

    private var items: some View {
        ForEach(Array(message.items.enumerated()), id: \.offset) { _, item in
            ItemView(item: item)
        }
    }
}

struct ItemView: View {
    let item: TranscriptItem

    var body: some View {
        switch item {
        case .text(let text):
            MarkdownView(text: text)
        case .tool(let name, let outcome):
            ToolRow(name: name, outcome: outcome)
        case .attachment(let kind, let name):
            AttachmentChip(kind: kind, name: name)
        case .notShown:
            Label("Not shown in this app", systemImage: "eye.slash")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
    }
}

struct MarkdownView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(MarkdownBlock.parse(text).enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func blockView(_ block: MarkdownBlock) -> some View {
        switch block {
        case .paragraph(let text):
            Text(text)
        case .heading(let level, let text):
            Text(text)
                .font(level <= 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
                .accessibilityAddTraits(.isHeader)
        case .listItem(let marker, let depth, let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(marker)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Text(text)
            }
            .padding(.leading, CGFloat(depth) * 16)
        case .quote(let text):
            Text(text)
                .foregroundStyle(.secondary)
                .padding(.leading, 10)
                .overlay(alignment: .leading) {
                    Rectangle().fill(.quaternary).frame(width: 3)
                }
        case .code(let code):
            ScrollView(.horizontal) {
                Text(code)
                    .font(.callout.monospaced())
                    .padding(10)
            }
            .background(.fill.tertiary, in: .rect(cornerRadius: 8))
        case .rule:
            Divider()
        }
    }
}

/// One line per tool call: which tool, and how it ended. Never its payloads.
struct ToolRow: View {
    let name: String
    let outcome: ToolOutcome

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "wrench.and.screwdriver")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            Text(name.isEmpty ? String(localized: "A tool") : name)
                .font(.callout.monospaced())
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 8)
            outcomeLabel
                .font(.footnote)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 10)
        .background(.fill.quaternary, in: .rect(cornerRadius: 8))
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var outcomeLabel: some View {
        switch outcome {
        case .succeeded:
            Label("Done", systemImage: "checkmark.circle").foregroundStyle(.green)
        case .failed:
            Label("Failed", systemImage: "xmark.octagon").foregroundStyle(.red)
        case .waiting:
            Label("Waiting", systemImage: "hourglass").foregroundStyle(.orange)
        case .noResult:
            Label("Didn't finish", systemImage: "minus.circle").foregroundStyle(.secondary)
        }
    }
}

/// An attachment, named but not opened yet.
struct AttachmentChip: View {
    let kind: AttachmentKind
    let name: String?

    var body: some View {
        Label {
            switch kind {
            case .image: Text(name ?? String(localized: "Image"))
            case .document: Text(name ?? String(localized: "Document"))
            }
        } icon: {
            Image(systemName: kind == .image ? "photo" : "doc")
        }
        .font(.footnote)
        .lineLimit(1)
        .padding(.vertical, 6)
        .padding(.horizontal, 10)
        .background(.fill.tertiary, in: .capsule)
        .accessibilityHint(Text("Not shown in this app yet"))
    }
}

/// A paused prompt, read-only. Answering from the app comes later.
struct PromptCard: View {
    let prompt: PendingPrompt

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: "exclamationmark.bubble.fill")
                .font(.headline)
                .foregroundStyle(.orange)
            detail
            Text("Waiting for your answer. You can answer it on the web for now.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.orange.opacity(0.10), in: .rect(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.orange.opacity(0.5)))
        .accessibilityElement(children: .combine)
    }

    private var title: LocalizedStringKey {
        switch prompt.kind {
        case .toolApproval(let toolName?, _): "Allow \(toolName) to run?"
        case .toolApproval: "Allow a tool to run?"
        case .question: "A question for you"
        case .form(let serverName?, _): "\(serverName) needs some details"
        case .form: "A connected service needs some details"
        case .connectAccount: "Connect an account to continue"
        case .browserSignIn: "Sign-in needed"
        case .localTool: "A step to run on your computer"
        case .other: "Waiting for you"
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch prompt.kind {
        case .toolApproval(_, let message?), .form(_, let message?):
            Text(message)
        case .question(let questions):
            ForEach(Array(questions.enumerated()), id: \.offset) { _, question in
                Text(question)
            }
        case .browserSignIn(let reason?):
            Text(reason)
        case .localTool(let toolName?):
            Text("\(toolName) runs in the terminal app.")
        default:
            EmptyView()
        }
    }
}

#Preview("Transcript") {
    NavigationStack {
        TranscriptContent(transcript: PreviewData.transcript)
            .navigationTitle("Plan the field trip")
            .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview("Prompts") {
    ScrollView {
        VStack(spacing: 12) {
            PromptCard(prompt: PendingPrompt(id: "1", kind: .question(["Which week works best?"])))
            PromptCard(prompt: PendingPrompt(id: "2", kind: .form(serverName: "Example Tracker", message: nil)))
            PromptCard(prompt: PendingPrompt(id: "3", kind: .browserSignIn(reason: "Sign in to the portal")))
        }
        .padding()
    }
}

import AgentCoreKit
import SwiftUI

/// The inbox: waiting runs to answer, and everything else the web app's bell shows.
struct InboxView: View {
    let inbox: InboxModel
    let session: SessionModel
    let user: SessionUser
    @State private var path: [String] = []
    @State private var reviewing: InboxItem?
    @State private var showingAccount = false

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let problem = inbox.problem, !inbox.items.isEmpty {
                    Section {
                        LoadProblemText(problem: problem)
                            .font(.footnote)
                    }
                }
                ForEach(inbox.items) { item in
                    Button {
                        open(item)
                    } label: {
                        InboxRow(item: item)
                    }
                    .buttonStyle(.plain)
                    .task { await inbox.loadMore(after: item) }
                }
                if inbox.canLoadMore {
                    loadMoreRow
                }
            }
            .overlay { placeholder }
            .refreshable { await inbox.refresh() }
            .navigationTitle("Inbox")
            .navigationDestination(for: String.self) { conversationId in
                TranscriptView(model: inbox.transcript(for: conversationId), title: nil)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Mark all read") {
                        Task { await inbox.markAllRead() }
                    }
                    .disabled(inbox.unreadCount == 0)
                }
                AccountButton(showingAccount: $showingAccount)
            }
        }
        .task {
            if !inbox.hasLoaded {
                await inbox.refresh()
            }
        }
        .sheet(item: $reviewing, onDismiss: { Task { await inbox.refresh() } }) { item in
            InboxReviewView(model: inbox.review(item)) {
                reviewing = nil
                if let conversationId = item.conversationId {
                    path.append(conversationId)
                }
            }
        }
        .sheet(isPresented: $showingAccount) {
            AccountView(session: session, user: user, showsDone: true)
        }
    }

    /// Answer a waiting run here when the inbox can; otherwise open what the row is about.
    private func open(_ item: InboxItem) {
        Task { await inbox.markRead(item) }
        if let needsUser = item.needsUser, needsUser.canAnswerHere() {
            reviewing = item
        } else if let conversationId = item.conversationId {
            path.append(conversationId)
        }
    }

    @ViewBuilder
    private var loadMoreRow: some View {
        if inbox.loadMoreFailed {
            Button("Couldn't load more. Try again") {
                Task { await inbox.loadMore() }
            }
            .font(.footnote)
        } else {
            HStack {
                Spacer()
                ProgressView()
                Spacer()
            }
            .listRowSeparator(.hidden)
        }
    }

    @ViewBuilder
    private var placeholder: some View {
        if !inbox.items.isEmpty {
            EmptyView()
        } else if !inbox.hasLoaded {
            ProgressView()
        } else if let problem = inbox.problem {
            ContentUnavailableView {
                Label("Couldn't load your inbox", systemImage: "tray")
            } description: {
                LoadProblemText(problem: problem)
            } actions: {
                Button("Try again") {
                    Task { await inbox.refresh() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(inbox.isRefreshing)
            }
        } else {
            ContentUnavailableView(
                "Nothing here",
                systemImage: "tray",
                description: Text("Scheduled runs that need you, and other updates, appear here.")
            )
        }
    }
}

/// One inbox row: what happened, what it's about, and when.
struct InboxRow: View {
    let item: InboxItem

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle()
                .fill(item.isRead ? AnyShapeStyle(.clear) : AnyShapeStyle(.tint))
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)

            Image(systemName: symbol)
                .foregroundStyle(item.needsUser?.isOpen() == true ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
                .frame(width: 22)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.body.weight(item.isRead ? .regular : .semibold))
                    .lineLimit(2)
                InboxRowDetail(item: item)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 8)

            if let date = item.createdAt {
                Text(RowDate.text(for: date))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .contentShape(.rect)
        .accessibilityElement(children: .combine)
        .accessibilityValue(item.isRead ? Text("") : Text("Unread"))
    }

    private var symbol: String {
        switch item.kind {
        case .needsUser: "exclamationmark.bubble"
        case .watcherFound: "binoculars"
        case .schedulePaused: "pause.circle"
        case .handoff: "arrow.triangle.branch"
        case .project: "person.2"
        case .quota: "gauge.with.dots.needle.50percent"
        case .other: "bell"
        }
    }

    private var title: String {
        switch item.kind {
        case .needsUser(let needsUser):
            needsUser.label.nonEmpty ?? String(localized: "A scheduled run")
        case .watcherFound(_, let title):
            title.nonEmpty ?? String(localized: "Something new")
        case .schedulePaused(let label):
            String(localized: "\(label.nonEmpty ?? String(localized: "A schedule")) stopped running")
        case .handoff(let event, let agentName, _):
            switch event {
            case .needsInput:
                String(localized: "\(agentName.nonEmpty ?? String(localized: "An agent")) needs your input")
            case .done:
                String(localized: "\(agentName.nonEmpty ?? String(localized: "An agent")) finished")
            case .stopped:
                String(localized: "\(agentName.nonEmpty ?? String(localized: "An agent")) stopped")
            }
        case .project(let kind):
            kind == "project_invited"
                ? String(localized: "You were added to \(item.projectName.nonEmpty ?? String(localized: "a project"))")
                : String(localized: "Update in \(item.projectName.nonEmpty ?? String(localized: "a project"))")
        case .quota(let event):
            switch event {
            case .requested(let requester):
                String(localized: "\(requester.nonEmpty ?? String(localized: "Someone")) asked for more quota")
            case .approved: String(localized: "Your quota request was approved")
            case .declined: String(localized: "Your quota request was declined")
            case .moreInformationRequested:
                String(localized: "Your administrator asked for a funding source for your quota request")
            case .extensionGranted: String(localized: "Your quota was increased")
            case .extensionEnded: String(localized: "An extension to your quota was ended")
            case .other: String(localized: "Quota update")
            }
        case .other:
            String(localized: "An update this app can't show yet")
        }
    }
}

/// The second line: for a waiting run, where it stands; otherwise what it's about.
struct InboxRowDetail: View {
    let item: InboxItem

    var body: some View {
        switch item.kind {
        case .needsUser(let needsUser):
            NeedsUserDetail(needsUser: needsUser)
        case .watcherFound(let label, _):
            if let label = label.nonEmpty { Text(label) }
        case .handoff(_, _, let sourceTitle):
            if let sourceTitle = sourceTitle.nonEmpty { Text("From \(sourceTitle)") }
        case .project:
            if let actor = item.actorName.nonEmpty { Text("By \(actor)") }
        case .quota, .schedulePaused, .other:
            EmptyView()
        }
    }
}

struct NeedsUserDetail: View {
    let needsUser: NeedsUser

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            status
            if needsUser.isOpen() {
                ForEach(needsUser.prompts, id: \.interruptId) { prompt in
                    Text(PromptLine.text(for: prompt))
                        .lineLimit(1)
                }
            }
        }
    }

    @ViewBuilder
    private var status: some View {
        switch needsUser.state {
        case .waiting, .retry:
            if !needsUser.isOpen() {
                Text("Expired")
            } else if needsUser.state == .retry {
                Text("Couldn't continue last time. Answer again.").foregroundStyle(.orange)
            } else if let expiresAt = needsUser.expiresAt {
                Text("Waiting for your answer · expires \(expiresAt, format: .relative(presentation: .named))")
                    .foregroundStyle(.orange)
            } else {
                Text("Waiting for your answer").foregroundStyle(.orange)
            }
        case .applying:
            Text("Answer sent. The run is continuing.")
        case .answered(let decision):
            switch decision {
            case "approved": Text("Approved")
            case "declined": Text("Declined")
            case "skipped": Text("Skipped")
            default: Text("Answered")
            }
        case .answeredElsewhere:
            Text("Answered in its conversation")
        case .expired:
            Text("Expired")
        }
    }
}

/// One prompt as a row line, from the row's own display fields.
enum PromptLine {
    static func text(for prompt: NeedsUser.Prompt) -> String {
        switch prompt.kind {
        case "tool_approval":
            String(localized: "Allow \(prompt.toolName.nonEmpty ?? String(localized: "a tool")) to run?")
        case "user_question":
            prompt.headline.nonEmpty ?? String(localized: "A question for you")
        case "mcp_elicitation":
            prompt.headline.nonEmpty
                ?? String(
                    localized: "\(prompt.serverName.nonEmpty ?? String(localized: "A service")) needs some details")
        case "browser_login":
            prompt.headline.nonEmpty ?? String(localized: "Sign-in needed")
        case "oauth":
            String(localized: "Connect an account to continue")
        default:
            String(localized: "Waiting for you")
        }
    }
}

extension Optional where Wrapped == String {
    /// The string, unless it is missing or only whitespace.
    var nonEmpty: String? {
        guard let value = self?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else { return nil }
        return value
    }
}

#Preview("Inbox") {
    InboxView(inbox: PreviewData.inbox(), session: PreviewData.session(), user: PreviewData.user)
}

#Preview("Rows") {
    List {
        ForEach(PreviewData.inboxItems) { InboxRow(item: $0) }
    }
}

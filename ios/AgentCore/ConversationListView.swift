import AgentCoreKit
import SwiftUI

/// The user's conversations, newest first, a page at a time.
struct ConversationListView: View {
    let list: ConversationListModel
    @Binding var selection: String?

    var body: some View {
        List(selection: $selection) {
            if let problem = list.problem, !list.conversations.isEmpty {
                Section {
                    LoadProblemText(problem: problem)
                        .font(.footnote)
                }
            }

            ForEach(list.conversations) { conversation in
                ConversationRow(conversation: conversation)
                    .tag(conversation.id)
                    .task { await list.loadMore(after: conversation) }
            }

            if list.canLoadMore {
                loadMoreRow
            }
        }
        .overlay { placeholder }
        .refreshable { await list.refresh() }
        .task {
            if !list.hasLoaded {
                await list.refresh()
            }
        }
    }

    @ViewBuilder
    private var loadMoreRow: some View {
        if list.loadMoreFailed {
            Button("Couldn't load more. Try again") {
                Task { await list.loadMore() }
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
        if !list.conversations.isEmpty {
            EmptyView()
        } else if !list.hasLoaded {
            ProgressView()
        } else if let problem = list.problem {
            ContentUnavailableView {
                Label("Couldn't load conversations", systemImage: "exclamationmark.bubble")
            } description: {
                LoadProblemText(problem: problem)
            } actions: {
                Button("Try again") {
                    Task { await list.refresh() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(list.isRefreshing)
            }
        } else {
            ContentUnavailableView(
                "No conversations yet",
                systemImage: "bubble.left.and.bubble.right",
                description: Text("Conversations you have on the web appear here.")
            )
        }
    }
}

/// One conversation: its title, when it last changed, and whether it needs the user.
struct ConversationRow: View {
    let conversation: ConversationSummary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Circle()
                .fill(conversation.unread ? AnyShapeStyle(.tint) : AnyShapeStyle(.clear))
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: 4) {
                Text(conversation.title.isEmpty ? String(localized: "Untitled conversation") : conversation.title)
                    .font(.body.weight(conversation.unread ? .semibold : .regular))
                    .lineLimit(2)
                if conversation.awaitingInput != nil {
                    Label("Waiting for your answer", systemImage: "exclamationmark.bubble.fill")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
            }

            Spacer(minLength: 8)

            if let date = conversation.lastMessageAt {
                Text(RowDate.text(for: date))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
        .accessibilityValue(conversation.unread ? Text("Unread") : Text(""))
    }
}

/// Today's time, this week's weekday, or the date.
enum RowDate {
    static func text(for date: Date, now: Date = .now, calendar: Calendar = .current) -> String {
        if calendar.isDate(date, inSameDayAs: now) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 7 {
            return date.formatted(.dateTime.weekday(.abbreviated))
        }
        return date.formatted(date: .abbreviated, time: .omitted)
    }
}

/// `LoadProblem` in words.
struct LoadProblemText: View {
    let problem: LoadProblem

    var body: some View {
        switch problem {
        case .unreachable:
            Text("Couldn't reach the server. Check your connection and try again.")
        case .notFound:
            Text("This conversation isn't available. It may have been deleted.")
        case .serverError:
            Text("The server had a problem. Try again in a moment.")
        }
    }
}

#Preview("List") {
    @Previewable @State var selection: String?
    NavigationStack {
        ConversationListView(list: PreviewData.list(), selection: $selection)
            .navigationTitle("Conversations")
    }
}

#Preview("Empty") {
    @Previewable @State var selection: String?
    NavigationStack {
        ConversationListView(list: PreviewData.list(body: #"{"sessions": []}"#), selection: $selection)
            .navigationTitle("Conversations")
    }
}

#Preview("Row") {
    List {
        ConversationRow(
            conversation: ConversationSummary(
                id: "c-1", title: "Weekly watch: new grants", lastMessageAt: .now, unread: true,
                awaitingInput: .toolApproval))
    }
}

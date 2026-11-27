import AgentCoreKit
import SwiftUI

/// The signed-in app: Conversations and the Inbox as tabs, each with the
/// account sheet (and Sign out) one tap away.
struct SignedInView: View {
    let session: SessionModel
    let user: SessionUser
    @State private var list: ConversationListModel?
    @State private var inbox: InboxModel?

    init(session: SessionModel, user: SessionUser) {
        self.session = session
        self.user = user
        self._list = State(initialValue: session.makeConversationList())
        self._inbox = State(initialValue: session.makeInbox())
    }

    var body: some View {
        if let list, let inbox {
            SignedInTabs(list: list, inbox: inbox, session: session, user: user)
        } else {
            // Signed in with no stored session can't last: the next check signs out.
            AccountView(session: session, user: user)
        }
    }
}

struct SignedInTabs: View {
    let list: ConversationListModel
    let inbox: InboxModel
    let session: SessionModel
    let user: SessionUser

    var body: some View {
        TabView {
            Tab("Conversations", systemImage: "bubble.left.and.bubble.right") {
                ConversationsView(list: list, session: session, user: user)
            }
            Tab("Inbox", systemImage: "tray") {
                InboxView(inbox: inbox, session: session, user: user)
            }
            .badge(inbox.unreadCount)
        }
        // The badge needs the count before the Inbox tab is ever opened.
        .task {
            if !inbox.hasLoaded {
                await inbox.refresh()
            }
        }
    }
}

struct ConversationsView: View {
    let list: ConversationListModel
    let session: SessionModel
    let user: SessionUser
    @State private var selection: String?
    @State private var showingAccount = false

    var body: some View {
        NavigationSplitView {
            ConversationListView(list: list, selection: $selection)
                .navigationTitle("Conversations")
                .toolbar { AccountButton(showingAccount: $showingAccount) }
        } detail: {
            if let selection {
                TranscriptView(
                    model: list.transcript(for: selection),
                    title: list.conversations.first { $0.id == selection }?.title
                )
                .id(selection)
            } else {
                ContentUnavailableView(
                    "Choose a conversation",
                    systemImage: "bubble.left.and.bubble.right",
                    description: Text("Pick one from the list to read it.")
                )
            }
        }
        .sheet(isPresented: $showingAccount) {
            AccountView(session: session, user: user, showsDone: true)
        }
    }
}

/// Opens the account sheet.
struct AccountButton: ToolbarContent {
    @Binding var showingAccount: Bool

    var body: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Button {
                showingAccount = true
            } label: {
                Label("Account", systemImage: "person.crop.circle")
            }
        }
    }
}

/// Who is signed in, where, and the way out.
struct AccountView: View {
    let session: SessionModel
    let user: SessionUser
    var showsDone = false
    @State private var confirmingSignOut = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section("Signed in as") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(user.name ?? user.email ?? user.userId)
                            .font(.headline)
                        if let email = user.email, user.name != nil {
                            Text(email)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                }

                if let host = session.server?.baseURL.host() {
                    Section("Server") {
                        Text(host)
                    }
                }

                Section {
                    Button("Sign out", role: .destructive) {
                        confirmingSignOut = true
                    }
                    // On the button, so the dialog points at what was tapped.
                    .confirmationDialog(
                        "Sign out of AgentCore?",
                        isPresented: $confirmingSignOut,
                        titleVisibility: .visible
                    ) {
                        Button("Sign out", role: .destructive) {
                            Task { await session.signOut() }
                        }
                    } message: {
                        Text("You'll need to sign in again to use the app.")
                    }
                }
            }
            .navigationTitle("Account")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if showsDone {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { dismiss() }
                    }
                }
            }
        }
    }
}

#Preview("Signed in") {
    SignedInTabs(
        list: PreviewData.list(), inbox: PreviewData.inbox(), session: PreviewData.session(), user: PreviewData.user)
}

#Preview("Account") {
    AccountView(session: PreviewData.session(), user: PreviewData.user, showsDone: true)
}

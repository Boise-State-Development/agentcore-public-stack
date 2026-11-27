import AgentCoreKit
import SwiftUI

/// The signed-in app: the conversation list, a transcript beside it on iPad
/// or pushed on iPhone, and the account sheet with Sign out.
struct SignedInView: View {
    let session: SessionModel
    let user: SessionUser
    @State private var list: ConversationListModel?

    init(session: SessionModel, user: SessionUser) {
        self.session = session
        self.user = user
        self._list = State(initialValue: session.makeConversationList())
    }

    var body: some View {
        if let list {
            ConversationsView(list: list, session: session, user: user)
        } else {
            // Signed in with no stored session can't last: the next check signs out.
            AccountView(session: session, user: user)
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
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            showingAccount = true
                        } label: {
                            Label("Account", systemImage: "person.crop.circle")
                        }
                    }
                }
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

#Preview("Conversations") {
    ConversationsView(list: PreviewData.list(), session: PreviewData.session(), user: PreviewData.user)
}

#Preview("Account") {
    AccountView(session: PreviewData.session(), user: PreviewData.user, showsDone: true)
}

import AgentCoreKit
import SwiftUI

/// Who is signed in, where, and the way out. Conversations replace the body
/// of this screen next.
struct SignedInView: View {
    let session: SessionModel
    let user: SessionUser
    @State private var confirmingSignOut = false

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
            .navigationTitle("AgentCore")
        }
    }
}

import AgentCoreKit
import SwiftUI

/// First launch: which deployment to sign in to. Nothing is compiled in, so
/// every organisation's build of the app asks.
struct ServerView: View {
    let session: SessionModel
    @State private var address = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://ai.example.edu/api", text: $address)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.continue)
                        .focused($focused)
                        .onSubmit(connect)
                } header: {
                    Text("Server address")
                } footer: {
                    if let problem = session.problem {
                        ProblemText(problem: problem)
                    } else {
                        Text("The address your organisation gave you for the app. It usually ends in /api.")
                    }
                }

                Section {
                    Button(action: connect) {
                        HStack {
                            Text("Continue")
                            Spacer()
                            if session.isBusy {
                                ProgressView()
                            }
                        }
                    }
                    .disabled(address.trimmingCharacters(in: .whitespaces).isEmpty || session.isBusy)
                }
            }
            .navigationTitle("AgentCore")
            .onAppear { focused = true }
        }
    }

    private func connect() {
        Task { await session.connect(to: address) }
    }
}

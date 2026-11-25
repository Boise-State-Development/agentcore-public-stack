import AgentCoreKit
import SwiftUI

/// The words for each `SessionModel.Problem`, kept in the app so they are
/// localised with the rest of its strings.
struct ProblemText: View {
    let problem: SessionModel.Problem

    var body: some View {
        Text(message)
            .foregroundStyle(.red)
    }

    private var message: LocalizedStringKey {
        switch problem {
        case .invalidServer(.empty):
            "Enter the server's address."
        case .invalidServer(.insecureScheme):
            "The address must start with https://."
        case .invalidServer(.unsupportedScheme):
            "The address must start with https://."
        case .invalidServer(.malformed):
            "That doesn't look like a web address."
        case .unreachable:
            "Couldn't reach that server. Check the address and your connection."
        case .signInNotSupported:
            "This server doesn't support signing in from the app yet."
        case .signInFailed:
            "Sign-in didn't finish. Try again."
        case .sessionExpired:
            "Your session ended. Sign in again."
        case .couldNotSaveSession:
            "Couldn't save your sign-in on this device. Try again."
        }
    }
}

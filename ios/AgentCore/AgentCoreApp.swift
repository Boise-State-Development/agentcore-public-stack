import AgentCoreKit
import SwiftUI

@main
struct AgentCoreApp: App {
    @State private var session = SessionModel.live()

    var body: some Scene {
        WindowGroup {
            RootView(session: session)
                .task { await session.start() }
        }
    }
}

extension SessionModel {
    /// The model wired to the real Keychain, UserDefaults and network.
    static func live() -> SessionModel {
        let bundleIdentifier = Bundle.main.bundleIdentifier ?? "org.example.agentcore"
        return SessionModel(
            serverStore: UserDefaultsServerStore(),
            sessionStore: KeychainSessionStore(service: "\(bundleIdentifier).session"),
            redirect: NativeRedirect(bundleIdentifier: bundleIdentifier),
            device: DeviceDescription(model: UIDevice.current.model, appVersion: AppInfo.marketingVersion)
        )
    }
}

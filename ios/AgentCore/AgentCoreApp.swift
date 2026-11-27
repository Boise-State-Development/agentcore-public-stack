import AgentCoreKit
import SwiftUI

@main
struct AgentCoreApp: App {
    @State private var session = SessionModel.live()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(session: session)
                .task { await session.start() }
        }
        // A session signed out from another device while the app was away
        // goes back to sign-in as soon as the app is back.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await session.revalidate() }
            }
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

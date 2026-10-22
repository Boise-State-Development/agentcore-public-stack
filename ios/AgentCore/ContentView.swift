import SwiftUI

/// Placeholder root view. The chat surface arrives once the native auth path
/// (docs/specs, next PR) and the API client exist; until then this screen
/// only proves the app builds, launches and carries the repo version.
struct ContentView: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.system(size: 56))
                .foregroundStyle(.tint)
            Text("AgentCore")
                .font(.largeTitle.bold())
            Text("Companion app scaffold · v\(AppInfo.marketingVersion)")
                .foregroundStyle(.secondary)
            Text("Not connected to a server yet.")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .padding()
    }
}

#Preview {
    ContentView()
}

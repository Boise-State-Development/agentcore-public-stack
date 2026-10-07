import Foundation

/// Build-time facts the app shows about itself. `MARKETING_VERSION` is written
/// into Config/Version.xcconfig from the repo-root VERSION file by
/// scripts/common/sync-version.sh, so this is the same number the web app,
/// backend and TUI report.
enum AppInfo {
    static var marketingVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    }
}

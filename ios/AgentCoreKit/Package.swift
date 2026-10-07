// swift-tools-version: 6.2
//
// Everything that is not a view lives here, not in the app target: the app
// shell stays a thin host so contributors rarely touch the Xcode project
// file, and the package's tests run without a simulator UI.
//
// Dependency policy (CLAUDE.md): every package added here is pinned with
// `exact:` and Package.resolved is committed. No ranges.

import PackageDescription

let package = Package(
    name: "AgentCoreKit",
    platforms: [.iOS(.v18)],
    products: [
        .library(name: "AgentCoreKit", targets: ["AgentCoreKit"])
    ],
    targets: [
        .target(
            name: "AgentCoreKit",
            swiftSettings: [.treatAllWarnings(as: .error)]
        ),
        .testTarget(
            name: "AgentCoreKitTests",
            dependencies: ["AgentCoreKit"],
            swiftSettings: [.treatAllWarnings(as: .error)]
        ),
    ],
    swiftLanguageModes: [.v6]
)

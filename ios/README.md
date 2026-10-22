# AgentCore iOS

A native iOS companion app for the AgentCore platform. Same models, same
RBAC, same quotas and the same cost tracking as the web app, on a phone.

## What it does today

Nothing yet. This is the scaffold: an app that builds, launches in the
simulator, carries the repo version, and runs its unit tests in CI. It exists
to settle the project structure, hygiene and gates before any product code
lands, because those are far harder to retrofit.

Next up, in order:

1. **Native auth path.** The SPA signs in with an httpOnly session cookie
   through the BFF and the TUI uses an API key against a tools-less endpoint.
   Neither fits a phone. The design (OIDC via `ASWebAuthenticationSession`,
   plus whatever app-api needs to issue a token the phone can hold) gets a
   spec under `docs/specs/` before any Swift is written.
2. **API client** in `AgentCoreKit`: sessions, SSE streaming, the event types
   in the root `CLAUDE.md`.
3. **Chat surface** in the app target.

## Requirements

- macOS with **Xcode 26 or newer** (the package is `swift-tools-version: 6.2`).
  The repo is currently built with Xcode 27.
- No Apple developer account for the simulator or for CI.

## Build and test

```bash
cd ios
xcodebuild test \
  -workspace AgentCore.xcworkspace \
  -scheme AgentCore \
  -destination 'platform=iOS Simulator,name=iPhone 17' \
  CODE_SIGNING_ALLOWED=NO
```

Or open `AgentCore.xcworkspace` in Xcode and press ⌘U. The scheme's test action
runs the `AgentCoreKit` package tests (Swift Testing). Always the workspace,
not the project: a local package's tests are only testable when the package is
a workspace member, so `-project` reports "no test bundles available".

Format and lint with the `swift format` that ships inside Xcode; nothing to
install:

```bash
cd ios
swift format lint --strict --recursive --parallel AgentCore AgentCoreKit
swift format --in-place --recursive AgentCore AgentCoreKit
```

## Layout

```
ios/
├── AgentCore.xcworkspace/    Open this. Holds the project and the package.
├── AgentCore.xcodeproj/      Thin app shell. Synchronized folders, so adding a
│                             file does not touch project.pbxproj.
├── AgentCore/                App target: SwiftUI views, assets, privacy manifest.
├── AgentCoreKit/             Local Swift package: everything that is not a view,
│   ├── Sources/              with its own test target. Most work lands here.
│   └── Tests/
├── Config/                   All build settings, as xcconfig (see below).
├── .swift-format             Formatter and lint rules.
└── .swift-version            Minimum toolchain.
```

The project file carries **no build settings**. Everything is in `Config/`:

| File | Holds | Tracked |
|---|---|---|
| `Base.xcconfig` | Platform, Swift 6 language mode, complete concurrency checking, warnings as errors, generated Info.plist keys | yes |
| `Debug.xcconfig` / `Release.xcconfig` | Per-configuration optimisation and debug-info settings | yes |
| `Version.xcconfig` | `MARKETING_VERSION`, generated from the repo-root `VERSION` by `scripts/common/sync-version.sh` | yes, do not edit |
| `Local.xcconfig` | `DEVELOPMENT_TEAM` and a real `PRODUCT_BUNDLE_IDENTIFIER` | **no** (copy from `Local.xcconfig.example`) |

The committed bundle id is a placeholder so an unsigned simulator build works
on a fresh clone. Building for a device or for distribution needs
`Local.xcconfig`. Never commit a team id, a real bundle id, a provisioning
profile or an App Store Connect key.

## Conventions

- **Swift 6, strict concurrency, warnings are errors.** In the app via
  xcconfig, in the package via `.treatAllWarnings(as: .error)`.
- **Exact dependency pins**, as everywhere in this repo. A package added to
  `Package.swift` uses `exact:` and `Package.resolved` is committed. The
  scaffold has no third-party dependencies, and the bar for adding one is
  high.
- **Thin app, fat package.** Views live in the app target. Models, networking,
  parsing and anything testable without a UI live in `AgentCoreKit`.
- **Nothing deployment-specific in the tree.** The server URL is entered by
  the user (`ServerConfiguration` validates it: `https` anywhere, `http` only
  on loopback). No host is compiled in.
- **Privacy manifest grows with the code.** `PrivacyInfo.xcprivacy` is empty
  on purpose. The PR that first touches a required-reason API (UserDefaults,
  file timestamps, …) adds the entry.
- **Version comes from `VERSION`.** Do not edit `MARKETING_VERSION` by hand;
  bump the root file and run `bash scripts/common/sync-version.sh`. A
  pre-release suffix is dropped for iOS because `CFBundleShortVersionString`
  must be plain dotted numbers.

## CI

The PR gate (`.github/workflows/ci.yml`) classifies changed paths and runs the
`test-ios` job in `tests.yml` only when `ios/**` changes. It runs on GitHub's
pinned `xcode-27` image, lints with `swift format`, picks the newest iPhone
simulator on the image, and runs `xcodebuild test` unsigned. macOS minutes
cost about ten times Ubuntu minutes, which is why the job is path-gated and
why no other workflow runs it.

App Store distribution (signing, TestFlight, fastlane, metadata) is
deliberately not part of the scaffold. It belongs with the first release that
has something to ship, in its own PR, with secrets held in a GitHub
environment and never in the repo.

## License

Same as the rest of the repository: PolyForm Noncommercial 1.0.0. That is a
source-available licence, not an OSI open-source one, which matters if you
intend to publish a build under your own account.

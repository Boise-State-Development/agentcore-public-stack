# AgentCore iOS

A native iOS companion app for the AgentCore platform. Same models, same
RBAC, same quotas and the same cost tracking as the web app, on a phone.

## What it does today

Sign-in, reading your conversations, and your inbox:

- **Choose a server.** The user enters the deployment's app-api address. It
  is checked (`https` anywhere, `http` only on loopback) and must answer
  `GET /auth/providers` before it is kept.
- **Sign in** in the system sheet (`ASWebAuthenticationSession`, shared with
  Safari), straight to the organisation's identity provider when the
  deployment has one, then exchange a one-time handoff code plus a PKCE
  verifier for a session. The session goes in the Keychain (this device only,
  after first unlock) and is sent as `Authorization: BFF <sealed>`.
- **Stay signed in** across launches, checked with `GET /auth/session`. An
  expired session goes back to sign-in with a note. Being offline keeps the
  session and offers Try again.
- **Sign out**, dropping the local copy first and then telling the server.
- **Conversations**, newest first, a page at a time (`GET /sessions`), with
  the unread dot a scheduled run leaves and a "Waiting for your answer" line
  on a conversation paused on a prompt. Pull to refresh.
- **Read a conversation** (`GET /sessions/{id}/messages`, every page): the
  text as markdown, each tool call as one line saying whether it finished,
  attachments as labelled chips, and a paused prompt (an approval, a
  question, a form, a sign-in) as a card that says it is waiting. Answering
  it from the app is next. Anything newer than the app (a content type it
  doesn't know) shows as "Not shown in this app" rather than failing.
  Side by side on iPad, pushed on iPhone.
- **Inbox** (`GET /notifications`), with an unread badge on its tab: a
  scheduled run waiting on you, a watcher's finding, a handoff that finished,
  a project or quota update. Opening one marks it read.
- **Answer a waiting run from the phone**: approve or decline a tool (with
  what it will send), answer the agent's questions, or fill in a connected
  service's form. Every answer goes in one request
  (`POST /notifications/{id}/respond`), then the app follows the run
  (`GET /runs/{id}`) and says how it ended. A sign-in or an account
  connection still needs the web app. The server half is the deployment's
  `PROACTIVE_RUNS_ENABLED`; without it, the app opens the conversation instead.

The design is `docs/specs/native-app-auth.md`. The server half
(`client=native` on `/auth/login` and `POST /auth/native/exchange`) is in
app-api, off until a deployment lists the app's redirect address in
`NATIVE_AUTH_REDIRECT_URIS`. A server that has not answers the exchange with a
404, which the app reports as "doesn't support signing in from the app yet".

Next up, in order:

1. **Signed-in devices**: list and revoke app sessions (spec PR 3).
2. **Push** notifications.
3. **Chat**: sending a turn, SSE streaming, the event types in the root
   `CLAUDE.md`.

### Trying sign-in

The app's redirect address is its bundle id, lowercased, as a scheme:
`<bundle id>://auth/callback`. A fresh clone's placeholder bundle id gives
`org.example.agentcore://auth/callback`; a build with your own bundle id in
`Local.xcconfig` needs that address added to the deployment's
`NATIVE_AUTH_REDIRECT_URIS` (`CDK_NATIVE_AUTH_REDIRECT_URIS` in the GitHub
environment) before it can sign in.

- Build **with** simulator signing (the default). `CODE_SIGNING_ALLOWED=NO`
  builds an app with no entitlements, and the Keychain then refuses to store
  the session (`-34018`), which the app reports as "Couldn't save your
  sign-in".
- Against a local stack on loopback (`http://localhost:<port>/api`), iOS logs
  that `ASWebAuthenticationSession` support for `http` is deprecated. It works
  today for loopback only; a local stack behind `https` avoids it.

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

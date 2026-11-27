# CLAUDE.md — iOS companion app

This adds to the repo-root `CLAUDE.MD` and doesn't replace it. The root file's cross-package contracts, SSE event table, auth rules and git workflow all apply here. This file covers what is specific to `ios/`. `ios/README.md` covers the same ground for people.

## Identity

- SwiftUI app for iPhone and iPad, **iOS 18.0** deployment target (`Config/Base.xcconfig`). Use only APIs available on iOS 18, or guard newer ones with `if #available`.
- **Swift 6.2 toolchain** (`.swift-version`, `swift-tools-version: 6.2`), Swift 6 language mode, `SWIFT_STRICT_CONCURRENCY = complete`, **every warning is an error** in both the app and the package. Xcode 26 or newer; CI builds with Xcode 27.
- **Thin app, fat package.** The app target holds views and app lifecycle only. Models, networking, SSE parsing, auth and anything testable without a UI go in the local `AgentCoreKit` package.
- Bundle id `org.example.agentcore` is a placeholder. A real one comes from the untracked `Config/Local.xcconfig`.
- Status: sign-in works end to end against a deployment (`docs/specs/native-app-auth.md`), and the signed-in app has two tabs: a read-only conversation list and transcript, and the inbox, which answers a waiting scheduled run from the phone. Next are signed-in devices, push, and chat with streaming.

## Open source first

This is a public repository. Other organisations clone it and ship **their own** app under their own Apple account, bundle id and deployment. Weigh every design, dependency and default against that: the right answer is one another organisation can adopt by changing configuration, never code.

- **Nothing deployment-specific in the tree.** No hosts, team ids, real bundle ids, tenant or IdP names, institution branding, App Store Connect keys or provisioning profiles. The server URL is entered by the user (`ServerConfiguration`), later supplied by MDM managed configuration, and never compiled in. Per-build identity lives in `Local.xcconfig`, which is untracked.
- **Derive, don't hard-code.** The sign-in URL scheme is the bundle id (spec), so it follows `PRODUCT_BUNDLE_IDENTIFIER`. Never write `edu.example.agentcore://` or any literal scheme into code.
- **Example values only** in code, tests, previews and docs: `ai.example.edu`, `user@example.edu`, `ABCDE12345`. Never a real host, user, session id or screenshot of a real deployment.
- **A fresh clone must build and test unsigned in the simulator** with nothing but Xcode. No Apple account, secrets or local file required. Don't add a step that breaks this.
- **Dependencies are a decision for every fork.** Prefer Apple's frameworks. Ask before adding a package. If one is approved, pin it with `exact:`, commit `Package.resolved`, and check its licence is compatible with the repo's (PolyForm Noncommercial 1.0.0). Never add analytics, crash reporting or other SDKs that send data off the device: telemetry is each deployment's choice, and the privacy manifest has to stay truthful.
- **Anything a deployment must configure** for the app (for example `NATIVE_AUTH_REDIRECT_URIS` on app-api) is documented in `docs-site/`, not only here or in a spec.
- **Backend support for the app follows the root feature-flag rules:** a capability a deployment may not want defaults off, and an allowlist that is empty by default counts as the switch.

## Build and test

Always build with `-workspace`, never `-project`. A local package's tests only exist as a workspace member, so `-project` reports "no test bundles available".

```bash
cd ios
xcodebuild test -workspace AgentCore.xcworkspace -scheme AgentCore \
  -destination 'platform=iOS Simulator,name=iPhone 17' CODE_SIGNING_ALLOWED=NO
swift format lint --strict --recursive --parallel AgentCore AgentCoreKit
swift format --in-place --recursive AgentCore AgentCoreKit
```

- If the session has Xcode's MCP tools or the iOS simulator tool, prefer them for build, test, run and preview. Their output is structured and smaller. The commands above are the fallback, and they are what CI runs.
- CI (`test-ios` in `.github/workflows/tests.yml`) runs only when `ios/**` changes. It lints, then tests on the newest iPhone simulator on the pinned `xcode-27` image. macOS minutes cost about ten times Linux minutes, so don't add iOS steps to other workflows.
- Before calling a change done, run lint and tests, and launch the app in the simulator for anything a user would see.

## Layout

```
ios/
├── AgentCore.xcworkspace/      Open and build this. Holds the project and the package.
├── AgentCore.xcodeproj/        App shell. Synchronized folders, no build settings. Don't edit by hand.
├── AgentCore/                  App target: views and app lifecycle only.
│   ├── AgentCoreApp.swift      @main entry point; builds the live SessionModel.
│   ├── RootView.swift          Switches screens on SessionModel.phase.
│   ├── ServerView.swift        Enter and check the server address.
│   ├── SignInView.swift        Providers and the ASWebAuthenticationSession sheet.
│   ├── SignedInView.swift      The signed-in tabs, the conversations split view, and the account sheet with Sign out.
│   ├── ConversationListView.swift  Conversations, newest first, paged; pull to refresh.
│   ├── TranscriptView.swift    One conversation, read-only: markdown, tool rows, attachment chips, paused prompts.
│   ├── InboxView.swift         The inbox: every row kind, the unread badge, Mark all read.
│   ├── InboxReviewView.swift   Answer a waiting run: approvals, questions, forms; then how the run ended.
│   ├── PreviewData.swift       Example data and a no-network transport for #Preview.
│   ├── ProblemText.swift       SessionModel.Problem in words.
│   ├── AppInfo.swift           Build-time facts (marketing version from Info.plist).
│   ├── PrivacyInfo.xcprivacy   Privacy manifest (UserDefaults, for the saved server).
│   └── Assets.xcassets/        App icon and accent color.
├── AgentCoreKit/               Local Swift package. Most work lands here.
│   ├── Package.swift           iOS 18, Swift 6 mode, warnings as errors, no third-party deps.
│   ├── Sources/AgentCoreKit/   Library code: server validation, PKCE, the client, Keychain store, SessionModel, conversation and transcript models.
│   └── Tests/AgentCoreKitTests/ Swift Testing suites, one file per source file (FooTests.swift).
├── Config/                     All build settings, as xcconfig.
│   ├── Base.xcconfig           Platform, Swift settings, generated Info.plist keys, placeholder identity.
│   ├── Debug/Release.xcconfig  Per-configuration settings. Both include Local.xcconfig last.
│   ├── Version.xcconfig        Generated from the repo-root VERSION. Don't edit.
│   └── Local.xcconfig.example  Template for the untracked signing file.
├── .swift-format               Formatter and lint rules (no force unwrap or force try, ordered imports).
└── .swift-version              Minimum toolchain.
```

## Rules

**Project files**
- **Never edit `project.pbxproj` by hand.** Synchronized folders mean a new file under `AgentCore/` is picked up without touching it. If a change seems to need the project file (a new target, a build phase), stop and ask.
- Build settings go in `Config/*.xcconfig`, never in the project. Info.plist keys are `INFOPLIST_KEY_*` lines in `Base.xcconfig`, because the plist is generated and there is no Info.plist file.
- Ask before changing the deployment target, entitlements, capabilities or URL types.
- To bump the version, edit the repo-root `VERSION` and run `bash scripts/common/sync-version.sh`. Never edit `MARKETING_VERSION`.

**Swift and SwiftUI**
- `@Observable` with `@State` / `@Bindable`, never `ObservableObject`, `@StateObject` or `@Published`. `NavigationStack` / `NavigationSplitView`, never `NavigationView`.
- Observable UI state is `@MainActor`. Types that cross actors are `Sendable` value types. Use `async`/`await` and actors, not completion handlers, Combine or GCD. No `@unchecked Sendable`, `nonisolated(unsafe)` or `@preconcurrency import` without a comment saying why it is safe.
- No force unwraps, force `try` or implicitly unwrapped optionals (`.swift-format` enforces this). Prefer early exits. Use typed throws for an error domain the caller handles (see `ServerConfigurationError`).
- Tests use **Swift Testing** (`@Suite`, `@Test`, `#expect`, parameterized `arguments:`), not XCTest. Test `AgentCoreKit` code in the package without a UI.
- Every view gets a `#Preview` with example data, so it can be checked with the preview renderer.
- User-facing text goes through String Catalogs (`LOCALIZATION_PREFERS_STRING_CATALOGS`). Support Dynamic Type and give VoiceOver labels to icon-only controls.
- When code first uses a required-reason API (UserDefaults, file timestamps, disk space, …), add its entry to `PrivacyInfo.xcprivacy` in the same PR.
- No `print()`. Use `Logger` from `os` with a subsystem and category. Never log a sealed session value, token, handoff code or message content.

## Talking to the backend

- **The app is a third client of the API and SSE contract**, after the SPA and the terminal client (root `CLAUDE.MD`, Cross-Package Contracts). app-api's route handlers define the shape, and Swift models match them. A renamed or reshaped event breaks all three clients. When the app needs a different shape, change the backend and every client in one PR.
- **Decode leniently.** Ignore unknown JSON fields and skip unknown SSE event names (log each once per turn), as the TUI does, so a new backend event never breaks a shipped app. `tui/src/aps_tui/client/agent_events.py` is a working reference parser.
- **The same rule holds for stored messages.** `GET /sessions/{id}/messages` decodes only what the transcript shows (`MessageWireModels.swift`), through `LenientList` and `lenient(_:forKey:)`, so one element that doesn't decode costs that element, never the screen. An unknown content block type renders as "Not shown in this app", a message that doesn't decode stays as a placeholder, and the replay fields (`toolSummaries`, `handoffs`, …) are ignored by not being declared. Add a field by decoding it the same way, with a fixture test.
- **Answers go out exactly as the web app's cards send them.** `POST /notifications/{id}/respond` takes, per prompt, `"approved"` / `"declined"`, `{answers: {header: {selected, text?}}}` / `{skipped: true}`, or `{action: "accept", content}` / `{action: "decline"}`, never null (`inbox_answer._decision_for`). `InboxAnswers.swift` builds them and checks a form with the server's own rules (`validate_answer`), so the server never refuses what the app sends. Answers are encoded with default keys: a key strategy would rewrite the user's question headers and form field names.
- **Two key spellings.** The `/auth/*` routes answer in snake_case; the `/sessions` routes answer in their Pydantic aliases (camelCase). `AgentCoreClient.decode` takes the key strategy per call.
- **Errors arrive as assistant messages** (`stream_error`) inside a 200 stream, not as HTTP error codes. Render them in the conversation.
- **Auth** (`docs/specs/native-app-auth.md`):
  - The app presents its session as `Authorization: BFF <sealed>`, the same header transport the terminal client uses. Never `Bearer`, which upstream means a Cognito JWT. The app never holds Cognito tokens.
  - Sign-in is the app-bound handoff: `/auth/login?client=native` in a non-ephemeral `ASWebAuthenticationSession`, a one-time handoff code, an app-side PKCE verifier, then `POST /auth/native/exchange`. Don't use the terminal's device grant (`/auth/cli/*`): it isn't bound to the device that started it.
  - Call this path **native**, not "device". In this codebase, "device" means the terminal's device grant.
  - Store the sealed value only in the Keychain (`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, no iCloud sync). Never in UserDefaults, files, URLs or logs. A `401` means signed out.
  - Don't invent a new way to present a session. If the app needs something new from the backend, it goes in app-api. Never add routes to inference-api (root file, Inference API boundary).
- **Local development:** `ServerConfiguration` allows `http://` on loopback only, so the simulator can reach a local app-api at `http://localhost:8000`.

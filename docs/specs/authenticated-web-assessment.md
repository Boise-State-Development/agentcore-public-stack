# Authenticated web assessment (browser takeover, profiles, axe)

**Status:** IN PROGRESS — **PRs 1, 2, 3 merged; 2b (the viewer) built**. D3 and D6 rewritten (backend takeover: `request_user_login`, take/release control, the `browser_login_required` interrupt + SSE event, the D4 metadata projection, reaper pinning, the abandonment deadline, and the flag) and **PR 2's backend + SPA half** (the live-view route, its IAM, and the SPA's event/resume wiring). The viewer page itself is deliberately still open — see "Build notes (PR 2)". PRs 3-4 not started. Sized for four PRs.
**Driver:** Two user requests, both blocked on the same missing capability:
a Library agent that evaluates the accessibility of the databases they renew
annually (subscription-gated), and a VPAT-evaluation agent that must test
vendor demo instances that are not public.
**Refs:** `backend/src/agents/builtin_tools/browser/`,
`docs/specs/ask-user-question.md` (interrupt pattern),
`docs/kaizen/scoping/mcp-apps-host-renderer.md` (sandbox-proxy pattern),
PR #1101 (never put a presigned URL in a tool result)

## Problem

`browse_web` can read any page that a logged-out, AWS-hosted Chromium can
reach. Every target in both requests fails at least one of those conditions:

- **Login-gated, publicly routable.** A subscription database; a vendor's demo
  tenant behind a username and password. This is the bulk of the Library's
  renewal list.
- **Network-gated.** Campus-IP-only, VPN, or a vendor IP allowlist.
- **Private TLS.** A staging instance with an internal CA.

There is no path for a user to *authenticate* a browsing session today. The
naive fix — let the user paste credentials into the composer so the agent can
type them — is the one option that must never ship: the secret would land in
the prompt, in AgentCore Memory, and in the cacheable prefix, and be re-read on
every subsequent turn for the life of the conversation.

## What already exists

Nearly all of it. This is a wiring spec, not a capability spec.

**Human takeover is in the pinned SDK.** `bedrock-agentcore==1.21.0` ships the
pair that swaps the browser between agent control and human control, both thin
wrappers over `UpdateBrowserStream`:

```
browser_client.py:636   def take_control(self):     self.update_stream("DISABLED")
browser_client.py:648   def release_control(self):  self.update_stream("ENABLED")
```

**The IAM is already granted.** `UpdateBrowserStream` and
`ConnectBrowserLiveViewStream` are both on the Runtime execution role
(`infrastructure/lib/constructs/inference-api/inference-agentcore-construct.ts:227`).
Nothing calls them.

**Persistent login is a `start()` argument.** `profile_configuration=
{"profileIdentifier": ...}` (`browser_client.py:320`) persists browser state —
cookies, local storage — across sessions. This is what turns "log in every
conversation" into "log in once per vendor, per year."

**Network and TLS are `create_browser` arguments.** `network_configuration`
(`PUBLIC` | `VPC` + `vpcConfig`), `certificates` (root CAs from Secrets
Manager), `recording` (session capture to S3), `enterprise_policies` (up to ten
Chromium managed-policy JSON files from S3), and `extensions`
(`browser_client.py:117`, `:320`).

**The framing pattern is built.** MCP Apps already solve "embed a hostile-ish
document at a separate origin that only the SPA may frame": a CloudFront
distribution over S3 serving a static shell, with a response
`Content-Security-Policy: frame-ancestors <SPA origin only>`
(`infrastructure/lib/constructs/mcp-sandbox/mcp-sandbox-distribution-construct.ts`).

**An interrupt that pauses a turn and resumes it** is shipped twice over —
`OAuthConsentHook` and `ask_user_question`. Both route through Strands'
`_stop_for_interrupts`, a `PausedTurnSnapshot`, a `PendingInterrupt`
breadcrumb, and the resume route in `inference_api/chat/routes.py`.

## What is missing or broken

1. **No takeover action.** `browse_web` exposes `live_view` only, documented as
   "a URL where the user can **watch**" (`browse_tool.py:148`).

2. **`live_view` is broken in the way PR #1101 already diagnosed.**
   `generate_live_view_url` signs with `SigV4QueryAuth`
   (`browser_client.py:597`) — the signature lives in the query string — and
   `browse_tool.py:288` returns that URL as text in the tool result. The model
   will re-emit it truncated at the `?`. Same failure as the `.docx` download
   link. **Max expiry is 300 seconds**, which is also far too short for a human
   to read a message, find their credentials, and log in.

3. **The session reaper will kill a takeover mid-login.**
   `IDLE_REAP_SECONDS = 600` and `SESSION_TIMEOUT_SECONDS = 900`
   (`session_pool.py:39`, `:43`). "Idle" is measured by agent tool calls, and
   during a takeover there are none by construction.

4. **Live View is DCV, and AWS ships only a React component.** Per the
   [Live View docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-dcv-integration.html),
   the stream is AWS DCV and the supported embed is `<BrowserLiveView>` from the
   `bedrock-agentcore` **TypeScript/React** SDK, which wraps the DCV Web Client.
   Our SPA is Angular. This is the single biggest design constraint here.

5. **Our browser is `networkMode: 'PUBLIC'`** (`browser-construct.ts:62`), and
   `executionRoleArn` on `CfnBrowserCustom` is create-only — the construct
   already carries scar tissue about replacement collisions.

6. **No accessibility tooling.** `evaluate` can run JS, but `MAX_EVAL_CHARS =
   4000` (`browse_tool.py:40`) truncates any real axe report, and enterprise
   apps with a strict CSP will block a CDN-injected axe-core outright.

## Decision summary

| # | Decision |
|---|----------|
| D1 | Takeover is a **separately grantable tool** (`request_user_login`), not an action on `browse_web` — RBAC granularity is per `tool_id`. **Kept as a rollout control, not a security boundary** — see D6 and Security 3 |
| D1b | Takeover is an **interrupt**, not a new endpoint — reuse `ask_user_question`'s machinery |
| D2 | The live-view URL is **never** model-visible; it is minted on demand by app-api |
| D3 | DCV is embedded via a **plain-JS viewer page at the sandbox origin**, framed by the SPA. No React and no npm: AWS's `BrowserLiveView` imports `dcv`/`dcv-ui`, which are undeclared and unavailable on npm. The SDK is fetched and signature-verified at build time, never committed — it is EULA-licensed and this repo is public |
| D4 | Browser session identity moves to the **DynamoDB session-metadata row** so app-api can act on it |
| D5 | Profiles are keyed `user + assessment target`, with an explicit user-facing "forget this login" |
| D6 | **One** browser, with a Chromium `URLBlocklist`. ⚠️ Session-level policies are **RECOMMENDED-only** (measured — MANAGED is rejected), so this constrains the agent but not a human in a takeover; the real control needs `CreateBrowser`. Supersedes the second-resource/`URLAllowlist` draft — the browser resource is immutable (no `UpdateBrowser`) and RBAC cannot express a per-site rule |
| D7 | axe-core ships as a **browser extension**, and the full report goes to the workspace, never to the model |
| D8 | Everything rides `BROWSER_TAKEOVER_ENABLED` (default on, kill switch) |
| D9 | A 40-target sweep is **40 sessions, not one turn** — the cost hazard is session shape, not takeover |

## Design

### D1 — Takeover is its own tool, not a `browse_web` action

**This is a correction to an earlier draft of this spec**, which made takeover an
action on `browse_web`. That is not grantable.

RBAC granularity in this codebase is exactly one `tool_id`. A role's
`grantedTools` holds catalog keys (`apis/shared/rbac/models.py:72`), those roll
up through parents in `_compute_effective_permissions`
(`apis/shared/rbac/admin_service.py:411`), and the result becomes
`enabled_tool_ids` for `ToolFilter`. There is no sub-tool granularity and no
per-action gate. An action on `browse_web` therefore ships to **everyone who can
browse** — which is the opposite of what is wanted: students should be able to
browse, and should not be able to take over a browser inside our AWS account.

So `request_user_login` and `accessibility_scan` are each **registered tools with
their own `TOOL_CATALOG` entries**, granted independently, exactly like
`ask_user_question`. A user without the grant never has the tool registered, so
it never reaches `toolConfig` and the model cannot offer it. A student who hits a
login wall gets a clean "I can't get past this sign-in" instead of a capability
they shouldn't have.

⚠️ Per the RBAC rule in CLAUDE.md, the admin UI must write **through** to each
role's `grantedTools` (`add_tool_to_role` / `set_roles_for_tool`) and then run
`sync_effective_permissions`. A tool list persisted on the resource alone grants
nothing, silently.

`live_view` is removed from `browse_web`'s action enum — it is superseded by the
interrupt flow and is broken today (see "What is missing or broken" #2).

Suggested default posture:

| Tool | Who |
|---|---|
| `browse_web` | broad, including students |
| `accessibility_scan` | broad — cheap and useful on any public page |
| `request_user_login` | restricted to staff/evaluator roles |

### D1b — Takeover is an interrupt

The tool:

1. calls `client.take_control()` — the agent's automation stream goes
   `DISABLED`, so the agent provably cannot act while the human is driving;
2. pins the session against the reaper (D4);
3. raises an interrupt via `ToolContext.interrupt`, exactly as
   `ask_user_question` does, with `kind: "browser_login"` and a payload of
   `{sessionId, browserId, viewport: {width, height}, targetUrl}` — **no URL**;
4. on resume, calls `client.release_control()` and continues the turn.

Why an interrupt rather than a new endpoint: the turn genuinely must stop. The
agent has nothing to do until a human finishes, and a turn that polls for login
completion burns model calls to learn nothing. The pause/snapshot/resume path is
built, tested, and already survives the container-reassignment problem.

A new SSE event `browser_login_required` is emitted after `message_stop`, in the
same `done` block as `oauth_required` and `user_question_required`. Payload:
`{type, interruptId, toolUseId, sessionId, browserId, viewport, targetUrl}`.

Resume contract mirrors `user_question_required`: the SPA POSTs an
`interrupt_responses` entry whose `response` is **always an object, never null**
— `{completed: true}` or `{skipped: true}`. `ToolContext.interrupt` only treats
a non-None response as an answer; a null re-raises the interrupt forever.

⚠️ **Abandonment is the known hazard here.** A user who opens the live view and
walks away leaves both a paused turn (which bricks later turns —
see the stale-interrupt failure mode) and a browser session billing until TTL.
Mitigation: the interrupt carries a deadline; when it lapses, the tool releases
control, stops the session, and returns an ordinary error result so the turn
completes normally rather than staying pinned.

### D2 — The live-view URL is never model-visible

**Rule, inherited from PR #1101: no presigned URL in a tool result, ever.**

The 300-second cap makes this not merely a style rule but a correctness one — a
URL minted at tool time is dead before a human reacts, and dead again on every
reload of the thread.

New app-api route:

```
POST /sessions/{session_id}/browser/live-view
  → { url, expiresAt, viewport: { width, height } }
```

`Depends(get_current_user_from_session)`, owner-scoped to the conversation. The
SPA sends **only** the conversation id; app-api looks the browser session up
server-side (D4) and mints a fresh URL per call. The viewer page re-requests on
expiry, so a login that takes twenty minutes works fine.

Per the inference-api boundary rule this route belongs on app-api, not
inference-api — the Runtime data plane proxies only `/invocations` and `/ping`,
so a route added there would 404 in cloud.

app-api's task role needs `ConnectBrowserLiveViewStream`, `UpdateBrowserStream`
and `GetBrowserSession` on the browser ARN. (Built in PR 2, deliberately
narrower than the Runtime's own grant — no Start/Stop and no
`ConnectBrowserAutomationStream`, because app-api must never drive the
browser.)

### D3 — A plain-JS viewer at the sandbox origin, SDK fetched at build time

**This supersedes an earlier draft** that reached for AWS's React
`<BrowserLiveView>` component from the `bedrock-agentcore` npm package. That
package is official AWS and Apache-2.0, but the component **cannot be built
from public npm**:

```
import dcv from 'dcv';
import { DCVViewer } from 'dcv-ui';
```

Neither is in its `dependencies`, `peerDependencies` or
`optionalDependencies`. `dcv-ui` does not exist on the registry at all, and the
only `dcv` package there is an unrelated third-party Vue component library. The
component silently assumes you have already vendored the Amazon DCV Web Client
SDK and made it resolvable under those names. So the React path is a strict
*superset* of the vendoring work, not an alternative to it — and it would pull
React plus ~189 packages into a surface where a user types a password.

**So: no React, no npm, no third-party code.** The viewer is
`infrastructure/assets/mcp-sandbox/live-view.{html,js}` — plain ES2017 against
the official Amazon DCV Web Client SDK, which is framework-agnostic.

**The SDK is fetched at build time, never committed.** It is a EULA-licensed
AWS download and **this repository is public**, so vendoring it would be
redistribution. `scripts/build/fetch-dcv-sdk.sh` downloads it, verifies it, and
extracts it to a gitignored directory; `platform.yml` runs it before the CDK
deploy. This is also AWS's own instruction — "place the extracted directory on
your web server" — and it means the signature is checked on every build rather
than trusted once at commit time.

What makes that safe is **not** "it comes from AWS". Three pins do: the exact
version, the archive SHA256, and the signing-key fingerprint — the last two
held in the script rather than read from the network, because whoever could
swap the artifact could swap the published checksum and serve a different key.
Importing whatever key the URL offers today and trusting it is
trust-on-first-use on every build. ⚠️ The fingerprint was captured once and
pinned at review time; AWS does not appear to publish it out-of-band.

**Why a separate origin rather than an Angular component in the SPA.** The
original reason — "AWS ships only React" — is gone. Two reasons remain, and the
first is the real one:

- This is the one surface where a user types a password, and `dcv.js` forwards
  their keystrokes. Keeping it in an origin that holds **no session cookie**
  means a compromised SDK cannot also reach their session.
- The SDK must be *served* with its folder structure and license files intact,
  which suits a static origin and fights a bundler.

Note the isolation argument that justifies this origin for MCP Apps does **not**
transfer: DCV streams pixels and forwards input, so the remote page's code runs
in AWS's Chromium, never in our DOM. This is defence in depth, chosen
deliberately rather than inherited.

**The SPA mints; the viewer never calls app-api.** app-api's CORS is a
credentialed allowlist, so letting the viewer call it would mean allowlisting an
origin that also serves untrusted MCP App HTML. Instead the SPA mints the URL
and `postMessage`s it in, targeted at the sandbox origin explicitly — never
`'*'`, which would post a live credential to whatever happened to be framed. The
viewer accepts messages only from the origin that framed it, and stores nothing.

**CSP.** The sandbox origin's CloudFront function already composes `connect-src`
from a `?csp=` query parameter, and is attached to the **default** behaviour, so
it covers any path there. The SPA mints first and then names the minted URL's
own origin in that parameter, so the grant is exactly the endpoint in use rather
than a wildcard. This resolves risk 3 below.

`remoteWidth`/`remoteHeight` come from the event's `viewport` and are applied
via `requestDisplayLayout`; a mismatch crops or letterboxes the stream, which is
why the viewport is carried rather than re-declared in the frontend.

### D3a — Sizing the handover: 1:1 in the docked panel

The agent browses at 1280x800, but the user signs in through a docked rail
about 800px wide, so the unfitted stream drew the login page at roughly half
size. Measured on dev-ai, 2026-10-17:

* DCV's `requestDisplayLayout` resizes the remote X screen at any time, takeover
  included. It refuses widths under 640 and snaps both sides down to multiples
  of 8. The size persists across reconnects.
* Chrome's window does **not** follow the screen. There is no window manager,
  so maximize is ignored, and fullscreen and F11 do not re-fit it. Only CDP
  `Browser.setWindowBounds` does, and the page reflows.
* CDP is refused (HTTP 403) while the automation stream is DISABLED.
* The agent's CDP screenshots follow the window, not the screen.

So the size is picked **before** the handover and undone **after** it:

1. `session_pool.take_control` sets the window to `TAKEOVER_VIEWPORT`
   (768x832, snapped to what the display accepts) while CDP still answers, then
   disables the stream. The event's `viewport` is that size, so `live-view.js`
   pins the screen to it at `firstFrame`. The agent's own size stays on the
   session as `viewport`; the handover size is held apart as
   `takeover_viewport`. 768 is wide enough that most sites still serve their
   tablet or desktop layout.
2. `release_control` re-enables the stream, reconnects to the same browser
   and sets the window back to the agent's size. This is best-effort and
   logged; a failure never fails the resume.
3. The SPA widens the rail to the viewport plus its padding when the window
   leaves 360px for the chat, and gives the user's width back on close. The
   frame is never drawn wider than the remote viewport: a wider rail centres
   it, a narrower one scales it down. **Expand** shows the same stream over
   the whole window by restyling the pane in place, because moving the iframe
   would reload it and drop the stream.

Live re-fitting while the user drags the rail is deliberately not done. It
would need CDP during the takeover, which means re-enabling the agent's stream,
and that is the one property the takeover exists to guarantee.

`BROWSER_TAKEOVER_FIT_ENABLED=false` skips steps 1 and 2 (a rollout switch;
see `feature_flags.browser_takeover_fit_enabled`). The SPA needs no switch: it
reads whatever `viewport` the event carries, so with the switch off it still
widens the rail, now toward 1280, and scales down.

Not done yet: the handover height is a fixed 832. A user whose panel is
shorter sees the frame scaled down a little. Sizing it from the panel would
mean the SPA sending its rail height on the chat request and the route
threading it to the tool, which is a turn-path change of its own.

### D4 — Browser session identity in the session-metadata row

Today the browser session id lives in `agent.state`, which the Strands session
manager restores from AgentCore Memory — app-api cannot read it, and app-api is
where the live-view route must live (D2).

Write `{browserSessionId, browserId, viewport, controlState, expiresAt}` onto
the conversation's session-metadata row in DynamoDB, which app-api already owns
and scopes by owner. `agent.state` stays the agent-side source for the in-loop
path; the metadata row is the projection app-api reads. Per the
"one session, more than one agent" rule, neither may be cached on an agent
instance — the row is re-read per turn.

This row is also what lets the reaper know a takeover is in progress: while
`controlState == "user"`, `IDLE_REAP_SECONDS` does not apply.

### D5 — Profiles keyed by user and target

`profileIdentifier` = a stable hash of `(userId, assessmentTargetId)`. A
"target" is a named vendor/product in the assessment config, not a bare
hostname, so a vendor whose login and app live on different domains is one
profile.

The profile is what makes the annual sweep viable: log in once per vendor, and
next year's run resumes authenticated.

It also means **a durable credential-equivalent now exists per user per vendor**,
which needs a lifecycle:

- a Customize-surface list of saved logins with a per-row "forget this login";
- deletion on user deactivation;
- an expiry, so an abandoned profile does not hold a session cookie forever.

Profiles are per-user by default. A shared institutional account (one Library
subscription, several evaluators) is a genuinely different model with a
different blast radius and is **out of scope** for v1.

### D6 — One browser, a MANAGED URL blocklist applied per session

**This supersedes an earlier draft** which added a second `CfnBrowserCustom`
carrying a `URLAllowlist` of the targets under evaluation. Three findings
killed that design; the replacement is simpler and is the control that
actually implements the requirement.

**The requirement, stated from the outcome.** Faculty and staff run
accessibility and VPAT checks behind authenticated pages. Nobody — including a
grad assistant who legitimately holds a staff role — uses the same capability
to have an agent submit their own coursework. That is a constraint on **which
sites**, not on **which people**, and RBAC cannot express it: effective
permissions merge as a **union** across every role a user matches
(`rbac/service.py`, "Tools: Union"), priority decides only the quota tier, and
there is no deny. A user in both the staff and the current-term student Entra
groups gets the union of both roles' tools. At a university that population is
large and permanent.

**An application-level URL check cannot implement it either.** Takeover hands
the human a fully interactive Chromium. A check inside `request_user_login`
sees only the *starting* page; once the user is driving they can navigate
anywhere. The only thing that constrains them is Chromium itself refusing,
which means an enterprise policy.

**Blocklist, not allowlist — because the browser resource is immutable.**
There is no `UpdateBrowser` operation (the control plane has only
`CreateBrowser`, `GetBrowser`, `DeleteBrowser`, `ListBrowsers`), and per the
[enterprise-policy docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-enterprise-policies.html)
"policy files are read from Amazon S3 at the time of the API call. Changes to
policy files in Amazon S3 after calling `CreateBrowser` or
`StartBrowserSession` are not reflected." So a policy attached at
`CreateBrowser` is frozen for the life of the resource, and every edit means
replacing it. An allowlist of vendors under evaluation would therefore require
a browser replacement per VPAT review — untenable. A **blocklist of
institutional systems** (the LMS, the SIS, HR, email) is small, changes about
yearly, and fits an immutable resource. It also matches the requirement's
actual shape: everything is open *except* where an agent acting as the user has
academic or administrative consequences.

**⚠️ MEASURED 2026-09-19, AND THIS IS WHERE THE DESIGN BROKE.** The plan was
to apply the policy at `StartBrowserSession` as MANAGED, because its `type`
enum is `['MANAGED', 'RECOMMENDED']`. **The enum lies.** The service returns:

```
ValidationException ... Invalid value for parameter 'type'.
MANAGED is not supported for session-level policies.
```

And it does not degrade — `StartBrowserSession` fails outright, so **every**
browser session dies and `browse_web` stops working entirely. That regression
reached dev (never prod) and was reverted to `RECOMMENDED`.

So risk 2b resolved **against** the design. Session-level policies are
RECOMMENDED-only, which Chromium treats as a user-overridable default. What
ships today therefore constrains the **agent** — which drives via CDP and never
opens settings — but **not a human holding the browser during a takeover**,
who is the threat this control exists for.

**Consequence: `request_user_login` must stay ungranted** until the MANAGED
policy is applied at `CreateBrowser`. That needs a custom resource, since
`CfnBrowserCustom` does not expose `enterprisePolicies`, and it reinstates
replace-per-edit on the browser resource — survivable only because profiles
turned out to be account-level (risk 2). The RBAC grant and
`BROWSER_TAKEOVER_ENABLED` hold the line meanwhile.

The original reasoning for preferring session level, now moot:
`StartBrowserSession` takes `enterprisePolicies`, and the docs' prose describes
MANAGED as a `CreateBrowser` thing. This matters because the two levels are not
interchangeable: MANAGED is "required and mandated by an administrator… cannot
be overridden" and lands in `/etc/chromium/policies/managed/`; RECOMMENDED is
user-overridable and lower-precedence, which makes it advisory rather than a
boundary against the person driving the browser.

Applying it per session buys three things a `CreateBrowser` policy cannot: the
list is read fresh from S3 on every session, it needs no `CfnBrowserCustom`
change, and it works **today** — `CfnBrowserCustom` in our pinned aws-cdk-lib
(2.251.0) does not expose `enterprisePolicies` at all (its props are
`browserSigning`, `description`, `executionRoleArn`, `name`,
`networkConfiguration`, `recordingConfig`, `tags`), so the CFN route would need
a custom resource *and* would inherit the replacement-per-edit problem.

⚠️ **Verify before granting the tool to anyone:** that MANAGED is honoured at
`StartBrowserSession` and not silently downgraded. An enum accepting a value is
not proof the service enforces it. The test is one browser session on dev:
apply the policy, navigate to a blocked host, confirm Chromium refuses. Until
that passes, the blocklist is unproven and the RBAC grant is the only control.

**Consequences:**

- **No second browser resource.** One `URLBlocklist` covers `browse_web` and
  `request_user_login` alike. The LMS already has a first-class API
  integration in this product, so the browser reaching it was the unsanctioned
  path regardless.
- **VPC `networkMode`, `certificates` and `recording` are decoupled** from this
  and can be revisited on their own merits, for campus-only targets.
- `session_pool._start_remote_session` is the single place a session is
  started, so "the policy is always applied" is one assertion in one place.

**The list.** Seeded with the LMS and extended after the CISO review. Match on
the origin actually navigated to, not a vanity hostname — a CNAME that
redirects to the real origin is not what Chromium's `URLBlocklist` sees:

```json
{
  "URLBlocklist": [
    "boisestatecanvas.instructure.com"
  ]
}
```

It lives in CDK config and deploys as the S3 object, rather than being editable
in place. A security control that can be changed out-of-band without review is
worse than one that needs a deploy, and this list changes about yearly.

**Residual risk, stated plainly.** A blocklist is only as good as its entries.
It stops the named threat and whatever else is enumerated; it does not stop a
takeover against some authenticated system nobody listed. Session recording
(Security 2) is the detective complement.

### D7 — axe-core as an extension; report to the workspace

Two failures if axe is injected via `evaluate` from a CDN: enterprise apps with
a strict CSP block the injected script — i.e. it fails precisely on the serious
vendor products — and a full axe report vastly exceeds `MAX_EVAL_CHARS = 4000`.

So: axe-core ships as an S3-hosted browser extension (`start(extensions=[...])`),
which runs outside page CSP. New action `accessibility_scan`:

- returns to the model **only** a bounded summary — violation ids, impact,
  node counts, and the page URL;
- writes the **full JSON** to the user's workspace via the existing
  workspace-write path, and surfaces it as a download card.

This is the cost tenet applied directly: a forty-database sweep that pipes raw
axe output through the prompt is exactly the unbounded per-turn payload the rule
exists to catch.

### D8 — Feature flag

`BROWSER_TAKEOVER_ENABLED`, default on with a kill switch, following the
house pattern (an empty-string var means on). While off, `request_user_login`
and `accessibility_scan` are never registered — an unregistered tool never
reaches `toolConfig`, so the model cannot pause a turn behind a prompt the
client has no renderer for.

## Cost analysis

All figures measured against the real serialized tool specs, not estimated.

**Prefix (`toolConfig`), per user who is granted the tool:**

| Tool | Tokens |
|---|---|
| `browse_web` (today) | 493 |
| `request_user_login` (new) | ~206 |
| `accessibility_scan` (new) | ~191 |

Because D1 makes these separate tools, **a user who is not granted them pays
zero** — the prefix is built from that user's filtered tool list. Removing
`live_view` from `browse_web`'s enum claws a little back. So the marginal prefix
cost for a student is negative, and for an evaluator is ~400 tokens, paid once
per cache write.

**Per turn.** A complete human login round trip costs about **one short tool
result** — on the order of 30–40 tokens. Everything expensive about it travels
outside the model's context by construction: the live-view URL is never
model-visible (D2), the DCV stream is a video stream the model never sees, the
user's keystrokes and the login page itself never enter the prompt, and the full
axe report goes to the workspace (D7). `accessibility_scan`'s summary is capped
at `max_violations` (default 25), so roughly 350 tokens worst case.

Takeover is, in prompt terms, one of the cheapest features in this codebase.

**The real spend is elsewhere, and it is worth being blunt about it.** For these
two agents the cost driver is ordinary browsing, not takeover:

- `screenshot` produces vision tokens — the single most expensive output the
  browser tool can generate. An accessibility agent is precisely the agent most
  tempted to screenshot every page. The tool docstring already discourages it;
  the assessment *skill* should forbid it except where layout is genuinely the
  question.
- `extract_text` is capped at `MAX_TEXT_CHARS = 8000` ≈ 2k tokens **per call**,
  and every one of those accumulates in conversation history for the rest of the
  session.

**Browser session wall-clock** is the non-token cost, and takeover lengthens
sessions by design — a human login is minutes. Two controls: the abandonment
deadline (D1b), and profiles (D5) making takeover once-per-vendor rather than
once-per-conversation. The second matters more, and is why D5 is not deferred.

### D9 — A sweep is many sessions, not one turn

The naive shape of "evaluate all 40 databases we renew" is one enormous
conversation. At ~2k tokens of page text per read and ten pages per target,
that is several hundred thousand tokens of transcript accumulating in a single
session — which then triggers compaction, which is its own cost event, and does
it repeatedly.

The assessment workflow must therefore be **one session per target**, with
results written to the workspace and aggregated at the end. This is a skill and
UX decision more than a code one, but it belongs in this spec because it is
where the actual money is, and because getting it wrong would make the feature
look expensive when the feature itself is nearly free.

## Security

**An interactive browser inside our AWS account is the headline risk.** During
takeover the user has a fully interactive Chromium with our egress. Controls:

1. **A MANAGED `URLBlocklist` Chromium enterprise policy**, applied on every
   `StartBrowserSession` (D6). This is the primary control, and it is the only
   one on this list that constrains **where the human can go** once they hold
   the browser. MANAGED specifically: RECOMMENDED is user-overridable and so is
   not a boundary against the person driving.
2. **Session recording to S3** — every takeover is recorded. This must be
   disclosed to users in the UI before control is handed over; silent recording
   of a session in which someone types a password is not acceptable.
3. **RBAC is a rollout control, not a security boundary.** It was drafted as
   one, and it cannot be: effective permissions are a **union** across matched
   roles (`rbac/service.py`) with no deny, so a grad assistant holding both a
   staff and a student role receives the union. Granting `request_user_login`
   separately from `browse_web` is still worth keeping — it lets the capability
   be piloted with the evaluator group before it is widened — but the thing
   that stops coursework submission is control 1, not this.
4. **`frame-ancestors`** on the viewer origin, so only the SPA can frame it.
5. **The agent provably cannot act while the human holds control** — the
   automation stream is `DISABLED` at the service, not by convention in our code.

**Credentials never enter the conversation.** The whole point of D1 is that the
user types their password into a real browser with their own hands. Nothing in
this design should ever accept a credential as a tool argument.

**Recording captures the login.** A recorded session includes the user typing
into a password field. DCV will mask nothing. The S3 bucket needs the same
treatment as any credential store — encryption, tight access policy, a
retention window — and users must be told before they take control.

**Authorization to test is a real precondition, not a formality.** Automated
scanning of a vendor's non-public demo instance may breach that vendor's terms.
For a Library procurement workflow this is fixable at the source — ask for
scanning rights in the renewal terms, or request an evaluation tenant. Worth
having in writing before forty scans run, not after.

## Build notes (PR 1)

Two deliberate deviations from the sketch above, both recorded here so PR 2
does not have to rediscover them:

1. **`sessionId` on the event is the conversation, not the browser session.**
   D1b wrote a single ambiguous `sessionId`. Every other SSE event in this
   codebase uses `sessionId` for the conversation, and D2's route is owner-
   scoped by conversation, so the event carries both: `sessionId` (conversation)
   and `browserSessionId`. Collapsing them would have collided the moment
   app-api needed to check ownership.

2. **The conversation id and the D4 projection are stamped on in the streaming
   layer, not the tool.** The tool knows only which browser session it handed
   over; `stream_coordinator` is the layer that holds `session_id` / `user_id`.
   Keeping identity out of the tool also keeps it a *static* registry tool
   rather than an `extra_tools` injection, so it does not touch the injected-
   tool agent-cache bypass.

Three things worth knowing before building PR 2:

* **Strands re-executes an interrupted tool from the top on resume**
  (`strands/types/interrupt.py`), so `request_user_login` runs twice per
  takeover. `take_control` is idempotent and a marker on `agent.state` carries
  the descriptor across the pause; without the marker a resume that lands in a
  container which lost the session would call `acquire` and silently start a
  *second, unauthenticated* browser, then report success.
* **The abandonment deadline is enforced by the reaper, not by a timer.**
  Nothing runs while a turn is paused. `_reap_idle` exempts a user-controlled
  session only until `deadlineAt`, and it runs on any conversation's next
  browser call in the same container, with the remote TTL as the backstop.
  `BROWSER_TAKEOVER_DEADLINE_SECONDS` defaults to 480, with a 60s grace on the
  resume side so a user who finished at 7:59 whose POST lands at 8:01 is not
  told their sign-in expired.
* **`live_view` is gone from `browse_web`.** It returned a presigned URL as
  tool-result text — the exact failure PR #1101 diagnosed — and its 300-second
  cap made it useless for a human anyway.

⚠️ Still unverified, and still blocking PR 3: whether `profileConfiguration`
survives a browser *resource* replacement (risk 2 below).

## Build notes (PR 2)

The route, its IAM and the SPA's handling of `browser_login_required` are
built. The **viewer page is not**, and two findings from building the rest
change its design — which is why it was left open rather than stubbed.

### D3 correction — the viewer must NOT call app-api itself

D3 says "the page calls the D2 route for its own URL and refreshes it on
expiry." It must not. app-api's CORS is an allowlist with
`allow_credentials=True`, so for the viewer to call the route from the sandbox
origin, **that origin would have to be added to `CORS_ORIGINS`** — and the
mcp-sandbox origin is where we frame *untrusted MCP App HTML*. Granting it
credentialed access to app-api would hand every App a same-origin-ish path to
the user's session.

The viewer should instead receive its URL by `postMessage` from the SPA, which
already holds the cookie and is already the trusted origin:

- the SPA calls `POST /sessions/{id}/browser/live-view` (built, and the SPA
  client for it is in `BrowserLoginService.mintLiveView`),
- posts `{url, viewport}` into the frame,
- re-mints before `expiresAt` and posts again.

The viewer then needs no credentials, no CORS entry, and no knowledge of
app-api at all. This is strictly less surface than D3 as drafted.

### D3 correction — no new distribution, no new certificate

D3 implies a viewer origin built like the mcp-sandbox one. A *second*
CloudFront distribution means a second subdomain, a second us-east-1 ACM cert
and a second `CDK_*_CERTIFICATE_ARN` deploy var — and per the scar tissue in
`mcp-sandbox-distribution-construct.ts` (#396) a configured domain with a
missing cert **throws at synth**, so adding one ahead of the cert would break
every deploy until ops caught up.

The mcp-sandbox origin already is what D3 describes: a static, non-SPA origin
with `frame-ancestors` locked to the SPA. The viewer should be one more static
file served from it. Note its `BucketDeployment` is `prune: true` with no key
prefix, so the page belongs in `infrastructure/assets/mcp-sandbox/` — a second
prefixed deployment to the same bucket would be pruned by the first.

### What PR 2 ships

`POST /sessions/{session_id}/browser/live-view` on app-api (404 for a
conversation that is not the caller's or has no browser session, 409 for one
whose session has ended, 404 while the flag is off), a **narrower** IAM
statement than the Runtime's — `ConnectBrowserLiveViewStream`,
`UpdateBrowserStream`, `GetBrowserSession` only, no Start/Stop and no
`ConnectBrowserAutomationStream`, because app-api must never drive the browser
— plus the SPA's validator, `BrowserLoginService`, and the resume path.

The SPA validator rejects any event carrying a `url`, mirroring the backend's
`assert_no_url`: nothing upstream should ever put one there, so its presence
means a contract regression shipped and refusing to render beats framing a URL
of unknown provenance.

⚠️ Still unverified, and now the *first* thing the viewer PR must answer: does
DCV work inside a cross-origin iframe under this CSP? It opens a WebSocket to
the AgentCore data plane, which the sandbox origin's `connect-src` does not
currently allow (risk 3 below).

## PR breakdown

| PR | Scope | Notes |
|----|-------|-------|
| **1** | Backend takeover: `request_user_login`, take/release control, interrupt + `browser_login_required` SSE event, D4 metadata row, reaper pinning, abandonment deadline, flag | Testable end to end with a curl-minted live-view URL — no frontend needed |
| **2** | app-api live-view route + IAM; SPA `browser_login_required` handling and resume | Shipped WITHOUT the viewer — see "Build notes (PR 2)" |
| **2b** | The viewer itself: sign-in prompt component, plain-JS `live-view.{html,js}`, the DCV SDK fetch step, `sandboxOrigin` on the event | The PR that makes the feature usable at all. No npm dependency (D3) |
| **3** | **MANAGED `URLBlocklist` applied per session (D6)** + the policy object and its IAM | Re-sequenced ahead of profiles. This is the control that lets the tool be granted at all, so it gates rollout rather than following it |
| **4** | Profiles (D5) + the Customize-surface "saved logins" list with forget/expiry | The PR that makes the annual sweep actually repeatable |
| **5** | axe extension and `accessibility_scan` (D7) | Turns the output into VPAT-grade evidence |

PR 1 alone unblocks a developer-driven demo. PRs 1+2 unblock the Library user
for anything login-gated and publicly routable, which is most of the renewal
list. **PR 3 is the gate on granting the tool to real users** — until the
blocklist is in place and verified, RBAC is the only control, and Security 3
explains why that is not enough on its own. PR 4 is what makes the sweep worth
doing annually.

Profiles were re-sequenced *after* the blocklist deliberately: a persisted
login is standing authenticated access with no takeover in the path, so
shipping it first would remove the one control the user actually passes
through.

## Testing

- **Unit:** interrupt raise/resume round trip; null-response guard (the
  re-raise-forever trap); abandonment deadline releases control and stops the
  session; reaper does not reap while `controlState == "user"`; `evaluate` and
  `accessibility_scan` truncation budgets.
- **Contract:** `browser_login_required` in `stream-parser-core`; no URL-shaped
  string anywhere in the tool result (assert it, as PR #1101 did).
- **Integration (dev):** real takeover against a known login-gated target —
  confirm the automation stream is `DISABLED` while the human drives, that the
  agent resumes authenticated, and that a second conversation with the same
  profile starts already logged in.
- **Manual:** viewport match (no crop/letterbox), URL refresh across the
  300-second boundary, light and dark, and phone width.

## Risks and open questions

1. ~~**Is `networkConfiguration` create-only on `CfnBrowserCustom`?**~~
   **Moot, and worse than assumed.** There is no `UpdateBrowser` operation at
   all, so *every* property is effectively create-only and any change replaces
   the resource. This is what moved the URL policy off the browser resource and
   onto `StartBrowserSession` (D6).
2. ~~**Does `profileConfiguration` survive a browser *resource* replacement?**~~
   **Answered: yes.** `CreateBrowserProfile` takes `name`, `description`,
   `clientToken` and `tags` — and **no `browserId`**. Profiles are account-level
   resources, not scoped to a browser, so replacing the browser resource does
   not invalidate saved logins. PR 3 and the policy work can land in either
   order.
2b. ~~**Is `type: MANAGED` honoured at `StartBrowserSession`?**~~
   **ANSWERED, AGAINST US (dev, 2026-09-19).** It is rejected:
   `Invalid value for parameter 'type'. MANAGED is not supported for
   session-level policies.` Not a silent downgrade — `StartBrowserSession`
   fails and every session dies. See D6. **Do not grant
   `request_user_login` until the MANAGED policy is applied at
   `CreateBrowser`.**
3. ~~**Does the DCV viewer work inside a cross-origin iframe with our CSP?**~~
   **Resolved in principle.** The sandbox origin's CloudFront function composes
   `connect-src` from a `?csp=` query parameter and is attached to the
   *default* behaviour, so it covers `live-view.html` too. The SPA names the
   minted URL's own origin there. Still to confirm against a live session:
   that DCV needs nothing beyond `connect-src` (its workers and decoders are
   same-origin, and `worker-src 'self' blob:` is already granted).
4. **Mobile.** DCV interaction on a phone, for a login form in a 768x832
   remote viewport (D3a), is likely poor. May need an explicit "open in a new tab"
   escape hatch rather than pretending the frame works everywhere.
5. **Concurrency.** Two evaluators sharing a Library account, both taking
   control of different sessions against the same vendor, may collide on a
   single-session-per-login vendor. Out of scope for v1, but it will come up.

## Out of scope

- Shared/institutional profiles (D5 is per-user).
- Any credential-as-tool-argument path, including Secrets Manager injection. If
  a shared account genuinely needs automation later, that is its own spec with
  its own security review.
- Replaying recordings in the SPA. Recordings land in S3 and are read out of
  band for v1.
- Remediation advice quality — this spec gets the agent *to* the page. What it
  concludes about WCAG conformance is a prompt and skill problem, not a tooling
  one.

# Authenticated web assessment (browser takeover, profiles, axe)

**Status:** PROPOSED — no code written. Sized for four PRs.
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
| D1 | Takeover is a **separately grantable tool** (`request_user_login`), not an action on `browse_web` — RBAC granularity is per `tool_id` |
| D1b | Takeover is an **interrupt**, not a new endpoint — reuse `ask_user_question`'s machinery |
| D2 | The live-view URL is **never** model-visible; it is minted on demand by app-api |
| D3 | DCV is embedded via a **static viewer page at the sandbox origin**, framed by the SPA — not a React island in the Angular app |
| D4 | Browser session identity moves to the **DynamoDB session-metadata row** so app-api can act on it |
| D5 | Profiles are keyed `user + assessment target`, with an explicit user-facing "forget this login" |
| D6 | A **second** `CfnBrowserCustom` for assessment; never mutate the fleet's browser |
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
and `GetBrowserSession` on the assessment browser ARN.

### D3 — DCV embeds via a viewer page at the sandbox origin

AWS ships React; we are Angular. Three options were considered:

| Option | Verdict |
|---|---|
| Drive the DCV Web Client SDK directly from Angular | Reimplements connection setup, SigV4 handling and frame rendering; carries the maintenance forever |
| Mount a React island inside the Angular SPA | Adds React to the SPA bundle for one component; poor trade |
| **Static viewer page at a separate origin, framed by the SPA** | **Chosen** |

The third is what MCP Apps already do. A `live-view.html` asset built against
`bedrock-agentcore`'s `BrowserLiveView`, deployed to the same CloudFront/S3
origin pattern with `frame-ancestors <SPA origin only>`. React stays entirely
outside the SPA bundle. The SPA frames it and passes the conversation id; the
page calls the D2 route for its own URL and refreshes it on expiry.

`remoteWidth`/`remoteHeight` **must** match the session viewport or the stream
crops. `DEFAULT_VIEWPORT` is currently `1280x800` (`session_pool.py:45`); the
viewport must be carried on the event rather than duplicated as a constant in
the frontend, so the two cannot drift.

⚠️ `bedrock-agentcore` (npm) is a **new dependency** and needs explicit
approval before anyone installs it. Exact-pin, no `^`.

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

### D6 — A second browser resource

Add `browser-assessment` (`CfnBrowserCustom`) alongside the existing one:

- `networkMode: 'VPC'` with subnets/SGs, for campus-reachable targets;
- `recording: { enabled: true, s3Location: ... }` — for a VPAT, evidence that a
  test ran against a specific build is often worth more than the findings;
- `enterprise_policies` carrying a Chromium `URLAllowlist` restricted to the
  targets under evaluation (see Security);
- `certificates` for internal CAs.

Never mutate the existing browser. `executionRoleArn` is create-only and the
construct documents the replacement collision; assume `networkConfiguration` is
create-only too rather than discover it during a deploy. `BROWSER_ID` already
selects the browser per environment (`session_pool.py:64`), so routing
assessment sessions to the second resource is a config change.

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

1. **`URLAllowlist` Chromium enterprise policy** on the assessment browser,
   scoped to the targets under evaluation. This is the primary control.
2. **Session recording to S3** — every takeover is recorded. This must be
   disclosed to users in the UI before control is handed over; silent recording
   of a session in which someone types a password is not acceptable.
3. **Takeover is gated by the same RBAC as `browse_web`**, and the assessment
   browser is a separate resource so it can be granted separately.
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

## PR breakdown

| PR | Scope | Notes |
|----|-------|-------|
| **1** | Backend takeover: `request_user_login`, take/release control, interrupt + `browser_login_required` SSE event, D4 metadata row, reaper pinning, abandonment deadline, flag | Testable end to end with a curl-minted live-view URL — no frontend needed |
| **2** | app-api live-view route + IAM; `live-view.html` viewer at the sandbox origin; SPA framing and resume | Needs the `bedrock-agentcore` npm dependency approved first |
| **3** | Profiles (D5) + the Customize-surface "saved logins" list with forget/expiry | The PR that makes the annual sweep actually repeatable |
| **4** | `browser-assessment` CDK resource (D6) + axe extension and `accessibility_scan` (D7) | D6 needs campus network decisions from whoever owns routing |

PR 1 alone unblocks a developer-driven demo. PRs 1+2 unblock the Library user
for anything login-gated and publicly routable, which is most of the renewal
list. PR 3 is what makes it worth doing annually. PR 4 covers campus-only
targets and turns the output into VPAT-grade evidence.

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

1. **Is `networkConfiguration` create-only on `CfnBrowserCustom`?** Assumed yes.
   Confirm before planning PR 4; if it is not, D6 could collapse to a config
   change on the existing resource — though a separate resource is still the
   better design for blast radius.
2. **Does `profileConfiguration` survive a browser *resource* replacement?**
   If profiles are scoped to the browser id rather than the account, a PR-4
   deploy could invalidate every saved login. Verify before shipping PR 3.
3. **Does the DCV viewer work inside a cross-origin iframe with our CSP?**
   MCP Apps proved the pattern for `srcdoc` content; DCV opens a WebSocket and
   may need `connect-src` allowances the current policy does not grant.
4. **Mobile.** DCV interaction on a phone, for a login form in a 1280x800
   remote viewport, is likely poor. May need an explicit "open in a new tab"
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

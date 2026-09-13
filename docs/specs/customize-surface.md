# Customize — a browse surface for tools, skills and connectors

**Status:** Step 1 (Customize shell: Tools + Skills) SHIPPED — PR #1072, validated on dev.
Step 3 (drop model + params from the drawer) SHIPPED — PR #1073, validated on dev.
Step 4 (agent-lock surfacing) SHIPPED — PR #1075, validated on dev.
Step 2 (Connectors tab) SHIPPED — PR #1076, validated on dev.
Step 5 (drawer deleted) in flight. Steps 6–7 queued.
**Supersedes:** the composer settings drawer (`components/model-settings/`) as the home for
tool and skill enablement.
**Related:** `docs/specs/skills-as-agent-primitive.md` (D6 opt-in), `docs/specs/agent-marketplace.md` (D1 one noun),
`docs/specs/per-tool-mcp-enablement.md` equivalent in `tool-search-token-bloat-strategy.md`.

## Problem

Tool and skill enablement is **global, durable, per-user state**:

- `services/skill/skill.service.ts:32` — "preferences persist globally per user"
- `services/tool/tool.service.ts:414` — `savePreferences()` POSTs to the user preferences endpoint

But it is presented in a drawer hanging off the composer, opened by a settings icon
inside the chat input. That container reads as *settings for this conversation*. It is not.
A user who enables a tool to get through one question has changed the `toolConfig` of every
future turn, in every future session, permanently — and nothing in the UI said so.

That is the defect. The secondary problem is capacity: the drawer is a ~320px column with
collapsible sections, and the tool catalog has outgrown it. Per-tool MCP enablement means a
single server (Student MyBoiseState, 17+ tools; canvas_faculty, 44) can exceed the entire
drawer's comfortable length on its own. Browsing is not a thing the drawer can be made to do.

## What Customize is

A full page at `/customize` that owns the things a user *adds to* their assistant:

| Tab | Contents | Today's home |
|-----|----------|--------------|
| **Tools** | The RBAC-granted tool catalog, per-tool and per-server enablement | Drawer § Tools |
| **Skills** | Accessible skills (catalog-granted ∪ authored), opt-in toggles | Drawer § Skills |
| **Connectors** | OAuth connection state for external MCP servers | `Settings → Connectors` (folded in, step 2) |

It is deliberately **capabilities only**. See §"What Customize is not".

## Decision summary

| Question | Decision |
|----------|----------|
| Does the composer settings drawer survive? | **No.** Deleted in step 5, once its contents have homes |
| Does the settings icon survive? | **No.** `showSettingsControl` default flips to `false`, then the input is removed |
| Where does model selection live? | Composer, where it already is (`chat-input.component.html:274`) |
| Where do inference params live? | **Nowhere user-facing.** Effort subsumes them (step 3) |
| What happens to Conversation Modes? | **Retired as a migration to Agents** (step 6), gated on prod usage |
| Does the Agent Marketplace move into Customize? | **No.** See §"What Customize is not" |
| Does Customize honour the Agent binding lock? | **No — deliberately.** See §"The agent-lock seam" |

## Why the drawer can die entirely

The drawer holds four things. Three of them are already redundant or dead:

**Model selection — redundant.** `<app-model-dropdown />` is already in the composer at
`session/components/chat-input/chat-input.component.html:274`. The drawer's model section is
a second copy of a control the user can already see.

**Advanced params — superseded by effort, already lying, and partly duplicated.** Effort
lives in the model dropdown's submenu with the active level in the trigger
(`components/model-dropdown/model-dropdown.component.ts:41`). Meanwhile GPT-5.6
**hard-rejects** `temperature` and `top_p` (measured; see `docs/specs/gpt-5-6-prompt-caching.md`
and the inference-params findings), so a per-model numeric param form already misrepresents
part of the catalog. Effort is the portable abstraction; the form is not.

**Measured on dev before cutting** (all 9 enabled models, via the live picker + drawer):

| Model | Advanced rows the drawer offered | Effort in the picker |
|-------|----------------------------------|----------------------|
| GPT-5.6 Sol / Luna | Max Output Tokens, **Reasoning Effort** | yes |
| Claude Sonnet 5, Opus 4.7 | Max Output Tokens, **Effort** | yes |
| Claude Sonnet 4.6 | Temperature, Top P, Max Output Tokens, **Effort** | yes |
| Claude Haiku 4.5 | Temperature, Top P, Max Output Tokens | **no** |
| Gemma 4 31B, GPT-5.4 | *(none — section already hidden)* | no |

Two things that changes:

1. **No enabled model exposes Extended Thinking to users.** The `thinking` param is declared
   in `curated-models.ts` for Sonnet 4.6 and Haiku 4.5, but the *deployed* records don't
   enable it, so the row never renders. The feared capability loss does not exist — but note
   the trap: the curated template is not the catalog, and only the live records answer this.
   Re-check before removing anything param-shaped in an environment other than dev.
2. **Effort was rendered twice** — in the picker AND as a row in the drawer's Advanced list.
   So the Advanced section was not merely superseded; for five of nine models its headline
   control was a literal duplicate of one three inches away.

What is left once Effort is deduped is Temperature, Top P and Max Output Tokens — sampling
knobs and a truncation guard.

Removing the form also retires the `max_tokens` ↔ extended-thinking coupling — Anthropic
requires `thinking budget < max_tokens`, which is the entire reason `model-settings.ts`
carried `unsatisfiable`, `clampNotices`, `disabledByConflict` and the post-edit re-check in
`reconcileThinkingAfterMaxTokens`. That machinery and its whole error-state vocabulary go
with it: ~410 lines of component and ~330 of template.

⚠️ `max_tokens` is a **truncation guard**, not a tuning knob. Removing the user control means
the admin default applies — which is already what every untouched user gets (the drawer read
"Defaults" for them). Admin-locked params (`row.locked`, "locked by admin") are unaffected:
this removes the *user-facing form*, not the governance behind it.

⚠️ **Stale overrides are the real hazard, not the missing form.** Overrides live in
`sessionStorage` under `inferenceParamOverrides`, so a tab open across the deploy still holds
whatever the user last typed, and it would keep riding every request with nothing in the UI
to show or reset it. `ModelService.dropRetiredOverrides` strips non-effort keys once, on load,
and rewrites storage. Effort is preserved explicitly — `setEffort` writes through this same
store, so a blanket purge would clear a control the user can still see and is still using.

**Conversation Mode — a strictly weaker Agent.** An admin-authored system prompt attached to
a conversation, with no tools, no skills, no bindings, no icon and no `@`-mention. That is
precisely the relationship Assistants had to Agents, which Marketplace D1 ("there is one noun,
and it is Agent") resolved by migration. Untouched since the PR that introduced it (#411) apart
from the delegated-admin-scope sweep and a theming pass.

Retiring it is bigger than deleting a drawer section — it has admin CRUD pages and routes, an
`admin.system_prompts` delegated scope, an admin nav entry, a user-facing `/system-prompts`
app_api route, a DynamoDB entity, and `system_prompt_id` on the invocation payload
(`apis/inference_api/chat/models.py:188`). ⚠️ **Gate on prod usage before writing any of it.**
"Dormant in git" is not "dormant in prod"; if a department is using a Mode, it has users and
the answer is a migration with redirects, not a deletion.

**Skills and Tools — global state in a conversational container.** The actual problem. They
move to Customize.

Strip the first three and nothing conversation-scoped remains. The drawer is not slimmed; it
is emptied, and then removed.

## What Customize is not

**Not the Agent Marketplace.** Customize is *capabilities you add to your assistant*. An Agent
is not a capability you toggle — it is a thing you talk to. Putting Discover under Customize
while My Agents stays in the sidenav splits one noun across two surfaces, which is the exact
failure D1 exists to prevent and the reason the whole `/assistants` deprecation was written.

The marketplace's real problem is narrower than placement: **Discover is already the first tab
of `/agents`** (`agents/components/agents-tabs.component.ts:22`), but `/agents` resolves to
*My Agents*, which is empty for nearly every user. The emptiest tab is the landing tab. The fix
is a routing change — land `/agents` on Discover when the user has no agents — not a
relocation. It is tracked here as step 7 and is independent of everything else in this spec.

If a future decision does move the marketplace into Customize, it must move **all** of
`/agents` and drop the sidenav entry. Splitting it is the one outcome to avoid.

## The agent-lock seam

⚠️ This is the sharp edge in PR-1, and it is a pre-existing defect the new surface exposes
rather than one it creates.

`ToolService` and `SkillService` are `providedIn: 'root'` singletons. When a conversation is
bound to an Agent, `session.page.ts:806` calls `lockToAgentTools()` / `lockToAgentSkills()`,
which makes `agentLocked()` true, rewrites what `visibleTools()` returns, and causes
`toggleTool()` to **early-return without saving** (`tool.service.ts:272`).

`ngOnDestroy` does **not** release these locks. They are cleared only when the session page
later loads an unbound conversation (`session.page.ts:341,791`). So a user who navigates from
an agent-bound chat straight to `/customize` arrives at a page where:

1. the list shows the Agent's bound set instead of their own preferences, and
2. every toggle silently does nothing.

The lock is conversation-scoped state that leaked into a global singleton. Customize is a
global surface and therefore **must not consult it**:

- Read the user's true state (`tool.isEnabled` / `skill.isEnabled`), never the
  display-shim (`isToolShownEnabled` / `isSkillShownEnabled`).
- Write through a lock-agnostic path. `toggleTool` / `toggleSkill` take an optional
  `{ respectAgentLock }`, defaulting to `true` so drawer behaviour is byte-identical.

Deliberately **not** fixed by clearing locks in `ngOnDestroy`: the `/` ↔ `/s/:id` transition
recreates the session component on a legitimate navigation (`session.page.ts:498`), so a
destroy-time clear would drop and re-apply the lock mid-flow.

The proper resolution is step 4 — the lock is a fact about the *conversation*, so it belongs
on the assistant indicator pill
(`session/components/assistant-indicator/assistant-indicator.component.ts`), which is already
in the conversation and already has an actions menu.

**Step 4's shape.** The indicator takes an `AgentGovernance` input (`modelName`, `toolCount`,
`skillCount`; `null` on a field means "the user's own setting applies"), renders a lock glyph
on the chip, and lists what is fixed in its menu — ending with the line that closes the loop:
*"Your own choices in Customize don't apply in this conversation."*

⚠️ It is derived from the **Agent record** (`chat-container`'s `agent()` input), NOT from
`ToolService.agentLocked()` & friends. Those are the leaking singletons this section is about;
reading them here would reintroduce the same staleness on the surface whose whole job is to
tell the truth about the current conversation. Deriving from the record also makes the preview
surfaces correct for free: the Designer preview and the marketplace test-drive render the
indicator without passing `[agent]`, so they get `null` and say nothing — right, because a
draft being previewed is not a conversation anyone's saved settings apply to.

## Cost consequence

⚠️ **A surface designed to make enabling tools easy will raise enabled-tools-per-user, and
every enabled tool grows `toolConfig` on every turn of every session** — the cacheable prefix
the cost-effectiveness tenet exists to protect. This is the same pressure that made new
injected tools ship `enabledByDefault=False` (default-on was a fleet-wide cache bypass), with
a friendlier face.

Two obligations, in the spec rather than discovered post-ship:

1. **Ordering stays deterministic** regardless of the order the new UI writes preferences in.
   Prompt-cache stability is exact-prefix-match; a set that reorders because the user toggled
   from a grid instead of a list rewrites the whole prefix at the cache-write premium.
2. **Browse should not be cost-blind.** Surfacing per-tool prompt weight (description +
   schema token count) is the honest version of a store that encourages adding things. Not in
   PR-1; named here so it is a decision rather than an omission.

## Sequencing

Each step is independently shippable. 3 and 6 do not depend on Customize at all.

| # | Step | Depends on |
|---|------|-----------|
| 1 | **Customize shell** — `/customize`, Tools + Skills tabs, nav entry. Drawer stays; both live — **shipped (#1072)** | — |
| 2 | Fold `Settings → Connectors` in as the Connectors tab — **shipped (#1076)** | 1 |
| 3 | Drop model + Advanced params from the drawer (pure dedup + param removal) — **shipped (#1073)** | — |
| 4 | Agent-lock surfacing moves to the assistant indicator — **shipped (#1075)** | — |
| 5 | Delete the drawer and the settings icon — **in flight** | 1, 2, 3, 4 |
| 6 | Conversation Modes retired as an Agent migration | prod-usage check |
| 7 | `/agents` lands on Discover for users with no agents | — |

Step 5 is last for a reason: pull the icon before Customize exists and you have removed the
only path to skills and tools. The end-state composer already renders today —
`showSettingsControl` is an existing input, set `false` in the agent preview
(`agents/agent-form/components/agent-preview.component.ts:121`) and the marketplace review
test-drive (`admin/marketplace/components/review-test-drive.component.ts:144`). Step 5 flips
the default and deletes the input.

## Resolved questions

**Does the composer keep a pointer to Customize?** No. The sidenav entry is the path, always
visible and one click away — the same shape Claude uses. Adding a composer affordance would
have reintroduced an icon to replace the one step 5 removes.

**What happens to Conversation Mode?** ⚠️ The spec originally had it retired in step 6 as "a
strictly weaker Agent", on the premise it was dormant. **That premise was wrong.** Prod carries
one enabled mode — *Guided Learning*, a Socratic tutoring prompt — and its use is accelerating:
1 session in July, 20 in August, **60 in the first 12 days of September**. Measured against
`boisestateai-v2-system-prompts` and `sessions-metadata` in the prod account.

So Mode is not dormant, and it is the one genuinely **per-conversation** control the drawer
held. It could not follow Skills and Tools to Customize without recreating the exact scope lie
this epic exists to fix, so it went the other way: into the **composer**, beside the model and
effort controls. Those three are the same question — how should *this* conversation run.

That also reframes step 6: retiring Modes is much harder to justify against growing usage, and
the migration is not clean — a Mode applies to the conversation you are already in, whereas an
Agent is a separate thing you start a chat with. Step 6 is now "reconsider", not "execute".

This is the second time the "git history says dormant" heuristic has misled on this epic (the
first was `thinking` in step 3, declared in `curated-models.ts` and absent from the deployed
records). **Check the data in the environment that matters.**

## Step 5 notes

⚠️ **A latent bug surfaced while verifying the new picker, and is fixed here.** The session
page hydrates the active mode twice on load: once provisionally, before the session's metadata
arrives, and again with the real value. The provisional call CLAIMED the session id, so the
clobber guard in `hydrateFromSession` rejected the real hydration that followed.

That was not cosmetic. `chat-request.service` sends `selected_prompt_id` from
`activePromptId()`, so **after any reload the mode silently stopped being applied to every
later turn**, while the stored session preference still said it was on. On prod that is every
Guided Learning user who reloaded mid-conversation. The provisional call now passes
`claim: false`; a deliberate "None" still claims, so stale metadata cannot undo it.

It is fixed here rather than deferred because step 5 promotes this control to a first-class
composer affordance, and shipping it more prominently while knowing it silently drops would be
worse than leaving it where it was.

## PR-1 scope

**In:**

- `/customize` → `/customize/tools`, `/customize/skills`, lazy-loaded, `authGuard`
- Tabs strip mirroring `AgentsTabsComponent`
- Browse idiom borrowed from `agents/discover`: search box + category chips + responsive grid
- Cards read the existing root services — no new endpoints, no new state. Because both
  surfaces share the same singletons, a toggle in Customize updates the drawer live and
  vice versa
- Sidenav entry (which also gives `/my-skills` a reachable home — see the standing
  `sidenav.html:92` comment saying its navigation was undecided)
- The lock-agnostic write path described in §"The agent-lock seam"

**Out:** connectors tab (step 2), tool detail pane / per-sub-tool expansion, any drawer
deletion, prompt-weight display, marketplace changes.

## Step 2 notes

The page moved wholesale (`git mv`, so history follows it); only the shell changed — tabs
plus an `h1`, and the row/empty/error containers went to `rounded-2xl` so the three tabs read
as one surface. The Connect/Disconnect buttons and the `vendor-*` icon tokens were left
exactly as they were: both carry load-bearing contrast reasoning in their comments.

⚠️ `/settings/connectors` stays as a **redirect**, declared BEFORE the `settings` route whose
`loadChildren` would otherwise swallow it and land the user on the settings shell with no
matching child. A test asserts that ordering, because the failure is silent.

The `settings/connectors/` **services** deliberately did not move. `UserConnectorsService` and
`ConnectorStatusService` have nine importers across the app (oauth-consent, export-dialog,
knowledge-base, the drawer's tool-detail, the Customize Tools tab…), so relocating them is a
wide, purely-mechanical diff that belongs on its own. Their real home is probably
`services/connectors/` — noted, not done here.

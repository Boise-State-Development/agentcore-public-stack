# Customize — a browse surface for tools, skills and connectors

**Status:** PR-1 (Customize shell: Tools + Skills) in flight. Steps 2–7 queued.
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
| **Connectors** | OAuth connection state for external MCP servers | `Settings → Connectors` |

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

**Advanced params — superseded by effort, and already lying.** Effort lives in the model
dropdown's submenu with the active level in the trigger
(`components/model-dropdown/model-dropdown.component.ts:41`). Meanwhile GPT-5.6
**hard-rejects** `temperature` and `top_p` (measured; see `docs/specs/gpt-5-6-prompt-caching.md`
and the inference-params findings), so a per-model numeric param form already misrepresents
part of the catalog. Effort is the portable abstraction; the form is not.

Removing the param form also retires the `max_tokens` ↔ extended-thinking coupling —
Anthropic requires `thinking budget < max_tokens`, which is the entire reason
`model-settings.ts:132-180` carries `unsatisfiable`, `clampNotices`, `disabledByConflict`
and the post-edit re-check in `reconcileThinkingAfterMaxTokens`. That machinery, and its
whole error-state vocabulary, goes with it.

⚠️ `max_tokens` is a **truncation guard**, not a tuning knob. Before step 3 lands, confirm
the admin-side default is generous enough that removing user control does not start clipping
long outputs. Admin-locked params (`row.locked`, "locked by admin") are unaffected — this
removes the *user-facing form*, not the governance behind it.

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
in the conversation and already has an actions menu, but says nothing about bindings today.
Until then, a user toggling a skill in Customize has no way to know their agent-bound
conversation will ignore it.

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
| 1 | **Customize shell** — `/customize`, Tools + Skills tabs, nav entry. Drawer stays; both live | — |
| 2 | Fold `Settings → Connectors` in as the Connectors tab | 1 |
| 3 | Drop model + Advanced params from the drawer (pure dedup + param removal) | — |
| 4 | Agent-lock surfacing moves to the assistant indicator | — |
| 5 | Delete the drawer and the settings icon | 1, 2, 3, 4 |
| 6 | Conversation Modes retired as an Agent migration | prod-usage check |
| 7 | `/agents` lands on Discover for users with no agents | — |

Step 5 is last for a reason: pull the icon before Customize exists and you have removed the
only path to skills and tools. The end-state composer already renders today —
`showSettingsControl` is an existing input, set `false` in the agent preview
(`agents/agent-form/components/agent-preview.component.ts:121`) and the marketplace review
test-drive (`admin/marketplace/components/review-test-drive.component.ts:144`). Step 5 flips
the default and deletes the input.

## Open question

**Does the composer keep a pointer to Customize?** Removing the icon outright is the clean
version; a menu item under the `+` is the hedged one. Since enablement is global and durable,
"I need a tool mid-conversation" is rarer than it feels — Claude itself has no in-composer
skill toggle. But the path has to exist somewhere, and it should be decided rather than
discovered. Resolve before step 5.

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

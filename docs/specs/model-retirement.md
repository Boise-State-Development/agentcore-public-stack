# Retiring a managed model

**Status:** Runbook, with the §7 change that Stages 1 and 3 depend on built
alongside it (PR #1271). The one §7 piece still missing is the AWS
end-of-life error classifier, which needs the exception text Bedrock actually
returns (§4).
**Scope:** A row in the managed-models table (`MODEL#<uuid>`, looked up by
`modelId` through `ModelIdIndex`) that users select in chat, Agents bind through
`modelConfig`, and roles grant through `grantedModels`. Models the backend names
in code rather than in the catalog — the fallback default, the side-channel
models — differ enough to get their own section (§8).
**Refs:** `docs/specs/mcp-server-retirement.md` is the sibling runbook and the
template for this one; read its §1 and §4 first. `agent_binding_resolver.py` for
the D5 block; `docs/specs/agent-version-snapshots.md` for why a published Agent
cannot be fixed by its author alone.

---

## The question this answers

> We have a plan for retiring tools. Do we need the same for models?

**Yes, and it can't be a copy.** The shape carries over — inventory, stop new
adoption, migrate, cut over, clean up — but three facts about models change
what each stage has to do:

1. **The runtime model check reads role grants and nothing else** — not the
   catalog row, not `enabled`. So every lever an admin has today (disable,
   delete) changes what the *pickers* show and leaves the *runtime* alone (§1).
2. **AWS owns the clock.** A tool retires when we decide. A Bedrock model
   retires on the EOL date on its AWS model card, whether we are ready or not,
   and after that date every holder of the id fails with a generic error (§3).
3. **Models can be swapped for a successor; tools can't.** Nobody expects
   `canvas_faculty` to answer as `hello_world`. An Agent built on Sonnet 4.6
   answering on Sonnet 5 is the *expected* outcome. That gives models a lever
   tools lack — **redirect** — and it changes what the cutover should be (§5).

---

## 1. The thing that decides the design

There are two model-access predicates, and they disagree on purpose:

| Predicate | Reads | Used by |
|---|---|---|
| `ModelAccessService._grants_access` ([`model_access.py:71`](../../backend/src/apis/app_api/admin/services/model_access.py#L71)) | `enabled` **and** role grants (`*` or the id) **and** legacy `availableToRoles` | `GET /models` (chat picker), `bindable_catalog` (Designer palette), `binding_validation._validate_model` (Agent **write** check) |
| `AppRoleService.can_access_model` ([`rbac/service.py:285`](../../backend/src/apis/shared/rbac/service.py#L285)) | role grants only: `"*" in models or model_id in models` | **every runtime path** — plain chat ([`routes.py:2391`](../../backend/src/apis/inference_api/chat/routes.py#L2391)), the saved-default re-check (`routes.py:3177`), Agent `modelConfig` ([`agent_binding_resolver.py:244`](../../backend/src/apis/inference_api/chat/agent_binding_resolver.py#L244)), the Converse API ([`converse_routes.py:632`](../../backend/src/apis/app_api/chat/converse_routes.py#L632)) |

> CLAUDE.md says `can_access_model` and `filter_accessible_models` "both delegate
> to a single `_grants_access` predicate". That is true of the two methods on
> `ModelAccessService`, but `ModelAccessService.can_access_model` has **no
> production caller**. The runtime calls the *other* `can_access_model`, on
> `AppRoleService`, which never reads the catalog.

That split is a hide-then-still-allow divergence. As the tool runbook's §4
explains, that is the safe direction. It means the runtime never learns that a
model is disabled or gone:

| Surface | Model inaccessible (grant revoked) | Row `enabled: false` | Row hard-deleted, grant survives |
|---|---|---|---|
| Chat picker | hidden | hidden | hidden |
| Stale tab / explicit `model_id` | **HTTP 403** `Access denied to model: <id>` — not a `stream_error` | runs | runs, **degraded** (below) |
| Saved `defaultModelId` | silently falls back to `Defaults.MODEL_ID` | runs | runs, degraded |
| Agent `modelConfig` | **hard block** (D5): *"This agent runs on **<id>**, which isn't available to your account…"* | runs | runs, degraded |
| Project harness `modelConfig` | degrades to the invoker's default + `agent_notice` | runs | runs, degraded |
| Agent Designer save | 403 `You do not have access to model` | **403** — the §3 trap | **400** `Model '<id>' is not available` |
| Paused-turn resume | runs — `snapshot.model_id` is **not** re-checked (`routes.py:3094`) | runs | runs, degraded |

"Degraded" is its own row in §2, because it is the model equivalent of the
tool runbook's silent-capability-loss state, and it is worse.

The levers, then:

| Lever | Effect | Verdict |
|---|---|---|
| Disable (`enabled: false`) | Hidden from every picker; runtime unchanged; **Designer save of any Agent that keeps the model now 403s** | Close to Stage 1, but it lies to authors (§3). Don't use for retirement |
| Revoke grants | Hard-blocks Agents, 403s stale tabs, silently re-routes saved defaults. **Cannot reach `*` holders** (`system_admin` and friends) or inherited grants | Stage 3 lever **only when there's no successor**, and even then incomplete (§5) |
| Hard-delete the row | See §2 | **Never.** Tombstone instead (Stage 4) |
| Redirect to a successor | Every holder keeps working on the new model | **The Stage 3 lever for models** — needs §7 |
| Do nothing | AWS EOL hard-fails every holder with a generic error | What happens by default. The runbook exists to get ahead of it |

---

## 2. The worst state: a deleted row with a surviving grant

`DELETE /admin/managed-models/{id}` ([`admin/routes.py:803`](../../backend/src/apis/app_api/admin/routes.py#L803))
deletes the row **first**, then calls `revoke_model_from_all_roles`. That call
strips only *direct* `grantedModels` entries, so it cannot touch a `*` grant or
an inherited one. After a delete, every wildcard holder still passes
`AppRoleService.can_access_model` for the dead id. Their turn then runs with:

- **No metering.** `pricing_config.get_model_by_model_id` finds no row, so
  `_calculate_cost` returns `None` ([`stream_coordinator.py:3681`](../../backend/src/agents/main_agent/streaming/stream_coordinator.py#L3681)).
  The turn is free against quota. The Converse API skips cost recording the
  same way.
- **No prompt caching.** `_resolve_model_settings` sets `caching = None` when
  `_find_managed_model` returns nothing. At our prefix sizes that is a real
  price increase for a model we are no longer even charging for.
- **No parameter guard.** `_merge_inference_params` treats a missing spec as
  "permissive" and forwards request params verbatim. That is exactly the
  `temperature`-kills-Opus-4.7 class the spec inversion was written to close.
- **No `max_tokens` default** from the row, and no model-relative compaction
  threshold (it falls back to the Strands table, then to 100k).

It keeps working until AWS EOL, then fails as §3 describes. **Grants off before
the row does not work for models** (the tool runbook's rule): no admin action
removes a model from a `*` grant. So the rule here is stronger:

> **A model row is never hard-deleted.** Its last state is a tombstone
> (`status: retired`), which the runtime reads (§7).

Two smaller reasons point the same way. `DEFAULT_MODELS` in
`seed_bootstrap_data.py` re-creates a deleted Haiku 4.5, Sonnet 4.6 or Nova 2
Sonic on the next bootstrap run (skip-if-present by `modelId`, same as
`DEFAULT_TOOLS`), with no grants. And cost history (`ROLLUP#MODEL`,
per-message `modelInfo.modelId`) keeps naming the id forever.

Until §7 lands, a tombstone is just a row: it keeps metering and caching
correct, but it redirects and denies nothing.

---

## 3. Why `enabled: false` is not Stage 1

On the surface, disabling is the one-way hide we want: pickers drop the model,
and every Agent and saved default that already has it keeps running. Three
reasons not to use it:

1. **It 403s authors whose Agent keeps the model.** `_validate_model` reads
   `filter_accessible_models`, which rejects a disabled row. The SPA always
   sends `modelConfig` on save (`agent-form.page.ts:1052`), so changing one word
   of a Sonnet-4.6 Agent's instructions fails with *"You do not have access to
   model"* — for a model it can still run. That is the tool runbook's §4 trap,
   and here it is already live.
2. **It silently swaps plain-chat users.** `ModelService.loadModels` keeps only
   enabled models and falls back through sessionStorage → user default →
   `isDefault` → first model with no message. A user mid-conversation on the
   retiring model moves to another one on their next page load and is never
   told.
3. **`enabled` already means something else.** Dev's Nova 2 Sonic row is
   `enabled: false` because it is voice-only, not selectable in chat. It is
   still priced from the row, because `list_managed_models(user_roles=None)`
   includes disabled rows. Overloading the flag makes "why is this off?"
   unanswerable.

So, as with tools: a lifecycle `status` separate from the on/off flag, which
the pickers read and the write validator does **not** filter on.

---

## 4. The external clock

From the [Bedrock model lifecycle](https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html) page:

- The states are **Active → Legacy → EOL**. `ListFoundationModels` /
  `GetFoundationModel` report them as `modelLifecycle.status`, and the admin
  Bedrock browser page already displays it (`admin/routes.py:162`). **Nothing
  copies it onto the catalog row.**
- **Legacy periods are 6 months or 45 days.** The EOL date appears on the AWS
  model card when Legacy starts. Only the Bedrock date applies, not the
  provider's.
- During Legacy, *"existing customers may lose access after 15 days of
  inactivity."* In a quiet account (dev, or a model almost nobody picks), the
  real deadline can come **before** the EOL date.
- After EOL, requests fail. AWS won't migrate anything for us.

What our users see at EOL is not good. Nothing in the stream path recognises
an end-of-life error: a Bedrock `ClientError` reaches the generic handler in
`stream_processor.py:1624` and becomes *"⚠️ Something went wrong while
processing your request … Please try again."* Retrying won't help, on every
turn, for every holder. (We haven't captured the exact exception class and text
Bedrock returns for an EOL id. Capture it the first time one is seen, before
writing the classifier in §7.)

**Where we are today, measured 2026-09-24 (us-west-2):**

| | |
|---|---|
| Foundation models in `LEGACY` | `anthropic.claude-sonnet-4-20250514-v1:0`, `anthropic.claude-opus-4-1-20250805-v1:0` |
| dev catalog (`dev-boisestateai-v2-managed-models`, 11 rows) | holds **neither**. Nothing to retire in dev today |
| prod catalog | **not checked.** Run Stage 0 there before concluding anything |

Our rows carry inference-profile ids (`us.*`, `global.*`), while
`ListFoundationModels` reports base ids. Strip the geo prefix to match them.
Mantle ids (`openai.gpt-5.4`, `google.gemma-4-31b`) and the hosted OpenAI
family may not appear in `ListFoundationModels` at all. For those, the AWS
model card is the only source.

---

## 5. The staged sequence

Two lines, as in the tool runbook, plus a third that we don't control:

- **The blast line is Stage 3**, where a holder first sees the model change or
  stop.
- **The point of no return is Stage 4.**
- **The AWS EOL date** is a hard deadline for Stage 3. Plan Stage 3 at least
  two weeks before it, and before the 15-day inactivity cliff for any model with
  low traffic.

### Stage 0 — Inventory (no user-visible change)

Decide the **successor** first; it shapes everything else. Usually it is the
same family one generation on (Sonnet 4.6 → Sonnet 5). Sometimes there isn't
one, e.g. a specialist model with no replacement.

**a. Roles.** The model already has a better endpoint than tools do. It
classifies every grant as `direct`, `wildcard` or `inherited`, so a
`*` holder is visible here (not blind the way `get_roles_for_tool` is):

```bash
curl -s "$APP_API/api/admin/managed-models/$MODEL_UUID/roles" | jq '.[] | {roleId, source}'
```

Run it for the **successor** too, and record the difference: every role that
grants the retiree and not the successor. Redirect (Stage 3) checks access
against the successor, so that difference is exactly the set of users who
would be blocked instead of redirected.

**b. Agents — live records and published snapshots.** The model is a
singleton `modelConfig`, not a binding, so the tool runbook's `bindings` scan
doesn't apply. One scan covers both populations (`SK = METADATA` and
`SK = VERSION#…` share `PK = AST#<id>`):

```bash
aws dynamodb scan --table-name "$ASSISTANTS_TABLE" \
  --filter-expression "modelConfig.modelId = :m" \
  --expression-attribute-values "{\":m\":{\"S\":\"$MODEL_ID\"}}" \
  --projection-expression "PK,SK,ownerId,#n,listing" \
  --expression-attribute-names '{"#n":"name"}' --output json \
| jq '.Items[] | {pk: .PK.S, sk: .SK.S, name: .name.S, owner: .ownerId.S,
                 publishedVersion: .listing.M.publishedVersion.N}'
```

Split the result three ways:

- **Live-record Agents**: the owner can fix these.
- **Published snapshots**: the owner cannot fix these alone. The snapshot
  freezes `modelConfig` exactly as it freezes `bindings`. See the tool
  runbook §5d.
- **Project harnesses**: these degrade rather than block, so they are the
  least urgent.

**c. Admin agent templates.** Scan `TEMPLATE#` rows for a `modelId`. The
built-in seed templates use `model_id=None`; admin-created ones may pin a model.

**d. Saved user defaults.** `USER#<id>` / `SETTINGS` rows in the user-settings
table, `defaultModelId = <id>`. These fall back silently. Count them so the
Stage 1 notice reaches the right people, not because they will break.

**e. Real usage.** The `ROLLUP#MODEL` rows for the last two months say whether
anyone actually uses the model, and how much spend moves to the successor at
its price. A model with zero use and zero Agents skips straight to Stage 3.

**f. Code references.** `git grep -n "<model-id>"` over `backend/`,
`frontend/` and `infrastructure/`. A hit means §8 applies.

**g. Export the row.** `GET /api/admin/managed-models/$MODEL_UUID` →
`retirement-<modelId>.json`. `supportedParams`, the four prices and the
hand-tuned `maxInputTokens` are admin-entered and exist nowhere else.

API-key clients (`/chat/api-converse`) send ids from their own code. We can't
enumerate them from our data; look for the id in the app-api access logs.

### Stage 1 — Deprecate. Stop new adoption *(needs §7)*

1. Set `status: deprecated`, `replacedBy: <successor>`, `retiresOn: <date>`
   (our Stage 3 date, **not** AWS's EOL date), and a `retirementNote`.
2. If the retiree is `isDefault`, move the flag to the successor in the same
   edit.
3. Remove it from admin templates.

Effect:

- Every picker refuses a **new** selection.
- Plain-chat users with it selected keep it, now shown with a retiring note.
- Every Agent keeps running.
- Authors can still save Agents that keep it, because the write validator does
  not filter on `status`.

Tell Agent owners and saved-default users now (§9).

**Rollback:** set `status: active`. Seconds, no trace.

### Stage 2 — Migrate

- **Roles:** grant the successor to every role in the Stage 0a difference.
  Do this first. Until it lands, those users get blocked instead of redirected
  at Stage 3.
- **Live-record Agents:** the owner selects the successor in the Designer. The
  save validates against the successor's `supportedParams`, which may drop an
  `effort` value or a temperature the old model allowed. The owner sees that
  in the form.
- **Published Agents:** the owner resubmits and an admin approves. Takedown
  un-freezes the snapshot, as in the tool runbook. Unlike tools, **redirect
  makes this stage optional rather than required**: a published Agent that
  never resubmits keeps working at Stage 3, on the successor. Chase owners
  anyway, because only a resubmitted version has actually been *reviewed* on
  the new model.

Re-run the Stage 0b scan; don't trust the ticket.

### Stage 3 — Cut over. **The blast line**

**With a successor: redirect.** Set `status: retired`. From the next turn
(≤ 60 s catalog cache + agent-cache rebuild), every runtime path that would
invoke the retiree invokes the successor instead:

- plain chat
- saved default
- Agent live record and published snapshot
- project harness
- paused-turn resume
- Converse API

Access is checked against the **successor**. Cost is priced on the successor.
The Agent's stored `params` go through the successor's spec, so anything it no
longer supports is dropped and logged, never forwarded.

Cost of the cutover: every live session on the model pays **one** prompt-cache
prefix write at the successor's rate on its next turn. The agent cache keys on
model id, so the rebuild is automatic; the cost is one-off and unavoidable. Do
it as a single edit, not a morning of toggling.

What redirect deliberately does *not* do is rewrite stored records. A published
snapshot still says `sonnet-4-6`, because that is what was reviewed. Rewriting
it would make the version history lie. The redirect is resolved at invocation
and is visible to the owner in the Designer (§7).

**Without a successor: deny.** Set `status: retired` with no `replacedBy`.
The runtime treats the model as inaccessible for **everyone, including `*`
holders**. This is the one thing revoking grants cannot do:

- Agents hard-block (D5).
- Harnesses degrade.
- Saved defaults fall back.
- Explicit requests get the §7 conversational error, not a raw 403.

**Rollback:** set `status: deprecated`. It takes effect within a minute. Messages
already shown and turns already answered on the successor stay as they were.

### Stage 4 — Tombstone. **The point of no return**

**Don't delete the row.** Leave it `retired`, hidden from the admin list's
default filter. This state costs nothing and has four jobs:

- it keeps the runtime redirect or deny working;
- it keeps `pricing_config` able to name the model in historical cost views;
- it stops `DEFAULT_MODELS` resurrecting the row;
- it closes §2's wildcard hole for good.

What *can* be removed at this stage:

- **Direct `grantedModels` entries for the retiree** (the model form's roles
  editor writes through). Harmless to leave, but noise on the role page.
- **`inferenceParamOverrides` sessionStorage keys.** These are per-tab and
  expire on their own.

What makes this the point of no return is AWS, not us. Once the EOL date
passes, flipping `status` back to `active` just produces §4's generic error
instead of a working model.

---

## 6. Retiring the default model

`isDefault` on the catalog is read **only by the SPA**. The backend's fallback
is the hard-coded `Defaults.MODEL_ID` (Haiku 4.5, `constants.py:137`), and the
SPA's last resort is its own hard-coded `DEFAULT_MODEL` (Haiku 4.5,
`model.service.ts:34`). When the SPA reaches that one it sends
`model_id: null` and lets the backend choose. So moving `isDefault` in the admin
UI changes what *new chats in the SPA* pick, and changes **nothing** about:

- saved-default fallback,
- scheduled runs (the worker passes no model),
- project harness degradation,
- any request without an explicit id.

Retiring the model that sits in `Defaults.MODEL_ID` is therefore a **code
change** (§8), and the admin UI can't do it alone. Haiku 4.5 is that model
today. Follow-up 3 fixes that by making the backend read `isDefault`.

---

## 7. The change Stages 1 and 3 need

### Backend

Four fields on `ManagedModel` (`apis/shared/models/models.py`), carried on the
create/update/read models, the DynamoDB write path and `BindableItem.meta` for
`kind: "model"` (plus `replacedByName`, so the Designer can name a successor
the author may not hold yet):

| Field | Holds |
|---|---|
| `status` | `active` (default; absent reads as active) · `deprecated` · `retired`. An unknown stored value also reads as `active` rather than failing validation: a row that fails to parse drops out of the catalog, and a model with no row runs unmetered (§2) |
| `replacedBy` | a successor `modelId`. Unlike tools, where free text was right because successors split or don't exist, a model successor is *invoked*, so it has to be an id. Validated on write: must exist, must be `active` **and enabled** (the SPA only follows a redirect to a model it offers), must not be the model itself |
| `retiresOn` | ISO `YYYY-MM-DD`, validated exactly as on `ToolDefinition` |
| `retirementNote` | free text ≤ 300 chars |

On an update, absent leaves a field alone and `""` clears it (the attribute is
`REMOVE`d, not stored as `""`), the same contract as `iconSlug`. The admin
write path (`validate_lifecycle`) also refuses a non-active model as the
default, and `DELETE` refuses a model that another row names as `replacedBy`
with a 409.

**One resolution function**, `resolve_effective_model` in
`apis/shared/models/retirement.py`, returning an `EffectiveModel` (the id to
invoke, the successor's row and provider, the retired row, `denied`). It reads
the catalog (already cached 60 s), follows `replacedBy` through `retired` rows
with a depth cap and cycle guard (A → B, and later B → C), and **fails open**:
if the catalog can't be read, the id runs as requested. It is called before the
access check at every runtime entry point, and the access check then runs on
the effective id:

| Entry point | Redirect | Deny |
|---|---|---|
| `/invocations`, request `model_id` (top of the handler, so the App tool-call / context / continuation paths share the turn's agent-cache slot) | `model_id` **and `provider`** swapped to the successor's | conversational `stream_error`, not the bare 403 |
| `_resolve_user_default_model` | successor id + provider | treated as unset → system default |
| `agent_binding_resolver` (live record **and** published snapshot) | `model_override` on the successor, Agent `params` riding along | `AgentBindingBlockedError`; a project harness degrades instead |
| `/chat/api-converse` | `request.model_id` swapped, so the response names the model that answered | `410 Gone` |
| Paused-turn resume | **not redirected** — the turn finishes on its snapshot's model, which is what rebuilds the paused agent's cache slot. Snapshots are short-lived | — |

This makes the runtime catalog-aware for exactly one thing — `retired` — and
leaves `AppRoleService.can_access_model` itself untouched. A model with no row
still resolves to itself, so custom and hand-entered ids behave as today.

**Pickers do not filter by status.** `filter_accessible_models` stays as it is,
so `_validate_model` keeps accepting an Agent that keeps its deprecated model.
That is the tool runbook's §4 rule carried over: display-layer guard, one
grant set.

**Not built: an EOL error classifier** in `stream_processor`/`errors.py`:
*"<Model> has been retired by AWS and can no longer be used. Choose another
model."* in place of "Something went wrong … try again". This is the
belt-and-braces for the model nobody ran this runbook on. It needs the real
exception text first (§4).

### SPA

`isRetiring`, `formatRetiresOn` and `retirementDetail` moved out of
`tool.service.ts` into `shared/utils/retirement.ts` (`tool.service.ts`
re-exports them, so no tool surface changed), joined by
`modelRetirementDetail`. The model sentence differs from the tool one in a
single way: a model usually has a successor answering in its place, so
*"it stops working"* is only said when it has none.

| Surface | Guard |
|---|---|
| Chat model dropdown | A non-`active` model is not offered, *unless* it is the current selection, where it shows with a retiring badge and the `retirementDetail` line. Hiding is right here (unlike the tool picker's disabled row), because the successor is the obvious alternative and a disabled menu row is noise |
| `ModelService.loadModels` fallback | When the saved or session model is `retired` and has a `replacedBy`, select the successor **and say so** with a one-line toast, instead of today's silent swap |
| Agent Designer model cards | A non-`active` card is **hidden unless it is the agent's model** — a departure from the tool pattern's disabled row, because a retired model stays in the catalog as a tombstone for good and would otherwise grow a permanent graveyard of dead cards. The agent's own model stays selected with a `retiring` badge and a section notice: what happens (the successor, the date), and what to do (choose another model; resubmit if published). `selectModel` refuses a retiring ref in the add direction as the backstop |
| Settings → default model | The option is `[disabled]` unless it is the current value. A saved default on a *retired* model matches no option, so a line under the select names the successor now answering in its place |
| Admin model list | A `deprecated` / `retired` badge beside the name |
| Agent detail page (`/agents/:id`) | `modelRetirement` on `GET /agents/{id}` (status, successor's display name, date, note) renders a line under the Details panel's Model row: *"Retired. Claude Sonnet 5 now answers in its place."*, or *"…can't run until its owner chooses another model"* when there is no successor |
| "Will it run?" (per viewer, and per role for pins) | `resolve_runnability` and `role_pin_service._diff_against_role` resolve retirement first, exactly like the runtime: a redirect is checked against the **successor**, and a retired model with no successor is missing (*"Claude Opus 4.1 (retired)"*) whatever the viewer or role holds — before this they answered *ready* for an agent the runtime refuses |
| Admin model form | The four fields, revealed when Status leaves `active` (hidden, not disabled, as on the tool form); `replacedBy` is a select over active models |

**Compatibility on deploy:** every existing row lacks `status` and reads
`active`, so the change is a strict no-op until an admin sets a status. Verify
per environment with a scan for `status <> active`.

---

## 8. Models named in code

The catalog runbook doesn't reach these; each needs a PR:

| Where | Model today | Failure mode at EOL |
|---|---|---|
| `Defaults.MODEL_ID` (`agents/main_agent/config/constants.py:137`) + `agents/main_agent/__init__.py:22` | Haiku 4.5 | **Every** fallback turn fails: saved-default, scheduled, harness, null `model_id` |
| SPA `DEFAULT_MODEL` (`model.service.ts:34`) | Haiku 4.5 | Display only. It sends `null`, which lands on the row above |
| `DEFAULT_MODELS` seed (`seed_bootstrap_data.py:233`) | Haiku 4.5 (`isDefault`), Sonnet 4.6, Nova 2 Sonic | Resurrects a deleted row; seeds a fresh stack with a dead default |
| Title generation (`inference_api/chat/service.py:588`) | Nova Micro | Fails open → "New Conversation" |
| Tool summaries (`tool_summaries/summarizer.py:42`) | Nova Micro | Fails open → client-side formatter |
| Compaction summary (`AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID`, `constants.py:197`) | Nova 2 Lite | Fails open. **Compaction stops**, and the cost goes to prompt size rather than errors |
| Document digest (`DOCUMENT_DIGEST_MODEL_ID`) | — | Fails open |
| Voice (`NOVA_SONIC_MODEL_ID`, `constants.py:242`) | Nova 2 Sonic | Voice fails; pricing also depends on the catalog row |
| Embeddings (`kb_backend/provisioning.py:93`, CDK `managed-kb-role-construct.ts`) | Titan v2 | **Existing vectors are tied to the model.** Changing it means re-embedding every KB. A project in its own right, not a runbook step |
| Memory role IAM (`memory-construct.ts:66`) | `anthropic.claude-*`, `amazon.nova-*` wildcards | Only matters if a successor is from another vendor |

Three of these fail *open*. That is the right choice for availability, but it
means their retirement is noticed as a quality or cost drift, never as an
error. The weekly lifecycle check (follow-up 1) is what catches them.

---

## 9. What to tell people

**Stage 1 — Agent owners (0b) and saved-default users (0d):**

> **`<Model>` is being retired on `<date>`.** Nothing changes today.
> Your agent **`<Agent>`** uses it and keeps working as it does now.
> From `<date>` it will run on **`<Successor>`** instead. Answers may differ a
> little, and `<cost comparison — "about the same" / "roughly 2× the quota use">`.
> To test it before then, switch the agent to `<Successor>` in the Designer.
> If your agent is published, resubmit it so the reviewed version runs on the
> new model.

(If there's no successor: *"…after `<date>` an agent that still uses it will
stop running and tell its users to contact an administrator."*)

**Stage 3 — everyone who used the model in the last 30 days (0e):**

> `<Model>` has been retired. Conversations and agents that used it now run on
> `<Successor>`.

**Stage 4:** internal only.

---

## 10. Follow-ups this runbook wants and does not have

1. **Nothing watches AWS lifecycle state.** The admin Bedrock page shows
   `modelLifecycle`, but only to someone who goes and looks. A weekly check —
   catalog `modelId`s (geo prefix stripped) against `ListFoundationModels`,
   anything `LEGACY` → an admin badge on the model row plus an alert — turns
   §4 from a surprise into a ticket. It also belongs in `kaizen-research`'s
   internal audit.
2. **Plain chat answers a denied model with HTTP 403, not a `stream_error`**
   (`routes.py:2394`). The house rule is that errors stream as assistant
   messages. A stale tab after Stage 3 (deny) hits this.
3. **The backend never reads `isDefault`** (§6). The admin "Default" toggle
   and `Defaults.MODEL_ID` are two answers to one question.
4. **`PUT /user-settings` never validates `defaultModelId`.** It calls
   `get_managed_model`, which keys on the row UUID, with a provider model id,
   so it always misses and saves anyway (`user_settings/routes.py:47`).
   `_validate_model` in the Designer documents the same trap and avoids it.
5. **Paused-turn resume re-checks neither model access nor retirement**
   (`routes.py`, the `is_resume` branch). Deliberate for retirement — the
   resume must rebuild the paused agent's cache slot — and low severity because
   snapshots are short-lived, but it is the one runtime path Stage 3 does not
   reach.
6. **The delete endpoint deletes the row before revoking**, and can't revoke
   wildcards (§2). It now refuses a model that another row names as its
   replacement (409); it should go further and refuse a model that still has
   holders, or become the tombstone action.
7. **CLAUDE.md's "single `_grants_access` predicate" note** describes
   `ModelAccessService` accurately but implies it guards the runtime, which it
   doesn't (§1). Correct it when §7 lands.

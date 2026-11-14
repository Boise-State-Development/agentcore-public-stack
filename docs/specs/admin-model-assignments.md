# Admin model assignments

**Status:** Plan, not built. Audit taken 2026-11-13 on `develop` at `bee114fb9`.
**Supersedes:** the narrower "admin-configurable model for document extraction"
follow-up. That model is one slot here (§3, slot 1).

---

## 1. The problem

The model a user picks for a turn is already an admin decision. Catalog rows, role
grants, `isDefault` and retirement all live in the admin model settings, and
`grants_model_access` is the one check that reads them.

Every other model the platform calls is chosen in code or at deploy time. That covers
the side-channel calls: titles, tool summaries, compaction summaries, document digests
and structured extraction. Two costs follow:

1. **Changing one takes a code change or a deploy.** An admin can't move compaction to
   a better summarizer, or extraction to a model the account actually has quota for,
   without a release.
2. **Nothing checks that the id has a catalog row.** A side-channel id with no row runs
   unmetered: it prices to `None`, counts against no quota and fires
   `UnmeteredModelCall`. Extraction's `DOCUMENT_EXTRACT_MODEL_ID` falls into this
   today if someone sets a `global.*` id that prod does not catalog.

**Goal:** every model the platform chooses at run time on a user's behalf is either
assigned by an admin from the catalog, or deliberately stays fixed at deploy time, with
the reason written down.

**Not a goal:**
- choosing models for offline tooling (evals, load tests, benchmarks);
- the embedding model, which is bound to the vector index (§2.3);
- changing how a turn's own model is resolved.

---

## 2. Audit

These are the places that choose a model, not every place that mentions one.

The search covered `backend/src`, `infrastructure/lib`, `frontend/ai.client/src`,
`tui/`, `scripts/` and `.github/`, matching Bedrock id shapes (`us.`/`global.` profiles,
`anthropic.claude`, `amazon.nova`, `amazon.titan`, `openai.gpt`, …) and `*_MODEL_ID`
environment reads. It found 66 files. Most are not choices:
- docstrings and examples;
- test fixtures;
- the curated catalog seed (`curated-models.ts`, 32 ids, which is catalog data, not a choice);
- the SPA's demo data for the cost dashboard.

### 2.1 Runtime side-channel calls → admin slots

| # | Where | Model today | Set by | What it does |
|---|---|---|---|---|
| 1 | `apis/shared/feature_flags.py` `document_extract_model_id()`; CDK `config.documentExtract.modelId` (`config.ts:2089`) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | env, Runtime only while the feature is on | Structured extraction's constrained call. **Needs Converse strict tool use** |
| 2 | `agents/main_agent/config/constants.py:204` `COMPACTION_SUMMARY_MODEL_ID` | `us.amazon.nova-2-lite-v1:0` | constant | Rolls older turns into the summary. Quality measured: Nova Micro kept ~77% of planted facts and Nova 2 Lite ~99%. The output ceiling is looked up per model (`compaction_summary.py:80`, `_MAX_OUTPUT_TOKENS_BY_MODEL`) |
| 3 | `apis/inference_api/chat/service.py:967` (inline) | `us.amazon.nova-micro-v1:0` | inline literal | Conversation title on the first turn, concurrent with the stream |
| 4 | `apis/shared/tool_summaries/summarizer.py:44` `_MODEL_ID` | `us.amazon.nova-micro-v1:0` | constant | One-line summary of each tool batch, concurrent with the stream |
| 5 | `apis/shared/files/document_digest.py:71` (app-api) **and** CDK `DIGEST_MODEL_ID` (`large-document-prepare-construct.ts:87`, prepare Lambda, IAM scoped to it) | Nova Micro (`us.` in app-api, base id in the Lambda) | env with a default, and a CDK constant | A large document's abstract. **Two copies that must agree** |
| 6 | `constants.py:262` `NOVA_SONIC_MODEL_ID`; `voice_routes.py:503` | `amazon.nova-2-sonic-v1:0` | constant | Voice mode. Only the Sonic family works, and pricing needs its catalog row |

### 2.2 Fallbacks and defaults → remove or derive

| # | Where | Today | Problem |
|---|---|---|---|
| 7 | `constants.py:149` `Defaults.MODEL_ID` (reached via `model_config.py:419`) | Haiku 4.5 `us.*` | The last resort when the catalog has no enabled `isDefault`. Prod catalogs `global.*`, so a turn landing here is unmetered (`routes.py:655–688` says so) |
| 8 | SPA `session/services/model/model.service.ts:63` `DEFAULT_MODEL` | Haiku 4.5 `us.*`, "System Default" | A client-side copy of #7, shown when the catalog list is empty. The backend refuses a model with no catalog row, so a send on it fails |
| 9 | SPA `settings/pages/api-keys/api-keys.page.ts:500` | `us.anthropic.claude-sonnet-4-20250514-v1:0` | The example snippet's model when the user has none. A stale id that may not be granted |
| 10 | TUI `tui/src/aps_tui/config.py:56–58` | Three `us.*` Claude ids | A hard-coded model list in the terminal client, which should come from `GET /models` like the SPA's |

### 2.3 Deploy-time on purpose → keep, but in one place

| # | Where | Model | Why it stays fixed |
|---|---|---|---|
| 11 | `kb_backend/provisioning.py:93`, `kb_backend/records.py:204`, `embeddings/bedrock_embeddings.py:26`, `managed-kb-role-construct.ts:18`, `config.ts:296` | `amazon.titan-embed-text-v2:0` | The vector index's dimensions and every stored vector belong to this model. Changing it is a re-index, not a setting. Worth one shared constant, though: five copies today |
| 12 | `memory-construct.ts:66–67` | IAM wildcards for `anthropic.claude-*` / `amazon.nova-*` | AgentCore Memory's managed strategies choose their own models; we only grant access |
| 13 | `apis/shared/memory/tokens.py:37` `MEMORY_TOKEN_COUNT_MODEL_ID` | `anthropic.claude-haiku-4-5…` | CountTokens for an estimate only; never billed as inference. Already env-overridable |
| 14 | `apis/app_api/telemetry/ttft_probe.py:202` `TTFT_PROBE_MODEL_ID` | env only | Synthetic probe; ops configuration |

### 2.4 Offline and dev tooling → leave as is

| Where | Model | Notes |
|---|---|---|
| `apis/shared/evals/judges.py:56` `DEFAULT_JUDGE_MODEL` | `us.anthropic.claude-sonnet-4-6` | A parameter with a default. The judge must differ from the model under test |
| `scripts/nightly/eval-harness.sh:106` | Haiku/Opus/Sonnet 5.5 | Env-overridable (`EVAL_MODEL_IDS`) |
| `scripts/load-test/*`, `scripts/local-dev/kb-cap-benchmark.py` | various | CLI flags with defaults |

### 2.5 Capability knowledge keyed by model family — not choices

`model_config.py`, `context_window.py`, `compaction_summary.py` (output ceilings),
`observability/prompt_cache.py` (`openai.gpt-5.6`), `bedrock_count_tokens.py`,
`mantle.py`, `bedrock_responses.py` and `aws_limits/bedrock_match.py` encode *what a
model can do*: context windows, caching, transports, CountTokens support. They don't
choose anything, and they stay out of scope. They matter here in one way: a slot that
lets an admin pick a model must check that the model has the capability the slot needs
(§4.2). Moving this knowledge onto catalog rows is separate work.

---

## 3. Slots

A **slot** is one named job the platform runs a model for. Each slot has:
- **an assignment:** a catalog model id, or none;
- **a default:** today's value, used while nothing is assigned;
- **constraints:** what a model needs before it can fill the slot.

| Slot | Default (today's value) | Constraints | Metered today |
|---|---|---|---|
| `extraction` | `DOCUMENT_EXTRACT_MODEL_ID` → Haiku 4.5 | Converse **strict tool use** (structured outputs) | Yes, `C#` row, `callKind: document_extract` |
| `compaction_summary` | Nova 2 Lite | Text in/out; a known output ceiling (else the 4,000 default) | Yes (#2001 prices compaction) |
| `session_title` | Nova Micro | Text in/out, `temperature` only (the call sends no `topP`) | **No.** Side channels were left unmetered because Nova Micro makes them negligible |
| `tool_summary` | Nova Micro | Text in/out | **No**, same as above |
| `document_digest` | Nova Micro | Text in/out. **Two consumers** (app-api and the prepare Lambda); see §4.4 | Check before building |
| `voice` | Nova 2 Sonic | Speech in/out (the row's SPEECH output modality) | Yes, when the Sonic row exists |

**Precedence, per call:** assignment if set and still valid, then the slot's default.
With nothing assigned, every call runs exactly as today. That is what makes the
backend half safe to merge before any UI exists.

---

## 4. Design

### 4.1 Storage

One item in the existing managed-models table: `PK = SYSTEM#MODEL_ASSIGNMENTS`,
`SK = SYSTEM#MODEL_ASSIGNMENTS`. It holds a map of slot → `{modelId, updatedAt,
updatedBy}`.

No new table, index or IAM. The tasks that read the catalog already read this table.
**Check:** that the prepare Lambda can read it before the `document_digest` slot ships.

Alternative considered: an `assignedSlots` field on each catalog row, like `isDefault`.
Rejected, because it lets two rows claim one slot, and every read would have to scan
the catalog to find the holder.

### 4.2 Validation on write

The admin `PUT` refuses an assignment unless:

1. **The model has an enabled catalog row.** Use `resolve_effective_model` and the same
   row predicate as `grants_model_access` (`apis/shared/rbac/model_access.py`), never a
   second check. This closes the unmetered gap for every slot at once.
2. **The model meets the slot's constraints.** These come from the row (modalities,
   provider) and from capability helpers that already exist.
   - Strict tool use for `extraction`: check whether a capability helper exists; if not,
     an allowlist the spec owns.
   - SPEECH output for `voice`.
   - Text in/out for everything else.

   A refusal names the reason.
3. **The id is the effective one.** Retirement is resolved at write time and again at
   read time, so a retired assignment follows its successor the way a turn's model does.

On read, a slot whose assignment is no longer valid falls back to its default. Reasons
include a row deleted or disabled, or a successor that no longer meets the constraints.
The fallback logs once, and the admin page marks the slot.

### 4.3 Read path, and what it must not cost

- **TTFT.** `session_title` and `tool_summary` start concurrently with the stream, and
  nothing waits for them. Their read must still never sit before the first token. Read
  through the config-catalog cache (the `CONFIG_CACHE_ENABLED` path), with a TTL, and
  fill it off the turn path. A miss serves the default and fills in the background.
  `extraction`, `compaction_summary` and `document_digest` run after the model answers
  or out of band. `docs/specs/turn-path-ttft.md` §6 is the regression plan to follow.
- **Prompt cache.** No slot touches the system prompt, `toolConfig` or restored
  history. One subtlety: a new compaction model changes the summary text written at the
  **next** checkpoint. A checkpoint rewrites the prefix anyway, so this costs nothing
  extra. It must never re-summarize an existing checkpoint.
- **Runtime environment budget.** Nothing new goes into the Runtime's environment. The
  defaults stay where they are. `DOCUMENT_EXTRACT_MODEL_ID` keeps its CDK path as the
  `extraction` default.

### 4.4 The digest's two consumers

app-api and the prepare Lambda both make the digest call, and the Lambda's IAM is
scoped to `DIGEST_MODEL_ID`. An admin assignment means one of two things:
- **(a)** widen the Lambda's `bedrock:InvokeModel` to the catalog's models and have it
  read the assignment; or
- **(b)** leave `document_digest` deploy-time, and only unify the two copies into one
  CDK value passed to both.

Recommend **(b) first.** It removes the drift risk for free, and (a) can follow if
anyone asks to change the digest model.

### 4.5 Admin surface

- **app-api:**
  - `GET /admin/models/assignments` returns each slot's assignment, default, effective
    model and validity.
  - `PUT /admin/models/assignments/{slot}` sets or clears one slot.

  Both use `Depends(require_admin)`, and each write is audited as
  `model_assignment.updated`.
- **SPA:** a **Model assignments** section on the Manage Models page. One row per slot,
  showing what it does, its default, a picker limited to the models that pass §4.2, and
  "Use default". The picker comes from the backend, so the SPA never decides
  eligibility.

### 4.6 Metering — a decision this plan needs

`session_title` and `tool_summary` are unmetered because Nova Micro makes them
negligible. Once an admin can assign Opus there, that is no longer true. Options:
- **(a)** meter both slots like extraction (a `C#` row with `callKind`), which costs a
  write per call;
- **(b)** meter only while the slot holds a non-default model;
- **(c)** limit those two slots to models under a per-token price ceiling.

Recommend **(a)**: it is simple, and the release ledger already handles `callKind`. Its
write cost needs measuring first.

---

## 5. Cleanups that need no setting

These come out of §2.2 and §2.3 and can land first, on their own:

1. **`Defaults.MODEL_ID` (#7).** Refuse a turn with a conversational error ("No default
   model is configured") instead of running uncatalogued and unmetered. Its
   `UnmeteredModelCall` path then disappears.
2. **SPA `DEFAULT_MODEL` (#8).** Drop it. An empty catalog shows "No models available"
   rather than a model the backend will refuse.
3. **API-keys example (#9).** Use the first available model, or a placeholder string,
   never a hard-coded id.
4. **TUI model list (#10).** Fetch `GET /models`. This needs the TUI's BFF session,
   which it already holds.
5. **Embedding model id (#11).** One backend constant and one CDK export, imported
   everywhere instead of five literals. It stays deploy-time.
6. **Digest model (#5).** Option (b): one CDK value feeding both consumers.

---

## 6. Build order

| PR | Contents |
|---|---|
| **1** | Cleanups 1–3, 5–6 (§5). No setting, no flag |
| **2** | The store, validation and read path (§4.1–§4.3) with the `extraction` and `compaction_summary` slots, plus the admin routes. Inert until something is assigned |
| **3** | The SPA Model assignments section |
| **4** | `session_title` and `tool_summary`, after §4.6 is decided and metering is in place |
| **5** | `voice`, if anyone needs it (one family today); TUI cleanup 4 alongside |

**Flag.** By the CLAUDE.md rules, the backend read path is inert while nothing is
assigned. The admin routes are half-finished until PR 3 ships the UI. Recommend an
in-development `MODEL_ASSIGNMENTS_ENABLED` (app-api only) gating the write routes and the
SPA section from PR 2 to PR 3. The read path stays unflagged, so turning the flag off
never strands an assignment.

---

## 7. Open questions

1. **Metering of title and tool summaries (§4.6):** (a), (b) or (c)?
2. **Digest:** is (b) (unify, keep deploy-time) enough, or should an admin control it too?
3. **Strict-tool-use capability:** derive it from the catalog row (a new capability
   field), or keep an allowlist in the extraction slot?
4. **Per-environment defaults:** should an assignment carry across environments (seeded
   by the bootstrap), or stay deliberately per deployment like the catalog?

# Agentic Platform Primitives — Enablement Plan

**Status:** Draft / strategy + handoff (for implementation by Fable)
**Author:** Phil Merrell (drafted with Claude)
**Date:** 2026-07-05
**Targets branch:** `develop`
**Supersedes framing of:** `docs/specs/scheduled-agent-runs.md` (was "Oliver") — that doc is now the *detailed design for one primitive*, not the deliverable
**Related explorations this rolls into:**
- **AgentCore Registry** — catalog + discovery + governance seam (`docs/specs/tool-search-token-bloat-strategy.md`, `project_skills_registry_tool_binding`)
- **AgentCore Harness** — the agent-execution loop (`agents/main_agent/`), tracked as an external-pattern scan; here it becomes a *headless run entrypoint*
- Per-user markdown memory (`docs/specs/user-markdown-memory.md`), Admin Skills + RBAC (`docs/specs/admin-skills-rbac-tool-binding.md`), KB sync (`docs/specs/assistant-kb-sync.md`)

---

## 0. Reframe (read first)

We are **not** shipping a chief-of-staff assistant. We are enabling the **primitive layer** underneath it, so that "Oliver" — and a wide range of other non-coding use cases — becomes **configuration on top of platform building blocks, not a bespoke feature**.

The test for every item below is: *is this a clean, reusable primitive that any assistant / skill / schedule / agent could depend on, or is it welded to one feature?* Where a primitive is missing or welded, that's the work.

Two in-flight explorations are the natural home for this, and they carve the space cleanly:

- **Harness** = *how an agent turn runs*. Today a turn only runs from a live, browser-facing chat request that streams SSE. The keystone fundamental is to externalize the loop into a **trigger-agnostic, headless run entrypoint** — "run agent as user U with input P, return a structured result" — that chat, schedules, A2A, and webhooks all share. Everything proactive rides this.
- **Registry** = *how agents/tools/skills/KBs are cataloged, discovered, and governed*. It's the unifying catalog seam over every primitive, and the place audit + policy hooks attach. Deferred today, but the repository seams already exist.

Oliver is demoted to **one validation use case among several** (§5). Build the primitives; prove them with a few use cases; open them to anyone.

---

## 1. Primitive maturity & gap ledger

| Primitive | Where it lives | Maturity | Reusable today? | Gap to a clean primitive |
|---|---|---|---|---|
| **Tools** (multi-protocol catalog, Gateway, per-tool enablement) | `apis/shared/tools/`, `apis/app_api/admin/tools/` | High | **Yes** | Token bloat at scale → dynamic discovery (Registry) |
| **Skills** (instructions + reference files + bound tools + progressive disclosure) | `apis/shared/skills/`, `apis/app_api/*/skills/` | High | Yes, but **dark behind `SKILLS_ENABLED`** | Un-defer; cross-source tool binding (binds local tools only today) |
| **Assistants** (persona + RAG + sharing/visibility) | `apis/shared/assistants/`, `apis/app_api/assistants/` | High | **Yes** | No per-assistant tool binding (only skills have it) |
| **RBAC** (roles → tools/models/skills grants, inheritance, cache) | `apis/shared/rbac/` | High | **Yes** — the uniform gate | Extend the grant vocabulary to new primitives (schedules, KBs) |
| **Quota + Cost** (tiers, soft/hard limits, per-token attribution) | `apis/shared/quota.py`, `apis/shared/costs/` | High | **Yes** (per-user) | No org/team rollup |
| **KB / RAG** (ingestion → S3 vectors → search, sync/re-index) | `apis/app_api/documents/`, `kb_sync/`, `constructs/rag*` | High engine, **welded to Assistants** | **No** — KB is a subordinate of an assistant (`AST#{id}/DOC#{id}`) | KB as a **first-class entity** any assistant/skill/schedule can attach |
| **Memory** (AgentCore Memory; user markdown) | `agents/main_agent/session/`, `apis/app_api/memory/`, `docs/specs/user-markdown-memory.md` | **Low** | **No** — AgentCore Memory is **write-only in cloud**; markdown memory is **spec-only, unbuilt** | Build the read/write user-memory primitive + user-facing CRUD |
| **Scheduled tasks** | — | **Missing** | No | Net-new; `sync_policies` is the template |
| **Delivery / events** | `agents/main_agent/streaming/` (SSE) | **SSE-only** | **No** — nothing durable off the live socket | Async result spine: run-record + notification/inbox (+ optional email/webhook) |
| **Governance: audit / guardrails / data-class / policy** | — (RBAC + quota exist; the rest don't) | **Missing** | No | Audit log, Bedrock Guardrails, PII/FERPA classification, declarative policy — attach at the Registry seam |
| **Registry** (catalog + discovery + governance) | seams: `SkillCatalogRepository`, `ToolCatalogRepository` | **Deferred**, seams present | Not yet | Stand up as the catalog backing; add audit/policy hooks |
| **Harness** (agent run loop) | `agents/main_agent/` (inference-api `/invocations`) | Runs; **not externalized** | **No** — only reachable via live chat | A **headless, trigger-agnostic run entrypoint** |

**Read of the ledger:** the *knowledge and tool* primitives are strong; the *proactive, governed, multi-trigger* primitives are the hole. Three clusters are missing or welded: **(a) headless execution + delivery**, **(b) first-class KB + memory**, **(c) catalog + governance**. Those are the fundamentals.

## 2. The missing fundamentals — address now

Ranked by how much each unblocks. F1 is the keystone; without it, scheduled tasks / proactive agents / A2A-server are all separately-hacked instead of one shared seam.

**F1 — Headless, trigger-agnostic agent-run entrypoint (the Harness fundamental).**
One internal primitive: *run a turn as user U, with input P and a resolved config (assistant/skill/tools/model), without a live browser session, and return a structured result (final message + tool trace + cost).* Today the loop is only reachable via cookie/Bearer chat that streams SSE to a browser; the two hard sub-parts (proven feasible by KB-sync's unattended per-user OAuth path) are **(1) authenticating an unattended caller via workload identity + explicit `user_id`**, and **(2) consuming the run server-side instead of relaying SSE to a browser**. *Unblocks:* scheduled tasks, proactive monitoring, A2A-server, webhooks, "regenerate this" background jobs, eval harnesses. **This is the single highest-leverage build.** Overlaps: this *is* the Harness exploration made concrete. ⚠️ If we expose it as A2A, `CLAUDE.md` is explicit: the first A2A server's `capabilities` **must** include `streaming=True` or clients hang ~40 min.

**F2 — Async result + delivery spine.**
A headless run needs somewhere to land and a way to be noticed. Minimum viable: a **run-record** + materialize the result as a session (`ensure_session_metadata_exists` + `update_session_title` already exist) + a lightweight **notification/inbox** row. *Unblocks:* every async producer — scheduled tasks, KB-sync-complete, long artifacts, future webhooks/email. Today delivery is ephemeral SSE only; a disconnect loses it. Overlaps: pairs with F1; the event schema is a Registry-adjacent contract.

**F3 — Scheduled trigger.**
A thin scheduler (EventBridge rate → dispatcher → F1) modeled on `sync_policies`: sparse "due" GSI, conditional re-arm, `paused_error`, runaway guards. It is *just one trigger* on F1 — deliberately thin. Detailed design in `docs/specs/scheduled-agent-runs.md`. *Unblocks:* briefings, digests, periodic re-summarize, monitoring.

**F4 — KB as a first-class primitive.**
Decouple the (strong) RAG engine from Assistants: a `knowledge_base_id` independent of `assistant_id`, with its own CRUD/permissions/lifecycle, attachable by assistants **and** skills **and** scheduled runs. *Unblocks:* KB-grounded skills, a scheduled "re-summarize this corpus" agent, shared org KBs. Reuse: the ingestion/vector/sync machinery is already general; the change is the ownership model, not the pipeline.

**F5 — Memory primitive.**
Finish `user-markdown-memory.md` (S3 store + DynamoDB manifest + read/write tools + consolidation) and add user-facing `/memory` CRUD. AgentCore Memory stays the write-only summary sink; the markdown layer is the read-time source of truth. *Unblocks:* any persistent, personalized agent (the thing that makes a "chief of staff" more than a prompt). Independent of F1–F3 — parallelizable.

**F6 — Governance floor + Registry-backed catalog.** *Split into two slices, decided (§6-2) to sequence independently:*
- **F6a — Governance floor (pulled forward, foundational).** **Audit log**, a **Bedrock Guardrails** hook at inference, and a **PII/FERPA data-classification checkpoint** on what an unattended run may read/emit. This is a *foundation* concern, not a late add-on: the moment F1 runs unattended **as a user**, it touches user data with no human in the loop — the governance floor must exist *before* that ships. Keep it **backend-invisible** wherever possible (audit + inference-time guardrails + classification), so it adds safety without UX friction.
- **F6b — Registry catalog + dynamic discovery (deferred).** Swap `*CatalogRepository` behind a Registry source (already the seam) for org-wide discovery/governance and to answer tool-search token bloat (index-only discovery). Per `tool-search-token-bloat-strategy.md`, spike this **after** a Tier-1 Gateway-search measurement — so F6b follows, while F6a leads.

*Unblocks:* a regulated-data story for unattended agents (F6a); org-wide discovery + governance across tools/skills/KBs/agents/schedules (F6b). Overlaps: this *is* the Registry exploration, with its governance slice de-coupled and promoted.

## 3. Where the two explorations overlap the primitive work

| Primitive / fundamental | Harness (execution) | Registry (catalog + governance) |
|---|---|---|
| F1 headless run entrypoint | **Is the Harness work** | run-config resolved from the catalog |
| F2 async result / delivery | run-record is a Harness output | event/result schema is a Registry contract |
| F3 scheduled trigger | a trigger *on* the Harness | schedules are catalog entries (discoverable, governed) |
| F4 first-class KB | a run can attach a KB headlessly | KBs become catalog resources |
| F5 memory | memory is loaded inside the Harness run | — |
| F6 catalog + governance | Harness enforces policy at run time | **Is the Registry work** |
| Tools token bloat | Harness promotes matched schemas | Registry is the dynamic-discovery index |

Net: **Harness owns "run," Registry owns "catalog + govern."** Every primitive plugs into one or both. That's the unification Phil asked for — this plan is the Harness/Registry roadmap expressed as concrete primitives.

## 4. Sequenced plan (fundamentals first, use-cases last)

- **Phase A — Execution spine (F1 + minimal F2) + governance floor (F6a).** Headless run entrypoint + run-record + session materialization, **with** the audit-log + guardrails + data-classification floor wired in from the start (because A is the first time we run unattended *as a user* — the floor can't be retrofitted after user data is already flowing). **Validation:** a "Run now" action that executes a prompt as a user off the live socket, passes the governance checkpoint, and lands a result. This is the spike that de-risks everything.
- **Phase B — Scheduled trigger (F3).** Scheduler on top of A. **Validation:** a schedule fires unattended, is governed, and delivers.
- **Phase C — Knowledge & memory (F4 ∥ F5).** Independent, parallelizable; each stands alone.
- **Phase D — Registry catalog + dynamic discovery (F6b).** Registry-backed catalog; also resolves tool-search token bloat. Follows the Tier-1 Gateway-search measurement.
- **Cross-cutting:** cohort gating via a new RBAC capability + a global kill-switch flag (house style), so each primitive ships to prod but scoped, then GAs by granting the capability to the default role — no redeploy. **UX bar:** the foundation buys flexibility, but user-facing surfaces stay minimal — governance is backend-invisible, scheduling is a few fields, defaults are sane.

## 5. Validation use cases (Oliver is just one)

The primitives are proven — and dogfooded — by pointing a few use cases at them:

- **Oliver** — proactive chief-of-staff: scheduled morning briefing (F1+F2+F3), persona + reference files (Assistants + F4/F5), connector tools. The showcase, not the deliverable.
- **Scheduled corpus re-summarize** — a KB-grounded agent that digests a document set weekly (F3 + F4).
- **Weekly status digest** — summarize a user's own activity into a shareable update (F1 + F2).
- **Inbox / meeting-prep triage** — connector tools + memory on a cadence (F1 + F5 + tools).
- **Proactive monitoring** — a schedule that checks a condition and only notifies on change (F2 + F3).

The point: once F1–F6 exist, each of these is **configuration**, not engineering — which is exactly the dogfooding flywheel.

## 6. Decisions & open questions

**Resolved (2026-07-05 — build to these):**

1. ✅ **Harness surface (F1) = minimal internal function, A2A-ready.** Build `run_agent_headless(...)` as an internal primitive first; keep the seam clean so an A2A server / runtime endpoint can front it later without a rewrite. If that A2A surface lands, its `capabilities` **must** include `streaming=True` (`CLAUDE.md`).
2. ✅ **Governance is foundational, split from discovery.** Pull the **F6a governance floor** (audit / guardrails / PII-classification) forward into Phase A — it gates the first unattended-as-user run and can't be retrofitted. Keep **F6b Registry discovery** deferred to Phase D (after the Tier-1 Gateway-search measurement). Prioritize a flexible foundation; keep user-facing surfaces minimal and governance backend-invisible.

**Resolved at the spike decision gate (2026-07-05, post-`harness-entrypoint-spike-findings.md`):**

3. ✅ **Act-as-user auth policy** *(new — surfaced by the spike).* The preferred workload-token front-door path is **provably impossible** (gateway JWT authorizer can't parse the opaque workload blob; SigV4 refused once a JWT authorizer is configured). Chosen path: **platform mints a per-owner Cognito access token** so every downstream layer sees a genuine user token. This means the platform can act as a user unattended for the token's backing lifetime. **Decision:** accept for Phase A, **but gate it behind an explicit "headless-grant" record** (own consent + revocation + lifecycle), not a silent replay of the BFF session — with a documented "must have logged in within N days for the platform to act as you" policy (default ≈ the BFF 30-day cap). This is the FERPA-defensible form.
4. ✅ **F6a floor = role/auth-based, not content-scrubbing** *(revised 2026-07-05).* A headless run executes **as the owner, with the owner's RBAC**, and delivers **only to the owner's own session list** — so it crosses no new access boundary and introduces no new recipient. Governance is therefore the controls we already have: **RBAC (run-as-user)** + the **grant lifecycle** (consent/revocation/N-days — the real FERPA control) + **quota/runaway guards** + a **fail-closed audit** floor. A PII/content-classification pass adds nothing for the deliver-to-self model, so it is **not** a scheduled-runs prerequisite; `check_input`/`classify_output` stay **dormant seams**, relevant only if a run ever delivers to a *different recipient* (e.g. the deferred email feature), governed there. (Supersedes the earlier "PII checkpoint required before scheduled runs" draft.)
5. ✅ **KB decoupling (F4) — defer.** Let scheduled runs **borrow an assistant's KB** for now; first-class KB is not on the Phase A critical path (the spike confirms F1 doesn't need it). Revisit in Phase C.
6. ✅ **Sequencing — proactive-spine-first confirmed.** F1→F2→F3 leads; knowledge/memory (F4/F5) proceed in parallel.

---

## Appendix — what changed from the Oliver framing

The prior draft (`scheduled-agent-runs.md`, née "Oliver — Proactive Chief-of-Staff") specified a *feature*. This doc reframes it as a *primitive-enablement plan*: the scheduling engine is one fundamental (F3) on top of a bigger keystone (F1, the headless Harness entrypoint), the assistant persona is one validation use case (§5), and the whole effort is expressed as the concrete roadmap for the Harness and Registry explorations. Deliver the building blocks; let Oliver — and everything like it — fall out as configuration.

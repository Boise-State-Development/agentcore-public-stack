# Skill Creator — guided skill authoring, and the agent write path it needs

**Status:** Accepted with revisions (2026-10-12). Drafted 2026-07-19. PR-A merged (#1555); PR-B (the tools) built against this revision, with the corrections in §PR-B as built.
**Author:** Phil Merrell (drafted with Claude)
**Targets branch:** `develop`
**Depends on:** [`skills-as-agent-primitive.md`](skills-as-agent-primitive.md) PR-1–PR-5 — merged. [`skill-bundle-import.md`](skill-bundle-import.md) PR-1 (nested paths) and its server-side `SKILL.md` parser — **draft, branch-only**; see D7.
**Related:** [`user-markdown-memory.md`](user-markdown-memory.md) §W1/B3 (`memory_write` — the sole precedent for an agent tool that writes user-owned platform content); [`agentic-platform-primitives.md`](agentic-platform-primitives.md) F1 (headless entrypoint — the prerequisite for the eval half, see D8); [`agent-designer.md`](agent-designer.md) (the authoring surface for Agents, which this deliberately does not extend); [`admin-skills-rbac-tool-binding.md`](admin-skills-rbac-tool-binding.md) §0.4 (the existing drag-and-drop import-prefill flow, which PR-1 rides).
**Prior art:** [Anthropic's open-source `skill-creator`](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md).

### Revision 2026-10-12

The July draft was reviewed against `develop` before implementation. Four things changed underneath it, and one claim in it was wrong:

- **Wrong: "go through `SkillUserService`".** The class is `UserSkillService`, and it lives in `apis/app_api/skills/`, along with the `SkillCatalogService` file machinery it delegates to. The runtime cannot import either one (`tests/architecture/test_import_boundaries.py`). D5 now says the write path moves to `apis.shared` first, which is PR-A.
- **New since July: skill versions and pins** (Shared Projects 3.1). Pins limit the damage of a live edit, but only for bindings that pin. See D9.
- **New since July: Shared Projects bind skills to a project's harness.** Together with Agent invoke-through, a user's own skill can now run for people other than its author. See D9.
- **The approval interrupt exists.** `tool_approval_required` and `MCPExternalApprovalHook` shipped for MCP tools. D10 extends that hook to these tools and closes open question 4.
- **The cost and rollout rules got sharper.** CLAUDE.md now has the prompt-cache contract and the in-development flag rule. See D11 and D12.

Building PR-B turned up five more places where this spec and the code disagreed. They are corrected in place and listed in §PR-B as built.

The phasing is reordered (§Phasing). The service-layer move ships first because every later PR needs it, and it has no agent surface. The content-only skill no longer gates *building* the tools. It gates *widening* them past the pilot.

## Summary

Users can author skills today — `/skills/mine` CRUD, bundle upload, and the My Skills form all shipped in Skills v2 PR-3. What they cannot do is author a *good* one without already knowing what makes a skill trigger reliably and stay lean. Anthropic ships a `skill-creator` skill that closes exactly that gap, and it is worth adopting.

But it is two things welded together, and they have very different fates here:

| Upstream half | Fate in our stack |
|---|---|
| **Authoring methodology** — interview the user, write a description that triggers, structure instructions for progressive disclosure | Ports cleanly. Pure content. Works today with **zero new code**. |
| **Eval harness** — `evals/evals.json`, parallel with-skill vs. baseline runs, `scripts/aggregate_benchmark`, `claude -p` optimization loop, HTML review viewer | Does not port. Assumes a filesystem, subagent spawning, and script execution. Our `scripts/` are **accept-and-inert by design** ([Skills v2 D3](skills-as-agent-primitive.md)), and there is no subagent facility in `ChatAgent`. |

So this spec does two things. It ships the methodology as a platform skill immediately (PR-1, no code), and it specifies the one genuinely missing primitive that makes the experience feel like authoring rather than dictation: **an agent write path into the user's own skills** (PR-2–PR-4). The eval half is scoped out and hung off F1 (D8).

## Why now

Three things just became true at once:

1. Skills v2 PR-5 flipped `SKILLS_ENABLED` on by default and gated the surfaces behind the `skills` capability. GA is now a grant, not a deploy.
2. My Skills works end-to-end but its sidenav entry was just removed (PR #690) pending a navigation decision. A skill-creator is a plausible *answer* to that question — you reach your skills through the thing that helps you make them — which makes this spec partly a discovery-path proposal.
3. `memory_write` established the pattern for an agent tool that writes user-owned content and re-checks permission per call. There is now a house precedent to copy instead of invent.

## D1 — The skill creator is a skill, not a feature

It ships as an admin-catalog skill (`owner_id="system"`, `visibility=ADMIN`), granted to roles like any other. No new page, no new route, no new nav entry.

This is the whole point of the Skills v2 primitive and the cheapest possible test of whether the methodology lands. It also means the rollout controls already exist: grant it to a pilot role, widen to `default`, or revoke — all without a deploy. The rejected alternative — a bespoke "create a skill" wizard in the SPA — would duplicate the My Skills form, couple authoring guidance to a release cycle, and teach us nothing about whether skills are a good delivery vehicle for procedural knowledge.

## D2 — We adapt the content. We do not vendor upstream's SKILL.md

Upstream's instructions tell the agent to spawn parallel subagents, run `scripts/run_loop`, write `iteration-N/` directories, and open an HTML viewer. Dropped in verbatim, our agent will repeatedly attempt operations that are impossible in this runtime, narrate steps that never happen, and produce a first-run experience that reads as broken.

Keep: the intent-capture interview, the description-writing guidance (including the deliberate "pushiness" to combat undertriggering), progressive-disclosure discipline, the "keep prompts lean, explain reasoning" principle, and the instinct to bundle repeated helper content as reference files.

Cut: everything downstream of `evals/`, all script execution, all subagent orchestration, the `claude -p` loop, and the viewer.

Add (ours, not upstream's): our `SKILL.md` frontmatter dialect as `generate_skill_md` emits it, the fact that `allowed-tools` is **advisory only and never enforced** ([Skills v2 D4](skills-as-agent-primitive.md)), the fact that `scripts/` are stored but never run, our caps (50 skills/user, 50 files/skill, 1 MiB/file), and the reload rule from D6.

Attribution and license terms for the derived content are an open question (§Open questions).

## D3 — PR-1 ships with zero new code, by routing through the form

The agent drafts a complete `SKILL.md` — frontmatter and body — in the conversation. The user copies it into the My Skills form, which already parses frontmatter into fields and body into instructions (`my-skill-form.page.ts`; the same import-prefill flow described in [`admin-skills-rbac-tool-binding.md`](admin-skills-rbac-tool-binding.md) §0.4).

That is a real, shippable v0. It tests the methodology — the expensive-to-validate part — before we build any tooling for it. If guided authoring turns out not to produce skills people keep, we learn that for the cost of writing one markdown file.

It is also honest about its seam: the handoff is a copy-paste, and the skill's instructions should say so plainly rather than pretending otherwise.

## D4 — The write path is agent tools executing as the invoking user

This is the missing primitive. Today **no agent tool can write a skill**: nothing but `read_skill_file` touches the skill-resources bucket and that is `get`-only, and no tool in the codebase calls back into app-api over HTTP.

Nor should it. The inference-api boundary means only `/invocations` and `/ping` are reachable through the AgentCore Runtime gateway (restated as a load-bearing invariant in [`invocation-path-refactor.md`](invocation-path-refactor.md) — also branch-only at time of writing), and every existing resource-writing tool — `memory_write`, `create_artifact`, the Word document tools — goes direct to the service layer via boto3. The skill-authoring tools follow that pattern.

Permission model, mirroring `memory_write` exactly: **the tool executes as the invoking user, and the service re-checks ownership on every call.** For skills, ownership *is* the grant (`resolve_owned_skill_ids`), and `require_owned` already collapses "not yours" into a 404. The tool surface is a UX affordance, not the security boundary.

## D5 — Tools take inline text; no new HTTP endpoint is required

The only bundle-file ingress today is `POST /skills/mine/{id}/resources`, **multipart binary upload only**. An agent produces text, not multipart, and — per D4 — does not speak HTTP to app-api at all.

So the write tools call a service-layer method that accepts inline `content: str`. No new route. The SPA keeps its multipart upload path unchanged. Both paths share the same bundle-file code as the admin tier, so a bundle the agent writes has exactly the same layout as one uploaded by hand.

**That shared code has to move first.** In July it all lived in `apis/app_api/skills/`: `UserSkillService` (ownership, caps, id allocation) and the `SkillCatalogService` methods it delegates to for files (caps, type allowlist, manifest, `SKILL.md` projection, orphan GC). Neither is importable from the runtime. PR-A moves them:

- `apis/shared/skills/bundle_files.py` (`SkillBundleFiles`): the file and projection code, with no RBAC dependencies. `SkillCatalogService` composes it and keeps its admin-scoped wrappers (`require_catalog_skill`, role sync), so the admin surface does not change.
- `apis/shared/skills/user_service.py` (`UserSkillService`): moved as is. It now composes `SkillBundleFiles` instead of the whole `SkillCatalogService`. That also drops its transitive dependency on `AppRoleAdminService`, which a runtime tool has no reason to build.
- `UserSkillService.write_text_file(skill_id, kind, filename, content: str, user)`: the inline-text entry point. It encodes UTF-8 and goes through the same `add_resource` code as an upload. It refuses a filename whose allowlisted type is not text (`.png`, `.pdf`), because the agent's text written into one would be a corrupt binary.

Two consequences that are easy to miss and expensive to rediscover:

- **Go through `UserSkillService`, not raw boto3.** It gives you the `SKILL.md` projection, `_gc_orphaned` cleanup and, the sharp edge, the `freshness.invalidate()` call. Skip it and the 10-second TTL cache serves a stale catalog right after the agent writes. The invalidation only reaches the process it runs in: a runtime write clears the runtime's cache, and app-api's copy expires on its own TTL. That is fine, because the SPA reads through app-api and 10 seconds is shorter than any human round trip.
- **Slug the name on the DB→`Skill` mapping.** Per [`skills-v2-pr2-spike-findings.md`](skills-v2-pr2-spike-findings.md), Strands `Skill(...)` does no name validation at all, so a display name the agent invents can produce an invalid skill name that fails silently downstream.

## D6 — A skill written mid-turn is not usable that turn

`build_skills_runtime` runs at agent construction, so the turn's skill set is fixed before the first token. A skill the agent creates at 14:32 does not exist for the agent at 14:33 in the same conversation.

We do not fight this. Re-constructing the agent mid-turn to pick up a just-written skill would invalidate the `skills_hash` cache key, disturb cache points that [the PR-2 spike](skills-v2-pr2-spike-findings.md) verified are preserved, and interact badly with interrupt-resume snapshot replay — a lot of risk for a nicety.

Instead the skill-creator's instructions state it plainly and end the authoring flow with an explicit "start a new conversation to try it" step. Left unstated, this reads as a bug: the user asks the agent to use the skill it just wrote, and it has no idea what they mean.

*Revision 2026-10-12:* a new conversation is not enough by itself. Skills are **opt-in per turn** (Skills v2 D6): the turn's set is the accessible set narrowed by the request's `enabled_skills`, and an empty selection means no skills at all (`_apply_enabled_skills_filter`). A skill the agent just created is accessible but not selected. So the closing step is "turn it on under **Customize → Skills** (`/customize/skills`), then start a new conversation". `my_skill_create`'s result says the same thing, so the instruction does not depend on the skill-creator skill being active.

*Corrected 2026-10-12 (PR-B):* the July text offered "or use its slash command" as an alternative. It is not one. The composer's slash menu lists only skills that are already on (`SkillCommandService` filters to `isSkillShownEnabled`), so a new skill has no slash command until it is turned on. Verified on dev the same day: a freshly created skill reads `isEnabled: false` and the slash menu shows "No skills match" until Customize → Skills turns it on.

## D7 — Nested paths and the server-side parser belong to `skill-bundle-import`, not here

[`skill-bundle-import.md`](skill-bundle-import.md) (draft, on `feature/skill-bundle-import-spec`) already specifies both:

- **Nested bundle paths** (its D4 + §6.4 path validator). Our filename regex forbids separators, so every kind is a flat directory today. Upstream's skill-creator bundles `agents/grader.md` and `eval-viewer/generate_review.py` — keys we literally cannot represent.
- **A server-side `SKILL.md` parser** (its §7.4) — the inverse of `generate_skill_md`, which does not exist; parsing lives only in the SPA form.

Both are genuine prerequisites for a richer skill-creator, and both are **already owned elsewhere**. This spec depends on them and specifies neither. If the bundle-import spec stalls, the nested-path work should move here rather than being duplicated — but that is a re-assignment to make explicitly, not by accident.

Note the ordering consequence: PR-1 and PR-2–PR-4 do **not** need nested paths (flat `references/` is enough for a skill-creator that writes one or two reference files). Only a skill-creator that bundles its own helper tree does.

## D8 — Evals are out of scope, and belong to the headless lane

Comparative eval — run a prompt with and without the skill, grade both, aggregate — needs parallel agent runs and script execution. `ChatAgent` has neither, and `scripts/` are inert by a deliberate security decision we should not trade away for this.

The right home is F1, the headless run entrypoint, whose stated unblock list in [`agentic-platform-primitives.md`](agentic-platform-primitives.md) already names "eval harnesses" — the only mention of evals anywhere in `docs/specs/`. When F1's act-as-user path is production-grade, an eval loop becomes a headless orchestration over it, not a chat-runtime feature.

Recording the dependency and moving on is the correct call. A half-eval in the chat runtime — "here are three prompts, try them yourself" — is worse than none, because it looks like the real thing.

## D9 — An edit reaches everyone the skill runs for, so the tool says who that is

*(Added 2026-10-12.)* In July, an owned skill ran only for its owner. That is no longer true. A user's own skill also resolves:

- for anyone an Agent is shared with, when the Agent binds it (invoke-through, `resolve_invocable_skill_ids` clause 3), and
- for every member of a Shared Project whose harness binds it.

An **unpinned** binding runs the live row. So `my_skill_update` and `my_skill_write_file` change what other people's agents do, on their next turn, without them doing anything. A **pinned** binding (`id@N`, `apis/shared/skills/pins.py`) runs a frozen version, and the edit does not reach it, by design.

The UI has the same exposure, so this is not a new hole. What the agent path changes is speed and attention: a person editing in the My Skills form is looking at the skill, while an agent mid-task may not be. Rules:

- **Before an edit is applied, the tool states its reach.** The edit tools look up where the skill is bound and, if it is bound anywhere unpinned, include that in what the approval prompt shows (D10): "This skill runs unpinned in 2 shared agents and 1 project. They will use the new version on their next turn." Pinned bindings are listed separately as unaffected.
- **No binding lookup on the turn path.** It runs inside the tool call, after the user asked for an edit, never in a `BeforeInvocation`/`BeforeModelCall` hook (TTFT budget).
- **The tools never pin or unpin.** Pinning is a decision for whoever owns the binding (the Agent or project owner), and that is often not the skill's author.

If the reverse lookup ("which bindings name skill X") has no index, PR-B builds it from the owner's own Agents and projects only, since invoke-through needs the Agent owner to be the skill owner. That bounds it to one owner-partition query per kind. If that turns out not to hold for projects, PR-B records the gap rather than scanning.

*As built (PR-B):* it holds for projects, and it is one query, not one per kind. A project's harness is an Agent record (`kind: "project"`) in the same assistants table, and the resolver judges invoke-through on the harness's own `ownerId`, which is the project's creator and is immutable on the record. So every binding that can run skill X for someone else is in X's owner's partition of `OwnerStatusIndex`, harnesses and drafts included, and the lookup (`apis/shared/skills/reach.py`) is exact. There is no gap to record. A binding on another user's Agent can only ever run the skill for its author.

## D10 — Every write asks the user first, through the existing approval interrupt

*(Added 2026-10-12. Resolves open question 4.)* A skill is instructions that persist into every future conversation that turns it on. The agent writing one is the one place in this design where content the agent *read* (a web page, a document, an MCP result) can become instructions it *follows* in later sessions. In other words, it is a persistent prompt-injection channel. `memory_write`'s per-call permission check does not cover this, because the threat is not "wrong user", it is "right user, content they never saw".

So `my_skill_create`, `my_skill_update`, `my_skill_write_file` and `my_skill_delete_file` pause for approval before they run. Reads (`my_skill_list`, `my_skill_read`) do not.

- **Reuse the existing gate, don't build a second one.** `MCPExternalApprovalHook` (`agents/main_agent/session/hooks/tool_approval.py`) already pauses on `BeforeToolCallEvent`, emits `tool_approval_required`, resumes on the user's decision and turns a denial into `cancel_tool`. Its gating set comes from an `ApprovalNamesLookup`. PR-B composes that lookup with a fixed set of built-in names, so the snapshot, the resume route, the `PendingInterrupt` breadcrumb and the SPA card all work unchanged.
- **The card must show what will be written.** The hook already JSON-encodes the tool input for "Inspect arguments", and that is enough for v1. A rendered diff against the current row is a nice-to-have, not a requirement.
- *As built (PR-B):* the SPA card renders the tool input and **not** the event's `message`. So the reach (D9) is the first key of the input the card shows, and is also in `message` for a future card. The hook takes an optional `ApprovalDetailsLookup` that replaces the shown input and message for a call it is about to pause; the tool still receives its real input. Long text arguments are cut to an 8,000-character preview on the card, because the shown input is also persisted as the `PendingInterrupt` breadcrumb on the session row, where a 1 MiB file body would not fit in a DynamoDB item.
- *Where the reach lookup runs (PR-B):* in that details lookup, which the hook awaits only for the three edit tools, at the moment it pauses. That is after the model has already emitted the tool call, so it adds nothing before the first token, and every other tool call costs one set lookup, as before. It is a `BeforeToolCallEvent` callback, not the `BeforeInvocation`/`BeforeModelCall` hooks D9 rules out, because the card is drawn before the tool runs, so the reach has to be known by then. The alternative was to have each tool raise its own interrupt, as `ask_user_question` does, which would compute reach inside the tool body but would leave the shared hook, and its `cancel_tool` on decline, for create only.
- **The `confirm=true` pattern is rejected here.** `set_default_model` gates on a model-supplied flag. That is fine for a reversible setting, but it is exactly the control prompt injection defeats, because the injected content can tell the model the user already confirmed.

## D11 — What authoring costs against the prompt cache

*(Added 2026-10-12.)* The tools are bounded by the prompt-cache contract (CLAUDE.md):

- **Tool specs.** Seven specs add roughly 1–1.5k tokens to `toolConfig` on every turn, for every user who has them. *Measured (PR-B):* **1,303 tokens** on Haiku 4.5 (Bedrock CountTokens, the seven specs against a one-tool baseline). That is acceptable only because they are not default-on: a single catalog id (D12) provisions them, so a user who never authors skills pays nothing. Their order is fixed at the source like every other injected tool.
- **Creating a skill changes no prefix.** Skills are opt-in per turn (D6 revision), so a new skill is not in anyone's `<available_skills>` until someone turns it on, and that is a deliberate change.
- **Editing an enabled skill's name or description re-writes the prefix once.** Those fields are in `<available_skills>` in the system prompt. Editing only the instructions or a reference file does not touch the prefix, because those load through tool results.
- **The model can read stale instructions in history.** If the skill was activated earlier in the session, its *old* instructions remain in that `skills` tool result. The update tool's result says so ("re-activate the skill to load the new instructions"), rather than letting the agent act on both versions.
- **`my_skill_read` returns the whole skill.** It is bounded by the author's own caps (instructions on the row, files at 1 MiB each). It does not return file bodies; those go through a per-file read, which the tool-result offloader handles as usual. That resolves open question 2.

## D12 — An in-development flag, and the runtime gets write access to one bucket

*(Added 2026-10-12.)*

- **Flag.** `SKILL_AUTHORING_ENABLED` (backend, read by the Runtime) and `CDK_SKILL_AUTHORING_ENABLED`, default off: only `"true"` enables it, and an empty string counts as unset. It ANDs with `skills_enabled()` and the tool grant. There is no frontend flag in v1, because the tools reuse the existing approval card and there is no new UI. PR-A ships no agent surface, so it needs no flag.
- **IAM.** The Runtime role already has `PutItem`/`UpdateItem` on the app-roles table, where skill rows live, through `DynamoDBTableAccess`. On the skill-resources bucket it has only `s3:GetObject` and `s3:ListBucket` (`SkillResourcesBucketRead`, "app-api owns writes"). PR-B adds `s3:PutObject` and `s3:DeleteObject`, scoped to `skills/*`, with the comment updated to say why the runtime now writes. That is one statement on an existing policy, so no new resources (the stack resource budget is unaffected).
- *As built (PR-B):* the new statement is `SkillResourcesBucketWrite`, beside the unchanged read statement, on the same role policy. Pinned versions' frozen files live under `skill-versions/`, outside the grant, so nothing the Runtime does can change what a pin runs. The Runtime gets `SKILL_AUTHORING_ENABLED` only while the flag is on (the code reads unset as off), so an environment that leaves it off spends no environment-budget bytes on it.

## Data model

**No schema changes.** Skills written by the agent are ordinary user-tier rows: `owner_id = <user_id>`, `visibility = PRIVATE`, server-allocated `skill_id`, same `resources[]` manifest, same S3 bundle layout. That is the point — an agent-authored skill and a hand-authored one are indistinguishable at rest, so every existing surface (My Skills, the picker, invoke-through, delete) works on them with no changes.

The caps in `SkillCatalogService` apply unchanged and become failure modes an agent can actually hit in a loop: `MAX_SKILLS_PER_USER = 50` (surfaces as 409), `MAX_RESOURCES_PER_SKILL = 50`, `MAX_RESOURCE_BYTES = 1_048_576`. The tools must return these as legible errors the model can recover from, not raw exceptions.

## Tools

Named against the `/skills/mine` surface so they never read as siblings of the existing `read_skill_file`, which does something different (reads reference files of skills *active in the current turn*, structurally scoped so no id can escape the turn's set).

| Tool | Shape | Approval (D10) | Notes |
|---|---|---|---|
| `my_skill_list` | `() -> [{skill_id, display_name, description, status}]` | no | Owned only, via GSI4 |
| `my_skill_read` | `(skill_id) -> {metadata, instructions, resources[]}` | no | Ownership re-checked; not-yours = not-found. Manifest only, no file bodies (D11) |
| `my_skill_read_file` | `(skill_id, filename) -> str` | no | *Added 2026-10-12.* One owned file's text, so an edit can start from the current content. `read_skill_file` cannot do this: it only reaches skills enabled for the turn |
| `my_skill_create` | `(display_name, description, instructions, category?, skill_metadata?) -> skill_id` | yes | Server allocates `skill_id` with collision suffixing. Result tells the user how to turn the skill on (D6) |
| `my_skill_update` | `(skill_id, ...partial) -> ok` | yes, with reach (D9) | Re-projects `SKILL.md`. Result warns that earlier activations in history are stale (D11) |
| `my_skill_write_file` | `(skill_id, kind, filename, content: str) -> ok` | yes, with reach (D9) | Inline text (D5); upsert by `(kind, filename)`; text types only |
| `my_skill_delete_file` | `(skill_id, filename) -> ok` | yes, with reach (D9) | Scoped to one skill's bundle |

**Deliberately absent: whole-skill delete.** User-tier delete is a hard delete plus S3 purge with no undo. That stays a UI action behind human intent. An agent that can create skills in a loop should not also be able to destroy them in one.

## Gating

Follow the Word-document pattern (`_build_word_document_tools` in `apis/inference_api/chat/routes.py`) rather than the catalog: a context-bound factory built per request, capturing the user by closure (the runtime does not populate `ToolContext`), gated on a single tool id in `enabled_tools` that provisions the whole toolset — `SKILL_AUTHORING_TOOL_IDS = {"my_skill_create"}` in `apis/shared/tools/injected.py`, mirroring `WORD_DOCUMENT_TOOL_IDS`.

*Revision 2026-10-12, agent cache:* an injected family not in `KEY_DESCRIBED_INJECTED_TOOL_IDS` bypasses the agent cache on every turn it is enabled. That means a full `initialize()`, a Memory restore and a prefix re-write. The factory therefore closes over `(session_id, user_id, user_email)` only, all of which the cache key describes, and PR-B adds the family to the key-described set under the guard tests in `tests/shared/test_injected_tool_cache_eligibility.py`. The binding-reach lookup (D9) runs inside the tool call, so the closure never needs more. The catalog seed is `enabledByDefault: false`.

Further gates, all cheap:

- `skill_authoring_enabled()` (D12), the in-development flag.

- `skills_enabled()` — no authoring tools when the kill switch is off, matching how the router unmounts.
- ~~The `skills` capability.~~ *Corrected (PR-B):* there is no such capability any more. It was removed because a capability id cannot be granted from the admin roles UI (see `skills_enabled()` and the `apis/app_api/skills/routes.py` docstring), and the My Skills routes now require only a session. The per-user gate is the `my_skill_create` grant itself.

One caveat on that grant, recorded rather than fixed here: the inference API does not intersect a request's `enabled_tools` with the caller's RBAC grants (a pre-existing gap that applies to every tool, not just this family). A crafted request could therefore provision these tools without the grant, while the flag is on. That is not a privilege escalation for this family: the tools only write the caller's own skills, which any signed-in user can already do in My Skills, and every write still pauses for the caller's approval. The grant is a provisioning and cost control here, not the security boundary.

## Frontend

PR-1 needs nothing. PR-2–PR-4 need nothing strictly, but two small things make the loop feel finished:

- A link from the conversation to the created skill (`/my-skills/{id}`), so the D6 "new conversation" step lands somewhere concrete.
- Resolving the My Skills navigation question (PR #690). Guided authoring is a plausible entry point — you reach your skills through the thing that made them — but that is a product call, not a consequence of this spec.

## Phasing

*Revised 2026-10-12.* The service-layer move goes first. Every later PR needs it, it has no agent surface, and it fixes a boundary the July draft got wrong. The content-only skill (old PR-1) can ship any time in parallel. Its pilot now gates *widening* the tools (PR-D), not *building* them, because editing an existing skill is the case copy-paste serves worst, and the pilot cannot measure that.

- **PR-A — spec revision + service layer.** This revision, plus D5's move: `SkillBundleFiles` and `UserSkillService` into `apis.shared.skills`, `SkillCatalogService` composing the former, and `UserSkillService.write_text_file` for inline text. Behavior-preserving for both existing tiers (their route and service suites pass unchanged, apart from import paths and the module that holds the caps). No flag, no agent surface, no infra.
- **PR-B — the tools.** The seven `my_skill_*` factories; `SKILL_AUTHORING_TOOL_IDS`, in the key-described set; the D12 flag and the S3 write grant; the D10 approval lookup; the D9 reach lookup; legible errors for caps and permissions. Catalog seed `enabledByDefault: false`.
- **PR-C — the skill.** The adapted `skill-creator` SKILL.md (D2), written for the tools and including the D6 enable step, seeded as an admin-catalog skill and granted to a pilot role. The copy-paste flow (D3) remains its fallback when the tools are not granted.
- **PR-D — GA by grant.** Widen once the pilot says the skills produced are worth keeping. Promoting the flag to default-on is its own change, per the feature-flag rules.

The original July phasing is kept below for the record.

- **PR-1 — the skill, content only.** Author the adapted `skill-creator` SKILL.md; seed as an admin-catalog skill; grant to a pilot role. No code. Ships the methodology and the copy-paste flow (D3).
- **PR-2 — service layer.** Inline-text write method on `SkillUserService` + ownership re-check + `freshness.invalidate()`. Unit-tested against the caps. No agent surface yet.
- **PR-3 — the tools.** `my_skill_*` factories, `SKILL_AUTHORING_TOOL_IDS` gate, wired into the `extra_tools` chain. Legible cap/permission errors.
- **PR-4 — the skill, rewritten for the tools.** Replace the copy-paste handoff with direct authoring; add the D6 reload step.
- **PR-5 — GA by grant.** Widen to `default` once the pilot says the skills produced are worth keeping.

PR-1 is independently valuable and independently revertable. If it lands and nobody uses the output, PR-2–PR-5 should not be built. *(Superseded 2026-10-12. See the revised phasing above.)*

## Non-goals (v1)

- Eval/benchmark tooling of any kind (D8).
- Script execution. `scripts/` stay accept-and-inert.
- Nested bundle paths and server-side `SKILL.md` parsing — depended upon, owned by [`skill-bundle-import.md`](skill-bundle-import.md) (D7).
- Authoring *admin-catalog* skills via the agent. User tier only; the admin catalog is governed content.
- Authoring Agents. That is [`agent-designer.md`](agent-designer.md)'s surface, and conflating the two would put a second authoring path on the same primitive.
- Sharing/publishing flows. An agent-authored skill is `PRIVATE` like any other; sharing rides existing invoke-through.

## Open questions

1. **Licensing and attribution.** Upstream `skill-creator` is Anthropic's open-source work. D2 makes ours a derivative-by-methodology rather than a copy, but we should confirm the license terms and decide what attribution the shipped skill carries.
2. **Does the agent need to read the skill it is editing?** `my_skill_read` returns full instructions, which for a large skill is a meaningful chunk of context. Worth checking against [`tool-search-token-bloat-strategy.md`](tool-search-token-bloat-strategy.md) before defaulting to full-body reads. *Resolved 2026-10-12 by D11: yes, but `my_skill_read` returns the manifest without file bodies, and `my_skill_read_file` reads one file at a time.*
3. **Should PR-1 be admin-catalog or a seeded user skill?** D1 says admin-catalog, but seeding a *copy* into each user's tier would let them edit the creator itself. Probably wrong — divergent copies, no upgrade path — but worth one round of thought.
4. **Prompt-injection surface.** [`skill-bundle-import.md`](skill-bundle-import.md) §6.10 is honest that a skill bundle is by construction instructions a model will follow. Agent-authored skills are self-authored so the trust story is better, but a skill written from content the agent fetched off the web is not. Does `my_skill_write_file` need provenance guidance in the skill's own instructions? *Resolved 2026-10-12 by D10: every write pauses for user approval through the existing interrupt, and the card shows what will be written.*
5. **Is this the My Skills entry point?** Raised under Frontend. A product call.

## PR-B as built

What PR-B found where this spec and the code disagreed, all corrected above:

1. **D6:** a new skill has no slash command until it is on, so `my_skill_create`'s result names Customize → Skills and a new conversation, with no slash-command alternative.
2. **D9:** the reverse lookup is exact, not bounded-with-a-gap. A harness is an Agent in its creator's partition, so one owner-partition query covers Agents and projects.
3. **D10:** the SPA card shows the tool input, not the message, so the reach goes in the input. The lookup runs when the hook pauses one of the three edit tools, which is after the model emits the call and before the tool runs.
4. **Gating:** the `skills` capability no longer exists. The grant is `my_skill_create`, with the RBAC-intersection caveat above.
5. **D11:** the seven specs measure 1,303 tokens.

Implementation map: tools in `agents/builtin_tools/skill_authoring/tools.py` (synchronous, so Strands runs them in a worker thread and `UserSkillService`'s blocking boto3 never stalls the event loop); the card in `.../approval.py`; reach in `apis/shared/skills/reach.py`; the factory `_build_skill_authoring_tools` in `apis/inference_api/chat/routes.py`; the composed gate in `BaseAgent._build_mcp_external_approval_hook`.

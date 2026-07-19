# Skill Creator — guided skill authoring, and the agent write path it needs

**Status:** Draft / proposal (2026-07-19)
**Author:** Phil Merrell (drafted with Claude)
**Targets branch:** `develop`
**Depends on:** [`skills-as-agent-primitive.md`](skills-as-agent-primitive.md) PR-1–PR-5 — merged. [`skill-bundle-import.md`](skill-bundle-import.md) PR-1 (nested paths) and its server-side `SKILL.md` parser — **draft, branch-only**; see D7.
**Related:** [`user-markdown-memory.md`](user-markdown-memory.md) §W1/B3 (`memory_write` — the sole precedent for an agent tool that writes user-owned platform content); [`agentic-platform-primitives.md`](agentic-platform-primitives.md) F1 (headless entrypoint — the prerequisite for the eval half, see D8); [`agent-designer.md`](agent-designer.md) (the authoring surface for Agents, which this deliberately does not extend); [`admin-skills-rbac-tool-binding.md`](admin-skills-rbac-tool-binding.md) §0.4 (the existing drag-and-drop import-prefill flow, which PR-1 rides).
**Prior art:** [Anthropic's open-source `skill-creator`](https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md).

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

So the write tools call a service-layer method that accepts inline `content: str`. No new route. The SPA keeps its multipart upload path unchanged; both converge on the same `SkillCatalogService` file machinery that the user and admin tiers already share, so bundles written by the agent are byte-identical in layout to bundles uploaded by hand.

Two consequences that are easy to miss and expensive to rediscover:

- **Go through `SkillUserService`, not raw boto3.** You get the `SKILL.md` S3 write-through projection, `_gc_orphaned` cleanup, and — the sharp edge — the `freshness.invalidate()` call. Bypass it and the 10-second TTL cache serves a stale catalog immediately after the agent writes.
- **Slug the name on the DB→`Skill` mapping.** Per [`skills-v2-pr2-spike-findings.md`](skills-v2-pr2-spike-findings.md), Strands `Skill(...)` does no name validation at all, so a display name the agent invents can produce an invalid skill name that fails silently downstream.

## D6 — A skill written mid-turn is not usable that turn

`build_skills_runtime` runs at agent construction, so the turn's skill set is fixed before the first token. A skill the agent creates at 14:32 does not exist for the agent at 14:33 in the same conversation.

We do not fight this. Re-constructing the agent mid-turn to pick up a just-written skill would invalidate the `skills_hash` cache key, disturb cache points that [the PR-2 spike](skills-v2-pr2-spike-findings.md) verified are preserved, and interact badly with interrupt-resume snapshot replay — a lot of risk for a nicety.

Instead the skill-creator's instructions state it plainly and end the authoring flow with an explicit "start a new conversation to try it" step. Left unstated, this reads as a bug: the user asks the agent to use the skill it just wrote, and it has no idea what they mean.

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

## Data model

**No schema changes.** Skills written by the agent are ordinary user-tier rows: `owner_id = <user_id>`, `visibility = PRIVATE`, server-allocated `skill_id`, same `resources[]` manifest, same S3 bundle layout. That is the point — an agent-authored skill and a hand-authored one are indistinguishable at rest, so every existing surface (My Skills, the picker, invoke-through, delete) works on them with no changes.

The caps in `SkillCatalogService` apply unchanged and become failure modes an agent can actually hit in a loop: `MAX_SKILLS_PER_USER = 50` (surfaces as 409), `MAX_RESOURCES_PER_SKILL = 50`, `MAX_RESOURCE_BYTES = 1_048_576`. The tools must return these as legible errors the model can recover from, not raw exceptions.

## Tools

Named against the `/skills/mine` surface so they never read as siblings of the existing `read_skill_file`, which does something different (reads reference files of skills *active in the current turn*, structurally scoped so no id can escape the turn's set).

| Tool | Shape | Notes |
|---|---|---|
| `my_skill_list` | `() -> [{skill_id, display_name, description, status}]` | Owned only, via GSI4 |
| `my_skill_read` | `(skill_id) -> {metadata, instructions, resources[]}` | Ownership re-checked; not-yours = not-found |
| `my_skill_create` | `(display_name, description, instructions, category?, skill_metadata?) -> skill_id` | Server allocates `skill_id` with collision suffixing |
| `my_skill_update` | `(skill_id, ...partial) -> ok` | Re-projects `SKILL.md` |
| `my_skill_write_file` | `(skill_id, kind, filename, content: str) -> ok` | Inline text (D5); upsert by `(kind, filename)` |
| `my_skill_delete_file` | `(skill_id, filename) -> ok` | Scoped to one skill's bundle |

**Deliberately absent: whole-skill delete.** User-tier delete is a hard delete plus S3 purge with no undo. That stays a UI action behind human intent. An agent that can create skills in a loop should not also be able to destroy them in one.

## Gating

Follow the Word-document pattern in `apis/inference_api/chat/routes.py:456` rather than the catalog: a context-bound factory built per request, capturing `user_id` by closure (the runtime does not populate `ToolContext`), gated on a single tool id in `enabled_tools` that provisions the whole toolset — `SKILL_AUTHORING_TOOL_IDS = {"my_skill_create"}`, mirroring `WORD_DOCUMENT_TOOL_IDS`.

Two additional gates, both cheap:

- `skills_enabled()` — no authoring tools when the kill switch is off, matching how the router unmounts.
- The `skills` capability. Note the existing asymmetry: the capability gates the app-api surfaces but deliberately **not** the runtime, so invoke-through keeps working for users without it. Authoring is a *write* into the user's own catalog and is the app-api-shaped side of that line, so it should honor the capability even though it executes in the runtime.

## Frontend

PR-1 needs nothing. PR-2–PR-4 need nothing strictly, but two small things make the loop feel finished:

- A link from the conversation to the created skill (`/my-skills/{id}`), so the D6 "new conversation" step lands somewhere concrete.
- Resolving the My Skills navigation question (PR #690). Guided authoring is a plausible entry point — you reach your skills through the thing that made them — but that is a product call, not a consequence of this spec.

## Phasing

- **PR-1 — the skill, content only.** Author the adapted `skill-creator` SKILL.md; seed as an admin-catalog skill; grant to a pilot role. No code. Ships the methodology and the copy-paste flow (D3).
- **PR-2 — service layer.** Inline-text write method on `SkillUserService` + ownership re-check + `freshness.invalidate()`. Unit-tested against the caps. No agent surface yet.
- **PR-3 — the tools.** `my_skill_*` factories, `SKILL_AUTHORING_TOOL_IDS` gate, wired into the `extra_tools` chain. Legible cap/permission errors.
- **PR-4 — the skill, rewritten for the tools.** Replace the copy-paste handoff with direct authoring; add the D6 reload step.
- **PR-5 — GA by grant.** Widen to `default` once the pilot says the skills produced are worth keeping.

PR-1 is independently valuable and independently revertable. If it lands and nobody uses the output, PR-2–PR-5 should not be built.

## Non-goals (v1)

- Eval/benchmark tooling of any kind (D8).
- Script execution. `scripts/` stay accept-and-inert.
- Nested bundle paths and server-side `SKILL.md` parsing — depended upon, owned by [`skill-bundle-import.md`](skill-bundle-import.md) (D7).
- Authoring *admin-catalog* skills via the agent. User tier only; the admin catalog is governed content.
- Authoring Agents. That is [`agent-designer.md`](agent-designer.md)'s surface, and conflating the two would put a second authoring path on the same primitive.
- Sharing/publishing flows. An agent-authored skill is `PRIVATE` like any other; sharing rides existing invoke-through.

## Open questions

1. **Licensing and attribution.** Upstream `skill-creator` is Anthropic's open-source work. D2 makes ours a derivative-by-methodology rather than a copy, but we should confirm the license terms and decide what attribution the shipped skill carries.
2. **Does the agent need to read the skill it is editing?** `my_skill_read` returns full instructions, which for a large skill is a meaningful chunk of context. Worth checking against [`tool-search-token-bloat-strategy.md`](tool-search-token-bloat-strategy.md) before defaulting to full-body reads.
3. **Should PR-1 be admin-catalog or a seeded user skill?** D1 says admin-catalog, but seeding a *copy* into each user's tier would let them edit the creator itself. Probably wrong — divergent copies, no upgrade path — but worth one round of thought.
4. **Prompt-injection surface.** [`skill-bundle-import.md`](skill-bundle-import.md) §6.10 is honest that a skill bundle is by construction instructions a model will follow. Agent-authored skills are self-authored so the trust story is better, but a skill written from content the agent fetched off the web is not. Does `my_skill_write_file` need provenance guidance in the skill's own instructions?
5. **Is this the My Skills entry point?** Raised under Frontend. A product call.

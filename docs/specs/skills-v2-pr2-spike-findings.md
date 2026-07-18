# Skills v2 PR-2 — AgentSkills spike gate findings

**Decision: GO ✅** — wire the Strands `AgentSkills` plugin into `ChatAgent`.

Spike run against the pinned `strands-agents==1.48.0` (the spec assumed 1.47.0; the pin
was bumped for the cachePoint-after-document fix — `AgentSkills`/`Skill` are present and
unchanged in 1.48.0). Harness: `scratchpad/skills_v2_spike.py`, 20/20 checks pass. It
builds the Strands `Agent` exactly as `agent_factory.py` does (`CountTokensBedrockModel`,
`SequentialToolExecutor`, `caching_enabled=True`) plus `plugins=[AgentSkills(...)]`, then
fires the `BeforeInvocation` hook and the activation/state paths directly (no network).

## Gate items (spec §8)

| Gate | Result | Evidence |
|---|---|---|
| Plugin composes with our `Agent` construction | ✅ | `skills` activation tool auto-registers; `BeforeInvocation` hook fires through the agent's plugin registry |
| Prompt injection composes with our prompt assembly | ✅ | `<available_skills>` (names + descriptions = L1 disclosure) injected; original prompt preserved; idempotent across turns (single block) |
| Cache points preserved | ✅ | See "cache-point path" below — the block-level path preserves a `cachePoint` block and never duplicates it |
| `skills_hash` discrimination | ✅ | Narrowing the effective skill set changes the injected XML → the effective set (already the `skills_hash` input) drives the prompt |
| `agent.state` persistence survives the session manager | ✅ | `SessionAgent.from_agent` serializes `agent.state.get()` (incl. the plugin's `agent_skills` key); `initialize` restores via `agent.state = AgentState(...)`; activated skills round-trip; resume re-injection idempotent |

## Key mechanics confirmed

- **We always get the cache-point-safe path.** `split_system_prompt()` turns even a plain
  **string** `system_prompt` (our construction) into `[{"text": prompt}]`, so
  `agent.system_prompt_content` is never `None` and the plugin's `_on_before_invocation`
  always takes the **block-level** injection path (remove-prior-block + append), which
  preserves cache points and other structured blocks. The string-manipulation fallback is
  effectively dead code for us.
- **Injection is idempotent per agent.** The plugin tracks `last_injected_xml` in
  `agent.state["agent_skills"]` and removes it before re-appending, so a cached agent reused
  across turns never accumulates duplicate `<available_skills>` blocks. For a stable skill
  set the injected bytes are stable turn-to-turn → prompt cache stays warm.
- **State round-trips through our session manager unchanged.** `TurnBasedSessionManager`
  only overrides `initialize()` to layer compaction and calls `super().initialize()`; the
  Strands `RepositorySessionManager` `sync_agent`/`initialize` pair carries `agent.state`
  (including `agent_skills.activated_skills` / `last_injected_xml`) via `SessionAgent.state`.

## Caveats to carry into the PR-2 build (non-blocking)

1. **Slugged `Skill.name`.** The `Skill(...)` constructor does **no** name validation
   (validation only fires on `from_content`/`from_file`/`from_url`). A human-friendly DB
   name like `"Web Research"` constructs fine and is activatable by that exact string. But
   `Skill.name` is (a) the injected label, (b) the `skills` tool activation key, and (c) the
   agentskills.io directory/`SKILL.md` name for the S3 projection + harness portability
   (spec §4). **Map DB → `Skill` with a slug** (lowercase-hyphen, matching
   `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`) as `Skill.name`, carrying the human title in
   `description`/`metadata`. Derive the slug from the skill id or a stored slug field.
2. **Benign resume warning.** On a cross-container / cache-miss resume the agent's base
   prompt is rebuilt fresh while the restored `agent.state` still holds a stale
   `last_injected_xml`; the plugin logs `unable to find previously injected skills XML in
   system prompt, re-appending` and re-appends. Output stays correct and single-block — this
   is expected log noise in cloud (Memory restore is degraded / the in-process agent cache is
   the real cross-turn continuity), not an error.
3. **Cloud state continuity is best-effort, and that's fine.** Even if `agent.state` is not
   restored in cloud (write-only Memory), the skill set is rebuilt from the effective records
   each turn and `<available_skills>` is re-injected; the model re-activates as needed
   (L2 disclosure is per-turn ephemeral, and activated instructions already live in message
   history as a tool_result). No hard dependency on state restore.

## What PR-2 now proceeds to build (spec §8)

Wire `AgentSkills` into `ChatAgent` (conditional plugin when the effective skill set is
non-empty), add the thin `read_skill_file` S3 adapter, retire `SkillAgent` +
`skill_registry`/`skill_tools` disclosure, and normalize the S3 layout with a `SKILL.md`
write-through projection. On a no-go this doc would instead record the fallback to a
fold-free homegrown dispatcher — not needed.

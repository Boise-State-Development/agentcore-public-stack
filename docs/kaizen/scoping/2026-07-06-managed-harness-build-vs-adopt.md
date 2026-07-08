# Build vs. adopt: AWS AgentCore managed **Harness** for the headless/scheduled lane

**Date:** 2026-07-06 · **Status:** spike proposed (not started) · **Trigger:** while dogfooding scheduled runs, Phil asked whether we use the AWS AgentCore *Harness* feature (we don't — we use the lower-level Runtime).

## TL;DR

The AWS **managed Harness** (GA, [devguide/harness.html](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness.html)) is a config-not-code Strands orchestration loop running *inside* Runtime. We currently use the layer **below** it — AgentCore **Runtime** (bring-your-own container = our `inference-api`, our own loop). Our internal `apis/shared/harness/` (`run_agent_headless`) is a **naming collision**, not the AWS product.

**Recommendation:** do **not** replace the interactive chat stack — it's differentiated by exactly the extension points the managed Harness forbids (`Hooks ❌`, `Choice of framework ❌`, non-agent-loop patterns ❌). **Do spike** the managed Harness as the backing for the **headless / scheduled / proactive lane** (the F1/F3 primitives we just shipped), where it's a real fit and would hand us managed memory + versioned endpoints + Step Functions composition for free. The **export-to-code** escape hatch means a "yes" is not a lock-in bet.

## What the managed Harness is

A managed agent loop where model / tools / skills / memory / limits are **configuration**. Runs in an isolated Firecracker microVM per session with filesystem + shell, auto observability, immutable versions + named endpoints, execution limits, context-truncation strategies, and an `InvokeHarness` Step Functions state. Powered by Strands. Inbound auth is **SigV4 or OAuth/JWT** (custom JWT authorizer → OIDC discovery URL + `allowedClients`); per-user identity threading (and token-vault on-behalf-of) requires the **OAuth inbound path**.

## Where it fits us (feature-grid reading, harness-vs-runtime)

| Lane | Verdict | Why |
|---|---|---|
| **Interactive chat** (`inference-api`) | **Keep building (build)** | Depends on `Hooks` (OAuth-consent, tool-approval, RBAC tool-fold, quota, context-attribution), a custom loop (compaction/`session_title`/interrupted-turn/concurrent streaming), MCP Apps UI hosting, and our rich SSE contract — none configurable in the managed Harness. |
| **Headless / scheduled / proactive** (`run_agent_headless` + dispatcher/worker) | **Spike (adopt candidate)** | New, config-shaped, doesn't need MCP Apps UI or the full SSE vocabulary; would inherit managed memory + versioned endpoints + Step Functions. Maps directly onto the **F1 Harness** fundamental. |

## Pros — what adopting unlocks (gaps we hand-build or lack)

- **Managed memory that reads in cloud** — fixes the "AgentCore Memory is write-only in cloud" degradation and hands us long-term memory (semantic / summarization / user-preference / episodic, `actorId`-scoped) as config: a large chunk of the **F5** gap.
- **Model flexibility + mid-session provider switch** (Bedrock/OpenAI/Gemini/LiteLLM) by config.
- **Immutable versions + named endpoints + instant rollback** — ops maturity we approximate with ECR tags + `update-function-code` (the exact fragility that bit the scheduled-runs deploy).
- **`InvokeHarness` Step Functions state** — clean multi-step composition for proactive/scheduled pipelines (F2/F3 spine, managed).
- **Auto unified observability** (X-Ray/CloudWatch) — app logs weren't flowing cleanly to the runtime log group.
- **Execution limits + truncation strategies** as config vs our custom compaction.
- **Low lock-in escape hatch** — `agentcore export harness` generates a normal Strands Runtime agent we own and can self-host (Lambda/ECS/K8s). Adopt-to-prototype, export-when-stuck is viable.
- **Our identity model survives** — OAuth-inbound threads the end-user JWT and reads user-scoped tokens from the **same AgentCore Identity token vault** we already use; our headless path already mints a Cognito bearer.

## Cons — what it can't do that we rely on

- **`Hooks ❌`** — our OAuth-consent / tool-approval / RBAC-fold / quota / context-attribution enforcement is hook-based.
- **`Choice of agent framework ❌` / custom loop** — our loop's compaction, `session_title`, interrupted-turn, per-session concurrent streaming aren't configurable.
- **MCP Apps (SEP-1865) UI hosting** — not a Harness concept; it streams plain Converse `toolUse` frames.
- **Our SSE event contract** — the SPA depends on our vocabulary; full adopt = rewrite the streaming contract.
- **RBAC-resolved per-user tool sets + quota/cost** — Harness offers `allowedTools` globs (per-invoke) + Gateway Cedar policies; coarser than our per-user RBAC + quota tiers + cost rollups.
- **Newly GA** (past training cutoff) — treat maturity / pricing / quotas as spike unknowns.

## The spike — three questions decide it

Scope: prototype an `InvokeHarness`-backed path for one real schedule and answer:

1. **RBAC → `allowedTools`.** Does our per-user RBAC-resolved tool set reduce cleanly to Harness `allowedTools` glob patterns (+ Gateway Cedar policies) per invocation, without the hook-based enforcement?
2. **Per-user connector tokens.** Do our vaulted 3LO connector tokens resolve through Harness **OAuth-inbound + AgentCore Identity** (on-behalf-of), acting as the schedule owner, exactly as `run_agent_headless` does today?
3. **Acceptable losses on the headless path.** Is giving up MCP Apps UI + our SSE contract fine *for headless runs specifically* (hypothesis: yes — a scheduled run delivers a session, not an interactive App frame)?

If all three clear: managed memory + ops maturity on the proactive lane **without touching interactive chat**, with export as the exit.

## Relationship to the primitives plan

This is the "**AgentCore Harness — tracked as an external-pattern scan**" line in `agentic-platform-primitives.md:10` coming due. It maps onto **F1** (headless entrypoint) and de-risks **F5** (memory). It does **not** change the "Harness owns run, Registry owns catalog+govern" framing — it just asks whether AWS's managed Harness can *be* our run engine on the proactive lane.

## Refs

- Overview: `devguide/harness.html` · vs Runtime: `harness-vs-runtime.html` · security/auth: `harness-security.html` · tools: `harness-tools.html` · skills: `harness-skills.html` · memory: `harness-memory.html` · export: `harness-export.html`
- Internal: `docs/specs/agentic-platform-primitives.md`, `docs/specs/scheduled-agent-runs.md`, `docs/specs/harness-entrypoint-spike-findings.md`

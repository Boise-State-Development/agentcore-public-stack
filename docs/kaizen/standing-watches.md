# Kaizen standing watches

State for the `kaizen-research` skill's standing watches: what is tracked, what upstream has landed, and what was settled by measurement. The skill reads this file every run and updates it in the research PR.

2a. **Prompt caching across providers — standing watch (added 2026-09-05)**

   We carry custom code that exists only because Strands' caching support is
   Bedrock/Anthropic-shaped. Upstream is converging on a provider-agnostic
   `CacheConfig`, so this is *mostly* a **subtraction** item: each upstream landing
   should delete code here, not add it. Check every run, and check it for *all*
   cacheable families — Anthropic, OpenAI/GPT, and any newly cacheable model — not
   just the one that prompted this.

   ⚠️ **Amended 2026-09-11: it is not subtraction-only, and the second half is now
   the more valuable one.** Two kinds of drift matter, and only the first deletes
   code. **(i) Convergence** — upstream absorbs something we hand-roll; propose the
   swap. **(ii) Coverage** — a Bedrock family gains prompt caching that *neither*
   we nor upstream will use, because both gate on the same `_cache_strategy`
   string test for `"claude"`/`"anthropic"`. Measured that day: **Nova Micro
   accepts an explicit system cachePoint and honors it** (7,203 input tokens → 2,
   7,201 cache-written) while `bedrock_cache_points_supported()` refuses it. That
   is caching left on the floor, it grows every time AWS ships a cacheable model,
   and no test can catch it — a string predicate keeps answering the same thing
   while the platform moves underneath it. Coverage findings are **additions**;
   file them anyway.

   Our carried code, and what would retire it:

   | Ours | Retired by |
   |---|---|
   | `usage_normalization.py` — maps `cache_write_tokens`, which Strands drops | [harness-sdk#4193](https://github.com/strands-agents/harness-sdk/pull/4193) (ours) — **merged 2026-09-25, after `python/v1.57.1`**; retires on the first release that contains it |
   | `usage_normalization.py` — disjointness shim for OpenAI's inclusive `input_tokens` | **Not retired by upstream — see the #4618 warning below.** [#3546](https://github.com/strands-agents/harness-sdk/issues/3546) closes once [#4617](https://github.com/strands-agents/harness-sdk/pull/4617) merges; its successor [#4618](https://github.com/strands-agents/harness-sdk/issues/4618) picks the *other* convention |
   | `build_prompt_cache_key()` in `bedrock_responses.py` | `strands/models/_openai_cache.py::apply_cache_config` — **already on upstream main**, maps `CacheConfig.cache_key` → `prompt_cache_key`. Adopt on the next pin bump. |
   | `apply_explicit_prompt_cache()` (breakpoints) | No upstream equivalent yet — `apply_cache_config` emits no `prompt_cache_breakpoint`. Ours is OFF by default (measured 57% worse); don't re-enable without re-running the probe. |
   | `cache_ttl_seconds_for()` in `observability/prompt_cache.py` | A model-derived TTL upstream. Note `apply_cache_config` maps ttl to `prompt_cache_retention` (`in_memory`/`24h`), *not* GPT-5.6's `prompt_cache_options.ttl: "30m"` — so these are not yet the same concept. |
   | `bedrock_cache_points_supported()` + the hand-placed system cachePoint | **NOTHING upstream can retire this — settled by measurement 2026-09-11, do not re-propose.** `format_request` copies `system_prompt_content` verbatim and `_apply_system_cache_ttl` never removes a point, so the block reaches Bedrock, which answers `AccessDeniedException` on a model that can't cache. The rejection is Bedrock's, not the SDK's. Only its `tools_ttl` half is redundant (upstream applies the same test at `bedrock.py:579`). |

   ⚠️ **harness-sdk#4618 (tracked from 2026-09-25) inverts our convention. It is a
   pin-bump hazard, not a subtraction.** #4618 proposes one canonical `Usage`
   convention, semconv **subset**: `inputTokens` *includes* cache reads and
   writes, and `totalTokens == inputTokens + outputTokens`. It enforces this at
   the provider boundary, so the **Bedrock and Anthropic adapters would fold
   cache tokens into `inputTokens`**. Everything we own assumes the opposite,
   the disjoint Converse convention: `CostCalculator.calculate_message_cost`,
   the context-size sum in `stream_coordinator.py`, `observability/prompt_cache.py`
   and `prefix_tokens.py`, and the `C#` metadata rows. The day a pin containing
   it lands, every cached Bedrock token is billed twice (input rate *plus* the
   cache rate), and a cache write is billed at the input rate plus the 1.25×
   premium. On our default path that is most of the prompt. Nothing errors,
   and the only symptom is cost and quota inflating on cache hits.
   `normalize_usage()` would have to learn to subtract for Bedrock too. Its
   docstring's "leaves Bedrock usage untouched" would then be the bug. Direct
   boto3 Converse callers (e.g. `app_api/chat/converse_routes.py`) keep the
   disjoint shape, so two conventions would coexist inside our own code. The
   proposal also adds a reasoning-token field (a *subset* of `outputTokens`,
   so it is a breakdown, never an extra charge). Status at filing: open,
   "warrants a short design doc before implementation", no PR yet.

   Each run, answer:
   - **#4618:** did a design doc, a PR, or a release note land? Does any adapter
     in the pinned version (or the candidate bump) now fold cache tokens into
     `inputTokens`? Check Bedrock specifically:
     `strands/models/bedrock.py` usage handling, and whether `_total_prompt_tokens`
     still exists (its deletion is the tell that the flip shipped). If it
     shipped, **the bump PR must carry the matching `normalize_usage` change**,
     plus a live check that one cached Bedrock call prices identically before
     and after. Queue it as a blocking gate on the bump, not an idea.
   - Does the pinned Strands version now ship `_openai_cache.py` / a `CacheConfig`
     that covers a provider we hand-roll? If so, propose the swap and say which of
     our modules shrinks.
   - Did `CacheConfig` gain a field (`cache_key`, `tools_ttl`, `system_prompt_ttl`, …)
     that maps onto something we do manually?
   - Any new Bedrock model family with prompt caching? Confirm which API surface
     serves it — GPT-5.6 caches **only** over the Responses API, and the same model
     over Converse caches not at all. Then **measure it, don't read it**:

         cd backend
         AWS_PROFILE=dev-ai uv run python scripts/probe_bedrock_cache_point_support.py \
             --model-id <the new model id>

     One command, ~$0.01, and it answers both layers at once — what the pinned
     Strands emits for that id, and whether Bedrock accepts and honors an explicit
     cachePoint. A row where Bedrock caches but `_cache_strategy` is `None` is a
     coverage finding (ii) — file it. Use `--offline-only` for a free check of the
     SDK half alone after a pin bump.
   - Movement on #3546 / #4617 / #4618, and whether #4193 has reached a release.
     All of them change what our cost math may assume.

   ⚠️ Never adopt an upstream caching default on inspection alone. This stack has
   already shipped one caching change whose premise was wrong and cost ~57% more
   in a live measurement. `backend/scripts/probe_gpt56_cache_rates.py` is the gate:
   beat the current arm, measured, before switching.

   ⚠️ **A caching A/B measures nothing if its prefix is under the model's minimum.**
   Below that floor Bedrock **silently ignores** the cache point — no error, no
   cache buckets, a result indistinguishable from "unsupported". Measured on Haiku
   4.5 in us-west-2 (2026-09-11): 2,351 and 3,911 tokens wrote **nothing**, 5,202
   wrote in full — a real floor of **4,096**, twice the 2,048 the first-party
   Anthropic docs publish for Haiku. Do not trust a vendor-documented minimum;
   bracket it. This already produced one false "does not cache" against our own
   production model.

2b. **Multi-agent patterns and inter-agent communication — standing watch (added 2026-10-13)**

   This is strategic: composing agents (agents as tools, Swarm, Graph, messages
   between conversations, and eventually a drag-and-drop canvas in the Agent
   Designer) is where the platform is headed. Two living specs carry the plan,
   and this watch keeps them true:

   - `docs/specs/multi-agent-patterns.md`: the pattern map, the shared sub-agent
     runner (§4), what Strands handles vs. what's ours (§4.2), the Designer
     composition model (§6), the sequence (§8)
   - `docs/specs/agent-handoffs-and-consults.md`: handoffs (Phase 0), deliveries
     (§4.8), consults (Phase 1), messages between conversations (Phase 2)

   Read both before scanning. Lens (b), **capability unlock**, usually dominates
   here, but subtraction matters too: each upstream release that ships a piece of
   §4's substrate (nested interrupts, multi-agent session persistence, per-node
   metering, nested streaming) is a piece we shouldn't build.

   Sources:
   - **Strands multi-agent.** In each harness-sdk release, look for changes to
     `strands/multiagent/` (`swarm.py`, `graph.py`, `a2a/`, `_snapshot.py`) and to
     `strands-agents-tools`' `workflow`, `swarm`, `agent_graph`, `use_agent` and
     `a2a_client` tools. Shares source 2's web requests; no extra budget.
   - **A2A protocol.** The spec and SDK releases (confirm the current repo; it moved
     to the `a2aproject` org), and AgentCore Runtime A2A support in source 1.
   - **Inter-agent patterns in shipped harnesses.** Claude Code's changelog (source
     6) for subagents, cross-session messaging and agent teams; opencode (6a) for
     sub-agent and task delegation.
   - **Visual agent composition.** Products that let users wire agents together
     (e.g. Amazon Bedrock Flows, OpenAI's agent builder, LangGraph Studio). Pattern-only
     references for the Designer canvas (§6 of the patterns doc); 1–2 requests, and
     only when a release headlines composition.

   Each run, answer:
   - **Did §4.2 move?** Does the pinned Strands version (or the candidate bump)
     change how Swarm/Graph handle interrupts, `session_manager`, hooks, plugins or
     prebuilt nodes? Does either of §4.2's two verify-firsts now have an upstream
     answer?
   - **Did anything retire a piece of the substrate?** Name the row of the patterns
     doc §4 table it retires, and what we'd no longer build.
   - **Did our own code move?** `git log origin/develop --since="7 days ago"` plus a grep for
     the names the specs promise: `HANDOFF#`, `DELIVERY#`, `apis.shared.handoff`,
     `resolve_turn_agent`, `send_to_conversation`, `consult-`, `strands.multiagent`,
     `Binding.kind` `'agent'`. A landed PR means a spec's **Status** line, phase
     table or "what already exists" section is now stale.
   - **Does any claim in either spec no longer hold?** A pinned version, a parameter
     name, a "not used in `backend/src`", a cost figure, a gate assumption.

   **Output: a "Spec sync" list** in the research doc (see the template). Each entry
   names the doc and section, quotes the stale claim, cites the evidence (URL, commit
   or file:line) and gives the proposed replacement text. Queue each as a review-queue
   item with **Surface: docs**. This skill still never edits the specs; the sync lands
   in its own small PR after review, like every other idea. An empty week says
   "no drift found" explicitly, so silence is distinguishable from not checking.

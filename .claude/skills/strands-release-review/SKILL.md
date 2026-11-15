---
name: strands-release-review
description: Review every `strands-agents` Python release since our pin and say what we can adopt, not only what might break. Reads the release notes (tag `python/vX.Y.Z` on `strands-agents/harness-sdk`), diffs the two wheels from PyPI, greps our import surface against what changed (private modules included), diffs `model_info.json` for every curated model id, reads docstrings for behaviour that only changes when a config is passed, and gives each change one verdict (Adopt / Gate / Guard / Subtract / Inert) with the file it touches and a prompt-cache impact line. A separate capability-unlock lens lists what the release lets us offer that's new, and what that would cost in speed, quality and cost. Ends with what the bump PR must carry. Writes `docs/kaizen/scoping/strands-<new>-readout.md`. Use when the user says "strands release review", "review the strands bump", "what's in strands 1.66", "should we bump strands", "strands readout", or when `kaizen-research` finds `strands-agents` pin lag above 0.
---

# Strands release review

Strands releases are the largest single source of platform changes we get for free, and the riskiest dependency we bump. Reading the release notes and treating each item as a risk doesn't cover it. On 2026-11-14, 1.64.0 had waited ten days behind a "measure GPT-5.6 caching first" gate. The wheel diff showed the gate wasn't needed: we never pass `cache_config` to the OpenAI family, so the change was inert for us. This skill reads the code that ships, not the notes about it, and asks of every change: **can we use this?** first and **does it break us?** second.

It runs once per release, whenever our pin is behind. It's read-only: it writes one readout under `docs/kaizen/scoping/` and never edits source, `pyproject.toml` or `uv.lock`. The bump is its own PR, and the readout is that PR's checklist.

## Inputs

- **Our pin:** `backend/pyproject.toml` (`strands-agents==X` and `strands-agents[bidi]==X` must match).
- **Releases since the pin:** `gh release list --repo strands-agents/harness-sdk --limit 30`. Only `python/vX.Y.Z` tags matter. `harness-python/*`, `harness-typescript/*`, `typescript/*`, `harness-cli/*` and `mcp/*` are other packages.
- **The wheels:** PyPI JSON, `https://pypi.org/pypi/strands-agents/<v>/json`.
- **Curated models:** `frontend/ai.client/src/app/admin/manage-models/models/curated-models.ts` (`modelId:` lines).
- **What we carry that upstream might retire:** `docs/kaizen/standing-watches.md` (2a caching, 2b multi-agent).
- **The last readout:** the newest `docs/kaizen/scoping/strands-*-readout.md`. Items it marked Gate or Guard that never shipped carry forward.
- **Memory:** "Strands bump readout recipe" holds the method and the findings from earlier bumps.
- **Strands docs, if the `strands-docs` MCP server is available** (the `strands@strands-agents` Claude Code plugin; load `search_docs` / `fetch_doc` with `ToolSearch` query `strands-docs`). Use it for how upstream means a feature to be used, in step 5's Adopt rows and the Unlocks table. It documents the latest release, not our pin, so it never decides a verdict: the wheel diff does. Its search ranks loosely: on 2026-11-15, "concurrent tool executor" returned the MCP agent tool, structured-output tool and tool-provider pages, and not the executor's. So prefer `fetch_doc` on an API page named after the changed module (`https://strandsagents.com/docs/api/python/<module>/index.md`) over a broad query (`fetch_doc` with an empty `uri` lists every page, when the module name is unclear), and never read a search that misses as "not documented". Without the server, skip it and say so under "Not checked".

## How to run

Use a scratchpad (`$S` below), never the repo. The wheel trees are untrusted downloads, so unzip each into its own empty directory, keep your scripts elsewhere, and run Python over them with `-I`.

### 1. Pin and release notes

```bash
grep -n 'strands-agents' backend/pyproject.toml
gh release view python/v<X> --repo strands-agents/harness-sdk --json publishedAt,body -q '.publishedAt + "\n" + .body'
```

Do this for every `python/v*` tag after the pin. **Filter out** `feat(site)`, `docs(*)`, `ci(*)`, `chore(site|docs)`, `test(*)`, `feat(cli)`/`fix(cli)`, anything marked `(typescript)`/`(TypeScript)`/`(ts)`, and `feat(ts)`. The release notes are drafted from commits across the monorepo. **Treat what's left as leads, not facts:** a note may describe a TypeScript-only change, and a change may ship with no note. The wheel diff decides. On 1.65.0, #6055 ("keep other sources' tools when an MCP server refreshes") touched no file in the Python wheel, and `model_info.json` flips appeared in no note at all.

**Settle each lead's language with its PR's file list.** Unlabelled lines often read as Python or shared and touch only `strands-ts/`. The 1.55 → 1.63 audit found five (#4408, #4863, #4569, #5065, #4851), and #4408 had already been listed as a Python unlock in a research doc. One call per PR settles it:

```bash
gh pr view <N> --repo strands-agents/harness-sdk --json files \
  -q '[.files[].path | select(startswith("strands-py/src/") or startswith("shared/"))]'
```

Count `shared/` as Python: `shared/model-info.json` ships in the wheel as `strands/models/model_info.json`. #6073, the Haiku 5.5 `forcedToolChoice` flip, touched only `shared/` and tests, so a `strands-py/src` filter alone would drop it. A lead with neither path is TypeScript- or CLI-only: list it under "dropped" and give it no verdict. For a release with many leads, loop the call over the PR numbers rather than reading notes.

### 2. Wheel diff and our import surface

```bash
S=<scratchpad>; mkdir -p $S/wheels && cd $S/wheels
for v in <old> <new>; do
  meta=$(curl -s https://pypi.org/pypi/strands-agents/$v/json)
  url=$(printf '%s' "$meta" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(next(u["url"] for u in d["urls"] if u["filename"].endswith(".whl")))')
  sha=$(printf '%s' "$meta" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(next(u["digests"]["sha256"] for u in d["urls"] if u["filename"].endswith(".whl")))')
  curl -sL -o s-$v.whl "$url" && echo "$sha  s-$v.whl" | shasum -a 256 -c
  mkdir -p w$v && unzip -q -o s-$v.whl -d w$v
done
diff -rq w<old>/strands w<new>/strands | grep -v __pycache__
diff <(grep ^Requires-Dist w<old>/*.dist-info/METADATA) <(grep ^Requires-Dist w<new>/*.dist-info/METADATA)
```

"Only in" lines are new or removed modules. The `Requires-Dist` diff catches a moved `mcp` or `anthropic` range, which `tests/supply_chain/test_strands_agentcore_pairing.py` has to agree with.

Then list what we import, private modules included, and cross it with the changed files:

```bash
cd backend && grep -rhoE '^\s*(from strands[a-zA-Z0-9_.]* import|import strands[a-zA-Z0-9_.]*)' src scripts \
  | sed -E 's/^\s+//; s/ import$//; s/^(from|import) //' | sort | uniq -c | sort -rn
```

Our private-module imports are where a bump breaks without a deprecation: `models._openai_bedrock`, `models._strict_schema`, `models._defaults`, `plugins._discovery`, `types._events`, and `_interrupt_state` read through `getattr` (`chat/routes.py`, `chat/service.py`, `stream_coordinator.py`). Also check the private names we reach *into* on a public module, such as `strands.models.bedrock._SKIP_COUNT_TOKENS_MODELS` in `bedrock_count_tokens.py`. For each changed file we import, read the diff (`diff -u … > $S/diffs/<file>.diff`, then read it in pieces). A changed file we don't import still matters when something we call routes through it. On 1.65.0, `event_loop.py`, `stream_messages` and `SlidingWindowConversationManager`'s summarizer all moved onto the new `ModelProxy`.

### 3. `model_info.json` for every curated model

```bash
python3 -I .claude/skills/strands-release-review/model_info_diff.py \
  $S/wheels/w<old>/strands $S/wheels/w<new>/strands \
  frontend/ai.client/src/app/admin/manage-models/models/curated-models.ts
```

It resolves each curated id the way `lookup_model` does, using the suffix patterns from each wheel's own `_model_info.py`. It prints every capability or limit that changed, and marks keys the Python SDK doesn't map. Release notes miss most of these flips (`forcedToolChoice`, `defaultEffort`, `temporaryMessages`, `serverSideSummary`, `promptCaching`). For each one, grep the new wheel for the snake_case field and follow it to the code that reads it. A flip only matters on a path that reads it with our config. Example: `forced_tool_choice=False` moves `BedrockModel.structured_output` from a forced tool to native output, which is `document_extract`'s path, but only if `DOCUMENT_EXTRACT_MODEL_ID` names that model.

### 4. Docstrings: behaviour that changes only when a config is passed

Read the docstrings of every changed public config (`CacheConfig`, `BedrockConfig`, `OpenAIResponsesModel.OpenAIConfig`, `ModelInfo` capabilities) and every new keyword. For each, answer: **do we pass it, on which model class?** Answer with `rg` over `backend/src/agents/main_agent/core/model_config.py` (`to_bedrock_config`, `to_openai_config`, `to_mantle_config`, `to_bedrock_responses_config`, `to_gemini_config`) and `agent_factory.py`, not from memory. The 1.64.0 case: `cache_config` on `OpenAIResponsesModel` now places GPT-5.6 message checkpoints (`strategy="auto"`). `bedrock_responses.py` measured an end-of-history breakpoint at **+11.8% cost**, but none of our OpenAI configs carry `cache_config`, so the change is inert until someone adds one.

### 5. One verdict per change

| Verdict | Means | Must name |
|---|---|---|
| **Adopt** | a free win on a path we run, with nothing for us to write | the path it improves (file:line) |
| **Gate** | the bump needs a code or doc change, in the bump PR or before it | the file:line to change and the test that proves it |
| **Guard** | don't turn it on; the bump PR adds a test so nobody does by accident | the config it would take, the measured or reasoned cost, the test file |
| **Subtract** | upstream now does something we built; check `docs/kaizen/standing-watches.md` | our module that shrinks, and whether the swap is byte-identical on the wire |
| **Inert** | not on any path we run, with the reason (config we don't pass, provider we don't use, TypeScript only) | the grep that shows it |

Every verdict names a file and line in our tree. If you can't name one, the verdict is Inert, and you say why. Don't write Gate to mean "be careful". A Gate is a specific change, sized, with its test. A Subtract is usually a follow-up PR, not part of the bump. It goes to `docs/kaizen/review-queue.md` through the review, not into the bump.

**Capability unlock: a second lens, scored apart from the verdict.** The verdict asks what a change does to what we already run. This lens asks what it lets us *offer* that we couldn't easily build before: a new primitive (vended subagent/swarm/graph tools, the public middleware, `DecisionModel`, HITL modes), a new protocol surface (MCP tasks, `_meta`), or a new storage or context mechanism. It is the "capability unlock" lens (b) from standing watch 2b in `docs/kaizen/standing-watches.md`, applied to the whole release. Most unlocks are verdict-Inert, because nothing we run calls them yet, so without this lens they would disappear into the Inert rows. For each one, name:

- **what it unlocks:** the product capability or UX, and the surface it would land on (a tool, an SSE event, a spec section such as handoffs §4 or patterns §4.2);
- **what it costs speed, quality and cost:** an unlock must cost those three nothing, or say what it costs (a model call per tool round, a resource that bills, latency before the first token). "Unknown" is an answer, and it makes the next step a measurement;
- **the next step:** a scoping item, a spec-sync row, or "watch". Never part of the bump.

When the Strands docs are available (see Inputs), read the page for each Adopt row and each unlock before writing "Lands on": the documented usage is often what tells you whether the unlock fits an existing surface or needs a new one.

**Prompt-cache impact line (required, one per readout).** List the request bytes that change at deploy, and for which model families. Our cached prefix is exact-match, so any bytes the bump changes get re-written once at the cache-write premium, in every live session. Answer per family (Bedrock Claude, Bedrock Nova/others, OpenAI Responses on bedrock-runtime, Mantle, Gemini). The proof for Bedrock Claude is `tests/agents/main_agent/core/test_bedrock_golden_requests.py`: run it on the new pin. A pass means byte-identical requests. A failure means a real prefix rewrite: price it and say so in the PR. For families the golden test doesn't cover, reason from the diff and say it's reasoned, not measured.

### 6. What the bump PR must carry

Always:

- Every **Gate**, each with its test.
- Every **Guard** test.
- **The `format_request` / `ModelProxy` A/B benchmark**, run on an idle machine from a synced venv (`uv sync --extra agentcore --extra dev` in a checkout on the bump branch, which needs the developer's go-ahead to install). Put the old wheel ahead of site-packages with `PYTHONPATH=$S/wheels/w<old> uv run python bench.py`, so both arms share every other dependency. Use a production shape: 40 tools, a 60k-character system prompt, 41 messages. Report µs per call for both arms, and the `stream()` preamble if `ModelProxy` or another per-call wrapper changed. Under a parallel pytest run the 1.63.0 numbers read 29 → 242 µs; idle they read 22 → 102 µs. A busy machine reports a regression that isn't there.
- **The full suite:** `uv run python -m pytest tests/ -n 3` (≤3 workers on this Mac). Some bump effects surface only there; the 1.63.0 vended `context_offloader` registration change did.
- **The golden-request test**, passing or deliberately regenerated, with the cost stated.
- **The smoke matrix's interrupt/resume rows** on dev after deploy: `uv run python scripts/smoke_turns.py … --only ask_user_question_resume,tool_approval_resume,client_tool_call_resume,restore_after_reload,second_turn_cache` (see `docs/testing/smoke-regression.md`). Strands' interrupt and session-restore code changes in most releases, and only real turns exercise our resume path.
- **CLAUDE.md, spec and standing-watch text** the release made stale. Quote each line.

## Output — `docs/kaizen/scoping/strands-<new>-readout.md`

The version is written with hyphens: `strands-1-65-0-readout.md`. That file is the worked example of this skill (1.63.0 → 1.65.0).

```markdown
# Strands <old> → <new> readout
> Reviewed YYYY-MM-DD with the `strands-release-review` skill. Pin: <old>. Releases in range: python/vA (date), python/vB (date).
> Wheels: sha256 verified from PyPI. Python files changed: N (M new). Requires-Dist: [changes or "unchanged"].

## Verdict
[2–3 sentences: bump now / bump with gates / hold, and the one reason. Name anything that was blocking the bump and isn't.]

## Prompt-cache impact
[Per family: which request bytes change at deploy, measured (golden test) or reasoned.]

## Changes
| # | Upstream | Verdict | Where it lands for us | Why |
|---|---|---|---|---|
| 1 | #NNNN short title | Adopt | `path/file.py:NN` | … |

## Unlocks
| Upstream | Unlocks | Lands on | Costs speed / quality / cost | Next step |
|---|---|---|---|---|
| #NNNN … | … | `path` or spec § | none, or what it costs | scoping / spec sync / watch |

## model_info.json (curated ids)
[The helper's output, trimmed to changed ids, with a "read by" note per field.]

## The bump PR must carry
- [ ] …

## Not checked
[What this run couldn't do (benchmark without a synced venv, a live probe), and who or what does it.]
```

Then update memory: add the findings that will matter on the *next* bump to "Strands bump readout recipe" (an upstream behaviour we work around, a memo to retire later). Leave out the whole readout.

## Rules

- **The wheel is the source of truth.** A release note with no matching Python diff is Inert. A Python diff with no note still gets a verdict.
- **Adoption first.** For every change, ask what it lets us delete or improve before asking what it breaks. A readout with no Adopt or Subtract row across several releases is a finding. Say so in the Verdict. The same goes for an empty Unlocks table on a release that shipped new primitives: an unlock filed as Inert is a lost unlock.
- **Gates must be needed.** Before calling something a blocking gate, show the config or code path that makes it reachable for us. A gate on a path we don't run delays the bump and its fixes.
- **Measure, don't guess,** on the turn path. A per-call wrapper (`ModelProxy`) or `format_request` change gets the A/B numbers in the bump PR, or the readout says "not benchmarked" and the bump PR owes them. See CLAUDE.md's TTFT budget.
- **No source edits here.** This skill writes the readout (and its PR). The bump, the gates and the guards ship in the bump PR.
- **No real identifiers** (user ids, session ids, account ids) in the readout. It's a public repo.

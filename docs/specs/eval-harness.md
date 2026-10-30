# Evaluation harness — phase 1, the release-gate script

**Status:** PR-1 merged and validated on dev (2026-10-30). PR-3 BUILT (2026-10-30): the eval user, its sign-in, the first Haiku 4.5 baseline, and the release and nightly wiring; see §13. PR-2 BUILT (2026-10-30): the judges, pinned to strands-agents-evals 1.7.0, and a re-promoted Haiku baseline with judge scores (54 of 57 cases pass). Phase 1 of a three-phase plan; §12 names the other two so nobody designs phase 1 in a way that blocks them.
**Refs:** `docs/specs/agentcore-evaluations-spike-findings.md` (the judge spike and its hybrid recommendation), `docs/specs/response-feedback.md` §2 and §11 PR-4 (thumbs as a sampler, the managed-judge adapter), `docs/testing/smoke-regression.md` (the pass this slots into), `backend/scripts/compaction_quality_harness.py` and `backend/scripts/large_document_quality_harness.py` (the two harnesses whose method this generalises), `docs/specs/turn-path-ttft.md` §6 (the regression plan this is the quality half of), `docs/kaizen/scoping/2026-09-21-quality-veto-harness.md` (the scoping that built the compaction harness as slice 1 and deferred the live-arm slices; its §2.3 on what the runner can and cannot vary is load-bearing for §11 here), `docs/kaizen/review-queue.md` [2026-09-21] "The cheap quality veto does not exist".

## 1. Problem

Cost on this platform is instrumented to three decimal places: every model call writes a `C#` row with its cache status, its waste in dollars and the hash that explains a miss. Quality is not instrumented at all. CLAUDE.md's cost tenet says that when cost and quality conflict, quality wins, and today nothing could detect the conflict. Every compaction threshold, model default, context-offload gate and system-prompt edit has shipped with cost measured and quality asserted.

Three things have been tried, and each proves a piece of the answer without being it:

- **Two bespoke quality harnesses** (compaction, large documents) each built their own corpus, arms, judge and paired statistics. The compaction one, built as slice 1 of the 2026-09-21 scoping, found that the default summariser was silently forgetting constraints (constraint retention 1.00 → 0.72) and drove a model change. That is the proof that a quality veto pays for itself. But each took a week, the scoping's live-arm slices were deferred behind them, and the next question gets a third one.
- **The feedback eval sampler** wires AgentCore Evaluations' sixteen judges to down-thumbed turns. The judge works; the sampler is starved, because prod produces about one thumb a day against a floor of twenty per arm. It cannot answer "did this PR make answers worse".
- **The smoke matrix** drives real turns and asserts every SSE code path. It proves the wire, not the answer.

The gap is a shared golden set, a committed baseline, and one runner that any change can be measured against before it reaches prod. That is this spec.

What it is **not**:

- Not a per-PR CI gate. A run needs a deployed stack, a headless grant, minutes and dollars, and its judge scores are nondeterministic. It is a release gate and a tool for the author of a model-call-path change, the way the TTFT budget is.
- Not a quality KPI. Every number is a paired comparison against a baseline with its `n`, or it is not reported (response-feedback §9). No pooled score ever leaves the script.
- Not an instrument over user conversations. The corpus is synthetic. §4 D6 makes this a rule, not a default.

## 2. Thesis

> **Measure the deployed agent, not a model.** The quality a user gets comes from RBAC, the tool catalogue's descriptions, skills, compaction, the system prompt and the model together. The myBoiseState evaluation found its gains in tool descriptions, not the skill; the compaction readout found its loss in a side-channel summariser. A harness that runs an in-process `Agent` with a hand-picked tool list measures none of that.

So the unit under test is one real turn through the deployed AgentCore Runtime, as a real user, and the result is read off the stream exactly as the scheduled-runs worker reads it. Everything else in this spec is scoring.

## 3. What strands-agents-evals gives us, verified

Read from the package source and PyPI on 2026-10-29 (`strands-agents-evals` 1.7.0, released 2026-10-21; weekly releases, Apache-2.0).

- **Dependencies fit our pins.** It requires `strands-agents>=1.45.0` (ours 1.61.1) and `strands-agents-tools>=0.1.0,<1.0.0` (ours 0.8.8), plus pydantic 2, rich, tenacity and the OpenTelemetry SDK. It does not depend on `bedrock-agentcore`.
- **A `Session` can be built by hand.** `strands_evals.types.trace` defines `Session`, `Trace`, `AgentInvocationSpan` (`user_prompt`, `agent_response`, `available_tools`, `system_prompt`) and `ToolExecutionSpan` (`tool_call`, `tool_result`) as plain pydantic models. `TraceExtractor` walks them by `isinstance`. Nothing requires an in-process agent or an OpenTelemetry exporter.
- **Judges run locally.** `HelpfulnessEvaluator`, `FaithfulnessEvaluator`, `InstructionFollowingEvaluator`, `ToolSelectionAccuracyEvaluator` and the rest construct a Strands `Agent(model=...)` and call Bedrock from wherever the script runs, with structured output. No span ingestion, no 180-second delay, no managed evaluator, and the text they read is the text we hand them.
- **Deterministic scorers exist.** `TrajectoryEvaluator` ships `exact_match_scorer`, `in_order_match_scorer` and `any_order_match_scorer`; `SkillInvoked` is a presence check. These cost nothing.
- **The task contract is a callable.** `Experiment.run_evaluations_async(task, max_workers)` calls `task(case)` and accepts `{"output": str, "trajectory": Session}`. An `async` task that awaits `run_agent_headless` is a supported shape.
- **What it does not give us** is the same list as the spike's: the arm design, replicates, the paired statistic, the corpus, and the baseline lifecycle. Those stay ours, and the two existing harnesses already have them.

One consequence changes the spike's conclusions: its structural objection (the managed service cannot blind a judge because content-in-spans *is* the mechanism) does not apply here. We build the `Session`, so we scrub it first (§6.4).

## 4. Decisions

**D1 — The task is `run_agent_headless`.** `apis.shared.harness.runner.run_agent_headless` already runs one turn as a user through the deployed Runtime and returns a `RunResult` with `final_message`, `tool_trace`, `usage`, `status` and `stop_reason`. The harness never constructs a Strands `Agent`. Phase 1 adds two passthroughs the runner lacked: `file_upload_ids`, which the smoke matrix already sent from its own payload builder and the document family needs, and `enabled_skills`, which the skill family needs to fix the set of skills a case discloses. Both mirror `InvocationRequest` fields and are sent only when given.

**D2 — Judging runs locally through strands-agents-evals over a hand-built `Session`.** The managed AgentCore Evaluations path stays where it is (the feedback sampler, `apis/shared/feedback_eval/sampler.py`) and is not used here: the Runtime emits `[REDACTED]` span content since `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_unredacted_attributes=` was set, the managed path cannot scrub, and its ingestion delay makes a forty-case run an hour of sleeping.

**D3 — The golden set is JSON in the repo, in our schema.** Cases live in `backend/evals/cases/<family>.json` (§5) and are adapted into `strands_evals.Case` at load. The package's own `Experiment.to_file()` format is not the storage format: it changes with the package, and the cases must outlive any one judge library. The adapter is the seam.

**D4 — Scoring is paired against a committed baseline, never absolute.** Each case runs `k=3` times (D10 says when more). A case's outcome on a run is the majority of its samples, ties counting as wrong, exactly as the two harnesses do. Per family, the run is compared with the baseline on the discordant cases with an exact McNemar test (`compaction_quality.score.mcnemar_exact`, which the large-document harness already imports), and `n` is printed on every row. There is no pooled score across families.

**D5 — Deterministic families gate; judge families advise.** A family whose scorer is a pure function (planted facts, required tools, refusal phrases, format regexes, skill invocation) fails the run when it regresses against the baseline at `p < 0.05` on `n ≥ 10`. A family scored by an LLM judge reports the delta and the per-case flips and never fails the run on its own. This is the compaction harness's lesson: the exact-match families found the regression; the judge confirmed it. A judge is a second opinion, not a gate.

**D6 — Synthetic corpus only.** The judges read every byte we hand them, and a case file is a public artifact. No case may be derived from a recorded conversation, name a real person or organisation, or carry a real identifier (`feedback_no_real_identifiers_in_public_artifacts`). The large-document fixtures (`large_document_quality/fixtures.py`) and the compaction corpus (`compaction_quality/corpus.py`) are already built this way and are reused, not copied.

**D7 — The harness runs as a dedicated eval user, not a developer.** The smoke matrix runs as its author's headless grant and its colour prompts have leaked into that user's long-term memory. Memory is scoped per actor, so a dedicated `eval` Cognito user with its own grant isolates the harness from everyone's LTM and makes "the eval user's RBAC" a fixed thing the baseline can describe. The role is seeded the way `scripts/seed_e2e_role.py` seeds `e2e_user`: explicit tools, explicit models, all through `AppRoleRepository` so the `effective_permissions` rows land.

As built (`backend/scripts/seed_eval_user.py`, idempotent):

- **Role.** `eval_harness`, mapped to the Cognito group `eval-harness`. It grants exactly the tools and skills the golden set offers (`required_grants` over the case files) and every model, so adding a case that needs a new tool means re-running the seed, and the seed test fails until the grant follows.
- **User.** A Cognito-native `eval-harness` user with a permanent generated password. The password exists only in the SSM SecureString `/<prefix>/eval-harness/credentials`; nobody types or sees it, and a re-run reuses it.
- **Sign-in.** A headless grant lasts at most 30 days from an attended login, which an account nobody logs into would outlive. So `run --auth cognito` signs in fresh on every run with the smoke matrix's scripted Hosted UI login and renews its own grant from that session (`POST /runs/grant`). The turns then go through `run_agent_headless` exactly as before. This needs the classic Hosted UI and the COGNITO provider on the BFF app client, both defaults.
- **Long-term memory off.** The seed sets the user's `memoryMode` to `off`. Retention cases plant facts, and a user whose memory kept them would let a later run answer from memory instead of the conversation: a pass that proves nothing. Conversation history still persists within a session, which is what the retention family measures. The trade is that the harness measures a configuration with memory retrieval off; memory quality is a separate question this harness does not answer.

**D8 — The target is dev or a local stack; prod is not a target.** The script has no `--allow-prod`. A prod run would write sessions and spend quota as a user in prod for a question that dev answers identically, because the code under test is the same image. A branch is proved against a local inference-api before it deploys, the way the smoke doc's local recipe does (and with the same worktree caveat: `PYTHONPATH` or the run silently tests `develop`).

**D9 — Every run is estimated before it spends, and refuses without `--yes`.** The pattern is the harnesses': `run` prints cases × k × arms, the token estimate and the dollar figure from the catalogue's rates, then stops. Results append to `results.jsonl` and a re-run skips rows already present, so a run killed by an expired SSO token resumes rather than restarts.

**D10 — The model under test is a run parameter, and the baseline is per model.** `--model-id` is required. Defaulting to the eval user's default model would leave the harness unable to say which model a run measured, since the stream does not name it, and a baseline must. A baseline file is keyed by model id (§8), so switching the platform default is a new baseline, not a regression, and a PR that changes a model can be run against both.

**D11 — The package is pinned exactly and imported lazily behind `apis/shared/evals/`.** `strands-agents-evals==1.7.0` goes in the `dev` extra, not the service images. Nothing under `apis/shared/evals/` imports `strands_evals` at module import time (the `AgentCoreJudge` pattern), so `apis.shared` stays importable everywhere and a breaking release upstream is one file to fix. Adding the dependency is its own approval (CLAUDE.md: never install without explicit approval).

**D12 — Nothing here touches the turn path.** The only product code is `apis/shared/evals/`, which no service imports at runtime, and the two passthroughs on the runner. TTFT and the cacheable prefix are unaffected by construction; the spec says so because the review checklist will ask.

## 5. The case format

One file per family, `backend/evals/cases/<family>.json`, an array of cases:

```json
{
  "id": "tool_selection.multiplication_reaches_for_calculator",
  "family": "tool_selection",
  "turns": [
    {"prompt": "What is 48,217 multiplied by 913? I need the exact figure."}
  ],
  "run": {"enabled_tools": ["calculator", "whoami"], "enabled_skills": []},
  "expect": {
    "contains_any": ["44,022,121"],
    "tools": {"must_call": ["calculator"], "must_not_call": ["whoami"]}
  },
  "notes": "Exact five-by-three digit product: a model that answers from memory is often wrong."
}
```

Every key is optional except `id`, `family` and `turns`, and parsing is strict: an unknown key is an error, because a misspelt `must_not_call` would otherwise turn a guard into a case that can never fail.

- `turns` is a list because retention cases need more than one; every turn after the first reuses the session. Only the last turn is scored.
- `run` holds `enabled_tools` and `enabled_skills`, which mirror `InvocationRequest`. `null` (or absent) means the eval user's full grant; `[]` means none. There is no model here: the model is a run parameter (D10).
- `expect` is the deterministic scorer's input (§6.2): `contains_any`, `contains_all`, `forbidden`, `tools` (`must_call`, `must_not_call`, `in_order`), `refuses`, `regex` (`fullmatch`, `search`, `absent`) and `skill` (`loads`, `not_loads`). A gating family's case must declare at least one; the loader rejects one that declares none, and one that requires a tool or skill its `run` does not offer.
- `judge.evaluators` lists strands evaluator names (`helpfulness`, `faithfulness`, `instruction_following`, `tool_selection`, `refusal`, `conciseness`); `rubric` feeds `OutputEvaluator`. A `general_chat` case must name one. Empty means no judge spend for the case.
- `files` names fixtures in `backend/evals/fixtures/` that the harness registers as READY uploads in the case's session and sends with the first turn (§6.1).

A case id is stable forever: baselines key on it. Renaming a case is deleting it and adding another.

## 6. Architecture

```
backend/
  evals/
    cases/<family>.json          the golden set (D3)
    baselines/<model-id>.json    promoted run outcomes per case (§8)
    fixtures/                    small synthetic files; large ones are generated
  scripts/eval_harness.py        the CLI (§7)
  src/apis/shared/evals/
    __init__.py
    cases.py                     load + strictly validate case files (§5)
    score.py                     deterministic scorers, mcnemar_exact, the gate rule (§6.2, §8)
    execute.py                   run one sample of one case through an injected turn runner (§6.1)
    report.py                    baselines, per-family comparison, --explain (§8)
    session_mapper.py            RunResult → strands_evals Session-shaped dict (§6.3)
    scrub.py                     strip digests/tool payloads before a judge reads (§6.4)
    judges.py                    the Judge protocol; the strands adapter is PR-2 (D11)
    fixtures.py                  register a fixture as a READY upload (moved from smoke_turns.py)
```

The text helpers and `mcnemar_exact` moved here from `scripts/compaction_quality/score.py`, which re-exports them, so the three harnesses score with one definition.

`apis/shared/evals/` sits in `apis.shared` because phase 3's admin trigger and phase 4's per-agent test sets will call the same mapper and judges from app-api and a worker, and the import-boundary test forbids either from importing a script.

### 6.1 Running a case

1. Open a fresh session with an `eval-` id. Not `headless-`: that prefix marks a turn as unattended, which changes behaviour (a project-memory save becomes a proposal), and the harness measures the attended chat path.
2. Register the case's fixtures as READY uploads in that session (`apis/shared/evals/fixtures.py`, which the smoke matrix now shares).
3. For each turn, `await run_agent_headless(user_id=eval_user, prompt=..., auth=grant, session_id=..., model_id=..., enabled_tools=..., enabled_skills=..., file_upload_ids=<first turn only>, trigger="eval", title="[eval] <case id>")`. A turn that does not complete stops the case.
4. Score the last `RunResult`. A `status` other than `completed` scores the sample as wrong and records the status; `oauth_required` is a configuration error that stops the run (the eval user's grants are the fixed thing, D7), and re-running the same command resumes it.
5. Append the row, which carries every turn's `RunResult` and the mapped `Session`, so `score` and PR-2's `judge` need nothing but the run directory.
6. Delete the sessions and fixture uploads at the end of the run unless `--keep`, as the smoke matrix does.

### 6.2 Deterministic scoring

A pure function of `(case.expect, RunResult)`:

| check | source | rule |
|---|---|---|
| `contains_any` / `contains_all` / `forbidden` | `final_message` | word-bounded, comma- and quote-normalised matching (the compaction harness's `is_correct`); an answer giving both the old and the new value fails |
| `tools.must_call` / `must_not_call` / `in_order` | `tool_trace[].name` | set and subsequence checks |
| `refuses` | `final_message` | `true`: the answer declines in so many words (`REFUSAL_PATTERNS`); `false`: it does not. Deliberately coarse, so refusal cases also list `forbidden` values the harmful answer would contain; the advisory `refusal` judge is the second opinion |
| `regex` | `final_message` | `fullmatch` on the stripped answer, every `search` present, every `absent` absent |
| `skill` | `tool_trace` | a `skills` or `read_skill_file` call whose input names the skill (the two names `context_itemization.py` already treats as the skill tools) |

A sample passes when every declared check passes, and an empty answer always fails.

### 6.3 Mapping `RunResult` to a `Session`

One `Trace` per turn, with:

- an `AgentInvocationSpan` carrying the turn's prompt, `final_message`, and `available_tools` built from the case's `enabled_tools` with descriptions read from the tool catalogue (`ToolConfig(name, description)`); `system_prompt` is **omitted**, because the runner does not see it and a fabricated one would mislead `InstructionFollowingEvaluator`;
- one `ToolExecutionSpan` per `tool_trace` entry (`ToolCall(name, arguments=input)`, `ToolResult(content=result_preview or "", error=... if is_error)`).

`SpanInfo` timestamps for the agent span come from `started_at` / `finished_at`; span ids are synthetic. `RunResult` carries no per-tool timing, so tool spans get synthetic, strictly increasing timestamps in `tool_trace` order inside the agent span's window: the tool-level extractor orders a tool's "prior executions" by end time, and equal timestamps would make every tool look concurrent. Each tool span names the agent span as both its parent and its `agent_span_id`, so ownership never depends on the package's inference. The output is a plain dict; `Session.model_validate` is the whole adapter, and it was validated against the 1.7.0 model when PR-1 was built. This is enough for `TraceExtractor` at every level, and it is the shape the spike said a hand-built session would need.

### 6.4 Scrubbing

Before any judge reads a `Session`, `scrub.py` removes what gives the arm away or carries bulk: `<document-digest>` blocks, `document_read` result previews, attachment markers, and tool results over a size cap. The judges see the user's prompt, the final answer and the tool names and arguments. This is the blinded judge the offload evaluation asked for and the managed service could not provide.

## 7. The CLI

```bash
cd backend && uv run python scripts/eval_harness.py --help
```

| subcommand | spends | what it does |
|---|---|---|
| `cases` | no | validate every case file and fixture reference, print the family table and the tools and skills the eval user must be granted |
| `run` | **yes** | run `--family` (default all) with `--k` samples on `--model-id`, as the headless-grant owner `--user-id` on `--prefix` (or against a local stack with `--invocations-url`); estimate first, stop without `--yes`; resumable into `--out/results.jsonl`; refuses anything that looks like production |
| `judge` | **yes** | run the case's listed evaluators (and `rubric`, through `OutputEvaluator`) over the scrubbed sessions in `--out`, with `--judge-model` (default Sonnet 4.6 regional); estimate first, stop without `--yes`; resumable into `judgements.jsonl`; skips cases with no evaluators. `score` then prints an advisory table of paired mean judge scores against the baseline's, and refuses to compare across judge models |
| `score` | no | compare `--out` with `baselines/<model-id>.json` per family (§8); exit non-zero on a gating regression |
| `baseline` | no | promote `--out` to `baselines/<model-id>.json`, refusing unless every case in the golden set ran at full `k`; prints the cases whose outcome changes |
| `cleanup` | no | delete the sessions and fixture uploads recorded in `--out` |

`judge` arrives with PR-2. `run` and `judge` are separate because judging is the expensive half and is often wanted on a subset (`--family general_chat`), and because the deterministic gate must be readable before a cent of judge spend.

## 8. Baselines, regressions, and reading a failure

A baseline is the per-case majority outcome of a promoted run on one model id, one representative answer per case, and the run's metadata (date, target, commit, image tags from SSM, k; the eval user's role hash joins with PR-3). It never carries the user id. `score` prints, per family:

```
family              n   base  run   flips(+/-)  McNemar p   gate
tool_selection     12   12    10    0/2         0.500       pass
document_qa        16   15    11    0/4         0.125       pass
instruction        14   14     8    0/6         0.031       FAIL
retention           8    8     5    0/3         0.250       FAIL (lost 3 of 8)
```

The gate rule is D5 made precise. A deterministic family fails when either holds:

1. it lost more cases than it gained, and the two-sided exact McNemar test on the discordant cases (`mcnemar_exact(b, c)`, the helper both harnesses use) gives `p < 0.05`; or
2. the family has fewer than six discordant cases, where that test cannot reach 0.05 at all, and the run lost at least a third of the baseline's passes with no gains.

The second clause exists because a family of eight cases losing three is a regression however the statistic reads, and the families here are small by design. Judge families print the same table with mean score in place of the pass count, and never a gate.

Improvements never fail a run. Reading a failure: `score --explain <family>` prints each flipped case with the run's failure reasons, its tool calls, the baseline's answer (stored in the baseline file, since the corpus is synthetic) and the run's answer. The `C#`-row cache status is open question 3. Most regressions found so far were one of three things, and the explain output is built to separate them: a tool that stopped being called (trace diff), a fact that stopped being retained (answer diff with `UNKNOWN`), or a model that changed under the run (baseline model id ≠ run model id, which `score` refuses outright).

A baseline is re-promoted only on purpose: after a release, after a deliberate model or prompt change, with the diff reviewed in the PR that commits it. A baseline that moves with every run is no baseline.

## 9. Families for phase 1

| family | catches | scorer | gates |
|---|---|---|---|
| `tool_selection` | a tool description or system-prompt edit that stops the model reaching for the right tool, or sends it on a search spree (the myBoiseState 14-call turn) | `must_call`, `must_not_call`, `in_order`; `tool_selection` judge advisory | yes |
| `document_qa` | an attachment, digest or `document_read` change that loses a fact, or a model that invents one; three small synthetic fixtures with planted distractors, `literal` and `paraphrase` wording, and an absent-fact case | `contains_any` / `forbidden` / `regex` | yes |
| `retention` | a history, truncation-anchor or compaction change that forgets a constraint stated earlier in the same conversation; 3–6 turn cases, the fact planted on turn 1 and asked on the last | `contains_any` / `forbidden` | yes |
| `instruction_following` | a system-prompt change that breaks format compliance ("exactly three bullets", "reply with only the number", "do not use a tool") | `regex`, `must_not_call` | yes |
| `refusal` | a safety or prompt regression in either direction: a harmful request answered, or a benign request refused | `refuses: true/false` with `forbidden`; `refusal` judge advisory | yes |
| `skill_selection` | a catalogue or progressive-disclosure change that stops a skill loading when its description matches (the myBoiseState skill never activated across four turns), or loads one that does not; built on the two skills bootstrap seeds, `web_research` and `skill_creator` | `skill` | yes |
| `general_chat` | the thing no deterministic check sees: a plainly worse answer | `helpfulness`, `faithfulness` judges | no |

PR-1 ships 51 cases, eight or nine per gating family; `general_chat` (six cases) comes with the judges in PR-2. The small fixtures are inline-class documents. The large-document fixtures (`large_document_quality/fixtures.py`) join `document_qa` once the harness can wait for the prepare pipeline the way that harness's `wait` step does. The compaction-sized retention question (a cut that actually fires) stays in the compaction harness: forcing a cut needs seeded memory records and 200k characters of filler, which is a different run shape, and that harness already drives the real `TurnBasedSessionManager`.

## 10. Cost, time and where it runs

Per full run on dev with Haiku 4.5 as the model under test and Sonnet 4.6 as judge, before measuring:

| item | estimate |
|---|---|
| turns | PR-1's 51 cases × 3 samples is 153 samples and 219 turns |
| agent spend | measured $0.86 for the first full run on Haiku 4.5 (2026-10-30); `run` now estimates $1.20 from that run's token mix |
| judge spend | ≈ $2–4 (six judged cases × 3 samples × 2 evaluators, plus advisory judges) |
| wall clock | about 7 minutes measured with `run --workers 4` (one session per case, so cases parallelise; turns within a case do not) |

Where it runs, added to `docs/testing/smoke-regression.md` §4:

| change touches | run |
|---|---|
| the system prompt, a tool description in the catalogue, skills disclosure, compaction or history code, document offload, a model default | `run` + `score` on the affected families against dev once the branch is there, numbers in the PR, the way TTFT evidence is owed today |
| a release | the full run against dev on the release branch, after L1, before the release PR; `score` output pasted into the release notes' verification section |
| nightly | `--family tool_selection,instruction_following --k 1` against the nightly stack, `continue-on-error` with the JSON uploaded, as `turn-smoke.sh` does; promoted to blocking once green for two weeks |

As built, `scripts/nightly/eval-harness.sh` seeds the eval user on the ephemeral stack, runs `tool_selection`, `document_qa`, `retention`, `instruction_following` and `refusal` once per case, and scores them against the committed Haiku baseline. `skill_selection` is left out because the nightly stack never runs the bootstrap seeding, so its two skills do not exist there. The credentials parameter outlives the stack's teardown, but the nightly prefix is reused, so the next night overwrites the same one.

## 11. Non-goals for phase 1

- Multi-arm experiments (A/B of two configs in one run). A baseline comparison is a two-arm experiment with one arm stored. The scoping doc's §2.3 is why the live arms stay out: `run_agent_headless` varies only what `InvocationRequest` carries (model, tools, agent, inference params), so a compaction or offload arm needs either an offline driver (the compaction harness) or a runtime environment change between arms. Those harnesses keep their own `arms.py`; this one compares a deploy with the last promoted one.
- Simulated users and multi-turn goal success (`ActorSimulator`, `GoalSuccessRateEvaluator`). Worth it for phase 4, where a user's agent has a goal to state.
- Chaos and red-team evaluators. The package has them; nothing here precludes them; nothing here needs them.
- Persisting results anywhere but the run directory and the committed baseline. Phase 3 decides the row.

## 12. The two later phases, so phase 1 does not block them

**Phase 3 — an admin readout.** A read-only Evaluations page listing runs and per-family trends, then a "run now" background task, the way `POST /admin/feedback/evaluations/run` works. It needs phase 1's run report written somewhere durable with a stable shape; §8's report is that shape, and it must not grow fields a page cannot render. Not before the script has run a dozen times.

**Phase 4 — per-agent test sets.** An owner attaches cases to an Agent or Project in the Designer, runs them as themself against the draft and the published version, and sees the flips. Same runner, same mapper, same judges; the cases carry `agent_id` and spend lands on the owner's quota. It needs its own RBAC, a judge-model choice, UX and a feature flag defaulting off. §5's format already has room for `run.rag_assistant_id` and `run.agent_type` because the runner does.

## 13. Punch list

**PR-1 — the seam and the set. BUILT 2026-10-29.**

- `apis/shared/evals/` as laid out in §6, with the judge protocol only.
- The six deterministic families, 51 cases, and three synthetic fixtures under `backend/evals/`.
- `scripts/eval_harness.py` with `cases`, `run`, `score`, `baseline` and `cleanup`.
- `file_upload_ids` and `enabled_skills` passthroughs on `run_agent_headless`, with a test.
- The smoke matrix's upload helper and the compaction harness's scoring helpers now come from `apis/shared/evals/`.
- Tests: the committed set loads and validates on every CI run; scoring, the gate rule, execution against a stand-in runner, baselines, the CLI's exit codes, and the mapper's shape. The shape test validates against the package when it is installed (PR-2); until then it is pinned field by field.
- Not done: no run against a deployed stack, and no baseline committed. Both need the eval user, which is PR-3.

**PR-2 — the judges. BUILT 2026-10-30.**

- `StrandsEvalsJudge` in `apis/shared/evals/judges.py` maps the six case evaluator names to strands-evals 1.7.0 classes and a case `rubric` to `OutputEvaluator`. It imports the package lazily, records a judge that fails or has nothing to score (a tool-level judge on a turn with no tool calls) as a verdict with no score, and keeps the other verdicts. `judge_row` scrubs before any judge reads a session.
- The case loader's evaluator list is derived from the adapter, so the two cannot drift.
- `eval_harness.py judge`; `score` prints judge deltas; `baseline` records per-case mean judge scores and the judge model, refusing a partly judged run or mixed judges.
- Six `general_chat` cases, three with their own rubric. The `refusal` and `tool_selection` cases gain their advisory judges, as §9 planned.
- Tests run against a stand-in package with the 1.7.0 shapes.

- `strands-agents-evals==1.7.0` in the `dev` extra, locked with uv 0.7.12 so the lock stays at revision 2. The one other lock change is `rich` 14.3.3 to 15.0.0, which the full backend suite passes on.
- **The refusal judge is oriented by the case.** strands-evals' `RefusalEvaluator` scores 1.0 for "refused", which is right for a harmful request and wrong for a benign one; a mean across both would let a newly refused benign request cancel a newly answered harmful one. `orient_refusal` inverts the score when the case expects no refusal, and the loader rejects a refusal judge on a case without `expect.refuses`.
- A case counts as judged when a judge ran, even if it had nothing to score (the tool-selection judge on a case that rightly calls no tool).
- The re-promoted baseline (dev, 2026-10-30, Haiku 4.5, k=3, judged by Sonnet 4.6) passes 54 of 57 cases. Judge means: tool selection 1.00, refusal 1.00 once oriented, and for `general_chat` helpfulness 0.84, faithfulness 0.86, the case rubrics 0.78 and conciseness 0.44. The last says Haiku answers at more length than the judge thinks the questions need.
- `instruction_following.word_limit` sits on its 15-word boundary and flips between runs; it is a candidate for a wider margin.

**PR-3 — the eval user and the wiring. BUILT 2026-10-30.** `scripts/seed_eval_user.py` and the `--auth cognito` sign-in (D7); the eval user created on dev; the first baseline, `backend/evals/baselines/us.anthropic.claude-haiku-4-5-20251001-v1_0.json`, from a full k=3 run on dev as that user; the smoke doc's layer table, §4 and §5; the release skill's quality-evidence step; and the nightly step (§10). Not verified: the nightly step has not run yet, and whether the nightly deploy role may write an SSM SecureString is unknown until it does. A failure there is non-blocking and reports exit 2.

## 14. Open questions

1. ~~**Does the eval user need every tool, or a fixed small set?**~~ Answered by PR-3: exactly what the golden set offers, derived from the case files. Each case names its own `enabled_tools`, so the model chooses among a fixed set either way; the role only has to make that set reachable.
2. **Judge model.** Sonnet 4.6 regional is the proposal; the compaction harness judged with Haiku and found it adequate for exact-answer families. Decide on the first `judge` run's disagreement rate with the deterministic gate on the overlapping cases.
3. **Should `score` read the `C#` rows?** It would let a flip be explained by a cache or compaction status in one place. It also couples the script to the sessions-metadata table shape. Leaning yes, best-effort, behind `--explain`.

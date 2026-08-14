---
name: kaizen-research
description: Weekly Friday early-morning scan of the three libraries this stack is built on — Strands Agents, AWS Bedrock, and Bedrock AgentCore — plus their dependency releases, asking exactly two questions: what NEW capability do they now enable, and what CUSTOM CODE do they now let us delete. Tracks AWS Bedrock + AgentCore announcements/release notes/pricing, Strands Agents releases and issues, the bedrock-agentcore SDK + starter-toolkit, the aws-samples/sample-strands-agent-with-agentcore reference repo, Bedrock model-catalog changes (including vendor capability announcements filtered to "is this reachable on Bedrock?"), and the MCP/FastMCP surface *only where it touches AgentCore Gateway or our MCP host*. Audits internal signals (recent commits, open PRs, CI failures, version-pin lag, dormant skills). Outputs a dated research doc + queues ideas in `docs/kaizen/review-queue.md` for that same morning's `kaizen-review-prep` (runs ~2 hours later) to rank into decisions. Opens a PR into `develop`. **Out of scope**: general agentic UI/UX trends, competing agent harnesses (Claude Code, opencode, LangChain, pydantic-ai), parallel chat platforms (LibreChat), frontend component libraries (Vercel AI SDK, assistant-ui), UX research, HN/Reddit community signal, and security advisories / Dependabot / CodeQL. Triggers: "kaizen research", "weekly research scan", "external scan", "what should we look at this week".
---

# Kaizen Research

Friday early morning. The "what did Strands, AgentCore, and Bedrock ship this week that we should use or delete code for — and what is our own week telling us?" scan. Pairs with `kaizen-review-prep` which runs ~2 hours later the same morning and ranks this skill's output into a decision agenda — both docs ready before Phil sits down to review Friday morning.

## Philosophy

### The two questions

This stack is built on **Strands Agents**, **AWS Bedrock**, and **Bedrock AgentCore**. That is the scope. Every item this skill surfaces must answer at least one of exactly two questions:

1. **Capability unlock** — *what can we now build that we couldn't easily build before, because Strands / AgentCore / Bedrock shipped it?* New platform primitives, new SDK hooks, new model capabilities, new Gateway/Memory/Identity/Policy features, new pricing or quota shapes that change an architectural choice.
2. **Addition through subtraction** — *what custom code in this repo can we now delete, because an upstream release does it for us?* When Strands, the AgentCore SDK, or Bedrock ships something we hand-rolled or filed an issue for, the win is closing our version and adopting upstream. Example: Strands v1.37/v1.38 silently closed our open issues #266 and #267 — the codebase shrinks even though we "added" a dep bump.

**An item that answers neither question is out of scope.** Not "low priority" — out. Don't surface it, don't queue it, don't spend web budget on it. Being interesting is not the bar; being *actionable against these three libraries* is.

- **Subtraction is the sharper of the two.** Prefer the item that deletes 200 lines of ours over the item that adds a capability we haven't been asked for. When both are available, the subtraction ships first because it carries less risk and leaves the codebase smaller. Every run should surface at least one deletable-custom-code candidate; if it can't, say so plainly rather than padding.
- **Classify honestly.** A dep-bump's win is usually subtraction; a *new* platform primitive's win is usually capability unlock. Don't hedge an unlock into "replaces future glue we haven't written" — that under-weights the real story (the 2026-05-10 BYO-filesystem item made exactly this mistake). And don't dress an addition up as a subtraction because the format has a `Subtracts` field.
- **The libraries are the lens, not the whole world.** Model-vendor announcements, MCP spec changes, and FastMCP releases are in scope **only through a Bedrock/AgentCore aperture**: *is this reachable on Bedrock, does Strands expose it, does it touch our AgentCore Gateway or MCP host?* If the answer is no, it's out — however good the idea is.
- **Subagent fan-out.** External sources are independent — fan them out to parallel subagents and synthesize. Keeps the main context clean and runs faster.
- **Web budget soft cap.** Target ≤30 web requests (narrowed from 50 when the scope narrowed — fewer sources, scanned deeper). If a source is exhausted, unreachable, or rate-limited, list it as "not scanned this week" — don't skip silently. Modest overage is fine when it's surfacing real signal; document it in the Web Budget block. Don't pad.
- **Cite everything.** Every external claim gets a URL + access date in the Sources Scanned appendix. Web findings rot fast and you'll re-read them next week.
- **No edits outside `docs/kaizen/`.** This skill writes a dated research doc and updates `review-queue.md`. It never touches `backend/`, `frontend/`, `infrastructure/`, `CLAUDE.md`, or skill files.

## When to run

Friday early morning (~6am MT). `kaizen-review-prep` runs ~2 hours later (~8am MT) so both docs are waiting when Phil sits down Friday morning. Phil reviews, picks 1–3 to ship over the coming week, and POCs additional items over the weekend. Last weekend's POC findings surface in *this* run's review-prep as Carried Over items (lifted from comments on the previous week's research PR).

## Sources

### External (web — last 7 days unless noted)

> **Scope guard.** Sources 1–5 are the point of this skill. Sources 6–7 are in scope *only* through a Bedrock/AgentCore aperture. Anything not on this list is out of scope — see "Explicitly out of scope" below. Don't reintroduce a source because a single item looked interesting; propose a scope change instead.

#### Core — Strands, AgentCore, Bedrock

1. **AWS Bedrock + AgentCore announcements**
   - https://aws.amazon.com/about-aws/whats-new/recent/feed/ (canonical AWS What's New RSS — filter for Bedrock/AgentCore)
   - https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/release-notes.html (AgentCore release notes — often ahead of What's New)
   - https://aws.amazon.com/blogs/machine-learning/ (filter: bedrock, agentcore)
   - Filter to: AgentCore Runtime / Gateway / Memory / Identity / Policy / Code Interpreter / Browser / Evaluations; Bedrock models, Knowledge Bases, Guardrails, Mantle; availability, region, quota, and default-behavior changes.
   - **Highest-value shape**: a new AgentCore primitive that replaces something we hand-rolled (managed Harness vs our headless lane, managed Memory vs our session manager, Gateway rate limits vs our app-layer quota).

2. **Strands Agents SDK** — the harness we run on. Read every release in window, not just the headline.
   - https://github.com/strands-agents/sdk-python/releases
   - https://github.com/strands-agents/sdk-python/issues?q=is%3Aissue+sort%3Aupdated-desc
   - https://pypi.org/project/strands-agents/ + `strands-agents-tools` (version + release date; the registry JSON is authoritative for dates)
   - For each release identify: breaking changes; new hooks, plugins, model/session primitives; and — most importantly — **any feature that duplicates code in `backend/src/agents/main_agent/`**. Explicitly check the version we *already run*, not just newer ones: a primitive shipped two minors ago that we never adopted is a subtraction sitting on the table.
   - ⚠️ Release notes are **monorepo-wide** (tags `python/vX`, `typescript/vX`, `mcp/vX`) and `CHANGELOG.md` on `main` is gone. Diff the wheels before acting on a specific feature claim.

3. **AgentCore SDK + starter-toolkit** — the library and its early-signal bug tracker.
   - https://github.com/aws/bedrock-agentcore-sdk-python/releases
   - https://github.com/aws/bedrock-agentcore-sdk-python/issues?q=is%3Aissue+sort%3Aupdated-desc
   - https://github.com/aws/bedrock-agentcore-starter-toolkit/issues
   - Identify: fixes that close a hazard we guard against by hand (the win is deleting our guard); open issues that describe a failure mode we're exposed to; limits with no upstream fix, which stay our problem.

4. **Reference repo — `aws-samples/sample-strands-agent-with-agentcore`** — the only third-party codebase in scope, because it is Strands + AgentCore doing what we do.
   - https://github.com/aws-samples/sample-strands-agent-with-agentcore/commits/main
   - Diff since the last research run. Identify patterns worth porting, approaches they *removed*, and places where they use a library primitive we hand-rolled.

5. **Bedrock model catalog, pricing, and quota**
   - https://aws.amazon.com/bedrock/pricing/ + https://aws.amazon.com/bedrock/agentcore/pricing/
   - ⚠️ Both pages are JS-heavy and have returned wrong/stale rows. Prefer the **AWS Price List API** (`aws pricing get-products --service-code AmazonBedrock`) — it is diffable across runs and doesn't require scraping.
   - Model availability and capability changes on Bedrock, including the Mantle endpoint. Price/quota moves that would change an architectural choice (model tiering, cache strategy, memory strategy, compute type).

#### Adjacent — in scope only through a Bedrock/AgentCore aperture

6. **Model-vendor capability announcements — filtered to one question: "is this reachable on Bedrock, and does Strands expose it?"**
   - https://www.anthropic.com/news (primary — Claude is our main model path)
   - Other vendors **only** when the model ships on Bedrock or Mantle.
   - Include a capability item **only** if it plausibly reaches us through Converse / Mantle / Strands. A Claude-API-only feature is out of scope unless the item is explicitly *"probe whether Bedrock exposes this"* — which is itself in scope and often the highest-value idea of a run.
   - Bedrock-relevant technique docs (e.g. Anthropic's prompt-caching / cost-optimization cookbooks) count here **when they document behavior of a model we invoke through Bedrock** — the test is whether the finding is verifiable against our own Converse traffic.
   - Explicitly **not** in scope: model benchmarks, product launches, non-Bedrock API changes, agent-framework news from vendors.

7. **MCP + FastMCP — only where they touch AgentCore Gateway or our MCP Apps host**
   - https://blog.modelcontextprotocol.io + https://modelcontextprotocol.io/specification/versioning (spec revisions, SEP outcomes)
   - https://modelcontextprotocol.io/extensions/apps/overview (MCP Apps — we ship a host)
   - https://github.com/jlowin/fastmcp/releases (our externally hosted servers behind Gateway run FastMCP; not pinned in this repo)
   - The test: does this change how AgentCore Gateway routes/meters/authorizes, how our MCP host negotiates, or how a Gateway-fronted server behaves? If yes, in scope. General MCP-ecosystem news, new community servers, and registry churn are **out**.

8. **Seasonal** (only when in window)
   - AWS re:Invent (late Nov / early Dec) — the year's largest Bedrock/AgentCore drop.
   - If today's date is not in a known window, skip with "no seasonal sources this week".

#### Explicitly out of scope

Removed 2026-08-14 when the loop was narrowed to Strands / AgentCore / Bedrock. Do **not** re-add without an explicit scope change from Phil:

- **Agentic UI/UX pattern sources** — Vercel AI SDK, assistant-ui, Linear/Cursor product blogs, OpenAI Canvas, NN/g UX research. Frontend patterns are real work but they are not library-capability work; they belong in GitHub issues, not the kaizen queue.
- **Competing agent harnesses** — Claude Code CHANGELOG, opencode, LangChain, LlamaIndex, pydantic-ai. Interesting, never actionable against our three libraries.
- **Parallel chat platforms** — LibreChat.
- **Community signal** — HN, Reddit. Historically low yield (an AWS Instances GA post drew 1 point); the AgentCore/Strands issue trackers are the real early-signal source and are already covered by sources 2–3.
- **Security advisories / Dependabot / CodeQL** — dedicated tooling already owns these.

### Internal (this repo)

13. **Recent commits.** `git log develop --since="7 days ago" --oneline --no-merges`. Cluster by area (`backend/`, `frontend/`, `infrastructure/`). Reverts and high-churn files signal pain points.

14. **Open PRs + review comments.** `gh pr list --base develop --state open --limit 20`, then `gh pr view <n> --comments` on the top 3 by comment count. Repeated review feedback is a CLAUDE.md or skill-update signal.

15. **GitHub issues opened in last 7 days.** `gh issue list --state open --search "created:>$(date -v-7d +%Y-%m-%d)"`. Bug clustering = refactor signal.

16. **CI failures.** `gh run list --status=failure --limit 30`. Group by workflow + job. Flaky tests and recurring infra failures.

17. **Recent CHANGELOG.md / RELEASE_NOTES.md entries** (last 14 days). Used as the "don't re-propose what we just shipped" filter.

18. **Skill inventory.** `find .claude/skills -name SKILL.md -exec stat -f "%Sm %N" {} \;`. Skills not modified in 60+ days and not visibly referenced in recent PRs are retirement candidates.

19. **Version-pin lag.** Compute lag in releases and days for each tracked dep. **Tiered — a bump is only Top 5 material when a release note maps to a specific file or failure mode:**
    - **Core (always check, always report)**: `strands-agents`, `strands-agents-tools`, `bedrock-agentcore`, `boto3`/`botocore` (coupled to the AgentCore SDK; the floor is pinned in 4 files), `mcp`.
    - **Supporting (report the row, don't editorialize unless something breaks)**: `fastapi`, `pydantic`, `aws-cdk-lib`, `constructs`, `@angular/core`, `vitest`, `typescript`.
    - Source files: `backend/pyproject.toml`, `frontend/ai.client/package.json`, `infrastructure/package.json`.
    - ⚠️ Verify dates against raw registry JSON (`upload_time_iso_8601`, npm packument `time`) — a summarizer has fabricated a release date before.

20. **Decisions log** — `docs/kaizen/decisions.md` (if it exists). Items previously declined; don't re-propose without materially new context.

21. **Recent reviews** — `docs/kaizen/reviews/*.md` (last 1–2). Used to avoid duplicate proposals.

## Output

### 1. Primary doc — `docs/kaizen/research/YYYY-MM-DD.md`

```markdown
# Kaizen Research — [Day, Month D, YYYY]
> Scan window: [Month D – Month D, YYYY] (7 days)
> Web budget: N/30 used (target).

## TL;DR

[2-3 sentences. The single most important external move and the single most pressing internal signal. Name the recommended #1 idea here.]

## External Scan

### What's moving this week

[1-2 paragraphs — gestalt. What's the shape of the week? Are vendors converging on a pattern? Anything surprise you?]

### Notable items by source

> **Annotation conventions:**
> - `*relevance*:` — impact-on-existing-code lens. What construct/file does this affect? What does it replace, simplify, or obsolete?
> - `*unlocks*:` — capability-unlock lens (use when applicable, especially for *new* platform primitives, SDK hooks, or UX patterns). What net-new product capability or enhancement does this make possible? What could we now build that we couldn't before?
>
> Bug-fixes and incremental dep-bumps usually only need `*relevance*`. New platform features, new SDK primitives, new spec capabilities, and new UX patterns usually deserve both.

#### AWS Bedrock / AgentCore
- **[Item]** — [1-2 sentence summary] — [URL] — *relevance*: [specific construct/file] — *unlocks* (if applicable): [net-new capability this enables] — *subtracts* (if applicable): [custom code this lets us delete]

#### Strands Agents
- **[Release / issue]** — [URL] — *relevance*: [which of our files does this touch?] — *subtracts*: [what in `agents/main_agent/` becomes deletable, or "nothing"]
- Include a **"already in our pin but unadopted"** sub-list: primitives in the version we *currently run* that duplicate our custom code. These are free subtractions and are routinely missed by release-note-only scanning.

#### AgentCore SDK + starter-toolkit
- **[Release / issue]** — [URL] — *relevance*: [fix that retires one of our guards / open failure mode we're exposed to]

#### Reference repo (aws-samples/sample-strands-agent-with-agentcore)
- **[Commit / change]** — [diff summary] — [URL] — *applicability*: [does our equivalent do this differently? do they use a library primitive we hand-rolled?]

#### Bedrock model catalog / pricing / quota
- **[Change]** — [URL] — *relevance*: [architectural choice this would shift — model tiering, cache strategy, memory strategy, compute type]

#### Model-vendor capability — Bedrock reachability only
- **[Capability]** — [URL] — *Bedrock reachable?*: [yes / no / **unknown — probe it**] — *does Strands expose it?*: […] — *relevance*: […]
- An **unknown** here is a legitimate and often high-value Top 5 idea: "probe whether Bedrock exposes X". A confirmed **no** closes the question and is also worth recording.

#### MCP / FastMCP — Gateway + MCP-host surface only
- **[Spec change / release]** — [URL] — *does it touch AgentCore Gateway routing/metering/auth, our MCP Apps host, or a Gateway-fronted server?*: […] — if no, it should not be in this doc.

#### Seasonal
- [content, or "Out of window — none scanned this week"]

### Patterns worth considering

- **[Pattern]** — [3 sentences: what it is, where it's appearing, fit for this repo]
  - **Where**: [examples]
  - **Fit**: [would this help? what does it replace? cost to adopt?]
  - **Verdict**: [Worth trying / Not a fit / Monitor]

## Internal Audit

### Activity (last 7 days)
- **Commits on develop**: N (across N PRs)
- **PRs opened**: N — **merged**: N — **reverted**: N
- **Issues opened**: N — **closed**: N
- **CI failures (workflow → count)**: …

### Repeated friction signals
- **[Pattern]** (N occurrences) — [evidence: commit SHAs, PR numbers, issue links]
  - **Hypothesis**: [root cause]
  - **Fix candidate**: [specific change — file + behavior]

### Version-pin lag
| Dep | Pinned | Latest | Lag | Notes |
|---|---|---|---|---|
| strands-agents | x.y.z | a.b.c | N releases / N days | [breaking? new feature relevant to us?] |

### Retirement candidates
- **[Skill / file / config]** — [evidence: not modified in N days, replaced by X, never referenced]

### Risks introduced this week
<!-- Defensive scanning — things that could break us if ignored. -->
- **[Risk]** — [source URL or PR] — *what breaks if we ignore this*

## Ideas — Top 5 (ranked)

| # | Idea | Surface | Effort | Impact | Subtracts? | Unlocks? |
|---|---|---|---|---|---|---|
| 1 | [Title] | backend / frontend / infra / cross-cutting | L/M/H | L/M/H | [what it retires, or "addition only — justified because…"] | [net-new capability, or "—" if not applicable] |
| 2 | … | | | | | |

### 1. [Idea title]
- **Source**: [external item / internal signal — URL or commit SHA]
- **Surface area**: [paths affected]
- **Change**: [what specifically would change]
- **Subtracts**: [what this retires/simplifies, or explicitly: "addition only — justified because…"]
- **Unlocks** (if applicable): [net-new product capability, UX pattern, or enhancement this enables — bulleted if multiple. Omit field when not a capability-unlock item.]
- **Effort × Impact**: [Low/Med/High] × [Low/Med/High]
- **Verdict**: [Worth trying / Not a fit / Monitor]

### 2. …

## Take

[2-4 sentences. Net read of the week. Is the system trending toward the ecosystem or away from it? One change that would matter most. What Phil would notice first if shipped.]

---

## Sources Scanned

| # | Source | URL | Accessed | Items |
|---|---|---|---|---|
| 1 | AWS Bedrock What's New | https://… | 2026-05-10 | 3 |

## Web Budget

Used: N / 30 requests (target).
Skipped (unreachable / rate-limited): [list]
Skipped (other): [list with reason]
Notes: [if the cap was exceeded, name the source category that justified it]
```

### 2. Handoff — `docs/kaizen/review-queue.md` (rolling, not dated)

The explicit contract with `kaizen-review-prep`. This skill **appends** new entries under `## Open`. It never edits `## Resolved` (review-prep does the move).

```markdown
# Kaizen Review Queue

Items added by `kaizen-research`, consumed by `kaizen-review-prep`.

## Open
<!-- Newest at top. -->

### [YYYY-MM-DD] [Idea title]
- **Source**: research/YYYY-MM-DD.md
- **Surface**: backend | frontend | infrastructure | cross-cutting
- **Effort × Impact**: L/M/H × L/M/H
- **Subtracts**: [yes — what / no — justification]
- **Unlocks** (if applicable): [net-new capability, UX pattern, or enhancement this enables; bulleted if multiple. Omit when not a capability-unlock item.]
- **Status**: open

## Resolved
<!-- kaizen-review-prep moves entries here after a review. -->

### [YYYY-MM-DD] [Idea title]
- **Source**: research/YYYY-MM-DD.md
- **Decision**: Ship | Decline | Defer until [date]
- **Reasoning**: [Phil's reason, one sentence]
- **Reviewed in**: reviews/YYYY-MM-DD.md
```

## How to run

1. **Bootstrap.** If `docs/kaizen/`, `docs/kaizen/research/`, `docs/kaizen/reviews/`, or `docs/kaizen/review-queue.md` don't exist, create them. The queue starts with the headers above and empty sections.

2. **Read recent context** (sequential — small reads):
   - Last 1-2 files in `docs/kaizen/research/`
   - Last 1-2 files in `docs/kaizen/reviews/`
   - `docs/kaizen/decisions.md` if present
   - `docs/kaizen/review-queue.md`
   - Last 14 days of `CHANGELOG.md` and `RELEASE_NOTES.md`

3. **Inventory internal signals** (parallel Bash calls):
   - `git log develop --since="7 days ago" --oneline --no-merges`
   - `gh pr list --base develop --state open --limit 20`
   - `gh issue list --state open --search "created:>$(date -v-7d +%Y-%m-%d)"`
   - `gh run list --status=failure --limit 30`
   - `find .claude/skills -name SKILL.md -exec stat -f "%Sm %N" {} \;`
   - Read pinned versions from the three manifest files.

4. **Fan out external scan** — spawn parallel `general-purpose` subagents (or `Explore` where a source needs several targeted lookups). **One subagent per source category 1–7** (seven, down from fifteen). Sources 1–5 get the depth; 6–7 get a light touch and a hard aperture instruction. Each subagent receives:
   - The exact URLs to scan
   - Scope: last 7 days (or "since the last research run", whichever is longer)
   - Web budget for that subagent (3–5 requests soft target)
   - **The two questions, verbatim**, plus the instruction: *return nothing for items that answer neither.* An empty report is a valid and useful result — do not pad.
   - Required output: 3–5 bullet items max — title, 1–2 sentence summary, URL, and an explicit *unlocks* and/or *subtracts* line naming the file or construct involved.
   - **Required**: cite URLs; never fabricate. Registry JSON (PyPI/npm `time` maps) is authoritative for release dates — a summarizer has fabricated one before.

   Total budget across subagents targets ≤30. Modest overage is acceptable when surfacing real signal; beyond that, stop and document the skip.

5. **Version-pin diff.** For each tracked dep, fetch latest release version (WebFetch on the release page or registry equivalent — counts toward budget). Compute lag in releases and days. If a budget hit prevents a check, list the dep under "Skipped".

6. **Synthesize.** Write the research doc per the shape above. Pull subagent reports verbatim into source sections; write the gestalt narrative (TL;DR, "What's moving", Take) yourself. **Top 5 weighting, in order:**
   1. **Deletable custom code** — an upstream release that lets us remove something we maintain. Highest rank, because it is the lowest-risk win and leaves the codebase smaller. Name the file.
   2. **Unadopted capability already in our pin** — a Strands/AgentCore primitive we could use *today* with no bump. Nearly free; routinely missed.
   3. **New capability unlock** — a genuinely new Strands/AgentCore/Bedrock primitive enabling product surface we couldn't easily build before. Rank on strategic merit, not on whether it intersects existing code.
   4. **Probe items** — an unresolved "is this reachable on Bedrock?" question sitting under queued work. Cheap, and a negative result is a real deliverable.
   5. **Version-pin lag with a named consequence** — a bump is only Top 5 material when a specific release note maps to a specific file or failure mode. "We're N minors behind" alone is a table row, not an idea.

7. **Update review queue.** For each Top 5 idea, prepend a new entry under `## Open` in `docs/kaizen/review-queue.md`. Never touch `## Resolved`.

8. **Open a PR** — see "PR creation".

## PR creation

```bash
DATE=$(TZ=America/Denver date +'%Y-%m-%d')
BRANCH="kaizen/research-${DATE}"

git checkout -b "$BRANCH" develop
git add docs/kaizen/
git commit -m "$(cat <<EOF
chore(kaizen): weekly research scan ${DATE}

Generated by the kaizen-research skill. Top 5 ideas appended to
docs/kaizen/review-queue.md for the kaizen-review-prep run later this morning.
EOF
)"
git push -u origin "$BRANCH"

gh pr create --base develop --head "$BRANCH" \
  --title "chore(kaizen): weekly research scan ${DATE}" \
  --body "$(cat <<'EOF'
## Summary
- External scan (narrowed to Strands / AgentCore / Bedrock): AWS Bedrock + AgentCore announcements, Strands releases + issues, AgentCore SDK + starter-toolkit, the reference repo, Bedrock model catalog/pricing/quota, plus model-vendor capability and MCP/FastMCP **only where Bedrock-reachable or Gateway-touching**.
- Internal audit: recent commits, open PRs, GitHub issues, CI failures, version-pin lag, retirement candidates.
- Top 5 ideas in the dated research doc and queued in `docs/kaizen/review-queue.md`.

## Review
- Read the research doc.
- Comment on the PR with reactions and any weekend POC findings — these become first-class signal for *next* Friday's `kaizen-review-prep`.
- POC promising ideas over the weekend.

## Decision
Ship the doc to `develop`. Ranking into decisions happens in the kaizen-review-prep PR opened later this morning. Action on individual ideas happens in separate PRs the following week.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

The branch is one-shot — squash-merging the PR lands the doc on `develop` and the branch can be deleted.

## Rules

- **Scope is the first filter, not the last.** Before writing an item down, answer: *does this let us build something new on Strands/AgentCore/Bedrock, or delete custom code?* If neither, it doesn't go in the doc — not even as a "worth noting". A shorter, sharper doc is the goal of the narrowing.
- **Every Top 5 idea names a file.** "Adopt Strands hooks" is too vague. "Delete `_build_filtered_tools`'s scoped-id filtering in favour of Strands 1.51 MCP name-prefix filtering" is actionable.
- **Check the pin we already run, not just newer ones.** The cheapest subtractions are primitives that shipped in a version we installed months ago and never adopted.
- **No fabrication.** If a source is rate-limited or empty, list it as "not scanned" — don't invent content. The Sources Scanned table is auditable. Registry JSON is authoritative for versions and dates.
- **Web budget is a soft target, not a hard cap.** ≤30 requests is the goal. Overage is acceptable when justified by signal (document it). Don't pad — if a source is empty after one fetch, move on.
- **Subtraction first.** Every run should surface at least one deletable-custom-code candidate. If it genuinely can't, say so — that's a finding about the week, not a reason to pad the list.
- **Honest about dry weeks.** A quiet week produces a short doc. With the narrowed scope, dry weeks will be more common and that is the intended trade: fewer, better items.
- **Out-of-scope work still exists — it just isn't kaizen's.** A good frontend/UX idea or a competing-harness pattern that surfaces incidentally goes in a GitHub issue, not the review queue. Note it in one line and move on.
- **No edits to source code.** This skill only writes under `docs/kaizen/`.
- **Don't re-propose declined ideas** without materially new context. Check `docs/kaizen/decisions.md` and recent reviews.
- **Cite everything.** Every external claim has a URL + access date in the Sources Scanned appendix.
- **Don't auto-merge the PR.** Phil reviews and merges Friday morning. Review-prep runs against the unmerged PR's docs — it reads the file from the working tree, not from `develop`.

## Confirmation

After the PR is opened, tell Phil:
1. PR URL.
2. Top 1-2 ideas (title + Effort×Impact).
3. One-sentence Take.
4. Web budget used (N/30 target) and any skipped sources.
5. Whether the run found at least one **deletable-custom-code** candidate — and say so plainly if it didn't.

Brief. The full doc is on the PR.

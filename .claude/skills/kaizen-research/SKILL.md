---
name: kaizen-research
description: Weekly Friday early-morning external + internal scan for emerging functionality, agentic trends, tools, and feature/UX improvements in the AgentCore Public Stack repo. Tracks AWS Bedrock + AgentCore announcements, Strands Agents releases, FastMCP (used by externally hosted MCP servers), the aws-samples/sample-strands-agent-with-agentcore reference repo, the MCP ecosystem (including MCP Apps + extensions), frontier model announcements, agent-harness patterns (including opencode (anomalyco/opencode) as an open-source coding-agent harness reference scanned through tooling, cost-effectiveness, and context-engineering lenses — releases-first, light touch), agentic UI/UX patterns (MCP Apps, Vercel AI SDK, assistant-ui, NN/g AI research, Linear/Cursor/Anthropic product blogs), and LibreChat as a parallel open-source agentic-platform reference (releases-first, light touch). Audits internal signals (recent commits, open PRs, CI failures, version-pin lag, dormant skills). Outputs a dated research doc + queues ideas in `docs/kaizen/review-queue.md` for that same morning's `kaizen-review-prep` (runs ~2 hours later) to rank into decisions. Opens a PR into `develop`. **Out of scope**: security advisories / Dependabot / CodeQL — those have dedicated tooling and don't need a weekly kaizen lens. Triggers: "kaizen research", "weekly research scan", "external scan", "what should we look at this week".
---

# Kaizen Research

Friday early morning. The "what's the rest of the world learning that we should consider, and what's our own week telling us?" scan. Pairs with `kaizen-review-prep` which runs ~2 hours later the same morning and ranks this skill's output into a decision agenda — both docs ready before Phil sits down to review Friday morning.

## Philosophy

- **Subtraction first.** Every research run should propose at least as many things to *remove or simplify* as to add. A smaller stack you trust beats a bigger one you route around. **Subtraction explicitly includes replacing custom code with library-native equivalents** — when an upstream release (Strands, AgentCore SDK, FastMCP, MCP, etc.) ships a capability we'd already built or filed an issue for, the win is closing our version and adopting upstream. Example: the 2026-05-10 bootstrap run found that Strands v1.37/v1.38 silently closed our open issues #266 and #267 — the codebase surface area shrinks even though we "added" a dep bump.
- **Dual lens — impact + capability-unlock.** Evaluate every upstream feature through *two* lenses, not one: (a) **impact on existing code** (does it change, simplify, or obsolete something we already have?) and (b) **capability unlock** (what *new* product capability, UX pattern, or enhancement does this make possible that we couldn't easily do before?). Subtraction-first still applies to the first lens. But capability-unlock items — features that enable net-new product surface — must be evaluated on their strategic merit, *not* hedged into "replaces future glue we haven't written." Example: the 2026-05-10 AgentCore Runtime BYO filesystem was first framed only as "could replace future filesystem-staging glue" — under-weighting the real story (code-interpreter sandboxes, cross-session uploads, shared skill hot-swap, persistent vector indexes). A dep-bump's win is usually subtraction; a *new* platform primitive's win is usually capability unlock. Don't mis-classify.
- **Subagent fan-out.** External sources are independent — fan them out to parallel subagents and synthesize. Keeps the main context clean and runs faster.
- **Web budget soft cap.** Target ≤50 web requests. If a source is exhausted, unreachable, or rate-limited, list it as "not scanned this week" — don't skip silently. Going modestly over the cap (say, to 60) is fine if the extra requests are surfacing real signal; document the overage in the Web Budget block. Don't pad — if 30 requests covered every source meaningfully, stop at 30.
- **Cite everything.** Every external claim gets a URL + access date in the Sources Scanned appendix. Web findings rot fast and you'll re-read them next week.
- **No edits outside `docs/kaizen/`.** This skill writes a dated research doc and updates `review-queue.md`. It never touches `backend/`, `frontend/`, `infrastructure/`, `CLAUDE.md`, or skill files.

## When to run

Friday early morning (~6am MT). `kaizen-review-prep` runs ~2 hours later (~8am MT) so both docs are waiting when Phil sits down Friday morning. Phil reviews, picks 1–3 to ship over the coming week, and POCs additional items over the weekend. Last weekend's POC findings surface in *this* run's review-prep as Carried Over items (lifted from comments on the previous week's research PR).

## Sources

### External (web — last 7 days unless noted)

1. **AWS Bedrock + AgentCore "What's New"**
   - https://aws.amazon.com/about-aws/whats-new/recent/feed/ (canonical AWS What's New RSS — filter entries for Bedrock/AgentCore)
   - https://aws.amazon.com/blogs/machine-learning/ (filter: bedrock, agentcore)
   - Filter to: Bedrock, AgentCore, Bedrock Agents, Knowledge Bases, Guardrails, model availability/region/quota changes.

2. **Strands Agents SDK**
   - https://github.com/strands-agents/sdk-python/releases
   - https://github.com/strands-agents/sdk-python/blob/main/CHANGELOG.md
   - https://github.com/strands-agents/sdk-python/issues?q=is%3Aissue+sort%3Aupdated-desc
   - For each new release, identify: breaking changes, new hooks/features, fixes that map to current usage in `backend/src/agents/main_agent/`.

3. **Reference repo — `aws-samples/sample-strands-agent-with-agentcore`**
   - https://github.com/aws-samples/sample-strands-agent-with-agentcore/commits/main
   - Diff the last 7 days (or "since last research run" — whichever is longer). Identify new patterns, removed approaches, or fixes that map to constructs in this repo: agent setup, tool registration, AgentCore Identity flows, Memory configuration, Gateway/MCP wiring.
   - This repo has historically informed our architecture; week-over-week deltas are first-class signal.

4. **MCP ecosystem**
   - https://modelcontextprotocol.io (blog, spec changes)
   - https://github.com/modelcontextprotocol/servers (new servers, retired servers)
   - MCP registry / awesome-mcp lists for new servers relevant to the stack (Bedrock, AWS, GitHub, Slack, observability).

4a. **FastMCP** — used by our externally hosted MCP servers (Lambda-backed, behind AgentCore Gateway). FastMCP is **not** pinned in this repo's `pyproject.toml`; it lives in the MCP server repos this stack consumes via Gateway. Track upstream releases because changes affect server behavior we depend on.
   - https://github.com/jlowin/fastmcp/releases
   - https://github.com/jlowin/fastmcp/blob/main/CHANGELOG.md
   - https://github.com/jlowin/fastmcp/issues?q=is%3Aissue+sort%3Aupdated-desc
   - https://pypi.org/project/fastmcp/ (for latest version + release date)
   - Identify: breaking changes, new server-side primitives (resources/prompts/tool decorators, lifespan, auth helpers), transport changes (especially relevant if MCP SEP-2567 sessionless transport lands), and Lambda/runtime adapter changes.

4b. **Agentic UI/UX patterns** — emerging UI and UX conventions for AI/agentic apps. We're Angular + Tailwind, so React-specific libraries are **pattern-only** references (extract the idea, implement in signals). Focus on functionality + interaction + visual conventions, not generic "good chat UX".
   - **MCP Apps + extensions** (priority): https://modelcontextprotocol.io/extensions/apps/overview, https://github.com/modelcontextprotocol/ext-apps, https://blog.modelcontextprotocol.io. The "MCP server returns an interactive UI inline with the chat" standard. Track host adoption (Claude Desktop, ChatGPT, VS Code Copilot, Goose, Postman) and new MCP extension SEPs.
   - **AI SDK / Generative UI** (Vercel): https://ai-sdk.dev/docs/ai-sdk-ui, https://ai-sdk.dev/cookbook. Canonical reference for tool-call rendering, multi-step UI, generative UI, streaming state patterns. React, but the patterns port.
   - **assistant-ui**: https://www.assistant-ui.com/docs, https://github.com/Yonom/assistant-ui/releases. React component library purpose-built for AI chat UI. Tracks attachment UX, threading, tool-call rendering primitives.
   - **Vendor product-blog UX writeups**: https://linear.app/blog (Linear Agent), https://www.cursor.com/blog (canvas, agent harness), https://www.anthropic.com/news filtered for `artifact`/`ui`/`design`. Where in-app agentic patterns get documented by the teams shipping them.
   - **OpenAI Canvas + ChatGPT UI**: https://openai.com/blog filtered for `canvas`, `chatgpt`, agent UI updates.
   - **Nielsen Norman Group AI articles**: https://www.nngroup.com/topic/artificial-intelligence/. UX-research perspective; evidence-based; slow cadence — surfaces in ~1 of 4 weekly runs but high signal when it does.
   - Identify: new agentic UI standards (especially MCP Apps + adjacent SEPs), tool-result rendering patterns, attachment/preview UX, multi-agent attribution patterns, consent/elicitation UX, evidence-based usability findings.

5. **Frontier model announcements**
   - https://www.anthropic.com/news
   - https://openai.com/blog (filter: API, agents, tools)
   - https://blog.google/technology/google-deepmind/ (Gemini)
   - https://ai.meta.com/blog/ (Llama)
   - Focus on capability deltas affecting agent harness design: longer context, native tool use changes, prompt caching APIs, computer use, structured output, latency/cost shifts.

6. **Agent harness patterns**
   - https://www.anthropic.com/engineering (Claude Code, agent design posts)
   - https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md
   - LangChain / LlamaIndex / Pydantic-AI release notes — for ideas, not adoption.

6a. **opencode** (`anomalyco/opencode`) — open-source, terminal-native AI coding agent (a Claude Code analog; TypeScript/MIT; very active). Track as a parallel coding-agent-harness reference: how a fast-moving competing harness solves the same problems we face in `backend/src/agents/main_agent/`. Light-touch scan (releases-first); deeper dives only when a release headline maps onto our agent loop, tool layer, or context handling.
   - https://github.com/anomalyco/opencode/releases (primary — read the latest release notes)
   - https://github.com/anomalyco/opencode/commits (supplementary — skim recent commits only if a release headline warrants a closer look; confirm the default branch before relying on a path)
   - Web budget: 1–2 requests per week.
   - Identify across the three lenses we track this repo for:
     - **Tooling** — how tools are defined, surfaced, gated/permissioned, and composed; the built-in tool set; tool-call/permission UX; sub-agent / task delegation. *Maps to*: our ToolRegistry, `agents/main_agent/tools/__init__.py`, RBAC/`enabled_tools`, and the multi-protocol tool architecture (direct / AWS SDK / MCP+SigV4 / A2A).
     - **Cost-effectiveness** — model routing and selection, cheap-vs-capable fallback, prompt caching, token-spend controls, anything that lowers per-turn cost. *Maps to*: model selection in `inference_api`, and our caching/compaction story.
     - **Context engineering** — context-window management, compaction/summarization, file/context selection, prompt assembly, retrieval into the window. *Maps to*: our `compaction` SSE event, session/AgentCore-Memory restore, and agent prompt assembly.
   - TypeScript/CLI app, so any UI items are pattern-only references (extract the idea, implement in Angular signals). If a release headlines something material *outside* these three lenses (e.g., a new MCP capability or UX pattern), flag it for the relevant section rather than expanding the opencode scan inline.

7. **AWS Bedrock pricing + quota**
   - https://aws.amazon.com/bedrock/pricing/
   - Note any model price/quota changes that could shift architecture choices in this repo (e.g., model selection in `inference_api`).

8. **AgentCore SDK / starter-toolkit issues**
   - https://github.com/aws/bedrock-agentcore-sdk-python/issues
   - https://github.com/aws/bedrock-agentcore-starter-toolkit/issues
   - Early-signal bugs/limits other users hit before we do.

9. **Community signal (filtered)**
   - HN search: `site:news.ycombinator.com bedrock OR agentcore OR strands OR "claude code"` (last 7 days)
   - r/LocalLLaMA, r/MachineLearning — agent-harness critiques and patterns surface here before vendor blogs.

10. **Anthropic cookbook**
    - https://github.com/anthropics/anthropic-cookbook
    - Worked examples often outpace docs — especially for caching, tool use, and agent loops.

12. **LibreChat** — open-source ChatGPT-like agentic platform; useful as a parallel-implementation reference cutting across UI/UX, MCP integration, agent/RAG architecture, and provider-routing decisions. Light-touch scan (releases-first); deeper dives only when a release headline maps onto something we're building.
    - https://github.com/danny-avila/LibreChat/releases (primary — read the latest release notes)
    - https://github.com/danny-avila/LibreChat/blob/main/CHANGELOG.md (supplementary if releases are sparse)
    - Web budget: 1–2 requests per week. If a release headlines something material (new MCP capability, attachment UX, agent harness pattern, OAuth/identity flow, multi-model routing), flag it for the Agentic UI/UX or MCP ecosystem sections rather than expanding the LibreChat scan inline.
    - Identify across four lenses: (a) **UI/UX patterns** — chat UX, attachments, tool-call rendering, agent UI; (b) **comparable-platform choices** — agent harness, RAG, multi-provider routing, feature parity vs this stack; (c) **MCP integration** — how they wire MCP servers, tool routing, OAuth/consent; (d) **release-only signal** — feature ships worth knowing about even if we don't act.
    - React/Node app, so UI items are pattern-only references (extract idea, implement in Angular signals).

11. **Seasonal sources** (only when in window)
    - AWS re:Invent (typically late Nov / early Dec) — Bedrock/AgentCore announcements.
    - NeurIPS / ICLR / EMNLP agent tracks (when proceedings drop).
    - If today's date is not in a known window, skip with "no seasonal sources this week".

### Internal (this repo)

13. **Recent commits.** `git log develop --since="7 days ago" --oneline --no-merges`. Cluster by area (`backend/`, `frontend/`, `infrastructure/`). Reverts and high-churn files signal pain points.

14. **Open PRs + review comments.** `gh pr list --base develop --state open --limit 20`, then `gh pr view <n> --comments` on the top 3 by comment count. Repeated review feedback is a CLAUDE.md or skill-update signal.

15. **GitHub issues opened in last 7 days.** `gh issue list --state open --search "created:>$(date -v-7d +%Y-%m-%d)"`. Bug clustering = refactor signal.

16. **CI failures.** `gh run list --status=failure --limit 30`. Group by workflow + job. Flaky tests and recurring infra failures.

17. **Recent CHANGELOG.md / RELEASE_NOTES.md entries** (last 14 days). Used as the "don't re-propose what we just shipped" filter.

18. **Skill inventory.** `find .claude/skills -name SKILL.md -exec stat -f "%Sm %N" {} \;`. Skills not modified in 60+ days and not visibly referenced in recent PRs are retirement candidates.

19. **Version-pin lag.** For each tracked dep, fetch latest release version and compute lag:
    - Backend: `strands-agents`, `boto3`, `botocore`, `fastapi`, `pydantic`, `bedrock-agentcore`, `mcp`
    - Frontend: `@angular/core`, `@analogjs/platform`, `vitest`
    - Infrastructure: `aws-cdk-lib`, `constructs`
    - Source files: `backend/pyproject.toml`, `frontend/ai.client/package.json`, `infrastructure/package.json`.

20. **Decisions log** — `docs/kaizen/decisions.md` (if it exists). Items previously declined; don't re-propose without materially new context.

21. **Recent reviews** — `docs/kaizen/reviews/*.md` (last 1–2). Used to avoid duplicate proposals.

## Output

### 1. Primary doc — `docs/kaizen/research/YYYY-MM-DD.md`

```markdown
# Kaizen Research — [Day, Month D, YYYY]
> Scan window: [Month D – Month D, YYYY] (7 days)
> Web budget: N/50 used (target).

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
- **[Item]** — [1-2 sentence summary] — [URL] — *relevance*: [specific construct/file] — *unlocks* (if applicable): [net-new capability or enhancement this enables]

#### Strands Agents
- **[Item]** — …

#### Reference repo (aws-samples/sample-strands-agent-with-agentcore)
- **[Commit / change]** — [diff summary] — [URL] — *applicability*: [does our equivalent code do this differently? worth porting?]

#### MCP ecosystem
- …

#### FastMCP
- **[Release / change]** — [URL] — *implications for our MCP servers*: [breaking change? new primitive worth adopting?]

#### Agentic UI/UX patterns
- **[Pattern / release]** — [URL] — *what it is*: [1-2 sentences] — *fit for our stack*: [direct port / pattern-only (Angular equivalent: …) / not applicable] — *where it'd land*: [SSE event / component / route]

#### Frontier model announcements
- …

#### Agent harness patterns
- …

#### Pricing / quota
- …

#### Community + GitHub issues
- …

#### Cookbook / courses
- …

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

Used: N / 50 requests (target).
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

4. **Fan out external scan** — spawn parallel `general-purpose` subagents (or `Explore` for sources requiring multiple targeted lookups). One subagent per source category 1–12 above (15 categories total including 4a FastMCP, 4b Agentic UI/UX, 6a opencode, and 12 LibreChat). LibreChat and opencode each get a *light* subagent — releases-first, 1–2 web requests; do not fan either out further unless a headline maps onto something we're shipping. Each subagent receives:
   - The exact URLs to scan
   - Scope: last 7 days
   - Web budget for that subagent (3–5 requests soft target)
   - Required output: 3-5 bullet items max — title, 1-2 sentence summary, URL, "relevance to this repo" line.
   - **Required**: cite URLs; never fabricate. If empty, return "no notable items this week".

   Total budget across subagents targets ≤50. Track centrally; modest overage (~60) is acceptable when surfacing real signal — beyond that, stop and document the skip.

5. **Version-pin diff.** For each tracked dep, fetch latest release version (WebFetch on the release page or registry equivalent — counts toward budget). Compute lag in releases and days. If a budget hit prevents a check, list the dep under "Skipped".

6. **Synthesize.** Write the research doc per the shape above. Pull subagent reports verbatim into source sections; write the gestalt narrative (TL;DR, "What's moving", Take) yourself. **Top 5 weighting**:
   - **Library-native subtraction** opportunities (where upstream closed a custom-code need) get a subtraction boost.
   - **Capability-unlock** items — new platform primitives, SDK hooks, spec capabilities, or UX patterns that enable net-new product surface we couldn't easily build before — rank on their strategic merit, *not* deprioritized just because they don't intersect existing code. Apply the dual lens from Philosophy: if a feature genuinely unlocks new capability (code-interpreter, persistent agent state, multi-agent UI attribution, etc.), rank it like a fit item, not like a "monitor" item. Resist the temptation to hedge unlock items into "replaces future glue we haven't written" — that under-weights the real story.
   - **Concrete fit** UI/UX patterns that match an existing surface (tool-call rendering, attachments, A2A attribution, consent flows) get a fit boost over generic "interesting trend" items.

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
- External scan: AWS Bedrock/AgentCore, Strands Agents, FastMCP, reference repo, MCP, agentic UI/UX patterns, frontier models, agent-harness patterns, pricing.
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

- **No fabrication.** If a source is rate-limited or empty, list it as "not scanned" — don't invent content. The Sources Scanned table is auditable.
- **Web budget is a soft target, not a hard cap.** ≤50 requests is the goal. Overage is acceptable when justified by signal (document in the Web Budget block). Don't pad — if a source is empty after one fetch, move on.
- **Subtraction first.** Top 5 should include at least 2 retire/simplify candidates if the system has been running >2 weeks.
- **Concrete, not aspirational.** "Consider Strands hooks" is too vague. "Add a Strands `BeforeToolCall` hook in `backend/src/agents/main_agent/hooks/` to attribute tokens by tool" is actionable.
- **No edits to source code.** This skill only writes under `docs/kaizen/`.
- **Honest about dry weeks.** A quiet week produces a short doc, not a padded one.
- **Don't re-propose declined ideas** without materially new context. Check `docs/kaizen/decisions.md` and recent reviews.
- **Cite everything.** Every external claim has a URL + access date in the Sources Scanned appendix.
- **Don't auto-merge the PR.** Phil reviews and merges Friday morning. Review-prep runs against the unmerged PR's docs — it reads the file from the working tree, not from `develop`.

## Confirmation

After the PR is opened, tell Phil:
1. PR URL.
2. Top 1-2 ideas (title + Effort×Impact).
3. One-sentence Take.
4. Web budget used (N/50 target) and any skipped sources.

Brief. The full doc is on the PR.

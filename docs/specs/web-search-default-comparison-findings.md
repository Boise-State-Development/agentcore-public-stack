# AgentCore Web Search vs Brave Search: which web search should a fork default to?

**Date:** October 31, 2026
**Script:** `backend/scripts/compare_web_search.py`, using the 30 queries in `backend/scripts/web_search_comparison/queries.json`
**Run on:** the dev deployment (us-west-2), with both tools called through the deployment's AgentCore Gateway

**Recommendation: yes. AgentCore Web Search should be the web search a fork registers by default.** It is about twice as fast as Brave. Every result carries a published date, while Brave dates about 71% of them. The LLM judge rated the two tools equally on relevance. The decisive point for a fork is that it needs no infrastructure: there is no MCP server to deploy, no API key to manage, and queries never leave AWS.

The price is context. Each Web Search result is about 3.3× larger than a Brave result, so one search puts around 5.3k tokens into the conversation where Brave puts around 1.6k. Section 4 covers the cost and the lever that would close most of the gap.

---

## 1. What was compared

| | AgentCore Web Search | Brave Search |
|---|---|---|
| How it is reached | Built-in Gateway connector `web-search`, pinned at 1.3.0 | An MCP server deployed as a Lambda (server 1.30.0) behind the same Gateway, calling Brave's API |
| Tool the model sees | `WebSearch`, with 3 parameters | `brave_web_search`, with 14 parameters. The same server also registers video, image and news search |
| Search fee | $7 per 1,000 queries | $5 per 1,000 queries (Brave "Search" plan, plus $5 a month in free credit) |
| What a fork has to run | Nothing; an admin registers it in Admin → Tools | A separate Lambda MCP server and a Brave API key |

Every query was sent to both tools, alternating which tool went first, with 10 results requested from each. Both tools' native date filters were used on the six date-bounded queries: Web Search's `filters.publishedDateFilter` and Brave's `freshness`. Token counts come from Bedrock CountTokens against the Claude Haiku 4.5 tokenizer. They measure only the tool-result `content` the model receives, as the marginal cost over an empty result.

## 2. Results

| | Web Search | Brave |
|---|---|---|
| Calls succeeded | 30 / 30 | 30 / 30 |
| Latency p50 / p95 | **429 / 666 ms** | 897 / 1,760 ms |
| Results per query | 10 | 10 |
| Results with a published date | **100%** | 71% |
| Tokens per search result payload, mean / p95 | 5,349 / 6,907 | **1,627 / 2,050** |
| Tokens per individual result | ~535 | ~163 |
| Payload at k=5 (separate run) | 2,648 tokens, p50 361 ms | — |
| Tokens the tool definition adds to every turn's `toolConfig` | **412** | 964 for the web tool alone; 2,528 for all four tools as registered |
| LLM judge, advisory (Sonnet 4.6, mean 0–3) | 2.67 | 2.67 |
| Judge preferred | 15 | 15 |

Web Search's latency advantage held in every category, at a p50 of 345–473 ms against Brave's 850–944 ms. The Brave figures include our Lambda hop as well as Brave's API time. Because that hop is what a fork would deploy, it is the fair comparison.

The two tools return different pages. The mean URL overlap (Jaccard) is 0.15 and the mean domain overlap is 0.25, and 3 queries shared no URL at all. Even so, the judge found them equally useful. Its preferences by category (Web Search–Brave):

| Category | Web Search | Brave |
|---|---|---|
| Campus and education lookups | 4 | 2 |
| Date-bounded queries | 4 | 2 |
| Entity facts | 3 | 3 |
| Current events | 2 | 4 |
| Niche technical queries | 2 | 4 |

Most individual verdicts were 3–3 on score, with only a slight preference either way. The judge's stated reasons favour Web Search for exact figures and timestamps, and Brave for official API documentation and cleaner snippets.

## 3. Caveats

- This is 30 queries, run once, on one day. The judge is one model's opinion of titles, URLs, dates and the first 400 characters of each snippet. That truncation hides most of what makes Web Search's snippets larger, both their extra usefulness and their extra cost.
- Brave was measured as registered here, which exposes four tools. A fork could register only `brave_web_search`, which narrows the `toolConfig` gap to 552 tokens.
- Web Search has no equivalent of Brave's image, video or news tools. A deployment that relies on those still needs Brave or something like it.
- No raw results were kept. Both providers' terms restrict storing search results, so the script writes URLs, titles, dates and metrics only.

## 4. Cost, per the token-cost tenet

The figures below are priced on Haiku 4.5 Regional at $1.10/MTok input. A tool result is cache-written once at 1.25× that rate, then cache-read at 0.1× by every later model call in the session.

| Per 1,000 searches | Web Search, k=10 | Web Search, k=5 | Brave |
|---|---|---|---|
| Search fee | $7.00 | $7.00 | $5.00 |
| Result cache write | $7.35 | $3.64 | $2.24 |
| **First-call total** | **$14.35** | **$10.64** | **$7.24** |
| Each later model call re-reads it | +$0.59 | +$0.29 | +$0.18 |

On Sonnet 4.6 the token share of those totals triples: $29.06, $17.92 and $11.71 respectively.

The other side of the ledger is the tool definition, which sits in every turn's `toolConfig` at the head of the cacheable prefix. Compared with Brave as registered, Web Search removes 2,116 tokens from that prefix. On Haiku that saves about $0.23 per 1,000 model calls in cache reads, plus the cache write each time the prefix is refreshed.

Putting the two together: at k=10, Web Search costs less than Brave-as-registered for sessions that make fewer than about one search per ~30 model calls. At k=5 the break-even is about one search per ~15 model calls. A session that searches more than that costs more on Web Search, mostly because of the payload rather than the fee.

**The lever is `maxResults`.** The payload is about 535 tokens per result and scales linearly. The connector's schema defaults to 10 results, and its description is fixed by the connector, so we cannot change what the model is told about it. A `BeforeToolCall` hook that fills in `maxResults=5` when the model omits it would halve the payload. It adds nothing to the prompt and is deterministic, so it is cache-safe. Section 2's k=5 run shows the result: 2,648 tokens and a 361 ms p50. That is worth doing before Web Search becomes the documented default. Because it changes a tool argument, it should be its own change with its own readout.

## 5. Reproducing

From `backend/`, with AWS credentials for the deployment:

```bash
PROJECT_PREFIX=<prefix> uv run python scripts/compare_web_search.py --list-tools \
  --arm 'web_search=gateway:*___WebSearch' --arm 'brave=catalog:<brave tool id>#*brave_web_search'
PROJECT_PREFIX=<prefix> uv run python scripts/compare_web_search.py --out /tmp/web-search-cmp \
  --arm 'web_search=gateway:*___WebSearch' --arm 'brave=catalog:<brave tool id>#*brave_web_search' \
  --usd-per-1k web_search=7 --usd-per-1k brave=5 --judge-model us.anthropic.claude-sonnet-4-6
```

The run makes 30 searches on each tool, which is about $0.21 of Web Search plus Brave's fee. Do not commit `--out`.

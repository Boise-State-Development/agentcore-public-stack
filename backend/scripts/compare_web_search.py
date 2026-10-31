"""Side-by-side comparison of two (or more) web search tools, called over MCP.

Built to answer one question: is AgentCore Web Search (a built-in Gateway
connector, `<target>___WebSearch`) a better *default* web search for a fork of
this stack than the Brave Search MCP server? Quality, latency and per-turn token
cost all count, so for every query and every arm this records:

  - latency of the `tools/call` (warm session; the session's cold
    initialize + `tools/list` is recorded once per arm)
  - result count, and how many results carry a published date
  - URL and domain overlap between the first two arms
  - the size of the payload the model would see: bytes, and Bedrock
    CountTokens tokens (marginal over an empty tool result, so the
    conversation scaffolding is not counted)
  - the arm's tool spec in tokens — it sits in every turn's `toolConfig`,
    the head of the cacheable prefix

Only the result's `content` blocks are sized: Strands' Bedrock formatter sends
`content` to the model and drops `structuredContent`, so a tool that returns
both is charged for the text alone.

An arm is named on the command line as `NAME=SPEC`:

    gateway:<glob>         a tool on this deployment's AgentCore Gateway whose
                           MCP name matches <glob> (e.g. `*___WebSearch`)
    catalog:<tool_id>      a tool-catalog row: `mcp_external` connects to its
                           `mcp_config` via `create_external_mcp_client`
                           (custom headers and SigV4 included); a Gateway row
                           (`mcp`) goes through the Gateway by its target name
    url:<https://...>      an unauthenticated streamable-HTTP MCP server

Append `#<glob>` to a catalog or url spec to pick one tool on a multi-tool
server. Arguments are built from each tool's own input schema: the query goes
in its query field, `--k` in its result-count field (`maxResults` / `count` /
...), and a query's `since`/`until` in its native date filter
(`filters.publishedDateFilter` or Brave's `freshness`) when it has one.

What is written to `--out` (never commit it):

    results.jsonl   one row per (query, arm): metrics, URLs, titles, dates
    results.csv     the same, flattened
    summary.json    per-arm and pairwise aggregates
    summary.md      the readout

Result snippets are NOT written unless `--keep-snippets` is given: both
providers' terms restrict storing search results, and the metrics do not need
them. The optional LLM judge (`--judge-model`) reads snippets in memory only.
Its scores are advisory — one model's opinion of relevance, not ground truth.

Usage (from backend/):

    # Discover arm tool names and schemas; spends no searches
    AWS_PROFILE=dev-ai PROJECT_PREFIX=<prefix> uv run python \\
        scripts/compare_web_search.py --list-tools \\
        --arm 'web_search=gateway:*___WebSearch' --arm brave=catalog:brave_search

    # The comparison (~30 queries x 2 arms; Web Search bills $7 per 1,000)
    AWS_PROFILE=dev-ai PROJECT_PREFIX=<prefix> uv run python \\
        scripts/compare_web_search.py --out /tmp/web-search-cmp \\
        --arm 'web_search=gateway:*___WebSearch' --arm brave=catalog:brave_search \\
        --usd-per-1k web_search=7 --judge-model us.anthropic.claude-sonnet-4-6

A Gateway with JWT inbound auth needs a user access token: pass
`--gateway-token-env VAR` naming an environment variable that holds it. The
default is SigV4 with the caller's AWS credentials, which is what an
`AWS_IAM` Gateway expects.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import fnmatch
import json
import logging
import os
import random
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import boto3  # noqa: E402
from botocore.config import Config as BotocoreConfig  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from strands.tools.mcp import MCPClient  # noqa: E402

from agents.main_agent.core.bedrock_count_tokens import base_foundation_model_id  # noqa: E402
from agents.main_agent.integrations.external_mcp_client import create_external_mcp_client  # noqa: E402
from agents.main_agent.integrations.gateway_auth import get_sigv4_auth  # noqa: E402
from agents.main_agent.integrations.oauth_auth import create_oauth_bearer_auth  # noqa: E402
from apis.shared.tools.gateway_identity import gateway_url_from_id, resolve_gateway_id  # noqa: E402
from apis.shared.tools.models import ToolProtocol  # noqa: E402
from apis.shared.tools.repository import ToolCatalogRepository  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger("compare-web-search")
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-west-2")
DEFAULT_QUERIES = Path(__file__).parent / "web_search_comparison" / "queries.json"
DEFAULT_COUNT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Haiku 4.5 Regional (CRIS) base input rate, the stack's default model; see
# CLAUDE.md's prompt-cache note. A tool result is cache-written once (1.25x)
# and cache-read (0.1x) by every later model call in the session.
DEFAULT_INPUT_USD_PER_MTOK = 1.10
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1

QUERY_KEYS = ("query", "q", "search_query", "searchQuery")
COUNT_KEYS = ("maxResults", "max_results", "count", "num_results", "numResults", "limit", "top_k", "page_size", "per_page", "size")
URL_KEYS = ("url", "link", "href")
TITLE_KEYS = ("title", "name")
DATE_KEYS = ("publishedDate", "published_date", "datePublished", "published", "page_age", "age", "date")
SNIPPET_KEYS = ("text", "snippet", "description", "content", "extra_snippets")
LABELS = {
    "url": "url",
    "title": "title",
    "description": "snippet",
    "snippet": "snippet",
    "published": "published",
    "age": "published",
    "date": "published",
}
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|ref$|ref_src$)")


# --------------------------------------------------------------------- arms


@dataclass
class Arm:
    """One web search tool under test, and the session that reaches it."""

    name: str
    spec: str
    factory: Callable[[], MCPClient]
    tool_glob: str
    # The name the agent would register the tool under: Gateway tools get the
    # `gateway_` prefix, external tools keep the server's name.
    agent_name_prefix: str = ""
    client: Optional[MCPClient] = None
    tool_name: str = ""
    tool_spec: Dict[str, Any] = field(default_factory=dict)
    tool_spec_tokens: Optional[int] = None
    cold_start_ms: float = 0.0

    @property
    def schema(self) -> Dict[str, Any]:
        return self.tool_spec.get("inputSchema", {}).get("json", {})


def _gateway_factory(args: argparse.Namespace) -> Callable[[], MCPClient]:
    gateway_id = resolve_gateway_id(project_prefix=args.prefix, region=REGION)
    url = gateway_url_from_id(gateway_id, region=REGION)
    if args.gateway_token_env:
        token = os.environ.get(args.gateway_token_env)
        if not token:
            raise SystemExit(f"--gateway-token-env {args.gateway_token_env} is not set")
        auth = create_oauth_bearer_auth(token=token)
    else:
        auth = get_sigv4_auth(region=REGION)
    return lambda: MCPClient(lambda: streamablehttp_client(url, auth=auth))


def _split_tool_glob(rest: str) -> Tuple[str, Optional[str]]:
    if "#" in rest:
        head, glob = rest.split("#", 1)
        return head, glob
    return rest, None


async def resolve_arm(name: str, spec: str, args: argparse.Namespace) -> Arm:
    kind, _, rest = spec.partition(":")
    if kind == "gateway":
        return Arm(name, spec, _gateway_factory(args), rest or "*", agent_name_prefix="gateway_")

    if kind == "url":
        url, glob = _split_tool_glob(rest)
        return Arm(name, spec, lambda: MCPClient(lambda: streamablehttp_client(url)), glob or "*")

    if kind == "catalog":
        tool_id, glob = _split_tool_glob(rest)
        table = os.environ.get("DYNAMODB_APP_ROLES_TABLE_NAME") or f"{args.prefix}-app-roles"
        tool = await ToolCatalogRepository(table_name=table).get_tool(tool_id)
        if tool is None:
            raise SystemExit(f"arm {name}: no tool-catalog row '{tool_id}' in {table}")
        if tool.protocol == ToolProtocol.MCP_EXTERNAL and tool.mcp_config:
            config = tool.mcp_config

            def factory() -> MCPClient:
                client = create_external_mcp_client(config, tool)
                if client is None:
                    raise SystemExit(f"arm {name}: create_external_mcp_client refused '{tool_id}'")
                return client

            return Arm(name, spec, factory, glob or "*")
        if tool.protocol == ToolProtocol.MCP_GATEWAY and tool.mcp_gateway_config:
            target = tool.mcp_gateway_config.target_name
            return Arm(name, spec, _gateway_factory(args), glob or f"{target}___*", agent_name_prefix="gateway_")
        raise SystemExit(f"arm {name}: '{tool_id}' is protocol {tool.protocol}; only mcp_external and mcp rows are supported")

    raise SystemExit(f"arm {name}: unknown spec '{spec}' (use gateway:, catalog: or url:)")


def _pick_tool(arm: Arm, tools: Sequence[Any]) -> Any:
    matches = [t for t in tools if fnmatch.fnmatch(t.mcp_tool.name, arm.tool_glob)]
    if len(matches) > 1:
        searchy = [t for t in matches if "search" in t.mcp_tool.name.lower()]
        matches = searchy if len(searchy) == 1 else matches
    if len(matches) != 1:
        names = sorted(t.mcp_tool.name for t in tools)
        raise SystemExit(f"arm {arm.name}: '{arm.tool_glob}' matched {len(matches)} of {len(names)} tools; narrow it with '#<glob>'. Tools: {names}")
    return matches[0]


def open_arm(arm: Arm) -> None:
    started = time.perf_counter()
    arm.client = arm.factory()
    arm.client.start()
    tools: List[Any] = []
    token = None
    while True:
        page = arm.client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = page.pagination_token
        if token is None:
            break
    arm.cold_start_ms = (time.perf_counter() - started) * 1000
    picked = _pick_tool(arm, tools)
    arm.tool_name = picked.mcp_tool.name
    spec = picked.tool_spec
    arm.tool_spec = {
        "name": f"{arm.agent_name_prefix}{arm.tool_name}"[:64],
        "description": spec.get("description", ""),
        "inputSchema": spec.get("inputSchema", {"json": {"type": "object"}}),
    }


def close_arm(arm: Arm) -> None:
    if arm.client is not None:
        try:
            arm.client.stop(None, None, None)
        except Exception as exc:  # noqa: BLE001 - teardown must not hide the readout
            logger.warning("arm %s: close failed: %s", arm.name, exc)


# ---------------------------------------------------------------- arguments


def build_arguments(schema: Dict[str, Any], query: Dict[str, Any], k: int, date_filters: bool) -> Tuple[Dict[str, Any], bool]:
    """Map a query onto a tool's own input schema. Returns (arguments, date_filter_applied)."""
    props: Dict[str, Any] = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    query_key = next((key for key in QUERY_KEYS if key in props), None)
    if query_key is None:
        query_key = next(
            (key for key in required if (props.get(key) or {}).get("type") == "string"),
            "query",
        )
    arguments: Dict[str, Any] = {query_key: query["query"]}

    count_key = next((key for key in COUNT_KEYS if key in props), None)
    if count_key:
        maximum = (props[count_key] or {}).get("maximum")
        arguments[count_key] = min(k, maximum) if isinstance(maximum, int) else k

    applied = False
    since, until = query.get("since"), query.get("until")
    if date_filters and (since or until):
        filters_props = (props.get("filters") or {}).get("properties") or {}
        if "publishedDateFilter" in filters_props:
            window: Dict[str, str] = {}
            if since:
                window["from"] = f"{since}T00:00:00Z"
            if until:
                window["to"] = f"{until}T23:59:59Z"
            arguments["filters"] = {"publishedDateFilter": window}
            applied = True
        elif "freshness" in props and since and until:
            arguments["freshness"] = f"{since}to{until}"
            applied = True
    return arguments, applied


# ------------------------------------------------------------------ parsing


def _first(row: Dict[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def _find_rows(payload: Any, depth: int = 0) -> Optional[List[Dict[str, Any]]]:
    """Find the first list of dicts that look like search results."""
    if depth > 5:
        return None
    if isinstance(payload, list):
        dicts = [item for item in payload if isinstance(item, dict)]
        if dicts and any(_first(item, URL_KEYS) for item in dicts):
            return dicts
        for item in payload:
            rows = _find_rows(item, depth + 1)
            if rows:
                return rows
    if isinstance(payload, dict):
        for value in payload.values():
            rows = _find_rows(value, depth + 1)
            if rows:
                return rows
    return None


def extract_results(content: List[Dict[str, Any]], structured: Any) -> Tuple[List[Dict[str, Any]], str]:
    """Return (results, how) from what the tool returned.

    The model-visible `content` is tried first, so a result counted here is one
    the model actually sees; `structuredContent` and a URL scrape are fallbacks.
    """
    payloads: List[Any] = []
    texts: List[str] = []
    for block in content:
        if "json" in block:
            payloads.append(block["json"])
        elif "text" in block:
            texts.append(block["text"])
            try:
                payloads.append(json.loads(block["text"]))
            except (ValueError, TypeError):
                pass
    # One result per block (`{"url": ...}` each), as Brave's MCP server returns them.
    block_rows = [p for p in payloads if isinstance(p, dict) and _first(p, URL_KEYS)]
    if len(block_rows) > 1:
        return [_normalize_row(row) for row in block_rows], "content"
    for how, candidates in (("content", payloads), ("structured", [structured] if structured is not None else [])):
        for payload in candidates:
            rows = _find_rows(payload)
            if rows:
                return [_normalize_row(row) for row in rows], how

    labeled = [row for text in texts for row in _labeled_rows(text)]
    if labeled:
        return labeled, "labeled_text"

    results: List[Dict[str, Any]] = []
    seen = set()
    for text in texts:
        for url in URL_RE.findall(text):
            url = url.rstrip(".,;")
            if url not in seen:
                seen.add(url)
                results.append({"url": url, "title": None, "published": None, "snippet": None})
    return results, "url_scrape" if results else "none"


def _labeled_rows(text: str) -> List[Dict[str, Any]]:
    """Parse `Title: ...` / `URL: ...` / `Description: ...` blocks separated by blank lines."""
    rows = []
    for chunk in re.split(r"\n\s*\n", text):
        fields = {}
        for line in chunk.splitlines():
            label, sep, value = line.partition(":")
            if sep and label.strip().lower() in LABELS:
                fields[LABELS[label.strip().lower()]] = value.strip()
        if fields.get("url"):
            rows.append({key: fields.get(key) for key in ("url", "title", "published", "snippet")})
    return rows


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    snippet = _first(row, SNIPPET_KEYS)
    if isinstance(snippet, list):
        snippet = " ".join(str(part) for part in snippet)
    published = _first(row, DATE_KEYS)
    return {
        "url": _first(row, URL_KEYS),
        "title": _first(row, TITLE_KEYS),
        "published": str(published) if published is not None else None,
        "snippet": str(snippet) if snippet is not None else None,
    }


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def jaccard(a: set, b: set) -> Optional[float]:
    if not a and not b:
        return None
    return len(a & b) / len(a | b)


def payload_bytes(content: List[Dict[str, Any]]) -> int:
    total = 0
    for block in content:
        if "text" in block:
            total += len(block["text"].encode("utf-8"))
        elif "json" in block:
            total += len(json.dumps(block["json"]).encode("utf-8"))
    return total


def model_content(content: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The tool-result blocks Bedrock would receive (text and json only)."""
    blocks = [{"text": b["text"]} if "text" in b else {"json": b["json"]} for b in content if "text" in b or "json" in b]
    return blocks or [{"text": ""}]


# ------------------------------------------------------------------- tokens


class TokenCounter:
    """Marginal Bedrock CountTokens measurements; None where a count fails."""

    def __init__(self, model_id: str) -> None:
        self.model_id = base_foundation_model_id(model_id)
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=REGION,
            config=BotocoreConfig(retries={"max_attempts": 4, "mode": "adaptive"}, read_timeout=20),
        )
        self.failures = 0

    def _count(self, converse: Dict[str, Any]) -> Optional[int]:
        try:
            response = self.client.count_tokens(modelId=self.model_id, input={"converse": converse})
            return int(response["inputTokens"])
        except Exception as exc:  # noqa: BLE001 - a failed count is reported as missing, not fatal
            self.failures += 1
            if self.failures <= 3:
                logger.warning("CountTokens failed (%s): %s", self.model_id, exc)
            return None

    def tool_spec_tokens(self, tool_spec: Dict[str, Any]) -> Optional[int]:
        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        with_tool = self._count({"messages": messages, "toolConfig": {"tools": [{"toolSpec": tool_spec}]}})
        without = self._count({"messages": messages})
        if with_tool is None or without is None:
            return None
        return with_tool - without

    def result_tokens(self, tool_spec: Dict[str, Any], query: str, arguments: Dict[str, Any], content: List[Dict[str, Any]]) -> Optional[int]:
        def converse(result_content: List[Dict[str, Any]]) -> Dict[str, Any]:
            return {
                "messages": [
                    {"role": "user", "content": [{"text": query}]},
                    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": tool_spec["name"], "input": arguments}}]},
                    {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": result_content}}]},
                ],
                "toolConfig": {"tools": [{"toolSpec": tool_spec}]},
            }

        full = self._count(converse(model_content(content)))
        empty = self._count(converse([{"text": "."}]))
        if full is None or empty is None:
            return None
        return max(full - empty, 0)


# -------------------------------------------------------------------- judge

JUDGE_PROMPT = """You are grading web search results for an AI assistant at a university. Today is {today}.

Query: {query}

Two search tools returned the result lists below (order and labels are random). For each list, judge how well it would let an assistant answer the query accurately and with citations: relevance, authority of sources, and freshness where the query needs it.

{lists}

Reply with JSON only, no prose:
{{"A": {{"relevant": <number of relevant results>, "score": <0-3: 0 useless, 1 weak, 2 adequate, 3 strong>}}, "B": {{"relevant": <n>, "score": <0-3>}}, "preferred": "A" | "B" | "tie", "reason": "<25 words or fewer>"}}"""


def _judge_view(row: Dict[str, Any]) -> Any:
    """Parsed results, or the raw text when parsing found URLs only."""
    return row["_raw_text"] if row.get("parsed_from") == "url_scrape" else row["_results"]


def _format_list(results: Any, limit: int) -> str:
    if isinstance(results, str):
        return f"(raw tool output, truncated)\n{results}"
    lines = []
    for i, row in enumerate(results[:limit], 1):
        snippet = (row.get("snippet") or "").replace("\n", " ")[:400]
        lines.append(f"{i}. {row.get('title') or '(no title)'} | {row.get('url')} | {row.get('published') or 'no date'}\n   {snippet}")
    return "\n".join(lines) or "(no results)"


def judge_query(
    client: Any, model_id: str, query: str, results_by_arm: Dict[str, Any], rng: random.Random, k: int
) -> Dict[str, Any]:
    arms = list(results_by_arm)[:2]
    rng.shuffle(arms)
    labels = dict(zip("AB", arms))
    lists = "\n\n".join(f"List {label}:\n{_format_list(results_by_arm[arm], k)}" for label, arm in labels.items())
    prompt = JUDGE_PROMPT.format(today=datetime.now(timezone.utc).date().isoformat(), query=query, lists=lists)
    try:
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        verdict = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except Exception as exc:  # noqa: BLE001 - the judge is advisory; a failure is a gap, not a crash
        return {"error": str(exc)[:200]}
    preferred = verdict.get("preferred")
    return {
        "scores": {labels[label]: (verdict.get(label) or {}).get("score") for label in labels},
        "relevant": {labels[label]: (verdict.get(label) or {}).get("relevant") for label in labels},
        "preferred": labels.get(preferred, "tie") if preferred in labels else "tie",
        "reason": verdict.get("reason"),
    }


# --------------------------------------------------------------------- run


def run_query(arm: Arm, query: Dict[str, Any], args: argparse.Namespace, counter: Optional[TokenCounter]) -> Dict[str, Any]:
    arguments, date_applied = build_arguments(arm.schema, query, args.k, not args.no_date_filters)
    row: Dict[str, Any] = {
        "query_id": query["id"],
        "category": query.get("category"),
        "arm": arm.name,
        "tool": arm.tool_name,
        "arguments": {key: value for key, value in arguments.items() if key not in QUERY_KEYS},
        "date_filter_applied": date_applied,
    }
    started = time.perf_counter()
    try:
        result = arm.client.call_tool_sync(
            f"cmp-{uuid.uuid4().hex[:12]}",
            arm.tool_name,
            arguments,
            read_timeout_seconds=timedelta(seconds=args.timeout),
        )
    except Exception as exc:  # noqa: BLE001 - one failed call is a data point
        row.update(latency_ms=round((time.perf_counter() - started) * 1000, 1), status="exception", error=str(exc)[:300])
        return row
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    content = list(result.get("content") or [])
    row["status"] = result.get("status", "success")
    if row["status"] != "success":
        row["error"] = " ".join(b.get("text", "") for b in content)[:300]

    results, how = extract_results(content, result.get("structuredContent"))
    dated = [r for r in results if r.get("published")]
    row.update(
        parsed_from=how,
        result_count=len(results),
        dated_count=len(dated),
        payload_bytes=payload_bytes(content),
        payload_tokens=counter.result_tokens(arm.tool_spec, query["query"], arguments, content) if counter else None,
        urls=[r["url"] for r in results if r.get("url")],
        titles=[r.get("title") for r in results],
        published=[r.get("published") for r in results],
        _results=results,
        # What the judge reads when nothing better than a URL scrape parsed.
        _raw_text="\n".join(b.get("text", "") for b in content)[:4000],
    )
    return row


def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _mean(values: List[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(statistics.mean(clean), 2) if clean else None


def summarize(arms: List[Arm], rows: List[Dict[str, Any]], pairs: List[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    per_arm: Dict[str, Any] = {}
    for arm in arms:
        mine = [r for r in rows if r["arm"] == arm.name]
        ok = [r for r in mine if r.get("status") == "success"]
        latencies = [r["latency_ms"] for r in ok]
        results_total = sum(r["result_count"] for r in ok)
        tokens = [r["payload_tokens"] for r in ok if r.get("payload_tokens") is not None]
        usd_per_1k = args.usd_per_1k.get(arm.name)
        mean_tokens = statistics.mean(tokens) if tokens else None
        rate = args.input_usd_per_mtok / 1_000_000
        per_arm[arm.name] = {
            "spec": arm.spec,
            "tool": arm.tool_name,
            "calls": len(mine),
            "ok": len(ok),
            "errors": len(mine) - len(ok),
            "empty": sum(1 for r in ok if r["result_count"] == 0),
            "cold_start_ms": round(arm.cold_start_ms, 1),
            "latency_p50_ms": _pct(latencies, 0.5),
            "latency_p95_ms": _pct(latencies, 0.95),
            "latency_mean_ms": _mean(latencies),
            "results_mean": _mean([r["result_count"] for r in ok]),
            "dated_share": round(sum(r["dated_count"] for r in ok) / results_total, 3) if results_total else None,
            "payload_bytes_mean": _mean([r["payload_bytes"] for r in ok]),
            "payload_tokens_mean": round(mean_tokens, 1) if mean_tokens is not None else None,
            "payload_tokens_p95": _pct(tokens, 0.95),
            "tool_spec_tokens": arm.tool_spec_tokens,
            "search_usd_per_1k": usd_per_1k,
            # One result, priced as it enters a session: cache-written once,
            # then cache-read by every later model call in that session.
            "result_write_usd_per_1k": round(mean_tokens * rate * CACHE_WRITE_MULTIPLIER * 1000, 3) if mean_tokens else None,
            "result_read_usd_per_1k_per_later_call": round(mean_tokens * rate * CACHE_READ_MULTIPLIER * 1000, 4) if mean_tokens else None,
            "date_filter_applied": sum(1 for r in ok if r.get("date_filter_applied")),
            "by_category": {
                cat: {
                    "latency_p50_ms": _pct([r["latency_ms"] for r in ok if r["category"] == cat], 0.5),
                    "results_mean": _mean([r["result_count"] for r in ok if r["category"] == cat]),
                    "payload_tokens_mean": _mean([r.get("payload_tokens") for r in ok if r["category"] == cat]),
                }
                for cat in sorted({r["category"] for r in ok})
            },
        }

    pairwise: Dict[str, Any] = {}
    if pairs:
        url_j = [p["url_jaccard"] for p in pairs]
        dom_j = [p["domain_jaccard"] for p in pairs]
        pairwise = {
            "arms": pairs[0]["arms"],
            "queries": len(pairs),
            "url_jaccard_mean": _mean(url_j),
            "domain_jaccard_mean": _mean(dom_j),
            "zero_url_overlap": sum(1 for p in pairs if p["url_shared"] == 0),
        }
        judged = [p["judge"] for p in pairs if p.get("judge") and "scores" in p["judge"]]
        if judged:
            a, b = pairs[0]["arms"]
            pairwise["judge"] = {
                "model": args.judge_model,
                "advisory": True,
                "queries": len(judged),
                f"{a}_score_mean": _mean([j["scores"].get(a) for j in judged]),
                f"{b}_score_mean": _mean([j["scores"].get(b) for j in judged]),
                f"{a}_preferred": sum(1 for j in judged if j["preferred"] == a),
                f"{b}_preferred": sum(1 for j in judged if j["preferred"] == b),
                "ties": sum(1 for j in judged if j["preferred"] == "tie"),
            }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "region": REGION,
        "k": args.k,
        "count_model": args.count_model,
        "input_usd_per_mtok": args.input_usd_per_mtok,
        "arms": per_arm,
        "pairwise": pairwise,
    }


def render_markdown(summary: Dict[str, Any]) -> str:
    arms = summary["arms"]
    names = list(arms)
    metrics = [
        ("Tool", "tool"),
        ("Calls ok / errors / empty", None),
        ("Cold start (init + tools/list), ms", "cold_start_ms"),
        ("Latency p50 / p95, ms", None),
        ("Results per query (mean)", "results_mean"),
        ("Results with a published date", "dated_share"),
        ("Payload bytes (mean)", "payload_bytes_mean"),
        ("Payload tokens mean / p95", None),
        ("Tool spec tokens (every turn's toolConfig)", "tool_spec_tokens"),
        ("Search fee, $ per 1k queries", "search_usd_per_1k"),
        ("Result cache-write, $ per 1k results", "result_write_usd_per_1k"),
        ("Result cache-read per later call, $ per 1k", "result_read_usd_per_1k_per_later_call"),
        ("Date filter applied (queries)", "date_filter_applied"),
    ]
    lines = [
        f"# Web search comparison — {summary['generated_at']}",
        "",
        f"Region {summary['region']}, k={summary['k']}, tokens counted on `{summary['count_model']}` at ${summary['input_usd_per_mtok']}/MTok input.",
        "",
        "| Metric | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for label, key in metrics:
        cells = []
        for name in names:
            a = arms[name]
            if label.startswith("Calls"):
                cells.append(f"{a['ok']} / {a['errors']} / {a['empty']}")
            elif label.startswith("Latency"):
                cells.append(f"{a['latency_p50_ms']} / {a['latency_p95_ms']}")
            elif label.startswith("Payload tokens"):
                cells.append(f"{a['payload_tokens_mean']} / {a['payload_tokens_p95']}")
            else:
                value = a.get(key)
                cells.append("—" if value is None else f"`{value}`" if key == "tool" else str(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    categories = sorted({cat for a in arms.values() for cat in a["by_category"]})
    if categories:
        lines += ["", "## By category (p50 ms · results · tokens)", "", "| Category | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for cat in categories:
            cells = []
            for name in names:
                c = arms[name]["by_category"].get(cat, {})
                cells.append(f"{c.get('latency_p50_ms')} · {c.get('results_mean')} · {c.get('payload_tokens_mean')}")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    pair = summary.get("pairwise") or {}
    if pair:
        a, b = pair["arms"]
        lines += [
            "",
            f"## Overlap ({a} vs {b})",
            "",
            f"- Mean URL Jaccard: {pair['url_jaccard_mean']}; mean domain Jaccard: {pair['domain_jaccard_mean']}",
            f"- Queries with no shared URL: {pair['zero_url_overlap']} of {pair['queries']}",
        ]
        judge = pair.get("judge")
        if judge:
            lines += [
                "",
                f"## LLM judge — advisory ({judge['model']}, {judge['queries']} queries)",
                "",
                "One model's opinion of relevance from titles, URLs, dates and snippets; not ground truth.",
                "",
                f"- Mean score (0–3): {a} {judge[f'{a}_score_mean']}, {b} {judge[f'{b}_score_mean']}",
                f"- Preferred: {a} {judge[f'{a}_preferred']}, {b} {judge[f'{b}_preferred']}, tie {judge['ties']}",
            ]
    return "\n".join(lines) + "\n"


def write_outputs(out: Path, rows: List[Dict[str, Any]], summary: Dict[str, Any], keep_snippets: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl").open("w") as fh:
        for row in rows:
            record = {k: v for k, v in row.items() if not k.startswith("_")}
            if keep_snippets:
                record["snippets"] = [r.get("snippet") for r in row.get("_results", [])]
            fh.write(json.dumps(record, default=str) + "\n")
    columns = [
        "query_id",
        "category",
        "arm",
        "tool",
        "status",
        "latency_ms",
        "result_count",
        "dated_count",
        "payload_bytes",
        "payload_tokens",
        "date_filter_applied",
        "parsed_from",
        "url_count",
        "error",
    ]
    with (out / "results.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "url_count": len(row.get("urls") or [])})
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "summary.md").write_text(render_markdown(summary))


def load_queries(path: Path, limit: Optional[int], categories: Optional[List[str]]) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text())
    queries = data["queries"] if isinstance(data, dict) else data
    if categories:
        queries = [q for q in queries if q.get("category") in categories]
    for q in queries:
        if len(q["query"]) > 200:
            raise SystemExit(f"query {q['id']} is over 200 characters (Web Search's limit)")
    return queries[:limit] if limit else queries


def parse_arm_values(values: List[str]) -> Dict[str, float]:
    parsed = {}
    for value in values:
        name, _, number = value.partition("=")
        parsed[name] = float(number)
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", action="append", required=True, help="NAME=SPEC (repeat; the first two are compared pairwise)")
    parser.add_argument("--out", type=Path, help="output directory (required unless --list-tools)")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--limit", type=int, help="first N queries only")
    parser.add_argument("--category", action="append", help="only these categories (repeat)")
    parser.add_argument("--k", type=int, default=10, help="results requested per query (default 10)")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-call read timeout, seconds")
    parser.add_argument("--prefix", default=os.environ.get("PROJECT_PREFIX"), help="deployment prefix (default $PROJECT_PREFIX)")
    parser.add_argument("--gateway-token-env", help="env var holding a user access token, for a JWT-authorized Gateway")
    parser.add_argument("--no-date-filters", action="store_true", help="never pass since/until to a native date filter")
    parser.add_argument("--count-model", default=DEFAULT_COUNT_MODEL, help="model whose tokenizer CountTokens uses")
    parser.add_argument("--no-count-tokens", action="store_true", help="skip CountTokens (bytes only)")
    parser.add_argument("--input-usd-per-mtok", type=float, default=DEFAULT_INPUT_USD_PER_MTOK)
    parser.add_argument("--usd-per-1k", action="append", default=[], help="NAME=USD search fee per 1,000 queries (repeat)")
    parser.add_argument("--judge-model", help="Bedrock model id for the advisory LLM relevance judge")
    parser.add_argument("--seed", type=int, default=7, help="seed for the judge's A/B shuffle")
    parser.add_argument("--keep-snippets", action="store_true", help="also write result snippets (see the terms note above)")
    parser.add_argument("--list-tools", action="store_true", help="print each arm's tool and schema, then exit")
    args = parser.parse_args()
    args.usd_per_1k = parse_arm_values(args.usd_per_1k)

    if not args.prefix:
        parser.error("--prefix (or PROJECT_PREFIX) is required to find the Gateway and tool catalog")
    if not args.list_tools and not args.out:
        parser.error("--out is required")

    arm_specs = []
    for value in args.arm:
        name, _, spec = value.partition("=")
        if not name or not spec:
            parser.error(f"--arm '{value}' is not NAME=SPEC")
        arm_specs.append((name, spec))

    arms = [asyncio.run(resolve_arm(name, spec, args)) for name, spec in arm_specs]
    try:
        for arm in arms:
            open_arm(arm)
            logger.info("arm %s: %s (cold start %.0f ms)", arm.name, arm.tool_name, arm.cold_start_ms)
        if args.list_tools:
            for arm in arms:
                print(json.dumps({"arm": arm.name, "toolSpec": arm.tool_spec}, indent=2))
            return

        queries = load_queries(args.queries, args.limit, args.category)
        counter = None if args.no_count_tokens else TokenCounter(args.count_model)
        if counter:
            for arm in arms:
                arm.tool_spec_tokens = counter.tool_spec_tokens(arm.tool_spec)

        judge_client = boto3.client("bedrock-runtime", region_name=REGION) if args.judge_model else None
        rng = random.Random(args.seed)
        rows: List[Dict[str, Any]] = []
        pairs: List[Dict[str, Any]] = []
        for i, query in enumerate(queries):
            # Rotate who goes first so neither arm always pays a warm-up.
            order = arms[i % len(arms) :] + arms[: i % len(arms)]
            by_arm = {}
            for arm in order:
                row = run_query(arm, query, args, counter)
                rows.append(row)
                by_arm[arm.name] = row
            line = " · ".join(
                f"{a.name} {by_arm[a.name].get('status')} {by_arm[a.name].get('latency_ms')}ms "
                f"{by_arm[a.name].get('result_count', 0)}r {by_arm[a.name].get('payload_tokens')}t"
                for a in arms
            )
            logger.info("[%d/%d] %s: %s", i + 1, len(queries), query["id"], line)

            if len(arms) >= 2:
                first, second = by_arm[arms[0].name], by_arm[arms[1].name]
                urls_a = {normalize_url(u) for u in first.get("urls") or []}
                urls_b = {normalize_url(u) for u in second.get("urls") or []}
                pair = {
                    "query_id": query["id"],
                    "arms": [arms[0].name, arms[1].name],
                    "url_shared": len(urls_a & urls_b),
                    "url_jaccard": jaccard(urls_a, urls_b),
                    "domain_jaccard": jaccard({domain_of(u) for u in urls_a}, {domain_of(u) for u in urls_b}),
                }
                if judge_client and first.get("status") == "success" and second.get("status") == "success":
                    pair["judge"] = judge_query(
                        judge_client,
                        args.judge_model,
                        query["query"],
                        {arms[0].name: _judge_view(first), arms[1].name: _judge_view(second)},
                        rng,
                        args.k,
                    )
                first["pair"] = second["pair"] = {k: v for k, v in pair.items() if k != "arms"}
                pairs.append(pair)

        summary = summarize(arms, rows, pairs, args)
        write_outputs(args.out, rows, summary, args.keep_snippets)
        print(render_markdown(summary))
        logger.info("wrote %s", args.out)
    finally:
        for arm in arms:
            close_arm(arm)


if __name__ == "__main__":
    main()

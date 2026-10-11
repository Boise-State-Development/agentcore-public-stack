"""Build the Conversation Cost Patterns dashboard datasets from raw DynamoDB scans.

Pure and local: reads the JSON the skill's `aws dynamodb scan` / `query` steps
saved, classifies every conversation in the window into a usage pattern (what
the user was doing) plus modifiers, and writes one content-free JSON file per
dashboard dataset. It never touches AWS, so it runs without the prod guard
having anything to inspect.

Content-free by construction: the inputs are projected without message text,
titles, file names or citations, and the outputs carry no user ids. Session
ids are kept so an admin can open the console drill-down for a row.

Run with `python3 -I` (no site-packages, no script-dir imports); stdlib only.

    python3 -I build_datasets.py \
        --cost-rows RAW/cost_rows.json --uploads RAW/uploads.json \
        [--meta-dir RAW/meta] [--history RAW/runs_previous.json] \
        --window-start 2026-09-09 --window-end 2026-10-09 \
        --console-base https://example.edu --out OUT

The taxonomy below is the single source of truth for the rules; when the
discovery queue turns up a new pattern, add it to PATTERNS (order matters:
first match wins), bump RULES_VERSION, and describe it in
references/taxonomy.md.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

RULES_VERSION = "2026-10-09.1"

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

Feat = Dict[str, Any]

PATTERNS: List[Tuple[str, str, str, str, Callable[[Feat], bool]]] = [
    # (id, label, definition, engineering lever, rule)
    ("API_PIPELINE", "API pipeline",
     "Programmatic traffic through an API key (api-converse sessions).",
     "Make it visible: cost is stored as a bare number and these sessions are missing from the console list.",
     lambda f: f["api"]),
    ("BROWSER_CRAWL", "Browser crawl",
     "25+ browse/fetch calls, or 10+ at 6+ model calls per turn.",
     "A browsing sub-agent with its own short context, or a per-turn step budget.",
     lambda f: f["browse"] >= 25 or (f["browse"] >= 10 and f["calls_per_turn"] >= 6)),
    ("BATCH_ITEMS", "Batch items",
     "15+ uploads, at least one for every three turns: a queue of independent items in one thread.",
     "A batch mode that gives each item a fresh context; today every item rides along for every later one.",
     lambda f: f["uploads"] >= 15 and f["uploads"] >= 0.3 * f["turns"]),
    ("SPREADSHEET_LOOP", "Spreadsheet loop",
     "15+ spreadsheet tool calls (analyze, read, modify).",
     "Bound and de-duplicate Code Interpreter results; fix the error rate that drives retries.",
     lambda f: f["sheet"] >= 15),
    ("WORKSPACE_ASSEMBLY", "Workspace assembly",
     "10+ Drive, Canvas or Google Workspace calls: building something from the user's own files.",
     "Fix connector search errors; offload large file reads instead of inlining them.",
     lambda f: f["workspace"] >= 10),
    ("ITERATIVE_DRAFTING", "Iterative drafting",
     "3+ artifact updates or 6+ artifact writes: one document revised over many turns.",
     "Patch-style artifact edits, and drop superseded versions from history.",
     lambda f: f["art_updates"] >= 3 or f["artifact"] >= 6),
    ("DOCUMENT_STUDY", "Document study",
     "At least 1 MB uploaded: reading and discussing attached documents.",
     "Document offload and digests, so the document is not re-written on every idle return.",
     lambda f: f["uploads"] >= 1 and f["up_mb"] >= 1.0),
    ("SCREENSHOT_SUPPORT", "Screenshot support",
     "4+ image uploads: troubleshooting from screenshots.",
     "Drop images from the prefix after the turn that introduced them.",
     lambda f: f["up_images"] >= 4),
    ("WEB_RESEARCH", "Web research",
     "10+ search or browse calls.",
     "Bound search payloads; cite and summarise instead of carrying raw results.",
     lambda f: f["search"] + f["browse"] >= 10),
    ("OFFICE_GENERATION", "Office generation",
     "3+ Word/PowerPoint generation calls.",
     "None yet.",
     lambda f: f["office"] >= 3),
    ("LONGFORM_CHAT", "Long-form chat",
     "10+ turns with almost no tools (at most 2, or one per ten turns).",
     "Shrink the context before the user returns, so the idle re-write is small.",
     lambda f: f["tool_total"] <= max(2, 0.1 * f["turns"]) and f["turns"] >= 10),
    ("PASTED_CORPUS", "Pasted corpus",
     "A 40K+ token jump on a turn with no attachment and no tool result: a large paste into the composer.",
     "Turn large pastes into attachments so they take the document-offload path.",
     lambda f: f["paste_jump_k"] >= 40 and f["uploads"] == 0),
    ("QUICK_QA", "Quick Q&A",
     "Under 10 turns and under $0.50.",
     "None: cheap by nature.",
     lambda f: f["turns"] < 10 and f["cost"] < 0.5),
]

UNCLASSIFIED = ("UNCLASSIFIED", "Unclassified",
                "Matched no rule. Conversations here over $1 are the discovery queue for new patterns.",
                "Read the discovery queue; promote a recurring signature to a pattern.")

MODIFIERS: List[Tuple[str, str, str, Callable[[Feat], bool]]] = [
    ("OVER_100K", "Over 100K context", "Context peaked at 100K tokens or more.",
     lambda f: f["peak_k"] >= 100),
    ("SLOW_CADENCE", "Slow cadence", "At least 8 turns, and at least half of them came after a 5+ minute pause.",
     lambda f: f["turns"] >= 8 and f["slow_cadence"] >= 0.5),
    ("BIG_PASTE", "Large paste", "A 40K+ token paste in a conversation with no uploads.",
     lambda f: f["paste_jump_k"] >= 40 and f["uploads"] == 0 and not f["api"]),
    ("TOOL_LOOP", "Tool loop", "5+ model calls per turn on average.",
     lambda f: f["calls_per_turn"] >= 5),
    ("PREMIUM_MODEL", "Premium model", "Half or more of calls on Opus, Fable or GPT-6.",
     lambda f: f["premium_share"] >= 0.5),
    ("MULTI_WEEK", "Multi-week", "Spans 14+ days or 5+ active days.",
     lambda f: f["span_days"] >= 14 or f["active_days"] >= 5),
]

MECHANISMS = [
    ("first_write", "First write", "The cache write on a conversation's first call."),
    ("idle_rewrite", "Idle re-write", "Cache writes on calls after a 5+ minute pause: the cached context expired and was written again."),
    ("live_rewrite", "Live re-write", "Partial misses inside the cache window: the history was re-written while warm."),
    ("growth_write", "Growth write", "Cache writes of genuinely new content inside the cache window."),
    ("read", "Cache read", "Reading the cached context on every call."),
    ("output", "Output", "Tokens the model generated."),
    ("input", "Uncached input", "Input tokens outside any cache point."),
    ("unattributed", "Unattributed", "Cost recorded without a breakdown (API pipeline rows)."),
]

DRIVE = {"search_files", "read_file_content", "list_recent_files", "create_file",
         "get_file_metadata", "update_file", "download_file_content"}
CANVAS_PREFIX = ("list_courses", "get_course", "list_assignments", "get_assignment", "get_syllabus",
                 "list_modules", "list_pages", "get_page_content", "list_discussion",
                 "get_submission", "list_submissions", "list_students")
GOOGLE_PREFIX = ("gmail", "calendar", "list_events", "search_threads", "get_thread", "list_tasks", "get_message")
SHEET = {"analyze_spreadsheet", "list_spreadsheets", "read_excel_spreadsheet",
         "modify_excel_spreadsheet", "create_excel_spreadsheet"}
OFFICE = {"create_word_document", "create_powerpoint_presentation", "list_word_documents", "modify_word_document"}
PREMIUM_MARKERS = ("opus", "fable", "gpt-6")

IDLE_SECONDS = 300
TURN_GAP_SECONDS = 60
TOP_CONVERSATIONS = 30
DISCOVERY_MIN_USD = 1.0
DISCOVERY_MAX = 40
DAILY_TOP_PATTERNS = 7

# ---------------------------------------------------------------------------
# DynamoDB JSON helpers
# ---------------------------------------------------------------------------


def unmarshal(value: Dict[str, Any]) -> Any:
    (kind, raw), = value.items()
    if kind == "S":
        return raw
    if kind == "N":
        return float(raw)
    if kind in ("BOOL", "NULL"):
        return None if kind == "NULL" else raw
    if kind == "M":
        return {k: unmarshal(v) for k, v in raw.items()}
    if kind == "L":
        return [unmarshal(v) for v in raw]
    return raw


def load_items(path: str) -> List[Dict[str, Any]]:
    with open(path) as fh:
        doc = json.load(fh)
    return [{k: unmarshal(v) for k, v in item.items()} for item in doc.get("Items", [])]


def dig(d: Any, *keys: str, default: Any = 0) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def cost_of(row: Dict[str, Any], part: str = "total") -> float:
    c = row.get("cost")
    if isinstance(c, dict):
        return float(c.get(part) or 0)
    if part == "total" and isinstance(c, (int, float)):
        return float(c)
    return 0.0


def cost_known(row: Dict[str, Any]) -> bool:
    c = row.get("cost")
    return isinstance(c, (int, float)) or (isinstance(c, dict) and c.get("total") is not None)


def context_tokens(row: Dict[str, Any]) -> float:
    return (dig(row, "tokenUsage", "inputTokens") + dig(row, "tokenUsage", "cacheReadInputTokens")
            + dig(row, "tokenUsage", "cacheWriteInputTokens"))


def parse_ts(row: Dict[str, Any]) -> datetime:
    return datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))


def has_attachment(row: Dict[str, Any]) -> bool:
    return bool(dig(row, "documentsAttached")) or bool(dig(row, "hasDocuments", default=False))


def short_model(model_id: str) -> str:
    for prefix in ("global.", "us.", "eu.", "apac."):
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
    return model_id.replace("anthropic.", "")


def upload_kind(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if "pdf" in mime:
        return "pdf"
    if "sheet" in mime or "csv" in mime or "excel" in mime:
        return "spreadsheet"
    if "word" in mime or "presentation" in mime or "officedocument" in mime:
        return "office document"
    return "text"


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def first_call_baselines(by_session: Dict[str, List[Dict[str, Any]]]) -> Dict[str, float]:
    """p10 first-call context per toolConfigHash: what an empty conversation costs with that tool set."""
    firsts: Dict[str, List[float]] = collections.defaultdict(list)
    for calls in by_session.values():
        first = calls[0]
        if has_attachment(first):
            continue
        h = dig(first, "prefixFingerprints", "toolConfigHash", default="")
        if h:
            firsts[h].append(context_tokens(first))
    return {h: sorted(xs)[len(xs) // 10] for h, xs in firsts.items() if len(xs) >= 3}


def features(sid: str, calls: List[Dict[str, Any]], uploads: List[Tuple[str, float]],
             baselines: Dict[str, float]) -> Feat:
    f: Feat = {"sid": sid, "api": sid.startswith("api-converse"), "calls": len(calls)}
    f["cost"] = sum(cost_of(r) for r in calls)
    gaps: List[Optional[float]] = [None] + [
        (parse_ts(b) - parse_ts(a)).total_seconds() for a, b in zip(calls, calls[1:])]
    f["turns"] = sum(1 for g in gaps if g is None or g > TURN_GAP_SECONDS)
    tools: collections.Counter = collections.Counter()
    for r in calls:
        for name, x in (r.get("toolCalls") or {}).items():
            tools[name] += int(dig(x, "calls"))
    f["tools"] = tools

    def count(pred: Callable[[str], bool]) -> int:
        return sum(c for k, c in tools.items() if pred(k))

    f["browse"] = tools["browse_web"] + tools["fetch_url_content"]
    f["search"] = count(lambda k: "brave" in k or "search_boise" in k or "semantic" in k or "arxiv" in k)
    f["artifact"] = tools["create_artifact"] + tools["update_artifact"]
    f["art_updates"] = tools["update_artifact"]
    f["sheet"] = count(lambda k: k in SHEET)
    f["office"] = count(lambda k: k in OFFICE)
    f["workspace"] = count(lambda k: k in DRIVE or k.startswith(CANVAS_PREFIX) or k.startswith(GOOGLE_PREFIX))
    f["tool_total"] = sum(tools.values())
    f["uploads"] = len(uploads)
    f["up_images"] = sum(1 for m, _ in uploads if m.startswith("image/"))
    f["up_mb"] = sum(b for _, b in uploads) / 1e6
    kinds = {upload_kind(m) for m, _ in uploads}
    f["attachment_kind"] = "none" if not kinds else (kinds.pop() if len(kinds) == 1 else "mixed")
    models = collections.Counter(short_model(dig(r, "modelInfo", "modelId", default="unknown")) for r in calls)
    f["models"] = models
    f["premium_share"] = sum(c for m, c in models.items() if any(p in m for p in PREMIUM_MARKERS)) / len(calls)
    ctx = [context_tokens(r) for r in calls]
    f["peak_k"] = max(ctx) / 1000
    f["span_days"] = (parse_ts(calls[-1]) - parse_ts(calls[0])).total_seconds() / 86400
    f["active_days"] = len({r["timestamp"][:10] for r in calls})
    f["calls_per_turn"] = len(calls) / max(1, f["turns"])
    f["slow_cadence"] = sum(1 for g in gaps[1:] if g and g > IDLE_SECONDS) / max(1, f["turns"] - 1)

    # Largest paste: growth on a user turn (previous call issued no tools, this one carries no
    # attachment), net of the previous reply's own output; for the first call, growth over the
    # tool set's empty-conversation baseline.
    jump = 0.0
    for a, b, ca, cb in zip(calls, calls[1:], ctx, ctx[1:]):
        if not a.get("toolCalls") and not has_attachment(b):
            jump = max(jump, cb - ca - dig(a, "tokenUsage", "outputTokens"))
    if not has_attachment(calls[0]):
        base = baselines.get(dig(calls[0], "prefixFingerprints", "toolConfigHash", default=""))
        if base is not None:
            jump = max(jump, ctx[0] - base)
    f["paste_jump_k"] = jump / 1000

    mech = dict.fromkeys((m for m, _, _ in MECHANISMS), 0.0)
    for i, (r, g) in enumerate(zip(calls, gaps)):
        write = cost_of(r, "cacheWriteCost")
        if i == 0:
            mech["first_write"] += write
        elif g is not None and g > IDLE_SECONDS:
            mech["idle_rewrite"] += write
        elif r.get("cacheStatus") == "partial_miss":
            mech["live_rewrite"] += write
        else:
            mech["growth_write"] += write
        mech["read"] += cost_of(r, "cacheReadCost")
        mech["output"] += cost_of(r, "outputCost")
        mech["input"] += cost_of(r, "inputCost")
    mech["unattributed"] = max(0.0, f["cost"] - sum(mech.values()))
    f["mech"] = mech
    f["first_at"] = calls[0]["timestamp"]
    f["last_at"] = calls[-1]["timestamp"]
    return f


def classify(f: Feat) -> Tuple[str, List[str]]:
    primary = next((pid for pid, _, _, _, rule in PATTERNS if rule(f)), UNCLASSIFIED[0])
    return primary, [mid for mid, _, _, rule in MODIFIERS if rule(f)]


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def r2(x: float) -> float:
    return round(x, 2)


def r4(x: float) -> float:
    return round(x, 4)


def pattern_labels() -> Dict[str, str]:
    out = {pid: label for pid, label, _, _, _ in PATTERNS}
    out[UNCLASSIFIED[0]] = UNCLASSIFIED[1]
    return out


def ds_patterns(feats: List[Feat], fleet: float) -> List[Dict[str, Any]]:
    rows = []
    for order, (pid, label, definition, lever, _) in enumerate(PATTERNS + [UNCLASSIFIED + (None,)]):
        members = [f for f in feats if f["pattern"] == pid]
        cost = sum(f["cost"] for f in members)
        row = {"pattern": label, "pattern_id": pid, "rule_order": order + 1,
               "sessions": len(members), "cost_usd": r2(cost),
               "share": r4(cost / fleet) if fleet else 0,
               "cost_per_session": r2(cost / len(members)) if members else 0,
               "over_1_usd": sum(1 for f in members if f["cost"] >= DISCOVERY_MIN_USD),
               "definition": definition, "lever": lever}
        for mid, _, _ in MECHANISMS:
            row[mid + "_usd"] = r2(sum(f["mech"][mid] for f in members))
        rows.append(row)
    rows.sort(key=lambda r: -r["cost_usd"])
    return rows


def ds_modifiers(feats: List[Feat], fleet: float) -> List[Dict[str, Any]]:
    rows = []
    for mid, label, definition, _ in MODIFIERS:
        members = [f for f in feats if mid in f["mods"]]
        cost = sum(f["cost"] for f in members)
        rows.append({"modifier": label, "modifier_id": mid, "sessions": len(members), "cost_usd": r2(cost),
                     "share": r4(cost / fleet) if fleet else 0, "definition": definition})
    rows.sort(key=lambda r: -r["cost_usd"])
    return rows


def ds_daily(rows: List[Dict[str, Any]], pattern_of: Dict[str, str], patterns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep = {p["pattern"] for p in patterns[:DAILY_TOP_PATTERNS]}
    labels = pattern_labels()
    acc: Dict[Tuple[str, str], float] = collections.defaultdict(float)
    days = set()
    for r in rows:
        day = r["timestamp"][:10]
        days.add(day)
        label = labels[pattern_of[r["sessionId"]]]
        acc[(day, label if label in keep else "Other")] += cost_of(r)
    order = [p["pattern"] for p in patterns[:DAILY_TOP_PATTERNS]] + ["Other"]
    return [{"day": d, "pattern": p, "cost_usd": r2(acc.get((d, p), 0.0))}
            for d in sorted(days) for p in order]


def conversation_row(f: Feat, rank: Optional[int], queue: str, meta: Dict[str, Dict[str, Any]],
                     console_base: str) -> Dict[str, Any]:
    c = f["cost"] or 1e-9
    m = meta.get(f["sid"], {})
    top_tools = ", ".join(f"{k} {v}" for k, v in f["tools"].most_common(3))
    return {
        "session": f["sid"], "short_id": "api:" + f["sid"][13:21] if f["api"] else f["sid"][:8],
        "queue": queue, "rank": rank,
        "pattern": pattern_labels()[f["pattern"]], "modifiers": " · ".join(
            label for mid, label, _, _ in MODIFIERS if mid in f["mods"]),
        "cost_usd": r2(f["cost"]), "lifetime_cost_usd": r2(m["totalCost"]) if m.get("totalCost") is not None else None,
        "calls": f["calls"], "turns": f["turns"], "calls_per_turn": round(f["calls_per_turn"], 1),
        "peak_context_k": round(f["peak_k"]), "active_days": f["active_days"],
        "uploads": f["uploads"], "upload_mb": round(f["up_mb"], 1), "largest_paste_k": round(f["paste_jump_k"]),
        "idle_rewrite_share": r4(f["mech"]["idle_rewrite"] / c), "output_share": r4(f["mech"]["output"] / c),
        "read_share": r4(f["mech"]["read"] / c), "tool_calls": f["tool_total"], "top_tools": top_tools,
        "model": ", ".join(m_ for m_, _ in f["models"].most_common(2)),
        "message_count": int(m["messageCount"]) if m.get("messageCount") is not None else None,
        "deleted": bool(m.get("deleted")) if m else None,
        "last_active": f["last_at"][:10],
        "console_url": "" if f["api"] or not console_base else f"{console_base.rstrip('/')}/admin/costs/sessions/{f['sid']}",
    }


def ds_conversations(feats: List[Feat], meta: Dict[str, Dict[str, Any]], console_base: str) -> List[Dict[str, Any]]:
    ranked = sorted(feats, key=lambda f: -f["cost"])
    rank_of = {f["sid"]: i + 1 for i, f in enumerate(ranked[:TOP_CONVERSATIONS])}
    discovery = {f["sid"] for f in ranked if f["pattern"] == UNCLASSIFIED[0]
                 and f["cost"] >= DISCOVERY_MIN_USD}
    discovery = {sid for sid in [f["sid"] for f in ranked if f["sid"] in discovery][:DISCOVERY_MAX]}
    rows = []
    for f in ranked:
        in_top, in_queue = f["sid"] in rank_of, f["sid"] in discovery
        if not (in_top or in_queue):
            continue
        queue = "top+discovery" if in_top and in_queue else "top" if in_top else "discovery"
        rows.append(conversation_row(f, rank_of.get(f["sid"]), queue, meta, console_base))
    return rows


def ds_gaps(by_session: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    buckets = [("under 1 min", 0, 60), ("1–5 min", 60, 300), ("5–15 min", 300, 900),
               ("15–60 min", 900, 3600), ("1–6 h", 3600, 21600), ("over 6 h", 21600, float("inf"))]
    acc = {b[0]: {"calls": 0, "rewrites": 0, "write_usd": 0.0, "total_usd": 0.0} for b in buckets}
    for calls in by_session.values():
        for a, b in zip(calls, calls[1:]):
            gap = (parse_ts(b) - parse_ts(a)).total_seconds()
            name = next(n for n, lo, hi in buckets if lo <= gap < hi)
            read = dig(b, "tokenUsage", "cacheReadInputTokens")
            write = dig(b, "tokenUsage", "cacheWriteInputTokens")
            acc[name]["calls"] += 1
            acc[name]["rewrites"] += 1 if write > read and write > 0 else 0
            acc[name]["write_usd"] += cost_of(b, "cacheWriteCost")
            acc[name]["total_usd"] += cost_of(b)
    return [{"gap": n, "order": i, "calls": acc[n]["calls"],
             "rewrite_rate": r4(acc[n]["rewrites"] / acc[n]["calls"]) if acc[n]["calls"] else 0,
             "cache_write_usd": r2(acc[n]["write_usd"]), "cost_usd": r2(acc[n]["total_usd"])}
            for i, (n, _, _) in enumerate(buckets)]


def ds_tools(by_session: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    calls_n: collections.Counter = collections.Counter()
    errors: collections.Counter = collections.Counter()
    sessions: Dict[str, set] = collections.defaultdict(set)
    followup: collections.Counter = collections.Counter()
    for sid, calls in by_session.items():
        for i, r in enumerate(calls):
            tc = r.get("toolCalls") or {}
            erring = {k: int(dig(x, "errors")) for k, x in tc.items() if dig(x, "errors")}
            for k, x in tc.items():
                calls_n[k] += int(dig(x, "calls"))
                errors[k] += int(dig(x, "errors"))
                sessions[k].add(sid)
            if erring and i + 1 < len(calls):
                nxt = cost_of(calls[i + 1])
                total = sum(erring.values())
                for k, e in erring.items():
                    followup[k] += nxt * e / total
    rows = [{"tool": k, "calls": n, "errors": errors[k], "error_rate": r4(errors[k] / n) if n else 0,
             "sessions": len(sessions[k]), "error_followup_usd": r2(followup[k])}
            for k, n in calls_n.items() if n >= 20]
    rows.sort(key=lambda r: -r["calls"])
    return rows


def ds_models(rows: List[Dict[str, Any]], fleet: float) -> List[Dict[str, Any]]:
    acc: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        m = short_model(dig(r, "modelInfo", "modelId", default="unknown"))
        a = acc[m]
        a["calls"] += 1
        a["cost"] += cost_of(r)
        a["unmetered"] += 0 if cost_known(r) else 1
        a["read_tok"] += dig(r, "tokenUsage", "cacheReadInputTokens")
        a["write_tok"] += dig(r, "tokenUsage", "cacheWriteInputTokens")
        a["output"] += cost_of(r, "outputCost")
    out = [{"model": m, "calls": int(a["calls"]), "cost_usd": r2(a["cost"]),
            "share": r4(a["cost"] / fleet) if fleet else 0,
            "write_read_ratio": round(a["write_tok"] / a["read_tok"], 2) if a["read_tok"] else None,
            "output_share": r4(a["output"] / a["cost"]) if a["cost"] else 0,
            "unmetered_calls": int(a["unmetered"])}
           for m, a in acc.items()]
    out.sort(key=lambda r: -r["cost_usd"])
    return out


def ds_compaction(rows: List[Dict[str, Any]], feats: List[Feat]) -> List[Dict[str, Any]]:
    events: collections.Counter = collections.Counter()
    sessions: Dict[str, set] = collections.defaultdict(set)
    for r in rows:
        for e in r.get("compactionEvents") or []:
            kind = e.get("kind") if isinstance(e, dict) else None
            if kind:
                events[kind] += 1
                sessions[kind].add(r["sessionId"])
        if dig(r, "windowRemovedMessages"):
            events["window_trim"] += 1
            sessions["window_trim"].add(r["sessionId"])
    over = [f for f in feats if f["peak_k"] >= 100]
    out = [{"signal": k, "events": n, "sessions": len(sessions[k]),
            "cost_usd": r2(sum(f["cost"] for f in feats if f["sid"] in sessions[k]))}
           for k, n in events.most_common()]
    out.append({"signal": "context over 100K", "events": None, "sessions": len(over),
                "cost_usd": r2(sum(f["cost"] for f in over))})
    return out


def ds_attachments(feats: List[Feat], fleet: float) -> List[Dict[str, Any]]:
    acc: Dict[str, List[float]] = collections.defaultdict(list)
    for f in feats:
        if not f["api"]:
            acc[f["attachment_kind"]].append(f["cost"])
    out = [{"attachments": k, "sessions": len(v), "cost_usd": r2(sum(v)),
            "share": r4(sum(v) / fleet) if fleet else 0, "cost_per_session": r2(sum(v) / len(v))}
           for k, v in acc.items()]
    out.sort(key=lambda r: -r["cost_usd"])
    return out


def ds_runs(previous: List[Dict[str, Any]], current: Dict[str, Any]) -> List[Dict[str, Any]]:
    kept = [r for r in previous if r.get("window_end") != current["window_end"]]
    return sorted(kept + [current], key=lambda r: r["window_end"])


DATASETS = [
    ("runs", "Run history",
     "One row per refresh of this dashboard: window, spend, idle re-write share, discovery queue size. From per-call cost rows."),
    ("patterns", "Spend by usage pattern",
     "Conversations in the window grouped by usage pattern, with spend split by cost mechanism and the engineering lever. From per-call cost rows and upload records."),
    ("modifiers", "Spend by modifier",
     "Traits that cut across patterns (over 100K context, slow cadence, large paste, tool loop, premium model, multi-week)."),
    ("daily", "Daily spend by pattern",
     "Spend per UTC day for the costliest patterns, the rest grouped as Other. From per-call cost rows."),
    ("conversations", "Top and unclassified conversations",
     "The 30 costliest conversations in the window and up to 40 unclassified ones over $1, with content-free features. Session ids only, no titles or users."),
    ("gaps", "Cache writes by pause",
     "Calls grouped by the pause since the previous call in the same conversation, with cache-write spend and how often the call re-wrote more than it read."),
    ("tools", "Tool reliability",
     "Calls and errors per tool (20+ calls), with the cost of the model call that handled each error."),
    ("models", "Spend by model",
     "Calls, spend, cache write:read ratio and calls with no recorded cost, per model."),
    ("compaction", "Context management events",
     "Compaction and trimming events recorded on calls in the window, and the spend of the conversations they occurred in."),
    ("attachments", "Spend by upload type",
     "Conversations grouped by what was uploaded into them, from upload records (type and size only)."),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_meta(meta_dir: Optional[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not meta_dir:
        return out
    for path in glob.glob(os.path.join(meta_dir, "*.json")):
        for item in load_items(path):
            if item.get("sessionId"):
                out[item["sessionId"]] = item
    return out


def load_history(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    with open(path) as fh:
        doc = json.load(fh)
    rows = doc if isinstance(doc, list) else doc.get("rows", [])
    return [r for r in rows if isinstance(r, dict)]


def write(out_dir: str, name: str, rows: Iterable[Dict[str, Any]]) -> None:
    with open(os.path.join(out_dir, f"{name}.json"), "w") as fh:
        json.dump(list(rows), fh, separators=(",", ":"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-rows", required=True)
    ap.add_argument("--uploads", required=True)
    ap.add_argument("--meta-dir")
    ap.add_argument("--history")
    ap.add_argument("--window-start", required=True)
    ap.add_argument("--window-end", required=True)
    ap.add_argument("--console-base", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    rows = [r for r in load_items(args.cost_rows) if r.get("sessionId") and r.get("timestamp")]
    rows = [r for r in rows if args.window_start <= r["timestamp"][:10] <= args.window_end]
    by_session: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by_session[r["sessionId"]].append(r)
    for calls in by_session.values():
        calls.sort(key=lambda r: r["timestamp"])

    uploads: Dict[str, List[Tuple[str, float]]] = collections.defaultdict(list)
    for u in load_items(args.uploads):
        key = u.get("GSI1PK") or ""
        if key.startswith("CONV#"):
            uploads[key[5:]].append((u.get("mimeType") or "", float(u.get("sizeBytes") or 0)))

    baselines = first_call_baselines(by_session)
    feats = [features(sid, calls, uploads.get(sid, []), baselines) for sid, calls in by_session.items()]
    for f in feats:
        f["pattern"], f["mods"] = classify(f)
    pattern_of = {f["sid"]: f["pattern"] for f in feats}
    fleet = sum(f["cost"] for f in feats)
    meta = load_meta(args.meta_dir)

    patterns = ds_patterns(feats, fleet)
    ranked = sorted((f["cost"] for f in feats), reverse=True)
    unclassified_big = [f for f in feats if f["pattern"] == UNCLASSIFIED[0] and f["cost"] >= DISCOVERY_MIN_USD]
    mech_total = {m: sum(f["mech"][m] for f in feats) for m, _, _ in MECHANISMS}
    current_run = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": args.window_start, "window_end": args.window_end,
        "window_days": (datetime.fromisoformat(args.window_end) - datetime.fromisoformat(args.window_start)).days + 1,
        "rules_version": RULES_VERSION, "console_base": args.console_base.rstrip("/"),
        "fleet_cost_usd": r2(fleet), "conversations": len(feats), "calls": len(rows),
        "users": len({r["userId"] for r in rows if r.get("userId")}),
        "cost_per_conversation": r2(fleet / len(feats)) if feats else 0,
        "top10_share": r4(sum(ranked[:10]) / fleet) if fleet else 0,
        "top100_share": r4(sum(ranked[:100]) / fleet) if fleet else 0,
        "idle_rewrite_usd": r2(mech_total["idle_rewrite"]),
        "idle_rewrite_share": r4(mech_total["idle_rewrite"] / fleet) if fleet else 0,
        "output_usd": r2(mech_total["output"]), "read_usd": r2(mech_total["read"]),
        "unclassified_over_1_n": len(unclassified_big),
        "unclassified_over_1_usd": r2(sum(f["cost"] for f in unclassified_big)),
        "unmetered_calls": sum(1 for r in rows if not cost_known(r)),
        "api_cost_usd": r2(sum(f["cost"] for f in feats if f["api"])),
        "top_pattern": patterns[0]["pattern"] if patterns else "",
        "top_pattern_share": patterns[0]["share"] if patterns else 0,
    }

    write(args.out, "runs", ds_runs(load_history(args.history), current_run))
    write(args.out, "patterns", patterns)
    write(args.out, "modifiers", ds_modifiers(feats, fleet))
    write(args.out, "daily", ds_daily(rows, pattern_of, patterns))
    write(args.out, "conversations", ds_conversations(feats, meta, args.console_base))
    write(args.out, "gaps", ds_gaps(by_session))
    write(args.out, "tools", ds_tools(by_session))
    write(args.out, "models", ds_models(rows, fleet))
    write(args.out, "compaction", ds_compaction(rows, feats))
    write(args.out, "attachments", ds_attachments(feats, fleet))

    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump([{"id": i, "title": title, "description": desc, "file": f"{i}.json"}
                   for i, title, desc in DATASETS], fh, indent=1)

    print(json.dumps({k: current_run[k] for k in (
        "window_start", "window_end", "fleet_cost_usd", "conversations", "calls", "top_pattern",
        "top_pattern_share", "idle_rewrite_share", "unclassified_over_1_n", "unmetered_calls")}, indent=1))
    for p in patterns:
        print(f"  {p['pattern']:20} {p['sessions']:6} ${p['cost_usd']:9.2f} {100 * p['share']:5.1f}%")


if __name__ == "__main__":
    main()

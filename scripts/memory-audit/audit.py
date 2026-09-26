"""AgentCore Memory baseline audit (Shared Projects spec §1, Phase 0.1).

Answers one question with evidence: does long-term memory work in a deployed
environment? Read-only by default. Only ``probe``, ``calibrate`` and ``cleanup`` write,
and they touch nothing but a synthetic ``memory-audit-probe-*`` actor.

Subcommands
-----------
``inventory`` (default, read-only)
    1. ``GetMemory``: strategies, namespace templates, event expiry. Compares
       each template with the namespace the backend queries
       (``session_factory.py``). A mismatch means retrieval reads an empty path.
    2. ``ListActors``: counts actors and classifies their ids. An actor whose
       only session has the same id as the actor is the ``base_agent.py``
       ``user_id or session_id`` fallback firing.
    3. ``ListMemoryRecords`` over the whole memory (namespace prefix ``/``):
       record count per strategy, per namespace shape, per actor (histogram),
       and record age. ``ListMemoryExtractionJobs`` (page size <= 50): status histogram of jobs
       still eligible to be (re)started, which in practice means failed jobs.
    4. CloudWatch (``FilterLogEvents``, no Logs Insights): discovery failures
       (``No memory strategies found``), retrieval hit lines
       (``Retrieved N customer context items``), retrieval failures and
       throttles in the runtime log group.

``probe`` (writes, dev only)
    The service half of the behavioral test (spec §1.2 step 5). Writes a
    two-message conversation stating a synthetic fact under a synthetic actor,
    waits for extraction, then runs ``RetrieveMemoryRecords`` the same way the
    runtime hook does (``namespacePath`` built from the backend template,
    ``topK=10`` and its relevance cut). Deletes the records and events afterwards
    unless ``--keep``. The app half (session A states a fact, session B recalls
    it) is a manual chat-UI step; see ``README.md``.

``calibrate`` (writes, dev only)
    Relevance-cut calibration on ``calibration_set.json``: one synthetic actor
    states ~15 facts and preferences, extraction is polled until the record
    count settles, then each fact is queried three ways (direct, indirect,
    wrapped in filler) plus ~20 unrelated questions, raw and with filler
    stripped. Every returned record is labelled from its text; ``summary.json``
    gets score distributions and per-policy recall, precision and noise.
    Cleans up in ``finally``, with late sweeps. ``--reanalyze`` recomputes the
    summary from ``raw/`` without AWS calls.

Output
------
``--out DIR`` receives ``summary.json`` (aggregates only: counts, histograms,
namespace *shapes* with ids replaced by placeholders) and ``raw/`` (actor ids,
record text, log lines). **``raw/`` contains personal data. Keep it out of the
repository**; the decision record and PRs use ``summary.json`` only.

Credentials come from the normal boto3 chain (``--profile`` or ``AWS_PROFILE``).
Needs boto3 >= 1.43 for the ``bedrock-agentcore`` clients (the backend's
``agentcore`` extra has it; the system CLI may not).

Examples::

    python scripts/memory-audit/audit.py --profile dev-ai --region us-west-2 \\
        --prefix dev-boisestateai-v2 --out /tmp/memaudit inventory
    python scripts/memory-audit/audit.py --profile dev-ai --region us-west-2 \\
        --prefix dev-boisestateai-v2 --out /tmp/memaudit probe
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 10, "mode": "adaptive"},
    user_agent_extra="agentcore-memory-audit/1.0",
)

# Namespaces the backend queries, as written in
# backend/src/agents/main_agent/session/session_factory.py. The test suite
# asserts these strings still appear there verbatim, so a backend change that
# is not mirrored here fails CI instead of silently skewing the comparison.
BACKEND_NAMESPACE_TEMPLATES = {
    "USER_PREFERENCE": "/strategies/{memoryStrategyId}/actors/{actorId}",
    "SEMANTIC": "/strategies/{memoryStrategyId}/actors/{actorId}",
    "SUMMARIZATION": "/strategies/{memoryStrategyId}/actors/{actorId}/sessions/{sessionId}",
}

# Retrieval parameters of TurnBasedSessionManager.retrieve_customer_context.
RETRIEVAL_TOP_K = 10
RETRIEVAL_RELEVANCE = 0.4

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PREVIEW_RE = re.compile(r"^preview[-_]", re.I)

LOG_PATTERNS = {
    "discovery_failed": '"No memory strategies found"',
    "retrieval_hits": '"customer context items"',
    "retrieval_failed": '"memory retrieval failed"',
    "retrieval_throttled": '"memory retrieval throttled"',
    "retrieval_reconnected": '"memory retrieval reconnected"',
    "retrieval_error": '"Failed to retrieve customer context"',
    "ltm_enabled": '"Long-term memory"',
}


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _paginate(call, key: str, **kwargs) -> Iterable[dict]:
    token = None
    while True:
        page = call(**kwargs, **({"nextToken": token} if token else {}))
        yield from page.get(key, [])
        token = page.get("nextToken")
        if not token:
            return


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    return str(o)


def classify_actor(actor_id: str, session_ids: list[str]) -> str:
    """Bucket an actor id without revealing it.

    ``session_fallback`` is the precise signature of ``user_id or session_id``:
    the actor owns exactly the one session named after itself.
    """
    if session_ids and actor_id in session_ids:
        return "session_fallback"
    if PREVIEW_RE.match(actor_id):
        return "preview"
    m = UUID_RE.match(actor_id)
    if m:
        return f"uuid_v{m.group(1)}"
    return "other"


def namespace_shape(ns: str, strategy_ids: dict[str, str]) -> str:
    """Replace ids in a concrete namespace with placeholders, keeping structure."""
    trailing = "/" if ns.endswith("/") else ""
    parts = ns.strip("/").split("/")
    out = []
    for i, p in enumerate(parts):
        prev = parts[i - 1] if i else ""
        if prev == "strategies":
            out.append("{" + strategy_ids.get(p, "unknownStrategy") + "}")
        elif prev == "actors":
            out.append("{actorId}")
        elif prev == "sessions":
            out.append("{sessionId}")
        else:
            out.append(p)
    return "/" + "/".join(out) + trailing


def histogram(values: Iterable[int], edges: tuple[int, ...] = (0, 1, 2, 5, 10, 25, 50, 100, 250)) -> dict[str, int]:
    buckets: Counter[str] = Counter()
    for v in values:
        label = f">{edges[-1]}"
        for lo, hi in zip(edges, edges[1:] + (None,)):
            if hi is None:
                break
            if lo <= v < hi:
                label = f"{lo}" if hi - lo == 1 else f"{lo}-{hi - 1}"
                break
        buckets[label] += 1
    return dict(buckets)


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


# --------------------------------------------------------------------------- #
# Calibration helpers (pure; covered by tests/supply_chain)                   #
# --------------------------------------------------------------------------- #
CALIBRATION_SET = Path(__file__).resolve().parent / "calibration_set.json"
RETRIEVED_TYPES = ("SEMANTIC", "USER_PREFERENCE")

# Leading conversational filler: acknowledgements, discourse markers and
# "different question:"-style pivots. Deliberately conservative: it only ever
# removes words from the front of the last paragraph.
_FILLER_LEAD = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|thanks|thank you|thx|cool|great|nice|hmm+|um+|so|anyway|also|and|btw|by the way"
    r"|random|separate thing|totally unrelated|unrelated|quick one|remind me|another thing"
    r"|one more thing(?: before i forget)?|(?:a )?(?:different|another|new|quick|separate|unrelated) question)"
    r"\b\s*[,.:;!\-–—]*\s*)+",
    re.I,
)
_ACK_SENTENCE = re.compile(
    r"^\s*(?:ok(?:ay)?|thanks|thank you|cool|great|nice|got it|perfect|awesome)\b[^.!?\n]{0,40}[.!]\s+", re.I
)


def strip_query_filler(text: str) -> str:
    """The question a message asks, minus pasted context and filler.

    Keeps the last paragraph (a pasted block usually precedes the question),
    then drops leading acknowledgement sentences and discourse markers. Falls
    back to the original text rather than ever returning an empty query.
    """
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    q = paras[-1] if paras else text
    prev = None
    while prev != q:
        prev = q
        q = _FILLER_LEAD.sub("", _ACK_SENTENCE.sub("", q))
    q = q.strip()
    if not q:
        return text.strip()
    return q[0].upper() + q[1:]


def record_matches(text: str, keys: Iterable[str]) -> bool:
    """A record matches a fact when any key starts a word in its text."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k.lower()), text.lower()) for k in keys)


def _cut(c: float):
    return lambda hits: [h for h in hits if h["score"] >= c]


def _top_k_floor(k: int, floor: float):
    return lambda hits: [h for h in hits[:k] if h["score"] >= floor]


def _margin(floor: float, margin: float, high: float):
    """Everything >= ``high``, plus the top hit when it is >= ``floor`` and
    beats the runner-up by ``margin`` (a lone hit always clears the margin)."""
    def policy(hits: list[dict]) -> list[dict]:
        kept = [h for h in hits if h["score"] >= high]
        if hits and hits[0]["score"] >= floor and hits[0] not in kept:
            runner_up = hits[1]["score"] if len(hits) > 1 else 0.0
            if hits[0]["score"] - runner_up >= margin:
                kept.insert(0, hits[0])
        return kept
    return policy


CALIBRATION_POLICIES = {
    **{f"cut>={c:.2f}": _cut(c) for c in (0.30, 0.35, 0.38, 0.40, 0.42, 0.45, 0.50, 0.55)},
    **{f"top1>={f:.2f}": _top_k_floor(1, f) for f in (0.35, 0.38, 0.40, 0.42)},
    **{f"top3>={f:.2f}": _top_k_floor(3, f) for f in (0.35, 0.40)},
    **{f"margin{m:.2f}>={f:.2f}|>={hi:.2f}": _margin(f, m, hi)
       for f in (0.35, 0.40) for m in (0.03, 0.05) for hi in (0.50,)},
}


def _dist(values: list[float]) -> dict[str, Any]:
    r = lambda v: None if v is None else round(v, 3)  # noqa: E731
    return {"n": len(values), "min": r(min(values, default=None)), "p10": r(pct(values, 0.1)),
            "p50": r(pct(values, 0.5)), "p90": r(pct(values, 0.9)), "max": r(max(values, default=None))}


def analyze_calibration(rows: list[dict]) -> dict[str, Any]:
    """Aggregate labelled retrieval results into distributions and policy metrics.

    ``rows``: one per (query, variant, namespace type), each with ``query``,
    ``fact`` (None for a negative), ``style``, ``variant`` and ``hits`` sorted
    by score descending, each hit ``{"score", "correct", "chars"}`` and
    optionally ``related`` (an on-topic record of a related fact: neither a
    match nor noise). Returns aggregates only (no text).
    """
    def wrong(h: dict) -> bool:
        return not h["correct"] and not h.get("related")

    queries: dict[tuple[str, str], dict[str, list[dict]]] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        queries.setdefault((row["query"], row["variant"]), {})[row["namespace"]] = row["hits"]
        meta[row["query"]] = {"fact": row["fact"], "style": row["style"]}
    variants = sorted({v for _, v in queries})

    # Score distributions, per namespace type, variant and style.
    dists: dict[str, Any] = {}
    for (qid, variant), by_ns in queries.items():
        style = meta[qid]["style"]
        for ns, hits in by_ns.items():
            d = dists.setdefault(variant, {}).setdefault(ns, {}).setdefault(
                style, {"bestCorrect": [], "bestIncorrect": [], "allIncorrect": [], "gap": [], "rank1OnTopic": 0,
                        "queriesWithCorrect": 0, "queries": 0, "related": 0})
            d["queries"] += 1
            correct = [h["score"] for h in hits if h["correct"]]
            incorrect = [h["score"] for h in hits if wrong(h)]
            d["allIncorrect"].extend(incorrect)
            if incorrect:
                d["bestIncorrect"].append(max(incorrect))
            if correct:
                d["queriesWithCorrect"] += 1
                d["bestCorrect"].append(max(correct))
                d["rank1OnTopic"] += int(not wrong(hits[0]))
                d["gap"].append(max(correct) - (max(incorrect) if incorrect else 0.0))
            d["related"] += sum(1 for h in hits if h.get("related"))
    for variant in dists.values():
        for ns in variant.values():
            for style, d in ns.items():
                ns[style] = {k: (_dist(v) if isinstance(v, list) else v) for k, v in d.items()}

    # Policy metrics. A turn is a hit when any namespace keeps a correct record.
    metrics: dict[str, Any] = {}
    for variant in variants:
        for name, policy in CALIBRATION_POLICIES.items():
            fact_turns = neg_turns = hits_ = ceiling = 0
            kept_correct = kept_wrong = neg_with_any = neg_items = chars = 0
            by_style: dict[str, list[int]] = {}
            for (qid, v), by_ns in queries.items():
                if v != variant:
                    continue
                kept = [h for hs in by_ns.values() for h in policy(hs)]
                chars += sum(h["chars"] for h in kept)
                if meta[qid]["fact"] is None:
                    neg_turns += 1
                    neg_with_any += int(bool(kept))
                    neg_items += len(kept)
                    continue
                kept = [h for h in kept if not h.get("related")]
                fact_turns += 1
                hit = any(h["correct"] for h in kept)
                hits_ += int(hit)
                ceiling += int(any(h["correct"] for hs in by_ns.values() for h in hs))
                kept_correct += sum(h["correct"] for h in kept)
                kept_wrong += sum(wrong(h) for h in kept)
                s = by_style.setdefault(meta[qid]["style"], [0, 0])
                s[0] += int(hit)
                s[1] += 1
            total_turns = fact_turns + neg_turns
            metrics.setdefault(variant, {})[name] = {
                "recall": round(hits_ / fact_turns, 3) if fact_turns else None,
                "recallByStyle": {k: round(a / b, 3) for k, (a, b) in sorted(by_style.items())},
                "retrievable": round(ceiling / fact_turns, 3) if fact_turns else None,
                "precision": round(kept_correct / (kept_correct + kept_wrong), 3) if kept_correct + kept_wrong else None,
                "wrongItemsPerFactTurn": round(kept_wrong / fact_turns, 2) if fact_turns else None,
                "negativeTurnsWithInjection": round(neg_with_any / neg_turns, 3) if neg_turns else None,
                "itemsPerNegativeTurn": round(neg_items / neg_turns, 2) if neg_turns else None,
                "injectedTokensPerTurn": round(chars / 4 / total_turns) if total_turns else None,
            }
    return {"scoreDistributions": dists, "policies": metrics,
            "turns": {"fact": sum(1 for (q, v) in queries if v == variants[0] and meta[q]["fact"]),
                      "negative": sum(1 for (q, v) in queries if v == variants[0] and not meta[q]["fact"])}
            if variants else {}}


class Audit:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        session = boto3.session.Session(profile_name=args.profile, region_name=args.region)
        self.ssm = session.client("ssm", config=BOTO_CONFIG)
        self.cp = session.client("bedrock-agentcore-control", config=BOTO_CONFIG)
        self.dp = session.client("bedrock-agentcore", config=BOTO_CONFIG)
        self.logs = session.client("logs", config=BOTO_CONFIG)
        self.out = Path(args.out)
        (self.out / "raw").mkdir(parents=True, exist_ok=True)
        self.summary: dict[str, Any] = {"generatedAt": datetime.now(timezone.utc).isoformat()}

    # ---- discovery (same SSM paths backup.py uses) ----------------------- #
    def _ssm(self, name: str) -> str | None:
        try:
            return self.ssm.get_parameter(Name=f"/{self.args.prefix}/{name}")["Parameter"]["Value"]
        except ClientError:
            return None

    def memory_id(self) -> str:
        mid = self.args.memory_id or self._ssm("inference-api/memory-id")
        if not mid:
            sys.exit("memory id not found (pass --memory-id or check SSM /{prefix}/inference-api/memory-id)")
        return mid

    def write_raw(self, name: str, rows: Iterable[Any]) -> None:
        with open(self.out / "raw" / name, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, default=_json_default) + "\n")

    # ---- step 1: inventory ---------------------------------------------- #
    def step_inventory(self, mid: str) -> dict[str, str]:
        mem = self.cp.get_memory(memoryId=mid)["memory"]
        self._memory = mem
        self.write_raw("memory.jsonl", [mem])
        strategies = []
        type_by_id: dict[str, str] = {}
        for s in mem.get("strategies", []) or mem.get("memoryStrategies", []):
            sid = s.get("strategyId") or s.get("memoryStrategyId")
            stype = s.get("type") or s.get("memoryStrategyType")
            type_by_id[sid] = stype
            templates = s.get("namespaces") or []
            expected = BACKEND_NAMESPACE_TEMPLATES.get(stype)
            strategies.append({
                "type": stype,
                "name": s.get("name"),
                "status": s.get("status"),
                "namespaceTemplates": templates,
                "backendQueries": expected,
                "templateMatchesBackendExactly": expected in templates if expected else None,
                "templateMatchesIgnoringTrailingSlash": (
                    any(t.rstrip("/") == expected.rstrip("/") for t in templates) if expected else None
                ),
            })
        self.summary["inventory"] = {
            "status": mem.get("status"),
            "eventExpiryDays": mem.get("eventExpiryDuration"),
            "strategies": strategies,
        }
        return type_by_id

    # ---- step 2: write path --------------------------------------------- #
    def step_actors(self, mid: str) -> list[str]:
        actors = [a["actorId"] for a in _paginate(self.dp.list_actors, "actorSummaries", memoryId=mid, maxResults=100)]
        classes: Counter[str] = Counter()
        sessions_per_actor: list[int] = []
        raw = []
        for aid in actors:
            sids = [s["sessionId"] for s in _paginate(
                self.dp.list_sessions, "sessionSummaries", memoryId=mid, actorId=aid, maxResults=100)]
            cls = classify_actor(aid, sids)
            classes[cls] += 1
            sessions_per_actor.append(len(sids))
            raw.append({"actorId": aid, "class": cls, "sessions": len(sids)})
        self.write_raw("actors.jsonl", raw)
        self.summary["actors"] = {
            "count": len(actors),
            "idClasses": dict(classes),
            "sessionsPerActorHistogram": histogram(sessions_per_actor),
            "totalSessions": sum(sessions_per_actor),
        }
        return actors

    # ---- step 3: extraction --------------------------------------------- #
    def step_records(self, mid: str, type_by_id: dict[str, str], actors: list[str]) -> None:
        records = list(_paginate(self.dp.list_memory_records, "memoryRecordSummaries",
                                 memoryId=mid, namespace="/", maxResults=100))
        now = datetime.now(timezone.utc)
        by_type: Counter[str] = Counter()
        by_shape: Counter[str] = Counter()
        per_actor: Counter[str] = Counter()
        per_actor_type: dict[str, Counter[str]] = {}
        ages: list[float] = []
        lengths: dict[str, list[int]] = {}
        raw = []
        for r in records:
            stype = type_by_id.get(r.get("memoryStrategyId"), "UNKNOWN")
            by_type[stype] += 1
            nss = r.get("namespaces") or []
            for ns in nss:
                by_shape[namespace_shape(ns, type_by_id)] += 1
                m = re.search(r"/actors/([^/]+)", ns)
                if m:
                    per_actor[m.group(1)] += 1
                    per_actor_type.setdefault(stype, Counter())[m.group(1)] += 1
            created = r.get("createdAt")
            if isinstance(created, datetime):
                ages.append((now - created).total_seconds() / 86400)
            text = (r.get("content") or {}).get("text", "")
            lengths.setdefault(stype, []).append(len(text))
            raw.append({"type": stype, "namespaces": nss, "createdAt": created, "text": text})
        self.write_raw("records.jsonl", raw)

        actors_with_records = set(per_actor)
        self.summary["records"] = {
            "total": len(records),
            "byStrategyType": dict(by_type),
            "byNamespaceShape": dict(by_shape),
            "actorsWithAnyRecord": len(actors_with_records),
            "actorsWithoutRecords": len(set(actors) - actors_with_records),
            "recordsPerActorHistogram": histogram(per_actor.values()),
            "actorsPerType": {t: len(c) for t, c in per_actor_type.items()},
            "ageDays": {"p50": pct(ages, 0.5), "p90": pct(ages, 0.9), "max": max(ages) if ages else None,
                        "last7d": sum(1 for a in ages if a <= 7), "last30d": sum(1 for a in ages if a <= 30)},
            "textLengthChars": {t: {"p50": pct(v, 0.5), "p90": pct(v, 0.9)} for t, v in lengths.items()},
        }

        try:
            jobs = list(_paginate(self.dp.list_memory_extraction_jobs, "jobs", memoryId=mid, maxResults=50))
        except ClientError as exc:
            self.summary["extractionJobs"] = {"error": exc.response["Error"]["Code"]}
            return
        self.write_raw("extraction_jobs.jsonl", jobs)
        self.summary["extractionJobs"] = {
            "note": "ListMemoryExtractionJobs lists jobs eligible to be (re)started, i.e. not a full history",
            "count": len(jobs),
            "byStatus": dict(Counter(j.get("status") for j in jobs)),
            "byStrategyType": dict(Counter(type_by_id.get(j.get("strategyId"), "UNKNOWN") for j in jobs)),
            "failureReasons": dict(Counter((j.get("failureReason") or "")[:80] for j in jobs if j.get("failureReason"))),
        }

    # ---- step 4: read path ---------------------------------------------- #
    def step_logs(self) -> None:
        rid = self.args.runtime_id or self._ssm("inference-api/runtime-id")
        if not rid:
            self.summary["logs"] = {"error": "runtime id not found"}
            return
        group = f"/aws/bedrock-agentcore/runtimes/{rid}-DEFAULT"
        start = int((datetime.now(timezone.utc) - timedelta(days=self.args.days)).timestamp() * 1000)
        result: dict[str, Any] = {"windowDays": self.args.days}
        raw = []
        for key, pattern in LOG_PATTERNS.items():
            events = []
            try:
                for e in _paginate_logs(self.logs, group, pattern, start, self.args.max_log_events):
                    events.append(e)
            except ClientError as exc:
                result[key] = {"error": exc.response["Error"]["Code"]}
                continue
            entry: dict[str, Any] = {"lines": len(events)}
            if key == "retrieval_hits":
                counts = [int(m.group(1)) for e in events
                          if (m := re.search(r"Retrieved (\d+) customer context items", e["message"]))]
                entry["itemsPerHitTurnHistogram"] = histogram(counts)
            result[key] = entry
            raw.extend({"pattern": key, "ts": e["timestamp"], "message": e["message"][:2000]} for e in events)
        self.write_raw("log_lines.jsonl", raw)
        self.summary["logs"] = result

    def run_inventory(self) -> None:
        mid = self.memory_id()
        type_by_id = self.step_inventory(mid)
        actors = self.step_actors(mid)
        self.step_records(mid, type_by_id, actors)
        if not self.args.skip_logs:
            self.step_logs()

    # ---- step 5 (service half): probe ------------------------------------ #
    def run_probe(self) -> None:
        mid = self.memory_id()
        type_by_id = self.step_inventory(mid)
        actor = f"memory-audit-probe-{uuid.uuid4()}"
        session = f"memory-audit-probe-session-{uuid.uuid4()}"
        fact = self.args.fact
        now = datetime.now(timezone.utc)
        turns = [
            ("USER", f"Please remember this for later: {fact}"),
            ("ASSISTANT", f"Got it. I'll remember that {fact[0].lower() + fact[1:]}"),
        ]
        event_ids = []
        for i, (role, text) in enumerate(turns):
            ev = self.dp.create_event(
                memoryId=mid, actorId=actor, sessionId=session,
                eventTimestamp=now + timedelta(seconds=i),
                payload=[{"conversational": {"content": {"text": text}, "role": role}}],
            )["event"]
            event_ids.append(ev["eventId"])
        result: dict[str, Any] = {"eventsWritten": len(event_ids)}
        started = time.monotonic()
        prefix = "/strategies/"
        found: list[dict] = []
        while time.monotonic() - started < self.args.wait_seconds:
            found = [r for r in _paginate(self.dp.list_memory_records, "memoryRecordSummaries",
                                          memoryId=mid, namespace="/", maxResults=100)
                     if any(f"/actors/{actor}" in ns for ns in r.get("namespaces") or [])]
            if {type_by_id.get(r["memoryStrategyId"]) for r in found} >= set(type_by_id.values()):
                break
            time.sleep(15)
        result["secondsToFirstRecords"] = round(time.monotonic() - started) if found else None
        result["recordsByType"] = dict(Counter(type_by_id.get(r["memoryStrategyId"], "UNKNOWN") for r in found))
        result["recordNamespaceShapes"] = sorted({namespace_shape(ns, type_by_id)
                                                  for r in found for ns in r.get("namespaces") or []})

        # Retrieve exactly as retrieve_customer_context does ("backend"), and
        # with the strategy's own template, which ends in "/" ("template").
        templates = {s.get("strategyId") or s.get("memoryStrategyId"): (s.get("namespaces") or [None])[0]
                     for s in self._memory.get("strategies", [])}
        retrieval = {}
        for sid, stype in type_by_id.items():
            tmpl = BACKEND_NAMESPACE_TEMPLATES.get(stype)
            if not tmpl or stype == "SUMMARIZATION":
                continue
            variants = {"backend": tmpl}
            if templates.get(sid):
                variants["template"] = templates[sid]
            for label, t in variants.items():
                path = t.format(memoryStrategyId=sid, actorId=actor, sessionId=session)
                for qi, question in enumerate(self.args.questions):
                    t0 = time.monotonic()
                    hits = self.dp.retrieve_memory_records(
                        memoryId=mid, namespacePath=path,
                        searchCriteria={"searchQuery": question, "topK": RETRIEVAL_TOP_K},
                    ).get("memoryRecordSummaries", [])
                    ms = round((time.monotonic() - t0) * 1000)
                    kept = [h for h in hits if h.get("score", 0.0) >= RETRIEVAL_RELEVANCE]
                    retrieval[f"{stype}/{label}/q{qi}"] = {
                        "trailingSlash": path.endswith("/"),
                        "hits": len(hits), "keptAfterRelevanceCut": len(kept), "latencyMs": ms,
                        "topScore": max((h.get("score", 0.0) for h in hits), default=None),
                        "factRecalled": any(
                            self.args.expect.lower() in (h.get("content") or {}).get("text", "").lower()
                            for h in kept),
                    }
        result["questions"] = {f"q{i}": q for i, q in enumerate(self.args.questions)}
        result["retrievalViaBackendNamespace"] = retrieval
        self.write_raw("probe_records.jsonl", found)

        if not self.args.keep:
            deleted = 0
            found = [r for r in _paginate(self.dp.list_memory_records, "memoryRecordSummaries",
                                          memoryId=mid, namespace="/", maxResults=100)
                     if any(f"/actors/{actor}" in ns for ns in r.get("namespaces") or [])]
            for r in found:
                self.dp.delete_memory_record(memoryId=mid, memoryRecordId=r["memoryRecordId"])
                deleted += 1
            for eid in event_ids:
                self.dp.delete_event(memoryId=mid, actorId=actor, sessionId=session, eventId=eid)
            result["cleanup"] = {"recordsDeleted": deleted, "eventsDeleted": len(event_ids)}
            # Extraction can land late; say so rather than claim a clean slate.
            result["cleanupNote"] = f"re-run with --cleanup-actor {actor} if records appear after this run"
        result["actor"] = "synthetic (memory-audit-probe-*)"
        self.summary["probe"] = result
        (self.out / "raw" / "probe_actor.txt").write_text(actor + "\n")

    # ---- calibrate: relevance cut on a labelled synthetic eval set -------- #
    def _actor_records(self, mid: str, actor: str) -> list[dict]:
        """Every record of ``actor`` in any strategy. ``namespace`` is a prefix
        filter, so this reads the actor's records only, not the whole memory."""
        out = []
        for sid in self._strategy_ids:
            out += [r for r in _paginate(self.dp.list_memory_records, "memoryRecordSummaries", memoryId=mid,
                                         namespace=f"/strategies/{sid}/actors/{actor}/", maxResults=100)
                    if any(f"/actors/{actor}/" in ns + "/" for ns in r.get("namespaces") or [])]
        return out

    def _delete_actor_records(self, mid: str, actor: str) -> int:
        n = 0
        for r in self._actor_records(mid, actor):
            try:
                self.dp.delete_memory_record(memoryId=mid, memoryRecordId=r["memoryRecordId"])
                n += 1
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
        return n

    def run_calibrate(self) -> None:
        spec = json.loads(CALIBRATION_SET.read_text())
        if self.args.reanalyze:
            rows = [json.loads(line) for line in open(self.out / "raw" / "calibrate_hits.jsonl")]
            self._label(rows, {f["id"]: f for f in spec["facts"]})
            prior = json.loads((self.out / "summary.json").read_text()) if (self.out / "summary.json").exists() else {}
            result = {k: v for k, v in (prior.get("calibrate") or {}).items() if k in ("extraction", "cleanup")}
            result.update(analyze_calibration([self._strip_row(r) for r in rows]))
            self.summary["calibrate"] = result
            return

        mid = self.memory_id()
        type_by_id = self.step_inventory(mid)
        self._strategy_ids = list(type_by_id)
        sid_by_type = {t: s for s, t in type_by_id.items()}
        actor = f"memory-audit-probe-{uuid.uuid4()}"
        (self.out / "raw" / "calibrate_actor.txt").write_text(actor + "\n")
        print(f"calibrate: synthetic actor {actor}", file=sys.stderr)
        facts = {f["id"]: f for f in spec["facts"]}
        fill = lambda s: s.replace("{pasted}", spec["fillers"]["pasted"])  # noqa: E731
        written: list[tuple[str, str]] = []  # (sessionId, eventId)
        result: dict[str, Any] = {"actor": "synthetic (memory-audit-probe-*)"}
        try:
            # 1. Write the conversations: one event per message, as the runtime does.
            t = datetime.now(timezone.utc)
            for group in spec["sessions"]:
                session = f"memory-audit-probe-session-{uuid.uuid4()}"
                for fid in group:
                    for role, text in (("USER", facts[fid]["say"]),
                                       ("ASSISTANT", "Thanks, noted. Happy to help with that.")):
                        t += timedelta(seconds=1)
                        ev = self.dp.create_event(
                            memoryId=mid, actorId=actor, sessionId=session, eventTimestamp=t,
                            payload=[{"conversational": {"content": {"text": text}, "role": role}}],
                        )["event"]
                        written.append((session, ev["eventId"]))
            result["eventsWritten"] = len(written)

            # 2. Wait for extraction to settle: both retrieved types present and
            #    the record count unchanged for --settle-seconds.
            started = time.monotonic()
            first_seen: dict[str, int] = {}
            last_count, last_change = -1, started
            records: list[dict] = []
            while time.monotonic() - started < self.args.wait_seconds:
                records = self._actor_records(mid, actor)
                now = time.monotonic()
                for r in records:
                    first_seen.setdefault(type_by_id.get(r["memoryStrategyId"], "UNKNOWN"), round(now - started))
                if len(records) != last_count:
                    last_count, last_change = len(records), now
                print(f"calibrate: t+{round(now - started)}s records={len(records)} "
                      f"types={sorted(first_seen)}", file=sys.stderr)
                if set(RETRIEVED_TYPES) <= set(first_seen) and now - last_change >= self.args.settle_seconds:
                    break
                time.sleep(15)
            by_type = Counter(type_by_id.get(r["memoryStrategyId"], "UNKNOWN") for r in records)
            labelled = []
            for r in records:
                text = (r.get("content") or {}).get("text", "")
                labelled.append({"id": r["memoryRecordId"], "type": type_by_id.get(r["memoryStrategyId"]),
                                 "facts": [f for f in facts if record_matches(text, facts[f]["keys"])],
                                 "text": text})
            self.write_raw("calibrate_records.jsonl", labelled)
            retrievable = [x for x in labelled if x["type"] in RETRIEVED_TYPES]
            result["extraction"] = {
                "secondsToFirstRecordByType": first_seen,
                "waitedSeconds": round(time.monotonic() - started),
                "recordsByType": dict(by_type),
                "factsFoundByType": {t: sorted({f for x in retrievable if x["type"] == t for f in x["facts"]})
                                     for t in RETRIEVED_TYPES},
                "factsMissing": sorted(set(facts) - {f for x in retrievable for f in x["facts"]}),
                "recordsMatchingSeveralFacts": sum(1 for x in retrievable if len(x["facts"]) > 1),
                "recordsMatchingNoFact": sum(1 for x in retrievable if not x["facts"]),
                "recordChars": {t: _dist([len(x["text"]) for x in retrievable if x["type"] == t])
                                for t in RETRIEVED_TYPES},
            }

            # 3. Replay retrieval as retrieve_customer_context does, raw and
            #    with filler stripped, for every fact query and negative.
            queries = [(f"{fid}/{style}", fid, style, fill(q))
                       for fid, f in facts.items() for style, q in f["queries"].items()]
            queries += [(f"neg{i:02d}", None, "negative", fill(q)) for i, q in enumerate(spec["negatives"])]
            rows = []
            for qid, fid, style, text in queries:
                for variant, q in (("raw", text), ("stripped", strip_query_filler(text))):
                    for stype in RETRIEVED_TYPES:
                        sid = sid_by_type.get(stype)
                        if not sid:
                            continue
                        path = BACKEND_NAMESPACE_TEMPLATES[stype].format(memoryStrategyId=sid, actorId=actor)
                        hits = self.dp.retrieve_memory_records(
                            memoryId=mid, namespacePath=path,
                            searchCriteria={"searchQuery": q[:10_000], "topK": RETRIEVAL_TOP_K},
                        ).get("memoryRecordSummaries", [])
                        out = [{"score": h.get("score", 0.0), "recordId": h["memoryRecordId"],
                                "text": (h.get("content") or {}).get("text", "")}
                               for h in sorted(hits, key=lambda h: -h.get("score", 0.0))]
                        rows.append({"query": qid, "fact": fid, "style": style, "variant": variant,
                                     "namespace": stype, "queryText": q, "hits": out})
            self._label(rows, facts)
            self.write_raw("calibrate_hits.jsonl", rows)
            result.update(analyze_calibration([self._strip_row(r) for r in rows]))
        finally:
            # 4. Always clean up, including records that extraction delivers late.
            if not self.args.keep:
                cleanup = {"recordsDeleted": self._delete_actor_records(mid, actor), "eventsDeleted": 0}
                for session, eid in written:
                    try:
                        self.dp.delete_event(memoryId=mid, actorId=actor, sessionId=session, eventId=eid)
                        cleanup["eventsDeleted"] += 1
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                            raise
                sweeps = 0
                while sweeps < self.args.late_sweeps:
                    time.sleep(60)
                    sweeps += 1
                    late = self._delete_actor_records(mid, actor)
                    cleanup["recordsDeleted"] += late
                    if not late and sweeps >= 2:
                        break
                cleanup["lateSweeps"] = sweeps
                cleanup["recordsRemaining"] = len(self._actor_records(mid, actor))
                cleanup["eventsRemaining"] = sum(
                    1 for s in {s for s, _ in written}
                    for _ in _paginate(self.dp.list_events, "events", memoryId=mid, actorId=actor,
                                       sessionId=s, maxResults=100))
                result["cleanup"] = cleanup
                print(f"calibrate: cleanup {cleanup}; if records appear later run "
                      f"cleanup --cleanup-actor {actor}", file=sys.stderr)
            self.summary["calibrate"] = result

    @staticmethod
    def _label(rows: list[dict], facts: dict[str, dict]) -> None:
        """Label every hit from its text with the eval set's current keys, so a
        key fix can be applied to a finished run with ``--reanalyze``."""
        for row in rows:
            for h in row["hits"]:
                h["facts"] = [f for f in facts if record_matches(h["text"], facts[f]["keys"])]
                h["correct"] = row["fact"] in h["facts"]
                fact = facts.get(row["fact"], {})
                h["related"] = not h["correct"] and (
                    bool(set(h["facts"]) & set(fact.get("related", [])))
                    or record_matches(h["text"], fact.get("relatedKeys", [])))
                h["chars"] = len(h["text"])

    @staticmethod
    def _strip_row(row: dict) -> dict:
        return {**row, "hits": [{k: h[k] for k in ("score", "correct", "related", "chars")} for h in row["hits"]]}

    def run_cleanup(self, actor: str) -> None:
        if not actor.startswith("memory-audit-probe-"):
            sys.exit("--cleanup-actor only accepts synthetic memory-audit-probe-* actors")
        mid = self.memory_id()
        n = 0
        for r in _paginate(self.dp.list_memory_records, "memoryRecordSummaries",
                           memoryId=mid, namespace="/", maxResults=100):
            if any(f"/actors/{actor}" in ns for ns in r.get("namespaces") or []):
                self.dp.delete_memory_record(memoryId=mid, memoryRecordId=r["memoryRecordId"])
                n += 1
        self.summary["cleanup"] = {"recordsDeleted": n}


def _paginate_logs(logs, group: str, pattern: str, start_ms: int, cap: int) -> Iterable[dict]:
    token = None
    seen = 0
    while True:
        kw = {"logGroupName": group, "filterPattern": pattern, "startTime": start_ms}
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        for e in page.get("events", []):
            yield e
            seen += 1
            if seen >= cap:
                return
        token = page.get("nextToken")
        if not token:
            return


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--profile")
    p.add_argument("--region", default="us-west-2")
    p.add_argument("--prefix", required=True, help="CDK project prefix, e.g. dev-boisestateai-v2")
    p.add_argument("--memory-id", help="override SSM discovery")
    p.add_argument("--runtime-id", help="override SSM discovery")
    p.add_argument("--out", required=True, help="output dir; raw/ holds personal data, keep it out of git")
    p.add_argument("--days", type=int, default=7, help="log window")
    p.add_argument("--max-log-events", type=int, default=5000)
    p.add_argument("--skip-logs", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("inventory")
    pr = sub.add_parser("probe")
    pr.add_argument("--fact", default="My test office for the memory audit is room Z-999 in the Probe Building.")
    pr.add_argument("--question", action="append", dest="questions",
                    help="repeatable; default: three phrasings from indirect to verbatim")
    pr.add_argument("--expect", default="Z-999")
    pr.add_argument("--wait-seconds", type=int, default=600)
    pr.add_argument("--keep", action="store_true", help="leave the probe records and events in place")
    ca = sub.add_parser("calibrate")
    ca.add_argument("--wait-seconds", type=int, default=900, help="give up waiting for extraction after this")
    ca.add_argument("--settle-seconds", type=int, default=90,
                    help="extraction counts as done once the record count is stable this long")
    ca.add_argument("--late-sweeps", type=int, default=5, help="max 60 s cleanup sweeps for late records")
    ca.add_argument("--keep", action="store_true", help="leave the synthetic records and events in place")
    ca.add_argument("--reanalyze", action="store_true",
                    help="recompute the summary from --out/raw/calibrate_hits.jsonl; no AWS calls")
    cl = sub.add_parser("cleanup")
    cl.add_argument("--cleanup-actor", required=True)
    args = p.parse_args(argv)
    if args.cmd == "probe" and not args.questions:
        args.questions = [
            "Which room should I go to for the memory audit?",
            "Where is my test office for the memory audit?",
            args.fact,
        ]

    audit = Audit(args)
    if args.cmd in (None, "inventory"):
        audit.run_inventory()
    elif args.cmd == "probe":
        audit.run_probe()
    elif args.cmd == "calibrate":
        audit.run_calibrate()
    elif args.cmd == "cleanup":
        audit.run_cleanup(args.cleanup_actor)
    (audit.out / "summary.json").write_text(json.dumps(audit.summary, indent=2, default=_json_default))
    print(json.dumps(audit.summary, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())

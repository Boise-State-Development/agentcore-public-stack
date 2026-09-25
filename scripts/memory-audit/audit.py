"""AgentCore Memory baseline audit (Shared Projects spec §1, Phase 0.1).

Answers one question with evidence: does long-term memory work in a deployed
environment? Read-only by default. Only ``probe`` and ``cleanup`` write, and
both touch nothing but a synthetic ``memory-audit-probe-*`` actor.

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
RETRIEVAL_RELEVANCE = 0.5

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-([0-9a-f])[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PREVIEW_RE = re.compile(r"^preview[-_]", re.I)

LOG_PATTERNS = {
    "discovery_failed": '"No memory strategies found"',
    "retrieval_hits": '"customer context items"',
    "retrieval_failed": '"memory retrieval failed"',
    "retrieval_throttled": '"memory retrieval throttled"',
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
    elif args.cmd == "cleanup":
        audit.run_cleanup(args.cleanup_actor)
    (audit.out / "summary.json").write_text(json.dumps(audit.summary, indent=2, default=_json_default))
    print(json.dumps(audit.summary, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    sys.exit(main())

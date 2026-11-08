"""Publishing quality runs into the release ledger (docs/specs/release-ledger.md §5.2, PR-5).

``eval_harness.py score --publish`` writes one ``LEDGER#EVAL`` row per eval run
and ``smoke_turns.py --publish`` one ``LEDGER#SMOKE`` row per smoke run, into
the deployment's ``system-cost-rollup`` table beside the per-call ledger rows.
The admin quality route and the readout (``apis.shared.costs.ledger_reader``)
read them back by ``(appVersion, build)``.

Four rules, each a guard of §8:

* **Keyed by the build that served the turns.** That is the AgentCore Runtime's
  build, never app-api's: an eval run's ``meta.json`` top-level ``build`` comes
  from app-api's ``/health`` and names the wrong image. The served build is
  read from the run's own ``C#`` rows, which the Runtime stamps (D1), so a
  deploy in the middle of a run is visible rather than guessed; the Runtime's
  image tag from SSM is the fallback when no cost row can be read. A row that
  can name neither is not published.
* **Idempotent.** A row is one ``PutItem`` keyed by the run's ``startedAt``, so
  publishing the same run twice writes the same item twice. Nothing is
  ``ADD``ed: an ``ADD`` replayed is a double count.
* **Content-free (D7).** A row carries ids (version, build, model, family, row
  name, evaluator, commit, target) and numbers. No prompt, answer, failure
  text, session id or user id; :func:`assert_content_free` refuses a row with
  any string that is not id-shaped or that contains one of the run's own
  session or user ids, before anything is written.
* **Best-effort.** A failed write is printed and logged and never changes the
  calling script's exit code: the run's result is its result whether or not
  the ledger heard about it.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EVAL_PK = "LEDGER#EVAL"
SMOKE_PK = "LEDGER#SMOKE"
ROLLUP_TABLE_ENV = "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME"
RUNTIME_SERVICE = "inference-api"

#: The shape every published string must have: the same pattern the ledger
#: reader's export filter keeps (``apis.shared.costs.ledger_reader``), so a
#: published row reads back whole rather than with fields silently dropped.
ID_TEXT = re.compile(r"^[A-Za-z0-9_.:#@/+\-]{0,160}$")

#: Where the build a row is keyed by came from, most trustworthy first.
BUILD_FROM_COST_ROWS = "cost_rows"
BUILD_FROM_IMAGE_TAG = "image_tag"

#: Top-level attributes each row may carry; :func:`assert_content_free` and
#: the tests hold the builders to these.
EVAL_ROW_FIELDS = frozenset({
    "PK", "SK", "runKind", "date", "startedAt", "publishedAt", "appVersion", "build", "buildSource",
    "otherBuildCalls", "gitCommit", "modelId", "k", "target", "families", "gateFailed", "judgeModel",
    "judgeMeans", "samples", "calls", "costUsd", "durationS",
})
SMOKE_ROW_FIELDS = frozenset({
    "PK", "SK", "runKind", "date", "startedAt", "publishedAt", "appVersion", "build", "buildSource",
    "otherBuildCalls", "gitCommit", "modelId", "target", "pass", "fail", "skip", "observed", "rows",
    "firstTokenMsMedian", "firstTokenTurns", "secondTurnCacheRead", "secondTurnCacheWrite", "calls",
    "costUsd", "durationS",
})


class PublishError(RuntimeError):
    """The row cannot be published as built (no build, or content in it)."""


# ---------------------------------------------------------------------------
# The served build
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServedBuild:
    """The ``(appVersion, build)`` a row is keyed by, and how it was found."""

    app_version: str
    build: str
    source: str
    #: Calls of the run that ran on some other build (a deploy mid-run); 0 normally.
    other_build_calls: int = 0


def served_build_from_calls(call_rows: Iterable[Mapping[str, Any]]) -> Optional[ServedBuild]:
    """The build most of the run's model calls ran on, from their ``C#`` stamps.

    ``None`` when no row carries both stamps (rows written before PR-1, or none
    readable). Ties break on the newer-sorting build so the answer is stable.
    """
    counts: Counter = Counter()
    for row in call_rows:
        version, build = row.get("appVersion"), row.get("build")
        if version and build:
            counts[(str(version), str(build))] += 1
    if not counts:
        return None
    (version, build), calls = max(counts.items(), key=lambda item: (item[1], item[0]))
    return ServedBuild(version, build, BUILD_FROM_COST_ROWS, sum(counts.values()) - calls)


def served_build(
    call_rows: Iterable[Mapping[str, Any]],
    *,
    image_tag: Optional[str],
    app_version: Optional[str],
) -> Optional[ServedBuild]:
    """The cost rows' build, else the Runtime's image tag labelled with ``app_version``.

    The image tag is ``APP_BUILD`` (``build-one.sh`` passes the content hash as
    both), so the fallback names the same build the cost rows would. Its
    version label comes from wherever the caller has one (app-api's
    ``/health``), which is right except across a release bump that one service
    has deployed and the other has not; the cost rows have no such gap.
    """
    from_calls = served_build_from_calls(call_rows)
    if from_calls is not None:
        return from_calls
    if image_tag and app_version:
        return ServedBuild(str(app_version), str(image_tag), BUILD_FROM_IMAGE_TAG)
    return None


def runtime_image_tag(image_tags: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The Runtime's tag from an eval run's ``meta.json`` ``imageTags``; app-api's is never it."""
    tag = (image_tags or {}).get(RUNTIME_SERVICE)
    return str(tag) if tag else None


def read_runtime_image_tag(prefix: str, region: str) -> Optional[str]:
    """``/<prefix>/inference-api/image-tag`` from SSM, the tag after the last ``:``. Best effort."""
    try:
        import boto3

        uri = boto3.client("ssm", region_name=region).get_parameter(
            Name=f"/{prefix}/{RUNTIME_SERVICE}/image-tag"
        )["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 — a fallback, never a failure
        logger.info("runtime image tag unavailable: %s", exc)
        return None
    return uri.rsplit(":", 1)[-1] or None


# ---------------------------------------------------------------------------
# Cost rows
# ---------------------------------------------------------------------------


def call_cost(row: Mapping[str, Any]) -> float:
    cost = row.get("cost")
    total = cost.get("total") if isinstance(cost, Mapping) else cost
    try:
        return float(total) if total is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def call_usage(row: Mapping[str, Any], name: str) -> Optional[int]:
    usage = row.get("tokenUsage")
    value = usage.get(name) if isinstance(usage, Mapping) else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def total_cost(call_rows: Iterable[Mapping[str, Any]]) -> float:
    return round(sum(call_cost(r) for r in call_rows), 8)


# ---------------------------------------------------------------------------
# Row builders (pure)
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_between(start: Optional[str], end: Optional[str]) -> Optional[float]:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(max(0.0, (b - a).total_seconds()), 1)


def _base_row(pk: str, kind: str, started_at: str, served: ServedBuild, published_at: Optional[str]) -> Dict[str, Any]:
    if not started_at or not ID_TEXT.match(str(started_at)) or len(str(started_at)) < 10:
        raise PublishError(f"run has no usable startedAt ({started_at!r}); it is the row's key")
    row: Dict[str, Any] = {
        "PK": pk,
        "SK": str(started_at),
        "runKind": kind,
        "date": str(started_at)[:10],
        "startedAt": str(started_at),
        "publishedAt": published_at or now_iso(),
        "appVersion": served.app_version,
        "build": served.build,
        "buildSource": served.source,
    }
    if served.other_build_calls:
        row["otherBuildCalls"] = served.other_build_calls
    return row


def _gate(comparison: Any) -> str:
    if getattr(comparison, "failed", False):
        return "fail"
    return "pass" if getattr(comparison, "gating", False) else "advisory"


def eval_ledger_row(
    meta: Mapping[str, Any],
    comparisons: Sequence[Any],
    *,
    served: ServedBuild,
    result_rows: Sequence[Mapping[str, Any]] = (),
    judge_comparisons: Sequence[Any] = (),
    judge_model: Optional[str] = None,
    call_rows: Sequence[Mapping[str, Any]] = (),
    published_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``LEDGER#EVAL`` row: §8's report table, with nothing from any answer.

    ``comparisons`` are ``apis.shared.evals.score.FamilyComparison`` and
    ``judge_comparisons`` ``apis.shared.evals.report.JudgeComparison``. A
    comparison's ``reason`` is prose and the flipped case ids are left out: the
    row says *that* a family moved, and ``score --explain`` on the run says why.
    """
    row = _base_row(EVAL_PK, "eval", str(meta.get("startedAt") or ""), served, published_at)
    row.update({
        "modelId": str(meta.get("model_id") or ""),
        "k": int(meta.get("k") or 0),
        "target": str(meta.get("target") or ""),
        "families": {
            c.family: {
                "n": c.n, "base": c.base_pass, "run": c.run_pass, "gained": c.gains, "lost": c.losses,
                "p": round(float(c.p_value), 6), "gate": _gate(c), "gating": bool(c.gating),
            }
            for c in comparisons
        },
        "gateFailed": any(getattr(c, "failed", False) for c in comparisons),
        "samples": len(result_rows),
    })
    if meta.get("gitCommit"):
        row["gitCommit"] = str(meta["gitCommit"])
    if judge_comparisons:
        means: Dict[str, Dict[str, Any]] = {}
        for j in judge_comparisons:
            means.setdefault(j.family, {})[j.evaluator] = {
                "n": j.n, "base": round(float(j.base_mean), 4), "run": round(float(j.run_mean), 4),
            }
        row["judgeMeans"] = means
        if judge_model:
            row["judgeModel"] = str(judge_model)
    if call_rows:
        row["calls"] = len(call_rows)
        row["costUsd"] = total_cost(call_rows)
    finished = max((str(r.get("finished_at")) for r in result_rows if r.get("finished_at")), default=None)
    duration = _seconds_between(row["startedAt"], finished)
    if duration is not None:
        row["durationS"] = duration
    return row


#: Smoke statuses as ``smoke_turns.RowResult`` writes them, and their counters.
SMOKE_STATUSES = (("PASS", "pass"), ("FAIL", "fail"), ("SKIP", "skip"), ("OBSERVED", "observed"))


def smoke_ledger_row(
    *,
    started_at: str,
    finished_at: Optional[str],
    target: str,
    results: Sequence[Mapping[str, Any]],
    served: ServedBuild,
    first_token_ms: Sequence[float] = (),
    model_id: Optional[str] = None,
    git_commit: Optional[str] = None,
    shared_session_calls: Sequence[Mapping[str, Any]] = (),
    call_rows: Sequence[Mapping[str, Any]] = (),
    published_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``LEDGER#SMOKE`` row: counts per status, each row's status, first token, the cache row.

    ``results`` are ``{row, status, data}`` dicts (``smoke_turns.RowResult``).
    Only a row's name and status are kept; its detail and warnings are prose.
    The second turn's cache read and write come from that row's data when the
    run could read the admin anatomy, else from the shared session's last
    ``C#`` row, which is the same number read straight from the table.
    """
    row = _base_row(SMOKE_PK, "smoke", started_at, served, published_at)
    statuses = Counter(str(r.get("status") or "") for r in results)
    for status, counter in SMOKE_STATUSES:
        row[counter] = statuses.get(status, 0)
    row["rows"] = {str(r.get("row")): str(r.get("status")) for r in results if r.get("row")}
    row["target"] = str(target)
    if model_id:
        row["modelId"] = str(model_id)
    if git_commit:
        row["gitCommit"] = str(git_commit)
    if first_token_ms:
        row["firstTokenMsMedian"] = int(round(statistics.median(first_token_ms)))
        row["firstTokenTurns"] = len(first_token_ms)

    cache = next((r.get("data") or {} for r in results if r.get("row") == "second_turn_cache"), {})
    read, write = cache.get("cacheRead"), cache.get("cacheWrite")
    if (read is None or write is None) and len(shared_session_calls) >= 2:
        last = sorted(shared_session_calls, key=lambda c: str(c.get("timestamp") or ""))[-1]
        read = call_usage(last, "cacheReadInputTokens") if read is None else read
        write = call_usage(last, "cacheWriteInputTokens") if write is None else write
    if read is not None:
        row["secondTurnCacheRead"] = int(read)
    if write is not None:
        row["secondTurnCacheWrite"] = int(write)

    if call_rows:
        row["calls"] = len(call_rows)
        row["costUsd"] = total_cost(call_rows)
    duration = _seconds_between(started_at, finished_at)
    if duration is not None:
        row["durationS"] = duration
    return row


# ---------------------------------------------------------------------------
# Guard and write
# ---------------------------------------------------------------------------


def _strings(value: Any, path: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for name, inner in value.items():
            yield f"{path}.{name} (key)", str(name)
            yield from _strings(inner, f"{path}.{name}")
    elif isinstance(value, (list, tuple, set)):
        for i, inner in enumerate(value):
            yield from _strings(inner, f"{path}[{i}]")


def assert_content_free(row: Mapping[str, Any], allowed: Iterable[str], forbidden: Iterable[str] = ()) -> None:
    """Raise :class:`PublishError` unless ``row`` holds only allowed, id-shaped fields.

    ``forbidden`` is the run's own session and user ids: none may appear in
    any string of the row, even inside an otherwise id-shaped one.
    """
    extra = sorted(set(row) - set(allowed))
    if extra:
        raise PublishError(f"unexpected attribute(s) on a ledger row: {extra}")
    needles = [f for f in dict.fromkeys(str(f) for f in forbidden if f) if len(f) >= 6]
    for path, text in _strings(row):
        if not ID_TEXT.match(text):
            raise PublishError(f"{path or 'row'} is not id-shaped; ledger rows carry ids and numbers only")
        for needle in needles:
            if needle in text:
                raise PublishError(f"{path or 'row'} contains one of the run's own session or user ids")


def to_item(row: Mapping[str, Any]) -> Dict[str, Any]:
    """JSON-shaped data as a DynamoDB item: floats become ``Decimal``, ``None`` is dropped."""
    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): clean(v) for k, v in value.items() if v is not None}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value if v is not None]
        return value

    return json.loads(json.dumps(clean(row)), parse_float=Decimal)


def rollup_table_name(prefix: str) -> str:
    """``{prefix}-system-cost-rollup``, from the same manifest the Runtime hydrates."""
    from apis.shared.config.runtime_environment import derived_environment_manifest

    entry = next(v for v in derived_environment_manifest() if v.name == ROLLUP_TABLE_ENV)
    name = entry.derive(prefix, None)
    if not name:
        raise PublishError(f"cannot derive {ROLLUP_TABLE_ENV} for prefix {prefix!r}")
    return name


def put_ledger_row(row: Mapping[str, Any], *, table: Any = None, prefix: Optional[str] = None,
                   region: Optional[str] = None) -> bool:
    """Write ``row`` with one ``PutItem``; ``True`` on success. Never raises.

    The same row written twice is the same item, which is what makes a re-run
    of ``score --publish`` on one run directory safe.
    """
    try:
        if table is None:
            if not prefix:
                raise PublishError("no prefix to derive the rollup table from")
            import boto3

            table = boto3.resource("dynamodb", region_name=region).Table(rollup_table_name(prefix))
        table.put_item(Item=to_item(row))
    except Exception as exc:  # noqa: BLE001 — best-effort by contract (§8 Failure)
        logger.warning("ledger publish failed for %s %s: %s", row.get("PK"), row.get("SK"), exc)
        return False
    return True


def describe(row: Mapping[str, Any]) -> str:
    """One line for the script's output: what was keyed to what."""
    return (
        f"{row.get('PK')} {row.get('SK')} → {row.get('appVersion')}#{row.get('build')} "
        f"(build from {row.get('buildSource')}"
        + (f", {row['otherBuildCalls']} call(s) on another build" if row.get("otherBuildCalls") else "")
        + ")"
    )


def session_ids(rows: Iterable[Mapping[str, Any]]) -> List[str]:
    return list(dict.fromkeys(str(r.get("session_id")) for r in rows if r.get("session_id")))

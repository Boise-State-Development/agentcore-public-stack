"""Ask each planted question on each arm's probe-time history (Bedrock Converse).

Every question for one (variant, arm) shares the same history, so a cachePoint
at the end of the history turns all but the first call into a cache read —
the same prompt-cache discipline the product runs under. Calls are ordered so
the first call per history writes the cache before any other reads it.

The system prompt here is deliberately small: it is the history the arms
differ on, not the system prompt. The model is asked for the value only, which
keeps exact-match scoring honest and output spend near zero.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .corpus import TOOL_SPECS, Plant
from .score import is_correct

ASK_SYSTEM_PROMPT = (
    "You are the assistant in the ongoing conversation above, helping the user with a grant proposal. "
    "Answer the user's latest question from what was said in this conversation. "
    "Reply with only the requested value — no explanation. "
    "If the conversation does not tell you, reply exactly: UNKNOWN. "
    "Do not call any tools."
)

# Per-MTok input/output. Regional (us.*) rates — dev's SCP denies global.*.
# Source of truth is curated-models.ts; these only size the pre-flight estimate.
RATES: Dict[str, Tuple[float, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "us.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "us.anthropic.claude-sonnet-5": (2.20, 11.00),
    "us.amazon.nova-micro-v1:0": (0.035, 0.14),
}


@dataclass(frozen=True)
class Job:
    variant: int
    arm: str
    plant: Plant
    sample: int
    statement_retained: bool

    @property
    def key(self) -> str:
        return f"{self.arm}|{self.variant}|{self.plant.plant_id}|{self.sample}"


def build_request(
    history: Sequence[Dict[str, Any]],
    question: str,
    *,
    model_id: str,
    temperature: Optional[float],
    max_tokens: int = 64,
) -> Dict[str, Any]:
    """Converse kwargs: history + cachePoint, then the question. Never mutates ``history``."""
    messages = copy.deepcopy(list(history))
    if messages:
        messages[-1]["content"] = list(messages[-1]["content"]) + [{"cachePoint": {"type": "default"}}]
    messages.append({"role": "user", "content": [{"text": question}]})
    inference: Dict[str, Any] = {"maxTokens": max_tokens}
    if temperature is not None:
        inference["temperature"] = temperature
    return {
        "modelId": model_id,
        "system": [{"text": ASK_SYSTEM_PROMPT}],
        "messages": messages,
        # Required by Converse whenever the history carries toolUse/toolResult.
        "toolConfig": {"tools": TOOL_SPECS},
        "inferenceConfig": inference,
    }


def estimate_cost(
    history_tokens_by_group: Dict[Tuple[int, str], int],
    calls_per_group: int,
    model_id: str,
    *,
    output_tokens_per_call: int = 16,
) -> Dict[str, Any]:
    """First call per group writes the history (1.25x); the rest read it (0.1x)."""
    rate_in, rate_out = RATES.get(model_id, (None, None))
    if rate_in is None:
        return {"modelId": model_id, "usd": None, "note": "no rate on file — pass a known model or price it by hand"}
    base_tokens = sum(history_tokens_by_group.values())
    write = base_tokens * 1.25
    read = base_tokens * 0.10 * max(0, calls_per_group - 1)
    calls = len(history_tokens_by_group) * calls_per_group
    usd = (write + read) * rate_in / 1e6 + calls * output_tokens_per_call * rate_out / 1e6
    return {
        "modelId": model_id,
        "groups": len(history_tokens_by_group),
        "calls": calls,
        "historyTokens": base_tokens,
        "usd": round(usd, 2),
        # A cache miss on every call is the ceiling if caching does not take.
        "usdIfCacheNeverHits": round((base_tokens * calls_per_group) * rate_in / 1e6, 2),
    }


def plan_jobs(
    runs: Iterable[Dict[str, Any]],
    plants_by_variant: Dict[int, List[Plant]],
    availability: Dict[Tuple[int, str, str], Dict[str, Any]],
    k: int,
) -> Iterator[Tuple[Dict[str, Any], List[Job]]]:
    for run in runs:
        variant, arm = int(run["variant"]), run["arm"]
        jobs = [
            Job(variant, arm, plant, sample,
                bool(availability.get((variant, arm, plant.plant_id), {}).get("statementRetained")))
            for sample in range(k)
            for plant in plants_by_variant[variant]
        ]
        yield run, jobs


def _answer_text(response: Dict[str, Any]) -> str:
    content = (response.get("output", {}).get("message", {}) or {}).get("content", []) or []
    return " ".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)).strip()


def _ask_one(
    client: Any,
    history: Sequence[Dict[str, Any]],
    job: Job,
    *,
    model_id: str,
    temperature: Optional[float],
    retries: int,
) -> Dict[str, Any]:
    request = build_request(history, job.plant.question, model_id=model_id, temperature=temperature)
    started = time.monotonic()
    attempt = 0
    while True:
        try:
            response = client.converse(**request)
            break
        except Exception as exc:  # noqa: BLE001 - throttles are expected at volume
            if attempt >= retries or "Throttl" not in type(exc).__name__ + str(exc):
                raise
            time.sleep(2 ** attempt)
            attempt += 1
    answer = _answer_text(response)
    usage = response.get("usage", {}) or {}
    return {
        "key": job.key,
        "variant": job.variant,
        "arm": job.arm,
        "plantId": job.plant.plant_id,
        "family": job.plant.family,
        "sample": job.sample,
        "statementRetained": job.statement_retained,
        "answer": answer,
        "correct": is_correct(answer, job.plant.expected, job.plant.forbidden),
        "stopReason": response.get("stopReason"),
        "inputTokens": usage.get("inputTokens"),
        "cacheReadInputTokens": usage.get("cacheReadInputTokens"),
        "cacheWriteInputTokens": usage.get("cacheWriteInputTokens"),
        "outputTokens": usage.get("outputTokens"),
        "latencyMs": int((time.monotonic() - started) * 1000),
        "modelId": model_id,
    }


def run_jobs(
    client: Any,
    groups: Iterable[Tuple[Dict[str, Any], List[Job]]],
    out_path: Path,
    *,
    model_id: str,
    temperature: Optional[float],
    done: Optional[Set[str]] = None,
    retries: int = 6,
    workers: int = 1,
) -> int:
    """Run every job not already in ``done``, appending one JSON line per call.

    Per history, the first call runs alone so it writes the cache; the rest
    then run ``workers`` at a time and read it.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    done = done or set()
    written = 0
    lock = Lock()
    with out_path.open("a", encoding="utf-8") as sink:
        def record(row: Dict[str, Any]) -> None:
            nonlocal written
            with lock:
                sink.write(json.dumps(row) + "\n")
                sink.flush()
                written += 1

        for run, jobs in groups:
            todo = [j for j in jobs if j.key not in done]
            if not todo:
                continue
            ask = lambda job: _ask_one(  # noqa: E731
                client, run["history"], job, model_id=model_id, temperature=temperature, retries=retries,
            )
            record(ask(todo[0]))
            if len(todo) > 1:
                with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                    for row in pool.map(ask, todo[1:]):
                        record(row)
    return written


def load_done(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            keys.add(json.loads(line)["key"])
    return keys

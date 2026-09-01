#!/usr/bin/env python3
"""Query BOTH engines on the same corpus and print the results side by side.

    cd backend
    uv run python ../scripts/local-dev/kb-compare-engines.py ast-1a90784a7f18 \
        "what are the AI degree requirements"

    # several queries at once
    uv run python ../scripts/local-dev/kb-compare-engines.py ast-1a90784a7f18 -f queries.txt

WHY THIS WORKS WITHOUT A SECOND ASSISTANT
A promoted knowledge base sits in `retain` for 30 days, during which the legacy
S3 Vectors index is left fully intact — promotion moves no data, it flips one
attribute. So for any assistant in `retain` both engines hold the same corpus and
can answer the same query. That is a genuine A/B on identical documents, which
two separate assistants could only approximate.

Read-only: issues retrievals, writes nothing.

WHAT THE NUMBERS MEAN
Both engines are asked for the same `top_k` and both report `relevance`, higher
is better (`s3vectors_backend` converts its native cosine distance by exact
negation, so legacy relevance is negative — that is expected, and only the
ORDER and the SPREAD are comparable across engines, never the absolute values).

`spread` is what the demo is about. Only MAX_CONTEXT_CHARS (2,000) of retrieved
text reaches the model, so when every chunk scores nearly the same the cap
truncates chunks that were barely distinguishable and the best one can be lost.
Legacy has no reranking; managed reranks. Watch the spread, not the top score.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / "backend" / "src" / ".env", override=True)

TOP_K = 5
CONTEXT_CAP = 2000  # rag_service.MAX_CONTEXT_CHARS


def _bar(value: float, lo: float, hi: float, width: int = 22) -> str:
    if hi <= lo:
        return "─" * width
    filled = int(round((value - lo) / (hi - lo) * width))
    return "█" * max(1, filled) + "·" * (width - max(1, filled))


def _spread(scores) -> str:
    real = [s for s in scores if s is not None]
    if len(real) < 2:
        return "—"
    return f"{max(real) - min(real):.4f}"


def _render(engine: str, chunks, elapsed_ms: float) -> None:
    print(f"\n  ── {engine}  ({elapsed_ms:.0f} ms, {len(chunks)} chunks)")
    if not chunks:
        print("     (nothing returned)")
        return
    scores = [c.relevance for c in chunks if c.relevance is not None]
    lo, hi = (min(scores), max(scores)) if scores else (0.0, 1.0)

    running = 0
    for rank, chunk in enumerate(chunks, 1):
        score = chunk.relevance
        # Does this chunk survive the 2,000-char cap that the model actually sees?
        running += len(chunk.text)
        fits = "in " if running <= CONTEXT_CAP else "CUT"
        bar = _bar(score, lo, hi) if score is not None else "?" * 22
        text = " ".join(chunk.text.split())[:58]
        print(
            f"     {rank}. {score if score is None else f'{score:+.4f}'}  {bar}  "
            f"[{fits}]  {chunk.document_id[:18]:18} {text!r}"
        )
    print(f"     spread(best−worst) = {_spread([c.relevance for c in chunks])}")


async def compare_one(assistant_id: str, query: str) -> None:
    import time

    from apis.shared.kb_backend.managed_backend import ManagedKbBackend
    from apis.shared.kb_backend.s3vectors_backend import S3VectorsBackend

    print("\n" + "=" * 100)
    print(f"QUERY: {query!r}")
    print("=" * 100)

    for engine, backend in (
        ("LEGACY  (S3 Vectors, no reranking)", S3VectorsBackend()),
        ("MANAGED (Bedrock, MANAGED reranking)", ManagedKbBackend()),
    ):
        started = time.perf_counter()
        try:
            chunks = await backend.search(assistant_id, query, TOP_K)
        except Exception as exc:  # noqa: BLE001 — diagnostic harness
            print(f"\n  ── {engine}\n     RAISED {type(exc).__name__}: {str(exc)[:200]}")
            continue
        _render(engine, chunks, (time.perf_counter() - started) * 1000.0)


async def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return 2
    assistant_id = args[0]

    if args[1] in ("-f", "--file"):
        queries = [
            line.strip()
            for line in Path(args[2]).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        queries = [" ".join(args[1:])]

    from apis.shared.kb_backend import records as r

    record = r.get_kb_record(assistant_id, assistant_id) or {}
    state = record.get("migrationState")
    print(f"assistant       : {assistant_id}")
    print(f"retrievalEngine : {record.get('retrievalEngine', 'absent ⇒ legacy')}")
    print(f"migrationState  : {state}")
    if state != r.RETAIN:
        print(
            "\n⚠️  This assistant is not in `retain`. The comparison is only a true\n"
            "    A/B while BOTH indexes hold the corpus. Outside `retain` the legacy\n"
            "    side may be empty or stale."
        )

    for query in queries:
        await compare_one(assistant_id, query)

    print(
        f"\n\nReading this: only the first {CONTEXT_CAP} characters of retrieved text\n"
        "reach the model, marked [in ] / [CUT] above. Compare the SPREAD between\n"
        "engines — a flat distribution means the cap is choosing arbitrarily among\n"
        "chunks the retriever could not tell apart. Absolute scores are NOT\n"
        "comparable across engines (legacy is a negated cosine distance); order and\n"
        "spread are.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

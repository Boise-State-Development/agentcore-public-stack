#!/usr/bin/env python3
"""Answer-quality A/B for the managed context cap — the measurement task 16.1 needs.

    cd backend
    uv run python ../scripts/local-dev/kb-cap-benchmark.py ast-1a90784a7f18 \
        "is CS434 a required course or an elective?"

    # a scorable question set, one query per line (blank / #-comment lines skipped)
    uv run python ../scripts/local-dev/kb-cap-benchmark.py ast-1a90784a7f18 -f queries.txt

WHAT THIS ANSWERS, AND WHY kb-compare-engines.py IS NOT ENOUGH
`kb-compare-engines.py` shows which chunks clear the 2,000-char cap. It measures
the RETRIEVER. It cannot show the consequence HANDOFF §5.40 is actually about:
the model gave a *materially wrong answer* (a Major-Core course called an
elective) even though retrieval ranked the right chunk first, because the cap
discarded the neighbours that carried the section header. Answer quality is not
retrieval quality — §5.40's own "earlier demo advice was wrong" note is exactly
this mistake. So this harness runs the whole path: retrieve → augment → MODEL,
and prints the answer at each cap side by side.

THE EXPERIMENT IS CONTROLLED
Everything is held identical across the two arms except ``max_context_length``:
same assistant, same retrieved chunks (retrieval runs once), same system prompt
(the assistant's own instructions), same model, temperature 0. The only thing
that changes is how many of the retrieved chunks survive the cap and reach the
model. It reuses the REAL production ``rag_service.augment_prompt_with_context``,
so a passing result here is a statement about the code that ships, not a proxy.

DEFAULT CAPS: 2000 (today) vs 8000 (the evaluation's §13.6 sizing — the point at
which all five managed chunks fit, at ~966 extra input tokens/turn). Override
with --caps.

READ + INFERENCE ONLY: issues retrievals, one DynamoDB get for the assistant's
instructions, and Bedrock Converse calls. Writes nothing, mutates nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / "backend" / "src" / ".env", override=True)

TOP_K = 5
DEFAULT_CAPS = (2000, 8000)
# Held constant across both arms — the cap is the only variable. Sonnet is a
# capable default; override with --model to match a specific assistant.
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
MAX_ANSWER_TOKENS = 1024


def _fits_count(chunk_texts: list[str], cap: int) -> int:
    """How many whole chunks clear the cap, mirroring augment's accumulation."""
    running = 0
    fit = 0
    for i, text in enumerate(chunk_texts, 1):
        block = f"[Context {i}]\n{text.strip()}\n"
        if running + len(block) > cap:
            break
        running += len(block)
        fit += 1
    return fit


def _assistant_instructions(assistant_id: str) -> str:
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        print("  ⚠️  DYNAMODB_ASSISTANTS_TABLE_NAME not set; using an empty system prompt")
        return ""
    region = os.environ.get("AWS_REGION", "us-west-2")
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    item = table.get_item(Key={"PK": f"AST#{assistant_id}", "SK": "METADATA"}).get("Item") or {}
    return item.get("instructions") or ""


def _answer(model_id: str, system_prompt: str, augmented_message: str) -> str:
    import boto3

    region = os.environ.get("AWS_REGION", "us-west-2")
    client = boto3.client("bedrock-runtime", region_name=region)
    kwargs = {
        "modelId": model_id,
        "messages": [{"role": "user", "content": [{"text": augmented_message}]}],
        "inferenceConfig": {"maxTokens": MAX_ANSWER_TOKENS, "temperature": 0.0},
    }
    if system_prompt.strip():
        kwargs["system"] = [{"text": system_prompt}]
    resp = client.converse(**kwargs)
    parts = resp["output"]["message"]["content"]
    return "".join(p.get("text", "") for p in parts).strip()


async def run(assistant_id: str, queries: list[str], caps: tuple[int, ...], model_id: str,
              instructions_file: str | None = None) -> int:
    from apis.shared.assistants.rag_service import augment_prompt_with_context
    from apis.shared.kb_backend.resolver import load_record, resolve_backend, resolve_engine_for

    record = load_record(assistant_id)
    engine = resolve_engine_for(assistant_id, record=record)
    print(f"assistant       : {assistant_id}")
    print(f"retrievalEngine : {engine}")
    print(f"model           : {model_id}")
    print(f"caps            : {', '.join(str(c) for c in caps)}")
    if engine != "managed":
        print(
            "\n⚠️  This assistant is not on the managed engine, so the cap A/B is not\n"
            "    meaningful here — legacy chunks are small and the cap rarely bites.\n"
        )

    backend = resolve_backend(assistant_id, record=record)
    if instructions_file:
        instructions = Path(instructions_file).read_text()
    else:
        instructions = _assistant_instructions(assistant_id)

    for query in queries:
        print("\n" + "=" * 100)
        print(f"QUERY: {query!r}")
        print("=" * 100)

        chunks = await backend.search(assistant_id, query, TOP_K)
        texts = [c.text for c in chunks]
        if not chunks:
            print("  (retrieval returned nothing — skipping)")
            continue

        sizes = ", ".join(str(len(t)) for t in texts)
        print(f"  retrieved {len(chunks)} chunks, sizes (chars): {sizes}")

        for cap in caps:
            fit = _fits_count(texts, cap)
            chunk_dicts = [{"text": t} for t in texts]
            augmented = augment_prompt_with_context(
                user_message=query, context_chunks=chunk_dicts, max_context_length=cap
            )
            try:
                answer = _answer(model_id, instructions, augmented)
            except Exception as exc:  # noqa: BLE001 — diagnostic harness
                answer = f"[model call RAISED {type(exc).__name__}: {str(exc)[:200]}]"
            print(f"\n  ── cap={cap}  ({fit}/{len(chunks)} chunks reach the model)")
            for line in answer.splitlines() or [""]:
                print(f"     {line}")

    print(
        "\n\nReading this: the two answers differ ONLY because a different number of the\n"
        "same retrieved chunks reached the model. If cap=2000 is wrong and cap=8000 is\n"
        "right, that is the §5.40 defect and its fix, measured end to end. Score each\n"
        "answer against ground truth you hold (this harness does not judge for you).\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("assistant_id")
    parser.add_argument("query", nargs="*", help="a single query (words joined); or use -f")
    parser.add_argument("-f", "--file", help="file of queries, one per line")
    parser.add_argument("--caps", default=",".join(str(c) for c in DEFAULT_CAPS), help="comma-separated char caps")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Bedrock model id")
    parser.add_argument("--instructions-file", default=None, help="file whose contents become the system prompt (else read the assistant's METADATA row)")
    args = parser.parse_args()

    if args.file:
        queries = [
            ln.strip() for ln in Path(args.file).read_text().splitlines() if ln.strip() and not ln.startswith("#")
        ]
    elif args.query:
        queries = [" ".join(args.query)]
    else:
        parser.error("give a query or -f FILE")

    caps = tuple(int(c) for c in args.caps.split(",") if c.strip())
    return asyncio.run(run(args.assistant_id, queries, caps, args.model, args.instructions_file))


if __name__ == "__main__":
    raise SystemExit(main())

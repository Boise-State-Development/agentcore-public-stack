"""Stand-in for AgentCore LTM ``ConversationSummary`` records (the ``records`` summary mode).

In production a cut's summary comes from the session's LTM summary records when
the summarization strategy has produced any (``summarySource: "ltm"``), and from
the first-line fallback otherwise. Those records are written asynchronously by
AgentCore over the whole session; we cannot call that service offline, so this
approximates it: one record per ``chunk_turns`` turns, written by a model with
a summarization prompt, available one turn after its chunk ends (AgentCore's
extraction lags the conversation).

⚠️ An approximation. The records' wording, length and lag are ours, not
AgentCore's, so a ``records``-mode result measures "a cut with an LTM-shaped
summary", not the production summary byte for byte. Record it next to any
number it produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List

from .corpus import Transcript, message_text

RECORD_PROMPT = (
    "Summarize this portion of a conversation between a user and an AI assistant working on a grant proposal. "
    "Keep standing instructions, decisions, exact identifiers, numbers, dates and names verbatim. "
    "Note anything that changed and what it changed to. Plain bullets, under {max_words} words."
)


def build_records(
    client: Any,
    transcript: Transcript,
    *,
    model_id: str,
    chunk_turns: int = 8,
    max_words: int = 250,
) -> List[Dict[str, Any]]:
    """One record per ``chunk_turns`` turns. Prod's records total a median of
    ~20k tokens at cut time (2026-09-25 readout), which is what makes
    ``bound_summary`` compress; ``chunk_turns=2, max_words=600`` reproduces
    that on the default corpus, while the defaults stay under the budget."""
    records = []
    for start in range(0, len(transcript.turns), chunk_turns):
        end = min(start + chunk_turns, len(transcript.turns)) - 1
        text = "\n\n".join(
            f"[{m['role']}] {message_text(m)}"
            for turn in transcript.turns[start:end + 1]
            for m in turn
        )
        response = client.converse(
            modelId=model_id,
            system=[{"text": RECORD_PROMPT.replace("{max_words}", str(max_words))}],
            messages=[{"role": "user", "content": [{"text": text}]}],
            inferenceConfig={"maxTokens": max(600, int(max_words * 2)), "temperature": 0.1},
        )
        content = response["output"]["message"]["content"]
        summary = " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
        records.append({"startTurn": start, "endTurn": end, "text": summary})
    return records


def records_lookup(path: Path) -> Callable[[int], Callable[[int], List[str]]]:
    """``lookup(variant)(turn)`` → the records that exist when turn ``turn`` completes."""
    data: Dict[str, List[Dict[str, Any]]] = json.loads(path.read_text(encoding="utf-8"))

    def for_variant(variant: int) -> Callable[[int], List[str]]:
        rows = data.get(str(variant))
        if rows is None:
            raise KeyError(f"no records for variant {variant} in {path}")
        return lambda turn: [r["text"] for r in rows if r["endTurn"] < turn]

    return for_variant

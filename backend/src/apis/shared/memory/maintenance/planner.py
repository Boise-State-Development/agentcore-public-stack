"""The maintenance planner: one model call per file (Shared Projects §4.6 step 3).

The model sees one file's items, numbered, with their dates and pins, and
answers through a forced tool call, so its reply is structured data rather
than prose to parse. It proposes only; ``verify.py`` decides what survives.

The call runs in the maintenance worker, never on a turn, and is bounded: one
call per file, a fixed output ceiling, and a read timeout well inside the
worker's 15 minutes. Usage comes back with the plan so the run can meter it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from ..format import Item
from ..models import ItemProvenance
from .verify import PlannedChange

logger = logging.getLogger(__name__)

TOOL_NAME = "propose_changes"
MAX_OUTPUT_TOKENS = 4_000
_READ_TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = """You maintain one file of a team's shared memory. The file is a list of items, each one fact, decision or note the team saved. Over time items repeat each other, get replaced by newer ones, or describe things that are over. You propose changes that keep the file accurate and short. A person reviews every change before anything happens.

You can propose three kinds of change:

- merge: two or more items that state the same fact. Write one item that says everything they say, using only their words, names, numbers and dates. Keep every number, date, link ([[name]]), URL, email and `code` from the sources, and keep any reason they give ("because ...").
- supersede: a newer item replaces an older one it contradicts or updates. Give [old, new]. Use it only when the newer item clearly makes the older one wrong or out of date.
- prune: an item about something with a date that has passed and nothing lasting in it (a meeting that happened, a deadline that is over). Its reason is "expired". Never prune a decision, a rule, a convention or anything still true.

Rules:
- Never invent anything. If you are not sure a change is right, leave the items alone.
- Never change a pinned item's meaning, and never remove one.
- An item may appear in at most one change.
- Items are numbered; refer to them by number.
- Propose nothing when nothing needs to change. That is a good answer.
- Give each change a short "why" a teammate would understand."""

TOOL_SPEC = {
    "toolSpec": {
        "name": TOOL_NAME,
        "description": "Propose changes to the memory file. Call it once, with an empty list if nothing should change.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["merge", "supersede", "prune"]},
                                "items": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "Item numbers. merge: two or more. supersede: [old, new]. prune: one.",
                                },
                                "text": {"type": "string", "description": "merge only: the one item that replaces them."},
                                "reason": {"type": "string", "enum": ["expired"], "description": "prune only."},
                                "why": {"type": "string", "description": "One short sentence for the reviewer."},
                            },
                            "required": ["type", "items", "why"],
                        },
                    }
                },
                "required": ["changes"],
            }
        },
    }
}


@dataclass
class Plan:
    changes: List[PlannedChange] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class PlannerError(RuntimeError):
    """The model call failed or answered without the tool. The file is reported as failed.

    Carries whatever usage the call reported, so a call that was billed is still metered.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def render_file_for_planner(
    slug: str,
    description: str,
    items: Sequence[Item],
    *,
    pinned: Sequence[str] = (),
    provenance: Optional[Dict[str, ItemProvenance]] = None,
    today: date,
) -> str:
    """The user message: the file's items, numbered, each with its date and pin."""
    prov = provenance or {}
    pins = set(pinned)
    lines = [f"Today is {today.isoformat()}.", f"File: {slug}"]
    if description:
        lines.append(f"About: {description}")
    lines.append("")
    for number, item in enumerate(items, start=1):
        p = prov.get(item.anchor or "")
        written = (p.updated_at or p.added_at) if p else ""
        tags = []
        if written:
            tags.append(f"written {written[:10]}")
        if (item.anchor or "") in pins:
            tags.append("pinned")
        suffix = f" ({', '.join(tags)})" if tags else ""
        body = item.text.replace("\n", "\n    ")
        lines.append(f"[{number}]{suffix} {body}")
    return "\n".join(lines)


def _client(region: Optional[str]) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            read_timeout=_READ_TIMEOUT_SECONDS,
            connect_timeout=10,
            retries={"total_max_attempts": 4, "mode": "adaptive"},
            user_agent_extra="memory-maintenance",
        ),
    )


def _parse_changes(tool_input: Any) -> List[PlannedChange]:
    raw = tool_input.get("changes") if isinstance(tool_input, dict) else None
    if not isinstance(raw, list):
        raise PlannerError("The planner's answer had no list of changes.")
    changes: List[PlannedChange] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ids = entry.get("items")
        ids = tuple(int(i) for i in ids if isinstance(i, (int, float)) or (isinstance(i, str) and i.isdigit())) if isinstance(ids, list) else ()
        changes.append(
            PlannedChange(
                type=str(entry.get("type") or ""),
                ids=ids,
                text=entry.get("text") if isinstance(entry.get("text"), str) else None,
                reason=entry.get("reason") if isinstance(entry.get("reason"), str) else None,
                why=str(entry.get("why") or ""),
            )
        )
    return changes


class Planner:
    """Calls the model once per file. ``client`` is injectable for tests."""

    def __init__(self, model_id: str, *, client: Any = None, region: Optional[str] = None) -> None:
        self.model_id = model_id
        # Built here, not lazily: the runner calls ``plan`` from several threads,
        # and a botocore client is safe to share once it exists.
        self.client = client or _client(region or os.environ.get("AWS_REGION"))

    def plan(
        self,
        slug: str,
        description: str,
        items: Sequence[Item],
        *,
        pinned: Sequence[str] = (),
        provenance: Optional[Dict[str, ItemProvenance]] = None,
        today: date,
    ) -> Plan:
        message = render_file_for_planner(
            slug, description, items, pinned=pinned, provenance=provenance, today=today
        )
        try:
            response = self.client.converse(
                modelId=self.model_id,
                system=[{"text": SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": message}]}],
                toolConfig={"tools": [TOOL_SPEC], "toolChoice": {"any": {}}},
                inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS},
            )
        except Exception as exc:  # noqa: BLE001 - reported per file; one bad call never fails the run
            raise PlannerError(f"The model call failed ({type(exc).__name__}).") from exc
        usage = response.get("usage") or {}
        plan = Plan(
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
        )
        billed = {"input_tokens": plan.input_tokens, "output_tokens": plan.output_tokens}
        if response.get("stopReason") == "max_tokens":
            raise PlannerError("The planner's answer was cut off.", **billed)
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        tool_use = next((c["toolUse"] for c in content if isinstance(c, dict) and "toolUse" in c), None)
        if tool_use is None or tool_use.get("name") != TOOL_NAME:
            raise PlannerError("The planner answered without proposing changes.", **billed)
        tool_input = tool_use.get("input")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except ValueError as exc:
                raise PlannerError("The planner's answer wasn't valid JSON.", **billed) from exc
        try:
            plan.changes = _parse_changes(tool_input)
        except PlannerError as exc:
            raise PlannerError(str(exc), **billed) from exc
        return plan

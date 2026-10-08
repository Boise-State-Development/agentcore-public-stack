"""The maintenance planner: one model call per file, two for a large one (Shared Projects §4.6 step 3).

The model sees one file's items, numbered, with their dates and pins, and
answers through a forced tool call, so its reply is structured data rather
than prose to parse. It proposes only; ``verify.py`` decides what survives.

The call runs in the maintenance worker, never on a turn, and is bounded: one
call per file, a fixed output ceiling, and a read timeout well inside the
worker's 15 minutes. Usage comes back with the plan so the run can meter it.

A file at the soft size threshold also gets a second call (2.6c) with its own
prompt and tool, :data:`SPLIT_PROMPT` and ``propose_splits``, which asks only
for groups of items to move into files of their own. Offering splits inside
the first call didn't work: Haiku 4.5 returned an empty ``splits`` list in 16
of 16 samples across three wordings, since that prompt rightly tells it to
leave items alone when unsure. A separate call also leaves the change prompt
byte-identical for every file, large ones included.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..format import Item
from ..models import ItemProvenance
from .verify import PlannedChange

logger = logging.getLogger(__name__)

TOOL_NAME = "propose_changes"
SPLIT_TOOL_NAME = "propose_splits"
MAX_OUTPUT_TOKENS = 4_000
_READ_TIMEOUT_SECONDS = 120

SYSTEM_PROMPT = """You maintain one file of a team's shared memory. The file is a list of items, each one fact, decision or note the team saved. Over time items repeat each other, get replaced by newer ones, or describe things that are over. You propose changes that keep the file accurate and short. A person reviews every change before anything happens.

You can propose three kinds of change:

- merge: two or more items that state the same fact. Write the one item that replaces them in "text"; a merge without text is discarded. It must say everything they say, using only their words, names, numbers and dates. Keep every number, date, link ([[name]]), URL, email and `code` from the sources, and keep any reason they give ("because ...").
- supersede: a newer item replaces an older one it contradicts or updates. Give [old, new]. Use it only when the newer item clearly makes the older one wrong or out of date.
- prune: an item about something with a date that has passed and nothing lasting in it (a meeting that happened, a deadline that is over). Its reason is "expired". Never prune a decision, a rule, a convention or anything still true.

Rules:
- Never invent anything. If you are not sure a change is right, leave the items alone.
- Never change a pinned item's meaning, and never remove one.
- An item may appear in at most one change.
- Items are numbered; refer to them by number in "items".
- Propose nothing when nothing needs to change. That is a good answer.
- Give each change a short "why" a teammate would understand. The reviewer never sees the numbers, so the why says what the items are about ("the owner changed from Marcus to Priya"), never "item 4"."""

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
                                "text": {
                                    "type": "string",
                                    "description": "For a merge: the one item that replaces them. For supersede and prune: an empty string.",
                                },
                                "reason": {"type": "string", "enum": ["expired"], "description": "prune only."},
                                "why": {"type": "string", "description": "One short sentence for the reviewer, about what the items say. No item numbers."},
                            },
                            # `text` is required for every change, though only a merge uses it:
                            # optional, Haiku 4.5 left it out of 2 in 8 merges on dev
                            # (2026-10-08), which the verifier then had to drop; required,
                            # 0 in 8.
                            "required": ["type", "items", "text", "why"],
                        },
                    }
                },
                "required": ["changes"],
            }
        },
    }
}


SPLIT_PROMPT = """You organise one file of a team's shared memory. The file is a list of items, each one fact, decision or note the team saved. It has grown close to its size limit. A large file is worse to use: a task reads a whole file at a time, so a task that needs one fact pays for all of them, and once the file reaches its limit nothing more can be saved to it.

Find groups of items that are about a sub-topic of their own and would read better as a separate file, and propose moving each group into a new file. Moved items keep their exact words; one line pointing to the new file takes their place, so nothing is lost.

For each group give:
- "items": the item numbers that move, five or more.
- "newSlug": a short name for the new file, lowercase words joined by hyphens (for example "canvas-rate-limits"). Not this file's name.
- "description": one line, under 160 characters, saying what the new file holds.
- "why": why these items belong together, for the reviewer. The reviewer never sees the numbers, so say what the items are about, never "items 4 to 9".

Rules:
- Only split along a real seam: a sub-topic someone would look for on its own, different in kind from the rest of the file. A list of the same kind of thing (fields, endpoints, terms, people) is one topic: never cut it into smaller lists.
- A group must be big enough to be worth a file of its own: at least 5 items, usually 10 or more.
- Leave the file's main topic in the file.
- Never move a pinned item.
- An item may be in at most one group.
- If the file is about one subject, propose no groups. That is a good answer."""

SPLIT_TOOL_SPEC = {
    "toolSpec": {
        "name": SPLIT_TOOL_NAME,
        "description": "Propose groups of items to move into new files. Call it once, with an empty list if none should move.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "splits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "Item numbers that move, five or more.",
                                },
                                "newSlug": {"type": "string", "description": "The new file's name: lowercase words joined by hyphens."},
                                "description": {"type": "string", "description": "One line: what the new file holds."},
                                "why": {"type": "string", "description": "One short sentence for the reviewer. No item numbers."},
                            },
                            # All required: Haiku leaves optional fields out (2.6a's merge text).
                            "required": ["items", "newSlug", "description", "why"],
                        },
                    }
                },
                "required": ["splits"],
            }
        },
    }
}


@dataclass(frozen=True)
class FileSize:
    """A file's size and the limit for one file, in tokens: given only for a file near that limit."""

    tokens: int
    cap: int


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
    size: Optional[FileSize] = None,
) -> str:
    """The user message: the file's items, numbered, each with its date and pin (and, for splits, its size)."""
    prov = provenance or {}
    pins = set(pinned)
    lines = [f"Today is {today.isoformat()}.", f"File: {slug}"]
    if description:
        lines.append(f"About: {description}")
    if size is not None:
        lines.append(f"Size: about {size.tokens:,} tokens; the limit for one file is {size.cap:,}.")
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


def _ids(entry: Dict[str, Any]) -> tuple:
    ids = entry.get("items")
    if not isinstance(ids, list):
        return ()
    return tuple(int(i) for i in ids if isinstance(i, (int, float)) or (isinstance(i, str) and i.isdigit()))


def _text(entry: Dict[str, Any], key: str) -> Optional[str]:
    return entry.get(key) if isinstance(entry.get(key), str) else None


def _parse_splits(tool_input: Any) -> List[PlannedChange]:
    raw = tool_input.get("splits") if isinstance(tool_input, dict) else None
    if not isinstance(raw, list):
        raise PlannerError("The planner's answer had no list of splits.")
    return [
        PlannedChange(
            type="split", ids=_ids(entry), new_slug=_text(entry, "newSlug"),
            description=_text(entry, "description"), why=str(entry.get("why") or ""),
        )
        for entry in raw
        if isinstance(entry, dict)
    ]


def _parse_changes(tool_input: Any) -> List[PlannedChange]:
    raw = tool_input.get("changes") if isinstance(tool_input, dict) else None
    if not isinstance(raw, list):
        raise PlannerError("The planner's answer had no list of changes.")
    changes: List[PlannedChange] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ids = _ids(entry)
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
        size: Optional[FileSize] = None,
    ) -> Plan:
        """Plan one file. Given ``size`` (a file near its limit), also ask for splits.

        The changes call decides whether the file fails: a failed split call
        is logged and its usage metered, and the file keeps its changes.
        """
        message = render_file_for_planner(
            slug, description, items, pinned=pinned, provenance=provenance, today=today
        )
        tool_input, usage = self._call(SYSTEM_PROMPT, TOOL_SPEC, TOOL_NAME, message)
        plan = Plan(input_tokens=usage[0], output_tokens=usage[1])
        try:
            plan.changes = _parse_changes(tool_input)
        except PlannerError as exc:
            raise PlannerError(str(exc), input_tokens=usage[0], output_tokens=usage[1]) from exc
        if size is None:
            return plan
        sized = render_file_for_planner(
            slug, description, items, pinned=pinned, provenance=provenance, today=today, size=size
        )
        try:
            split_input, split_usage = self._call(SPLIT_PROMPT, SPLIT_TOOL_SPEC, SPLIT_TOOL_NAME, sized)
            plan.input_tokens += split_usage[0]
            plan.output_tokens += split_usage[1]
            plan.changes.extend(_parse_splits(split_input))
        except PlannerError as exc:
            plan.input_tokens += exc.input_tokens
            plan.output_tokens += exc.output_tokens
            logger.warning("memory-maintenance: the split call for %s failed: %s", slug, exc)
        return plan

    def _call(self, system: str, tool: Dict[str, Any], tool_name: str, message: str) -> Tuple[Any, Tuple[int, int]]:
        """One forced-tool Converse call: ``(tool input, (input tokens, output tokens))``."""
        try:
            response = self.client.converse(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": message}]}],
                toolConfig={"tools": [tool], "toolChoice": {"any": {}}},
                inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS},
            )
        except Exception as exc:  # noqa: BLE001 - reported per file; one bad call never fails the run
            raise PlannerError(f"The model call failed ({type(exc).__name__}).") from exc
        usage = response.get("usage") or {}
        billed = {"input_tokens": int(usage.get("inputTokens") or 0), "output_tokens": int(usage.get("outputTokens") or 0)}
        if response.get("stopReason") == "max_tokens":
            raise PlannerError("The planner's answer was cut off.", **billed)
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        tool_use = next((c["toolUse"] for c in content if isinstance(c, dict) and "toolUse" in c), None)
        if tool_use is None or tool_use.get("name") != tool_name:
            raise PlannerError("The planner answered without proposing changes.", **billed)
        tool_input = tool_use.get("input")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except ValueError as exc:
                raise PlannerError("The planner's answer wasn't valid JSON.", **billed) from exc
        return tool_input, (billed["input_tokens"], billed["output_tokens"])

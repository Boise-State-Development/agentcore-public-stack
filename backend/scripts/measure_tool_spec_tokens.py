"""Measure what each seeded catalog tool's spec costs in ``toolConfig``, and
commit it as ``backend/evals/tool_spec_tokens.json``.

Release-ledger spec §10 Q3 (decided yes): the Releases tab's tools table shows,
beside a tool's per-call *payload* (the census's ``resultChars``), what its
*spec* costs — the schema the model is sent on every turn the tool is enabled,
which sits in the cached ``toolConfig`` prefix. The two are reported side by
side and never added together (spec D4): one is per call, the other per turn
and cached.

**What is measured.** Every ``toolId`` in ``DEFAULT_TOOLS`` (the bootstrap
seeder, the only writer of ``TOOL#`` rows) is built with the code the turn path
uses — the ``ToolRegistry`` for registered tools, the ``_build_*_tools``
factories in ``apis/inference_api/chat/routes.py`` for context-bound ones (one
catalog id can provision a family: ``create_word_document`` is five tools) — so
the specs are the ones a turn sends, not a copy. Feature flags that gate a
family member are forced on in-process (:data:`MEASURE_ENV`), so the number is
the family at its widest.

**How.** Bedrock CountTokens on the base model id (the ``us.*`` profile ids
refuse it), as the *marginal* cost beside an anchor tool: the first tool in a
request carries Bedrock's fixed tool-use preamble (~500 tokens), which a
harness pays once whatever it enables, so ``count(anchor + family) −
count(anchor)`` is the honest per-tool number. Each family member is also
measured alone the same way, so a census tool name can be joined to its own
figure. ``chars`` is the spec's serialised length, for a chars/4 sanity check.

**Staleness.** Each row carries ``specHash``, a hash of the family's specs
computed without AWS. ``tests/test_tool_spec_tokens.py`` recomputes it and fails
when a seeded tool has no row or its schema changed since it was measured, so
the file cannot silently go stale; ``--check`` prints the same verdict.

Usage (from ``backend/``)::

    eval "$(aws configure export-credentials --profile dev-ai --format env)"; unset AWS_PROFILE
    .venv/bin/python scripts/measure_tool_spec_tokens.py --region us-west-2
    .venv/bin/python scripts/measure_tool_spec_tokens.py --check   # no AWS

Content: tool names, token counts, lengths and hashes only. Specs are public
code; nothing user-bearing is read or written.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BACKEND = Path(__file__).resolve().parent.parent
OUTPUT = BACKEND / "evals" / "tool_spec_tokens.json"

#: Flags that gate a seeded family or one of its members, forced on so the
#: measurement is the family at its widest. Applied before any import reads them.
MEASURE_ENV: Dict[str, str] = {
    "PLATFORM_SELF_SERVICE_ENABLED": "true",
    "MEETING_TASKS_ENABLED": "true",
    "WORKSPACE_TOOLS_ENABLED": "true",
    "WORD_TRACKED_CHANGES_ENABLED": "true",
    "SKILLS_ENABLED": "true",
    "SKILL_AUTHORING_ENABLED": "true",
    "ASK_USER_QUESTION_ENABLED": "true",
    "BROWSER_TAKEOVER_ENABLED": "true",
}

#: CountTokens accepts only a base foundation-model id. Haiku 4.5 is the
#: platform default; Sonnet 4.6 / 4.5 count identically (measured 2026-10-29).
DEFAULT_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"

#: The tool already in the request, so the fixed tool-use preamble is paid by
#: the baseline and every measured figure is marginal.
ANCHOR_SPEC: Dict[str, Any] = {
    "name": "measurement_anchor",
    "description": "No-op.",
    "inputSchema": {"json": {"type": "object", "properties": {}}},
}

_PROBE_MESSAGES = [{"role": "user", "content": [{"text": "hi"}]}]
_SESSION_ID = "measure-session"
_USER_ID = "measure-user"


def _apply_measure_env() -> None:
    os.environ.update(MEASURE_ENV)
    src = str(BACKEND / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def seeded_tool_ids() -> List[str]:
    """``toolId`` of every ``DEFAULT_TOOLS`` row, in seed order."""
    path = BACKEND / "scripts" / "seed_bootstrap_data.py"
    spec = importlib.util.spec_from_file_location("seed_bootstrap_data_for_spec_tokens", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # its dataclasses look themselves up there
    spec.loader.exec_module(module)
    return [row["toolId"] for row in module.DEFAULT_TOOLS]


def _spec_of(tool_obj: Any) -> Dict[str, Any]:
    spec = getattr(tool_obj, "tool_spec", None)
    if not isinstance(spec, dict):
        raise TypeError(f"{tool_obj!r} has no tool_spec")
    return spec


def _tool_builder() -> Callable[[str], List[Any]]:
    """A builder from ``toolId`` to the tool objects a turn would get for
    that id. Registered tools come from the registry; the rest from the turn
    path's own ``_build_*_tools`` (never a copy of them)."""
    from agents.main_agent.tools.tool_registry import create_default_registry
    from apis.inference_api.chat import routes
    from apis.shared.auth.models import User

    registry = create_default_registry()
    user = User(email="measure@example.invalid", user_id=_USER_ID, name="Measure", roles=[])

    def registered(tool_id: str) -> List[Any]:
        tool_obj = registry.get_tool(tool_id)
        return [tool_obj] if tool_obj is not None else []

    def meeting(tool_id: str) -> List[Any]:
        # The route reads connector ids from the catalog; a spec does not
        # depend on them, so the family is built with none bound.
        from agents.local_tools.meeting_tasks import build_meeting_tasks_tools

        return build_meeting_tasks_tools([tool_id], _USER_ID, {tool_id: None})

    injected: List[Callable[[str], List[Any]]] = [
        lambda t: routes._build_spreadsheet_tools([t], None, _SESSION_ID, _USER_ID),
        lambda t: routes._build_account_tools([t], user),
        lambda t: routes._build_artifact_tools([t], _SESSION_ID, _USER_ID),
        lambda t: routes._build_word_document_tools([t], _SESSION_ID, _USER_ID, "Measure"),
        lambda t: routes._build_skill_authoring_tools([t], _SESSION_ID, _USER_ID, user.email),
        lambda t: routes._build_workspace_tools([t], _SESSION_ID, _USER_ID),
        lambda t: routes._build_excel_spreadsheet_tools([t], _SESSION_ID, _USER_ID),
        lambda t: routes._build_powerpoint_presentation_tools([t], _SESSION_ID, _USER_ID),
    ]

    def build(tool_id: str) -> List[Any]:
        from apis.shared.tools.injected import MEETING_TASKS_TOOL_IDS

        if tool_id in MEETING_TASKS_TOOL_IDS:
            return meeting(tool_id)
        for factory in injected:
            tools = factory(tool_id)
            if tools:
                return tools
        return registered(tool_id)

    return build


def build_catalog_tool_specs() -> Dict[str, List[Dict[str, Any]]]:
    """Every seeded ``toolId`` -> its family's tool specs, sorted by name.
    An id that builds nothing locally (a gateway or MCP tool) maps to ``[]``."""
    _apply_measure_env()
    build = _tool_builder()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for tool_id in seeded_tool_ids():
        specs = [_spec_of(t) for t in build(tool_id)]
        out[tool_id] = sorted(specs, key=lambda s: s["name"])
    return out


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def spec_hash(specs: List[Dict[str, Any]]) -> str:
    """Stable 16-hex hash of a family's specs; changes when any schema does."""
    return hashlib.sha256(_canonical(specs).encode("utf-8")).hexdigest()[:16]


def stale_rows(committed: Dict[str, Any], specs: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
    """``toolId`` -> why its committed row is missing or out of date."""
    rows = committed.get("tools") or {}
    problems: Dict[str, str] = {}
    for tool_id, family in specs.items():
        row = rows.get(tool_id)
        if row is None:
            problems[tool_id] = "no row"
        elif row.get("specHash") != spec_hash(family):
            problems[tool_id] = "schema changed since it was measured"
    return problems


def _count(client: Any, model_id: str, specs: List[Dict[str, Any]]) -> int:
    response = client.count_tokens(
        modelId=model_id,
        input={
            "converse": {
                "messages": _PROBE_MESSAGES,
                "toolConfig": {"tools": [{"toolSpec": s} for s in [ANCHOR_SPEC, *specs]]},
            }
        },
    )
    return int(response["inputTokens"])


def measure(region: str, model_id: str) -> Dict[str, Any]:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    specs = build_catalog_tool_specs()
    baseline = _count(client, model_id, [])
    tools: Dict[str, Any] = {}
    for tool_id, family in specs.items():
        if not family:
            tools[tool_id] = {"tokens": None, "tools": {}, "chars": 0, "specHash": spec_hash(family),
                              "note": "no local spec (gateway or MCP tool)"}
            continue
        tools[tool_id] = {
            "tokens": _count(client, model_id, family) - baseline,
            "tools": {s["name"]: _count(client, model_id, [s]) - baseline for s in family},
            "chars": sum(len(_canonical(s)) for s in family),
            "specHash": spec_hash(family),
        }
        print(f"  {tool_id:34s} {tools[tool_id]['tokens']:>6} tokens  ({len(family)} tool(s))")
    return {
        "measuredAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "modelId": model_id,
        "region": region,
        "method": (
            "Bedrock CountTokens, marginal beside one anchor tool (the fixed tool-use "
            "preamble is in the baseline); flags in MEASURE_ENV forced on"
        ),
        "baselineTokens": baseline,
        "tools": dict(sorted(tools.items())),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="report stale rows without calling AWS")
    args = parser.parse_args(argv)

    if args.check:
        committed = json.loads(args.out.read_text()) if args.out.exists() else {}
        problems = stale_rows(committed, build_catalog_tool_specs())
        for tool_id, why in sorted(problems.items()):
            print(f"{tool_id}: {why}")
        print("up to date" if not problems else f"{len(problems)} stale row(s): re-run without --check")
        return 1 if problems else 0

    result = measure(args.region, args.model_id)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")
    print(f"wrote {args.out} ({len(result['tools'])} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

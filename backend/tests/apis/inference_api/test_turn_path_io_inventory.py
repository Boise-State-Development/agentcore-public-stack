"""No new round trip reaches the turn path before the first token without someone noticing.

CLAUDE.md makes time to first token a hard budget, and every latency signal we have is
after the fact: the eval harness's ``firstTokenMs`` moves 20–115% a night on its own, the
smoke pass prints a median, the stage metrics have no alarm. A PR that adds one more
``get_item`` to ``_prepare_session_state``, or an awaited read in a ``BeforeModelCallEvent``
hook, passes every other test and costs every turn 10–30 ms.

So this counts instead of timing. ``turn_io/driver.py`` runs four turns through the real
``/invocations`` handler — the route's phases, the real agent build, the head of the
stream — in a fresh process, against a production-sized deployment seeded through real
code (``turn_io/world.py``), and records every AWS API call and HTTP request made before
the stubbed model's first byte (``turn_io/recorder.py``). Each call is tagged:

* **awaited** — it ran on the path to the model call; the model could not start until
  it finished. Adding one adds its round trip to every such turn.
* **concurrent** — it ran in a task or thread beside the path (the title, a search
  started ahead of the build). It costs first-token time only if it outlasts what it
  overlaps.

The inventory is committed as ``turn_io/inventory.json``. Any difference fails. When
the change is deliberate (a new call you have measured and accepted, a removed one, a
renamed function), regenerate it and commit the diff, which is what review reads:

    cd backend && UPDATE_TURN_IO_INVENTORY=1 uv run python -m pytest \\
        tests/apis/inference_api/test_turn_path_io_inventory.py

See docs/specs/turn-path-ttft.md §6A (invariant I11) and the TTFT rule in CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

import boto3
import pytest

# Module level, before any `tests.apis` import: `backend/tests/apis/__init__.py` makes
# `tests/apis` a package also named `apis`, and the first `apis.` import decides which one
# the name binds to (the same trap test_preparing_phase.py documents).
import apis.shared.caching  # noqa: F401

from tests.apis.inference_api.turn_io.recorder import FakeAws, Recorder

BACKEND = Path(__file__).resolve().parents[3]
INVENTORY = Path(__file__).resolve().parent / "turn_io" / "inventory.json"
UPDATE_ENV = "UPDATE_TURN_IO_INVENTORY"
REGENERATE = f"cd backend && {UPDATE_ENV}=1 uv run python -m pytest " "tests/apis/inference_api/test_turn_path_io_inventory.py"

_LINE = re.compile(r"^(\d+)x (.*)$")


def _run_driver(tmp_path: Path) -> dict:
    out = tmp_path / "observed.json"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PYTHONPATH": os.pathsep.join([str(BACKEND / "src"), str(BACKEND)]),
        "PYTHONHASHSEED": "0",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "tests.apis.inference_api.turn_io.driver", "--out", str(out)],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, f"the turn-io driver crashed:\n{proc.stderr[-6000:]}"
    return json.loads(out.read_text())


def _counts(lines: List[str]) -> Counter:
    counts: Counter = Counter()
    for line in lines:
        match = _LINE.match(line)
        counts[match.group(2)] += int(match.group(1))
    return counts


def _diff(expected: Dict[str, dict], observed: Dict[str, dict]) -> List[str]:
    messages = []
    for scenario in sorted(set(expected) | set(observed)):
        if scenario not in observed:
            messages.append(f"[{scenario}] no longer runs")
            continue
        if scenario not in expected:
            messages.append(f"[{scenario}] is new; regenerate the inventory")
            continue
        for lane in ("awaited", "concurrent"):
            want = _counts(expected[scenario][lane])
            got = _counts(observed[scenario][lane])
            for call in sorted(set(want) | set(got)):
                delta = got[call] - want[call]
                if delta:
                    messages.append(f"[{scenario}] {lane} {delta:+d}: {call}")
    return messages


def _failure(problems: List[str], hooks: List[str], drift: List[str]) -> str:
    parts = []
    if problems:
        parts.append(
            "The inventory's turns did not run as written, so their calls cannot be compared. "
            "Fix the fixture (turn_io/world.py, turn_io/driver.py) or the fake "
            "(turn_io/recorder.py):\n" + "\n".join(problems)
        )
    if hooks:
        parts.append(
            "A Before* hook did awaited I/O before a later model call of a tool round. Those "
            "hooks fire before EVERY model call and tool call, so the round trip repeats on each "
            "round. CLAUDE.md's TTFT rule: a hook captures raw facts only; anything derived or "
            "fetched happens after the model answers, or off the path:\n" + "\n".join(hooks)
        )
    if drift:
        lead = "The turn path's calls before the first token changed.\n\n"
        if any("] awaited +" in line for line in drift):
            lead += (
                "An `awaited +N` line is a new blocking round trip before the first token, on every "
                "turn of that kind (10-30 ms each on dev). CLAUDE.md treats time to first token as a "
                "hard budget: move the call off the path (overlap it, defer it past the first token, "
                "or answer it from a snapshot already read), or measure it at realistic scale, state "
                "the number in the PR and get the developer driving the change to accept it. The map "
                "of the path is docs/specs/turn-path-ttft.md (§2 for the stages, §6A I11 for this "
                "check).\n\n"
            )
        parts.append(
            lead + "\n".join(drift) + "\n\nA `concurrent` line runs beside the path and costs first-token time only if it "
            "outlasts what it overlaps. A `-N` line is a call that went away. A renamed function "
            "shows as one removal and one addition. If the change is deliberate, regenerate and "
            f"commit the inventory:\n\n    {REGENERATE}"
        )
    return "\n\n".join(parts)


def test_the_turn_path_calls_match_the_inventory(tmp_path):
    """One test, so the driver runs once per suite even under ``pytest -n``."""
    observed = _run_driver(tmp_path)
    problems = list(observed.get("problems", []))
    problems += [f"unanswered by the fake: {op}" for op in observed.get("unanswered_operations", [])]
    hooks = observed.get("before_hook_io_in_tool_round", [])

    if os.environ.get(UPDATE_ENV) and not problems:
        INVENTORY.write_text(
            json.dumps(
                {
                    "_regenerate": REGENERATE,
                    "_see": "docs/specs/turn-path-ttft.md §6A I11; tests/apis/inference_api/test_turn_path_io_inventory.py",
                    "scenarios": observed["scenarios"],
                },
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )

    expected = json.loads(INVENTORY.read_text())["scenarios"]
    drift = _diff(expected, observed["scenarios"])
    if problems or hooks or drift:
        pytest.fail(_failure(problems, hooks, drift), pytrace=False)


# ── the recorder's own lanes ────────────────────────────────────────────
#
# The inventory is only as good as the awaited/concurrent split, so the split is pinned
# here on toy code, where the right answer is obvious.


@pytest.fixture
def recorder():
    rec = Recorder(FakeAws(), "toy")
    rec.install()
    try:
        yield rec
    finally:
        rec.uninstall()


def _get(table: str = "toy-things") -> None:
    boto3.client("dynamodb", region_name="us-east-1").get_item(TableName=table, Key={"PK": {"S": "x"}})


def _model() -> None:
    boto3.client("bedrock-runtime", region_name="us-east-1").converse_stream(modelId="m", messages=[{"role": "user", "content": [{"text": "hi"}]}])


async def _turn(recorder: Recorder, body, after=None) -> Dict[str, List[str]]:
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(recorder.task_factory)
    try:
        recorder.start_scenario()
        await body()
        await asyncio.to_thread(_model)
        if after is not None:
            await after()
        await recorder.settle()
        return recorder.inventory_before_model_call(0)
    finally:
        loop.set_task_factory(previous)


def _ops(lines: List[str]) -> List[str]:
    return sorted(line.split("  <-")[0] for line in lines)


@pytest.mark.asyncio
async def test_a_read_awaited_on_the_path_is_awaited(recorder):
    async def body():
        await asyncio.to_thread(_get)
        _get()  # a synchronous call on the event loop blocks it outright

    lanes = await _turn(recorder, body)
    assert _ops(lanes["awaited"]) == ["2x dynamodb.GetItem things"]
    assert lanes["concurrent"] == []


@pytest.mark.asyncio
async def test_a_task_beside_the_path_is_concurrent_even_when_awaited_later(recorder):
    async def body():
        ahead = asyncio.create_task(asyncio.to_thread(_get, "toy-ahead"))
        await asyncio.to_thread(_get)
        await ahead

    lanes = await _turn(recorder, body)
    assert _ops(lanes["awaited"]) == ["1x dynamodb.GetItem things"]
    assert _ops(lanes["concurrent"]) == ["1x dynamodb.GetItem ahead"]


@pytest.mark.asyncio
async def test_pool_work_joined_at_once_is_awaited_and_a_prefetch_is_concurrent(recorder):
    pool = ThreadPoolExecutor(max_workers=2)

    async def body():
        pool.submit(_get).result()
        prefetch = pool.submit(_get, "toy-prefetch")
        await asyncio.to_thread(_get, "toy-between")
        prefetch.result()

    try:
        lanes = await _turn(recorder, body)
    finally:
        pool.shutdown()
    assert _ops(lanes["awaited"]) == ["1x dynamodb.GetItem between", "1x dynamodb.GetItem things"]
    assert _ops(lanes["concurrent"]) == ["1x dynamodb.GetItem prefetch"]


@pytest.mark.asyncio
async def test_a_call_after_the_first_byte_is_not_counted(recorder):
    async def body():
        await asyncio.to_thread(_get)

    async def after():
        await asyncio.to_thread(_get, "toy-after")

    lanes = await _turn(recorder, body, after)
    assert _ops(lanes["awaited"]) == ["1x dynamodb.GetItem things"]
    assert lanes["concurrent"] == []

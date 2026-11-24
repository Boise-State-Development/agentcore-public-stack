"""Run the inventory's turns in a fresh process and write what each one called.

    python -m tests.apis.inference_api.turn_io.driver --out inventory.json

A fresh process on purpose: the turn path is full of per-process caches (the agent
cache, RBAC, the catalog snapshot, the Memory strategy ids), and a first turn on a new
microVM — every V2 restore — meets them all empty. Run inside the pytest worker, the
first turn's inventory would depend on which tests that worker ran before it.

The four turns run in order on one process, so each later one meets the caches the
earlier ones filled, as it would on a pinned microVM:

* ``first_turn``      — a new conversation: every default tool, a skill, a PDF.
* ``warm_follow_up``  — the same conversation, same configuration: a cache hit. Its
  model answers with a tool call first, so it also yields ``tool_round``, the window
  between its first and second model calls.
* ``agent_mention``   — the same conversation, ``@``-mentioning an Agent with a
  knowledge base.
* ``project_turn``    — a new conversation on a Shared Project's harness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[4]
for path in (str(BACKEND / "src"), str(BACKEND)):
    if path not in sys.path:
        sys.path.insert(0, path)

from tests.apis.inference_api.turn_io import world as world_module  # noqa: E402

_socket_attempts: list = []


def _guard_sockets() -> None:
    def refuse(self, address, *args, **kwargs):
        _socket_attempts.append(repr(address))
        raise OSError(f"turn-io driver: network refused ({address!r})")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]


def _text_of(frames: str) -> str:
    out = []
    for frame in frames.split("\n\n"):
        if frame.startswith("event: content_block_delta\ndata: "):
            data = json.loads(frame.split("data: ", 1)[1])
            out.append(data.get("text", ""))
    return "".join(out)


async def _run() -> dict:
    from apis.inference_api.chat.models import InvocationRequest
    import apis.inference_api.chat.routes as routes
    from apis.shared.auth.models import User

    from tests.apis.inference_api.turn_io.recorder import (
        FakeAws,
        Recorder,
        text_response,
        tool_use_response,
    )

    fake = FakeAws()
    recorder = Recorder(fake, world_module.PREFIX)
    recorder.install()
    loop = asyncio.get_running_loop()
    loop.set_task_factory(recorder.task_factory)

    world = await world_module.build_world(fake)
    user = User(
        email=world_module.USER_EMAIL,
        user_id=world_module.USER_ID,
        name=world_module.USER_NAME,
        roles=["Staff"],
        raw_token="tok",
    )
    base = {
        "model_id": world.model_id,
        "enabled_tools": list(world.tool_ids),
        "enabled_skills": list(world.skill_ids),
    }
    turns = [
        (
            "first_turn",
            dict(
                base,
                session_id=world_module.CHAT_SESSION,
                message="Here is our Q3 plan. What are the risks?",
                file_upload_ids=[world_module.UPLOAD_ID],
            ),
            [text_response("Three risks stand out.")],
        ),
        (
            "warm_follow_up",
            dict(base, session_id=world_module.CHAT_SESSION, message="What is 17 times 23?"),
            [tool_use_response("tooluse-tio-1", "calculator", {"expression": "17*23"}), text_response("391.")],
        ),
        (
            "agent_mention",
            dict(
                base,
                session_id=world_module.CHAT_SESSION,
                message="When is the add/drop deadline?",
                rag_assistant_id=world.agent_id,
                agent_mention=True,
            ),
            [text_response("Before the add/drop deadline.")],
        ),
        (
            "project_turn",
            dict(
                base, session_id=world_module.PROJECT_SESSION, message="Summarize where the plan stands.", rag_assistant_id=world.project_harness_id
            ),
            [text_response("The plan is on track.")],
        ),
    ]

    result: dict = {"scenarios": {}, "problems": []}
    for name, payload, script in turns:
        fake.model.responses = script
        fake.model._calls = 0
        recorder.start_scenario()
        response = await routes.invocations(InvocationRequest(**payload), user)
        frames = []
        async for chunk in response.body_iterator:
            frames.append(chunk if isinstance(chunk, str) else chunk.decode())
        await recorder.settle()
        body = "".join(frames)
        expected = script[-1][1]["contentBlockDelta"]["delta"]["text"]
        if expected not in _text_of(body):
            calls = "\n".join(f"  {c.key()}  <- {c.origin}" for c in recorder.calls)
            result["problems"].append(
                f"{name}: the stream never carried the model's answer; the turn stopped early.\n" f"{body[-2000:]}\ncalls made:\n{calls}"
            )
            continue
        if len(recorder.model_calls) != len(script):
            result["problems"].append(f"{name}: expected {len(script)} model call(s), saw {len(recorder.model_calls)}")
            continue
        result["scenarios"][name] = recorder.inventory_before_model_call(0)
        if len(script) > 1:
            result["scenarios"]["tool_round"] = recorder.inventory_before_model_call(1)
            result["before_hook_io_in_tool_round"] = recorder.awaited_before_hook_calls(0)

    if _socket_attempts:
        result["problems"].append(f"socket connections attempted: {_socket_attempts}")
    if fake.errors:
        result["problems"].extend(f"the fake failed to answer {error}" for error in sorted(set(fake.errors)))
    if fake.unhandled:
        result["unanswered_operations"] = sorted(fake.unhandled)
    recorder.uninstall()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.CRITICAL)
    os.environ.update(world_module.environment())
    _guard_sockets()
    result = asyncio.run(_run())
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

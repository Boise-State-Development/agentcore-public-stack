"""The run brief names a prompt step as what it is (workflow-designer.md §21.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

import apis.inference_api.chat.handoff_brief as brief_module
from apis.shared.handoff.brief import RunBriefStep
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep


class PromptRunStep(WorkflowRunStep):
    """A run step with D.6a's ``kind`` and ``modelId`` (stand-in until those fields land)."""

    kind: str = "agent"
    model_id: Optional[str] = None


def _run() -> WorkflowRun:
    now = datetime.now(timezone.utc).isoformat()
    return WorkflowRun(
        run_id="r1",
        user_id="alice",
        session_id="src",
        status="open",
        request="Review this proposal",
        steps=[
            PromptRunStep(step_id="s1", handoff_id="h1", agent_id="a1", agent_name="Generalist", request="Review this proposal"),
            PromptRunStep(
                step_id="s2",
                handoff_id="h2",
                agent_id="",
                agent_name="",
                step_name="budget-lens",
                request="Check the budget only",
                kind="prompt",
                model_id="us.anthropic.claude-haiku-5-5",
            ),
        ],
        created_at=now,
        updated_at=now,
    )


def test_a_prompt_step_is_named_by_its_step_and_described_as_a_prompt_step():
    async def go():
        async def descriptions():
            return ["Reads anything.", None]

        return await brief_module._run_steps(_run(), asyncio.ensure_future(descriptions()))

    steps = asyncio.run(go())

    assert steps == [
        RunBriefStep("Generalist", "Review this proposal", "Reads anything."),
        RunBriefStep("budget-lens", "Check the budget only", brief_module.PROMPT_STEP_BRIEF_DESCRIPTION),
    ]


def test_a_run_of_agent_steps_is_named_as_before():
    run = _run()
    run.steps = [run.steps[0]]

    steps = asyncio.run(brief_module._run_steps(run, None))

    assert steps == [RunBriefStep("Generalist", "Review this proposal", None)]

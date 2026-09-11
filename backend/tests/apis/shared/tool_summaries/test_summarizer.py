"""Tests for the tool-batch summarizer side-channel.

The property that matters most here is that a BAD summary is worse than no
summary. The SPA always has a deterministic line to fall back on, so every
failure mode must return ``None`` rather than surface a fragment — a half
sentence in the rail reads as a bug in the product, where the fallback reads
as normal.

The truncation case is not hypothetical: observed live on dev 2026-09-11,
Nova began ``Found the course "Faculty Demo: Intro to MCP"`` and was hard-cut
at the token ceiling, persisting ``Found the course "Faculty Demo: Intro to
MCP`` with a dangling quote.
"""

from unittest.mock import MagicMock, patch

import pytest

from apis.shared.tool_summaries.summarizer import (
    _build_prompt,
    _clean,
    _MAX_CALLS,
    summarize_tool_batch,
)


def _response(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
    }


def _calls(n: int = 1):
    return [
        {
            "toolUseId": f"t{i}",
            "toolName": "list_courses",
            "input": '{"term": "fall"}',
            "result": '{"courses": ["BIO 101"]}',
            "ok": True,
            "durationMs": 120,
        }
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def summaries_enabled(monkeypatch):
    monkeypatch.setenv("TOOL_SUMMARIES_ENABLED", "true")


@pytest.fixture
def bedrock(monkeypatch):
    """Patch boto3.client so no test ever reaches Bedrock."""
    client = MagicMock()
    module = MagicMock()
    module.client.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "boto3", module)
    return client


# -- the truncation regression -------------------------------------------


@pytest.mark.asyncio
async def test_a_generation_cut_at_the_token_ceiling_is_discarded(bedrock):
    bedrock.converse.return_value = _response(
        'Found the course "Faculty Demo: Intro to MCP', stop_reason="max_tokens"
    )

    assert await summarize_tool_batch(_calls()) is None


@pytest.mark.asyncio
async def test_a_complete_generation_is_kept(bedrock):
    bedrock.converse.return_value = _response("Found 3 active courses")

    assert await summarize_tool_batch(_calls()) == "Found 3 active courses"


@pytest.mark.asyncio
async def test_truncation_is_judged_by_stop_reason_not_by_length(bedrock):
    """A short line is not evidence of completeness, and vice versa.

    `stopReason` is the only signal that distinguishes "the model finished"
    from "we cut it off", so a long-but-finished summary must survive.
    """
    long_but_finished = "Found the Syllabus Acknowledgment and Homework 1 assignments"
    bedrock.converse.return_value = _response(long_but_finished)

    assert await summarize_tool_batch(_calls()) == long_but_finished


# -- every other failure is also a None -----------------------------------


@pytest.mark.asyncio
async def test_empty_batch_returns_none(bedrock):
    assert await summarize_tool_batch([]) is None
    bedrock.converse.assert_not_called()


@pytest.mark.asyncio
async def test_flag_off_returns_none_without_calling_bedrock(bedrock, monkeypatch):
    monkeypatch.setenv("TOOL_SUMMARIES_ENABLED", "false")

    assert await summarize_tool_batch(_calls()) is None
    bedrock.converse.assert_not_called()


@pytest.mark.asyncio
async def test_a_model_error_returns_none(bedrock):
    bedrock.converse.side_effect = RuntimeError("throttled")

    assert await summarize_tool_batch(_calls()) is None


@pytest.mark.asyncio
async def test_an_empty_generation_returns_none(bedrock):
    bedrock.converse.return_value = _response("   ")

    assert await summarize_tool_batch(_calls()) is None


@pytest.mark.asyncio
async def test_a_malformed_response_returns_none(bedrock):
    bedrock.converse.return_value = {"output": {}}

    assert await summarize_tool_batch(_calls()) is None


# -- cleaning --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Output: Found 3 courses", "Found 3 courses"),
        ("Summary: Found 3 courses", "Found 3 courses"),
        ('"Found 3 courses"', "Found 3 courses"),
        ("Found 3 courses.", "Found 3 courses"),
        ("Found 3 courses\nAnd some commentary", "Found 3 courses"),
        ("  Found 3 courses  ", "Found 3 courses"),
    ],
)
def test_clean_strips_the_wrappers_small_models_add(raw, expected):
    assert _clean(raw) == expected


def test_clean_keeps_an_interior_quote():
    # Stripping only the OUTER quotes matters: the specific thing being named
    # is often quoted, and eating those quotes loses the specificity that is
    # the whole point of the summary.
    assert _clean('Found the course "Intro to MCP"') == 'Found the course "Intro to MCP"'


# -- prompt bounds ---------------------------------------------------------


def test_prompt_is_bounded_for_a_wide_batch():
    prompt = _build_prompt(_calls(_MAX_CALLS + 5))

    assert prompt.count("list_courses(") == _MAX_CALLS
    assert "and 5 more call(s)" in prompt


def test_prompt_marks_a_failed_call_as_failed():
    calls = _calls()
    calls[0]["ok"] = False
    calls[0]["result"] = "422 rubric association required"

    assert "FAILED" in _build_prompt(calls)

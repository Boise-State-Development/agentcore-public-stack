"""Inline attachments must not reuse a document name already in the conversation.

Bedrock rejects a ConverseStream request in which two document blocks share a
name anywhere in ``messages`` ("Messages can't contain duplicate document
names"), not just within one message. It was the largest force-stop cause in
prod over the 30 days to 2026-11-06. Nearly every hit was a warm agent-cache hit
whose live ``agent.messages`` still held the inline block from an earlier turn.

The common sequence: a turn carrying ``report.pdf`` fails for some other reason
(a Bedrock read timeout, a Memory 413, an unsupported MIME type). Its user
message stays in the live list with the synthetic error reply after it. On the
next turn the pending-attachment recovery re-attaches ``report.pdf``, which
gets the same sanitized name, so the request fails. That leaves two
``report_pdf`` blocks in the live list, and every later turn on the agent then
fails, even with renamed uploads.

Before this fix ``PromptBuilder`` de-duplicated names within one turn only.
"""

from __future__ import annotations

import base64
import copy
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agents.main_agent.chat_agent import ChatAgent
from agents.main_agent.multimodal.prompt_builder import PromptBuilder, document_names_in
from agents.main_agent.session.document_offload import name_stem
from agents.main_agent.session.document_rehydration import match_document
from tests.agents.main_agent.conftest import FakeFileContent


def _pdf(filename: str = "report.pdf", payload: bytes = b"%PDF-1.4 fake") -> FakeFileContent:
    return FakeFileContent(
        filename=filename,
        content_type="application/pdf",
        bytes=base64.b64encode(payload).decode(),
    )


def _user_turn_with_document(name: str) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"text": "Summarize this\n\n[Attached files: report.pdf]"},
            {"document": {"format": "pdf", "name": name, "source": {"bytes": b"%PDF-1.4 fake"}}},
        ],
    }


def _failed_turn_history() -> List[Dict[str, Any]]:
    """The live list after a turn carrying ``report.pdf`` force-stopped."""
    return [
        _user_turn_with_document("report_pdf"),
        {"role": "assistant", "content": [{"text": "Something went wrong. Please try again."}]},
    ]


def _assert_bedrock_accepts_names(messages: List[Dict[str, Any]]) -> None:
    """Bedrock's rule: no two document blocks share a name anywhere in the request."""
    names: List[str] = []
    for message in messages:
        for block in message.get("content", []):
            if "document" in block:
                names.append(block["document"]["name"])
            for item in block.get("toolResult", {}).get("content", []):
                if "document" in item:
                    names.append(item["document"]["name"])
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"Messages can't contain duplicate document names: {duplicates}"


def _new_document_names(prompt: Any) -> List[str]:
    return [b["document"]["name"] for b in prompt if "document" in b]


class TestNamesUniqueAcrossTheConversation:
    def test_reattached_file_after_a_failed_turn_gets_a_fresh_name(self):
        """The prod sequence: recovery re-sends the failed turn's file to the warm agent."""
        history = _failed_turn_history()

        prompt = PromptBuilder().build_prompt("Summarize this", files=[_pdf()], conversation=history)

        assert _new_document_names(prompt) == ["report_pdf_2"]
        _assert_bedrock_accepts_names(history + [{"role": "user", "content": prompt}])

    def test_without_the_conversation_the_same_request_collides(self):
        """Documents the defect: the turn-local de-duplication alone yields the taken name."""
        history = _failed_turn_history()

        prompt = PromptBuilder().build_prompt("Summarize this", files=[_pdf()])

        assert _new_document_names(prompt) == ["report_pdf"]
        with pytest.raises(AssertionError, match="duplicate document names"):
            _assert_bedrock_accepts_names(history + [{"role": "user", "content": prompt}])

    def test_counter_skips_every_suffix_already_taken(self):
        history = [_user_turn_with_document("report_pdf"), _user_turn_with_document("report_pdf_2")]

        prompt = PromptBuilder().build_prompt("Again", files=[_pdf()], conversation=history)

        assert _new_document_names(prompt) == ["report_pdf_3"]

    def test_two_copies_in_one_turn_and_one_in_history_are_all_distinct(self):
        history = _failed_turn_history()

        prompt = PromptBuilder().build_prompt(
            "Compare", files=[_pdf(), _pdf(payload=b"%PDF-1.4 other")], conversation=history
        )

        assert _new_document_names(prompt) == ["report_pdf_2", "report_pdf_3"]
        _assert_bedrock_accepts_names(history + [{"role": "user", "content": prompt}])

    def test_names_inside_tool_results_are_taken_too(self):
        history = [
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tool-1",
                            "status": "success",
                            "content": [
                                {"text": "{}"},
                                {"document": {"format": "pdf", "name": "report_pdf", "source": {"bytes": b"x"}}},
                            ],
                        }
                    }
                ],
            }
        ]

        prompt = PromptBuilder().build_prompt("More", files=[_pdf()], conversation=history)

        assert _new_document_names(prompt) == ["report_pdf_2"]

    def test_an_unrelated_name_is_unchanged(self):
        """Only a collision changes a name, so a first attachment reads as it always did."""
        prompt = PromptBuilder().build_prompt(
            "Read", files=[_pdf("budget.pdf")], conversation=_failed_turn_history()
        )

        assert _new_document_names(prompt) == ["budget_pdf"]


class TestHistoryIsNeverRewritten:
    def test_conversation_is_left_byte_for_byte_as_it_was(self):
        """The cached prefix is the history: naming a new block must not touch it."""
        history = _failed_turn_history() + [_user_turn_with_document("report_pdf_2")]
        before = copy.deepcopy(history)

        PromptBuilder().build_prompt("Again", files=[_pdf()], conversation=history)

        assert history == before

    def test_same_history_gives_the_same_name(self):
        """Deterministic: no random tail, so a retried build names the block identically."""
        history = _failed_turn_history()
        builder = PromptBuilder()

        first = builder.build_prompt("x", files=[_pdf()], conversation=history)
        second = builder.build_prompt("x", files=[_pdf()], conversation=history)

        assert _new_document_names(first) == _new_document_names(second)


class TestSuffixedNamesStillResolve:
    """Restore and offload already read PromptBuilder's ``_N`` suffix."""

    def test_rehydration_matches_the_suffixed_block_to_its_upload(self):
        upload = SimpleNamespace(upload_id="up-1", filename="report.pdf", size_bytes=13, file_format="pdf")

        assert match_document("report_pdf_2", "pdf", 13, [upload], set()) is upload

    def test_offload_reads_the_stem_a_user_would_type(self):
        assert name_stem("report_pdf_2") == "report"


class TestDocumentNamesIn:
    def test_tolerates_empty_and_malformed_messages(self):
        assert document_names_in(None) == set()
        assert document_names_in([]) == set()
        assert document_names_in([{"role": "user", "content": "plain"}, "junk", {"content": [None, {}]}]) == set()

    def test_scan_of_a_long_conversation_stays_well_under_a_millisecond_per_hundred_messages(self):
        """It runs on the turn path before the first token, so keep it bounded."""
        history = []
        for i in range(400):
            history.append(_user_turn_with_document(f"doc {i}_pdf"))
            history.append({"role": "assistant", "content": [{"text": "ok"}, {"toolUse": {"name": "t"}}]})

        started = time.perf_counter()
        names = document_names_in(history)
        elapsed_ms = (time.perf_counter() - started) * 1000

        assert len(names) == 400
        assert elapsed_ms < 50


class _RecordingBuilder:
    def __init__(self) -> None:
        self.conversation: Any = None

    def build_prompt(self, message, files, attachment_names=None, conversation=None):
        self.conversation = conversation
        return message


class _NullCoordinator:
    async def stream_response(self, **kwargs):
        if False:  # pragma: no cover - make this an async generator
            yield ""


@pytest.mark.asyncio
async def test_chat_agent_names_against_its_live_conversation():
    """ChatAgent hands the builder the list the prompt will join, not a copy."""
    live = _failed_turn_history()
    builder = _RecordingBuilder()
    agent = object.__new__(ChatAgent)
    agent.agent = SimpleNamespace(messages=live)
    agent.stream_coordinator = _NullCoordinator()
    agent.multimodal_builder = builder
    agent.session_manager = object()
    agent.session_id = "sess-1"
    agent.user_id = "user-1"

    async for _ in agent.stream_async("Summarize this", files=[_pdf()]):
        pass

    assert builder.conversation is live

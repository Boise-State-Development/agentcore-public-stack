"""The route's half of unified attachment delivery phase 5 (§4.3, the ``search`` row).

The planner decides (``attachment_plan.plan_conversation``); the route reads the
conversation's file rows without adding to the time to first token, records the
decision on ``attachmentPlan`` and puts the steering in the turn's user message.
These tests pin:

- the session-files read runs only under the ``token`` profile with the KB on,
  never for a brief, a resume or a continuation, off the event loop, and keeps
  the caller's rows only (a session id alone does not name a conversation);
- the conversation plan lands on the turn's plan, and a failed read is a turn
  with no steering, never a failed turn;
- a file the planner sent ``search`` is offered ``query`` even when it is over a
  budget, and one it sent ``on_demand`` is not;
- the steering line is deterministic, counts only, and appears only when the
  planner steers and the tool has ``query``.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import replace

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import FileContent
from apis.inference_api.chat.routes import TurnAttachments, TurnModel, _carry_out_token_plan, _file_facts
from apis.shared.files import models as file_models
from apis.shared.files.attachment_plan import (
    MODE_NATIVE,
    MODE_SEARCH,
    MODE_TEXT,
    ConversationPlan,
    plan_attachments,
    token_profile,
)
from apis.shared.files.reference_attachments import ReferenceAttachment

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _row(upload_id: str, *, text: int = 60_000, user: str = "user-1", kb=None, filename="memo.docx", mime=DOCX):
    return file_models.FileMetadata(
        upload_id=upload_id, user_id=user, session_id="sess-1", filename=filename, mime_type=mime,
        size_bytes=200_000, s3_key="k", s3_bucket="b", status=file_models.FileStatus.READY,
        facts={"version": 1, "class": "document", "tokens": {"text": text, "native": 50_000},
               "pages": None, "hasTextLayer": True, "units": "paragraph"},
        kb_index=kb,
    )


INDEXED = {"status": "indexed"}
HAIKU55 = TurnModel(
    model_id="us.anthropic.claude-haiku-5-5", provider="bedrock", caching_enabled=None, inference_params={},
    mantle_api_mode=None, mantle_region=None, input_modalities=["TEXT", "IMAGE"], context_window=100_000,
)


@pytest.fixture
def token_on(monkeypatch):
    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
    monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_ENABLED", "true")
    for name in ("AGENTCORE_MEMORY_COMPACTION_CEILING_CAP_TOKENS", "AGENTCORE_MEMORY_COMPACTION_CEILING_RATIO"):
        monkeypatch.delenv(name, raising=False)


def _attachments(rows) -> TurnAttachments:
    profile = token_profile()
    files = [FileContent(filename=r.filename, content_type=r.mime_type, bytes=base64.b64encode(b"x" * 100).decode()) for r in rows]
    upload_rows = {id(f): r for f, r in zip(files, rows)}
    facts = [_file_facts(f, upload_rows) for f in files]
    plan = plan_attachments(facts, None, profile=profile)
    inline = plan.indices(MODE_NATIVE, MODE_TEXT)
    return TurnAttachments(
        files_to_send=[files[i] for i in inline], marker_names=[r.filename for r in rows],
        plan=plan, plan_facts=facts, inline_indices=inline, profile=profile, upload_rows=upload_rows,
    )


class _Repo:
    def __init__(self, rows):
        self.rows = rows
        self.threads = []

    def list_session_files_sync(self, session_id, status=None):
        self.threads.append(threading.current_thread() is threading.main_thread())
        assert status == file_models.FileStatus.READY
        return list(self.rows)


@pytest.fixture
def repo(monkeypatch):
    holder = _Repo([])
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: holder)
    return holder


# --- the read ---


@pytest.mark.parametrize("kw", [
    {"is_brief": True, "is_resume": False, "is_continuation": False},
    {"is_brief": False, "is_resume": True, "is_continuation": False},
    {"is_brief": False, "is_resume": False, "is_continuation": True},
])
def test_no_read_where_no_message_is_composed(token_on, repo, kw):
    assert routes._start_session_files_read("sess-1", "user-1", **kw) is None


@pytest.mark.parametrize("env", [{"ATTACHMENT_PLAN_ENABLED": "false"}, {"ATTACHMENT_KB_ENABLED": "false"}])
def test_no_read_without_the_token_profile_and_the_kb(token_on, repo, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert routes._start_session_files_read(
        "sess-1", "user-1", is_brief=False, is_resume=False, is_continuation=False
    ) is None


def test_the_read_is_off_the_event_loop_and_keeps_the_callers_rows(token_on, repo):
    repo.rows = [_row("up-a"), _row("up-b", user="someone-else")]

    async def go():
        task = routes._start_session_files_read(
            "sess-1", "user-1", is_brief=False, is_resume=False, is_continuation=False
        )
        return await task

    rows = asyncio.run(go())
    assert [r.upload_id for r in rows] == ["up-a"]
    assert repo.threads == [False]


# --- the plan ---


def _planned(rows_this_turn, earlier_rows, *, fail=False):
    attachments = _attachments(rows_this_turn)
    _carry_out_token_plan(attachments, HAIKU55)

    async def go():
        async def read():
            if fail:
                raise RuntimeError("throttled")
            return earlier_rows

        attachments.session_files_task = asyncio.ensure_future(read())
        return await routes._plan_conversation_set(attachments)

    return attachments, asyncio.run(go())


def test_the_conversation_lands_on_the_turns_plan(token_on):
    earlier = [_row("up-1", kb=INDEXED), _row("up-2", kb=INDEXED), _row("up-3")]
    attachments, conversation = _planned([], earlier)
    assert (conversation.documents, conversation.searchable, conversation.steer) == (3, 2, True)
    row = attachments.plan.to_row()
    assert row["conversation"]["steer"] == "search" and row["conversation"]["reason"] == "set_size"


def test_this_turns_indexed_over_budget_file_goes_search(token_on):
    attachments, _ = _planned([_row("up-new", kb=INDEXED)], [])
    assert [e.mode for e in attachments.plan.entries] == [MODE_SEARCH]
    assert routes._search_upload_ids(attachments.plan) == frozenset({"up-new"})


def test_a_failed_read_is_a_turn_without_steering(token_on):
    attachments, conversation = _planned([], [], fail=True)
    assert conversation is None
    assert attachments.plan.to_row() is None


def test_no_task_no_plan(token_on):
    attachments = _attachments([])
    _carry_out_token_plan(attachments, HAIKU55)
    assert asyncio.run(routes._plan_conversation_set(attachments)) is None


# --- the guidance ---


def _guidance(refs, *, search_ids=frozenset(), conversation=None, search_available=True):
    return routes._build_attachment_guidance(
        [], [], [], enabled_tools=["document_read"], references=refs,
        search_available=search_available, search_upload_ids=search_ids, conversation_set=conversation,
    )


def test_an_indexed_over_budget_file_is_offered_query():
    ref = ReferenceAttachment(meta=_row("up-a"), over_budget=True, token_budget=True)
    assert "first with `query`" in _guidance([ref], search_ids=frozenset({"up-a"}))
    assert "first with `pattern`" in _guidance([ref])


STEER = ConversationPlan(documents=4, searchable=3, tokens=120_000, steer=True, reason="set_size")


def test_the_steering_line():
    line = routes._conversation_search_sentence(STEER, True)
    assert line == (
        "_This conversation has 4 documents that are read on demand rather than kept in context, and "
        "3 of them are indexed for search. To find something in them, call `document_read` with `query` "
        "and no `upload_id` first: one search covers every indexed document. Then read the pages or "
        "passages it points to._"
    )
    assert "all of them are indexed" in routes._conversation_search_sentence(replace(STEER, searchable=4), True)
    assert "1 of them is indexed" in routes._conversation_search_sentence(replace(STEER, searchable=1), True)


@pytest.mark.parametrize("conversation,available", [
    (None, True),
    (replace(STEER, steer=False, reason="below_threshold"), True),
    (STEER, False),
])
def test_no_steering_line(conversation, available):
    assert routes._conversation_search_sentence(conversation, available) == ""
    assert "one search covers" not in _guidance([], conversation=conversation, search_available=available)


def test_the_guidance_carries_the_steer_with_no_attachment_this_turn():
    assert "one search covers every indexed document" in _guidance([], conversation=STEER)

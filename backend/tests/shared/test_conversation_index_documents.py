"""The conversation-search index's document shape and its reserved knowledge base.

docs/specs/conversation-search.md §4 (PR-2b). Pure rules first — how an archive key
is parsed, how a turn becomes a KB document id and attributes — then the guards
that keep the shared ``conversations`` KB_Record out of agent teardown.
"""

import pytest

from apis.shared.conversation_archive import parse_archive_key
from apis.shared.conversation_archive.documents import ArchivedTurn, archive_key
from apis.shared.conversation_archive.index_documents import (
    index_attributes,
    index_document_id,
    is_legacy_index_document_id,
    index_text,
    parse_index_document_id,
    turn_matches_key,
)
from apis.shared.kb_backend import provisioning
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID, is_reserved_kb_id


def _turn(**overrides) -> ArchivedTurn:
    fields = dict(
        user_id="user-1",
        session_id="sess-1",
        message_index=6,
        user_text="the syllabus rewrite",
        assistant_text="Here is the revised syllabus.",
        created_at="2026-10-07T12:00:00Z",
    )
    fields.update(overrides)
    return ArchivedTurn(**fields)


# ── Archive keys ─────────────────────────────────────────────────────────────
def test_parse_archive_key_inverts_archive_key():
    assert parse_archive_key(archive_key("user-1", "sess-1", 6)) == ("user-1", "sess-1", 6)
    assert parse_archive_key(archive_key("u", "s", 1_234_567)) == ("u", "s", 1_234_567)


@pytest.mark.parametrize(
    "key",
    [
        "conversations/user-1/000006.json",
        "conversations/user-1/sess-1/x/000006.json",
        "conversations/user-1/sess-1/6.json",
        "conversations/user-1/sess-1/000006.txt",
        "conversations//sess-1/000006.json",
        "conversations/user-1//000006.json",
        "assistants/user-1/sess-1/000006.json",
        "",
    ],
)
def test_parse_archive_key_refuses_anything_the_archive_did_not_write(key):
    assert parse_archive_key(key) is None


def test_from_json_round_trips_to_json():
    turn = _turn(project_id="proj-1", assistant_id="ast-1")
    assert ArchivedTurn.from_json(turn.to_json()) == turn


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"schemaVersion": 2, "userId": "u", "sessionId": "s", "messageIndex": 0, "createdAt": "t"}',
        b'{"schemaVersion": 1, "userId": "u", "sessionId": "s", "createdAt": "t"}',
        b'{"schemaVersion": 1, "userId": "u", "sessionId": "s", "messageIndex": "0", "createdAt": "t"}',
    ],
)
def test_from_json_refuses_unknown_or_incomplete_bodies(body):
    with pytest.raises(ValueError):
        ArchivedTurn.from_json(body)


# ── Index documents ──────────────────────────────────────────────────────────
def test_document_id_is_user_session_and_unpadded_index():
    assert index_document_id("user-1", "sess-1", 6) == "conv#user-1#sess-1#6"
    ref = parse_index_document_id("conv#user-1#sess-1#6")
    assert ref == ("user-1", "sess-1", 6)
    assert (ref.user_id, ref.session_id, ref.message_index) == ("user-1", "sess-1", 6)


def test_two_users_with_the_same_session_id_get_different_documents():
    """Session ids are not unique across users (dev, 2026-10-07): one user's turn
    must never replace, or be deleted with, another user's."""
    assert index_document_id("user-a", "shared", 0) != index_document_id("user-b", "shared", 0)


@pytest.mark.parametrize(
    "value",
    ["sess-1#6", "conv#sess-1", "conv#sess-1#6", "conv##s#6", "conv#u##6", "conv#u#s#x", "conv#u#s#1#2", "doc-123"],
)
def test_parse_document_id_refuses_other_ids(value):
    assert parse_index_document_id(value) is None


@pytest.mark.parametrize("value", ["conv#sess-1#6", "conv#11111111-aaaa#0"])
def test_first_format_ids_are_recognised_as_legacy(value):
    assert is_legacy_index_document_id(value)


@pytest.mark.parametrize("value", ["conv#u#sess-1#6", "conv#sess-1#x", "conv##6", "doc-123", "sess-1#6"])
def test_other_ids_are_not_legacy(value):
    assert not is_legacy_index_document_id(value)


@pytest.mark.parametrize("user_id,session_id", [("a#b", "s"), ("", "s"), ("u", "a#b"), ("u", "")])
def test_document_id_refuses_parts_that_would_not_parse_back(user_id, session_id):
    with pytest.raises(ValueError):
        index_document_id(user_id, session_id, 0)


def test_text_is_user_then_assistant_without_a_dangling_separator():
    assert index_text(_turn()) == "the syllabus rewrite\n\nHere is the revised syllabus."
    assert index_text(_turn(assistant_text="")) == "the syllabus rewrite"


def test_attributes_are_strings_and_omit_unset_scope():
    assert index_attributes(_turn()) == {
        "user_id": "user-1",
        "session_id": "sess-1",
        "message_index": "6",
        "created_at": "2026-10-07T12:00:00Z",
    }
    scoped = index_attributes(_turn(project_id="proj-1", assistant_id="ast-1"))
    assert scoped["project_id"] == "proj-1" and scoped["assistant_id"] == "ast-1"


def test_a_body_must_agree_with_its_key():
    turn = _turn()
    assert turn_matches_key(turn, "user-1", "sess-1", 6)
    assert not turn_matches_key(turn, "user-2", "sess-1", 6)
    assert not turn_matches_key(turn, "user-1", "sess-2", 6)
    assert not turn_matches_key(turn, "user-1", "sess-1", 8)


# ── The reserved knowledge base ──────────────────────────────────────────────
def test_reserved_ids_match_exactly():
    assert is_reserved_kb_id(CONVERSATIONS_KB_ID)
    assert not is_reserved_kb_id("conversations-1")
    assert not is_reserved_kb_id("ast-0123456789ab")
    assert not is_reserved_kb_id(None)


def test_generated_assistant_ids_can_never_be_reserved():
    from apis.shared.assistants.service import _generate_assistant_id

    assert not any(is_reserved_kb_id(_generate_assistant_id()) for _ in range(50))


def test_request_teardown_refuses_the_conversations_kb(assistants_table):
    r.create_provisioning(
        CONVERSATIONS_KB_ID,
        provisioning.new_managed_kb_record(CONVERSATIONS_KB_ID, "system", client_token="t" * 40),
    )

    assert r.request_teardown(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID, "2026-10-07T12:00:00Z") is None

    record = r.get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)
    assert "migrationState" not in record
    assert "GSI7_PK" not in record


# ── Extra tags on provisioning ───────────────────────────────────────────────
def test_extra_tags_add_to_the_contract_tags():
    tags = provisioning._with_extra_tags({"ManagedKbAppKbId": "x"}, {"purpose": "conversation-search"})
    assert tags == {"ManagedKbAppKbId": "x", "purpose": "conversation-search"}


def test_extra_tags_cannot_override_a_contract_key():
    with pytest.raises(ValueError):
        provisioning._with_extra_tags({"ManagedKbAppKbId": "x"}, {"ManagedKbAppKbId": "y"})

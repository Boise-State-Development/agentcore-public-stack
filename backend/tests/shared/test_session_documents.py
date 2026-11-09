"""Which session document the model meant (``apis.shared.files.session_documents``).

Shared by ``verify_sources`` and ``document_extract``. Pure, so the rules are
tested here once and each tool's suite tests only how it uses them.
"""

from __future__ import annotations

from apis.shared.files.session_documents import (
    MAX_LISTED_DOCUMENTS,
    document_choices,
    filename_keys,
    match_session_document,
    name_key,
)


def _doc(upload_id: str, filename: str) -> dict:
    return {"upload_id": upload_id, "filename": filename}


DOCS = [_doc("up-a", "facilities_policy.md"), _doc("up-b", "Budget Justification.pdf")]


def test_every_spelling_of_a_filename_shares_a_key() -> None:
    keys = filename_keys("facilities_policy.md")
    for spelling in ("facilities_policy.md", "facilities_policy_md", "Facilities Policy"):
        assert name_key(spelling) in keys


def test_an_exact_id_is_itself() -> None:
    assert match_session_document(DOCS, "up-b") == "up-b"


def test_a_filename_is_its_document() -> None:
    assert match_session_document(DOCS, "budget justification") == "up-b"
    assert match_session_document(DOCS, "facilities_policy_md") == "up-a"


def test_an_unknown_name_among_several_is_unresolved() -> None:
    assert match_session_document(DOCS, "nsf23603") is None


def test_the_only_document_answers_any_single_request() -> None:
    only = [_doc("up-only", "solicitation.md")]
    assert match_session_document(only, "nsf23603") == "up-only"
    assert match_session_document(only, "") == "up-only"


def test_but_not_one_of_several_requests() -> None:
    only = [_doc("up-only", "solicitation.md")]
    assert match_session_document(only, "nsf23603", only_request=False) is None


def test_an_ambiguous_filename_is_never_guessed() -> None:
    twins = [_doc("up-1", "budget.md"), _doc("up-2", "budget.md")]
    assert match_session_document(twins, "budget.md") is None


def test_no_documents_resolves_nothing() -> None:
    assert match_session_document([], "anything") is None


def test_the_choices_are_bounded() -> None:
    many = [_doc(f"up-{i}", f"doc-{i}.md") for i in range(MAX_LISTED_DOCUMENTS + 3)]
    text = document_choices(many)
    assert text.count("'up-") == MAX_LISTED_DOCUMENTS
    assert "and 3 more" in text

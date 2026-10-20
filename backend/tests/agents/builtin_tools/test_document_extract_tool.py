"""``document_extract`` end to end, with the model call and the writers faked.

``docs/specs/structured-extraction.md`` §5, §8, §9. What is under test is the
tool's own contract: what reaches the model, what reaches the side channel,
what the failures say, and the one subtlety a reader would not guess — that
``pages`` narrows what is *extracted* but never what provenance is verified
against.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

import agents.builtin_tools.document_extract_tool as tool_module
from agents.builtin_tools.document_extract_tool import make_document_extract_tool

FIELD_SET = """---
title: NSF Solicitation
version: 4
---

| name | title | type | find | enum |
|---|---|---|---|---|
| deadline | Submission deadline | date | the deadline | |
| budget_cap | Budget cap | currency | the cap | |
"""

PAGES = [
    "Full proposals are due March 3, 2026.",
    "Awards will be made for up to $500,000 in total costs.",
    "Appendix A: reporting requirements.",
]


class FakeRef:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.kind = "reference"
        self.s3_key = f"skills/grants/references/{filename}"


class FakeRecord:
    skill_id = "grants"
    resources = [FakeRef("field-set.nsf-solicitation.md")]


class FakeMeta:
    upload_id = "up-1"
    filename = "nsf-26-501.pdf"


class FakeToolContext:
    tool_use = {"toolUseId": "tu-7"}


@pytest.fixture
def wired(monkeypatch):
    """Everything outside the tool faked at its own seam."""
    state: Dict[str, Any] = {"persisted": [], "queued": [], "extract_text": None}

    monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("DOCUMENT_EXTRACT_MODEL_ID", "us.test-model")

    class Store:
        def get(self, key: str) -> bytes:
            return FIELD_SET.encode("utf-8")

    import apis.shared.extraction.resolver as resolver

    monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Store())

    async def fake_units(user_id, session_id, upload_id):
        if upload_id == "missing":
            from apis.shared.files.document_read import DocumentReadError

            raise DocumentReadError("no such document")
        return FakeMeta(), "pdf", list(PAGES)

    monkeypatch.setattr(tool_module, "document_provenance_units", fake_units)

    from apis.shared.extraction.extractor import ExtractionResult

    async def fake_extract(field_set, document_text, *, filename, model_id):
        state["extract_text"] = document_text
        if state.get("extract_raises"):
            raise state["extract_raises"]
        return ExtractionResult(
            rows=state.get(
                "rows",
                {
                    "deadline": {
                        "value": "2026-03-03",
                        "quote": "due March 3, 2026",
                    },
                    "budget_cap": {
                        "value": "$500,000",
                        "quote": "up to $500,000 in total costs",
                    },
                },
            ),
            schema_enforced=state.get("enforced", True),
            model_id=model_id,
            usage={"inputTokens": 5000, "outputTokens": 200},
        )

    monkeypatch.setattr(tool_module, "extract_fields", fake_extract)

    async def fake_artifact(**kwargs):
        state["artifact"] = kwargs
        return "art-1"

    async def fake_workbook(**kwargs):
        state["workbook"] = kwargs
        return ("dl-1", "4.2 KB")

    async def fake_persist(user_id, session_id, tool_use_id, payload):
        state["persisted"].append(payload)

    async def fake_meter(**kwargs):
        state["metered"] = kwargs

    monkeypatch.setattr(tool_module, "_write_artifact", fake_artifact)
    monkeypatch.setattr(tool_module, "_write_workbook", fake_workbook)
    monkeypatch.setattr(tool_module, "_persist", fake_persist)
    monkeypatch.setattr(tool_module, "_meter", fake_meter)
    monkeypatch.setattr(
        tool_module, "queue_extraction", lambda u, s, p: state["queued"].append(p)
    )
    monkeypatch.setattr(tool_module, "record_document_extract", lambda *a, **k: None)
    return state


@pytest.mark.asyncio
async def test_a_run_returns_values_and_states_to_the_model(wired) -> None:
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    result = await fn(
        upload_id="up-1",
        field_set="nsf-solicitation",
        tool_context=FakeToolContext(),
    )
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    by_name = {f["name"]: f for f in payload["fields"]}
    assert by_name["budget_cap"]["value"] == "$500,000"
    assert by_name["budget_cap"]["state"] == "supported"
    assert payload["artifactId"] == "art-1"


@pytest.mark.asyncio
async def test_no_provenance_detail_reaches_the_model(wired) -> None:
    """§5.1, D8. The tool result IS the Converse payload and IS in the
    cacheable prefix."""
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    result = await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    serialized = json.dumps(result["content"][0]["json"])
    for leaked in ("up to $500,000 in total costs", "p. 2", "measured", "literal"):
        assert leaked not in serialized


@pytest.mark.asyncio
async def test_the_model_is_told_the_table_is_already_on_screen(wired) -> None:
    """The ``_download_card`` precedent: without it the model helpfully
    re-types the table and invents a download link."""
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    result = await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    note = result["content"][0]["json"]["note"]
    assert "already displayed" in note
    assert "Do not re-type the table" in note
    assert "spreadsheet" in note


@pytest.mark.asyncio
async def test_the_side_channel_gets_the_full_rows(wired) -> None:
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    [queued] = wired["queued"]
    assert queued["toolUseId"] == "tu-7"
    assert queued["fieldSet"] == "nsf-solicitation"
    assert queued["fieldSetVersion"] == "4"
    assert queued["downloadUploadId"] == "dl-1"
    budget = next(f for f in queued["fields"] if f["name"] == "budget_cap")
    assert budget["quote"] == "up to $500,000 in total costs"
    assert budget["locator"]["value"] == 2


@pytest.mark.asyncio
async def test_the_row_is_persisted_before_the_broker_handoff(wired) -> None:
    """The emit loop may never reach the payload on a cancelled turn, and the
    row is what survives a reload."""
    order: List[str] = []

    async def persist(user_id, session_id, tool_use_id, payload):
        order.append("persist")

    import agents.builtin_tools.document_extract_tool as m

    m._persist = persist
    m.queue_extraction = lambda u, s, p: order.append("queue")

    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    assert order == ["persist", "queue"]


@pytest.mark.asyncio
async def test_the_extraction_call_is_metered(wired) -> None:
    """D12. Unlike its side-channel neighbours, this call's input scales with
    the document, so it is billed like any other call."""
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    assert wired["metered"]["model_id"] == "us.test-model"
    assert wired["metered"]["usage"]["inputTokens"] == 5000


class TestPagesNarrowsExtractionNotVerification:
    """The subtlety a reader would not guess, and the one that would produce a
    *manufactured* hallucination signal if it were wrong."""

    @pytest.mark.asyncio
    async def test_pages_narrows_what_is_sent_to_the_model(self, wired) -> None:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        await fn(
            upload_id="up-1",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
            pages="2",
        )
        assert "Awards will be made" in wired["extract_text"]
        assert "Full proposals are due" not in wired["extract_text"]

    @pytest.mark.asyncio
    async def test_provenance_still_searches_the_whole_document(self, wired) -> None:
        """A quote the model took from page 1 of a run narrowed to page 2 is
        still a real passage of this document. Searching only the selection
        would report it ``unverified`` — inventing a hallucination signal,
        which is the one error this must never produce.
        """
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
            pages="2",
        )
        by_name = {f["name"]: f for f in result["content"][0]["json"]["fields"]}
        # `deadline`'s quote is on page 1, outside the extraction selection.
        assert by_name["deadline"]["state"] == "supported"
        [queued] = wired["queued"]
        deadline = next(f for f in queued["fields"] if f["name"] == "deadline")
        assert deadline["locator"]["value"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pages,expected_fragment",
        [
            ("1-2", "Full proposals"),
            ("1,3", "Appendix A"),
            ("99", "Full proposals"),   # out of range -> whole document
            ("nonsense", "Full proposals"),
            (None, "Full proposals"),
        ],
    )
    async def test_page_selections_are_parsed_forgivingly(
        self, wired, pages, expected_fragment
    ) -> None:
        """A model guessing "pages 1-5" at a three-page document means "the
        front of it"; failing the call over the fourth would be pedantry at the
        user's expense."""
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        await fn(
            upload_id="up-1",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
            pages=pages,
        )
        assert expected_fragment in wired["extract_text"]


class TestFailuresAreConversational:
    @pytest.mark.asyncio
    async def test_an_unknown_field_set_names_what_is_available(self, wired) -> None:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1", field_set="nope", tool_context=FakeToolContext()
        )
        assert result["status"] == "error"
        assert "nsf-solicitation" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_missing_field_set_argument_is_refused_early(self, wired) -> None:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1", field_set="  ", tool_context=FakeToolContext()
        )
        assert result["status"] == "error"
        assert "nsf-solicitation" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_the_set_is_resolved_before_the_document_is_read(
        self, wired, monkeypatch
    ) -> None:
        """A mistyped set name costs nothing to correct; reading a 300-page
        package first would spend an S3 GET to find that out."""
        read = {"called": False}

        async def never(user_id, session_id, upload_id):
            read["called"] = True
            raise AssertionError("should not be reached")

        monkeypatch.setattr(tool_module, "document_provenance_units", never)
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1", field_set="nope", tool_context=FakeToolContext()
        )
        assert result["status"] == "error"
        assert read["called"] is False

    @pytest.mark.asyncio
    async def test_a_malformed_field_set_blames_the_author_not_the_model(
        self, wired, monkeypatch
    ) -> None:
        """A parse failure is the author's mistake and a missing name is the
        model's. Each needs a different person to read the message."""
        import apis.shared.extraction.resolver as resolver

        class Broken:
            def get(self, key: str) -> bytes:
                return b"---\ntitle: X\n---\n\nno table\n"

        monkeypatch.setattr(resolver, "get_skill_resource_store", lambda: Broken())
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
        )
        assert result["status"] == "error"
        text = result["content"][0]["text"]
        assert "not with your request" in text
        assert "maintains the skill" in text

    @pytest.mark.asyncio
    async def test_an_unreadable_document_is_an_error_not_a_crash(self, wired) -> None:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="missing",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
        )
        assert result["status"] == "error"
        assert "no such document" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_failed_extraction_says_it_did_not_run(self, wired) -> None:
        from apis.shared.extraction.extractor import ExtractionError

        wired["extract_raises"] = ExtractionError("grammar never compiled")
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_id="up-1",
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
        )
        assert result["status"] == "error"
        assert "did not run" in result["content"][0]["text"]

    def test_the_tool_requires_an_identity_to_bind_to(self) -> None:
        with pytest.raises(ValueError):
            make_document_extract_tool("", "u1", [FakeRecord()])
        with pytest.raises(ValueError):
            make_document_extract_tool("s1", "", [FakeRecord()])


@pytest.mark.asyncio
async def test_an_unenforced_run_is_disclosed_everywhere(wired) -> None:
    """D11: the marker reaches the model, the event and the writers."""
    wired["enforced"] = False
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    result = await fn(
        upload_id="up-1", field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    assert result["content"][0]["json"]["schemaEnforced"] is False
    assert wired["queued"][0]["schemaEnforced"] is False
    assert wired["artifact"]["content"].count("not enforced") >= 1

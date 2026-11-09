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
from apis.shared.extraction.batch import MAX_DOCUMENTS

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
    def __init__(self) -> None:
        self.upload_id = "up-1"
        self.filename = "nsf-26-501.pdf"


class FakeToolContext:
    tool_use = {"toolUseId": "tu-7"}


@pytest.fixture
def wired(monkeypatch):
    """Everything outside the tool faked at its own seam."""
    state: Dict[str, Any] = {
        "persisted": [],
        "queued": [],
        "extract_text": None,
        # The session's documents, as ``list_session_documents`` returns them.
        # Empty by default, so an id passes through exactly as before.
        "documents": [],
        "listings": 0,
    }

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
        if upload_id.startswith("ghost") or upload_id.endswith((".md", ".pdf")):
            from apis.shared.files.workspace import WorkspaceFileNotFoundError

            raise WorkspaceFileNotFoundError(f"No file with id '{upload_id}'")
        meta = FakeMeta()
        meta.upload_id = upload_id
        meta.filename = (
            "nsf-26-501.pdf" if upload_id == "up-1" else f"{upload_id}.pdf"
        )
        return meta, "pdf", list(PAGES)

    monkeypatch.setattr(tool_module, "document_provenance_units", fake_units)

    async def fake_listing(user_id, session_id):
        state["listings"] += 1
        if state.get("listing_raises"):
            raise state["listing_raises"]
        return {"documents": list(state["documents"]), "count": len(state["documents"])}

    monkeypatch.setattr(tool_module, "list_session_documents", fake_listing)

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
        upload_ids=["up-1"],
        field_set="nsf-solicitation",
        tool_context=FakeToolContext(),
    )
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    by_name = {f["name"]: f for f in payload["documents"][0]["fields"]}
    assert by_name["budget_cap"]["value"] == "$500,000"
    assert by_name["budget_cap"]["state"] == "supported"
    assert payload["artifactId"] == "art-1"
    assert payload["documentCount"] == 1


@pytest.mark.asyncio
async def test_no_provenance_detail_reaches_the_model(wired) -> None:
    """§5.1, D8. The tool result IS the Converse payload and IS in the
    cacheable prefix."""
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    result = await fn(
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
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
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
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
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
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
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    assert order == ["persist", "queue"]


@pytest.mark.asyncio
async def test_the_extraction_call_is_metered(wired) -> None:
    """D12. Unlike its side-channel neighbours, this call's input scales with
    the document, so it is billed like any other call."""
    tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
    fn = getattr(tool, "original_function", None) or tool
    await fn(
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
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
            upload_ids=["up-1"],
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
            upload_ids=["up-1"],
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
            pages="2",
        )
        by_name = {
            f["name"]: f for f in result["content"][0]["json"]["documents"][0]["fields"]
        }
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
            upload_ids=["up-1"],
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
            upload_ids=["up-1"], field_set="nope", tool_context=FakeToolContext()
        )
        assert result["status"] == "error"
        assert "nsf-solicitation" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_a_missing_field_set_argument_is_refused_early(self, wired) -> None:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_ids=["up-1"], field_set="  ", tool_context=FakeToolContext()
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
            upload_ids=["up-1"], field_set="nope", tool_context=FakeToolContext()
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
            upload_ids=["up-1"],
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
            upload_ids=["missing"],
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
            upload_ids=["up-1"],
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
        )
        assert result["status"] == "error"
        text = result["content"][0]["text"]
        # The batch form: it says how many of how many failed, and why, rather
        # than one undifferentiated error — a reason is reusable, a failure
        # count is not.
        assert "No document could be extracted" in text
        assert "1 of 1" in text
        assert "grammar never compiled" in text

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
        upload_ids=["up-1"], field_set="nsf-solicitation", tool_context=FakeToolContext()
    )
    assert result["content"][0]["json"]["schemaEnforced"] is False
    assert result["content"][0]["json"]["documents"][0]["schemaEnforced"] is False
    assert wired["queued"][0]["schemaEnforced"] is False
    assert wired["artifact"]["content"].count("not enforced") >= 1


class TestNDocumentsPerCall:
    """§17. The whole stack in one call, because the useful answer is a
    judgement across the set."""

    async def run(self, ids, **kw):
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        return await fn(
            upload_ids=ids,
            field_set="nsf-solicitation",
            tool_context=FakeToolContext(),
            **kw,
        )

    @pytest.mark.asyncio
    async def test_every_document_comes_back(self, wired) -> None:
        result = await self.run(["a", "b", "c"])
        payload = result["content"][0]["json"]
        assert payload["documentCount"] == 3
        assert [d["uploadId"] for d in payload["documents"]] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_one_artifact_and_one_workbook_for_the_whole_batch(
        self, wired
    ) -> None:
        """Not N of each (§17.2): the deliverable is the comparison, and a
        reviewer handed five tables has been handed the work of joining them."""
        await self.run(["a", "b", "c", "d", "e"])
        assert "5 documents" in wired["artifact"]["title"]
        assert wired["workbook"]["filename"] == "nsf-solicitation-5-documents.xlsx"
        # One of each, despite five documents.
        assert len([k for k in wired if k == "artifact"]) == 1

    @pytest.mark.asyncio
    async def test_the_comparison_table_is_in_the_artifact(self, wired) -> None:
        await self.run(["a", "b"])
        content = wired["artifact"]["content"]
        assert "## Comparison" in content
        assert "a.pdf" in content and "b.pdf" in content

    @pytest.mark.asyncio
    async def test_a_single_document_gets_no_comparison_table(self, wired) -> None:
        """Nothing to compare it with, and a one-column comparison is noise."""
        await self.run(["only"])
        assert "## Comparison" not in wired["artifact"]["content"]

    @pytest.mark.asyncio
    async def test_one_side_channel_payload_per_document(self, wired) -> None:
        await self.run(["a", "b", "c"])
        assert [p["uploadId"] for p in wired["queued"]] == ["a", "b", "c"]
        assert [p["uploadId"] for p in wired["persisted"]] == ["a", "b", "c"]
        # All share the call's toolUseId, and each carries the batch's size.
        assert {p["toolUseId"] for p in wired["queued"]} == {"tu-7"}
        assert {p["documentCount"] for p in wired["queued"]} == {3}

    @pytest.mark.asyncio
    async def test_the_batch_is_metered_once_with_the_summed_usage(
        self, wired
    ) -> None:
        """Per-document calls are one logical unit of work; their tokens add."""
        await self.run(["a", "b", "c"])
        assert wired["metered"]["usage"]["inputTokens"] == 15000  # 3 x 5000
        assert wired["metered"]["usage"]["outputTokens"] == 600

    @pytest.mark.asyncio
    async def test_a_duplicate_id_is_extracted_once(self, wired) -> None:
        """Extracting the same document twice would double its cost and put it
        in the comparison twice."""
        payload = (await self.run(["a", "b", "a"]))["content"][0]["json"]
        assert [d["uploadId"] for d in payload["documents"]] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_bare_string_is_accepted_as_one_document(self, wired) -> None:
        """An unenforced tool argument guarantees nothing (§6.2); a model that
        sends a string meant one document and should get it."""
        payload = (await self.run("a"))["content"][0]["json"]
        assert [d["uploadId"] for d in payload["documents"]] == ["a"]

    @pytest.mark.asyncio
    async def test_a_comma_separated_string_is_split(self, wired) -> None:
        payload = (await self.run("a, b ,c"))["content"][0]["json"]
        assert [d["uploadId"] for d in payload["documents"]] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_no_ids_in_a_conversation_without_documents_is_refused(
        self, wired
    ) -> None:
        result = await self.run([])
        assert result["status"] == "error"
        assert "no readable document" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_too_many_documents_is_refused_with_the_count(self, wired) -> None:
        """Refused rather than truncated: a set quietly reduced from sixty to
        fifty produces a confident judgement about the wrong set."""
        result = await self.run([f"d{i}" for i in range(MAX_DOCUMENTS + 5)])
        assert result["status"] == "error"
        assert str(MAX_DOCUMENTS + 5) in result["content"][0]["text"]
        assert "batches" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_one_unreadable_document_does_not_sink_the_batch(
        self, wired
    ) -> None:
        payload = (await self.run(["a", "missing", "c"]))["content"][0]["json"]
        assert payload["documentCount"] == 2
        assert [d["uploadId"] for d in payload["documents"]] == ["a", "c"]

    @pytest.mark.asyncio
    async def test_a_failed_document_is_named_in_the_note(self, wired) -> None:
        """A set where one failed silently is a judgement on two documents
        presented as a judgement on three."""
        payload = (await self.run(["a", "missing", "c"]))["content"][0]["json"]
        assert "1 of 3 documents could not be extracted" in payload["note"]
        assert "no such document" in payload["note"]

    @pytest.mark.asyncio
    async def test_failures_are_listed_in_the_artifact(self, wired) -> None:
        await self.run(["a", "missing"])
        content = wired["artifact"]["content"]
        assert "could not be extracted" in content
        assert "no such document" in content

    @pytest.mark.asyncio
    async def test_a_large_batch_is_told_to_work_field_by_field(self, wired) -> None:
        """Straight out of §17.7: every miss above ten documents is a
        cross-document fact going unnoticed."""
        payload = (await self.run([f"d{i}" for i in range(12)]))["content"][0]["json"]
        assert "one FIELD at a time" in payload["note"]

    @pytest.mark.asyncio
    async def test_a_small_batch_is_not(self, wired) -> None:
        payload = (await self.run(["a", "b"]))["content"][0]["json"]
        assert "one FIELD at a time" not in payload["note"]

    @pytest.mark.asyncio
    async def test_the_model_is_told_to_compare_rather_than_summarise(
        self, wired
    ) -> None:
        payload = (await self.run(["a", "b", "c"]))["content"][0]["json"]
        assert "which documents disagree or collide" in payload["note"]

    @pytest.mark.asyncio
    async def test_still_no_provenance_reaches_the_model(self, wired) -> None:
        """The per-document bound from PR-2 has to survive batching."""
        payload = (await self.run(["a", "b", "c"]))["content"][0]["json"]
        serialized = json.dumps(payload["documents"])
        for leaked in ("up to $500,000 in total costs", "p. 2", "measured", "literal"):
            assert leaked not in serialized


def _doc(upload_id: str, filename: str) -> Dict[str, Any]:
    return {"upload_id": upload_id, "filename": filename, "format": "md"}


class TestWhichDocuments:
    """The model often has no upload id to give.

    A small attachment reaches it as an inline block named by filename, so it
    sends the filename — on dev it did exactly that, failed, listed the
    documents, and only then ran. The rules are shared with ``verify_sources``
    (``apis.shared.files.session_documents``).
    """

    @staticmethod
    async def run(ids: Any) -> Dict[str, Any]:
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        return await fn(
            upload_ids=ids, field_set="nsf-solicitation", tool_context=FakeToolContext()
        )

    @staticmethod
    def extracted(result: Dict[str, Any]) -> List[str]:
        return [d["uploadId"] for d in result["content"][0]["json"]["documents"]]

    @pytest.mark.asyncio
    async def test_filenames_resolve_to_their_documents(self, wired) -> None:
        wired["documents"] = [
            _doc("up-a", "arid-lands-water-solicitation.md"),
            _doc("up-c", "coastal-resilience-solicitation.md"),
        ]
        result = await self.run(
            ["arid-lands-water-solicitation.md", "coastal-resilience-solicitation.md"]
        )
        assert self.extracted(result) == ["up-a", "up-c"]

    @pytest.mark.asyncio
    async def test_the_inline_block_name_resolves_too(self, wired) -> None:
        """Bedrock's sanitizer turns ``coastal.md`` into ``coastal_md``."""
        wired["documents"] = [_doc("up-c", "coastal.md"), _doc("up-a", "arid.md")]
        assert self.extracted(await self.run(["coastal_md", "Arid"])) == ["up-c", "up-a"]

    @pytest.mark.asyncio
    async def test_a_real_id_resolves_as_itself(self, wired) -> None:
        wired["documents"] = [_doc("up-a", "a.md"), _doc("up-b", "b.md")]
        assert self.extracted(await self.run(["up-b"])) == ["up-b"]

    @pytest.mark.asyncio
    async def test_an_id_and_its_filename_are_one_document(self, wired) -> None:
        """Extracting one document twice doubles its cost and its comparison row."""
        wired["documents"] = [_doc("up-a", "a.md"), _doc("up-b", "b.md")]
        assert self.extracted(await self.run(["up-a", "a.md", "b.md"])) == [
            "up-a",
            "up-b",
        ]

    @pytest.mark.asyncio
    async def test_no_ids_in_a_one_document_conversation_means_that_one(
        self, wired
    ) -> None:
        wired["documents"] = [_doc("up-only", "solicitation.md")]
        assert self.extracted(await self.run([])) == ["up-only"]

    @pytest.mark.asyncio
    async def test_no_ids_among_several_documents_asks_which(self, wired) -> None:
        """Never "all of them": the cost of a run scales with each document."""
        wired["documents"] = [_doc("up-a", "a.md"), _doc("up-b", "b.md")]
        result = await self.run([])
        text = result["content"][0]["text"]
        assert result["status"] == "error"
        assert "'up-a' (a.md)" in text and "'up-b' (b.md)" in text

    @pytest.mark.asyncio
    async def test_one_wrong_name_in_a_one_document_conversation_is_that_one(
        self, wired
    ) -> None:
        wired["documents"] = [_doc("up-only", "solicitation.md")]
        assert self.extracted(await self.run(["nsf23603"])) == ["up-only"]

    @pytest.mark.asyncio
    async def test_several_wrong_names_are_not_all_the_only_document(
        self, wired
    ) -> None:
        """Three names in a one-document session did not mean it three times."""
        wired["documents"] = [_doc("up-only", "solicitation.md")]
        result = await self.run(["ghost-1", "ghost-2"])
        assert result["status"] == "error"
        assert "'up-only' (solicitation.md)" in result["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_an_ambiguous_filename_is_not_guessed(self, wired) -> None:
        wired["documents"] = [
            _doc("up-1", "budget.md"),
            _doc("up-2", "budget.md"),
            _doc("up-3", "other.md"),
        ]
        result = await self.run(["budget.md", "other.md"])
        payload = result["content"][0]["json"]
        assert [d["uploadId"] for d in payload["documents"]] == ["up-3"]
        # The failure names the real documents, once, so the next call can pick.
        assert payload["note"].count("This conversation's documents are") == 1
        assert "'up-1' (budget.md)" in payload["note"]

    @pytest.mark.asyncio
    async def test_an_unknown_name_lists_the_documents_in_the_note(self, wired) -> None:
        wired["documents"] = [_doc("up-a", "a.md"), _doc("up-b", "b.md")]
        result = await self.run(["a.md", "ghost"])
        note = result["content"][0]["json"]["note"]
        assert self.extracted(result) == ["up-a"]
        assert "ghost: no document with that id or filename" in note
        assert "'up-b' (b.md)" in note

    @pytest.mark.asyncio
    async def test_the_listing_is_asked_once_per_call(self, wired) -> None:
        wired["documents"] = [_doc("up-a", "a.md"), _doc("up-b", "b.md")]
        await self.run(["a.md", "b.md"])
        assert wired["listings"] == 1

    @pytest.mark.asyncio
    async def test_a_failed_listing_passes_ids_through(self, wired) -> None:
        """The listing is a convenience; losing it must not cost the run."""
        wired["listing_raises"] = RuntimeError("dynamodb down")
        assert self.extracted(await self.run(["a", "b"])) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_skill_name_runs_its_only_field_set(self, wired) -> None:
        """Seen on dev: the model named the skill, not the set inside it."""
        tool = make_document_extract_tool("s1", "u1", [FakeRecord()])
        fn = getattr(tool, "original_function", None) or tool
        result = await fn(
            upload_ids=["up-1"], field_set="grants", tool_context=FakeToolContext()
        )
        assert result["status"] == "success"
        assert result["content"][0]["json"]["fieldSet"] == "nsf-solicitation"


#: The agent-facing spec sits in the cacheable ``toolConfig``, ahead of the
#: system prompt and the whole restored history. A change here is a deliberate
#: prefix rewrite at deploy: update the pin with it and state the token delta
#: in the PR. Last changed when ``upload_ids`` started accepting filenames
#: (666 tokens on Haiku 4.5, 6 fewer than before).
SPEC_DIGEST = "0b778cd9490d355c19099a78ee5961bc97b91c8447347b80dde195fd9630ca8e"


def test_the_spec_is_pinned_and_constant_per_deployment() -> None:
    """Neither the identity nor the turn's skills reach the spec (D1)."""
    import hashlib

    def digest(tool: Any) -> str:
        return hashlib.sha256(
            json.dumps(tool.tool_spec, sort_keys=True).encode()
        ).hexdigest()

    one = make_document_extract_tool("s1", "u1", [FakeRecord()])
    other = make_document_extract_tool("s2", "u2", [])
    assert digest(one) == digest(other) == SPEC_DIGEST

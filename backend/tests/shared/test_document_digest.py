"""DocumentDigest (apis/shared/files/document_digest.py).

The deterministic outline (headings, counts, table/figure mentions, sampled
sections), the rendered block and its hard token budget — including the
spec's gate, a 200-page PDF digest under 1,500 tokens — the fail-open
abstract, and ``build_digest``'s never-raise contract. Fixtures are the same
hand-built PDFs and DOCX archives the document_read tests use.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.shared import aws_clients
from apis.shared.files import document_digest as dd
from tests.shared.test_document_read import build_docx, build_pdf

MODULE = "apis.shared.files.document_digest"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class TestHeadings:
    @pytest.mark.parametrize(
        "line,expected",
        [
            ("1. Introduction", True),
            ("2.3 Coverage Limits", True),
            ("ARTICLE IV", True),
            ("Section 12 Termination", True),
            ("DEFINITIONS", True),
            ("Named Insured And Limits", True),
            ("This policy covers the named insured for losses.", False),
            ("the quick brown fox jumps over the lazy dog again", False),
            ("ok", False),
            ("Total:", False),
        ],
    )
    def test_heuristic(self, line, expected):
        assert dd.looks_like_heading(line) is expected


class TestOutline:
    def test_pdf_outline_carries_page_numbers_and_structure_counts(self):
        pages = ["1. Declarations\nNamed insured Acme", "body text only here.", "2. Insuring Agreements\nsee Table 1 and Figure 2"]
        outline = dd.extract_outline("pdf", build_pdf(pages))
        assert (outline.unit, outline.count) == ("page", 3)
        assert [(s.start, s.title) for s in outline.sections] == [(1, "1. Declarations"), (3, "2. Insuring Agreements")]
        assert (outline.tables, outline.figures) == (1, 1)
        assert outline.chars > 0 and outline.abstract is None

    def test_pdf_without_headings_anchors_on_first_lines(self):
        outline = dd.extract_outline("pdf", build_pdf(["scanned text one.", "", "scanned text three."]))
        assert [(s.start, s.title) for s in outline.sections] == [(1, "scanned text one."), (3, "scanned text three.")]

    def test_docx_outline_uses_paragraph_numbers(self):
        raw = build_docx(["Executive Summary", "The budget grows by 4 million this year.", "Risks", "Some risk text."])
        outline = dd.extract_outline("docx", raw)
        assert (outline.unit, outline.count) == ("paragraph", 4)
        assert [(s.start, s.title) for s in outline.sections] == [(1, "Executive Summary"), (3, "Risks")]

    def test_markdown_headings_win_and_html_tags_are_stripped(self):
        md = dd.extract_outline("md", b"# Title\nbody\n## Second part\nmore")
        assert [(s.start, s.title) for s in md.sections] == [(1, "Title"), (3, "Second part")]
        html = dd.extract_outline("html", b"<h1>OVERVIEW</h1>\n<p>lower case body sentence.</p>")
        assert [s.title for s in html.sections] == ["OVERVIEW"]

    def test_sections_are_sampled_evenly_past_the_cap(self, monkeypatch):
        monkeypatch.setattr(dd, "DOCUMENT_DIGEST_MAX_SECTIONS", 5)
        outline = dd.extract_outline("pdf", build_pdf([f"{i}. Heading {i}" for i in range(1, 21)]))
        assert len(outline.sections) == 5
        assert [s.start for s in outline.sections] == [1, 5, 9, 13, 17]

    def test_text_sample_spreads_across_pages(self):
        sample = dd.text_sample("pdf", build_pdf(["first page words", "middle page words", "last page words"]), limit=200)
        assert "first" in sample and "middle" in sample and "last" in sample
        assert len(dd.text_sample("md", b"x" * 50_000, limit=100)) == 100


class TestDocumentText:
    """The text a TEXT-only model reads in place of an attached document."""

    def test_a_document_that_fits_comes_back_whole(self):
        out = dd.document_text("pdf", build_pdf(["first page", "second page"]), limit=1_000)
        assert out.text == "first page\n\nsecond page"
        assert out.truncated is False
        assert (out.unit, out.count) == ("page", 2)

    def test_a_long_document_comes_back_as_an_excerpt_within_the_limit(self):
        pages = [f"page {i} alpha beta gamma delta" for i in range(1, 61)]
        out = dd.document_text("pdf", build_pdf(pages), limit=400)
        assert out.truncated is True
        assert out.text.startswith("page 1 alpha")
        assert len(out.text) <= 400
        assert out.count == 60

    def test_docx_and_text_family(self):
        assert dd.document_text("docx", build_docx(["One", "Two"]), limit=100).text == "One\nTwo"
        out = dd.document_text("txt", b"a\n\nb", limit=100)
        assert (out.text, out.unit, out.truncated) == ("a\nb", "line", False)

    def test_a_page_with_no_text_layer_yields_empty_text(self):
        assert dd.document_text("pdf", build_pdf([""]), limit=100).text == ""


class TestRender:
    def _digest(self, sections=3, abstract="An abstract."):
        return dd.DocumentDigest(
            format="pdf", unit="page", count=47, tables=2, figures=0, abstract=abstract,
            sections=[dd.DigestSection(start=i + 1, title=f"Section {i + 1}") for i in range(sections)],
        )

    def test_block_shape(self):
        text = dd.render_digest(self._digest(), filename='Policy <"A&B">.pdf', upload_id="up-1")
        assert text.startswith('<document-digest name="Policy &lt;&quot;A&amp;B&quot;&gt;.pdf" upload_id="up-1" format="pdf" pages="47" tables="2">')
        assert "  <abstract>An abstract.</abstract>" in text
        assert '  <section page="3">Section 3</section>' in text
        assert text.endswith("</document-digest>")

    def test_budget_drops_sections_first_then_trims_the_abstract(self):
        digest = self._digest(sections=40, abstract="word " * 400)
        full = dd.render_digest(digest, filename="a.pdf", upload_id="u")
        assert dd.estimate_tokens(full) > 300
        tight = dd.render_digest(digest, filename="a.pdf", upload_id="u", budget_tokens=300)
        assert dd.estimate_tokens(tight) <= 300
        assert '<section page="1">' not in tight or "…" in tight
        tiny = dd.render_digest(digest, filename="a.pdf", upload_id="u", budget_tokens=40)
        assert dd.estimate_tokens(tiny) <= 40 + 4  # header always fits, may exceed a tiny budget by a line
        assert tiny.startswith("<document-digest") and "<section" not in tiny

    def test_two_hundred_page_pdf_digest_stays_under_the_spec_ceiling(self):
        pages = [f"{i}. A reasonably long heading for section number {i} of the policy\nbody body body" for i in range(1, 201)]
        outline = dd.extract_outline("pdf", build_pdf(pages))
        outline.abstract = "Sentence one. " * 40
        text = dd.render_digest(outline, filename="big.pdf", upload_id="u")
        assert outline.count == 200
        assert dd.estimate_tokens(text) <= dd.DOCUMENT_DIGEST_MAX_TOKENS == 1_500


def _bedrock(monkeypatch, text="A crisp abstract.", stop="end_turn", fail=False):
    client = MagicMock()
    if fail:
        client.converse.side_effect = RuntimeError("throttled")
    else:
        client.converse.return_value = {
            "stopReason": stop,
            "output": {"message": {"content": [{"text": text}]}},
        }
    boto3 = MagicMock()
    boto3.client.return_value = client
    monkeypatch.setitem(__import__("sys").modules, "boto3", boto3)
    # The client is cached per process; drop the previous fake so this one is built.
    aws_clients.reset_cached_clients()
    return client


@pytest.fixture(autouse=True)
def _fresh_bedrock_client():
    """The Bedrock client is cached per process; each test builds its own."""
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


class TestAbstract:
    @pytest.mark.asyncio
    async def test_calls_the_cheap_model_with_outline_and_sample(self, monkeypatch):
        client = _bedrock(monkeypatch, text="  Two\n sentences.  ")
        outline = dd.extract_outline("pdf", build_pdf(["1. Intro\nhello"]))
        result = await dd.generate_abstract(outline, "hello world", model_id="us.amazon.nova-micro-v1:0")
        assert result == "Two sentences."
        kwargs = client.converse.call_args.kwargs
        assert kwargs["modelId"] == "us.amazon.nova-micro-v1:0"
        prompt = kwargs["messages"][0]["content"][0]["text"]
        assert "- (page 1) 1. Intro" in prompt and "hello world" in prompt

    @pytest.mark.asyncio
    async def test_truncated_or_failed_generations_are_none(self, monkeypatch):
        _bedrock(monkeypatch, stop="max_tokens")
        assert await dd.generate_abstract(dd.DocumentDigest(), "text") is None
        _bedrock(monkeypatch, fail=True)
        assert await dd.generate_abstract(dd.DocumentDigest(), "text") is None
        assert await dd.generate_abstract(dd.DocumentDigest(), "   ") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop", ["guardrail_intervened", "content_filtered", "something_new", None])
    async def test_a_refused_generation_is_none(self, monkeypatch, stop):
        """Only a finished generation is an abstract; a refusal's text is not."""
        _bedrock(monkeypatch, text="Sorry, the model cannot answer this.", stop=stop)
        assert await dd.generate_abstract(dd.DocumentDigest(), "text") is None

    @pytest.mark.asyncio
    async def test_abstracts_reuse_one_bedrock_client(self, monkeypatch):
        """A client per upload paid botocore's model load and a fresh TLS handshake."""
        client = _bedrock(monkeypatch)
        factory = __import__("sys").modules["boto3"].client

        for _ in range(3):
            assert await dd.generate_abstract(dd.DocumentDigest(), "text") == "A crisp abstract."

        assert factory.call_count == 1
        assert client.converse.call_count == 3


class TestBuild:
    @pytest.mark.asyncio
    async def test_ready_digest_with_abstract_and_token_estimate(self, monkeypatch):
        _bedrock(monkeypatch, text="Policy abstract.")
        digest = await dd.build_digest(raw=build_pdf(["1. Intro\nbody", "2. Terms"]), mime_type="application/pdf", filename="p.pdf", upload_id="u1")
        assert digest.status == "ready" and digest.abstract == "Policy abstract."
        assert digest.model_id == dd.DOCUMENT_DIGEST_MODEL_ID
        assert digest.count == 2 and digest.tokens > 0 and digest.extractor_ms >= 0
        item = digest.to_item()
        assert item["version"] == dd.DIGEST_VERSION and "error" not in item
        assert dd.DocumentDigest.from_item(item).sections[1].title == "2. Terms"

    @pytest.mark.asyncio
    async def test_abstract_failure_keeps_the_outline(self, monkeypatch):
        _bedrock(monkeypatch, fail=True)
        digest = await dd.build_digest(raw=build_pdf(["1. Intro"]), mime_type="application/pdf", filename="p.pdf", upload_id="u1")
        assert digest.status == "ready" and digest.abstract is None and digest.model_id is None
        assert digest.sections[0].title == "1. Intro"

    @pytest.mark.asyncio
    async def test_extraction_failure_is_a_failed_digest_with_a_class_name_only(self):
        digest = await dd.build_digest(raw=b"not a pdf", mime_type="application/pdf", filename="p.pdf", upload_id="u1", with_abstract=False)
        assert digest.status == "failed" and digest.error == "DocumentReadError"
        assert "not a pdf" not in digest.to_item().values().__repr__()

    @pytest.mark.asyncio
    async def test_non_documents_are_refused(self):
        digest = await dd.build_digest(raw=b"a,b", mime_type="text/csv", filename="d.csv", upload_id="u1")
        assert (digest.status, digest.error) == ("failed", "NotADocument")

    def test_malformed_rows_read_as_no_digest(self):
        assert dd.DocumentDigest.from_item("nope") is None
        assert dd.DocumentDigest.from_item({"count": "many"}) is None

    def test_metric_is_content_free(self, monkeypatch):
        calls = []
        monkeypatch.setattr("apis.shared.observability.emf.emit_emf_metrics", lambda ns, metrics, properties=None, units=None: calls.append((ns, metrics, properties)))
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", lambda: True)
        dd.record_digest(dd.DocumentDigest(format="pdf", tokens=900, extractor_ms=1200, abstract="SECRET"))
        assert calls == [("AgentCoreStack/Compaction", {"DocumentDigestGenerated": 1, "DocumentDigestTokens": 900, "DocumentDigestMs": 1200}, {"format": "pdf", "outcome": "ready"})]
        assert "SECRET" not in repr(calls)

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "false")
        assert dd.document_digest_enabled() is False
        monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "")
        assert dd.document_digest_enabled() is True



class TestRenderBudgetIsHard:
    """``render_digest`` escaped the abstract *after* slicing it to the room
    that was left, so ``&`` -> ``&amp;`` could render up to 5x longer than the
    slice it was measured on. Measured: an all-``&`` abstract rendered 388
    tokens against a 100-token budget. The cap is the contract (spec PR-2
    decision #15 — ``digest.tokens`` is a stored fact per file), so it is now
    measured on the rendered text, and the abstract is dropped rather than
    allowed to overshoot.
    """

    def _digest(self, abstract: str) -> dd.DocumentDigest:
        return dd.DocumentDigest(
            status="ready", format="pdf", unit="page", count=10,
            abstract=abstract, sections=[dd.DigestSection(start=1, title="Intro")],
        )

    @pytest.mark.parametrize(
        "abstract",
        [
            "&" * 4000,                                  # worst case: every char expands 5x
            "<tier> & <tier> " * 400,
            "R&D spending rose. " * 300,
            "Ordinary prose with no entities at all. " * 200,
            "&amp; already-escaped-looking text " * 200,
        ],
        ids=["all-amps", "angle-and-amp", "r-and-d", "plain-prose", "looks-escaped"],
    )
    @pytest.mark.parametrize("budget", [40, 100, 400, 1_500])
    def test_never_exceeds_the_budget(self, abstract, budget):
        text = dd.render_digest(self._digest(abstract), filename="a.pdf", upload_id="u", budget_tokens=budget)
        assert dd.estimate_tokens(text) <= budget, dd.estimate_tokens(text)

    def test_entities_are_never_split(self):
        text = dd.render_digest(self._digest("R&D " * 500), filename="a.pdf", upload_id="u", budget_tokens=60)
        # A truncated "&amp;" would leave a bare "&" or a fragment like "&am".
        assert text.count("&") == text.count("&amp;")

    def test_the_handle_always_survives(self):
        """Only the opening tag may outlive a budget this small — it is what
        makes the document retrievable at all."""
        text = dd.render_digest(self._digest("&" * 4000), filename="a.pdf", upload_id="u-keep", budget_tokens=1)
        assert 'upload_id="u-keep"' in text

    def test_a_normal_digest_is_unchanged(self):
        digest = self._digest("A short, ordinary abstract of the policy.")
        text = dd.render_digest(digest, filename="a.pdf", upload_id="u", budget_tokens=1_500)
        assert "A short, ordinary abstract of the policy." in text
        assert "Intro" in text

"""The inference route renders a turn's non-PDF attachments off the event loop.

With ``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED`` on, the document-cap projection
sends every docx, txt, md, csv and html block to a Converse model as text. The
projection runs inside the model call, on the event loop, so the route renders
the new attachments in worker threads first and the projection only reads the
memo. These tests pin when that pre-render runs (flag on, Converse model, a
renderable attachment) and that it is what the projection later finds.
"""

import base64
import threading

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.routes import TurnAttachments, TurnModel, _prerender_documents_as_text
from apis.shared.models import document_cap
from tests.shared.test_document_read import build_docx


class _File:
    def __init__(self, filename: str, content_type: str, raw: bytes = b"x"):
        self.filename = filename
        self.content_type = content_type
        self.bytes = base64.b64encode(raw).decode()


def _model(provider="bedrock", model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0") -> TurnModel:
    return TurnModel(
        model_id=model_id,
        provider=provider,
        caching_enabled=None,
        inference_params={},
        mantle_api_mode=None,
        mantle_region=None,
    )


@pytest.fixture
def rendered(monkeypatch):
    """Record each pre-render's format and the thread it ran on."""
    calls = []

    def record(fmt, raw):
        calls.append((fmt, raw, threading.current_thread() is threading.main_thread()))
        return True

    monkeypatch.setattr(document_cap, "prerender_document_text", record)
    return calls


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", "true")


def _attachments(*files, profile=None) -> TurnAttachments:
    """The inline set as `_resolve_turn_attachments` plans it (model not yet known)."""
    from apis.shared.files.attachment_plan import MODE_NATIVE, current_limits_profile, plan_attachments

    profile = profile or current_limits_profile()
    facts = [routes._file_facts(f, {}) for f in files]
    plan = plan_attachments(facts, None, profile=profile)
    inline = plan.indices(MODE_NATIVE)
    return TurnAttachments(
        files_to_send=[files[i] for i in inline],
        marker_names=[f.filename for f in files],
        plan=plan,
        plan_facts=facts,
        inline_indices=inline,
        profile=profile,
    )


@pytest.mark.asyncio
async def test_off_by_default(monkeypatch, rendered):
    monkeypatch.delenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", raising=False)
    await _prerender_documents_as_text(_attachments(_File("a.docx", "application/octet-stream")), _model())
    assert rendered == []


@pytest.mark.asyncio
async def test_renders_each_renderable_attachment_in_a_worker_thread(flag_on, rendered):
    files = [
        _File("Minutes.DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"d"),
        _File("notes.md", "text/markdown", b"m"),
        _File("page.html", "text/html", b"h"),
        _File("data.csv", "text/csv", b"c"),
        _File("log.txt", "text/plain", b"t"),
        _File("scan.pdf", "application/pdf"),
        _File("photo.png", "image/png"),
        _File("old.doc", "application/msword"),
    ]
    await _prerender_documents_as_text(_attachments(*files), _model())
    # Threads finish in any order. A CSV is tabular: the planner sends it to
    # the spreadsheet tools, so it is never inline and never pre-rendered.
    assert sorted((fmt, raw) for fmt, raw, _ in rendered) == [
        ("docx", b"d"), ("html", b"h"), ("md", b"m"), ("txt", b"t"),
    ]
    assert not any(on_main for *_, on_main in rendered), "the parse never runs on the event loop's thread"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        _model(provider="mantle", model_id="openai.gpt-oss-120b"),
        _model(provider="bedrock-responses", model_id="openai.gpt-5.4"),
        _model(provider="openai", model_id="gpt-5"),
        _model(provider=None, model_id="gemini-2.5-pro"),
    ],
)
async def test_a_model_off_converse_is_rendered_whatever_the_flag(monkeypatch, rendered, model):
    """Every other provider's projection renders non-PDF documents as text
    (``agent_factory``), so the route renders them in worker threads too."""
    monkeypatch.delenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", raising=False)
    await _prerender_documents_as_text(_attachments(_File("a.txt", "text/plain")), model)
    assert [(fmt, on_main) for fmt, _, on_main in rendered] == [("txt", False)]


@pytest.mark.asyncio
async def test_a_model_without_a_provider_is_converse(flag_on, rendered):
    await _prerender_documents_as_text(_attachments(_File("a.txt", "text/plain")), _model(provider=None))
    assert len(rendered) == 1


@pytest.mark.asyncio
async def test_nothing_to_render(flag_on, rendered):
    await _prerender_documents_as_text(_attachments(), _model())
    await _prerender_documents_as_text(_attachments(_File("scan.pdf", "application/pdf")), _model())
    assert rendered == []


@pytest.mark.asyncio
async def test_a_failed_render_never_fails_the_turn(flag_on, monkeypatch):
    def boom(fmt, raw):
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(document_cap, "prerender_document_text", boom)
    await _prerender_documents_as_text(_attachments(_File("a.docx", "application/octet-stream")), _model())


@pytest.mark.asyncio
async def test_the_projection_finds_what_the_route_rendered(flag_on, monkeypatch):
    """End to end over the real memo: the route decodes the upload, PromptBuilder
    decodes it again into another bytes object, and the projection's lookup hits."""
    from apis.shared.files import docx_markdown

    monkeypatch.setattr(document_cap, "_TEXT_CACHE", type(document_cap._TEXT_CACHE)())
    monkeypatch.setattr(document_cap, "_text_cache_bytes", 0)
    calls = []
    real = docx_markdown.docx_to_markdown
    monkeypatch.setattr(docx_markdown, "docx_to_markdown", lambda b: calls.append(1) or real(b))

    raw = build_docx(["Quarterly figures."])
    upload = _File("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", raw)
    await _prerender_documents_as_text(_attachments(upload), _model())

    block_bytes = base64.b64decode(upload.bytes)
    history = [{"role": "user", "content": [{"text": "Summarize."}, {"document": {"format": "docx", "name": "report_docx", "source": {"bytes": block_bytes}}}]}]
    projected = document_cap.project_document_cap(history, as_text=True)

    assert "Quarterly figures." in projected[0]["content"][1]["text"]
    assert len(calls) == 1


def test_the_route_wires_the_phase_after_the_model_is_known():
    import inspect

    source = inspect.getsource(routes)
    adapt = source.index("await _adapt_attachments_for_model(attachments, turn_model)")
    prerender = source.index("await _prerender_documents_as_text(attachments, turn_model)")
    assert 0 < prerender - adapt < 400

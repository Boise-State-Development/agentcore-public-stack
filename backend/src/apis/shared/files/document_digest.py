"""DocumentDigest — a bounded, once-per-upload description of a document.

The offload design (``docs/specs/document-context-offload.md`` §4A) keeps a
document inline on the turn that introduces it and hands later turns a
*digest* plus a ``document_read`` handle instead. This module builds that
digest, off the model path, when an upload completes (PR-2). Nothing in the
chat path reads it yet; PR-3 renders it in place of the restore placeholder.

Two parts, deliberately split:

* **Outline** — deterministic, no model: page / paragraph / line count, a
  heading outline with the unit each heading starts on, table / figure
  mentions, and a text sample. PDF text comes from ``pypdfium2`` (already a
  dependency), DOCX from the stdlib extractor in ``document_read``, the text
  family from the bytes. A document with no detectable headings gets an
  outline sampled from page first-lines so the model still has anchors.
* **Abstract** — 3–5 sentences from a cheap text model over the sample plus
  the outline (Nova Micro, the same model the tool-batch summaries and the
  compaction summary use; ``DOCUMENT_DIGEST_MODEL_ID`` overrides). Fail-open:
  a model error leaves the digest without an abstract, never without an
  outline.

The rendered form (``render_digest``) is what enters context later. It is
hard-capped at ``DOCUMENT_DIGEST_MAX_TOKENS`` (default 1,500, chars/4): the
outline is trimmed from the end first, then the abstract. Every digest
records its rendered token estimate so the cap is a stored fact per file.

Content policy: the digest carries model prose about the user's document
(``abstract``) and heading text (``sections``). Both are denylisted in
``content_policy.CONTENT_BEARING``; the numeric fields (``status``,
``tokens``, ``count``, ``format``) are projectable.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from .document_read import _open_pdf, _pdf_page_text, docx_paragraphs, document_format_for

logger = logging.getLogger(__name__)

DIGEST_VERSION = 1

#: Same heuristic the compaction estimator uses (``compaction_policy.CHARS_PER_TOKEN``);
#: duplicated here because ``apis.shared`` must not import the agent layer.
_CHARS_PER_TOKEN = 4

#: Rendered budget. The spec's ceiling: a digest must be strictly smaller than
#: any document worth offloading (the trigger's floor is 5,000 tokens).
DOCUMENT_DIGEST_MAX_TOKENS = int(os.environ.get("DOCUMENT_DIGEST_MAX_TOKENS", 1_500))
#: Outline entries kept (evenly sampled when the document has more headings).
DOCUMENT_DIGEST_MAX_SECTIONS = int(os.environ.get("DOCUMENT_DIGEST_MAX_SECTIONS", 40))
#: Characters of document text handed to the abstract model.
DOCUMENT_DIGEST_SAMPLE_CHARS = int(os.environ.get("DOCUMENT_DIGEST_SAMPLE_CHARS", 12_000))
#: Cheap text model for the abstract. Nova Micro is what the tool-batch
#: summaries and the compaction summary already run on; it is text-only,
#: which is fine because the outline extractor hands it text.
DOCUMENT_DIGEST_MODEL_ID = os.environ.get("DOCUMENT_DIGEST_MODEL_ID", "").strip() or "us.amazon.nova-micro-v1:0"
_ABSTRACT_MAX_OUTPUT_TOKENS = 320
_HEADING_MAX_CHARS = 90
_SECTION_TITLE_CHARS = 80
_SAMPLE_PER_PAGE_CHARS = 600


def document_digest_enabled() -> bool:
    """Default ON with a kill switch (house style): only the literal "false" disables."""
    return os.environ.get("DOCUMENT_DIGEST_ENABLED", "").strip().lower() != "false"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class DigestSection(BaseModel):
    """One outline entry: the unit (page / paragraph / line, 1-indexed) a
    heading starts on and the heading text, bounded."""

    start: int
    title: str


class DocumentDigest(BaseModel):
    """What is persisted on ``FileMetadata.digest`` (as a plain dict)."""

    version: int = DIGEST_VERSION
    status: str = "ready"  # ready | failed
    format: Optional[str] = None
    unit: str = "page"  # page | paragraph | line
    count: int = 0
    sections: List[DigestSection] = Field(default_factory=list)
    tables: int = 0
    figures: int = 0
    chars: int = 0
    abstract: Optional[str] = None
    tokens: int = 0  # rendered-form estimate (chars/4)
    model_id: Optional[str] = None
    extractor_ms: int = 0
    error: Optional[str] = None  # exception class name only, never a message
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_item(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)

    @classmethod
    def from_item(cls, item: Any) -> Optional["DocumentDigest"]:
        if not isinstance(item, dict):
            return None
        try:
            return cls(**item)
        except Exception:  # noqa: BLE001 - a malformed row reads as "no digest"
            return None


# ---------------------------------------------------------------------------
# Outline extraction (deterministic)
# ---------------------------------------------------------------------------

_NUMBERED = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+\S|[IVXLC]{1,6}[.)]\s+\S|(?:article|section|chapter|part|appendix|schedule|exhibit|title)\s+[\w-]+)",
    re.IGNORECASE,
)
_TABLE = re.compile(r"\btable\s+\d+|\btable\b\s*[:\-]", re.IGNORECASE)
_FIGURE = re.compile(r"\b(?:figure|fig\.|chart|diagram)\s+\d+", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def looks_like_heading(line: str, *, standalone: bool = False) -> bool:
    """Cheap, format-agnostic heading test: short, no sentence punctuation at
    the end, and either numbered, ALL CAPS, or title-cased with few words.

    ``standalone`` is for units that are already a whole paragraph (DOCX,
    text lines): a one-word capitalized paragraph ("Risks") is a heading
    there, while a one-word line inside a PDF page is usually noise.
    """
    text = line.strip()
    if not (3 <= len(text) <= _HEADING_MAX_CHARS) or text.endswith((".", ",", ";", ":")):
        return False
    if _NUMBERED.match(text):
        return True
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        return True
    words = [w for w in re.split(r"\s+", text) if w]
    if len(words) <= 10:
        capitalized = sum(1 for w in words if w[0].isupper() or not w[0].isalpha())
        return capitalized / len(words) >= 0.7 and len(words) >= (1 if standalone else 2)
    return False


def _strip_html(text: str) -> str:
    return _HTML_TAG.sub(" ", text)


def _units_for(fmt: str, raw: bytes) -> Tuple[str, List[str]]:
    """``(unit name, unit texts)`` — one string per page / paragraph / line."""
    if fmt == "pdf":
        pdf = _open_pdf(raw)
        try:
            return "page", [_pdf_page_text(pdf, i) for i in range(len(pdf))]
        finally:
            pdf.close()
    if fmt == "docx":
        return "paragraph", docx_paragraphs(raw)
    text = raw.decode("utf-8", errors="replace")
    if fmt == "html":
        text = _strip_html(text)
    return "line", text.splitlines()


def _sample_sections(sections: List[DigestSection], limit: int) -> List[DigestSection]:
    if len(sections) <= limit:
        return sections
    step = len(sections) / limit
    return [sections[int(i * step)] for i in range(limit)]


def extract_outline(fmt: str, raw: bytes) -> DocumentDigest:
    """The deterministic half of the digest (no abstract, no tokens yet)."""
    unit, texts = _units_for(fmt, raw)
    sections: List[DigestSection] = []
    tables = figures = chars = 0
    for index, text in enumerate(texts, start=1):
        chars += len(text)
        tables += len(_TABLE.findall(text))
        figures += len(_FIGURE.findall(text))
        lines = text.splitlines() if unit == "page" else [text]
        for line in lines:
            md = _MD_HEADING.match(line) if fmt == "md" else None
            title = md.group(1) if md else (
                line.strip() if looks_like_heading(line, standalone=unit != "page") else None
            )
            if title:
                sections.append(DigestSection(start=index, title=title[:_SECTION_TITLE_CHARS]))
                if unit != "page":
                    break
    if not sections and unit == "page" and texts:
        # No headings (a scan, a form): anchor the outline on page first-lines.
        for index, text in enumerate(texts, start=1):
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if first:
                sections.append(DigestSection(start=index, title=first[:_SECTION_TITLE_CHARS]))
    return DocumentDigest(
        format=fmt,
        unit=unit,
        count=len(texts),
        sections=_sample_sections(sections, DOCUMENT_DIGEST_MAX_SECTIONS),
        tables=tables,
        figures=figures,
        chars=chars,
    )


def text_sample(fmt: str, raw: bytes, limit: int = DOCUMENT_DIGEST_SAMPLE_CHARS) -> str:
    """Up to ``limit`` characters spread across the document (not just its
    head), so the abstract sees the middle and the end of a long file."""
    _, texts = _units_for(fmt, raw)
    if not texts:
        return ""
    if fmt == "pdf":
        per_page = max(_SAMPLE_PER_PAGE_CHARS, limit // max(1, len(texts)))
        pieces = [t.strip()[:per_page] for t in texts if t.strip()]
        return "\n\n".join(pieces)[:limit]
    return "\n".join(t for t in texts if t.strip())[:limit]


# ---------------------------------------------------------------------------
# Abstract (cheap model, fail-open)
# ---------------------------------------------------------------------------

_ABSTRACT_SYSTEM_PROMPT = (
    "You write short, factual abstracts of documents for an assistant that will "
    "later retrieve specific pages on demand. Write 3 to 5 plain sentences: what "
    "the document is, who it is for or from, its main parts, and any notable "
    "numbers, dates or decisions. No preamble, no bullet points, no quotes."
)


def _abstract_prompt(outline: DocumentDigest, sample: str) -> str:
    lines = [f"Document type: {outline.format}, {outline.count} {outline.unit}s."]
    if outline.sections:
        lines.append("Outline:")
        lines.extend(f"- ({outline.unit} {s.start}) {s.title}" for s in outline.sections[:25])
    lines.append("Text sample:")
    lines.append(sample)
    return "\n".join(lines)


async def generate_abstract(outline: DocumentDigest, sample: str, model_id: str = DOCUMENT_DIGEST_MODEL_ID) -> Optional[str]:
    """3–5 sentences from the cheap model, or ``None`` (never raises)."""
    if not sample.strip():
        return None
    try:
        import boto3
    except ImportError:  # pragma: no cover
        return None
    try:
        client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        response = await asyncio.to_thread(
            client.converse,
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": _abstract_prompt(outline, sample)}]}],
            system=[{"text": _ABSTRACT_SYSTEM_PROMPT}],
            inferenceConfig={"temperature": 0.2, "maxTokens": _ABSTRACT_MAX_OUTPUT_TOKENS, "topP": 0.9},
        )
        if response.get("stopReason") == "max_tokens":
            logger.debug("Document abstract hit the token ceiling; discarding")
            return None
        text = response["output"]["message"]["content"][0]["text"].strip()
        return re.sub(r"\s+", " ", text) or None
    except Exception:  # noqa: BLE001 - an abstract is never worth a failed upload
        logger.debug("Document abstract generation skipped", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Rendering (what PR-3 puts in context) and the budget
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def render_digest(
    digest: DocumentDigest,
    *,
    filename: str,
    upload_id: str,
    budget_tokens: int = DOCUMENT_DIGEST_MAX_TOKENS,
) -> str:
    """The ``<document-digest …>`` block, trimmed to ``budget_tokens``.

    Sections are dropped from the end first (the model can always ask
    ``document_read`` for more), then the abstract is truncated. The opening
    tag always fits: it is the ``document_read`` handle.
    """
    header = (
        f'<document-digest name="{_xml_escape(filename)}" upload_id="{upload_id}" '
        f'format="{digest.format or ""}" {digest.unit}s="{digest.count}"'
    )
    if digest.tables:
        header += f' tables="{digest.tables}"'
    if digest.figures:
        header += f' figures="{digest.figures}"'
    header += ">"
    footer = "</document-digest>"
    abstract_line = f"  <abstract>{_xml_escape(digest.abstract)}</abstract>" if digest.abstract else ""
    section_lines = [
        f'  <section {digest.unit}="{s.start}">{_xml_escape(s.title)}</section>' for s in digest.sections
    ]

    def _build(abstract: str, sections: Sequence[str]) -> str:
        parts = [header]
        if abstract:
            parts.append(abstract)
        parts.extend(sections)
        parts.append(footer)
        return "\n".join(parts)

    budget_chars = max(0, budget_tokens) * _CHARS_PER_TOKEN
    kept = list(section_lines)
    text = _build(abstract_line, kept)
    while len(text) > budget_chars and kept:
        kept.pop()
        text = _build(abstract_line, kept)
    if len(text) > budget_chars and abstract_line:
        room = budget_chars - len(_build("", kept)) - len("  <abstract></abstract>") - 1
        if room > 20 and digest.abstract:
            trimmed = _xml_escape(digest.abstract[: max(0, room - 1)].rstrip()) + "…"
            text = _build(f"  <abstract>{trimmed}</abstract>", kept)
        else:
            text = _build("", kept)
    return text


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


async def build_digest(
    *,
    raw: bytes,
    mime_type: str,
    filename: str,
    upload_id: str,
    model_id: str = DOCUMENT_DIGEST_MODEL_ID,
    with_abstract: bool = True,
) -> DocumentDigest:
    """Outline + abstract + rendered token estimate. Never raises: an
    extraction failure yields ``status="failed"`` with the exception class
    name; an abstract failure yields a digest without one."""
    started = time.monotonic()
    fmt = document_format_for(mime_type, filename)
    if fmt is None:
        return DocumentDigest(status="failed", error="NotADocument", extractor_ms=0)
    try:
        outline = await asyncio.to_thread(extract_outline, fmt, raw)
        sample = await asyncio.to_thread(text_sample, fmt, raw) if with_abstract else ""
    except Exception as e:  # noqa: BLE001
        logger.warning("Document digest extraction failed (%s)", type(e).__name__, exc_info=True)
        return DocumentDigest(
            status="failed", format=fmt, error=type(e).__name__,
            extractor_ms=int((time.monotonic() - started) * 1000),
        )
    if with_abstract:
        outline.abstract = await generate_abstract(outline, sample, model_id=model_id)
        outline.model_id = model_id if outline.abstract else None
    outline.extractor_ms = int((time.monotonic() - started) * 1000)
    outline.tokens = estimate_tokens(render_digest(outline, filename=filename, upload_id=upload_id))
    return outline


def record_digest(digest: DocumentDigest) -> None:
    """One content-free EMF record per digest build. Never raises."""
    logger.info(
        "document_digest: status=%s format=%s %s=%d sections=%d tokens=%d abstract=%s ms=%d",
        digest.status, digest.format, digest.unit, digest.count, len(digest.sections),
        digest.tokens, bool(digest.abstract), digest.extractor_ms,
    )
    try:
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled
        from apis.shared.observability.emf import emit_emf_metrics

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "DocumentDigestGenerated": 1,
                "DocumentDigestTokens": int(digest.tokens),
                "DocumentDigestMs": int(digest.extractor_ms),
            },
            properties={
                "format": digest.format,
                "outcome": (
                    "failed" if digest.status != "ready"
                    else ("ready" if digest.abstract else "no_abstract")
                ),
            },
            units={"DocumentDigestTokens": "Count", "DocumentDigestMs": "Milliseconds"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("document_digest EMF skipped: %s", e)


__all__ = [
    "DIGEST_VERSION",
    "DOCUMENT_DIGEST_MAX_SECTIONS",
    "DOCUMENT_DIGEST_MAX_TOKENS",
    "DOCUMENT_DIGEST_MODEL_ID",
    "DigestSection",
    "DocumentDigest",
    "build_digest",
    "document_digest_enabled",
    "extract_outline",
    "generate_abstract",
    "looks_like_heading",
    "record_digest",
    "render_digest",
    "text_sample",
]

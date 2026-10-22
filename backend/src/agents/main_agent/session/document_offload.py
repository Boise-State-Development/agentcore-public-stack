"""Document offload — swap an inline document for its digest once it is no
longer the active subject, and only when the prefix re-write is free or
unavoidable (docs/specs/document-context-offload.md §4C / §4D, PR-4).

A document enters the cacheable prefix on the turn that attaches it and is
re-written in full on every cold turn for the life of the session — that is
the cost this spec exists to bound. The digest (PR-2) is ~1/20th of a large
PDF and the model can pull any page back with ``document_read`` (PR-1), so
after the document has had its turn the bytes can leave the prefix.

Three rules, all enforced here as pure functions over ``agent.messages``:

* **Pinning** (§4D) — a document stays inline while it is the active
  subject: on the attach turn and the next ``pin_turns - 1`` turns; while
  the incoming or previous prompt names it; while a ``document_read`` result
  for it sits in the recent turns.
* **Worth it** — the document's estimated tokens are at least
  ``DOCUMENT_OFFLOAD_MIN_TOKENS``; below that the re-write costs more than the
  eviction saves (the ``clear_at_least`` idea). The estimate is
  ``document_tokens.estimate_document_tokens`` — pages x the per-page image
  estimate for a PDF, bytes/4 otherwise — *not* bytes/4 for everything, which
  under-counted PDFs ~14x and held large scans below the floor forever.
* **Free or unavoidable** — decided by the session manager, which owns the
  cache-gap facts: the prompt cache has expired since the last turn, the
  model/agent prefix changed, or the last turn's input exceeded the
  compaction ceiling. Never while the cache is live (the 72% violation the
  compaction PR-3 rule fixed).

The replacement is **the same transformation restore performs** (PR-3):
match the block to its upload row, render the persisted digest. So an
offloaded block is byte-identical to what the next cold restore would have
produced for it anyway — the offload only moves the live prefix toward the
restore shape, earlier and on purpose. A block that cannot be matched stays
inline (unlike restore, the live path is allowed to keep bytes).

``document_read`` page slices (tool-result document blocks) are aged the
same way, on the live path and on restore: older than ``slice_turns`` turns
they become a one-line stub. Their names carry a random tail, so the stub
is a deterministic function of the block and stable across restores.

Mutation is in place on the message dicts; ``agent.messages`` is never
rebound (the #741 alias). Never raises.
"""

from __future__ import annotations

import logging
import os
import re
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from agents.main_agent.multimodal.file_sanitizer import FileSanitizer
from agents.main_agent.session.compaction_policy import CHARS_PER_TOKEN
from apis.shared.files.document_tokens import estimate_document_tokens

logger = logging.getLogger(__name__)

#: Below this estimated size an eviction is not worth the prefix re-write.
DOCUMENT_OFFLOAD_MIN_TOKENS = int(os.environ.get("DOCUMENT_OFFLOAD_MIN_TOKENS", 5_000))
#: A document is pinned on the turn it was attached and this many turns after
#: (2 = attach turn + the next one, spec §4D).
DOCUMENT_OFFLOAD_PIN_TURNS = int(os.environ.get("DOCUMENT_OFFLOAD_PIN_TURNS", 2))
#: ``document_read`` page slices older than this many turns are stubbed.
DOCUMENT_SLICE_MAX_TURNS = int(os.environ.get("DOCUMENT_SLICE_MAX_TURNS", 2))
#: Shortest document-name stem that counts as "the prompt names it".
_MIN_STEM_CHARS = 4
_KNOWN_FORMAT_SUFFIX = re.compile(r"_(pdf|docx|doc|txt|md|html|csv|xls|xlsx)(_\d+)?$", re.IGNORECASE)
_DUPLICATE_SUFFIX = re.compile(r"_\d+$")


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def document_offload_enabled() -> bool:
    """Default ON with a kill switch: only the literal "false" disables."""
    return os.environ.get("DOCUMENT_OFFLOAD_ENABLED", "").strip().lower() != "false"


def rollout_percent() -> int:
    raw = os.environ.get("DOCUMENT_OFFLOAD_ROLLOUT_PERCENT", "").strip()
    try:
        return max(0, min(100, int(raw))) if raw else 100
    except ValueError:
        return 100


def session_bucket(session_id: str) -> int:
    """Stable 0–99 bucket for a session (crc32, not ``hash`` — which is
    salted per process). The evaluation spec's randomized per-session arm."""
    return zlib.crc32((session_id or "").encode("utf-8")) % 100


def offload_enabled_for(session_id: Optional[str]) -> bool:
    """Kill switch, the ``document_read`` gate, and the rollout bucket
    together. Sessions below the percent are the treated arm; the rest keep
    today's inline-forever behavior.

    ``DOCUMENT_READ_ENABLED=false`` disables this path too. The live offload
    is the one place bytes leave the prefix *optionally* — restore has to drop
    them, this does not — so evicting a document while its only recovery path
    is switched off would be strictly worse than the pre-offload world, in
    which live bytes never left. Spec §5: "a digest that points at a tool
    nobody has is no better than today's placeholder."
    """
    from apis.shared.feature_flags import document_read_enabled

    if not document_offload_enabled() or not session_id:
        return False
    if not document_read_enabled():
        return False
    return session_bucket(session_id) < rollout_percent()


# ---------------------------------------------------------------------------
# Reading the conversation (content-free outputs only)
# ---------------------------------------------------------------------------


def _is_prompt(message: Dict[str, Any]) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return not any(isinstance(b, dict) and "toolResult" in b for b in content)


def prompt_indices(messages: Sequence[Dict[str, Any]]) -> List[int]:
    """Indices of the user prompts — one per turn."""
    return [i for i, m in enumerate(messages) if _is_prompt(m)]


def _text_of(message_or_prompt: Any) -> str:
    if isinstance(message_or_prompt, str):
        return message_or_prompt
    if isinstance(message_or_prompt, dict):
        message_or_prompt = message_or_prompt.get("content")
    if isinstance(message_or_prompt, str):
        return message_or_prompt
    if isinstance(message_or_prompt, list):
        return " ".join(
            b.get("text", "") for b in message_or_prompt if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def name_stem(block_name: str) -> str:
    """``"BBR Policy_pdf_2"`` → ``"BBR Policy"`` — what a user would type."""
    stem = _KNOWN_FORMAT_SUFFIX.sub("", block_name)
    stem = _DUPLICATE_SUFFIX.sub("", stem)
    return stem.strip()


def _inline_document(block: Any) -> Optional[Tuple[str, str, int]]:
    """``(name, format, size)`` for a document block with inline bytes."""
    if not isinstance(block, dict):
        return None
    doc = block.get("document")
    if not isinstance(doc, dict):
        return None
    raw = (doc.get("source") or {}).get("bytes") if isinstance(doc.get("source"), dict) else None
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return str(doc.get("name", "")), str(doc.get("format", "")), len(raw)


def _inline_document_bytes(block: Any) -> Optional[bytes]:
    """The block's raw bytes, for weighing it (``estimate_document_tokens``)."""
    if not isinstance(block, dict):
        return None
    doc = block.get("document")
    if not isinstance(doc, dict):
        return None
    raw = (doc.get("source") or {}).get("bytes") if isinstance(doc.get("source"), dict) else None
    return raw if isinstance(raw, (bytes, bytearray)) else None


def pinned_document_names(
    messages: Sequence[Dict[str, Any]],
    incoming_prompt: Any = None,
    *,
    pin_turns: int = DOCUMENT_OFFLOAD_PIN_TURNS,
) -> Set[str]:
    """Block names that must stay inline this turn (spec §4D)."""
    pinned: Set[str] = set()
    prompts = prompt_indices(messages)
    recent_start = prompts[-pin_turns] if len(prompts) >= pin_turns else (prompts[0] if prompts else len(messages))
    if pin_turns <= 0:
        recent_start = len(messages)

    # Every document name in the conversation, to test against prompt text.
    all_names: Set[str] = set()
    read_names: Set[str] = set()
    for idx, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            inline = _inline_document(block)
            if inline:
                all_names.add(inline[0])
                if idx >= recent_start:
                    pinned.add(inline[0])   # attach turn + the next pin_turns-1
                continue
            if idx >= recent_start and isinstance(block, dict) and isinstance(block.get("toolResult"), dict):
                for inner in block["toolResult"].get("content") or []:
                    payload = inner.get("json") if isinstance(inner, dict) else None
                    if isinstance(payload, dict) and isinstance(payload.get("filename"), str):
                        read_names.add(FileSanitizer.sanitize_filename(payload["filename"]))

    # Named in the incoming prompt or the previous one.
    texts = [_text_of(incoming_prompt)]
    if prompts:
        texts.append(_text_of(messages[prompts[-1]]))
    haystack = " ".join(texts).lower()
    for name in all_names:
        stem = name_stem(name).lower()
        if len(stem) >= _MIN_STEM_CHARS and stem in haystack:
            pinned.add(name)
        base = _DUPLICATE_SUFFIX.sub("", name)
        if name in read_names or base in read_names:
            pinned.add(name)
    return pinned


@dataclass
class Candidate:
    message_index: int
    block_index: int
    name: str
    format: str
    size: int
    #: Estimated prefix weight — pages x the per-page image estimate for PDFs,
    #: bytes/4 otherwise. This, not ``size``, is what the ``min_tokens`` floor
    #: and the ledger's ``documentTokens`` are measured against, so the row and
    #: the eviction decision always read the same quantity.
    tokens: int = 0


def candidate_documents(
    messages: Sequence[Dict[str, Any]],
    pinned: Set[str],
    *,
    min_tokens: int = DOCUMENT_OFFLOAD_MIN_TOKENS,
) -> List[Candidate]:
    """Inline documents that are unpinned and large enough to be worth it."""
    out: List[Candidate] = []
    for mi, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for bi, block in enumerate(content):
            inline = _inline_document(block)
            if not inline:
                continue
            name, fmt, size = inline
            if name in pinned:
                continue
            tokens = estimate_document_tokens(fmt, _inline_document_bytes(block))
            if tokens < min_tokens:
                continue
            out.append(Candidate(mi, bi, name, fmt, size, tokens))
    return out


# ---------------------------------------------------------------------------
# Mutations (in place)
# ---------------------------------------------------------------------------


@dataclass
class OffloadResult:
    offloaded: int = 0
    evicted_tokens: int = 0
    digest_tokens: int = 0
    skipped_unmatched: int = 0
    slices_aged: int = 0
    slice_tokens: int = 0
    upload_ids: List[str] = field(default_factory=list)


def offload_documents(
    messages: List[Dict[str, Any]],
    candidates: Sequence[Candidate],
    *,
    session_id: Optional[str],
    user_id: Optional[str],
) -> OffloadResult:
    """Replace each candidate block with its digest, in place, using the
    restore path's own matcher and renderer so the bytes equal what a cold
    restore would produce. Unmatched candidates stay inline. Never raises."""
    from agents.main_agent.session.document_rehydration import digest_for, load_session_documents, match_document
    from apis.shared.feature_flags import document_read_enabled
    from apis.shared.files.document_digest import render_digest

    result = OffloadResult()
    if not candidates:
        return result
    try:
        rows = load_session_documents(session_id or "", user_id)
    except Exception:  # noqa: BLE001
        logger.warning("document_offload: upload rows unavailable; leaving documents inline", exc_info=True)
        result.skipped_unmatched = len(candidates)
        return result

    used: Set[str] = set()
    for cand in candidates:
        try:
            content = messages[cand.message_index]["content"]
            block = content[cand.block_index]
            inline = _inline_document(block)
            if not inline or inline[0] != cand.name:
                continue  # the list moved under us; skip rather than guess
            meta = match_document(cand.name, cand.format, cand.size, rows, used)
            if meta is None:
                result.skipped_unmatched += 1
                continue
            raw = block["document"]["source"]["bytes"]
            digest = digest_for(meta, bytes(raw))
            if digest is None:
                result.skipped_unmatched += 1
                continue
            text = render_digest(
                digest, filename=meta.filename, upload_id=meta.upload_id,
                include_handle=document_read_enabled(),
            )
            content[cand.block_index] = {"text": text}
            used.add(meta.upload_id)
            result.offloaded += 1
            result.evicted_tokens += cand.tokens
            result.digest_tokens += len(text) // CHARS_PER_TOKEN
            result.upload_ids.append(meta.upload_id)
        except Exception:  # noqa: BLE001 - leave this block inline
            logger.warning("document_offload: one block skipped", exc_info=True)
            result.skipped_unmatched += 1
    return result


def slice_stub(name: str) -> str:
    return f"[Retrieved pages placeholder: name={name} — call document_read again to reload them]"


def age_document_slices(
    messages: List[Dict[str, Any]],
    *,
    max_turns: int = DOCUMENT_SLICE_MAX_TURNS,
) -> Tuple[int, int]:
    """Stub ``document_read`` page slices older than ``max_turns`` turns, in
    place. Returns ``(slices, estimated tokens evicted)``."""
    prompts = prompt_indices(messages)
    if max_turns <= 0:
        cutoff = len(messages)
    elif len(prompts) >= max_turns:
        cutoff = prompts[-max_turns]
    else:
        return 0, 0
    count = tokens = 0
    for idx in range(min(cutoff, len(messages))):
        message = messages[idx]
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("toolResult"), dict):
                continue
            inner_content = block["toolResult"].get("content")
            if not isinstance(inner_content, list):
                continue
            for ii, inner in enumerate(inner_content):
                inline = _inline_document(inner)
                if not inline:
                    continue
                name, fmt, _size = inline
                tokens += estimate_document_tokens(fmt, _inline_document_bytes(inner))
                inner_content[ii] = {"text": slice_stub(name)}
                count += 1
    return count, tokens


__all__ = [
    "Candidate",
    "DOCUMENT_OFFLOAD_MIN_TOKENS",
    "DOCUMENT_OFFLOAD_PIN_TURNS",
    "DOCUMENT_SLICE_MAX_TURNS",
    "OffloadResult",
    "age_document_slices",
    "candidate_documents",
    "document_offload_enabled",
    "name_stem",
    "offload_documents",
    "offload_enabled_for",
    "pinned_document_names",
    "prompt_indices",
    "rollout_percent",
    "session_bucket",
    "slice_stub",
]

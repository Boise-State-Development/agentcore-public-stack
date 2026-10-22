"""Text helpers shared by conversation search's write and read sides.

The lexical leg (``docs/specs/conversation-search.md`` §5) is a DynamoDB
``contains()`` over two attributes on the session row, and ``contains`` is
case-sensitive. So both attributes are written already normalized, and the
query is normalized the same way before it is compared:

* ``titleLower`` is the title, normalized (:func:`normalize_search_text`).
* ``firstPrompt`` is the first :data:`FIRST_PROMPT_MAX_CHARS` characters of the
  conversation's opening prompt, normalized (:func:`first_prompt_value`).

One function does the normalizing for the writer (``update_session_title``, the
full-row store, the backfill script) and the reader, so the two can never drift
into a mismatch that would silently find nothing.

Stdlib only, and in ``sessions/`` on purpose: ``metadata`` imports this on every
title write, and the lean images that carry ``metadata`` (scheduled runs) copy
this package whole, so a helper here needs no change to any image.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional

#: How much of the opening prompt is kept on the session row for the lexical leg.
#: ~300 bytes a session; the full text is the index's job.
FIRST_PROMPT_MAX_CHARS = 300

#: How long a result's snippet may be.
SNIPPET_MAX_CHARS = 240

#: Query terms shorter than this are not used to place a snippet window (they
#: match almost anywhere, so the window would land on noise).
_MIN_TERM_CHARS = 2

_WHITESPACE = re.compile(r"\s+")


def normalize_search_text(text: Optional[str]) -> str:
    """Case-fold, NFKC-normalize and collapse whitespace.

    NFKC so a full-width or ligature form matches its plain spelling, and
    ``casefold`` rather than ``lower`` so the comparison is the Unicode
    caseless one. Whatever this returns is what is stored and what is compared.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", folded).strip()


def first_prompt_value(prompt: Optional[str]) -> str:
    """The ``firstPrompt`` attribute for an opening prompt: normalized, then cut.

    Normalized before it is cut, so the stored value is exactly the first
    :data:`FIRST_PROMPT_MAX_CHARS` characters a query is compared against.
    """
    return normalize_search_text(prompt)[:FIRST_PROMPT_MAX_CHARS].rstrip()


def query_terms(query: str) -> List[str]:
    """The query's words, longest first, as used to place a snippet window."""
    words = {w for w in normalize_search_text(query).split(" ") if len(w) >= _MIN_TERM_CHARS}
    return sorted(words, key=lambda w: (-len(w), w))


def make_snippet(text: str, query: str, max_chars: int = SNIPPET_MAX_CHARS) -> str:
    """A window of ``text`` around the first place the query appears.

    The whole query is looked for first, then its longest words. With no
    literal match (a semantic hit, which is what hybrid retrieval returns for a
    query in the user's own words) the snippet is the text's opening. Plain
    text, whitespace collapsed; ``…`` marks a cut at either end. The SPA does
    its own emphasis from the query it sent, so no markup travels here.
    """
    flat = _WHITESPACE.sub(" ", text or "").strip()
    if len(flat) <= max_chars:
        return flat

    haystack = flat.casefold()
    needles = [normalize_search_text(query)] + query_terms(query)
    position = -1
    match_length = 0
    for needle in needles:
        if not needle:
            continue
        position = haystack.find(needle)
        if position >= 0:
            match_length = len(needle)
            break

    if position < 0:
        return flat[: max_chars - 1].rstrip() + "…"

    # Put the match about a third of the way in, so the reader sees what led up
    # to it and most of what follows.
    lead = max(0, min(position - max_chars // 3, len(flat) - max_chars))
    if match_length >= max_chars:
        lead = position
    end = lead + max_chars
    body = flat[lead:end]
    if lead > 0:
        body = "…" + body[1:].lstrip()
    if end < len(flat):
        body = body[:-1].rstrip() + "…"
    return body


_FRACTION = re.compile(r"(\.\d+)")


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp leniently, to an aware UTC ``datetime``.

    The index stores ``created_at`` as Python writes it
    (``2026-10-07T17:09:45.716213+00:00``) and Bedrock hands it back reformatted
    (``2026-10-07T17:09:45.716Z``, measured on dev 2026-10-07): a ``Z`` suffix
    and milliseconds. Both, and a bare date, parse here. A naive value is read
    as UTC. Anything unparseable is ``None``, never an exception.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    # Pad or trim the fraction to microseconds, whatever precision arrived.
    raw = _FRACTION.sub(lambda m: (m.group(1) + "000000")[:7], raw, count=1)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "FIRST_PROMPT_MAX_CHARS",
    "SNIPPET_MAX_CHARS",
    "first_prompt_value",
    "make_snippet",
    "normalize_search_text",
    "parse_timestamp",
    "query_terms",
]

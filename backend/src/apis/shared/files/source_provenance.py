"""Does a cited passage exist in the document, and does it say what we claim?

``docs/specs/source-provenance-verification.md``. Two independent questions,
asked in order and never collapsed into one:

1. **Is the quoted passage in the document?** Unicode-normalized string search
   against the document's own extracted text.
2. **Does the asserted value appear in that passage?** The load-bearing one. A
   located quote is evidence that the model read the document; it is not
   evidence that the document says what the answer says. A model that invents
   an award amount and pairs it with a real sentence from the budget section
   passes (1) and fails (2), and answering only (1) certifies the weaker
   proposition.

Question 2 is asked only once question 1 says yes: a passage we could not find
proves nothing about the value sitting beside it, so an unlocated quote is
never promoted.

THE STATE IS THE API
--------------------
:func:`verify_claim` is the only way to obtain a :class:`SourceVerification`,
and there is deliberately **no public function that takes the two underlying
booleans**. A function that did would be the misuse vector it looks like a
convenience for: any caller could pass a pair it invented, or pass "the quote
exists" twice and re-certify the weaker claim under the stronger badge.

For the same reason the booleans are not fields on the result. What is recorded
instead is :attr:`SourceVerification.method` — the single reason the state came
out as it did. It carries strictly more information than the pair (it tells
``no_match`` apart from ``quote_too_long``) and, unlike the pair, there is no
second verdict anyone can derive from it.

PURITY
------
Standard library only. No boto, no DynamoDB, no model call, and no import from
any other ``apis.shared`` package — that is what makes this safe to import from
app-api, the AgentCore Runtime and a Lambda alike, and what makes every state
above unit-testable without a fixture. The number and date parsing below is
duplicated here in miniature rather than imported from a heavier validator; the
trade buys the purity.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------

#: The value itself appears in a passage located in the document. The only
#: state that earns a plain locator.
STATE_SUPPORTED = "supported"

#: The passage is real, the value is not in it. The hallucination signal: a
#: located quote that does not say what the value claims. Must render like one.
STATE_QUOTE_UNSUPPORTED = "quote_unsupported"

#: The passage is real, but the value is not the kind of thing that can be
#: found in it — an enum answer that maps the prose rather than quoting it, a
#: non-scalar, a quote too long to scan. Traceable, not confirmable.
STATE_UNASSESSED = "unassessed"

#: No passage could be located, or none was given. Nothing here is traced.
STATE_UNVERIFIED = "unverified"

#: Locator units a document can actually have. ``page`` only where a page
#: boundary was measured or estimated — never invented for a format that has
#: no pages (see :data:`UNIT_KINDS`).
KIND_PAGE = "page"
KIND_PARAGRAPH = "paragraph"
KIND_LINE = "line"

#: How a locator's position was arrived at. Required on every locator and
#: always serialized: an optional flag whose absence reads as exact is how a
#: guess gets rendered as ``p. 234``.
PRECISION_MEASURED = "measured"
PRECISION_ESTIMATED = "estimated"

#: ``document_format_for`` format -> the unit that format's text is numbered
#: by, matching ``large_document.UNIT_FOR_FORMAT`` and
#: ``document_digest._units_for``. Duplicated rather than imported to keep this
#: module free of ``apis.shared`` imports; a test pins the two together.
UNIT_KINDS = {
    "pdf": KIND_PAGE,
    "docx": KIND_PARAGRAPH,
    "txt": KIND_LINE,
    "md": KIND_LINE,
    "html": KIND_LINE,
}


@dataclass(frozen=True)
class Locator:
    """Where in the document a located passage starts.

    ``kind`` stops us inventing a page for a document that has none: a ``.docx``
    reaches us as paragraphs, no page boundary was ever measured, and ``p. 3``
    there is as invented as an interpolated page would be.
    """

    kind: str
    #: 1-indexed unit number.
    value: int
    precision: str

    def to_payload(self) -> dict:
        """JSON for the SSE event and the stored row. ``precision`` is always
        present — a reader must never have to guess it."""
        return {"kind": self.kind, "value": self.value, "precision": self.precision}


@dataclass(frozen=True)
class SourceVerification:
    """One claim's verdict. ``state`` is the only thing a consumer may branch on.

    ``method`` exists to be measured, not to be re-interpreted: it says how the
    state was reached, so the distribution across ``literal`` / ``numeric`` /
    ``date`` / ``no_match`` and the ``unassessed`` reasons is readable before
    any of it drives a badge.
    """

    #: Caller-supplied id, echoed so a batch's verdicts can be matched up.
    claim_id: str
    state: str
    method: str
    locator: Optional[Locator] = None


@dataclass(frozen=True)
class Claim:
    """A value the model is asserting, and the passage it says supports it.

    ``enum_field`` marks a value drawn from a fixed set that maps the prose
    rather than quoting it ("Yes" for "cost sharing is required"). The value
    question does not apply to those, and counting them unsupported would load
    the measurement with a class that carries no hallucination signal.
    """

    claim_id: str
    value: Any
    quote: Optional[str]
    enum_field: bool = False


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

#: 1:1-or-expanding character folds applied to both the document text and the
#: quote before matching. Model output routinely differs from a PDF text layer
#: by exactly these: smart quotes, dash variants, NBSP, ligatures.
_CHAR_FOLDS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "ﬁ": "fi", "ﬂ": "fl",
}

#: Dropped entirely: soft hyphen, BOM, zero-width space / non-joiner / joiner.
#: A PDF that hyphenates across a line break leaves a soft hyphen the model
#: never reproduces.
_DROPPED = frozenset({"­", "﻿", "​", "‌", "‍"})


def normalize_with_map(text: str) -> Tuple[str, List[int]]:
    """Lowercase, fold and whitespace-collapse *text*.

    Returns ``(normalized, index_map)`` where ``index_map[i]`` is the offset in
    the original text of the character that produced ``normalized[i]``, so a
    match found in normalized space projects back to a real position.
    """
    out: List[str] = []
    index_map: List[int] = []
    last_was_space = True  # also trims leading whitespace
    for i, ch in enumerate(text):
        if ch in _DROPPED:
            continue
        for c in _CHAR_FOLDS.get(ch, ch):
            if c.isspace():
                if last_was_space:
                    continue
                out.append(" ")
                index_map.append(i)
                last_was_space = True
            else:
                out.append(c.lower())
                index_map.append(i)
                last_was_space = False
    if out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


# ---------------------------------------------------------------------------
# The document side
# ---------------------------------------------------------------------------

#: Joins the unit texts into the one string a quote is searched against. A
#: newline rather than nothing, so the last word of a page and the first of the
#: next do not fuse into a token that matches neither.
_UNIT_JOIN = "\n"


class DocumentUnits:
    """A document's text as the units it actually has, ready to be searched.

    Built from the ``(unit, texts)`` pair every reading path in the codebase
    already produces — ``document_digest._units_for`` in process, or
    ``large_document.decode_text_layer`` over the prepared ``text.jsonl``,
    whose records *are* the persisted page markers for a prepared PDF.

    The unit texts are concatenated once, with a parallel array of unit start
    offsets, so an offset resolves to its unit by binary search. That is a
    marker list, derived rather than persisted: ``text.jsonl`` already fixes
    every boundary exactly, and a second copy could only disagree with it.

    Searching the concatenation rather than each unit in turn is deliberate. A
    quote spanning a page break exists in neither page and would read
    ``unverified`` — a *manufactured* hallucination signal, which is the one
    error this module must never produce. A straddling match is reported at the
    unit it starts on, marked ``estimated``, because the single number returned
    is a convention about a passage that genuinely occupies two.

    Normalization is done once here, so N claims against one document pay for
    it once.
    """

    __slots__ = ("kind", "precision", "text", "_unit_starts", "_norm", "_index_map")

    def __init__(self, kind: str, texts: Sequence[str], *, precision: str) -> None:
        if kind not in (KIND_PAGE, KIND_PARAGRAPH, KIND_LINE):
            raise ValueError(f"unknown locator kind: {kind!r}")
        if precision not in (PRECISION_MEASURED, PRECISION_ESTIMATED):
            raise ValueError(f"precision must be stated explicitly, got {precision!r}")
        self.kind = kind
        self.precision = precision
        starts: List[int] = []
        offset = 0
        for text in texts:
            starts.append(offset)
            offset += len(text) + len(_UNIT_JOIN)
        self._unit_starts = starts
        self.text = _UNIT_JOIN.join(texts)
        self._norm, self._index_map = normalize_with_map(self.text)

    @property
    def unit_count(self) -> int:
        return len(self._unit_starts)

    def locate(self, quote: str) -> Optional[Locator]:
        """The locator for *quote*, or ``None`` when it is not in the document.

        Exact match first and short-circuiting; the normalized match is the
        fallback, not the default, so a verbatim quote costs one ``str.find``.
        """
        if not self.text or not quote:
            return None
        offset = self.text.find(quote)
        matched_length = len(quote)
        if offset == -1:
            norm_quote, _ = normalize_with_map(quote)
            if not norm_quote:
                return None
            start = self._norm.find(norm_quote)
            if start == -1:
                return None
            offset = self._index_map[start]
            end_index = start + len(norm_quote) - 1
            matched_length = self._index_map[end_index] - offset + 1
        return self._locator_for(offset, offset + max(matched_length, 1) - 1)

    def _locator_for(self, start: int, end: int) -> Locator:
        unit = bisect_right(self._unit_starts, start)
        # A match that runs past its own unit's end spans a boundary: the unit
        # we name is where the passage begins, which is a convention rather
        # than a measurement, so the position is downgraded to estimated.
        straddles = unit < len(self._unit_starts) and end >= self._unit_starts[unit]
        precision = PRECISION_ESTIMATED if straddles else self.precision
        return Locator(kind=self.kind, value=unit, precision=precision)


def document_units(
    fmt: str, texts: Sequence[str], *, precision: str = PRECISION_MEASURED
) -> DocumentUnits:
    """:class:`DocumentUnits` for a ``document_format_for`` format.

    ``precision`` defaults to measured because every path in slice one extracts
    with ``pypdfium2`` or reads the text layer it wrote, both of which fix real
    boundaries. A caller whose positions are inferred — a managed-KB chunk page,
    an OCR pass with no page structure — must pass ``estimated`` itself.
    """
    kind = UNIT_KINDS.get(fmt)
    if kind is None:
        raise ValueError(f"no locator unit for format {fmt!r}")
    return DocumentUnits(kind, texts, precision=precision)


# ---------------------------------------------------------------------------
# The value check
# ---------------------------------------------------------------------------

#: A value that names its own absence has nothing to support, so it is never
#: counted either way.
_NOT_FOUND = frozenset({
    "", "n/a", "na", "n.a.", "not found", "not available", "not applicable",
    "none", "null", "nil", "unknown", "-", "--", "---", "nan", "no data",
    "no value", "not provided", "not specified", "not present", "not stated",
    "not mentioned", "not given", "no information", "no entry", "missing",
    "empty", "blank",
})

_CURRENCY = "$€£¥"

#: Comma groups only in thousands position. A permissive ``[\\d,]*`` swallows
#: "sections 1, 2 and 3" as one token, which then parses as 123 and "supports"
#: a value of 123.
_NUMBER_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")

#: A quote is a passage, not a document. Past this the value question is not
#: worth asking, and asking it is not free: the date scan backtracks badly on
#: long space-free text, and the quote is model-controlled input.
_MAX_QUOTE_CHARS = 4000

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    rf"|(?:{_MONTHS})[a-z]*\.?\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})[a-z]*\.?,?\s+\d{{4}}",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y",
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y",
)

# Methods. Each names the single reason a state was reached.
METHOD_NO_QUOTE = "no_quote"
METHOD_QUOTE_NOT_LOCATED = "quote_not_located"
METHOD_DOCUMENT_UNREADABLE = "document_unreadable"
METHOD_QUOTE_TOO_LONG = "quote_too_long"
METHOD_EMPTY_VALUE = "empty_value"
METHOD_ENUM_FIELD = "enum_field"
METHOD_NON_SCALAR = "non_scalar"
METHOD_LITERAL = "literal"
METHOD_NUMERIC = "numeric"
METHOD_DATE = "date"
METHOD_NO_MATCH = "no_match"


def _is_not_found(value: Any) -> bool:
    if value is None:
        return True
    return str(value).strip().lower() in _NOT_FOUND


def _as_number(text: str) -> Optional[float]:
    cleaned = text.strip()
    for ch in _CURRENCY:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.replace(",", "").replace(" ", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _number_with_unit(text: str) -> Optional[Tuple[float, bool]]:
    """``(number, is_percent)``, or ``None`` when the whole string is not one.

    A percentage is not the same quantity as the bare number: ``$50`` must not
    be certified by a quote reading "50% of MTDC", so the unit travels with the
    value instead of being stripped off it.
    """
    s = text.strip()
    is_percent = s.endswith("%")
    if is_percent:
        s = s[:-1]
    number = _as_number(s)
    return None if number is None else (number, is_percent)


def _numbers_in(text: str) -> set:
    found = set()
    for match in _NUMBER_RE.finditer(text):
        number = _as_number(match.group())
        if number is not None:
            found.add((number, text[match.end():match.end() + 1] == "%"))
    return found


def _as_date(text: str) -> Optional[date]:
    s = text.strip().rstrip(".,").replace(".", "")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _dates_in(text: str) -> set:
    found = set()
    for match in _DATE_RE.finditer(text):
        parsed = _as_date(match.group())
        if parsed is not None:
            found.add(parsed)
    return found


def _on_value_boundaries(quote: str, start: int, end: int, *, numeric: bool) -> bool:
    """Is the match at ``[start, end)`` a whole value rather than a fragment?

    Without this the literal check is a bare substring test, so a quote saying
    5000000 "supports" a value of 500000, $500,000.99 supports $500,000, and
    "John Smithson" supports "John Smith" — over-claiming in exactly the
    direction the value check exists to catch.
    """
    before = quote[start - 1] if start > 0 else ""
    after = quote[end] if end < len(quote) else ""
    if numeric:
        if before.isdigit() or before in ".,":
            return False
        if after.isdigit():
            return False
        # A trailing "." or "," is part of the number only when a digit
        # follows, so ordinary sentence punctuation still ends a value.
        if after in ".," and end + 1 < len(quote) and quote[end + 1].isdigit():
            return False
        return True
    return not (before.isalnum() or after.isalnum())


def _literal_match(norm_value: str, norm_quote: str, *, numeric: bool) -> bool:
    """Whether *norm_value* occurs in *norm_quote* as a whole value.

    Every occurrence is scanned: the first may be a fragment ("500,000" inside
    "1,500,000") while a later one is genuine.
    """
    index = norm_quote.find(norm_value)
    while index != -1:
        if _on_value_boundaries(norm_quote, index, index + len(norm_value), numeric=numeric):
            return True
        index = norm_quote.find(norm_value, index + 1)
    return False


def _value_in_quote(value: Any, quote: str, *, enum_field: bool) -> Tuple[Optional[bool], str]:
    """``(supported, method)`` for a quote already known to exist.

    ``supported`` is ``None`` when the question does not apply, which is what
    separates :data:`STATE_UNASSESSED` from :data:`STATE_QUOTE_UNSUPPORTED`.

    Deliberately strict: only literal, numeric and date equivalence count, and
    a literal match must sit on value boundaries. A multi-part value assembled
    from a sentence ("Jane Smith, Chemistry") reads as unsupported — the
    conservative direction while the state is new. If the measured distribution
    shows it dominates, a partial-coverage rule can be added with evidence
    behind it.

    Three weaknesses to read a ``True`` against. Very short values ("1", "A")
    match almost any passage by coincidence. A value that is a judgment about
    the passage rather than a span of it reads unsupported unless the field is
    an enum. And this asks only whether the value appears *somewhere* in the
    passage, not whether the passage assigns it to this field: a "Direct Costs"
    value is supported by a quote labelling that same figure indirect, and a
    year (2024) by a dollar amount ($2,024). The honest reading of ``True`` is
    "this value is in this passage", never "this passage says this field is
    this value".
    """
    if len(quote) > _MAX_QUOTE_CHARS:
        return None, METHOD_QUOTE_TOO_LONG
    if _is_not_found(value):
        return None, METHOD_EMPTY_VALUE
    if enum_field:
        return None, METHOD_ENUM_FIELD
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        # Lists, dicts and bools have no verbatim form to look for. Counting
        # them unsupported would load the measurement with a class that
        # carries no hallucination signal.
        return None, METHOD_NON_SCALAR

    value_str = str(value).strip()
    norm_value, _ = normalize_with_map(value_str)
    norm_quote, _ = normalize_with_map(quote)
    if not norm_value:
        return None, METHOD_EMPTY_VALUE

    number = _number_with_unit(value_str)
    if _literal_match(norm_value, norm_quote, numeric=number is not None):
        return True, METHOD_LITERAL
    if number is not None and number in _numbers_in(quote):
        return True, METHOD_NUMERIC

    parsed_date = _as_date(value_str)
    if parsed_date is not None and parsed_date in _dates_in(quote):
        return True, METHOD_DATE

    return False, METHOD_NO_MATCH


# ---------------------------------------------------------------------------
# The only entry points
# ---------------------------------------------------------------------------


def verify_claim(claim: Claim, document: Optional[DocumentUnits]) -> SourceVerification:
    """Verify one claim. The only way to obtain a :class:`SourceVerification`.

    There is deliberately no public function taking "the quote exists" and "the
    value is in it" as arguments: a caller could pass a pair it invented, or
    pass the weaker answer twice and earn the stronger badge. Both questions
    are asked here, in order, and only the collapsed state comes back.
    """
    quote = claim.quote.strip() if isinstance(claim.quote, str) else None
    if not quote:
        return SourceVerification(claim.claim_id, STATE_UNVERIFIED, METHOD_NO_QUOTE)
    if document is None or not document.text.strip():
        # No text layer to search — a scanned PDF, or a document we could not
        # read. Nothing is traced, and saying so is the honest answer.
        return SourceVerification(
            claim.claim_id, STATE_UNVERIFIED, METHOD_DOCUMENT_UNREADABLE
        )

    locator = document.locate(quote)
    if locator is None:
        return SourceVerification(
            claim.claim_id, STATE_UNVERIFIED, METHOD_QUOTE_NOT_LOCATED
        )

    # Only now: a passage we could not find proves nothing about the value
    # beside it, so an unlocated quote is never promoted past unverified.
    supported, method = _value_in_quote(claim.value, quote, enum_field=claim.enum_field)
    if supported is True:
        state = STATE_SUPPORTED
    elif supported is False:
        state = STATE_QUOTE_UNSUPPORTED
    else:
        state = STATE_UNASSESSED
    return SourceVerification(claim.claim_id, state, method, locator)


def verify_claims(
    claims: Sequence[Claim], document: Optional[DocumentUnits]
) -> List[SourceVerification]:
    """:func:`verify_claim` over a batch against one document.

    Its own entry point because :class:`DocumentUnits` normalizes once on
    construction, so N claims against one document pay that cost once.
    """
    return [verify_claim(claim, document) for claim in claims]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_locator(locator: Optional[Locator]) -> Optional[str]:
    """``"p. 12"``, ``"p. ~12"``, ``"¶ 34"``, ``"line 88"``, or ``None``.

    ``None`` when there is nothing to cite, so a caller falls back to the bare
    document name rather than printing a position it does not have. An
    estimated page is hedged: an unnecessary tilde costs a tilde, while invented
    precision costs trust.
    """
    if locator is None:
        return None
    marker = "~" if locator.precision == PRECISION_ESTIMATED else ""
    if locator.kind == KIND_PAGE:
        return f"p. {marker}{locator.value}"
    if locator.kind == KIND_PARAGRAPH:
        return f"¶ {marker}{locator.value}"
    return f"line {marker}{locator.value}"


__all__ = [
    "Claim",
    "DocumentUnits",
    "Locator",
    "SourceVerification",
    "STATE_SUPPORTED",
    "STATE_QUOTE_UNSUPPORTED",
    "STATE_UNASSESSED",
    "STATE_UNVERIFIED",
    "KIND_PAGE",
    "KIND_PARAGRAPH",
    "KIND_LINE",
    "PRECISION_MEASURED",
    "PRECISION_ESTIMATED",
    "UNIT_KINDS",
    "METHOD_NO_QUOTE",
    "METHOD_QUOTE_NOT_LOCATED",
    "METHOD_DOCUMENT_UNREADABLE",
    "METHOD_QUOTE_TOO_LONG",
    "METHOD_EMPTY_VALUE",
    "METHOD_ENUM_FIELD",
    "METHOD_NON_SCALAR",
    "METHOD_LITERAL",
    "METHOD_NUMERIC",
    "METHOD_DATE",
    "METHOD_NO_MATCH",
    "document_units",
    "normalize_with_map",
    "render_locator",
    "verify_claim",
    "verify_claims",
]

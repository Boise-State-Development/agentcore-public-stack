"""Who gets what: the attachment planner's table (unified-attachment-delivery §8.3).

``plan_attachments`` is a pure function from file facts × model facts to a plan,
so the whole delivery policy is a table: each row below is a turn's attachments,
the model and the profile, and the expected ``(mode, class, reason)`` per file.
The table runs with no model call and no AWS, and its digest is pinned the way a
tool spec's is: any change to who gets what — intended or not — changes the
digest, and the PR that changes it says so.

The selection cases that used to live beside the route's helpers
(``test_attachment_turn_guard.py``, ``test_inline_budget_by_reference.py``,
``test_large_document_turn_path.py``, ``test_presentation_attachment_carveout.py``)
are rows here now; those files keep the route's wiring only (§9.1, phase 1).

The ``legacy`` profile reproduces the byte rules the route applied before the
planner existed. Its values are written out below rather than read from the
environment, so the table is hermetic; ``test_the_legacy_profile_reads_today's_constants``
ties them back to the module constants.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace
from typing import List, Optional, Tuple

import pytest

from apis.shared.files import attachment_plan as ap
from apis.shared.files import models as file_models
from apis.shared.files.attachment_plan import (
    ConversationFacts,
    FileFacts,
    LimitsProfile,
    ModelFacts,
    cap_message_attachments,
    delivers_by_reference,
    file_class,
    plan_attachments,
)

MB = 1_000_000
MIB = 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PNG = "image/png"
TXT = "text/plain"
CSV = "text/csv"
MD = "text/markdown"
HTML = "text/html"
DOC = "application/msword"
EVENT_QUOTA_BYTES = 10_000_000  # AgentCore Memory event payload quota

#: The legacy profile with today's defaults, written out.
LEGACY = LimitsProfile(
    name="legacy",
    max_files_per_message=5,
    inline_document_max_bytes=4 * MIB,
    inline_total_max_bytes=7_000_000,
    turn_guard=True,
    over_budget_by_reference=True,
    documents_as_text=False,
    text_only_max_chars=60_000,
)

CLAUDE = ModelFacts(text_only=False, converse=True)
MANTLE = ModelFacts(text_only=False, converse=False)
TEXT_ONLY = ModelFacts(text_only=True, converse=True)


def f(name: str, mime: str = PDF, size: int = 1_000, **kw) -> FileFacts:
    """A direct (no row) attachment unless ``upload_id`` / ``has_row`` say otherwise."""
    if "upload_id" in kw and "has_row" not in kw:
        kw["has_row"] = True
    return FileFacts(filename=name, mime_type=mime, size_bytes=size, **kw)


def up(name: str, mime: str = PDF, size: int = 1_000, **kw) -> FileFacts:
    """An uploaded attachment: it has a row ``document_read`` can read later."""
    return f(name, mime, size, upload_id=f"up-{name}", **kw)


def ref(name: str, mime: str = PDF, size: int = 38 * MIB, **kw) -> FileFacts:
    """A reference-class upload the resolver returned without its bytes."""
    return up(name, mime, size, by_reference=True, **kw)


N, T, OD, TOOL, D = ap.MODE_NATIVE, ap.MODE_TEXT, ap.MODE_ON_DEMAND, ap.MODE_TOOL, ap.MODE_DROPPED


@dataclass
class Case:
    name: str
    files: List[FileFacts]
    expect: List[Tuple[str, str, str]]
    model: Optional[ModelFacts] = CLAUDE
    profile: LimitsProfile = LEGACY
    tokens: Optional[List[int]] = None
    requested: Optional[int] = None
    text_char_limit: Optional[int] = None


def _budget(n: int) -> LimitsProfile:
    return replace(LEGACY, inline_total_max_bytes=n)


CASES: List[Case] = [
    # --- the inline set and the combined budget (was test_attachment_turn_guard) ---
    Case("empty", [], []),
    Case(
        "under the budget keeps everything in order",
        [f("a.pdf", size=3 * MIB), f("b.pdf", size=3 * MIB)],
        [(N, "pdf", "inline"), (N, "pdf", "inline")],
        profile=_budget(7_500_000),
        requested=6 * MIB,
    ),
    Case(
        "over the budget moves the file that breaks it",
        [f("a.pdf", size=4 * MIB - 1), f("b.pdf", size=4 * MIB - 1)],
        [(N, "pdf", "inline"), (D, "pdf", "over_budget")],
        profile=_budget(7_500_000),
        requested=2 * (4 * MIB - 1),
    ),
    Case(
        "first fit: earlier files win and a later small file still rides",
        [f("a.pdf", size=4 * MIB), f("b.pdf", size=4 * MIB), f("c.pdf", size=1 * MIB)],
        [(N, "pdf", "inline"), (D, "pdf", "over_budget"), (N, "pdf", "inline")],
        profile=_budget(7_500_000),
    ),
    Case(
        "order is attachment order in both outcomes",
        [f(f"{x}.pdf", size=3 * MIB) for x in "zyxw"],
        [(N, "pdf", "inline")] * 2 + [(D, "pdf", "over_budget")] * 2,
        profile=_budget(7_500_000),
    ),
    Case(
        "images count toward the budget",
        [f("photo.png", PNG, 4 * MIB), f("report.pdf", size=4 * MIB)],
        [(N, "image", "inline"), (D, "pdf", "over_budget")],
        profile=_budget(7_500_000),
    ),
    Case(
        "a single image over the budget is dropped, never raised",
        [f("huge.png", PNG, 8 * MIB)],
        [(D, "image", "over_budget")],
        profile=_budget(7_500_000),
    ),
    Case(
        "exactly at the budget is allowed",
        [f("a.png", PNG, 7_500_000)],
        [(N, "image", "inline")],
        profile=_budget(7_500_000),
    ),
    Case(
        "a non-positive budget disables it",
        [f("a.pdf", size=4 * MIB), f("b.pdf", size=4 * MIB)],
        [(N, "pdf", "inline"), (N, "pdf", "inline")],
        profile=_budget(0),
        requested=8 * MIB,
    ),
    Case(
        "the guard off disables the budget",
        [f("a.pdf", size=4 * MIB), f("b.pdf", size=4 * MIB)],
        [(N, "pdf", "inline"), (N, "pdf", "inline")],
        profile=replace(LEGACY, turn_guard=False),
    ),
    Case(
        "files that each pass the per-file gate are caught by the budget; a sheet never counts",
        [f(f"f{i}.pdf", size=4 * MIB - 1024) for i in range(3)] + [f("data.xlsx", XLSX, 6 * MB)],
        [(N, "pdf", "inline"), (D, "pdf", "over_budget"), (D, "pdf", "over_budget"), (TOOL, "tabular", "tabular")],
        requested=3 * (4 * MIB - 1024),
    ),
    # --- over-budget documents by reference (was test_inline_budget_by_reference) ---
    Case(
        "the incident shape keeps every file",
        [
            ref("policy-a.pdf", size=int(5.7 * MB)),
            up("policy-b.pdf", size=int(3.9 * MB)),
            up("policy-c.pdf", size=int(0.17 * MB)),
            up("policy-d.pdf", size=int(4.0 * MB)),
            up("policy-e.pdf", size=int(2.7 * MB)),
        ],
        [
            (OD, "pdf", "reference_class"),
            (N, "pdf", "inline"),
            (N, "pdf", "inline"),
            (OD, "pdf", "over_budget_reference"),
            (N, "pdf", "inline"),
        ],
    ),
    Case(
        "the kill switch restores the drop",
        [
            ref("policy-a.pdf", size=int(5.7 * MB)),
            up("policy-b.pdf", size=int(3.9 * MB)),
            up("policy-c.pdf", size=int(0.17 * MB)),
            up("policy-d.pdf", size=int(4.0 * MB)),
            up("policy-e.pdf", size=int(2.7 * MB)),
        ],
        [(OD, "pdf", "reference_class"), (N, "pdf", "inline"), (N, "pdf", "inline"), (D, "pdf", "over_budget"), (N, "pdf", "inline")],
        profile=replace(LEGACY, over_budget_by_reference=False),
    ),
    Case(
        "an uploaded image over the budget is still dropped; a document goes by reference",
        [up("scan-1.pdf", size=int(3.9 * MB)), up("photo.png", PNG, int(3.5 * MB)), up("notes.docx", DOCX, int(3.6 * MB))],
        [(N, "pdf", "inline"), (D, "image", "over_budget"), (OD, "document", "over_budget_reference")],
    ),
    Case(
        "direct bytes over the budget are still dropped (no row to read later)",
        [f("one.pdf", size=int(3.9 * MB)), f("two.pdf", size=int(3.9 * MB))],
        [(N, "pdf", "inline"), (D, "pdf", "over_budget")],
    ),
    Case(
        "within the budget nothing changes",
        [ref("policy-a.pdf", size=int(5.7 * MB)), up("policy-b.pdf", size=int(3.9 * MB)), up("policy-c.pdf", size=int(2.7 * MB)), up("chart.png", PNG, int(0.3 * MB))],
        [(OD, "pdf", "reference_class"), (N, "pdf", "inline"), (N, "pdf", "inline"), (N, "image", "inline")],
    ),
    # --- reference-class uploads (was test_large_document_turn_path) ---
    Case(
        "a reference gets its own mode beside an inline file",
        [f("memo.pdf", size=1_000), ref("manual.pdf")],
        [(N, "pdf", "inline"), (OD, "pdf", "reference_class")],
    ),
    Case(
        "a reference adds nothing to the inline budget",
        [ref("manual.pdf", size=49 * MIB), f("memo.pdf", size=3 * MIB)],
        [(OD, "pdf", "reference_class"), (N, "pdf", "inline")],
        requested=3 * MIB,
    ),
    Case(
        "direct bytes over the inline limit stay oversized",
        [f("big.pdf", size=5 * MIB)],
        [(D, "pdf", "oversized")],
    ),
    Case(
        "the per-file limit is inclusive",
        [f("at.pdf", size=4 * MIB), f("over.txt", TXT, 4 * MIB + 1)],
        [(N, "pdf", "inline"), (D, "document", "oversized")],
        profile=_budget(0),
    ),
    Case(
        "an image is never oversized by the document limit",
        [f("photo.png", PNG, 5 * MIB)],
        [(N, "image", "inline")],
    ),
    # --- spreadsheets and decks (was test_presentation_attachment_carveout) ---
    Case("a deck goes to its tools", [f("deck.pptx", PPTX)], [(TOOL, "presentation", "presentation")]),
    Case(
        "a tiny deck is still a deck, never oversized",
        [f("small.pptx", PPTX, 3)],
        [(TOOL, "presentation", "presentation")],
    ),
    Case(
        "a huge deck or sheet goes to its tools, never oversized",
        [f("big.pptx", PPTX, 20 * MIB), f("big.xlsx", XLSX, 20 * MIB)],
        [(TOOL, "presentation", "presentation"), (TOOL, "tabular", "tabular")],
    ),
    Case(
        "a deck by extension when the MIME is missing",
        [f("deck.PPTX", "application/octet-stream")],
        [(TOOL, "presentation", "presentation")],
    ),
    Case(
        "a mixed batch lands where it should",
        [f("report.pdf"), f("data.xlsx", XLSX), f("deck.pptx", PPTX), f("data.csv", CSV)],
        [(N, "pdf", "inline"), (TOOL, "tabular", "tabular"), (TOOL, "presentation", "presentation"), (TOOL, "tabular", "tabular")],
    ),
    # --- the model's rules ---
    Case(
        "documents as text: renderable non-PDF documents on a Converse model",
        [f("Minutes.DOCX", DOCX), f("notes.md", MD), f("page.html", HTML), f("log.txt", TXT), f("scan.pdf"), f("photo.png", PNG), f("old.doc", DOC), f("data.csv", CSV)],
        [
            (T, "document", "documents_as_text"),
            (T, "document", "documents_as_text"),
            (T, "document", "documents_as_text"),
            (T, "document", "documents_as_text"),
            (N, "pdf", "inline"),
            (N, "image", "inline"),
            # Legacy .doc: Bedrock reads it as a block, document_read cannot.
            (N, "other", "inline"),
            (TOOL, "tabular", "tabular"),
        ],
        profile=replace(LEGACY, documents_as_text=True),
    ),
    Case(
        "documents as text off: the same set stays native",
        [f("Minutes.docx", DOCX), f("log.txt", TXT)],
        [(N, "document", "inline"), (N, "document", "inline")],
    ),
    Case(
        "off Converse a renderable document is always text; a PDF and an image stay native",
        [f("Minutes.docx", DOCX), f("log.txt", TXT), f("scan.pdf"), f("photo.png", PNG), f("old.doc", DOC)],
        [
            (T, "document", "provider_needs_text"),
            (T, "document", "provider_needs_text"),
            (N, "pdf", "inline"),
            (N, "image", "inline"),
            (N, "other", "inline"),
        ],
        model=MANTLE,
    ),
    Case(
        "off Converse with the flag on: the same reason",
        [f("Minutes.docx", DOCX)],
        [(T, "document", "provider_needs_text")],
        model=MANTLE,
        profile=replace(LEGACY, documents_as_text=True),
    ),
    Case(
        "a text-only model: documents as text, images and unreadable files dropped",
        [f("photo.png", PNG), f("scan.pdf", size=400_000), f("old.doc", DOC), f("ok.txt", TXT, 9_000), f("memo.docx", DOCX)],
        [
            (D, "image", "text_only_image"),
            (T, "pdf", "text_only_model"),
            (D, "other", "text_only_unreadable"),
            (T, "document", "text_only_model"),
            (T, "document", "text_only_model"),
        ],
        model=TEXT_ONLY,
        tokens=[0, 20_000 // 4, 0, 9_000 // 4, 1_000 // 4],
        text_char_limit=20_000,
    ),
    Case(
        "a text-only model: the per-document share has a floor",
        [f(f"d{i}.txt", TXT) for i in range(20)],
        [(T, "document", "text_only_model")] * 20,
        model=TEXT_ONLY,
        text_char_limit=4_000,
    ),
    Case(
        "a text-only model wins over documents as text",
        [f("Minutes.docx", DOCX)],
        [(T, "document", "text_only_model")],
        model=TEXT_ONLY,
        profile=replace(LEGACY, documents_as_text=True),
    ),
    Case(
        "a text-only model: an unknown file type is unreadable",
        [f("blob.bin", "application/octet-stream")],
        [(D, "other", "text_only_unreadable")],
        model=TEXT_ONLY,
    ),
    Case(
        "the model rules never touch a reference, a tool file or a dropped file",
        [ref("manual.pdf"), f("data.xlsx", XLSX), f("big.pdf", size=5 * MIB)],
        [(OD, "pdf", "reference_class"), (TOOL, "tabular", "tabular"), (D, "pdf", "oversized")],
        model=TEXT_ONLY,
    ),
    Case(
        "planned before the model is known: the byte rules only",
        [f("Minutes.docx", DOCX), f("photo.png", PNG)],
        [(N, "document", "inline"), (N, "image", "inline")],
        model=None,
        profile=replace(LEGACY, documents_as_text=True),
    ),
    # --- the fixture-only override (§8.2) ---
    Case(
        "forced on_demand: the resolver returned it by reference",
        [ref("small.pdf", size=1_000, forced_mode="on_demand")],
        [(OD, "pdf", "forced")],
    ),
    Case(
        "forced native: past the per-file limit and never moved out by the budget",
        [up("a.pdf", size=5 * MIB, forced_mode="native"), up("b.pdf", size=3 * MIB)],
        [(N, "pdf", "forced"), (OD, "pdf", "over_budget_reference")],
    ),
    Case(
        "forced native docx stays a block on Converse with documents-as-text on",
        [up("memo.docx", DOCX, forced_mode="native"), up("note.docx", DOCX)],
        [(N, "document", "forced"), (T, "document", "documents_as_text")],
        profile=replace(LEGACY, documents_as_text=True),
    ),
    Case(
        "forced native off Converse is still text: those formatters cannot read the block",
        [up("memo.docx", DOCX, forced_mode="native")],
        [(T, "document", "provider_needs_text")],
        model=MANTLE,
    ),
    Case(
        "forced text: a pdf and a docx, held whole whatever the per-file limit",
        [up("a.pdf", size=5 * MIB, forced_mode="text"), up("b.docx", DOCX, forced_mode="text")],
        [(T, "pdf", "forced"), (T, "document", "forced")],
        tokens=[5 * MIB // 4, 250],
    ),
    Case(
        "forced text on an image plans as usual",
        [up("p.png", PNG, forced_mode="text")],
        [(N, "image", "inline")],
    ),
    Case(
        "a forced value the profile cannot realise plans as usual",
        [up("a.pdf", forced_mode="search"), up("b.xlsx", XLSX, forced_mode="native")],
        [(N, "pdf", "inline"), (TOOL, "tabular", "tabular")],
    ),
    # --- token estimates (legacy: from facts in hand, no parse) ---
    Case(
        "tokens: bytes/4 for documents, a flat estimate for images, pages when the row knows",
        [f("a.txt", TXT, 4_000), f("p.png", PNG), ref("m.pdf", page_count=10), f("x.xlsx", XLSX)],
        [(N, "document", "inline"), (N, "image", "inline"), (OD, "pdf", "reference_class"), (TOOL, "tabular", "tabular")],
        tokens=[1_000, 1_500, 0, 0],
    ),
]


def _rows(plan) -> List[Tuple[str, str, str]]:
    return [(e.mode, e.file_class, e.reason) for e in plan.entries]


def _plan(case: Case):
    return plan_attachments(case.files, case.model, ConversationFacts(), profile=case.profile)


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_table(case: Case):
    plan = _plan(case)

    assert _rows(plan) == case.expect
    assert [e.index for e in plan.entries] == list(range(len(case.files)))
    assert [e.upload_id for e in plan.entries] == [x.upload_id for x in case.files]
    if case.tokens is not None:
        assert [e.tokens for e in plan.entries] == case.tokens
    if case.requested is not None:
        assert plan.requested_inline_bytes == case.requested
    if case.text_char_limit is not None:
        assert plan.text_char_limit == case.text_char_limit


#: sha256 over every row's inputs and the plan the planner produced for them.
#: A change here is a change to who gets what: say so in the PR.
TABLE_DIGEST = "c0a65269a994c043930829c3444b51c5d2a2fa7347157657d5f99d7fe1b83ecd"


#: Fields added for the ``token`` profile (phase 3). Omitted from a payload while
#: at their default, so the ``legacy`` table's digest is the one it had before
#: they existed: its rows provably did not change.
_PHASE3_DEFAULTS = {
    "text_tokens": None, "native_tokens": None, "has_text_layer": None,
    "model_id": None, "chat_ceiling": None, "document_budget_share": 0.0,
}


def _facts_dict(obj) -> dict:
    return {k: v for k, v in asdict(obj).items() if not (k in _PHASE3_DEFAULTS and v == _PHASE3_DEFAULTS[k])}


def _table_payload(cases=None) -> str:
    rows = []
    for case in CASES if cases is None else cases:
        plan = _plan(case)
        rows.append(
            {
                "name": case.name,
                "files": [_facts_dict(x) for x in case.files],
                "model": _facts_dict(case.model) if case.model else None,
                "profile": _facts_dict(case.profile),
                "plan": [asdict(e) for e in plan.entries],
                "textCharLimit": plan.text_char_limit,
            }
        )
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def test_the_table_digest_is_pinned():
    digest = hashlib.sha256(_table_payload().encode("utf-8")).hexdigest()
    assert digest == TABLE_DIGEST, f"the planner's table changed: {digest}"


# ---------------------------------------------------------------------------
# Properties the table alone does not state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sizes",
    [
        [4 * MIB, 4 * MIB],
        [4 * MIB] * 5,
        [3 * MIB] * 3,
        [2_600_000] * 3,
        [7_500_000, 1],
        [1] * 5 + [4 * MIB] * 5,
    ],
)
def test_the_kept_set_encodes_under_the_event_quota(sizes):
    """What survives the default budget is writable as one AgentCore Memory event
    (base64 in a ``blob``), so the turn degrades to a note, never ``SessionException``."""
    files = [f(f"f{i}.png", PNG, s) for i, s in enumerate(sizes)]
    plan = plan_attachments(files, CLAUDE, profile=LEGACY)

    kept = [files[i] for i in plan.indices(ap.MODE_NATIVE)]
    encoded = sum(4 * math.ceil(x.size_bytes / 3) for x in kept)
    assert encoded <= EVENT_QUOTA_BYTES
    # Nothing is lost silently: every file has exactly one entry.
    assert len(plan.entries) == len(files)


def test_planning_twice_with_the_model_is_idempotent():
    files = [f("a.txt", TXT), f("b.png", PNG), f("c.docx", DOCX)]
    first = plan_attachments(files, TEXT_ONLY, profile=LEGACY)
    again = ap.refine_for_model(first, files, TEXT_ONLY, profile=LEGACY)
    assert again == first


def test_planning_before_and_after_the_model_agrees_with_planning_once():
    files = [up("a.docx", DOCX), up("b.pdf", size=5 * MIB), f("c.png", PNG), ref("d.pdf")]
    profile = replace(LEGACY, documents_as_text=True)
    staged = ap.refine_for_model(plan_attachments(files, None, profile=profile), files, CLAUDE, profile=profile)
    assert staged == plan_attachments(files, CLAUDE, profile=profile)


def test_an_outcome_is_recorded_not_chosen():
    files = [f("data.xlsx", XLSX), f("a.pdf")]
    plan = plan_attachments(files, CLAUDE, profile=LEGACY).with_outcome(0, ap.REASON_PERSIST_FAILED)
    assert _rows(plan) == [(D, "tabular", "persist_failed"), (N, "pdf", "inline")]
    # A later model refinement keeps it.
    assert _rows(ap.refine_for_model(plan, files, TEXT_ONLY, profile=LEGACY))[0] == (D, "tabular", "persist_failed")


# ---------------------------------------------------------------------------
# The count cap, before anything is fetched
# ---------------------------------------------------------------------------


def _named(*names: str) -> list:
    return [SimpleNamespace(filename=n) for n in names]


COUNT_CASES = [
    # (name, direct, ids, folders, folders_dropped, profile, kept_direct, kept_ids, kept_folders, dropped_names, dropped_total, resolver_max)
    ("under the cap", 1, 2, 0, 0, LEGACY, 1, 2, 0, [], 0, None),
    ("ids beyond the cap are counted, not fetched", 0, 7, 0, 0, LEGACY, 0, 5, 0, [], 2, None),
    ("direct files beyond the cap are named", 6, 0, 0, 0, LEGACY, 5, 0, 0, ["d5.pdf"], 1, None),
    ("direct first, ids fill the remainder", 2, 5, 0, 0, LEGACY, 2, 3, 0, [], 2, None),
    ("direct overflow leaves no room for ids", 6, 1, 0, 0, LEGACY, 5, 0, 0, ["d5.pdf"], 2, None),
    ("a non-positive cap disables it", 9, 9, 0, 0, replace(LEGACY, max_files_per_message=0), 9, 9, 0, [], 0, None),
    ("folders count one each, after the files", 0, 2, 2, 0, replace(LEGACY, max_files_per_message=3), 0, 2, 1, [], 1, None),
    ("the folder limit of its own is always counted", 0, 0, 2, 1, LEGACY, 0, 0, 2, [], 1, None),
    ("the guard off: nothing capped, the resolver's old backstop", 9, 9, 2, 1, replace(LEGACY, turn_guard=False), 9, 9, 2, [], 1, 5),
]


@pytest.mark.parametrize("case", COUNT_CASES, ids=[c[0] for c in COUNT_CASES])
def test_the_count_cap(case):
    (_, n_direct, n_ids, n_folders, folders_dropped, profile, k_direct, k_ids, k_folders, names, total, backstop) = case
    direct = _named(*[f"d{i}.pdf" for i in range(n_direct)])
    ids = [f"u{i}" for i in range(n_ids)]
    folders = [f"fa-{i}" for i in range(n_folders)]

    cap = cap_message_attachments(direct, ids, folders, profile, folders_dropped=folders_dropped)

    assert cap.direct_files == direct[:k_direct]
    assert cap.upload_ids == ids[:k_ids]
    assert cap.folder_ids == folders[:k_folders]
    assert cap.dropped_names == names
    assert cap.dropped_total == total
    assert cap.resolver_max_files == backstop


# ---------------------------------------------------------------------------
# What the resolver fetches (asked before any S3 read)
# ---------------------------------------------------------------------------


def _meta(filename: str = "policy.pdf", mime: str = PDF, size: int = 1_000, **kw) -> file_models.FileMetadata:
    return file_models.FileMetadata(
        upload_id="up-1",
        user_id="user-1",
        session_id="sess-1",
        filename=filename,
        mime_type=mime,
        size_bytes=size,
        s3_key="k",
        s3_bucket="b",
        status=file_models.FileStatus.READY,
        **kw,
    )


@pytest.mark.parametrize(
    "meta,expected",
    [
        (_meta(size=4 * MIB), False),
        (_meta(size=4 * MIB + 1), True),
        (_meta(size=file_models.LARGE_DOCUMENT_MAX_BYTES + 1), False),
        (_meta("big.xlsx", XLSX, 10 * MIB), False),
        (_meta("deck.pptx", PPTX, 10 * MIB), False),
        (_meta("photo.png", PNG, 10 * MIB), False),
        (_meta(size=1_000, delivery="reference"), True),
        (_meta("photo.png", PNG, 1_000, delivery="reference"), False),
        (_meta(size=1_000, delivery_override="on_demand"), True),
        (_meta("photo.png", PNG, 1_000, delivery_override="on_demand"), False),
        (_meta(size=20 * MIB, delivery_override="native"), False),
        (_meta(size=20 * MIB, delivery_override="text"), False),
        (_meta(size=1_000, delivery="reference", delivery_override="native"), False),
    ],
    ids=[
        "at the inline limit",
        "one byte over",
        "over the large-document ceiling",
        "a large sheet",
        "a large deck",
        "a large image",
        "a folder file",
        "a folder image",
        "forced on_demand",
        "forced on_demand on an image",
        "forced native",
        "forced text",
        "forced native wins over a folder file",
    ],
)
def test_delivers_by_reference(meta, expected):
    assert delivers_by_reference(meta) is expected


# ---------------------------------------------------------------------------
# Classification and the vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,mime,expected",
    [
        ("a.pdf", PDF, "pdf"),
        ("a.docx", DOCX, "document"),
        ("a.txt", TXT, "document"),
        ("a.xlsx", XLSX, "tabular"),
        ("a.csv", CSV, "tabular"),
        ("a.pptx", PPTX, "presentation"),
        ("a.png", PNG, "image"),
        ("a.bin", "application/octet-stream", "other"),
        ("old.ppt", "application/vnd.ms-powerpoint", "other"),
    ],
)
def test_file_class(name, mime, expected):
    assert file_class(name, mime) == expected


def test_documents_as_text_matches_the_projection():
    from apis.shared.models.document_cap import RENDERABLE_FORMATS

    assert ap.DOCUMENTS_AS_TEXT_EXTENSIONS == RENDERABLE_FORMATS


@pytest.mark.parametrize(
    "meta,expected",
    [
        (_meta(size=1_000, delivery_override="on_demand"), True),
        (_meta(size=4 * MIB + 1, delivery_override="on_demand"), False),
        (_meta(size=1_000), False),
        (_meta(size=1_000, delivery_override="native"), False),
    ],
    ids=["small forced on_demand", "reference-class forced on_demand", "a user row", "forced native"],
)
def test_reads_from_original(meta, expected):
    """A small forced on_demand file renders like an over-budget reference, never a "preparing" stub."""
    assert ap.reads_from_original(meta) is expected


def test_the_forceable_modes_are_the_row_values():
    assert ap.FORCEABLE_MODES == file_models.DELIVERY_OVERRIDE_VALUES


def test_the_legacy_profile_reads_todays_constants(monkeypatch):
    for var in (
        "ATTACHMENT_TURN_GUARD_ENABLED",
        "INLINE_BUDGET_BY_REFERENCE_ENABLED",
        "CONVERSE_DOCUMENTS_AS_TEXT_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    assert ap.legacy_profile() == LimitsProfile(
        name="legacy",
        max_files_per_message=file_models.MAX_FILES_PER_MESSAGE,
        inline_document_max_bytes=file_models.INLINE_DOCUMENT_MAX_BYTES,
        inline_total_max_bytes=file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
        text_only_max_chars=file_models.TEXT_ONLY_DOCUMENTS_MAX_CHARS,
    )
    # And the defaults are the numbers the table writes out.
    assert replace(ap.legacy_profile(), name="legacy") == LEGACY


@pytest.mark.parametrize(
    "env,field_name",
    [
        ("ATTACHMENT_TURN_GUARD_ENABLED", "turn_guard"),
        ("INLINE_BUDGET_BY_REFERENCE_ENABLED", "over_budget_by_reference"),
    ],
)
def test_a_kill_switch_is_a_profile_input(monkeypatch, env, field_name):
    monkeypatch.setenv(env, "false")
    assert getattr(ap.legacy_profile(), field_name) is False


def test_documents_as_text_is_a_profile_input(monkeypatch):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", "true")
    assert ap.legacy_profile().documents_as_text is True


def test_the_flag_selects_the_profile(monkeypatch):
    for value in ("", "false", "1", "yes"):
        monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", value)
        assert ap.current_limits_profile().name == ap.PROFILE_LEGACY
    monkeypatch.delenv("ATTACHMENT_PLAN_ENABLED")
    assert ap.current_limits_profile().name == ap.PROFILE_LEGACY
    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "TRUE")
    assert ap.current_limits_profile().name == ap.PROFILE_TOKEN


def test_the_projection_follows_the_profile(monkeypatch):
    """The per-call projection renders exactly what the plan said is text."""
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", "false")
    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "false")
    assert ap.documents_as_text() is False
    monkeypatch.setenv("ATTACHMENT_PLAN_ENABLED", "true")
    assert ap.documents_as_text() is True


def test_the_token_profile_is_the_legacy_byte_limits_plus_the_budget(monkeypatch):
    monkeypatch.setenv("CONVERSE_DOCUMENTS_AS_TEXT_ENABLED", "false")
    legacy, token = ap.legacy_profile(), ap.token_profile()
    assert replace(token, name=legacy.name, documents_as_text=legacy.documents_as_text, document_budget_share=0.0) == legacy
    assert (token.name, token.documents_as_text, token.document_budget_share) == ("token", True, 0.5)


# ---------------------------------------------------------------------------
# What the cost row carries
# ---------------------------------------------------------------------------


def test_the_row_is_ids_codes_and_numbers():
    files = [up("Secret Plan.pdf"), f("photo.png", PNG), f("data.xlsx", XLSX)]
    row = plan_attachments(files, CLAUDE, profile=LEGACY, dropped_over_count=1).to_row()

    assert row == {
        "profile": "legacy",
        "files": [
            {"mode": "native", "class": "pdf", "tokens": 250, "reason": "inline", "uploadId": "up-Secret Plan.pdf"},
            {"mode": "native", "class": "image", "tokens": 1_500, "reason": "inline"},
            {"mode": "tool", "class": "tabular", "tokens": 0, "reason": "tabular"},
        ],
        "droppedOverCount": 1,
    }
    # No filename anywhere but inside the test's own fake upload id.
    assert "photo" not in json.dumps(row) and "data.xlsx" not in json.dumps(row)


def test_no_attachments_writes_nothing():
    assert plan_attachments([], CLAUDE, profile=LEGACY).to_row() is None
    assert plan_attachments([], CLAUDE, profile=LEGACY, dropped_over_count=2).to_row() == {
        "profile": "legacy",
        "files": [],
        "droppedOverCount": 2,
    }



# ---------------------------------------------------------------------------
# The ``token`` profile (phase 3, spec §4.3 and §5.2)
# ---------------------------------------------------------------------------

TOKEN = replace(LEGACY, name="token", documents_as_text=True, document_budget_share=0.5)
#: The catalog windows and the ceilings CompactionPolicy gives them on dev.
HAIKU55 = ModelFacts(context_window=100_000, model_id="us.anthropic.claude-haiku-5-5", chat_ceiling=50_000)
SONNET46 = ModelFacts(context_window=1_000_000, model_id="us.anthropic.claude-sonnet-4-6", chat_ceiling=100_000)
HAIKU45 = ModelFacts(context_window=200_000, model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0", chat_ceiling=100_000)
OPUS55 = ModelFacts(context_window=1_000_000, model_id="us.anthropic.claude-opus-5-5", chat_ceiling=100_000)
MANTLE_T = ModelFacts(converse=False, context_window=272_000, model_id="openai.gpt-5.4", chat_ceiling=100_000)
TEXT_ONLY_T = ModelFacts(text_only=True, model_id="zai.glm-5", chat_ceiling=100_000)
NO_CEILING = ModelFacts(model_id="us.anthropic.claude-haiku-5-5")
#: A prose page's text layer in the delivery fixtures: ``tokens.text`` / pages.
PROSE_PAGE = 960


def pdf(name: str, pages: int, text: Optional[int] = None, *, row: bool = True, scanned: bool = False, **kw) -> FileFacts:
    """A PDF with row facts: ``text`` defaults to a prose page's text a page."""
    text = PROSE_PAGE * pages if text is None else text
    make = up if row else f
    return make(
        name, PDF, 200 * pages, page_count=pages, text_tokens=0 if scanned else text,
        native_tokens=1_500 * pages, has_text_layer=not scanned, **kw,
    )


def doc(name: str, mime: str, text: int, *, row: bool = True, **kw) -> FileFacts:
    make = up if row else f
    return make(name, mime, max(1_000, text // 3), text_tokens=text, native_tokens=text, has_text_layer=True, **kw)


TOKEN_CASES: List[Case] = [
    # --- the validation turns (the task's dev checks), on both models ---
    Case("a sparse 10-page PDF fits Haiku 5.5's 25k", [pdf("short.pdf", 10, 3_000)],
         [(N, "pdf", "within_budget")], HAIKU55, TOKEN, tokens=[15_650 + 4_200]),
    Case("a prose 10-page PDF is ~29k on Haiku 5.5: over its 25k", [pdf("ten.pdf", 10)],
         [(OD, "pdf", "over_document_budget")], HAIKU55, TOKEN, tokens=[0]),
    Case("the same PDF is ~24.8k on Sonnet 4.6: within its 50k", [pdf("ten.pdf", 10)],
         [(N, "pdf", "within_budget")], SONNET46, TOKEN, tokens=[15_650 + 9_120]),
    Case("a 150-page PDF goes on demand on Haiku 5.5", [pdf("long.pdf", 150)],
         [(OD, "pdf", "over_document_budget")], HAIKU55, TOKEN),
    Case("a 150-page PDF goes on demand on Sonnet 4.6", [pdf("long.pdf", 150)],
         [(OD, "pdf", "over_document_budget")], SONNET46, TOKEN),
    Case("a 60k-token docx goes on demand on either model",
         [doc("memo.docx", DOCX, 60_000)], [(OD, "document", "over_document_budget")], SONNET46, TOKEN),
    Case("three small mixed files: text, an image, text",
         [doc("notes.docx", DOCX, 2_000), f("chart.png", PNG, 200_000, upload_id="up-chart"), doc("readme.md", MD, 1_000)],
         [(T, "document", "within_budget"), (N, "image", "inline"), (T, "document", "within_budget")],
         HAIKU55, TOKEN, tokens=[2_800, 1_500, 1_400]),
    # --- the budget is spent in attachment order ---
    Case("first fit: the second PDF no longer fits, a later small file still does",
         [pdf("a.pdf", 12), pdf("b.pdf", 12), doc("c.txt", TXT, 1_000)],
         [(N, "pdf", "within_budget"), (OD, "pdf", "over_document_budget"), (T, "document", "within_budget")],
         SONNET46, TOKEN, tokens=[18_780 + 10_944, 0, 950]),
    Case("an image counts against the budget it cannot leave",
         [f("p.png", PNG, upload_id="up-p"), doc("a.txt", TXT, 17_000)],
         [(N, "image", "inline"), (OD, "document", "over_document_budget")], HAIKU55, TOKEN),
    # --- every non-PDF document is text, never a native block ---
    Case("html, md and txt are text whatever the documents-as-text flag says",
         [doc("a.html", HTML, 100), doc("b.md", MD, 100), doc("c.txt", TXT, 100), doc("d.docx", DOCX, 100)],
         [(T, "document", "within_budget")] * 4, SONNET46, replace(TOKEN, documents_as_text=False)),
    Case("off Converse the ladder is the same", [doc("a.docx", DOCX, 2_000), pdf("b.pdf", 2)],
         [(T, "document", "within_budget"), (N, "pdf", "within_budget")], MANTLE_T, TOKEN),
    # --- PDF page limits (the phase 0 probe) ---
    Case("past Haiku 4.5's 100 pages, a sparse PDF's text fits: text",
         [pdf("sparse.pdf", 150, 20_000)], [(T, "pdf", "pdf_page_limit")], HAIKU45, TOKEN, tokens=[19_000]),
    Case("the same PDF is within Opus 5.5's 600 pages but its images are not within the budget",
         [pdf("sparse.pdf", 150, 20_000)], [(OD, "pdf", "over_document_budget")], OPUS55, TOKEN),
    Case("a scanned PDF past the page limit has no text to send: on demand",
         [pdf("scan.pdf", 150, scanned=True)], [(OD, "pdf", "over_document_budget")], HAIKU45, TOKEN),
    # --- costs without facts, and files with nothing to read them from later ---
    Case("no facts yet: today's rules for this turn, counted at the bytes estimate",
         [up("old.docx", DOCX, 40_000), up("fresh.pdf", PDF, 59_470), doc("c.md", MD, 20_000)],
         [(T, "document", "facts_pending"), (N, "pdf", "facts_pending"), (OD, "document", "over_document_budget")],
         HAIKU55, TOKEN, tokens=[14_000, 20_814, 0]),
    Case("over the budget with no row: sent as text and recorded",
         [doc("pasted.docx", DOCX, 60_000, row=False)], [(T, "document", "no_row")], HAIKU55, TOKEN, tokens=[84_000]),
    Case("a direct PDF over the budget with no row stays native",
         [pdf("direct.pdf", 30, row=False)], [(N, "pdf", "no_row")], HAIKU55, TOKEN),
    # --- never dropped where it can be read later ---
    Case("a document over the inline limit with a row goes on demand, not dropped",
         [up("big.pdf", PDF, 5 * MIB), f("big2.pdf", PDF, 5 * MIB)],
         [(OD, "pdf", "over_budget_reference"), (D, "pdf", "oversized")], SONNET46, TOKEN),
    Case("reference class, spreadsheets and decks are as before",
         [ref("huge.pdf", page_count=400), up("t.xlsx", XLSX), up("d.pptx", PPTX)],
         [(OD, "pdf", "reference_class"), (TOOL, "tabular", "tabular"), (TOOL, "presentation", "presentation")],
         SONNET46, TOKEN),
    Case("a forced arm keeps its mode and is not counted",
         [pdf("arm.pdf", 100, forced_mode="native"), pdf("b.pdf", 10)],
         [(N, "pdf", "forced"), (N, "pdf", "within_budget")], SONNET46, TOKEN),
    # --- where the token profile defers ---
    Case("a TEXT-only model keeps its extraction rules",
         [doc("a.docx", DOCX, 1_000), f("p.png", PNG)],
         [(T, "document", "text_only_model"), (D, "image", "text_only_image")], TEXT_ONLY_T, TOKEN),
    Case("no known ceiling: text by the profile, no budget",
         [doc("a.docx", DOCX, 90_000), pdf("b.pdf", 200)],
         [(T, "document", "documents_as_text"), (N, "pdf", "inline")], NO_CEILING, TOKEN),
    Case("before the model is known the token profile plans the byte rules",
         [pdf("a.pdf", 200), doc("b.docx", DOCX, 90_000)], [(N, "pdf", "inline"), (N, "document", "inline")],
         None, TOKEN),
]


@pytest.mark.parametrize("case", TOKEN_CASES, ids=[c.name for c in TOKEN_CASES])
def test_the_token_table(case: Case):
    test_the_table(case)


#: The ``token`` table's own digest. A change here is a change to who gets what.
TOKEN_TABLE_DIGEST = "287ff5bf6f34d18c3787f2b89a09715ecd3b5277922ad711e768af8836849149"


def test_the_token_table_digest_is_pinned():
    digest = hashlib.sha256(_table_payload(TOKEN_CASES).encode("utf-8")).hexdigest()
    assert digest == TOKEN_TABLE_DIGEST, f"the token table changed: {digest}"


@pytest.mark.parametrize("model_id,factor", [
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 0.95),
    ("us.anthropic.claude-sonnet-4-6", 0.95),
    ("us.anthropic.claude-haiku-5-5", 1.40),
    ("us.anthropic.claude-opus-4-7", 1.40),
    ("us.anthropic.claude-opus-5-5", 1.40),
    ("global.anthropic.claude-sonnet-5", 1.40),
    (None, 1.40),
])
def test_tokenizer_factor_is_the_measured_one(model_id, factor):
    assert ap.tokenizer_factor(model_id) == factor


def test_the_budget_is_half_the_ceiling():
    assert ap.document_budget(HAIKU55, TOKEN) == 25_000
    assert ap.document_budget(SONNET46, TOKEN) == 50_000
    assert ap.document_budget(SONNET46, LEGACY) is None
    assert ap.document_budget(NO_CEILING, TOKEN) is None


@pytest.mark.parametrize("model,limit", [
    (HAIKU45, 100), (HAIKU55, 600), (SONNET46, 600), (OPUS55, 600), (ModelFacts(context_window=200_000), 100), (ModelFacts(), 100),
])
def test_pdf_page_limit_is_the_probes(model, limit):
    assert ap.pdf_page_limit(model) == limit


def test_the_measured_fixture_costs_what_bedrock_billed():
    """``delivery_report_30k.pdf``: 32 pages, ``tokens.text`` 30,779 (§2.1)."""
    fixture = pdf("delivery_report_30k.pdf", 32, 30_779)
    assert abs(ap.document_cost(fixture, "pdf", N, HAIKU55) - 92_896) / 92_896 < 0.01
    assert abs(ap.document_cost(fixture, "pdf", N, SONNET46) - 78_966) / 78_966 < 0.01
    assert abs(ap.document_cost(fixture, "pdf", T, HAIKU55) - 43_225) / 43_225 < 0.01


@pytest.mark.parametrize("model", [HAIKU55, SONNET46, MANTLE_T, TEXT_ONLY_T, NO_CEILING])
def test_token_planning_is_idempotent(model):
    files = [pdf("a.pdf", 10), doc("b.docx", DOCX, 30_000), f("p.png", PNG), doc("c.md", MD, 500)]
    once = plan_attachments(files, model, ConversationFacts(), profile=TOKEN)
    assert ap.refine_for_model(once, files, model, profile=TOKEN) == once
    before = plan_attachments(files, None, ConversationFacts(), profile=TOKEN)
    assert ap.refine_for_model(before, files, model, profile=TOKEN) == once


def test_the_token_profile_never_drops_a_file_it_can_read_later():
    files = [pdf(f"p{i}.pdf", 40 * (i + 1)) for i in range(3)] + [doc("d.docx", DOCX, 200_000), up("big.pdf", PDF, 9 * MIB)]
    for model in (HAIKU55, SONNET46, HAIKU45, OPUS55, MANTLE_T):
        plan = plan_attachments(files, model, ConversationFacts(), profile=TOKEN)
        assert all(e.mode != D for e in plan.entries), model

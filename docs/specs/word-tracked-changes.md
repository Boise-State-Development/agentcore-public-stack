# Word tracked changes: reading redlines and writing them

Status: shipped, behind `WORD_TRACKED_CHANGES_ENABLED` (on by default); whole-paragraph insertions
(§3a), the opt-in `if_unplaced` argument (§3b) and whole-paragraph deletions (§3c)
ship with the tool, and comparing two documents into a redline (§3d) ships as a tool
beside it, with no flags of their own (§4a–§4d).

## 1. Problem

Contract review runs on Word's tracked changes. The platform handled them in
exactly one place, knowledge-base ingestion (`apis/shared/kb_backend/docx_revisions.py`).
Everywhere a chat model meets a `.docx` lost them:

| Surface | What the model saw for `State of [deleted: Idaho][inserted: Delaware]` |
|---|---|
| Inline chat attachment (Converse `document` block) | `State of IdahoDelaware` (measured on dev, Haiku 4.5, 2026-10-12). Bedrock's parser keeps every run and drops the markup, so both sides of each change are merged into one string |
| `document_read`, the upload digest, text-only models (`docx_paragraphs`) | `State of Delaware`. `w:delText` is not `w:t`, so deletions vanish and insertions read as settled text |
| `read_word_document` (python-docx in Code Interpreter) | `State of `. `Paragraph.text` reads only direct-child runs, and both sides of a change sit inside `w:ins` / `w:del` |

The Word tools could not write a redline either. `modify_word_document` runs
python-docx, which has no revision API, so every edit was a silent rewrite of the
contract.

## 2. Reading

One transform, already proven in ingestion: `annotate_tracked_changes(bytes)`
rewrites each revised span as an ordinary run reading `[deleted: …]` or
`[inserted: …]`, adds one note paragraph naming the authors, and inlines comments
as `[comment by …: …]`. A document with no tracked changes returns `None` and is
used unchanged, so every other upload reads exactly as before.

With the flag on it is applied at the three surfaces:

- **R1 `docx_paragraphs`** (`apis/shared/files/document_read.py`). That one change
  covers `document_read`, the upload digest (app-api) and the text-only-model
  extraction (`_adapt_attachments_for_model`). The digest and `document_read`
  must number paragraphs the same way (the note adds one at the top), which is
  why the flag is set on **app-api as well as the Runtime**.
- **R2 `read_word_document`**. The bytes are annotated before they go to the
  sandbox. python-docx then reads the markers as ordinary runs, the same way it
  does for Docling.
- **R3 Inline attachments**. At the end of `_resolve_turn_attachments`
  (`preamble.files`), each inline `.docx` with tracked changes has its bytes
  swapped for the annotated package. It stays a `docx` document block, so its
  name, format and the offload/digest paths are unchanged. Measured on dev: the
  annotated package comes through Converse verbatim (unlike the managed KB's
  model-based parser, which is why ingestion sends that engine plain text).

Prompt cache: inline document bytes are stripped from restored history
(`_strip_document_bytes`), so R3 affects only the turn that attaches the file.
The transform is a pure function of the bytes, so a retry produces the same
message.

TTFT: R3 runs before the first token, inside `preamble.files`, but only on a
turn that attaches a `.docx` while the flag is on, and off the event loop. With
the flag off it costs one environment read. Measured locally (median of 30,
including the base64 round trip): a `.docx` with no tracked changes costs
**0.3 ms**; a 215 KB `document.xml` (about 50 pages) with 40 changes costs
**3.5 ms**; an 859 KB one with 150 changes costs **13 ms**. Word's own markup is
heavier per paragraph than the synthetic test text, so a real 50-page redline
likely lands at 5–12 ms. **Accepted by the developer driving the change, 2026-10-12.** If it proves too much, the rewrite can start as a task in
`_resolve_turn_attachments` and be awaited at prompt build, which hides it behind
the session-state, quota and model stages.

## 3. Writing: `redline_word_document`

Structured edits, not code. Each change is `{find, replace?, comment?, occurrence?}`:

- `find`: exact text inside one paragraph. Matching treats curly and straight
  quotes, dash variants and non-breaking spaces as equal, and any run of
  whitespace as one space.
- `replace`: the new wording. `""` deletes. The edit is trimmed to the words that
  actually differ, so `find: "within thirty (30) days"` / `replace: "within
  ninety (90) days"` deletes `thirty (30)` and inserts `ninety (90)`. A
  replacement that only adds words becomes a pure insertion.
- `comment`: a Word comment anchored on the change (or on `find` alone).
- `occurrence`: 1-based, required only when `find` appears more than once.

All or nothing. Every change is located before any is applied. A miss, an
ambiguous match, an overlap between two changes, or a match inside existing
tracked changes, a hyperlink or a field fails the call with one line per problem,
and nothing is saved. The output is a new file. The source is never modified.

The author defaults to the signed-in user's name, because a redline goes back to
the counterparty as theirs. The model passes `author` when the user asks for
something else.

Engine: `agents/builtin_tools/office/docx_redline.py`, stdlib only, no Code
Interpreter round trip. It re-serializes only the paragraphs it edits and splices
them back into `word/document.xml`. Every other byte of the package is kept,
including namespace declarations that `mc:Ignorable` refers to, which
ElementTree would drop. Comments create or extend `word/comments.xml` and add the
content-type override and the relationship when they are missing.

Not in v1: edits that span paragraphs, headers and footers, footnotes, moves,
and formatting-only revisions. A match touching any of these fails with a reason
instead of producing a malformed file. (Whole new paragraphs were added later,
§3a.)

Provisioning: the tool joins the Word family (the `create_word_document` gate)
only while the flag is on. Off, its spec never reaches `toolConfig`. On, it adds
one constant spec of about 515 tokens (cl100k) for users who have the Word tools.
The spec is part of the cacheable prefix, so the cost is a one-time cache write per
session, then cache reads.

## 3a. Inserting whole paragraphs

**Why.** v1 could only change wording inside a paragraph that already exists. A
contract redline also adds clauses: a missing Survival section, a new item in a
lettered list. Without a way to add a paragraph, a reviewer agent had two bad
options. It could append the clause to the end of a neighbouring paragraph and
ask a human to move it, losing the heading format and numbering. Or it could
retype a 20K-token python-docx redline engine into `modify_word_document` on
every run (several minutes of output on Opus 5.5), or run that engine from a skill
script, which executes user-authored code in a networked sandbox.

**Shape.** A change gives `insert` and `paragraphs` instead of `replace`:

```json
{"find": "indirect, incidental or consequential damages",
 "insert": "after",
 "paragraphs": [{"text": "Survival", "like": "Limitation of Liability"},
                {"text": "Sections 3 and 4 survive termination of this Agreement."}],
 "comment": "Group F: survival is mandatory."}
```

- `find` names the anchor paragraph, matched exactly as for an edit: one
  paragraph, `occurrence` when the wording repeats.
- `insert`: `"after"` or `"before"` that paragraph.
- `paragraphs`: the new paragraphs in order. Each one is `{text, like?, lead?}`,
  or a bare string, which is read as `text`.
  - `like`: wording from the paragraph whose formatting this one copies. The
    default is the anchor. It must name exactly one paragraph.
  - `lead`: an optional inline heading, such as `Invoices.`, written before
    `text`.
- `comment` is anchored on the **anchor paragraph**, not inside the new ones
  (see below).

**What is copied.** The new paragraph gets the `like` paragraph's whole `w:pPr`:
style, `w:numPr`, indentation, spacing, keep-with-next and its mark's `w:rPr`.
Two things are dropped from the copy: a `w:sectPr` (a section break must never
be duplicated) and any revision record (`w:pPrChange`, and `w:ins`/`w:del`/move
marks on the paragraph mark). This handles both numbering schemes:

- **Style-borne numbering.** A heading numbered by its `Heading1` style keeps
  its style, so Word numbers it and renumbers the sections after it once the
  change is accepted.
- **Direct numbering.** A list item numbered by its own `w:numPr` keeps that
  `w:numPr` and takes the next letter in the list.
- **Typed numbers.** When the numbers are typed into the text, the model writes
  the number in `text`.

The runs copy the `like` paragraph's `w:rPr`. The body text takes the formatting
of the run holding the most wording, and `lead` takes the formatting of the
first run that contains a letter. That is how `**Invoices.** University shall
pay…` gives a bold lead-in and a plain body. Formatting is never guessed from
whole-document statistics.

**Revision markup.** Each new paragraph's runs sit in one `w:ins`, and its
paragraph mark is a revision too (`w:pPr/w:rPr/w:ins`). On Reject All the
inserted mark joins its (now empty) paragraph to the next one, so no empty
paragraph is left behind.

Sometimes no paragraph follows the anchor: it is the last paragraph of a table
cell or of the document, or it comes just before a table. Then the anchor's own
mark becomes the inserted one, and the last new paragraph carries the anchor's
original properties. That last paragraph therefore cannot take another
paragraph's `like`, and the call says so rather than producing a file that would
change formatting on Reject All. An anchor whose mark is already a revision, or
a section break, cannot be inserted after in that position either.

**Why the comment is on the anchor.** LibreOffice keeps a comment's anchor when
it rejects the insertion around it. Measured headless (26.2, 2026-11-07): every
placement of a comment inside an inserted paragraph tested left an empty
paragraph after Reject All, including Word's own layout (range and reference
inside the `w:ins`). With no comment, or with the comment on the anchor, the
paragraph rejected cleanly. A comment beside the insertion it explains reads the
same in the margin.

**Guarantees kept.**

- **All or nothing.** Every anchor and `like` is located before anything is
  written, and every problem comes back as one `Change N: …` line.
- **Narrow re-serialization.** Only anchor paragraphs are re-serialized. New
  paragraphs are written into the anchor's source span, or into the tree when
  the anchor is nested, as in a text box.
- **Namespace-safe.** The same paragraph writer as v1 is used, so prefixes are
  kept as the document spells them.
- **Self-check.** Before saving, every edited paragraph is read with this
  call's insertions dropped and its deletions kept, and must equal its original
  wording, paragraph marks and properties included (§3c widened it from
  wording alone). A failure raises and nothing is written. This is the edit-in-place
  check a hand-written engine would run, done on every call.

**Cost.** The spec gains 285 tokens in `toolConfig` (568 → 853 by Bedrock
CountTokens on Haiku 4.5, `evals/tool_spec_tokens.json`; 515 → 767 in cl100k).
The deploy that ships it rewrites the cached prefix once, for users who have the
Word tools, and never per session. A new clause costs the output tokens of its
own wording, against about 20K for retyping an engine.

**Capability against a hand-written contract redline engine.** The reference is
a team's python-docx engine that a contract-review Agent was retyping each run:

| Engine behaviour | `redline_word_document` |
|---|---|
| Surgical replace, trimmed to the differing words; pure strike that takes one space with it | Yes (v1) |
| Normalized matching (quotes, dashes, nbsp, whitespace), whole words only | Yes (v1). The case-insensitive rung is a hint in the error, not a silent match |
| Refuse to cut hyperlinks, fields, content controls, other authors' revisions | Yes (v1) |
| Pick one of several matches by a context sentence | `occurrence` (v1) |
| Comment on a paragraph, a disclaimer on the first paragraph | Yes, as a comment on `find` (v1) |
| Continuation: add sentences inside an existing paragraph | Yes, by repeating the neighbouring sentence in `replace` (v1) |
| **New section as its own paragraphs, heading numbered by the document's scheme** | **§3a**. v1 could only append to an existing paragraph |
| **Font and heading formatting for new text** | **§3a**, copied from a named paragraph rather than inferred from document-wide statistics |
| **New list item that takes the list's numbering** | **§3a** |
| Validation gate: reject-view equals the input | The self-check above, on every call |
| Never fail: unplaceable edits become ACTION NEEDED comments | Opt-in, §3b: `if_unplaced: "comment"`. The default stays all or nothing with one line per problem |
| Topic-aware placement, the heading-style heuristics, typed-vs-automatic numbering notes, seam and capitalization repair, `[RESERVED]` notes | The agent's drafting judgment. It picks `find`, `like` and the wording |
| Whole-paragraph deletion (removing the paragraph mark) | The engine does not: it strikes the text and leaves the mark. **§3c** deletes the mark too, so later items and sections renumber |
| Moves, formatting-only revisions, headers, footers, footnotes | Neither does it |

## 3b. Unplaced changes as comments: `if_unplaced`

**Problem.** All or nothing (§3) costs a long review several full rounds. One
change whose `find` is slightly off fails the whole call, so the model re-sends
every change, each with its full clause text: a 40-change contract review can
spend two or three full rounds of output tokens and minutes on Opus before it
gives up on the stragglers. The hand-written engine it replaced never failed: it
left each unplaceable change as an ACTION NEEDED margin comment.

**Shape.** One top-level argument, `if_unplaced`: `"fail"` (the default, §3
unchanged) or `"comment"`. Under `"comment"`, every change the *document*
defeats becomes a comment instead of a failure, the rest are applied, and the
call saves. Its result lists the two sets apart:

```
Tracked changes by Pat Reviewer: 1. deleted "thirty (30)", inserted "sixty (60)";
3. deleted "one year", inserted "two years". Not placed, left as ACTION NEEDED
comments (1): 2. "force majeure": The wording to change was not found in the document.
```

so the model reads back once and fixes only what needs it, by calling again on
the saved redline with just those changes.

**What becomes a comment, and where it goes.** The comment covers the whole
paragraph the change most likely meant:

| Problem (the `"fail"` line is unchanged) | Comment on |
|---|---|
| `find` not found | the paragraph of a match ignoring capitalization; else the paragraph holding at least half of its words (3+ letters; the first on a tie); else the first paragraph with any wording |
| `find` spans paragraphs | the same ladder, matching the first line |
| `find` appears N times, no `occurrence` | the first candidate |
| `occurrence` out of range | the last candidate (the first, for one below 1) |
| runs into a hyperlink, field, content control or existing revision | its paragraph |
| overlaps another change | its paragraph. The **later change in request order** is the comment, whatever its position, so the result is deterministic |
| `like` found nowhere, or in several paragraphs | the anchor |
| an insertion point that cannot take one (mark already revised; last paragraph of a cell, the document or before a table with a different `like`) | the anchor. Only the offending change of a group drops, and the group is re-checked |

The comment begins `ACTION NEEDED —`, gives the reason in one plain sentence for
a reader of the document, then quotes the requested `find` in full and gives the
full `replace` ("Delete it." for `""`) or each new paragraph, and the change's own
`comment`, one line each.

**What still fails.** Problems with the request itself, which no document would
place: an empty `find`; neither `replace` nor `comment`; `replace` equal to
`find` with no comment; a malformed insertion (`insert` not after/before, both
`replace` and `insert`, no `paragraphs`, a paragraph with no `text` or an empty
`lead`); malformed arguments; an unknown `if_unplaced`. Then only those
problems are listed and nothing is saved.

**Guarantees kept.**

- **Placed changes are byte-identical to `"fail"`.** The ACTION NEEDED comments
  are written after every placed change and insertion, so each placed change
  takes the revision and comment ids it would take in a `"fail"` run of the
  placed changes alone. Taking the three comment markers out of the body gives
  that run's `word/document.xml` exactly (tested).
- **Narrow re-serialization.** A paragraph that only receives an ACTION NEEDED
  comment is now re-serialized, the one paragraph each such comment adds.
  Its wording is untouched, and every other paragraph keeps its bytes.
- **Self-check.** The reject-view check runs over every re-serialized paragraph,
  the commented ones included.
- With nothing unplaced, `"comment"` writes the same file as `"fail"`.

**Cost.** The spec grows 853 → 1004 tokens (+151, Bedrock CountTokens;
`evals/tool_spec_tokens.json`), one deliberate `toolConfig` rewrite at the deploy
for users with the Word tools, never per session. Against that, a long review
stops paying a full re-send of every change for each `find` that misses.

## 3c. Deleting whole paragraphs

No flag of its own, like §3a and §3b (§4c): it is on wherever
`WORD_TRACKED_CHANGES_ENABLED` builds the tool.

**Why.** `replace: ""` strikes a paragraph's wording but leaves its paragraph
mark, so after Accept All an empty paragraph remains. In a numbered list or a
numbered section it still holds its number. Striking clause (b) of (a)–(c)
leaves "(a) …, (b) [empty], (c) …" instead of "(a) …, (b) …", and a contract
reviewer has to clean every one up by hand.

**Shape.** A change gives `delete` instead of `replace`:

```json
{"find": "Contractor has given written notice", "delete": "paragraph"}
{"find": "Payment", "delete": "paragraph", "through": "University has not cured",
 "comment": "Payment terms move to Exhibit B."}
```

- `find` names the first paragraph to delete, matched exactly as for an edit:
  one paragraph, `occurrence` when the wording repeats.
- `delete`: `"paragraph"` (`"paragraphs"` is read the same).
- `through`: optional wording from a later paragraph. Every paragraph from
  `find`'s through **the first later paragraph in the same container** (the
  body, one table cell, one text box) whose wording contains it is deleted, so a
  section goes in one change, from its heading to its last body paragraph.
  Nearest-first means a `through` that repeats deletes less, never more.
- `comment` goes on a paragraph whose **mark survives** (below).

**Revision markup.** Every run of each paragraph moves into `w:del` (its `w:t`
renamed `w:delText`), and each paragraph mark is a deletion too
(`w:pPr/w:rPr/w:del`). On Accept All each deleted paragraph joins the next one,
whose mark, and so whose style and numbering, is the one that survives; the
list or the style-numbered headings after it renumber. On Reject All the marks
are restored and nothing has moved.

**No paragraph after the deletion.** The last paragraph of a table cell or of
the document, and a paragraph just before a table, have no following mark to
merge into, so their mark has to stay. This mirrors §3a's adopted mark, the
other way round:

- When the paragraph **before** the deletion is a paragraph formatted exactly
  like the last deleted one (the same `w:pPr`, whitespace and an empty mark
  `w:rPr` aside), the deletion deletes that paragraph's mark instead and keeps
  the last deleted paragraph's. On Accept All the paragraph before ends up with a mark identical to
  its own, so nothing about it changes. Deleting a whole section that ends just
  before a table is this case: the previous section's last body paragraph and
  this section's last body paragraph are formatted alike.
- When it is formatted differently, the call refuses with a problem line
  (keeping it would hand that paragraph the deleted one's style). The model
  strikes the wording with `replace: ""` instead, or starts the deletion after
  a paragraph formatted like its last one. Rewriting the surviving mark's
  properties with a `w:pPrChange` would avoid the refusal; it is not built,
  because LibreOffice's reject of a paragraph-property revision has not been
  verified.
- When nothing comes before it in the container either, every paragraph of the
  container is being deleted. The container keeps one empty paragraph, as Word
  requires of a table cell, and the summary line says so.
- When a table or a content control comes before it, the call refuses.

**Refused.** A paragraph that holds a hyperlink, a field, a content control, an
existing tracked change by anyone, a section break, a tracked formatting change,
or content with no wording the tool can mark (an equation). A `through` that
does not name a later paragraph in the same container, or a span with a table or
content control inside it. A deletion that shares a paragraph with another
deletion, with a text edit, or with an insertion's anchor. An insertion after the
paragraph whose mark a deletion takes. A comment whose next paragraph another
change deletes (the fix is one change with `through`). Bookmarks,
proofing marks and comment ranges inside a deleted paragraph are allowed and
stay where they are.

**With `if_unplaced: "comment"` (§3b).** A deletion the document defeats (its
`find` or `through` not found, a locked paragraph, an end it cannot take) is an
ACTION NEEDED comment like any other miss, on its `find` paragraph, and the
comment says "Delete that paragraph." or names the `through` wording. Two
changes that cannot both be made (a deletion and an edit, an insertion or
another deletion in the same paragraph) resolve by request order: the later
one becomes the comment. **No comment, placed or ACTION NEEDED, ever sits in a
paragraph whose mark this call deletes**: one that would (a miss whose likely
paragraph is being deleted, a deletion's comment whose next paragraph is
deleted too) moves to the paragraph that takes that mark over, following the
chain to one whose mark survives; where that paragraph's own wording is deleted
(it keeps the mark of a backwards merge), the comment is a point after the
struck wording, since Word drops a comment whose whole range is deleted text
when the deletion is accepted. A comment on a text edit inside the paragraph
whose mark a backwards merge takes cannot move with its edit, so that pair is
refused (or, partial, the later one commented).

**Where the comment goes.** Measured headless (LibreOffice
26.2, 2026-11-08): a comment anchored around the deleted wording, as Word itself
places one, survives Accept All in LibreOffice and keeps the deleted paragraph
alive as an empty paragraph that still holds its letter, which is exactly what
deleting the mark is for. The same happens on the paragraph whose mark a
backwards merge takes, though its wording stays. So the comment goes on the next
paragraph, or, where the deletion takes the previous mark, on the last deleted
paragraph, whose mark is the one that survives; both accept cleanly. It is the
same finding as §3a's comment on the anchor.

**Self-check.** The §3a check now reads each edited paragraph as Reject All
would leave it: this call's insertions dropped, its deletions kept, a paragraph
mark it inserted merged into the next paragraph, a mark it deleted restored, and
each mark's paragraph properties compared with this call's mark revisions
removed. That view must equal the original. Separately, every deleted paragraph
must hold no wording outside `w:del`. Either failure is a bug, and nothing is
written.

**Cost.** The spec gains 148 tokens by Bedrock CountTokens on Haiku 4.5 (1004 →
1152 on top of §3a and §3b, `evals/tool_spec_tokens.json`). The deploy that
ships it rewrites the cached `toolConfig` prefix once, for users who have the
Word tools, never per session. Deleting a section costs one short change
instead of one `replace: ""` per paragraph and a manual clean-up.

## 3d. Comparing two documents: `compare_word_documents`

Built beside `redline_word_document`, under the same condition
(`WORD_TRACKED_CHANGES_ENABLED` and the Word grant), with no flag of its own
(§4d).

**Why.** A counterparty often sends a contract back as a clean copy, with no
tracked changes, and the user asks what changed. Without a tool the model reads
both files and reconstructs the edits by hand: it misses small ones (a number in
a table cell, a dropped "and"), it cannot show them in Word, and the user still
has to run Word's own Compare to get a redline they can accept or reject.

**What it does.** Given an original and a revised `.docx`, it writes the revised
wording into a copy of the **original** as tracked changes by one author. The
author defaults to `Counterparty`, because the revisions are theirs; the model
passes a name when the user gives one. Reject All gives back the original. Accept
All gives the revised wording. The tool returns the download card and a bounded
list of the changes (at most 40 lines, 4,000 characters), so the model can
summarize them without reading either document.

### Design

**Blocks.** Each container (the body, then each table cell) is read as a
sequence of blocks: paragraphs, tables, and anything else at block level (a
content control). Each block is keyed by its wording, folded the way `find` is
folded (curly and straight quotes, dash variants and non-breaking spaces equal,
whitespace collapsed).

**Paragraph alignment.** `difflib.SequenceMatcher` aligns the two key sequences
(an LCS-style alignment, `autojunk` off). Equal blocks are left alone, byte for
byte. Inside each differing stretch, blocks are paired in order by a weighted
LCS (dynamic programming over the stretch) whose score is the similarity of the
two paragraphs: the larger of a word-level and, for short paragraphs, a
character-level `SequenceMatcher` ratio, counted only at 0.5 or above (0.3 for
tables). A stretch of one paragraph against one paragraph is always a pairing,
which is what a changed table cell is (`$10,000` → `$15,000` shares no word).
A stretch larger than 2,500 candidate pairs is not paired; it is a rewrite, and
is written as removals and additions.

**Reworded paragraphs** (paired) are diffed word by word. Tokens are aligned
finely (words, single punctuation marks and spaces, with spaces as `difflib`
junk so a run of spaces never anchors the alignment), each differing stretch is
widened to whole words, stretches separated only by spaces and punctuation are
joined, and the redline engine's own `_trim` narrows each to the words that
differ, so `receipt.` → `receipt, without setoff.` is a pure insertion of
`, without setoff`, exactly as a `redline_word_document` change would be. Each
stretch becomes one `w:del` / `w:ins` pair written by the redline engine's
`_apply`, in the **original paragraph's own runs and formatting**: the
comparison is against the original's formatting, never the revised copy's.

**Removed paragraphs** are §3c's whole-paragraph deletions, built by the
engine itself (`_deletion_of`, the rule `redline_word_document`'s `delete`
uses): neighbouring removed paragraphs form one run, their runs move into
`w:del` and their paragraph marks are deleted, so Accept All leaves no empty
paragraph and Reject All restores them. Where no paragraph follows (the end of a
cell or of the document, or before a table) the paragraph before the run gives
up its mark instead, when it is formatted like the last one; otherwise the last
mark stays, as Word keeps it, and Accept All leaves that one paragraph empty. A
paragraph §3c cannot delete (it holds a hyperlink, field, section break or an
equation) becomes a comment. When a differing stretch ends with no paragraph
after it and has both a removed and an added paragraph after its last pairing,
those two are paired instead, so a replaced last paragraph is an exact rewording.

**Added paragraphs** reuse §3a's tracked paragraph insertions (runs in `w:ins`,
paragraph mark in `w:pPr/w:rPr/w:ins`). They are written **before** the next
original paragraph that stays, so the inserted mark always has a paragraph to
merge into on Reject All; only where none follows are they written after the
previous one that stays, and then the last of them takes that paragraph's mark
and formatting (§3a's adopted-mark rule). They are never anchored on a removed
paragraph, nor after one whose mark a deletion takes. An added paragraph keeps the **revised** paragraph's own run
formatting run by run (a bold lead-in stays bold) and its paragraph properties,
with its style and list ids mapped to the original's: each id pair seen on the
paragraphs the two documents share teaches the mapping (an editor that renamed
`Heading1` is followed), an id the original defines is kept, and one it does not
is dropped rather than left dangling. Markup outside WordprocessingML (`w14`
attributes and the like) is not copied, because the original may not declare it.

**Tables.** A paired table whose grid is unchanged (same rows, the same cells
per row, the same `gridSpan` and `vMerge`) is compared cell by cell, each cell
as a container of its own. A table whose rows or columns changed gets a comment
on its first paragraph saying so and quoting the revised table (at most 500
characters), and its contents are not marked. An added or removed table, and an
added or removed content control, get the same kind of comment.

**What cannot be marked** becomes a comment instead, and a "Not marked" line in
the result: a rewording that would cut into a hyperlink, field, smart tag or
content control (the same locks as v1), and a removed paragraph that holds one.
The other edits in such a paragraph are still marked; the comment quotes only
the ones that could not be, and the revised paragraph whole.
A comment is never placed inside a paragraph the comparison removes, because
LibreOffice keeps a comment's anchor alive when the text around it is removed
(§3a).

**Already redlined documents are refused.** If either document holds any
tracked change (insertion, deletion, move, or a formatting, numbering or table
revision), the call fails with one line naming which document. Comparing on top
of someone's live revisions would mix two authors' changes, and the self-checks
below assume every revision is this call's. The model is told to read the
document instead, which shows its changes (§2).

**No differences.** When the wording is the same (typography, spacing and
formatting are not differences), nothing is saved and the model is told so.

**Out of scope.** Formatting-only changes (bold, a style, numbering format),
headers, footers, footnotes and endnotes, text boxes and shapes, images, and
moves: a moved paragraph reads as removed in one place and added in another,
which is also how a reviewer reads it in the margin. Inserted text from a
hyperlink or field in the revised copy arrives as plain runs with that run's
formatting.

**Guarantees kept.**

- **Narrow re-serialization.** Only paragraphs that change are re-serialized and
  spliced into the original's bytes through the redline engine's writer
  (`_write`, shared with `redline_word_document`), namespace-safe; every other
  paragraph and every other part of the package is copied unchanged.
- **Reject-view self-check.** As for every redline: each edited paragraph, with
  this call's insertions dropped and deletions kept, must read as it did.
- **Accept-view self-check.** New here: each reworded paragraph, once accepted,
  must read as its revised counterpart; each removed one as empty; and each
  group of added paragraphs as the revised paragraphs it came from. Either check
  failing is a bug, and nothing is written.

### A new tool, not a mode of `redline_word_document`

| Choice | cl100k (spec JSON, §3a's first method) | Bedrock CountTokens (marginal, `tool_spec_tokens.json`) |
|---|---|---|
| New tool `compare_word_documents` (chosen) | **378** | **419** (Word family 2,928 → 3,347) |
| A `revised_name` mode of `redline_word_document` (prototyped, not built) | about 131 more on the redline spec | not measured |

The mode is about 250 tokens cheaper and was rejected anyway:

- It would make `changes` optional on `redline_word_document`, weakening a
  schema the model uses every time it proposes edits.
- The two jobs have opposite default authors: a redline is the user's, a
  comparison is the counterparty's. One tool with both defaults is a
  misattributed redline waiting to happen.
- "What changed?" maps onto a tool named for it, and the redline spec stays
  byte-identical (its digest is asserted again in this tool's tests).

**Cost, every turn, for every user with the Word tools.** The spec is a
constant in the cacheable `toolConfig` prefix. Shipping it is one deliberate
`toolConfig` rewrite at deploy for those users, never per session; afterwards it
is a cache read of 419 tokens on each turn (about $0.00005 a turn on Haiku 4.5). A
deployment that does not want the tool turns off `WORD_TRACKED_CHANGES_ENABLED`,
which removes the whole tracked-changes family. The tool's result is bounded,
and replaces the model reading two documents and writing out the differences
itself.

## 4. Flag

`WORD_TRACKED_CHANGES_ENABLED` / `CDK_WORD_TRACKED_CHANGES_ENABLED` /
`wordTrackedChanges.enabled`. Default on, with a kill switch: only `"false"`
disables it. CDK sets it on app-api always and on the AgentCore Runtime only
while off, so the default spends nothing from the Runtime's environment budget.
Both services must agree, because the upload digest built on app-api has to
number paragraphs the way `document_read` does on the Runtime. There is no SPA
flag: the redline comes back on the existing download card.

It is a **feature switch**, not a rollout switch: it changes what users'
documents read like and adds a tool.

### 4a. Why §3a has no flag

It was built behind an in-development flag while the new markup (paragraph-mark
revisions, and the anchor's own mark becoming the inserted one) was unproven in
Word. Once Word for Mac had opened it cleanly (§6), the flag protected nothing:
the change is additive, old calls behave exactly as before, and the new markup
appears only when a change gives `insert`. A flag would only have postponed the
one `toolConfig` rewrite and doubled the tool's spec, wiring and tests. The kill
switch is `WORD_TRACKED_CHANGES_ENABLED`, which removes the tool.

### 4b. Why §3b has no flag

`if_unplaced` is an optional argument whose default, `"fail"`, is the call as it
was: a model that never passes it gets exactly the old behaviour, so the
argument is its own switch. A flag would have doubled the spec, its pins and its
tests to postpone one `toolConfig` rewrite. Before merge it was checked in Word
for Mac (§6) and its spec cost measured (§3b). The kill switch is
`WORD_TRACKED_CHANGES_ENABLED`, which removes the tool.

### 4c. Why §3c has no flag

§3c followed the same reasoning as §3a without a flag at all: deletion is optional on
a tool the deployment already chose, old calls are unchanged, and its markup
appears only when a change gives `delete`. Evidence took the flag's place
before merge: Word for Mac and LibreOffice read-backs of fixture redlines
(§6), and the spec's measured growth, one deliberate `toolConfig` rewrite at
deploy (§3c, Cost).

### 4d. Why §3d has no flag

It adds a tool to a family a deployment already chose with
`WORD_TRACKED_CHANGES_ENABLED`, and its markup is §3a's and §3c's, already proven in Word.
Evidence before merge stands in for a flag: Word for Mac and LibreOffice
read-backs (§6) and a measured, pinned spec. A deployment that does not want
it turns off the tracked-changes family.

## 5. Tests

- `tests/agents/builtin_tools/test_docx_redline.py`: the engine. Covers
  replace/delete/insert/comment, trimming to the differing words, normalized
  matching, ambiguity and occurrence, overlap, locked spans, multi-run splits
  that keep run formatting, untouched paragraphs staying byte-identical,
  `mc:Ignorable` declarations surviving, revision ids staying unique, and the
  round trip that `annotate_tracked_changes` reads back as the intended redline.
- `tests/agents/builtin_tools/test_docx_redline_paragraphs.py`: §3a, on a real
  LibreOffice-written package (`fixtures/contract-numbered.docx`, with style-numbered
  headings, a directly numbered lettered list, a bold lead-in and a table) and on
  synthetic edge cases. Covers Reject All giving back the original text, style
  and numbering with no empty paragraph; Accept All giving the copied style and
  numbering; lead and body run formatting; the adopted mark before a table, at a
  cell's end and at the document's end; section breaks and revision records not
  copied; a text box; text edits and insertions together; request order at one
  anchor; unique ids; the problem lines; and the self-check refusing a leaked run.
- `tests/apis/inference_api/test_word_redline_paragraphs.py`: the tool's spec
  (digest-pinned), an end-to-end call read back through `docx_paragraphs`, and
  malformed `insert` / `paragraphs` arguments changing nothing.
- `tests/agents/builtin_tools/test_docx_redline_deletions.py`: §3c (the engine) on the same
  fixture with the same Word-semantics views (a deleted mark joins the next
  paragraph on Accept All). Covers deleting a lettered item (the next item takes
  its letter), a whole numbered section with `through`, nearest-first `through`,
  the only paragraph of a table cell, the last paragraph of a two-paragraph cell
  (the previous mark taken), a section ending before a table, the lone paragraph
  before a table and the last paragraph after one (refused), comment placement,
  deletions with insertions and text edits in one call, unique ids, every
  problem line, and the self-check refusing a run left outside `w:del` and a
  paragraph property changed outside a revision.
- `tests/apis/inference_api/test_word_redline_paragraph_deletions.py`: the
  spec documenting `delete` / `through` after the insertion wording (the whole
  spec is digest-pinned in `test_word_redline_paragraphs.py`); an end-to-end
  delete read back through `docx_paragraphs` as `[deleted: …]` with the comment
  on the next paragraph; `through` through the tool; malformed `delete` /
  `through` changing nothing.
- `tests/agents/builtin_tools/test_docx_redline_unplaced.py`: §3b. Each kind of
  miss lands as its comment on the right paragraph; the comment's wording; the
  caller's own problems still failing; overlaps resolved by request order; and a
  mixed list whose placed edits equal a `"fail"` run of just those edits, byte
  for byte once the action comments are taken out, insertions included.
- `tests/agents/builtin_tools/test_docx_compare.py`: §3d, on the real fixture
  and a LibreOffice-written revised copy (`fixtures/contract-numbered-revised.docx`:
  two reworded clauses, a removed list item, an added numbered section and a
  changed table cell) and on synthetic edge cases. Covers Accept All equal to
  the revised document and Reject All to the original (text, style and
  numbering), the change list, word-level marks in the original's runs, the
  removed item's deleted mark, untouched paragraphs and parts keeping their
  bytes, the author, the reverse comparison, refusing tracked changes (a
  formatting revision included), no differences (typography, spacing,
  formatting), additions at the start and end, removing the last paragraph (the previous mark taken, or kept
  when formatted differently) and replacing it, removed and added paragraphs
  together as whole-paragraph revisions, moves, run formatting and style mapping from the revised copy,
  foreign markup not copied, cells, changed grids, added and removed tables,
  hyperlinks (a comment, with the other edits still marked), the pairing cap,
  and the accept-view self-check refusing.
- `tests/apis/inference_api/test_word_compare_documents.py`: both specs pinned,
  the gating, and the tool's calls and messages.
- Read surfaces: `docx_paragraphs` with the flag on and off, R3 swapping only a
  redlined `.docx`, and R2 annotating before the sandbox.
- Flag and tool injection: the tool is built only with the flag on and the Word
  gate granted.
- CDK: the env var is opt-in and set only while on, on both services.

## 6. Validation

Done locally, 2026-10-12. A contract was built in a Word-authored package (Word's
bundled `Word 2010.dotx`, given a modern root tag that declares 22 namespaces
with `mc:Ignorable`), with paragraph ids, split runs, proofing marks, a bookmark,
a table and an internal hyperlink. Seven changes were written: replacements, a
pure insertion, a deletion, a change inside a table cell, and comments on a
replacement and on a hyperlink. **Microsoft Word for Mac** opened it with no
repair prompt and showed every change under the author in the margin, and every
comment in the comment pane. LibreOffice converted it headless without
complaint.

§3a, locally, 2026-11-07: the fixture was redlined with seven changes in one
call. They were a new numbered section (heading and body), a list item, a
lead-in paragraph, a new section before a table, a line in a table cell, a
paragraph inserted before another, and a surgical edit, three of them with
comments. LibreOffice 26.2, driven headless through UNO, loaded the file with
8 tracked-change regions. Reject All gave back the original paragraph text and
list labels exactly. Accept All gave the new list item `(c)` with the old `(c)`
renumbered `(d)`, and the new headings as `4.` and `6.` with the later sections
renumbered.

The same file in **Microsoft Word for Mac**, 2026-11-08: it opened with no repair
prompt and reported 8 revisions. The new list item showed as `(c)` with the next
renumbered `(d)`, and the new headings as `4.` and `6.`. Comments sat on their
anchor paragraphs. Read back through Word's own AppleScript (`reject all
revisions`, then each paragraph's `list string` and text), Reject All was
identical to the original document: the same paragraphs, list labels and table
cells, with no empty paragraph where an insertion had been. Accept All gave the
intended text and numbering.

§3c, locally, 2026-11-08, LibreOffice 26.2 headless (a Python macro run inside
`soffice`, since its standalone Python is killed on this Mac): the fixture was
redlined with a lettered item deleted (with and without a comment), the Payment
section deleted heading-to-list and heading-to-intro, the only paragraph of a
table cell deleted, the Entire Agreement section deleted up to the table, and a
deletion beside a text edit and an insertion. Every file loaded with its tracked
changes. Reject All gave back the original text, list labels and paragraph
styles exactly. Accept All removed the paragraphs: old (c) became (b), the
headings after the Payment section renumbered from 3. and 4. to 2. and 3., the
cell kept one empty paragraph, and no empty paragraph was left anywhere else.
**Microsoft Word for Mac**, the same day, driven through its AppleScript
(`open POSIX file`, `reject all revisions` / `accept all revisions`, then each
paragraph's `list string of list format of text object`, style and `content of
text object`, then `close saving no`), on six of those redlines including the
combined five-change call and the synthetic two-paragraph cell plus document
end: every file opened with no repair prompt and its revisions counted. Reject
All equalled the original exactly in every file, list labels and styles
included. Accept All gave old (c) as (b), the headings after the Payment section
as 2. and 3., the Entire Agreement section gone with no empty paragraph before
the table, the emptied cell with one empty paragraph, and the backwards-merged
cell and document end with no stray paragraph; the paragraph that took over a
deleted mark kept its own style.

§3c with §3b, 2026-11-08, after both merged: one call with
`if_unplaced: "comment"` that deletes a lettered item (with a comment),
inserts a list item, deletes the Entire Agreement section up to the table (with
a comment, so on the backwards-merged paragraph), edits a figure and leaves a
change with no match as an ACTION NEEDED comment; and a second whose two misses
most resemble paragraphs being deleted. In Word for Mac both opened with no
repair prompt, Reject All equalled the original exactly (list labels and
styles), and Accept All gave (a), (b) = old (c), (c) = the new item, the
sections after renumbered, no empty paragraph, and **every comment kept** (3
of 3, 2 of 2). That last needed one change: a comment on a paragraph whose
wording is all deleted but whose mark survives is anchored as a point after the
struck wording, because Word drops a comment whose whole range is deleted text
when the deletion is accepted (a first run, with the range around the struck
wording, lost one comment per file). LibreOffice headless gave the same views,
comments kept in both. (Word refuses scripted opens outside its
sandbox with a Grant File Access prompt; copies in
`~/Library/Containers/com.microsoft.Word/Data/tmp/` open without one.)

§3b in **Microsoft Word for Mac**, 2026-11-08: the same fixture was redlined with
nine changes and `if_unplaced: "comment"`. Four placed (a commented replacement,
a new numbered section, two surgical edits) and five became ACTION NEEDED
comments (two not found, one ambiguous, one capitalization-only, and an
insertion at a table cell's end asking for another paragraph's format). Word
opened it with no repair prompt and reported 7 revisions and 6 comments. Read
back through its AppleScript, each ACTION NEEDED comment's scope was the intended
paragraph: the title for the two misses with no words in common, the first
candidate for the ambiguous one, the `Kickoff` cell, and the signature line for
the capitalization one. The placed change's own comment stayed on its edit.
Reject All was identical to the original document read the same way (27
paragraphs, text and list labels), and Accept All gave the edits, with the new
section numbered `4.` and the next renumbered `5.`.

§3d, locally, 2026-11-08, on the engine with §3b and §3c merged: the fixture and
its LibreOffice-written revised copy were compared (9 tracked-change regions,
all by Counterparty).

- **LibreOffice 26.2**, headless through UNO (a Python macro in a private
  profile): Accept All gave the revised document's paragraphs exactly, list
  labels and heading numbers included (`(b)` relettered from `(c)`, `4. Survival`
  with `Entire Agreement` renumbered `5.`); Reject All gave the original's
  exactly. So did the reverse comparison. Six synthetic cases gave the original
  on Reject All every time, and the revised copy on Accept All except the two
  designed as comments: a table with a new row, and an edit inside a hyperlink.
  Removing the document's last paragraph is exact: the paragraph before gives up
  its mark (§3c).
- **Microsoft Word for Mac**, read back through Word's own AppleScript (`open`,
  `accept all revisions` / `reject all revisions`, then each paragraph's `list
  string of list format` and `content` of its `text object`, `close saving no`),
  with copies in Word's container so no access prompt appears: the comparison
  opened with no repair prompt and 9 revisions; Accept All was identical to the
  revised document (28 paragraphs: text, list labels, table cells) and Reject
  All to the original (27). The reverse comparison was exact both ways. The
  synthetic cases were not read in Word: the test helper's minimal packages
  (`tests/shared/docx_fixtures.build_docx`) declare `mc:Ignorable="w14"` without
  the `w14` namespace, so Word offers to repair those inputs themselves.

On dev, once the flag is on:

1. Attach a redlined contract and ask what changed. The model should name each
   change, not report merged text.
2. Ask for a redline of a clean contract. Open the output in Word: each change
   shows in the Reviewing pane under the user's name, comments appear in the
   margin, and Accept All gives the intended text.
3. Attach a contract and a clean revised copy and ask what changed. The model
   should call `compare_word_documents`, list the changes from its result, and
   the download opens in Word with each change under Counterparty.

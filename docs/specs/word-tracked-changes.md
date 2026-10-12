# Word tracked changes: reading redlines and writing them

Status: in development, behind `WORD_TRACKED_CHANGES_ENABLED` (off by default).

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
instead of producing a malformed file.

Provisioning: the tool joins the Word family (the `create_word_document` gate)
only while the flag is on. Off, its spec never reaches `toolConfig`. On, it adds
one constant spec of about 515 tokens (cl100k) for users who have the Word tools.
The spec is part of the cacheable prefix, so the cost is a one-time cache write per
session, then cache reads.

## 4. Flag

`WORD_TRACKED_CHANGES_ENABLED` / `CDK_WORD_TRACKED_CHANGES_ENABLED` /
`wordTrackedChanges.enabled`. In development, so only `"true"` enables it. It is
set on the AgentCore Runtime and app-api only while on, so an environment that
leaves it off spends nothing from the Runtime's environment budget. There is no
SPA flag: the redline comes back on the existing download card.

When it ships it becomes a **feature switch**, not a rollout switch: it changes
what users' documents read like and adds a tool.

## 5. Tests

- `tests/agents/builtin_tools/test_docx_redline.py`: the engine. Covers
  replace/delete/insert/comment, trimming to the differing words, normalized
  matching, ambiguity and occurrence, overlap, locked spans, multi-run splits
  that keep run formatting, untouched paragraphs staying byte-identical,
  `mc:Ignorable` declarations surviving, revision ids staying unique, and the
  round trip that `annotate_tracked_changes` reads back as the intended redline.
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

On dev, once the flag is on:

1. Attach a redlined contract and ask what changed. The model should name each
   change, not report merged text.
2. Ask for a redline of a clean contract. Open the output in Word: each change
   shows in the Reviewing pane under the user's name, comments appear in the
   margin, and Accept All gives the intended text.

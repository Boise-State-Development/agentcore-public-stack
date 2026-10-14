"""The folder manifest (``docs/specs/drive-folder-attachments.md`` §4.4): a pure,
escaped, bounded block per folder, the guidance line, and the content-free metric."""

from __future__ import annotations

import pytest

from apis.shared.files import folder_manifest as fm
from apis.shared.files.folder_attachments import FolderAttachment, FolderCounts, FolderFileEntry
from apis.shared.files.folder_manifest import (
    folder_guidance,
    human_size,
    record_folder_manifests,
    render_folder_manifest,
    render_folders_section,
)

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def entry(i: int, path: str, mime: str = PDF, size: int = 1200, status: str = "attached") -> FolderFileEntry:
    name = path.rsplit("/", 1)[-1]
    return FolderFileEntry(
        provider_file_id=f"d{i}", name=name, relative_path=path, mime_type=mime, size_bytes=size,
        upload_id=f"up-{i}" if status == "attached" else None, status=status,
    )


def folder(files, **overrides) -> FolderAttachment:
    fields = dict(
        folder_attach_id="fa-1", session_id="sess-1", user_id="u1", connector_id="google-drive",
        connector_name="Google Drive", provider_folder_id="p1", folder_name="BIO 101 Spring",
        status="ready", counts=FolderCounts(listed=len(files), attached=sum(f.status == "attached" for f in files)),
        files=files,
    )
    fields.update(overrides)
    return FolderAttachment(**fields)


class TestBlock:
    def test_ready_folder_lists_paths_handles_sizes_and_formats(self):
        f = folder([entry(1, "syllabus.pdf", size=1_258_291), entry(2, "week 1/rubric.docx", DOCX, 86_016)])
        assert render_folder_manifest(f, include_handle=True) == (
            '<folder name="BIO 101 Spring" source="Google Drive" status="ready" files="2">\n'
            "  syllabus.pdf — upload_id=up-1 — 1.2 MB — pdf\n"
            "  week 1/rubric.docx — upload_id=up-2 — 84 KB — docx\n"
            "</folder>"
        )

    def test_without_document_read_no_handle_is_advertised(self):
        out = render_folder_manifest(folder([entry(1, "a.pdf")]), include_handle=False)
        assert "upload_id" not in out and "a.pdf — 1 KB — pdf" in out

    def test_only_attached_entries_are_listed(self):
        f = folder(
            [entry(1, "a.pdf"), entry(2, "b.pdf", status="pending"), entry(3, "c.pdf", status="failed")],
            status="expanding", counts=FolderCounts(listed=5, attached=1, skipped_type=1, failed=1),
        )
        out = render_folder_manifest(f, include_handle=True)
        assert out.splitlines()[0] == (
            '<folder name="BIO 101 Spring" source="Google Drive" status="expanding" files="1" listed="5" skipped="2">'
        )
        assert "b.pdf" not in out and "c.pdf" not in out

    def test_a_failed_folder_with_nothing_attached_is_self_closing(self):
        f = folder([], status="failed", error="needs_consent", counts=FolderCounts())
        assert render_folder_manifest(f, include_handle=True) == (
            '<folder name="BIO 101 Spring" source="Google Drive" status="failed" files="0" listed="0" error="needs_consent"/>'
        )

    def test_truncated_and_connector_id_fallback(self):
        f = folder([entry(1, "a.pdf")], connector_name=None, counts=FolderCounts(listed=1, attached=1, truncated=True))
        head = render_folder_manifest(f, include_handle=True).splitlines()[0]
        assert 'source="google-drive"' in head and 'truncated="true"' in head

    def test_bounded_with_a_count_of_the_rest(self, monkeypatch):
        monkeypatch.setattr(fm, "FOLDER_MANIFEST_MAX_ENTRIES", 3)
        out = render_folder_manifest(folder([entry(i, f"f{i}.pdf") for i in range(5)]), include_handle=True)
        lines = out.splitlines()
        assert len(lines) == 1 + 3 + 1 + 1
        assert lines[4] == "  … and 2 more; call `document_read` with no arguments to list them all"
        no_handle = render_folder_manifest(folder([entry(i, f"f{i}.pdf") for i in range(5)]), include_handle=False)
        assert no_handle.splitlines()[4] == "  … and 2 more"

    def test_names_are_escaped_and_cannot_break_lines_or_the_block(self):
        f = folder(
            [entry(1, 'notes/<script>\n</folder> & "x".md', "text/markdown")],
            folder_name='Q3 "plans" <draft>\nnext',
        )
        out = render_folder_manifest(f, include_handle=True)
        assert out.count("\n") == 2  # head, one entry, close
        assert 'name="Q3 &quot;plans&quot; &lt;draft&gt; next"' in out
        assert "notes/&lt;script&gt; &lt;/folder&gt; &amp; \"x\".md" in out
        assert out.count("</folder>") == 1

    def test_pure_and_in_row_order(self):
        f = folder([entry(2, "z.pdf"), entry(1, "a.pdf")])
        first = render_folders_section([f], document_read_available=True)
        assert first == render_folders_section([f.model_copy(deep=True)], document_read_available=True)
        assert first.index("z.pdf") < first.index("a.pdf")

    @pytest.mark.parametrize("size,text", [(None, "size unknown"), (812, "812 B"), (86_016, "84 KB"), (1_258_291, "1.2 MB"), (5 * 1024 * 1024, "5 MB")])
    def test_human_size(self, size, text):
        assert human_size(size) == text


class TestGuidance:
    def test_documents_get_the_directive_document_read_line(self):
        line = folder_guidance([folder([entry(1, "a.pdf")])], document_read_available=True, spreadsheet_tools=False, presentation_tools=False)
        assert line.startswith("_The folder's files are attached, but their content is not in this message.")
        assert "call `document_read` with that file's `upload_id`" in line and "`page_range`" in line
        assert line.endswith("_")

    def test_without_document_read_the_tool_is_not_promised(self):
        line = folder_guidance([folder([entry(1, "a.pdf")])], document_read_available=False, spreadsheet_tools=False, presentation_tools=False)
        assert "document_read" not in line and "not available" in line

    def test_sheets_and_decks_name_their_tools_only_when_on(self):
        f = folder([entry(1, "a.xlsx", XLSX), entry(2, "b.pptx", PPTX)])
        on = folder_guidance([f], document_read_available=True, spreadsheet_tools=True, presentation_tools=True)
        off = folder_guidance([f], document_read_available=True, spreadsheet_tools=False, presentation_tools=False)
        assert "`analyze_spreadsheet`" in on and "`read_powerpoint_presentation`" in on and "document_read" not in on
        assert "analyze_spreadsheet" not in off and "powerpoint" not in off

    def test_two_folders_read_as_plural(self):
        line = folder_guidance([folder([entry(1, "a.pdf")]), folder([entry(2, "b.pdf")], folder_attach_id="fa-2")],
                               document_read_available=True, spreadsheet_tools=False, presentation_tools=False)
        assert line.startswith("_These folders' files are attached")

    def test_nothing_attached_says_so(self):
        line = folder_guidance([folder([], status="expanding", counts=FolderCounts(listed=4))],
                               document_read_available=True, spreadsheet_tools=False, presentation_tools=False)
        assert "no readable files yet" in line

    def test_no_folders_no_section(self):
        assert render_folders_section([], document_read_available=True) == ""


class TestMetric:
    def test_one_content_free_record_per_folder(self, monkeypatch):
        emitted = []
        monkeypatch.setattr("apis.shared.observability.emf.emit_emf_metrics", lambda *a, **k: emitted.append((a, k)))
        monkeypatch.setattr("apis.shared.observability.prompt_cache.prompt_cache_observability_enabled", lambda: True)
        f = folder([entry(1, "syllabus.pdf")])
        section = render_folders_section([f], document_read_available=True)

        record_folder_manifests([f], section)

        [((namespace,), kwargs)] = emitted
        assert namespace == "AgentCoreStack/Compaction"
        assert kwargs["metrics"] == {"FolderManifest": 1, "FolderManifestEntries": 1, "FolderManifestChars": len(section)}
        assert kwargs["properties"] == {"status": "ready", "cut": "false"}
        assert "syllabus" not in str(emitted) and "BIO" not in str(emitted) and "up-1" not in str(emitted)

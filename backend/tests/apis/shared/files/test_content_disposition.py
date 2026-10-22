"""Unit tests for the shared `Content-Disposition` builder.

Pins the two properties every caller depends on: the value is always latin-1
encodable (Starlette's response-header codec, and S3's for a presigned
`response-content-disposition`), and no filename can forge a header line.
"""

from __future__ import annotations

import pytest

from apis.shared.files.content_disposition import build_content_disposition


class TestAsciiNames:
    def test_plain_name_round_trips(self):
        cd = build_content_disposition("attachment", "report.csv")
        assert cd == "attachment; filename=\"report.csv\"; filename*=UTF-8''report.csv"

    def test_disposition_type_is_honored(self):
        assert build_content_disposition("inline", "a.png").startswith("inline; ")

    def test_spaces_survive_the_quoted_string(self):
        cd = build_content_disposition("attachment", "My Brain.zip")
        assert 'filename="My Brain.zip"' in cd
        assert "filename*=UTF-8''My%20Brain.zip" in cd


class TestNonLatin1Names:
    @pytest.mark.parametrize(
        "name",
        [
            "Research \U0001f9e0 Notes.zip",          # emoji
            "研究ノート.zip",      # CJK
            "Phil’s Space.zip",                   # curly quote
            "Screenshot at 10.22.14 AM.png",      # U+202F, macOS screenshots
            "résumé.pdf",                    # latin-1 encodable but non-ASCII
        ],
    )
    def test_value_is_latin1_encodable(self, name):
        build_content_disposition("attachment", name).encode("latin-1")

    def test_real_name_is_preserved_in_filename_star(self):
        cd = build_content_disposition("attachment", "研究.zip")
        assert "filename*=UTF-8''%E7%A0%94%E7%A9%B6.zip" in cd
        # Nothing ASCII survived, so the fallback is the generic base — but it
        # still carries the extension so the client saves a usable file.
        assert 'filename="download.zip"' in cd


class TestHardening:
    @pytest.mark.parametrize("hostile", ['a"b.zip', "a\r\nX-Evil: 1.zip", "a\nb.zip"])
    def test_no_header_injection_in_the_ascii_fallback(self, hostile):
        cd = build_content_disposition("attachment", hostile)
        ascii_part = cd.split("; filename*=")[0]
        assert '"' not in ascii_part[len('attachment; filename="') : -1]
        assert "\r" not in cd and "\n" not in cd

    def test_long_name_truncates_but_keeps_the_extension(self):
        cd = build_content_disposition("attachment", "a" * 400 + ".zip")
        ascii_name = cd.split('filename="')[1].split('"')[0]
        assert ascii_name.endswith(".zip")
        assert len(ascii_name) <= 124

    @pytest.mark.parametrize("empty", ["", "   ", "...", "___"])
    def test_empty_or_fully_stripped_name_falls_back(self, empty):
        assert 'filename="download"' in build_content_disposition("attachment", empty)

    def test_dotted_name_is_not_mistaken_for_an_extension(self):
        # A long trailing segment is part of the name, not an extension.
        cd = build_content_disposition("attachment", "Notes. Final thoughts")
        assert 'filename="Notes. Final thoughts"' in cd

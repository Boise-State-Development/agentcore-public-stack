"""Tests for ``scrub_log`` — the CodeQL ``py/log-injection`` mitigation.

The helper shipped without tests and is now wrapped around a user-controlled value at
every admin/marketplace log site, so the properties it promises are worth pinning:
line terminators must never survive into a log record, and the original content must
still be legible to whoever is reading the log.
"""

import pytest

from apis.shared.security.log_sanitize import scrub_log


class TestLineTerminators:
    """The whole point: a user-supplied value cannot start a new log line."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("plain", "plain"),
            ("a\nb", "a\\nb"),
            ("a\rb", "a\\rb"),
            ("a\r\nb", "a\\r\\nb"),
            ("a\tb", "a\\tb"),
            ("\n", "\\n"),
        ],
    )
    def test_terminators_become_visible_escapes(self, raw: str, expected: str) -> None:
        assert scrub_log(raw) == expected

    def test_no_raw_newline_survives(self) -> None:
        forged = "agent-1\n2026-08-01 - root - ERROR - fabricated entry"
        out = scrub_log(forged)
        assert "\n" not in out
        assert "\r" not in out
        # Escaped rather than deleted — the attempt stays visible to a reader.
        assert "\\n" in out
        assert "fabricated entry" in out


class TestControlCharacters:
    def test_other_c0_controls_are_stripped(self) -> None:
        # NUL and BEL carry no debugging value and can corrupt log viewers.
        assert scrub_log("a\x00b\x07c") == "abc"

    def test_delete_is_stripped(self) -> None:
        assert scrub_log("a\x7fb") == "ab"

    def test_printable_unicode_is_preserved(self) -> None:
        # Only control characters are in scope; a name in any script must survive.
        assert scrub_log("café — 日本語") == "café — 日本語"


class TestNonStringInputs:
    """Call sites pass exceptions and ids, not only strings."""

    def test_exception_is_stringified(self) -> None:
        assert scrub_log(ValueError("bad\nvalue")) == "bad\\nvalue"

    @pytest.mark.parametrize("raw,expected", [(None, "None"), (42, "42"), (True, "True")])
    def test_scalars_stringify(self, raw: object, expected: str) -> None:
        assert scrub_log(raw) == expected

    def test_result_is_always_str(self) -> None:
        assert isinstance(scrub_log(object()), str)


class TestIdempotence:
    """Double-wrapping a value must not mangle it — nested helpers happen."""

    def test_scrubbing_twice_matches_scrubbing_once(self) -> None:
        raw = "a\nb\tc\x00d"
        assert scrub_log(scrub_log(raw)) == scrub_log(raw)

"""Tests for the log-injection sanitizer (CodeQL py/log-injection).

`scrub_log` must neutralize line terminators and control characters so a
user-controlled value can't forge or inject extra log lines.
"""

from apis.shared.security.log_sanitize import scrub_log
from apis.shared.security import scrub_log as scrub_log_reexport


class TestScrubLog:
    def test_replaces_newlines_and_carriage_returns(self):
        out = scrub_log("alice\nADMIN login succeeded")
        assert "\n" not in out
        assert "\r" not in out
        assert out == "alice\\nADMIN login succeeded"

    def test_neutralizes_crlf_forged_log_line(self):
        forged = "user1\r\n2024-01-01 ERROR forged entry"
        out = scrub_log(forged)
        assert "\r" not in out and "\n" not in out
        # The whole payload collapses onto a single line.
        assert "\n" not in out.splitlines()[0]
        assert len(out.splitlines()) == 1

    def test_replaces_tabs(self):
        assert scrub_log("a\tb") == "a\\tb"

    def test_strips_other_control_characters(self):
        # NUL, bell, escape (ANSI), and DEL must be removed entirely.
        assert scrub_log("a\x00b\x07c\x1bd\x7fe") == "abcde"

    def test_leaves_ordinary_text_untouched(self):
        assert scrub_log("provider-google_workspace.v2") == "provider-google_workspace.v2"

    def test_coerces_non_string_values(self):
        assert scrub_log(42) == "42"
        assert scrub_log(None) == "None"

    def test_handles_exception_objects(self):
        err = ValueError("bad input\ninjected line")
        out = scrub_log(err)
        assert "\n" not in out
        assert out == "bad input\\ninjected line"

    def test_reexported_from_security_package(self):
        assert scrub_log_reexport is scrub_log

"""UTC timestamp serialization, and a guard against the idiom that broke it.

`datetime.now(timezone.utc)` is tz-aware, so `.isoformat()` already emits `+00:00`.
Appending `"Z"` yields `…+00:00Z` — an offset *and* a Z — which is not valid ISO 8601 and
parses to `Invalid Date` in JavaScript. It spread to 52 call sites before anyone read one
of the rendered dates, because every SPA formatter falls back silently on an unparseable
value ("Last updated —", "recently").
"""

import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.timestamps import from_iso, to_iso, utc_now_iso

# The exact expression that caused the outage. Kept as one regex so the guard test below
# and this docstring cannot drift apart.
BROKEN_IDIOM = re.compile(r'now\(timezone\.utc\)\.isoformat\(\)\s*\+\s*"Z"')

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _js_parseable(value: str) -> bool:
    """Mirror the subset of ISO 8601 that `new Date()` accepts.

    Deliberately *not* `datetime.fromisoformat` — Python 3.11+ happily parses the broken
    `+00:00Z` form that browsers reject, so validating with it would pass the very string
    this module exists to prevent.
    """
    return bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", value)
    )


def test_broken_form_is_actually_rejected_by_the_matcher():
    """Guard the guard: the old output must fail `_js_parseable`, or these tests prove nothing."""
    assert not _js_parseable("2026-07-27T05:09:55.853557+00:00Z")


def test_utc_now_iso_is_js_parseable():
    assert _js_parseable(utc_now_iso())


def test_utc_now_iso_ends_in_z_with_no_offset():
    value = utc_now_iso()
    assert value.endswith("Z")
    assert "+00:00" not in value


def test_to_iso_normalizes_an_aware_utc_datetime():
    dt = datetime(2026, 7, 27, 5, 9, 55, 853557, tzinfo=timezone.utc)
    assert to_iso(dt) == "2026-07-27T05:09:55.853557Z"


def test_to_iso_treats_naive_as_utc_rather_than_local():
    """A naive value must not be shifted by the host's offset."""
    naive = datetime(2026, 7, 27, 5, 9, 55, 853557)
    aware = naive.replace(tzinfo=timezone.utc)
    assert to_iso(naive) == to_iso(aware)


def test_to_iso_converts_a_non_utc_offset_to_utc():
    dt = datetime(2026, 7, 27, 0, 9, 55, tzinfo=timezone(timedelta(hours=-5)))
    assert to_iso(dt) == "2026-07-27T05:09:55Z"


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-27T05:09:55.853557Z",
        "2026-07-27T05:09:55Z",
    ],
)
def test_known_good_shapes_are_js_parseable(value):
    assert _js_parseable(value)


def test_from_iso_reads_the_legacy_form():
    """Rows written before the fix keep `…+00:00Z` forever; they must still parse."""
    assert from_iso("2026-07-27T05:09:55.853557+00:00Z") == datetime(
        2026, 7, 27, 5, 9, 55, 853557, tzinfo=timezone.utc
    )


def test_from_iso_reads_the_current_form():
    assert from_iso("2026-07-27T05:09:55.853557Z") == datetime(
        2026, 7, 27, 5, 9, 55, 853557, tzinfo=timezone.utc
    )


def test_from_iso_always_returns_an_aware_datetime():
    """The trap that `rstrip("Z")` fell into.

    `datetime.fromisoformat(value.rstrip("Z"))` returns an *aware* datetime for the broken
    `…+00:00Z` form and a *naive* one for a correct `…Z` — so fixing the writers alone
    silently flipped round-tripped values to naive, and any comparison against
    `datetime.now(timezone.utc)` would then raise TypeError.
    """
    for value in (
        "2026-07-27T05:09:55.853557+00:00Z",
        "2026-07-27T05:09:55.853557Z",
        "2026-07-27T05:09:55.853557",  # no offset at all
    ):
        assert from_iso(value).tzinfo is not None, value


def test_round_trip_through_to_iso_and_back_is_lossless():
    dt = datetime(2026, 7, 27, 5, 9, 55, 853557, tzinfo=timezone.utc)
    assert from_iso(to_iso(dt)) == dt


def test_no_compensating_rstrip_parser_remains():
    """`rstrip("Z")` before `fromisoformat` encodes the writer's bug into the reader.

    ⚠️ Use `from_iso` instead. A reader shaped around a broken writer is how this survived
    for as long as it did.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{i}"
        for path in SRC.rglob("*.py")
        if path.name != "timestamps.py"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if 'rstrip("Z")' in line
    ]
    assert offenders == [], f"Use apis.shared.timestamps.from_iso. Offenders: {offenders}"


def test_the_broken_idiom_is_gone_from_the_tree():
    """A reintroduction fails CI instead of waiting for someone to notice a blank date.

    ⚠️ If this fails on a line you just wrote: use `utc_now_iso()` / `to_iso(dt)` from
    `apis.shared.timestamps`. Do not "fix" it by editing this test.
    """
    offenders = [
        f"{path.relative_to(SRC)}:{i}"
        for path in SRC.rglob("*.py")
        if path.name != "timestamps.py"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if BROKEN_IDIOM.search(line)
    ]
    assert offenders == [], (
        "datetime.now(timezone.utc).isoformat() + \"Z\" emits an invalid `+00:00Z` "
        "timestamp that JavaScript cannot parse. Use apis.shared.timestamps instead. "
        f"Offenders: {offenders}"
    )

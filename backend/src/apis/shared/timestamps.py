"""UTC timestamp serialization — one spelling, because the obvious one is wrong.

``datetime.now(timezone.utc)`` is timezone-**aware**, so ``.isoformat()`` already renders
the offset as ``+00:00``. Appending ``"Z"`` to that produces ``2026-07-27T05:09:55.853557
+00:00Z`` — an offset *and* a Z — which is not valid ISO 8601. ``new Date()`` returns
``Invalid Date`` for it in every browser, so any UI that formats the value silently shows
a placeholder instead of a date.

That is exactly what happened: the agent detail page rendered "Last updated —" on an agent
edited minutes earlier, and the admin Reports queue rendered "recently" for every report
ever filed, because both SPA helpers fall back defensively on an unparseable date. Nothing
errored, nothing logged, and the values looked plausible in DynamoDB. The idiom had spread
to 50+ call sites by the time anyone read one of the rendered dates.

``sync_policies.service._iso`` had already discovered and fixed this locally; this module
is that fix, promoted so there is one implementation to import rather than a convention to
remember.

**Use** :func:`utc_now_iso` for "now", and :func:`to_iso` for a datetime you already hold::

    from apis.shared.timestamps import utc_now_iso

    item["createdAt"] = utc_now_iso()

⚠️ **Do not write ``datetime.now(timezone.utc).isoformat() + "Z"``.** A regression test
(`tests/shared/test_timestamps.py`) greps the tree for it, so a reintroduction fails CI
rather than waiting for someone to notice a blank date.

**On the stored data.** Rows written before this landed still hold the ``+00:00Z`` form.
They are deliberately **not** backfilled: the SPA normalizes both spellings on read
(`normalizeIsoTimestamp` in `frontend/.../utils/date.ts`), which fixes the historical rows
without rewriting items whose values are embedded in GSI sort keys (``GSI5_SK =
CREATED#{created_at}`` and friends). Mixed formats are safe to compare as strings: the
suffix only differs *after* the full date-time-microseconds, so two distinct instants
still order correctly, and only an exact microsecond tie could compare unequal.
"""

from datetime import datetime, timezone

__all__ = ["utc_now_iso", "to_iso", "from_iso"]


def to_iso(dt: datetime) -> str:
    """Serialize a datetime as strict ISO 8601 UTC with a ``Z`` suffix.

    Accepts aware or naive datetimes. A naive value is *assumed* to be UTC — that is the
    convention everywhere in this codebase, and treating it as local time would silently
    shift timestamps by the host's offset in a container that is not on UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    # `.isoformat()` renders UTC as "+00:00"; normalize that single spelling to "Z" so the
    # result is valid ISO 8601 rather than carrying both an offset and a Z.
    return dt.isoformat().replace("+00:00", "Z")


def utc_now_iso() -> str:
    """The current time as strict ISO 8601 UTC (``…Z``)."""
    return to_iso(datetime.now(timezone.utc))


def from_iso(value: str) -> datetime:
    """Parse a stored timestamp, tolerating the legacy ``…+00:00Z`` spelling.

    Always returns an **aware** UTC datetime.

    ⚠️ **This exists because the obvious parser silently depended on the writer's bug.**
    Call sites used ``datetime.fromisoformat(value.rstrip("Z"))``, which happened to work
    only while the writer emitted the broken form: stripping the ``Z`` off
    ``…+00:00Z`` leaves ``…+00:00`` (aware), but stripping it off a *correct* ``…Z``
    leaves a bare ``…`` (naive). Fixing the writers alone would therefore have flipped
    every round-tripped timestamp from aware to naive — comparisons against
    ``datetime.now(timezone.utc)`` would start raising
    ``TypeError: can't compare offset-naive and offset-aware datetimes``.

    `tests/shared/test_skills_models.py::test_round_trip_preserves_fields` is what caught
    it. Do not reintroduce ``rstrip("Z")``.
    """
    normalized = value.replace("+00:00Z", "Z")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        # A stored value with no offset at all: same UTC assumption as `to_iso`.
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

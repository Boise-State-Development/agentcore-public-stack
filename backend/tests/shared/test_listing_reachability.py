"""Reachability: publication and access are separate axes, and the store guards only one.

`GET /agents/store` is a pure sparse-GSI5 read with **no** access check, while
`GET /agents/{id}` enforces `get_assistant_with_access_check`. Nothing in the listing
lifecycle touches `visibility`, so an approved PRIVATE or SHARED Agent gets a shelf tile
that 404s for everyone who cannot already reach it. Both live dev listings were in exactly
that state when this was found.

These tests pin the *derivation* and, more importantly, the decision that it stays
advisory — a gate here, or an auto-widening on approve, would make a store decision
silently rewrite an access decision.
"""

import pytest

from apis.app_api.agent_designer.services.listing_service import _reachability


class _Assistant:
    """Only the field the derivation reads. A full Assistant would hide what matters."""

    def __init__(self, visibility: str):
        self.visibility = visibility


@pytest.mark.parametrize(
    "visibility,expected",
    [
        ("PUBLIC", "everyone"),
        ("SHARED", "shared_only"),
        ("PRIVATE", "owner_only"),
    ],
)
def test_reachability_projects_visibility(visibility, expected):
    assert _reachability(_Assistant(visibility)) == expected


def test_only_public_is_unlimited():
    """The two non-public states must stay distinguishable.

    Collapsing them to one "not public" value would lose the difference between "nobody
    but the author" and "the team it was shared with" — which is the difference between a
    mistake and a deliberate, legitimate publication.
    """
    values = {v: _reachability(_Assistant(v)) for v in ("PUBLIC", "SHARED", "PRIVATE")}
    assert len(set(values.values())) == 3
    assert values["PUBLIC"] == "everyone"


def test_unknown_visibility_is_treated_as_unreachable():
    """Fail closed on the *warning*, which is the safe direction here.

    An unrecognized visibility must not resolve to `everyone` — that would suppress the
    warning on precisely the case nobody anticipated.
    """
    assert _reachability(_Assistant("SOMETHING_NEW")) == "owner_only"

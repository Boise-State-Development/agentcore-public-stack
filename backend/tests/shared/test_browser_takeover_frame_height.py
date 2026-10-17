"""The sign-in panel height on the chat request: tolerant in, bounded out.

The SPA sends ``browser_frame_height`` on every chat request (spec D3a). It is
a hint about a window, so nothing about it may fail a turn: an unusable value
becomes None and the takeover falls back to its fixed size.
"""

import pytest

from apis.inference_api.chat.models import InvocationRequest
from apis.shared.browser_takeover import (
    FRAME_HEIGHT_STATE_KEY,
    coerce_frame_height,
    frame_height_from,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (778, 778),
        (778.0, 778),
        (1, 1),
        (10_000, 10_000),
        (0, None),
        (-5, None),
        (10_001, None),
        (777.5, None),
        (True, None),
        ("778", None),
        (None, None),
        ([778], None),
    ],
)
def test_coerce_frame_height(value, expected) -> None:
    assert coerce_frame_height(value) == expected


def test_frame_height_from_reads_the_invocation_state_key() -> None:
    assert frame_height_from({FRAME_HEIGHT_STATE_KEY: 640}) == 640
    assert frame_height_from({"something_else": 640}) is None
    assert frame_height_from(None) is None


class TestInvocationRequestField:
    def test_absent_is_none(self) -> None:
        assert InvocationRequest(session_id="s").browser_frame_height is None

    def test_a_usable_height_is_kept(self) -> None:
        req = InvocationRequest(session_id="s", browser_frame_height=778)
        assert req.browser_frame_height == 778

    @pytest.mark.parametrize("junk", ["tall", -1, 0, 99_999, 12.5, True])
    def test_an_unusable_height_never_fails_the_request(self, junk) -> None:
        req = InvocationRequest(session_id="s", browser_frame_height=junk)
        assert req.browser_frame_height is None

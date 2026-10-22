"""Carrying a queued follow-up through the resume path.

Mid-turn steering's paused-turn case (docs/specs/mid-turn-steering.md). A turn
paused for OAuth consent or tool approval has no running loop to steer, and the
pause releases its lease — inbox and all — when the stream closes. The
follow-ups the user queued meanwhile ride the resume request and are seeded
onto the resumed turn's lease, where the ordinary ``SteeringHook`` injects them
at its first tool boundary.

This covers the payload's shape at the API edge. The seeding itself — and the
load-bearing acquire→seed order, since acquire REMOVEs the inbox — lives in
``tests/shared/test_session_lease.py``.
"""

import pytest
from pydantic import ValidationError

from apis.inference_api.chat.models import InvocationRequest
from apis.shared.sessions.session_lease import STEER_QUEUE_MAX_CHARS


class TestRequestShape:
    def test_carries_entries_with_their_client_minted_ids(self):
        request = InvocationRequest(
            session_id="s1",
            steering=[{"id": "e1", "text": "use the other file"}],
        )
        # The id is the one the composer holds and `steering_applied` names
        # back — carrying a fresh id would make the ack unmatchable.
        assert [(e.id, e.text) for e in request.steering] == [
            ("e1", "use the other file")
        ]

    def test_absent_by_default(self):
        assert InvocationRequest(session_id="s1").steering is None

    def test_rejects_an_empty_entry(self):
        with pytest.raises(ValidationError):
            InvocationRequest(session_id="s1", steering=[{"id": "e1", "text": ""}])
        with pytest.raises(ValidationError):
            InvocationRequest(session_id="s1", steering=[{"id": "", "text": "hi"}])

    def test_rejects_text_past_the_inbox_cap(self):
        with pytest.raises(ValidationError):
            InvocationRequest(
                session_id="s1",
                steering=[{"id": "e1", "text": "x" * (STEER_QUEUE_MAX_CHARS + 1)}],
            )

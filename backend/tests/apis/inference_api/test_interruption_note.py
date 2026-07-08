"""The reason-driven interruption note prepended to the turn after an
interrupted one (see `_build_interruption_note` in inference-api chat routes).

The note is built from the reason POPPED from the session record at turn
start (`clear_interrupted_turn` returns it), because the reason is not
knowable at cancellation time — the client's `user_stopped` signal races the
server-side `connection_lost` backstop and precedence only settles in
DynamoDB. The two reasons carry opposite guidance, which is the whole point
of capturing intent.
"""

from apis.inference_api.chat.routes import _build_interruption_note


class TestBuildInterruptionNote:
    def test_user_stopped_tells_model_not_to_resume(self):
        note = _build_interruption_note("user_stopped")
        assert note.startswith("<interruption_note>")
        assert note.endswith("</interruption_note>")
        assert "deliberately stopped" in note
        assert "do not" in note.lower()

    def test_connection_lost_tells_model_it_may_continue(self):
        note = _build_interruption_note("connection_lost")
        assert note.startswith("<interruption_note>")
        assert "did not stop it deliberately" in note
        assert "continue" in note.lower()

    def test_unknown_reason_treated_as_technical_drop(self):
        # `unknown` carries no user intent, so it must NOT claim the user
        # stopped the response.
        note = _build_interruption_note("unknown")
        assert "deliberately stopped" not in note
        assert "did not stop it deliberately" in note

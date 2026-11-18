"""``smoke_turns.py`` when the user it runs as is over its quota.

On dev from 2026-11-15 to 11-17 the nightly smoke ran as an eval user whose
monthly quota was spent, so every turn was refused with a ``quota_exceeded``
frame after ``message_stop``. Each row failed the tail contract, and
``--publish`` wrote ``pass 0 / fail 14`` rows, which read like a broken build.
A refused turn now stops the run as a setup error (exit 2) and nothing is
published.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "smoke_turns.py"
_spec = importlib.util.spec_from_file_location("smoke_turns", _SCRIPT)
smoke_turns = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = smoke_turns
_spec.loader.exec_module(smoke_turns)

Frame = smoke_turns.Frame
REFUSAL = "You've reached your usage limit for this period."


def _refused_turn() -> Any:
    """The frames dev streamed for a refused turn: the refusal is the assistant text, then the event."""
    names: List[Tuple[str, Dict[str, Any]]] = [
        ("message_start", {"role": "assistant"}),
        ("content_block_delta", {"type": "text", "text": REFUSAL}),
        ("message_stop", {"stopReason": "quota_exceeded"}),
        ("quota_exceeded", {"message": REFUSAL}),
        ("done", {}),
    ]
    return smoke_turns.TurnResult(
        status_code=200, frames=[Frame(name=n, data=d, t_ms=float(i)) for i, (n, d) in enumerate(names)]
    )


class _QuotaBlockedTransport:
    via_app_api = True
    label = "cognito as eval-harness@example.invalid"

    def __init__(self) -> None:
        self.turns = 0
        self.deleted: List[str] = []

    async def stream_turn(self, payload: Dict[str, Any], **_: Any) -> Any:
        self.turns += 1
        return _refused_turn()

    async def get_calls(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        return 200, {"calls": []}

    async def delete_session(self, session_id: str) -> int:
        self.deleted.append(session_id)
        return 204

    async def close(self) -> None:
        return None


def test_frame_contract_raises_quota_blocked_not_a_row_failure() -> None:
    with pytest.raises(smoke_turns.QuotaBlocked, match="usage limit"):
        smoke_turns.assert_frame_contract(_refused_turn())


def test_quota_row_accepts_the_refusal_in_the_tail() -> None:
    # The quota_exceeded row expects exactly this turn, so the tail must allow it there.
    assert isinstance(smoke_turns.assert_frame_contract(_refused_turn(), expect_quota_refusal=True), list)


def test_a_quota_blocked_run_exits_2_after_one_turn_and_publishes_nothing(tmp_path: pathlib.Path, capsys) -> None:
    transport = _QuotaBlockedTransport()
    report = tmp_path / "turn-smoke-report.json"
    args = smoke_turns.parse_args(
        ["--base-url", "https://dev.example.test/api", "--auth", "eval-user", "--prefix", "dev-example",
         "--publish", "--json", str(report)]
    )
    publish = AsyncMock(return_value=True)
    with patch.object(smoke_turns, "build_transport", AsyncMock(return_value=transport)), \
            patch.object(smoke_turns, "publish_smoke", publish), \
            patch("apis.shared.costs.ledger_publish.read_runtime_image_tag", return_value="tag"), \
            patch.object(smoke_turns, "_health_version", return_value="1.36.1"):
        code = asyncio.run(smoke_turns.main_async(args))

    assert code == 2
    assert transport.turns == 1, "the run stops at the first refused turn"
    publish.assert_not_called()
    assert transport.deleted, "the refused turn's session is still cleaned up"
    assert "over its quota" in capsys.readouterr().err
    body = json.loads(report.read_text())
    assert body["published"] is False
    assert body["quotaBlocked"].startswith("plain_first_turn:")

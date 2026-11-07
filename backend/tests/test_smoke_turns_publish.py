"""``smoke_turns.py --publish``: one ``LEDGER#SMOKE`` row per run (release-ledger spec §5.2, PR-5).

The row is keyed by the build that served the run's turns, read from the
run's own ``C#`` rows, with the Runtime's image tag (read before the first
turn) as the fallback; a failed publish never changes the exit status.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import pathlib
import sys
from typing import Any, Dict, List

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "smoke_turns.py"
_spec = importlib.util.spec_from_file_location("smoke_turns", _SCRIPT)
smoke_turns = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = smoke_turns
_spec.loader.exec_module(smoke_turns)

RUNTIME = "d5ead33e05f3e229"
SESSION = "smoke-0123456789abcdef"
USER = "c1a2b3c4-d5e6-7890-abcd-ef0123456789"


def _args(**overrides: Any) -> argparse.Namespace:
    base = dict(prefix="dev-example", region="us-west-2", model_id=None, user_id=None, publish=True,
                base_url="https://dev.example.test/api", allow_prod=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _ctx() -> Any:
    ctx = smoke_turns.Context(transport=None, args=_args())  # type: ignore[arg-type]
    ctx.created_sessions = [SESSION]
    ctx.shared_session = SESSION
    ctx.first_token_ms = [800.0, 1000.0]
    return ctx


RESULTS = [
    smoke_turns.RowResult("plain_first_turn", "PASS", "ok"),
    smoke_turns.RowResult("second_turn_cache", "PASS", "cache rows unreadable (admin endpoint -> 403)"),
    smoke_turns.RowResult("attach_pdf", "FAIL", f"session {SESSION} failed with: the text was wrong"),
]


@pytest.fixture
def published(monkeypatch):
    from apis.shared.costs import ledger_publish

    rows: List[Dict[str, Any]] = []
    monkeypatch.setattr(ledger_publish, "put_ledger_row", lambda row, **kw: rows.append(dict(row)) or True)
    monkeypatch.setattr(smoke_turns.asyncio, "sleep", _no_sleep)
    return rows


async def _no_sleep(_seconds: float) -> None:
    return None


def _calls(*calls: Dict[str, Any]):
    async def fake(prefix: str, region: str, session_ids: List[str]) -> Dict[str, Any]:
        return {SESSION: list(calls)} if calls else {}

    return fake


def test_publishes_the_run_keyed_by_the_cost_rows_build(monkeypatch, published, capsys):
    first = {"timestamp": "t1", "appVersion": "1.33.0", "build": RUNTIME, "cost": {"total": 0.001},
             "tokenUsage": {"cacheReadInputTokens": 0, "cacheWriteInputTokens": 3000}}
    second = {"timestamp": "t2", "appVersion": "1.33.0", "build": RUNTIME, "cost": {"total": 0.001},
              "tokenUsage": {"cacheReadInputTokens": 3000, "cacheWriteInputTokens": 20}}
    monkeypatch.setattr(smoke_turns, "_session_calls", _calls(first, second))
    plan = smoke_turns.PublishPlan(started_at="2026-11-07T04:10:00.000001Z", image_tag="stale0000000000",
                                   app_version="1.33.0")
    ok = asyncio.run(smoke_turns.publish_smoke(_args(), plan, _ctx(), RESULTS, "https://dev.example.test/api", USER))
    assert ok and len(published) == 1
    row = published[0]
    assert (row["PK"], row["SK"]) == ("LEDGER#SMOKE", "2026-11-07T04:10:00.000001Z")
    assert (row["build"], row["buildSource"]) == (RUNTIME, "cost_rows")
    assert (row["pass"], row["fail"], row["skip"]) == (2, 1, 0)
    assert row["rows"] == {"plain_first_turn": "PASS", "second_turn_cache": "PASS", "attach_pdf": "FAIL"}
    assert (row["secondTurnCacheRead"], row["secondTurnCacheWrite"]) == (3000, 20)
    assert row["firstTokenMsMedian"] == 900 and row["calls"] == 2
    assert "the text was wrong" not in str(row) and SESSION not in str(row)
    assert "the turns ran on" in capsys.readouterr().err  # the stale image tag is named, not used


def test_falls_back_to_the_image_tag_read_before_the_run(monkeypatch, published):
    monkeypatch.setattr(smoke_turns, "_session_calls", _calls())
    plan = smoke_turns.PublishPlan(started_at="2026-11-07T04:10:00Z", image_tag=RUNTIME, app_version="1.33.0")
    assert asyncio.run(smoke_turns.publish_smoke(_args(), plan, _ctx(), RESULTS, "runtime:dev-example via http://x", USER))
    assert published[0]["buildSource"] == "image_tag" and published[0]["target"] == "runtime:dev-example_via_http://x"


def test_nothing_to_key_by_is_not_published(monkeypatch, published, capsys):
    monkeypatch.setattr(smoke_turns, "_session_calls", _calls())
    plan = smoke_turns.PublishPlan(started_at="2026-11-07T04:10:00Z")
    assert not asyncio.run(smoke_turns.publish_smoke(_args(), plan, _ctx(), RESULTS, "x", USER))
    assert published == [] and "not published" in capsys.readouterr().err


def test_publish_needs_a_prefix_before_any_turn(capsys):
    code = asyncio.run(smoke_turns.main_async(smoke_turns.parse_args(
        ["--base-url", "https://dev.example.test/api", "--publish"])))
    assert code == 2 and "--publish needs --prefix" in capsys.readouterr().err


def test_eval_user_auth_needs_a_prefix():
    with pytest.raises(SystemExit, match="needs --prefix"):
        smoke_turns.eval_user_credentials(None, "us-west-2")

"""The harness keeps a full watch run's ``watch_probe`` frame (quiet-watcher triage spec §3.1)."""

from apis.shared.harness.models import RunResult
from apis.shared.harness.sse import InvocationStreamAccumulator

FRAME = {
    "type": "watch_probe",
    "fingerprintVersion": 1,
    "probeable": True,
    "calls": [{"toolUseId": "t1", "name": "list", "fingerprint": "ab" * 16}],
}


def test_the_frame_is_kept_without_its_type():
    acc = InvocationStreamAccumulator()
    acc.handle("watch_probe", FRAME)
    assert acc.watch_probe == {k: v for k, v in FRAME.items() if k != "type"}
    assert acc.events_seen["watch_probe"] == 1


def test_no_frame_is_none():
    assert InvocationStreamAccumulator().watch_probe is None
    assert RunResult(run_id="r", session_id="s", user_id="u", status="completed").watch_probe is None


def test_it_serializes_with_the_result():
    result = RunResult(run_id="r", session_id="s", user_id="u", status="completed", watch_probe={"probeable": False})
    assert result.to_dict()["watch_probe"] == {"probeable": False}

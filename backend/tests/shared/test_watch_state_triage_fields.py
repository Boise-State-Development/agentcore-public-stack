"""The triage fields on ``SCHEDSTATE#`` (docs/specs/quiet-watcher-triage.md §3.4), on moto DynamoDB.

Written only through ``save_watch_state(triage=…)`` in the same conditional write as the rest
of the state; set when the run left them, removed when it didn't, untouched without ``triage``;
bounded on read; and never in the model's dump, so ``GET /schedules/{id}/state`` doesn't show them.
"""

from apis.shared.scheduled_prompts.watch_probe import FINGERPRINT_VERSION, MAX_PROBE_INPUT_CHARS, FullRunTriage
from apis.shared.scheduled_prompts.watch_state import WatchState, get_watch_state, save_watch_state

USER_ID = "user-1"
SCHEDULE = "sched-1"
PROBE = [{"name": "list_prs", "input": '{"repo":"r"}', "fingerprint": "ab" * 16}]


def _item(table) -> dict:
    return table.get_item(Key={"PK": f"USER#{USER_ID}", "SK": f"SCHEDSTATE#{SCHEDULE}"})["Item"]


def _probeable(**overrides) -> FullRunTriage:
    fields = dict(
        last_full_run_at="2026-11-25T12:00:00Z",
        probe=PROBE,
        probe_run_id="run-1",
        wake_at="2026-11-26T09:00:00Z",
    )
    fields.update(overrides)
    return FullRunTriage(**fields)


def test_a_probeable_run_writes_every_field(sessions_metadata_table):
    assert save_watch_state(USER_ID, SCHEDULE, WatchState(seen=["a"]), expected_version=None, triage=_probeable())
    item = _item(sessions_metadata_table)
    assert item["probe"] == PROBE
    assert item["fingerprintVersion"] == FINGERPRINT_VERSION
    assert (item["probeRunId"], item["lastFullRunAt"], item["wakeAt"]) == ("run-1", "2026-11-25T12:00:00Z", "2026-11-26T09:00:00Z")

    read = get_watch_state(USER_ID, SCHEDULE)
    assert read.probe == PROBE and read.fingerprint_version == FINGERPRINT_VERSION
    assert (read.probe_run_id, read.last_full_run_at, read.wake_at) == ("run-1", "2026-11-25T12:00:00Z", "2026-11-26T09:00:00Z")


def test_a_run_with_no_probe_removes_the_probe_its_version_and_run_id(sessions_metadata_table):
    save_watch_state(USER_ID, SCHEDULE, WatchState(), expected_version=None, triage=_probeable())
    save_watch_state(
        USER_ID, SCHEDULE, WatchState(), expected_version=1, triage=FullRunTriage(last_full_run_at="2026-11-25T13:00:00Z")
    )
    item = _item(sessions_metadata_table)
    assert not {"probe", "fingerprintVersion", "probeRunId", "wakeAt"} & set(item)
    assert item["lastFullRunAt"] == "2026-11-25T13:00:00Z"
    assert item["version"] == 2


def test_without_triage_the_fields_are_left_alone(sessions_metadata_table):
    save_watch_state(USER_ID, SCHEDULE, WatchState(), expected_version=None, triage=_probeable())
    save_watch_state(USER_ID, SCHEDULE, WatchState(cursors={"since": "x"}), expected_version=1)
    item = _item(sessions_metadata_table)
    assert item["probe"] == PROBE and item["wakeAt"] == "2026-11-26T09:00:00Z"
    assert item["cursors"] == {"since": "x"}


def test_a_lost_race_writes_none_of_it(sessions_metadata_table):
    save_watch_state(USER_ID, SCHEDULE, WatchState(), expected_version=None)
    assert save_watch_state(USER_ID, SCHEDULE, WatchState(), expected_version=7, triage=_probeable()) is False
    assert "probe" not in _item(sessions_metadata_table)


def test_a_probe_over_a_bound_is_stored_as_none(sessions_metadata_table):
    oversize = [{**PROBE[0], "input": "x" * (MAX_PROBE_INPUT_CHARS + 1)}]
    save_watch_state(USER_ID, SCHEDULE, WatchState(), expected_version=None, triage=_probeable(probe=oversize))
    item = _item(sessions_metadata_table)
    assert not {"probe", "fingerprintVersion", "probeRunId"} & set(item)
    assert item["lastFullRunAt"] == "2026-11-25T12:00:00Z"


def test_a_malformed_stored_probe_reads_as_none():
    assert WatchState.model_validate({"probe": [{"name": "x"}]}).probe is None
    assert WatchState.model_validate({"probe": [PROBE[0]] * 6}).probe is None
    assert WatchState.model_validate({"probe": "nope", "fingerprintVersion": "v1"}).fingerprint_version is None


def test_the_fields_never_reach_the_state_route():
    from apis.app_api.schedules.models import WatchStateResponse

    state = WatchState.model_validate(
        {"probe": PROBE, "fingerprintVersion": 1, "probeRunId": "run-1", "lastFullRunAt": "t", "wakeAt": "w", "seen": ["a"]}
    )
    dumped = WatchStateResponse(state=state).model_dump(by_alias=True)["state"]
    assert not {"probe", "fingerprintVersion", "probeRunId", "lastFullRunAt", "wakeAt"} & set(dumped)
    assert dumped["seen"] == ["a"]

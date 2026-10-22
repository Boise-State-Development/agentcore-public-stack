"""Deleting a session purges its AgentCore short-term events AND the summary
records extracted from it (Shared Projects Phase 0.2).

Semantic facts and preferences are actor-scoped and carry no source session,
so they are deliberately left alone; these tests pin both halves.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apis.app_api.sessions.services.session_service import SessionService

USER = "a1b2c3d4-0000-4000-8000-000000000001"
SESSION = "5b1f3c2e-8f7a-4d2b-9c1e-000000000001"
OTHER_SESSION = SESSION + "-sibling"
SUMMARY = "ConversationSummary-test"
SEMANTIC = "SemanticFactExtraction-test"


def _ns(strategy: str, session: str | None = None) -> str:
    base = f"/strategies/{strategy}/actors/{USER}/"
    return base + (f"sessions/{session}/" if session else "")


def _client(events=(), records=()):
    client = MagicMock()
    client.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
    client.list_events.return_value = {"events": [{"eventId": e} for e in events]}
    client.list_memory_records.return_value = {"memoryRecordSummaries": list(records)}
    client.batch_delete_memory_records.side_effect = lambda memoryId, records: {
        "successfulRecords": records,
        "failedRecords": [],
    }
    return client


def _run(client, summary_id=SUMMARY):
    config = SimpleNamespace(is_cloud_mode=True, memory_id="mem-test", region="us-west-2")
    with patch("agents.main_agent.session.memory_config.load_memory_config", return_value=config), \
         patch("boto3.client", return_value=client), \
         patch(
             "apis.app_api.memory.services.memory_service._get_strategy_namespaces",
             return_value=(SEMANTIC, "Pref-test", summary_id),
         ):
        SessionService().delete_agentcore_memory(SESSION, USER)


def test_deletes_events_and_only_this_sessions_summaries():
    client = _client(
        events=["e1", "e2"],
        records=[
            {"memoryRecordId": "sum-this", "namespaces": [_ns(SUMMARY, SESSION)]},
            # A prefix match on a different session id must not be deleted.
            {"memoryRecordId": "sum-sibling", "namespaces": [_ns(SUMMARY, OTHER_SESSION)]},
        ],
    )
    _run(client)

    assert client.delete_event.call_count == 2
    listed = client.list_memory_records.call_args.kwargs
    assert listed["namespace"] == f"/strategies/{SUMMARY}/actors/{USER}/sessions/{SESSION}"
    client.batch_delete_memory_records.assert_called_once_with(
        memoryId="mem-test", records=[{"memoryRecordId": "sum-this"}]
    )


def test_purges_summaries_even_when_no_events_remain():
    # Events expire after 90 days; the summaries extracted from them do not.
    client = _client(events=[], records=[{"memoryRecordId": "sum-old", "namespaces": [_ns(SUMMARY, SESSION)]}])
    _run(client)

    client.delete_event.assert_not_called()
    client.batch_delete_memory_records.assert_called_once()


def test_never_touches_actor_level_namespaces():
    client = _client(events=["e1"], records=[])
    _run(client)

    for call in client.list_memory_records.call_args_list:
        assert "/sessions/" in call.kwargs["namespace"]
    client.batch_delete_memory_records.assert_not_called()


def test_skips_purge_without_a_summary_strategy():
    client = _client(events=["e1"])
    _run(client, summary_id=None)

    client.list_memory_records.assert_not_called()
    assert client.delete_event.call_count == 1


def test_purge_failure_does_not_raise():
    client = _client(events=["e1"])
    client.list_memory_records.side_effect = RuntimeError("boom")
    _run(client)  # background task: logs, never raises

    assert client.delete_event.call_count == 1

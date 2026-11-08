"""Every EMF record names the build that emitted it (release-ledger spec §5.1)."""

import json
import logging
from typing import Dict, List

import pytest

from apis.shared.observability import emf


@pytest.fixture()
def records(monkeypatch: pytest.MonkeyPatch) -> List[Dict]:
    seen: List[Dict] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(json.loads(record.getMessage()))

    sink = _Sink()
    emf._emf_logger.addHandler(sink)
    monkeypatch.setattr(emf, "build_stamp", lambda: {"appVersion": "1.33.0", "build": "8f676bb2e1a4c0d2"})
    yield seen
    emf._emf_logger.removeHandler(sink)


def test_prompt_cache_record_carries_the_build(records):
    emf.emit_prompt_cache_metrics(cache_read_tokens=10, cache_write_tokens=0, avoidable_miss=False, model_id="m")
    (record,) = records
    assert record["appVersion"] == "1.33.0"
    assert record["build"] == "8f676bb2e1a4c0d2"
    assert record["modelId"] == "m"
    # A property, never a dimension: the fleet sums stay one stream.
    assert record["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]


def test_generic_record_carries_the_build_on_any_namespace(records):
    emf.emit_emf_metrics("AgentCoreStack/TurnLatency", {"FirstTokenMs": 1200}, properties={"streamKind": "agent"})
    (record,) = records
    assert record["appVersion"] == "1.33.0"
    assert record["build"] == "8f676bb2e1a4c0d2"
    assert record["streamKind"] == "agent"


def test_a_caller_s_own_property_wins(records):
    """The telemetry route reports the SPA's version under its own name, but a
    caller that deliberately sets `appVersion` (a backfill replay) is believed."""
    emf.emit_emf_metrics("X", {"M": 1}, properties={"appVersion": "1.32.0"})
    assert records[0]["appVersion"] == "1.32.0"

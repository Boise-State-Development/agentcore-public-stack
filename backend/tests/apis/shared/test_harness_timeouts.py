"""The headless harness's timeouts, pinned to the numbers they must agree with.

Three of them live in other packages, so nothing but this test keeps them in
step:

- the chat proxy's read timeout: a headless run should survive exactly the
  silences an attended turn survives (the harness once kept 300s after the
  proxy moved to 660s, and long silent runs failed only when headless);
- the scheduled-runs worker Lambda's timeout: a run must end before Lambda
  kills it, because the dispatcher invokes asynchronously and Lambda retries
  a timed-out invocation, which runs the prompt again;
- the SPA's ``RUN_LOST_AFTER_MS``: a "Run now" still running must not be
  reported lost.
"""

from __future__ import annotations

import re
from pathlib import Path

from apis.app_api.chat.proxy_routes import (
    _PROXY_READ_TIMEOUT_SECONDS,
    _PROXY_TIMEOUT_SECONDS,
)
from apis.shared.harness.runner import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MAX_DURATION_SECONDS,
    DEFAULT_READ_TIMEOUT_SECONDS,
    _build_http_client,
)

_REPO = Path(__file__).resolve().parents[4]
_WORKER_CONSTRUCT = _REPO / "infrastructure/lib/constructs/scheduled-runs/scheduled-runs-construct.ts"
_RUN_NOW_SERVICE = _REPO / "frontend/ai.client/src/app/schedules/services/run-now.service.ts"

# Time the worker needs after the run ends to record it and deliver the result.
_WORKER_BOOKKEEPING_SECONDS = 60


def test_read_timeout_matches_the_chat_proxy():
    assert DEFAULT_READ_TIMEOUT_SECONDS == _PROXY_READ_TIMEOUT_SECONDS
    assert CONNECT_TIMEOUT_SECONDS == _PROXY_TIMEOUT_SECONDS


def test_client_raises_only_the_read_timeout():
    client = _build_http_client(DEFAULT_READ_TIMEOUT_SECONDS)
    assert client.timeout.read == DEFAULT_READ_TIMEOUT_SECONDS
    assert client.timeout.connect == CONNECT_TIMEOUT_SECONDS
    assert client.timeout.write == CONNECT_TIMEOUT_SECONDS
    assert client.timeout.pool == CONNECT_TIMEOUT_SECONDS


def test_the_cap_allows_at_least_one_full_silence():
    assert DEFAULT_MAX_DURATION_SECONDS > DEFAULT_READ_TIMEOUT_SECONDS


def test_a_run_ends_before_the_worker_lambda_times_out():
    source = _WORKER_CONSTRUCT.read_text()
    worker = source[source.index("'ScheduledRunsWorkerLambda'") :]
    minutes = int(re.search(r"timeout: cdk\.Duration\.minutes\((\d+)\)", worker).group(1))
    assert DEFAULT_MAX_DURATION_SECONDS + _WORKER_BOOKKEEPING_SECONDS <= minutes * 60


def test_the_spa_waits_past_the_cap_before_calling_a_run_lost():
    source = _RUN_NOW_SERVICE.read_text()
    minutes = int(re.search(r"RUN_LOST_AFTER_MS = (\d+) \* 60 \* 1000", source).group(1))
    assert minutes * 60 > DEFAULT_MAX_DURATION_SECONDS

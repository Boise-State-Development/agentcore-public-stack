"""Shared fixtures for agentcore-identity OAuth tests.

These tests exercise `_resolve_workload_token` branches that depend on
whether `AGENTCORE_RUNTIME_WORKLOAD_NAME` is set. Other tests in the
broader pytest run can transitively call `load_dotenv(override=True)`
(e.g. via `apis.app_api.main`), which mutates `os.environ` for the rest
of the process. If `src/.env` defines that var, these tests then take
the workload-mint path instead of the cache-hit / consent-required path
they intend to exercise — failing with `WorkloadTokenUnavailableError`.

The autouse fixture below scrubs the var before every test in this
package so results are deterministic regardless of suite ordering or
local `.env` contents.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_workload_env(monkeypatch):
    monkeypatch.delenv("AGENTCORE_RUNTIME_WORKLOAD_NAME", raising=False)

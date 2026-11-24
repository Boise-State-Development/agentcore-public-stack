"""Shared fixtures for the built-in tool tests."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from apis.shared.skills import script_trust


class TrustRowsStub:
    """Stands in for ``script_trust.read_trust_rows``: no DynamoDB, every read recorded.

    ``rows`` is what the next read returns; set it to give a call a review or
    a switch. ``calls`` counts reads, so a test can prove a verdict was read
    on every call rather than kept.
    """

    def __init__(self) -> None:
        self.rows = script_trust.TrustRows()
        self.calls: List[Dict[str, Any]] = []
        self.fail: Optional[Exception] = None

    def __call__(self, record: Any, invoker_id: str, scope_id: Optional[str], **_: Any) -> script_trust.TrustRows:
        self.calls.append({"skill_id": getattr(record, "skill_id", ""), "invoker": invoker_id, "scope": scope_id})
        if self.fail is not None:
            raise self.fail
        return self.rows


@pytest.fixture(autouse=True)
def trust_rows(monkeypatch: pytest.MonkeyPatch) -> TrustRowsStub:
    """Every script call here reads its trust rows from this stub (no rows by default)."""
    stub = TrustRowsStub()
    monkeypatch.setattr(script_trust, "read_trust_rows", stub)
    return stub

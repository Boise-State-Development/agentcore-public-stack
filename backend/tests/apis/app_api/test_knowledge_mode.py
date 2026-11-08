"""``GET /assistants/{id}/knowledge-base/mode`` (apis/app_api/assistants/knowledge_mode.py).

The assessment itself is ``kb_full_context.assess_full_context``, covered by
``tests/shared/test_kb_full_context.py``; here, the route's gates and how it words
each verdict for the Agent Designer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.app_api.assistants.knowledge_mode as km
from apis.shared.assistants import kb_full_context as fc
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

USER = User(user_id="u-author", email="author@example.edu", name="Au", roles=["default"])
AGENT = "ast-designer-1"


def _assessment(**kwargs) -> fc.FullContextAssessment:
    base = dict(eligible=False, reason=None, document_count=0, skipped=(), text_chars=0, budget_tokens=50_000)
    base.update(kwargs)
    return fc.FullContextAssessment(**base)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", "true")
    monkeypatch.delenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, raising=False)
    permission = AsyncMock(return_value=(SimpleNamespace(assistant_id=AGENT), "owner"))
    monkeypatch.setattr(km, "resolve_assistant_permission", permission)
    state = SimpleNamespace(assessment=_assessment(eligible=True, document_count=3, text_chars=48_000), calls=[])

    def _assess(assistant_id, budget):
        state.calls.append((assistant_id, budget))
        if isinstance(state.assessment, Exception):
            raise state.assessment
        return state.assessment

    monkeypatch.setattr(fc, "assess_full_context", _assess)
    app = FastAPI()
    app.include_router(km.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: USER
    state.client = TestClient(app)
    state.permission = permission
    return state


def _get(setup):
    return setup.client.get(f"/assistants/{AGENT}/knowledge-base/mode")


class TestVerdicts:
    def test_sent_whole(self, setup):
        body = _get(setup).json()
        assert body == {
            "mode": "whole", "reason": None, "documentCount": 3, "estimatedTokens": 12_000,
            "budgetTokens": 50_000, "skippedDocuments": [],
        }
        assert setup.calls == [(AGENT, 50_000)]

    def test_whole_with_a_spreadsheet_left_to_search(self, setup):
        setup.assessment = _assessment(eligible=True, document_count=2, skipped=("budget.xlsx",), text_chars=400)
        body = _get(setup).json()
        assert body["mode"] == "whole" and body["skippedDocuments"] == ["budget.xlsx"]

    @pytest.mark.parametrize("reason", [
        fc.REASON_OVER_BUDGET, fc.REASON_DOCUMENT_OVER_BUDGET, fc.REASON_RAW_BYTES, fc.REASON_NO_TEXT_LAYER,
        fc.REASON_NO_TEXT_DOCUMENTS,
    ])
    def test_searched_says_why(self, setup, reason):
        setup.assessment = _assessment(reason=reason, document_count=4, text_chars=400_000)
        body = _get(setup).json()
        assert body["mode"] == "searched" and body["reason"] == reason
        assert body["estimatedTokens"] == 100_000

    def test_no_documents_yet(self, setup):
        setup.assessment = _assessment(reason=fc.REASON_EMPTY)
        assert _get(setup).json()["mode"] == "empty"

    def test_the_deployment_budget_is_the_one_used(self, setup, monkeypatch):
        monkeypatch.setenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, "75000")
        assert _get(setup).json()["budgetTokens"] == 75_000
        assert setup.calls == [(AGENT, 75_000)]


class TestGates:
    @pytest.mark.parametrize("env", [("ASSISTANT_KB_FULL_CONTEXT_ENABLED", ""), (fc.FULL_CONTEXT_MAX_TOKENS_ENV, "0")])
    def test_full_context_off_is_searched_and_reads_nothing(self, setup, monkeypatch, env):
        monkeypatch.setenv(*env)
        body = _get(setup).json()
        assert body["mode"] == "searched" and body["reason"] == "full_context_off"
        assert setup.calls == []

    def test_a_viewer_is_refused(self, setup):
        setup.permission.return_value = (SimpleNamespace(assistant_id=AGENT), "viewer")
        assert _get(setup).status_code == 403
        assert setup.calls == []

    def test_someone_who_cannot_see_the_agent_gets_404(self, setup):
        setup.permission.return_value = (None, None)
        assert _get(setup).status_code == 404

    def test_a_failure_renders_nothing_rather_than_500(self, setup):
        setup.assessment = RuntimeError("S3 is down")
        response = _get(setup)
        assert response.status_code == 200 and response.json()["mode"] == "unknown"

"""``GET /sessions/search`` end to end: the real route, service, session table and
``ManagedKbBackend``, with only the Bedrock runtime client replaced.

The isolation requirements (spec §7, PR-4) are proved here through the route,
not only the helper:

* each of two users sees only their own conversations;
* the Bedrock call always carries ``user_id equals <caller>``, taken from the
  session cookie and never from the request;
* with the filter **lost** (the fake KB ignores it and returns everyone's
  turns), nothing leaks, because hits are checked against the caller and joined
  to the caller's own session rows;
* a document whose attribute claims the caller but whose session belongs to
  someone else is dropped by the join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.sessions.search_routes import router
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.conversation_search.service import text_search_throttle
from apis.shared.sessions.search_text import first_prompt_value, normalize_search_text

AWS_KB_ID = "KBTESTCONV1"


# ── fakes ────────────────────────────────────────────────────────────────────
@dataclass
class Doc:
    session_id: str
    message_index: int
    user_id: str
    text: str
    score: float = 0.5
    created_at: str = "2026-10-07T17:09:45.716Z"
    project_id: Optional[str] = None

    @property
    def metadata(self) -> Dict[str, str]:
        md = {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "message_index": str(self.message_index),
            "created_at": self.created_at,
            "document_id": f"conv#{self.user_id}#{self.session_id}#{self.message_index}",
        }
        if self.project_id:
            md["project_id"] = self.project_id
        return md


def _matches(flt: Optional[Dict[str, Any]], metadata: Dict[str, str]) -> bool:
    if not flt:
        return True
    if "andAll" in flt:
        return all(_matches(f, metadata) for f in flt["andAll"])
    if "equals" in flt:
        return metadata.get(flt["equals"]["key"]) == flt["equals"]["value"]
    raise AssertionError(f"unexpected filter {flt}")


@dataclass
class FakeRuntime:
    """Bedrock's ``retrieve`` over a list of docs, best score first.

    ``honor_filter=False`` simulates a filter lost somewhere between the
    caller and the service: every user's matching turns come back.
    """

    docs: List[Doc]
    honor_filter: bool = True
    fail: bool = False
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def retrieve(self, **payload):
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("bedrock is down")
        config = payload["retrievalConfiguration"]["managedSearchConfiguration"]
        words = payload["retrievalQuery"]["text"].lower().split()
        found = [
            d
            for d in self.docs
            if (not self.honor_filter or _matches(config.get("filter"), d.metadata))
            and any(w in d.text.lower() for w in words)
        ]
        found.sort(key=lambda d: -d.score)
        return {
            "retrievalResults": [
                {
                    "content": {"text": d.text},
                    "score": d.score,
                    "location": {"type": "CUSTOM", "customDocumentLocation": {"id": f"conv#{d.user_id}#{d.session_id}#{d.message_index}"}},
                    "metadata": d.metadata,
                }
                for d in found[: config["numberOfResults"]]
            ]
        }


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("CONVERSATION_SEARCH_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    monkeypatch.delenv("CONVERSATION_RETENTION_DAYS", raising=False)
    text_search_throttle().reset()
    yield
    text_search_throttle().reset()


@pytest.fixture()
def tables(sessions_metadata_table, assistants_table):
    assistants_table.put_item(
        Item={
            "PK": "AST#conversations",
            "SK": "KB#conversations",
            "awsKbId": AWS_KB_ID,
            "awsDataSourceId": "DS1",
            "retrievalEngine": "managed",
        }
    )
    return sessions_metadata_table


@pytest.fixture()
def runtime(monkeypatch):
    fake = FakeRuntime(docs=[])
    monkeypatch.setattr("apis.shared.kb_backend.managed_backend.bedrock_agent_runtime_client", lambda: fake)
    return fake


def _session(table, user_id: str, session_id: str, title: str, *, status: str = "active", first_prompt: str = "",
             last_message_at: str = "2026-10-01T00:00:00+00:00", project_id: Optional[str] = None,
             title_lower: Optional[str] = None) -> None:
    item: Dict[str, Any] = {
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "sessionId": session_id,
        "userId": user_id,
        "title": title,
        "titleLower": normalize_search_text(title) if title_lower is None else title_lower,
        "status": status,
        "createdAt": "2026-09-01T00:00:00+00:00",
        "lastMessageAt": last_message_at,
        "messageCount": 4,
    }
    if not item["titleLower"]:
        del item["titleLower"]
    if status == "deleted":
        item["deleted"] = True
    if first_prompt:
        item["firstPrompt"] = first_prompt_value(first_prompt)
    if project_id:
        item["preferences"] = {"projectId": project_id}
        if status == "active":
            item["GSI5_PK"] = f"PROJECT#{project_id}#USER#{user_id}"
            item["GSI5_SK"] = f"{last_message_at}#{session_id}"
    table.put_item(Item=item)


def _client_as(user_id: str) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    user = User(email=f"{user_id}@example.com", user_id=user_id, name=user_id, roles=["User"])
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


def _search(user_id: str, q: str, **params) -> Dict[str, Any]:
    response = _client_as(user_id).get("/sessions/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: Dict[str, Any]) -> List[str]:
    return [r["sessionId"] for r in body["results"]]


#: What user-a's "budget" search finds with no text leg: the title, then the
#: conversation whose opening prompt mentions budgets.
LEXICAL_ONLY = [("a-budget", "title"), ("a-sql", "prompt")]


def _kinds(body: Dict[str, Any]) -> List[tuple]:
    return [(r["sessionId"], r["matchKind"]) for r in body["results"]]


@pytest.fixture()
def two_users(tables, runtime):
    _session(tables, "user-a", "a-budget", "Q3 budget review", last_message_at="2026-10-02T00:00:00+00:00")
    _session(tables, "user-a", "a-sql", "SQL help", first_prompt="How do I write a window function over budgets?")
    _session(tables, "user-b", "b-budget", "Budget for the lab", last_message_at="2026-10-03T00:00:00+00:00")
    _session(tables, "user-b", "b-other", "Unrelated")
    runtime.docs = [
        Doc("a-sql", 2, "user-a", "The budget totals use a window function.", score=0.9),
        Doc("b-other", 4, "user-b", "Secret budget numbers for user b.", score=0.95),
        Doc("b-budget", 0, "user-b", "Lab budget line items.", score=0.85),
    ]
    return tables


# ── the route ────────────────────────────────────────────────────────────────
def test_404_while_the_flag_is_off(monkeypatch, tables):
    monkeypatch.setenv("CONVERSATION_SEARCH_ENABLED", "")
    assert _client_as("user-a").get("/sessions/search", params={"q": "x"}).status_code == 404


def test_401_without_a_session(tables):
    from fastapi import HTTPException

    app = FastAPI()
    app.include_router(router)

    def _no_user():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user_from_session] = _no_user
    assert TestClient(app).get("/sessions/search", params={"q": "x"}).status_code == 401


def test_401_comes_before_the_flag(monkeypatch, tables):
    """An unauthenticated caller cannot tell whether search is switched on."""
    from fastapi import HTTPException

    monkeypatch.setenv("CONVERSATION_SEARCH_ENABLED", "")
    app = FastAPI()
    app.include_router(router)

    def _no_user():
        raise HTTPException(status_code=401, detail="Not authenticated")

    app.dependency_overrides[get_current_user_from_session] = _no_user
    assert TestClient(app).get("/sessions/search").status_code == 401


def test_two_users_each_see_only_their_own_conversations(two_users, runtime):
    a = _search("user-a", "budget", mode="all")
    b = _search("user-b", "budget", mode="all")

    assert set(_ids(a)) == {"a-budget", "a-sql"}
    assert set(_ids(b)) == {"b-budget", "b-other"}
    assert "Secret" not in str(a) and "window function" not in str(b)

    filters = [c["retrievalConfiguration"]["managedSearchConfiguration"]["filter"] for c in runtime.calls]
    assert filters == [
        {"equals": {"key": "user_id", "value": "user-a"}},
        {"equals": {"key": "user_id", "value": "user-b"}},
    ]
    assert all(c["knowledgeBaseId"] == AWS_KB_ID for c in runtime.calls)


def test_a_lost_filter_still_leaks_nothing(two_users, runtime):
    runtime.honor_filter = False
    body = _search("user-a", "budget", mode="all")
    assert set(_ids(body)) == {"a-budget", "a-sql"}
    assert "Secret" not in str(body) and "Lab budget" not in str(body)


def test_a_forged_owner_attribute_is_dropped_by_the_row_join(two_users, runtime):
    # The document claims user-a, but its session row lives under user-b.
    runtime.docs.append(Doc("b-other", 6, "user-a", "budget leak attempt", score=0.99))
    body = _search("user-a", "budget", mode="all")
    assert "b-other" not in _ids(body)
    assert "leak attempt" not in str(body)


def test_two_users_sharing_a_session_id_each_see_only_their_own_turn(tables, runtime):
    """Session ids are not unique across users (found on dev, 2026-10-07)."""
    _session(tables, "user-a", "shared", "Chat")
    _session(tables, "user-b", "shared", "Chat")
    runtime.docs = [
        Doc("shared", 0, "user-a", "budget, as user a wrote it", score=0.9),
        Doc("shared", 0, "user-b", "budget, as user b wrote it", score=0.8),
    ]
    for honor in (True, False):
        runtime.honor_filter = honor
        a = _search("user-a", "budget", mode="all")
        b = _search("user-b", "budget", mode="all")
        assert [r["snippet"] for r in a["results"]] == ["budget, as user a wrote it"]
        assert [r["snippet"] for r in b["results"]] == ["budget, as user b wrote it"]
        text_search_throttle().reset()


def test_a_user_parameter_is_ignored(two_users, runtime):
    body = _client_as("user-a").get(
        "/sessions/search", params={"q": "budget", "mode": "all", "userId": "user-b", "user_id": "user-b"}
    ).json()
    assert set(_ids(body)) == {"a-budget", "a-sql"}
    assert runtime.calls[0]["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]["equals"]["value"] == "user-a"


def test_merge_order_and_text_hit_shape(two_users):
    body = _search("user-a", "budget", mode="all")
    title, text = body["results"]
    assert (title["sessionId"], title["matchKind"], title["snippet"]) == ("a-budget", "title", "")
    assert text["sessionId"] == "a-sql"
    assert text["matchKind"] == "text"
    assert text["messageId"] == "msg-a-sql-2"
    assert text["score"] == 0.9
    assert "window function" in text["snippet"]
    assert body["textSearchAvailable"] is True


def test_lexical_mode_never_calls_the_knowledge_base(two_users, runtime):
    body = _search("user-a", "window function", mode="lexical")
    assert runtime.calls == []
    assert [(r["sessionId"], r["matchKind"]) for r in body["results"]] == [("a-sql", "prompt")]
    assert "window function" in body["results"][0]["snippet"]
    assert body["textSearchAvailable"] is True


def test_short_query_skips_the_text_leg(two_users, runtime):
    _search("user-a", "bu", mode="all")
    assert runtime.calls == []


def test_deleted_rows_are_dropped_and_archived_rows_are_marked(tables, runtime):
    _session(tables, "user-a", "gone", "Old budget", status="deleted")
    _session(tables, "user-a", "shelved", "Shelved", status="archived")
    runtime.docs = [
        Doc("gone", 0, "user-a", "budget from a deleted chat", score=0.9),
        Doc("shelved", 2, "user-a", "budget in an archived chat", score=0.8),
        Doc("never-existed", 0, "user-a", "budget with no row at all", score=0.7),
    ]
    body = _search("user-a", "budget", mode="all")
    assert _ids(body) == ["shelved"]
    assert body["results"][0]["archived"] is True


def test_turns_past_retention_are_dropped(tables, runtime, monkeypatch):
    monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "30")
    old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    _session(tables, "user-a", "s1", "Chat")
    runtime.docs = [
        Doc("s1", 0, "user-a", "budget, long ago", score=0.9, created_at=old),
        Doc("s1", 4, "user-a", "budget, recently", score=0.5),
    ]
    body = _search("user-a", "budget", mode="all")
    assert body["results"][0]["messageId"] == "msg-s1-4"
    assert body["results"][0]["alsoMatched"] == []


def test_one_row_per_conversation_with_also_matched(tables, runtime):
    _session(tables, "user-a", "s1", "Chat")
    runtime.docs = [Doc("s1", i * 2, "user-a", f"budget part {i}", score=1 - i / 10) for i in range(5)]
    body = _search("user-a", "budget", mode="all")
    assert len(body["results"]) == 1
    assert body["results"][0]["messageId"] == "msg-s1-0"
    assert body["results"][0]["alsoMatched"] == ["msg-s1-2", "msg-s1-4"]


def test_a_title_match_with_a_text_match_opens_at_the_passage(tables, runtime):
    _session(tables, "user-a", "s1", "Budget chat")
    runtime.docs = [Doc("s1", 6, "user-a", "the budget passage", score=0.7)]
    (result,) = _search("user-a", "budget", mode="all")["results"]
    assert result["matchKind"] == "title"
    assert result["messageId"] == "msg-s1-6"
    assert result["snippet"] == "the budget passage"


def test_project_scope(tables, runtime):
    _session(tables, "user-a", "p1", "Budget in project", project_id="proj-1")
    _session(tables, "user-a", "p2", "Budget elsewhere", project_id="proj-2")
    _session(tables, "user-a", "free", "Budget, no project")
    runtime.docs = [
        Doc("p1", 0, "user-a", "budget text", project_id="proj-1"),
        Doc("p2", 0, "user-a", "budget text", project_id="proj-2"),
    ]
    body = _search("user-a", "budget", mode="all", projectId="proj-1")
    assert _ids(body) == ["p1"]
    flt = runtime.calls[0]["retrievalConfiguration"]["managedSearchConfiguration"]["filter"]
    assert flt == {
        "andAll": [
            {"equals": {"key": "user_id", "value": "user-a"}},
            {"equals": {"key": "project_id", "value": "proj-1"}},
        ]
    }


def test_spent_rate_degrades_to_lexical(two_users, runtime):
    for _ in range(20):
        assert _search("user-a", "budget", mode="all")["textSearchAvailable"] is True
    body = _search("user-a", "budget", mode="all")
    assert body["textSearchAvailable"] is False
    assert _kinds(body) == LEXICAL_ONLY
    assert len(runtime.calls) == 20
    # Another user's bucket is their own.
    assert _search("user-b", "budget", mode="all")["textSearchAvailable"] is True


def test_index_off_means_lexical_only(two_users, runtime, monkeypatch):
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "")
    body = _search("user-a", "budget", mode="all")
    assert runtime.calls == []
    assert body["textSearchAvailable"] is False
    assert _kinds(body) == LEXICAL_ONLY


def test_a_failing_knowledge_base_degrades_to_lexical(two_users, runtime):
    runtime.fail = True
    body = _search("user-a", "budget", mode="all")
    assert body["textSearchAvailable"] is False
    assert _kinds(body) == LEXICAL_ONLY


def test_an_unprovisioned_knowledge_base_degrades_to_lexical(sessions_metadata_table, assistants_table, runtime):
    _session(sessions_metadata_table, "user-a", "s1", "Budget")
    body = _search("user-a", "budget", mode="all")
    assert body["textSearchAvailable"] is False
    assert runtime.calls == []
    assert _ids(body) == ["s1"]


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "x" * 201}, {"q": "x", "limit": 0}, {"q": "x", "mode": "vector"}])
def test_request_validation(tables, params):
    assert _client_as("user-a").get("/sessions/search", params=params).status_code == 422

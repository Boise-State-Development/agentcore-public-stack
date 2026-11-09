"""``seed_eval_fixtures.py``: idempotent, digest-keyed fixture projects and agents (eval-harness spec D13).

Run against an in-memory stand-in for app-api, so the order of calls (create,
upload, wait, the superseded file's delete-then-replace, archive before purge)
is pinned without a deployment.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import seed_eval_fixtures as seed  # noqa: E402
from eval_fixtures import entities  # noqa: E402


class FakeAppApi:
    """Projects, agents and their documents, enough of app-api for the seed."""

    def __init__(self) -> None:
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.docs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.calls: List[str] = []
        self.fail_file: Optional[str] = None
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    async def call(self, method: str, path: str, *, json_body: Any = None, params: Any = None):
        self.calls.append(f"{method} {path}")
        parts = path.strip("/").split("/")
        if path == "/projects" and method == "GET":
            return 200, {"projects": list(self.projects.values())}
        if path == "/projects" and method == "POST":
            pid, aid = self._id("p"), self._id("ast")
            self.projects[pid] = {"projectId": pid, "harnessAgentId": aid, "status": "active", **json_body}
            self.docs[f"/projects/{pid}/knowledge"] = {}
            return 201, self.projects[pid]
        if path == "/assistants" and method == "GET":
            return 200, {"assistants": list(self.agents.values())}
        if path == "/assistants" and method == "POST":
            aid = self._id("ast")
            self.agents[aid] = {"assistantId": aid, **json_body}
            self.docs[f"/assistants/{aid}/documents"] = {}
            return 201, self.agents[aid]
        if parts[0] == "projects" and len(parts) == 3 and parts[2] == "instructions":
            return 200, {"instructions": json_body["instructions"]}
        if parts[0] == "projects" and len(parts) == 2 and method == "PATCH":
            self.projects[parts[1]]["status"] = json_body["status"]
            return 200, self.projects[parts[1]]
        if parts[0] == "projects" and len(parts) == 2 and method == "DELETE":
            assert self.projects[parts[1]]["status"] == "archived", "purge before archive"
            del self.projects[parts[1]]
            return 204, None
        if parts[0] == "assistants" and len(parts) == 2 and method == "DELETE":
            del self.agents[parts[1]]
            return 204, None
        base = "/" + "/".join(parts[:3])
        if path.endswith("/upload-url"):
            doc_id = self._id("doc")
            status = "failed" if json_body["filename"] == self.fail_file else "complete"
            self.docs[base][doc_id] = {"documentId": doc_id, "filename": json_body["filename"], "status": status}
            return 200, {"documentId": doc_id, "uploadUrl": f"https://s3.example/{doc_id}"}
        if method == "GET" and path in self.docs:
            return 200, {"documents": list(self.docs[path].values())}
        if method == "DELETE" and base in self.docs:
            del self.docs[base][parts[3]]
            return 204, None
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture(autouse=True)
def _no_s3(monkeypatch):
    import httpx

    class _Put:
        status_code = 200
        text = ""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def put(self, url, content, headers):
            return _Put()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(seed, "POLL_S", 0)


def _seed(api, **kw):
    return asyncio.run(seed.seed(api, dry_run=kw.pop("dry_run", False), timeout_s=5))


def test_first_seed_creates_every_fixture_with_its_digest():
    api = FakeAppApi()
    report = _seed(api)
    assert all(line.endswith(": create") for line in report)
    names = {p["name"] for p in api.projects.values()} | {a["name"] for a in api.agents.values()}
    assert names == {entities.NAME_PREFIX + n for n in [*entities.PROJECTS, *entities.AGENTS]}
    small = next(p for p in api.projects.values() if p["name"].endswith("kb_small"))
    assert small["description"] == entities.description(entities.PROJECTS["kb_small"])


def test_a_second_seed_changes_nothing():
    api = FakeAppApi()
    _seed(api)
    api.calls.clear()
    report = _seed(api)
    assert all(line.endswith(": current") for line in report)
    assert all(call.startswith("GET ") for call in api.calls)


def test_a_stale_fixture_is_replaced_and_the_rest_left_alone():
    api = FakeAppApi()
    _seed(api)
    stale = next(p for p in api.projects.values() if p["name"].endswith("kb_small"))
    stale["description"] = "eval-fixture sha256:old"
    report = _seed(api)
    assert "project kb_small: replace" in report and "project kb_large: current" in report
    assert stale["projectId"] not in api.projects
    assert sum(p["name"].endswith("kb_small") for p in api.projects.values()) == 1


def test_the_superseded_file_is_uploaded_then_deleted_before_its_replacement():
    api = FakeAppApi()
    _seed(api)
    large = next(p for p in api.projects.values() if p["name"].endswith("kb_large"))
    files = [d["filename"] for d in api.docs[f"/projects/{large['projectId']}/knowledge"].values()]
    assert sorted(files) == sorted(entities.KB_LARGE_FILES)
    deletes = [c for c in api.calls if c.startswith(f"DELETE /projects/{large['projectId']}/knowledge/")]
    assert len(deletes) == 1


def test_a_failed_file_stops_the_seed():
    api = FakeAppApi()
    api.fail_file = "onboarding_policy.md"
    with pytest.raises(seed.SeedError, match="failed to ingest"):
        _seed(api)


def test_dry_run_writes_nothing():
    api = FakeAppApi()
    report = _seed(api, dry_run=True)
    assert report and not api.projects and not api.agents


def test_resolve_names_a_project_by_its_harness_agent():
    api = FakeAppApi()
    _seed(api)
    ids = asyncio.run(seed.resolve(api))
    large = next(p for p in api.projects.values() if p["name"].endswith("kb_large"))
    assert ids["project:kb_large"] == large["harnessAgentId"]
    assert set(ids) == {f"project:{n}" for n in entities.PROJECTS} | {f"agent:{n}" for n in entities.AGENTS}

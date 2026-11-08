"""Per-sample projects for the project-memory cases (eval-harness spec D14, D16, EH-6).

Run against an in-memory stand-in for app-api: the setup order (create, add
member2, personal spaces, seed), the read-back a memory check scores, the
purge, and the sweep of leftovers.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import eval_project_memory as epm  # noqa: E402
import seed_eval_fixtures as seed  # noqa: E402

from apis.shared.evals.cases import parse_case  # noqa: E402


class FakeApi:
    def __init__(self, who: str, world: Dict[str, Any]) -> None:
        self.who, self.w = who, world
        self.calls: List[str] = []

    async def call(self, method: str, path: str, *, json_body: Any = None, params: Any = None):
        w = self.w
        self.calls.append(f"{self.who} {method} {path}")
        parts = path.strip("/").split("/")
        if path == "/projects" and method == "POST":
            pid = f"p{len(w['projects']) + 1}"
            w["projects"][pid] = {"projectId": pid, "harnessAgentId": f"ast-{pid}", "status": "active",
                                  "createdAt": "2026-11-08T00:00:00Z", **json_body}
            w["members"][pid] = {}
            return 201, w["projects"][pid]
        if path == "/projects" and method == "GET":
            return 200, {"projects": list(w["projects"].values())}
        if parts[0] == "projects" and parts[2:] == ["members"]:
            w["members"][parts[1]][self.w["emails"]["member2"]] = json_body["role"]
            return 200, {}
        if parts[0] == "projects" and parts[2:] == ["memory", "mine"]:
            space = f"mine-{self.who}-{parts[1]}"
            w["spaces"].setdefault(space, {})
            return 200, {"spaceId": space}
        if parts[0] == "projects" and parts[2:] == ["memory"]:
            space = f"shared-{parts[1]}"
            w["spaces"].setdefault(space, {})
            return 200, {"sharedSpaceId": space, "personalSpaceId": None, "role": "owner"}
        if parts[0] == "projects" and parts[2:] == ["memory", "proposals"]:
            return 200, {"proposals": w["proposals"].get(parts[1], [])}
        if parts[0] == "memory" and parts[3:4] == ["entries"] and method == "PUT":
            w["spaces"][parts[2]]["/".join(parts[4:])] = json_body
            return 200, {}
        if parts[0] == "memory" and parts[3:] == ["entries"]:
            return 200, {"entries": [{"slug": k, "description": v.get("description", "")}
                                     for k, v in w["spaces"][parts[2]].items()]}
        if parts[0] == "memory" and parts[3:4] == ["entries"]:
            return 200, {"slug": parts[4], "content": w["spaces"][parts[2]][parts[4]]["body"]}
        if parts[0] == "memory" and parts[3:] == ["index"]:
            return 404, None
        if parts[0] == "projects" and len(parts) == 2 and method == "PATCH":
            w["projects"][parts[1]]["status"] = json_body["status"]
            return 200, {}
        if parts[0] == "projects" and len(parts) == 2 and method == "DELETE":
            assert w["projects"][parts[1]]["status"] == "archived"
            del w["projects"][parts[1]]
            return 204, None
        raise AssertionError(f"unexpected {method} {path}")


def _world():
    return {"projects": {}, "members": {}, "spaces": {}, "proposals": {}, "emails": {"member2": "m2@example.invalid"}}


def _accounts(world, member2=True):
    return epm.EvalAccounts(
        main=FakeApi("eval", world), main_user_id="u-eval",
        member2=FakeApi("member2", world) if member2 else None,
        member2_user_id="u-m2" if member2 else None, member2_email="m2@example.invalid",
    )


def _case(**run):
    return parse_case({
        "id": "project_memory.example", "family": "project_memory",
        "turns": [{"prompt": "Remember X."}],
        "run": {"project": "@ephemeral", **run},
        "expect": {"memory": [{"scope": "project", "unchanged": True}]},
    }, family="project_memory")


def test_setup_adds_member2_seeds_memory_and_reads_it_back():
    world = _world()
    case = _case(member2_role="viewer",
                 project_memory={"project": {"naming": {"body": "- Files are site-date.", "description": "naming"}}})
    project = asyncio.run(epm.EphemeralProject.create(_accounts(world), case, 0))
    assert world["members"][project.project_id] == {"m2@example.invalid": "viewer"}
    assert "Files are site-date" in project.before["project"] and project.before["mine"] == ""
    state = asyncio.run(project.inspect())
    assert state["project"] == project.before["project"] and state["proposals"] == 0
    asyncio.run(project.purge())
    assert project.project_id not in world["projects"]


def test_a_failed_setup_purges_the_project():
    world = _world()
    case = _case(member2_role="editor")
    with pytest.raises(epm.ProjectMemoryError, match="member2"):
        asyncio.run(epm.EphemeralProject.create(_accounts(world, member2=False), case, 0))
    assert world["projects"] == {}


def test_turns_run_as_the_right_user():
    accounts = _accounts(_world())
    assert accounts.user_id("eval") == "u-eval" and accounts.user_id("member2") == "u-m2"
    with pytest.raises(epm.ProjectMemoryError):
        _accounts(_world(), member2=False).user_id("member2")


def test_the_seed_sweeps_leftover_sample_projects_over_an_hour_old():
    world = _world()
    api = FakeApi("eval", world)
    asyncio.run(api.call("POST", "/projects", json_body={"name": epm.TMP_PREFIX + "old", "description": ""}))
    asyncio.run(api.call("POST", "/projects", json_body={"name": "[eval] kb_small", "description": ""}))
    from datetime import datetime, timezone

    created = datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp()
    assert asyncio.run(seed.sweep_ephemeral(api, dry_run=False, now=created + 60)) == []
    report = asyncio.run(seed.sweep_ephemeral(api, dry_run=False, now=created + 7200))
    assert report == [f"leftover {epm.TMP_PREFIX}old: purged"]
    assert [p["name"] for p in world["projects"].values()] == ["[eval] kb_small"]


def test_the_bearer_cache_keeps_one_token_per_user():
    import eval_harness

    class Inner:
        def __init__(self):
            self.minted = []

        async def mint_bearer_for_user(self, user_id):
            self.minted.append(user_id)
            return f"token-{user_id}"

    inner = Inner()
    auth = eval_harness.CachedBearerAuth(inner)

    async def go():
        return [await auth.mint_bearer_for_user(u) for u in ("u-eval", "u-m2", "u-eval", "u-m2")]

    assert asyncio.run(go()) == ["token-u-eval", "token-u-m2", "token-u-eval", "token-u-m2"]
    assert inner.minted == ["u-eval", "u-m2"]

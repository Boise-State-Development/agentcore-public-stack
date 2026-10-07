"""Proposals to a project's shared memory (Shared Projects 2.5a).

Real ``MemorySpaceService``, ``ProjectService`` and ``NotificationService``
over moto, as in the 2.4a suite: who may propose, review and withdraw comes
from the project, and an approval is an ordinary save.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.memory.service import (
    MemoryProposalStateError,
    MemorySpaceConcurrencyError,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemoryValidationError,
)
from apis.shared.notifications.service import NotificationService
from apis.shared.projects.memory_proposals import ProjectMemoryProposals, ProposalProjectError

from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    ITEMS,
    OWNER,
    STRANGER,
    VIEWER,
    _team,
    env,
    gateway,
    memory,
    projects,
)
from tests.shared.test_project_settings import AuditRecorder
from tests.shared.test_projects import TABLE as PROJECTS_TABLE

FACT = "- Term codes are YYYYTT because the SIS requires it.\n"


@pytest.fixture()
def inbox(env) -> NotificationService:
    return NotificationService(table_name=PROJECTS_TABLE)


@pytest.fixture()
def team(projects):
    return _team(projects)


@pytest.fixture()
def proposals(projects, memory, inbox) -> ProjectMemoryProposals:
    return ProjectMemoryProposals(
        repository=projects.repository, memory=memory, notifications=inbox, audit=AuditRecorder()
    )


def _kinds(inbox: NotificationService, user: User, kind: str) -> List[dict]:
    return [n.payload for n in inbox.list(user.email)[0] if n.kind == kind]


def _save(memory, team, user, slug, text, **kw):
    return memory.save_entry(team.shared_space_id, user.user_id, user.email, slug, text, **kw)


class TestPropose:
    def test_a_viewer_proposes_and_the_owner_and_editors_hear_about_it(self, proposals, memory, inbox, team):
        proposal, warnings = proposals.propose(team.project_id, VIEWER, "sis", FACT, description="SIS conventions")

        assert (proposal.state, proposal.slug, proposal.base_version, proposal.proposer_kind) == (
            "pending", "sis", 0, "member",
        )
        assert proposal.proposer_email == VIEWER.email and warnings == []
        assert memory.repository.get_index(team.shared_space_id).entries == []
        for reviewer in (OWNER, EDITOR):
            assert _kinds(inbox, reviewer, "project_proposal_pending") == [
                {"proposalId": proposal.proposal_id, "slug": "sis"}
            ]
        assert _kinds(inbox, VIEWER, "project_proposal_pending") == []
        assert proposals.audit.records[-1]["action"] == "project.memory_proposed"

    def test_checked_like_a_save_so_a_bad_file_is_refused_now(self, proposals, memory, inbox, team):
        with pytest.raises(MemoryValidationError):
            proposals.propose(team.project_id, VIEWER, "sis", "Just some prose.")
        assert memory.repository.list_proposals(team.shared_space_id) == []
        assert _kinds(inbox, OWNER, "project_proposal_pending") == []

    def test_a_non_member_is_not_found(self, proposals, team):
        with pytest.raises(ProposalProjectError) as e:
            proposals.propose(team.project_id, STRANGER, "sis", FACT)
        assert e.value.status_code == 404

    def test_an_archived_project_takes_no_proposals(self, proposals, projects, team):
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        with pytest.raises(ProposalProjectError) as e:
            proposals.propose(team.project_id, VIEWER, "sis", FACT)
        assert e.value.status_code == 409

    def test_a_members_own_space_needs_no_review(self, projects, memory, team):
        mine = projects.get_or_create_personal_space(team.project_id, VIEWER)
        with pytest.raises(MemoryValidationError, match="shared memory"):
            memory.create_proposal(mine, VIEWER.user_id, VIEWER.email, "sis", FACT)

    def test_pending_proposals_are_capped(self, proposals, team, monkeypatch):
        monkeypatch.setenv("MEMORY_MAX_PENDING_PROPOSALS", "1")
        proposals.propose(team.project_id, VIEWER, "a", FACT)
        with pytest.raises(MemoryProposalStateError, match="waiting for review"):
            proposals.propose(team.project_id, VIEWER, "b", FACT)


class TestReview:
    def test_editors_see_every_proposal_and_members_their_own(self, proposals, team):
        mine = proposals.propose(team.project_id, VIEWER, "a", FACT)[0]
        theirs = proposals.propose(team.project_id, EDITOR, "b", FACT)[0]

        assert [p.proposal_id for p in proposals.list(team.project_id, OWNER)] == [theirs.proposal_id, mine.proposal_id]
        assert [p.proposal_id for p in proposals.list(team.project_id, VIEWER)] == [mine.proposal_id]
        with pytest.raises(MemorySpaceNotFoundError):
            proposals.get(team.project_id, VIEWER, theirs.proposal_id)

    def test_approving_saves_it_indexes_a_new_file_and_tells_the_proposer(self, proposals, memory, inbox, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT, description="SIS conventions")[0]
        decided, result = proposals.approve(team.project_id, EDITOR, proposal.proposal_id, note="Thanks")

        assert (decided.state, decided.result_version, decided.decided_by_email, decided.note) == (
            "approved", 1, EDITOR.email, "Thanks",
        )
        [version] = memory.repository.list_file_versions(team.shared_space_id, "sis")
        assert (version.reason, version.proposal_id, version.updated_by) == ("proposal", proposal.proposal_id, EDITOR.user_id)
        assert "[[sis]] — SIS conventions" in memory.read_index(team.shared_space_id, OWNER.user_id, OWNER.email)
        assert _kinds(inbox, VIEWER, "project_proposal_decided") == [
            {"proposalId": proposal.proposal_id, "slug": "sis", "decision": "approved", "note": "Thanks"}
        ]
        assert proposals.audit.records[-1]["after"] == {"proposalId": proposal.proposal_id, "slug": "sis", "version": 1}

    def test_a_viewer_cannot_approve(self, proposals, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        with pytest.raises(MemorySpacePermissionError):
            proposals.approve(team.project_id, VIEWER, proposal.proposal_id)

    def test_a_file_that_moved_on_needs_an_edited_approval(self, proposals, memory, team):
        _save(memory, team, EDITOR, "sis", ITEMS)
        proposal = proposals.propose(team.project_id, VIEWER, "sis", ITEMS + FACT)[0]
        assert proposal.base_version == 1
        _save(memory, team, EDITOR, "sis", ITEMS + "- A newer fact.\n")

        assert proposals.get(team.project_id, EDITOR, proposal.proposal_id)[1] is True
        with pytest.raises(MemorySpaceConcurrencyError, match="has changed since"):
            proposals.approve(team.project_id, EDITOR, proposal.proposal_id)
        assert proposals.get(team.project_id, EDITOR, proposal.proposal_id)[0].state == "pending"

        decided, result = proposals.approve(
            team.project_id, EDITOR, proposal.proposal_id, text=ITEMS + "- A newer fact.\n" + FACT,
        )
        assert decided.edited and result.ref.version == 3

    def test_a_failed_save_puts_the_proposal_back(self, proposals, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        with pytest.raises(MemoryValidationError):
            proposals.approve(team.project_id, EDITOR, proposal.proposal_id, text="Prose is not an item.")
        assert proposals.get(team.project_id, EDITOR, proposal.proposal_id)[0].state == "pending"

    def test_rejecting_writes_nothing_and_tells_the_proposer_why(self, proposals, memory, inbox, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        decided = proposals.reject(team.project_id, OWNER, proposal.proposal_id, note="  Already  covered  ")

        assert (decided.state, decided.note) == ("rejected", "Already covered")
        assert memory.repository.get_index(team.shared_space_id).entries == []
        assert _kinds(inbox, VIEWER, "project_proposal_decided")[0]["decision"] == "rejected"

    def test_a_decision_is_final(self, proposals, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        proposals.reject(team.project_id, OWNER, proposal.proposal_id)
        for decide in (
            lambda: proposals.approve(team.project_id, EDITOR, proposal.proposal_id),
            lambda: proposals.reject(team.project_id, EDITOR, proposal.proposal_id),
            lambda: proposals.withdraw(team.project_id, VIEWER, proposal.proposal_id),
        ):
            with pytest.raises(MemoryProposalStateError, match="already rejected"):
                decide()

    def test_only_the_proposer_withdraws(self, proposals, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        with pytest.raises(MemorySpaceNotFoundError):
            proposals.withdraw(team.project_id, EDITOR, proposal.proposal_id)
        assert proposals.withdraw(team.project_id, VIEWER, proposal.proposal_id).state == "withdrawn"

    def test_an_archived_project_takes_no_decisions_but_allows_a_withdrawal(self, proposals, projects, team):
        a = proposals.propose(team.project_id, VIEWER, "a", FACT)[0]
        asyncio.run(projects.update_project(team.project_id, OWNER, status="archived"))
        with pytest.raises(ProposalProjectError) as e:
            proposals.approve(team.project_id, OWNER, a.proposal_id)
        assert e.value.status_code == 409
        assert proposals.withdraw(team.project_id, VIEWER, a.proposal_id).state == "withdrawn"

    def test_a_long_note_is_refused(self, proposals, team):
        proposal = proposals.propose(team.project_id, VIEWER, "sis", FACT)[0]
        with pytest.raises(MemoryValidationError, match="280"):
            proposals.reject(team.project_id, OWNER, proposal.proposal_id, note="x" * 281)


class TestTheTool:
    @pytest.fixture()
    def runtime(self, memory, monkeypatch):
        monkeypatch.setenv("MEMORY_SPACES_ENABLED", "true")
        monkeypatch.setattr("apis.shared.memory.service.MemorySpaceService", lambda: memory)
        monkeypatch.setattr("agents.builtin_tools.memory_spaces.project_tools.MemorySpaceService", lambda: memory)
        return memory

    def _tools(self, team, user):
        from agents.builtin_tools.memory_spaces.project_tools import ProjectMemoryScopes, make_project_memory_tools

        tools = make_project_memory_tools(ProjectMemoryScopes.for_member(team.project_id, team.shared_space_id, None, user))
        return {t.tool_spec["name"]: t for t in tools}

    def test_a_viewer_proposes_through_the_assistant(self, runtime, inbox, team):
        result = asyncio.run(self._tools(team, VIEWER)["memory_propose"](slug="sis", text=FACT, description="SIS"))

        assert result["status"] == "success"
        assert 'Proposed a new file, "sis" for review' in result["content"][0]["text"]
        [proposal] = runtime.repository.list_proposals(team.shared_space_id)
        assert (proposal.proposer_kind, proposal.proposer_id) == ("agent", VIEWER.user_id)
        assert _kinds(inbox, OWNER, "project_proposal_pending")

    def test_a_viewers_refused_save_points_at_proposing(self, runtime, team):
        result = asyncio.run(self._tools(team, VIEWER)["memory_save"](scope="project", slug="sis", text=FACT))
        assert result["status"] == "error" and "memory_propose" in result["content"][0]["text"]

    def test_a_bad_file_is_an_error_result(self, runtime, team):
        result = asyncio.run(self._tools(team, VIEWER)["memory_propose"](slug="sis", text="Prose."))
        assert result["status"] == "error" and result["content"][0]["text"].startswith("❌ Not proposed:")


class TestRoutes:
    @pytest.fixture()
    def client_for(self, projects, memory, inbox, monkeypatch):
        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(
            memory_routes, "_proposals",
            lambda: ProjectMemoryProposals(repository=projects.repository, memory=memory, notifications=inbox),
        )
        monkeypatch.setattr(memory_routes, "display_names", lambda emails: {})

        def make(user: User) -> TestClient:
            app = FastAPI()
            app.include_router(memory_routes.router)
            app.dependency_overrides[get_current_user_from_session] = lambda: user
            return TestClient(app)

        return make

    def test_propose_review_and_the_status_codes(self, client_for, team):
        base = f"/projects/{team.project_id}/memory/proposals"
        created = client_for(VIEWER).post(base, json={"slug": "sis", "text": FACT})
        assert created.status_code == 201
        body = created.json()
        assert body["isMine"] and body["proposedByEmail"] == VIEWER.email and "proposerId" not in body
        pid = body["proposalId"]

        assert client_for(STRANGER).get(base).status_code == 404
        assert client_for(VIEWER).post(f"{base}/{pid}/approve", json={}).status_code == 403
        assert client_for(VIEWER).post(base, json={"slug": "sis", "text": "prose"}).status_code == 400

        approved = client_for(OWNER).post(f"{base}/{pid}/approve", json={"note": "ok"})
        assert approved.status_code == 200 and approved.json()["resultVersion"] == 1
        assert client_for(OWNER).post(f"{base}/{pid}/reject", json={}).status_code == 409
        assert [p["state"] for p in client_for(VIEWER).get(base).json()["proposals"]] == ["approved"]
        assert client_for(EDITOR).get(f"{base}/nope").status_code == 404

    def test_the_queue_marks_stale_rows_and_a_detail_carries_the_current_file(self, client_for, memory, team):
        _save(memory, team, EDITOR, "sis", ITEMS)
        base = f"/projects/{team.project_id}/memory/proposals"
        pid = client_for(VIEWER).post(base, json={"slug": "sis", "text": ITEMS + FACT}).json()["proposalId"]
        fresh = client_for(VIEWER).post(base, json={"slug": "new-file", "text": FACT}).json()["proposalId"]
        assert {p["proposalId"]: p["stale"] for p in client_for(OWNER).get(f"{base}?state=pending").json()["proposals"]} == {
            pid: False, fresh: False,
        }

        _save(memory, team, EDITOR, "sis", ITEMS + "- A newer fact.\n")
        rows = {p["proposalId"]: p["stale"] for p in client_for(OWNER).get(base).json()["proposals"]}
        assert rows == {pid: True, fresh: False}

        detail = client_for(OWNER).get(f"{base}/{pid}").json()
        assert detail["stale"] is True
        assert not detail["currentText"].startswith("---") and "A newer fact." in detail["currentText"]
        assert client_for(OWNER).get(f"{base}/{fresh}").json()["currentText"] is None

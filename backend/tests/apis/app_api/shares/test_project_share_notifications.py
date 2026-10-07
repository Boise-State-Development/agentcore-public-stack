"""Share notifications: ``notify`` and ``note`` on a project share (shared-projects 2.5b).

Sharing a task can tell members about it. Nobody is told unless the sharer asks,
the sharer is never told, and naming a non-member is a 400 before anything is
written. The note travels on the share row, the ``SHARED_TASK#`` pointer and the
notification payload.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest
from pydantic import ValidationError

from apis.app_api.shares.models import CreateShareRequest, UpdateShareRequest
from apis.app_api.shares.service import ProjectShareError, ShareService
from apis.shared.notifications.models import Notification
from apis.shared.notifications.service import NotificationService

from tests.apis.app_api.shares.test_project_shares import (  # noqa: F401  (fixtures)
    AUTHOR,
    OWNER,
    VIEWER,
    _pointers,
    _session,
    _sources,
    env,
    project,
    projects,
    shares,
)
from tests.shared.test_project_settings import AuditRecorder
from tests.shared.test_projects import TABLE

PENDING = "invitee@example.edu"


@pytest.fixture()
def inbox(env) -> NotificationService:
    return NotificationService(table_name=TABLE)


@pytest.fixture()
def notifying(shares: ShareService, inbox: NotificationService) -> ShareService:
    shares._notifications = inbox
    shares._audit = AuditRecorder()
    return shares


def _create(shares: ShareService, project, schedule=None, session_id: str = "s1", **body):
    meta = _session(session_id=session_id, project_id=project.project_id)
    meta_patch, msgs_patch = _sources(meta)
    request = CreateShareRequest(accessLevel="project", **body)
    with meta_patch, msgs_patch:
        return asyncio.run(shares.create_share(meta.session_id, AUTHOR, request, schedule=schedule))


def _shared(inbox: NotificationService, email: str) -> List[Notification]:
    """The inbox's share notifications (adding a member already left a ``project_invited``)."""
    return [n for n in inbox.list(email)[0] if n.kind == "project_task_shared"]


def _kinds(inbox: NotificationService, email: str) -> List[str]:
    return [n.kind for n in _shared(inbox, email)]


class TestWhoIsTold:
    def test_nobody_is_told_without_notify(self, notifying, inbox, project):
        _create(notifying, project)
        assert all(_kinds(inbox, e) == [] for e in (OWNER.email, VIEWER.email))
        after = notifying._audit.records[-1]["after"]
        assert (after["notified"], after["hasNote"]) == (0, False)

    def test_all_tells_the_owner_and_every_member_but_the_sharer(self, notifying, inbox, projects, project):
        projects.add_members(project.project_id, OWNER, [PENDING], "viewer")
        share = _create(notifying, project, notify={"all": True}, note="Can you take the vendor reply?")

        for email in (OWNER.email, VIEWER.email, PENDING):
            [n] = _shared(inbox, email)
            assert n.kind == "project_task_shared"
            assert (n.project_id, n.project_name, n.actor_email) == (project.project_id, "Project A", AUTHOR.email)
            assert n.payload == {"shareId": share.share_id, "title": "Budget draft", "note": "Can you take the vendor reply?"}
        assert _kinds(inbox, AUTHOR.email) == []

    def test_named_members_only_and_emails_match_case_insensitively(self, notifying, inbox, project):
        _create(notifying, project, notify={"emails": [" Viewer@Example.edu "]})
        assert _kinds(inbox, VIEWER.email) == ["project_task_shared"]
        assert _kinds(inbox, OWNER.email) == []

    def test_naming_the_sharer_alone_tells_nobody(self, notifying, inbox, project):
        _create(notifying, project, notify={"emails": [AUTHOR.email]})
        assert notifying._audit.records[-1]["after"]["notified"] == 0

    def test_a_non_member_is_a_400_that_names_them_and_shares_nothing(self, notifying, projects, project):
        with pytest.raises(ProjectShareError) as e:
            _create(notifying, project, notify={"emails": [VIEWER.email, "Nobody@Example.edu"]})
        assert e.value.status_code == 400
        assert "nobody@example.edu" in str(e.value)
        assert notifying._find_shares_by_session("s1") == []
        assert _pointers(projects, project.project_id) == []

    def test_the_fan_out_is_handed_to_the_scheduler_not_run_inline(self, notifying, inbox, project):
        queued = []
        _create(notifying, project, schedule=lambda fn, *args: queued.append((fn, args)), notify={"all": True})
        assert _kinds(inbox, VIEWER.email) == []

        fn, args = queued.pop()
        fn(*args)
        assert _kinds(inbox, VIEWER.email) == ["project_task_shared"]

    def test_a_failed_inbox_write_never_fails_the_share(self, notifying, inbox, projects, project, monkeypatch):
        monkeypatch.setattr(type(inbox), "table", property(lambda self: (_ for _ in ()).throw(RuntimeError("down"))))
        share = _create(notifying, project, notify={"all": True})
        assert [p.share_id for p in _pointers(projects, project.project_id)] == [share.share_id]


class TestTheNote:
    def test_the_note_is_on_the_pointer_and_a_reshare_replaces_it(self, notifying, projects, project):
        _create(notifying, project, note="  First pass, numbers unchecked  ")
        [pointer] = _pointers(projects, project.project_id)
        assert pointer.note == "First pass, numbers unchecked"

        _create(notifying, project)
        [pointer] = _pointers(projects, project.project_id)
        assert pointer.note is None

    def test_the_trail_records_a_count_and_whether_there_was_a_note(self, notifying, project):
        _create(notifying, project, notify={"all": True}, note="FYI")
        after = notifying._audit.records[-1]["after"]
        assert (after["notified"], after["hasNote"]) == (2, True)
        assert "recipients" not in after and "note" not in after

    def test_leaving_the_project_drops_the_note(self, notifying, project):
        share = _create(notifying, project, note="FYI")
        asyncio.run(notifying.update_share(share.share_id, AUTHOR, UpdateShareRequest(accessLevel="public")))
        assert "note" not in notifying._get_share_item(share.share_id)


class TestTheRequest:
    @pytest.mark.parametrize("body", [
        {"accessLevel": "public", "notify": {"all": True}},
        {"accessLevel": "public", "note": "hi"},
        {"accessLevel": "project", "notify": {"all": True, "emails": ["a@x.edu"]}},
        {"accessLevel": "project", "notify": {"all": False}},
        {"accessLevel": "project", "notify": {"emails": []}},
        {"accessLevel": "project", "note": "x" * 281},
    ])
    def test_refused(self, body):
        with pytest.raises(ValidationError):
            CreateShareRequest(**body)

    def test_a_blank_note_is_no_note(self):
        assert CreateShareRequest(accessLevel="project", note="   ").note is None

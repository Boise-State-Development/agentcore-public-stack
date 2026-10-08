"""Splits, link maintenance and supersede markers (Shared Projects 2.6c).

The pure pieces (verifier, ``apply_ops``, the planner's prompt) are tested
directly; the rest uses the 2.6a/2.6b harness: real services over moto, a
scripted planner, and a recorder in place of the worker invoke.

What is under test:

- a split is offered only to a file at the soft size threshold, and every other
  file's prompt and tool are byte-for-byte what they were;
- a split moves items unchanged (anchors and provenance kept, plus where they
  came from), leaves a pointer item where they stood, and the new file joins
  ``MEMORY.md``; nothing it moves is archived, and no other file is rewritten;
- a failed save of the file rolls back the files its splits made;
- a member's own memory applies and undoes a split whole; project memory
  proposes it, and approval creates the file;
- an item that replaced others reads what it replaced (its supersede marker).
"""

from __future__ import annotations

from datetime import date
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.memory.format import Item
from apis.shared.memory.maintenance import planner as planner_module
from apis.shared.memory.maintenance.ops import apply_ops
from apis.shared.memory.maintenance.planner import FileSize, Planner, render_file_for_planner
from apis.shared.memory.maintenance.verify import PlannedChange, split_pointer, verify_plan
from apis.shared.memory.models import MemoryEntryRef
from apis.shared.memory.service import MemorySpaceConcurrencyError
from apis.shared.projects.memory_files import ProjectMemoryFiles

from tests.shared.test_project_memory_maintenance import (  # noqa: F401 (fixtures)
    MODEL,
    NOW,
    ScriptedPlanner,
    inbox,
    invoked,
    maintenance,
    proposals,
    runner_for,
    team,
)
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    VIEWER,
    env,
    gateway,
    memory,
    projects,
)

TODAY = date(2026, 10, 7)
CANVAS = [
    "Batch Canvas enrollment calls in groups of 50; larger batches hit the rate limit.",
    "Canvas enrollment sync runs nightly at 02:00 Mountain time from the integration server.",
    "Canvas section ids are not stable across terms, so always key enrollments on the SIS section id.",
    "The Canvas API token for the sync belongs to the integration service account, not a person.",
]
SIS = [
    "Term codes are YYYYTT because the SIS requires it, and TT is 10, 20 or 30.",
    "SIS course numbers keep their leading zeros; store them as strings, never as numbers.",
    "The SIS nightly extract lands in the shared drive by 01:30, before the Canvas sync starts.",
    "Cross-listed SIS sections share one Canvas course; the parent section owns the enrollments.",
    "Students with a FERPA hold are sent to Canvas without their preferred name.",
]
FILE = "".join(f"- {line}\n" for line in CANVAS + SIS)
# The file is about 255 tokens with its frontmatter (len // 4 in this harness):
# a 300-token cap puts its soft threshold (75%) at 225, so it is offered splits.
CAP = "300"
SPLIT = PlannedChange(
    type="split", ids=(5, 6, 7, 8, 9), new_slug="SIS Conventions", description="SIS term codes, course numbers and extracts",
    why="These are about the SIS, not Canvas",
)
FIVE = (1, 2, 3, 4, 5)
POINTER = split_pointer("sis-conventions", "SIS term codes, course numbers and extracts")


def _items(lines: List[str]) -> List[Item]:
    return [Item(text=t, anchor=f"a{i:07d}") for i, t in enumerate(lines)]


def _ref(slug: str, aliases=()) -> MemoryEntryRef:
    return MemoryEntryRef(slug=slug, content_hash="h", size=1, s3_key="k", aliases=list(aliases))


def _texts(memory, space_id: str, slug: str, user: User = OWNER) -> List[str]:
    _, items, _ = memory.read_file_items(space_id, user.user_id, user.email, slug)
    return [i.text for i in items]


def _index(memory, space_id: str) -> str:
    space = memory.repository.get_space(space_id)
    return memory.store.get(space.index_s3_key).decode("utf-8") if space.index_s3_key else ""


# ── the verifier ────────────────────────────────────────────────────────


class TestVerifySplit:
    def test_a_split_moves_items_under_a_cleaned_name_with_a_pointer(self):
        ops, report = verify_plan([SPLIT], _items(CANVAS + SIS), today=TODAY, files=[_ref("canvas")], allow_split=True)
        [op] = ops
        assert (op.type, op.new_slug, op.description, op.text) == (
            "split", "sis-conventions", "SIS term codes, course numbers and extracts", POINTER,
        )
        assert [s.text for s in op.sources] == SIS and op.removed == op.anchors
        assert report.dropped == []

    @pytest.mark.parametrize(
        "change, files, code",
        [
            (SPLIT, [_ref("canvas"), _ref("sis-conventions")], "name_taken"),
            (SPLIT, [_ref("canvas"), _ref("sis", aliases=["SIS-Conventions"])], "name_taken"),
            (PlannedChange(type="split", ids=FIVE, new_slug="MEMORY.md", description="x"), [], "bad_name"),
            (PlannedChange(type="split", ids=FIVE, new_slug="../etc", description="x"), [], "bad_name"),
            (PlannedChange(type="split", ids=FIVE, new_slug="sis", description="  "), [], "missing_description"),
            (PlannedChange(type="split", ids=FIVE, new_slug="sis", description="x" * 200), [], "bad_description"),
            # A single-topic reference cut into small lists is what the floor stops.
            (PlannedChange(type="split", ids=(1, 2, 3, 4), new_slug="sis", description="x"), [], "too_small"),
            (PlannedChange(type="split", ids=tuple(range(1, 10)), new_slug="sis", description="x"), [], "bad_shape"),
        ],
    )
    def test_a_split_that_breaks_a_rule_is_dropped(self, change, files, code):
        ops, report = verify_plan([change], _items(CANVAS + SIS), today=TODAY, files=files, allow_split=True)
        assert ops == [] and [d.code for d in report.dropped] == [code]

    def test_only_a_large_file_is_split_and_pinned_items_stay(self):
        items = _items(CANVAS + SIS)
        _, report = verify_plan([SPLIT], items, today=TODAY)
        assert [d.code for d in report.dropped] == ["not_large"]
        _, report = verify_plan([SPLIT], items, today=TODAY, pinned=[items[5].anchor], allow_split=True)
        assert [d.code for d in report.dropped] == ["pinned"]

    def test_two_splits_cant_take_one_name_or_one_item(self):
        twin = PlannedChange(type="split", ids=(1, 2, 3, 4, 10), new_slug="sis-conventions", description="again")
        merge = PlannedChange(type="merge", ids=(5, 6), text="x", why="")
        items = _items(CANVAS + SIS + ["One more Canvas note about course shells."])
        _, report = verify_plan([SPLIT, twin, merge], items, today=TODAY, allow_split=True)
        assert [d.code for d in report.dropped] == ["name_taken", "overlap"]


# ── applying ────────────────────────────────────────────────────────────


class TestApplySplit:
    def _op(self, items):
        [op], _ = verify_plan([SPLIT], items, today=TODAY, allow_split=True)
        return op

    def test_moved_items_leave_unchanged_and_a_pointer_takes_their_place(self):
        items = _items(SIS[:2] + CANVAS + SIS[2:])
        change = PlannedChange(type="split", ids=(1, 2, 7, 8, 9), new_slug="sis", description="SIS rules")
        [op], _ = verify_plan([change], items, today=TODAY, allow_split=True)
        result = apply_ops(items, [op])
        assert [i.text for i in result.items] == [split_pointer("sis", "SIS rules"), *CANVAS]
        assert result.items[0].anchor is None
        [new] = result.new_files
        assert (new.slug, new.description) == ("sis", "SIS rules")
        assert [(i.text, i.anchor) for i in new.items] == [(i.text, i.anchor) for i in items if i.text in SIS]
        assert result.archive == {} and result.moved == {i.anchor for i in new.items}

    def test_a_split_whose_name_is_taken_or_whose_items_changed_is_skipped(self):
        items = _items(CANVAS + SIS)
        op = self._op(items)
        assert apply_ops(items, [op], taken=["SIS-Conventions"]).skipped == [0]
        edited = [*items[:5], Item(text="edited", anchor=items[5].anchor), *items[6:]]
        assert apply_ops(edited, [op]).skipped == [0]
        assert apply_ops(items, [op], pinned=[items[7].anchor]).skipped == [0]


# ── the planner's prompt ────────────────────────────────────────────────


class _Client:
    """Answers each Converse call in turn; an Exception in ``answers`` is raised instead."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        name = kwargs["toolConfig"]["tools"][0]["toolSpec"]["name"]
        return {
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "output": {"message": {"content": [{"toolUse": {"name": name, "input": answer}}]}},
        }


class TestPlannerOffersSplits:
    def test_a_file_under_the_threshold_gets_exactly_the_2_6a_call(self):
        client = _Client({"changes": []})
        Planner("m", client=client).plan("canvas", "", _items(CANVAS), today=TODAY)
        [call] = client.calls
        assert call["system"] == [{"text": planner_module.SYSTEM_PROMPT}]
        assert call["toolConfig"]["tools"] == [planner_module.TOOL_SPEC]
        assert "Size:" not in call["messages"][0]["content"][0]["text"]

    def test_a_large_file_gets_the_same_first_call_and_a_split_call(self):
        splits = {"splits": [{"items": [5, 6, 7, 8, 9], "newSlug": "sis", "description": "SIS", "why": "w"}]}
        client = _Client({"changes": []}, splits)
        plan = Planner("m", client=client).plan(
            "canvas", "", _items(CANVAS + SIS), today=TODAY, size=FileSize(tokens=6_400, cap=8_000)
        )
        changes, split = client.calls
        # The change call is byte-for-byte what every other file gets.
        assert changes["system"] == [{"text": planner_module.SYSTEM_PROMPT}]
        assert changes["toolConfig"]["tools"] == [planner_module.TOOL_SPEC]
        assert "Size:" not in changes["messages"][0]["content"][0]["text"]
        assert split["system"] == [{"text": planner_module.SPLIT_PROMPT}]
        schema = split["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
        assert schema["required"] == ["splits"]
        assert schema["properties"]["splits"]["items"]["required"] == ["items", "newSlug", "description", "why"]
        assert "Size: about 6,400 tokens; the limit for one file is 8,000." in split["messages"][0]["content"][0]["text"]
        [change] = plan.changes
        assert (change.type, change.ids, change.new_slug, change.description) == ("split", (5, 6, 7, 8, 9), "sis", "SIS")
        assert (plan.input_tokens, plan.output_tokens) == (20, 10)

    def test_a_failed_split_call_keeps_the_changes(self):
        merge = {"type": "merge", "items": [1, 2], "text": "t", "why": "w"}
        client = _Client({"changes": [merge]}, RuntimeError("throttled"))
        plan = Planner("m", client=client).plan(
            "canvas", "", _items(CANVAS + SIS), today=TODAY, size=FileSize(tokens=6_400, cap=8_000)
        )
        assert [c.type for c in plan.changes] == ["merge"] and len(client.calls) == 2

    def test_the_change_message_is_unchanged(self):
        before = render_file_for_planner("canvas", "About it", _items(CANVAS), today=TODAY)
        assert before.splitlines()[:4] == ["Today is 2026-10-07.", "File: canvas", "About: About it", ""]


# ── a member's own memory: applied, and undone whole ───────────────────


@pytest.fixture()
def small_cap(monkeypatch):
    monkeypatch.setenv("MEMORY_FILE_HARD_CAP_TOKENS", CAP)


def _mine(projects, memory, team, user: User = VIEWER) -> str:
    space_id = projects.get_or_create_personal_space(team.project_id, user)
    memory.save_entry(space_id, user.user_id, user.email, "canvas", FILE, description="Canvas and SIS notes")
    return space_id


def _run_mine(maintenance, memory, projects, inbox, team, planner, user: User = VIEWER):
    queued = maintenance.start(team.project_id, user, MODEL, scope="mine")
    space_id = projects.repository.get_personal_space_id(team.project_id, user.user_id)
    return runner_for(memory, projects, inbox, planner).run(space_id, queued.run_id)


class TestPersonalSplit:
    def test_a_split_creates_the_file_moves_items_and_indexes_it(
        self, maintenance, memory, projects, inbox, team, small_cap
    ):
        space_id = _mine(projects, memory, team)
        before = memory.repository.get_provenance(space_id, "canvas")
        planner = ScriptedPlanner([SPLIT])
        run = _run_mine(maintenance, memory, projects, inbox, team, planner)

        assert planner.sizes[0] is not None and planner.sizes[0].cap == 300
        source, created = run.results
        assert (source.outcome, [op.type for op in source.ops]) == ("applied", ["split"])
        assert (created.slug, created.outcome, created.split_from, created.version) == (
            "sis-conventions", "created", "canvas", 1,
        )
        assert _texts(memory, space_id, "canvas", VIEWER) == [*CANVAS, POINTER]
        assert _texts(memory, space_id, "sis-conventions", VIEWER) == SIS

        ref, items, prov = memory.read_file_items(space_id, VIEWER.user_id, VIEWER.email, "sis-conventions")
        assert ref.description == "SIS term codes, course numbers and extracts"
        for item in items:
            # Same anchor, same history, plus where it came from.
            assert prov[item.anchor].added_at == before[item.anchor].added_at
            assert (prov[item.anchor].moved_from, prov[item.anchor].moved_by) == ("canvas", VIEWER.email)
        latest = memory.list_file_versions(space_id, VIEWER.user_id, VIEWER.email, "sis-conventions")[0]
        assert (latest.reason, latest.run_id) == ("maintenance", run.run_id)
        # Nothing moved is archived: it hasn't left the space.
        assert memory.list_archived_items(space_id, VIEWER.user_id, VIEWER.email) == []
        assert "- [[sis-conventions]] — SIS term codes, course numbers and extracts" in _index(memory, space_id)

    def test_a_file_under_the_threshold_is_not_offered_splits(self, maintenance, memory, projects, inbox, team):
        _mine(projects, memory, team)
        planner = ScriptedPlanner([SPLIT])
        run = _run_mine(maintenance, memory, projects, inbox, team, planner)
        assert planner.sizes == [None]
        assert [r.outcome for r in run.results] == ["nothing_to_do"] and run.results[0].dropped == 1

    def test_undo_puts_items_back_with_their_history_and_takes_the_file_away(
        self, maintenance, memory, projects, inbox, team, small_cap
    ):
        space_id = _mine(projects, memory, team)
        before = memory.repository.get_provenance(space_id, "canvas")
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner([SPLIT]))
        undone = maintenance.undo(team.project_id, VIEWER, run.run_id)

        assert [(r.slug, r.undo) for r in undone.results] == [("canvas", "restored"), ("sis-conventions", "removed")]
        assert _texts(memory, space_id, "canvas", VIEWER) == CANVAS + SIS
        prov = memory.repository.get_provenance(space_id, "canvas")
        for anchor, p in before.items():
            assert (p.added_at, prov[anchor].moved_from, prov[anchor].restored_by) == (prov[anchor].added_at, None, None)
        assert all(e.slug != "sis-conventions" for e in memory.repository.get_index(space_id).entries)
        assert memory.repository.list_file_versions(space_id, "sis-conventions") == []
        assert "sis-conventions" not in _index(memory, space_id)
        # The pointer leaves without an archive row: it said nothing that is lost.
        assert memory.list_archived_items(space_id, VIEWER.user_id, VIEWER.email) == []

    def test_an_edit_to_the_new_file_leaves_both_files_alone(
        self, maintenance, memory, projects, inbox, team, small_cap
    ):
        space_id = _mine(projects, memory, team)
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner([SPLIT]))
        memory.save_entry(space_id, VIEWER.user_id, VIEWER.email, "sis-conventions", "- Only this now.\n")
        undone = maintenance.undo(team.project_id, VIEWER, run.run_id)
        assert [(r.slug, r.undo) for r in undone.results] == [("canvas", "changed"), ("sis-conventions", "changed")]
        assert _texts(memory, space_id, "canvas", VIEWER) == [*CANVAS, POINTER]

    def test_a_failed_save_of_the_file_rolls_back_the_new_file(self, memory, projects, team, small_cap, monkeypatch):
        space_id = _mine(projects, memory, team)
        ref = next(e for e in memory.repository.get_index(space_id).entries if e.slug == "canvas")
        items = memory._current_file(ref, "canvas").items
        [op], _ = verify_plan([SPLIT], items, today=TODAY, allow_split=True)
        result = apply_ops(items, [op])
        real = memory._save

        def save(space, user_id, user_email, slug, *args, **kwargs):
            if slug == "canvas":
                raise MemorySpaceConcurrencyError("moved")
            return real(space, user_id, user_email, slug, *args, **kwargs)

        monkeypatch.setattr(memory, "_save", save)
        with pytest.raises(MemorySpaceConcurrencyError):
            memory.apply_maintenance(
                space_id, user_id=VIEWER.user_id, user_email=VIEWER.email, slug="canvas", result=result,
                run_id="r", base_content_hash=ref.content_hash, index_budget=1_000,
            )
        assert [e.slug for e in memory.repository.get_index(space_id).entries] == ["canvas"]
        assert memory.repository.list_file_versions(space_id, "sis-conventions") == []

    def test_two_files_in_one_run_cant_split_into_one_name(
        self, maintenance, memory, projects, inbox, team, small_cap
    ):
        space_id = _mine(projects, memory, team)
        memory.save_entry(space_id, VIEWER.user_id, VIEWER.email, "canvas-copy", FILE)
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner([SPLIT]))
        outcomes = sorted((r.slug, r.outcome) for r in run.results)
        assert outcomes == [("canvas", "applied"), ("canvas-copy", "nothing_to_do"), ("sis-conventions", "created")]


# ── project memory: proposed, and created on approval ──────────────────


class TestSharedSplit:
    def _propose(self, maintenance, memory, projects, inbox, team):
        memory.save_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "canvas", FILE)
        queued = maintenance.start(team.project_id, EDITOR, MODEL)
        run = runner_for(memory, projects, inbox, ScriptedPlanner([SPLIT])).run(team.shared_space_id, queued.run_id)
        [result] = run.results
        assert result.outcome == "proposed"
        return memory.repository.get_proposal(team.shared_space_id, result.proposal_id)

    def test_a_split_is_proposed_and_nothing_changes_until_approved(
        self, maintenance, memory, projects, inbox, team, small_cap
    ):
        proposal = self._propose(maintenance, memory, projects, inbox, team)
        [op] = proposal.ops
        assert (op.type, op.new_slug, op.text) == ("split", "sis-conventions", POINTER)
        assert POINTER in proposal.text
        assert [e.slug for e in memory.repository.get_index(team.shared_space_id).entries] == ["canvas"]

    def test_approval_creates_the_file_and_records_it(
        self, maintenance, memory, projects, inbox, team, proposals, small_cap
    ):
        proposal = self._propose(maintenance, memory, projects, inbox, team)
        decided, saved = proposals.approve(team.project_id, OWNER, proposal.proposal_id)
        assert decided.created_files == ["sis-conventions"] and [r.slug for r in saved.created] == ["sis-conventions"]
        assert _texts(memory, team.shared_space_id, "sis-conventions") == SIS
        assert _texts(memory, team.shared_space_id, "canvas") == [*CANVAS, POINTER]
        _, _, prov = memory.read_file_items(team.shared_space_id, OWNER.user_id, OWNER.email, "sis-conventions")
        assert {p.moved_by for p in prov.values()} == {OWNER.email}
        assert "[[sis-conventions]]" in _index(memory, team.shared_space_id)
        [record] = [r for r in proposals.audit.records if r["after"].get("kind") == "compaction"]
        assert record["after"]["createdFiles"] == ["sis-conventions"]

    def test_a_name_taken_since_skips_the_split(self, maintenance, memory, projects, inbox, team, proposals, small_cap):
        proposal = self._propose(maintenance, memory, projects, inbox, team)
        memory.save_entry(team.shared_space_id, EDITOR.user_id, EDITOR.email, "sis", "- x\n", aliases=["sis-conventions"])
        with pytest.raises(MemorySpaceConcurrencyError, match="none of these changes"):
            proposals.approve(team.project_id, OWNER, proposal.proposal_id)


# ── supersede markers ───────────────────────────────────────────────────


class TestSupersedeMarkers:
    def test_the_newer_item_reads_what_it_replaced_through_the_file_route(
        self, maintenance, memory, projects, inbox, team, monkeypatch
    ):
        space_id = _mine(projects, memory, team)
        replace = PlannedChange(type="supersede", ids=(2, 4), why="The token moved to the service account")
        run = _run_mine(maintenance, memory, projects, inbox, team, ScriptedPlanner([replace]))
        assert run.results[0].outcome == "applied"

        from apis.app_api.projects import memory_routes, routes as project_routes
        from apis.shared.auth.dependencies import get_current_user_from_session

        monkeypatch.setattr(project_routes, "_service", projects)
        monkeypatch.setattr(memory_routes, "_files", lambda: ProjectMemoryFiles(repository=projects.repository, memory=memory))
        monkeypatch.setattr(memory_routes, "display_names", lambda emails: {})
        app = FastAPI()
        app.include_router(memory_routes.files_router)
        app.dependency_overrides[get_current_user_from_session] = lambda: VIEWER
        body = TestClient(app).get(f"/projects/{team.project_id}/memory/files/canvas?scope=mine").json()

        marked = {i["text"]: i["replaces"] for i in body["items"] if i["replaces"]}
        [replaced] = marked[CANVAS[3]]
        assert (replaced["text"], replaced["reason"]) == (CANVAS[1], "superseded")
        assert replaced["archiveId"] and replaced["restorableUntil"]
        assert all(not i["replaces"] for i in body["items"] if i["text"] != CANVAS[3])
        # Restoring it from the archive ends the marker.
        memory.restore_archived_item(space_id, VIEWER.user_id, VIEWER.email, replaced["archiveId"])
        assert memory.replaced_items(space_id, VIEWER.user_id, VIEWER.email, "canvas") == {}

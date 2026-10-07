"""A project harness's memory on the Runtime (Shared Projects 2.4b).

Real ``MemorySpaceService`` and ``ProjectService`` over moto, as in the 2.4a
suite, so the turn-path load, the lean index read and the scope-addressed tools
all run through the same project-aware permissions app-api uses. Only the
harness Agent is faked.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.builtin_tools.memory_spaces.project_tools import (
    ProjectMemoryScopes,
    make_project_memory_tools,
)
from apis.inference_api.chat.project_memory import (
    ProjectMemoryTurn,
    await_project_memory,
    build_project_memory_tools,
    load_project_memory,
)
from apis.inference_api.chat.routes import _project_turn_gate
from apis.shared.memory.service import MemorySpaceNotFoundError
from apis.shared.projects.repository import ProjectRepository
from tests.shared.test_project_memory_spaces import (  # noqa: F401 (fixtures)
    EDITOR,
    OWNER,
    STRANGER,
    VIEWER,
    _team,
    env,
    gateway,
    memory,
    projects,
)

ITEMS = "- Batch enrollment calls in groups of 50.\n"


@pytest.fixture()
def runtime(memory, monkeypatch):
    """Make the Runtime's default-constructed services use the moto-backed ones."""
    monkeypatch.setenv("MEMORY_SPACES_ENABLED", "true")
    monkeypatch.setattr("apis.shared.memory.service.MemorySpaceService", lambda: memory)
    monkeypatch.setattr("agents.builtin_tools.memory_spaces.project_tools.MemorySpaceService", lambda: memory)
    return memory


def _tools(project, user, personal=None):
    tools = make_project_memory_tools(
        ProjectMemoryScopes.for_member(project.project_id, project.shared_space_id, personal, user)
    )
    return {t.tool_spec["name"]: t for t in tools}


def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return asyncio.run(fn(**kwargs))


def _text(result) -> str:
    return result["content"][0]["text"]


def _load(project, user) -> ProjectMemoryTurn:
    return asyncio.run(load_project_memory(project.project_id, project.shared_space_id, user.user_id))


# ── the turn-path load ──────────────────────────────────────────────────


def test_a_new_project_injects_nothing(projects, runtime):
    """A fresh space's index is ``# Memory``; nothing but a heading costs no tokens."""
    project = _team(projects)
    turn = _load(project, EDITOR)
    assert turn.memory_context == ""
    assert (turn.shared_space_id, turn.personal_space_id) == (project.shared_space_id, None)
    assert turn.binding_key() == {
        "projectId": project.project_id,
        "sharedSpaceId": project.shared_space_id,
        "personalSpaceId": None,
    }


def test_both_blocks_render_by_scope_without_anchors(projects, runtime):
    project = _team(projects)
    runtime.update_index(
        project.shared_space_id, OWNER.user_id, OWNER.email,
        "# Memory\n\n- Canvas sync notes <!-- e:abcdefgh -->\n",
    )
    personal = projects.get_or_create_personal_space(project.project_id, EDITOR)
    runtime.update_index(personal, EDITOR.user_id, EDITOR.email, "# Memory\n\n- My drafts <!-- e:hjkmnpqr -->\n")
    asyncio.run(projects.update_project(project.project_id, OWNER, name="Renamed"))

    turn = _load(project, EDITOR)
    ctx = turn.memory_context
    assert turn.personal_space_id == personal
    assert ctx.index('scope="project"') < ctx.index('scope="mine"')
    assert "- Canvas sync notes\n" in ctx and "- My drafts\n" in ctx
    assert "<!-- e:" not in ctx
    # Labelled by scope: a personal space keeps its creation-time name on rename.
    assert "name=" not in ctx and "Enrollment Sync" not in ctx
    # Another member sees the project block only.
    other = _load(project, VIEWER).memory_context
    assert 'scope="project"' in other and 'scope="mine"' not in other and "My drafts" not in other


def test_a_project_without_a_shared_space_gets_no_project_block(projects, runtime):
    """Made before 2.4a and never opened: the Runtime does not backfill."""
    project = _team(projects)
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, "# Memory\n\n- Shared fact\n")
    turn = asyncio.run(load_project_memory(project.project_id, None, EDITOR.user_id))
    assert turn.memory_context == "" and turn.shared_space_id is None
    assert ProjectRepository().get_project(project.project_id).shared_space_id == project.shared_space_id


def test_a_pointer_to_another_projects_space_reads_as_missing(projects, runtime):
    project = _team(projects)
    other = _team(projects, "Other")
    runtime.update_index(other.shared_space_id, OWNER.user_id, OWNER.email, "# Memory\n\n- Other's fact\n")
    turn = asyncio.run(load_project_memory(project.project_id, other.shared_space_id, EDITOR.user_id))
    assert turn.memory_context == ""
    with pytest.raises(MemorySpaceNotFoundError):
        runtime.read_project_space_index(
            other.shared_space_id, project_id=project.project_id, scope="shared", user_id=EDITOR.user_id
        )


def test_someone_elses_personal_space_reads_as_missing(projects, runtime):
    project = _team(projects)
    personal = projects.get_or_create_personal_space(project.project_id, EDITOR)
    with pytest.raises(MemorySpaceNotFoundError):
        runtime.read_project_space_index(
            personal, project_id=project.project_id, scope="personal_in_project", user_id=VIEWER.user_id
        )


def test_a_failed_read_costs_the_block_not_the_turn(projects, runtime, monkeypatch, caplog):
    project = _team(projects)

    def boom(*args, **kwargs):
        raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(runtime, "read_project_space_index", boom)
    turn = _load(project, EDITOR)
    assert turn.memory_context == ""

    async def awaited():
        return await await_project_memory(asyncio.create_task(load_project_memory(
            project.project_id, project.shared_space_id, EDITOR.user_id
        )))

    with caplog.at_level("INFO"):
        asyncio.run(awaited())
    assert any("project_memory project=" in r.message and "waitedMs=" in r.message for r in caplog.records)


def test_budgets_are_per_scope(projects, runtime):
    project = _team(projects)
    long_index = "# Memory\n\n" + "".join(f"- Fact number {i} about the enrollment sync.\n" for i in range(400))
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, long_index)
    ctx = _load(project, EDITOR).memory_context
    assert "[truncated" in ctx
    assert len(ctx) < 2_000 * 4 + 600  # budget in chars plus the wrapper


# ── the gate ────────────────────────────────────────────────────────────


def test_the_gate_hands_back_the_project_it_read(projects):
    project = _team(projects)
    refusal, gated = asyncio.run(_project_turn_gate(project.project_id))
    assert refusal is None and gated.shared_space_id == project.shared_space_id


# ── the tools ───────────────────────────────────────────────────────────


def test_the_tools_close_over_a_copy_of_the_member_without_the_token(projects):
    project = _team(projects)
    from dataclasses import replace

    with_token = replace(EDITOR, raw_token="secret-token")
    turn = ProjectMemoryTurn(project.project_id, project.shared_space_id, None)
    tools = build_project_memory_tools(turn, with_token)
    assert [t.tool_spec["name"] for t in tools] == [
        "memory_list", "memory_read", "memory_query", "memory_save", "memory_propose",
    ]
    scopes = ProjectMemoryScopes.for_member(project.project_id, None, None, with_token)
    assert scopes.user.raw_token is None and scopes.user.user_id == EDITOR.user_id


def test_tools_are_built_once_per_member_and_spaces(projects):
    """Building costs ~1.2 ms on the path to the first token, so later turns reuse them."""
    project = _team(projects)
    turn = ProjectMemoryTurn(project.project_id, project.shared_space_id, None)
    first = build_project_memory_tools(turn, EDITOR)
    assert [id(t) for t in build_project_memory_tools(turn, EDITOR)] == [id(t) for t in first]
    for other_turn, user in (
        (ProjectMemoryTurn(project.project_id, project.shared_space_id, "spc_mine"), EDITOR),
        (turn, VIEWER),
    ):
        assert not set(map(id, build_project_memory_tools(other_turn, user))) & set(map(id, first))


def test_an_editor_saves_to_the_project_and_everyone_reads_it(projects, runtime):
    project = _team(projects)
    result = _call(_tools(project, EDITOR)["memory_save"], scope="project", slug="enrollment", text=ITEMS)
    assert result["status"] == "success", result
    assert 'Saved "enrollment" to project memory (version 1' in _text(result)
    assert 'Added "- [[enrollment]]" to the MEMORY.md index' in _text(result)

    viewer = _tools(project, VIEWER)
    listed = _call(viewer["memory_list"], scope="project")["content"][0]["json"]["files"]
    assert [f["slug"] for f in listed] == ["enrollment"] and "tokens" in listed[0]
    body = _text(_call(viewer["memory_read"], scope="project", slug="enrollment"))
    assert "Batch enrollment calls" in body and "<!-- e:" in body  # memory_read keeps anchors
    history = runtime.list_file_versions(project.shared_space_id, EDITOR.user_id, EDITOR.email, "enrollment")
    assert history[0].reason == "save" and history[0].updated_by == EDITOR.user_id


def test_a_viewer_is_refused_the_project_and_pointed_at_mine(projects, runtime):
    project = _team(projects)
    result = _call(_tools(project, VIEWER)["memory_save"], scope="project", slug="notes", text=ITEMS)
    assert result["status"] == "error"
    assert "Only project editors" in _text(result) and '"mine"' in _text(result)


def test_the_first_save_to_mine_creates_the_space_once(projects, runtime):
    project = _team(projects)
    tools = _tools(project, VIEWER)
    assert _call(tools["memory_list"], scope="mine")["content"][0]["json"]["files"] == []

    first = _call(tools["memory_save"], scope="mine", slug="drafts", text=ITEMS)
    assert first["status"] == "success", first
    space_id = ProjectRepository().get_personal_space_id(project.project_id, VIEWER.user_id)
    assert space_id and runtime.repository.get_space(space_id).scope == "personal_in_project"

    second = _call(tools["memory_save"], scope="mine", slug="more", text=ITEMS)
    assert second["status"] == "success"
    assert ProjectRepository().get_personal_space_id(project.project_id, VIEWER.user_id) == space_id
    # A fresh tool set (next turn) finds it through the pointer.
    fresh = _tools(project, VIEWER)
    assert {f["slug"] for f in _call(fresh["memory_list"], scope="mine")["content"][0]["json"]["files"]} == {
        "drafts", "more"
    }
    # Nobody else can reach it through their own "mine".
    assert _call(_tools(project, EDITOR)["memory_list"], scope="mine")["content"][0]["json"]["files"] == []


def test_prose_is_rejected_with_the_item_rule(projects, runtime):
    project = _team(projects)
    result = _call(_tools(project, EDITOR)["memory_save"], scope="project", slug="notes", text="Just prose here.")
    assert result["status"] == "error" and _text(result).startswith("❌ Not saved:")


def test_an_archived_project_says_archived_not_editor_required(projects, runtime):
    project = _team(projects)
    _call(_tools(project, EDITOR)["memory_save"], scope="mine", slug="drafts", text=ITEMS)
    asyncio.run(projects.update_project(project.project_id, OWNER, status="archived"))

    for user, scope in ((EDITOR, "project"), (EDITOR, "mine"), (VIEWER, "mine")):
        result = _call(_tools(project, user)["memory_save"], scope=scope, slug="late", text=ITEMS)
        assert result["status"] == "error"
        assert "archived" in _text(result) and "editor" not in _text(result), (user, scope, result)
    # Reading still works.
    assert _call(_tools(project, VIEWER)["memory_list"], scope="project")["status"] == "success"


def test_a_removed_member_loses_access_mid_session(projects, runtime):
    project = _team(projects)
    tools = _tools(project, EDITOR)
    projects.remove_member(project.project_id, OWNER, EDITOR.email)
    for name, kwargs in (
        ("memory_list", {"scope": "project"}),
        ("memory_read", {"scope": "project", "slug": "MEMORY.md"}),
        ("memory_save", {"scope": "project", "slug": "x", "text": ITEMS}),
    ):
        result = _call(tools[name], **kwargs)
        assert result["status"] == "error" and "no longer a member" in _text(result), (name, result)


def test_the_index_reads_and_writes_by_scope(projects, runtime):
    project = _team(projects)
    tools = _tools(project, EDITOR)
    _call(tools["memory_save"], scope="project", slug="enrollment", text=ITEMS)
    saved = _call(tools["memory_save"], scope="project", slug="MEMORY.md", text="# Memory\n\n- [[enrollment]] — batching\n")
    assert saved["status"] == "success"
    assert "[[enrollment]]" in _text(_call(tools["memory_read"], scope="project", slug="MEMORY.md"))
    assert "[[enrollment]]" in _load(project, VIEWER).memory_context


def test_query_filters_by_text_and_date(projects, runtime):
    project = _team(projects)
    tools = _tools(project, EDITOR)
    _call(tools["memory_save"], scope="project", slug="enrollment", text=ITEMS, description="Canvas batching")
    _call(tools["memory_save"], scope="project", slug="vendors", text="- Vendor A is preferred.\n")

    def slugs(where):
        result = _call(tools["memory_query"], scope="project", where=where)
        assert result["status"] == "success", result
        return [f["slug"] for f in result["content"][0]["json"]["files"]]

    assert slugs({"text": "CANVAS"}) == ["enrollment"]
    assert slugs({"text": "vendor"}) == ["vendors"]
    assert sorted(slugs({"updated_after": "2000-01-01"})) == ["enrollment", "vendors"]
    assert slugs({"updated_after": "2999-01-01"}) == []
    bad = _call(tools["memory_query"], scope="project", where={"owner": "x"})
    assert bad["status"] == "error" and "Unknown filter owner" in _text(bad)


def test_unknown_scopes_and_missing_files_are_clear(projects, runtime):
    project = _team(projects)
    tools = _tools(project, EDITOR)
    assert 'Unknown scope "team"' in _text(_call(tools["memory_list"], scope="team"))
    assert "no file 'ghost'" in _text(_call(tools["memory_read"], scope="project", slug="ghost"))
    assert "not saved anything" in _text(_call(tools["memory_read"], scope="mine", slug="x"))


def test_a_project_without_a_shared_space_points_saves_at_mine(projects, runtime):
    project = _team(projects)
    tools = make_project_memory_tools(ProjectMemoryScopes.for_member(project.project_id, None, None, EDITOR))
    save = {t.tool_spec["name"]: t for t in tools}["memory_save"]
    result = _call(save, scope="project", slug="x", text=ITEMS)
    assert result["status"] == "error" and "no shared memory yet" in _text(result) and '"mine"' in _text(result)


def test_strangers_never_reach_a_project_through_the_tools(projects, runtime):
    project = _team(projects)
    tools = _tools(project, STRANGER)
    assert _call(tools["memory_list"], scope="project")["status"] == "error"
    result = _call(tools["memory_save"], scope="mine", slug="x", text=ITEMS)
    assert result["status"] == "error" and "no longer a member" in _text(result)
    assert ProjectRepository().get_personal_space_id(project.project_id, STRANGER.user_id) is None


# ── a new file indexes itself (2026-10 team simulation, G2) ─────────────


def _index(runtime, space_id, user=OWNER) -> str:
    return runtime.read_index(space_id, user.user_id, user.email)


def test_a_new_file_is_indexed_and_teammates_see_it(projects, runtime):
    """The simulation's failure: an editor's save never reached a teammate's task."""
    project = _team(projects)
    result = _call(
        _tools(project, EDITOR)["memory_save"],
        scope="project", slug="escalations", text=ITEMS, description="Open escalations and owners",
    )
    assert result["status"] == "success", result
    assert 'Added "- [[escalations]] — Open escalations and owners" to the MEMORY.md index' in _text(result)
    # The blank starter's placeholder prose goes; the heading stays.
    assert _index(runtime, project.shared_space_id) == "# Memory\n\n- [[escalations]] — Open escalations and owners\n"
    assert "- [[escalations]] — Open escalations and owners" in _load(project, VIEWER).memory_context


def test_lines_append_in_order_and_keep_the_members_own_text(projects, runtime):
    project = _team(projects)
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, "# Team memory\n\nRead this first.\n")
    save = _tools(project, EDITOR)["memory_save"]
    _call(save, scope="project", slug="vendors", text=ITEMS, description="Vendor decisions")
    _call(save, scope="project", slug="budget", text=ITEMS)
    assert _index(runtime, project.shared_space_id) == (
        "# Team memory\n\nRead this first.\n\n- [[vendors]] — Vendor decisions\n- [[budget]]\n"
    )


def test_an_undescribed_file_asks_for_a_better_line(projects, runtime):
    project = _team(projects)
    result = _call(_tools(project, EDITOR)["memory_save"], scope="project", slug="budget", text=ITEMS)
    assert 'Added "- [[budget]]"' in _text(result) and "Replace that line" in _text(result)


def test_an_update_never_touches_the_index(projects, runtime):
    """A member who took a file out of the index meant it."""
    project = _team(projects)
    save = _tools(project, EDITOR)["memory_save"]
    _call(save, scope="project", slug="vendors", text=ITEMS, description="Vendor decisions")
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, "# Memory\n")
    body = _text(_call(_tools(project, EDITOR)["memory_read"], scope="project", slug="vendors"))
    updated = _call(save, scope="project", slug="vendors", text=body.split("---\n")[-1] + "- Vendor B is backup.\n")
    assert updated["status"] == "success", updated
    assert "version 2" in _text(updated) and "MEMORY.md" not in _text(updated)
    assert _index(runtime, project.shared_space_id) == "# Memory\n"


def test_a_file_the_index_already_links_is_not_added_twice(projects, runtime):
    project = _team(projects)
    save = _tools(project, EDITOR)["memory_save"]
    _call(save, scope="project", slug="vendors", text=ITEMS, description="Vendor decisions")
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, "# Memory\n\n- [[Vendors]] — mine\n")
    runtime.delete_entry(project.shared_space_id, OWNER.user_id, OWNER.email, "vendors")
    again = _call(save, scope="project", slug="vendors", text=ITEMS, description="Vendor decisions")
    assert again["status"] == "success" and "version 1" in _text(again), again
    assert "MEMORY.md" not in _text(again)
    assert _index(runtime, project.shared_space_id) == "# Memory\n\n- [[Vendors]] — mine\n"


def test_a_full_index_is_left_alone_and_the_model_is_told(projects, runtime):
    """Past the 2k budget the line would be truncated out of every task anyway."""
    from apis.shared.memory.hydration import PROJECT_MEMORY_MAX_TOKENS

    project = _team(projects)
    full = "# Memory\n\n" + "x" * (PROJECT_MEMORY_MAX_TOKENS * 4 - len("# Memory\n\n") - 4) + "\n"
    runtime.update_index(project.shared_space_id, OWNER.user_id, OWNER.email, full)
    result = _call(
        _tools(project, EDITOR)["memory_save"], scope="project", slug="vendors", text=ITEMS, description="Vendors",
    )
    assert result["status"] == "success"
    assert "2,000-token limit, so it was not indexed" in _text(result) and "[[vendors]]" in _text(result)
    assert _index(runtime, project.shared_space_id) == full


def test_a_personal_preference_reaches_the_members_next_task(projects, runtime):
    """G17: a "just for me" preference saved in one task applies in the next."""
    project = _team(projects)
    result = _call(
        _tools(project, VIEWER)["memory_save"],
        scope="mine", slug="preferences", text="- Summaries as bullet lists.\n", description="How I like answers",
    )
    assert result["status"] == "success", result
    assert "your memory in this project" in _text(result) and "Added" in _text(result)
    turn = _load(project, VIEWER)
    assert 'scope="mine"' in turn.memory_context and "[[preferences]] — How I like answers" in turn.memory_context
    assert "preferences" not in _load(project, EDITOR).memory_context


def test_concurrent_new_files_keep_both_lines(projects, runtime, monkeypatch):
    """Two members creating files at once: the index write is conditional, so the loser retries."""
    from apis.shared.memory.models import MemoryEntryRef

    project = _team(projects)
    repo = runtime.repository
    real = repo.put_space_if_index_unchanged
    raced = []

    def racing(space, expected):
        if not raced:
            raced.append(True)
            other = MemoryEntryRef(slug="budget", description="Budget", content_hash="h", size=1, s3_key="k", updated="")
            runtime.add_index_link(space.space_id, OWNER.user_id, OWNER.email, other, max_tokens=2000)
        return real(space, expected)

    monkeypatch.setattr(repo, "put_space_if_index_unchanged", racing)
    _call(_tools(project, EDITOR)["memory_save"], scope="project", slug="vendors", text=ITEMS, description="Vendors")
    assert _index(runtime, project.shared_space_id) == "# Memory\n\n- [[budget]] — Budget\n- [[vendors]] — Vendors\n"


def test_a_failed_index_write_keeps_the_save_and_falls_back_to_the_nudge(projects, runtime, monkeypatch):
    project = _team(projects)

    def broken(*args, **kwargs):
        raise RuntimeError("S3 is down")

    monkeypatch.setattr(runtime, "add_index_link", broken)
    result = _call(_tools(project, EDITOR)["memory_save"], scope="project", slug="vendors", text=ITEMS)
    assert result["status"] == "success"
    assert 'add a line like "- [[vendors]] — what it holds" to MEMORY.md' in _text(result)
    assert [e.slug for e in runtime.list_entries(project.shared_space_id, EDITOR.user_id, EDITOR.email)] == ["vendors"]


def test_an_ordinary_agents_memory_write_never_touches_the_index(runtime, monkeypatch):
    """Only the harness indexes: an Agent-bound space's tools behave as before."""
    from agents.builtin_tools.memory_spaces.tools import make_memory_write_tool

    monkeypatch.setattr("agents.builtin_tools.memory_spaces.tools.MemorySpaceService", lambda: runtime)
    space = runtime.create_space(OWNER.user_id, OWNER.email, "Brain")
    before = _index(runtime, space.space_id)
    tool = make_memory_write_tool(space.space_id, "Brain", OWNER.user_id, OWNER.email)
    result = _call(tool, slug="jane", body="Jane is the CFO.", description="person")
    assert result["status"] == "success"
    assert _index(runtime, space.space_id) == before

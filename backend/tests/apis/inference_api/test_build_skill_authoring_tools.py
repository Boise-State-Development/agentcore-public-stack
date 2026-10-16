"""The _build_skill_authoring_tools extra_tools seam (docs/specs/skill-creator.md, Gating).

Three gates, all required: the in-development ``SKILL_AUTHORING_ENABLED`` (only
``"true"`` enables), ``SKILLS_ENABLED`` (on unless ``"false"``), and the
``my_skill_create`` id in the turn's tools, which provisions all seven. With any
of them off nothing is built, so no spec reaches ``toolConfig``.
"""

import pytest

from apis.inference_api.chat.routes import _build_skill_authoring_tools

SEVEN = [
    "my_skill_list",
    "my_skill_read",
    "my_skill_read_file",
    "my_skill_create",
    "my_skill_update",
    "my_skill_write_file",
    "my_skill_delete_file",
]


def _names(enabled_tools):
    return [t.tool_name for t in _build_skill_authoring_tools(enabled_tools, "s1", "u1", "u1@x.edu")]


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("SKILL_AUTHORING_ENABLED", "true")
    monkeypatch.delenv("SKILLS_ENABLED", raising=False)


def test_one_id_provisions_all_seven(on):
    assert _names(["web_search", "my_skill_create"]) == SEVEN


@pytest.mark.parametrize("enabled_tools", [None, [], ["web_search"], ["my_skill_update"]])
def test_without_the_gate_id_nothing_is_built(on, enabled_tools):
    # Only the catalog id gates; naming a tool it provisions does not.
    assert _names(enabled_tools) == []


@pytest.mark.parametrize("value", [None, "", "false", "yes", "1"])
def test_the_in_development_flag_is_opt_in(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SKILL_AUTHORING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SKILL_AUTHORING_ENABLED", value)
    assert _names(["my_skill_create"]) == []


def test_the_skills_kill_switch_also_removes_them(on, monkeypatch):
    monkeypatch.setenv("SKILLS_ENABLED", "false")
    assert _names(["my_skill_create"]) == []


def test_the_tools_act_as_the_closed_over_user(on, monkeypatch):
    """The closure is (session, user, email) only: what the cache key describes."""
    from agents.builtin_tools.skill_authoring import tools as skill_tools

    seen = []

    class _Service:
        async def list_my_skills(self, user):
            seen.append(user.user_id)
            return []

    monkeypatch.setattr(skill_tools, "get_user_skill_service", lambda: _Service())
    [list_tool] = [t for t in _build_skill_authoring_tools(["my_skill_create"], "s1", "u1", None) if t.tool_name == "my_skill_list"]
    list_tool()
    assert seen == ["u1"]

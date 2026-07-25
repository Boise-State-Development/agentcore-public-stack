"""Agent Marketplace Phase 3 — the capability projection and the D6 diff.

Two properties are under test, and both are the kind that quietly rot:

* **Capabilities carry names, never refs.** The detail page is seen by anyone who can
  reach the Agent, so a tool id or skill id appearing in that list is a leak of exactly
  the sort ``AgentListingResponse`` was narrowed to prevent.
* **Runnability composes ``list_bindable`` and nothing else.** Every assertion here goes
  through the real ``resolve_runnability`` with a stubbed catalog, so a future change
  that grows a sixth access service instead of composing the five fails loudly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.agent_designer.services.agent_detail import (
    resolve_capabilities,
    resolve_listing_display,
    resolve_runnability,
)
from apis.shared.assistants.models import (
    AgentBinding,
    AgentModelConfig,
    Assistant,
    BindableItem,
)
from apis.shared.auth.models import User

MODULE = "apis.app_api.agent_designer.services.agent_detail"


def _agent(**overrides) -> Assistant:
    defaults = dict(
        assistantId="ast-001",
        ownerId="user-author",
        ownerName="Ada Author",
        name="Policy Lookup",
        description="Finds policy",
        instructions="SECRET SYSTEM PROMPT",
        vectorIndexId="idx-001",
        visibility="PUBLIC",
        usageCount=3,
        createdAt="2026-07-01T00:00:00Z",
        updatedAt="2026-07-01T00:00:00Z",
        status="COMPLETE",
    )
    defaults.update(overrides)
    return Assistant.model_validate(defaults)


def _user(user_id="user-viewer") -> User:
    return User(user_id=user_id, email=f"{user_id}@x.edu", name="A Viewer", roles=[])


class _Tools:
    """Stands in for ToolCatalogService's unfiltered ``get_tool`` lookup."""

    def __init__(self, labels: dict):
        self._labels = labels

    async def get_tool(self, tool_id):
        label = self._labels.get(tool_id)
        return SimpleNamespace(tool_id=tool_id, display_name=label) if label else None


def _catalog(**by_kind):
    """A fake ``list_bindable`` returning the viewer's catalog per kind."""

    async def _list_bindable(kind, user, **_kwargs):
        return [
            BindableItem(kind=kind, ref=ref, label=label)
            for ref, label in by_kind.get(kind, {}).items()
        ]

    return _list_bindable


def _skills(labels: dict):
    repo = SimpleNamespace(
        batch_get_skills=AsyncMock(
            return_value=[
                SimpleNamespace(skill_id=ref, display_name=label) for ref, label in labels.items()
            ]
        )
    )
    return patch(f"{MODULE}.get_skill_catalog_repository", return_value=repo)


def _models(*pairs):
    return patch(
        f"{MODULE}.list_all_managed_models",
        new_callable=AsyncMock,
        return_value=[SimpleNamespace(model_id=mid, model_name=name) for mid, name in pairs],
    )


# ── capabilities: names, not refs ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_capabilities_carry_display_names_never_refs():
    agent = _agent(
        bindings=[
            AgentBinding(kind="tool", ref="document_search"),
            AgentBinding(kind="skill", ref="skl_9f2c"),
        ],
        model_settings=AgentModelConfig(model_id="us.anthropic.claude-sonnet"),
    )
    with _skills({"skl_9f2c": "Policy Citation Format"}), _models(
        ("us.anthropic.claude-sonnet", "Claude Sonnet 4.5")
    ):
        capabilities, model_label = await resolve_capabilities(
            agent, _user(), tool_service=_Tools({"document_search": "Document Search"})
        )

    assert [(c.label, c.kind) for c in capabilities] == [
        ("Document Search", "tool"),
        ("Policy Citation Format", "skill"),
    ]
    assert model_label == "Claude Sonnet 4.5"

    # The refs themselves must appear nowhere in the projection.
    rendered = [c.model_dump() for c in capabilities]
    assert not any("document_search" in str(row) or "skl_9f2c" in str(row) for row in rendered)


@pytest.mark.asyncio
async def test_capabilities_omit_the_synthesized_knowledge_base():
    """A legacy Agent's KB binding is welded plumbing, not a capability to advertise."""
    with _models():
        capabilities, model_label = await resolve_capabilities(_agent(), _user())

    assert capabilities == []
    assert model_label is None


@pytest.mark.asyncio
async def test_an_unresolvable_ref_falls_back_to_a_kind_label_not_the_ref():
    """A deleted tool must not leak its id into the page as a 'name'."""
    agent = _agent(bindings=[AgentBinding(kind="tool", ref="tool_that_was_deleted")])
    with _models():
        capabilities, _ = await resolve_capabilities(agent, _user(), tool_service=_Tools({}))

    assert [c.label for c in capabilities] == ["A tool"]


@pytest.mark.asyncio
async def test_scoped_tool_bindings_collapse_to_one_named_capability():
    """Two sub-tools of one MCP server are one line on the page, resolved by base id."""
    agent = _agent(
        bindings=[
            AgentBinding(kind="tool", ref="wikipedia::search"),
            AgentBinding(kind="tool", ref="wikipedia::fetch"),
        ]
    )
    with _models():
        capabilities, _ = await resolve_capabilities(
            agent, _user(), tool_service=_Tools({"wikipedia": "Wikipedia"})
        )

    assert [(c.label, c.kind) for c in capabilities] == [("Wikipedia", "tool")]


# ── listing display: ids resolved to names ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_listing_display_resolves_publisher_and_category_to_names():
    from apis.shared.assistants.models import AgentListing, AgentCategory, PublisherProfile

    agent = _agent(
        listing=AgentListing(
            state="published", category="Administration", publisher_id="pub-registrar"
        )
    )
    profile = PublisherProfile(
        id="pub-registrar", label="Office of the Registrar", kind="department", verified=True
    )
    with patch(f"{MODULE}.get_publisher", new_callable=AsyncMock, return_value=profile), patch(
        f"{MODULE}.get_category",
        new_callable=AsyncMock,
        return_value=AgentCategory(id="Administration", label="University Operations"),
    ):
        publisher, category_label = await resolve_listing_display(agent)

    assert (publisher.label, publisher.kind, publisher.verified) == (
        "Office of the Registrar",
        "department",
        True,
    )
    # The renamed label renders, not the immutable partition id.
    assert category_label == "University Operations"
    # And the publisher id never rides along — it is an internal reference.
    assert "id" not in publisher.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_listing_display_survives_a_deleted_publisher():
    from apis.shared.assistants.models import AgentListing

    agent = _agent(
        listing=AgentListing(state="published", category="Research", publisher_id="pub-gone")
    )
    with patch(f"{MODULE}.get_publisher", new_callable=AsyncMock, return_value=None), patch(
        f"{MODULE}.get_category", new_callable=AsyncMock, return_value=None
    ):
        publisher, category_label = await resolve_listing_display(agent)

    assert publisher is None
    assert category_label == "Research"  # falls back to the id, which is also the seeded label


@pytest.mark.asyncio
async def test_an_agent_that_was_never_submitted_has_no_listing_display():
    publisher, category_label = await resolve_listing_display(_agent())

    assert publisher is None
    assert category_label is None


# ── runnability: the three-way outcome (D6) ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_ready_when_the_viewer_holds_everything():
    agent = _agent(
        bindings=[AgentBinding(kind="tool", ref="document_search")],
        model_settings=AgentModelConfig(model_id="claude-sonnet"),
    )
    catalog = _catalog(
        model={"claude-sonnet": "Claude Sonnet 4.5"}, tool={"document_search": "Document Search"}
    )
    with patch(f"{MODULE}.list_bindable", side_effect=catalog), _models():
        result = await resolve_runnability(
            agent, _user(), tool_service=_Tools({"document_search": "Document Search"})
        )

    assert result.state == "ready"
    assert result.missing == []
    assert result.agent_id == "ast-001"


@pytest.mark.asyncio
async def test_blocked_when_a_required_tool_is_not_in_the_viewers_catalog():
    """Names what is missing — by label, because the SPA renders this line verbatim."""
    agent = _agent(bindings=[AgentBinding(kind="tool", ref="workday_query")])
    with patch(f"{MODULE}.list_bindable", side_effect=_catalog(tool={})), _models():
        result = await resolve_runnability(
            agent, _user(), tool_service=_Tools({"workday_query": "Workday Query"})
        )

    assert result.state == "blocked"
    assert [(m.label, m.kind, m.optional) for m in result.missing] == [
        ("Workday Query", "tool", False)
    ]


@pytest.mark.asyncio
async def test_limits_when_only_an_optional_binding_is_missing():
    """The one gap that degrades instead of blocking — see the module docstring."""
    agent = _agent(
        bindings=[
            AgentBinding(kind="tool", ref="document_search"),
            AgentBinding(kind="tool", ref="grants_gov", config={"optional": True}),
        ]
    )
    catalog = _catalog(tool={"document_search": "Document Search"})
    with patch(f"{MODULE}.list_bindable", side_effect=catalog), _models():
        result = await resolve_runnability(
            agent,
            _user(),
            tool_service=_Tools(
                {"document_search": "Document Search", "grants_gov": "Grants.gov Search"}
            ),
        )

    assert result.state == "limits"
    assert [(m.label, m.optional) for m in result.missing] == [("Grants.gov Search", True)]


@pytest.mark.asyncio
async def test_one_required_gap_outranks_any_number_of_optional_ones():
    agent = _agent(
        bindings=[
            AgentBinding(kind="tool", ref="grants_gov", config={"optional": True}),
            AgentBinding(kind="tool", ref="workday_query"),
        ]
    )
    with patch(f"{MODULE}.list_bindable", side_effect=_catalog(tool={})), _models():
        result = await resolve_runnability(
            agent,
            _user(),
            tool_service=_Tools(
                {"grants_gov": "Grants.gov Search", "workday_query": "Workday Query"}
            ),
        )

    assert result.state == "blocked"
    assert {m.label for m in result.missing} == {"Grants.gov Search", "Workday Query"}


@pytest.mark.asyncio
async def test_a_model_the_viewer_cannot_access_blocks_and_is_named():
    """The pinned model is never optional — the run-time resolver raises outright."""
    agent = _agent(model_settings=AgentModelConfig(model_id="claude-opus"))
    with patch(f"{MODULE}.list_bindable", side_effect=_catalog(model={})), _models(
        ("claude-opus", "Claude Opus 4.5")
    ):
        result = await resolve_runnability(agent, _user())

    assert result.state == "blocked"
    assert [(m.label, m.kind) for m in result.missing] == [("Claude Opus 4.5", "model")]


@pytest.mark.asyncio
async def test_a_legacy_agent_with_no_bindings_is_ready():
    """The trap: compat synthesizes a knowledge_base binding on *every* legacy row while
    ``list_bindable('knowledge_base')`` is empty by design. Diffing it would mark the
    entire back catalogue unrunnable."""
    called_kinds = []

    async def _tracking(kind, user, **_kwargs):
        called_kinds.append(kind)
        return []

    with patch(f"{MODULE}.list_bindable", side_effect=_tracking), _models():
        result = await resolve_runnability(_agent(), _user())

    assert result.state == "ready"
    assert result.missing == []
    assert "knowledge_base" not in called_kinds


@pytest.mark.asyncio
async def test_an_unknown_binding_kind_does_not_block():
    """Storage tolerates kinds newer code wrote; the run-time resolver ignores them, so
    previewing a block on one would be a preview of something that will not happen."""
    agent = _agent(bindings=[AgentBinding(kind="quantum_flux", ref="qf-1")])
    with patch(f"{MODULE}.list_bindable", side_effect=_catalog()), _models():
        result = await resolve_runnability(agent, _user())

    assert result.state == "ready"


@pytest.mark.asyncio
async def test_publisher_is_never_consulted_when_deciding_runnability():
    """D12: ``publisherId`` is display-only. Re-attributing a listing changes the name on
    the shelf and nothing about who can run it."""
    from apis.shared.assistants.models import AgentListing

    bindings = [AgentBinding(kind="tool", ref="document_search")]
    catalog = _catalog(tool={"document_search": "Document Search"})
    tools = _Tools({"document_search": "Document Search"})

    results = []
    for publisher_id in ("pub-registrar", "pub-institution"):
        agent = _agent(
            bindings=bindings,
            listing=AgentListing(
                state="published", category="Administration", publisher_id=publisher_id
            ),
        )
        with patch(f"{MODULE}.list_bindable", side_effect=catalog), _models():
            results.append(await resolve_runnability(agent, _user(), tool_service=tools))

    assert [r.state for r in results] == ["ready", "ready"]

"""Agent detail + "will it run?" on a retiring model — docs/specs/model-retirement.md.

The runtime resolves a retired model before its access check: it runs the successor,
or refuses everyone when there is none. The detail page and both runnability checks
(per viewer, and per role for pins) must give the same answer, or they say *ready*
for an Agent the runtime refuses and name a model the runtime no longer runs.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.agent_designer.services import role_pin_service
from apis.app_api.agent_designer.services.agent_detail import (
    resolve_model_retirement,
    resolve_runnability,
)
from apis.shared.assistants.models import AgentModelConfig, Assistant, BindableItem
from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.models.retirement import resolve_from_catalog

MODULE = "apis.app_api.agent_designer.services.agent_detail"
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _row(model_id: str, name: str, *, status="active", replaced_by=None, **kw) -> ManagedModel:
    return ManagedModel(
        id=f"uuid-{model_id}", modelId=model_id, modelName=name, provider="bedrock",
        providerName="Anthropic", inputModalities=["text"], outputModalities=["text"],
        maxInputTokens=200000, enabled=True, inputPricePerMillionTokens=3.0,
        outputPricePerMillionTokens=15.0, status=status, replacedBy=replaced_by,
        createdAt=NOW, updatedAt=NOW, **kw,
    )


CATALOG = [
    _row("sonnet-5", "Claude Sonnet 5"),
    _row("sonnet-4-6", "Claude Sonnet 4.6", status="retired", replaced_by="sonnet-5"),
    _row("sonnet-4", "Claude Sonnet 4", status="retired", replaced_by="sonnet-4-6"),
    _row("opus-4-1", "Claude Opus 4.1", status="retired"),
    _row("haiku-4-5", "Claude Haiku 4.5", status="deprecated", replaced_by="sonnet-5",
         retiresOn="2026-10-31", retirementNote="Sonnet 5 is faster."),
    _row("orphan", "Orphaned", status="deprecated", replaced_by="gone"),
]


def _agent(model_id: str) -> Assistant:
    return Assistant.model_validate(
        dict(
            assistantId="ast-001", ownerId="user-author", ownerName="Ada", name="Policy Lookup",
            description="d", instructions="i", vectorIndexId="idx", visibility="PUBLIC",
            createdAt="2026-07-01T00:00:00Z", updatedAt="2026-07-01T00:00:00Z", status="COMPLETE",
            model_settings=AgentModelConfig(model_id=model_id),
        )
    )


def _user() -> User:
    return User(user_id="user-viewer", email="viewer@x.edu", name="Viewer", roles=[])


async def _resolve(model_id):
    return resolve_from_catalog(model_id, CATALOG) if model_id else None


def _patched(viewer_models: set):
    async def _list_bindable(kind, user, **_kwargs):
        refs = viewer_models if kind == "model" else set()
        return [BindableItem(kind=kind, ref=ref, label=ref) for ref in refs]

    return (
        patch(f"{MODULE}.resolve_effective_model", _resolve),
        patch(f"{MODULE}.list_all_managed_models", new_callable=AsyncMock, return_value=CATALOG),
        patch(f"{MODULE}.list_bindable", side_effect=_list_bindable),
    )


async def _runnability(model_id: str, viewer_models: set):
    a, b, c = _patched(viewer_models)
    with a, b, c:
        return await resolve_runnability(_agent(model_id), _user())


class TestViewerRunnability:
    @pytest.mark.asyncio
    async def test_retired_without_successor_blocks_even_when_the_viewer_holds_it(self):
        result = await _runnability("opus-4-1", {"opus-4-1", "sonnet-5"})
        assert result.state == "blocked"
        assert [(m.label, m.kind) for m in result.missing] == [("Claude Opus 4.1 (retired)", "model")]

    @pytest.mark.asyncio
    async def test_redirect_is_ready_when_the_viewer_holds_the_successor(self):
        # Holding the retired model is irrelevant — the successor is what runs.
        result = await _runnability("sonnet-4-6", {"sonnet-5"})
        assert result.state == "ready"
        assert result.missing == []

    @pytest.mark.asyncio
    async def test_redirect_blocks_naming_the_successor_the_viewer_lacks(self):
        result = await _runnability("sonnet-4", {"sonnet-4", "sonnet-4-6"})
        assert result.state == "blocked"
        assert [m.label for m in result.missing] == ["Claude Sonnet 5"]

    @pytest.mark.asyncio
    async def test_deprecated_model_is_checked_as_itself(self):
        assert (await _runnability("haiku-4-5", {"haiku-4-5"})).state == "ready"


class TestRolePinRunnability:
    def _role(self, models):
        return SimpleNamespace(effective_permissions=SimpleNamespace(models=models, tools=[], skills=[]))

    async def _diff(self, model_id, role_models):
        with patch(f"{MODULE}.resolve_effective_model", _resolve), \
             patch(f"{MODULE}.list_all_managed_models", new_callable=AsyncMock, return_value=CATALOG), \
             patch.object(role_pin_service, "_labels_by_kind", AsyncMock(return_value={})):
            return await role_pin_service._diff_against_role(_agent(model_id), self._role(role_models), None)

    @pytest.mark.asyncio
    async def test_retired_without_successor_is_missing_even_for_a_wildcard_role(self):
        _, missing, _ = await self._diff("opus-4-1", ["*"])
        assert [m.label for m in missing] == ["Claude Opus 4.1 (retired)"]

    @pytest.mark.asyncio
    async def test_redirect_checks_the_role_against_the_successor(self):
        _, missing, _ = await self._diff("sonnet-4-6", ["sonnet-5"])
        assert missing == []
        _, missing, _ = await self._diff("sonnet-4-6", ["sonnet-4-6"])
        assert [m.label for m in missing] == ["Claude Sonnet 5"]


class TestModelRetirementDetail:
    async def _detail(self, model_id):
        with patch(f"{MODULE}.list_all_managed_models", new_callable=AsyncMock, return_value=CATALOG):
            return await resolve_model_retirement(model_id)

    @pytest.mark.asyncio
    async def test_active_or_unknown_model_has_no_retirement(self):
        assert await self._detail("sonnet-5") is None
        assert await self._detail("custom.model") is None

    @pytest.mark.asyncio
    async def test_retired_names_the_model_that_now_runs_through_a_chain(self):
        detail = await self._detail("sonnet-4")
        assert (detail.status, detail.successor_label) == ("retired", "Claude Sonnet 5")

    @pytest.mark.asyncio
    async def test_retired_without_successor_names_none(self):
        detail = await self._detail("opus-4-1")
        assert (detail.status, detail.successor_label) == ("retired", None)

    @pytest.mark.asyncio
    async def test_deprecated_carries_successor_date_and_note(self):
        detail = await self._detail("haiku-4-5")
        assert detail.model_dump(by_alias=True) == {
            "status": "deprecated",
            "successorLabel": "Claude Sonnet 5",
            "retiresOn": "2026-10-31",
            "retirementNote": "Sonnet 5 is faster.",
        }

    @pytest.mark.asyncio
    async def test_a_successor_with_no_row_is_not_named_by_its_id(self):
        # Refs are not display content: no label beats a raw model id.
        assert (await self._detail("orphan")).successor_label is None

    @pytest.mark.asyncio
    async def test_catalog_failure_hides_the_block_rather_than_failing_the_read(self):
        with patch(f"{MODULE}.list_all_managed_models", new_callable=AsyncMock, side_effect=RuntimeError("x")):
            assert await resolve_model_retirement("sonnet-4-6") is None

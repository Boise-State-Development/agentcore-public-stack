"""Fixtures shared by the app-api route tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Dict, Optional, Set

import pytest

#: Catalog ids a prompt step names in these tests (workflow-designer §21.3).
HAIKU_ID = "us.anthropic.claude-haiku-5-5-v1:0"
SONNET_ID = "us.anthropic.claude-sonnet-5-5-v1:0"


@dataclass
class PromptModels:
    """The model catalog and the caller's model grants, as a prompt step's sheet reads them.

    ``catalog`` maps an id to its row (display name and input rate); an id missing from it has no
    row. ``allowed`` is what the shared access rule grants the caller; ``retired`` maps a retired
    id to its successor (``None`` for no successor).
    """

    catalog: Dict[str, SimpleNamespace] = field(default_factory=dict)
    allowed: Set[str] = field(default_factory=set)
    retired: Dict[str, Optional[str]] = field(default_factory=dict)
    checks: list = field(default_factory=list)

    def add(self, model_id: str, name: str, rate: float, *, allowed: bool = True) -> None:
        self.catalog[model_id] = SimpleNamespace(model_id=model_id, model_name=name, input_price_per_million_tokens=rate)
        if allowed:
            self.allowed.add(model_id)


@pytest.fixture()
def prompt_models(monkeypatch) -> PromptModels:
    """Patch retirement resolution and the model-access rule the workflow sheet calls."""
    from apis.shared.models.retirement import EffectiveModel
    from apis.shared.rbac import service as rbac_service
    from apis.shared.workflows import sheet as workflow_sheet

    models = PromptModels()
    models.add(HAIKU_ID, "Claude Haiku 5.5", 1.1)
    models.add(SONNET_ID, "Claude Sonnet 5.5", 3.3)
    # A test module may already stub retirement for its Agents' models: ids this catalog doesn't
    # know go to that stub, so an Agent step prices as it did before.
    earlier = workflow_sheet.resolve_effective_model

    async def resolve(model_id):
        if model_id not in models.catalog and model_id not in models.retired and getattr(earlier, "__module__", "") != "apis.shared.models.retirement":
            return await earlier(model_id)
        if model_id in models.retired:
            successor = models.retired[model_id]
            if successor is None:
                return EffectiveModel(requested_id=model_id, model_id=model_id, denied=True)
            return EffectiveModel(requested_id=model_id, model_id=successor, record=models.catalog.get(successor))
        return EffectiveModel(requested_id=model_id, model_id=model_id, record=models.catalog.get(model_id))

    class _Roles:
        async def can_access_model(self, user, model_id, *, record=None, catalog_read=True):
            models.checks.append((user.user_id, model_id))
            return record is not None and model_id in models.allowed

    monkeypatch.setattr(workflow_sheet, "resolve_effective_model", resolve)
    monkeypatch.setattr(rbac_service, "get_app_role_service", lambda: _Roles())
    return models

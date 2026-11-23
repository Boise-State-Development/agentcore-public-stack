"""Task models: admin-assigned models for titles, tool summaries, compaction and extraction.

docs/specs/admin-model-assignments.md. Covers the one eligibility rule, resolution precedence
(assignment, then default, retirement followed on both), the store round trip and its cache,
and the admin routes.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.models import task_model_defaults
from apis.shared.models import task_models as tm
from apis.shared.models.models import ManagedModel, ModelParamSpec, SupportedParams
from apis.shared.models.task_models import (
    SOURCE_ASSIGNED,
    SOURCE_DEFAULT,
    TaskModelChoice,
    TaskModelSlot,
    build_slot_views,
    choose_task_model,
    eligibility_problem,
    effort_levels,
    get_slot_spec,
    load_assignments,
    resolve_task_model,
    save_assignment,
    validate_assignment,
)

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
SONNET = "us.anthropic.claude-sonnet-5-5"
HAIKU = "us.anthropic.claude-haiku-5-5"
MICRO = task_model_defaults.SESSION_TITLE_DEFAULT_MODEL_ID


def _row(model_id: str, *, effort=None, temperature=True, **kw) -> ManagedModel:
    params = {}
    if effort is not None:
        params["effort"] = ModelParamSpec(supported=True, allowed=effort, default=effort[-1])
    if not temperature:
        params["temperature"] = ModelParamSpec(supported=False)
    return ManagedModel(
        id=f"uuid-{model_id}",
        modelId=model_id,
        modelName=kw.pop("name", model_id.split(".")[-1]),
        provider=kw.pop("provider", "bedrock"),
        providerName="Anthropic",
        inputModalities=kw.pop("inputModalities", ["TEXT"]),
        outputModalities=kw.pop("outputModalities", ["TEXT"]),
        maxInputTokens=200000,
        enabled=kw.pop("enabled", True),
        inputPricePerMillionTokens=1.0,
        outputPricePerMillionTokens=5.0,
        supportedParams=SupportedParams(params=params) if params else None,
        createdAt=NOW,
        updatedAt=NOW,
        **kw,
    )


TITLE = get_slot_spec(TaskModelSlot.SESSION_TITLE)


class TestDefaults:
    def test_code_defaults_are_pinned(self):
        """The ids to watch for an AWS retirement notice. Changing one is a deliberate edit."""
        assert task_model_defaults.SESSION_TITLE_DEFAULT_MODEL_ID == "us.amazon.nova-micro-v1:0"
        assert task_model_defaults.TOOL_SUMMARY_DEFAULT_MODEL_ID == "us.amazon.nova-micro-v1:0"
        assert task_model_defaults.COMPACTION_SUMMARY_DEFAULT_MODEL_ID == "us.amazon.nova-2-lite-v1:0"

    def test_compaction_constant_reads_the_shared_default(self):
        from agents.main_agent.config.constants import Defaults

        assert Defaults.COMPACTION_SUMMARY_MODEL_ID == task_model_defaults.COMPACTION_SUMMARY_DEFAULT_MODEL_ID

    def test_compaction_default_honours_the_deploy_override(self, monkeypatch):
        monkeypatch.setenv("AGENTCORE_MEMORY_COMPACTION_SUMMARY_MODEL_ID", "us.amazon.nova-lite-v1:0")
        assert get_slot_spec(TaskModelSlot.COMPACTION_SUMMARY).default_model_id() == "us.amazon.nova-lite-v1:0"

    def test_every_slot_has_copy(self):
        for spec in tm.TASK_SLOTS:
            assert spec.title and spec.description and spec.guidance


class TestEligibility:
    def test_a_plain_bedrock_text_row_is_eligible(self):
        assert eligibility_problem(_row(SONNET)) is None

    @pytest.mark.parametrize(
        "row",
        [
            None,
            _row(SONNET, enabled=False),
            _row(SONNET, status="retired"),
            _row(SONNET, provider="mantle"),
            _row(SONNET, outputModalities=["SPEECH"]),
        ],
    )
    def test_refusals_name_a_reason(self, row):
        assert eligibility_problem(row)

    def test_effort_levels_come_from_the_row(self):
        assert effort_levels(_row(SONNET, effort=["low", "medium", "high"])) == ["low", "medium", "high"]
        assert effort_levels(_row(MICRO)) == []


class TestChoose:
    def test_nothing_assigned_runs_the_default_unchanged(self):
        choice = choose_task_model(TITLE, None, [], MICRO)
        assert (choice.model_id, choice.effort, choice.source) == (MICRO, None, SOURCE_DEFAULT)

    def test_a_valid_assignment_wins_with_its_effort(self):
        catalog = [_row(SONNET, effort=["low", "high"])]
        choice = choose_task_model(TITLE, {"modelId": SONNET, "effort": "low"}, catalog, MICRO)
        assert (choice.model_id, choice.effort, choice.source) == (SONNET, "low", SOURCE_ASSIGNED)

    def test_an_effort_the_row_no_longer_declares_is_dropped(self):
        catalog = [_row(SONNET, effort=["high"])]
        choice = choose_task_model(TITLE, {"modelId": SONNET, "effort": "low"}, catalog, MICRO)
        assert choice.model_id == SONNET and choice.effort is None

    def test_a_disabled_assignment_falls_back_and_says_why(self):
        catalog = [_row(SONNET, enabled=False)]
        choice = choose_task_model(TITLE, {"modelId": SONNET}, catalog, MICRO)
        assert choice.model_id == MICRO and choice.source == SOURCE_DEFAULT
        assert "turned off" in choice.fallback_reason

    def test_a_deleted_assignment_falls_back(self):
        choice = choose_task_model(TITLE, {"modelId": SONNET}, [], MICRO)
        assert choice.model_id == MICRO and choice.fallback_reason

    def test_a_retired_assignment_follows_its_successor(self):
        catalog = [_row(SONNET, status="retired", replacedBy=HAIKU), _row(HAIKU)]
        choice = choose_task_model(TITLE, {"modelId": SONNET}, catalog, MICRO)
        assert (choice.model_id, choice.source) == (HAIKU, SOURCE_ASSIGNED)

    def test_a_retired_default_follows_its_successor(self):
        catalog = [_row(MICRO, status="retired", replacedBy=HAIKU), _row(HAIKU)]
        choice = choose_task_model(TITLE, None, catalog, MICRO)
        assert (choice.model_id, choice.source) == (HAIKU, SOURCE_DEFAULT)

    def test_an_unreadable_catalog_runs_the_assignment_as_written(self):
        choice = choose_task_model(TITLE, {"modelId": SONNET, "effort": "low"}, None, MICRO)
        assert (choice.model_id, choice.effort, choice.source) == (SONNET, "low", SOURCE_ASSIGNED)


class TestRequestShape:
    def test_no_effort_sends_no_extra_fields(self):
        choice = TaskModelChoice(TaskModelSlot.SESSION_TITLE, MICRO, None, SOURCE_DEFAULT)
        assert choice.converse_kwargs() == {"modelId": MICRO}
        assert choice.inference_config(temperature=0.3, max_tokens=50) == {"maxTokens": 50, "temperature": 0.3}

    def test_effort_rides_in_additional_model_request_fields(self):
        choice = TaskModelChoice(TaskModelSlot.SESSION_TITLE, SONNET, "low", SOURCE_ASSIGNED)
        assert choice.converse_kwargs() == {
            "modelId": SONNET,
            "additionalModelRequestFields": {"output_config": {"effort": "low"}},
        }

    def test_temperature_is_dropped_for_a_row_that_refuses_it(self):
        choice = TaskModelChoice(
            TaskModelSlot.SESSION_TITLE, SONNET, None, SOURCE_ASSIGNED, record=_row(SONNET, temperature=False)
        )
        assert choice.inference_config(temperature=0.3, max_tokens=50) == {"maxTokens": 50}

    def test_temperature_is_dropped_when_a_declared_params_map_leaves_it_out(self):
        """The Claude 5.5 rows on dev declare only effort and max_tokens, and Bedrock refuses
        temperature for them ("`temperature` is deprecated for this model", seen 2026-11-23)."""
        row = _row(HAIKU, effort=["low", "medium", "high"])
        choice = TaskModelChoice(TaskModelSlot.SESSION_TITLE, HAIKU, "low", SOURCE_ASSIGNED, record=row)
        assert choice.inference_config(temperature=0.3, max_tokens=50) == {"maxTokens": 50}

    def test_temperature_is_kept_for_a_row_that_declares_it(self):
        row = _row("zai.glm-5", effort=None)
        row.supported_params = SupportedParams(params={"temperature": ModelParamSpec(supported=True)})
        choice = TaskModelChoice(TaskModelSlot.SESSION_TITLE, "zai.glm-5", None, SOURCE_ASSIGNED, record=row)
        assert choice.inference_config(temperature=0.3, max_tokens=50)["temperature"] == 0.3


class TestValidate:
    def test_accepts_an_eligible_model_and_effort(self):
        catalog = [_row(SONNET, effort=["low", "high"])]
        assert validate_assignment(SONNET, "high", catalog) == (SONNET, "high")

    def test_stores_a_retired_model_as_its_successor(self):
        catalog = [_row(SONNET, status="retired", replacedBy=HAIKU), _row(HAIKU)]
        assert validate_assignment(SONNET, None, catalog) == (HAIKU, None)

    @pytest.mark.parametrize(
        "model_id, effort, catalog, message",
        [
            (SONNET, None, [], "isn't in the model catalog"),
            (SONNET, None, [_row(SONNET, status="deprecated")], "deprecated"),
            (SONNET, "low", [_row(SONNET)], "no effort setting"),
            (SONNET, "max", [_row(SONNET, effort=["low", "high"])], "one of: low, high"),
            (SONNET, None, [_row(SONNET, status="retired")], "retired with no replacement"),
        ],
    )
    def test_refuses_with_the_reason(self, model_id, effort, catalog, message):
        with pytest.raises(ValueError, match=message):
            validate_assignment(model_id, effort, catalog)


class TestViews:
    def test_an_uncataloged_default_is_flagged_for_the_retirement_watch(self):
        views = build_slot_views({}, [_row(SONNET)])
        title = next(v for v in views if v.spec.slot == TaskModelSlot.SESSION_TITLE)
        assert "isn't in the model catalog" in title.default_notice
        assert [m.model_id for m in title.eligible] == [SONNET]

    def test_deprecated_models_are_not_offered(self):
        views = build_slot_views({}, [_row(SONNET, status="deprecated")])
        assert views[0].eligible == []

    def test_an_unset_extraction_default_says_so(self, monkeypatch):
        monkeypatch.delenv("DOCUMENT_EXTRACT_MODEL_ID", raising=False)
        views = build_slot_views({}, [])
        extract = next(v for v in views if v.spec.slot == TaskModelSlot.DOCUMENT_EXTRACT)
        assert "DOCUMENT_EXTRACT_MODEL_ID" in extract.default_notice


class TestStore:
    @pytest.fixture(autouse=True)
    def _table(self, managed_models_table, monkeypatch):
        import apis.shared.models.managed_models as mm

        monkeypatch.setattr(mm, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))

    @pytest.mark.asyncio
    async def test_set_read_and_clear(self):
        assert await load_assignments() == {}
        await save_assignment(TaskModelSlot.SESSION_TITLE, SONNET, "low", updated_by="admin@example.com")
        await save_assignment(TaskModelSlot.TOOL_SUMMARY, HAIKU, None, updated_by="admin@example.com")

        stored = await load_assignments()
        assert stored["session_title"]["modelId"] == SONNET
        assert stored["session_title"]["effort"] == "low"
        assert stored["tool_summary"] == {**stored["tool_summary"], "modelId": HAIKU}
        assert "effort" not in stored["tool_summary"]

        await save_assignment(TaskModelSlot.SESSION_TITLE, None, None, updated_by="admin@example.com")
        assert set(await load_assignments()) == {"tool_summary"}

    @pytest.mark.asyncio
    async def test_the_item_never_appears_as_a_catalog_row(self):
        from apis.shared.models.managed_models import list_all_managed_models

        await save_assignment(TaskModelSlot.SESSION_TITLE, SONNET, None, updated_by="a")
        assert await list_all_managed_models() == []

    @pytest.mark.asyncio
    async def test_reads_are_cached_until_a_write(self):
        await save_assignment(TaskModelSlot.SESSION_TITLE, SONNET, None, updated_by="a")
        await load_assignments()
        with patch.object(tm, "_read_item", side_effect=AssertionError("cache miss")):
            assert (await load_assignments())["session_title"]["modelId"] == SONNET

    @pytest.mark.asyncio
    async def test_resolve_uses_a_valid_assignment(self):
        from apis.shared.models.managed_models import create_managed_model
        from apis.shared.models.models import ManagedModelCreate

        await create_managed_model(
            ManagedModelCreate(
                modelId=SONNET, modelName="Sonnet", provider="bedrock", providerName="Anthropic",
                inputModalities=["TEXT"], outputModalities=["TEXT"], maxInputTokens=200000,
                maxOutputTokens=4096, inputPricePerMillionTokens=3.0, outputPricePerMillionTokens=15.0,
                enabled=True,
            )
        )
        await save_assignment(TaskModelSlot.TOOL_SUMMARY, SONNET, None, updated_by="a")
        choice = await resolve_task_model(TaskModelSlot.TOOL_SUMMARY)
        assert (choice.model_id, choice.source) == (SONNET, SOURCE_ASSIGNED)


class TestResolveNeverRaises:
    @pytest.mark.asyncio
    async def test_no_table_configured_runs_the_default(self, monkeypatch):
        monkeypatch.delenv("DYNAMODB_MANAGED_MODELS_TABLE_NAME", raising=False)
        choice = await resolve_task_model(TaskModelSlot.SESSION_TITLE)
        assert (choice.model_id, choice.source) == (MICRO, SOURCE_DEFAULT)

    @pytest.mark.asyncio
    async def test_a_store_failure_runs_the_default(self, monkeypatch):
        monkeypatch.setenv("DYNAMODB_MANAGED_MODELS_TABLE_NAME", "t")
        with patch.object(tm, "load_assignments", AsyncMock(side_effect=RuntimeError("down"))), patch.object(
            tm, "_load_catalog", AsyncMock(return_value=None)
        ):
            choice = await resolve_task_model(TaskModelSlot.COMPACTION_SUMMARY, default_model_id="x")
        assert (choice.model_id, choice.source) == ("x", SOURCE_DEFAULT)


# =============================================================================
# Admin routes
# =============================================================================

ROUTES = "apis.app_api.admin.task_models.routes"


@pytest.fixture
def client(make_user):
    from apis.app_api.admin.routes import router
    from tests.conftest import override_admin_auth

    app = FastAPI()
    app.include_router(router)
    override_admin_auth(app, lambda: make_user(email="admin@example.com", roles=["Admin"]))
    return TestClient(app)


@pytest.fixture
def make_user():
    from apis.shared.auth.models import User

    def _make(email="admin@example.com", roles=None):
        return User(email=email, user_id="admin-1", name="Admin", roles=roles or ["Admin"])

    return _make


class TestRoutes:
    @pytest.fixture(autouse=True)
    def _stubs(self):
        self.catalog = [_row(SONNET, effort=["low", "medium", "high"]), _row(HAIKU)]
        self.saved = {}

        async def _load(fresh=False):
            return dict(self.saved)

        async def _save(slot, model_id, effort, *, updated_by):
            if model_id is None:
                self.saved.pop(slot.value, None)
            else:
                self.saved[slot.value] = {"modelId": model_id, "effort": effort, "updatedBy": updated_by}

        with patch(f"{ROUTES}.list_all_managed_models", AsyncMock(side_effect=lambda: list(self.catalog))), patch(
            f"{ROUTES}.load_assignments", side_effect=_load
        ), patch(f"{ROUTES}.save_assignment", side_effect=_save), patch(f"{ROUTES}.get_audit_service") as audit:
            self.audit = audit
            yield

    def test_list_returns_every_task_with_its_choices(self, client):
        body = client.get("/admin/task-models").json()
        slots = {s["slot"]: s for s in body["slots"]}
        assert list(slots) == [s.slot.value for s in tm.TASK_SLOTS]
        title = slots["session_title"]
        assert title["defaultModel"]["modelId"] == MICRO
        assert title["effective"] == {"modelId": MICRO, "modelName": None, "effort": None, "source": "default"}
        sonnet = next(m for m in title["eligibleModels"] if m["modelId"] == SONNET)
        assert sonnet["effortLevels"] == ["low", "medium", "high"]
        assert title["metered"] is False

    def test_put_assigns_and_audits(self, client):
        resp = client.put("/admin/task-models/session_title", json={"modelId": SONNET, "effort": "low"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["effective"]["modelId"] == SONNET and body["effective"]["effort"] == "low"
        assert body["assignment"]["updatedBy"] == "admin@example.com"
        kwargs = self.audit.return_value.record.call_args.kwargs
        assert kwargs["action"] == "task_model.updated" and kwargs["target_id"] == "session_title"
        assert kwargs["after"] == {"modelId": SONNET, "effort": "low"}

    def test_put_null_clears_back_to_the_default(self, client):
        client.put("/admin/task-models/session_title", json={"modelId": SONNET})
        body = client.put("/admin/task-models/session_title", json={"modelId": None}).json()
        assert body["assignment"] is None and body["effective"]["source"] == "default"

    def test_put_refuses_an_ineligible_model(self, client):
        resp = client.put("/admin/task-models/session_title", json={"modelId": "nope"})
        assert resp.status_code == 400 and "catalog" in resp.json()["detail"]

    def test_put_refuses_an_effort_the_model_lacks(self, client):
        resp = client.put("/admin/task-models/session_title", json={"modelId": HAIKU, "effort": "low"})
        assert resp.status_code == 400

    def test_put_refuses_effort_without_a_model(self, client):
        resp = client.put("/admin/task-models/session_title", json={"modelId": None, "effort": "low"})
        assert resp.status_code == 400

    def test_unknown_task_is_404(self, client):
        assert client.put("/admin/task-models/nope", json={"modelId": SONNET}).status_code == 404

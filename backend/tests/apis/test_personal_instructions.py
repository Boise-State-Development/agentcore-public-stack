"""Personal instructions (shared-projects PR-1.4b, §4.5).

A user's standing preferences, saved in their settings and appended last in the
instructions block of every conversation, below any agent's or project's
instructions, which win a conflict. A user without them keeps a byte-identical
prompt, so their cached prefix does not move.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

import apis.app_api.user_settings.routes as settings_routes
from apis.inference_api.chat import routes as chat_routes
from apis.inference_api.chat.routes import (
    compose_agent_system_prompt,
    compose_personal_instructions,
    personal_plain_prompt,
)
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.user_settings.models import MAX_PERSONAL_INSTRUCTIONS_CHARS
from apis.shared.user_settings.repository import UserSettingsRepository

TABLE = "test-user-settings"
USER = User(user_id="u-1", email="u1@example.edu", name="U", roles=["default"])
BASE = "PLATFORM BASE PROMPT\n\nCurrent date: 2026-09-24"


@pytest.fixture()
def repo(monkeypatch) -> UserSettingsRepository:
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_USER_SETTINGS_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        yield UserSettingsRepository(table_name=TABLE)


@pytest.fixture()
def client(repo) -> TestClient:
    app = FastAPI()
    app.include_router(settings_routes.router)
    app.dependency_overrides[get_current_user_from_session] = lambda: USER
    app.dependency_overrides[settings_routes.get_user_settings_repository] = lambda: repo
    return TestClient(app, raise_server_exceptions=False)


# ---- saving them -----------------------------------------------------------


def test_saved_trimmed_kept_beside_the_default_model_and_cleared_by_blank(client):
    client.put("/users/me/settings", json={"defaultModelId": "m-1"})
    saved = client.put("/users/me/settings", json={"personalInstructions": "  Answer briefly.  "}).json()
    assert saved == {"defaultModelId": "m-1", "personalInstructions": "Answer briefly."}
    assert client.get("/users/me/settings").json()["personalInstructions"] == "Answer briefly."

    cleared = client.put("/users/me/settings", json={"personalInstructions": "   "}).json()
    assert cleared == {"defaultModelId": "m-1", "personalInstructions": None}


def test_too_long_is_refused(client):
    response = client.put(
        "/users/me/settings", json={"personalInstructions": "x" * (MAX_PERSONAL_INSTRUCTIONS_CHARS + 1)}
    )
    assert response.status_code == 422


# ---- the prompt -------------------------------------------------------------


def test_nothing_changes_for_a_user_without_them():
    agent_prompt = compose_agent_system_prompt(BASE, "Be an expert.", project_harness=False)
    assert personal_plain_prompt(None, None) is None
    assert personal_plain_prompt(agent_prompt, None) == agent_prompt


def test_below_an_agents_instructions_which_win_a_conflict():
    prompt = compose_personal_instructions(
        compose_agent_system_prompt(BASE, "Be an expert.", project_harness=False),
        "Answer briefly.",
        over="Assistant-Specific Instructions",
    )
    assert prompt == (
        f"{BASE}\n\n## Assistant-Specific Instructions\n\nBe an expert.\n\n## Personal Instructions\n\n"
        "The user's standing preferences for how you work with them. Where they conflict with the "
        "Assistant-Specific Instructions above, follow the Assistant-Specific Instructions.\n\nAnswer briefly."
    )


def test_a_projects_instructions_win_and_the_project_prefix_is_shared():
    """Two members render the project's part identically; only their own part differs."""
    project_part = compose_agent_system_prompt(BASE, "Use the FY27 ledger.", project_harness=True)
    ana = compose_personal_instructions(project_part, "Use bullet points.", over="Project Instructions")
    ben = compose_personal_instructions(project_part, "Be formal.", over="Project Instructions")

    assert ana.startswith(project_part) and ben.startswith(project_part)
    assert "follow the Project Instructions" in ana


def test_a_plain_chat_gets_the_default_prompt_then_theirs_with_nothing_to_defer_to():
    from agents.main_agent.core.system_prompt_builder import SystemPromptBuilder

    prompt = personal_plain_prompt(None, "Answer briefly.")
    assert prompt.startswith(SystemPromptBuilder().build(include_date=True))
    assert prompt.endswith("## Personal Instructions\n\nThe user's standing preferences for how you work with them.\n\nAnswer briefly.")
    assert "follow the" not in prompt


def test_an_app_dispatch_builds_the_same_prompt_as_the_turn(monkeypatch):
    """Else the dispatch misses the turn's cached agent, and an App's pushed context is lost."""
    async def settings(_user_id):
        return {"personalInstructions": "Answer briefly."}

    monkeypatch.setattr(chat_routes, "_load_user_settings", settings)

    class Plain:
        rag_assistant_id = None
        system_prompt = None

    class WithAgent:
        rag_assistant_id = "ast-1"
        system_prompt = "the request's prompt"

    dispatched = asyncio.run(chat_routes._plain_turn_prompt(Plain(), "u-1"))
    assert dispatched == personal_plain_prompt(None, "Answer briefly.")
    assert asyncio.run(chat_routes._plain_turn_prompt(WithAgent(), "u-1")) == "the request's prompt"


def test_the_default_model_lookup_reuses_the_turns_settings_read(monkeypatch):
    async def must_not_read(_user_id):
        raise AssertionError("settings were read twice in one turn")

    async def no_managed_model(_model_id):
        return None

    monkeypatch.setattr(chat_routes, "_load_user_settings", must_not_read)
    monkeypatch.setattr(chat_routes, "_find_managed_model", no_managed_model)
    assert asyncio.run(
        chat_routes._resolve_user_default_model("u-1", settings={"defaultModelId": "m-9"})
    ) == ("m-9", None)

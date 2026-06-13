"""Route tests for ``GET /admin/mantle/models`` (Bedrock Mantle browse)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.shared.auth.models import User

from apis.app_api.admin import routes as admin_routes


def _admin() -> User:
    return User(
        user_id="admin-1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        raw_token="test-token",
    )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[require_admin] = _admin
    return TestClient(app)


def _mantle_model(model_id: str, owned_by: str = "bedrock") -> SimpleNamespace:
    return SimpleNamespace(
        id=model_id, created=1750000000, owned_by=owned_by, object="model"
    )


def _mock_openai_client(models):
    client = MagicMock()
    client.models.list.return_value = SimpleNamespace(data=models)
    return client


class TestListMantleModels:
    def test_lists_models_sorted_by_id(self, client, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        mock_client = _mock_openai_client(
            [
                _mantle_model("qwen.qwen3-coder-30b-a3b-instruct"),
                _mantle_model("openai.gpt-oss-120b"),
            ]
        )
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            return_value="bedrock-api-key-abc",
        ) as mock_token, patch("openai.OpenAI", return_value=mock_client) as mock_ctor:
            response = client.get("/admin/mantle/models")

        assert response.status_code == 200
        body = response.json()
        assert body["region"] == "us-west-2"
        assert body["totalCount"] == 2
        assert [m["id"] for m in body["models"]] == [
            "openai.gpt-oss-120b",
            "qwen.qwen3-coder-30b-a3b-instruct",
        ]
        mock_token.assert_called_once_with("us-west-2")
        # OpenAI-compatible client pointed at the regional Mantle endpoint,
        # authenticated with the minted bearer token.
        _, kwargs = mock_ctor.call_args
        assert kwargs["base_url"] == "https://bedrock-mantle.us-west-2.api.aws/v1"
        assert kwargs["api_key"] == "bedrock-api-key-abc"

    def test_region_override_and_max_results(self, client, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        mock_client = _mock_openai_client(
            [_mantle_model("a.model"), _mantle_model("b.model")]
        )
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            return_value="bedrock-api-key-abc",
        ) as mock_token, patch("openai.OpenAI", return_value=mock_client):
            response = client.get(
                "/admin/mantle/models", params={"region": "eu-west-1", "max_results": 1}
            )

        assert response.status_code == 200
        body = response.json()
        assert body["region"] == "eu-west-1"
        assert body["totalCount"] == 1
        mock_token.assert_called_once_with("eu-west-1")

    def test_credential_failure_is_500_with_detail(self, client):
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            side_effect=ValueError("No AWS credentials available"),
        ):
            response = client.get("/admin/mantle/models")

        assert response.status_code == 500
        assert "bearer token" in response.json()["detail"]

    def test_mantle_api_failure_is_500(self, client):
        failing_client = MagicMock()
        failing_client.models.list.side_effect = RuntimeError("connection refused")
        with patch(
            "apis.shared.bedrock.generate_bedrock_bearer_token",
            return_value="bedrock-api-key-abc",
        ), patch("openai.OpenAI", return_value=failing_client):
            response = client.get("/admin/mantle/models")

        assert response.status_code == 500
        assert "Mantle" in response.json()["detail"]

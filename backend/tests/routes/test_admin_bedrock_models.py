"""Tests for ``GET /admin/bedrock/models`` filter validation and error handling.

Two invariants are locked in here:

1. Filter values that don't conform to the AWS API's accepted shapes are
   rejected at the FastAPI/Pydantic boundary with a 422 — never sent
   downstream and never reflected in the response body.
2. When the boto3 call does raise, the response body never reflects the
   AWS error message, the user input, or AWS-internal pattern detail.
   The shared ``register_aws_client_error_handler`` (registered on the
   app at startup) maps ``ValidationException``-class errors to a
   generic 400, all other ``ClientError``s to a generic 502, and the
   route itself no longer carries its own reflective handlers.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.admin.routes import router
from apis.shared.auth.rbac import require_admin
from apis.shared.security import (
    register_aws_client_error_handler,
    register_validation_error_handler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    # Wire the same global handlers the production app installs.
    register_aws_client_error_handler(_app)
    register_validation_error_handler(_app)
    return _app


def _admin_client(app: FastAPI, make_user) -> TestClient:
    user = make_user(roles=["system_admin"])
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pydantic-level rejection: invalid enum values short-circuit before AWS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param,value",
    [
        ("by_output_modality", "INVALID"),
        ("by_output_modality", "<script>"),
        ("by_inference_type", "INVALID"),
        ("by_inference_type", "<img src=x>"),
        ("by_customization_type", "INVALID"),
        ("by_customization_type", "garbage"),
    ],
)
def test_invalid_enum_filter_returns_422_without_calling_aws(
    app, make_user, param: str, value: str
) -> None:
    client = _admin_client(app, make_user)
    with patch("apis.app_api.admin.routes.boto3.client") as boto3_client:
        resp = client.get(f"/admin/bedrock/models?{param}={value}")

    assert resp.status_code == 422
    boto3_client.assert_not_called()
    body_text = resp.text
    # Whatever the validation error structure is, neither the input
    # nor the enum's allowed-value set should be reflected back.
    assert value not in body_text
    assert "INFERENCE_PROFILE" not in body_text
    assert "FINE_TUNING" not in body_text
    assert "EMBEDDING" not in body_text


@pytest.mark.parametrize(
    "value",
    [
        "<script>",
        "<img src=x onerror=alert(1)>",
        "has/slash",
        "has,comma",
        "has\nnewline",
        "x" * 64,  # longer than AWS's 63-char cap
    ],
)
def test_invalid_provider_filter_returns_422_without_calling_aws(
    app, make_user, value: str
) -> None:
    client = _admin_client(app, make_user)
    with patch("apis.app_api.admin.routes.boto3.client") as boto3_client:
        resp = client.get("/admin/bedrock/models", params={"by_provider": value})

    assert resp.status_code == 422
    boto3_client.assert_not_called()
    body_text = resp.text
    assert value not in body_text


def test_legitimate_filters_are_accepted(app, make_user) -> None:
    client = _admin_client(app, make_user)

    fake_bedrock = MagicMock()
    fake_bedrock.list_foundation_models.return_value = {"modelSummaries": []}

    with patch(
        "apis.app_api.admin.routes.boto3.client", return_value=fake_bedrock
    ):
        resp = client.get(
            "/admin/bedrock/models",
            params={
                "by_provider": "Anthropic",
                "by_output_modality": "TEXT",
                "by_inference_type": "ON_DEMAND",
                "by_customization_type": "FINE_TUNING",
            },
        )

    assert resp.status_code == 200
    fake_bedrock.list_foundation_models.assert_called_once_with(
        byProvider="Anthropic",
        byOutputModality="TEXT",
        byInferenceType="ON_DEMAND",
        byCustomizationType="FINE_TUNING",
    )


# ---------------------------------------------------------------------------
# AWS-side errors: response body never reflects upstream detail
# ---------------------------------------------------------------------------


def test_aws_validation_error_returns_400_generic(app, make_user) -> None:
    """Even if a value passes our Pydantic validators (e.g. an
    accidentally-configured AWS API regression), the global
    ClientError handler maps the upstream ValidationException to a
    generic 400 with no reflection."""
    client = _admin_client(app, make_user)

    fake_bedrock = MagicMock()
    fake_bedrock.list_foundation_models.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ValidationException",
                "Message": (
                    "1 validation error detected: Value '<script>alert(1)</script>' "
                    "at 'byProvider' failed to satisfy constraint: Member must "
                    "satisfy regular expression pattern: [A-Za-z0-9- ]{1,63}"
                ),
            }
        },
        operation_name="ListFoundationModels",
    )

    with patch(
        "apis.app_api.admin.routes.boto3.client", return_value=fake_bedrock
    ):
        resp = client.get("/admin/bedrock/models", params={"by_provider": "Anthropic"})

    assert resp.status_code == 400
    body_text = resp.text
    # AWS error code, the regex, the user input, and the parameter
    # name all must be absent from the response body.
    assert "ValidationException" not in body_text
    assert "<script>" not in body_text
    assert "[A-Za-z0-9-" not in body_text
    assert "byProvider" not in body_text


def test_aws_other_client_error_returns_502_generic(app, make_user) -> None:
    client = _admin_client(app, make_user)

    fake_bedrock = MagicMock()
    fake_bedrock.list_foundation_models.side_effect = ClientError(
        error_response={
            "Error": {
                "Code": "ThrottlingException",
                "Message": "Rate exceeded for arn:aws:bedrock:us-west-2:123456789012:model/x",
            }
        },
        operation_name="ListFoundationModels",
    )

    with patch(
        "apis.app_api.admin.routes.boto3.client", return_value=fake_bedrock
    ):
        resp = client.get("/admin/bedrock/models", params={"by_provider": "Anthropic"})

    assert resp.status_code == 502
    body_text = resp.text
    assert "ThrottlingException" not in body_text
    assert "arn:aws" not in body_text


def test_unhandled_exception_does_not_reflect_message(app, make_user) -> None:
    """A non-ClientError raised from inside the route must not leak its
    str() representation into the response body. FastAPI's default 500
    handler returns a generic 'Internal Server Error' string."""
    _admin_client(app, make_user)

    fake_bedrock = MagicMock()
    fake_bedrock.list_foundation_models.side_effect = RuntimeError(
        "internal detail at /db/users that must not leak"
    )

    with patch(
        "apis.app_api.admin.routes.boto3.client", return_value=fake_bedrock
    ):
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/admin/bedrock/models",
            params={"by_provider": "Anthropic"},
        )

    assert resp.status_code == 500
    body_text = resp.text
    assert "internal detail" not in body_text
    assert "/db/users" not in body_text
    assert "RuntimeError" not in body_text

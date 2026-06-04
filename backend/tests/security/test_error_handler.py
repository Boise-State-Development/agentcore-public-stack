"""Unit tests for ``apis.shared.security.error_handler``.

Verifies that AWS ``ClientError`` exceptions become generic JSON responses
with no AWS message reflection, and that unhandled exceptions become a
generic 500 with no traceback in the response body.
"""

from __future__ import annotations

from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.security.error_handler import (
    register_aws_client_error_handler,
    register_safe_500_handler,
)


def _validation_error(message: str) -> ClientError:
    return ClientError(
        error_response={
            "Error": {
                "Code": "ValidationException",
                "Message": message,
            }
        },
        operation_name="ListFoundationModels",
    )


def _throttling_error() -> ClientError:
    return ClientError(
        error_response={
            "Error": {
                "Code": "ThrottlingException",
                "Message": "Rate exceeded for arn:aws:bedrock:us-west-2:123456789012:foundation-model/x",
            }
        },
        operation_name="ListFoundationModels",
    )


def _make_app(*, register_500: bool = False) -> FastAPI:
    app = FastAPI()
    register_aws_client_error_handler(app)
    if register_500:
        register_safe_500_handler(app)

    @app.get("/aws-validation")
    async def _v() -> dict:
        raise _validation_error(
            "1 validation error detected: Value '<script>alert(1)</script>' at "
            "'byProvider' failed to satisfy constraint: Member must satisfy "
            "regular expression pattern: [A-Za-z0-9- ]{1,63}"
        )

    @app.get("/aws-other")
    async def _o() -> dict:
        raise _throttling_error()

    @app.get("/boom")
    async def _b() -> dict:
        raise RuntimeError("internal detail that must not be leaked at /db/users")

    return app


def test_validation_client_error_maps_to_400_generic() -> None:
    client = TestClient(_make_app())
    resp = client.get("/aws-validation")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid request parameters."}


def test_validation_response_does_not_reflect_user_input() -> None:
    client = TestClient(_make_app())
    resp = client.get("/aws-validation")
    body_text = resp.text
    # Reflected payload must not appear in the body.
    assert "<script>" not in body_text
    assert "byProvider" not in body_text
    # Internal AWS regex pattern must not leak.
    assert "[A-Za-z0-9-" not in body_text
    # AWS error code must not leak.
    assert "ValidationException" not in body_text


def test_other_client_error_maps_to_502_generic() -> None:
    client = TestClient(_make_app())
    resp = client.get("/aws-other")
    assert resp.status_code == 502
    assert resp.json() == {"detail": "Upstream service error."}
    # AWS error message containing an ARN must not leak.
    body_text = resp.text
    assert "arn:aws" not in body_text
    assert "ThrottlingException" not in body_text


def test_safe_500_handler_returns_generic_body_without_traceback() -> None:
    client = TestClient(_make_app(register_500=True), raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error."}
    body_text = resp.text
    assert "RuntimeError" not in body_text
    assert "internal detail" not in body_text
    assert "/db/users" not in body_text


def test_safe_500_handler_preserves_aws_handler_when_registered_first() -> None:
    """Both handlers can coexist; AWS errors still get the specific mapping."""
    client = TestClient(_make_app(register_500=True))
    resp = client.get("/aws-validation")
    assert resp.status_code == 400
    assert resp.json() == {"detail": "Invalid request parameters."}

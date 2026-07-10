"""Tests for the Cognito Pre-Token-Generation v2 access-token enrichment handler.

Run with: uv run pytest infrastructure/lambda-assets/token-enrichment/ (from repo root)
or point pytest at this directory. The handler is stdlib-only, so these tests
have no third-party dependencies beyond pytest itself.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

# Load handler.py directly by path so the test doesn't depend on package layout.
_HANDLER_PATH = Path(__file__).parent / "handler.py"
_spec = importlib.util.spec_from_file_location(
    "token_enrichment_handler", _HANDLER_PATH
)
assert _spec and _spec.loader
handler_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler_module)
handler = handler_module.handler


BSU_CLAIM = "https://boisestate.edu/employee_number"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no ACCESS_TOKEN_CLAIMS unless it sets one."""
    monkeypatch.delenv("ACCESS_TOKEN_CLAIMS", raising=False)


def _make_event(user_attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """A minimal but realistic Pre-Token-Generation v2 event."""
    return {
        "version": "2",
        "triggerSource": "TokenGeneration_HostedAuth",
        "userPoolId": "us-west-2_Example",
        "userName": "ms-entra-id_113124161",
        "request": {
            "userAttributes": user_attributes if user_attributes is not None else {},
            "scopes": ["openid", "email"],
        },
        "response": {},
    }


def _access_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the claimsToAddOrOverride map (or {} if not present)."""
    response = event.get("response") or {}
    details = response.get("claimsAndScopeOverrideDetails") or {}
    access_token_gen = details.get("accessTokenGeneration") or {}
    claims: dict[str, Any] = access_token_gen.get("claimsToAddOrOverride") or {}
    return claims


class TestEnrichment:
    def test_present_attribute_is_copied_to_the_namespaced_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = _make_event({"sub": "uuid-1", "custom:provider_sub": "113124161"})

        result = handler(event, None)

        assert _access_claims(result)[BSU_CLAIM] == "113124161"

    def test_multiple_claims_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS",
            json.dumps(
                {
                    BSU_CLAIM: "custom:provider_sub",
                    "https://boisestate.edu/roles": "custom:roles",
                }
            ),
        )
        event = _make_event(
            {"custom:provider_sub": "113124161", "custom:roles": "student,alum"}
        )

        claims = _access_claims(handler(event, None))

        assert claims[BSU_CLAIM] == "113124161"
        assert claims["https://boisestate.edu/roles"] == "student,alum"

    def test_missing_attribute_is_skipped_not_errored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Native Cognito user has no custom:provider_sub — claim must be omitted.
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = _make_event({"sub": "native-user"})

        result = handler(event, None)

        assert BSU_CLAIM not in _access_claims(result)

    def test_empty_string_attribute_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = _make_event({"custom:provider_sub": ""})

        assert BSU_CLAIM not in _access_claims(handler(event, None))

    def test_only_present_subset_is_copied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS",
            json.dumps(
                {
                    BSU_CLAIM: "custom:provider_sub",
                    "https://boisestate.edu/roles": "custom:roles",
                }
            ),
        )
        event = _make_event({"custom:provider_sub": "113124161"})  # no roles attr

        claims = _access_claims(handler(event, None))

        assert claims == {BSU_CLAIM: "113124161"}

    def test_preserves_preexisting_claims_to_add(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = _make_event({"custom:provider_sub": "113124161"})
        event["response"] = {
            "claimsAndScopeOverrideDetails": {
                "accessTokenGeneration": {
                    "claimsToAddOrOverride": {"existing": "keep-me"}
                }
            }
        }

        claims = _access_claims(handler(event, None))

        assert claims["existing"] == "keep-me"
        assert claims[BSU_CLAIM] == "113124161"


class TestFailOpen:
    def test_no_env_returns_event_unchanged(self) -> None:
        event = _make_event({"custom:provider_sub": "113124161"})

        result = handler(event, None)

        assert result is event
        assert _access_claims(result) == {}

    def test_empty_env_returns_event_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACCESS_TOKEN_CLAIMS", "")
        event = _make_event({"custom:provider_sub": "113124161"})

        assert handler(event, None) is event

    def test_malformed_json_env_returns_event_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACCESS_TOKEN_CLAIMS", "{not valid json")
        event = _make_event({"custom:provider_sub": "113124161"})

        result = handler(event, None)

        assert result is event
        assert _access_claims(result) == {}

    def test_non_object_json_env_is_treated_as_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ACCESS_TOKEN_CLAIMS", json.dumps(["not", "a", "map"]))
        event = _make_event({"custom:provider_sub": "113124161"})

        assert _access_claims(handler(event, None)) == {}

    def test_unexpected_event_shape_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        # No "request" key at all.
        event: dict[str, Any] = {"version": "2"}

        # Must not raise; returns the event (enrichment simply can't happen).
        result = handler(event, None)

        assert result is event

    def test_request_missing_user_attributes_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = {"version": "2", "request": {}}

        assert handler(event, None) == {"version": "2", "request": {}}

    def test_null_claims_details_is_replaced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Cognito can send claimsAndScopeOverrideDetails as an explicit null.
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS", json.dumps({BSU_CLAIM: "custom:provider_sub"})
        )
        event = _make_event({"custom:provider_sub": "113124161"})
        event["response"] = {"claimsAndScopeOverrideDetails": None}

        claims = _access_claims(handler(event, None))

        assert claims[BSU_CLAIM] == "113124161"


class TestClaimMapParsing:
    def test_non_string_values_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "ACCESS_TOKEN_CLAIMS",
            json.dumps({BSU_CLAIM: "custom:provider_sub", "bad": 123}),
        )
        event = _make_event(
            {"custom:provider_sub": "113124161", "123": "should-not-map"}
        )

        claims = _access_claims(handler(event, None))

        assert claims == {BSU_CLAIM: "113124161"}

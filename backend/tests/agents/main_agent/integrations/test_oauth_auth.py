"""
Tests for OAuthBearerAuth and CompositeAuth.

Requirements: 19.1–19.5
"""
import pytest
import httpx2

from agents.main_agent.integrations.oauth_auth import OAuthBearerAuth, CompositeAuth


class TestOAuthBearerAuthStaticToken:
    """Tests for OAuthBearerAuth with a static token."""

    def test_static_token_adds_authorization_header(self):
        """Req 19.1: WHEN a static token is provided, auth_flow adds 'Authorization: Bearer {token}'."""
        auth = OAuthBearerAuth(token="my-secret-token")
        request = httpx2.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["Authorization"] == "Bearer my-secret-token"


class TestOAuthBearerAuthTokenProvider:
    """Tests for OAuthBearerAuth with a token_provider callback."""

    def test_token_provider_callback_is_called(self):
        """Req 19.2: WHEN a token_provider callback is provided, auth_flow calls the provider and uses the returned token."""
        called = False

        def provider():
            nonlocal called
            called = True
            return "provider-token"

        auth = OAuthBearerAuth(token_provider=provider)
        request = httpx2.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert called
        assert modified_request.headers["Authorization"] == "Bearer provider-token"

    def test_token_provider_returns_none_no_header(self):
        """Req 19.3: WHEN the token_provider returns None, auth_flow does not add an Authorization header."""
        auth = OAuthBearerAuth(token_provider=lambda: None)
        request = httpx2.Request("GET", "https://example.com/api")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert "Authorization" not in modified_request.headers


class TestOAuthBearerAuthValidation:
    """Tests for OAuthBearerAuth constructor validation."""

    def test_raises_value_error_when_neither_token_nor_provider(self):
        """Req 19.4: IF neither token nor token_provider is provided, raises ValueError."""
        with pytest.raises(ValueError, match="Either token or token_provider must be provided"):
            OAuthBearerAuth()


class TestCompositeAuth:
    """Tests for CompositeAuth applying multiple auth handlers in order."""

    def test_applies_all_handlers_in_order(self):
        """Req 19.5: CompositeAuth applies all auth handlers in order to the request."""
        auth1 = OAuthBearerAuth(token="token-one")
        auth2 = OAuthBearerAuth(token="token-two")

        composite = CompositeAuth(auth1, auth2)
        request = httpx2.Request("GET", "https://example.com/api")

        flow = composite.auth_flow(request)
        modified_request = next(flow)

        # The second handler overwrites the first, proving both ran in order
        assert modified_request.headers["Authorization"] == "Bearer token-two"

    def test_applies_mixed_auth_handlers(self):
        """Req 19.5 (detail): CompositeAuth works with handlers that set different headers."""

        class CustomHeaderAuth(httpx2.Auth):
            def auth_flow(self, request):
                request.headers["X-Custom"] = "custom-value"
                yield request

        custom_auth = CustomHeaderAuth()
        oauth = OAuthBearerAuth(token="my-token")

        composite = CompositeAuth(custom_auth, oauth)
        request = httpx2.Request("GET", "https://example.com/api")

        flow = composite.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["X-Custom"] == "custom-value"
        assert modified_request.headers["Authorization"] == "Bearer my-token"

    def test_empty_composite_yields_unmodified_request(self):
        """Req 19.5 (edge): CompositeAuth with no handlers yields the request unchanged."""
        composite = CompositeAuth()
        request = httpx2.Request("GET", "https://example.com/api")
        original_headers = dict(request.headers)

        flow = composite.auth_flow(request)
        modified_request = next(flow)

        # No auth headers should have been added
        assert "Authorization" not in modified_request.headers

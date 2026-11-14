"""
Tests for SigV4HTTPXAuth and get_gateway_region_from_url.

Requirements: 20.1–20.5
"""
import pytest
import httpx2
from unittest.mock import patch, MagicMock

from agents.main_agent.integrations.gateway_auth import (
    SigV4HTTPXAuth,
    get_gateway_region_from_url,
)


class TestSigV4HTTPXAuthFlow:
    """Tests for SigV4HTTPXAuth.auth_flow signing behavior."""

    def _make_auth(self):
        """Create a SigV4HTTPXAuth with mocked credentials and signer."""
        mock_credentials = MagicMock()
        mock_signer = MagicMock()

        # When add_auth is called, simulate adding signature headers
        def fake_add_auth(aws_request):
            aws_request.headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=..."
            aws_request.headers["X-Amz-Date"] = "20250101T000000Z"

        mock_signer.add_auth.side_effect = fake_add_auth

        with patch(
            "agents.main_agent.integrations.gateway_auth.SigV4Auth",
            return_value=mock_signer,
        ):
            auth = SigV4HTTPXAuth(
                credentials=mock_credentials,
                service="bedrock-agentcore",
                region="us-west-2",
            )

        # Replace the signer with our mock after construction
        auth.signer = mock_signer
        return auth, mock_signer

    def test_auth_flow_adds_signature_headers(self):
        """Req 20.1: auth_flow adds AWS signature headers to the request."""
        auth, mock_signer = self._make_auth()
        request = httpx2.Request("POST", "https://gateway.example.com/mcp", content=b"body")

        flow = auth.auth_flow(request)
        signed_request = next(flow)

        mock_signer.add_auth.assert_called_once()
        assert "Authorization" in signed_request.headers
        assert "X-Amz-Date" in signed_request.headers

    def test_auth_flow_removes_connection_header(self):
        """Req 20.2: auth_flow removes the 'connection' header before signing."""
        auth, mock_signer = self._make_auth()
        request = httpx2.Request(
            "POST",
            "https://gateway.example.com/mcp",
            headers={"connection": "keep-alive"},
            content=b"body",
        )

        # Capture the headers passed to AWSRequest
        captured_headers = {}

        def capture_add_auth(aws_request):
            captured_headers.update(dict(aws_request.headers))
            aws_request.headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=..."

        mock_signer.add_auth.side_effect = capture_add_auth

        with patch(
            "agents.main_agent.integrations.gateway_auth.AWSRequest"
        ) as mock_aws_request:
            # Make AWSRequest return a mock that stores headers
            mock_aws_req_instance = MagicMock()
            mock_aws_req_instance.headers = {}
            mock_aws_request.return_value = mock_aws_req_instance

            # Capture what headers are passed to AWSRequest constructor
            flow = auth.auth_flow(request)
            next(flow)

            # Verify AWSRequest was called and 'connection' was NOT in the headers
            call_kwargs = mock_aws_request.call_args
            passed_headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert "connection" not in passed_headers


class TestGetGatewayRegionFromUrl:
    """Tests for get_gateway_region_from_url extraction and fallback."""

    def test_extracts_region_from_standard_url(self):
        """Req 20.3: Extracts the correct region from a standard Gateway URL pattern."""
        url = "https://gateway-abc123.bedrock-agentcore.us-west-2.amazonaws.com/agents/mcp"
        assert get_gateway_region_from_url(url) == "us-west-2"

    def test_extracts_region_us_east_1(self):
        """Req 20.3: Extracts us-east-1 from a Gateway URL."""
        url = "https://gateway-xyz.bedrock-agentcore.us-east-1.amazonaws.com/tools"
        assert get_gateway_region_from_url(url) == "us-east-1"

    def test_extracts_region_eu_west_1(self):
        """Req 20.3: Extracts eu-west-1 from a Gateway URL."""
        url = "https://gateway-foo.bedrock-agentcore.eu-west-1.amazonaws.com/mcp/sse"
        assert get_gateway_region_from_url(url) == "eu-west-1"

    @patch("agents.main_agent.integrations.gateway_auth.boto3.Session")
    def test_fallback_to_boto3_session_region(self, mock_session_cls):
        """Req 20.4: WHEN URL doesn't match, falls back to boto3 session region."""
        mock_session = MagicMock()
        mock_session.region_name = "ap-southeast-1"
        mock_session_cls.return_value = mock_session

        url = "https://some-other-service.example.com/api"
        assert get_gateway_region_from_url(url) == "ap-southeast-1"

    @patch("agents.main_agent.integrations.gateway_auth.boto3.Session")
    def test_raises_value_error_when_no_region(self, mock_session_cls):
        """Req 20.5: IF no region from URL or boto3 session, raises ValueError."""
        mock_session = MagicMock()
        mock_session.region_name = None
        mock_session_cls.return_value = mock_session

        url = "https://some-other-service.example.com/api"
        with pytest.raises(ValueError, match="Cannot extract region from URL"):
            get_gateway_region_from_url(url)

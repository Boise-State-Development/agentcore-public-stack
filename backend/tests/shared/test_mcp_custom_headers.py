"""Static custom headers on an external MCP connector (`customHeaders`).

GitHub's MCP server picks its toolsets from an `X-MCP-Toolsets` header; its
`/mcp/x/<toolset>` URLs leave out the `context` toolset that `issue_write`'s
App calls (`ui_get`). These tests pin the model contract that lets an admin
send such a header: it round-trips through storage and the admin API, it is
normalized deterministically, and it can never stand in for the auth or
session headers the transport owns.
"""

import pytest
from pydantic import ValidationError

from apis.shared.tools.models import (
    MAX_CUSTOM_HEADERS,
    MCPDiscoverRequest,
    MCPServerConfig,
    MCPServerConfigRequest,
    MCPServerConfigResponse,
    validate_custom_headers,
)

GITHUB_URL = "https://api.githubcopilot.com/mcp/"


class TestRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        config = MCPServerConfig(
            server_url=GITHUB_URL,
            auth_type="oauth2",
            custom_headers={"X-MCP-Toolsets": "issues,context"},
        )

        stored = config.to_dict()
        assert stored["customHeaders"] == {"X-MCP-Toolsets": "issues,context"}
        assert MCPServerConfig.from_dict(stored) == config

    def test_rows_written_before_the_field_read_as_empty(self):
        legacy = {"serverUrl": GITHUB_URL, "authType": "none", "tools": []}

        assert MCPServerConfig.from_dict(legacy).custom_headers == {}

    def test_a_stored_null_reads_as_empty(self):
        row = {"serverUrl": GITHUB_URL, "authType": "none", "customHeaders": None}

        assert MCPServerConfig.from_dict(row).custom_headers == {}

    def test_admin_request_and_response_carry_the_headers(self):
        request = MCPServerConfigRequest.model_validate(
            {
                "serverUrl": GITHUB_URL,
                "authType": "none",
                "customHeaders": {"X-MCP-Toolsets": "issues,context"},
            }
        )
        config = request.to_model()
        response = MCPServerConfigResponse.from_model(config).model_dump(by_alias=True)

        assert config.custom_headers == {"X-MCP-Toolsets": "issues,context"}
        assert response["customHeaders"] == {"X-MCP-Toolsets": "issues,context"}

    def test_discover_request_passes_the_headers_to_the_client_config(self):
        request = MCPDiscoverRequest.model_validate(
            {
                "serverUrl": GITHUB_URL,
                "authType": "none",
                "customHeaders": {"X-MCP-Toolsets": "issues,context"},
            }
        )

        assert request.to_config().custom_headers == {"X-MCP-Toolsets": "issues,context"}


class TestNormalization:
    def test_names_and_values_are_stripped(self):
        assert validate_custom_headers({"  X-MCP-Toolsets ": " issues,context  "}) == {
            "X-MCP-Toolsets": "issues,context"
        }

    def test_sorted_by_name_whatever_the_input_order(self):
        headers = validate_custom_headers({"X-Zeta": "1", "x-alpha": "2", "X-Mid": "3"})

        assert list(headers) == ["x-alpha", "X-Mid", "X-Zeta"]

    def test_empty_and_none_are_empty(self):
        assert validate_custom_headers(None) == {}
        assert validate_custom_headers({}) == {}


class TestRefusals:
    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "AUTHORIZATION",
            "Cookie",
            "Host",
            "Mcp-Session-Id",
            "mcp-session-id",
            "Mcp-Protocol-Version",
            "Proxy-Authorization",
            "Content-Type",
            "Content-Length",
            "X-Amz-Security-Token",
            "x-amz-date",
        ],
    )
    def test_reserved_names_are_refused(self, name):
        with pytest.raises(ValueError, match="cannot be a custom header"):
            validate_custom_headers({name: "x"})

    def test_a_reserved_name_is_a_422_on_the_admin_request(self):
        with pytest.raises(ValidationError):
            MCPServerConfigRequest.model_validate(
                {
                    "serverUrl": GITHUB_URL,
                    "customHeaders": {"Authorization": "Bearer stolen"},
                }
            )

    def test_a_reserved_name_is_refused_on_discovery_too(self):
        with pytest.raises(ValidationError):
            MCPDiscoverRequest.model_validate(
                {"serverUrl": GITHUB_URL, "customHeaders": {"Cookie": "a=b"}}
            )

    def test_the_stored_model_refuses_them_as_well(self):
        with pytest.raises(ValidationError):
            MCPServerConfig(server_url=GITHUB_URL, custom_headers={"Host": "evil"})

    def test_case_insensitive_duplicates_are_refused(self):
        with pytest.raises(ValueError, match="more than once"):
            validate_custom_headers({"X-MCP-Toolsets": "issues", "x-mcp-toolsets": "repos"})

    @pytest.mark.parametrize("name", ["", "   ", "Bad Name", "X:Colon", "X\nY", "Ünïcode"])
    def test_malformed_names_are_refused(self, name):
        with pytest.raises(ValueError):
            validate_custom_headers({name: "x"})

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "a\r\nX-Injected: 1", "line\nbreak", "nul\x00", "ünïcode"],
    )
    def test_malformed_values_are_refused(self, value):
        with pytest.raises(ValueError):
            validate_custom_headers({"X-Test": value})

    def test_non_text_values_are_refused(self):
        with pytest.raises(ValueError, match="text value"):
            validate_custom_headers({"X-Test": 5})

    def test_too_many_headers_are_refused(self):
        headers = {f"X-H{i}": "v" for i in range(MAX_CUSTOM_HEADERS + 1)}

        with pytest.raises(ValueError, match=f"At most {MAX_CUSTOM_HEADERS}"):
            validate_custom_headers(headers)

    def test_the_cap_itself_is_allowed(self):
        headers = {f"X-H{i}": "v" for i in range(MAX_CUSTOM_HEADERS)}

        assert len(validate_custom_headers(headers)) == MAX_CUSTOM_HEADERS

    def test_overlong_name_and_value_are_refused(self):
        with pytest.raises(ValueError):
            validate_custom_headers({"X-" + "a" * 200: "v"})
        with pytest.raises(ValueError):
            validate_custom_headers({"X-Test": "v" * 2000})


class TestStoredRows:
    def test_a_header_that_no_longer_validates_is_dropped_not_fatal(self):
        """If a later release reserves a name an existing row uses, the row
        (and its tool) must still load, minus that header."""
        row = {
            "serverUrl": GITHUB_URL,
            "authType": "none",
            "customHeaders": {"Authorization": "Bearer x", "X-MCP-Toolsets": "issues"},
        }

        assert MCPServerConfig.from_dict(row).custom_headers == {"X-MCP-Toolsets": "issues"}

    def test_a_non_map_value_reads_as_empty(self):
        row = {"serverUrl": GITHUB_URL, "customHeaders": "X-MCP-Toolsets: issues"}

        assert MCPServerConfig.from_dict(row).custom_headers == {}

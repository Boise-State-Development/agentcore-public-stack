"""Tests for the config-message injection.

The SPA can no longer hold a Cognito access token (BFF cutover), so the
voice WS proxy injects ``auth_token`` and ``user_id`` into the SPA's first
config frame before forwarding upstream. Inference-api reads those fields
from that frame to identify the user.
"""

from __future__ import annotations

import json

from apis.app_api.voice.proxy import _inject_config_auth


def test_injects_auth_token_and_user_id() -> None:
    raw = json.dumps({"type": "config", "session_id": "sess-A"})
    out = _inject_config_auth(raw, access_token="cognito-token", user_id="user-1")
    parsed = json.loads(out)
    assert parsed["auth_token"] == "cognito-token"
    assert parsed["user_id"] == "user-1"
    assert parsed["session_id"] == "sess-A"


def test_preserves_existing_user_id_from_spa() -> None:
    raw = json.dumps({"type": "config", "session_id": "sess-A", "user_id": "spa-set"})
    parsed = json.loads(_inject_config_auth(raw, access_token="t", user_id="proxy-set"))
    # Don't clobber a value the SPA explicitly set.
    assert parsed["user_id"] == "spa-set"
    # auth_token always comes from the proxy.
    assert parsed["auth_token"] == "t"


def test_non_config_frame_is_passthrough() -> None:
    raw = json.dumps({"type": "bidi_audio_input", "audio": "base64..."})
    assert _inject_config_auth(raw, access_token="t", user_id="u") == raw


def test_non_json_frame_is_passthrough() -> None:
    assert _inject_config_auth("not-json", access_token="t", user_id="u") == "not-json"


def test_non_object_json_is_passthrough() -> None:
    assert _inject_config_auth("[1,2,3]", access_token="t", user_id="u") == "[1,2,3]"

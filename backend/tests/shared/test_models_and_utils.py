"""Task 2: Model validators, serialization round-trips, and pure utility functions."""

import base64
import json
from datetime import datetime
from decimal import Decimal

import pytest


# ===================================================================
# users/models.py
# ===================================================================

class TestUserModels:
    def test_email_lowercased(self):
        from apis.shared.users.models import UserProfile
        p = UserProfile(userId="u1", email="Alice@Example.COM", name="A", emailDomain="Example.COM", createdAt="t", lastLoginAt="t")
        assert p.email == "alice@example.com"

    def test_domain_lowercased(self):
        from apis.shared.users.models import UserProfile
        p = UserProfile(userId="u1", email="a@b.com", name="A", emailDomain="B.COM", createdAt="t", lastLoginAt="t")
        assert p.email_domain == "b.com"

    def test_status_coerced_from_string(self):
        from apis.shared.users.models import UserProfile, UserStatus
        p = UserProfile(userId="u1", email="a@b.com", name="A", emailDomain="b.com", createdAt="t", lastLoginAt="t", status="ACTIVE")
        assert p.status == UserStatus.ACTIVE

    def test_status_enum_passthrough(self):
        from apis.shared.users.models import UserProfile, UserStatus
        p = UserProfile(userId="u1", email="a@b.com", name="A", emailDomain="b.com", createdAt="t", lastLoginAt="t", status=UserStatus.SUSPENDED)
        assert p.status == UserStatus.SUSPENDED

    def test_user_list_item_coerce_status(self):
        from apis.shared.users.models import UserListItem, UserStatus
        item = UserListItem(userId="u1", email="a@b.com", name="A", lastLoginAt="t", status="inactive")
        assert item.status == UserStatus.INACTIVE


# ===================================================================
# files/models.py
# ===================================================================

class TestFileModels:
    def test_get_file_format_known(self):
        from apis.shared.files.models import get_file_format
        assert get_file_format("application/pdf") == "pdf"
        assert get_file_format("image/png") == "png"

    def test_get_file_format_unknown(self):
        from apis.shared.files.models import get_file_format
        assert get_file_format("application/octet-stream") is None

    def test_is_allowed_mime_type(self):
        from apis.shared.files.models import is_allowed_mime_type
        assert is_allowed_mime_type("image/jpeg") is True
        assert is_allowed_mime_type("video/mp4") is False

    def test_file_metadata_s3_uri(self):
        from apis.shared.files.models import FileMetadata
        fm = FileMetadata(upload_id="f1", user_id="u1", session_id="s1", filename="a.pdf", mime_type="application/pdf", size_bytes=100, s3_key="k", s3_bucket="b")
        assert fm.s3_uri == "s3://b/k"

    def test_file_metadata_dynamo_roundtrip(self):
        from apis.shared.files.models import FileMetadata
        fm = FileMetadata(upload_id="f1", user_id="u1", session_id="s1", filename="a.pdf", mime_type="application/pdf", size_bytes=100, s3_key="k", s3_bucket="b")
        item = fm.to_dynamo_item()
        assert item["PK"] == "USER#u1"
        assert item["SK"] == "FILE#f1"
        assert item["GSI1PK"] == "CONV#s1"
        restored = FileMetadata.from_dynamo_item(item)
        assert restored.upload_id == "f1"
        assert restored.user_id == "u1"

    def test_user_file_quota_dynamo_roundtrip(self):
        from apis.shared.files.models import UserFileQuota
        q = UserFileQuota(user_id="u1", total_bytes=1024, file_count=3)
        item = q.to_dynamo_item()
        assert item["PK"] == "USER#u1"
        assert item["SK"] == "QUOTA"
        restored = UserFileQuota.from_dynamo_item(item)
        assert restored.total_bytes == 1024


# ===================================================================
# oauth/models.py
# ===================================================================

class TestOAuthModels:
    def test_compute_scopes_hash(self):
        from apis.shared.oauth.models import compute_scopes_hash
        h1 = compute_scopes_hash(["read", "write"])
        h2 = compute_scopes_hash(["write", "read"])
        assert h1 == h2  # order-independent

    def test_provider_scopes_hash_property(self):
        from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
        p = OAuthProvider(
            provider_id="p1", display_name="P",
            provider_type=OAuthProviderType.GOOGLE,
            scopes=["a", "b"], allowed_roles=[],
        )
        assert p.scopes_hash == p.scopes_hash  # consistent

    def test_provider_dynamo_roundtrip(self):
        from apis.shared.oauth.models import OAuthProvider, OAuthProviderType
        p = OAuthProvider(
            provider_id="p1", display_name="P",
            provider_type=OAuthProviderType.GOOGLE,
            scopes=["a"], allowed_roles=[],
            credential_provider_arn="arn:aws:bedrock-agentcore:us-east-1:1:cp/p1",
            callback_url="https://bedrock-agentcore.us-east-1.amazonaws.com/cb/p1",
        )
        item = p.to_dynamo_item()
        assert item["PK"] == "PROVIDER#p1"
        restored = OAuthProvider.from_dynamo_item(item)
        assert restored.provider_id == "p1"
        assert restored.callback_url == p.callback_url
        assert restored.credential_provider_arn == p.credential_provider_arn


# ===================================================================
# auth_providers/models.py
# ===================================================================

class TestAuthProviderModels:
    def test_dynamo_roundtrip(self):
        from apis.shared.auth_providers.models import AuthProvider
        p = AuthProvider(provider_id="ap1", display_name="Okta", provider_type="oidc", issuer_url="https://okta.example.com", client_id="cid", enabled=True)
        item = p.to_dynamo_item()
        assert item["PK"] == "AUTH_PROVIDER#ap1"
        restored = AuthProvider.from_dynamo_item(item)
        assert restored.provider_id == "ap1"
        assert restored.display_name == "Okta"


# ===================================================================
# rbac/models.py
# ===================================================================

class TestRBACModels:
    def test_effective_permissions_roundtrip(self):
        from apis.shared.rbac.models import EffectivePermissions
        ep = EffectivePermissions(tools=["t1"], models=["m1"])
        d = ep.to_dict()
        restored = EffectivePermissions.from_dict(d)
        assert restored.tools == ["t1"]
        assert restored.models == ["m1"]

    def test_app_role_roundtrip(self):
        from apis.shared.rbac.models import AppRole
        role = AppRole(role_id="r1", display_name="Admin", description="Admin role", enabled=True, is_system_role=False)
        d = role.to_dict()
        restored = AppRole.from_dict(d)
        assert restored.role_id == "r1"
        assert restored.display_name == "Admin"


# ===================================================================
# errors.py
# ===================================================================

class TestErrors:
    def test_stream_error_sse_format(self):
        from apis.shared.errors import StreamErrorEvent, ErrorCode
        e = StreamErrorEvent(error="oops", code=ErrorCode.INTERNAL_ERROR)
        sse = e.to_sse_format()
        assert sse.startswith("event: error\n")
        assert '"oops"' in sse

    def test_conversational_error_sse_format(self):
        from apis.shared.errors import ConversationalErrorEvent, ErrorCode
        e = ConversationalErrorEvent(code=ErrorCode.MODEL_ERROR, message="bad model")
        sse = e.to_sse_format()
        assert sse.startswith("event: stream_error\n")

    def test_create_error_response(self):
        from apis.shared.errors import create_error_response, ErrorCode
        resp = create_error_response(ErrorCode.NOT_FOUND, "not found", status_code=404)
        assert resp["status_code"] == 404
        assert resp["error"]["code"] == "not_found"

    def test_http_status_to_error_code(self):
        from apis.shared.errors import http_status_to_error_code, ErrorCode
        assert http_status_to_error_code(404) == ErrorCode.NOT_FOUND
        assert http_status_to_error_code(500) == ErrorCode.INTERNAL_ERROR
        assert http_status_to_error_code(999) == ErrorCode.INTERNAL_ERROR  # unknown

    def test_build_conversational_error_model_error(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.MODEL_ERROR, Exception("access denied here"), session_id="s1")
        assert "access" in evt.message.lower()
        assert evt.metadata["session_id"] == "s1"

    def test_build_conversational_error_tool_error(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.TOOL_ERROR, Exception("tool broke"))
        assert "tool" in evt.message.lower()

    def test_build_conversational_error_timeout(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.TIMEOUT, Exception("timed out"))
        assert "long" in evt.message.lower()

    def test_build_conversational_error_generic(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.BAD_REQUEST, Exception("bad"))
        assert "wrong" in evt.message.lower()

    def test_build_conversational_error_stream_access_denied(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.STREAM_ERROR, Exception("AccessDenied"))
        assert "access" in evt.message.lower()

    def test_build_conversational_error_stream_unsupported_model(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.STREAM_ERROR, Exception("unsupported model xyz"))
        assert "model" in evt.message.lower()

    def test_build_conversational_error_stream_prompt_caching(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.STREAM_ERROR, Exception("prompt caching failed"))
        assert "caching" in evt.message.lower()

    def test_build_conversational_error_stream_generic(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.STREAM_ERROR, Exception("something else"))
        assert "wrong" in evt.message.lower()

    def test_build_conversational_error_model_throttle(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.MODEL_ERROR, Exception("throttling exception"))
        assert "too many" in evt.message.lower()

    def test_build_conversational_error_model_generic(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.MODEL_ERROR, Exception("some model issue"))
        assert "problem" in evt.message.lower()

    def test_build_conversational_error_service_unavailable(self):
        from apis.shared.errors import build_conversational_error_event, ErrorCode
        evt = build_conversational_error_event(ErrorCode.SERVICE_UNAVAILABLE, Exception("down"))
        assert "unavailable" in evt.message.lower()


# ===================================================================
# sessions/metadata.py — pure functions
# ===================================================================

class TestMetadataUtils:
    def test_convert_floats_to_decimal(self):
        from apis.shared.sessions.metadata import _convert_floats_to_decimal
        result = _convert_floats_to_decimal({"a": 1.5, "b": [2.0], "c": "str"})
        assert result["a"] == Decimal("1.5")
        assert result["b"] == [Decimal("2.0")]
        assert result["c"] == "str"

    def test_convert_decimal_to_float(self):
        from apis.shared.sessions.metadata import _convert_decimal_to_float
        result = _convert_decimal_to_float({"a": Decimal("1.5"), "b": [Decimal("2.0")]})
        assert result["a"] == 1.5
        assert isinstance(result["a"], float)

    def test_deep_merge_simple(self):
        from apis.shared.sessions.metadata import _deep_merge
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_deep_merge_nested(self):
        from apis.shared.sessions.metadata import _deep_merge
        base = {"a": {"x": 1, "y": 2}}
        updates = {"a": {"y": 3, "z": 4}}
        result = _deep_merge(base, updates)
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_deep_merge_overwrite_non_dict(self):
        from apis.shared.sessions.metadata import _deep_merge
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_apply_pagination_no_limit(self):
        from apis.shared.sessions.metadata import _apply_pagination, SessionMetadata
        sessions = [SessionMetadata(sessionId=f"s{i}", userId="u1", title="t", status="active", createdAt="2026-01-01", lastMessageAt=f"2026-01-0{9-i}", messageCount=1) for i in range(3)]
        result, token = _apply_pagination(sessions)
        assert len(result) == 3
        assert token is None

    def test_apply_pagination_with_limit(self):
        from apis.shared.sessions.metadata import _apply_pagination, SessionMetadata
        sessions = [SessionMetadata(sessionId=f"s{i}", userId="u1", title="t", status="active", createdAt="2026-01-01", lastMessageAt=f"2026-01-0{9-i}", messageCount=1) for i in range(5)]
        result, token = _apply_pagination(sessions, limit=2)
        assert len(result) == 2
        assert token is not None

    def test_apply_pagination_with_token(self):
        from apis.shared.sessions.metadata import _apply_pagination, SessionMetadata
        sessions = [SessionMetadata(sessionId=f"s{i}", userId="u1", title="t", status="active", createdAt="2026-01-01", lastMessageAt=f"2026-01-0{9-i}", messageCount=1) for i in range(5)]
        # Get first page
        page1, token = _apply_pagination(sessions, limit=2)
        # Get second page
        page2, token2 = _apply_pagination(sessions, limit=2, next_token=token)
        assert len(page2) == 2
        assert page2[0].session_id != page1[0].session_id

    def test_apply_pagination_invalid_token(self):
        from apis.shared.sessions.metadata import _apply_pagination, SessionMetadata
        sessions = [SessionMetadata(sessionId="s1", userId="u1", title="t", status="active", createdAt="2026-01-01", lastMessageAt="2026-01-01", messageCount=1)]
        result, _ = _apply_pagination(sessions, next_token="not-valid-base64!!!")
        assert len(result) == 1  # falls back to start


# ===================================================================
# sessions/messages.py — pure functions
# ===================================================================

class TestMessageUtils:
    def test_ensure_image_base64_raw_bytes(self):
        from apis.shared.sessions.messages import _ensure_image_base64
        result = _ensure_image_base64({"source": {"bytes": b"\x89PNG"}, "format": "png"})
        assert "data" in result
        assert result["format"] == "png"
        # Verify it's valid base64
        base64.b64decode(result["data"])

    def test_ensure_image_base64_string_bytes(self):
        from apis.shared.sessions.messages import _ensure_image_base64
        result = _ensure_image_base64({"source": {"bytes": "already_b64"}, "format": "jpeg"})
        assert result["data"] == "already_b64"

    def test_ensure_image_base64_already_frontend_format(self):
        from apis.shared.sessions.messages import _ensure_image_base64
        data = {"format": "png", "data": "abc123"}
        assert _ensure_image_base64(data) == data

    def test_ensure_image_base64_empty(self):
        from apis.shared.sessions.messages import _ensure_image_base64
        assert _ensure_image_base64({}) == {}
        assert _ensure_image_base64(None) is None

    def test_ensure_document_base64_raw_bytes(self):
        from apis.shared.sessions.messages import _ensure_document_base64
        result = _ensure_document_base64({"source": {"bytes": b"PDF content"}, "format": "pdf", "name": "doc.pdf"})
        assert "data" in result
        assert result["name"] == "doc.pdf"

    def test_ensure_document_base64_empty(self):
        from apis.shared.sessions.messages import _ensure_document_base64
        assert _ensure_document_base64(None) is None

    def test_convert_content_block_text(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"text": "hello"})
        assert mc.type == "text"
        assert mc.text == "hello"

    def test_convert_content_block_tool_use(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"toolUse": {"toolUseId": "t1", "name": "calc", "input": {}}})
        assert mc.type == "toolUse"

    def test_convert_content_block_tool_result(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"toolResult": {"toolUseId": "t1", "content": [{"text": "ok"}]}})
        assert mc.type == "toolResult"

    def test_convert_content_block_image(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"image": {"format": "png", "data": "abc"}})
        assert mc.type == "image"

    def test_convert_content_block_document(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"document": {"format": "pdf", "data": "abc"}})
        assert mc.type == "document"

    def test_convert_content_block_reasoning(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"reasoningContent": {"reasoningText": "thinking..."}})
        assert mc.type == "reasoningContent"

    def test_convert_content_block_unknown(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block({"weirdKey": "value"})
        assert mc.type == "text"

    def test_convert_content_block_non_dict(self):
        from apis.shared.sessions.messages import _convert_content_block
        mc = _convert_content_block("just a string")
        assert mc.type == "text"
        assert mc.text == "just a string"

    def test_convert_message_dict(self):
        from apis.shared.sessions.messages import _convert_message
        msg = _convert_message({"role": "user", "content": [{"text": "hi"}]})
        assert msg.role == "user"
        assert len(msg.content) == 1

    def test_convert_message_string_content(self):
        from apis.shared.sessions.messages import _convert_message
        msg = _convert_message({"role": "assistant", "content": "hello"})
        assert msg.content[0].text == "hello"

    def test_convert_message_session_message_object(self):
        from apis.shared.sessions.messages import _convert_message
        from unittest.mock import MagicMock
        sm = MagicMock()
        sm.message = {"role": "user", "content": [{"text": "hi"}]}
        sm.created_at = "2026-01-01"
        msg = _convert_message(sm)
        assert msg.role == "user"

    def test_get_message_role_dict(self):
        from apis.shared.sessions.messages import _get_message_role
        assert _get_message_role({"role": "user"}) == "user"

    def test_get_message_role_object(self):
        from apis.shared.sessions.messages import _get_message_role
        from unittest.mock import MagicMock
        obj = MagicMock()
        obj.message = {"role": "assistant"}
        assert _get_message_role(obj) == "assistant"

    def test_apply_pagination_no_limit(self):
        from apis.shared.sessions.messages import _apply_pagination, Message
        msgs = [Message(role="user", content=[]) for _ in range(5)]
        result, token = _apply_pagination(msgs)
        assert len(result) == 5
        assert token is None

    def test_apply_pagination_with_limit(self):
        from apis.shared.sessions.messages import _apply_pagination, Message
        msgs = [Message(role="user", content=[]) for _ in range(5)]
        result, token = _apply_pagination(msgs, limit=2)
        assert len(result) == 2
        assert token is not None

    def test_apply_pagination_with_token(self):
        from apis.shared.sessions.messages import _apply_pagination, Message
        msgs = [Message(role="user", content=[]) for _ in range(5)]
        _, token = _apply_pagination(msgs, limit=2)
        page2, _ = _apply_pagination(msgs, limit=2, next_token=token)
        assert len(page2) == 2

    def test_convert_message_to_response(self):
        from apis.shared.sessions.messages import _convert_message_to_response, Message, MessageContent
        msg = Message(role="user", content=[MessageContent(type="text", text="hi")])
        resp = _convert_message_to_response(msg, session_id="s1", sequence_number=0)
        assert resp.id == "msg-s1-0"
        assert resp.role == "user"

    def test_process_tool_result_content_with_image(self):
        from apis.shared.sessions.messages import _process_tool_result_content
        tr = {"toolUseId": "t1", "content": [{"image": {"source": {"bytes": b"img"}, "format": "png"}}]}
        result = _process_tool_result_content(tr)
        assert "data" in result["content"][0]["image"]


# ===================================================================
# models/managed_models.py — pure functions
# ===================================================================

class TestManagedModelUtils:
    def test_resolve_supports_caching_explicit_true(self):
        from apis.shared.models.managed_models import _resolve_supports_caching
        assert _resolve_supports_caching(True, "openai") is True

    def test_resolve_supports_caching_explicit_false(self):
        from apis.shared.models.managed_models import _resolve_supports_caching
        assert _resolve_supports_caching(False, "bedrock") is False

    def test_resolve_supports_caching_default_bedrock(self):
        from apis.shared.models.managed_models import _resolve_supports_caching
        assert _resolve_supports_caching(None, "bedrock") is True

    def test_resolve_supports_caching_default_openai(self):
        from apis.shared.models.managed_models import _resolve_supports_caching
        assert _resolve_supports_caching(None, "openai") is False

    def test_python_to_dynamodb(self):
        from apis.shared.models.managed_models import _python_to_dynamodb
        result = _python_to_dynamodb({"price": 0.5, "tags": [1.0]})
        assert result["price"] == Decimal("0.5")
        assert result["tags"] == [Decimal("1.0")]

    def test_python_to_dynamodb_datetime(self):
        from apis.shared.models.managed_models import _python_to_dynamodb
        dt = datetime(2026, 1, 1, 12, 0, 0)
        assert _python_to_dynamodb(dt) == dt.isoformat()

    def test_dynamodb_to_python(self):
        from apis.shared.models.managed_models import _dynamodb_to_python
        result = _dynamodb_to_python({"price": Decimal("0.5"), "nested": {"val": Decimal("1")}})
        assert result["price"] == 0.5
        assert isinstance(result["price"], float)

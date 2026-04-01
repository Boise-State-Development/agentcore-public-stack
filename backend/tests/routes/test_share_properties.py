"""Property-based tests for the share conversations feature.

Uses Hypothesis to verify invariants across randomly generated inputs.
Each test maps to a design property from the share-conversations spec.

Feature: share-conversations
"""

from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

from apis.app_api.shares.models import (
    CreateShareRequest,
    ShareResponse,
    UpdateShareRequest,
)
from apis.app_api.shares.routes import conversations_share_router, shares_router
from apis.app_api.shares.service import (
    AccessDeniedError,
    NotOwnerError,
    ShareNotFoundError,
    ShareService,
)
from apis.shared.auth.models import User
from apis.shared.sessions.models import MessageResponse

from tests.routes.conftest import mock_auth_user


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

st_email = st.emails()

st_user_id = st.uuids().map(str)

st_share_id = st.uuids().map(str)

st_session_id = st.uuids().map(str)

st_access_level = st.sampled_from(["public", "specific"])

st_iso_timestamp = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 1, 1),
).map(lambda dt: dt.replace(tzinfo=timezone.utc).isoformat())

st_title = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Z")),
    min_size=1,
    max_size=100,
)

st_message_content = st.fixed_dictionaries({
    "type": st.just("text"),
    "text": st.text(min_size=1, max_size=200),
})

st_message = st.fixed_dictionaries({
    "id": st.uuids().map(str),
    "role": st.sampled_from(["user", "assistant"]),
    "content": st.lists(st_message_content, min_size=1, max_size=3),
    "createdAt": st_iso_timestamp,
})

st_email_list = st.lists(st_email, min_size=1, max_size=10)


@st.composite
def st_share_response(draw):
    """Generate a random valid ShareResponse."""
    access = draw(st_access_level)
    emails = draw(st_email_list) if access == "specific" else None
    return ShareResponse(
        share_id=draw(st_share_id),
        session_id=draw(st_session_id),
        owner_id=draw(st_user_id),
        access_level=access,
        allowed_emails=emails,
        created_at=draw(st_iso_timestamp),
        share_url=f"/shared/{draw(st_share_id)}",
    )


# ---------------------------------------------------------------------------
# Property 2: Owner email auto-inclusion invariant
# Validates: Requirements 1.2, 4.2, 4.5
# ---------------------------------------------------------------------------

class TestOwnerEmailAutoInclusion:
    """Property 2: Owner email auto-inclusion invariant.

    For any create or update where access_level is 'specific',
    the resolved allowed_emails always contains the owner's email.
    """

    @given(
        owner_email=st_email,
        input_emails=st.lists(st_email, min_size=0, max_size=10),
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_owner_always_in_allowed_emails(self, owner_email, input_emails):
        """Feature: share-conversations, Property 2: Owner email auto-inclusion invariant

        Validates: Requirements 1.2, 4.2, 4.5
        """
        result = ShareService._resolve_allowed_emails(
            access_level="specific",
            allowed_emails=input_emails,
            owner_email=owner_email,
        )

        assert result is not None
        assert owner_email.lower() in [e.lower() for e in result]

    @given(owner_email=st_email)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_owner_included_even_when_emails_empty(self, owner_email):
        """Feature: share-conversations, Property 2: Owner email auto-inclusion (empty list)

        Validates: Requirements 1.2, 4.2, 4.5
        """
        result = ShareService._resolve_allowed_emails(
            access_level="specific",
            allowed_emails=[],
            owner_email=owner_email,
        )

        assert result is not None
        assert owner_email in result

    @given(owner_email=st_email)
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_owner_included_when_emails_none(self, owner_email):
        """Feature: share-conversations, Property 2: Owner email auto-inclusion (None)

        Validates: Requirements 1.2, 4.2, 4.5
        """
        result = ShareService._resolve_allowed_emails(
            access_level="specific",
            allowed_emails=None,
            owner_email=owner_email,
        )

        assert result is not None
        assert owner_email in result


# ---------------------------------------------------------------------------
# Property 3: "Specific" access requires non-empty allowed_emails
# Validates: Requirements 1.3, 4.3
# ---------------------------------------------------------------------------

class TestSpecificRequiresEmails:
    """Property 3: 'Specific' access requires non-empty allowed_emails.

    For any create/update with access_level 'specific' and empty/missing
    allowed_emails, the Pydantic model raises a validation error.
    """

    @given(data=st.data())
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_create_specific_empty_emails_raises(self, data):
        """Feature: share-conversations, Property 3: Specific access requires non-empty allowed_emails (create)

        Validates: Requirements 1.3, 4.3
        """
        empty_emails = data.draw(st.sampled_from([[], None]))

        with pytest.raises(ValueError, match="allowed_emails is required"):
            CreateShareRequest(
                access_level="specific",
                allowed_emails=empty_emails,
            )

    @given(data=st.data())
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_update_specific_empty_emails_raises(self, data):
        """Feature: share-conversations, Property 3: Specific access requires non-empty allowed_emails (update)

        Validates: Requirements 1.3, 4.3
        """
        empty_emails = data.draw(st.sampled_from([[], None]))

        with pytest.raises(ValueError, match="allowed_emails is required"):
            UpdateShareRequest(
                access_level="specific",
                allowed_emails=empty_emails,
            )


# ---------------------------------------------------------------------------
# Property 5: Access control matrix
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
# ---------------------------------------------------------------------------

class TestAccessControlMatrix:
    """Property 5: Access control matrix.

    For any shared conversation and any authenticated requesting user,
    access is granted/denied according to the access_level rules.
    """

    @given(
        owner_id=st_user_id,
        requester_id=st_user_id,
        requester_email=st_email,
        access_level=st_access_level,
        allowed_emails=st.lists(st_email, min_size=0, max_size=5),
    )
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_access_control_matches_matrix(
        self, owner_id, requester_id, requester_email, access_level, allowed_emails
    ):
        """Feature: share-conversations, Property 5: Access control matrix

        Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5
        """
        service = ShareService.__new__(ShareService)

        item = {
            "share_id": "test-share",
            "owner_id": owner_id,
            "access_level": access_level,
            "allowed_emails": allowed_emails,
        }

        requester = User(
            email=requester_email,
            user_id=requester_id,
            name="Test",
            roles=["User"],
        )

        is_owner = requester_id == owner_id
        email_in_allowed = requester_email.lower() in [e.lower() for e in allowed_emails]

        # Determine expected outcome
        if is_owner:
            should_allow = True
        elif access_level == "public":
            should_allow = True
        elif access_level == "specific":
            should_allow = email_in_allowed
        else:
            should_allow = False

        if should_allow:
            # Should not raise
            service._check_access(item, requester)
        else:
            with pytest.raises(AccessDeniedError):
                service._check_access(item, requester)


# ---------------------------------------------------------------------------
# Property 8: Non-specific access levels clear allowed_emails
# Validates: Requirements 4.1, 4.4
# ---------------------------------------------------------------------------

class TestNonSpecificClearsEmails:
    """Property 8: Non-specific access levels clear allowed_emails.

    For any access_level that is not 'specific', _resolve_allowed_emails
    returns None.
    """

    @given(
        access_level=st.just("public"),
        emails=st.lists(st_email, min_size=0, max_size=5),
        owner_email=st_email,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_non_specific_returns_none(self, access_level, emails, owner_email):
        """Feature: share-conversations, Property 8: Non-specific access levels clear allowed_emails

        Validates: Requirements 4.1, 4.4
        """
        result = ShareService._resolve_allowed_emails(
            access_level=access_level,
            allowed_emails=emails,
            owner_email=owner_email,
        )

        assert result is None


# ---------------------------------------------------------------------------
# Property 9: ShareResponse serialization round-trip
# Validates: Requirements 10.5
# ---------------------------------------------------------------------------

class TestShareResponseRoundTrip:
    """Property 9: ShareResponse serialization round-trip.

    For any valid ShareResponse, serializing to JSON and deserializing
    back produces an equivalent object.
    """

    @given(resp=st_share_response())
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_json_round_trip(self, resp):
        """Feature: share-conversations, Property 9: ShareResponse serialization round-trip

        Validates: Requirements 10.5
        """
        json_str = resp.model_dump_json(by_alias=True)
        restored = ShareResponse.model_validate_json(json_str)

        assert restored.share_id == resp.share_id
        assert restored.session_id == resp.session_id
        assert restored.owner_id == resp.owner_id
        assert restored.access_level == resp.access_level
        assert restored.allowed_emails == resp.allowed_emails
        assert restored.created_at == resp.created_at
        assert restored.share_url == resp.share_url


# ---------------------------------------------------------------------------
# Property 4: Non-owner operations return 403
# Validates: Requirements 1.5, 3.2, 4.6
# ---------------------------------------------------------------------------

class TestNonOwnerOperationsReturn403:
    """Property 4: Non-owner operations return 403.

    For any share operation attempted by a non-owner, the API returns 403.
    """

    @given(
        owner_id=st_user_id,
        requester_id=st_user_id,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_non_owner_create_returns_403(self, owner_id, requester_id):
        """Feature: share-conversations, Property 4: Non-owner operations return 403 (create)

        Validates: Requirements 1.5, 3.2, 4.6
        """
        assume(owner_id != requester_id)

        app = FastAPI()
        app.include_router(conversations_share_router)

        requester = User(
            email="requester@example.com",
            user_id=requester_id,
            name="Requester",
            roles=["User"],
        )
        mock_auth_user(app, requester)

        mock_svc = AsyncMock()
        mock_svc.create_share = AsyncMock(side_effect=NotOwnerError())
        with patch("apis.app_api.shares.routes.get_share_service", return_value=mock_svc):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/conversations/sess-001/share",
                json={"accessLevel": "public"},
            )

        assert resp.status_code == 403

    @given(
        owner_id=st_user_id,
        requester_id=st_user_id,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_non_owner_revoke_returns_403(self, owner_id, requester_id):
        """Feature: share-conversations, Property 4: Non-owner operations return 403 (revoke)

        Validates: Requirements 1.5, 3.2, 4.6
        """
        assume(owner_id != requester_id)

        app = FastAPI()
        app.include_router(shares_router)

        requester = User(
            email="requester@example.com",
            user_id=requester_id,
            name="Requester",
            roles=["User"],
        )
        mock_auth_user(app, requester)

        mock_svc = AsyncMock()
        mock_svc.revoke_share = AsyncMock(side_effect=NotOwnerError())
        with patch("apis.app_api.shares.routes.get_share_service", return_value=mock_svc):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.delete("/shares/share-001")

        assert resp.status_code == 403

    @given(
        owner_id=st_user_id,
        requester_id=st_user_id,
    )
    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def test_non_owner_update_returns_403(self, owner_id, requester_id):
        """Feature: share-conversations, Property 4: Non-owner operations return 403 (update)

        Validates: Requirements 1.5, 3.2, 4.6
        """
        assume(owner_id != requester_id)

        app = FastAPI()
        app.include_router(shares_router)

        requester = User(
            email="requester@example.com",
            user_id=requester_id,
            name="Requester",
            roles=["User"],
        )
        mock_auth_user(app, requester)

        mock_svc = AsyncMock()
        mock_svc.update_share = AsyncMock(side_effect=NotOwnerError())
        with patch("apis.app_api.shares.routes.get_share_service", return_value=mock_svc):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.patch(
                "/shares/share-001",
                json={"accessLevel": "public"},
            )

        assert resp.status_code == 403

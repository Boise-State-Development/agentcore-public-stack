""""Run now" — the attended validation surface for the headless harness.

``POST /runs/now`` executes one agent turn *through the exact machinery a
scheduled run will use* (headless grant → per-owner Cognito mint → runtime
``/invocations`` → server-side SSE drain → governance floor → session
materialization) while the user is present to watch it. It deliberately
does NOT shortcut through the caller's live access token: the point of the
surface is to validate the unattended path end-to-end (scheduled-runs PR-1,
docs/specs/scheduled-agent-runs.md §7).

Gating — two independent controls (spec §6):

* ``SCHEDULED_RUNS_ENABLED`` — per-environment kill switch (default on).
  Off → every route here 404s, as if unmounted.
* ``scheduled-runs`` RBAC capability — *who* may use the surface. Granted
  to the beta cohort's AppRole; missing → 403. GA = grant to ``default``.

Auth is the standard SPA cookie dependency (``get_current_user_from_session``)
per the CLAUDE.md app-api rule. The headless grant is **created-on-enable**:
each attended ``POST /runs/now`` pins the caller's live session refresh
token into their grant record (renewing the 30-day login-recency window);
``GET/DELETE /runs/grant`` expose status and revocation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import scheduled_runs_enabled
from apis.shared.harness import (
    CognitoRefreshBearerAuth,
    HeadlessAuthError,
    HeadlessGrant,
    HeadlessGrantService,
    RunResult,
    run_agent_headless,
)
from apis.shared.rbac.capabilities import (
    SCHEDULED_RUNS_CAPABILITY,
    user_has_capability,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])

_MAX_PROMPT_CHARS = 20_000

_grant_service: Optional[HeadlessGrantService] = None


def get_headless_grant_service() -> HeadlessGrantService:
    """Lazy module singleton; tests monkeypatch this factory."""
    global _grant_service
    if _grant_service is None:
        _grant_service = HeadlessGrantService()
    return _grant_service


async def require_scheduled_runs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + kill switch + cohort capability, in that order.

    404 when the environment kill switch is off (the surface behaves as if
    unmounted — runtime-checked so tests and env flips need no module
    reload), 403 when the authenticated caller lacks the ``scheduled-runs``
    capability.
    """
    if not scheduled_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not await user_has_capability(user, SCHEDULED_RUNS_CAPABILITY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to scheduled runs.",
        )
    return user


# ─── API models ────────────────────────────────────────────────────────────


class RunNowRequest(BaseModel):
    """Run-config mirrors ``InvocationRequest`` — no new config type."""

    prompt: str = Field(..., min_length=1, max_length=_MAX_PROMPT_CHARS)
    title: Optional[str] = Field(None, max_length=200)
    model_id: Optional[str] = Field(None, alias="modelId")
    rag_assistant_id: Optional[str] = Field(None, alias="ragAssistantId")
    # None = the user's defaults (all RBAC-allowed tools), exactly as an
    # attended chat turn resolves them — see run_agent_headless docstring.
    enabled_tools: Optional[List[str]] = Field(None, alias="enabledTools")
    agent_type: Optional[str] = Field(None, alias="agentType")

    model_config = {"populate_by_name": True}


class ToolTraceEntryResponse(BaseModel):
    tool_use_id: str = Field(..., alias="toolUseId")
    name: str
    input: Dict[str, Any] = Field(default_factory=dict)
    result_preview: Optional[str] = Field(None, alias="resultPreview")
    is_error: bool = Field(False, alias="isError")

    model_config = {"populate_by_name": True}


class OAuthConsentRequiredResponse(BaseModel):
    provider_id: str = Field(..., alias="providerId")
    authorization_url: str = Field(..., alias="authorizationUrl")

    model_config = {"populate_by_name": True}


class RunNowResponse(BaseModel):
    run_id: str = Field(..., alias="runId")
    session_id: str = Field(..., alias="sessionId")
    status: str
    final_message: str = Field("", alias="finalMessage")
    stop_reason: Optional[str] = Field(None, alias="stopReason")
    error: Optional[str] = None
    title: Optional[str] = None
    tool_trace: List[ToolTraceEntryResponse] = Field(
        default_factory=list, alias="toolTrace"
    )
    usage: Dict[str, Any] = Field(default_factory=dict)
    oauth_required: List[OAuthConsentRequiredResponse] = Field(
        default_factory=list, alias="oauthRequired"
    )
    started_at: str = Field("", alias="startedAt")
    finished_at: str = Field("", alias="finishedAt")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_run_result(cls, result: RunResult) -> "RunNowResponse":
        return cls(
            run_id=result.run_id,
            session_id=result.session_id,
            status=result.status,
            final_message=result.final_message,
            stop_reason=result.stop_reason,
            error=result.error,
            title=result.title,
            tool_trace=[
                ToolTraceEntryResponse(
                    tool_use_id=t.tool_use_id,
                    name=t.name,
                    input=t.input,
                    result_preview=t.result_preview,
                    is_error=t.is_error,
                )
                for t in result.tool_trace
            ],
            usage=result.usage,
            oauth_required=[
                OAuthConsentRequiredResponse(
                    provider_id=o.provider_id,
                    authorization_url=o.authorization_url,
                )
                for o in result.oauth_required
            ],
            started_at=result.started_at,
            finished_at=result.finished_at,
        )


class GrantStatusResponse(BaseModel):
    enabled: bool
    grant_id: Optional[str] = Field(None, alias="grantId")
    created_at: Optional[int] = Field(None, alias="createdAt")
    updated_at: Optional[int] = Field(None, alias="updatedAt")
    expires_at: Optional[int] = Field(None, alias="expiresAt")
    last_used_at: Optional[int] = Field(None, alias="lastUsedAt")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_grant(cls, grant: Optional[HeadlessGrant]) -> "GrantStatusResponse":
        if grant is None:
            return cls(enabled=False)
        return cls(
            enabled=True,
            grant_id=grant.grant_id,
            created_at=grant.created_at,
            updated_at=grant.updated_at,
            expires_at=grant.ttl,
            last_used_at=grant.last_used_at,
        )


class GrantRevokeResponse(BaseModel):
    revoked: bool


# ─── Routes ─────────────────────────────────────────────────────────────────


async def _resolve_grant(request: Request, user: User) -> HeadlessGrant:
    """Create-on-enable: pin the live session's token, else use an existing grant.

    The BFF middleware attaches the caller's ``SessionRecord`` to
    ``request.state.bff_session``; when present, the grant is created or
    renewed from it (that session's ``created_at`` anchors the 30-day
    login-recency window — see ``apis.shared.harness.grants``). Without a
    session record (e.g. local SKIP_AUTH dev) an already-active grant still
    works; having neither is a 409, not a 401 — the caller *is*
    authenticated, they just have no credential the platform may act
    headlessly with.
    """
    grants = get_headless_grant_service()
    session_record = getattr(request.state, "bff_session", None)
    if session_record is not None:
        return await grants.enable(
            user_id=user.user_id,
            username=session_record.username,
            refresh_token=session_record.cognito_refresh_token,
            token_issued_at=session_record.created_at,
        )
    grant = await grants.get_active_grant(user.user_id)
    if grant is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No headless grant exists for this account and the current "
                "request carries no session to create one from."
            ),
        )
    return grant


@router.post("/now", response_model=RunNowResponse, response_model_by_alias=True)
async def run_now(
    body: RunNowRequest,
    request: Request,
    user: User = Depends(require_scheduled_runs_user),
) -> RunNowResponse:
    """Execute one agent turn as the caller through the headless harness.

    Synchronous from the caller's perspective: the response is the full
    ``RunResult`` once the turn drains (bounded by the harness's 300s
    budget, matching the chat proxy). The result also lands as a session in
    the caller's conversation list, so a closed tab loses nothing.
    """
    await _resolve_grant(request, user)

    try:
        result = await run_agent_headless(
            user_id=user.user_id,
            prompt=body.prompt,
            auth=CognitoRefreshBearerAuth(grants=get_headless_grant_service()),
            title=body.title,
            model_id=body.model_id,
            rag_assistant_id=body.rag_assistant_id,
            enabled_tools=body.enabled_tools,
            agent_type=body.agent_type,
            trigger="run_now",
        )
    except HeadlessAuthError as exc:
        # The grant exists but could not mint (Cognito refused — token
        # expired or revoked upstream). 409, not 401: a 401 here would
        # bounce the SPA through the login redirect even though the
        # *session* is fine.
        logger.warning("Run-now mint failed for user %s: %s", user.user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Headless credential could not be minted; log in again to renew it.",
        )

    logger.info(
        "Run-now %s for user %s finished status=%s session=%s",
        result.run_id,
        user.user_id,
        result.status,
        result.session_id,
    )
    return RunNowResponse.from_run_result(result)


@router.get(
    "/grant", response_model=GrantStatusResponse, response_model_by_alias=True
)
async def get_grant_status(
    user: User = Depends(require_scheduled_runs_user),
) -> GrantStatusResponse:
    """The caller's headless-grant status (never the token itself)."""
    grant = await get_headless_grant_service().get_active_grant(user.user_id)
    return GrantStatusResponse.from_grant(grant)


@router.delete("/grant", response_model=GrantRevokeResponse)
async def revoke_grant(
    user: User = Depends(require_scheduled_runs_user),
) -> GrantRevokeResponse:
    """Revoke the caller's headless grant (total revocation — the stored
    credential is deleted in the same write)."""
    revoked = await get_headless_grant_service().revoke(user.user_id)
    return GrantRevokeResponse(revoked=revoked)

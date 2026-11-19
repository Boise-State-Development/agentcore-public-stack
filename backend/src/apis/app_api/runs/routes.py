""""Run now" — the attended validation surface for the headless harness.

``POST /runs/now`` executes one agent turn *through the exact machinery a
scheduled run will use* (headless grant → per-owner Cognito mint → runtime
``/invocations`` → server-side SSE drain → governance floor → session
materialization) while the user is present to watch it. It deliberately
does NOT shortcut through the caller's live access token: the point of the
surface is to validate the unattended path end-to-end (scheduled-runs PR-1,
docs/specs/scheduled-agent-runs.md §7).

Gating — a single per-environment control (spec §6):

* ``SCHEDULED_RUNS_ENABLED`` — kill switch (default on). Off → every route
  here 404s, as if unmounted.

The surface is otherwise open to any authenticated user — deliberately *not*
behind the ``scheduled-runs`` RBAC capability, which turned every non-beta
caller into a 403 the SPA surfaced as an "Access Denied" toast. Each run
still executes with the caller's own RBAC-allowed tools (see
``run_agent_headless``), so this widens *who* can reach the surface, not
*what* any one caller can do.

Auth is the standard SPA cookie dependency (``get_current_user_from_session``)
per the CLAUDE.md app-api rule. The headless grant is **created-on-enable**:
each attended ``POST /runs/now`` pins the caller's live session refresh
token into their grant record (renewing the 30-day login-recency window);
``GET/DELETE /runs/grant`` expose status and revocation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Coroutine, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import scheduled_runs_enabled
from apis.shared.harness import (
    BearerAuthStrategy,
    CognitoRefreshBearerAuth,
    GovernanceFloor,
    HeadlessAuthError,
    HeadlessGrant,
    HeadlessGrantService,
    RunAuditRecorder,
    RunResult,
    StaticBearerAuth,
    run_agent_headless,
)
from apis.shared.rbac.service import get_app_role_service
from apis.shared.scheduled_prompts.models import HEADLESS_SESSION_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])

_MAX_PROMPT_CHARS = 20_000

_RUN_ID_PATTERN = r"^run-[0-9a-f]{12}$"

_grant_service: Optional[HeadlessGrantService] = None
_audit_recorder: Optional[RunAuditRecorder] = None

# Strong references to runs in flight. The event loop holds tasks weakly, so a
# bare ``create_task`` can be garbage-collected mid-run.
_run_tasks: set[asyncio.Task[None]] = set()


def get_headless_grant_service() -> HeadlessGrantService:
    """Lazy module singleton; tests monkeypatch this factory."""
    global _grant_service
    if _grant_service is None:
        _grant_service = HeadlessGrantService()
    return _grant_service


def get_run_audit_recorder() -> RunAuditRecorder:
    """Lazy module singleton; tests monkeypatch this factory."""
    global _audit_recorder
    if _audit_recorder is None:
        _audit_recorder = RunAuditRecorder()
    return _audit_recorder


def get_headless_auth() -> BearerAuthStrategy:
    """Mints the caller's bearer from their headless grant; tests monkeypatch it."""
    return CognitoRefreshBearerAuth(grants=get_headless_grant_service())


def spawn_run(coro: Coroutine[Any, Any, None]) -> None:
    """Start a run in the background; tests monkeypatch this to run it inline."""
    task = asyncio.get_running_loop().create_task(coro)
    _run_tasks.add(task)
    task.add_done_callback(_run_tasks.discard)


async def require_scheduled_runs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + kill switch, in that order.

    404 when the environment kill switch is off (the surface behaves as if
    unmounted — runtime-checked so tests and env flips need no module
    reload). Otherwise any authenticated user may use the surface — it is
    intentionally not behind an RBAC capability (see module docstring).
    """
    if not scheduled_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
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


class RunStartedResponse(BaseModel):
    """``202`` body of ``POST /runs/now``: the ids to poll and to open."""

    run_id: str = Field(..., alias="runId")
    session_id: str = Field(..., alias="sessionId")
    status: str = "started"

    model_config = {"populate_by_name": True}


class RunStatusResponse(BaseModel):
    """``GET /runs/{runId}``: the run's audit row, without the prompt.

    ``status`` is ``started`` while the turn runs, then one of the
    ``RunStatus`` values. The answer itself is in the run's conversation.
    """

    run_id: str = Field(..., alias="runId")
    session_id: str = Field(..., alias="sessionId")
    status: str
    title: Optional[str] = None
    error: Optional[str] = None
    started_at: str = Field("", alias="startedAt")
    finished_at: Optional[str] = Field(None, alias="finishedAt")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_audit_item(cls, item: Dict[str, Any]) -> "RunStatusResponse":
        return cls(
            run_id=item["runId"],
            session_id=item["sessionId"],
            status=item.get("status", "started"),
            title=item.get("title") or None,
            error=item.get("errorDetail") or None,
            started_at=item.get("startedAt", ""),
            finished_at=item.get("finishedAt") or None,
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


class GrantEnableResponse(GrantStatusResponse):
    """Identical shape to the status response. A distinct name documents
    intent at the call site (POST = mutate/enable, GET = read status)."""


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


async def _execute_run(*, governance: GovernanceFloor, **run_kwargs: Any) -> None:
    """The background half of ``POST /runs/now``.

    ``run_agent_headless`` writes the end audit row itself. If it raises
    instead, write an ``error`` end row here so the SPA's poll stops at once
    rather than waiting for the run to count as lost.
    """
    run_id = run_kwargs["run_id"]
    user_id = run_kwargs["user_id"]
    try:
        result = await run_agent_headless(
            **run_kwargs, governance=governance, audit_started=True
        )
    except Exception:
        logger.error("Run-now %s for user %s raised", run_id, user_id, exc_info=True)
        await governance.on_run_end(
            result=RunResult(
                run_id=run_id,
                session_id=run_kwargs["session_id"],
                user_id=user_id,
                status="error",
                error="The run stopped unexpectedly.",
                title=run_kwargs.get("title"),
                finished_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        )
        return
    logger.info(
        "Run-now %s for user %s finished status=%s session=%s",
        result.run_id,
        user_id,
        result.status,
        result.session_id,
    )


@router.post(
    "/now",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunStartedResponse,
    response_model_by_alias=True,
)
async def run_now(
    body: RunNowRequest,
    request: Request,
    user: User = Depends(require_scheduled_runs_user),
) -> RunStartedResponse:
    """Start one agent turn as the caller through the headless harness.

    Returns ``202`` with the run and session ids as soon as the run is
    authorized and audited; the turn itself runs in the background (up to
    the harness's 300s budget, longer than the 60s CloudFront and ALB
    timeouts in front of app-api). Poll ``GET /runs/{runId}`` for the
    outcome; the answer lands as a session in the caller's conversations.

    Everything that can reject the run happens before the ``202``: the
    grant, the RBAC tool filter, the bearer mint, and the start audit row.
    A run lost to an app-api restart keeps a ``started`` row; the SPA
    treats one older than the harness budget as lost.
    """
    await _resolve_grant(request, user)

    # The request body is attacker-controlled within the caller's own session,
    # and the downstream tool filter performs no RBAC check — so narrow the
    # requested tools to the caller's actual grant before handing them to the
    # headless harness. ``None`` keeps "resolve to the user's defaults".
    enabled_tools = body.enabled_tools
    if enabled_tools is not None:
        allowed = await get_app_role_service().filter_requested_tools(user, enabled_tools)
        if len(allowed) != len(enabled_tools):
            logger.warning(
                "Run-now dropped %d requested tool(s) outside user %s's RBAC grant",
                len(enabled_tools) - len(allowed),
                user.user_id,
            )
        enabled_tools = allowed

    # Mint here, not in the background, so a dead credential is still a 409
    # the form can show. A Cognito access token outlives the 300s budget.
    try:
        bearer = await get_headless_auth().mint_bearer_for_user(user.user_id)
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

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    session_id = f"{HEADLESS_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
    governance = GovernanceFloor(audit=get_run_audit_recorder())

    # Fail-closed audit before the 202: a run that can't be audited must not
    # execute, and the caller should hear about it now rather than via a poll.
    await governance.on_run_start(
        run_id=run_id,
        user_id=user.user_id,
        session_id=session_id,
        trigger="run_now",
        prompt=body.prompt,
        title=body.title,
    )

    spawn_run(
        _execute_run(
            governance=governance,
            user_id=user.user_id,
            prompt=body.prompt,
            auth=StaticBearerAuth(bearer),
            run_id=run_id,
            session_id=session_id,
            title=body.title,
            model_id=body.model_id,
            rag_assistant_id=body.rag_assistant_id,
            enabled_tools=enabled_tools,
            agent_type=body.agent_type,
            trigger="run_now",
        )
    )

    logger.info("Run-now %s for user %s started session=%s", run_id, user.user_id, session_id)
    return RunStartedResponse(run_id=run_id, session_id=session_id)


@router.post(
    "/grant", response_model=GrantEnableResponse, response_model_by_alias=True
)
async def enable_grant(
    request: Request,
    user: User = Depends(require_scheduled_runs_user),
) -> GrantEnableResponse:
    """Create or refresh the caller's headless grant from their live session.

    Lets a user turn on scheduled runs directly (e.g. from the schedules
    page) without first having to exercise "Run now". Shares the exact
    create-on-enable logic ``run_now`` uses via ``_resolve_grant`` — the
    same 409 applies if the caller has neither an existing grant nor a
    live session to pin one from (e.g. a stale API-key-only context).
    """
    grant = await _resolve_grant(request, user)
    return GrantEnableResponse.from_grant(grant)


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


# Registered after the literal ``/grant`` routes so ``GET /runs/grant`` is
# never read as a run id.
@router.get(
    "/{run_id}", response_model=RunStatusResponse, response_model_by_alias=True
)
async def get_run(
    run_id: str = Path(..., pattern=_RUN_ID_PATTERN),
    user: User = Depends(require_scheduled_runs_user),
) -> RunStatusResponse:
    """The caller's own run, read from its audit row. 404 for any other run."""
    item = await asyncio.to_thread(
        get_run_audit_recorder().get, user_id=user.user_id, run_id=run_id
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunStatusResponse.from_audit_item(item)

"""Schedule routes — CRUD for scheduled agent runs (`/schedules/*`).

**B1 is deliberately inert**: schedules can be created, listed, edited,
paused/resumed, and deleted here, but nothing fires yet — the
dispatcher/worker that reads ``DueScheduleIndex`` and calls
``run_agent_headless`` is B2 (docs/specs/scheduled-agent-runs.md §7).

Gating mirrors the Phase A "Run now" surface exactly (two independent
controls, spec §6):

* ``SCHEDULED_RUNS_ENABLED`` — per-environment kill switch (default on).
  Off -> every route here 404s, as if unmounted.
* ``scheduled-runs`` RBAC capability -- *who* may use the surface. Granted
  to the beta cohort's AppRole; missing -> 403. GA = grant to ``default``.

Auth is the standard SPA cookie dependency (``get_current_user_from_session``)
per the CLAUDE.md app-api rule.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import scheduled_runs_enabled
from apis.shared.rbac.capabilities import SCHEDULED_RUNS_CAPABILITY, user_has_capability
from apis.shared.rbac.service import get_app_role_service
from apis.shared.scheduled_prompts.service import (
    ScheduledPromptLimitExceeded,
    compute_next_run_at,
    create_scheduled_prompt,
    delete_scheduled_prompt,
    get_scheduled_prompt,
    list_scheduled_prompts,
    set_schedule_state,
    update_scheduled_prompt,
)

from apis.app_api.schedules.models import (
    CreateScheduleRequest,
    ScheduledPromptResponse,
    ScheduledPromptsListResponse,
    UpdateScheduleRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])


async def require_scheduled_runs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + kill switch + cohort capability, in that order.

    404 when the environment kill switch is off (the surface behaves as if
    unmounted), 403 when the authenticated caller lacks the
    ``scheduled-runs`` capability. Mirrors ``apis.app_api.runs.routes``.
    """
    if not scheduled_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not await user_has_capability(user, SCHEDULED_RUNS_CAPABILITY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to scheduled runs.",
        )
    return user


async def _resolve_enabled_tools_snapshot(user: User, requested: Optional[List[str]]) -> Optional[List[str]]:
    """Snapshot enabled_tools at creation time (Phase A punch #7).

    An explicit list from the caller is intersected with the user's current
    RBAC grant before it is frozen — the SPA picker filters for UX, but the
    request body is attacker-controlled within the caller's own session, so
    the server must not let a crafted list enable a tool the AppRole does not
    carry (the snapshot is later handed straight to the tool filter, which
    performs no RBAC check of its own). ``None`` resolves to the user's
    current RBAC-allowed tool set *right now* and freezes it — the catalog
    shifting later never changes what a sleeping schedule is allowed to call.
    """
    if requested is not None:
        allowed = await get_app_role_service().filter_requested_tools(user, requested)
        if len(allowed) != len(requested):
            logger.warning(
                "Dropped %d requested tool(s) outside user %s's RBAC grant on schedule create",
                len(requested) - len(allowed),
                user.user_id,
            )
        return allowed
    permissions = await get_app_role_service().resolve_user_permissions(user)
    return list(permissions.tools)


def _require_schedule_or_404(schedule, schedule_id: str):
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule not found: {schedule_id}")
    return schedule


@router.post("", response_model=ScheduledPromptResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    request: CreateScheduleRequest,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    enabled_tools = await _resolve_enabled_tools_snapshot(user, request.enabled_tools)
    try:
        schedule = await create_scheduled_prompt(
            user_id=user.user_id,
            label=request.label,
            prompt_text=request.prompt_text,
            cadence=request.cadence,
            hour_local=request.hour_local,
            timezone_name=request.timezone,
            weekday=request.weekday,
            assistant_id=request.assistant_id,
            enabled_tools=enabled_tools,
            deliver_email=request.deliver_email,
        )
    except ScheduledPromptLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return ScheduledPromptResponse.from_schedule(schedule)


@router.get("", response_model=ScheduledPromptsListResponse)
async def list_schedules(
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptsListResponse:
    schedules = await list_scheduled_prompts(user.user_id)
    return ScheduledPromptsListResponse(
        schedules=[ScheduledPromptResponse.from_schedule(s) for s in schedules]
    )


@router.get("/{schedule_id}", response_model=ScheduledPromptResponse)
async def get_schedule(
    schedule_id: str,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    schedule = _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)
    return ScheduledPromptResponse.from_schedule(schedule)


@router.patch("/{schedule_id}", response_model=ScheduledPromptResponse)
async def update_schedule(
    schedule_id: str,
    request: UpdateScheduleRequest,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    """Edit a schedule's fields and/or transition its state.

    Editing any cadence field (cadence/hour_local/weekday/timezone) on an
    *active* schedule recomputes ``next_run_at`` in the same request — an
    edited schedule never keeps a stale due time. A ``paused`` schedule just
    remembers the new cadence for when it resumes (mirrors
    ``sync_policies.change_policy_interval``).
    """
    schedule = _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)

    effective_cadence = request.cadence if request.cadence is not None else schedule.cadence
    effective_weekday = request.weekday if request.weekday is not None else schedule.weekday
    if effective_cadence == "weekly" and effective_weekday is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="weekday is required when cadence is 'weekly'",
        )

    # An edited tool list is a write path into the same frozen snapshot, so it
    # gets the same RBAC intersection as creation — a PATCH must not be a way
    # around the create-time check. ``None`` means "leave tools unchanged".
    enabled_tools = request.enabled_tools
    if enabled_tools is not None:
        enabled_tools = await _resolve_enabled_tools_snapshot(user, enabled_tools)

    schedule = await update_scheduled_prompt(
        user.user_id,
        schedule_id,
        label=request.label,
        prompt_text=request.prompt_text,
        cadence=request.cadence,
        hour_local=request.hour_local,
        weekday=request.weekday,
        timezone_name=request.timezone,
        assistant_id=request.assistant_id,
        enabled_tools=enabled_tools,
        deliver_email=request.deliver_email,
    )

    if request.state is not None and request.state != schedule.state:
        if request.state == "active":
            next_run_at = compute_next_run_at(
                schedule.cadence, schedule.hour_local, schedule.timezone, weekday=schedule.weekday
            )
            await set_schedule_state(user.user_id, schedule_id, "active", next_run_at=next_run_at)
        else:
            await set_schedule_state(user.user_id, schedule_id, "paused", state_reason="Paused by user")
        schedule = await get_scheduled_prompt(user.user_id, schedule_id)

    return ScheduledPromptResponse.from_schedule(schedule)


@router.post("/{schedule_id}/pause", response_model=ScheduledPromptResponse)
async def pause_schedule(
    schedule_id: str,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    """Pause a schedule — removes it from DueScheduleIndex immediately."""
    schedule = _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)
    if schedule.state == "paused":
        return ScheduledPromptResponse.from_schedule(schedule)

    await set_schedule_state(user.user_id, schedule_id, "paused", state_reason="Paused by user")
    schedule = await get_scheduled_prompt(user.user_id, schedule_id)
    return ScheduledPromptResponse.from_schedule(schedule)


@router.post("/{schedule_id}/resume", response_model=ScheduledPromptResponse)
async def resume_schedule(
    schedule_id: str,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    """Resume a paused (or paused_error) schedule — recomputes next_run_at
    fresh from now and re-adds it to DueScheduleIndex."""
    schedule = _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)
    if schedule.state == "active":
        return ScheduledPromptResponse.from_schedule(schedule)

    next_run_at = compute_next_run_at(
        schedule.cadence, schedule.hour_local, schedule.timezone, weekday=schedule.weekday
    )
    await set_schedule_state(user.user_id, schedule_id, "active", next_run_at=next_run_at)
    schedule = await get_scheduled_prompt(user.user_id, schedule_id)
    return ScheduledPromptResponse.from_schedule(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    user: User = Depends(require_scheduled_runs_user),
) -> None:
    """Delete = total revocation (no orphan timers)."""
    _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)
    await delete_scheduled_prompt(user.user_id, schedule_id)
    return None

"""Schedule routes — CRUD for scheduled agent runs (`/schedules/*`).

**B1 is deliberately inert**: schedules can be created, listed, edited,
paused/resumed, and deleted here, but nothing fires yet — the
dispatcher/worker that reads ``DueScheduleIndex`` and calls
``run_agent_headless`` is B2 (docs/specs/scheduled-agent-runs.md §7).

Gating is a single per-environment control (spec §6):

* ``SCHEDULED_RUNS_ENABLED`` — kill switch (default on). Off -> every route
  here 404s, as if unmounted.

The surface is otherwise open to any authenticated user, and the SPA links
it from the sidebar's Schedules entry. It is deliberately *not* behind the
``scheduled-runs`` RBAC capability (which turned every non-beta caller into a
403 the SPA surfaced as a toast).

Auth is the standard SPA cookie dependency (``get_current_user_from_session``)
per the CLAUDE.md app-api rule.

The quiet watcher (docs/specs/quiet-watcher.md) adds a schedule's ``mode`` and
``modelId``, accepted only while ``PROACTIVE_RUNS_ENABLED`` is on, and three
routes for personal schedules that 404 while it is off: ``GET …/runs`` and
``GET``/``DELETE …/state``.

Slice 4 (docs/specs/proactive-trust.md §4) adds ``maxCostPerDay``, accepted only
while the flag is on, and required there on an interval under 60 minutes: left
out, the deployment's ``SCHEDULE_DEFAULT_MAX_COST_PER_DAY`` fills it in; an
explicit null is a 422. A resume, and an edit that recomputes ``nextRunAt``, never
lands inside a budget hold; a budget raised above today's spend releases it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import proactive_runs_enabled, scheduled_runs_enabled
from apis.shared.rbac.service import get_app_role_service
from apis.shared.scheduled_prompts.service import (
    MIN_INTERVAL_MINUTES,
    UNSET,
    InvalidTimezoneError,
    ScheduledPromptLimitExceeded,
    compute_next_run_at,
    create_scheduled_prompt,
    interval_to_minutes,
    delete_scheduled_prompt,
    get_scheduled_prompt,
    list_scheduled_prompts,
    read_scheduled_prompt,
    remember_owner_email,
    set_schedule_state,
    update_scheduled_prompt,
)
from apis.shared.scheduled_prompts.budget import (
    SHORT_INTERVAL_MINUTES,
    default_max_cost_per_day,
    requires_budget,
    respect_hold,
    utc_now,
)
from apis.shared.scheduled_prompts.actions import list_schedule_actions
from apis.shared.scheduled_prompts.runs import list_schedule_runs
from apis.shared.scheduled_prompts.watch_state import clear_watch_state, get_watch_state
from apis.shared.projects.models import normalize_email

from apis.app_api.schedules.models import (
    CreatePersonalScheduleRequest,
    ScheduledPromptResponse,
    ScheduleActionsResponse,
    ScheduledPromptsListResponse,
    ScheduleRunsResponse,
    UpdatePersonalScheduleRequest,
    UpdateScheduleRequest,
    WatchStateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/schedules", tags=["schedules"])


async def require_scheduled_runs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + kill switch, in that order.

    404 when the environment kill switch is off (the surface behaves as if
    unmounted). Otherwise any authenticated user may use the surface — it is
    intentionally not behind an RBAC capability (see module docstring).
    Mirrors ``apis.app_api.runs.routes``.
    """
    if not scheduled_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


async def require_proactive_runs_user(
    user: User = Depends(require_scheduled_runs_user),
) -> User:
    """``require_scheduled_runs_user``, then the quiet-watcher flag: 404 while it is off.

    Authentication comes first, so an anonymous caller gets 401 whatever the
    flag says and the flag's state is never disclosed to one.
    """
    if not proactive_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


_PROACTIVE_OFF_DETAIL = "Watch mode and a schedule's own model are not available here."
_BUDGET_OFF_DETAIL = "A schedule's daily budget is not available here."
_BUDGET_REQUIRED_DETAIL = (
    f"maxCostPerDay is required for a schedule that runs more often than every {SHORT_INTERVAL_MINUTES} minutes."
)


def _budget_given(request) -> bool:
    """Whether the body named ``maxCostPerDay`` at all (a null included), not just set it."""
    return "max_cost_per_day" in request.model_fields_set


def _create_budget(request: CreatePersonalScheduleRequest) -> Optional[Decimal]:
    """The new schedule's ``maxCostPerDay`` (docs/specs/proactive-trust.md §4.4).

    While the flag is off, a budget is refused and none is stored. While on, an
    interval under 60 minutes must have one: left out, the deployment's default
    fills it in, so the user sees a number rather than an error; an explicit null
    is a 422 naming the field.
    """
    given = _budget_given(request)
    if not proactive_runs_enabled():
        if given and request.max_cost_per_day is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BUDGET_OFF_DETAIL)
        return None
    short = requires_budget(request.cadence, interval_to_minutes(request.interval_value, request.interval_unit))
    if given:
        if request.max_cost_per_day is None and short:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BUDGET_REQUIRED_DETAIL)
        return request.max_cost_per_day
    return default_max_cost_per_day() if short else None


def _update_budget(request: UpdatePersonalScheduleRequest, schedule, *, cadence, interval_minutes):
    """A PATCH's ``maxCostPerDay`` argument: ``UNSET``, a value, or ``None`` to clear it.

    The same rule as create, against the cadence the edit leaves: an explicit null on
    an interval under 60 minutes is a 422, and an edit that leaves such a schedule
    with no budget gets the default.
    """
    given = _budget_given(request)
    if not proactive_runs_enabled():
        if given and request.max_cost_per_day is not None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BUDGET_OFF_DETAIL)
        return UNSET
    if schedule.project_id:
        return UNSET
    short = requires_budget(cadence, interval_minutes)
    if given:
        if request.max_cost_per_day is None and short:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BUDGET_REQUIRED_DETAIL)
        return request.max_cost_per_day
    if short and schedule.max_cost_per_day is None:
        return default_max_cost_per_day()
    return UNSET


_EXEMPTION_OFF_DETAIL = "A schedule's Act without asking list is not available here."
_EXEMPTION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}/[A-Za-z0-9_.:-]{1,128}$")


async def _check_act_without_asking(user: User, entries: Optional[List[str]]) -> Optional[List[str]]:
    """The validated ``actWithoutAsking`` list (proactive-trust §7.5), or None for none.

    Refused while the flag is off. Each entry names one tool on one server,
    ``{catalogId}/{toolName}``, never a whole server, and must be a tool the caller
    may use by RBAC (the server's id, the scoped ``id::tool``, or the wildcard), not
    the schedule's ``enabledTools``, which may be null. Order kept, duplicates dropped.
    """
    if entries is None:
        return None
    if not proactive_runs_enabled():
        if entries:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_EXEMPTION_OFF_DETAIL)
        return None
    cleaned = list(dict.fromkeys(entry.strip() for entry in entries))
    bad = [entry for entry in cleaned if not _EXEMPTION_RE.match(entry)]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"actWithoutAsking entries are '<toolId>/<toolName>', one tool each: {bad[0]!r}",
        )
    if not cleaned:
        return []
    permissions = await get_app_role_service().resolve_user_permissions(user)
    granted = set(permissions.tools or [])
    for entry in cleaned:
        catalog_id, tool_name = entry.split("/", 1)
        if "*" in granted or catalog_id in granted or f"{catalog_id}::{tool_name}" in granted:
            continue
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"You can't use {entry!r}, so a schedule can't run it without asking.",
        )
    return cleaned


def _resume_time(schedule) -> str:
    """A resumed schedule's ``nextRunAt``: its cadence from now, never inside a budget hold."""
    next_run_at = compute_next_run_at(
        schedule.cadence,
        schedule.hour_local,
        schedule.timezone,
        weekday=schedule.weekday,
        interval_minutes=interval_to_minutes(schedule.interval_value, schedule.interval_unit),
    )
    return respect_hold(schedule, next_run_at, utc_now())


async def _check_watch_fields(
    user: User,
    *,
    mode: Optional[str],
    model_id: Optional[str],
    clear_model: bool = False,
) -> None:
    """Refuse the quiet-watcher fields while the flag is off, and a model the caller can't use.

    While off, anything but the defaults (``mode`` unset or ``"standard"``, no
    ``modelId``, no clear) is a 422, so an older client's request is unchanged.
    While on, ``modelId`` is held to the same predicate the turn will be
    (``AppRoleService.can_access_model`` → ``grants_model_access``), which
    refuses a model with no catalog row.
    """
    asks = (mode not in (None, "standard")) or model_id is not None or clear_model
    if not proactive_runs_enabled():
        if asks:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_PROACTIVE_OFF_DETAIL)
        return
    if model_id is not None and not await get_app_role_service().can_access_model(user, model_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"You can't use the model {model_id!r} for a schedule.",
        )


def _only_state(request: UpdateScheduleRequest) -> bool:
    """Whether a PATCH asks for nothing but a state change."""
    return set(request.model_dump(exclude_none=True, exclude_defaults=True)) <= {"state"}


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


async def _backfill_owner_email(user: User, schedules) -> None:
    """Record the caller's email on their personal schedules that predate storing it.

    A pause notice is addressed by email (``pausing.notify_schedule_paused``).
    Writes only for a schedule with none, once each; best-effort, never fails the list.
    """
    email = normalize_email(user.email or "")
    missing = [s for s in schedules if not s.project_id and not s.owner_email]
    if not email or not missing:
        return
    for schedule in missing:
        try:
            await asyncio.to_thread(remember_owner_email, user.user_id, schedule.schedule_id, email)
        except Exception:
            logger.warning("Could not record the owner email on schedule %s", schedule.schedule_id, exc_info=True)


def _require_schedule_or_404(schedule, schedule_id: str):
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule not found: {schedule_id}")
    return schedule


def _refuse_project_schedule(schedule) -> None:
    """A project schedule is changed and resumed from its project (Shared Projects 3.2).

    There its creator's standing is checked and the change is on the project's
    trail. Pausing and deleting stay open here: stopping your own schedule never
    needs the project's say.
    """
    if schedule.project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This schedule runs in a project. Change or resume it from the project.",
        )


@router.post("", response_model=ScheduledPromptResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    request: CreatePersonalScheduleRequest,
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptResponse:
    await _check_watch_fields(user, mode=request.mode, model_id=request.model_id)
    budget = _create_budget(request)
    exemptions = await _check_act_without_asking(user, request.act_without_asking)
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
            interval_value=request.interval_value,
            interval_unit=request.interval_unit,
            assistant_id=request.assistant_id,
            enabled_tools=enabled_tools,
            deliver_email=request.deliver_email,
            # Where a pause notice goes: the scheduled-runs Lambdas can't look it up.
            owner_email=normalize_email(user.email or "") or None,
            mode=request.mode,
            model_id=request.model_id,
            max_cost_per_day=budget,
            act_without_asking=exemptions or None,
        )
    except ScheduledPromptLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except InvalidTimezoneError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    return ScheduledPromptResponse.from_schedule(schedule)


@router.get("", response_model=ScheduledPromptsListResponse)
async def list_schedules(
    user: User = Depends(require_scheduled_runs_user),
) -> ScheduledPromptsListResponse:
    schedules = await list_scheduled_prompts(user.user_id)
    await _backfill_owner_email(user, schedules)
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
    request: UpdatePersonalScheduleRequest,
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
    if not (schedule.project_id and request.state == "paused" and _only_state(request)):
        _refuse_project_schedule(schedule)
    await _check_watch_fields(
        user, mode=request.mode, model_id=request.model_id, clear_model=request.clear_model
    )

    effective_cadence = request.cadence if request.cadence is not None else schedule.cadence
    effective_weekday = request.weekday if request.weekday is not None else schedule.weekday
    if effective_cadence == "weekly" and effective_weekday is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="weekday is required when cadence is 'weekly'",
        )

    effective_interval_value = (
        request.interval_value if request.interval_value is not None else schedule.interval_value
    )
    effective_interval_unit = (
        request.interval_unit if request.interval_unit is not None else schedule.interval_unit
    )
    if effective_cadence == "interval":
        if effective_interval_value is None or effective_interval_unit is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="intervalValue and intervalUnit are required when cadence is 'interval'",
            )
        if interval_to_minutes(effective_interval_value, effective_interval_unit) < MIN_INTERVAL_MINUTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"interval must be at least {MIN_INTERVAL_MINUTES} minutes",
            )

    if "act_without_asking" in request.model_fields_set:
        exemptions_arg = await _check_act_without_asking(user, request.act_without_asking or [])
        exemptions_arg = exemptions_arg or None
    else:
        exemptions_arg = UNSET

    budget_arg = _update_budget(
        request,
        schedule,
        cadence=effective_cadence,
        interval_minutes=interval_to_minutes(effective_interval_value, effective_interval_unit),
    )

    # Clear intent is explicit — a bare null means "leave unchanged", so the
    # service uses the UNSET sentinel for "untouched". Clearing the assistant
    # reverts to the default agent; clearing tools re-snapshots the caller's
    # current RBAC-allowed set. An edited tool list is a write path into the
    # same frozen snapshot, so it gets the same create-time resolution — a
    # PATCH must not route around the create-time check.
    if request.clear_assistant:
        assistant_arg = None
    elif request.assistant_id is not None:
        assistant_arg = request.assistant_id
    else:
        assistant_arg = UNSET

    if request.clear_tools:
        tools_arg = await _resolve_enabled_tools_snapshot(user, None)
    elif request.enabled_tools is not None:
        tools_arg = await _resolve_enabled_tools_snapshot(user, request.enabled_tools)
    else:
        tools_arg = UNSET

    if request.clear_model:
        model_arg = None
    elif request.model_id is not None:
        model_arg = request.model_id
    else:
        model_arg = UNSET

    try:
        schedule = await update_scheduled_prompt(
            user.user_id,
            schedule_id,
            label=request.label,
            prompt_text=request.prompt_text,
            cadence=request.cadence,
            hour_local=request.hour_local,
            weekday=request.weekday,
            interval_value=request.interval_value,
            interval_unit=request.interval_unit,
            timezone_name=request.timezone,
            assistant_id=assistant_arg,
            enabled_tools=tools_arg,
            deliver_email=request.deliver_email,
            mode=request.mode,
            model_id=model_arg,
            max_cost_per_day=budget_arg,
            act_without_asking=exemptions_arg,
        )
    except InvalidTimezoneError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if request.state is not None and request.state != schedule.state:
        if request.state == "active":
            await set_schedule_state(user.user_id, schedule_id, "active", next_run_at=_resume_time(schedule))
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
    _refuse_project_schedule(schedule)

    await set_schedule_state(user.user_id, schedule_id, "active", next_run_at=_resume_time(schedule))
    schedule = await get_scheduled_prompt(user.user_id, schedule_id)
    return ScheduledPromptResponse.from_schedule(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    user: User = Depends(require_scheduled_runs_user),
) -> None:
    """Delete = total revocation (no orphan timers).

    A project schedule also leaves its project's list, and the project's trail
    records it, as if deleted from the project.
    """
    schedule = _require_schedule_or_404(await get_scheduled_prompt(user.user_id, schedule_id), schedule_id)
    if schedule.project_id:
        from apis.app_api.projects.routes import _svc as _projects
        from apis.shared.audit import AuditAction

        await asyncio.to_thread(_projects().schedules.delete, schedule)
        _projects().record(
            AuditAction.PROJECT_SCHEDULE_DELETED, user, schedule.project_id,
            before={"scheduleId": schedule_id, "label": schedule.label, "createdBy": schedule.owner_email},
        )
        return None
    await delete_scheduled_prompt(user.user_id, schedule_id)
    return None


async def _personal_schedule_or_404(user: User, schedule_id: str):
    """The caller's own personal schedule, or 404 (a project schedule's runs are on the project)."""
    schedule = await asyncio.to_thread(read_scheduled_prompt, user.user_id, schedule_id)
    if schedule is None or schedule.project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Schedule not found: {schedule_id}")
    return schedule


@router.get("/{schedule_id}/runs", response_model=ScheduleRunsResponse)
async def list_schedule_run_history(
    schedule_id: str,
    user: User = Depends(require_proactive_runs_user),
) -> ScheduleRunsResponse:
    """A personal schedule's runs, newest first (at most 50; rows expire after 30 days)."""
    await _personal_schedule_or_404(user, schedule_id)
    runs = await asyncio.to_thread(list_schedule_runs, user.user_id, schedule_id)
    return ScheduleRunsResponse(runs=runs)


@router.get("/{schedule_id}/actions", response_model=ScheduleActionsResponse)
async def list_schedule_action_audit(
    schedule_id: str,
    user: User = Depends(require_proactive_runs_user),
) -> ScheduleActionsResponse:
    """A personal schedule's audited calls, newest first (at most 50; rows are kept 365 days).

    Every call its unattended runs made that is not known to be read-only, recorded before
    it ran (docs/specs/proactive-trust.md §7.4). Ids, classes and outcomes only.
    """
    await _personal_schedule_or_404(user, schedule_id)
    actions = await asyncio.to_thread(list_schedule_actions, user.user_id, schedule_id)
    return ScheduleActionsResponse(actions=actions)


@router.get("/{schedule_id}/state", response_model=WatchStateResponse)
async def get_schedule_watch_state(
    schedule_id: str,
    user: User = Depends(require_proactive_runs_user),
) -> WatchStateResponse:
    """What a watcher is tracking; ``state`` is null when it has none yet."""
    await _personal_schedule_or_404(user, schedule_id)
    state = await asyncio.to_thread(get_watch_state, user.user_id, schedule_id)
    return WatchStateResponse(state=state)


@router.delete("/{schedule_id}/state", status_code=status.HTTP_204_NO_CONTENT)
async def clear_schedule_watch_state(
    schedule_id: str,
    user: User = Depends(require_proactive_runs_user),
) -> None:
    """Start over: the watcher's next run reads everything as new."""
    await _personal_schedule_or_404(user, schedule_id)
    await asyncio.to_thread(clear_watch_state, user.user_id, schedule_id)
    return None

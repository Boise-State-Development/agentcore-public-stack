"""Problem reports — the user's half and the admin queue (D15, Phase 8).

Two surfaces over ``apis.shared.assistants.reports``, which owns the storage:

* **A user reports a published Agent.** Gated on reachability *and* publication (D15.3):
  you may report what the store offered you. Reporting a ``private`` or ``in_review``
  Agent is not a thing — nobody outside the author was invited to it, and a takedown is
  not available as a remedy for something that was never listed.

* **An admin triages.** The reporter is visible here and nowhere else (D15.2), and a
  decision writes only the report (D15.5).

⚠️ **A report is not a permission signal and not a ranking input.** Nothing in this module
touches ``usageCount``, the store front, or any ordering, and no count it produces is ever
returned on a user-facing read. The moment report volume influences placement, reporting
becomes a way to bury a competitor's Agent.
"""

import logging
import os
from typing import List, Optional, Tuple

from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.models import (
    REPORT_REASON_SEVERITY,
    AdminReportRow,
    AgentReport,
    ReportReason,
)
from apis.shared.assistants.reports import (
    count_open_reports,
    list_open_reports,
    list_reports_for_agent,
    resolve_report,
    submit_report,
)
from apis.shared.assistants.service import (
    _get_assistant_cloud_without_ownership_check,
    get_assistant_with_access_check,
)
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)


class ReportError(Exception):
    """A report operation the caller may not perform, or that is not currently valid.

    Mirrors ``ListingError``: ``status_code`` maps straight to the HTTP response.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def file_report(
    agent_id: str, user: User, *, reason: ReportReason, note: Optional[str]
) -> Tuple[AgentReport, bool]:
    """Record a user's problem report against a published Agent (D15).

    Two gates, in this order, because they answer different questions:

    1. **Can the caller reach it?** The same ``get_assistant_with_access_check`` the
       detail page uses, so nothing here becomes a way to probe for Agents you cannot see.
    2. **Is it published?** (D15.3) Checked *after*, and reported as a 400 rather than a
       404 — the caller demonstrably could see the Agent, so pretending it does not exist
       would be a confusing lie rather than a useful one.

    Returns ``(report, replaced_existing)``; the second half is D15.4's one-open-report
    rule surfacing so the UI can say "we updated your report" rather than implying a
    second one is now queued.
    """
    assistant, _permission = await get_assistant_with_access_check(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if not assistant:
        raise ReportError(f"Agent not found: {agent_id}", status_code=404)

    listing = assistant.listing
    if listing is None or listing.state != "published":
        raise ReportError(
            "Only agents published in the store can be reported.", status_code=400
        )

    return await submit_report(
        agent_id,
        reporter_id=user.user_id,
        reporter_name=_display_name(user),
        reason=reason,
        note=(note or "").strip() or None,
    )


def _display_name(user: User) -> str:
    """The reporter as the admin queue should read them (D15.2).

    Falls back through name → email → id rather than rendering an empty cell: an admin
    triaging a possible brigade needs *something* to compare rows by.
    """
    return getattr(user, "name", None) or getattr(user, "email", None) or user.user_id


async def list_report_queue() -> Tuple[List[AdminReportRow], int]:
    """The admin Reports queue: open reports oldest-first, plus the nav badge count.

    Each row is joined to its Agent so the admin can triage without a second fetch. An
    Agent that no longer exists yields a row flagged ``agentMissing`` rather than being
    dropped — a dropped row would be invisible *and* still counted, and the admin would
    have no way to clear it.

    Within the sweep, rows sort by ``(severity, createdAt)``: ``inappropriate`` should
    page a human rather than wait its turn behind a stale-link complaint.
    """
    reports = await list_open_reports()
    rows = [await _to_row(report) for report in reports]
    rows.sort(key=lambda r: (REPORT_REASON_SEVERITY.get(r.reason, 99), r.created_at))
    return rows, len(rows)


async def list_agent_report_history(agent_id: str) -> List[AdminReportRow]:
    """Every report on one Agent, newest first — the admin's per-agent history."""
    return [await _to_row(report) for report in await list_reports_for_agent(agent_id)]


async def triage_report(
    agent_id: str, report_id: str, admin: User, *, decision: str, note: Optional[str]
) -> AdminReportRow:
    """Resolve or dismiss a report (D15.5).

    ⚠️ This changes ``listing.state`` for nobody. A report is a note *about* an Agent, not
    a state *of* it; if one warrants delisting, the admin uses the existing takedown path
    and that is a separate, recorded act. Wiring the two together here would make the
    queue's "Resolve" button quietly delist an Agent.
    """
    if decision not in ("resolve", "dismiss"):
        raise ReportError(f"Unknown decision: {decision}", status_code=400)

    state = "resolved" if decision == "resolve" else "dismissed"
    try:
        report = await resolve_report(
            agent_id,
            report_id,
            state=state,
            resolved_by=_display_name(admin),
            note=(note or "").strip() or None,
        )
    except ValueError as e:
        raise ReportError(str(e), status_code=404) from e

    return await _to_row(report)


async def _to_row(report: AgentReport) -> AdminReportRow:
    """Join one report to its Agent for the console.

    The Agent is read **without** an ownership or visibility check — deliberately. A
    reviewer fails an ownership check by definition, and a report on an Agent whose author
    has since made it ``PRIVATE`` is exactly the report an admin most needs to see. Same
    reasoning as the listing service's admin load path.

    A missing Agent yields a flagged row rather than an omitted one: a row that is dropped
    from the queue but still counted by the badge is one an admin can neither see nor
    clear.
    """
    base = dict(
        report_id=report.report_id,
        agent_id=report.agent_id,
        reporter_id=report.reporter_id,
        reporter_name=report.reporter_name,
        reason=report.reason,
        note=report.note,
        state=report.state,
        created_at=report.created_at,
        resolved_at=report.resolved_at,
        resolved_by=report.resolved_by,
        resolution_note=report.resolution_note,
    )

    assistant = None
    try:
        table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
        if table_name:
            assistant = await _get_assistant_cloud_without_ownership_check(
                report.agent_id, table_name
            )
    except Exception:
        logger.warning(f"Failed to load agent {report.agent_id} for report row", exc_info=True)

    if assistant is None:
        return AdminReportRow(**base, agent_name=report.agent_id, agent_missing=True)

    return AdminReportRow(
        **base,
        agent_name=assistant.name,
        emoji=assistant.emoji,
        icon_url=icon_url(assistant.assistant_id, assistant.icon_key),
        owner_name=assistant.owner_name,
        listing_state=assistant.listing.state if assistant.listing else None,
    )


async def open_report_count() -> int:
    """The D10 nav badge. A COUNT query, never a projection of rows nobody renders."""
    return await count_open_reports()

"""Headless agent-run harness (primitive F1).

The trigger-agnostic entrypoint for running an agent turn as a user with no
live browser session: schedules, "Run now", A2A, webhooks, and eval harnesses
are all just callers of :func:`run_agent_headless`.

Lives in ``apis.shared`` because it is consumed by more than one service
(app-api "Run now" routes, the Phase-B dispatcher/worker Lambdas) and must
never be an inference-api route — the AgentCore Runtime data plane only
exposes ``/invocations`` + ``/ping`` (see CLAUDE.md, Inference API boundary).
The harness is a *client* of ``/invocations``, not a new server surface.

Design + spike evidence: docs/specs/harness-entrypoint-spike-findings.md.
"""

from apis.shared.harness.auth import (
    BearerAuthStrategy,
    CognitoRefreshBearerAuth,
    HeadlessAuthError,
    StaticBearerAuth,
)
from apis.shared.harness.governance import GovernanceFloor, RunAuditRecorder
from apis.shared.harness.grants import (
    HEADLESS_GRANT_MAX_AGE_DAYS,
    HeadlessGrant,
    HeadlessGrantService,
)
from apis.shared.harness.models import (
    OAuthConsentRequired,
    RunResult,
    RunStatus,
    ToolTraceEntry,
)
from apis.shared.harness.runner import build_invocations_url, run_agent_headless

__all__ = [
    "BearerAuthStrategy",
    "CognitoRefreshBearerAuth",
    "GovernanceFloor",
    "HEADLESS_GRANT_MAX_AGE_DAYS",
    "HeadlessAuthError",
    "HeadlessGrant",
    "HeadlessGrantService",
    "OAuthConsentRequired",
    "RunAuditRecorder",
    "RunResult",
    "RunStatus",
    "StaticBearerAuth",
    "ToolTraceEntry",
    "build_invocations_url",
    "run_agent_headless",
]

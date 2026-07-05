"""Headless agent-run harness (F1 spike).

The trigger-agnostic entrypoint for running an agent turn as a user with no
live browser session: schedules, "Run now", A2A, webhooks, and eval harnesses
are all just callers of :func:`run_agent_headless`.

Lives in ``apis.shared`` because it is consumed by more than one service
(app-api "Run now" routes, future dispatcher/worker Lambdas) and must never
be an inference-api route — the AgentCore Runtime data plane only exposes
``/invocations`` + ``/ping`` (see CLAUDE.md, Inference API boundary). The
harness is a *client* of ``/invocations``, not a new server surface.

Spike status: see docs/specs/harness-entrypoint-spike-findings.md.
"""

from apis.shared.harness.auth import (
    BearerAuthStrategy,
    CognitoRefreshBearerAuth,
    HeadlessAuthError,
    StaticBearerAuth,
)
from apis.shared.harness.governance import GovernanceFloor, RunAuditRecorder
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
    "HeadlessAuthError",
    "OAuthConsentRequired",
    "RunAuditRecorder",
    "RunResult",
    "RunStatus",
    "StaticBearerAuth",
    "ToolTraceEntry",
    "build_invocations_url",
    "run_agent_headless",
]

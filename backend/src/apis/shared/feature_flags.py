"""Process-level feature flags resolved from environment variables.

These gate optional product surfaces per environment. Each flag documents
its own default: deferred features default off until explicitly turned on
(the ``FINE_TUNING_ENABLED`` pattern), while shipping features default on
with a kill switch (the ``KB_SYNC_ENABLED`` pattern). Each flag is read on
every call (not cached at import) so that:

* import-time callers (conditional router mounting) and per-request callers
  observe the same value, and
* tests can flip a flag with ``monkeypatch.setenv`` (per-request paths) or a
  module reload (import-time paths) without a process restart.
"""

import os


def skills_enabled() -> bool:
    """Whether the Skills feature is enabled for this environment.

    Covers the admin skills catalog, the user-facing skills picker, and
    skills mode (routing turns through the ``SkillAgent``). Defaults off;
    set ``SKILLS_ENABLED=true`` to turn it on. While off, new turns are
    forced through the plain ``ChatAgent`` and the skills surfaces are
    unmounted / hidden, but all skills data and code remain intact.
    """
    return os.environ.get("SKILLS_ENABLED", "false").lower() == "true"


def scheduled_runs_enabled() -> bool:
    """Whether the scheduled-runs surface is enabled for this environment.

    Covers the headless "Run now" route and headless-grant lifecycle today,
    and the schedule CRUD + dispatcher when Phase B lands. **Default ON
    with a kill switch** (house style, mirroring ``CDK_KB_SYNC_ENABLED``):
    unset or empty resolves to enabled; only the literal ``"false"``
    (case-insensitive) disables. The CDK side threads
    ``config.scheduledRuns.enabled`` into this env var with the same
    empty-string-safe ternary, so an unset GitHub Actions variable can
    never silently turn the feature off.

    Note this flag gates *feature existence* per environment; *who* can use
    it is the ``scheduled-runs`` RBAC capability
    (``apis.shared.rbac.capabilities``) — two independent controls.
    """
    return os.environ.get("SCHEDULED_RUNS_ENABLED", "").strip().lower() != "false"


def memory_spaces_enabled() -> bool:
    """Whether the Memory Spaces feature is enabled for this environment.

    Covers the user-owned/shareable markdown "second brain" surface (F5):
    the app-api ``/memory/spaces`` CRUD, the runtime read/write tools, and
    the SPA Memory panel. **Defaults off** (the ``SKILLS_ENABLED`` pattern):
    set ``MEMORY_SPACES_ENABLED=true`` to turn it on. While off, the surfaces
    are unmounted / hidden but all data and code remain intact. The feature
    ships incrementally across several PRs, so it stays dark until complete.

    Note this flag gates *feature existence* per environment; *who* can use it
    will be an RBAC capability — two independent controls (mirroring
    ``scheduled_runs_enabled``).
    """
    return os.environ.get("MEMORY_SPACES_ENABLED", "false").lower() == "true"

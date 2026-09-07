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
    """Whether the Skills feature exists in this environment.

    Covers the admin skills catalog, the user-facing skills picker, My Skills
    authoring, and skill resolution on the runtime path. **Default ON with a
    kill switch** (house style, mirroring ``SCHEDULED_RUNS_ENABLED``): unset or
    empty resolves to enabled; only the literal ``"false"`` (case-insensitive)
    disables. The CDK side threads ``config.skills.enabled`` into this env var
    with the same empty-string-safe ternary, so an unset GitHub Actions
    variable can never silently turn the feature off.

    Flipped from default-off in Skills v2 PR-5, once the epic was complete and
    dogfooded end to end (a real agentskills.io bundle uploaded as a user skill,
    bound to an Agent, exercised L1→L2→L3 including ``read_skill_file``).

    Note this flag gates *feature existence* per environment; *who* may use it
    is a role's ``grantedSkills`` — two independent controls. (A ``skills`` RBAC
    *capability* briefly gated the user-facing surfaces on top of this; it was
    removed because a capability id cannot be granted from the admin roles UI.
    See ``AppRoleService.resolve_user_permissions``.)
    """
    return os.environ.get("SKILLS_ENABLED", "").strip().lower() != "false"


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

    Note this flag is the *only* control on this surface. A ``scheduled-runs``
    RBAC capability once gated *who* could use it, but that gate 403'd in prod
    and was dropped; the routes are deliberately ungated now.
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


def workspace_tools_enabled() -> bool:
    """Whether the workspace file tools are enabled for this environment.

    Covers the ``workspace_list`` / ``workspace_read`` / ``workspace_write``
    agent tools (the generic file surface over the user-files store — see
    ``docs/specs/session-workspace-tools.md``). **Default ON with a kill
    switch** (house style, mirroring ``scheduled_runs_enabled``): unset or
    empty resolves to enabled; only the literal ``"false"``
    (case-insensitive) disables.

    Note this flag gates *feature existence* per environment; *who* may use
    the tools is the ``workspace_files`` catalog entry granted via roles —
    two independent controls.
    """
    return os.environ.get("WORKSPACE_TOOLS_ENABLED", "").strip().lower() != "false"


def agents_enabled() -> bool:
    """Whether the Agent Designer surface is enabled for this environment.

    Gates the ``/agents/*`` alias router (the governed Agent read/write surface over
    the evolved assistant store). **Default ON with a kill switch** (house style,
    mirroring ``scheduled_runs_enabled``): unset or empty resolves to enabled; only
    the literal ``"false"`` (case-insensitive) disables. The CDK side threads
    ``config.agents.enabled`` into this env var with the same empty-string-safe
    ternary, so an unset GitHub Actions variable can never silently turn it off. The
    Agent Designer shipped across several PRs (contract → surface → resolution →
    Designer UI → binding reflection); now complete, it defaults on.

    Gates *feature existence* per environment; *who* may use a specific agent is the
    identity-based access check already enforced by the assistant service. This flag is
    now the **only** control on the surface: the SPA nav was preview-gated to system
    admins until the marketplace went GA, and that condition came off with D14 (the nav
    entry is gated on this flag alone). See ``agent_marketplace_enabled`` for why there
    is no RBAC capability on this axis.

    ⚠️ **The kill switch's meaning changed in Designer Phase 5.** While the SPA shipped
    both nouns, turning this off degraded gracefully: the Agents nav disappeared and the
    Assistants editor was still there. Phase 5 retired that editor and redirected
    ``/assistants*`` onto the Agent surface, so there is nothing left to fall back to —
    off now means *no authoring surface at all*, not *the previous one*. Treat it as an
    outage switch, not a feature toggle. (The records are untouched either way; the
    routes and the SPA pages are what disappear.)
    """
    return os.environ.get("AGENTS_API_ENABLED", "").strip().lower() != "false"


def agent_marketplace_enabled() -> bool:
    """Whether the Agent Marketplace surface is enabled for this environment.

    Covers the listing lifecycle (submit / review / takedown), publisher profiles, and
    the admin Review queue + Listings pages. **Default ON with a kill switch** (house
    style, mirroring ``agents_enabled``): unset or empty resolves to enabled; only the
    literal ``"false"`` (case-insensitive) disables. The CDK side threads
    ``config.agentMarketplace.enabled`` into this env var with the same empty-string-safe
    ternary, so an unset GitHub Actions variable can never silently turn it off.

    App-api only. The marketplace adds no inference-api routes — publication is a
    catalog concern, and the inference API stays inference-only.

    **This flag is the only lever, and the store is GA.** D14 originally paired it with an
    ``agent-marketplace`` RBAC *capability* that would 404 the routes for ungranted roles,
    "mirroring the ``skills`` gate from Skills v2 PR-5". That gate no longer exists — it was
    removed because a capability id cannot be granted from the admin roles UI (see
    ``skills_enabled`` above and ``AppRoleService.resolve_user_permissions``), so copying it
    would ship a gate nobody can open. D14 has since been revised to drop the capability
    outright rather than defer it: per-role rollout of a feature *surface* needs a grantable
    axis this codebase does not have, and inventing one is not in this epic's scope.

    The interim state it left behind was worse than either end state. One template condition
    (``@if (showAgents() && isAdmin())``) hid the nav entry while ``/agents/discover``,
    ``/agents/{id}``, the composer ``@``-mention menu and role-seeded pins were all reachable
    by any authenticated user — the only closed door was the one we controlled. The nav gate
    is now this flag alone.
    """
    return os.environ.get("AGENT_MARKETPLACE_ENABLED", "").strip().lower() != "false"


def mid_turn_steering_enabled() -> bool:
    """Whether a follow-up may be injected into a turn that is still running.

    Covers the lease-row steering inbox, the runtime ``SteeringHook`` that
    injects at each tool boundary, the app-api ``/sessions/{id}/steer``
    endpoint, and the ``steering_applied`` SSE event (see
    ``docs/specs/mid-turn-steering.md``). **Default ON with a kill switch**
    (house style, mirroring ``scheduled_runs_enabled``): unset or empty
    resolves to enabled; only the literal ``"false"`` (case-insensitive)
    disables.

    While off, the hook is still registered but returns immediately, the steer
    endpoint 404s, and the SPA never POSTs — leaving exactly PR #916's
    behaviour, where a follow-up typed mid-stream is queued in the composer
    and flushed on the turn's falling edge. That fallback is permanent, not
    transitional: a turn that calls no tools has no boundary to inject at.
    """
    return os.environ.get("MID_TURN_STEERING_ENABLED", "").strip().lower() != "false"


def announcements_enabled() -> bool:
    """Whether the feature-announcement system is enabled for this environment.

    Covers the admin authoring surface (``/admin/announcements``) today, and
    the user-facing ``GET /announcements`` + ack endpoint and the panel /
    banner / modal surfaces as those land. **Default ON with a kill switch**
    (house style, mirroring ``scheduled_runs_enabled``): unset or empty
    resolves to enabled; only the literal ``"false"`` (case-insensitive)
    disables.

    While off, the admin router is unmounted so the surface 404s; the data and
    code remain intact. There is no separate RBAC capability on this axis —
    *who* may author is the delegable ``admin.announcements`` scope, and *who
    sees* a published announcement is the announcement's own ``targetRoles``
    display filter (which is deliberately **not** an RBAC grant; see
    ``docs/specs/feature-announcements.md`` §D9).
    """
    return os.environ.get("ANNOUNCEMENTS_ENABLED", "").strip().lower() != "false"


def artifact_share_inbox_enabled() -> bool:
    """Whether a recipient can *discover* artifacts shared with them.

    Covers the ``GET /shared-artifacts`` inbox endpoint and, through it,
    the library page's "Shared with you" tab. **Default ON with a kill
    switch** (house style, mirroring ``announcements_enabled`` and
    ``scheduled_runs_enabled``): unset or empty resolves to enabled; only
    the literal ``"false"`` (case-insensitive) disables.

    It shipped the other way round — default off, opt-in — because the
    surface landed before the product decision about it did. That
    decision was made in 1.18.0 and the inbox went live; carrying an
    opt-in default past it would mean every institution forking this
    repo silently loses a finished feature, and has to discover a
    variable to get it back. Default-on is the right answer for a fork,
    and ``"false"`` still turns it off for anyone who wants it dark.

    Note the empty-string case is load-bearing in the *opposite*
    direction now: an unset GitHub Actions variable forwards ``""``,
    which under this flag means **on**. That is deliberate — a fork that
    never sets the variable is exactly who this default is for.

    ############################################################
    # This flag gates the READ ONLY. The recipient fan-out rows the
    # inbox reads are written UNCONDITIONALLY, by every share write,
    # whether or not this is on.
    #
    # That asymmetry is the whole point. If the writes were gated too,
    # turning this on would expose an inbox missing every share created
    # while it was off — a wrong answer rather than an empty one, and
    # one nobody can see is wrong. Writing the pointer rows regardless
    # costs one small row per recipient and makes the flip complete and
    # instant, with no backfill to sequence.
    #
    # So: do not "optimise" the write path by wrapping it in this flag.
    ############################################################
    """
    return (
        os.environ.get("ARTIFACT_SHARE_INBOX_ENABLED", "").strip().lower()
        != "false"
    )

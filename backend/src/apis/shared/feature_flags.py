"""Process-level feature flags resolved from environment variables.

These gate optional product surfaces that ship in the codebase but stay
disabled for an environment until explicitly turned on — mirroring the
``FINE_TUNING_ENABLED`` pattern used in app-api. Each flag is read on every
call (not cached at import) so that:

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

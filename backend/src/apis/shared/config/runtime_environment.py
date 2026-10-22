"""Derived environment: resource names rebuilt from ``PROJECT_PREFIX``.

Why this exists
---------------
The AgentCore Runtime V2 caps a runtime's environment-variable payload at
2,560 bytes (``docs/specs/agentcore-runtime-v2.md`` §3 B4). Most of our payload
is redundant: about 26 of the Runtime's variables are the stack prefix plus a
fixed suffix (``{prefix}-sessions-metadata``, ``{prefix}-rag-documents-{account}``),
because CDK names every one of those resources with ``getResourceName``. The
plan (spec §7) is to stop sending them and derive them here instead.

How it works
------------
``derived_environment.json`` beside this module is the one manifest of such
variables. ``hydrate_derived_environment()`` runs once, at the top of the
service entrypoint before any module reads the environment, and **sets each
manifest variable that is absent** to its derived value. A variable that is
already set is left alone, so:

* every existing ``os.environ.get("DYNAMODB_…")`` read site keeps working
  unchanged (there are ~330 of them across ~96 files; none had to move);
* an explicit value remains an override, for local ``.env`` files, tests and
  deployments that keep sending the variable;
* with ``PROJECT_PREFIX`` unset (unit tests, a bare local run) nothing happens,
  so behaviour there is exactly what it was.

While the Runtime still receives every variable (spec §7.6 PR A), hydration
sets nothing. What it does do is **compare** each explicit value with the
derived one and log a warning on any difference, which checks the derivation
against every real deployment for free before PR B drops the variables.
``audit_derived_environment()`` is the same comparison without the writes, for
app-api, which keeps its explicit variables (ECS has no payload limit).

Cost: string formatting at process start, once. Nothing here runs on a turn,
and nothing reaches the prompt or ``toolConfig``.

Keeping CDK and this manifest in step
-------------------------------------
``infrastructure/test/runtime-derived-environment-manifest.test.ts`` reads the
same JSON and asserts that the synthesized template names each resource
exactly as the manifest derives it. Renaming a resource in CDK without
updating the manifest fails CI; so does adding a manifest entry for a resource
CDK names some other way.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

PROJECT_PREFIX_ENV = "PROJECT_PREFIX"
"""The stack prefix every derived name starts with (CDK ``config.projectPrefix``)."""

ACCOUNT_ID_ENV = "AWS_ACCOUNT_ID"
"""The AWS account id that account-scoped bucket names end with.

Not on the Runtime yet (spec §7.6 PR B adds it, ~26 bytes). Until then
:func:`resolve_account_id` reads the account off any explicit account-scoped
value that already follows the pattern, so PR A can still check those names.
"""

_MANIFEST_PATH = Path(__file__).with_name("derived_environment.json")
_ACCOUNT_ID_RE = re.compile(r"^\d{12}$")


@dataclass(frozen=True)
class DerivedVariable:
    """One manifest entry: ``name`` = ``{prefix}-{suffix}`` (``-{account}`` when scoped)."""

    name: str
    suffix: str
    account_scoped: bool = False

    def derive(self, prefix: str, account_id: Optional[str]) -> Optional[str]:
        """The value CDK gives this variable, or ``None`` when an input is missing."""
        if not prefix:
            return None
        if self.account_scoped:
            if not account_id:
                return None
            return f"{prefix}-{self.suffix}-{account_id}"
        return f"{prefix}-{self.suffix}"

    def account_id_from(self, prefix: str, value: str) -> Optional[str]:
        """The account id an explicit account-scoped value ends with, if it fits the pattern."""
        if not self.account_scoped or not prefix:
            return None
        head = f"{prefix}-{self.suffix}-"
        if not value.startswith(head):
            return None
        tail = value[len(head):]
        return tail if _ACCOUNT_ID_RE.match(tail) else None


@lru_cache(maxsize=1)
def derived_environment_manifest() -> tuple[DerivedVariable, ...]:
    """The manifest, parsed once per process. Names are unique by construction."""
    with _MANIFEST_PATH.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    variables = tuple(
        DerivedVariable(
            name=entry["name"],
            suffix=entry["suffix"],
            account_scoped=bool(entry.get("accountScoped", False)),
        )
        for entry in raw["variables"]
    )
    names = [variable.name for variable in variables]
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"derived_environment.json lists a variable twice: {duplicates}")
    return variables


def resolve_account_id(env: Mapping[str, str]) -> Optional[str]:
    """The account id for account-scoped names: ``AWS_ACCOUNT_ID``, else read off an explicit value."""
    explicit = env.get(ACCOUNT_ID_ENV, "").strip()
    if _ACCOUNT_ID_RE.match(explicit):
        return explicit
    prefix = env.get(PROJECT_PREFIX_ENV, "").strip()
    for variable in derived_environment_manifest():
        value = env.get(variable.name, "")
        inferred = variable.account_id_from(prefix, value.strip()) if value else None
        if inferred:
            return inferred
    return None


@dataclass
class EnvironmentReport:
    """What one pass over the manifest found. ``applied`` is empty for an audit."""

    prefix: str = ""
    account_id: Optional[str] = None
    applied: dict[str, str] = field(default_factory=dict)
    """Variables that were absent and are now set (hydrate only)."""
    matched: list[str] = field(default_factory=list)
    """Variables whose explicit value equals the derived one."""
    drifted: dict[str, tuple[str, str]] = field(default_factory=dict)
    """name -> (explicit, derived) where the two disagree. The explicit value stays in force."""
    skipped: dict[str, str] = field(default_factory=dict)
    """name -> reason, for variables that could not be derived (no prefix, no account)."""

    @property
    def enabled(self) -> bool:
        return bool(self.prefix)


def _reconcile(env: MutableMapping[str, str], *, apply: bool) -> EnvironmentReport:
    prefix = env.get(PROJECT_PREFIX_ENV, "").strip()
    report = EnvironmentReport(prefix=prefix)
    if not prefix:
        # No prefix, nothing to derive. Tests and bare local runs land here and
        # behave exactly as before this module existed.
        return report
    report.account_id = resolve_account_id(env)

    for variable in derived_environment_manifest():
        derived = variable.derive(prefix, report.account_id)
        if derived is None:
            report.skipped[variable.name] = f"{ACCOUNT_ID_ENV} is not set and no explicit value carries the account"
            continue
        explicit = env.get(variable.name, "").strip()
        if explicit:
            if explicit == derived:
                report.matched.append(variable.name)
            else:
                report.drifted[variable.name] = (explicit, derived)
            continue
        if apply:
            env[variable.name] = derived
            report.applied[variable.name] = derived
        else:
            report.skipped[variable.name] = "not set (audit only, nothing written)"
    return report


def hydrate_derived_environment(env: Optional[MutableMapping[str, str]] = None) -> EnvironmentReport:
    """Set every absent manifest variable to its derived value. Call once, first thing, in the entrypoint.

    An empty string counts as absent: ``.env`` files written from
    ``.env.example`` leave blank ``NAME=`` lines, and the read sites treat
    ``""`` as unset anyway.
    """
    import os

    return _reconcile(os.environ if env is None else env, apply=True)


def audit_derived_environment(env: Optional[Mapping[str, str]] = None) -> EnvironmentReport:
    """Compare explicit values with derived ones without writing anything (app-api)."""
    import os

    source = dict(os.environ if env is None else env)
    return _reconcile(source, apply=False)


def log_environment_report(report: EnvironmentReport, logger: logging.Logger, *, service: str) -> None:
    """One INFO summary, one WARNING per drifted variable.

    The drift warning is the point of running this while the Runtime still
    sends every variable: a deployment whose resources are not named
    ``{prefix}-{suffix}`` shows up here before spec §7.6 PR B would have
    broken it.
    """
    if not report.enabled:
        logger.debug("%s: %s unset; derived environment not applied", service, PROJECT_PREFIX_ENV)
        return
    logger.info(
        "%s derived environment (prefix=%s, account=%s): %d set, %d matched, %d drifted, %d skipped",
        service,
        report.prefix,
        report.account_id or "unknown",
        len(report.applied),
        len(report.matched),
        len(report.drifted),
        len(report.skipped),
    )
    for name, value in report.applied.items():
        logger.info("%s derived %s=%s", service, name, value)
    for name, (explicit, derived) in report.drifted.items():
        logger.warning(
            "%s: %s is %r but PROJECT_PREFIX derives %r. The explicit value stays in force; "
            "dropping this variable from the Runtime (docs/specs/agentcore-runtime-v2.md §7) "
            "would change the name this process uses. Fix the manifest or the deployment first.",
            service,
            name,
            explicit,
            derived,
        )
    for name, reason in report.skipped.items():
        logger.debug("%s: %s not derived: %s", service, name, reason)

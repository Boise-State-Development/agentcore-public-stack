"""Which build of this service is running (docs/specs/release-ledger.md D1, D2).

``APP_VERSION`` is the repository's ``VERSION`` file (``1.33.0``) and
``APP_BUILD`` the 16-character content hash the image was tagged with. Both
are baked into every image by ``scripts/build/build-one.sh`` as build args,
never sent as runtime variables: every cost row, ``turn_prelude`` line and
EMF record carries them, and the AgentCore Runtime's environment is a
2,560-byte budget. A local run, or an image built outside that script,
reports ``unknown`` for both.

``build`` matters on its own because on dev the version is constant between
releases while every merge to ``develop`` is a new deploy: there the build is
the deploy identity and the version is a label. On prod they move together.

Read once at import: the values cannot change while a process lives, and the
rows that carry them are written on the hot path's tail.
"""

from __future__ import annotations

import os
from typing import Dict

UNKNOWN = "unknown"

APP_VERSION: str = (os.environ.get("APP_VERSION") or "").strip() or UNKNOWN
APP_BUILD: str = (os.environ.get("APP_BUILD") or "").strip() or UNKNOWN


def build_stamp() -> Dict[str, str]:
    """The two attributes a cost row or metric record carries: ``appVersion`` and ``build``."""
    return {"appVersion": APP_VERSION, "build": APP_BUILD}


def health_fields() -> Dict[str, str]:
    """The version fields a ``/health`` or ``/ping`` body reports."""
    return {"version": APP_VERSION, "build": APP_BUILD}

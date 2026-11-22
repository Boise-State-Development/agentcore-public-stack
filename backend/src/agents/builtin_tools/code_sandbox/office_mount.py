"""LibreOffice for ``run_code``: the read-only EFS mount its sessions start with.

Spec: ``docs/specs/skill-code-sandbox.md`` D9. Behind ``CODE_SANDBOX_OFFICE_ENABLED``.

The file system, its access points and the seeding interpreter are built by
``code-sandbox-office-construct.ts``, which publishes their ARNs (service
generated, so not in the Runtime's environment) as JSON in
``/<PROJECT_PREFIX>/agentcore/code-sandbox-office-mount``. This module reads it
once per process and turns it into the ``filesystemConfigurations`` a session
starts with: always the **read-only** access point, which is the only one the
run-time interpreter's role may mount.

A mount that fails fails the whole session start, so ``run_code`` retries
without it and :func:`mark_failed` skips the mount for a few minutes, rather
than paying a failed start on every call. Nothing here runs before the first
token: the first ``run_code`` call reads the parameter.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from apis.shared.feature_flags import code_sandbox_office_enabled

logger = logging.getLogger(__name__)

#: Must match ``OFFICE_MOUNT_SSM_SUFFIX`` in code-sandbox-office-construct.ts.
OFFICE_MOUNT_SSM_SUFFIX = "agentcore/code-sandbox-office-mount"
#: Must match ``OFFICE_MOUNT_PATH`` there.
MOUNT_PATH = "/mnt/office"
#: ``scripts/code-sandbox/seed-office.py`` points ``current`` at the installed
#: build, so an upgrade is one symlink swap and this path never changes.
PROGRAM_DIR = f"{MOUNT_PATH}/current/program"
#: How long a failed mount is skipped before it is tried again.
RETRY_AFTER_FAILURE_SECONDS = 300

_lock = threading.Lock()
_mount: Optional[Dict[str, Any]] = None
_skip_until = 0.0


def parameter_name() -> Optional[str]:
    """``/<PROJECT_PREFIX>/agentcore/code-sandbox-office-mount``, or ``None`` without a prefix."""
    prefix = (os.getenv("PROJECT_PREFIX") or "").strip()
    if not prefix:
        return None
    return f"/{prefix}/{OFFICE_MOUNT_SSM_SUFFIX}"


def _read_parameter(name: str) -> str:
    """The parameter's value. Blocking (SSM); raises on any failure."""
    import boto3

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    client = boto3.client("ssm", region_name=region) if region else boto3.client("ssm")
    return client.get_parameter(Name=name)["Parameter"]["Value"]


def _configuration(value: str) -> Dict[str, Any]:
    """The read-only mount from the parameter's JSON; raises if it is not one."""
    data = json.loads(value)
    file_system_arn = str(data["fileSystemArn"])
    access_point_arn = str(data["readOnlyAccessPointArn"])
    if ":elasticfilesystem:" not in file_system_arn or ":access-point/" not in access_point_arn:
        raise ValueError("the office mount parameter does not name an EFS access point")
    return {
        "efsConfiguration": {
            "fileSystemArn": file_system_arn,
            "accessPointArn": access_point_arn,
            "mountPath": MOUNT_PATH,
        }
    }


def session_mount() -> Optional[List[Dict[str, Any]]]:
    """The ``filesystemConfigurations`` for a ``run_code`` session, or ``None``.

    ``None`` while the flag is off, the parameter can't be read, or a recent
    mount failed. Blocking (one SSM read per process); call via
    ``asyncio.to_thread``. A read that fails is not kept, so it is tried again
    on the next call.
    """
    global _mount
    if not code_sandbox_office_enabled():
        return None
    with _lock:
        if time.monotonic() < _skip_until:
            return None
        known = _mount
    if known is None:
        name = parameter_name()
        if not name:
            logger.warning("run_code: CODE_SANDBOX_OFFICE_ENABLED is on but PROJECT_PREFIX is not set")
            return None
        try:
            known = _configuration(_read_parameter(name))
        except Exception as exc:  # noqa: BLE001 - no mount is the safe answer
            logger.warning("run_code: office mount parameter not readable: %s", exc)
            return None
        with _lock:
            _mount = known
    return [known]


def mark_failed() -> None:
    """Skip the mount for :data:`RETRY_AFTER_FAILURE_SECONDS` after a failed start."""
    global _skip_until
    with _lock:
        _skip_until = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS


def _reset_for_tests() -> None:
    global _mount, _skip_until
    with _lock:
        _mount = None
        _skip_until = 0.0

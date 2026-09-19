"""Process-cached boto3 clients and resources.

WHY THIS EXISTS
---------------
`sessions/metadata.py` alone constructs `boto3.resource("dynamodb")` in 28
separate function bodies, and the repo has 89 such sites across `apis/` and
`agents/`. Each construction resolves an endpoint, builds a credential
resolver and registers event hooks.

Measured: **~327ms for the first construction in a process, then ~1.5ms each.**
That sounds negligible until a single request makes a dozen of them. After
`docs/specs/turn-latency-preamble.md` PR-2 collapsed the preamble's eight
DynamoDB reads into one, `preamble.session_state` still measured 17-21ms on
dev while doing *no IO at all* — six helpers each building a client before
reaching their snapshot short-circuit. That residual is what this module
removes.

A boto3 resource is safe to share: botocore clients are thread-safe for API
calls, and credential refresh is handled inside the shared session. What is
NOT safe is sharing one across a *moto* boundary — see below.

THE MOTO TRAP, AND WHY `reset_cached_clients` EXISTS
----------------------------------------------------
`moto.mock_aws()` is entered per test (`tests/*/conftest.py`). A client built
inside test A's mock context keeps pointing at test A's backend, which is torn
down when that test ends. Cache it, and test B silently talks to a dead
backend — or worse, to a live AWS endpoint.

The failure would be **order-dependent and confusing**, which is exactly the
shape of bug this repo has already paid for elsewhere (a static memo leaking
across SPA spec files). So the cache is explicitly resettable, and the `aws`
fixture resets it on both entry and exit. A test that constructs its own
client directly is unaffected either way.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Keyed by (service_name, region). `region=None` means "whatever the ambient
# configuration resolves to", which is what every existing call site relies on.
_resources: Dict[Tuple[str, Optional[str]], Any] = {}
_clients: Dict[Tuple[str, Optional[str]], Any] = {}

# Construction is not atomic and two threads can race to build the same entry.
# The loser's client is simply discarded — harmless, but the lock keeps the
# dicts consistent and makes the "built once" claim in the docstring true.
_lock = threading.Lock()


def get_resource(service_name: str, region_name: Optional[str] = None) -> Any:
    """A process-cached ``boto3.resource``.

    Falls back to an uncached resource if construction fails inside the lock,
    so a transient credential problem cannot poison the cache for the life of
    the process.
    """
    key = (service_name, region_name)
    cached = _resources.get(key)
    if cached is not None:
        return cached

    import boto3

    with _lock:
        cached = _resources.get(key)
        if cached is not None:
            return cached
        resource = (
            boto3.resource(service_name, region_name=region_name)
            if region_name
            else boto3.resource(service_name)
        )
        _resources[key] = resource
        return resource


def get_client(service_name: str, region_name: Optional[str] = None) -> Any:
    """A process-cached ``boto3.client``."""
    key = (service_name, region_name)
    cached = _clients.get(key)
    if cached is not None:
        return cached

    import boto3

    with _lock:
        cached = _clients.get(key)
        if cached is not None:
            return cached
        client = (
            boto3.client(service_name, region_name=region_name)
            if region_name
            else boto3.client(service_name)
        )
        _clients[key] = client
        return client


def get_dynamodb_table(table_name: str, region_name: Optional[str] = None) -> Any:
    """A DynamoDB ``Table`` bound to the cached resource.

    The `Table` object itself is deliberately NOT cached. It is a thin handle
    over the resource — the expensive part is the resource, which is shared —
    and caching handles by name would keep a dict entry alive per table for no
    measurable gain.
    """
    return get_resource("dynamodb", region_name).Table(table_name)


def reset_cached_clients() -> None:
    """Drop every cached client and resource.

    Exists for tests. `moto.mock_aws()` is entered per test, so a client built
    under one test's mock must never be reused by the next — see the module
    docstring. Production code has no reason to call this.
    """
    with _lock:
        _resources.clear()
        _clients.clear()

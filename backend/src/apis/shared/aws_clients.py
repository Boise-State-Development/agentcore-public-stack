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

THE SHARED SESSION (`shared_boto_session`)
------------------------------------------
The cache above serves callers that ask *this module* for a client. Two SDKs
on the first-turn agent build do not: the AgentCore Memory session manager
and Strands' `BedrockModel` each construct a fresh `boto3.Session` and build
their clients on it. A fresh session re-parses every service model it
touches (the parse is per session, not per process), so a cold first turn
paid that parse several times over — see `docs/specs/turn-path-ttft.md`
§5 P2. Both SDKs accept a session, so `shared_boto_session()` is the one
process-wide session handed to them (behind `memory_shared_clients_enabled`,
an A/B arm) and to `apis/inference_api/warmup.py`, which builds its clients
at container start so a first turn finds them already parsed. Its `client()`
returns one client per configuration, so every caller that asks for the same
service with the same config shares one client and one connection pool.
Reset with everything else: a session built under one moto backend is as
stale as a client built under it.

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

import boto3
from botocore.config import Config as _BotocoreConfig

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


# ---------------------------------------------------------------------------
# The shared session
# ---------------------------------------------------------------------------

# Sized for concurrent sessions sharing one pool. Async persistence writes each
# message through ``asyncio.to_thread``, so a busy container can have dozens of
# ``CreateEvent`` calls in flight on these clients at once, where each session
# manager used to have a pool of its own. Past the pool size urllib3 still
# serves the request, but discards the extra connection afterwards (and warns).
SHARED_SESSION_MAX_POOL_CONNECTIONS = 50


def _shared_client_key(service_name: str, region_name: Optional[str], config: Any) -> Tuple[str, Optional[str], str]:
    # ``Config`` is not hashable; its user-provided options are what make two
    # configs different (the SDKs differ from each other only in user agent).
    options = getattr(config, "_user_provided_options", None) or {}
    return service_name, region_name, repr(sorted(options.items()))


class ClientReusingSession(boto3.Session):
    """A boto3 session whose ``client()`` returns one client per configuration.

    boto3 clients are thread-safe; building them is not, and building one is
    the expensive part, so creation happens once, under a lock. Calls carrying
    anything beyond region and config (explicit credentials, an endpoint
    override) are not ours to share and pass straight through; a keyword
    passed as ``None`` (Strands passes ``endpoint_url=None``) is not an
    override and does not defeat the sharing.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shared_clients: Dict[Tuple[str, Optional[str], str], Any] = {}
        self._shared_clients_lock = threading.Lock()

    def client(self, service_name: str, region_name: Optional[str] = None, config: Any = None, **kwargs: Any) -> Any:  # type: ignore[override]
        overrides = {key: value for key, value in kwargs.items() if value is not None}
        if overrides:
            return super().client(service_name, region_name=region_name, config=config, **overrides)
        key = _shared_client_key(service_name, region_name, config)
        with self._shared_clients_lock:
            client = self._shared_clients.get(key)
            if client is None:
                pooled = _BotocoreConfig(max_pool_connections=SHARED_SESSION_MAX_POOL_CONNECTIONS)
                merged = pooled.merge(config) if config is not None else pooled
                client = super().client(service_name, region_name=region_name, config=merged)
                self._shared_clients[key] = client
            return client


_shared_session: Optional[ClientReusingSession] = None


def shared_boto_session() -> ClientReusingSession:
    """The process-wide session the agent build's SDK clients are built on.

    Built by ``apis/inference_api/warmup.py`` at container start, so a first
    turn that reaches for it finds its service models already parsed; built
    here on first use otherwise.
    """
    global _shared_session
    session = _shared_session
    if session is not None:
        return session
    with _lock:
        if _shared_session is None:
            _shared_session = ClientReusingSession()
        return _shared_session


def reset_cached_clients() -> None:
    """Drop every cached client and resource, and the shared session.

    Exists for tests. `moto.mock_aws()` is entered per test, so a client built
    under one test's mock must never be reused by the next — see the module
    docstring. Production code has no reason to call this.
    """
    global _shared_session
    with _lock:
        _resources.clear()
        _clients.clear()
        _shared_session = None

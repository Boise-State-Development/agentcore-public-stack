"""Where a shared module's blocking call runs: a worker thread, or inline on the loop.

Shared code that both app-api and the AgentCore Runtime call makes blocking boto3
calls from ``async`` functions. The two processes want those calls run differently:

- **app-api** serves every request from one event loop, the ALB health check
  included. A blocking call there stalls all of them, and a burst of them got
  every task replaced on 2026-11-16 (a bulk document delete). So it runs them in a
  worker thread. This is the default.
- **The Runtime's turn path** awaits these lookups one after another before the
  first token, with nothing else on the loop, and V2 pins one ``(user, session)``
  per container. A thread hop (~36 µs p50, ~104 µs p99) would only add latency
  there, against the time-to-first-token budget. So ``inference_api/main.py``
  calls :func:`run_blocking_io_inline` at startup and the calls stay inline,
  exactly as before.

The setting is per process, set once at startup, and read on every call.
"""

import asyncio
from typing import Callable, TypeVar

T = TypeVar("T")

_inline = False


def run_blocking_io_inline(inline: bool = True) -> None:
    """Run :func:`run_blocking` calls on the loop instead of a thread (the Runtime)."""
    global _inline
    _inline = inline


def blocking_io_runs_inline() -> bool:
    """True when this process runs :func:`run_blocking` calls on the loop."""
    return _inline


async def run_blocking(fn: Callable[..., T], /, *args, **kwargs) -> T:
    """Call ``fn`` in a worker thread, or inline where this process asked for that.

    Build any boto3 resource or ``Table`` on the caller's side and pass the bound
    method in, so only the network call crosses into the thread.
    """
    if _inline:
        return fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)

"""Reading and writing the conversation archive bucket.

Writes are fire-and-forget by design. The runtime schedules one after a turn's
``done`` has already gone to the client, so nothing here sits on the
time-to-first-token path or delays the response; a failed write is logged and
dropped (a later turn of the same session does not repair it — the backfill
script and the daily reconciler are the repair paths).

The bucket is resolved from ``CONVERSATION_ARCHIVE_BUCKET_NAME`` (app-api, local
runs), else from SSM ``/{PROJECT_PREFIX}/conversations/archive-bucket-name``.
The runtime takes the SSM route because its 50 environment variables are
nearly spent; the lookup happens once per process, after a turn has finished.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Set

from apis.shared.aws_clients import get_client
from apis.shared.conversation_archive.documents import ArchivedTurn, parse_archive_key, session_prefix

logger = logging.getLogger(__name__)

BUCKET_ENV_VAR = "CONVERSATION_ARCHIVE_BUCKET_NAME"
BUCKET_SSM_SUFFIX = "conversations/archive-bucket-name"

# A failed SSM lookup is retried after this long rather than on every turn.
_NEGATIVE_CACHE_SECONDS = 300.0

_bucket_lock = threading.Lock()
_cached_bucket: Optional[str] = None
_bucket_miss_at: Optional[float] = None

# Strong references to in-flight writes. The event loop holds tasks weakly, so
# a fire-and-forget task could otherwise be garbage-collected before it lands.
_pending: Set["asyncio.Task[Any]"] = set()


def reset_bucket_cache() -> None:
    """Forget the resolved bucket (tests, and a process whose config changed)."""
    global _cached_bucket, _bucket_miss_at
    with _bucket_lock:
        _cached_bucket = None
        _bucket_miss_at = None


def _region() -> Optional[str]:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def archive_bucket_name() -> Optional[str]:
    """The archive bucket, or None when this environment has none."""
    global _cached_bucket, _bucket_miss_at
    from_env = os.environ.get(BUCKET_ENV_VAR, "").strip()
    if from_env:
        return from_env
    if _cached_bucket is not None:
        return _cached_bucket
    if _bucket_miss_at is not None and time.monotonic() - _bucket_miss_at < _NEGATIVE_CACHE_SECONDS:
        return None
    prefix = os.environ.get("PROJECT_PREFIX", "").strip()
    if not prefix:
        return None
    with _bucket_lock:
        if _cached_bucket is not None:
            return _cached_bucket
        try:
            response = get_client("ssm", _region()).get_parameter(Name=f"/{prefix}/{BUCKET_SSM_SUFFIX}")
            _cached_bucket = response["Parameter"]["Value"]
            _bucket_miss_at = None
        except Exception as exc:  # noqa: BLE001 — a missing parameter means "no archive here"
            logger.warning("Conversation archive bucket unresolved (%s); archive writes skipped", type(exc).__name__)
            _bucket_miss_at = time.monotonic()
            return None
        return _cached_bucket


def put_turns(turns: Sequence[ArchivedTurn]) -> int:
    """Write each turn's object; returns how many landed. Never raises.

    Re-writing a turn's key replaces it, so a retried or repeated write (a
    paused turn archived again once it resumes) is idempotent.
    """
    if not turns:
        return 0
    bucket = archive_bucket_name()
    if not bucket:
        return 0
    s3 = get_client("s3", _region())
    written = 0
    for turn in turns:
        try:
            s3.put_object(
                Bucket=bucket,
                Key=turn.key,
                Body=turn.to_json(),
                ContentType="application/json",
            )
            written += 1
        except Exception:  # noqa: BLE001 — best-effort; see module docstring
            logger.warning(
                "Conversation archive put failed for message %s", turn.message_index, exc_info=True
            )
    return written


def delete_session_archive(user_id: str, session_id: str) -> int:
    """Delete every archived turn of one session; returns how many went.

    Not gated on the index flag: a deployment that turned indexing off must
    still be able to remove what it already wrote when a user deletes the
    conversation. Each deletion raises S3's *Object Deleted* event, which is
    how the search index learns to drop the turn. Never raises.
    """
    bucket = archive_bucket_name()
    if not bucket:
        return 0
    try:
        prefix = session_prefix(user_id, session_id)
    except ValueError:
        logger.warning("Conversation archive delete skipped: unusable session or user id")
        return 0
    s3 = get_client("s3", _region())
    deleted = 0
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if not keys:
                continue
            # A list page holds at most 1,000 keys, the DeleteObjects limit.
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            errors = response.get("Errors", [])
            deleted += len(keys) - len(errors)
            if errors:
                logger.warning("Conversation archive delete left %d object(s) behind", len(errors))
    except Exception:  # noqa: BLE001 — the daily reconciler sweeps what this misses
        logger.warning("Conversation archive delete failed", exc_info=True)
    if deleted:
        logger.info("Deleted %d archived turn(s) for a deleted session", deleted)
    return deleted


#: Parallel GETs when reading a session back. A session is tens of turns; this
#: keeps a long one from taking a round trip per turn in series.
_READ_WORKERS = 8


def read_session_turns(user_id: str, session_id: str) -> List[ArchivedTurn]:
    """Every archived turn of one session, in conversation order. Never raises.

    The messages route's fallback for a session whose Memory events have
    expired. An object whose body names a different user, session or turn
    than its key is skipped, the same refusal the index consumer makes, so a
    malformed write can never show one user's words in another's session.
    Any failure reads as "nothing archived": the caller already has an empty
    history to show.
    """
    bucket = archive_bucket_name()
    if not bucket:
        return []
    try:
        prefix = session_prefix(user_id, session_id)
    except ValueError:
        return []
    s3 = get_client("s3", _region())
    try:
        keys: List[str] = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning("Conversation archive listing failed", exc_info=True)
        return []
    if not keys:
        return []

    def fetch(key: str) -> Optional[ArchivedTurn]:
        try:
            turn = ArchivedTurn.from_json(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception:  # noqa: BLE001 — one unreadable turn must not hide the rest
            logger.warning("Conversation archive object unreadable; skipped", exc_info=True)
            return None
        if parse_archive_key(key) != (turn.user_id, turn.session_id, turn.message_index):
            logger.warning("Conversation archive object does not match its key; skipped")
            return None
        return turn

    with ThreadPoolExecutor(max_workers=min(_READ_WORKERS, len(keys))) as pool:
        turns = [turn for turn in pool.map(fetch, keys) if turn is not None]
    return sorted(turns, key=lambda turn: turn.message_index)


async def write_turns(turns: Sequence[ArchivedTurn]) -> int:
    """``put_turns`` off the event loop."""
    return await asyncio.to_thread(put_turns, turns)


def schedule(job: Callable[[], Awaitable[Any]]) -> None:
    """Run ``job`` as a background task the caller does not wait for.

    Needs a running event loop; outside one (a sync caller) the job is
    dropped with a log line rather than raising into the caller's path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Conversation archive write dropped: no running event loop")
        return

    async def _run() -> None:
        try:
            await job()
        except Exception:  # noqa: BLE001
            logger.warning("Conversation archive background write failed", exc_info=True)

    task = loop.create_task(_run())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_pending() -> None:
    """Wait for every scheduled write (tests, and an orderly shutdown)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)

"""Artifact writer — S3 upload + DynamoDB version/HEAD rows.

Frozen cross-PR contract (must stay in sync with the render Lambda
`backend/src/lambdas/artifact_render/handler.py` and the app-api minter
`backend/src/apis/app_api/artifacts/service.py`):

  Version row : PK=USER#{user_id}  SK=ARTIFACT#{aid}#V#{version:05d}
                attrs storage="s3", content_key, content_type
  HEAD row    : PK=USER#{user_id}  SK=ARTIFACT#{aid}#HEAD
                + GSI1PK=SESSION#{session_id}
                + GSI1SK=ARTIFACT#{updated_at}#{aid}   (SessionIndex)
  S3 layout   : {user_id}/{aid}/v{n}/index.html

Versions are immutable (no DeleteObject grant in inference-api) — an
update writes a new version and re-points HEAD.

Markdown artifacts: when `content_type` is a Markdown type, the model
authors raw Markdown but S3 stores a self-contained HTML render wrapper
(the writer owns rendering — the render Lambda is a pass-through). The
version/HEAD rows keep the authored `content_type` (`text/markdown`) so
the card badge and list stay truthful; the render Lambda maps that type
to a `text/html` HTTP response so the browser renders the wrapper.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_DEFAULT_CONTENT_TYPE = "text/html; charset=utf-8"
# What S3 physically holds (and the HTTP type the render Lambda emits)
# for a Markdown artifact: a self-contained HTML render wrapper.
_RENDERED_CONTENT_TYPE = "text/html; charset=utf-8"
_MARKDOWN_MIME_TYPES = frozenset({"text/markdown", "text/x-markdown"})

# Markdown is base64-embedded so no character ever needs HTML/JS escaping
# and there is no second network fetch (the artifact-origin CSP sets
# connect-src 'none'). The rendered HTML is intentionally NOT sanitized:
# this document runs in the same null-origin sandboxed iframe as HTML
# artifacts, so its containment story is identical to theirs. `marked` is
# pinned (dependency-free single module) and loaded from esm.sh, which the
# artifact-origin CSP allows under script-src.
_MARKDOWN_RENDER_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__ARTIFACT_TITLE__</title>
<style>
  :root { color-scheme: light dark; }
  body {
    margin-inline: auto;
    max-width: 56rem;
    padding: 2.5rem clamp(1rem, 5vw, 4rem);
    font: 16px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
      Helvetica, Arial, sans-serif;
    color: #1f2328;
    background: #ffffff;
  }
  h1, h2, h3, h4 { line-height: 1.25; margin: 2rem 0 1rem; font-weight: 600; }
  h1 { font-size: 2rem; border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; }
  h2 { font-size: 1.5rem; border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; }
  h3 { font-size: 1.25rem; }
  a { color: #0969da; }
  p, ul, ol, blockquote, table, pre { margin: 0 0 1rem; }
  ul, ol { padding-left: 2rem; }
  code {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas,
      monospace;
    font-size: .9em;
    background: rgba(175, 184, 193, .2);
    padding: .2em .4em;
    border-radius: 6px;
  }
  pre { background: #f6f8fa; padding: 1rem; border-radius: 6px; overflow: auto; }
  pre code { background: none; padding: 0; font-size: .875em; }
  blockquote {
    margin-left: 0;
    padding: 0 1rem;
    color: #59636e;
    border-left: .25rem solid #d0d7de;
  }
  table { border-collapse: collapse; display: block; overflow: auto; }
  th, td { border: 1px solid #d0d7de; padding: .4rem .8rem; }
  th { background: #f6f8fa; }
  img { max-width: 100%; }
  hr { border: none; border-top: 1px solid #d0d7de; margin: 2rem 0; }
  @media (prefers-color-scheme: dark) {
    body { color: #e6edf3; background: #0d1117; }
    h1, h2 { border-bottom-color: #30363d; }
    a { color: #4493f8; }
    code { background: rgba(110, 118, 129, .4); }
    pre { background: #161b22; }
    blockquote { color: #9198a1; border-left-color: #30363d; }
    th, td { border-color: #30363d; }
    th { background: #161b22; }
    hr { border-top-color: #30363d; }
  }
</style>
</head>
<body>
<main id="content" aria-live="polite">Rendering…</main>
<script type="application/x-markdown-base64" id="md-src">__ARTIFACT_MD_B64__</script>
<script type="module">
  import { marked } from "https://esm.sh/marked@14.1.4";
  const b64 = document.getElementById("md-src").textContent.trim();
  const md = new TextDecoder().decode(
    Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)),
  );
  const el = document.getElementById("content");
  try {
    el.innerHTML = marked.parse(md, { gfm: true, breaks: false });
    const h1 = document.querySelector("h1");
    if (h1 && h1.textContent.trim()) document.title = h1.textContent.trim();
  } catch (err) {
    el.textContent = "Could not render this Markdown document.";
  }
</script>
</body>
</html>
"""


def _is_markdown(content_type: Optional[str]) -> bool:
    """True for a Markdown MIME type, ignoring any `; charset=` suffix."""
    bare = (content_type or "").split(";")[0].strip().lower()
    return bare in _MARKDOWN_MIME_TYPES


def _wrap_markdown(title: str, markdown: str) -> str:
    """Render Markdown source into a self-contained HTML document."""
    md_b64 = base64.b64encode(markdown.encode("utf-8")).decode("ascii")
    return _MARKDOWN_RENDER_TEMPLATE.replace(
        "__ARTIFACT_TITLE__", html.escape(title or "Markdown document")
    ).replace("__ARTIFACT_MD_B64__", md_b64)


_cached_bucket: Optional[str] = None
_cached_table: Optional[str] = None
_ssm_client = None
_s3_client = None
_ddb_resource = None


class ArtifactError(Exception):
    """Base class for artifact write failures."""


class ArtifactNotFoundError(ArtifactError):
    """Update target does not exist for this user."""


class ArtifactConfigError(ArtifactError):
    """Artifacts feature is not configured for this environment."""


def _reset_caches_for_tests() -> None:
    global _cached_bucket, _cached_table, _ssm_client, _s3_client, _ddb_resource
    _cached_bucket = None
    _cached_table = None
    _ssm_client = None
    _s3_client = None
    _ddb_resource = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _resolve(env_var: str, ssm_suffix: str) -> str:
    """Env var first, then SSM under the runtime's PROJECT_PREFIX.

    inference-api exposes PROJECT_PREFIX and holds ssm:GetParameter on
    `/{prefix}/*`, so the artifacts params published by the artifacts
    stack are readable without any extra wiring."""
    value = os.environ.get(env_var)
    if value:
        return value
    global _ssm_client
    prefix = os.environ.get("PROJECT_PREFIX")
    if not prefix:
        raise ArtifactConfigError(
            f"{env_var} unset and PROJECT_PREFIX unavailable"
        )
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm", region_name=_region())
    try:
        resp = _ssm_client.get_parameter(
            Name=f"/{prefix}/artifacts/{ssm_suffix}"
        )
    except ClientError as exc:
        raise ArtifactConfigError(
            f"artifacts {ssm_suffix} parameter not found"
        ) from exc
    return resp["Parameter"]["Value"]


def _bucket_name() -> str:
    global _cached_bucket
    if _cached_bucket is None:
        _cached_bucket = _resolve("S3_ARTIFACTS_BUCKET_NAME", "bucket-name")
    return _cached_bucket


def _table():
    global _cached_table, _ddb_resource
    if _cached_table is None:
        _cached_table = _resolve("DYNAMODB_ARTIFACTS_TABLE_NAME", "table-name")
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=_region())
    return _ddb_resource.Table(_cached_table)


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=_region())
    return _s3_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _put_object(user_id: str, artifact_id: str, version: int,
                content: str, content_type: str, title: str) -> str:
    key = f"{user_id}/{artifact_id}/v{version}/index.html"
    if _is_markdown(content_type):
        body = _wrap_markdown(title, content)
        object_content_type = _RENDERED_CONTENT_TYPE
    else:
        body = content
        object_content_type = content_type
    _s3().put_object(
        Bucket=_bucket_name(),
        Key=key,
        Body=body.encode("utf-8"),
        ContentType=object_content_type,
    )
    return key


def create_artifact_record(
    user_id: str,
    session_id: str,
    title: str,
    content: str,
    content_type: str,
) -> tuple[str, int]:
    """Create v1 of a new artifact. Returns (artifact_id, version)."""
    artifact_id = uuid.uuid4().hex
    version = 1
    content_type = content_type or _DEFAULT_CONTENT_TYPE
    now = _now_iso()
    content_key = _put_object(
        user_id, artifact_id, version, content, content_type, title
    )

    pk = f"USER#{user_id}"
    common = {
        "storage": "s3",
        "content_key": content_key,
        "content_type": content_type,
        "version": version,
        "artifact_id": artifact_id,
        "user_id": user_id,
        "session_id": session_id,
        "title": title,
        "created_at": now,
    }
    table = _table()
    try:
        table.put_item(
            Item={**common, "PK": pk, "SK": f"ARTIFACT#{artifact_id}#V#{version:05d}"},
            ConditionExpression="attribute_not_exists(SK)",
        )
        table.put_item(
            Item={
                **common,
                "PK": pk,
                "SK": f"ARTIFACT#{artifact_id}#HEAD",
                "updated_at": now,
                "GSI1PK": f"SESSION#{session_id}",
                "GSI1SK": f"ARTIFACT#{now}#{artifact_id}",
            },
            ConditionExpression="attribute_not_exists(SK)",
        )
    except ClientError as exc:
        raise ArtifactError("failed to write artifact metadata") from exc

    logger.info(
        "created artifact user=%s artifact=%s v=%s session=%s",
        user_id, artifact_id, version, session_id,
    )
    return artifact_id, version


def update_artifact_record(
    user_id: str,
    artifact_id: str,
    content: str,
    title: Optional[str],
    content_type: Optional[str],
) -> int:
    """Append a new immutable version and re-point HEAD. Returns version."""
    pk = f"USER#{user_id}"
    table = _table()
    try:
        head = table.get_item(
            Key={"PK": pk, "SK": f"ARTIFACT#{artifact_id}#HEAD"}
        ).get("Item")
    except ClientError as exc:
        raise ArtifactError("artifact metadata lookup failed") from exc
    if not head:
        raise ArtifactNotFoundError(artifact_id)

    current = int(head["version"])
    version = current + 1
    title = title or head.get("title", "")
    content_type = content_type or head.get("content_type") or _DEFAULT_CONTENT_TYPE
    now = _now_iso()
    content_key = _put_object(
        user_id, artifact_id, version, content, content_type, title
    )

    common = {
        "storage": "s3",
        "content_key": content_key,
        "content_type": content_type,
        "version": version,
        "artifact_id": artifact_id,
        "user_id": user_id,
        "session_id": head.get("session_id", ""),
        "title": title,
        "created_at": head.get("created_at", now),
    }
    try:
        table.put_item(
            Item={**common, "PK": pk, "SK": f"ARTIFACT#{artifact_id}#V#{version:05d}"},
            ConditionExpression="attribute_not_exists(SK)",
        )
        # Optimistic lock: HEAD must still be at the version we read, so
        # two concurrent updates can't silently clobber each other.
        table.put_item(
            Item={
                **common,
                "PK": pk,
                "SK": f"ARTIFACT#{artifact_id}#HEAD",
                "updated_at": now,
                "GSI1PK": f"SESSION#{head.get('session_id', '')}",
                "GSI1SK": f"ARTIFACT#{now}#{artifact_id}",
            },
            ConditionExpression="version = :cur",
            ExpressionAttributeValues={":cur": current},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ArtifactError(
                "artifact changed concurrently; retry the update"
            ) from exc
        raise ArtifactError("failed to write artifact metadata") from exc

    logger.info(
        "updated artifact user=%s artifact=%s v=%s", user_id, artifact_id, version
    )
    return version


def set_produced_by_message_index(
    user_id: str, artifact_id: str, message_index: int
) -> None:
    """Stamp the HEAD row with the index of the assistant message that
    produced (or last updated) the artifact this turn.

    The artifact tool can't know this at write time — the turn isn't
    finished — so the stream coordinator writes it back post-turn using
    the same odd-position index it already computes for per-message
    metadata (`initial_message_count + 2*i + 1`). That index matches the
    `idx` the messages endpoint enumerates on reload, so the SPA can
    render the card inline after the right assistant message.

    Best-effort: a SET on a single attribute, keyed by the HEAD row, that
    deliberately does not touch `version` so it can never collide with
    the update_artifact optimistic lock. Failures are swallowed by the
    caller (linkage is a UX nicety, never worth breaking a turn over).
    """
    table = _table()
    table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": f"ARTIFACT#{artifact_id}#HEAD"},
        UpdateExpression="SET produced_by_message_index = :idx",
        ExpressionAttributeValues={":idx": message_index},
        ConditionExpression="attribute_exists(SK)",
    )


_SESSION_INDEX = "SessionIndex"


def list_session_artifacts(user_id: str, session_id: str) -> list[dict]:
    """Current HEAD of every artifact written in a chat session.

    Read side of the same SessionIndex GSI the app-api list endpoint
    uses; the stream coordinator calls this post-turn to emit the live
    `artifact` SSE event. Only HEAD rows carry GSI1PK/GSI1SK, so the
    query returns one row per artifact (its current version). GSI1PK is
    SESSION#-scoped (not user-scoped) so every row is re-checked against
    the authenticated user's id.
    """
    table = _table()
    items: list[dict] = []
    kwargs: dict = {
        "IndexName": _SESSION_INDEX,
        "KeyConditionExpression": Key("GSI1PK").eq(f"SESSION#{session_id}"),
        "ScanIndexForward": False,  # GSI1SK embeds updated_at → newest first
    }
    try:
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except ClientError as exc:
        raise ArtifactError("artifact list query failed") from exc

    out: list[dict] = []
    for item in items:
        if item.get("user_id") != user_id:
            continue
        out.append(
            {
                "artifact_id": item.get("artifact_id", ""),
                "version": int(item.get("version", 0)),
                "title": item.get("title", ""),
                "content_type": item.get(
                    "content_type", _DEFAULT_CONTENT_TYPE
                ),
                "updated_at": item.get("updated_at", ""),
                "created_at": item.get("created_at"),
                "produced_by_message_index": item.get(
                    "produced_by_message_index"
                ),
            }
        )
    return out

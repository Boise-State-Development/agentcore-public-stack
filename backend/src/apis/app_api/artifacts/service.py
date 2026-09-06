"""Render-token minting service.

Mints the HS256 JWT that the artifact render Lambda verifies. The claim
shape, signing key, and DynamoDB lookup keys are a frozen cross-PR
contract with `backend/src/lambdas/artifact_render/handler.py` — any
change here must be mirrored in that verifier (and vice versa).

SECURITY: the minted token is a bearer credential carried in a URL.
Never log the token or the assembled URL — log identifiers only.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
import jwt
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from apis.shared.auth import User
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

# Frozen contract — must match the render Lambda's _verify_token.
_ISS = "app-api"
_AUD = "artifact-render"
# The verifier hard-caps exp - iat at 600s. 120s comfortably covers an
# iframe load while keeping a leaked-in-a-log token useless almost
# immediately.
_TTL_SECONDS = 120

_secret_lock = threading.Lock()
_table_lock = threading.Lock()
_s3_lock = threading.Lock()
_cached_signing_key: Optional[str] = None
_secrets_client = None
_ddb_table = None
_s3_client = None
_cached_bucket: Optional[str] = None

# Inline code-view ceiling. Past this the SPA shows a "too large to
# preview — download instead" affordance rather than highlighting a
# multi-MB blob in the DOM.
_MAX_CONTENT_BYTES = 2 * 1024 * 1024

# Bare Markdown MIME types. Duplicated (not imported) from the agent
# writer: the import-boundary rule forbids app_api importing from
# agents/, and this set rarely changes.
_MARKDOWN_MIME_TYPES = frozenset({"text/markdown", "text/x-markdown"})

# The writer embeds the authored Markdown as base64 in this exact script
# tag inside the rendered HTML wrapper (agents/builtin_tools/artifacts
# _MARKDOWN_RENDER_TEMPLATE). We unwrap it back to source for code view.
_MD_SRC_RE = re.compile(
    r'<script type="application/x-markdown-base64" id="md-src">'
    r"(?P<b64>[^<]*)</script>"
)


class RenderTokenError(Exception):
    """Base class for render-token failures."""


class ArtifactNotFoundError(RenderTokenError):
    """No version record for the requested (user, artifact, version)."""


class RenderTokenConfigError(RenderTokenError):
    """Required environment / AWS configuration is missing or unusable."""


class ArtifactQueryError(RenderTokenError):
    """A backing-store query failed at runtime (throttle, timeout,
    transient DynamoDB error) — distinct from a misconfiguration: the
    feature is set up correctly, the request just couldn't be served."""


class ArtifactTitleError(RenderTokenError):
    """A caller-supplied artifact title is empty or over the length cap.

    A 400, not a 500 — it describes the request, not the service. Kept in
    this exception family so the routes' existing except-ladder shape
    still applies.
    """


class ArtifactTooLargeError(RenderTokenError):
    """The artifact body exceeds the inline code-view cap. The caller
    should fall back to the download path rather than streaming a huge
    blob into the SPA's DOM for syntax highlighting."""


def _reset_caches_for_tests() -> None:
    """Drop process-wide singletons so test order can't leak a stale
    signing key, secrets client, or DDB table handle."""
    global _cached_signing_key, _secrets_client, _ddb_table
    global _s3_client, _cached_bucket
    _s3_client = None
    _cached_bucket = None
    _cached_signing_key = None
    _secrets_client = None
    _ddb_table = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _signing_key() -> str:
    """Fetch and cache the HMAC signing key. The secret is a plain
    string (Secrets Manager generateSecretString, no JSON wrapper) —
    same shape as the BFF cookie data key."""
    global _cached_signing_key, _secrets_client
    if _cached_signing_key is not None:
        return _cached_signing_key
    with _secret_lock:
        if _cached_signing_key is not None:
            return _cached_signing_key
        arn = os.environ.get("ARTIFACTS_RENDER_TOKEN_SECRET_ARN", "")
        if not arn:
            raise RenderTokenConfigError(
                "ARTIFACTS_RENDER_TOKEN_SECRET_ARN is not set"
            )
        if _secrets_client is None:
            _secrets_client = boto3.client(
                "secretsmanager", region_name=_region()
            )
        try:
            response = _secrets_client.get_secret_value(SecretId=arn)
        except ClientError as exc:
            raise RenderTokenConfigError(
                "could not read render token secret"
            ) from exc
        key = response.get("SecretString") or ""
        if not key:
            raise RenderTokenConfigError("render token secret is empty")
        _cached_signing_key = key
        return key


def _table():
    global _ddb_table
    if _ddb_table is not None:
        return _ddb_table
    with _table_lock:
        if _ddb_table is not None:
            return _ddb_table
        name = os.environ.get("DYNAMODB_ARTIFACTS_TABLE_NAME", "")
        if not name:
            raise RenderTokenConfigError(
                "DYNAMODB_ARTIFACTS_TABLE_NAME is not set"
            )
        _ddb_table = boto3.resource(
            "dynamodb", region_name=_region()
        ).Table(name)
        return _ddb_table


def _origin() -> str:
    """The artifact origin the render token is bound to.

    Validated like the signing key and table so a misconfigured deploy
    fails closed with a 500 — never returns a usable token embedded in a
    relative, unloadable URL. Infra sets this env var alongside the
    secret ARN and table name, so an empty value here means a broken
    artifacts deploy, not a disabled feature."""
    origin = os.environ.get("ARTIFACTS_ORIGIN", "").strip().rstrip("/")
    if not origin:
        raise RenderTokenConfigError("ARTIFACTS_ORIGIN is not set")
    return origin


def _assert_version_exists(
    user_id: str, artifact_id: str, version: int
) -> None:
    """Confirm the exact version row exists and belongs to this user.

    Building the PK from the authenticated user's id is what scopes the
    token: a caller can never mint for another user's artifact. The
    SK zero-pad must match the verifier's `V#{version:05d}`."""
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _table().get_item(
            Key={"PK": f"USER#{user_id}", "SK": sk}
        )
    except ClientError as exc:
        raise RenderTokenConfigError(
            "artifact metadata lookup failed"
        ) from exc
    if "Item" not in result:
        raise ArtifactNotFoundError("artifact version not found")


class RenderTokenService:
    def mint(
        self,
        *,
        user_id: str,
        artifact_id: str,
        version: int,
        session_id: Optional[str],
    ) -> tuple[str, int]:
        """Validate config + ownership/existence, then mint a token.

        Returns (render_url, exp_unix). Raises ArtifactNotFoundError or
        RenderTokenConfigError. Origin is resolved first so a misconfig
        fails closed before any DDB call or credential is generated."""
        origin = _origin()
        _assert_version_exists(user_id, artifact_id, version)
        now = int(time.time())
        exp = now + _TTL_SECONDS
        claims = {
            "iss": _ISS,
            "aud": _AUD,
            "sub": user_id,
            "aid": artifact_id,
            "ver": version,
            "sid": session_id or "",
            "iat": now,
            "exp": exp,
        }
        token = jwt.encode(claims, _signing_key(), algorithm="HS256")
        logger.info(
            "minted render token user=%s artifact=%s v=%s",
            user_id,
            scrub_log(artifact_id),
            scrub_log(version),
        )
        return f"{origin}/?t={token}", exp

    def mint_for_share(
        self, *, share_id: str, viewer: User
    ) -> tuple[str, int]:
        """Mint a render token for a *shared* artifact version.

        Returns (render_url, exp_unix). Raises ShareNotFoundError,
        ShareAccessDeniedError, ArtifactNotFoundError, or
        RenderTokenConfigError. Origin resolves first so a misconfigured
        deploy fails closed before any DDB call.

        ############################################################
        # SECURITY — READ BEFORE CHANGING `sub` BELOW.
        #
        # `sub` is the OWNER's user id, not the viewer's. That is
        # deliberate and load-bearing: the render Lambda uses `sub`
        # purely as the DynamoDB partition key it builds the lookup
        # from (PK = USER#{sub}), and performs no ownership comparison
        # of its own — it never sees the viewer. `sub` here is an
        # ADDRESS, not an identity assertion.
        #
        # Setting `sub` to the viewer would not "fix" anything; it
        # would point the Lambda at the viewer's own partition and the
        # shared artifact would simply 404.
        #
        # The consequence is that `_check_share_access` immediately
        # below is the ONLY thing standing between "sharing" and "read
        # any artifact by id". Do not reorder it, do not make it
        # conditional, and do not move minting ahead of it.
        #
        # The real viewer identity travels in `vwr`, and the grant it
        # was issued under in `shr`, so the render log can attribute
        # the view correctly rather than crediting it to the owner.
        # The deployed verifier validates a fixed claim list and has no
        # extras rejection, so these are forward-compatible additions
        # requiring no Lambda change or deploy sequencing.
        ############################################################
        """
        origin = _origin()
        share = _get_share_lookup(share_id)
        if not share:
            raise ShareNotFoundError("share not found")
        _check_share_access(share, viewer)

        owner_id = str(share.get("owner_id", ""))
        artifact_id = str(share.get("artifact_id", ""))
        version = int(share.get("version", 0))
        # The share row is denormalized metadata; the version row is the
        # truth. Re-assert it so a share whose artifact version has gone
        # away 404s here rather than minting a token that renders the
        # Lambda's error page inside the recipient's iframe.
        _assert_version_exists(owner_id, artifact_id, version)

        now = int(time.time())
        exp = now + _TTL_SECONDS
        claims = {
            "iss": _ISS,
            "aud": _AUD,
            "sub": owner_id,  # DynamoDB partition — NOT an identity claim.
            "aid": artifact_id,
            "ver": version,
            "sid": "",
            "vwr": viewer.user_id,  # who actually looked (audit)
            "shr": share_id,  # under which grant (audit)
            "iat": now,
            "exp": exp,
        }
        token = jwt.encode(claims, _signing_key(), algorithm="HS256")
        logger.info(
            "minted shared render token share=%s owner=%s viewer=%s "
            "artifact=%s v=%s",
            scrub_log(share_id),
            scrub_log(owner_id),
            scrub_log(viewer.user_id),
            scrub_log(artifact_id),
            scrub_log(version),
        )
        return f"{origin}/?t={token}", exp


    def mint_for_conversation_share(
        self,
        *,
        owner_id: str,
        artifact_id: str,
        version: int,
        conversation_share_id: str,
        viewer: User,
    ) -> tuple[str, int]:
        """Mint a render token for an artifact inside a *shared conversation*.

        Returns (render_url, exp_unix). Raises ArtifactNotFoundError or
        RenderTokenConfigError.

        ############################################################
        # SECURITY — this method performs NO access control.
        #
        # Unlike `mint_for_share`, which resolves and checks its own
        # share record, this one is handed an owner id and an artifact
        # id by its caller. Both the conversation-share ACL check and
        # the "is this artifact actually in that share's snapshot"
        # check happen in `shares/service.py`, because the grant lives
        # in the shared-conversations table, which this module does not
        # read.
        #
        # So the ONLY safe caller is one that has already done both. Do
        # not expose this on a route, do not call it with an
        # artifact id taken from a request, and do not add a default
        # for `owner_id`. Given `sub` is a partition address (see
        # `mint_for_share`), an unchecked call here is "read any
        # artifact by id" with extra steps.
        ############################################################
        """
        origin = _origin()
        # The snapshot is denormalized metadata; the version row is the
        # truth. Re-assert it so an artifact deleted since the
        # conversation was shared 404s here rather than minting a token
        # that renders the Lambda's error page in the recipient's frame.
        _assert_version_exists(owner_id, artifact_id, version)

        now = int(time.time())
        exp = now + _TTL_SECONDS
        claims = {
            "iss": _ISS,
            "aud": _AUD,
            "sub": owner_id,  # DynamoDB partition — NOT an identity claim.
            "aid": artifact_id,
            "ver": version,
            "sid": "",
            "vwr": viewer.user_id,  # who actually looked (audit)
            # The grant is a CONVERSATION share, not an artifact share.
            # Prefixed so a log or a later Lambda can tell the two apart
            # rather than silently reading it as an artifact share id.
            "shr": f"conv:{conversation_share_id}",
            "iat": now,
            "exp": exp,
        }
        token = jwt.encode(claims, _signing_key(), algorithm="HS256")
        logger.info(
            "minted conversation-share render token share=%s owner=%s "
            "viewer=%s artifact=%s v=%s",
            scrub_log(conversation_share_id),
            scrub_log(owner_id),
            scrub_log(viewer.user_id),
            scrub_log(artifact_id),
            scrub_log(version),
        )
        return f"{origin}/?t={token}", exp


def get_render_token_service() -> RenderTokenService:
    return RenderTokenService()


# Frozen contract — the HEAD row + SessionIndex keys the artifact writer
# (backend/src/agents/builtin_tools/artifacts/service.py) emits.
_SESSION_INDEX = "SessionIndex"
# Sparse index over HEAD rows only — read the block comment in
# `list_for_user` before assuming a missing artifact is a query bug.
_USER_INDEX = "UserArtifactsIndex"


class ArtifactListService:
    """List every version of every artifact created in a chat session.

    Two-step, because the SessionIndex GSI only projects HEAD rows (the
    writer attaches GSI1PK/GSI1SK to the HEAD put only):

      1. Query SessionIndex by GSI1PK=SESSION#{sid} to discover the
         artifacts in the session. GSI1PK is NOT user-scoped, so each
         HEAD row is re-checked against the authenticated user's id.
      2. Per artifact, query the main table by PK=USER#{uid} and
         SK begins_with ARTIFACT#{aid}#V# for all immutable version
         rows. PK is the authenticated user's id, so step 2 is
         ownership-safe by construction.

    The SPA renders one card per version, anchored to the turn that
    produced it via the per-version produced_by_message_index the writer
    stamps. Version rows written before per-version linkage shipped lack
    that attribute (and updated_at) and degrade to the SPA's
    end-of-conversation strip rather than a per-turn anchor.
    """

    def list_for_session(
        self, *, user_id: str, session_id: str
    ) -> list[dict]:
        table = _table()
        head_items: list[dict] = []
        kwargs: dict = {
            "IndexName": _SESSION_INDEX,
            "KeyConditionExpression": Key("GSI1PK").eq(
                f"SESSION#{session_id}"
            ),
            "ScanIndexForward": False,  # GSI1SK embeds updated_at → newest first
        }
        try:
            while True:
                resp = table.query(**kwargs)
                head_items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except ClientError as exc:
            raise ArtifactQueryError(
                "artifact list query failed"
            ) from exc

        # Distinct artifact ids in the session, newest-first, owned by
        # the caller. dict.fromkeys dedupes while preserving GSI order.
        artifact_ids = list(
            dict.fromkeys(
                item.get("artifact_id", "")
                for item in head_items
                if item.get("user_id") == user_id
                and item.get("artifact_id")
            )
        )

        summaries: list[dict] = []
        for artifact_id in artifact_ids:
            summaries.extend(
                self._versions_for_artifact(user_id, artifact_id)
            )
        return summaries

    def heads_for_session(
        self, *, user_id: str, session_id: str
    ) -> list[dict]:
        """Each artifact the session produced, at HEAD, newest-first.

        One row per *artifact*, unlike `list_for_session`, which returns
        one per version so the session view can anchor a card under the
        turn that made it. This is the shape a point-in-time snapshot
        wants: the version each artifact stood at when the conversation
        was shared.

        Only HEAD rows carry `GSI1PK`, so the index query alone is the
        answer — no per-artifact expansion, and no base-table read.

        `user_id` filters rather than keys, because `SessionIndex` is
        partitioned by session and is NOT user-scoped. Dropping that
        filter would let a borrowed session id enumerate somebody else's
        artifacts, which is the same reason `list_for_session` re-checks
        it per row.
        """
        table = _table()
        items: list[dict] = []
        kwargs: dict = {
            "IndexName": _SESSION_INDEX,
            "KeyConditionExpression": Key("GSI1PK").eq(
                f"SESSION#{session_id}"
            ),
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
            raise ArtifactQueryError("artifact head query failed") from exc

        heads: list[dict] = []
        seen: set = set()
        for item in items:
            artifact_id = str(item.get("artifact_id", ""))
            if not artifact_id or item.get("user_id") != user_id:
                continue
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            produced_by = item.get("produced_by_message_index")
            heads.append(
                {
                    "artifact_id": artifact_id,
                    "version": int(item.get("version", 1)),
                    "title": str(item.get("title", "")),
                    "content_type": str(
                        item.get("content_type", "text/html; charset=utf-8")
                    ),
                    "produced_by_message_index": (
                        int(produced_by) if produced_by is not None else None
                    ),
                }
            )
        return heads

    def list_for_user(self, *, user_id: str) -> list[dict]:
        """Every artifact the user owns, at HEAD, newest-first.

        Served from `UserArtifactsIndex` (GSI2PK=USER#{uid},
        GSI2SK=ARTIFACT#{updated_at}#{aid}) with
        `ScanIndexForward=False`.

        This was a base-table Query on the same partition until the
        index existed. That worked, but read badly: HEAD and version
        rows share the partition, so it spanned roughly 3x the rows it
        returned and then date-sorted them in memory. Only HEAD rows
        carry the GSI2 keys, so the index holds one row per artifact
        already in newest-first order — the amplification and the sort
        both go away, and the ordering comes from the store instead of
        being recomputed per request.

        ############################################################
        # This index is SPARSE. A HEAD row without GSI2PK is not stale
        # in it, it is ABSENT from it — and silently, surfacing as a
        # library that lists fewer artifacts than the user made.
        #
        # Two things keep it complete, and both must stay true:
        #   * the writer stamps GSI2PK/GSI2SK on BOTH of its write
        #     paths, and
        #   * rows predating that (2026-09-04) were stamped by
        #     `scripts/backfill_artifact_user_index_keys.py`.
        #
        # If an environment is ever found listing fewer artifacts than
        # its table holds, re-run that script before looking anywhere
        # else. It is idempotent.
        ############################################################

        Still returns the whole library in one response, paging the
        index internally. Exposing pagination is a bigger change than it
        looks: search and the type filter are applied in the SPA today,
        and a filter that sees only the loaded page is worse than no
        filter because it looks authoritative — both would have to move
        server-side in the same change. The index makes that possible
        whenever it is wanted; it is not wanted yet.
        """
        table = _table()
        rows: list[dict] = []
        kwargs: dict = {
            "IndexName": _USER_INDEX,
            "KeyConditionExpression": Key("GSI2PK").eq(f"USER#{user_id}"),
            # GSI2SK leads with updated_at, so descending IS newest-first.
            "ScanIndexForward": False,
        }
        try:
            while True:
                resp = table.query(**kwargs)
                for item in resp.get("Items", []):
                    if not item.get("artifact_id"):
                        continue
                    rows.append(
                        {
                            "artifact_id": item.get("artifact_id", ""),
                            "version": int(item.get("version", 0)),
                            "title": item.get("title", ""),
                            "content_type": item.get(
                                "content_type", "text/html; charset=utf-8"
                            ),
                            # Rows written before these attributes existed
                            # degrade to an empty string rather than
                            # dropping out of the library.
                            "created_at": item.get("created_at") or "",
                            "updated_at": item.get("updated_at") or "",
                            "session_id": item.get("session_id") or "",
                        }
                    )
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except ClientError as exc:
            raise ArtifactQueryError("artifact library query failed") from exc

        # No sort here on purpose — the index supplied the order. Adding
        # one back would silently mask a broken sort key.
        return rows

    @staticmethod
    def _versions_for_artifact(
        user_id: str, artifact_id: str
    ) -> list[dict]:
        """All immutable version rows for one artifact, scoped to the
        user by PK. The #HEAD row shares the SK prefix but not the `#V#`
        infix, so begins_with cleanly excludes it."""
        table = _table()
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with(f"ARTIFACT#{artifact_id}#V#"),
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
            raise ArtifactQueryError(
                "artifact version query failed"
            ) from exc

        return [
            {
                "artifact_id": item.get("artifact_id", ""),
                "version": int(item.get("version", 0)),
                "title": item.get("title", ""),
                "content_type": item.get(
                    "content_type", "text/html; charset=utf-8"
                ),
                "updated_at": item.get("updated_at", ""),
                "created_at": item.get("created_at"),
                "produced_by_message_index": item.get(
                    "produced_by_message_index"
                ),
            }
            for item in items
        ]


def get_artifact_list_service() -> ArtifactListService:
    return ArtifactListService()


def _bucket_name() -> str:
    """The artifacts S3 bucket. Set by app-api-stack alongside the table
    name; an empty value means a broken artifacts deploy, not a disabled
    feature, so fail closed with a 500."""
    global _cached_bucket
    if _cached_bucket is not None:
        return _cached_bucket
    with _s3_lock:
        if _cached_bucket is not None:
            return _cached_bucket
        name = os.environ.get("S3_ARTIFACTS_BUCKET_NAME", "")
        if not name:
            raise RenderTokenConfigError(
                "S3_ARTIFACTS_BUCKET_NAME is not set"
            )
        _cached_bucket = name
        return name


def _s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    with _s3_lock:
        if _s3_client is None:
            _s3_client = boto3.client("s3", region_name=_region())
        return _s3_client


def _get_version_item(
    owner_id: str, artifact_id: str, version: int
) -> dict:
    """Fetch the exact version row from `owner_id`'s partition.

    `owner_id` is an ADDRESS — the partition this read targets — not an
    identity assertion, exactly like the render token's `sub` claim.
    Two callers pass two different things, and the difference is the
    whole access-control model:

      - Owner routes pass the *authenticated session user*. Building the
        PK from the session is what prevents reading someone else's
        artifact; there is no other check.
      - Share routes pass the share's *owner*, and may only do so AFTER
        `_check_share_access` has admitted the viewer. Reaching this
        function with an owner id the caller has not ACL-checked is a
        read-any-artifact-by-id bug.

    SK zero-pad matches the writer/verifier `V#{version:05d}` contract."""
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _table().get_item(
            Key={"PK": f"USER#{owner_id}", "SK": sk}
        )
    except ClientError as exc:
        raise ArtifactQueryError(
            "artifact metadata lookup failed"
        ) from exc
    item = result.get("Item")
    if not item:
        raise ArtifactNotFoundError("artifact version not found")
    return item


def _is_markdown(content_type: str) -> bool:
    bare = (content_type or "").split(";")[0].strip().lower()
    return bare in _MARKDOWN_MIME_TYPES


def _unwrap_markdown(html_body: str) -> Optional[str]:
    """Recover the authored Markdown from the writer's HTML wrapper.

    Markdown artifacts are stored as a self-contained HTML render
    scaffold with the original source base64-embedded in a fixed
    `<script id="md-src">` tag. Returns the decoded Markdown, or None if
    the tag is absent / undecodable (legacy object or a future template
    change) so the caller can fall back to the raw bytes."""
    match = _MD_SRC_RE.search(html_body)
    if not match:
        return None
    try:
        return base64.b64decode(match.group("b64")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


class ArtifactContentService:
    """Return one artifact version's raw source for the code view.

    For Markdown the stored S3 object is a rendered HTML wrapper; we
    unwrap it back to the authored Markdown so code view shows what the
    model actually wrote, and normalize `content_type` to
    `text/markdown` to match. Anything that can't be unwrapped falls
    back to the raw stored bytes + real type so the view still shows
    something truthful instead of erroring.

    ACCESS CONTROL: this service performs none of its own. It reads
    whichever partition `owner_id` names — see `_get_version_item`. The
    owner route passes the authenticated session user (self-scoping);
    the shared route passes the share's owner and is responsible for
    having run the share ACL first."""

    def get(
        self, *, owner_id: str, artifact_id: str, version: int
    ) -> tuple[str, str]:
        bucket = _bucket_name()
        item = _get_version_item(owner_id, artifact_id, version)
        content_key = item.get("content_key")
        stored_type = item.get(
            "content_type", "text/html; charset=utf-8"
        )
        if not content_key:
            raise ArtifactNotFoundError("artifact has no stored content")

        try:
            obj = _s3().get_object(Bucket=bucket, Key=content_key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise ArtifactNotFoundError(
                    "artifact content not found"
                ) from exc
            raise ArtifactQueryError(
                "artifact content fetch failed"
            ) from exc

        if obj.get("ContentLength", 0) > _MAX_CONTENT_BYTES:
            raise ArtifactTooLargeError("artifact too large for code view")

        raw = obj["Body"].read(_MAX_CONTENT_BYTES + 1)
        if len(raw) > _MAX_CONTENT_BYTES:
            raise ArtifactTooLargeError("artifact too large for code view")
        body = raw.decode("utf-8", errors="replace")

        if _is_markdown(stored_type):
            unwrapped = _unwrap_markdown(body)
            if unwrapped is not None:
                return unwrapped, "text/markdown"
        return body, stored_type


def get_artifact_content_service() -> ArtifactContentService:
    return ArtifactContentService()


# ---------------------------------------------------------------------
# Artifact sharing
#
# Two rows per share, on this same table, written in one transaction:
#
#   owner row   PK = USER#{owner_id}
#               SK = SHARE#{artifact_id}#V#{version:05d}#{share_id}
#   lookup row  PK = SHARE#{share_id}
#               SK = META
#
# The owner row makes "list my shares for this artifact" a begins_with
# query on the owner's existing partition; the lookup row makes the
# recipient path — which knows only a share id — a single GetItem. Two
# items in a transaction is deliberately chosen over a GSI: an index
# would mean an infra deploy that has to land before the code that
# queries it, one UpdateTable at a time.
#
# Both rows carry the identical attribute set. The duplication is
# bounded (access_level / allowed_emails are the only mutable fields)
# and every write below rewrites both rows together, so they cannot
# drift.
# ---------------------------------------------------------------------

_SHARE_LOOKUP_SK = "META"

# ---------------------------------------------------------------------
# Recipient fan-out rows ("shared with you")
#
#   recipient row  PK = SHARED_WITH#{email_lower}
#                  SK = SHARE#{created_at}#{share_id}
#
# One row per (share, recipient). This is the only shape that answers
# "what has been shared with me" without a table scan, and it is not a
# convenience choice over an index: `allowed_emails` is a LIST, and a
# DynamoDB GSI cannot project one item into N index entries, so *any*
# recipient-lookup design needs a row per recipient. Given that, the row
# belongs in the recipient's own partition, where the query is already
# partitioned by exactly the access dimension, ordered by time via the
# sort key, and natively paginable. Moving those same rows into the
# owner's partition and adding a GSI over them would cost an index for
# no gain. (The table's index budget is better spent on the
# `UserArtifactsIndex` the writer already stamps GSI2PK/GSI2SK for —
# that one buys server-side ordering and pagination for the library.)
#
# The row is a POINTER, deliberately carrying no title or content type.
# Share rows denormalize those, and until this module's `rename` cascade
# they went stale on rename; copying them across N recipients would
# multiply that. The inbox resolves display fields from the share lookup
# row at read time instead — one GetItem per row, always current.
#
# Fan-out is NOT part of the two-row share transaction. It is written
# per item, after the core rows commit, and torn down before them. A
# transaction would cap the allowlist at ~40 (TransactWriteItems allows
# 100 items, and a full allowlist swap is N deletes + N puts + 2), which
# is a product limit invented by a storage choice. Per-item writes have
# no such ceiling and isolate failures.
#
# The failure direction is what makes that safe: these rows are a
# DISCOVERY surface, never an authorization one. `_check_share_access`
# against the share row remains the only thing that grants access, so a
# fan-out row that failed to write costs a recipient a listing, not
# their access — and a fan-out row that outlives its share grants
# nothing, because the inbox resolves every row through the share
# lookup row and drops the ones that no longer exist.
# ---------------------------------------------------------------------

_RECIPIENT_PK_PREFIX = "SHARED_WITH#"
_RECIPIENT_SK_PREFIX = "SHARE#"


def _normalize_email(email: str) -> str:
    """Fold an address to its partition-key form.

    Share rows store addresses exactly as the owner typed them and
    lowercase only at compare time (`_check_share_access`), so folding
    here is load-bearing rather than tidy: a share addressed to
    `Ada.Lovelace@x.edu` read by a viewer whose token says
    `ada.lovelace@x.edu` would land on a different partition and return
    an empty inbox — a wrong answer indistinguishable from "nobody has
    shared anything with you".
    """
    return (email or "").strip().lower()


def _recipient_pk(email: str) -> str:
    return f"{_RECIPIENT_PK_PREFIX}{_normalize_email(email)}"


def _recipient_sk(created_at: str, share_id: str) -> str:
    """Recipient-row sort key: time first, so a Query returns newest-first
    with no sort at read time and pages without a filter."""
    return f"{_RECIPIENT_SK_PREFIX}{created_at}#{share_id}"


# Largest inbox page a caller may ask for. Each row costs a GetItem to
# resolve, so this caps the fan-out of one request, not just its payload.
_MAX_INBOX_PAGE = 100


def _encode_inbox_cursor(sort_key: Optional[str]) -> Optional[str]:
    """Opaque continuation token for the inbox — the sort key, and only
    the sort key. See the security note in `list_for_recipient`."""
    if not sort_key:
        return None
    return base64.urlsafe_b64encode(str(sort_key).encode()).decode()


def _decode_inbox_cursor(cursor: Optional[str]) -> Optional[str]:
    """Recover a sort key from a cursor, or None if it is unusable.

    A malformed cursor restarts the listing rather than erroring: it is
    an opaque token the caller was handed, so the only way it can be
    wrong is if it was tampered with or truncated, and neither deserves
    a 500. It also cannot be used to reach another partition — the
    caller of this function rebuilds the partition key from the session.
    """
    if not cursor:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if decoded.startswith(_RECIPIENT_SK_PREFIX) else None


class ArtifactShareError(Exception):
    """Base class for artifact-share failures."""


class ShareNotFoundError(ArtifactShareError):
    """No share row for the requested share id (never created, or revoked)."""


class ShareAccessDeniedError(ArtifactShareError):
    """The viewer is not permitted to open this share."""


class NotShareOwnerError(ArtifactShareError):
    """A non-owner attempted to mutate or revoke a share."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_share_sk(artifact_id: str, version: int, share_id: str) -> str:
    """Owner-row sort key. The `V#{version:05d}` zero-pad matches the
    artifact version rows so the two key spaces read consistently."""
    return f"SHARE#{artifact_id}#V#{version:05d}#{share_id}"


def _owner_share_prefix(artifact_id: str) -> str:
    return f"SHARE#{artifact_id}#V#"


def _share_lookup_key(share_id: str) -> dict:
    return {"PK": f"SHARE#{share_id}", "SK": _SHARE_LOOKUP_SK}


def _get_share_lookup(share_id: str) -> Optional[dict]:
    """Resolve a share id to its record without knowing the owner.

    Returns None when the share does not exist — a revoked share is a
    deleted row, which is what makes revocation effective within one
    token TTL."""
    try:
        result = _table().get_item(Key=_share_lookup_key(share_id))
    except ClientError as exc:
        raise ArtifactQueryError("share lookup failed") from exc
    return result.get("Item")


def _check_share_access(share: dict, viewer: User) -> None:
    """Decide whether `viewer` may open `share`.

    Direct port of ShareService._check_access (conversation sharing).
    `public` means "any authenticated tenant user", never anonymous —
    every route reaching here is already behind the session dependency.

    This is the security boundary for the whole feature: the share-scoped
    mint hands the viewer a credential addressed to the *owner's*
    DynamoDB partition, so this check is the only thing between
    "sharing" and "read any artifact by id". Fail closed — an unknown or
    missing access level is treated as `specific` with no allowlist.
    """
    if viewer.user_id and viewer.user_id == share.get("owner_id"):
        return

    access_level = share.get("access_level", "specific")
    if access_level == "public":
        return

    if access_level == "specific":
        allowed = [
            str(e).lower() for e in (share.get("allowed_emails") or [])
        ]
        viewer_email = (viewer.email or "").lower()
        if viewer_email and viewer_email in allowed:
            return

    raise ShareAccessDeniedError("access denied")


def _resolve_allowed_emails(
    access_level: str,
    allowed_emails: Optional[list[str]],
    owner_email: str,
) -> Optional[list[str]]:
    """Normalize the allowlist, keeping the owner on it.

    Port of ShareService._resolve_allowed_emails: `public` carries no
    list at all, and the owner is always implicitly allowed (they also
    pass the owner branch of _check_share_access, but keeping them on
    the list makes the row self-describing in the share UI)."""
    if access_level != "specific":
        return None
    emails = list(allowed_emails or [])
    if owner_email and owner_email.lower() not in [
        e.lower() for e in emails
    ]:
        emails.insert(0, owner_email)
    return emails


class ArtifactShareService:
    """Owner-side CRUD for artifact shares.

    A share pins one immutable `(artifact_id, version)` pair — never
    `#HEAD`. Version rows are append-only, so the recipient's view can
    never change under them and no snapshot copy is needed.
    """

    def create(
        self,
        *,
        owner: User,
        artifact_id: str,
        version: int,
        access_level: str,
        allowed_emails: Optional[list[str]],
    ) -> dict:
        """Create a share for one artifact version.

        The version row is fetched with a PK built from the *owner's*
        session id, so a caller can only ever share their own artifact —
        an unknown or someone else's version is an indistinguishable
        404. Title and content type are denormalized off that row so the
        recipient header needs no second read.
        """
        item = _get_version_item(owner.user_id, artifact_id, version)

        share_id = str(uuid.uuid4())
        now = _now_iso()
        attrs = {
            "share_id": share_id,
            "artifact_id": artifact_id,
            "version": version,
            "owner_id": owner.user_id,
            "owner_email": owner.email,
            "access_level": access_level,
            "title": item.get("title", ""),
            "content_type": item.get(
                "content_type", "text/html; charset=utf-8"
            ),
            "session_id": item.get("session_id", ""),
            "created_at": now,
            "updated_at": now,
        }
        resolved = _resolve_allowed_emails(
            access_level, allowed_emails, owner.email
        )
        if resolved is not None:
            attrs["allowed_emails"] = resolved

        self._write_share_rows(attrs)
        # After, never before: a fan-out row must never point at a share
        # that does not exist yet.
        self._sync_recipient_rows(attrs)
        logger.info(
            "created artifact share share=%s artifact=%s v=%s access=%s",
            scrub_log(share_id),
            scrub_log(artifact_id),
            scrub_log(version),
            scrub_log(access_level),
        )
        return attrs

    def list_for_artifact(
        self, *, owner_id: str, artifact_id: str
    ) -> list[dict]:
        """Every share the caller owns for one artifact.

        Partition-scoped by construction: PK is the authenticated user,
        so this can never surface another owner's shares."""
        table = _table()
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(f"USER#{owner_id}")
            & Key("SK").begins_with(_owner_share_prefix(artifact_id)),
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
            raise ArtifactQueryError("share list query failed") from exc
        return [self._strip_keys(item) for item in items]

    def get_for_viewer(self, *, share_id: str, viewer: User) -> dict:
        """Access-checked read of a share record. Never returns content."""
        share = _get_share_lookup(share_id)
        if not share:
            raise ShareNotFoundError("share not found")
        _check_share_access(share, viewer)
        return self._strip_keys(share)

    def update(
        self,
        *,
        share_id: str,
        owner: User,
        access_level: Optional[str],
        allowed_emails: Optional[list[str]],
    ) -> dict:
        """Change access level / allowlist on an existing share.

        Rewrites both rows so the owner row and the lookup row the
        recipient path reads can never disagree about who may view."""
        share = _get_share_lookup(share_id)
        if not share:
            raise ShareNotFoundError("share not found")
        if share.get("owner_id") != owner.user_id:
            raise NotShareOwnerError("not the share owner")

        updated = self._strip_keys(share)
        # Snapshot before mutation — the fan-out diff needs the allowlist
        # as it was, and `updated` is edited in place below.
        previous = dict(updated)
        new_access = access_level or updated.get("access_level", "specific")
        updated["access_level"] = new_access

        if new_access == "specific":
            emails = allowed_emails or updated.get("allowed_emails") or []
            updated["allowed_emails"] = _resolve_allowed_emails(
                new_access, emails, str(updated.get("owner_email", ""))
            )
        else:
            # Switching to public — drop the stale allowlist rather than
            # leaving a list that no longer gates anything.
            updated.pop("allowed_emails", None)

        updated["version"] = int(updated.get("version", 0))
        updated["updated_at"] = _now_iso()

        self._write_share_rows(updated)
        # Covers specific→public too: the allowlist is gone, so every
        # fan-out row is a removal and the share drops out of every
        # inbox while staying reachable by link.
        self._sync_recipient_rows(updated, previous=previous)
        logger.info(
            "updated artifact share share=%s access=%s",
            scrub_log(share_id),
            scrub_log(new_access),
        )
        return updated

    def revoke(self, *, share_id: str, owner: User) -> None:
        """Delete both rows. Effective within one render-token TTL."""
        share = _get_share_lookup(share_id)
        if not share:
            raise ShareNotFoundError("share not found")
        if share.get("owner_id") != owner.user_id:
            raise NotShareOwnerError("not the share owner")

        table = _table()
        # Discovery first, then reachability, then visibility — the same
        # ordering principle as `ArtifactLifecycleService.delete`.
        self._delete_recipient_rows(table, share)
        owner_sk = _owner_share_sk(
            str(share.get("artifact_id", "")),
            int(share.get("version", 0)),
            share_id,
        )
        try:
            table.meta.client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": table.name,
                            "Key": {
                                "PK": f"USER#{share.get('owner_id', '')}",
                                "SK": owner_sk,
                            },
                        }
                    },
                    {
                        "Delete": {
                            "TableName": table.name,
                            "Key": _share_lookup_key(share_id),
                        }
                    },
                ]
            )
        except ClientError as exc:
            raise ArtifactQueryError("share revoke failed") from exc
        logger.info("revoked artifact share share=%s", scrub_log(share_id))

    @staticmethod
    def _recipient_emails(share: Optional[dict]) -> set:
        """Addresses that should hold a fan-out row for this share.

        Empty for a `public` share: "any authenticated tenant user" has
        no recipient list to fan out to, so public shares stay
        link-delivered and never appear in anyone's inbox. That is a
        product decision, not a limitation — an inbox listing every
        public share in the tenant is a different feature.

        The owner is filtered out even though `_resolve_allowed_emails`
        deliberately keeps them on the allowlist: without this, sharing
        your own artifact files it under "shared with you".
        """
        if not share or share.get("access_level") != "specific":
            return set()
        owner = _normalize_email(str(share.get("owner_email", "")))
        return {
            email
            for email in (
                _normalize_email(str(raw))
                for raw in (share.get("allowed_emails") or [])
            )
            if email and email != owner
        }

    @staticmethod
    def _recipient_key(share: dict, email: str) -> dict:
        """Fan-out row key for one recipient of one share.

        Keyed on `created_at`, never `updated_at`: the sort key has to be
        stable for the life of the share, or editing the allowlist would
        strand a duplicate row under the old timestamp for every
        recipient who was already on it.
        """
        return {
            "PK": _recipient_pk(email),
            "SK": _recipient_sk(
                str(share.get("created_at", "")), str(share["share_id"])
            ),
        }

    def _sync_recipient_rows(
        self, share: dict, *, previous: Optional[dict] = None
    ) -> None:
        """Reconcile fan-out rows to the share's current allowlist.

        A diff, not a rewrite: only added and removed addresses are
        touched, so re-saving a share with an unchanged allowlist costs
        nothing and an edit costs one write per changed address.

        Best-effort by design. These rows are discovery, not access (see
        the block comment above `_RECIPIENT_PK_PREFIX`), so a failure
        here must not fail the share write that already committed — the
        link works, the recipient simply has to follow it rather than
        find it. Raising would be worse: the caller has no way to undo
        the committed share, so it would report failure for a share that
        exists.
        """
        desired = self._recipient_emails(share)
        existing = self._recipient_emails(previous)
        if desired == existing:
            return

        table = _table()
        # Removals first: an address taken off the allowlist has already
        # lost access at `_check_share_access`, so clearing its listing
        # is the more urgent of the two.
        for email in existing - desired:
            self.delete_quietly(table, self._recipient_key(share, email))

        for email in desired - existing:
            row = {
                **self._recipient_key(share, email),
                "share_id": str(share["share_id"]),
                "artifact_id": str(share.get("artifact_id", "")),
                "version": int(share.get("version", 0)),
                "owner_id": str(share.get("owner_id", "")),
                "owner_email": str(share.get("owner_email", "")),
                "shared_at": str(share.get("created_at", "")),
            }
            try:
                table.put_item(Item=row)
            except ClientError:
                logger.warning(
                    "could not fan out artifact share %s to a recipient",
                    scrub_log(str(share["share_id"])),
                    exc_info=True,
                )

    def _delete_recipient_rows(self, table, share: dict) -> None:
        """Drop every fan-out row for one share.

        Called before the lookup row on every teardown path. A crash
        between the two leaves a live share that nobody can discover,
        which is inert; the reverse order would leave a dead share
        sitting in someone's inbox. (The inbox resolves each row through
        the lookup row and skips what has gone, so a stranded row is
        already harmless — this ordering keeps it from mattering at
        all.)
        """
        for email in self._recipient_emails(share):
            self.delete_quietly(table, self._recipient_key(share, email))

    @staticmethod
    def _write_share_rows(attrs: dict) -> None:
        """Put the owner row and the lookup row in one transaction.

        Both carry the same attributes; only the keys differ. A partial
        write would either strand an unreachable share (owner row with
        no lookup) or an unlistable one, so this is atomic. Plain Puts
        only — a ConditionCheck item would need `dynamodb:ConditionCheckItem`
        added to the task role, which plain writes do not.
        """
        table = _table()
        owner_sk = _owner_share_sk(
            str(attrs["artifact_id"]),
            int(attrs["version"]),
            str(attrs["share_id"]),
        )
        owner_row = {
            **attrs,
            "PK": f"USER#{attrs['owner_id']}",
            "SK": owner_sk,
        }
        lookup_row = {**attrs, **_share_lookup_key(str(attrs["share_id"]))}
        try:
            table.meta.client.transact_write_items(
                TransactItems=[
                    {"Put": {"TableName": table.name, "Item": owner_row}},
                    {"Put": {"TableName": table.name, "Item": lookup_row}},
                ]
            )
        except ClientError as exc:
            raise ArtifactQueryError("share write failed") from exc

    @staticmethod
    def _strip_keys(item: dict) -> dict:
        """Drop the DynamoDB key attributes and normalize `version`.

        `version` comes back off DynamoDB as a Decimal; the SK builder
        and the response models both want a real int."""
        stripped = {k: v for k, v in item.items() if k not in ("PK", "SK")}
        if "version" in stripped:
            stripped["version"] = int(stripped["version"])
        return stripped


    def delete_for_session(self, session_id: str, owner_id: str) -> int:
        """Revoke every artifact share produced by one chat session.

        Called as a background task when the session owner deletes a
        conversation, mirroring `ShareService.delete_shares_for_session`
        for conversation shares. Artifacts outlive the chat that made
        them, so without this a deleted conversation would leave live
        share links pointing at its artifacts.

        Best-effort and **never raises**: a delete that partially fails
        leaves an orphan row, and the caller has already returned 204.
        Returns the number of shares revoked (0 on any failure, and 0
        when the artifacts feature isn't configured for this deploy).

        Scoped by `owner_id` deliberately. `SessionIndex` is not
        user-partitioned — the same reason `ArtifactListService`
        re-checks every HEAD row — so filtering here is what stops a
        borrowed or colliding session id reaching another user's shares.

        Deliberately NOT in scope: deleting the artifact content itself.
        That is a retention decision about the artifacts feature as a
        whole, not about sharing.
        """
        try:
            table = _table()
        except RenderTokenConfigError:
            # Artifacts aren't enabled for this environment. The session
            # routes are always mounted, so this is a normal no-op, not
            # a failure.
            logger.debug("artifacts not configured — skipping share cascade")
            return 0

        try:
            shares = self._shares_for_session(table, session_id, owner_id)
            if not shares:
                return 0

            # Lookup rows first, owner rows second — and never the other
            # way round. The lookup row (PK=SHARE#{id}) is what the
            # recipient path resolves, so dropping it is what actually
            # kills the link. If the second pass fails we are left with
            # an unreachable owner row, which is inert; the reverse
            # order would leave a *live* share whose owner can no longer
            # see it to revoke.
            #
            # Plain DeleteItem per row, NOT `table.batch_writer()`.
            # `BatchWriteItem` is its own IAM action and is *not*
            # authorized by the underlying item actions the way
            # `TransactWriteItems` is — the task role is granted
            # GetItem/PutItem/UpdateItem/DeleteItem/Query and nothing
            # else, so a batch write fails closed with AccessDenied at
            # runtime. Per-item deletes also isolate failures: one bad
            # row can't strand the rest of the cascade.
            revoked = 0
            for share in shares:
                self._delete_recipient_rows(table, share)
            for share in shares:
                if self.delete_quietly(
                    table, _share_lookup_key(str(share["share_id"]))
                ):
                    revoked += 1
            for share in shares:
                self.delete_quietly(
                    table,
                    {
                        "PK": f"USER#{owner_id}",
                        "SK": _owner_share_sk(
                            str(share["artifact_id"]),
                            int(share["version"]),
                            str(share["share_id"]),
                        ),
                    },
                )

            logger.info(
                "revoked %s of %s artifact share(s) for deleted session %s",
                revoked,
                len(shares),
                scrub_log(session_id),
            )
            return revoked
        except Exception:
            logger.error(
                "failed to revoke artifact shares for session %s",
                scrub_log(session_id),
                exc_info=True,
            )
            return 0

    def revoke_for_artifact(self, *, owner_id: str, artifact_id: str) -> int:
        """Revoke every share of one artifact. Returns the count revoked.

        The cascade behind `ArtifactLifecycleService.delete`. Shares are
        per-version, so deleting an artifact has to sweep the whole
        `SHARE#{artifact_id}#V#` prefix — otherwise a link handed out for
        v2 outlives the artifact it points at.

        Ordering matches `delete_for_session` and must not be flipped:
        the lookup row (`PK=SHARE#{id}`) is what the recipient path
        resolves, so dropping it is what actually kills the link. If the
        second pass fails we are left with an unreachable owner row,
        which is inert; the reverse order would leave a *live* share
        whose owner can no longer see it to revoke.

        Unlike `delete_for_session`, enumeration failures raise. That
        call site is a fire-and-forget background task after a 204 has
        already gone out, so it can only swallow; this one runs inside
        the request that is about to start deleting rows, and failing
        before anything has changed is strictly better than proceeding
        blind. Individual row deletes stay best-effort for the same
        reason they are there: one bad row must not strand the rest.
        """
        table = _table()
        shares = self._shares_for_artifact(table, owner_id, artifact_id)
        if not shares:
            return 0

        revoked = 0
        for share in shares:
            self._delete_recipient_rows(table, share)
        for share in shares:
            if self.delete_quietly(
                table, _share_lookup_key(str(share["share_id"]))
            ):
                revoked += 1
        for share in shares:
            self.delete_quietly(
                table,
                {
                    "PK": f"USER#{owner_id}",
                    "SK": _owner_share_sk(
                        str(share["artifact_id"]),
                        int(share["version"]),
                        str(share["share_id"]),
                    ),
                },
            )

        logger.info(
            "revoked %s of %s artifact share(s) for deleted artifact %s",
            revoked,
            len(shares),
            scrub_log(artifact_id),
        )
        return revoked

    @staticmethod
    def _shares_for_artifact(
        table, owner_id: str, artifact_id: str
    ) -> list[dict]:
        """Every share row the owner holds for one artifact, across all
        versions. Partition-scoped to the owner, so it can never reach
        another user's shares."""
        shares: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(f"USER#{owner_id}")
            & Key("SK").begins_with(_owner_share_prefix(artifact_id)),
        }
        try:
            while True:
                resp = table.query(**kwargs)
                shares.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except ClientError as exc:
            raise ArtifactQueryError("share list query failed") from exc
        return shares

    def list_for_recipient(
        self,
        *,
        viewer: User,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str]]:
        """Artifacts other people have shared with this viewer, newest first.

        One Query on the viewer's own `SHARED_WITH#{email}` partition —
        no index, no scan, no filter — followed by one GetItem per row
        to resolve the share it points at.

        ############################################################
        # The fan-out row is a POINTER and is never trusted for
        # display or for access. Every row is resolved through the
        # share lookup row, and a row whose share has gone is dropped.
        # That is what makes best-effort fan-out safe: a stranded
        # pointer (a revoke that failed halfway, a crash between the
        # two teardown passes) lists nothing and grants nothing.
        #
        # `_check_share_access` is re-run per row even though the
        # pointer's existence implies the viewer was on the allowlist
        # when it was written. The allowlist can have changed since,
        # and this endpoint must agree with the recipient page about
        # who may see what — one predicate, both surfaces.
        ############################################################

        Per-item GetItem, never BatchGetItem: `dynamodb:BatchGetItem` is
        its own IAM action and is NOT authorized by the item actions the
        task role holds, so a batch read would fail closed at runtime
        while passing every moto-backed test. That is not hypothetical —
        it is exactly how `BatchWriteItem` shipped broken in the delete
        cascade. Per-item reads also isolate failures, so one bad row
        costs one listing rather than the page.

        Pagination is real, not decorative: this partition grows by one
        row per share received, for the life of the account, and unlike
        `list_for_user` it has no natural ceiling — it is bounded by how
        many people share with you, which is not a number this service
        controls.

        A page can come back shorter than `limit` (rows are dropped
        after the Query, by the resolve step above) while still having a
        next cursor. Callers must page until the cursor is None rather
        than until a short page, which is standard DynamoDB semantics
        and why the cursor, not the count, terminates the loop.
        """
        table = _table()
        viewer_email = _normalize_email(viewer.email)
        if not viewer_email:
            # A token with no email cannot be on any allowlist, so there
            # is nothing to look up. Empty, not an error.
            return [], None

        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(_recipient_pk(viewer_email))
            & Key("SK").begins_with(_RECIPIENT_SK_PREFIX),
            # Sort key leads with the share's creation time, so this is
            # newest-first with no sort at read time.
            "ScanIndexForward": False,
            "Limit": max(1, min(limit, _MAX_INBOX_PAGE)),
        }
        start_sk = _decode_inbox_cursor(cursor)
        if start_sk:
            ############################################################
            # The partition key is rebuilt from the authenticated
            # viewer and only the SORT key is taken from the cursor.
            # An opaque cursor is still attacker-supplied input, and a
            # cursor carrying its own PK would be a paging primitive
            # into another user's inbox. Do not "simplify" this by
            # round-tripping the whole LastEvaluatedKey.
            ############################################################
            kwargs["ExclusiveStartKey"] = {
                "PK": _recipient_pk(viewer_email),
                "SK": start_sk,
            }

        try:
            resp = table.query(**kwargs)
        except ClientError as exc:
            raise ArtifactQueryError("share inbox query failed") from exc

        items: list[dict] = []
        for pointer in resp.get("Items", []):
            share = _get_share_lookup(str(pointer.get("share_id", "")))
            if not share:
                continue  # revoked, or the artifact was deleted
            if share.get("owner_id") == viewer.user_id:
                continue  # your own artifact is not "shared with you"
            try:
                _check_share_access(share, viewer)
            except ShareAccessDeniedError:
                continue  # taken off the allowlist since
            items.append(
                {
                    "share_id": str(share.get("share_id", "")),
                    "title": str(share.get("title", "")),
                    "content_type": str(share.get("content_type", "")),
                    "version": int(share.get("version", 0)),
                    "owner_email": str(share.get("owner_email", "")),
                    "shared_at": str(
                        pointer.get("shared_at")
                        or share.get("created_at", "")
                    ),
                }
            )

        last = resp.get("LastEvaluatedKey") or {}
        return items, _encode_inbox_cursor(last.get("SK"))

    def retitle_for_artifact(
        self, *, owner_id: str, artifact_id: str, title: str
    ) -> int:
        """Push a renamed artifact's title onto its share rows.

        Share records denormalize `title` at creation time so the
        recipient header needs no second read. Nothing kept them current
        until this method: `ArtifactLifecycleService.rename` rewrote the
        HEAD and version rows, so the owner saw the new name on every
        surface they own while every recipient kept seeing the old one
        indefinitely. The owner cannot see the discrepancy and the
        recipient has nothing to compare against.

        Both rows per share, always together — the owner row and the
        lookup row must never disagree, which is the same invariant
        `_write_share_rows` holds atomically on the write path.

        Recipient fan-out rows are deliberately untouched: they carry no
        title (see `_RECIPIENT_PK_PREFIX`), precisely so that a rename
        stays bounded by the number of shares rather than by the number
        of shares times their recipients.

        Best-effort per row, like the delete cascades and for the same
        reason: one unwritable row must not strand the rest. Returns the
        number of shares retitled.

        ############################################################
        # Nothing this method does may raise. It runs AFTER the rename
        # has already committed to the HEAD and version rows, so an
        # exception escaping here would report failure for a rename the
        # caller can see succeeded everywhere they look — and would
        # invite a retry of an operation that is already done. The
        # blanket `except Exception` at the bottom is deliberate and
        # mirrors `delete_for_session`; do not narrow it to the
        # exceptions this code happens to raise today.
        ############################################################
        """
        try:
            return self._retitle_for_artifact(
                owner_id=owner_id, artifact_id=artifact_id, title=title
            )
        except RenderTokenConfigError:
            # Artifacts aren't configured here at all — a normal no-op.
            return 0
        except Exception:
            logger.error(
                "could not retitle shares for artifact %s",
                scrub_log(artifact_id),
                exc_info=True,
            )
            return 0

    def _retitle_for_artifact(
        self, *, owner_id: str, artifact_id: str, title: str
    ) -> int:
        """Cascade body. See `retitle_for_artifact` for the contract —
        in particular that this one is allowed to raise and its caller
        is not."""
        table = _table()
        shares = self._shares_for_artifact(table, owner_id, artifact_id)

        retitled = 0
        for share in shares:
            share_id = str(share.get("share_id", ""))
            keys = [
                {
                    "PK": f"USER#{owner_id}",
                    "SK": _owner_share_sk(
                        str(share.get("artifact_id", "")),
                        int(share.get("version", 0)),
                        share_id,
                    ),
                },
                _share_lookup_key(share_id),
            ]
            ok = True
            for key in keys:
                try:
                    table.update_item(
                        Key=key,
                        UpdateExpression="SET title = :t",
                        ExpressionAttributeValues={":t": title},
                        # Never resurrect a row a concurrent revoke removed.
                        ConditionExpression="attribute_exists(SK)",
                    )
                except ClientError:
                    ok = False
                    logger.warning(
                        "could not retitle artifact share %s",
                        scrub_log(share_id),
                        exc_info=True,
                    )
            if ok:
                retitled += 1

        if retitled:
            logger.info(
                "retitled %s artifact share(s) for artifact %s",
                retitled,
                scrub_log(artifact_id),
            )
        return retitled

    @staticmethod
    def delete_quietly(table, key: dict) -> bool:
        """Delete one row, reporting success rather than raising.

        A single failed row must not strand the rest of the cascade —
        every remaining share link would stay live."""
        try:
            table.delete_item(Key=key)
            return True
        except ClientError:
            logger.warning(
                "artifact share cascade could not delete %s",
                scrub_log(key.get("PK", "")),
                exc_info=True,
            )
            return False

    @staticmethod
    def _shares_for_session(
        table, session_id: str, owner_id: str
    ) -> list[dict]:
        """Every share row for the artifacts produced by one session.

        Two steps, because there is no index from session to share:
        `SessionIndex` projects only artifact HEAD rows, so it yields the
        artifact ids, and the shares are then read off the owner's own
        partition by SK prefix.
        """
        head_kwargs: dict = {
            "IndexName": _SESSION_INDEX,
            "KeyConditionExpression": Key("GSI1PK").eq(
                f"SESSION#{session_id}"
            ),
        }
        heads: list[dict] = []
        while True:
            resp = table.query(**head_kwargs)
            heads.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            head_kwargs["ExclusiveStartKey"] = last

        artifact_ids = list(
            dict.fromkeys(
                item.get("artifact_id", "")
                for item in heads
                if item.get("user_id") == owner_id and item.get("artifact_id")
            )
        )

        shares: list[dict] = []
        for artifact_id in artifact_ids:
            shares.extend(
                ArtifactShareService._shares_for_artifact(
                    table, owner_id, artifact_id
                )
            )
        return shares


def get_artifact_share_service() -> ArtifactShareService:
    return ArtifactShareService()


# ---------------------------------------------------------------------
# Artifact lifecycle — rename + delete
#
# The library page lists every artifact a user has ever produced, which
# made "get rid of this one" the first thing missing from it. Both
# operations live here rather than in the agent-side writer
# (`agents/builtin_tools/artifacts/service.py`) because they are
# user-initiated CRUD on an existing record, not part of the agent's
# write path — and because the inference-api container cannot serve a
# custom route at all (see the inference-api boundary note in CLAUDE.md).
#
# DELETE SEMANTICS — read this before changing anything below.
#
# The DynamoDB rows are hard-deleted; the S3 objects are soft-deleted by
# tagging them `lifecycle-class=deleted`, which the artifacts bucket's
# existing `expire-soft-deleted` lifecycle rule reaps after
# `config.artifacts.retentionDays`.
#
# That split is deliberate, and the DynamoDB half is the part worth
# defending. The rows are the only authority on reachability: the render
# Lambda resolves a token to content by GetItem-ing the *version* row and
# following its `content_key`, and every listing, content and share path
# keys off these rows too. Deleting them makes an artifact unreachable
# everywhere at once, with no new condition to write and — more to the
# point — no new condition for a future reader to *forget*. A soft flag
# on the row would have to be honoured by the render Lambda (a separate
# deployable, and a frozen cross-PR contract), by both list paths, by the
# content endpoint, by share creation and by the share-scoped mint; one
# missed filter is a deleted artifact that still renders, and it fails
# silently. There is no undo built on top of the flag either, since the
# S3 bytes are what an undo would need and they are on a retention clock
# regardless.
#
# So: from the user's side delete is immediate and permanent. The
# retention window is an operational recovery path, not a user-facing
# trash — restoring an artifact means restoring its rows (the table has
# point-in-time recovery enabled in production) inside the S3 retention
# window, not flipping a flag.
#
# IAM: this needs nothing new. The app-api task role is already granted
# `s3:PutObjectTagging` and `dynamodb:DeleteItem`
# (infrastructure/lib/constructs/app-api/app-api-iam-grants.ts), which is
# the other reason tagging beats `DeleteObject` here — no bucket-policy
# widening and no infra deploy has to land before this code ships. Note
# there is still no `dynamodb:BatchWriteItem`, so deletes below are
# per-item, exactly as `delete_for_session` documents.
# ---------------------------------------------------------------------

# The tag the artifacts bucket's `expire-soft-deleted` lifecycle rule
# filters on. Frozen contract with
# infrastructure/lib/constructs/artifacts/artifacts-data-construct.ts —
# a typo here is invisible (the object simply never expires).
_DELETED_TAG_KEY = "lifecycle-class"
_DELETED_TAG_VALUE = "deleted"

# Generous ceiling on a user-supplied title. Long enough that no honest
# title hits it, short enough that the attribute can't be used as free
# storage on a row every list endpoint reads.
MAX_ARTIFACT_TITLE_LENGTH = 200


class ArtifactLifecycleService:
    """Rename and delete whole artifacts, owner-scoped.

    Every method builds `PK=USER#{user_id}` from the authenticated
    session, so ownership is enforced by the key rather than checked
    after the fact: another user's artifact id resolves to no HEAD row
    and is an indistinguishable 404.
    """

    def __init__(self, shares: Optional["ArtifactShareService"] = None) -> None:
        # Injected so the delete cascade can be asserted in isolation and
        # so share-row key construction stays owned by the share service.
        self._shares = shares or ArtifactShareService()

    # -- rename --------------------------------------------------------

    def rename(self, *, user_id: str, artifact_id: str, title: str) -> dict:
        """Retitle an artifact. Returns the updated HEAD row.

        Writes `title` to the HEAD row *and* to every version row.
        Renaming HEAD alone would split the display name in two: the
        library reads HEAD, but the session list reads version rows, so
        the same artifact would show its new title on `/artifacts` and
        its old one on the conversation that produced it.

        Share rows denormalize the title too, so they are cascaded last
        (see `ArtifactShareService.retitle_for_artifact`). Before that
        cascade existed, a rename left every recipient looking at the
        title the artifact had on the day it was shared, with no way for
        either party to notice — the owner sees the new name everywhere
        they look. Best-effort: the rows that decide what the owner sees
        are already written by then, so a share that fails to retitle is
        exactly the old behaviour rather than a failed rename.

        ############################################################
        # This is a bare `SET title`. It must never touch `version`.
        # `update_artifact_record` re-points HEAD under an optimistic
        # lock (`ConditionExpression="version = :cur"`), so a rename that
        # wrote `version` would race a concurrent agent update and one of
        # them would lose. Same reasoning — and the same restraint — as
        # `set_produced_by_message_index` in the writer.
        ############################################################

        `updated_at` is deliberately left alone too. It is not just a
        display field: HEAD's `GSI1SK`/`GSI2SK` embed it, and only the
        writer keeps those in sync. Bumping the attribute without
        rewriting the keys would order the library (which sorts on the
        attribute) differently from the session index (which sorts on the
        key) for the same artifacts. "Updated" means the content changed;
        a rename records `renamed_at`, which nothing sorts on.
        """
        title = title.strip()
        if not title:
            raise ArtifactTitleError("title must not be empty")
        if len(title) > MAX_ARTIFACT_TITLE_LENGTH:
            raise ArtifactTitleError(
                f"title must be {MAX_ARTIFACT_TITLE_LENGTH} characters or fewer"
            )

        table = _table()
        head = self._require_head(table, user_id, artifact_id)
        now = _now_iso()

        # HEAD first: it is what both list surfaces read, so if the
        # per-version pass fails partway the user still sees the rename
        # take effect and can retry into a consistent state. The reverse
        # order would look like the rename silently did nothing.
        sort_keys = [f"ARTIFACT#{artifact_id}#HEAD"] + [
            f"ARTIFACT#{artifact_id}#V#{int(item['version']):05d}"
            for item in self._version_rows(table, user_id, artifact_id)
            if item.get("version") is not None
        ]
        try:
            for sk in sort_keys:
                table.update_item(
                    Key={"PK": f"USER#{user_id}", "SK": sk},
                    UpdateExpression="SET title = :t, renamed_at = :now",
                    ExpressionAttributeValues={":t": title, ":now": now},
                    # Never resurrect a row the delete path just removed.
                    ConditionExpression="attribute_exists(SK)",
                )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # The row went away under us — a concurrent delete.
                raise ArtifactNotFoundError(artifact_id) from exc
            raise ArtifactQueryError("artifact rename failed") from exc

        self._shares.retitle_for_artifact(
            owner_id=user_id, artifact_id=artifact_id, title=title
        )

        logger.info(
            "renamed artifact user=%s artifact=%s versions=%s",
            scrub_log(user_id),
            scrub_log(artifact_id),
            len(sort_keys) - 1,
        )
        return {**head, "title": title, "renamed_at": now}

    # -- delete --------------------------------------------------------

    def delete(self, *, user_id: str, artifact_id: str) -> int:
        """Delete an artifact and every version of it. Returns the
        number of version rows removed.

        All versions, never just the HEAD pointer. Versions are
        addressable independently — the panel's version picker mints a
        render token per version, and shares are per-version — so
        dropping only the pointer would leave every prior version live
        for anyone holding a link, and unlisted, so the owner could
        neither see nor clean them up. That is not a delete.

        Ordering is the load-bearing part, and it is the same principle
        as `delete_for_session`: kill reachability first, kill visibility
        last, so that every partial-failure state is fail-closed and a
        retry finishes the job.

          1. Revoke shares (lookup row before owner row, as ever).
          2. Tag the S3 objects `lifecycle-class=deleted` — this has to
             happen while the version rows still exist, because those
             rows hold the only `content_key` pointers to the objects.
          3. Delete the version rows. This is the moment the artifact
             stops rendering: the render Lambda's GetItem finds nothing.
          4. Delete the HEAD row last. It is what the library and the
             session index list, so an interrupted delete leaves an
             artifact that is already unreachable but still listed —
             visibly wrong and self-healing on retry, rather than
             invisibly still live.
        """
        table = _table()
        self._require_head(table, user_id, artifact_id)
        versions = self._version_rows(table, user_id, artifact_id)

        # 1 — shares. Enumeration failures raise (nothing has changed
        # yet, so there is a clean state to fail into); individual row
        # deletes are best-effort so one bad row can't strand the rest.
        self._shares.revoke_for_artifact(
            owner_id=user_id, artifact_id=artifact_id
        )

        # 2 — soft-delete the bytes.
        self._tag_objects_deleted(versions)

        # 3 — version rows.
        deleted = 0
        for item in versions:
            version = item.get("version")
            if version is None:
                continue
            if self._shares.delete_quietly(
                table,
                {
                    "PK": f"USER#{user_id}",
                    "SK": f"ARTIFACT#{artifact_id}#V#{int(version):05d}",
                },
            ):
                deleted += 1

        # 4 — HEAD.
        try:
            table.delete_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"ARTIFACT#{artifact_id}#HEAD",
                }
            )
        except ClientError as exc:
            raise ArtifactQueryError("artifact delete failed") from exc

        logger.info(
            "deleted artifact user=%s artifact=%s versions=%s/%s",
            scrub_log(user_id),
            scrub_log(artifact_id),
            deleted,
            len(versions),
        )
        return deleted

    # -- internals -----------------------------------------------------

    @staticmethod
    def _require_head(table, user_id: str, artifact_id: str) -> dict:
        try:
            result = table.get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"ARTIFACT#{artifact_id}#HEAD",
                }
            )
        except ClientError as exc:
            raise ArtifactQueryError("artifact lookup failed") from exc
        head = result.get("Item")
        if not head:
            raise ArtifactNotFoundError(artifact_id)
        return head

    @staticmethod
    def _version_rows(table, user_id: str, artifact_id: str) -> list[dict]:
        """Every immutable version row for one artifact. `#HEAD` shares
        the SK prefix but not the `#V#` infix, so it is excluded."""
        items: list[dict] = []
        kwargs: dict = {
            "KeyConditionExpression": Key("PK").eq(f"USER#{user_id}")
            & Key("SK").begins_with(f"ARTIFACT#{artifact_id}#V#"),
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
            raise ArtifactQueryError("artifact version query failed") from exc
        return items

    @staticmethod
    def _tag_objects_deleted(versions: list[dict]) -> None:
        """Tag each version's S3 object for lifecycle expiry.

        Best-effort per object, and never fatal. A failed tag leaves an
        orphaned object that the lifecycle rule will not reap — wasted
        bytes, logged loudly — whereas aborting the delete over it would
        leave the artifact *visible*, which is the failure the user
        actually cares about. The object is already unreachable the
        moment its row goes, tag or no tag.

        `put_object_tagging` replaces the whole tag set; artifact objects
        are written untagged, so there is nothing to preserve.
        """
        bucket = _bucket_name()
        client = _s3()
        for item in versions:
            key = item.get("content_key")
            if not isinstance(key, str) or not key:
                continue
            try:
                client.put_object_tagging(
                    Bucket=bucket,
                    Key=key,
                    Tagging={
                        "TagSet": [
                            {
                                "Key": _DELETED_TAG_KEY,
                                "Value": _DELETED_TAG_VALUE,
                            }
                        ]
                    },
                )
            except ClientError:
                logger.warning(
                    "artifact delete could not tag object for expiry "
                    "artifact=%s version=%s",
                    scrub_log(str(item.get("artifact_id", ""))),
                    item.get("version"),
                    exc_info=True,
                )


def get_artifact_lifecycle_service() -> ArtifactLifecycleService:
    return ArtifactLifecycleService()

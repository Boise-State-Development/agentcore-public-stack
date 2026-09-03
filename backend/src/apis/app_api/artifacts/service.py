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


def get_render_token_service() -> RenderTokenService:
    return RenderTokenService()


# Frozen contract — the HEAD row + SessionIndex keys the artifact writer
# (backend/src/agents/builtin_tools/artifacts/service.py) emits.
_SESSION_INDEX = "SessionIndex"


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
    user_id: str, artifact_id: str, version: int
) -> dict:
    """Fetch the exact version row, scoped to the authenticated user.

    Building the PK from the session user's id is what prevents reading
    another user's artifact. SK zero-pad matches the writer/verifier
    `V#{version:05d}` contract."""
    sk = f"ARTIFACT#{artifact_id}#V#{version:05d}"
    try:
        result = _table().get_item(
            Key={"PK": f"USER#{user_id}", "SK": sk}
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
    """Return one artifact version's raw source for the panel code view.

    Ownership is enforced by the PK lookup. For Markdown the stored S3
    object is a rendered HTML wrapper; we unwrap it back to the authored
    Markdown so code view shows what the model actually wrote, and
    normalize `content_type` to `text/markdown` to match. Anything that
    can't be unwrapped falls back to the raw stored bytes + real type so
    the view still shows something truthful instead of erroring."""

    def get(
        self, *, user_id: str, artifact_id: str, version: int
    ) -> tuple[str, str]:
        bucket = _bucket_name()
        item = _get_version_item(user_id, artifact_id, version)
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


def get_artifact_share_service() -> ArtifactShareService:
    return ArtifactShareService()

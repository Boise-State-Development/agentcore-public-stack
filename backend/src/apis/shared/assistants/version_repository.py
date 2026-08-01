"""Write-once persistence for ``AgentVersion`` snapshots.

Versions are child rows under the Agent on the **assistants** table, so a version is
deleted with the Agent it snapshots and never outlives it:

    PK = AST#{agent_id}, SK = VERSION#{n:08d}

(The spec writes these as ``AGENT#…`` / ``PROFILE``; the table has always keyed Agents as
``AST#{id}`` / ``METADATA`` — see ``listing_repository._key`` — and co-location requires
the *same* partition key, so the real prefix wins. Only the literal strings differ; the
"version rows live beside the Agent row, sorted" design is unchanged.)

Three things here are load-bearing:

**1. ⚠️ One condition enforces both immutability and version numbering.** Every write
carries ``attribute_not_exists(PK)``, so a second write to a version number that already
exists fails rather than overwrites. That is the immutability guarantee — and because the
allocator picks a number from a *read* that another submission may already have used, the
same failed condition is also how the race is detected. There is no second mechanism and
no lock: the write is the serialization point, and the read before it is only a hint.

**2. There is deliberately no counter attribute on the Agent item.** The obvious
alternative — ``ADD versionCounter :one`` on the Agent row — allocates atomically without a
retry, but ``service._update_assistant_cloud`` rewrites every attribute not in its
``immutable_fields`` set, so a routine author edit would reset the counter and hand out a
number that is already taken. The listing keys are in that set for exactly this reason;
adding a third such trap to remember is worse than a bounded retry loop.

**3. Snapshots persist their nulls.** Unlike ``write_listing``, the dump here does *not*
exclude ``None`` — ``versions.apply_version`` overlays only the fields a version actually
set, so dropping nulls on write would turn "this version had no tagline" into "this
version does not speak to the tagline", and the overlay would leave a tagline the author
added after approval. Absent and null are different claims here, and the storage layer has
to keep them apart.

Nothing reads any of this yet. The module lands ahead of its callers so the write path
(PR-2) and the invocation swap (PR-3) are behavior changes against a tested data layer
rather than a data layer plus a behavior change at once.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentVersion
from .serialization import from_ddb, to_ddb_safe
from .versions import VERSION_SK_PREFIX, version_sk

logger = logging.getLogger(__name__)

# How many times ``create_version`` will re-pick a number after losing a race. Concurrent
# submissions on one Agent are rare (an author double-tapping, at worst), so this is sized
# to survive a genuine collision, not a stampede — and exhausting it is a real error rather
# than something to paper over with an unbounded loop.
MAX_ALLOCATION_ATTEMPTS = 5

# The sparse store-index attributes. They live on the *published version* row rather than
# on the Agent row — see ``set_version_index``. Nothing else writes them.
_GSI5_ATTRS = ("GSI5_PK", "GSI5_SK")


class AgentVersionExistsError(ValueError):
    """A write to a version number that is already taken.

    Raised by ``put_version``, and caught internally by ``create_version`` as its signal to
    re-pick. Surfacing to a caller means the number was chosen explicitly and lost, which is
    a caller bug — versions are never rewritten.
    """

    def __init__(self, agent_id: str, number: int):
        self.agent_id = agent_id
        self.number = number
        super().__init__(
            f"Version {number} of agent {agent_id} already exists. Versions are immutable "
            "and are never rewritten."
        )


def _table():
    """Bind the assistants table, or raise if the environment is not configured."""
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")
    return boto3.resource("dynamodb").Table(table_name)


def _pk(agent_id: str) -> str:
    return f"AST#{agent_id}"


def _to_item(agent_id: str, version: AgentVersion) -> Dict[str, Any]:
    """The DynamoDB item for a numbered snapshot.

    ``exclude_none`` is deliberately off; see the module docstring.
    """
    if version.version is None:
        raise ValueError("A version must be numbered before it can be written.")
    item = to_ddb_safe(version.model_dump(by_alias=True))
    item["PK"] = _pk(agent_id)
    item["SK"] = version_sk(version.version)
    return item


def _from_item(item: Dict[str, Any]) -> AgentVersion:
    """Hydrate a snapshot from a raw DynamoDB item, dropping the composite keys.

    ``PK``/``SK`` are stripped rather than left to ``extra="allow"``: they are storage
    detail, and a version that carried them would round-trip them back into a re-read as
    model fields — the same shape bug that makes ``GSI5_PK`` need an immutable-fields guard
    on the Agent row.
    """
    data = {k: v for k, v in from_ddb(item).items() if k not in ("PK", "SK")}
    return AgentVersion(**data)


async def put_version(agent_id: str, version: AgentVersion) -> AgentVersion:
    """Write one numbered snapshot, refusing to overwrite an existing number.

    Raises ``AgentVersionExistsError`` if that version already exists, and ``ValueError``
    if the snapshot carries no number.
    """
    from botocore.exceptions import ClientError

    item = _to_item(agent_id, version)
    try:
        _table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise AgentVersionExistsError(agent_id, version.version) from e
        logger.error(f"Failed to write version {version.version} for {agent_id}: {e}")
        raise

    logger.info(f"📌 Version {version.version} cut for {agent_id}")
    return version


async def create_version(agent_id: str, snapshot: AgentVersion) -> AgentVersion:
    """Allocate the next version number for ``agent_id`` and write the snapshot.

    Returns the stored snapshot, numbered. The input must be **unnumbered**: a caller
    supplying a number is either re-putting an immutable record or second-guessing the
    allocator, and both are bugs worth failing loudly on.

    Concurrency: the number comes from a read of the highest existing version, so two
    submissions racing can pick the same one. The loser's conditional write fails and it
    re-picks — no lock, no counter, and no possibility of the two silently sharing a number.
    """
    if snapshot.version is not None:
        raise ValueError(
            "create_version allocates the version number; pass an unnumbered snapshot "
            "(use put_version to write at an explicit number)."
        )

    for attempt in range(MAX_ALLOCATION_ATTEMPTS):
        latest = await get_latest_version(agent_id)
        number = (latest.version or 0) + 1 if latest else 1
        candidate = snapshot.model_copy(update={"version": number})
        try:
            return await put_version(agent_id, candidate)
        except AgentVersionExistsError:
            logger.warning(
                f"Version {number} for {agent_id} was taken concurrently "
                f"(attempt {attempt + 1}/{MAX_ALLOCATION_ATTEMPTS}); re-picking"
            )

    raise RuntimeError(
        f"Could not allocate a version number for agent {agent_id} after "
        f"{MAX_ALLOCATION_ATTEMPTS} attempts."
    )


async def set_version_index(
    agent_id: str, number: int, keys: Optional[Dict[str, str]]
) -> None:
    """Write — or clear — the sparse store keys on one version item.

    ⚠️ **The only mutable thing on an otherwise immutable row, and the exception is the
    point.** A version's *content* is write-once (``put_version``'s condition). Which
    version is *published* is a fact about now, not about the snapshot, so it has to be
    able to change — promotion, takedown and unpublication all move it. Keeping the pointer
    as an index key on the row rather than as content is what lets both be true at once.

    This is the physics that ``listing.py`` prizes, extended one level: the store index is
    written only on the published version, so the browse query cannot return draft content
    because draft content has no key in it. Previously the keys lived on the Agent row —
    the very row the author edits — which is why an approved listing could serve rewritten
    instructions.

    ``keys`` is ``gsi5_keys(...)`` output: a mapping to write, or ``None`` to REMOVE both.
    Raises ``ValueError`` if the version does not exist, so a promotion can never point at
    a row that is not there.
    """
    from botocore.exceptions import ClientError

    if keys:
        expression = "SET " + ", ".join(f"{attr} = :{attr.lower()}" for attr in keys)
        values = {f":{attr.lower()}": value for attr, value in keys.items()}
    else:
        expression = "REMOVE " + ", ".join(_GSI5_ATTRS)
        values = None

    params: Dict[str, Any] = {
        "Key": {"PK": _pk(agent_id), "SK": version_sk(number)},
        "UpdateExpression": expression,
        "ConditionExpression": "attribute_exists(PK)",
        "ReturnValues": "NONE",
    }
    if values:
        params["ExpressionAttributeValues"] = values

    try:
        _table().update_item(**params)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValueError(f"Version {number} of agent {agent_id} does not exist.") from e
        logger.error(f"Failed to set store index on version {number} of {agent_id}: {e}")
        raise

    logger.info(
        f"📇 Version {number} of {agent_id} → "
        f"{'indexed ' + keys['GSI5_PK'] if keys else 'not indexed'}"
    )


async def get_version(agent_id: str, number: int) -> Optional[AgentVersion]:
    """One snapshot by number, or ``None`` if it does not exist."""
    response = _table().get_item(Key={"PK": _pk(agent_id), "SK": version_sk(number)})
    item = response.get("Item")
    return _from_item(item) if item else None


async def get_latest_version(agent_id: str) -> Optional[AgentVersion]:
    """The highest-numbered snapshot for an Agent, or ``None`` if it has none.

    A one-item backwards query over the partition, which is correct only because the sort
    key is zero-padded — the last key lexically is the highest number.

    ⚠️ Latest is **not** published. The published version is whatever
    ``listing.publishedVersion`` points at, which lags the latest whenever a submission is
    pending review. Readers that want "what users run" must go through the listing.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("PK").eq(_pk(agent_id))
        & Key("SK").begins_with(VERSION_SK_PREFIX),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return _from_item(items[0]) if items else None


async def delete_versions_for_agent(agent_id: str) -> int:
    """Delete every snapshot for an Agent. Called when the Agent itself is deleted.

    Versions are child rows under the Agent's partition for the reason the reports module
    states: child rows must never outlive what they concern. Without this they do — a
    deleted Agent's snapshots sit in the table forever, invisible (a deletable Agent's
    listing is ``private``, so its versions carry no store key) but permanent.

    Immutability is about *rewriting*, not about retention: a version may never be changed,
    and it is meaningless once the Agent it snapshots is gone. Nothing audits it either —
    §8 flags "versions referenced by an audit record should survive" as a question for a
    retention policy that does not exist yet, and there is no such reference today.
    """
    from boto3.dynamodb.conditions import Key

    table = _table()
    condition = Key("PK").eq(_pk(agent_id)) & Key("SK").begins_with(VERSION_SK_PREFIX)

    response = table.query(KeyConditionExpression=condition, ProjectionExpression="PK, SK")
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.query(
            KeyConditionExpression=condition,
            ProjectionExpression="PK, SK",
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))

    if not items:
        return 0

    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    logger.info(f"🗑️ Deleted {len(items)} version(s) with agent {agent_id}")
    return len(items)


async def batch_get_versions(refs: List[Tuple[str, int]]) -> Dict[str, AgentVersion]:
    """Fetch specific versions by ``(agent_id, number)``, keyed by agent id.

    The read behind the curated store front, which is an arbitrary set of ids rather than a
    category partition — so the GSI5 query does not fit and this is a ``BatchGetItem`` over
    exact keys, mirroring ``listing_repository.batch_get_agents``.

    **A missing version is simply absent from the result**, for the same reason a missing
    Agent is there: a featured entry whose version was never written should drop off the
    shelf, not error the whole page.
    """
    if not refs:
        return {}

    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    resource = boto3.resource("dynamodb")
    found: Dict[str, AgentVersion] = {}

    # De-duplicated because BatchGetItem rejects duplicate keys outright.
    unique = list(dict.fromkeys(refs))
    for start in range(0, len(unique), 100):  # BatchGetItem caps at 100 keys
        keys = [
            {"PK": _pk(agent_id), "SK": version_sk(number)}
            for agent_id, number in unique[start : start + 100]
        ]
        request = {table_name: {"Keys": keys}}
        while request:
            response = resource.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table_name, []):
                version = _from_item(item)
                if version.agent_id:
                    found[version.agent_id] = version
            request = response.get("UnprocessedKeys") or None

    return found


async def list_versions(agent_id: str, *, limit: Optional[int] = None) -> List[AgentVersion]:
    """Every snapshot for an Agent, newest first.

    Backs the review diff (PR-5) and any future rollback picker. Bounded by ``limit`` when
    the caller only wants the recent few; unbounded it pages the whole partition, which is
    a handful of rows per Agent at this scale.
    """
    from boto3.dynamodb.conditions import Key

    params: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(_pk(agent_id))
        & Key("SK").begins_with(VERSION_SK_PREFIX),
        "ScanIndexForward": False,
    }
    if limit is not None:
        params["Limit"] = limit

    table = _table()
    items: List[Dict[str, Any]] = []
    response = table.query(**params)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response and (limit is None or len(items) < limit):
        response = table.query(**params, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    if limit is not None:
        items = items[:limit]
    return [_from_item(item) for item in items]

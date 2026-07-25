"""Agent Marketplace Phase 4 — icon validation, storage and the author paths (D5).

Two things these are really testing, beyond the happy path:

* **Every rejection is one an author can act on.** Each declined image asserts the limit
  is named in the message, because the failure mode of an upload gate is not "it let
  something through", it is "it said no and the author cannot tell why".
* **The bytes never touch the record.** ``iconKey`` is a key; the object lives in S3. The
  400 KB ceiling is a DynamoDB item-limit lesson (MCP App icons), so a test asserts the
  stored attribute is a short key rather than anything resembling image data.
"""

import io

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from apis.app_api.agent_designer.services.icon_service import (
    AgentIconError,
    read_icon,
    remove_icon,
    upload_icon,
)
from apis.shared.assistants.icons import (
    ICON_MAX_BYTES,
    ICON_SIZE,
    AgentIconStore,
    IconError,
    build_icon_key,
    content_digest,
    icon_url,
    icon_version,
    normalize_icon,
)
from apis.shared.assistants.listing_repository import write_listing
from apis.shared.assistants.models import AgentListing
from apis.shared.auth.models import User

REGION = "us-east-1"
TABLE = "test-rag-assistants"
BUCKET = "test-rag-documents"


def _png(size=(512, 512), color=(30, 90, 180, 255), mode="RGBA") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color[: 3 if mode == "RGB" else 4]).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg(size=(512, 512)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _noise_png(size=(512, 512)) -> bytes:
    """An image that does not compress — the only realistic way to approach 400 KB."""
    import random

    rng = random.Random(7)
    image = Image.new("RGB", size)
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(size[0] * size[1])])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ── validation ───────────────────────────────────────────────────────────────────────
def test_a_square_png_normalizes_to_512():
    data, ext, content_type = normalize_icon(_png((640, 640)))

    assert (ext, content_type) == ("png", "image/png")
    assert Image.open(io.BytesIO(data)).size == (ICON_SIZE, ICON_SIZE)


def test_a_square_jpeg_stays_a_jpeg():
    data, ext, content_type = normalize_icon(_jpeg((1024, 1024)))

    assert (ext, content_type) == ("jpg", "image/jpeg")
    assert Image.open(io.BytesIO(data)).size == (ICON_SIZE, ICON_SIZE)


def test_an_exactly_512_image_is_still_re_encoded():
    """Re-encoding is what strips EXIF — an icon cropped from a phone photo would
    otherwise carry its GPS coordinates into the store."""
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "location: somewhere private"
    Image.new("RGB", (512, 512), (10, 10, 10)).save(buffer, format="JPEG", exif=exif)
    source = buffer.getvalue()
    assert b"location: somewhere private" in source

    data, _ext, _ct = normalize_icon(source)

    assert b"location: somewhere private" not in data


def test_normalization_is_deterministic():
    """Same image in, same digest out — the content-addressed key depends on it."""
    source = _png((600, 600))
    assert content_digest(normalize_icon(source)[0]) == content_digest(normalize_icon(source)[0])


@pytest.mark.parametrize(
    "content,expected",
    [
        (b"", "empty"),
        (b"not an image at all", "PNG or JPEG"),
        (_png((512, 384)), "square"),
        (_png((128, 128)), "at least 256"),
    ],
)
def test_declined_images_say_what_is_wrong(content, expected):
    with pytest.raises(IconError) as excinfo:
        normalize_icon(content)
    assert expected in str(excinfo.value)


def test_a_gif_is_declined_even_though_pillow_can_read_it():
    buffer = io.BytesIO()
    Image.new("P", (512, 512)).save(buffer, format="GIF")

    with pytest.raises(IconError, match="PNG or JPEG"):
        normalize_icon(buffer.getvalue())


def test_oversized_uploads_are_declined_before_decoding():
    with pytest.raises(IconError, match="400 KB or smaller"):
        normalize_icon(b"\x89PNG\r\n\x1a\n" + b"\x00" * ICON_MAX_BYTES)


def test_an_almost_square_image_is_accepted():
    """A hand-cropped square is often a pixel or two out; a 4:3 photo is not."""
    data, _ext, _ct = normalize_icon(_png((513, 512)))
    assert Image.open(io.BytesIO(data)).size == (ICON_SIZE, ICON_SIZE)


def test_the_encode_ladder_degrades_an_opaque_png_to_jpeg(monkeypatch):
    """The rung that only an already-512, near-the-ceiling image reaches.

    Driven through ``_encode_within_limit`` with a lowered ceiling because the natural
    input — a 512² of pure noise — is ~770 KB and never gets past the upload gate. The
    ladder is still what runs; only the number it is measured against moves.
    """
    import apis.shared.assistants.icons as icons_module

    monkeypatch.setattr(icons_module, "ICON_MAX_BYTES", 300_000)
    noise = Image.open(io.BytesIO(_noise_png())).convert("RGBA")

    data, ext = icons_module._encode_within_limit(noise, "png")

    assert ext == "jpg" and len(data) <= 300_000


def test_the_encode_ladder_keeps_alpha_by_quantizing(monkeypatch):
    """A transparent PNG cannot become a JPEG without losing its alpha, so it loses
    colors instead."""
    import apis.shared.assistants.icons as icons_module

    monkeypatch.setattr(icons_module, "ICON_MAX_BYTES", 300_000)
    noise = Image.open(io.BytesIO(_noise_png())).convert("RGBA")
    noise.putalpha(Image.new("L", noise.size, 128))

    result = icons_module._encode_within_limit(noise, "png")

    assert result is not None
    data, ext = result
    assert ext == "png" and len(data) <= 300_000
    assert Image.open(io.BytesIO(data)).convert("RGBA").getchannel("A").getextrema()[0] < 255


# ── keys and URLs ────────────────────────────────────────────────────────────────────
def test_the_key_carries_the_digest_and_the_url_carries_it_as_a_cache_version():
    key = build_icon_key("ast-1", "0123456789abcdef", "png")

    assert key == "assistants/ast-1/icons/0123456789abcdef.png"
    assert icon_version(key) == "0123456789abcdef"
    assert icon_url("ast-1", key) == "/agents/ast-1/icon?v=0123456789abcdef"


def test_no_key_means_no_url():
    """Absent is the signal for the generated gradient — never an empty string."""
    assert icon_url("ast-1", None) is None
    assert icon_version(None) is None


# ── the author paths, end to end ─────────────────────────────────────────────────────
@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI5_PK", "AttributeType": "S"},
                {"AttributeName": "GSI5_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "AgentDirectoryIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI5_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI5_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        # The store is a module-level singleton bound on first use; rebind it per test so
        # it picks up this moto session rather than a previous one's client.
        import apis.shared.assistants.icons as icons_module

        monkeypatch.setattr(icons_module, "_store", AgentIconStore(bucket_name=BUCKET, s3_client=s3))
        yield {"table": ddb.Table(TABLE), "s3": s3}


def _seed(table, agent_id="ast-1", owner_id="user-author", **extra):
    item = {
        "PK": f"AST#{agent_id}",
        "SK": "METADATA",
        "assistantId": agent_id,
        "ownerId": owner_id,
        "ownerName": "Ada Author",
        "name": "An Agent",
        "description": "Description",
        "instructions": "Instructions",
        "vectorIndexId": "assistants-index",
        "visibility": "PRIVATE",
        "usageCount": 0,
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-01T00:00:00Z",
        "status": "COMPLETE",
        "emoji": "📋",
    }
    item.update(extra)
    table.put_item(Item=item)
    return item


AUTHOR = User(user_id="user-author", name="Ada Author", email="ada@example.edu", roles=[])
STRANGER = User(user_id="user-other", name="Otto Other", email="otto@example.edu", roles=[])


@pytest.mark.asyncio
async def test_upload_stores_the_key_on_the_record_and_the_bytes_in_s3(aws):
    _seed(aws["table"])

    response = await upload_icon("ast-1", _png(), AUTHOR)

    item = aws["table"].get_item(Key={"PK": "AST#ast-1", "SK": "METADATA"})["Item"]
    assert item["iconKey"] == response.icon_key
    # The record carries a key, never an image. This is the 400 KB item-limit rule.
    assert len(item["iconKey"]) < 100
    assert item["iconKey"].startswith("assistants/ast-1/icons/")
    assert response.icon_url == f"/agents/ast-1/icon?v={icon_version(response.icon_key)}"

    stored = aws["s3"].get_object(Bucket=BUCKET, Key=response.icon_key)
    assert Image.open(io.BytesIO(stored["Body"].read())).size == (ICON_SIZE, ICON_SIZE)


@pytest.mark.asyncio
async def test_replacing_an_icon_deletes_the_old_object(aws):
    _seed(aws["table"])
    first = await upload_icon("ast-1", _png(color=(10, 10, 10, 255)), AUTHOR)

    second = await upload_icon("ast-1", _jpeg(), AUTHOR)

    assert second.icon_key != first.icon_key
    keys = {o["Key"] for o in aws["s3"].list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert keys == {second.icon_key}


@pytest.mark.asyncio
async def test_re_uploading_the_same_image_keeps_the_object(aws):
    """The key is the content digest, so this is idempotent — and the delete of the
    'previous' key must not remove the object the record still points at."""
    _seed(aws["table"])
    first = await upload_icon("ast-1", _png(), AUTHOR)

    second = await upload_icon("ast-1", _png(), AUTHOR)

    assert second.icon_key == first.icon_key
    assert aws["s3"].get_object(Bucket=BUCKET, Key=second.icon_key)["ContentLength"] > 0


@pytest.mark.asyncio
async def test_remove_clears_the_attribute_so_the_gradient_comes_back(aws):
    _seed(aws["table"])
    uploaded = await upload_icon("ast-1", _png(), AUTHOR)

    response = await remove_icon("ast-1", AUTHOR)

    assert response.icon_key is None and response.icon_url is None
    item = aws["table"].get_item(Key={"PK": "AST#ast-1", "SK": "METADATA"})["Item"]
    # REMOVE, not an empty string: absent is what the read shapes mean by "no icon".
    assert "iconKey" not in item
    assert aws["s3"].list_objects_v2(Bucket=BUCKET).get("Contents", []) == []
    assert uploaded.icon_key is not None


@pytest.mark.asyncio
async def test_a_stranger_cannot_set_an_icon(aws):
    _seed(aws["table"])

    with pytest.raises(AgentIconError) as excinfo:
        await upload_icon("ast-1", _png(), STRANGER)

    assert excinfo.value.status_code in (403, 404)


@pytest.mark.asyncio
async def test_an_invalid_image_is_a_400_and_writes_nothing(aws):
    _seed(aws["table"])

    with pytest.raises(AgentIconError) as excinfo:
        await upload_icon("ast-1", _png((512, 300)), AUTHOR)

    assert excinfo.value.status_code == 400
    item = aws["table"].get_item(Key={"PK": "AST#ast-1", "SK": "METADATA"})["Item"]
    assert "iconKey" not in item
    assert aws["s3"].list_objects_v2(Bucket=BUCKET).get("Contents", []) == []


# ── the read gate ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_published_agents_icon_is_readable_by_anyone(aws):
    """The shelf already shows this agent's name, tagline and emoji to every browsing
    user; gating the icon on the PRIVATE record would render a broken tile on the shelf."""
    _seed(aws["table"])
    await upload_icon("ast-1", _png(), AUTHOR)
    await write_listing(
        "ast-1",
        AgentListing(state="published", category="Administration", publisher_id="pub-1"),
        "2026-07-01T00:00:00Z",
    )

    data, content_type, version = await read_icon("ast-1", STRANGER)

    assert content_type == "image/png"
    assert Image.open(io.BytesIO(data)).size == (ICON_SIZE, ICON_SIZE)
    assert version and len(version) == 16


@pytest.mark.asyncio
async def test_an_unpublished_private_agents_icon_is_not(aws):
    _seed(aws["table"])
    await upload_icon("ast-1", _png(), AUTHOR)

    with pytest.raises(AgentIconError) as excinfo:
        await read_icon("ast-1", STRANGER)

    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_the_owner_reads_their_own_unpublished_icon(aws):
    _seed(aws["table"])
    await upload_icon("ast-1", _png(), AUTHOR)

    data, _content_type, _version = await read_icon("ast-1", AUTHOR)
    assert data


@pytest.mark.asyncio
async def test_a_key_that_outlived_its_object_is_a_404_not_a_500(aws):
    """So the SPA's <img> error path drops to the generated gradient (the designed
    default) rather than showing a broken tile."""
    _seed(aws["table"])
    uploaded = await upload_icon("ast-1", _png(), AUTHOR)
    aws["s3"].delete_object(Bucket=BUCKET, Key=uploaded.icon_key)

    with pytest.raises(AgentIconError) as excinfo:
        await read_icon("ast-1", AUTHOR)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_an_agent_with_no_icon_is_a_404(aws):
    _seed(aws["table"])

    with pytest.raises(AgentIconError) as excinfo:
        await read_icon("ast-1", AUTHOR)

    assert excinfo.value.status_code == 404

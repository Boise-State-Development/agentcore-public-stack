"""Managed-model icons — the built-in slug, the uploaded override, and precedence.

The interesting assertions are not the happy path:

* **A cleared slug has to survive ``exclude_none``.** ``ManagedModelUpdate`` drops
  ``None`` fields, which is what makes a PATCH a PATCH — so ``None`` cannot also
  mean "remove it". ``''`` is that signal, and the update path has to turn it into
  a DynamoDB REMOVE rather than storing an empty string.
* **The bytes never touch the record.** ``iconKey`` is a key; the object lives in
  S3. Same 400 KB DynamoDB item-limit lesson the Agent icons learned.
* **An unknown slug is refused at the write.** A slug we ship no asset for renders
  an invisible tile for every user, and nothing downstream would report it.
"""

import io

import boto3
import pytest
from moto import mock_aws
from PIL import Image

from apis.app_api.admin.services.model_icons import (
    ModelIconError,
    read_model_icon,
    remove_model_icon,
    upload_model_icon,
)
from apis.shared.models.managed_models import (
    create_managed_model,
    get_managed_model,
    update_managed_model,
)
from apis.shared.models.model_icons import (
    BUILTIN_MODEL_ICONS,
    ModelIconStore,
    build_model_icon_key,
    content_digest,
    model_icon_url,
    model_icon_version,
    normalize_icon_slug,
)
from apis.shared.models.models import ManagedModelCreate, ManagedModelUpdate

REGION = "us-west-2"
TABLE = "test-managed-models"
BUCKET = "test-rag-documents"


def _png(size=(512, 512), color=(30, 90, 200, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("DYNAMODB_MANAGED_MODELS_TABLE_NAME", TABLE)
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
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "ModelIdIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )

        # The managed-models repository binds its DynamoDB resource at import time,
        # and the icon store is a module-level singleton bound on first use. Rebind
        # both so they see this moto session rather than a previous test's.
        import apis.shared.models.managed_models as repo
        import apis.shared.models.model_icons as icons_module

        monkeypatch.setattr(repo, "dynamodb", ddb)
        monkeypatch.setattr(
            icons_module, "_store", ModelIconStore(bucket_name=BUCKET, s3_client=s3)
        )
        # The catalog scan is cached per process; a test writing two models in a row
        # would otherwise read the first one's snapshot.
        from apis.shared.caching import config_cache

        config_cache.invalidate(config_cache.MANAGED_MODELS)
        yield {"table": ddb.Table(TABLE), "s3": s3}


def _create(**extra):
    payload = {
        "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "modelName": "Claude Haiku 4.5",
        "provider": "bedrock",
        "providerName": "Anthropic",
        "inputModalities": ["TEXT"],
        "outputModalities": ["TEXT"],
        "maxInputTokens": 200000,
        "inputPricePerMillionTokens": 1.0,
        "outputPricePerMillionTokens": 5.0,
    }
    payload.update(extra)
    return create_managed_model(ManagedModelCreate.model_validate(payload))


# ── the built-in slug ────────────────────────────────────────────────────────


def test_a_known_slug_is_normalized_and_persisted():
    assert normalize_icon_slug(" Anthropic ") == "anthropic"


def test_an_unknown_slug_is_refused_with_the_list_of_real_ones():
    with pytest.raises(ValueError) as excinfo:
        normalize_icon_slug("acme-labs")

    message = str(excinfo.value)
    assert "acme-labs" in message
    for slug in BUILTIN_MODEL_ICONS:
        assert slug in message


def test_clearing_a_slug_survives_the_patch_models_exclude_none():
    """``None`` means "don't touch" on a PATCH, so ``''`` has to carry "remove it"
    all the way through ``model_dump(exclude_none=True)``."""
    cleared = ManagedModelUpdate.model_validate({"iconSlug": ""})

    assert cleared.model_dump(exclude_none=True, by_alias=True) == {"iconSlug": ""}


def test_an_absent_slug_stays_absent_from_the_patch():
    assert ManagedModelUpdate.model_validate({}).model_dump(exclude_none=True, by_alias=True) == {}


@pytest.mark.asyncio
async def test_a_cleared_slug_removes_the_attribute_rather_than_storing_empty(aws):
    model = await _create(iconSlug="anthropic")
    assert model.icon_slug == "anthropic"

    await update_managed_model(model.id, ManagedModelUpdate.model_validate({"iconSlug": ""}))

    item = aws["table"].get_item(
        Key={"PK": f"MODEL#{model.id}", "SK": f"MODEL#{model.id}"}
    )["Item"]
    assert "iconSlug" not in item


# ── keys and URLs ────────────────────────────────────────────────────────────


def test_the_key_is_content_addressed_under_its_own_models_prefix():
    digest = content_digest(_png())
    key = build_model_icon_key("m-1", digest, "png")

    assert key == f"models/m-1/icons/{digest}.png"


def test_the_url_carries_the_digest_so_a_replacement_busts_an_immutable_cache():
    key = build_model_icon_key("m-1", "0123456789abcdef", "png")

    assert model_icon_url("m-1", key) == "/models/m-1/icon?v=0123456789abcdef"
    assert model_icon_version(key) == "0123456789abcdef"


def test_no_key_means_no_url():
    assert model_icon_url("m-1", None) is None


# ── upload / read / remove ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_stores_bytes_in_s3_and_only_a_key_on_the_record(aws):
    model = await _create()

    icon_key, icon_url = await upload_model_icon(model.id, _png())

    assert icon_key.startswith(f"models/{model.id}/icons/")
    assert icon_url == f"/models/{model.id}/icon?v={model_icon_version(icon_key)}"

    item = aws["table"].get_item(
        Key={"PK": f"MODEL#{model.id}", "SK": f"MODEL#{model.id}"}
    )["Item"]
    # A short key, not anything resembling image data — the 400 KB DynamoDB item
    # limit is the whole reason the bytes live in S3.
    assert item["iconKey"] == icon_key
    assert len(item["iconKey"]) < 100

    stored = aws["s3"].get_object(Bucket=BUCKET, Key=icon_key)["Body"].read()
    assert Image.open(io.BytesIO(stored)).size == (512, 512)


@pytest.mark.asyncio
async def test_the_read_model_derives_icon_url_from_the_stored_key(aws):
    model = await _create()
    icon_key, _ = await upload_model_icon(model.id, _png())

    reread = await get_managed_model(model.id)

    assert reread.icon_key == icon_key
    assert reread.model_dump(by_alias=True)["iconUrl"] == model_icon_url(model.id, icon_key)


@pytest.mark.asyncio
async def test_reuploading_the_same_image_is_idempotent(aws):
    model = await _create()
    first, _ = await upload_model_icon(model.id, _png())

    second, _ = await upload_model_icon(model.id, _png())

    assert first == second
    # And the object is still there: the "delete the previous one" step must not
    # fire when the content-addressed key is unchanged.
    aws["s3"].get_object(Bucket=BUCKET, Key=second)


@pytest.mark.asyncio
async def test_replacing_an_icon_deletes_the_object_it_replaced(aws):
    model = await _create()
    first, _ = await upload_model_icon(model.id, _png(color=(200, 30, 30, 255)))

    second, _ = await upload_model_icon(model.id, _png(color=(30, 200, 90, 255)))

    assert first != second
    with pytest.raises(aws["s3"].exceptions.NoSuchKey):
        aws["s3"].get_object(Bucket=BUCKET, Key=first)


@pytest.mark.asyncio
async def test_removing_an_icon_clears_the_key_and_leaves_the_slug_alone(aws):
    model = await _create(iconSlug="anthropic")
    await upload_model_icon(model.id, _png())

    icon_key, icon_url = await remove_model_icon(model.id)

    assert icon_key is None and icon_url is None
    reread = await get_managed_model(model.id)
    assert reread.icon_key is None
    # The fallback the removal returns the model to — clearing an upload is not
    # the same act as clearing the built-in logo.
    assert reread.icon_slug == "anthropic"


@pytest.mark.asyncio
async def test_reading_an_icon_returns_the_bytes_and_its_cache_version(aws):
    model = await _create()
    icon_key, _ = await upload_model_icon(model.id, _png())

    data, content_type, version = await read_model_icon(model.id)

    assert content_type == "image/png"
    assert version == model_icon_version(icon_key)
    assert Image.open(io.BytesIO(data)).size == (512, 512)


@pytest.mark.asyncio
async def test_reading_a_model_with_no_icon_is_a_404_not_a_500(aws):
    """So the SPA's <img> error path falls through to the slug rather than
    rendering a broken tile."""
    model = await _create()

    with pytest.raises(ModelIconError) as excinfo:
        await read_model_icon(model.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_a_key_that_outlived_its_object_is_also_a_404(aws):
    model = await _create()
    icon_key, _ = await upload_model_icon(model.id, _png())
    aws["s3"].delete_object(Bucket=BUCKET, Key=icon_key)

    with pytest.raises(ModelIconError) as excinfo:
        await read_model_icon(model.id)

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_uploading_to_a_missing_model_is_a_404(aws):
    with pytest.raises(ModelIconError) as excinfo:
        await upload_model_icon("no-such-model", _png())

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_a_rejected_image_names_the_limit_it_broke(aws):
    """An upload gate's failure mode is not "it let something through", it is
    "it said no and the admin cannot tell why"."""
    model = await _create()

    with pytest.raises(ModelIconError) as excinfo:
        await upload_model_icon(model.id, _png(size=(512, 256)))

    assert excinfo.value.status_code == 400
    assert "square" in excinfo.value.message and "512×256" in excinfo.value.message

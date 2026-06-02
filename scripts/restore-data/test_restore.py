"""Tests for restore.py — focused on the four bugs found by the
first live-fire dry-run against the May 21 backup manifest.

These tests don't hit AWS. They use unittest.mock to stub the
small S3/SSM call surface and assert the restore tool reads the
right manifest fields and constructs the right paths.
"""

from __future__ import annotations

import gzip
import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the script directory to sys.path so we can import the module
# under test directly. The script is meant to be run as a CLI; it
# doesn't have a setup.py.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import restore  # noqa: E402


def _make_ctx(
    manifest: dict,
    *,
    backup_bucket: str = "test-backup-bucket",
    target_prefix: str = "test-prefix",
    region: str = "us-west-2",
    dry_run: bool = False,
) -> restore.RestoreContext:
    """Construct a RestoreContext suitable for unit tests."""
    session = MagicMock()
    return restore.RestoreContext(
        backup_bucket=backup_bucket,
        manifest_key="manifest.json",
        target_prefix=target_prefix,
        region=region,
        session=session,
        manifest=manifest,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------- #
# Bug 1 + 2: backup writes detail.export_manifest (a path to            #
# manifest-summary.json), not detail.export_manifest_key. Restore must  #
# read that field and indirect through manifestFilesS3Key →             #
# manifest-files.json (line-delimited dataFileS3Keys) to find the       #
# actual data files.                                                    #
# --------------------------------------------------------------------- #
def test_dynamodb_restore_reads_export_manifest_field():
    """When the backup component lacks export_manifest entirely,
    restore should skip with a precise reason."""
    ctx = _make_ctx({"root_prefix": "root", "components": {}})
    component = {
        "logical_name": "users",
        "status": "ok",
        "detail": {},  # no export_manifest field at all
    }
    result = restore.restore_dynamodb_table(ctx, "users", component)
    assert result["status"] == "skipped"
    assert "export_manifest" in result["reason"]
    # Crucially, NOT the obsolete "export_manifest_key" string.
    assert "export_manifest_key" not in result["reason"]


def test_dynamodb_restore_indirects_through_manifest_files():
    """Restore should read manifest-summary.json, follow
    manifestFilesS3Key, then read manifest-files.json (line-delimited)
    to find dataFileS3Key entries."""
    summary_payload = json.dumps({
        "version": "2020-06-30",
        "exportArn": "arn:aws:dynamodb:...:export/abc",
        "manifestFilesS3Key": "root/dynamodb/users/AWSDynamoDB/abc/manifest-files.json",
        "outputFormat": "DYNAMODB_JSON",
    })
    files_payload = (
        '{"itemCount": 1, "dataFileS3Key": "root/dynamodb/users/AWSDynamoDB/abc/data/file1.json.gz"}\n'
        '{"itemCount": 1, "dataFileS3Key": "root/dynamodb/users/AWSDynamoDB/abc/data/file2.json.gz"}\n'
    )
    # Each data file is gzipped line-delimited DynamoDB-JSON.
    data_payload_1 = json.dumps({"Item": {"PK": {"S": "USER#1"}, "name": {"S": "Alice"}}}).encode()
    data_payload_2 = json.dumps({"Item": {"PK": {"S": "USER#2"}, "name": {"S": "Bob"}}}).encode()
    gzipped_1 = gzip.compress(data_payload_1)
    gzipped_2 = gzip.compress(data_payload_2)

    s3_responses = {
        "root/dynamodb/users/AWSDynamoDB/abc/manifest-summary.json":
            {"Body": io.BytesIO(summary_payload.encode())},
        "root/dynamodb/users/AWSDynamoDB/abc/manifest-files.json":
            {"Body": io.BytesIO(files_payload.encode())},
        "root/dynamodb/users/AWSDynamoDB/abc/data/file1.json.gz":
            {"Body": io.BytesIO(gzipped_1)},
        "root/dynamodb/users/AWSDynamoDB/abc/data/file2.json.gz":
            {"Body": io.BytesIO(gzipped_2)},
    }

    s3 = MagicMock()
    s3.get_object.side_effect = lambda Bucket, Key: s3_responses[Key]

    table = MagicMock()
    batch_writer = MagicMock()
    batch_writer.__enter__ = MagicMock(return_value=batch_writer)
    batch_writer.__exit__ = MagicMock(return_value=False)
    table.batch_writer.return_value = batch_writer

    dynamodb = MagicMock()
    dynamodb.Table.return_value = table

    ctx = _make_ctx({"root_prefix": "root"})
    ctx.session.client.return_value = s3
    ctx.session.resource.return_value = dynamodb

    component = {
        "logical_name": "users",
        "status": "ok",
        "detail": {
            "table_name": "test-prefix-users",
            # Backup writes this AWS-returned field name.
            "export_manifest": "root/dynamodb/users/AWSDynamoDB/abc/manifest-summary.json",
        },
    }

    with patch.object(restore, "get_ssm_param", return_value="test-prefix-users"):
        result = restore.restore_dynamodb_table(ctx, "users", component)

    assert result["status"] == "ok", result
    assert result["items_written"] == 2
    # Should have called BatchWriter.put_item twice with deserialized dicts.
    put_calls = batch_writer.put_item.call_args_list
    assert len(put_calls) == 2
    written_pks = {c.kwargs["Item"]["PK"] for c in put_calls}
    assert written_pks == {"USER#1", "USER#2"}


# --------------------------------------------------------------------- #
# Bug 3: root_prefix concat used to drop the '/' separator. The         #
# manifest stores root_prefix as 'project/timestamp' (no trailing       #
# slash); the previous restore code did f"{root}{path}" and produced    #
# 'project/timestampcognito/users.jsonl.gz'. Verify that both the       #
# cognito and S3 sync code paths now construct the right keys.          #
# --------------------------------------------------------------------- #
def test_s3_restore_path_includes_separator():
    ctx = _make_ctx(
        {
            "root_prefix": "ai-sbmt-api/20260521T181146Z",  # no trailing slash
        },
        dry_run=True,
    )
    component = {
        "logical_name": "rag-documents",
        "status": "ok",
        "detail": {},  # no s3_prefix → falls back to f"s3/{logical}/"
    }

    with patch.object(restore, "get_ssm_param", return_value="ai-sbmt-api-rag-documents-12345"):
        result = restore.restore_s3_bucket(ctx, "rag-documents", component)

    assert result["status"] == "dry-run"
    # The bug produced "...20260521T181146Zs3/rag-documents/" with no slash.
    assert "20260521T181146Z/s3/rag-documents/" in result["source"], result["source"]
    assert "20260521T181146Zs3/rag-documents/" not in result["source"], result["source"]


def test_s3_restore_path_handles_root_prefix_with_trailing_slash():
    """If a future backup adds a trailing slash, restore shouldn't
    double it up."""
    ctx = _make_ctx(
        {"root_prefix": "ai-sbmt-api/20260521T181146Z/"},  # trailing slash
        dry_run=True,
    )
    component = {"logical_name": "rag-documents", "status": "ok", "detail": {}}

    with patch.object(restore, "get_ssm_param", return_value="dest"):
        result = restore.restore_s3_bucket(ctx, "rag-documents", component)

    assert "20260521T181146Z/s3/rag-documents/" in result["source"]
    assert "20260521T181146Z//s3/rag-documents/" not in result["source"]


# --------------------------------------------------------------------- #
# Bug 4: the `assistants` table was decommissioned in commit c977e04e   #
# (deletion of AssistantsTableConstruct). Restoring from an old backup  #
# should not attempt to write into a table that no longer exists.       #
# --------------------------------------------------------------------- #
def test_assistants_table_removed_from_convention_map():
    """The decommissioned `assistants` table was the only entry in
    TABLE_CONVENTION_MAP. The map should now be empty so an old
    backup's assistants component skips cleanly."""
    assert "assistants" not in restore.TABLE_CONVENTION_MAP
    assert restore.TABLE_CONVENTION_MAP == {}


def test_decommissioned_assistants_component_skips_with_clear_reason():
    ctx = _make_ctx({"root_prefix": "root"})
    component = {
        "logical_name": "assistants",
        "status": "ok",
        "detail": {
            "export_manifest": "root/dynamodb/assistants/AWSDynamoDB/x/manifest-summary.json",
        },
    }
    # No SSM lookup match either — assistants is not in TABLE_SSM_MAP
    with patch.object(restore, "get_ssm_param", return_value=None):
        result = restore.restore_dynamodb_table(ctx, "assistants", component)
    assert result["status"] == "skipped"
    assert "target table not found" in result["reason"]


# --------------------------------------------------------------------- #
# Bug 6: cognito identity-providers.json + app-clients.json are wrapped #
# objects ({"providers": [...]} and {"clients": [...]}), not bare       #
# arrays. Iterating the dict directly yielded keys (strings), then     #
# `idp.get(...)` raised AttributeError. Verify both wrappers are       #
# unwrapped correctly, plus the bare-array fallback for hand-edited    #
# backups.                                                              #
# --------------------------------------------------------------------- #
def test_cognito_identity_providers_unwraps_providers_key():
    """The backup writes {'providers': [...]} — restore must read that
    list, not iterate the dict's keys."""
    idp_payload = json.dumps({
        "providers": [
            {
                "ProviderName": "AzureAD",
                "ProviderType": "OIDC",
                "ProviderDetails": {"client_id": "abc"},
                "AttributeMapping": {"email": "email"},
                "IdpIdentifiers": [],
            },
        ],
    })
    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("identity-providers.json"):
            return {"Body": io.BytesIO(idp_payload.encode())}
        from botocore.exceptions import ClientError as _ClientError
        raise _ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    cognito = MagicMock()
    cognito.create_identity_provider = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    idp_result = next(r for r in results if r.get("component") == "cognito-idps")
    assert idp_result["status"] == "ok", idp_result
    assert idp_result["count"] == 1
    cognito.create_identity_provider.assert_called_once()
    call_kwargs = cognito.create_identity_provider.call_args.kwargs
    assert call_kwargs["ProviderName"] == "AzureAD"
    assert call_kwargs["ProviderType"] == "OIDC"


def test_cognito_app_clients_unwraps_clients_key():
    """Same wrapper as IdPs, but for app-clients.json."""
    clients_payload = json.dumps({
        "clients": [
            {"ClientName": "WebClient", "ClientId": "abc123"},
        ],
    })
    s3 = MagicMock()

    # Two get_object calls happen during restore_cognito: identity-providers
    # then app-clients. Make IdPs raise NoSuchKey (legitimate: no IdP backup
    # in this fixture) so we exercise the clients path.
    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("identity-providers.json"):
            from botocore.exceptions import ClientError as _ClientError
            raise _ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        if Key.endswith("app-clients.json"):
            return {"Body": io.BytesIO(clients_payload.encode())}
        # Anything else (users, groups, memberships) — also NoSuchKey
        from botocore.exceptions import ClientError as _ClientError
        raise _ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    cognito = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    clients_result = next(r for r in results if r.get("component") == "cognito-clients")
    assert clients_result["status"] == "ok"
    assert clients_result["count"] == 1


def test_cognito_identity_providers_falls_back_to_bare_array():
    """If a hand-edited or alternate backup writes a bare array,
    handle that too (defensive — backup always writes wrapped today)."""
    idp_payload = json.dumps([
        {"ProviderName": "BareArrayIdP", "ProviderType": "OIDC"},
    ])
    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("identity-providers.json"):
            return {"Body": io.BytesIO(idp_payload.encode())}
        from botocore.exceptions import ClientError as _ClientError
        raise _ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    cognito = MagicMock()
    cognito.create_identity_provider = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    idp_result = next(r for r in results if r.get("component") == "cognito-idps")
    assert idp_result["status"] == "ok"
    assert idp_result["count"] == 1


# --------------------------------------------------------------------- #
# Bug 7: Cognito's `sub` attribute is auto-generated and immutable.     #
# Backup captures user attributes verbatim from list-users; if the      #
# restore passes `sub` (or other Cognito-managed read-only attrs) back  #
# into AdminCreateUser, AWS rejects with                                #
#   "Cannot modify the non-mutable attribute sub".                      #
# --------------------------------------------------------------------- #
def test_cognito_users_strips_immutable_attributes():
    """A backed-up user with `sub`, `cognito:user_status`,
    `cognito:mfa_enabled`, and `identities` attributes is restored
    via AdminCreateUser with NONE of those four passed through."""
    user_record = {
        "Username": "colin",
        "Attributes": [
            {"Name": "sub", "Value": "fa84a268-3091-7032-1234-abcdef000000"},
            {"Name": "email", "Value": "colin@example.com"},
            {"Name": "email_verified", "Value": "true"},
            {"Name": "cognito:user_status", "Value": "CONFIRMED"},
            {"Name": "cognito:mfa_enabled", "Value": "false"},
            {"Name": "identities", "Value": "[]"},
            {"Name": "given_name", "Value": "Colin"},
        ],
    }
    users_jsonl = json.dumps(user_record) + "\n"
    users_gz = gzip.compress(users_jsonl.encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("users.jsonl.gz"):
            return {"Body": io.BytesIO(users_gz)}
        from botocore.exceptions import ClientError as _ClientError
        raise _ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    cognito = MagicMock()
    cognito.admin_create_user = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    users_result = next(r for r in results if r.get("component") == "cognito-users")
    assert users_result["status"] == "ok"
    assert users_result["count"] == 1

    cognito.admin_create_user.assert_called_once()
    call_kwargs = cognito.admin_create_user.call_args.kwargs
    submitted_names = {a["Name"] for a in call_kwargs["UserAttributes"]}

    forbidden = {"sub", "cognito:user_status", "cognito:mfa_enabled", "identities"}
    leaked = forbidden & submitted_names
    assert not leaked, f"Cognito-immutable attrs leaked into AdminCreateUser: {leaked}"

    # And the legit attrs ARE preserved.
    assert "email" in submitted_names
    assert "email_verified" in submitted_names
    assert "given_name" in submitted_names
    assert call_kwargs["Username"] == "colin"
    assert call_kwargs["MessageAction"] == "SUPPRESS"


# ===================================================================== #
# Cross-pool sub remapping — the load-bearing piece of the cross-pool   #
# migration. Cognito does not allow setting `sub` on user creation, so  #
# every user gets a NEW sub when the pool is recreated. The app keys    #
# all DynamoDB partitions and several S3 paths by <sub>. The restore    #
# tool builds an old_sub → new_sub map during cognito user creation     #
# and applies it in-flight while restoring DDB items and S3 objects.   #
# ===================================================================== #
def test_compile_sub_pattern_empty_returns_none():
    assert restore.compile_sub_pattern({}) is None


def test_compile_sub_pattern_compiles_alternation():
    sub_map = {
        "00000000-0000-0000-0000-000000000001": "11111111-1111-1111-1111-111111111111",
        "00000000-0000-0000-0000-000000000002": "22222222-2222-2222-2222-222222222222",
    }
    pattern = restore.compile_sub_pattern(sub_map)
    assert pattern is not None
    # Both old subs should match.
    assert pattern.search("USER#00000000-0000-0000-0000-000000000001") is not None
    assert pattern.search("USER#00000000-0000-0000-0000-000000000002") is not None
    # An unrelated UUID should not.
    assert pattern.search("USER#99999999-9999-9999-9999-999999999999") is None


def test_remap_subs_string_replacement():
    sub_map = {
        "old-sub-1": "new-sub-A",
        "old-sub-2": "new-sub-B",
    }
    pattern = restore.compile_sub_pattern(sub_map)
    assert restore.remap_subs("USER#old-sub-1", pattern, sub_map) == "USER#new-sub-A"
    assert restore.remap_subs("plain string", pattern, sub_map) == "plain string"
    # Multi-occurrence in one string.
    assert (
        restore.remap_subs("USER#old-sub-1/files/old-sub-1.pdf", pattern, sub_map)
        == "USER#new-sub-A/files/new-sub-A.pdf"
    )


def test_remap_subs_recursive_dict_and_list():
    sub_map = {"OLD": "NEW"}
    pattern = restore.compile_sub_pattern(sub_map)
    item = {
        "PK": "USER#OLD",
        "SK": "PROFILE",
        "owner_user_id": "OLD",
        "tags": ["OLD", "other", "USER#OLD"],
        "metadata": {
            "created_by": "USER#OLD",
            "history": [{"actor": "OLD"}, {"actor": "someone-else"}],
        },
        "count": 42,             # int — must be returned unchanged
        "active": True,          # bool — must be returned unchanged
        "binary": b"\x00\x01",   # bytes — must be returned unchanged
    }
    result = restore.remap_subs(item, pattern, sub_map)
    assert result["PK"] == "USER#NEW"
    assert result["owner_user_id"] == "NEW"
    assert result["tags"] == ["NEW", "other", "USER#NEW"]
    assert result["metadata"]["created_by"] == "USER#NEW"
    assert result["metadata"]["history"][0]["actor"] == "NEW"
    assert result["metadata"]["history"][1]["actor"] == "someone-else"
    assert result["count"] == 42
    assert result["active"] is True
    assert result["binary"] == b"\x00\x01"


def test_remap_subs_with_none_pattern_short_circuits():
    """When sub_map is empty (compile_sub_pattern returns None), the
    helper returns its input untouched. This is the common case for
    deployments with no Cognito users to migrate."""
    item = {"PK": "USER#abc-def", "data": "any string"}
    result = restore.remap_subs(item, None, {})
    assert result is item or result == item


def test_remap_subs_in_key_rewrites_s3_path():
    sub_map = {"old-uuid": "new-uuid"}
    pattern = restore.compile_sub_pattern(sub_map)
    assert (
        restore.remap_subs_in_key("users/old-uuid/file.pdf", pattern, sub_map)
        == "users/new-uuid/file.pdf"
    )
    # Key with no sub passes through.
    assert (
        restore.remap_subs_in_key("public/img.png", pattern, sub_map)
        == "public/img.png"
    )


# --------------------------------------------------------------------- #
# Cognito creates user, captures new sub, builds the map.              #
# --------------------------------------------------------------------- #
def test_cognito_user_creation_captures_new_sub_and_links_identity():
    """Backup contains a federated user with old sub and an `identities`
    blob. Restore creates the user, captures the AdminCreateUser-assigned
    new sub, and links the federated identity so future IdP logins
    resolve to the same user."""
    user_record = {
        "Username": "AzureAD_entra-user-id",
        "Attributes": [
            {"Name": "sub", "Value": "OLD-SUB-UUID"},
            {"Name": "email", "Value": "colin@example.com"},
            {"Name": "email_verified", "Value": "true"},
            {
                "Name": "identities",
                "Value": json.dumps([
                    {
                        "userId": "entra-user-id",
                        "providerName": "AzureAD",
                        "providerType": "OIDC",
                        "primary": True,
                        "dateCreated": 1700000000,
                    }
                ]),
            },
        ],
    }
    users_jsonl = json.dumps(user_record) + "\n"
    users_gz = gzip.compress(users_jsonl.encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("users.jsonl.gz"):
            return {"Body": io.BytesIO(users_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect
    s3.put_object = MagicMock()  # audit artifact write

    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "User": {
            "Username": "AzureAD_entra-user-id",
            "Attributes": [
                {"Name": "sub", "Value": "NEW-SUB-UUID"},
                {"Name": "email", "Value": "colin@example.com"},
            ],
        }
    }

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    # Sub mapping captured
    assert ctx.sub_map == {"OLD-SUB-UUID": "NEW-SUB-UUID"}
    # Pattern compiled and ready for downstream passes
    assert ctx.sub_map_pattern is not None
    assert ctx.sub_map_pattern.search("USER#OLD-SUB-UUID")

    # AdminCreateUser was called with sanitised attrs (no sub, no identities)
    create_kwargs = cognito.admin_create_user.call_args.kwargs
    submitted_names = {a["Name"] for a in create_kwargs["UserAttributes"]}
    assert "sub" not in submitted_names
    assert "identities" not in submitted_names
    assert "email" in submitted_names

    # AdminLinkProviderForUser was called for the AzureAD identity
    cognito.admin_link_provider_for_user.assert_called_once()
    link_kwargs = cognito.admin_link_provider_for_user.call_args.kwargs
    assert link_kwargs["DestinationUser"]["ProviderName"] == "Cognito"
    assert link_kwargs["DestinationUser"]["ProviderAttributeValue"] == "AzureAD_entra-user-id"
    assert link_kwargs["SourceUser"]["ProviderName"] == "AzureAD"
    assert link_kwargs["SourceUser"]["ProviderAttributeValue"] == "entra-user-id"

    # Result reports the remapping
    user_result = next(r for r in results if r.get("component") == "cognito-users")
    assert user_result["status"] == "ok"
    assert user_result["count"] == 1
    assert user_result["subs_remapped"] == 1
    assert user_result["identities_linked"] == 1

    # Audit artifact persisted to S3
    audit_calls = [c for c in s3.put_object.call_args_list
                   if "sub-mapping" in c.kwargs.get("Key", "")]
    assert len(audit_calls) == 1
    audit_body = json.loads(audit_calls[0].kwargs["Body"])
    assert audit_body["old_to_new"] == {"OLD-SUB-UUID": "NEW-SUB-UUID"}


def test_cognito_user_idempotent_rerun_picks_up_existing_sub():
    """Re-running the restore against an already-restored pool should
    succeed: AdminCreateUser raises UsernameExistsException, the code
    falls back to AdminGetUser and recovers the existing sub so the
    sub_map is built correctly even on re-runs."""
    user_record = {
        "Username": "colin",
        "Attributes": [
            {"Name": "sub", "Value": "OLD-SUB"},
            {"Name": "email", "Value": "colin@example.com"},
        ],
    }
    users_jsonl = json.dumps(user_record) + "\n"
    users_gz = gzip.compress(users_jsonl.encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("users.jsonl.gz"):
            return {"Body": io.BytesIO(users_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect
    s3.put_object = MagicMock()

    cognito = MagicMock()
    from botocore.exceptions import ClientError as _CE
    cognito.admin_create_user.side_effect = _CE(
        {"Error": {"Code": "UsernameExistsException"}}, "AdminCreateUser"
    )
    cognito.admin_get_user.return_value = {
        "Username": "colin",
        "UserAttributes": [
            {"Name": "sub", "Value": "EXISTING-NEW-SUB"},
            {"Name": "email", "Value": "colin@example.com"},
        ],
    }

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        restore.restore_cognito(ctx)

    assert ctx.sub_map == {"OLD-SUB": "EXISTING-NEW-SUB"}


# --------------------------------------------------------------------- #
# DDB restore applies the sub_map.                                     #
# --------------------------------------------------------------------- #
def test_dynamodb_restore_remaps_subs_in_items():
    """When ctx.sub_map is populated, restore_dynamodb_table rewrites
    every old sub → new sub in the item's string values BEFORE writing
    to DynamoDB (the load-bearing protection against orphaned data)."""
    component = {
        "logical_name": "users",
        "status": "ok",
        "detail": {
            "export_manifest": "root/dynamodb/users/AWSDynamoDB/x/manifest-summary.json",
        },
    }

    summary_payload = json.dumps({"manifestFilesS3Key": "root/dynamodb/users/AWSDynamoDB/x/manifest-files.json"})
    files_payload = json.dumps({"dataFileS3Key": "root/dynamodb/users/AWSDynamoDB/x/data.json.gz"}) + "\n"

    item = {
        "Item": {
            "PK": {"S": "USER#OLD-SUB"},
            "SK": {"S": "PROFILE"},
            "user_id": {"S": "OLD-SUB"},
            "email": {"S": "colin@example.com"},
            "audit_trail": {"L": [{"S": "actor=OLD-SUB"}, {"S": "ts=now"}]},
        }
    }
    data_payload = gzip.compress((json.dumps(item) + "\n").encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("manifest-summary.json"):
            return {"Body": io.BytesIO(summary_payload.encode())}
        if Key.endswith("manifest-files.json"):
            return {"Body": io.BytesIO(files_payload.encode())}
        if Key.endswith("data.json.gz"):
            return {"Body": io.BytesIO(data_payload)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    written_items: list[dict] = []

    class FakeBatch:
        def __enter__(self_inner): return self_inner
        def __exit__(self_inner, *a): return False
        def put_item(self_inner, Item):  # noqa: N803
            written_items.append(Item)

    fake_table = MagicMock()
    fake_table.batch_writer.return_value = FakeBatch()

    fake_dynamodb = MagicMock()
    fake_dynamodb.Table.return_value = fake_table

    def client_factory(name, *_a, **_kw):
        return s3
    def resource_factory(name, *_a, **_kw):
        return fake_dynamodb

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory
    ctx.session.resource.side_effect = resource_factory

    # Populate sub_map directly (in real restores, restore_cognito
    # would have done this before this function runs).
    ctx.sub_map = {"OLD-SUB": "NEW-SUB"}
    ctx.sub_map_pattern = restore.compile_sub_pattern(ctx.sub_map)

    with patch.object(restore, "get_ssm_param", return_value="ai-sbmt-api-users"):
        result = restore.restore_dynamodb_table(ctx, "users", component)

    assert result["status"] == "ok"
    assert result["items_written"] == 1
    assert result["items_remapped"] == 1

    [written] = written_items
    assert written["PK"] == "USER#NEW-SUB"
    assert written["user_id"] == "NEW-SUB"
    assert written["email"] == "colin@example.com"  # untouched
    assert written["audit_trail"][0] == "actor=NEW-SUB"  # nested rewrite
    assert written["audit_trail"][1] == "ts=now"


# --------------------------------------------------------------------- #
# S3 restore applies the sub_map to keys.                              #
# --------------------------------------------------------------------- #
def test_s3_restore_remaps_subs_in_keys():
    """A user-file-uploads bucket with keys like
    `users/<old-sub>/<file-id>.pdf` is copied to the target with the
    sub portion of the key rewritten to the new sub."""
    component = {
        "logical_name": "user-file-uploads",
        "status": "ok",
        "detail": {},  # source_prefix falls back to f"s3/{logical_name}/"
    }

    s3 = MagicMock()

    # list_objects_v2 paginator returns one page with two objects:
    # one whose key contains the old sub, one whose key doesn't.
    def get_paginator(_op_name):
        class P:
            def paginate(self, **kwargs):
                return iter([
                    {
                        "Contents": [
                            {"Key": "root/s3/user-file-uploads/users/OLD-SUB/file-1.pdf"},
                            {"Key": "root/s3/user-file-uploads/public/banner.png"},
                        ]
                    }
                ])
        return P()
    s3.get_paginator.side_effect = get_paginator
    s3.copy_object = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory
    ctx.sub_map = {"OLD-SUB": "NEW-SUB"}
    ctx.sub_map_pattern = restore.compile_sub_pattern(ctx.sub_map)

    with patch.object(restore, "get_ssm_param", return_value="ai-sbmt-api-user-file-uploads"):
        result = restore.restore_s3_bucket(ctx, "user-file-uploads", component)

    assert result["status"] == "ok"
    assert result["objects_copied"] == 2
    assert result["keys_remapped"] == 1

    # Inspect the copy_object calls
    target_keys = sorted(c.kwargs["Key"] for c in s3.copy_object.call_args_list)
    assert target_keys == ["public/banner.png", "users/NEW-SUB/file-1.pdf"]
    # Source keys preserved verbatim (we copy from the OLD-SUB key
    # because that's where the data lives in the backup).
    source_keys = sorted(c.kwargs["CopySource"]["Key"] for c in s3.copy_object.call_args_list)
    assert source_keys == [
        "root/s3/user-file-uploads/public/banner.png",
        "root/s3/user-file-uploads/users/OLD-SUB/file-1.pdf",
    ]


# --------------------------------------------------------------------- #
# Bug 8: AdminCreateUser leaves users in FORCE_CHANGE_PASSWORD state,   #
# which breaks the hosted UI's ForgotPassword flow (Cognito appears to  #
# accept the request but silently no-ops because there is no completed  #
# initial setup to reset against). Fix is to call AdminSetUserPassword  #
# with Permanent=True to transition them to CONFIRMED. Restored users  #
# can then ForgotPassword on first login and pick a real password.     #
# --------------------------------------------------------------------- #
def test_cognito_user_creation_transitions_to_confirmed():
    """After AdminCreateUser, restore must call AdminSetUserPassword
    with Permanent=True so the user lands in CONFIRMED state and the
    standard ForgotPassword flow works."""
    user_record = {
        "Username": "colin",
        "Attributes": [
            {"Name": "sub", "Value": "OLD-SUB"},
            {"Name": "email", "Value": "colin@example.com"},
            {"Name": "email_verified", "Value": "true"},
        ],
    }
    users_jsonl = json.dumps(user_record) + "\n"
    users_gz = gzip.compress(users_jsonl.encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("users.jsonl.gz"):
            return {"Body": io.BytesIO(users_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect
    s3.put_object = MagicMock()

    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "User": {
            "Username": "colin",
            "Attributes": [{"Name": "sub", "Value": "NEW-SUB"}],
        }
    }
    cognito.admin_set_user_password = MagicMock()

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        restore.restore_cognito(ctx)

    cognito.admin_set_user_password.assert_called_once()
    set_kwargs = cognito.admin_set_user_password.call_args.kwargs
    assert set_kwargs["UserPoolId"] == "us-west-2_test"
    assert set_kwargs["Username"] == "colin"
    assert set_kwargs["Permanent"] is True
    # Password should be at least 24 chars and contain all four char
    # classes — generic safety check, not a fixed string.
    pwd = set_kwargs["Password"]
    assert len(pwd) >= 24
    assert any(c.isupper() for c in pwd)
    assert any(c.islower() for c in pwd)
    assert any(c.isdigit() for c in pwd)
    assert any(c in "!@#$%^&*" for c in pwd)


def test_cognito_set_password_failure_is_warned_not_fatal():
    """If admin_set_user_password fails (e.g., policy mismatch), the
    restore continues with a warning. The user_count + sub_map for that
    user is still recorded so downstream DDB/S3 remap is not stalled."""
    user_record = {
        "Username": "colin",
        "Attributes": [
            {"Name": "sub", "Value": "OLD"},
            {"Name": "email", "Value": "colin@example.com"},
        ],
    }
    users_jsonl = json.dumps(user_record) + "\n"
    users_gz = gzip.compress(users_jsonl.encode("utf-8"))

    s3 = MagicMock()

    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("users.jsonl.gz"):
            return {"Body": io.BytesIO(users_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect
    s3.put_object = MagicMock()

    cognito = MagicMock()
    cognito.admin_create_user.return_value = {
        "User": {"Attributes": [{"Name": "sub", "Value": "NEW"}]}
    }
    from botocore.exceptions import ClientError as _CE
    cognito.admin_set_user_password.side_effect = _CE(
        {"Error": {"Code": "InvalidPasswordException"}}, "AdminSetUserPassword"
    )

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        results = restore.restore_cognito(ctx)

    user_result = next(r for r in results if r.get("component") == "cognito-users")
    assert user_result["status"] == "ok"
    # Mapping was still recorded — DDB/S3 remap should still work
    assert ctx.sub_map == {"OLD": "NEW"}


# ===================================================================== #
# AgentCore Memory event replay — verifies that backed-up events can be #
# replayed via CreateEvent with actorId remapping (sub_map), branch     #
# rootEventId rewriting, idempotent client tokens, and proper           #
# eventTimestamp parsing.                                                #
# ===================================================================== #
def test_memory_replay_skipped_when_flag_set():
    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.skip_memory_replay = True
    results = restore.restore_agentcore_memory(ctx)
    assert results == [{
        "component": "agentcore-memory",
        "status": "skipped",
        "reason": "--skip-memory-replay",
    }]


def test_memory_replay_skipped_when_no_target_memory():
    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    with patch.object(restore, "get_ssm_param", return_value=None):
        results = restore.restore_agentcore_memory(ctx)
    assert results[0]["status"] == "skipped"
    assert "memory id" in results[0]["reason"].lower()


def test_memory_replay_remaps_actor_id_via_sub_map():
    """An event with actorId == an old sub gets replayed with the new sub."""
    event_record = {
        "actorId": "OLD-SUB",
        "sessionId": "session-1",
        "event": {
            "actorId": "OLD-SUB",
            "sessionId": "session-1",
            "eventId": "orig-event-1",
            "eventTimestamp": "2026-05-21T18:00:00+00:00",
            "payload": [{"conversational": {"role": "USER", "content": {"text": "hi"}}}],
            "metadata": {"source": "chat"},
        },
    }
    events_jsonl = json.dumps(event_record) + "\n"
    events_gz = gzip.compress(events_jsonl.encode("utf-8"))

    s3 = MagicMock()
    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("events.jsonl.gz"):
            return {"Body": io.BytesIO(events_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    bedrock = MagicMock()
    bedrock.create_event.return_value = {"event": {"eventId": "new-event-1"}}

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "bedrock-agentcore":
            return bedrock
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory
    ctx.sub_map = {"OLD-SUB": "NEW-SUB"}
    ctx.sub_map_pattern = restore.compile_sub_pattern(ctx.sub_map)

    with patch.object(restore, "get_ssm_param", return_value="target-memory-id"):
        results = restore.restore_agentcore_memory(ctx)

    bedrock.create_event.assert_called_once()
    kwargs = bedrock.create_event.call_args.kwargs
    assert kwargs["memoryId"] == "target-memory-id"
    assert kwargs["actorId"] == "NEW-SUB"
    assert kwargs["sessionId"] == "session-1"
    assert kwargs["payload"] == event_record["event"]["payload"]
    assert kwargs["metadata"] == {"source": "chat"}
    assert kwargs["clientToken"]  # deterministic, present
    # Result counts include the actor-remap
    res = results[0]
    assert res["status"] == "ok"
    assert res["events_replayed"] == 1
    assert res["events_actor_remapped"] == 1


def test_memory_replay_rewrites_branch_root_event_id():
    """A branch-child event's rootEventId is rewritten using the
    old→new map populated by replaying the parent first."""
    parent = {
        "actorId": "user-A",
        "sessionId": "s1",
        "event": {
            "eventId": "parent-old",
            "actorId": "user-A",
            "sessionId": "s1",
            "eventTimestamp": "2026-05-21T18:00:00+00:00",
            "payload": [{"conversational": {"role": "USER", "content": {"text": "hi"}}}],
        },
    }
    child = {
        "actorId": "user-A",
        "sessionId": "s1",
        "event": {
            "eventId": "child-old",
            "actorId": "user-A",
            "sessionId": "s1",
            "eventTimestamp": "2026-05-21T18:01:00+00:00",
            "payload": [{"conversational": {"role": "USER", "content": {"text": "alt"}}}],
            "branch": {"name": "alternate", "rootEventId": "parent-old"},
        },
    }
    events_jsonl = json.dumps(parent) + "\n" + json.dumps(child) + "\n"
    events_gz = gzip.compress(events_jsonl.encode("utf-8"))

    s3 = MagicMock()
    def get_object_side_effect(*, Bucket, Key):
        if Key.endswith("events.jsonl.gz"):
            return {"Body": io.BytesIO(events_gz)}
        from botocore.exceptions import ClientError as _CE
        raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
    s3.get_object.side_effect = get_object_side_effect

    bedrock = MagicMock()
    # First call (parent) returns the new id; second (child) just succeeds.
    bedrock.create_event.side_effect = [
        {"event": {"eventId": "parent-new"}},
        {"event": {"eventId": "child-new"}},
    ]

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "bedrock-agentcore":
            return bedrock
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="target-memory-id"):
        results = restore.restore_agentcore_memory(ctx)

    assert bedrock.create_event.call_count == 2
    parent_kwargs = bedrock.create_event.call_args_list[0].kwargs
    child_kwargs = bedrock.create_event.call_args_list[1].kwargs

    assert "branch" not in parent_kwargs
    assert child_kwargs["branch"] == {"name": "alternate", "rootEventId": "parent-new"}

    res = results[0]
    assert res["events_replayed"] == 2
    assert res["events_branch_rewritten"] == 1


def test_memory_replay_client_token_is_deterministic():
    """Same (memory_id, original_event_id) → same client token, so re-runs
    hit AWS's idempotency window and don't double-write."""
    a = restore._client_token("mem-1", "ev-1")
    b = restore._client_token("mem-1", "ev-1")
    c = restore._client_token("mem-1", "ev-2")
    d = restore._client_token("mem-2", "ev-1")
    assert a == b
    assert a != c
    assert a != d
    assert len(a) == 64  # sha256 hex truncated/sized to 64


def test_memory_replay_missing_events_file_skips_cleanly():
    s3 = MagicMock()
    from botocore.exceptions import ClientError as _CE
    s3.get_object.side_effect = _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="target-memory-id"):
        results = restore.restore_agentcore_memory(ctx)
    assert results[0]["status"] == "skipped"
    assert "no events backup file" in results[0]["reason"]

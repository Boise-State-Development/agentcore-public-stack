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

    # AdminCreateUser was called with sanitised attrs AND with the SAFE
    # `migrated-<old-sub>` username — NOT the federated-pattern username
    # from the backup. Using the federated pattern would have caused
    # AdminLinkProviderForUser to fail with "Invalid SourceUser" and the
    # next IdP login to fail with "User already exists with provider
    # user id".
    create_kwargs = cognito.admin_create_user.call_args.kwargs
    assert create_kwargs["Username"] == "migrated-OLD-SUB-UUID"
    submitted_names = {a["Name"] for a in create_kwargs["UserAttributes"]}
    assert "sub" not in submitted_names
    assert "identities" not in submitted_names
    assert "email" in submitted_names

    # AdminLinkProviderForUser was called with the SAFE destination
    # username and the federated identity as the source.
    cognito.admin_link_provider_for_user.assert_called_once()
    link_kwargs = cognito.admin_link_provider_for_user.call_args.kwargs
    assert link_kwargs["DestinationUser"]["ProviderName"] == "Cognito"
    assert link_kwargs["DestinationUser"]["ProviderAttributeValue"] == "migrated-OLD-SUB-UUID"
    assert link_kwargs["SourceUser"]["ProviderName"] == "AzureAD"
    assert link_kwargs["SourceUser"]["ProviderAttributeName"] == "Cognito_Subject"
    assert link_kwargs["SourceUser"]["ProviderAttributeValue"] == "entra-user-id"

    # AdminSetUserPassword (CONFIRMED transition) targets the same name.
    sp_kwargs = cognito.admin_set_user_password.call_args.kwargs
    assert sp_kwargs["Username"] == "migrated-OLD-SUB-UUID"

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


# --------------------------------------------------------------------------- #
# DynamoDB-JSON binary-attribute decoding                                      #
# --------------------------------------------------------------------------- #
def test_deserialize_dynamodb_json_decodes_binary_top_level():
    """B-typed values arrive in the export as base64 strings. The
    deserializer must decode them to bytes before handing the item to
    boto3's TypeDeserializer (which raises TypeError on raw strings)."""
    import base64
    from boto3.dynamodb.types import Binary
    payload = b"\x00\x01\x02\x03"
    item = {
        "PK": {"S": "SESSION#abc"},
        "blob": {"B": base64.b64encode(payload).decode("ascii")},
    }
    result = restore._deserialize_dynamodb_json(item)
    assert result["PK"] == "SESSION#abc"
    assert result["blob"] == Binary(payload)


def test_deserialize_dynamodb_json_decodes_binary_set():
    """BS-typed values are lists of base64 strings; each member must be
    decoded individually."""
    import base64
    parts = [b"\x10\x20", b"\x30\x40"]
    item = {
        "PK": {"S": "x"},
        "blobs": {"BS": [base64.b64encode(p).decode("ascii") for p in parts]},
    }
    result = restore._deserialize_dynamodb_json(item)
    assert {bytes(b) for b in result["blobs"]} == {bytes(p) for p in parts}


def test_deserialize_dynamodb_json_decodes_nested_binary_in_map():
    """Binary attributes nested inside an M (map) must be decoded too —
    that's how DynamoDB exports represent struct-shaped fields like
    `payload: {hash: <bytes>, body: <str>}`."""
    import base64
    from boto3.dynamodb.types import Binary
    payload = b"\xde\xad\xbe\xef"
    item = {
        "PK": {"S": "x"},
        "envelope": {"M": {
            "hash": {"B": base64.b64encode(payload).decode("ascii")},
            "label": {"S": "test"},
        }},
    }
    result = restore._deserialize_dynamodb_json(item)
    assert result["envelope"]["hash"] == Binary(payload)
    assert result["envelope"]["label"] == "test"


def test_deserialize_dynamodb_json_decodes_nested_binary_in_list():
    """Binary attributes nested inside an L (list) must be decoded too."""
    import base64
    from boto3.dynamodb.types import Binary
    payload = b"\xff\xfe"
    item = {
        "PK": {"S": "x"},
        "items": {"L": [
            {"B": base64.b64encode(payload).decode("ascii")},
            {"S": "after"},
        ]},
    }
    result = restore._deserialize_dynamodb_json(item)
    assert result["items"][0] == Binary(payload)
    assert result["items"][1] == "after"


def test_deserialize_dynamodb_json_passes_through_non_binary_unchanged():
    """The decoder must not alter S/N/BOOL/NULL/SS/NS values."""
    item = {
        "name":   {"S": "alice"},
        "count":  {"N": "42"},
        "active": {"BOOL": True},
        "missing": {"NULL": True},
        "tags":   {"SS": ["a", "b"]},
        "scores": {"NS": ["1", "2"]},
    }
    result = restore._deserialize_dynamodb_json(item)
    from decimal import Decimal
    assert result["name"] == "alice"
    assert result["count"] == Decimal("42")
    assert result["active"] is True
    assert result["missing"] is None
    assert result["tags"] == {"a", "b"}
    assert result["scores"] == {Decimal("1"), Decimal("2")}


def test_boto_config_pool_size_covers_thread_workers():
    """The BOTO_CONFIG max_pool_connections must be >= the
    ThreadPoolExecutor worker count used by the S3 and Memory restore
    paths; otherwise urllib3 emits "Connection pool is full, discarding
    connection" warnings under load (no data loss but lots of wasted
    TLS handshakes). Pinned here so a future BOTO_CONFIG edit doesn't
    silently regress."""
    pool_size = restore.BOTO_CONFIG.max_pool_connections
    assert pool_size is not None
    # The S3 copy and Memory replay paths both use max_workers=16.
    assert pool_size >= 16, (
        f"BOTO_CONFIG.max_pool_connections={pool_size} is below the "
        f"ThreadPoolExecutor max_workers=16 used by S3 / Memory restore."
    )


# --------------------------------------------------------------------------- #
# Cognito IdP → app-client wiring                                              #
# --------------------------------------------------------------------------- #
def test_cognito_restore_wires_idps_into_app_client():
    """After restoring an IdP, the restore must call update_user_pool_client
    to add the provider to SupportedIdentityProviders on every CDK-managed
    app client. Without this step, Cognito's hosted UI shows
    'Login option is not available' immediately after a restore."""
    import io as _io, json as _json

    # --- Build fake S3 responses ---
    idp_json = _json.dumps({"providers": [
        {"ProviderName": "ms-entra-id", "ProviderType": "OIDC",
         "ProviderDetails": {"client_id": "abc", "oidc_issuer": "https://issuer", "client_secret": "s"},
         "AttributeMapping": {}, "IdpIdentifiers": []}
    ]}).encode()
    client_json = _json.dumps({"clients": [
        {"ClientId": "old-client-id", "ClientName": "my-prefix-bff-app-client"}
    ]}).encode()
    users_gz = gzip.compress(b"")

    def s3_get(Bucket, Key):
        m = {
            "root/cognito/identity-providers.json": {"Body": _io.BytesIO(idp_json)},
            "root/cognito/app-clients.json":        {"Body": _io.BytesIO(client_json)},
            "root/cognito/users.jsonl.gz":          {"Body": _io.BytesIO(users_gz)},
        }
        if Key not in m:
            from botocore.exceptions import ClientError as _CE
            raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return m[Key]

    s3 = MagicMock()
    s3.get_object.side_effect = s3_get

    # Cognito mock: create_identity_provider succeeds, list returns
    # the CDK-managed app client with only COGNITO in SupportedIdPs,
    # describe returns a minimal but plausible client spec.
    cognito = MagicMock()
    cognito.create_identity_provider.return_value = {}
    cognito.get_paginator.return_value.paginate.return_value = iter([{
        "UserPoolClients": [{"ClientId": "live-client-id", "ClientName": "my-prefix-bff-app-client"}]
    }])
    cognito.describe_user_pool_client.return_value = {"UserPoolClient": {
        "UserPoolId": "us-west-2_POOL",
        "ClientId": "live-client-id",
        "ClientName": "my-prefix-bff-app-client",
        "SupportedIdentityProviders": ["COGNITO"],
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid"],
        "AllowedOAuthFlowsUserPoolClient": True,
        "CallbackURLs": ["https://example.com/callback"],
        "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
    }}
    cognito.update_user_pool_client.return_value = {}
    # Users path
    cognito.list_users.return_value = {"Users": []}
    cognito.list_groups.return_value = {"Groups": []}

    def client_factory(name, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_POOL"):
        restore.restore_cognito(ctx)

    # The critical assertion: update_user_pool_client must be called
    # with the restored IdP in SupportedIdentityProviders.
    cognito.update_user_pool_client.assert_called_once()
    call_kwargs = cognito.update_user_pool_client.call_args.kwargs
    assert "ms-entra-id" in call_kwargs["SupportedIdentityProviders"]
    assert "COGNITO" in call_kwargs["SupportedIdentityProviders"]


def test_cognito_restore_skips_idp_wiring_when_already_present():
    """If the app client already has the IdP in SupportedIdentityProviders
    (e.g. on a re-run), update_user_pool_client must NOT be called."""
    import io as _io, json as _json

    idp_json = _json.dumps({"providers": [
        {"ProviderName": "ms-entra-id", "ProviderType": "OIDC",
         "ProviderDetails": {"client_id": "abc", "oidc_issuer": "https://issuer", "client_secret": "s"},
         "AttributeMapping": {}, "IdpIdentifiers": []}
    ]}).encode()
    client_json = _json.dumps({"clients": [
        {"ClientId": "old-id", "ClientName": "my-prefix-bff-app-client"}
    ]}).encode()
    users_gz = gzip.compress(b"")

    def s3_get(Bucket, Key):
        m = {
            "root/cognito/identity-providers.json": {"Body": _io.BytesIO(idp_json)},
            "root/cognito/app-clients.json":        {"Body": _io.BytesIO(client_json)},
            "root/cognito/users.jsonl.gz":          {"Body": _io.BytesIO(users_gz)},
        }
        if Key not in m:
            from botocore.exceptions import ClientError as _CE
            raise _CE({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return m[Key]

    s3 = MagicMock()
    s3.get_object.side_effect = s3_get
    cognito = MagicMock()
    cognito.create_identity_provider.side_effect = Exception("DuplicateProviderException")
    cognito.get_paginator.return_value.paginate.return_value = iter([{
        "UserPoolClients": [{"ClientId": "live-id", "ClientName": "my-prefix-bff-app-client"}]
    }])
    cognito.describe_user_pool_client.return_value = {"UserPoolClient": {
        "UserPoolId": "us-west-2_POOL",
        "ClientId": "live-id",
        "SupportedIdentityProviders": ["COGNITO", "ms-entra-id"],  # already wired
        "AllowedOAuthFlows": ["code"],
        "AllowedOAuthScopes": ["openid"],
        "AllowedOAuthFlowsUserPoolClient": True,
        "ExplicitAuthFlows": ["ALLOW_REFRESH_TOKEN_AUTH"],
    }}
    cognito.list_users.return_value = {"Users": []}
    cognito.list_groups.return_value = {"Groups": []}

    def client_factory(name, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_POOL"):
        try:
            restore.restore_cognito(ctx)
        except Exception:
            pass  # create_identity_provider intentionally raises above

    cognito.update_user_pool_client.assert_not_called()


# --------------------------------------------------------------------------- #
# _compute_created_username — federated user safe-username minting             #
# --------------------------------------------------------------------------- #
def test_compute_created_username_native_user_keeps_original():
    """Users with no identities (purely native users) must keep their
    original username — that's a no-behavior-change path for the
    common case."""
    assert restore._compute_created_username(
        original_username="alice",
        old_sub="abc-uuid",
        identities=[],
    ) == "alice"


def test_compute_created_username_federated_user_uses_migrated_prefix():
    """Federated users (non-empty identities) must NOT reuse the
    original username — that's Cognito's reserved `<provider>_<provider_user_id>`
    pattern and breaks AdminLinkProviderForUser + the next IdP login.
    Use a deterministic `migrated-<old-sub>` instead."""
    assert restore._compute_created_username(
        original_username="ms-entra-id_someProviderSub",
        old_sub="OLD-SUB-UUID",
        identities=[{"providerName": "ms-entra-id", "userId": "someProviderSub"}],
    ) == "migrated-OLD-SUB-UUID"


def test_compute_created_username_falls_back_to_hash_when_old_sub_missing():
    """If the backup is missing `sub` for a federated user (rare but
    possible from a partial export), the helper still produces a
    deterministic, collision-free name by hashing the identity tuple."""
    name = restore._compute_created_username(
        original_username="ms-entra-id_someProviderSub",
        old_sub=None,
        identities=[{"providerName": "ms-entra-id", "userId": "someProviderSub"}],
    )
    assert name.startswith("migrated-")
    # 'migrated-' + 16 hex chars
    assert len(name) == len("migrated-") + 16
    # Determinism: same inputs ⇒ same output
    name2 = restore._compute_created_username(
        original_username="ms-entra-id_someProviderSub",
        old_sub=None,
        identities=[{"providerName": "ms-entra-id", "userId": "someProviderSub"}],
    )
    assert name == name2
    # Distinct inputs ⇒ distinct outputs
    other = restore._compute_created_username(
        original_username="ms-entra-id_anotherSub",
        old_sub=None,
        identities=[{"providerName": "ms-entra-id", "userId": "anotherSub"}],
    )
    assert name != other


def test_compute_created_username_never_matches_federated_pattern():
    """Cognito reserves usernames matching `<provider>_<provider_user_id>`.
    The minted name must not look like that pattern — a leading
    `migrated-` token guarantees it doesn't."""
    name = restore._compute_created_username(
        original_username="ms-entra-id_xyz",
        old_sub="abc-uuid",
        identities=[{"providerName": "ms-entra-id", "userId": "xyz"}],
    )
    # Cognito's reserved pattern is <provider>_<provider_user_id>; a
    # username starting with `migrated-` and containing a dash before
    # any underscore won't match how Cognito parses federated names.
    assert "_" not in name.split("-", 1)[0]
    assert name.startswith("migrated-")


# --------------------------------------------------------------------------- #
# Federated-user re-run idempotency (post-restore)                            #
# --------------------------------------------------------------------------- #
def test_cognito_federated_user_idempotent_rerun_uses_migrated_username():
    """On re-run of restore against a pool where the federated user was
    already migrated (under `migrated-<old-sub>`), AdminCreateUser raises
    UsernameExistsException and the code must fall back to AdminGetUser
    targeting the SAME `migrated-<old-sub>` name — not the original
    federated-pattern username from the backup."""
    user_record = {
        "Username": "ms-entra-id_someProviderSub",
        "Attributes": [
            {"Name": "sub", "Value": "OLD-SUB-UUID"},
            {"Name": "email", "Value": "u@example.com"},
            {
                "Name": "identities",
                "Value": json.dumps([
                    {"providerName": "ms-entra-id", "userId": "someProviderSub"}
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
    s3.put_object = MagicMock()

    cognito = MagicMock()
    from botocore.exceptions import ClientError as _CE
    cognito.admin_create_user.side_effect = _CE(
        {"Error": {"Code": "UsernameExistsException", "Message": "User account already exists"}},
        "AdminCreateUser",
    )
    cognito.admin_get_user.return_value = {
        "UserAttributes": [
            {"Name": "sub", "Value": "EXISTING-SUB"},
            {"Name": "email", "Value": "u@example.com"},
        ]
    }

    def client_factory(name, *_a, **_kw):
        return s3 if name == "s3" else cognito

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="us-west-2_test"):
        restore.restore_cognito(ctx)

    # AdminCreateUser tried with the SAFE migrated-* username.
    create_kwargs = cognito.admin_create_user.call_args.kwargs
    assert create_kwargs["Username"] == "migrated-OLD-SUB-UUID"

    # AdminGetUser fallback used the SAME migrated-* username. If we
    # used the original federated-pattern username here we'd silently
    # fail to link this user on re-runs.
    get_kwargs = cognito.admin_get_user.call_args.kwargs
    assert get_kwargs["Username"] == "migrated-OLD-SUB-UUID"

    # Sub map records old → existing-on-rerun.
    assert ctx.sub_map == {"OLD-SUB-UUID": "EXISTING-SUB"}


# --------------------------------------------------------------------------- #
# S3 Vectors restore                                                          #
# --------------------------------------------------------------------------- #
def _vectors_jsonl_gz(records: list[dict]) -> bytes:
    """Helper: build a gzipped JSONL body from a list of put_vectors records."""
    body = "\n".join(json.dumps(r) for r in records).encode("utf-8")
    return gzip.compress(body)


def test_restore_vector_index_replays_via_put_vectors():
    """Backed-up vectors must be re-pushed to the target index in batches
    of 50 (matching bedrock_embeddings.store_embeddings_in_s3.BATCH_SIZE).
    Each record's key/data/metadata must round-trip 1:1."""
    records = [
        {
            "key": f"doc-1#{i}",
            "data": {"float32": [0.1 * i, 0.2 * i, 0.3 * i]},
            "metadata": {"text": f"chunk {i}", "assistant_id": "ast-abc",
                         "document_id": "doc-1", "source": "report.pdf"},
        }
        for i in range(125)  # > 2 batches of 50 to confirm flushing logic
    ]
    body = _vectors_jsonl_gz(records)

    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(body)}
    s3vectors = MagicMock()
    s3vectors.put_vectors.return_value = {}

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "s3vectors":
            return s3vectors
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    def fake_get(_session, _prefix, ssm_path):
        if ssm_path.endswith("/vector-bucket-name"):
            return "target-vector-bucket"
        if ssm_path.endswith("/vector-index-name"):
            return "target-vector-index"
        return None

    with patch.object(restore, "get_ssm_param", side_effect=fake_get):
        result = restore.restore_vector_index(ctx, "rag-vectors")

    assert result["status"] == "ok"
    assert result["vectors_written"] == 125
    # 125 records / 50 batch = 3 batches (50 + 50 + 25)
    assert result["batches_sent"] == 3
    assert result["target_bucket"] == "target-vector-bucket"
    assert result["target_index"] == "target-vector-index"

    # Verify put_vectors was called 3 times with the right batch sizes
    assert s3vectors.put_vectors.call_count == 3
    sizes = [len(c.kwargs["vectors"]) for c in s3vectors.put_vectors.call_args_list]
    assert sizes == [50, 50, 25]
    # Bucket + index args constant across calls
    for c in s3vectors.put_vectors.call_args_list:
        assert c.kwargs["vectorBucketName"] == "target-vector-bucket"
        assert c.kwargs["indexName"] == "target-vector-index"
    # First record matches the input shape exactly (1:1 round-trip)
    first_pushed = s3vectors.put_vectors.call_args_list[0].kwargs["vectors"][0]
    assert first_pushed == records[0]


def test_restore_vector_index_skips_when_backup_file_missing():
    """Older backups (pre-vectors-support) won't have vectors/*.jsonl.gz.
    Restore must skip cleanly with a clear reason rather than failing."""
    from botocore.exceptions import ClientError as _CE
    s3 = MagicMock()
    s3.get_object.side_effect = _CE(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject",
    )
    s3vectors = MagicMock()

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "s3vectors":
            return s3vectors
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    def fake_get(_session, _prefix, ssm_path):
        if ssm_path.endswith("/vector-bucket-name"):
            return "target-vector-bucket"
        if ssm_path.endswith("/vector-index-name"):
            return "target-vector-index"
        return None

    with patch.object(restore, "get_ssm_param", side_effect=fake_get):
        result = restore.restore_vector_index(ctx, "rag-vectors")

    assert result["status"] == "skipped"
    assert "no vectors backup file" in result["reason"]
    s3vectors.put_vectors.assert_not_called()


def test_restore_vector_index_skips_when_target_bucket_missing():
    """If the target platform doesn't publish the vector-bucket-name SSM
    (e.g. RAG disabled in this prefix), skip gracefully without making
    any AWS calls beyond the SSM lookup."""
    s3 = MagicMock()
    s3vectors = MagicMock()

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "s3vectors":
            return s3vectors
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value=None):
        result = restore.restore_vector_index(ctx, "rag-vectors")

    assert result["status"] == "skipped"
    assert "vector bucket/index not found" in result["reason"]
    s3.get_object.assert_not_called()
    s3vectors.put_vectors.assert_not_called()


def test_restore_vector_index_dry_run_does_not_call_put_vectors():
    """Dry run must report what *would* be restored without making any
    s3vectors API calls."""
    records = [
        {"key": f"k{i}", "data": {"float32": [0.0]}, "metadata": {}}
        for i in range(7)
    ]
    body = _vectors_jsonl_gz(records)

    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(body)}
    s3vectors = MagicMock()

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "s3vectors":
            return s3vectors
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=True)
    ctx.session.client.side_effect = client_factory

    def fake_get(_session, _prefix, ssm_path):
        if ssm_path.endswith("/vector-bucket-name"):
            return "target-bucket"
        if ssm_path.endswith("/vector-index-name"):
            return "target-index"
        return None

    with patch.object(restore, "get_ssm_param", side_effect=fake_get):
        result = restore.restore_vector_index(ctx, "rag-vectors")

    assert result["status"] == "skipped"
    assert result["reason"] == "dry-run"
    assert result["vectors_in_backup"] == 7
    s3vectors.put_vectors.assert_not_called()


def test_restore_vector_index_idempotent_on_rerun_via_put_vectors_upsert():
    """put_vectors keyed on `key` is an upsert in S3 Vectors — re-running
    the restore should call put_vectors with the SAME records again
    without raising. The test exercises that path."""
    records = [
        {"key": "doc-1#0", "data": {"float32": [0.1]}, "metadata": {"a": 1}},
    ]
    body = _vectors_jsonl_gz(records)

    s3 = MagicMock()
    s3.get_object.return_value = {"Body": io.BytesIO(body)}
    s3vectors = MagicMock()
    s3vectors.put_vectors.return_value = {}

    def client_factory(name, *_a, **_kw):
        if name == "s3":
            return s3
        if name == "s3vectors":
            return s3vectors
        return MagicMock()

    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    ctx.session.client.side_effect = client_factory

    with patch.object(restore, "get_ssm_param", return_value="target"):
        # Need to return a non-None for both bucket and index lookups —
        # the side-effect form would be cleaner but a plain return value
        # is enough here since both lookups expect a string.
        r1 = restore.restore_vector_index(ctx, "rag-vectors")

    # Re-run — same input, fresh body (S3 Body is a one-shot stream)
    s3.get_object.return_value = {"Body": io.BytesIO(body)}
    with patch.object(restore, "get_ssm_param", return_value="target"):
        r2 = restore.restore_vector_index(ctx, "rag-vectors")

    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert s3vectors.put_vectors.call_count == 2
    # Both calls had the same vectors payload
    assert (
        s3vectors.put_vectors.call_args_list[0].kwargs["vectors"]
        == s3vectors.put_vectors.call_args_list[1].kwargs["vectors"]
    )


def test_restore_vector_index_skips_unknown_logical_name():
    """Defensive: if a manifest references a logical name that's not in
    VECTOR_INDEXES, skip cleanly without crashing."""
    ctx = _make_ctx({"root_prefix": "root"}, dry_run=False)
    result = restore.restore_vector_index(ctx, "nonexistent-vector-store")
    assert result["status"] == "skipped"
    assert "no matching VECTOR_INDEXES entry" in result["reason"]


def test_restore_vector_index_in_vector_indexes_constant():
    """The known live deployment uses the `rag-vectors` logical name."""
    logicals = {c["logical"] for c in restore.VECTOR_INDEXES}
    assert "rag-vectors" in logicals

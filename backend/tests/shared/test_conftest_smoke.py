"""Smoke tests: verify every moto fixture creates a usable table/resource."""

import pytest


class TestDynamoDBTables:
    def test_users_table(self, users_table):
        users_table.put_item(Item={"PK": "USER#u1", "SK": "PROFILE", "userId": "u1", "email": "a@b.com"})
        assert users_table.get_item(Key={"PK": "USER#u1", "SK": "PROFILE"})["Item"]["email"] == "a@b.com"

    def test_auth_providers_table(self, auth_providers_table):
        auth_providers_table.put_item(Item={"PK": "AP#1", "SK": "AP#1", "GSI1PK": "ENABLED"})
        assert auth_providers_table.scan()["Count"] == 1

    def test_oauth_providers_table(self, oauth_providers_table):
        oauth_providers_table.put_item(Item={"PK": "P#1", "SK": "CONFIG", "GSI1PK": "ENABLED"})
        assert oauth_providers_table.scan()["Count"] == 1

    def test_oauth_tokens_table(self, oauth_tokens_table):
        oauth_tokens_table.put_item(Item={"PK": "USER#u1", "SK": "PROVIDER#p1", "GSI1PK": "PROVIDER#p1"})
        assert oauth_tokens_table.scan()["Count"] == 1

    def test_files_table(self, files_table):
        files_table.put_item(Item={"PK": "USER#u1", "SK": "FILE#f1", "GSI1PK": "CONV#s1", "GSI1SK": "FILE#f1"})
        assert files_table.scan()["Count"] == 1

    def test_roles_table(self, roles_table):
        roles_table.put_item(Item={"PK": "ROLE#r1", "SK": "DEFINITION"})
        assert roles_table.scan()["Count"] == 1

    def test_managed_models_table(self, managed_models_table):
        managed_models_table.put_item(Item={"PK": "MODEL#m1", "SK": "MODEL#m1", "GSI1PK": "MODEL#gpt4", "GSI1SK": "MODEL#m1"})
        assert managed_models_table.scan()["Count"] == 1

    def test_sessions_metadata_table(self, sessions_metadata_table):
        sessions_metadata_table.put_item(Item={"PK": "USER#u1", "SK": "C#ts#id", "GSI1PK": "USER#u1", "GSI1SK": "ts", "GSI_PK": "SESSION#s1", "GSI_SK": "C#ts"})
        assert sessions_metadata_table.scan()["Count"] == 1

    def test_assistants_table(self, assistants_table):
        assistants_table.put_item(Item={"PK": "AST#a1", "SK": "METADATA", "GSI_PK": "OWNER#o1", "GSI_SK": "STATUS#active", "GSI2_PK": "VIS#public", "GSI2_SK": "STATUS#active", "GSI3_PK": "SHARE#x", "GSI3_SK": "AST#a1"})
        assert assistants_table.scan()["Count"] == 1


class TestAWSServices:
    def test_kms_encrypt_decrypt(self, kms_key_arn):
        import boto3
        kms = boto3.client("kms", region_name="us-east-1")
        enc = kms.encrypt(KeyId=kms_key_arn, Plaintext=b"secret")
        dec = kms.decrypt(CiphertextBlob=enc["CiphertextBlob"])
        assert dec["Plaintext"] == b"secret"

    def test_s3_put_get(self, s3_bucket):
        import boto3
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(Bucket=s3_bucket, Key="test.txt", Body=b"hello")
        obj = s3.get_object(Bucket=s3_bucket, Key="test.txt")
        assert obj["Body"].read() == b"hello"

    def test_secrets_manager(self, secrets_manager):
        val = secrets_manager.get_secret_value(SecretId="auth-provider-secrets")
        assert val["SecretString"] == "{}"


class TestRepositoryFactories:
    def test_user_repository(self, user_repository):
        assert user_repository.enabled

    def test_auth_provider_repository(self, auth_provider_repository):
        assert auth_provider_repository.enabled

    def test_oauth_provider_repository(self, oauth_provider_repository):
        assert oauth_provider_repository.enabled

    def test_file_repository(self, file_repository):
        assert file_repository is not None

    def test_role_repository(self, role_repository):
        assert role_repository is not None

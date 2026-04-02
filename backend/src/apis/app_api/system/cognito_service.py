"""Cognito service for first-boot user creation and pool management."""

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CognitoService:
    """Encapsulates Cognito Admin API operations for first-boot flow."""

    def __init__(
        self,
        user_pool_id: Optional[str] = None,
        region: Optional[str] = None,
    ):
        self._user_pool_id = user_pool_id or os.getenv("COGNITO_USER_POOL_ID")
        self._region = region or os.getenv("AWS_REGION", "us-west-2")
        self._enabled = bool(self._user_pool_id)

        if not self._enabled:
            logger.warning(
                "COGNITO_USER_POOL_ID not set. Cognito service is disabled."
            )
            return

        profile = os.getenv("AWS_PROFILE")
        if profile:
            session = boto3.Session(profile_name=profile)
            self._client = session.client(
                "cognito-idp", region_name=self._region
            )
        else:
            self._client = boto3.client(
                "cognito-idp", region_name=self._region
            )

        logger.info(
            f"Initialized Cognito service: pool={self._user_pool_id}"
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def user_pool_id(self) -> Optional[str]:
        return self._user_pool_id

    def create_admin_user(
        self, username: str, email: str, password: str
    ) -> str:
        """
        Create a user in Cognito and set a permanent password.

        Uses AdminCreateUser with MessageAction=SUPPRESS to skip the
        welcome email, then AdminSetUserPassword with Permanent=True
        to bypass the forced password change on first login.

        Args:
            username: The desired username.
            email: The user's email address.
            password: The permanent password.

        Returns:
            The Cognito user ``sub`` (unique user ID).

        Raises:
            ClientError: On Cognito API failures (e.g.
                ``InvalidPasswordException``, ``UsernameExistsException``).
        """
        if not self._enabled:
            raise RuntimeError("Cognito service is not enabled")

        # Step 1: Create user with a temporary password (suppressed invite)
        response = self._client.admin_create_user(
            UserPoolId=self._user_pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )

        # Extract the sub from the created user attributes
        user_sub = ""
        for attr in response["User"]["Attributes"]:
            if attr["Name"] == "sub":
                user_sub = attr["Value"]
                break

        # Step 2: Set permanent password (skips FORCE_CHANGE_PASSWORD)
        self._client.admin_set_user_password(
            UserPoolId=self._user_pool_id,
            Username=username,
            Password=password,
            Permanent=True,
        )

        logger.info("Created Cognito admin user successfully")
        return user_sub

    def delete_user(self, username: str) -> None:
        """
        Delete a user from Cognito. Used for rollback on failure.

        Args:
            username: The Cognito username to delete.
        """
        if not self._enabled:
            return

        try:
            self._client.admin_delete_user(
                UserPoolId=self._user_pool_id,
                Username=username,
            )
            logger.info(f"Deleted Cognito user (rollback): {username}")
        except ClientError:
            logger.exception(
                f"Failed to delete Cognito user during rollback: {username}"
            )

    def add_user_to_group(self, username: str, group_name: str) -> None:
        """
        Add a user to a Cognito User Pool group, creating the group if needed.

        The group membership causes Cognito to include the group name in the
        ``cognito:groups`` claim of the JWT token, which the RBAC system uses
        to resolve AppRole permissions.

        Args:
            username: The Cognito username.
            group_name: The group to add the user to.
        """
        if not self._enabled:
            return

        # Ensure the group exists (idempotent)
        try:
            self._client.create_group(
                GroupName=group_name,
                UserPoolId=self._user_pool_id,
                Description=f"Auto-created group for {group_name} role",
            )
            logger.info(f"Created Cognito group: {group_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "GroupExistsException":
                raise
            # Group already exists — fine

        self._client.admin_add_user_to_group(
            UserPoolId=self._user_pool_id,
            Username=username,
            GroupName=group_name,
        )
        logger.info(f"Added user {username} to Cognito group: {group_name}")

    def disable_self_signup(self) -> None:
        """
        Disable self-signup on the User Pool by setting
        AllowAdminCreateUserOnly=true.

        Only updates AdminCreateUserConfig; the existing password policy
        (configured by CDK) is preserved.
        """
        if not self._enabled:
            raise RuntimeError("Cognito service is not enabled")

        self._client.update_user_pool(
            UserPoolId=self._user_pool_id,
            AdminCreateUserConfig={
                "AllowAdminCreateUserOnly": True,
            },
        )
        logger.info("Disabled self-signup on Cognito User Pool")


# Singleton instance
_cognito_service: Optional[CognitoService] = None


def get_cognito_service() -> CognitoService:
    """Get the Cognito service singleton."""
    global _cognito_service
    if _cognito_service is None:
        _cognito_service = CognitoService()
    return _cognito_service

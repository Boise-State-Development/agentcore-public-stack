"""Helpers for loading the OpenAI API key in deployed environments."""

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


def _build_secrets_client(region: str):
    profile = os.getenv("AWS_PROFILE")
    if profile:
        session = boto3.Session(profile_name=profile)
        return session.client("secretsmanager", region_name=region)
    return boto3.client("secretsmanager", region_name=region)


def _extract_secret_value(secret_string: str) -> str:
    # The deployment docs populate the secret as a raw string. Accept that directly.
    return secret_string.strip()


def hydrate_openai_api_key(secret_arn: Optional[str] = None) -> bool:
    """
    Load OPENAI_API_KEY from Secrets Manager when only the secret ARN is configured.

    Returns True when the key is available after hydration, False otherwise.
    """
    if os.getenv("OPENAI_API_KEY"):
        return True

    secret_id = secret_arn or os.getenv("OPENAI_API_KEY_SECRET_ARN")
    if not secret_id:
        logger.debug("OPENAI_API_KEY_SECRET_ARN not set; skipping OpenAI key hydration")
        return False

    region = os.getenv("AWS_REGION", "us-west-2")

    try:
        secrets_client = _build_secrets_client(region)
        response = secrets_client.get_secret_value(SecretId=secret_id)
        secret_string = response.get("SecretString")
        if not secret_string:
            logger.warning("OpenAI API key secret %s did not contain SecretString", secret_id)
            return False

        api_key = _extract_secret_value(secret_string)
        if not api_key:
            logger.warning("OpenAI API key secret %s was empty", secret_id)
            return False

        os.environ["OPENAI_API_KEY"] = api_key
        logger.info("Hydrated OPENAI_API_KEY from Secrets Manager")
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Failed to hydrate OPENAI_API_KEY from Secrets Manager: %s", exc)
        return False

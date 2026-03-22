#!/usr/bin/env python3
"""
Seed an OIDC authentication provider for first-time platform setup.

This script writes directly to DynamoDB and Secrets Manager, bypassing the
API entirely. Use it to bootstrap the very first auth provider before any
users can log in.

Usage:
    # Interactive mode (prompts for all values)
    python scripts/seed_auth_provider.py

    # Non-interactive with all required flags
    python scripts/seed_auth_provider.py \
        --provider-id entra-id \
        --display-name "Microsoft Entra ID" \
        --issuer-url "https://login.microsoftonline.com/{tenant}/v2.0" \
        --client-id "your-client-id" \
        --client-secret "your-client-secret" \
        --table-name auth-providers \
        --secrets-arn "arn:aws:secretsmanager:us-west-2:123456789:secret:auth-provider-secrets-xxxxx"

    # With OIDC auto-discovery and custom claim mappings
    python scripts/seed_auth_provider.py \
        --provider-id okta-prod \
        --display-name "Okta" \
        --issuer-url "https://dev-12345.okta.com/oauth2/default" \
        --client-id "0oa..." \
        --client-secret "secret" \
        --discover \
        --user-id-claim sub \
        --roles-claim groups \
        --table-name auth-providers \
        --secrets-arn "arn:aws:secretsmanager:..."

    # Dry-run to preview what would be written
    python scripts/seed_auth_provider.py --dry-run ...

Prerequisites:
    - AWS credentials configured (via env vars, profile, or IAM role)
    - DynamoDB auth-providers table deployed (via CDK AppApiStack)
    - Secrets Manager secret created (via CDK AppApiStack)
    - uv sync (boto3 and httpx are included in project dependencies)
"""

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def discover_oidc_endpoints(issuer_url: str) -> Dict[str, Any]:
    """Fetch OIDC configuration from .well-known endpoint."""
    import httpx

    discovery_url = issuer_url.rstrip("/") + "/.well-known/openid-configuration"
    print(f"  Discovering endpoints from: {discovery_url}")

    try:
        response = httpx.get(discovery_url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        print("  Discovery successful")
        return data
    except httpx.HTTPStatusError as e:
        print(f"  WARNING: Discovery failed with HTTP {e.response.status_code}")
        return {}
    except Exception as e:
        print(f"  WARNING: Discovery failed: {e}")
        return {}


def build_dynamo_item(args: argparse.Namespace, discovered: Dict[str, Any]) -> Dict[str, Any]:
    """Build the DynamoDB item from CLI args and discovered endpoints."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    item: Dict[str, Any] = {
        "PK": f"AUTH_PROVIDER#{args.provider_id}",
        "SK": f"AUTH_PROVIDER#{args.provider_id}",
        "GSI1PK": f"ENABLED#{str(args.enabled).lower()}",
        "GSI1SK": f"AUTH_PROVIDER#{args.provider_id}",
        "providerId": args.provider_id,
        "displayName": args.display_name,
        "providerType": "oidc",
        "enabled": args.enabled,
        "issuerUrl": args.issuer_url,
        "clientId": args.client_id,
        "scopes": args.scopes,
        "responseType": "code",
        "pkceEnabled": args.pkce_enabled,
        # Claim mappings
        "userIdClaim": args.user_id_claim,
        "emailClaim": args.email_claim,
        "nameClaim": args.name_claim,
        "rolesClaim": args.roles_claim,
        # Metadata
        "createdAt": now,
        "updatedAt": now,
        "createdBy": "seed-script",
    }

    # Endpoints: prefer explicit args, then discovered, then omit
    endpoints = {
        "authorizationEndpoint": args.authorization_endpoint or discovered.get("authorization_endpoint"),
        "tokenEndpoint": args.token_endpoint or discovered.get("token_endpoint"),
        "jwksUri": args.jwks_uri or discovered.get("jwks_uri"),
        "userinfoEndpoint": args.userinfo_endpoint or discovered.get("userinfo_endpoint"),
        "endSessionEndpoint": args.end_session_endpoint or discovered.get("end_session_endpoint"),
    }
    for key, value in endpoints.items():
        if value:
            item[key] = value

    # Optional claim fields
    if args.picture_claim:
        item["pictureClaim"] = args.picture_claim
    if args.first_name_claim:
        item["firstNameClaim"] = args.first_name_claim
    if args.last_name_claim:
        item["lastNameClaim"] = args.last_name_claim

    # Optional validation
    if args.user_id_pattern:
        try:
            re.compile(args.user_id_pattern)
        except re.error as e:
            print(f"  ERROR: Invalid user_id_pattern regex: {e}")
            sys.exit(1)
        item["userIdPattern"] = args.user_id_pattern

    if args.allowed_audiences:
        item["allowedAudiences"] = args.allowed_audiences

    # Optional appearance
    if args.logo_url:
        item["logoUrl"] = args.logo_url
    if args.button_color:
        if not re.match(r"^#[0-9a-fA-F]{6}$", args.button_color):
            print(f"  ERROR: Invalid button_color '{args.button_color}'. Must be hex like #0078D4")
            sys.exit(1)
        item["buttonColor"] = args.button_color

    if args.redirect_uri:
        item["redirectUri"] = args.redirect_uri

    return item


def prompt_for_missing(args: argparse.Namespace) -> None:
    """Interactively prompt for required values not provided via CLI."""
    if not args.provider_id:
        args.provider_id = input("Provider ID (e.g., entra-id, okta-prod): ").strip()
        if not args.provider_id or not re.match(r"^[a-z0-9][a-z0-9-]*$", args.provider_id):
            print("ERROR: Provider ID must be lowercase letters, numbers, and hyphens.")
            sys.exit(1)

    if not args.display_name:
        args.display_name = input("Display Name (e.g., Microsoft Entra ID): ").strip()
        if not args.display_name:
            print("ERROR: Display name is required.")
            sys.exit(1)

    if not args.issuer_url:
        args.issuer_url = input("Issuer URL (e.g., https://login.microsoftonline.com/{tenant}/v2.0): ").strip()
        if not args.issuer_url:
            print("ERROR: Issuer URL is required.")
            sys.exit(1)

    if not args.client_id:
        args.client_id = input("Client ID: ").strip()
        if not args.client_id:
            print("ERROR: Client ID is required.")
            sys.exit(1)

    if not args.client_secret:
        args.client_secret = getpass.getpass("Client Secret: ")
        if not args.client_secret:
            print("ERROR: Client secret is required.")
            sys.exit(1)

    if not args.table_name:
        args.table_name = input("DynamoDB Table Name [auth-providers]: ").strip() or "auth-providers"

    if not args.secrets_arn:
        args.secrets_arn = input("Secrets Manager ARN: ").strip()
        if not args.secrets_arn:
            print("ERROR: Secrets Manager ARN is required.")
            sys.exit(1)

    # Ask about discovery if not explicitly set
    if args.discover is None:
        do_discover = input("Auto-discover OIDC endpoints? [Y/n]: ").strip().lower()
        args.discover = do_discover != "n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed an OIDC authentication provider for first-time platform setup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive setup
  python scripts/seed_auth_provider.py

  # Microsoft Entra ID
  python scripts/seed_auth_provider.py \\
      --provider-id entra-id \\
      --display-name "Microsoft Entra ID" \\
      --issuer-url "https://login.microsoftonline.com/TENANT_ID/v2.0" \\
      --client-id "CLIENT_ID" \\
      --client-secret "CLIENT_SECRET" \\
      --discover \\
      --button-color "#0078D4" \\
      --table-name auth-providers \\
      --secrets-arn "arn:aws:secretsmanager:..."

  # Okta
  python scripts/seed_auth_provider.py \\
      --provider-id okta \\
      --display-name "Okta" \\
      --issuer-url "https://dev-XXXXX.okta.com/oauth2/default" \\
      --client-id "CLIENT_ID" \\
      --client-secret "CLIENT_SECRET" \\
      --discover \\
      --roles-claim groups \\
      --table-name auth-providers \\
      --secrets-arn "arn:aws:secretsmanager:..."
        """,
    )

    # Required provider config
    provider_group = parser.add_argument_group("Provider Configuration")
    provider_group.add_argument("--provider-id", help="Unique slug (lowercase, hyphens)")
    provider_group.add_argument("--display-name", help="Human-readable name for login page")
    provider_group.add_argument("--issuer-url", help="OIDC issuer URL")
    provider_group.add_argument("--client-id", help="OAuth client ID")
    provider_group.add_argument("--client-secret", help="OAuth client secret (prompted if omitted)")
    provider_group.add_argument("--enabled", type=bool, default=True, help="Enable provider (default: true)")

    # AWS resources
    aws_group = parser.add_argument_group("AWS Resources")
    aws_group.add_argument("--table-name", default=os.getenv("DYNAMODB_AUTH_PROVIDERS_TABLE_NAME"), help="DynamoDB table name (or set DYNAMODB_AUTH_PROVIDERS_TABLE_NAME)")
    aws_group.add_argument("--secrets-arn", default=os.getenv("AUTH_PROVIDER_SECRETS_ARN"), help="Secrets Manager ARN (or set AUTH_PROVIDER_SECRETS_ARN)")
    aws_group.add_argument("--region", default=os.getenv("AWS_REGION", "us-west-2"), help="AWS region (default: us-west-2)")
    aws_group.add_argument("--profile", default=os.getenv("AWS_PROFILE"), help="AWS profile (optional)")

    # Discovery
    discovery_group = parser.add_argument_group("OIDC Discovery")
    discovery_group.add_argument("--discover", action="store_true", default=None, help="Auto-discover endpoints from .well-known/openid-configuration")
    discovery_group.add_argument("--no-discover", action="store_false", dest="discover", help="Skip auto-discovery")

    # Manual endpoint overrides
    endpoint_group = parser.add_argument_group("Endpoint Overrides (skip if using --discover)")
    endpoint_group.add_argument("--authorization-endpoint", help="OAuth authorization endpoint")
    endpoint_group.add_argument("--token-endpoint", help="OAuth token endpoint")
    endpoint_group.add_argument("--jwks-uri", help="JWKS signing keys endpoint")
    endpoint_group.add_argument("--userinfo-endpoint", help="UserInfo endpoint")
    endpoint_group.add_argument("--end-session-endpoint", help="Logout/end-session endpoint")

    # OAuth config
    oauth_group = parser.add_argument_group("OAuth Configuration")
    oauth_group.add_argument("--scopes", default="openid profile email", help="Space-separated scopes (default: openid profile email)")
    oauth_group.add_argument("--pkce-enabled", type=bool, default=True, help="Enable PKCE (default: true)")
    oauth_group.add_argument("--redirect-uri", help="Redirect URI override")

    # Claim mappings
    claims_group = parser.add_argument_group("JWT Claim Mappings")
    claims_group.add_argument("--user-id-claim", default="sub", help="Claim for user ID (default: sub)")
    claims_group.add_argument("--email-claim", default="email", help="Claim for email (default: email)")
    claims_group.add_argument("--name-claim", default="name", help="Claim for display name (default: name)")
    claims_group.add_argument("--roles-claim", default="roles", help="Claim for roles array (default: roles)")
    claims_group.add_argument("--picture-claim", default="picture", help="Claim for profile picture")
    claims_group.add_argument("--first-name-claim", default="given_name", help="Claim for first name")
    claims_group.add_argument("--last-name-claim", default="family_name", help="Claim for last name")

    # Validation
    validation_group = parser.add_argument_group("Validation Rules")
    validation_group.add_argument("--user-id-pattern", help="Regex to validate user IDs")
    validation_group.add_argument("--allowed-audiences", nargs="+", help="Allowed JWT audience values")

    # Appearance
    appearance_group = parser.add_argument_group("Login Page Appearance")
    appearance_group.add_argument("--logo-url", help="URL to provider logo")
    appearance_group.add_argument("--button-color", help="Hex color for login button (e.g., #0078D4)")

    # Execution control
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to AWS")
    parser.add_argument("--force", action="store_true", help="Overwrite existing provider without prompting")

    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  Auth Provider Seed Script")
    print("=" * 60)
    print()

    # Interactive prompts for missing required values
    prompt_for_missing(args)

    # --- Step 1: OIDC Discovery ---
    discovered = {}
    if args.discover:
        print()
        print("[1/4] OIDC Discovery")
        discovered = discover_oidc_endpoints(args.issuer_url)
        if discovered:
            print(f"  authorization_endpoint: {discovered.get('authorization_endpoint', 'not found')}")
            print(f"  token_endpoint:         {discovered.get('token_endpoint', 'not found')}")
            print(f"  jwks_uri:               {discovered.get('jwks_uri', 'not found')}")
            print(f"  userinfo_endpoint:      {discovered.get('userinfo_endpoint', 'not found')}")
            print(f"  end_session_endpoint:   {discovered.get('end_session_endpoint', 'not found')}")

            scopes_supported = discovered.get("scopes_supported", [])
            if scopes_supported:
                print(f"  scopes_supported:       {', '.join(scopes_supported[:10])}")
    else:
        print()
        print("[1/4] OIDC Discovery (skipped)")

    # --- Step 2: Build DynamoDB item ---
    print()
    print("[2/4] Building provider configuration")
    item = build_dynamo_item(args, discovered)

    # Display summary
    print()
    print("  Provider Configuration Summary:")
    print(f"    Provider ID:       {item['providerId']}")
    print(f"    Display Name:      {item['displayName']}")
    print(f"    Enabled:           {item['enabled']}")
    print(f"    Issuer URL:        {item['issuerUrl']}")
    print(f"    Client ID:         {item['clientId']}")
    print(f"    Client Secret:     {'*' * min(len(args.client_secret), 20)}")
    print(f"    Scopes:            {item['scopes']}")
    print(f"    PKCE:              {item['pkceEnabled']}")
    print(f"    User ID Claim:     {item['userIdClaim']}")
    print(f"    Email Claim:       {item['emailClaim']}")
    print(f"    Name Claim:        {item['nameClaim']}")
    print(f"    Roles Claim:       {item['rolesClaim']}")
    if item.get("authorizationEndpoint"):
        print(f"    Auth Endpoint:     {item['authorizationEndpoint']}")
    if item.get("tokenEndpoint"):
        print(f"    Token Endpoint:    {item['tokenEndpoint']}")
    if item.get("jwksUri"):
        print(f"    JWKS URI:          {item['jwksUri']}")
    if item.get("buttonColor"):
        print(f"    Button Color:      {item.get('buttonColor')}")
    print()
    print(f"    DynamoDB Table:    {args.table_name}")
    print(f"    Secrets ARN:       {args.secrets_arn}")
    print(f"    Region:            {args.region}")
    if args.profile:
        print(f"    AWS Profile:       {args.profile}")

    # --- Dry run exit ---
    if args.dry_run:
        print()
        print("[DRY RUN] No changes written. Remove --dry-run to execute.")
        print()
        print("DynamoDB item that would be written:")
        # Remove the secret from display
        display_item = {k: v for k, v in item.items()}
        print(json.dumps(display_item, indent=2, default=str))
        sys.exit(0)

    # --- Step 3: Write to AWS ---
    print()
    print("[3/4] Writing to AWS")

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("  ERROR: boto3 is required. Install with: uv sync")
        sys.exit(1)

    # Create AWS session
    if args.profile:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
    else:
        session = boto3.Session(region_name=args.region)

    dynamodb = session.resource("dynamodb")
    secrets_client = session.client("secretsmanager")
    table = dynamodb.Table(args.table_name)

    # Check for existing provider
    try:
        existing = table.get_item(
            Key={"PK": f"AUTH_PROVIDER#{args.provider_id}", "SK": f"AUTH_PROVIDER#{args.provider_id}"}
        )
        if "Item" in existing:
            if not args.force:
                overwrite = input(f"\n  Provider '{args.provider_id}' already exists. Overwrite? [y/N]: ").strip().lower()
                if overwrite != "y":
                    print("  Aborted.")
                    sys.exit(0)
            print(f"  Overwriting existing provider: {args.provider_id}")
    except ClientError as e:
        print(f"  ERROR connecting to DynamoDB: {e}")
        sys.exit(1)

    # Write client secret to Secrets Manager
    print("  Writing client secret to Secrets Manager...")
    try:
        try:
            response = secrets_client.get_secret_value(SecretId=args.secrets_arn)
            secrets = json.loads(response["SecretString"])
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                secrets = {}
            else:
                raise
        except (json.JSONDecodeError, KeyError):
            # Secret exists but is empty or not valid JSON (e.g., newly created by CDK)
            secrets = {}

        secrets[args.provider_id] = args.client_secret
        secrets_client.put_secret_value(
            SecretId=args.secrets_arn,
            SecretString=json.dumps(secrets),
        )
        print("  Client secret stored successfully")
    except ClientError as e:
        print(f"  ERROR writing to Secrets Manager: {e}")
        sys.exit(1)

    # Write provider config to DynamoDB
    print("  Writing provider config to DynamoDB...")
    try:
        table.put_item(Item=item)
        print("  Provider config stored successfully")
    except ClientError as e:
        print(f"  ERROR writing to DynamoDB: {e}")
        sys.exit(1)

    # --- Step 4: Verification ---
    print()
    print("[4/4] Verification")

    try:
        verify = table.get_item(
            Key={"PK": f"AUTH_PROVIDER#{args.provider_id}", "SK": f"AUTH_PROVIDER#{args.provider_id}"}
        )
        if "Item" in verify:
            print(f"  DynamoDB:        Provider '{args.provider_id}' exists")
        else:
            print(f"  DynamoDB:        WARNING - Provider not found after write!")

        secret_verify = secrets_client.get_secret_value(SecretId=args.secrets_arn)
        secret_data = json.loads(secret_verify["SecretString"])
        if args.provider_id in secret_data:
            print(f"  Secrets Manager: Secret for '{args.provider_id}' exists")
        else:
            print(f"  Secrets Manager: WARNING - Secret not found after write!")

    except Exception as e:
        print(f"  Verification error (non-fatal): {e}")

    print()
    print("=" * 60)
    print("  Setup Complete!")
    print("=" * 60)
    print()
    print("  Next steps:")
    print(f"  1. Ensure DYNAMODB_AUTH_PROVIDERS_TABLE_NAME={args.table_name}")
    print(f"     and AUTH_PROVIDER_SECRETS_ARN={args.secrets_arn}")
    print(f"     are set in your backend environment.")
    print(f"  2. Run the bootstrap seed script with SEED_ADMIN_JWT_ROLE set to")
    print(f"     a JWT role your IdP issues (e.g., SEED_ADMIN_JWT_ROLE='Admin').")
    print(f"  3. Restart the backend service.")
    print(f"  4. Log in through the new provider and verify admin access.")
    print()


if __name__ == "__main__":
    main()

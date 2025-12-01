#!/bin/bash

# Refresh AWS SSO login for dev-ai profile
# This script authenticates with AWS SSO using the dev-ai profile

set -e  # Exit on error

PROFILE="${AWS_PROFILE:-dev-ai}"

echo "🔐 Refreshing AWS SSO login for profile: $PROFILE"

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "❌ Error: AWS CLI is not installed"
    echo "   Please install AWS CLI: https://aws.amazon.com/cli/"
    exit 1
fi

# Attempt to login with SSO
if aws sso login --profile "$PROFILE"; then
    echo "✅ Successfully authenticated with AWS SSO"
    echo ""
    echo "📝 Setting AWS_PROFILE environment variable to: $PROFILE"
    export AWS_PROFILE="$PROFILE"
    echo ""
    echo "You can now run Python services that require AWS credentials."
    echo ""
    echo "To use this profile in your current shell session, run:"
    echo "  export AWS_PROFILE=$PROFILE"
else
    echo "❌ Failed to authenticate with AWS SSO"
    echo ""
    echo "Troubleshooting:"
    echo "  1. Ensure your SSO profile is configured: aws configure sso --profile $PROFILE"
    echo "  2. Check that your SSO session hasn't expired"
    echo "  3. Verify you have access to the AWS account"
    exit 1
fi


# AWS Profile Configuration Guide

This guide explains how to configure AWS credentials for the AgentCore Public Stack using AWS profiles.

## Overview

The application supports multiple ways to provide AWS credentials with automatic fallback:

1. **AWS Profile** (recommended for local development)
2. **Environment Variables**
3. **Default AWS Credentials**

## AWS Profile Priority Order

The scripts check for AWS credentials in this order:

1. `AWS_PROFILE` **environment variable** (highest priority)
2. `AWS_PROFILE` in **backend/src/.env** file
3. **"default"** profile fallback
4. AWS SDK default credential chain (env vars, credentials file, IAM role)

## Setup Methods

### Method 1: Using AWS Profiles (Recommended)

#### Step 1: Configure AWS CLI Profiles

```bash
# Configure your default profile
aws configure
# Enter: Access Key ID, Secret Access Key, Region, Output format

# Or configure a named profile
aws configure --profile dev
aws configure --profile production
```

This creates/updates `~/.aws/credentials`:

```ini
[default]
aws_access_key_id = AKIA...
aws_secret_access_key = ...

[dev]
aws_access_key_id = AKIA...
aws_secret_access_key = ...

[production]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```

#### Step 2: Configure in .env File

Edit `backend/src/.env`:

```bash
AWS_REGION=us-west-2
AWS_PROFILE=dev  # Use the 'dev' profile
```

#### Step 3: Run Setup and Start

```bash
./setup.sh   # Validates AWS profile during setup
./start.sh   # Uses configured profile
```

### Method 2: Override with Environment Variable

You can override the `.env` file setting:

```bash
# Use a specific profile for this session
AWS_PROFILE=production ./setup.sh
AWS_PROFILE=production ./start.sh

# Or export it for multiple commands
export AWS_PROFILE=production
./setup.sh
./start.sh
```

### Method 3: Use Default Credentials

Set `AWS_PROFILE=default` or omit it entirely:

```bash
# In backend/src/.env
AWS_PROFILE=default

# Or don't set AWS_PROFILE at all - will use default
```

This falls back to the AWS SDK credential chain:
1. `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` environment variables
2. `~/.aws/credentials` default profile
3. IAM role (when running on AWS EC2/ECS/Lambda)

## What the Scripts Do

### setup.sh

During setup, the script:

1. ✅ Checks if AWS CLI is installed
2. ✅ Reads `AWS_PROFILE` from environment or `.env` file
3. ✅ Validates the profile exists using `aws configure list`
4. ✅ Warns if profile is missing (but continues)
5. ✅ Displays which profile/credentials will be used

Example output:

```
🔍 Checking AWS configuration...
✅ AWS CLI found
Using AWS profile: dev
✅ AWS profile 'dev' is configured
```

### start.sh

When starting services, the script:

1. ✅ Loads environment variables from `.env`
2. ✅ Configures AWS profile (env var > .env > default)
3. ✅ Validates credentials with `aws sts get-caller-identity`
4. ✅ Displays AWS account ID if credentials are valid
5. ✅ Exports `AWS_PROFILE` for child processes (APIs)

Example output:

```
🔍 Configuring AWS credentials...
Using AWS profile: dev
✅ AWS profile 'dev' is valid
✅ AWS credentials valid (Account: 123456789012)
```

## Common Scenarios

### Scenario 1: Multiple AWS Accounts (Work/Personal)

```bash
# Configure profiles
aws configure --profile work
aws configure --profile personal

# Use in .env
AWS_PROFILE=work

# Or switch on the fly
AWS_PROFILE=personal ./start.sh
```

### Scenario 2: Team Development (Shared Profile Names)

Your team agrees on profile names:

```bash
# Everyone configures the same profile names
aws configure --profile agentcore-dev
aws configure --profile agentcore-staging
aws configure --profile agentcore-prod

# In .env (committed to repo)
AWS_PROFILE=agentcore-dev

# Each developer has their own credentials under the same profile name
```

### Scenario 3: SSO (AWS IAM Identity Center)

```bash
# Configure SSO profile
aws configure sso --profile my-sso-profile

# Login before using
aws sso login --profile my-sso-profile

# Use in .env
AWS_PROFILE=my-sso-profile

# The scripts will use SSO credentials
./start.sh
```

### Scenario 4: CI/CD (No Profile)

In GitHub Actions, AWS CodeBuild, etc.:

```bash
# Don't set AWS_PROFILE (use IAM role or environment variables)
# The AWS SDK will automatically use:
# - GitHub OIDC credentials
# - CodeBuild service role
# - ECS task role
# - etc.

# In .env or leave unset
AWS_PROFILE=default
# Or omit the line entirely
```

### Scenario 5: Docker/Container Development

```bash
# Mount AWS credentials into container
docker run -v ~/.aws:/root/.aws:ro \
  -e AWS_PROFILE=dev \
  myapp

# Or use environment variables
docker run \
  -e AWS_ACCESS_KEY_ID=... \
  -e AWS_SECRET_ACCESS_KEY=... \
  -e AWS_REGION=us-west-2 \
  myapp
```

## Troubleshooting

### Profile Not Found

```
⚠️  AWS profile 'dev' not found, will use default credentials
```

**Solution:**
```bash
# List configured profiles
aws configure list-profiles

# Configure the missing profile
aws configure --profile dev
```

### Could Not Verify Credentials

```
⚠️  Could not verify AWS credentials
   Some features may not work. Run 'aws configure' to set up.
```

**Solutions:**

1. **Check profile exists:**
   ```bash
   aws configure list --profile your-profile
   ```

2. **Test credentials manually:**
   ```bash
   aws sts get-caller-identity --profile your-profile
   ```

3. **For SSO, login first:**
   ```bash
   aws sso login --profile your-sso-profile
   ```

4. **Check ~/.aws/credentials file:**
   ```bash
   cat ~/.aws/credentials
   ```

### AWS CLI Not Installed

```
⚠️  AWS CLI not found - some features may require AWS credentials
   Install from: https://aws.amazon.com/cli/
```

**Solution:**

Install AWS CLI:
- **macOS:** `brew install awscli`
- **Linux:** `pip install awscli` or download from AWS
- **Windows:** Download MSI installer from AWS

### SSO Session Expired

```
Error loading SSO Token: Token for ... does not exist
```

**Solution:**
```bash
aws sso login --profile your-sso-profile
./start.sh
```

## Security Best Practices

1. **Never commit credentials to git**
   - ✅ `.env` is in `.gitignore`
   - ✅ Use `.env.example` as template
   - ❌ Don't put real credentials in `.env.example`

2. **Use IAM roles when possible**
   - For EC2/ECS/Lambda: Use instance/task roles
   - For local development: Use profiles

3. **Rotate credentials regularly**
   ```bash
   aws iam create-access-key
   aws configure --profile your-profile
   # Update credentials, then:
   aws iam delete-access-key --access-key-id OLD_KEY
   ```

4. **Use least-privilege IAM policies**
   - Only grant permissions needed for AgentCore/Bedrock
   - Example services: Bedrock, S3, DynamoDB, CloudWatch

5. **Enable MFA for production profiles**
   ```bash
   # Add MFA device in IAM console
   # Use temporary credentials with MFA
   aws sts get-session-token --serial-number arn:aws:iam::123456789012:mfa/user --token-code 123456
   ```

## Environment Variables Reference

| Variable | Source | Priority | Default |
|----------|--------|----------|---------|
| `AWS_PROFILE` | CLI export | 1 (highest) | - |
| `AWS_PROFILE` | .env file | 2 | `default` |
| `AWS_ACCESS_KEY_ID` | Environment | 3 | - |
| `AWS_SECRET_ACCESS_KEY` | Environment | 3 | - |
| `AWS_SESSION_TOKEN` | Environment | 3 | - |
| `AWS_REGION` | .env file | - | `us-west-2` |

## Quick Reference Commands

```bash
# List all configured profiles
aws configure list-profiles

# View current profile configuration
aws configure list

# Test credentials
aws sts get-caller-identity

# See which profile is active
echo $AWS_PROFILE

# Use specific profile for one command
AWS_PROFILE=dev aws s3 ls

# Set profile for session
export AWS_PROFILE=dev

# Unset profile (use default)
unset AWS_PROFILE

# SSO login
aws sso login --profile my-sso-profile

# View credentials file
cat ~/.aws/credentials

# View config file
cat ~/.aws/config
```

## Additional Resources

- [AWS CLI Configuration Guide](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html)
- [Named Profiles](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-profiles.html)
- [AWS IAM Identity Center (SSO)](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html)
- [Environment Variables](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html)
- [Credential Provider Chain](https://docs.aws.amazon.com/sdkref/latest/guide/standardized-credentials.html)

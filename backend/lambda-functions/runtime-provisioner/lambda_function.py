"""
Runtime Provisioner Lambda for AgentCore Multi-Runtime Architecture

Automatically provisions, updates, and deletes AgentCore Runtimes based on
DynamoDB Stream events from the Auth Providers table.

Event Flow:
- INSERT: Create new runtime with provider's JWT config
- MODIFY: Update runtime if JWT-relevant fields changed
- REMOVE: Delete runtime and clean up SSM parameters
"""
import json
import os
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
import sys

# Install latest boto3 at runtime to get newest API support
from pip._internal import main
main(['install', '-I', '-q', 'boto3', '--target', '/tmp/', '--no-cache-dir', '--disable-pip-version-check'])
sys.path.insert(0, '/tmp/')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import boto3
from botocore.exceptions import ClientError

# AWS clients
dynamodb = boto3.client('dynamodb')
ssm = boto3.client('ssm')
ecr = boto3.client('ecr')
bedrock_agentcore = boto3.client('bedrock-agentcore-control')

# Environment variables
PROJECT_PREFIX = os.environ['PROJECT_PREFIX']
AWS_REGION = os.environ['AWS_REGION']
AUTH_PROVIDERS_TABLE = os.environ['AUTH_PROVIDERS_TABLE']


def lambda_handler(event, context):
    """
    Lambda handler for DynamoDB Stream events from Auth Providers table
    
    Processes INSERT, MODIFY, and REMOVE events to manage AgentCore Runtimes
    """
    try:
        logger.info(f"Event: {json.dumps(event)}")
        
        # Process each record in the stream
        for record in event.get('Records', []):
            event_name = record['eventName']
            
            logger.info(f"Processing {event_name} event")
            
            if event_name == 'INSERT':
                handle_insert(record)
            elif event_name == 'MODIFY':
                handle_modify(record)
            elif event_name == 'REMOVE':
                handle_remove(record)
            else:
                logger.warning(f"Unknown event type: {event_name}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'Successfully processed stream events'})
        }
        
    except Exception as e:
        logger.error(f"Error processing stream events: {str(e)}", exc_info=True)
        # Re-raise to trigger Lambda retry
        raise


def handle_insert(record: Dict[str, Any]) -> None:
    """
    Handle INSERT event - create new AgentCore Runtime
    
    Args:
        record: DynamoDB Stream record with NewImage
    """
    try:
        # Extract provider details from NewImage
        new_image = record['dynamodb']['NewImage']
        provider_id = deserialize_dynamodb_value(new_image['providerId'])
        
        logger.info(f"Creating runtime for provider: {provider_id}")
        
        # Parse provider configuration
        provider_config = parse_provider_from_stream(new_image)
        
        # Create runtime
        runtime_info = create_runtime(provider_id, provider_config)
        
        # Update DynamoDB with runtime details
        update_provider_runtime_info(
            provider_id=provider_id,
            runtime_arn=runtime_info['runtime_arn'],
            runtime_id=runtime_info['runtime_id'],
            endpoint_url=runtime_info['endpoint_url'],
            status='READY'
        )
        
        # Store runtime ARN in SSM for cross-stack reference
        store_runtime_arn_in_ssm(provider_id, runtime_info['runtime_arn'])
        
        logger.info(f"✅ Successfully created runtime for provider {provider_id}")
        
    except Exception as e:
        logger.error(f"Failed to create runtime: {str(e)}", exc_info=True)
        
        # Update DynamoDB with error status
        provider_id = deserialize_dynamodb_value(record['dynamodb']['NewImage']['providerId'])
        update_provider_runtime_error(provider_id, str(e))
        
        # Don't re-raise - let DynamoDB Streams retry logic handle it


def handle_modify(record: Dict[str, Any]) -> None:
    """
    Handle MODIFY event - update runtime if JWT config changed
    
    Args:
        record: DynamoDB Stream record with OldImage and NewImage
    """
    try:
        old_image = record['dynamodb'].get('OldImage', {})
        new_image = record['dynamodb']['NewImage']
        
        provider_id = deserialize_dynamodb_value(new_image['providerId'])
        
        logger.info(f"Checking if runtime update needed for provider: {provider_id}")
        
        # Check if JWT-relevant fields changed
        jwt_fields = ['issuerUrl', 'clientId', 'jwksUri']
        jwt_changed = any(
            deserialize_dynamodb_value(old_image.get(field, {})) != 
            deserialize_dynamodb_value(new_image.get(field, {}))
            for field in jwt_fields
        )
        
        if not jwt_changed:
            logger.info(f"No JWT config changes for {provider_id}, skipping update")
            return
        
        logger.info(f"JWT config changed for {provider_id}, updating runtime")
        
        # Get runtime ID from DynamoDB
        runtime_id = deserialize_dynamodb_value(new_image.get('agentcoreRuntimeId', {}))
        
        if not runtime_id:
            logger.warning(f"No runtime ID found for {provider_id}, cannot update")
            return
        
        # Parse new provider configuration
        provider_config = parse_provider_from_stream(new_image)
        
        # Update runtime
        update_runtime(runtime_id, provider_config)
        
        # Update DynamoDB status
        update_provider_runtime_status(provider_id, 'READY')
        
        logger.info(f"✅ Successfully updated runtime for provider {provider_id}")
        
    except Exception as e:
        logger.error(f"Failed to update runtime: {str(e)}", exc_info=True)
        
        # Update DynamoDB with error status
        provider_id = deserialize_dynamodb_value(record['dynamodb']['NewImage']['providerId'])
        update_provider_runtime_error(provider_id, str(e), status='UPDATE_FAILED')


def handle_remove(record: Dict[str, Any]) -> None:
    """
    Handle REMOVE event - delete runtime and clean up SSM
    
    Args:
        record: DynamoDB Stream record with OldImage
    """
    try:
        old_image = record['dynamodb']['OldImage']
        provider_id = deserialize_dynamodb_value(old_image['providerId'])
        runtime_id = deserialize_dynamodb_value(old_image.get('agentcoreRuntimeId', {}))
        
        logger.info(f"Deleting runtime for provider: {provider_id}")
        
        if not runtime_id:
            logger.warning(f"No runtime ID found for {provider_id}, nothing to delete")
            return
        
        # Delete runtime
        delete_runtime(runtime_id)
        
        # Clean up SSM parameter
        delete_runtime_arn_from_ssm(provider_id)
        
        logger.info(f"✅ Successfully deleted runtime for provider {provider_id}")
        
    except Exception as e:
        logger.error(f"Failed to delete runtime: {str(e)}", exc_info=True)
        # Don't re-raise - provider is already deleted from DynamoDB


def create_runtime(provider_id: str, provider_config: Dict[str, Any]) -> Dict[str, str]:
    """
    Create new AgentCore Runtime with provider's JWT configuration
    
    Args:
        provider_id: Unique provider identifier
        provider_config: Provider configuration from DynamoDB
        
    Returns:
        Dict with runtime_arn, runtime_id, endpoint_url
    """
    # Fetch container image tag from SSM
    image_tag = get_container_image_tag()
    
    # Construct runtime name (replace ALL hyphens with underscores for AWS validation)
    # Max length is 48 characters: [a-zA-Z][a-zA-Z0-9_]{0,47}
    safe_prefix = PROJECT_PREFIX.replace('-', '_')
    safe_provider_id = provider_id.replace('-', '_')
    base_name = f"{safe_prefix}_runtime_{safe_provider_id}"
    
    # Truncate if necessary to fit within 48 character limit
    if len(base_name) > 48:
        # Keep the provider_id recognizable by truncating the prefix
        max_provider_id_length = 48 - len("_runtime_") - 1  # -1 for first character
        truncated_provider_id = safe_provider_id[:max_provider_id_length]
        runtime_name = f"r_{truncated_provider_id}"  # 'r_' prefix to ensure it starts with letter
        # Ensure we're still under 48 chars
        runtime_name = runtime_name[:48]
    else:
        runtime_name = base_name
    
    logger.info(f"Runtime name: {runtime_name} (length: {len(runtime_name)})")
    
    # Get container image URI from ECR
    image_uri = get_container_image_uri(image_tag)
    
    # Determine discovery URL from issuer URL or JWKS URI
    discovery_url = determine_discovery_url(
        provider_config['issuer_url'],
        provider_config.get('jwks_uri')
    )
    
    # Fetch runtime execution role ARN from SSM
    execution_role_arn = get_runtime_execution_role_arn()
    
    # Fetch shared resource IDs from SSM
    shared_resources = get_shared_resource_ids()
    
    # Fetch all required environment variables from SSM
    runtime_env_vars = get_runtime_environment_variables(provider_id, shared_resources)
    
    logger.info(f"Creating runtime: {runtime_name}")
    logger.info(f"Discovery URL: {discovery_url}")
    logger.info(f"Client ID: {provider_config['client_id']}")
    
    # Log boto3 version for debugging
    import boto3
    logger.info(f"Boto3 version: {boto3.__version__}")
    
    # Call CreateAgentRuntime API
    response = bedrock_agentcore.create_agent_runtime(
        agentRuntimeName=runtime_name,
        agentRuntimeArtifact={
            'containerConfiguration': {
                'containerUri': image_uri
            }
        },
        authorizerConfiguration={
            'customJWTAuthorizer': {
                'discoveryUrl': discovery_url,
                'allowedAudience': [provider_config['client_id']]
            }
        },
        requestHeaderConfiguration={
            'requestHeaderAllowlist': ['Authorization']
        },
        roleArn=execution_role_arn,
        networkConfiguration={
            'networkMode': 'PUBLIC'
        },
        environmentVariables=runtime_env_vars
    )
    
    runtime_arn = response['agentRuntimeArn']
    runtime_id = response['agentRuntimeId']
    
    # Construct endpoint URL with properly encoded runtime ARN
    # The runtime ARN contains colons and slashes that must be URL-encoded
    from urllib.parse import quote
    encoded_runtime_arn = quote(runtime_arn, safe='')
    endpoint_url = f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{encoded_runtime_arn}/invocations"
    
    logger.info(f"Runtime created: {runtime_arn}")
    logger.info(f"Endpoint URL: {endpoint_url}")
    
    return {
        'runtime_arn': runtime_arn,
        'runtime_id': runtime_id,
        'endpoint_url': endpoint_url
    }


def update_runtime(runtime_id: str, provider_config: Dict[str, Any]) -> None:
    """
    Update existing AgentCore Runtime with new JWT configuration
    
    Args:
        runtime_id: Runtime ID to update
        provider_config: New provider configuration
    """
    # Determine discovery URL
    discovery_url = determine_discovery_url(
        provider_config['issuer_url'],
        provider_config.get('jwks_uri')
    )
    
    logger.info(f"Updating runtime {runtime_id}")
    logger.info(f"New discovery URL: {discovery_url}")
    
    # Fetch current runtime configuration to preserve settings
    current_runtime = bedrock_agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
    
    # Get current container image and other required fields
    current_artifact = current_runtime['agentRuntimeArtifact']
    current_network_config = current_runtime['networkConfiguration']
    current_role_arn = current_runtime['roleArn']
    
    # Update runtime with new JWT config (preserve other settings)
    bedrock_agentcore.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact=current_artifact,
        authorizerConfiguration={
            'customJWTAuthorizer': {
                'discoveryUrl': discovery_url,
                'allowedAudience': [provider_config['client_id']]
            }
        },
        requestHeaderConfiguration={
            'requestHeaderAllowlist': ['Authorization']
        },
        networkConfiguration=current_network_config,
        roleArn=current_role_arn
    )
    
    logger.info(f"Runtime {runtime_id} updated successfully")


def delete_runtime(runtime_id: str) -> None:
    """
    Delete AgentCore Runtime
    
    Args:
        runtime_id: Runtime ID to delete
    """
    logger.info(f"Deleting runtime {runtime_id}")
    
    try:
        bedrock_agentcore.delete_agent_runtime(agentRuntimeId=runtime_id)
        logger.info(f"Runtime {runtime_id} deleted successfully")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            logger.warning(f"Runtime {runtime_id} not found, already deleted")
        else:
            raise


# =============================================================================
# Helper Functions
# =============================================================================


def get_optional_parameter(parameter_name: str) -> Optional[str]:
    """
    Fetch an optional SSM parameter, returning None if it doesn't exist.
    
    Args:
        parameter_name: Full SSM parameter path
        
    Returns:
        Parameter value if it exists, None otherwise
    """
    try:
        response = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )
        return response['Parameter']['Value']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.info(f"Optional parameter {parameter_name} not found, skipping")
            return None
        else:
            logger.error(f"Error fetching optional parameter {parameter_name}: {e}")
            raise


def get_required_parameter(parameter_name: str) -> str:
    """
    Fetch a required SSM parameter, raising an exception if it doesn't exist.
    
    Args:
        parameter_name: Full SSM parameter path
        
    Returns:
        Parameter value
        
    Raises:
        ClientError: If the required parameter doesn't exist or other SSM errors
    """
    try:
        response = ssm.get_parameter(
            Name=parameter_name,
            WithDecryption=True
        )
        return response['Parameter']['Value']
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.error(f"Required parameter {parameter_name} not found")
            raise
        else:
            logger.error(f"Error fetching required parameter {parameter_name}: {e}")
            raise


def normalize_url(url: str) -> str:
    """
    Normalize a URL by ensuring it has a protocol.
    
    If the URL doesn't start with http:// or https://, prepends https://.
    
    Args:
        url: URL or domain name
        
    Returns:
        Normalized URL with protocol
    """
    url = url.strip()
    if not url:
        return url
    
    # If already has protocol, return as-is
    if url.startswith(('http://', 'https://')):
        return url
    
    # Auto-prepend https:// for domain names
    return f"https://{url}"


def validate_url(url: str, parameter_name: str) -> str:
    """
    Validate and normalize a URL parameter.
    
    Args:
        url: URL string to validate
        parameter_name: Parameter name for error messages
        
    Returns:
        Normalized URL with protocol
        
    Raises:
        ValueError: If URL is empty
    """
    if not url or not url.strip():
        raise ValueError(f"Empty URL value for {parameter_name}")
    
    # Normalize the URL (add https:// if missing)
    normalized = normalize_url(url)
    
    return normalized


def parse_provider_from_stream(image: Dict[str, Any]) -> Dict[str, Any]:
    """Parse provider configuration from DynamoDB Stream image"""
    return {
        'issuer_url': deserialize_dynamodb_value(image['issuerUrl']),
        'client_id': deserialize_dynamodb_value(image['clientId']),
        'jwks_uri': deserialize_dynamodb_value(image.get('jwksUri', {}))
    }


def deserialize_dynamodb_value(value: Dict[str, Any]) -> Any:
    """Deserialize DynamoDB attribute value"""
    if not value:
        return None
    
    if 'S' in value:
        return value['S']
    elif 'N' in value:
        return value['N']
    elif 'BOOL' in value:
        return value['BOOL']
    elif 'NULL' in value:
        return None
    elif 'L' in value:
        return [deserialize_dynamodb_value(item) for item in value['L']]
    elif 'M' in value:
        return {k: deserialize_dynamodb_value(v) for k, v in value['M'].items()}
    else:
        return None


def determine_discovery_url(issuer_url: str, jwks_uri: Optional[str]) -> str:
    """
    Determine OIDC discovery URL from issuer URL or JWKS URI
    
    Args:
        issuer_url: OIDC issuer URL
        jwks_uri: Optional JWKS URI
        
    Returns:
        Discovery URL for JWT validation
    """
    # If JWKS URI is provided, use issuer URL for discovery
    # AgentCore will fetch JWKS from the discovery document
    if issuer_url.endswith('/'):
        issuer_url = issuer_url.rstrip('/')
    
    # Standard OIDC discovery endpoint
    return f"{issuer_url}/.well-known/openid-configuration"


def get_container_image_tag() -> str:
    """Fetch current container image tag from SSM"""
    param_name = f"/{PROJECT_PREFIX}/inference-api/image-tag"
    
    try:
        response = ssm.get_parameter(Name=param_name)
        return response['Parameter']['Value']
    except ClientError as e:
        logger.error(f"Failed to get image tag from SSM: {e}")
        raise ValueError(f"Image tag not found in SSM: {param_name}")


def get_container_image_uri(image_tag: str) -> str:
    """
    Get full container image URI from ECR
    
    Args:
        image_tag: Image tag (e.g., 'latest', 'v1.0.0')
        
    Returns:
        Full ECR image URI
    """
    # Get ECR repository URI from SSM
    repo_param = f"/{PROJECT_PREFIX}/inference-api/ecr-repository-uri"
    
    try:
        response = ssm.get_parameter(Name=repo_param)
        repo_uri = response['Parameter']['Value']
        return f"{repo_uri}:{image_tag}"
    except ClientError as e:
        logger.error(f"Failed to get ECR repository URI: {e}")
        raise ValueError(f"ECR repository URI not found in SSM: {repo_param}")


def get_runtime_execution_role_arn() -> str:
    """Fetch runtime execution role ARN from SSM"""
    param_name = f"/{PROJECT_PREFIX}/inference-api/runtime-execution-role-arn"
    
    try:
        response = ssm.get_parameter(Name=param_name)
        return response['Parameter']['Value']
    except ClientError as e:
        logger.error(f"Failed to get execution role ARN: {e}")
        raise ValueError(f"Execution role ARN not found in SSM: {param_name}")


def store_runtime_arn_in_ssm(provider_id: str, runtime_arn: str) -> None:
    """Store runtime ARN in SSM for cross-stack reference"""
    param_name = f"/{PROJECT_PREFIX}/runtimes/{provider_id}/arn"
    
    try:
        ssm.put_parameter(
            Name=param_name,
            Value=runtime_arn,
            Type='String',
            Description=f"AgentCore Runtime ARN for provider {provider_id}",
            Overwrite=True
        )
        logger.info(f"Stored runtime ARN in SSM: {param_name}")
    except ClientError as e:
        logger.error(f"Failed to store runtime ARN in SSM: {e}")
        # Don't raise - this is not critical


def delete_runtime_arn_from_ssm(provider_id: str) -> None:
    """Delete runtime ARN from SSM"""
    param_name = f"/{PROJECT_PREFIX}/runtimes/{provider_id}/arn"
    
    try:
        ssm.delete_parameter(Name=param_name)
        logger.info(f"Deleted runtime ARN from SSM: {param_name}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ParameterNotFound':
            logger.warning(f"SSM parameter not found: {param_name}")
        else:
            logger.error(f"Failed to delete runtime ARN from SSM: {e}")


def update_provider_runtime_info(
    provider_id: str,
    runtime_arn: str,
    runtime_id: str,
    endpoint_url: str,
    status: str
) -> None:
    """Update provider record in DynamoDB with runtime information"""
    try:
        dynamodb.update_item(
            TableName=AUTH_PROVIDERS_TABLE,
            Key={
                'PK': {'S': f"AUTH_PROVIDER#{provider_id}"},
                'SK': {'S': f"AUTH_PROVIDER#{provider_id}"}
            },
            UpdateExpression='SET agentcoreRuntimeArn = :arn, agentcoreRuntimeId = :id, '
                           'agentcoreRuntimeEndpointUrl = :url, agentcoreRuntimeStatus = :status, '
                           'updatedAt = :updated',
            ExpressionAttributeValues={
                ':arn': {'S': runtime_arn},
                ':id': {'S': runtime_id},
                ':url': {'S': endpoint_url},
                ':status': {'S': status},
                ':updated': {'S': datetime.utcnow().isoformat() + 'Z'}
            }
        )
        logger.info(f"Updated provider {provider_id} with runtime info")
    except ClientError as e:
        logger.error(f"Failed to update provider runtime info: {e}")
        raise


def update_provider_runtime_status(provider_id: str, status: str) -> None:
    """Update provider runtime status in DynamoDB"""
    try:
        dynamodb.update_item(
            TableName=AUTH_PROVIDERS_TABLE,
            Key={
                'PK': {'S': f"AUTH_PROVIDER#{provider_id}"},
                'SK': {'S': f"AUTH_PROVIDER#{provider_id}"}
            },
            UpdateExpression='SET agentcoreRuntimeStatus = :status, updatedAt = :updated',
            ExpressionAttributeValues={
                ':status': {'S': status},
                ':updated': {'S': datetime.utcnow().isoformat() + 'Z'}
            }
        )
        logger.info(f"Updated provider {provider_id} status to {status}")
    except ClientError as e:
        logger.error(f"Failed to update provider status: {e}")


def update_provider_runtime_error(
    provider_id: str,
    error_message: str,
    status: str = 'FAILED'
) -> None:
    """Update provider record with error status and message"""
    try:
        dynamodb.update_item(
            TableName=AUTH_PROVIDERS_TABLE,
            Key={
                'PK': {'S': f"AUTH_PROVIDER#{provider_id}"},
                'SK': {'S': f"AUTH_PROVIDER#{provider_id}"}
            },
            UpdateExpression='SET agentcoreRuntimeStatus = :status, '
                           'agentcoreRuntimeError = :error, updatedAt = :updated',
            ExpressionAttributeValues={
                ':status': {'S': status},
                ':error': {'S': error_message[:1000]},  # Limit error message length
                ':updated': {'S': datetime.utcnow().isoformat() + 'Z'}
            }
        )
        logger.info(f"Updated provider {provider_id} with error status")
    except ClientError as e:
        logger.error(f"Failed to update provider error status: {e}")


def get_shared_resource_ids() -> Dict[str, str]:
    """
    Fetch shared AgentCore resource IDs from SSM
    
    Returns:
        Dict with memory_arn, memory_id, code_interpreter_id, browser_id, gateway_url
    """
    try:
        # Fetch all required SSM parameters in batch
        param_names = [
            f"/{PROJECT_PREFIX}/inference-api/memory-arn",
            f"/{PROJECT_PREFIX}/inference-api/memory-id",
            f"/{PROJECT_PREFIX}/inference-api/code-interpreter-id",
            f"/{PROJECT_PREFIX}/inference-api/browser-id",
            f"/{PROJECT_PREFIX}/gateway/gateway-url",
        ]
        
        response = ssm.get_parameters(Names=param_names, WithDecryption=False)
        
        # Build result dictionary
        params = {p['Name']: p['Value'] for p in response['Parameters']}
        
        return {
            'memory_arn': params.get(f"/{PROJECT_PREFIX}/inference-api/memory-arn", ''),
            'memory_id': params.get(f"/{PROJECT_PREFIX}/inference-api/memory-id", ''),
            'code_interpreter_id': params.get(f"/{PROJECT_PREFIX}/inference-api/code-interpreter-id", ''),
            'browser_id': params.get(f"/{PROJECT_PREFIX}/inference-api/browser-id", ''),
            'gateway_url': params.get(f"/{PROJECT_PREFIX}/gateway/gateway-url", ''),
        }
    except ClientError as e:
        logger.error(f"Failed to fetch shared resource IDs from SSM: {e}")
        raise ValueError(f"Could not fetch shared resource IDs: {e}")


def get_runtime_environment_variables(provider_id: str, shared_resources: Dict[str, str]) -> Dict[str, str]:
    """
    Construct complete environment variable dictionary for runtime
    
    Args:
        provider_id: Provider ID for this runtime
        shared_resources: Dict with shared resource IDs from get_shared_resource_ids()
        
    Returns:
        Dict of environment variables for runtime creation
    """
    try:
        # Define required parameters
        required_params = [
            # DynamoDB tables
            f"/{PROJECT_PREFIX}/users/users-table-name",
            f"/{PROJECT_PREFIX}/rbac/app-roles-table-name",
            f"/{PROJECT_PREFIX}/auth/oidc-state-table-name",
            f"/{PROJECT_PREFIX}/auth/api-keys-table-name",
            f"/{PROJECT_PREFIX}/oauth/providers-table-name",
            f"/{PROJECT_PREFIX}/oauth/user-tokens-table-name",
            f"/{PROJECT_PREFIX}/rag/assistants-table-name",
            # Quota & cost tracking tables
            f"/{PROJECT_PREFIX}/quota/user-quotas-table-name",
            f"/{PROJECT_PREFIX}/quota/quota-events-table-name",
            f"/{PROJECT_PREFIX}/cost-tracking/sessions-metadata-table-name",
            f"/{PROJECT_PREFIX}/cost-tracking/user-cost-summary-table-name",
            f"/{PROJECT_PREFIX}/cost-tracking/system-cost-rollup-table-name",
            f"/{PROJECT_PREFIX}/admin/managed-models-table-name",
            # Auth provider secrets
            f"/{PROJECT_PREFIX}/auth/auth-provider-secrets-arn",
            # OAuth configuration
            f"/{PROJECT_PREFIX}/oauth/token-encryption-key-arn",
            f"/{PROJECT_PREFIX}/oauth/client-secrets-arn",
            f"/{PROJECT_PREFIX}/oauth/callback-url",
            # S3 buckets
            f"/{PROJECT_PREFIX}/rag/vector-bucket-name",
            f"/{PROJECT_PREFIX}/rag/vector-index-name",
            # URLs
            f"/{PROJECT_PREFIX}/network/alb-url",
            f"/{PROJECT_PREFIX}/frontend/url",
            f"/{PROJECT_PREFIX}/frontend/cors-origins",
        ]
        
        # Fetch all required parameters
        params = {}
        for param_name in required_params:
            params[param_name] = get_required_parameter(param_name)
        
        # Validate and normalize URLs
        alb_url = validate_url(params[f"/{PROJECT_PREFIX}/network/alb-url"], "alb-url")
        frontend_url = validate_url(params[f"/{PROJECT_PREFIX}/frontend/url"], "frontend-url")
        callback_url = validate_url(params[f"/{PROJECT_PREFIX}/oauth/callback-url"], "oauth-callback-url")
        
        # Construct environment variables dictionary
        env_vars = {
            # Basic configuration
            'LOG_LEVEL': 'INFO',
            'PROJECT_NAME': PROJECT_PREFIX,
            'AWS_REGION': AWS_REGION,
            'AWS_DEFAULT_REGION': AWS_REGION,
            'PROVIDER_ID': provider_id,
            
            # DynamoDB tables
            'DYNAMODB_USERS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/users/users-table-name"],
            'DYNAMODB_APP_ROLES_TABLE_NAME': params[f"/{PROJECT_PREFIX}/rbac/app-roles-table-name"],
            'DYNAMODB_OIDC_STATE_TABLE_NAME': params[f"/{PROJECT_PREFIX}/auth/oidc-state-table-name"],
            'DYNAMODB_API_KEYS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/auth/api-keys-table-name"],
            'DYNAMODB_OAUTH_PROVIDERS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/oauth/providers-table-name"],
            'DYNAMODB_OAUTH_USER_TOKENS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/oauth/user-tokens-table-name"],
            'DYNAMODB_ASSISTANTS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/rag/assistants-table-name"],
            
            # Quota & cost tracking tables
            'DYNAMODB_QUOTA_TABLE': params[f"/{PROJECT_PREFIX}/quota/user-quotas-table-name"],
            'DYNAMODB_QUOTA_EVENTS_TABLE': params[f"/{PROJECT_PREFIX}/quota/quota-events-table-name"],
            'DYNAMODB_SESSIONS_METADATA_TABLE_NAME': params[f"/{PROJECT_PREFIX}/cost-tracking/sessions-metadata-table-name"],
            'DYNAMODB_COST_SUMMARY_TABLE_NAME': params[f"/{PROJECT_PREFIX}/cost-tracking/user-cost-summary-table-name"],
            'DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME': params[f"/{PROJECT_PREFIX}/cost-tracking/system-cost-rollup-table-name"],
            'DYNAMODB_MANAGED_MODELS_TABLE_NAME': params[f"/{PROJECT_PREFIX}/admin/managed-models-table-name"],
            
            # Auth providers
            'DYNAMODB_AUTH_PROVIDERS_TABLE_NAME': AUTH_PROVIDERS_TABLE,
            'AUTH_PROVIDER_SECRETS_ARN': params[f"/{PROJECT_PREFIX}/auth/auth-provider-secrets-arn"],
            
            # OAuth configuration
            'OAUTH_TOKEN_ENCRYPTION_KEY_ARN': params[f"/{PROJECT_PREFIX}/oauth/token-encryption-key-arn"],
            'OAUTH_CLIENT_SECRETS_ARN': params[f"/{PROJECT_PREFIX}/oauth/client-secrets-arn"],
            'OAUTH_CALLBACK_URL': callback_url,
            
            # AgentCore resources (from shared_resources parameter)
            'MEMORY_ARN': shared_resources['memory_arn'],
            'MEMORY_ID': shared_resources['memory_id'],
            'CODE_INTERPRETER_ID': shared_resources['code_interpreter_id'],
            'BROWSER_ID': shared_resources['browser_id'],
            'GATEWAY_URL': shared_resources['gateway_url'],
            
            # AgentCore Memory configuration
            'AGENTCORE_MEMORY_TYPE': 'dynamodb',
            'AGENTCORE_MEMORY_ID': shared_resources['memory_id'],
            
            # S3 storage
            'S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME': params[f"/{PROJECT_PREFIX}/rag/vector-bucket-name"],
            'S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME': params[f"/{PROJECT_PREFIX}/rag/vector-index-name"],
            
            # Authentication
            'ENABLE_AUTHENTICATION': 'true',
            
            # Directories (runtime-specific paths)
            'UPLOAD_DIR': '/tmp/uploads',
            'OUTPUT_DIR': '/tmp/output',
            'GENERATED_IMAGES_DIR': '/tmp/generated_images',
            
            # URLs
            'API_URL': alb_url,
            'FRONTEND_URL': frontend_url,
            'CORS_ORIGINS': params[f"/{PROJECT_PREFIX}/frontend/cors-origins"],
        }
        
        # Add optional API keys if they exist
        tavily_key = get_optional_parameter(f"/{PROJECT_PREFIX}/api-keys/tavily-api-key")
        if tavily_key:
            env_vars['TAVILY_API_KEY'] = tavily_key
            
        nova_act_key = get_optional_parameter(f"/{PROJECT_PREFIX}/api-keys/nova-act-api-key")
        if nova_act_key:
            env_vars['NOVA_ACT_API_KEY'] = nova_act_key
        
        logger.info(f"Constructed {len(env_vars)} environment variables for runtime")
        return env_vars
        
    except ClientError as e:
        logger.error(f"Failed to fetch environment variables from SSM: {e}")
        raise ValueError(f"Could not fetch environment variables: {e}")

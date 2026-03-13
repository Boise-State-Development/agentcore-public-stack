"""
Runtime Updater Lambda for AgentCore Multi-Runtime Architecture

Automatically updates all provider runtimes when new container images are deployed.
Triggered by EventBridge when SSM parameter for image tag changes.

Event Flow:
- EventBridge detects SSM parameter change
- Lambda queries all providers with existing runtimes
- Updates runtimes in parallel (max 5 concurrent)
- Retries failed updates with exponential backoff
- Sends SNS notification summary
"""
import json
import os
import logging
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import boto3
from botocore.exceptions import ClientError

# AWS clients
dynamodb = boto3.client('dynamodb')
ssm = boto3.client('ssm')
ecr = boto3.client('ecr')
bedrock_agentcore = boto3.client('bedrock-agentcore-control')
sns = boto3.client('sns')

# Environment variables
PROJECT_PREFIX = os.environ['PROJECT_PREFIX']
AWS_REGION = os.environ['AWS_REGION']
AUTH_PROVIDERS_TABLE = os.environ['AUTH_PROVIDERS_TABLE']
SNS_TOPIC_ARN = os.environ['SNS_TOPIC_ARN']

# Configuration
MAX_CONCURRENT_UPDATES = 5
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 2  # seconds


def lambda_handler(event, context):
    """
    Lambda handler for EventBridge events triggered by SSM parameter changes
    
    Processes image tag updates and updates all provider runtimes
    """
    try:
        logger.info(f"Event: {json.dumps(event)}")
        
        # Extract new image tag from event
        new_image_tag = extract_image_tag_from_event(event)
        
        if not new_image_tag:
            logger.error("Could not extract image tag from event")
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Invalid event format'})
            }
        
        logger.info(f"New image tag detected: {new_image_tag}")
        
        # Query DynamoDB for all providers with existing runtimes
        providers = get_providers_with_runtimes()
        
        if not providers:
            logger.info("No providers with runtimes found")
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'No runtimes to update'})
            }
        
        logger.info(f"Found {len(providers)} providers with runtimes")
        
        # Get new container image URI
        new_image_uri = get_container_image_uri(new_image_tag)
        
        logger.info(f"New image URI: {new_image_uri}")
        
        # Update runtimes in parallel
        results = update_runtimes_parallel(providers, new_image_uri)
        
        # Send SNS notification summary
        send_update_summary(results, new_image_tag)
        
        # Log summary
        success_count = sum(1 for r in results if r['success'])
        failure_count = len(results) - success_count
        
        logger.info(f"✅ Update complete: {success_count} succeeded, {failure_count} failed")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Runtime updates completed',
                'total': len(results),
                'succeeded': success_count,
                'failed': failure_count
            })
        }
        
    except Exception as e:
        logger.error(f"Error processing runtime updates: {str(e)}", exc_info=True)
        
        # Send SNS alert for critical failure
        send_critical_failure_alert(str(e))
        
        # Re-raise to mark Lambda execution as failed
        raise


def extract_image_tag_from_event(event: Dict[str, Any]) -> Optional[str]:
    """
    Extract image tag from EventBridge event
    
    Args:
        event: EventBridge event from SSM parameter change
        
    Returns:
        New image tag or None if not found
    """
    try:
        # EventBridge event structure for SSM parameter changes
        detail = event.get('detail', {})
        
        # Get parameter name from event
        param_name = detail.get('name', '')
        
        # Verify this is the image tag parameter
        expected_param = f"/{PROJECT_PREFIX}/inference-api/image-tag"
        
        if param_name == expected_param:
            # SSM Parameter Store Change events don't include the value,
            # so we always need to fetch it from SSM
            return get_image_tag_from_ssm()
        
        logger.warning(f"Event parameter name mismatch: {param_name} != {expected_param}")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting image tag: {e}")
        return None


def get_image_tag_from_ssm() -> str:
    """Fetch current image tag from SSM"""
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


def get_providers_with_runtimes() -> List[Dict[str, Any]]:
    """
    Query DynamoDB for all providers with existing runtimes
    
    Returns:
        List of provider records with runtime information
    """
    providers = []
    
    try:
        # Scan table for all providers
        response = dynamodb.scan(
            TableName=AUTH_PROVIDERS_TABLE,
            FilterExpression='attribute_exists(agentcoreRuntimeId) AND agentcoreRuntimeStatus <> :failed',
            ExpressionAttributeValues={
                ':failed': {'S': 'FAILED'}
            }
        )
        
        for item in response.get('Items', []):
            provider = {
                'provider_id': deserialize_dynamodb_value(item['providerId']),
                'runtime_id': deserialize_dynamodb_value(item.get('agentcoreRuntimeId', {})),
                'runtime_arn': deserialize_dynamodb_value(item.get('agentcoreRuntimeArn', {})),
                'display_name': deserialize_dynamodb_value(item.get('displayName', {}))
            }
            
            if provider['runtime_id']:
                providers.append(provider)
        
        # Handle pagination
        while 'LastEvaluatedKey' in response:
            response = dynamodb.scan(
                TableName=AUTH_PROVIDERS_TABLE,
                FilterExpression='attribute_exists(agentcoreRuntimeId) AND agentcoreRuntimeStatus <> :failed',
                ExpressionAttributeValues={
                    ':failed': {'S': 'FAILED'}
                },
                ExclusiveStartKey=response['LastEvaluatedKey']
            )
            
            for item in response.get('Items', []):
                provider = {
                    'provider_id': deserialize_dynamodb_value(item['providerId']),
                    'runtime_id': deserialize_dynamodb_value(item.get('agentcoreRuntimeId', {})),
                    'runtime_arn': deserialize_dynamodb_value(item.get('agentcoreRuntimeArn', {})),
                    'display_name': deserialize_dynamodb_value(item.get('displayName', {}))
                }
                
                if provider['runtime_id']:
                    providers.append(provider)
        
        return providers
        
    except ClientError as e:
        logger.error(f"Failed to query providers: {e}")
        raise


def update_runtimes_parallel(
    providers: List[Dict[str, Any]],
    new_image_uri: str
) -> List[Dict[str, Any]]:
    """
    Update runtimes in parallel with max concurrency limit
    
    Args:
        providers: List of provider records
        new_image_uri: New container image URI
        
    Returns:
        List of update results
    """
    results = []
    
    # Use ThreadPoolExecutor for parallel updates
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_UPDATES) as executor:
        # Submit all update tasks
        future_to_provider = {
            executor.submit(update_runtime_with_retry, provider, new_image_uri): provider
            for provider in providers
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_provider):
            provider = future_to_provider[future]
            
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Unexpected error updating {provider['provider_id']}: {e}")
                results.append({
                    'provider_id': provider['provider_id'],
                    'display_name': provider.get('display_name', 'Unknown'),
                    'success': False,
                    'error': str(e),
                    'attempts': 0
                })
    
    return results


def update_runtime_with_retry(
    provider: Dict[str, Any],
    new_image_uri: str
) -> Dict[str, Any]:
    """
    Update runtime with retry logic and exponential backoff
    
    Args:
        provider: Provider record with runtime information
        new_image_uri: New container image URI
        
    Returns:
        Update result dictionary
    """
    provider_id = provider['provider_id']
    runtime_id = provider['runtime_id']
    display_name = provider.get('display_name', 'Unknown')
    
    logger.info(f"Updating runtime for provider: {provider_id}")
    
    # Update DynamoDB status to UPDATING
    update_provider_status(provider_id, 'UPDATING')
    
    # Retry loop
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        try:
            logger.info(f"Attempt {attempt}/{MAX_RETRY_ATTEMPTS} for {provider_id}")
            
            # Fetch current runtime configuration
            current_runtime = bedrock_agentcore.get_agent_runtime(
                agentRuntimeId=runtime_id
            )
            
            # Update runtime with new container image
            update_runtime(runtime_id, current_runtime, new_image_uri)
            
            # Update DynamoDB status to READY
            update_provider_status(provider_id, 'READY')
            
            logger.info(f"✅ Successfully updated runtime for {provider_id}")
            
            return {
                'provider_id': provider_id,
                'display_name': display_name,
                'success': True,
                'attempts': attempt
            }
            
        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            
            logger.warning(
                f"Attempt {attempt} failed for {provider_id}: {error_code} - {error_msg}"
            )
            
            # Check if this is a retryable error
            if error_code in ['ThrottlingException', 'ServiceUnavailableException']:
                if attempt < MAX_RETRY_ATTEMPTS:
                    # Exponential backoff
                    sleep_time = RETRY_BACKOFF_BASE ** attempt
                    logger.info(f"Retrying in {sleep_time} seconds...")
                    time.sleep(sleep_time)
                    continue
            
            # Non-retryable error or max attempts reached
            logger.error(f"❌ Failed to update runtime for {provider_id}: {error_msg}")
            
            # Update DynamoDB with error status
            update_provider_error(provider_id, error_msg)
            
            return {
                'provider_id': provider_id,
                'display_name': display_name,
                'success': False,
                'error': error_msg,
                'attempts': attempt
            }
            
        except Exception as e:
            logger.error(f"Unexpected error for {provider_id}: {str(e)}", exc_info=True)
            
            if attempt < MAX_RETRY_ATTEMPTS:
                sleep_time = RETRY_BACKOFF_BASE ** attempt
                logger.info(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
                continue
            
            # Max attempts reached
            update_provider_error(provider_id, str(e))
            
            return {
                'provider_id': provider_id,
                'display_name': display_name,
                'success': False,
                'error': str(e),
                'attempts': attempt
            }
    
    # Should not reach here, but handle it
    error_msg = f"Failed after {MAX_RETRY_ATTEMPTS} attempts"
    update_provider_error(provider_id, error_msg)
    
    return {
        'provider_id': provider_id,
        'display_name': display_name,
        'success': False,
        'error': error_msg,
        'attempts': MAX_RETRY_ATTEMPTS
    }


def update_runtime(
    runtime_id: str,
    current_runtime: Dict[str, Any],
    new_image_uri: str
) -> None:
    """
    Update AgentCore Runtime with new container image
    
    Args:
        runtime_id: Runtime ID to update
        current_runtime: Current runtime configuration from GetAgentRuntime
        new_image_uri: New container image URI
    """
    logger.info(f"Updating runtime {runtime_id} with image {new_image_uri}")
    
    # Preserve all current configuration except container image
    update_params = {
        'agentRuntimeId': runtime_id,
        'agentRuntimeArtifact': {
            'containerConfiguration': {
                'containerUri': new_image_uri
            }
        },
        'roleArn': current_runtime['roleArn'],
        'networkConfiguration': current_runtime['networkConfiguration']
    }
    
    # Preserve authorizer configuration if present
    if 'authorizerConfiguration' in current_runtime:
        update_params['authorizerConfiguration'] = current_runtime['authorizerConfiguration']
    
    # ALWAYS set requestHeaderConfiguration with Authorization in the allowlist.
    # The previous approach of conditionally preserving the existing config was
    # fragile — if the GetAgentRuntime response ever omitted the field (API quirk,
    # race condition, eventual consistency), the Authorization header would silently
    # stop being forwarded, causing 401s on every request.
    # We now build the allowlist from scratch, merging in any existing custom headers.
    existing_headers = set()
    if 'requestHeaderConfiguration' in current_runtime:
        existing_headers = set(
            current_runtime['requestHeaderConfiguration'].get('requestHeaderAllowlist', [])
        )
    # Authorization MUST always be present — this is non-negotiable
    existing_headers.add('Authorization')
    update_params['requestHeaderConfiguration'] = {
        'requestHeaderAllowlist': sorted(existing_headers)
    }
    
    # Preserve environment variables if present
    if 'environmentVariables' in current_runtime:
        update_params['environmentVariables'] = current_runtime['environmentVariables']
    
    # Call UpdateAgentRuntime API
    bedrock_agentcore.update_agent_runtime(**update_params)
    
    logger.info(f"Runtime {runtime_id} update initiated")


def send_update_summary(results: List[Dict[str, Any]], image_tag: str) -> None:
    """
    Send SNS notification with update summary
    
    Args:
        results: List of update results
        image_tag: New image tag
    """
    success_count = sum(1 for r in results if r['success'])
    failure_count = len(results) - success_count
    
    # Build message
    subject = f"AgentCore Runtime Updates: {success_count} succeeded, {failure_count} failed"
    
    message_lines = [
        f"Runtime Update Summary",
        f"======================",
        f"",
        f"New Image Tag: {image_tag}",
        f"Total Runtimes: {len(results)}",
        f"Succeeded: {success_count}",
        f"Failed: {failure_count}",
        f"",
    ]
    
    # Add failure details if any
    if failure_count > 0:
        message_lines.append("Failed Updates:")
        message_lines.append("-" * 50)
        
        for result in results:
            if not result['success']:
                message_lines.append(
                    f"Provider: {result['display_name']} ({result['provider_id']})"
                )
                message_lines.append(f"Error: {result.get('error', 'Unknown error')}")
                message_lines.append(f"Attempts: {result.get('attempts', 0)}")
                message_lines.append("")
    
    message = "\n".join(message_lines)
    
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
        logger.info("SNS notification sent")
    except ClientError as e:
        logger.error(f"Failed to send SNS notification: {e}")


def send_critical_failure_alert(error_message: str) -> None:
    """
    Send SNS alert for critical Lambda failure
    
    Args:
        error_message: Error message
    """
    subject = "CRITICAL: AgentCore Runtime Updater Failed"
    
    message = f"""
Critical Failure in Runtime Updater Lambda

The Runtime Updater Lambda encountered a critical error and could not complete.

Error: {error_message}

Action Required: Investigate Lambda logs and retry manually if needed.

Timestamp: {datetime.utcnow().isoformat()}Z
"""
    
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message
        )
    except ClientError as e:
        logger.error(f"Failed to send critical failure alert: {e}")


# =============================================================================
# DynamoDB Helper Functions
# =============================================================================


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


def update_provider_status(provider_id: str, status: str) -> None:
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


def update_provider_error(provider_id: str, error_message: str) -> None:
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
                ':status': {'S': 'UPDATE_FAILED'},
                ':error': {'S': error_message[:1000]},  # Limit error message length
                ':updated': {'S': datetime.utcnow().isoformat() + 'Z'}
            }
        )
        logger.info(f"Updated provider {provider_id} with error status")
    except ClientError as e:
        logger.error(f"Failed to update provider error status: {e}")

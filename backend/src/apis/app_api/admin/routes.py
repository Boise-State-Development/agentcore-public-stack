"""Admin API routes

Provides privileged endpoints for administrative operations.
Requires admin role (Admin or SuperAdmin) via JWT token.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, status
from typing import Optional
import logging
import os
import boto3
from datetime import datetime
from botocore.exceptions import ClientError, BotoCoreError

from .models import (
    UserInfo,
    AllSessionsResponse,
    SessionDeleteResponse,
    SystemStatsResponse,
    BedrockModelsResponse,
    FoundationModelSummary,
)
from apis.shared.auth import User, require_admin, require_roles, has_any_role, get_current_user
from apis.app_api.sessions.services.metadata import list_user_sessions, get_session_metadata
from apis.app_api.sessions.services.messages import get_messages

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/me", response_model=UserInfo)
async def get_admin_info(admin_user: User = Depends(require_admin)):
    """
    Get information about the current admin user.

    This endpoint demonstrates basic admin authentication.
    Requires Admin or SuperAdmin role.

    Args:
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        UserInfo with admin user details

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
    """
    logger.info(f"Admin user info requested: {admin_user.email}")

    return UserInfo(
        email=admin_user.email,
        user_id=admin_user.user_id,
        name=admin_user.name,
        roles=admin_user.roles,
        picture=admin_user.picture,
    )


@router.get("/sessions/all", response_model=AllSessionsResponse)
async def list_all_sessions(
    limit: Optional[int] = Query(100, ge=1, le=1000, description="Maximum sessions to return"),
    next_token: Optional[str] = Query(None, description="Pagination token"),
    admin_user: User = Depends(require_admin),
):
    """
    List all sessions across all users (admin only).

    This endpoint allows admins to view sessions from any user in the system.
    Useful for monitoring, debugging, or support purposes.

    Args:
        limit: Maximum number of sessions to return (1-1000)
        next_token: Pagination token for next page
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        AllSessionsResponse with sessions and pagination info

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} listing all sessions (limit: {limit})")

    # NOTE: This is a simplified example. In a real implementation, you would:
    # 1. Query your database/storage to get all sessions across all users
    # 2. Implement proper pagination
    # 3. Add filtering/sorting options

    # For demonstration, we'll return a mock response
    # You should replace this with actual storage queries

    return AllSessionsResponse(
        sessions=[],
        total_count=0,
        next_token=None,
    )


@router.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
async def delete_any_session(
    session_id: str,
    admin_user: User = Depends(require_admin),
):
    """
    Delete any user's session (admin only).

    This endpoint allows admins to delete sessions from any user.
    Useful for handling abuse, privacy requests, or data cleanup.

    Args:
        session_id: ID of the session to delete
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        SessionDeleteResponse with deletion status

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if session not found
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} deleting session: {session_id}")

    # NOTE: This is a simplified example. In a real implementation, you would:
    # 1. Verify the session exists
    # 2. Delete session metadata and messages from storage
    # 3. Log the deletion for audit purposes
    # 4. Handle cleanup of related resources

    # For demonstration purposes, we'll return a success response
    # You should replace this with actual deletion logic

    return SessionDeleteResponse(
        success=True,
        session_id=session_id,
        message=f"Session {session_id} deleted by admin {admin_user.email}",
    )


@router.get("/stats", response_model=SystemStatsResponse)
async def get_system_stats(
    admin_user: User = Depends(require_admin),
):
    """
    Get system-wide statistics (admin only).

    Returns aggregated statistics about users, sessions, and messages.
    Useful for monitoring system usage and health.

    Args:
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        SystemStatsResponse with system statistics

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} requesting system stats")

    # NOTE: This is a simplified example. In a real implementation, you would:
    # 1. Query your database for actual statistics
    # 2. Cache results for performance
    # 3. Add more detailed metrics

    # For demonstration purposes, we'll return mock data
    # You should replace this with actual statistics queries

    return SystemStatsResponse(
        total_users=0,
        total_sessions=0,
        active_sessions=0,
        total_messages=0,
        stats_as_of=datetime.utcnow(),
    )


@router.get("/users/{user_id}/sessions")
async def get_user_sessions(
    user_id: str,
    limit: Optional[int] = Query(100, ge=1, le=1000),
    next_token: Optional[str] = Query(None),
    admin_user: User = Depends(require_admin),
):
    """
    Get all sessions for a specific user (admin only).

    This endpoint allows admins to view sessions for any user in the system.
    Useful for support and debugging purposes.

    Args:
        user_id: User ID to get sessions for
        limit: Maximum number of sessions to return
        next_token: Pagination token
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        SessionsListResponse with user's sessions

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if user not found
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} viewing sessions for user: {user_id}")

    # Use existing session service to get user's sessions
    result = await list_user_sessions(
        user_id=user_id,
        limit=limit,
        next_token=next_token,
    )

    return result


@router.get("/conditional-example")
async def conditional_admin_feature(
    current_user: User = Depends(get_current_user),
):
    """
    Example endpoint showing conditional admin features.

    This demonstrates how to use has_any_role() for conditional logic
    within a route handler, rather than blocking access entirely.

    Regular users can access this endpoint, but admins see additional data.

    Args:
        current_user: Authenticated user (injected by dependency)

    Returns:
        Dict with user-specific or admin-specific data
    """
    response = {
        "message": "Welcome!",
        "user_email": current_user.email,
        "user_roles": current_user.roles,
    }

    # Add admin-specific data if user is an admin
    if has_any_role(current_user, "Admin", "SuperAdmin"):
        logger.info(f"Admin {current_user.email} accessing with admin privileges")
        response["admin_data"] = {
            "debug_info": "Additional admin information",
            "system_health": "All systems operational",
        }

    return response


@router.post("/require-multiple-roles-example")
async def require_multiple_roles_example(
    admin_user: User = Depends(require_roles("Admin", "SuperAdmin", "DotNetDevelopers")),
):
    """
    Example endpoint requiring one of multiple specific roles.

    This demonstrates using require_roles() with multiple role options.
    User must have at least ONE of: Admin, SuperAdmin, or DotNetDevelopers.

    Args:
        admin_user: Authenticated user with required role (injected by dependency)

    Returns:
        Dict with success message
    """
    logger.info(f"User {admin_user.email} with roles {admin_user.roles} accessed multi-role endpoint")

    return {
        "message": "Access granted",
        "user": admin_user.email,
        "matched_roles": [role for role in admin_user.roles if role in ["Admin", "SuperAdmin", "DotNetDevelopers"]],
    }


@router.get("/bedrock/models", response_model=BedrockModelsResponse)
async def list_bedrock_models(
    by_provider: Optional[str] = Query(None, description="Filter by provider name (e.g., 'Anthropic', 'Amazon')"),
    by_output_modality: Optional[str] = Query(None, description="Filter by output modality (e.g., 'TEXT', 'IMAGE')"),
    by_inference_type: Optional[str] = Query(None, description="Filter by inference type (e.g., 'ON_DEMAND', 'PROVISIONED')"),
    max_results: Optional[int] = Query(100, ge=1, le=1000, description="Maximum number of models to return"),
    next_token: Optional[str] = Query(None, description="Pagination token for next page"),
    admin_user: User = Depends(require_admin),
):
    """
    List available AWS Bedrock foundation models (admin only).

    This endpoint queries AWS Bedrock to retrieve information about available
    foundation models, including their capabilities, providers, and configurations.

    Args:
        by_provider: Optional filter by provider name
        by_output_modality: Optional filter by output modality
        by_inference_type: Optional filter by inference type
        max_results: Maximum number of models to return (1-1000, default: 100)
        next_token: Pagination token for retrieving next page
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        BedrockModelsResponse with list of foundation models and pagination info

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if AWS API error or server error
    """
    logger.info(f"Admin {admin_user.email} listing Bedrock foundation models")

    try:
        # Initialize Bedrock control plane client (not bedrock-runtime)
        bedrock_region = os.environ.get('AWS_REGION', 'us-east-1')
        bedrock_client = boto3.client('bedrock', region_name=bedrock_region)

        # Build request parameters
        request_params = {
            'maxResults': max_results,
        }

        # Add optional filters
        if by_provider:
            request_params['byProvider'] = by_provider
        if by_output_modality:
            request_params['byOutputModality'] = by_output_modality
        if by_inference_type:
            request_params['byInferenceType'] = by_inference_type
        if next_token:
            request_params['nextToken'] = next_token

        # Call AWS Bedrock API
        logger.debug(f"Calling list_foundation_models with params: {request_params}")
        response = bedrock_client.list_foundation_models(**request_params)

        # Transform AWS response to our response model
        model_summaries = [
            FoundationModelSummary(
                modelId=model.get('modelId', ''),
                modelName=model.get('modelName', ''),
                providerName=model.get('providerName', ''),
                inputModalities=model.get('inputModalities', []),
                outputModalities=model.get('outputModalities', []),
                responseStreamingSupported=model.get('responseStreamingSupported', False),
                customizationsSupported=model.get('customizationsSupported', []),
                inferenceTypesSupported=model.get('inferenceTypesSupported', []),
                modelLifecycle=model.get('modelLifecycle'),
            )
            for model in response.get('modelSummaries', [])
        ]

        logger.info(f"✅ Retrieved {len(model_summaries)} Bedrock foundation models")

        return BedrockModelsResponse(
            models=model_summaries,
            nextToken=response.get('nextToken'),
            totalCount=len(model_summaries),
        )

    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"AWS Bedrock API error: {error_code} - {error_message}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AWS Bedrock API error: {error_code} - {error_message}"
        )
    except BotoCoreError as e:
        logger.error(f"Boto3 error calling Bedrock API: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error connecting to AWS Bedrock: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error listing Bedrock models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )

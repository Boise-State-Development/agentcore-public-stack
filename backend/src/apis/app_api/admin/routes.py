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
    GeminiModelsResponse,
    GeminiModelSummary,
    OpenAIModelsResponse,
    OpenAIModelSummary,
    ManagedModelCreate,
    ManagedModelUpdate,
    ManagedModel,
    ManagedModelsListResponse,
)
from apis.shared.auth import User, require_admin, require_roles, has_any_role, get_current_user
from apis.app_api.sessions.services.metadata import list_user_sessions, get_session_metadata
from apis.app_api.sessions.services.messages import get_messages
from apis.app_api.admin.services.managed_models import (
    create_managed_model,
    get_managed_model,
    list_managed_models,
    update_managed_model,
    delete_managed_model,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])





@router.get("/bedrock/models", response_model=BedrockModelsResponse)
async def list_bedrock_models(
    by_provider: Optional[str] = Query(None, description="Filter by provider name (e.g., 'Anthropic', 'Amazon')"),
    by_output_modality: Optional[str] = Query(None, description="Filter by output modality (e.g., 'TEXT', 'IMAGE')"),
    by_inference_type: Optional[str] = Query(None, description="Filter by inference type (e.g., 'ON_DEMAND', 'PROVISIONED')"),
    by_customization_type: Optional[str] = Query(None, description="Filter by customization type (e.g., 'FINE_TUNING', 'CONTINUED_PRE_TRAINING')"),
    max_results: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of models to return (client-side limit)"),
    admin_user: User = Depends(require_admin),
):
    """
    List available AWS Bedrock foundation models (admin only).

    This endpoint queries AWS Bedrock to retrieve information about available
    foundation models, including their capabilities, providers, and configurations.

    Note: The AWS Bedrock API doesn't support pagination or maxResults parameters.
    All filtering is done server-side via query parameters. Client-side limiting
    can be applied using the max_results parameter.

    Args:
        by_provider: Optional filter by provider name
        by_output_modality: Optional filter by output modality
        by_inference_type: Optional filter by inference type
        by_customization_type: Optional filter by customization type
        max_results: Optional client-side limit on number of models to return
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        BedrockModelsResponse with list of foundation models

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

        # Build request parameters (only supported parameters)
        request_params = {}

        # Add optional filters (only these are supported by the API)
        if by_provider:
            request_params['byProvider'] = by_provider
        if by_output_modality:
            request_params['byOutputModality'] = by_output_modality
        if by_inference_type:
            request_params['byInferenceType'] = by_inference_type
        if by_customization_type:
            request_params['byCustomizationType'] = by_customization_type

        # Call AWS Bedrock API
        logger.debug(f"Calling list_foundation_models with params: {request_params}")
        response = bedrock_client.list_foundation_models(**request_params)

        # Transform AWS response to our response model
        all_models = response.get('modelSummaries', [])
        
        # Apply client-side limiting if requested
        if max_results and len(all_models) > max_results:
            all_models = all_models[:max_results]
            logger.debug(f"Limited results to {max_results} models (client-side)")

        model_summaries = []
        for model in all_models:
            # Extract modelLifecycle status - it can be a dict with 'status' key or a string
            model_lifecycle = model.get('modelLifecycle')
            if isinstance(model_lifecycle, dict):
                model_lifecycle = model_lifecycle.get('status')

            model_summaries.append(
                FoundationModelSummary(
                    modelId=model.get('modelId', ''),
                    modelName=model.get('modelName', ''),
                    providerName=model.get('providerName', ''),
                    inputModalities=model.get('inputModalities', []),
                    outputModalities=model.get('outputModalities', []),
                    responseStreamingSupported=model.get('responseStreamingSupported', False),
                    customizationsSupported=model.get('customizationsSupported', []),
                    inferenceTypesSupported=model.get('inferenceTypesSupported', []),
                    modelLifecycle=model_lifecycle,
                )
            )

        # Sort models by ID in reverse order (newest versions typically have higher version numbers/dates)
        model_summaries.sort(key=lambda m: m.model_id, reverse=True)

        logger.info(f"✅ Retrieved {len(model_summaries)} Bedrock foundation models")

        return BedrockModelsResponse(
            models=model_summaries,
            nextToken=None,  # API doesn't support pagination
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


@router.get("/gemini/models", response_model=GeminiModelsResponse)
async def list_gemini_models(
    max_results: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of models to return"),
    admin_user: User = Depends(require_admin),
):
    """
    List available Google Gemini models (admin only).

    This endpoint uses the Google AI Python SDK to retrieve information about available
    Gemini models, including their capabilities, token limits, and supported methods.

    Args:
        max_results: Optional limit on number of models to return
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        GeminiModelsResponse with list of Gemini models

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if Google API error or server error
    """
    logger.info(f"Admin {admin_user.email} listing Gemini models")

    try:
        # Check if Google API key is configured
        # Try both GOOGLE_API_KEY and GOOGLE_GEMINI_API_KEY for compatibility
        google_api_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GOOGLE_GEMINI_API_KEY')
        if not google_api_key:
            logger.error("GOOGLE_API_KEY or GOOGLE_GEMINI_API_KEY environment variable not set")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Google API key not configured. Please set GOOGLE_API_KEY or GOOGLE_GEMINI_API_KEY environment variable."
            )

        # Import Google AI SDK
        try:
            from google import genai
        except ImportError:
            logger.error("Google GenAI SDK not installed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Google GenAI SDK not installed. Please install google-genai package."
            )

        # Initialize Gemini client
        client = genai.Client(api_key=google_api_key)

        # List available models
        logger.debug("Fetching Gemini models from Google API")
        all_models = []

        for model in client.models.list():
            # Extract model information according to Gemini API response structure
            # Note: The API returns camelCase properties (e.g., displayName, inputTokenLimit)
            # The SDK may expose these properties directly or convert them to snake_case

            # Get supportedGenerationMethods - try both naming conventions
            # According to API docs, this array includes: generateContent, countTokens, createCachedContent, batchGenerateContent
            # Note: streamGenerateContent is NOT listed but is available via SDK's generate_content_stream()
            supported_methods = getattr(model, 'supportedGenerationMethods', None)
            if supported_methods is None:
                supported_methods = getattr(model, 'supported_generation_methods', [])

            model_data = GeminiModelSummary(
                name=model.name,
                baseModelId=getattr(model, 'baseModelId', getattr(model, 'base_model_id', None)),
                version=getattr(model, 'version', None),
                displayName=getattr(model, 'displayName', getattr(model, 'display_name', model.name)),
                description=getattr(model, 'description', None),
                inputTokenLimit=getattr(model, 'inputTokenLimit', getattr(model, 'input_token_limit', None)),
                outputTokenLimit=getattr(model, 'outputTokenLimit', getattr(model, 'output_token_limit', None)),
                supportedGenerationMethods=supported_methods if supported_methods else [],
                thinking=getattr(model, 'thinking', None),
                temperature=getattr(model, 'temperature', None),
                maxTemperature=getattr(model, 'maxTemperature', getattr(model, 'max_temperature', None)),
                topP=getattr(model, 'topP', getattr(model, 'top_p', None)),
                topK=getattr(model, 'topK', getattr(model, 'top_k', None)),
            )
            all_models.append(model_data)

        # Sort models by name in reverse order (newest versions typically have higher version numbers)
        all_models.sort(key=lambda m: m.name, reverse=True)

        # Apply client-side limiting if requested
        if max_results and len(all_models) > max_results:
            all_models = all_models[:max_results]
            logger.debug(f"Limited results to {max_results} models")

        logger.info(f"✅ Retrieved {len(all_models)} Gemini models")

        return GeminiModelsResponse(
            models=all_models,
            totalCount=len(all_models),
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing Gemini models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching Gemini models: {str(e)}"
        )


@router.get("/openai/models", response_model=OpenAIModelsResponse)
async def list_openai_models(
    max_results: Optional[int] = Query(None, ge=1, le=1000, description="Maximum number of models to return"),
    admin_user: User = Depends(require_admin),
):
    """
    List available OpenAI models (admin only).

    This endpoint uses the OpenAI Python SDK to retrieve information about available
    models from OpenAI's API.

    Note: The OpenAI list models endpoint provides limited information compared to
    Bedrock and Gemini APIs. For more detailed model specifications, see:
    https://platform.openai.com/docs/models/compare

    Args:
        max_results: Optional limit on number of models to return
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        OpenAIModelsResponse with list of OpenAI models

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if OpenAI API error or server error
    """
    logger.info(f"Admin {admin_user.email} listing OpenAI models")

    try:
        # Check if OpenAI API key is configured
        openai_api_key = os.environ.get('OPENAI_API_KEY')
        if not openai_api_key:
            logger.error("OPENAI_API_KEY environment variable not set")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OpenAI API key not configured. Please set OPENAI_API_KEY environment variable."
            )

        # Import OpenAI SDK
        try:
            from openai import OpenAI
        except ImportError:
            logger.error("OpenAI SDK not installed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="OpenAI SDK not installed. Please install openai package."
            )

        # Initialize OpenAI client
        client = OpenAI(api_key=openai_api_key)

        # List available models
        logger.debug("Fetching OpenAI models from OpenAI API")
        all_models = []

        response = client.models.list()
        for model in response.data:
            model_data = OpenAIModelSummary(
                id=model.id,
                created=model.created,
                ownedBy=model.owned_by,
                object=model.object,
            )
            all_models.append(model_data)

        # Sort models by creation date (newest first), then by ID for consistency
        all_models.sort(key=lambda m: (-(m.created or 0), m.id))

        # Apply client-side limiting if requested
        if max_results and len(all_models) > max_results:
            all_models = all_models[:max_results]
            logger.debug(f"Limited results to {max_results} models")

        logger.info(f"✅ Retrieved {len(all_models)} OpenAI models")

        return OpenAIModelsResponse(
            models=all_models,
            totalCount=len(all_models),
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error listing OpenAI models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching OpenAI models: {str(e)}"
        )


# =============================================================================
# Enabled Models Endpoints (Model Management)
# =============================================================================

@router.get("/managed-models", response_model=ManagedModelsListResponse)
async def list_managed_models_endpoint(
    admin_user: User = Depends(require_admin),
):
    """
    List all enabled models (admin only).

    This endpoint returns all models that have been enabled for use in the system,
    regardless of role restrictions. Use GET /models for user-facing endpoint
    with role-based filtering.

    Args:
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        ManagedModelsListResponse with list of all enabled models

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} listing all enabled models")

    try:
        models = await list_managed_models(user_roles=None)  # None = no role filtering

        # Convert ManagedModel instances to dicts for Pydantic v2 validation
        models_dict = [model.model_dump(by_alias=True) for model in models]
        
        return ManagedModelsListResponse(
            models=models_dict,
            total_count=len(models),
        )

    except Exception as e:
        logger.error(f"Unexpected error listing enabled models: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing enabled models: {str(e)}"
        )


@router.post("/managed-models", response_model=ManagedModel, status_code=status.HTTP_201_CREATED)
async def create_managed_model_endpoint(
    model_data: ManagedModelCreate,
    admin_user: User = Depends(require_admin),
):
    """
    Create a new enabled model (admin only).

    This endpoint allows admins to add new models to the system and configure
    which roles have access to them.

    Args:
        model_data: Model creation data
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        ManagedModel: Created model with ID and timestamps

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 400 if model with same modelId already exists
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} creating enabled model: {model_data.model_name}")

    try:
        model = await create_managed_model(model_data)
        return model

    except ValueError as e:
        # Model already exists
        logger.warning(f"Model creation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Unexpected error creating enabled model: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating enabled model: {str(e)}"
        )


@router.get("/managed-models/{model_id}", response_model=ManagedModel)
async def get_managed_model_endpoint(
    model_id: str,
    admin_user: User = Depends(require_admin),
):
    """
    Get a specific enabled model by ID (admin only).

    Args:
        model_id: Model identifier
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        ManagedModel: Model details

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if model not found
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} requesting enabled model: {model_id}")

    try:
        model = await get_managed_model(model_id)

        if not model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model with ID '{model_id}' not found"
            )

        return model

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting enabled model: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting enabled model: {str(e)}"
        )


@router.put("/managed-models/{model_id}", response_model=ManagedModel)
async def update_managed_model_endpoint(
    model_id: str,
    updates: ManagedModelUpdate,
    admin_user: User = Depends(require_admin),
):
    """
    Update an enabled model (admin only).

    This endpoint allows admins to update model configuration, including
    pricing, role access, and enabled status.

    Args:
        model_id: Model identifier
        updates: Fields to update
        admin_user: Authenticated admin user (injected by dependency)

    Returns:
        ManagedModel: Updated model

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if model not found
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} updating enabled model: {model_id}")

    try:
        model = await update_managed_model(model_id, updates)

        if not model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model with ID '{model_id}' not found"
            )

        return model

    except ValueError as e:
        # Duplicate modelId or other validation error
        logger.warning(f"Model update failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating enabled model: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error updating enabled model: {str(e)}"
        )


@router.delete("/managed-models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_managed_model_endpoint(
    model_id: str,
    admin_user: User = Depends(require_admin),
):
    """
    Delete an enabled model (admin only).

    This endpoint permanently removes a model from the system.

    Args:
        model_id: Model identifier
        admin_user: Authenticated admin user (injected by dependency)

    Raises:
        HTTPException:
            - 401 if not authenticated
            - 403 if user lacks admin role
            - 404 if model not found
            - 500 if server error
    """
    logger.info(f"Admin {admin_user.email} deleting enabled model: {model_id}")

    try:
        deleted = await delete_managed_model(model_id)

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model with ID '{model_id}' not found"
            )

        return None

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error deleting enabled model: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting enabled model: {str(e)}"
        )


# ========== Include Quota Management Subrouter ==========
from .quota.routes import router as quota_router

router.include_router(quota_router)

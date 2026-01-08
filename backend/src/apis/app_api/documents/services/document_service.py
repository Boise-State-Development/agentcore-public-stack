"""Document service layer for DynamoDB operations

This service handles storing and retrieving document data using
single-table design with adjacency lists.

DynamoDB Schema:
- PK: AST#{assistant_id}
- SK: DOC#{document_id}
- GSI for status queries (optional)
"""

import logging
import os
import uuid
from typing import Optional, Tuple, List
from datetime import datetime

from apis.app_api.documents.models import Document, DocumentStatus

logger = logging.getLogger(__name__)


def _generate_document_id() -> str:
    """Generate a unique document ID with DOC prefix"""
    return f"DOC-{uuid.uuid4().hex[:12]}"


def _get_current_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return datetime.utcnow().isoformat() + "Z"


async def create_document(
    assistant_id: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    s3_key: str,
    document_id: Optional[str] = None
) -> Document:
    """
    Create a new document record in DynamoDB
    
    Initial status is 'uploading'. Lambda will update to 'chunking' after
    S3 event is received.
    
    Args:
        assistant_id: Parent assistant identifier
        filename: Original filename
        content_type: MIME type
        size_bytes: File size in bytes
        s3_key: S3 object key where file will be uploaded
    
    Returns:
        Document object with status='uploading'
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        raise
    
    table_name = os.environ.get('ASSISTANTS_TABLE_NAME')
    if not table_name:
        raise ValueError("ASSISTANTS_TABLE_NAME environment variable not set")
    
    if not document_id:
        document_id = _generate_document_id()
    now = _get_current_timestamp()
    
    document = Document(
        document_id=document_id,
        assistant_id=assistant_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
        s3_key=s3_key,
        status='uploading',
        created_at=now,
        updated_at=now
    )
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    item = document.model_dump(by_alias=True, exclude_none=True)
    item['PK'] = f'AST#{assistant_id}'
    item['SK'] = f'DOC#{document_id}'
    
    try:
        table.put_item(Item=item)
        logger.info(f"Created document {document_id} for assistant {assistant_id}")
        return document
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        logger.error(f"Failed to create document in DynamoDB: {error_code} - {e}")
        raise


async def get_document(
    assistant_id: str,
    document_id: str,
    owner_id: str
) -> Optional[Document]:
    """
    Retrieve document by ID with ownership verification
    
    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        owner_id: User identifier (for ownership verification)
    
    Returns:
        Document object if found and user owns parent assistant, None otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        from apis.app_api.assistants.services.assistant_service import get_assistant
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        return None
    
    # Verify assistant ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
        return None
    
    table_name = os.environ.get('ASSISTANTS_TABLE_NAME')
    if not table_name:
        logger.error("ASSISTANTS_TABLE_NAME environment variable not set")
        return None
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    try:
        response = table.get_item(
            Key={
                'PK': f'AST#{assistant_id}',
                'SK': f'DOC#{document_id}'
            }
        )
        
        if 'Item' not in response:
            logger.info(f"Document {document_id} not found for assistant {assistant_id}")
            return None
        
        item = response['Item']
        return Document.model_validate(item)
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'ResourceNotFoundException':
            logger.warning(f"Table {table_name} not found")
        else:
            logger.error(f"Failed to retrieve document from DynamoDB: {error_code} - {e}")
        return None
    except Exception as e:
        logger.error(f"Failed to retrieve document: {e}", exc_info=True)
        return None


async def update_document_status(
    assistant_id: str,
    document_id: str,
    status: DocumentStatus,
    vector_store_id: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
    error_details: Optional[str] = None,
    table_name: Optional[str] = None
) -> Optional[Document]:
    """
    Update document processing status
    
    Called by Lambda during processing pipeline:
    - 'uploading' -> 'chunking' (after S3 upload)
    - 'chunking' -> 'embedding' (after text extraction and chunking)
    - 'embedding' -> 'complete' (after embeddings stored in vector store)
    - any status -> 'failed' (on error)
    
    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        status: New processing status
        vector_store_id: Optional S3 vector store identifier
        chunk_count: Optional number of chunks created
        error_message: Optional user-friendly error message if status='failed'
        error_details: Optional technical error details if status='failed'
        table_name: Optional DynamoDB table name (defaults to ASSISTANTS_TABLE_NAME env var)
    
    Returns:
        Updated Document object if found, None otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        return None
    
    # Get table name from parameter or environment
    if not table_name:
        table_name = os.environ.get('ASSISTANTS_TABLE_NAME')
        if not table_name:
            logger.error("ASSISTANTS_TABLE_NAME environment variable not set")
            return None
    
    # Initialize DynamoDB resource
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    # Build update expression and attribute values
    set_parts = []
    remove_attributes = []
    expression_attribute_values = {}
    expression_attribute_names = {}
    
    # Always update status and updated_at
    set_parts.append("#status = :status")
    set_parts.append("updatedAt = :updated_at")
    expression_attribute_names["#status"] = "status"  # 'status' is a reserved word
    expression_attribute_values[":status"] = status
    expression_attribute_values[":updated_at"] = _get_current_timestamp()
    
    # Add optional fields
    if chunk_count is not None:
        set_parts.append("chunkCount = :chunk_count")
        expression_attribute_values[":chunk_count"] = chunk_count
    
    if vector_store_id is not None:
        set_parts.append("vectorStoreId = :vector_store_id")
        expression_attribute_values[":vector_store_id"] = vector_store_id
    
    # Handle error fields
    if status == 'failed':
        # Set error fields if provided
        if error_message is not None:
            set_parts.append("errorMessage = :error_message")
            expression_attribute_values[":error_message"] = error_message
        if error_details is not None:
            set_parts.append("errorDetails = :error_details")
            expression_attribute_values[":error_details"] = error_details
    else:
        # Remove error fields when status is not 'failed' (e.g., on retry)
        remove_attributes.extend(["errorMessage", "errorDetails"])
    
    # Build update expression
    update_expression_parts = []
    if set_parts:
        update_expression_parts.append("SET " + ", ".join(set_parts))
    if remove_attributes:
        update_expression_parts.append("REMOVE " + ", ".join(remove_attributes))
    
    update_expression = " ".join(update_expression_parts)
    
    # Perform update
    try:
        update_params = {
            'Key': {
                'PK': f'AST#{assistant_id}',
                'SK': f'DOC#{document_id}'
            },
            'UpdateExpression': update_expression,
            'ExpressionAttributeValues': expression_attribute_values,
            'ReturnValues': 'ALL_NEW'
        }
        
        # Only include ExpressionAttributeNames if we have reserved words
        if expression_attribute_names:
            update_params['ExpressionAttributeNames'] = expression_attribute_names
        
        response = table.update_item(**update_params)
        
        # Parse response and return Document object
        if 'Attributes' in response:
            item = response['Attributes']
            try:
                return Document.model_validate(item)
            except Exception as e:
                logger.warning(f"Failed to parse document from DynamoDB response: {e}")
                return None
        
        return None
        
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'ResourceNotFoundException':
            logger.warning(f"Document not found: assistant_id={assistant_id}, document_id={document_id}")
        else:
            logger.error(f"Failed to update document status in DynamoDB: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error updating document status: {e}", exc_info=True)
        return None


async def list_assistant_documents(
    assistant_id: str,
    owner_id: str,
    limit: Optional[int] = None,
    next_token: Optional[str] = None
) -> Tuple[List[Document], Optional[str]]:
    """
    List all documents for an assistant with pagination
    
    Query pattern:
    - PK = AST#{assistant_id}
    - SK begins_with DOC#
    
    Args:
        assistant_id: Parent assistant identifier
        owner_id: User identifier (for ownership verification)
        limit: Maximum number of documents to return
        next_token: Pagination token
    
    Returns:
        Tuple of (list of Document objects, next_token if more exist)
    """
    try:
        import boto3
        import json
        import base64
        from boto3.dynamodb.conditions import Key
        from botocore.exceptions import ClientError
        from apis.app_api.assistants.services.assistant_service import get_assistant
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        return [], None
    
    # Verify assistant ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
        return [], None
    
    table_name = os.environ.get('ASSISTANTS_TABLE_NAME')
    if not table_name:
        logger.error("ASSISTANTS_TABLE_NAME environment variable not set")
        return [], None
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    # Decode next_token for ExclusiveStartKey
    exclusive_start_key = None
    if next_token:
        try:
            decoded = base64.b64decode(next_token).decode('utf-8')
            exclusive_start_key = json.loads(decoded)
        except Exception as e:
            logger.warning(f"Invalid next_token: {e}, ignoring pagination")
    
    query_params = {
        'KeyConditionExpression': Key('PK').eq(f'AST#{assistant_id}') & Key('SK').begins_with('DOC#'),
    }
    
    if limit and limit > 0:
        query_params['Limit'] = limit
    
    if exclusive_start_key:
        query_params['ExclusiveStartKey'] = exclusive_start_key
    
    try:
        response = table.query(**query_params)
        
        documents = []
        for item in response.get('Items', []):
            try:
                documents.append(Document.model_validate(item))
            except Exception as e:
                logger.warning(f"Failed to parse document item: {e}")
                continue
        
        # Generate next_token from LastEvaluatedKey
        next_page_token = None
        if 'LastEvaluatedKey' in response:
            encoded = json.dumps(response['LastEvaluatedKey'])
            next_page_token = base64.b64encode(encoded.encode('utf-8')).decode('utf-8')
        
        logger.info(f"Listed {len(documents)} documents for assistant {assistant_id}")
        return documents, next_page_token
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        logger.error(f"Failed to list documents from DynamoDB: {error_code} - {e}")
        return [], None
    except Exception as e:
        logger.error(f"Failed to list documents: {e}", exc_info=True)
        return [], None


async def delete_document(
    assistant_id: str,
    document_id: str,
    owner_id: str
) -> bool:
    """
    Delete document record from DynamoDB
    
    Note: Caller should also delete S3 objects (source file and vector store data)
    
    Args:
        assistant_id: Parent assistant identifier
        document_id: Document identifier
        owner_id: User identifier (for ownership verification)
    
    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
        from apis.app_api.assistants.services.assistant_service import get_assistant
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        return False
    
    # Verify assistant ownership first
    assistant = await get_assistant(assistant_id, owner_id)
    if not assistant:
        logger.warning(f"Access denied: assistant {assistant_id} not owned by user {owner_id}")
        return False
    
    table_name = os.environ.get('ASSISTANTS_TABLE_NAME')
    if not table_name:
        logger.error("ASSISTANTS_TABLE_NAME environment variable not set")
        return False
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    try:
        table.delete_item(
            Key={
                'PK': f'AST#{assistant_id}',
                'SK': f'DOC#{document_id}'
            }
        )
        
        logger.info(f"Deleted document {document_id} for assistant {assistant_id}")
        return True
    
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        if error_code == 'ResourceNotFoundException':
            logger.warning(f"Document {document_id} not found")
        else:
            logger.error(f"Failed to delete document from DynamoDB: {error_code} - {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to delete document: {e}", exc_info=True)
        return False

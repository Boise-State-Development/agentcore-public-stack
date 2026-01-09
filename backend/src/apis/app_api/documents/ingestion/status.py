"""Document status management for Lambda ingestion pipeline

Centralized status update logic with error message formatting.
"""

import logging
import os
import traceback
import uuid
from datetime import datetime
from typing import Optional, Tuple, Literal

# Type alias for document processing status (duplicated from models.py for standalone use)
DocumentStatus = Literal['uploading', 'chunking', 'embedding', 'complete', 'failed']

logger = logging.getLogger(__name__)

def _get_current_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return datetime.utcnow().isoformat() + "Z"

async def update_document_status(
    assistant_id: str,
    document_id: str,
    status: DocumentStatus,
    table_name: str,
    vector_store_id: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
    error_details: Optional[str] = None,
) -> bool:
    """
    Standalone version of update_document_status for ingestion pipeline.
    Does not depend on other app_api modules.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        logger.error("boto3 is required for DynamoDB operations")
        return False
    
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
        if error_message is not None:
            set_parts.append("errorMessage = :error_message")
            expression_attribute_values[":error_message"] = error_message
        if error_details is not None:
            set_parts.append("errorDetails = :error_details")
            expression_attribute_values[":error_details"] = error_details
    else:
        remove_attributes.extend(["errorMessage", "errorDetails"])
    
    # Build update expression
    update_expression_parts = []
    if set_parts:
        update_expression_parts.append("SET " + ", ".join(set_parts))
    if remove_attributes:
        update_expression_parts.append("REMOVE " + ", ".join(remove_attributes))
    
    update_expression = " ".join(update_expression_parts)
    
    try:
        update_params = {
            'Key': {
                'PK': f'AST#{assistant_id}',
                'SK': f'DOC#{document_id}'
            },
            'UpdateExpression': update_expression,
            'ExpressionAttributeValues': expression_attribute_values,
            'ReturnValues': 'NONE'
        }
        
        if expression_attribute_names:
            update_params['ExpressionAttributeNames'] = expression_attribute_names
        
        logger.info(f"Updating document status: {status}, chunk_count={chunk_count}, assistant_id={assistant_id}, document_id={document_id}")
        table.update_item(**update_params)
        logger.info(f"Successfully updated document status in DynamoDB")
        return True
    except Exception as e:
        logger.error(f"Failed to update document status in DynamoDB: {e}", exc_info=True)
        return False

def _format_error_message(exception: Exception) -> Tuple[str, str]:
    """
    Format exception into user-friendly message and technical details
    
    Args:
        exception: Exception that occurred during processing
    
    Returns:
        Tuple of (user_friendly_message, full_traceback_string)
    """
    exception_type = type(exception).__name__
    exception_message = str(exception)
    
    # Get full traceback for technical details
    technical_details = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
    
    # Map exception types to user-friendly messages
    user_message = "Processing failed - please try again or contact support"
    
    # Check for specific error patterns
    error_str_lower = exception_message.lower()
    
    if "unsupported file type" in error_str_lower or isinstance(exception, ValueError) and "unsupported" in error_str_lower:
        user_message = "Unable to process file - format not supported"
    elif "pdf" in error_str_lower and ("corrupt" in error_str_lower or "parse" in error_str_lower or "invalid" in error_str_lower):
        user_message = "Unable to process PDF - file may be corrupted or password-protected"
    elif "s3" in error_str_lower or "bucket" in error_str_lower or "access denied" in error_str_lower:
        user_message = "Unable to access file - please try uploading again"
    elif "bedrock" in error_str_lower or "embedding" in error_str_lower or "model" in error_str_lower:
        user_message = "Unable to generate embeddings - service temporarily unavailable"
    elif "timeout" in error_str_lower or "timed out" in error_str_lower:
        user_message = "Processing timed out - file may be too large, please try a smaller file"
    elif "memory" in error_str_lower or "out of memory" in error_str_lower:
        user_message = "File is too large to process - please try a smaller file"
    elif "permission" in error_str_lower or "forbidden" in error_str_lower:
        user_message = "Permission denied - please check your access rights"
    
    return user_message, technical_details


def get_table_name() -> Optional[str]:
    """
    Get ASSISTANTS_TABLE_NAME from environment
    Returns:
        Table name if found, None otherwise
    """
    return os.environ.get('ASSISTANTS_TABLE_NAME')


def create_status_manager(table_name: Optional[str] = None) -> 'DocumentStatusManager':
    """
    Factory function to create DocumentStatusManager instance
    
    Args:
        table_name: Optional table name (defaults to ASSISTANTS_TABLE_NAME env var)
    
    Returns:
        DocumentStatusManager instance
    """
    if not table_name:
        table_name = get_table_name()
    return DocumentStatusManager(table_name=table_name)


class DocumentStatusManager:
    """Centralized manager for document status updates during ingestion pipeline"""
    
    def __init__(self, table_name: Optional[str] = None):
        """
        Initialize status manager
        
        Args:
            table_name: DynamoDB table name (defaults to ASSISTANTS_TABLE_NAME env var)
        """
        self.table_name = table_name or get_table_name()
        if not self.table_name:
            logger.warning("ASSISTANTS_TABLE_NAME not set - status updates will fail")
    
    async def update_status(
        self,
        assistant_id: str,
        document_id: str,
        new_status: DocumentStatus,
        **kwargs
    ) -> bool:
        """
        Centralized status update method
        
        Args:
            assistant_id: Parent assistant identifier
            document_id: Document identifier
            new_status: New processing status
            **kwargs: Additional fields to update (chunk_count, vector_store_id, error_message, error_details)
        
        Returns:
            True if update succeeded, False otherwise
        """
        if not self.table_name:
            logger.error("Cannot update status: table name not configured")
            return False
        
        try:
            result = await update_document_status(
                assistant_id=assistant_id,
                document_id=document_id,
                status=new_status,
                table_name=self.table_name,
                **kwargs
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to update document status: {e}", exc_info=True)
            return False
    
    async def mark_chunking(self, assistant_id: str, document_id: str) -> bool:
        """
        Mark document as chunking (uploading -> chunking)
        
        Args:
            assistant_id: Parent assistant identifier
            document_id: Document identifier
        
        Returns:
            True if update succeeded, False otherwise
        """
        logger.info(f"Updating status to 'chunking' for document {document_id}")
        return await self.update_status(assistant_id, document_id, 'chunking')
    
    async def mark_embedding(
        self,
        assistant_id: str,
        document_id: str,
        chunk_count: int
    ) -> bool:
        """
        Mark document as embedding (chunking -> embedding) with chunk count
        
        Args:
            assistant_id: Parent assistant identifier
            document_id: Document identifier
            chunk_count: Number of chunks created
        
        Returns:
            True if update succeeded, False otherwise
        """
        logger.info(f"Updating status to 'embedding' for document {document_id} with {chunk_count} chunks")
        return await self.update_status(
            assistant_id,
            document_id,
            'embedding',
            chunk_count=chunk_count
        )
    
    async def mark_complete(
        self,
        assistant_id: str,
        document_id: str,
        vector_store_id: str
    ) -> bool:
        """
        Mark document as complete (embedding -> complete) with vector store ID
        
        Args:
            assistant_id: Parent assistant identifier
            document_id: Document identifier
            vector_store_id: S3 vector store identifier
        
        Returns:
            True if update succeeded, False otherwise
        """
        logger.info(f"Updating status to 'complete' for document {document_id}")
        return await self.update_status(
            assistant_id,
            document_id,
            'complete',
            vector_store_id=vector_store_id
        )
    
    async def mark_failed(
        self,
        assistant_id: str,
        document_id: str,
        exception: Exception
    ) -> bool:
        """
        Mark document as failed with formatted error messages
        
        Args:
            assistant_id: Parent assistant identifier
            document_id: Document identifier
            exception: Exception that caused the failure
        
        Returns:
            True if update succeeded, False otherwise
        """
        user_message, technical_details = _format_error_message(exception)
        logger.error(f"Updating status to 'failed' for document {document_id}: {user_message}")
        
        return await self.update_status(
            assistant_id,
            document_id,
            'failed',
            error_message=user_message,
            error_details=technical_details
        )


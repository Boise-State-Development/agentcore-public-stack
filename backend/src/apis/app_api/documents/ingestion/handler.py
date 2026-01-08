"""Lambda handler for document ingestion pipeline

Triggered by S3 event when document is uploaded.
Orchestrates the full processing pipeline.
"""

print("DEBUG: Starting handler.py")
import logging
print("DEBUG: Imported logging")
import json
print("DEBUG: Imported json")
import os
print("DEBUG: Imported os")
import asyncio
print("DEBUG: Imported asyncio")
from typing import Dict, Any, Optional

# Set environment variables for model caching BEFORE importing other modules
# This ensures we use the baked-in models and avoid read-only errors in Lambda
if os.environ.get('AWS_EXECUTION_ENV'):
    print("DEBUG: Setting environment variables")
    # DOCLING_ARTIFACTS_PATH is the CRITICAL variable for docling
    os.environ.setdefault('DOCLING_ARTIFACTS_PATH', '/opt/ml/models/docling-artifacts')
    # HF_HOME points to baked-in tokenizer for HybridChunker
    os.environ.setdefault('HF_HOME', '/opt/ml/models/huggingface')
    # Prevent docling/HybridChunker from trying to download models/tokenizers at runtime
    os.environ.setdefault('HF_HUB_OFFLINE', '1')

print("DEBUG: Finished top-level code")
logger = logging.getLogger(__name__)
if len(logging.getLogger().handlers) > 0:
    logging.getLogger().setLevel(logging.INFO)
else:
    logging.basicConfig(level=logging.INFO)


def _get_mime_type_from_extension(filename: str) -> Optional[str]:
    """
    Map file extension to MIME type as fallback when S3 ContentType is missing
    """
    ext = os.path.splitext(filename)[1].lower()
    extension_to_mime = {
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.doc': 'application/msword',
        '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        '.txt': 'text/plain',
        '.rtf': 'text/rtf',
        '.md': 'text/markdown',
    }
    return extension_to_mime.get(ext)

def _detect_mime_type(content_type: Optional[str], filename: str) -> str:
    """
    Detect MIME type from S3 ContentType or file extension
    """
    # First, try S3 ContentType (most reliable)
    if content_type:
        return content_type
    
    # Fallback to file extension
    mime_type = _get_mime_type_from_extension(filename)
    if mime_type:
        return mime_type
    
    # Last resort: default to text/plain
    logger.warning(f"Could not determine MIME type for {filename}, defaulting to text/plain")
    return 'text/plain'


# Load .env for local testing (Lambda won't have dotenv installed)
if not os.environ.get('AWS_EXECUTION_ENV'):  # Not in Lambda
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        env_path = Path(__file__).parent.parent.parent.parent.parent / '.env'
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
    except ImportError:
        pass  # dotenv not installed, running in Lambda


async def async_lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Async implementation of the Lambda handler
    """
    from status import create_status_manager
    
    # Initialize status manager
    status_manager = create_status_manager()
    
    try:
        logger.info(f"Received S3 event: {json.dumps(event)}")
        
        # 1. Parse event and extract metadata
        event_data = _parse_s3_event(event)
        
        # 2. Update status to 'chunking'
        logger.info("Starting document processing")
        await status_manager.mark_chunking(
            assistant_id=event_data['assistant_id'],
            document_id=event_data['document_id']
        )
        
        # 3. Download and process document
        await _process_document_pipeline(
            bucket=event_data['bucket'],
            key=event_data['key'],
            assistant_id=event_data['assistant_id'],
            document_id=event_data['document_id'],
            filename=event_data['filename'],
            status_manager=status_manager
        )
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Document processed successfully',
                'assistant_id': event_data['assistant_id'],
                'document_id': event_data['document_id'],
                'filename': event_data['filename']
            })
        }
        
    except Exception as e:
        logger.error(f"Error processing document: {e}", exc_info=True)
        
        # Update document status to 'failed' with error message
        try:
            # Re-parse event data safely to ensure we have IDs for status update
            event_data = _parse_s3_event(event)
            await status_manager.mark_failed(
                assistant_id=event_data['assistant_id'],
                document_id=event_data['document_id'],
                exception=e
            )
        except Exception as status_error:
            logger.error(f"Failed to update status to 'failed': {status_error}", exc_info=True)
        
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Synchronous entrypoint for AWS Lambda.
    Wraps the async handler using asyncio.run().
    """
    return asyncio.run(async_lambda_handler(event, context))


def _parse_s3_event(event: Dict[str, Any]) -> Dict[str, str]:
    """
    Parse S3 event to extract metadata
    """
    if not event.get('Records') or len(event['Records']) == 0:
        raise ValueError("Invalid S3 event: No Records found")
    
    record = event['Records'][0]
    s3_data = record.get('s3', {})
    
    bucket = s3_data.get('bucket', {}).get('name')
    object_data = s3_data.get('object', {})
    key = object_data.get('key')
    
    if not bucket or not key:
        raise ValueError(f"Invalid S3 event: Missing bucket or key. bucket={bucket}, key={key}")
    
    # Check if metadata is already in object (test event format)
    assistant_id = object_data.get('assistant_id')
    document_id = object_data.get('document_id')
    filename = object_data.get('filename')
    
    if assistant_id and document_id and filename:
        return {
            'bucket': bucket,
            'key': key,
            'assistant_id': assistant_id,
            'document_id': document_id,
            'filename': filename
        }
    
    # Real S3 event format - parse from key path
    key_parts = key.split('/')
    
    if key_parts[0] == 'assistants' and len(key_parts) >= 5:
        assistant_id = key_parts[1]
        document_id = key_parts[3]
        filename = '/'.join(key_parts[4:])
    elif len(key_parts) >= 4:
        assistant_id = key_parts[1] if len(key_parts) >= 2 else None
        document_id = key_parts[2] if len(key_parts) >= 3 else None
        filename = '/'.join(key_parts[3:]) if len(key_parts) >= 4 else None
    else:
        raise ValueError(f"Unable to parse S3 key: {key}")
    
    if not assistant_id or not document_id or not filename:
        raise ValueError(f"Missing metadata in S3 key: assistant_id={assistant_id}, document_id={document_id}")
    
    return {
        'bucket': bucket,
        'key': key,
        'assistant_id': assistant_id,
        'document_id': document_id,
        'filename': filename
    }


async def _process_document_pipeline(
    bucket: str,
    key: str,
    assistant_id: str,
    document_id: str,
    filename: str,
    status_manager
) -> None:
    """
    Execute the full document processing pipeline
    """
    import boto3
    from processors import (
        process_with_docling,
        is_docling_supported
    )
    from embeddings.bedrock_embeddings import generate_embeddings, store_embeddings_in_s3, test_s3vector_dump
    
    # 1. Download document from S3
    s3_client = boto3.client('s3')
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        file_content = response['Body'].read()
        content_type = response.get('ContentType')
    except Exception as e:
        logger.error(f"Failed to download S3 object {bucket}/{key}: {str(e)}")
        raise e
    
    # 1.5. Validate file size (fail fast before expensive processing)
    MAX_FILE_SIZE_MB = 20  # Adjust based on acceptable processing time
    file_size_mb = len(file_content) / (1024 * 1024)
    
    if file_size_mb > MAX_FILE_SIZE_MB:
        error_msg = f"File too large ({file_size_mb:.1f}MB). Maximum size is {MAX_FILE_SIZE_MB}MB. Large files may take 5+ minutes to process."
        logger.error(error_msg)
        raise ValueError(error_msg)
    
    logger.info(f"File size: {file_size_mb:.2f}MB")
    
    # 2. Detect file type
    mime_type = _detect_mime_type(content_type, filename)
    logger.info(f"MIME type detected: {mime_type}")
    
    if not is_docling_supported(mime_type, filename):
        raise ValueError(f"Unsupported file type: {mime_type}. Docling does not support this format.")
    
    # 3. Process AND Chunk via Docling
    # We await the result which is a List[str] (the chunks)
    chunks = await process_with_docling(file_content, mime_type, filename)
    
    if not chunks:
        raise ValueError(f"Docling produced zero chunks for file: {filename}")

    logger.info(f"Docling produced {len(chunks)} layout-aware chunks. Skipping recursive splitter.")
    
    # Update status to 'embedding' with chunk count
    await status_manager.mark_embedding(
        assistant_id=assistant_id,
        document_id=document_id,
        chunk_count=len(chunks)
    )
    
    # 4. Generate embeddings (Pass the Docling chunks directly)
    embeddings = await generate_embeddings(chunks)
    logger.info(f"Embeddings generated for {len(embeddings)} chunks")
    
    # 5. Store in vector store
    await store_embeddings_in_s3(assistant_id, document_id, chunks, embeddings, {'filename': filename})
    
    # Get vector store identifier
    vector_store_id = os.environ.get('VECTOR_STORE_INDEX_NAME', 'assistants-index')
    
    # Update status to 'complete'
    await status_manager.mark_complete(
        assistant_id=assistant_id,
        document_id=document_id,
        vector_store_id=vector_store_id
    )
    logger.info("Embeddings stored, processing complete")
    
    # Test s3vector dump (Optional debugging)
    # await test_s3vector_dump()
"""Document ingestion pipeline for Lambda processing

This module is deployed as a standalone Lambda function that processes
documents uploaded to S3.

Lambda Trigger: S3 event notification on object creation
Processing Flow:
1. Extract text from document (PDF, DOCX, TXT, etc.)
2. Chunk text into semantic segments
3. Generate embeddings using Bedrock
4. Store embeddings in S3 vector store
5. Update document status in DynamoDB
"""

import logging
import tempfile
import os
from pathlib import Path
import shutil
from typing import Optional, List, Union
logger = logging.getLogger(__name__)

# Set environment variables for model caching and performance
# We do this conditionally, but since we set these in the Dockerfile, 
# this acts as a failsafe for local testing vs Lambda.
if os.environ.get('AWS_EXECUTION_ENV'):
    os.environ.setdefault('DOCLING_ARTIFACTS_PATH', '/opt/ml/models/docling-artifacts')
    os.environ.setdefault('HF_HOME', '/opt/ml/models/huggingface')
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('USE_NNPACK', '0')
    
def _ensure_tiktoken_cache():
    """
    Copy baked tiktoken files from Read-Only /opt to Writable /tmp
    This prevents the 'Read-only file system' error.
    """
    # Where we baked them in Docker
    source_dir = Path("/opt/ml/models/tiktoken_cache")
    # Where tiktoken is looking (from ENV var)
    target_dir = Path("/tmp/tiktoken_cache")
    
    if not target_dir.exists():
        if source_dir.exists():
            logger.info(f"Copying tiktoken cache from {source_dir} to {target_dir}")
            shutil.copytree(source_dir, target_dir)
        else:
            logger.warning(f"Baked tiktoken cache not found at {source_dir}. Library may try to download (and fail offline).")


DOCLING_SUPPORTED_MIME_TYPES = {
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/msword',
    'application/vnd.oasis.opendocument.text',
    'text/plain', 'text/rtf', 'text/markdown'
}

DOCLING_SUPPORTED_EXTENSIONS = {
    '.pdf', '.docx', '.pptx', '.doc', '.odt',
    '.txt', '.md', '.rtf', '.markdown'
}

def _get_file_extension(filename: Optional[str], mime_type: str) -> str:
    # (Your existing logic is fine here)
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in DOCLING_SUPPORTED_EXTENSIONS:
            return ext
    
    mime_to_ext = {
        'application/pdf': '.pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
        'application/msword': '.doc',
        'application/vnd.oasis.opendocument.text': '.odt',
        'text/plain': '.txt',
        'text/markdown': '.md',
        'text/rtf': '.rtf',
    }
    return mime_to_ext.get(mime_type, '.txt')

# Changed return type hint to allow returning list of chunks (Preferred) 
# or string (Legacy)
async def process_with_docling(file_bytes: bytes, mime_type: str, filename: Optional[str] = None) -> List[str]:
    """
    Extract and chunk text using Docling.
    Returns: A LIST of text chunks (preserving semantic boundaries).
    """
    
    logger.info(f"Docling processor initialized...starting to process document...")
    
    # Import inside function to avoid heavy load at cold start if not needed immediately
    import torch
    torch.backends.nnpack.enabled = False
    
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableStructureOptions
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
    from docling.datamodel.base_models import InputFormat
    import tiktoken
    
    ext = _get_file_extension(filename, mime_type)
    
    logger.info(f"Docling processor initialized with torch.backends.nnpack.enabled = {torch.backends.nnpack.enabled}")
    
    # Create a temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
        try:
            tmp_file.write(file_bytes)
            tmp_file.flush()
            tmp_file_path = tmp_file.name
            
            if mime_type == 'application/pdf' or tmp_file_path.lower().endswith('.pdf'):
                logger.info(f"Using PDF specific options...")
                pipeline_options = PdfPipelineOptions(
                    do_ocr=False,  # Disable OCR
                    do_table_structure=False,  # Disable table structure detection
                    generate_page_images=False,  # Don't generate page images
                    images_scale=0.5
                )
                
                # Create converter with PDF-specific options
                converter = DocumentConverter(
                    allowed_formats=[InputFormat.PDF],
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
                    }
                )
                logger.info(f"PDF specific options configured: OCR disabled, table structure disabled")
            else:
                logger.info(f"Using standard DocumentConverter for {mime_type}")
                converter = DocumentConverter()

            logger.info(f"Converting document {filename or 'temp'}...")
            
            # NOW this works because both branches created a 'DocumentConverter'
            result = converter.convert(tmp_file_path)
            dl_doc = result.document
            
            # Different formats have different structures
            page_count = len(dl_doc.pages) if dl_doc.pages else 0
            if page_count > 0:
                logger.info(f"Document converted successfully. Pages: {page_count}")
            else:
                # DOCX, TXT, etc. don't have explicit pages
                logger.info(f"Document converted successfully. Format: {mime_type}")
            
            _ensure_tiktoken_cache()
            enc = tiktoken.get_encoding("cl100k_base")
            
            tokenizer = OpenAITokenizer(
                tokenizer=enc,
                max_tokens=8192
            )

            chunker = HybridChunker(
                tokenizer=tokenizer,
                max_tokens=1024,
                merge_peers=True
            )
            
            logger.info(f"Starting chunking process...")
            chunk_iter = chunker.chunk(dl_doc=dl_doc)
            
            text_parts = []
            chunk_count = 0
            for chunk in chunk_iter:
                enriched_text = chunker.contextualize(chunk=chunk)
                if enriched_text:
                    text_parts.append(enriched_text)
                    chunk_count += 1
                    # Log progress every 10 chunks to avoid excessive logging
                    if chunk_count % 10 == 0:
                        logger.info(f"Processed {chunk_count} chunks so far...")
            
            logger.info(f"Chunking complete. Total chunks created: {len(text_parts)}")
            
            if not text_parts:
                logger.warning(f"No text extracted from {mime_type}")
                return []
            
            return text_parts 
            
        except Exception as e:
            logger.error(f"Docling processing failed: {str(e)}")
            raise e
        finally:
            if os.path.exists(tmp_file_path):
                os.unlink(tmp_file_path)

def is_docling_supported(mime_type: str, filename: Optional[str] = None) -> bool:
    # (Your existing logic is correct)
    if mime_type in DOCLING_SUPPORTED_MIME_TYPES:
        return True
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        return ext in DOCLING_SUPPORTED_EXTENSIONS
    return False
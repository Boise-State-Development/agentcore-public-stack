"""`.sql` files are plain text for knowledge-base ingestion.

Browsers label them application/sql, application/x-sql or nothing, and the upload
stores that label as the S3 ContentType. Docling knows none of them.
"""

import pytest

from apis.app_api.documents.ingestion import handler as handler_module
from apis.app_api.documents.ingestion.processors import docling_processor


@pytest.mark.parametrize("reported", ["application/sql", "application/x-sql", "application/octet-stream", "", None])
def test_sql_resolves_to_text_plain_whatever_the_browser_reported(reported):
    assert handler_module._detect_mime_type(reported, "schema.sql") == "text/plain"
    assert handler_module._detect_mime_type(reported, "SCHEMA.SQL") == "text/plain"


def test_other_files_still_trust_the_s3_content_type():
    assert handler_module._detect_mime_type("text/csv", "data.csv") == "text/csv"


@pytest.mark.parametrize("mime", ["application/sql", "text/plain", "application/octet-stream"])
def test_docling_accepts_sql(mime):
    assert docling_processor.is_docling_supported(mime, "schema.sql")
    assert ".sql" in docling_processor.DOCLING_SUPPORTED_EXTENSIONS

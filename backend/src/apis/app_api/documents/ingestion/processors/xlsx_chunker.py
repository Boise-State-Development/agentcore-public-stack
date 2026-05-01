"""XLSX-specific chunker for RAG ingestion.

Converts each sheet in an Excel workbook to CSV, then delegates to the
existing row-based CSV chunker. This avoids Docling's slow and
memory-intensive table parsing while preserving header-per-chunk structure
that produces better embeddings for tabular data.
"""

import csv
import io
import logging
from typing import List

from openpyxl import load_workbook

from .csv_chunker import chunk_csv

logger = logging.getLogger(__name__)


def chunk_xlsx(file_bytes: bytes, max_tokens: int = 900) -> List[str]:
    """
    Chunk an XLSX file by converting each sheet to CSV and chunking rows.

    Each sheet is processed independently. The sheet name is prepended as
    context to every chunk from that sheet so the embedding captures which
    sheet the data belongs to.

    Args:
        file_bytes: Raw XLSX file content.
        max_tokens: Maximum token count per chunk (passed to chunk_csv).

    Returns:
        List of text chunks across all sheets.
    """
    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    all_chunks: List[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        # Convert sheet rows to properly-quoted CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            if all(cell is None for cell in row):
                continue
            writer.writerow([str(cell) if cell is not None else "" for cell in row])
            row_count += 1

        if row_count == 0:
            logger.info(f"Sheet '{sheet_name}' is empty, skipping")
            continue

        csv_bytes = buf.getvalue().encode("utf-8")
        sheet_chunks = chunk_csv(csv_bytes, max_tokens=max_tokens)

        # Prepend sheet name for multi-sheet context
        if len(wb.sheetnames) > 1:
            sheet_chunks = [f"Sheet: {sheet_name}\n{chunk}" for chunk in sheet_chunks]

        logger.info(f"Sheet '{sheet_name}': {row_count} rows -> {len(sheet_chunks)} chunks")
        all_chunks.extend(sheet_chunks)

    wb.close()
    logger.info(f"XLSX chunked into {len(all_chunks)} total chunks across {len(wb.sheetnames)} sheets")
    return all_chunks

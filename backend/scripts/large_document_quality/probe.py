"""The free check: can each tier *reach* the planted fact at all? No model spend.

* **Tier 1:** the production ``pattern`` read (``read_document``) with the
  question's locator, the phrase a model would plausibly search for. ``located``
  when a match lands on the planted page; ``value`` when an expected value is in
  what that read returned (the matched lines and their context). On a scanned
  document both are false by construction: there is no text layer.
* **Tier 2:** the production search (``search_attachments``, one ``Retrieve``)
  with the question as asked. ``located`` when an excerpt comes from the planted
  page; ``value`` when an expected value is in an excerpt.

This bounds what the paid ``ask`` arms can do, and costs about $0.001 per
``Retrieve``. A Word document has no pages, so only ``value`` applies to it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from compaction_quality.score import contains_any

from .fixtures import Question


async def probe_question(question: Question, doc: Dict[str, Any]) -> Dict[str, Any]:
    from apis.shared.files.attachment_search import search_attachments
    from apis.shared.files.document_read import read_document

    row: Dict[str, Any] = {"qid": question.qid, "doc": question.doc, "wording": question.wording}

    result = await read_document(doc["userId"], doc["sessionId"], doc["uploadId"], pattern=question.locator)
    matches = result.payload.get("matches") or []
    pages = {m.get("page") for m in matches}
    row["tier1"] = {
        "matches": len(matches),
        "located": question.page in pages if question.page else bool(matches),
        "value": contains_any(json.dumps(matches), question.expected),
    }

    try:
        hits = await search_attachments(doc["userId"], doc["sessionId"], question.question, upload_id=doc["uploadId"])
    except Exception as exc:  # noqa: BLE001 - recorded, not fatal
        row["tier2"] = {"error": type(exc).__name__}
        return row
    row["tier2"] = {
        "hits": len(hits),
        "pages": [h.page for h in hits],
        "located": question.page in {h.page for h in hits} if question.page else None,
        "value": contains_any(" ".join(h.text for h in hits), question.expected),
    }
    return row


async def probe(questions: List[Question], seeded: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for question in questions:
        doc = seeded.get(question.doc)
        if doc:
            rows.append(await probe_question(question, doc))
    return rows

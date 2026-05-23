"""User-facing web-crawl ingestion for assistant knowledge.

A `web_source` is the simplest possible "file source": no OAuth provider, no
adapter — the user supplies a URL and (optionally) a small set of crawl
parameters, and the backend fetches the page(s) and stages markdown into the
documents bucket. From there the existing S3-event ingestion Lambda drives
chunking/embedding exactly as a device upload would.

See `crawler.py` for the BFS itself; `routes.py` for the public surface.
"""

"""Conversation search's read path (``docs/specs/conversation-search.md`` §5).

``session_rows`` (the lexical leg and the join, both under ``USER#{caller}``),
``index_search`` (the only code that queries the shared ``conversations``
knowledge base) and ``service`` (both legs and the merge).

The text helpers both sides share (normalizing, snippets) live in
``apis.shared.sessions.search_text``, beside the title write that uses them.
"""

"""Context-bound `save_conversation` agent tool.

The conversational surface of the export-target feature: lets the agent save
the current conversation transcript out to the user's connected app (Google
Drive) mid-chat, reusing the export core in `apis.shared.export_targets` and
the existing `oauth_required` consent gate. Factory-produced per request with
the session/user/connector context, like the artifact + spreadsheet tools.
"""

from .tools import make_save_conversation_tool

__all__ = ["make_save_conversation_tool"]

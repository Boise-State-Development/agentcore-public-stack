"""Scenario user classes.

``chat`` is the primary, expensive path. ``readonly`` is the cheap control.
"""

from .chat import ChatConversationUser
from .readonly import BrowsingUser

__all__ = ["BrowsingUser", "ChatConversationUser"]

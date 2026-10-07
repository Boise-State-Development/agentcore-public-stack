"""Conversation-share storage shared by app-api and the AgentCore Runtime.

app-api owns the share surface (``apis.app_api.shares``). The Runtime only
reads project shares, for a project harness's ``shared_task_read`` tool
(Shared Projects 2.5c), so the snapshot store and the read helpers live here.
"""

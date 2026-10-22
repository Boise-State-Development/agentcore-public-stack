"""Default entry point — the chat path.

    locust -f locustfile.py --host https://chat.example.edu/api

Every simulated turn spends real Bedrock tokens against a real user's quota.
Read tests/load/README.md before running this against anything.

For the free read-only scenario use `locustfile_readonly.py` instead. The two
are separate files on purpose: Locust runs every user class it finds in a
locustfile, so keeping them together would make "I just wanted the cheap test"
an expensive mistake.
"""

from agentcore_load.scenarios.chat import ChatConversationUser

__all__ = ["ChatConversationUser"]

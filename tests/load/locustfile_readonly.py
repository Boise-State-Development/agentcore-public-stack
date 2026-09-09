"""Read-only entry point — authenticated GETs, no inference, no Bedrock spend.

    locust -f locustfile_readonly.py --host https://chat.example.edu/api

Use this to load-test the BFF layer (ALB, Fargate, session middleware,
DynamoDB) in isolation, and to establish how many concurrent signed-in users
app-api sustains before any model is involved.
"""

from agentcore_load.scenarios.readonly import BrowsingUser

__all__ = ["BrowsingUser"]

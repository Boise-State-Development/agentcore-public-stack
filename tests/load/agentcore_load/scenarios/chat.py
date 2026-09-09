"""Primary scenario: a signed-in user holding a multi-turn conversation.

This is the expensive path and the one that matters. Every turn traverses
CloudFront -> ALB -> app-api (Fargate) -> inference-api on the AgentCore
Runtime, and app-api holds an httpx connection open relaying SSE for the whole
turn. Concurrency here is bounded by that held-open connection rather than by
request rate, which is why a modest number of simulated users can be a
meaningful test and why raw RPS is the wrong thing to watch.

WARNING: every turn spends real Bedrock tokens.
"""

from __future__ import annotations

import random

from locust import between, task

from ..users import ChatUser


class ChatConversationUser(ChatUser):
    """Logs in, then works through conversations turn by turn.

    Turns within a conversation reuse one ``session_id``, so history grows and
    each turn carries a larger prompt than the last — the same cost curve real
    conversations have, and the thing prompt caching exists to blunt. A test
    that sent every message in a fresh session would understate input tokens
    and never exercise the cache at all.
    """

    # Reading a reply then typing the next message. Short enough to keep
    # pressure on, long enough that the run is not a synthetic hammer.
    wait_time = between(5, 15)

    @task
    def hold_conversation(self) -> None:
        config = self.load_config
        if config is None:  # on_start stopped this user
            return

        session_id = self.new_conversation_id()
        prompts = random.sample(
            config.prompts,
            k=min(config.turns_per_conversation, len(config.prompts)),
        )

        for prompt in prompts:
            result = self.chat_turn(session_id, prompt)
            if result is None:
                # The failure is already recorded. Abandoning the conversation
                # rather than pressing on is deliberate: once a turn fails the
                # session's history is in an unknown state, and continuing
                # would attribute later failures to the wrong cause.
                return
            self.wait()

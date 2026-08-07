"""Conversation state tests.

No Textual and no HTTP — the point of extracting this layer is that it can be
tested directly, so these run in milliseconds and will keep working when the
chat UI is rebuilt around them.
"""

from __future__ import annotations

from agentcore_tui.conversation import ConversationStore, Message, new_session_id
from agentcore_tui.usage import Usage


class TestSessionId:
    def test_ids_are_unique(self) -> None:
        assert new_session_id() != new_session_id()

    def test_a_store_has_an_id_before_any_request(self) -> None:
        """Session ids are client-minted; there is no create-session endpoint."""
        assert ConversationStore().session_id

    def test_an_explicit_id_is_honoured(self) -> None:
        """Opening a stored conversation reuses its id rather than minting one."""
        assert ConversationStore(session_id="existing-id").session_id == "existing-id"


class TestMessage:
    def test_user_and_assistant_constructors_set_the_role(self) -> None:
        assert Message.user("hi").role == "user"
        assert Message.assistant("there").role == "assistant"

    def test_each_message_gets_its_own_id(self) -> None:
        assert Message.user("a").id != Message.user("a").id

    def test_messages_are_timestamped(self) -> None:
        assert Message.user("a").created_at > 0

    def test_usage_can_be_attached_after_the_fact(self) -> None:
        usage = Usage(input_tokens=1, output_tokens=2)
        assert Message.assistant("x").with_usage(usage).usage == usage

    def test_reasoning_is_kept_separate_from_content(self) -> None:
        message = Message.assistant("answer", reasoning="thinking")
        assert message.content == "answer"
        assert message.reasoning == "thinking"


class TestStoreContents:
    def test_starts_empty(self) -> None:
        store = ConversationStore()
        assert store.is_empty
        assert len(store) == 0
        assert store.messages == ()

    def test_append_records_in_order(self) -> None:
        store = ConversationStore()
        store.append_user("first")
        store.append_assistant("second")
        assert [m.content for m in store] == ["first", "second"]

    def test_messages_view_is_a_copy(self) -> None:
        """Callers must not be able to mutate the store through the view."""
        store = ConversationStore()
        store.append_user("one")
        snapshot = store.messages
        store.append_user("two")
        assert len(snapshot) == 1
        assert len(store.messages) == 2

    def test_replace_all_loads_history_wholesale(self) -> None:
        store = ConversationStore()
        store.replace_all([Message.user("a"), Message.assistant("b")])
        assert [m.role for m in store] == ["user", "assistant"]


class TestStoreDerived:
    def test_turns_counts_user_messages(self) -> None:
        store = ConversationStore()
        store.append_user("q1")
        store.append_assistant("a1")
        store.append_user("q2")
        assert store.turns == 2

    def test_last_assistant_skips_trailing_user_messages(self) -> None:
        store = ConversationStore()
        store.append_assistant("answer")
        store.append_user("follow-up")
        assert store.last_assistant is not None
        assert store.last_assistant.content == "answer"

    def test_last_assistant_is_none_when_there_is_none(self) -> None:
        store = ConversationStore()
        store.append_user("q")
        assert store.last_assistant is None

    def test_latest_usage_comes_from_the_last_answer(self) -> None:
        store = ConversationStore()
        store.append_assistant("old", usage=Usage(input_tokens=1))
        store.append_user("q")
        store.append_assistant("new", usage=Usage(input_tokens=9))
        assert store.latest_usage is not None
        assert store.latest_usage.input_tokens == 9

    def test_markdown_export_labels_both_roles(self) -> None:
        store = ConversationStore()
        store.append_user("a question")
        store.append_assistant("an answer")
        markdown = store.to_markdown()
        assert "## You" in markdown
        assert "## Assistant" in markdown
        assert "a question" in markdown
        assert "an answer" in markdown


class TestStoreLifecycle:
    def test_reset_clears_messages_and_title(self) -> None:
        store = ConversationStore()
        store.append_user("hi")
        store.set_title("Some title")
        store.reset()
        assert store.is_empty
        assert store.title is None

    def test_reset_mints_a_new_session_id(self) -> None:
        """Reusing the id would append to a conversation already persisted
        server-side under it."""
        store = ConversationStore()
        first = store.session_id
        second = store.reset()
        assert second != first
        assert store.session_id == second

    def test_title_can_be_set_outside_a_turn(self) -> None:
        """Titles arrive on an SSE event that can land after the terminal one."""
        store = ConversationStore()
        store.set_title("Generated later")
        assert store.title == "Generated later"

    def test_an_empty_title_is_normalised_to_none(self) -> None:
        store = ConversationStore(title="something")
        store.set_title("")
        assert store.title is None

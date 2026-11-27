"""``chat/prepare.py``: the request allowlist, the outcome body and the in-flight record.

The route-level behaviour (what a prepare skips, what it builds) is in
``test_invocations_prepare.py``; this pins the parts that need no route.
"""

from __future__ import annotations

import asyncio

import pytest

from apis.inference_api.chat import prepare as prepare_mode
from apis.inference_api.chat.models import InvocationRequest


@pytest.fixture(autouse=True)
def _reset():
    prepare_mode.reset_for_tests()
    yield
    prepare_mode.reset_for_tests()


class TestAllowlist:
    def test_keeps_only_the_selection_fields(self):
        request = InvocationRequest(
            session_id="s1",
            warm=True,
            prepare=True,
            model_id="m1",
            enabled_tools=["web_search"],
            enabled_skills=["sk"],
            rag_assistant_id="ast",
            message="a real turn",
            file_upload_ids=["u1"],
            interrupt_responses=[{"interruptId": "i", "response": {}}],
            continue_truncated=True,
            deliveries=[{"deliveryId": "d1"}],
            handoff_context={"handoffId": "h"},
            steering=[{"id": "e1", "text": "x"}],
        )

        reduced = prepare_mode.prepare_request(request)

        assert reduced.session_id == "s1"
        assert reduced.model_id == "m1"
        assert reduced.enabled_tools == ["web_search"]
        assert reduced.enabled_skills == ["sk"]
        assert reduced.rag_assistant_id == "ast"
        for dropped in (
            "message", "file_upload_ids", "interrupt_responses", "continue_truncated",
            "app_tool_call", "app_context_update", "deliveries", "handoff_context", "steering",
            "warm", "prepare", "handoff_brief", "workflow_prompt",
        ):
            assert not getattr(reduced, dropped), dropped

    def test_the_allowlist_names_no_turn_driving_field(self):
        assert not set(prepare_mode.PREPARE_FIELDS) & {
            "message", "file_upload_ids", "interrupt_responses", "continue_truncated",
            "app_tool_call", "app_context_update", "deliveries", "handoff_context", "steering",
            "folder_attachment_ids", "handoff_brief", "workflow_prompt", "client_tools", "run_mode",
        }

    def test_every_allowlisted_field_exists_on_the_request(self):
        assert set(prepare_mode.PREPARE_FIELDS) <= set(InvocationRequest.model_fields)

    def test_the_reduced_request_is_never_a_resume_continuation_or_dispatch(self):
        reduced = prepare_mode.prepare_request(
            InvocationRequest(
                session_id="s1", warm=True, prepare=True, interrupt_responses=[{"interruptId": "i", "response": {}}],
                continue_truncated=True,
            )
        )
        assert not reduced.interrupt_responses
        assert not reduced.continue_truncated
        assert reduced.app_tool_call is None
        assert reduced.app_context_update is None


class TestBody:
    def test_documented_shape(self):
        body = prepare_mode.PrepareOutcome(True, cache_hit=True, stages={"x": 1}).body(42)

        assert body == {"warmed": True, "prepared": True, "reason": None, "cacheHit": True, "ms": 42}

    def test_a_refusal_carries_its_reason_and_no_stages(self):
        body = prepare_mode.PrepareOutcome(False, "paused", stages={"x": 1}).body(1)

        assert body["prepared"] is False and body["reason"] == "paused"
        assert "stages" not in body


class TestInFlight:
    def test_begin_twice_refuses_the_second(self):
        async def go():
            assert prepare_mode.begin("u", "s") is True
            assert prepare_mode.begin("u", "s") is False

        asyncio.run(go())

    def test_keys_are_user_and_session(self):
        async def go():
            assert prepare_mode.begin("alice", "s") is True
            assert prepare_mode.begin("bob", "s") is True
            assert prepare_mode.begin("alice", "other") is True
            assert prepare_mode.begin("alice", "s") is False

        asyncio.run(go())

    def test_finish_allows_a_new_begin(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=False)
            assert prepare_mode.begin("u", "s") is True

        asyncio.run(go())

    def test_finish_wakes_a_waiter(self):
        async def go():
            prepare_mode.begin("u", "s")
            woke = asyncio.Event()

            async def waiter():
                await prepare_mode.wait_for_in_flight("u", "s")
                woke.set()

            task = asyncio.create_task(waiter())
            await asyncio.sleep(0.01)
            assert not woke.is_set()
            prepare_mode.finish("u", "s", prepared=True)
            await asyncio.wait_for(task, timeout=1)
            assert woke.is_set()

        asyncio.run(go())

    def test_wait_returns_at_once_with_nothing_in_flight(self):
        async def go():
            await asyncio.wait_for(prepare_mode.wait_for_in_flight("u", "s"), timeout=0.2)

        asyncio.run(go())

    def test_wait_does_not_block_on_another_users_prepare(self):
        async def go():
            prepare_mode.begin("alice", "s")
            await asyncio.wait_for(prepare_mode.wait_for_in_flight("bob", "s"), timeout=0.2)

        asyncio.run(go())

    def test_finish_with_nothing_in_flight_never_raises(self):
        prepare_mode.finish("u", "s", prepared=False)
        prepare_mode.finish("u", "s", prepared=True)

    def test_a_waiter_cancelled_from_outside_still_sees_its_cancellation(self):
        """The wait must not swallow a real cancellation (the turn's own stream being torn down)."""

        async def go():
            prepare_mode.begin("u", "s")
            task = asyncio.create_task(prepare_mode.wait_for_in_flight("u", "s"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            prepare_mode.finish("u", "s", prepared=False)

        asyncio.run(go())

    def test_a_failed_future_does_not_fail_the_waiter(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode._in_flight[("u", "s")].set_exception(RuntimeError("boom"))
            await asyncio.wait_for(prepare_mode.wait_for_in_flight("u", "s"), timeout=0.5)

        asyncio.run(go())

    def test_a_waiter_cancelled_from_outside_does_not_cancel_the_prepare(self):
        async def go():
            prepare_mode.begin("u", "s")
            task = asyncio.create_task(prepare_mode.wait_for_in_flight("u", "s"))
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            future = prepare_mode._in_flight[("u", "s")]
            assert not future.done()
            prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())

    def test_the_wait_is_bounded(self, monkeypatch):
        monkeypatch.setattr(prepare_mode, "IN_FLIGHT_WAIT_SECONDS", 0.05)

        async def go():
            prepare_mode.begin("u", "s")
            await asyncio.wait_for(prepare_mode.wait_for_in_flight("u", "s"), timeout=1)

        asyncio.run(go())


class TestPreparedAge:
    def test_none_without_a_finished_prepare(self):
        assert prepare_mode.prepared_age_ms("u", "s") is None

    def test_none_after_a_prepare_that_built_nothing(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=False)

        asyncio.run(go())
        assert prepare_mode.prepared_age_ms("u", "s") is None

    def test_a_non_negative_int_after_a_successful_one(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())
        age = prepare_mode.prepared_age_ms("u", "s")
        assert isinstance(age, int) and age >= 0

    def test_is_per_user(self):
        async def go():
            prepare_mode.begin("alice", "s")
            prepare_mode.finish("alice", "s", prepared=True)

        asyncio.run(go())
        assert prepare_mode.prepared_age_ms("bob", "s") is None

    def test_grows_with_the_clock(self, monkeypatch):
        now = {"t": 100.0}
        monkeypatch.setattr(prepare_mode.time, "monotonic", lambda: now["t"])

        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())
        now["t"] = 100.25
        assert prepare_mode.prepared_age_ms("u", "s") == 250

    def test_the_completed_map_stays_bounded_and_drops_the_oldest(self):
        async def go():
            for i in range(prepare_mode._COMPLETED_MAX + 20):
                prepare_mode.begin("u", f"s{i}")
                prepare_mode.finish("u", f"s{i}", prepared=True)

        asyncio.run(go())

        assert len(prepare_mode._completed_at) <= prepare_mode._COMPLETED_MAX
        assert prepare_mode.prepared_age_ms("u", "s0") is None
        assert prepare_mode.prepared_age_ms("u", f"s{prepare_mode._COMPLETED_MAX + 19}") is not None

    def test_refinishing_a_conversation_does_not_grow_the_map(self):
        async def go():
            for _ in range(5):
                prepare_mode.begin("u", "s")
                prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())
        assert len(prepare_mode._completed_at) == 1


class TestArm:
    def test_none_without_a_note(self):
        assert prepare_mode.arm_of("u", "s") is None

    def test_note_and_read(self):
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_HOLDOUT)
        assert prepare_mode.arm_of("u", "s") == "holdout"

    def test_is_per_user_and_session(self):
        prepare_mode.note_arm("alice", "s", prepare_mode.ARM_HOLDOUT)
        assert prepare_mode.arm_of("bob", "s") is None
        assert prepare_mode.arm_of("alice", "other") is None

    def test_an_unknown_arm_is_ignored(self):
        prepare_mode.note_arm("u", "s", "bogus")
        assert prepare_mode.arm_of("u", "s") is None
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_TREATED)
        prepare_mode.note_arm("u", "s", "bogus")
        assert prepare_mode.arm_of("u", "s") == "treated"

    def test_a_later_note_replaces_an_earlier_one(self):
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_HOLDOUT)
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_TREATED)
        assert prepare_mode.arm_of("u", "s") == "treated"
        assert len(prepare_mode._arms) == 1

    def test_the_map_stays_bounded(self):
        for i in range(prepare_mode._COMPLETED_MAX + 20):
            prepare_mode.note_arm("u", f"s{i}", prepare_mode.ARM_HOLDOUT)
        assert len(prepare_mode._arms) <= prepare_mode._COMPLETED_MAX
        assert prepare_mode.arm_of("u", "s0") is None
        assert prepare_mode.arm_of("u", f"s{prepare_mode._COMPLETED_MAX + 19}") == "holdout"

    def test_begin_records_treated_even_if_the_prepare_fails(self):
        async def go():
            assert prepare_mode.begin("u", "s") is True
            prepare_mode.finish("u", "s", prepared=False)

        asyncio.run(go())
        assert prepare_mode.arm_of("u", "s") == "treated"

    def test_a_refused_begin_does_not_change_the_arm(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.note_arm("u", "s", prepare_mode.ARM_HOLDOUT)
            assert prepare_mode.begin("u", "s") is False

        asyncio.run(go())
        assert prepare_mode.arm_of("u", "s") == "holdout"

    def test_reset_clears_arms(self):
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_HOLDOUT)
        prepare_mode.reset_for_tests()
        assert prepare_mode.arm_of("u", "s") is None

    def test_a_claim_returns_the_age_and_the_arm_once(self, monkeypatch):
        now = {"t": 100.0}
        monkeypatch.setattr(prepare_mode.time, "monotonic", lambda: now["t"])

        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())
        now["t"] = 100.5

        assert prepare_mode.claim_for_turn("u", "s") == (500, "treated")
        assert prepare_mode.claim_for_turn("u", "s") == (None, None)
        assert prepare_mode._arms == {} and prepare_mode._completed_at == {}

    def test_a_claim_of_a_holdout_has_an_arm_and_no_age(self):
        prepare_mode.note_arm("u", "s", prepare_mode.ARM_HOLDOUT)

        assert prepare_mode.claim_for_turn("u", "s") == (None, "holdout")
        assert prepare_mode.claim_for_turn("u", "s") == (None, None)

    def test_a_claim_without_a_prepare_is_empty(self):
        assert prepare_mode.claim_for_turn("u", "s") == (None, None)

    def test_a_claim_takes_only_its_own_conversation(self):
        prepare_mode.note_arm("alice", "s", prepare_mode.ARM_HOLDOUT)
        prepare_mode.note_arm("alice", "other", prepare_mode.ARM_TREATED)

        assert prepare_mode.claim_for_turn("bob", "s") == (None, None)
        assert prepare_mode.claim_for_turn("alice", "s") == (None, "holdout")
        assert prepare_mode.arm_of("alice", "other") == "treated"

    def test_a_prepare_after_a_claim_arms_the_next_turn_again(self):
        async def go():
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=True)
            prepare_mode.claim_for_turn("u", "s")
            prepare_mode.begin("u", "s")
            prepare_mode.finish("u", "s", prepared=True)

        asyncio.run(go())
        age, arm = prepare_mode.claim_for_turn("u", "s")
        assert arm == "treated" and isinstance(age, int)

    def test_the_arm_is_not_in_the_allowlist(self):
        assert "prepare_arm" not in prepare_mode.PREPARE_FIELDS
        reduced = prepare_mode.prepare_request(
            InvocationRequest(session_id="s", warm=True, prepare=True, prepare_arm="holdout")
        )
        assert reduced.prepare_arm is None

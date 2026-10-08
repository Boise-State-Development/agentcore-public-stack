"""api-converse runs its blocking Bedrock calls off the event loop, under a cap.

Background: app-api is one uvicorn process. ``boto3.converse`` and the
``for event in converse_stream()["stream"]`` loop are synchronous, and called
from the ``async def`` handler they froze the loop for the life of the model
call. During two batch-job bursts in 2026-10 every other route on the task,
``/health`` included, timed out at the ALB; ECS replaced the tasks, and the
caller's next calls stalled the survivors.

These tests pin the three properties that stop it recurring:

1. The loop stays free while a Bedrock call is in flight (non-streaming and
   streaming): a second, unrelated request completes while the model call is
   still blocked.
2. In-flight calls are capped per process, refused with 429 + ``Retry-After``
   rather than queued, and every slot is released — on success, on error, and
   on a mid-stream failure.
3. The thread bridge delivers events in order, re-raises failures, and stops
   the worker when the consumer walks away.
"""

import asyncio
import threading
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError as BotoClientError
from fastapi import FastAPI

from apis.app_api.chat import bedrock_offload
from apis.app_api.chat.bedrock_offload import (
    CapacityExceeded,
    InFlightCap,
    iterate_stream_in_thread,
    max_in_flight,
)
from apis.app_api.chat.converse_routes import router
from apis.shared.auth.api_keys.models import ValidatedApiKey

API_KEY = "test-api-key"
MOCK_KEY = ValidatedApiKey(key_id="k-offload", user_id="u-offload", name="Offload")
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

CONVERSE_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
    "usage": {"inputTokens": 5, "outputTokens": 1},
    "stopReason": "end_turn",
}

STREAM_EVENTS = [
    {"messageStart": {"role": "assistant"}},
    {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
    {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}},
    {"contentBlockStop": {"contentBlockIndex": 0}},
    {"messageStop": {"stopReason": "end_turn"}},
    {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 1}, "metrics": {}}},
]


def _body(stream: bool = False) -> dict:
    return {
        "model_id": MODEL_ID,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _sse_types(text: str) -> list[str]:
    return [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)

    @_app.get("/probe")
    async def probe():  # pragma: no cover - trivial
        return {"ok": True}

    return _app


@pytest.fixture(autouse=True)
def route_patches():
    """Everything the handler consults before it reaches Bedrock."""
    role_service = MagicMock()
    role_service.can_access_model = AsyncMock(return_value=True)
    with (
        patch("apis.app_api.chat.converse_routes._validate_api_key", AsyncMock(return_value=MOCK_KEY)),
        patch("apis.app_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled", return_value=False),
        patch("apis.app_api.chat.converse_routes.get_app_role_service", return_value=role_service),
        patch("apis.app_api.chat.converse_routes._record_cost", AsyncMock()),
    ):
        yield


@pytest.fixture
def cap_of(monkeypatch):
    """Re-size the process cap for one test and restore the singletons after."""

    def _set(limit: int) -> InFlightCap:
        monkeypatch.setenv(bedrock_offload.MAX_IN_FLIGHT_ENV, str(limit))
        bedrock_offload.reset_for_tests()
        return bedrock_offload.get_in_flight_cap()

    yield _set
    bedrock_offload.reset_for_tests()


class _GatedStream:
    """A stream whose iteration blocks on ``gate`` before each event.

    Stands in for a botocore ``EventStream``: each ``next()`` is a blocking
    network read, and ``close()`` exists.
    """

    def __init__(self, events, gate: threading.Event):
        self._events = iter(events)
        self._gate = gate
        self.closed = False
        self.delivered = 0

    def __iter__(self):
        return self

    def __next__(self):
        self._gate.wait(timeout=10)
        event = next(self._events)
        self.delivered += 1
        return event

    def close(self):
        self.closed = True


def _fake_client(*, converse_gate: threading.Event | None = None, stream=None) -> MagicMock:
    client = MagicMock()

    def _converse(**_params):
        if converse_gate is not None:
            converse_gate.wait(timeout=10)
        return CONVERSE_RESPONSE

    client.converse.side_effect = _converse
    client.converse_stream.return_value = {"stream": stream if stream is not None else iter(STREAM_EVENTS)}
    return client


async def _post(client: httpx.AsyncClient, stream: bool = False) -> httpx.Response:
    return await client.post("/chat/api-converse", json=_body(stream), headers={"X-API-Key": API_KEY})


# ---------------------------------------------------------------------------
# 1. The event loop stays free while Bedrock is in flight
# ---------------------------------------------------------------------------


class TestEventLoopStaysFree:
    @pytest.mark.asyncio
    async def test_non_streaming_call_does_not_block_other_routes(self, app):
        gate = threading.Event()
        fake = _fake_client(converse_gate=gate)
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                converse_task = asyncio.create_task(_post(client))
                # Let the handler reach the model call.
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if fake.converse.called:
                        break
                assert fake.converse.called, "handler never reached the Bedrock call"
                assert not converse_task.done()

                # The proof: an unrelated request completes while converse is blocked.
                probe = await asyncio.wait_for(client.get("/probe"), timeout=2)
                assert probe.status_code == 200
                assert not converse_task.done()

                gate.set()
                resp = await asyncio.wait_for(converse_task, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["content"] == "hi"

    @pytest.mark.asyncio
    async def test_streaming_iteration_does_not_block_other_routes(self, app):
        gate = threading.Event()
        stream = _GatedStream(STREAM_EVENTS, gate)
        fake = _fake_client(stream=stream)
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                stream_task = asyncio.create_task(_post(client, stream=True))
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if fake.converse_stream.called:
                        break
                assert fake.converse_stream.called
                assert not stream_task.done()

                # The worker is parked inside ``next()``; the loop still serves.
                probe = await asyncio.wait_for(client.get("/probe"), timeout=2)
                assert probe.status_code == 200
                assert not stream_task.done()

                gate.set()
                resp = await asyncio.wait_for(stream_task, timeout=5)
        assert resp.status_code == 200
        types = _sse_types(resp.text)
        assert types[0] == "message_start"
        assert types[-1] == "done"
        assert "metadata" in types

    @pytest.mark.asyncio
    async def test_bedrock_calls_run_off_the_loop_thread(self, app):
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}
        fake = MagicMock()

        def _converse(**_p):
            seen["converse"] = threading.get_ident()
            return CONVERSE_RESPONSE

        def _events() -> Iterator[dict]:
            seen["iterate"] = threading.get_ident()
            yield from STREAM_EVENTS

        def _converse_stream(**_p):
            seen["converse_stream"] = threading.get_ident()
            return {"stream": _events()}

        fake.converse.side_effect = _converse
        fake.converse_stream.side_effect = _converse_stream
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                assert (await _post(client)).status_code == 200
                assert (await _post(client, stream=True)).status_code == 200

        assert set(seen) == {"converse", "converse_stream", "iterate"}
        for where, ident in seen.items():
            assert ident != loop_thread, f"{where} ran on the event loop thread"


# ---------------------------------------------------------------------------
# 2. The in-flight cap
# ---------------------------------------------------------------------------


class TestInFlightCapOnRoute:
    @pytest.mark.asyncio
    async def test_non_streaming_refused_with_429_when_full_then_recovers(self, app, cap_of):
        cap = cap_of(1)
        gate = threading.Event()
        fake = _fake_client(converse_gate=gate)
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                first = asyncio.create_task(_post(client))
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if fake.converse.called:
                        break
                assert cap.in_flight == 1

                second = await asyncio.wait_for(_post(client), timeout=2)
                assert second.status_code == 429
                assert second.headers["Retry-After"] == bedrock_offload.RETRY_AFTER_SECONDS
                assert "in flight" in second.json()["detail"]
                # Refusal never touched Bedrock.
                assert fake.converse.call_count == 1

                gate.set()
                assert (await asyncio.wait_for(first, timeout=5)).status_code == 200
                assert cap.in_flight == 0

                third = await _post(client)
                assert third.status_code == 200
                assert cap.in_flight == 0

    @pytest.mark.asyncio
    async def test_streaming_refused_with_429_as_a_status_not_an_sse_frame(self, app, cap_of):
        cap = cap_of(1)
        gate = threading.Event()
        stream = _GatedStream(STREAM_EVENTS, gate)
        fake = _fake_client(stream=stream)
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                first = asyncio.create_task(_post(client, stream=True))
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if fake.converse_stream.called:
                        break
                assert cap.in_flight == 1

                second = await asyncio.wait_for(_post(client, stream=True), timeout=2)
                assert second.status_code == 429
                assert second.headers["Retry-After"] == bedrock_offload.RETRY_AFTER_SECONDS
                assert not second.headers["content-type"].startswith("text/event-stream")

                gate.set()
                resp = await asyncio.wait_for(first, timeout=5)
                assert resp.status_code == 200
                assert _sse_types(resp.text)[-1] == "done"
                assert cap.in_flight == 0

    @pytest.mark.asyncio
    async def test_non_streaming_error_releases_the_slot(self, app, cap_of):
        cap = cap_of(2)
        fake = MagicMock()
        fake.converse.side_effect = BotoClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
        )
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                resp = await _post(client)
        assert resp.status_code == 429  # Bedrock's own throttle, mapped as before
        assert cap.in_flight == 0

    @pytest.mark.asyncio
    async def test_mid_stream_failure_emits_error_then_done_and_releases_the_slot(self, app, cap_of):
        cap = cap_of(2)

        def _events():
            yield STREAM_EVENTS[0]
            yield STREAM_EVENTS[1]
            raise BotoClientError(
                {"Error": {"Code": "ModelStreamErrorException", "Message": "boom"}}, "ConverseStream"
            )

        fake = MagicMock()
        fake.converse_stream.return_value = {"stream": _events()}
        transport = httpx.ASGITransport(app=app)
        with patch("apis.app_api.chat.converse_routes._get_bedrock_client", return_value=fake):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                resp = await _post(client, stream=True)
        assert resp.status_code == 200
        types = _sse_types(resp.text)
        # The frames before the failure still reach the client, then a
        # conversational error and a terminal done — never a dropped socket.
        assert types[:2] == ["message_start", "content_block_start"]
        assert types[-2:] == ["error", "done"]
        assert cap.in_flight == 0

    @pytest.mark.asyncio
    async def test_openai_surface_is_not_counted_against_the_cap(self, app, cap_of):
        """Strands' async client holds no worker thread, so a full cap must not refuse it."""
        cap = cap_of(1)
        release = cap.acquire()  # fill the cap by hand
        try:
            fake_model = MagicMock()

            async def _gen(*_a, **_k):
                for event in STREAM_EVENTS:
                    yield event

            fake_model.stream.side_effect = _gen
            transport = httpx.ASGITransport(app=app)
            with (
                patch(
                    "apis.app_api.chat.converse_routes._resolve_model_routing",
                    AsyncMock(return_value=("mantle", "chat", "us-east-1")),
                ),
                patch("apis.app_api.chat.converse_routes.build_mantle_model", return_value=fake_model),
            ):
                async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                    resp = await _post(client)
            assert resp.status_code == 200
            assert resp.json()["content"] == "hi"
            assert cap.in_flight == 1  # ours, untouched
        finally:
            release()


class TestInFlightCapUnit:
    def test_counts_and_refuses_at_the_limit(self):
        cap = InFlightCap(2)
        r1 = cap.acquire()
        r2 = cap.acquire()
        assert cap.in_flight == 2
        with pytest.raises(CapacityExceeded):
            cap.acquire()
        r1()
        assert cap.in_flight == 1
        r3 = cap.acquire()
        assert cap.in_flight == 2
        r2()
        r3()
        assert cap.in_flight == 0

    def test_release_is_idempotent(self):
        cap = InFlightCap(1)
        release = cap.acquire()
        release()
        release()
        assert cap.in_flight == 0
        cap.acquire()  # the double release did not go negative and free a phantom slot
        assert cap.in_flight == 1

    def test_limit_must_be_positive(self):
        with pytest.raises(ValueError):
            InFlightCap(0)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, bedrock_offload.DEFAULT_MAX_IN_FLIGHT),
            ("", bedrock_offload.DEFAULT_MAX_IN_FLIGHT),
            ("4", 4),
            ("0", 1),
            ("-3", 1),
            ("lots", bedrock_offload.DEFAULT_MAX_IN_FLIGHT),
        ],
    )
    def test_max_in_flight_env(self, monkeypatch, raw, expected):
        if raw is None:
            monkeypatch.delenv(bedrock_offload.MAX_IN_FLIGHT_ENV, raising=False)
        else:
            monkeypatch.setenv(bedrock_offload.MAX_IN_FLIGHT_ENV, raw)
        assert max_in_flight() == expected

    def test_executor_is_sized_to_the_cap(self, cap_of):
        cap = cap_of(3)
        assert bedrock_offload.get_executor()._max_workers == cap.limit == 3


# ---------------------------------------------------------------------------
# 3. The thread bridge
# ---------------------------------------------------------------------------


class TestIterateStreamInThread:
    @pytest.fixture(autouse=True)
    def cold_singletons(self):
        """Start from no cap and no executor: the first call builds both.

        Guards the first-use path, where ``get_executor`` sizes itself from
        ``get_in_flight_cap`` under the same init lock — a plain ``Lock``
        there deadlocked the very first request a process served.
        """
        bedrock_offload.reset_for_tests()
        yield
        bedrock_offload.reset_for_tests()

    @pytest.mark.asyncio
    async def test_yields_in_order_and_runs_off_the_loop(self):
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def _open():
            seen["open"] = threading.get_ident()

            def _events():
                seen["iterate"] = threading.get_ident()
                yield from range(5)

            return _events()

        got = [item async for item in iterate_stream_in_thread(_open)]
        assert got == [0, 1, 2, 3, 4]
        assert seen["open"] != loop_thread
        assert seen["iterate"] != loop_thread

    @pytest.mark.asyncio
    async def test_open_failure_is_raised_to_the_consumer(self):
        def _open():
            raise RuntimeError("no headers")

        with pytest.raises(RuntimeError, match="no headers"):
            async for _ in iterate_stream_in_thread(_open):
                pytest.fail("nothing should be yielded")

    @pytest.mark.asyncio
    async def test_mid_iteration_failure_is_raised_after_the_good_items(self):
        def _open():
            def _events():
                yield "a"
                yield "b"
                raise ValueError("torn")

            return _events()

        got = []
        with pytest.raises(ValueError, match="torn"):
            async for item in iterate_stream_in_thread(_open):
                got.append(item)
        assert got == ["a", "b"]

    @pytest.mark.asyncio
    async def test_abandoning_the_consumer_stops_and_closes_the_stream(self):
        gate = threading.Event()
        gate.set()
        stream = _GatedStream(list(range(100)), gate)

        agen = iterate_stream_in_thread(lambda: stream)
        first = await agen.__anext__()
        assert first == 0
        await agen.aclose()

        # The worker observes the abandon flag at its next chunk, breaks, and
        # closes the stream — it does not drain the remaining 99 items.
        for _ in range(100):
            if stream.closed:
                break
            await asyncio.sleep(0.01)
        assert stream.closed
        assert stream.delivered < 100

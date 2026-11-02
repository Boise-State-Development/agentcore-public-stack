"""`/effort`: the one inference parameter a user may tune, per model."""

from __future__ import annotations

import json
from typing import Any

import httpx

from aps_tui.client.catalog import EffortControl, Model
from aps_tui.credentials import CredentialSource

from .conftest import make_config, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_pickers import catalog_for


def model_wire(model_id: str, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    wire: dict[str, Any] = {"modelId": model_id, "provider": "bedrock", "modelName": name, "inputModalities": ["TEXT"]}
    if params is not None:
        wire["supportedParams"] = {"params": params}
    return wire


EFFORT = {"effort": {"supported": True, "allowed": ["low", "medium", "high"], "default": "medium"}}


class TestEffortControlParsing:
    def test_a_model_with_allowed_levels_offers_a_control(self) -> None:
        model = Model.from_wire(model_wire("m1", "Sonnet", EFFORT))
        assert model.effort == EffortControl("effort", ("low", "medium", "high"), "medium")

    def test_reasoning_effort_is_the_second_key(self) -> None:
        model = Model.from_wire(model_wire("m1", "GPT", {"reasoning_effort": {"allowed": ["low", "high"]}}))
        assert model.effort is not None and model.effort.key == "reasoning_effort" and model.effort.default is None

    def test_locked_unsupported_or_unlisted_means_no_control(self) -> None:
        assert Model.from_wire(model_wire("a", "A", {"effort": {"allowed": ["low"], "locked": True}})).effort is None
        assert Model.from_wire(model_wire("b", "B", {"effort": {"supported": False, "allowed": ["low"]}})).effort is None
        assert Model.from_wire(model_wire("c", "C", {"temperature": {"min": 0, "max": 1}})).effort is None
        assert Model.from_wire(model_wire("d", "D")).effort is None

    def test_a_default_outside_the_levels_is_not_claimed(self) -> None:
        model = Model.from_wire(model_wire("m1", "X", {"effort": {"allowed": ["low", "high"], "default": "max"}}))
        assert model.effort is not None and model.effort.default is None


class Backend:
    def __init__(self) -> None:
        self.models = [model_wire("us.anthropic.claude-sonnet-4-6", "Sonnet 4.6", EFFORT), model_wire("plain-model", "Plain")]
        self.stream_bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"models": self.models})
        if path.endswith("/chat/stream"):
            self.stream_bodies.append(json.loads(request.content))
            return sse_response(agent_turn("ok"))
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": []})
        return httpx.Response(404, json={"detail": f"no route {path}"})


def app_for(backend: Backend, model_id: str = "us.anthropic.claude-sonnet-4-6"):  # type: ignore[no-untyped-def]
    config = make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION, model_id=model_id)
    app = build_agent_app(backend, config=config)
    app.chat._catalog = catalog_for(backend)
    return app


class TestEffortCommand:
    async def test_setting_a_level_sends_it_every_turn_and_shows_it(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/effort high")
            await pilot.pause()
            assert "effort: high" in app.chat.status.line
            await send(pilot, app, "one")
            await send(pilot, app, "two")
            await send(pilot, app, "/effort off")
            await pilot.pause()
            assert "effort:" not in app.chat.status.line
            await send(pilot, app, "three")
        assert [b.get("inference_params") for b in backend.stream_bodies] == [{"effort": "high"}, {"effort": "high"}, None]

    async def test_a_level_the_model_does_not_offer_is_refused(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/effort max")
            await pilot.pause()
            assert "effort:" not in app.chat.status.line
            await send(pilot, app, "hi")
        assert "inference_params" not in backend.stream_bodies[0]

    async def test_a_model_without_a_choice_says_so(self) -> None:
        backend = Backend()
        app = app_for(backend, model_id="plain-model")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/effort high")
            await pilot.pause()
            assert "effort:" not in app.chat.status.line
            assert app.chat._effort_by_model == {}

    async def test_the_choice_is_kept_per_model(self) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "/effort low")
            await app.chat.set_model("plain-model")
            await pilot.pause()
            assert "effort:" not in app.chat.status.line
            await send(pilot, app, "on plain")
            await app.chat.set_model("us.anthropic.claude-sonnet-4-6")
            await pilot.pause()
            assert "effort: low" in app.chat.status.line
            await send(pilot, app, "back on sonnet")
        assert "inference_params" not in backend.stream_bodies[0]
        assert backend.stream_bodies[1]["inference_params"] == {"effort": "low"}

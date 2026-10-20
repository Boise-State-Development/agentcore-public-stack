"""The constrained call, and what happens when enforcement is unavailable.

``docs/specs/structured-extraction.md`` §6.4, D11, D15. Two properties:

* **The fallback is marked, never silent.** A reviewer must be able to tell
  types that were guaranteed from types that were merely requested, and the
  three causes that earn a retry are narrow on purpose — a throttle or a
  credentials error is a reason to fail, not a reason to quietly produce a
  weaker result.
* **The call is a dedicated model instance.** Not the turn's, and
  ``strict_tools`` is set on it and nowhere else.

The ``BedrockModel`` is faked at the module seam rather than mocked through
boto, because what is under test is this module's control flow, not Bedrock's.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from apis.shared.extraction import extractor as ex
from apis.shared.extraction.field_sets import parse_field_set
from apis.shared.extraction.schema import model_for_field_set, reset_model_cache

SET = """---
title: NSF Solicitation
version: 2
---

| name | title | type | find |
|---|---|---|---|
| deadline | Deadline | date | the deadline |
| budget_cap | Budget cap | currency | the cap |
"""


@pytest.fixture(autouse=True)
def _clear() -> Any:
    reset_model_cache()
    yield
    reset_model_cache()


@pytest.fixture
def field_set():
    return parse_field_set(SET, name="nsf-solicitation")


ANSWER = {
    "deadline": {"value": "2026-03-03", "quote": "due March 3, 2026"},
    "budget_cap": {"value": "$500,000", "quote": "up to $500,000"},
}

USAGE = {
    "inputTokens": 4000,
    "outputTokens": 120,
    "totalTokens": 4120,
    "cacheReadInputTokens": 0,
    "cacheWriteInputTokens": 0,
}


def ModelStreamChunkEvent(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Strands' wrapper for a raw provider chunk: ``{"event": chunk}``.

    Reproduced rather than imported so the test pins the *shape* this module
    depends on. Importing the real class would make the test pass through any
    future rename of the wrapper key without saying so.
    """
    return {"event": chunk}


def ModelStopReason(stop_reason: str, message: Any, usage: Any, metrics: Any) -> Dict[str, Any]:
    """Strands' final event: ``{"stop": (stop_reason, message, usage, metrics)}``."""
    return {"stop": (stop_reason, message, usage, metrics)}


class FakeModel:
    """Records how it was built, and yields what it was told to."""

    built: List[Dict[str, Any]] = []

    def __init__(self, model_id: str, strict: bool, behaviour: Any) -> None:
        self.model_id = model_id
        self.strict = strict
        self._behaviour = behaviour
        FakeModel.built.append({"model_id": model_id, "strict": strict})

    async def structured_output(self, output_model, prompt, system_prompt=None):
        self.prompt = prompt
        self.system_prompt = system_prompt
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        if self._behaviour == "hang":
            await asyncio.sleep(10)
        # These are Strands' OWN event shapes, mirrored from
        # `strands/event_loop/streaming.py` and `strands/types/_events.py` —
        # not invented for the test. A fake that emitted a convenient shape
        # instead would confirm whatever the reader happened to look for, and
        # it did: an earlier revision read `event["metadata"]["usage"]`, which
        # process_stream never produces, so metering silently recorded nothing.
        yield ModelStreamChunkEvent({"metadata": {"usage": dict(USAGE)}})
        yield {"output": output_model.model_validate(ANSWER)}
        yield ModelStopReason("tool_use", {}, dict(USAGE), {"latencyMs": 10})


def install(monkeypatch, *behaviours: Any) -> None:
    """Install a fake ``_build_model`` that serves *behaviours* in order."""
    FakeModel.built = []
    queue = list(behaviours)

    def build(model_id: str, *, strict: bool) -> FakeModel:
        behaviour = queue.pop(0) if queue else behaviours[-1]
        return FakeModel(model_id, strict, behaviour)

    monkeypatch.setattr(ex, "_build_model", build)


class ValidationException(Exception):
    """Stands in for botocore's generated error class, matched by name."""


class TestTheHappyPath:
    @pytest.mark.asyncio
    async def test_a_strict_call_returns_enforced_rows(
        self, monkeypatch, field_set
    ) -> None:
        install(monkeypatch, "ok")
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert result.schema_enforced is True
        assert result.rows["budget_cap"]["value"] == "$500,000"
        assert result.rows["deadline"]["quote"] == "due March 3, 2026"
        assert result.usage["inputTokens"] == 4000

    @pytest.mark.asyncio
    async def test_strict_tools_is_requested_on_the_first_attempt(
        self, monkeypatch, field_set
    ) -> None:
        install(monkeypatch, "ok")
        await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert FakeModel.built == [{"model_id": "us.model", "strict": True}]

    @pytest.mark.asyncio
    async def test_rows_come_back_in_the_authored_order(
        self, monkeypatch, field_set
    ) -> None:
        install(monkeypatch, "ok")
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert list(result.rows) == ["deadline", "budget_cap"]

    @pytest.mark.asyncio
    async def test_the_conversation_is_not_in_the_prompt(
        self, monkeypatch, field_set
    ) -> None:
        """The side-channel property (§5): the input is the document and the
        schema. Extraction on turn 40 costs what it costs on turn 1, and
        nothing the user said earlier can talk the extractor out of reading the
        document.
        """
        captured: Dict[str, Any] = {}

        def build(model_id: str, *, strict: bool) -> Any:
            model = FakeModel(model_id, strict, "ok")
            captured["model"] = model
            return model

        FakeModel.built = []
        monkeypatch.setattr(ex, "_build_model", build)
        await ex.extract_fields(
            field_set, "DOCUMENT BODY", filename="nsf.pdf", model_id="us.model"
        )
        prompt = captured["model"].prompt
        assert len(prompt) == 1
        assert prompt[0]["role"] == "user"
        text = prompt[0]["content"][0]["text"]
        assert "DOCUMENT BODY" in text
        assert "the deadline" in text  # the field set's own find phrases


class TestUsageIsActuallyRead:
    """D12 depends on this, and getting it wrong is silent.

    ``_meter`` skips an empty reading, so a missed usage block writes no ``C#``
    row at all — worse than an unpriced model, because not even
    ``UnmeteredModelCall`` fires to say so. These tests pin the two event
    shapes Strands really emits, and one asserts them against the installed
    SDK so a rename breaks the build rather than the metering.
    """

    @pytest.mark.asyncio
    async def test_usage_is_read_from_the_stream(self, monkeypatch, field_set) -> None:
        install(monkeypatch, "ok")
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert result.usage["inputTokens"] == 4000
        assert result.usage["outputTokens"] == 120

    def test_the_authoritative_reading_is_the_final_stop_tuple(self) -> None:
        """``ModelStopReason`` is ``{"stop": (stop_reason, message, usage,
        metrics)}`` — emitted once, after the provider's metadata chunk has
        been folded in."""
        assert ex._usage_from(
            {"stop": ("tool_use", {}, {"inputTokens": 7, "outputTokens": 3}, {})}
        ) == {
            "inputTokens": 7,
            "outputTokens": 3,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }

    def test_the_chunk_wrapper_key_is_event_not_metadata(self) -> None:
        """``ModelStreamChunkEvent`` is ``{"event": chunk}``, so the provider's
        metadata is one level further in than it looks. Reading
        ``event["metadata"]`` finds nothing, every time."""
        wrapped = {"event": {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2}}}}
        assert ex._usage_from(wrapped)["inputTokens"] == 5
        # The shape that looks right and never occurs.
        assert ex._usage_from({"metadata": {"usage": {"inputTokens": 5}}}) == {}

    def test_an_all_zero_reading_is_treated_as_absent(self) -> None:
        """Strands seeds usage at zero and overwrites it when the metadata
        chunk arrives, so all-zero means "not reported yet". Metering it would
        price the call at $0 and look exactly like a model with no catalog
        row."""
        assert ex._usage_from({"stop": ("tool_use", {}, {"inputTokens": 0, "outputTokens": 0}, {})}) == {}

    def test_cache_token_counts_are_carried_when_present(self) -> None:
        usage = ex._usage_from(
            {
                "stop": (
                    "tool_use",
                    {},
                    {
                        "inputTokens": 100,
                        "outputTokens": 10,
                        "cacheReadInputTokens": 40,
                        "cacheWriteInputTokens": 60,
                    },
                    {},
                )
            }
        )
        assert usage["cacheReadInputTokens"] == 40
        assert usage["cacheWriteInputTokens"] == 60

    def test_a_malformed_event_yields_nothing_rather_than_raising(self) -> None:
        for bad in ({}, {"stop": None}, {"stop": ("x",)}, {"event": "nope"},
                    {"stop": ("x", {}, "not-a-dict", {})}):
            assert ex._usage_from(bad) == {}

    def test_the_shapes_match_the_installed_sdk(self) -> None:
        """The reason this file reproduces the wrappers instead of importing
        them: the test must fail if Strands renames a key, not silently follow
        it into a metering gap."""
        from strands.types._events import ModelStopReason, ModelStreamChunkEvent

        chunk = ModelStreamChunkEvent({"metadata": {"usage": {"inputTokens": 1}}})
        assert set(chunk) == {"event"}
        stop = ModelStopReason("tool_use", {}, {"inputTokens": 1}, {})
        assert set(stop) == {"stop"}
        assert len(stop["stop"]) == 4
        # Position 2 is usage, which is what `_usage_from` indexes.
        assert stop["stop"][2] == {"inputTokens": 1}


class TestTheMarkedFallback:
    """D11 — never silent."""

    @pytest.mark.asyncio
    async def test_a_grammar_compilation_timeout_retries_unenforced(
        self, monkeypatch, field_set
    ) -> None:
        monkeypatch.setattr(ex, "STRICT_TIMEOUT_SECONDS", 0.01)
        install(monkeypatch, "hang", "ok")
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert result.schema_enforced is False
        assert result.rows["budget_cap"]["value"] == "$500,000"
        assert [b["strict"] for b in FakeModel.built] == [True, False]

    @pytest.mark.asyncio
    async def test_an_unsupported_schema_feature_retries_unenforced(
        self, monkeypatch, field_set
    ) -> None:
        install(
            monkeypatch,
            ValidationException("The schema uses an unsupported feature"),
            "ok",
        )
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert result.schema_enforced is False

    @pytest.mark.asyncio
    async def test_a_model_that_ignored_the_forced_tool_retries_unenforced(
        self, monkeypatch, field_set
    ) -> None:
        """Strands raises ``ValueError`` when the stop reason is not
        ``tool_use`` or no usable tool input came back."""
        install(monkeypatch, ValueError('Model returned stop_reason: end_turn'), "ok")
        result = await ex.extract_fields(
            field_set, "a document", filename="nsf.pdf", model_id="us.model"
        )
        assert result.schema_enforced is False

    @pytest.mark.asyncio
    async def test_an_unrelated_failure_is_not_quietly_downgraded(
        self, monkeypatch, field_set
    ) -> None:
        """The narrowness is the point. A throttle or an access denial is a
        reason to fail — retrying would turn an infrastructure problem into a
        quietly weaker result that nobody is told about.
        """

        class AccessDeniedException(Exception):
            pass

        install(monkeypatch, AccessDeniedException("no"), "ok")
        with pytest.raises(ex.ExtractionError):
            await ex.extract_fields(
                field_set, "a document", filename="nsf.pdf", model_id="us.model"
            )
        assert [b["strict"] for b in FakeModel.built] == [True]

    @pytest.mark.asyncio
    async def test_both_attempts_failing_raises_rather_than_returning_nulls(
        self, monkeypatch, field_set
    ) -> None:
        """A table of nulls would be indistinguishable from a document that
        genuinely says nothing, which is the worse of the two answers."""
        install(
            monkeypatch,
            ValidationException("unsupported schema feature"),
            ValueError("still broken"),
        )
        with pytest.raises(ex.ExtractionError, match="with and without"):
            await ex.extract_fields(
                field_set, "a document", filename="nsf.pdf", model_id="us.model"
            )

    @pytest.mark.asyncio
    async def test_cancellation_is_never_swallowed(self, monkeypatch, field_set) -> None:
        install(monkeypatch, asyncio.CancelledError(), "ok")
        with pytest.raises(asyncio.CancelledError):
            await ex.extract_fields(
                field_set, "a document", filename="nsf.pdf", model_id="us.model"
            )


class TestRefusals:
    @pytest.mark.asyncio
    async def test_an_unset_model_id_is_refused_not_defaulted(self, field_set) -> None:
        """A default that looks like it works is how an unmetered model ships
        (§9)."""
        with pytest.raises(ex.ExtractionError, match="DOCUMENT_EXTRACT_MODEL_ID"):
            await ex.extract_fields(
                field_set, "a document", filename="nsf.pdf", model_id=""
            )

    @pytest.mark.asyncio
    async def test_a_document_with_no_text_layer_says_so(self, field_set) -> None:
        with pytest.raises(ex.ExtractionError, match="no readable text layer"):
            await ex.extract_fields(
                field_set, "   ", filename="scan.pdf", model_id="us.model"
            )

    @pytest.mark.asyncio
    async def test_an_oversized_document_points_at_the_pages_lever(
        self, field_set
    ) -> None:
        with pytest.raises(ex.ExtractionError, match="`pages`"):
            await ex.extract_fields(
                field_set,
                "x" * (ex.MAX_DOCUMENT_CHARS + 1),
                filename="huge.pdf",
                model_id="us.model",
            )


class TestAwkwardFieldNames:
    @pytest.mark.asyncio
    async def test_a_field_named_like_a_basemodel_attribute_reads_back(
        self, monkeypatch
    ) -> None:
        """Such a field is bound to a renamed Python attribute while keeping
        its authored name as the alias, so reading it back by name has to go
        through the alias."""
        field_set = parse_field_set(
            "| name | find |\n|---|---|\n| json | the json block |\n", name="awkward"
        )
        answer = {"json": {"value": "{}", "quote": "the json block is {}"}}

        class Model(FakeModel):
            async def structured_output(self, output_model, prompt, system_prompt=None):
                yield {"output": output_model.model_validate(answer)}

        FakeModel.built = []
        monkeypatch.setattr(
            ex, "_build_model", lambda model_id, *, strict: Model(model_id, strict, "ok")
        )
        result = await ex.extract_fields(
            field_set, "a document", filename="x.pdf", model_id="us.model"
        )
        assert result.rows["json"]["value"] == "{}"


class TestNoTurnScopedState:
    """§15, G5. A later change may run this concurrently over several
    documents; nothing here may assume it is the only extraction in flight."""

    @pytest.mark.asyncio
    async def test_concurrent_extractions_do_not_interfere(
        self, monkeypatch, field_set
    ) -> None:
        install(monkeypatch, "ok")
        results = await asyncio.gather(
            *[
                ex.extract_fields(
                    field_set, f"document {i}", filename=f"{i}.pdf", model_id="us.model"
                )
                for i in range(5)
            ]
        )
        assert all(r.schema_enforced for r in results)
        assert all(r.rows["budget_cap"]["value"] == "$500,000" for r in results)

    def test_the_module_holds_no_mutable_run_state(self) -> None:
        """Anything keyed on "the current turn's extraction" would make the
        fan-out above a rewrite rather than a change."""
        import inspect

        source = inspect.getsource(ex)
        for banned in ("global ", "_current", "_in_flight", "_last_"):
            assert banned not in source, f"{banned!r} suggests per-run state"

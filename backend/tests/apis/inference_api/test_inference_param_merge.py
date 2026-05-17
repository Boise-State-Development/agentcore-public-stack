"""Tests for the inference-param merge guard in ``apis.inference_api.chat.routes``.

Focus: the cross-param safety check that drops ``thinking`` when
``thinking >= max_tokens`` (Anthropic rejects that request outright). Inference
params arrive untyped (``Dict[str, Any]`` from JSON), so an int bound can show
up as a float — an ``isinstance(..., int)`` gate used to silently skip the
check on float input and let the bad request through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apis.inference_api.chat.routes import _as_int_or_none, _merge_inference_params
from apis.shared.models.models import ModelParamSpec, SupportedParams


def _model(**specs: ModelParamSpec) -> SimpleNamespace:
    """Minimal managed-model stand-in: only ``supported_params`` + ``model_id``."""
    return SimpleNamespace(
        model_id="test-model",
        supported_params=SupportedParams(params=dict(specs)),
    )


# Wide bounds so request values pass through unclamped (and keep their
# original float type), reproducing the JSON-sourced-float scenario.
_WIDE_MAX_TOKENS = ModelParamSpec(supported=True, min=1, max=200000)
_WIDE_THINKING = ModelParamSpec(supported=True, min=1024, max=None)


class TestAsIntOrNone:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (8192, 8192),
            (8192.0, 8192),
            (100000.0, 100000),
            (True, None),
            (False, None),
            (None, None),
            ("8192", None),
            ({"type": "enabled"}, None),
        ],
    )
    def test_coercion(self, value, expected):
        assert _as_int_or_none(value) == expected


class TestThinkingGuardFloatInput:
    def test_float_thinking_ge_float_max_tokens_drops_thinking(self):
        """The original bug: both arrive as floats, thinking >= max_tokens.
        The guard must still fire and drop thinking."""
        model = _model(max_tokens=_WIDE_MAX_TOKENS, thinking=_WIDE_THINKING)
        merged = _merge_inference_params(
            model, {"max_tokens": 2048.0, "thinking": 4096.0}
        )

        assert "thinking" not in merged
        assert merged["max_tokens"] == 2048.0

    def test_float_thinking_below_float_max_tokens_is_retained(self):
        """Guard must not over-drop when the float values are consistent."""
        model = _model(max_tokens=_WIDE_MAX_TOKENS, thinking=_WIDE_THINKING)
        merged = _merge_inference_params(
            model, {"max_tokens": 8192.0, "thinking": 2048.0}
        )

        assert merged["thinking"] == 2048.0
        assert merged["max_tokens"] == 8192.0

    def test_int_inputs_still_guarded(self):
        """Pre-existing int path must keep working."""
        model = _model(max_tokens=_WIDE_MAX_TOKENS, thinking=_WIDE_THINKING)
        merged = _merge_inference_params(
            model, {"max_tokens": 2048, "thinking": 4096}
        )

        assert "thinking" not in merged

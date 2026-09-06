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


class TestEffortAllowedGating:
    """`effort` is enum-gated: a request override must be a member of the
    admin-declared `allowed` set, else it falls back to the default. The
    per-model effort-tier difference (Sonnet 4.6 vs Opus 4.7) is data on
    `ModelParamSpec.allowed`, not model-family code."""

    _SONNET_EFFORT = ModelParamSpec(
        supported=True, allowed=["low", "medium", "high"], default="high"
    )
    _OPUS_EFFORT = ModelParamSpec(
        supported=True, allowed=["low", "medium", "high", "xhigh", "max"], default="high"
    )

    def test_in_domain_override_is_kept(self):
        model = _model(effort=self._SONNET_EFFORT)
        merged = _merge_inference_params(model, {"effort": "low"})
        assert merged["effort"] == "low"

    def test_out_of_domain_override_falls_back_to_default(self):
        # `xhigh` is Opus-4.7-only; on a Sonnet-4.6-shaped spec it's rejected
        # and the admin default wins instead of erroring mid-stream.
        model = _model(effort=self._SONNET_EFFORT)
        merged = _merge_inference_params(model, {"effort": "xhigh"})
        assert merged["effort"] == "high"

    def test_xhigh_allowed_on_opus_spec(self):
        model = _model(effort=self._OPUS_EFFORT)
        merged = _merge_inference_params(model, {"effort": "xhigh"})
        assert merged["effort"] == "xhigh"

    def test_no_override_uses_default(self):
        model = _model(effort=self._SONNET_EFFORT)
        merged = _merge_inference_params(model, {})
        assert merged["effort"] == "high"

    def test_out_of_domain_with_no_default_is_dropped(self):
        spec = ModelParamSpec(supported=True, allowed=["low", "medium", "high"])
        model = _model(effort=spec)
        merged = _merge_inference_params(model, {"effort": "max"})
        assert "effort" not in merged


class TestOmissionMeansUnsupported:
    """A spec that declares *any* param is authoritative: silence about a param
    means unsupported, not "pass it through".

    The original default forwarded any request key in ``KNOWN_CANONICAL_PARAMS``
    that the spec didn't mention. Anthropic deprecated ``temperature`` /
    ``top_p`` / ``top_k`` on Claude Opus 4.7 and later — a non-default value
    returns a hard 400 — and our curated templates for those models *omit*
    those params rather than declaring ``supported: false``. So the permissive
    default let a temperature reach Bedrock and kill the turn mid-stream.

    Note the SPA already behaved this way: `model-settings.ts` renders a row
    only for a param the spec declares AND marks supported, so omission was
    already "unsupported" in the UI. The backend was the surface that disagreed.
    """

    # Opus 4.7 / Sonnet 5 shape: max_tokens + effort declared, sampling params
    # deliberately absent because the model 400s on them.
    def _opus_47_spec(self) -> SimpleNamespace:
        return _model(
            max_tokens=ModelParamSpec(supported=True, min=1, max=64000, default=32000),
            effort=ModelParamSpec(
                supported=True, allowed=["low", "medium", "high"], default="medium"
            ),
        )

    def test_omitted_param_is_dropped_when_a_spec_is_declared(self):
        merged = _merge_inference_params(self._opus_47_spec(), {"temperature": 0.9})
        assert "temperature" not in merged

    def test_every_deprecated_sampling_param_is_dropped(self):
        merged = _merge_inference_params(
            self._opus_47_spec(),
            {"temperature": 0.9, "top_p": 0.5, "top_k": 40},
        )
        assert merged.keys() == {"max_tokens", "effort"}

    def test_declared_params_still_merge_normally(self):
        """The inversion must not disturb the params the spec does declare."""
        merged = _merge_inference_params(
            self._opus_47_spec(), {"max_tokens": 4096, "effort": "high"}
        )
        assert merged["max_tokens"] == 4096
        assert merged["effort"] == "high"

    def test_drop_is_logged_with_the_model_id(self, caplog):
        """The drop-log is the mitigation for taking a param away — without it
        the inversion is silent and unfalsifiable in production."""
        import logging

        with caplog.at_level(logging.INFO):
            _merge_inference_params(self._opus_47_spec(), {"temperature": 0.9})
        assert any(
            "omitted from its supportedParams" in r.getMessage()
            for r in caplog.records
        )

    def test_model_with_no_spec_stays_permissive(self):
        """A hand-created record that declares nothing hasn't made a claim, so
        there is no omission to read. Keep the canonical allow-list behavior."""
        no_spec = SimpleNamespace(model_id="hand-made", supported_params=None)
        merged = _merge_inference_params(no_spec, {"temperature": 0.7})
        assert merged["temperature"] == 0.7

    def test_model_with_empty_spec_stays_permissive(self):
        empty = _model()  # SupportedParams(params={})
        merged = _merge_inference_params(empty, {"temperature": 0.7})
        assert merged["temperature"] == 0.7

    def test_unrecognized_key_is_still_dropped_without_a_spec(self):
        no_spec = SimpleNamespace(model_id="hand-made", supported_params=None)
        merged = _merge_inference_params(no_spec, {"not_a_real_param": 1})
        assert merged == {}

    def test_explicit_unsupported_still_wins(self):
        """Declaring `supported: false` keeps working — the inversion only
        changes what *silence* means."""
        model = _model(temperature=ModelParamSpec(supported=False))
        merged = _merge_inference_params(model, {"temperature": 0.9})
        assert "temperature" not in merged

    def test_stale_persisted_override_can_no_longer_reach_the_provider(self):
        """The SPA persists overrides per model id in localStorage and sends
        them verbatim, unfiltered by the current spec. So an override set while
        a param was declared outlives the spec that justified it. Before the
        inversion that stale value reached Bedrock; now it is dropped."""
        merged = _merge_inference_params(
            self._opus_47_spec(), {"temperature": 1.0, "max_tokens": 8192}
        )
        assert merged == {"max_tokens": 8192, "effort": "medium"}


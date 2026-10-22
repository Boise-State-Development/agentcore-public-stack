"""Context-window resolution: catalog wins, SDK fills absence, conflicts are logged.

Issue #267. The cases that matter are the two that look alike from inside the
code — a *stale* catalog row and a *deliberate* pricing cap both present as
"our number disagrees with Strands'" — which is why the resolver detects
rather than resolves. See ``apis/shared/models/context_window.py``.
"""

import logging

import pytest

from apis.shared.models import context_window as cw


@pytest.fixture(autouse=True)
def _clear_disagreement_cache():
    """The warning is once-per-process by design; tests need it per-case."""
    cw._DISAGREEMENT_LOGGED.clear()
    yield
    cw._DISAGREEMENT_LOGGED.clear()


class TestSdkTable:
    def test_resolves_anthropic_bedrock_id_through_the_prefix_strip(self):
        # us.anthropic.* is a cross-region inference profile; Strands' table is
        # keyed on the base id, so this only works via its prefix strip.
        assert cw.sdk_context_window("us.anthropic.claude-sonnet-4-6") == 1_000_000

    def test_haiku_45_really_is_200k(self):
        # Guards against "fix" the 200k default by raising it globally.
        assert cw.sdk_context_window("us.anthropic.claude-haiku-4-5-20251001-v1:0") == 200_000

    @pytest.mark.parametrize(
        "model_id",
        [
            "us.moonshotai.kimi-k3",
            "us.deepseek.v3-2",
        ],
    )
    def test_non_anthropic_ids_are_absent(self, model_id):
        # The fallback is a partial safety net. If one of these starts
        # returning a value, check it against the catalog row the way the
        # hosted OpenAI rows below were checked.
        assert cw.sdk_context_window(model_id) is None

    @pytest.mark.parametrize("model_id", ["us.openai.gpt-6-astra", "us.openai.gpt-5.6-sol"])
    def test_hosted_openai_ids_now_resolve_to_their_full_window(self, model_id):
        # strands-agents 1.56+ strips nested prefixes (`us.` then `openai.`)
        # and its table carries these at 1,050,000. The curated rows pin
        # maxInputTokens at 272,000 on purpose (the pricing tier), so the
        # catalog value still wins; the pair now logs one disagreement warning
        # per process, which is expected rather than a stale row.
        assert cw.sdk_context_window(model_id) == 1_050_000

    def test_the_curated_272k_cap_still_wins_over_the_sdk_window(self):
        window, source = cw.resolve_context_window("us.openai.gpt-6-astra", 272_000)
        assert (window, source) == (272_000, "disagreement")

    def test_missing_model_id_is_not_an_error(self):
        assert cw.sdk_context_window(None) is None
        assert cw.sdk_context_window("") is None

    def test_import_failure_degrades_to_no_fallback(self, monkeypatch):
        # A Strands bump that moves the private module must not break a turn.
        import builtins

        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name == "strands.models._defaults":
                raise ImportError("moved upstream")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert cw.sdk_context_window("us.anthropic.claude-sonnet-4-6") is None


class TestPrecedence:
    def test_catalog_wins_when_present(self):
        window, source = cw.resolve_context_window("us.anthropic.claude-sonnet-4-6", 1_000_000)
        assert (window, source) == (1_000_000, "catalog")

    def test_sdk_fills_an_absent_catalog_value(self):
        # #267's actual ask: a record with no maxInputTokens used to yield None,
        # which silently dropped the model onto the fixed compaction threshold.
        window, source = cw.resolve_context_window("us.anthropic.claude-sonnet-4-6", None)
        assert (window, source) == (1_000_000, "sdk")

    def test_both_absent_reports_nothing_rather_than_guessing(self):
        assert cw.resolve_context_window("us.moonshotai.kimi-k3", None) == (None, None)

    @pytest.mark.parametrize("bad", [0, -1, "", "not-a-number", None])
    def test_unusable_catalog_values_fall_through(self, bad):
        window, source = cw.resolve_context_window("us.anthropic.claude-sonnet-4-6", bad)
        assert (window, source) == (1_000_000, "sdk")

    def test_string_catalog_value_is_coerced(self):
        # DynamoDB numbers arrive as Decimal/str through some paths.
        assert cw.resolve_context_window("us.anthropic.claude-sonnet-4-6", "1000000")[0] == 1_000_000


class TestDisagreement:
    STALE = "us.anthropic.claude-sonnet-4-6"

    def test_conflict_keeps_the_catalog_value_and_warns(self, caplog):
        # The stale-row shape: ours says 200k, the card and SDK say 1M.
        with caplog.at_level(logging.WARNING, logger=cw.logger.name):
            window, source = cw.resolve_context_window(self.STALE, 200_000)
        assert (window, source) == (200_000, "disagreement")
        assert "context_window_disagreement" in caplog.text
        assert "catalog=200000" in caplog.text
        assert "sdk=1000000" in caplog.text

    def test_a_deliberate_cap_would_present_identically(self, caplog):
        # The reason this is a detector and not a resolver. If Strands ever
        # learns the OpenAI ids, the 272K pricing cap trips this same path —
        # and taking the SDK's side there would open the second price card and
        # under-charge every long turn.
        with caplog.at_level(logging.WARNING, logger=cw.logger.name):
            window, source = cw.resolve_context_window(self.STALE, 272_000)
        assert window == 272_000
        assert source == "disagreement"
        assert "Verify against the AWS model card" in caplog.text

    def test_warning_is_emitted_once_per_model_id(self, caplog):
        # It is a property of the pair, not the turn — otherwise a deliberate
        # cap logs on every single model call.
        with caplog.at_level(logging.WARNING, logger=cw.logger.name):
            for _ in range(5):
                cw.resolve_context_window(self.STALE, 200_000)
        assert caplog.text.count("context_window_disagreement") == 1

    def test_agreement_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING, logger=cw.logger.name):
            cw.resolve_context_window(self.STALE, 1_000_000)
        assert "context_window_disagreement" not in caplog.text

"""Cross-package contract: the `supportsCaching` provider policy.

The admin form always posts a `supportsCaching` value, so the backend's
provider-aware default never sees ``None`` from the UI and can never apply.
That forces the same policy to exist on both sides — and two sources of truth
that can drift is exactly what produced the bug this test guards.

The failure it prevents is not cosmetic. On ``bedrock-responses`` caching is
implicit and server-side; nothing we send turns it off. A stored ``False``
there is untrue, and its only practical effect is that the cache rates get
cleared — pricing cached tokens at $0.00 while the provider bills them in full.
A live probe on ``us.openai.gpt-5.6-sol`` measured 38,410 cache-read and 13,405
cache-write tokens against 10 uncached input tokens on a warm conversation, so
that is close to total under-reporting of the model's spend.

So this reads the TypeScript source and asserts the two lists agree.
"""

import re
from pathlib import Path

import pytest

from apis.shared.models.managed_models import (
    _CACHING_DEFAULT_PROVIDERS,
    _CACHING_FORCED_PROVIDERS,
    _resolve_supports_caching,
)

_MODEL_TS = (
    Path(__file__).resolve().parents[2].parent
    / "frontend"
    / "ai.client"
    / "src"
    / "app"
    / "admin"
    / "manage-models"
    / "models"
    / "managed-model.model.ts"
)


def _ts_provider_list(name: str) -> tuple[str, ...]:
    """Extract a `readonly ModelProvider[]` literal from the TS source."""
    source = _MODEL_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}: readonly ModelProvider\[\] = \[(.*?)\];", source, re.S)
    assert match, f"{name} not found in {_MODEL_TS.name} — did it get renamed?"
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


@pytest.mark.skipif(not _MODEL_TS.exists(), reason="frontend sources not present")
class TestProviderListsAgree:
    def test_caching_defaults_match(self):
        assert _ts_provider_list("CACHING_DEFAULT_PROVIDERS") == _CACHING_DEFAULT_PROVIDERS

    def test_forced_providers_match(self):
        assert _ts_provider_list("CACHING_FORCED_PROVIDERS") == _CACHING_FORCED_PROVIDERS


class TestPolicyInvariants:
    def test_every_forced_provider_also_defaults_on(self):
        """A provider that is forced on but not a default would contradict itself."""
        for provider in _CACHING_FORCED_PROVIDERS:
            assert provider in _CACHING_DEFAULT_PROVIDERS

    def test_forcing_beats_an_explicit_false(self):
        assert _resolve_supports_caching(False, "bedrock-responses") is True

    def test_optional_providers_still_honour_an_explicit_false(self):
        assert _resolve_supports_caching(False, "bedrock") is False

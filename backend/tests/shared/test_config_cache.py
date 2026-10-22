"""Tests for the tenant-global config catalog cache.

The behaviours worth pinning here are the ones that would silently regress:
single-flight under a burst (the case the cache exists for), invalidation on
write, and the raw-items contract that keeps a mutating caller from poisoning
what every other user sees.
"""

import asyncio

import pytest

from apis.shared.caching import config_cache
from apis.shared.caching.config_cache import ConfigListCache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """Every test starts on an empty, enabled, default-TTL cache."""
    monkeypatch.delenv("CONFIG_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("CONFIG_CACHE_TTL_SECONDS", raising=False)
    config_cache.get_config_cache().clear()
    yield
    config_cache.get_config_cache().clear()


def _counting_loader(items, calls):
    async def loader():
        calls.append(1)
        return list(items)
    return loader


class TestCaching:
    @pytest.mark.asyncio
    async def test_second_read_does_not_hit_the_loader(self):
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        first = await cache.get_or_load("k", loader)
        second = await cache.get_or_load("k", loader)

        assert len(calls) == 1
        assert first == second == [{"id": "a"}]

    @pytest.mark.asyncio
    async def test_keys_are_independent(self):
        cache = ConfigListCache()
        calls_a, calls_b = [], []

        await cache.get_or_load("a", _counting_loader([{"id": "a"}], calls_a))
        await cache.get_or_load("b", _counting_loader([{"id": "b"}], calls_b))

        assert len(calls_a) == 1
        assert len(calls_b) == 1

    @pytest.mark.asyncio
    async def test_expired_entry_reloads(self, monkeypatch):
        monkeypatch.setenv("CONFIG_CACHE_TTL_SECONDS", "0")
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        await cache.get_or_load("k", loader)
        await cache.get_or_load("k", loader)

        # TTL 0 means every entry is already expired on read.
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_malformed_ttl_falls_back_to_default(self, monkeypatch):
        """A bad env var should degrade to sane caching, not break every read."""
        monkeypatch.setenv("CONFIG_CACHE_TTL_SECONDS", "not-a-number")
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        await cache.get_or_load("k", loader)
        await cache.get_or_load("k", loader)

        assert len(calls) == 1


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_concurrent_miss_loads_once(self):
        """The stampede case: a cold cache and a classroom arriving together.

        Without single flight this is one scan per request, and the pileup
        lands at exactly the moment the burst does.
        """
        cache = ConfigListCache()
        calls = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_loader():
            calls.append(1)
            started.set()
            await release.wait()
            return [{"id": "a"}]

        waiters = [asyncio.create_task(cache.get_or_load("k", slow_loader)) for _ in range(50)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*waiters)

        assert len(calls) == 1
        assert all(r == [{"id": "a"}] for r in results)

    @pytest.mark.asyncio
    async def test_loader_failure_is_not_cached(self):
        """A failed load must not strand later callers on a cached error."""
        cache = ConfigListCache()
        calls = []

        async def failing_loader():
            calls.append(1)
            raise RuntimeError("dynamo down")

        with pytest.raises(RuntimeError):
            await cache.get_or_load("k", failing_loader)

        # Next caller retries rather than inheriting the failure.
        items = await cache.get_or_load("k", _counting_loader([{"id": "a"}], calls))
        assert items == [{"id": "a"}]
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_failure_propagates_to_every_concurrent_waiter(self):
        cache = ConfigListCache()
        release = asyncio.Event()

        async def failing_loader():
            await release.wait()
            raise RuntimeError("dynamo down")

        waiters = [
            asyncio.create_task(cache.get_or_load("k", failing_loader))
            for _ in range(5)
        ]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)

        # The leader raises; the queued waiters re-check, miss, and retry the
        # loader themselves — every one of them ends in an error rather than a
        # silently empty catalog, which is the property that matters.
        assert all(isinstance(r, RuntimeError) for r in results)


class TestInvalidation:
    @pytest.mark.asyncio
    async def test_invalidate_forces_reload(self):
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        await cache.get_or_load("k", loader)
        cache.invalidate("k")
        await cache.get_or_load("k", loader)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_invalidate_oauth_drops_both_entries(self):
        """`enabled` is part of the GSI key, so a write moves providers between
        the enabled-only and full result sets — both must be dropped."""
        calls_enabled, calls_all = [], []
        await config_cache.get_or_load(
            config_cache.OAUTH_PROVIDERS_ENABLED,
            _counting_loader([{"id": "p"}], calls_enabled),
        )
        await config_cache.get_or_load(
            config_cache.OAUTH_PROVIDERS_ALL,
            _counting_loader([{"id": "p"}], calls_all),
        )

        config_cache.invalidate_oauth_providers()

        await config_cache.get_or_load(
            config_cache.OAUTH_PROVIDERS_ENABLED,
            _counting_loader([{"id": "p"}], calls_enabled),
        )
        await config_cache.get_or_load(
            config_cache.OAUTH_PROVIDERS_ALL,
            _counting_loader([{"id": "p"}], calls_all),
        )

        assert len(calls_enabled) == 2
        assert len(calls_all) == 2


class TestIsolationContract:
    @pytest.mark.asyncio
    async def test_caller_cannot_mutate_the_cached_list(self):
        """A caller that appends or sorts its result must not corrupt the entry.

        This is the guard that lets us cache at all: `hydrate_model_roles` and
        `list_tools_with_roles` mutate what they are handed.
        """
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        first = await cache.get_or_load("k", loader)
        first.append({"id": "injected"})
        first.clear()

        second = await cache.get_or_load("k", loader)
        assert second == [{"id": "a"}]
        assert len(calls) == 1


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_disabled_bypasses_the_cache_entirely(self, monkeypatch):
        monkeypatch.setenv("CONFIG_CACHE_ENABLED", "false")
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        await cache.get_or_load("k", loader)
        await cache.get_or_load("k", loader)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_empty_value_is_still_enabled(self, monkeypatch):
        """House style: unset or empty resolves to ON; only literal 'false' disables."""
        monkeypatch.setenv("CONFIG_CACHE_ENABLED", "")
        cache = ConfigListCache()
        calls = []
        loader = _counting_loader([{"id": "a"}], calls)

        await cache.get_or_load("k", loader)
        await cache.get_or_load("k", loader)

        assert len(calls) == 1

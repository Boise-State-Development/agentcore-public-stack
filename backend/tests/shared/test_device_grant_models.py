"""Device-authorization grant model tests.

Pure domain logic — no AWS, no HTTP. The properties worth pinning down are the
ones that make the low-entropy user code safe and the terminal states final.
"""

from __future__ import annotations

import time

import pytest

from apis.shared.auth.device_grants.models import (
    DEVICE_CODE_BYTES,
    GRANT_TTL_SECONDS,
    MIN_POLL_GAP_SECONDS,
    USER_CODE_ALPHABET,
    USER_CODE_LENGTH,
    DeviceGrant,
    GrantStatus,
    generate_device_code,
    generate_user_code,
    hash_device_code,
    normalise_user_code,
)


def make_grant(**overrides: object) -> DeviceGrant:
    now = int(time.time())
    fields: dict[str, object] = {
        "device_code_hash": hash_device_code("dc"),
        "user_code": "CDFG-HJKM",
        "status": GrantStatus.PENDING,
        "created_at": now,
        "expires_at": now + GRANT_TTL_SECONDS,
    }
    fields.update(overrides)
    return DeviceGrant(**fields)  # type: ignore[arg-type]


class TestDeviceCode:
    def test_is_long_enough_to_resist_guessing(self) -> None:
        # 32 bytes url-safe base64 -> 43 chars, no padding.
        assert len(generate_device_code()) >= 40

    def test_codes_are_unique(self) -> None:
        assert len({generate_device_code() for _ in range(50)}) == 50

    def test_is_url_safe(self) -> None:
        code = generate_device_code()
        assert "=" not in code and "+" not in code and "/" not in code

    def test_entropy_constant_is_not_weakened(self) -> None:
        """A guard on intent: this is the only thing protecting a pending grant."""
        assert DEVICE_CODE_BYTES >= 32


class TestUserCode:
    def test_is_grouped_for_transcription(self) -> None:
        code = generate_user_code()
        assert len(code) == USER_CODE_LENGTH + 1
        assert code[USER_CODE_LENGTH // 2] == "-"

    def test_uses_only_unambiguous_characters(self) -> None:
        """0/O, 1/I/L, 2/Z, 5/S and 8/B are indistinguishable in many terminal
        fonts, so none of them may appear."""
        body = generate_user_code().replace("-", "")
        assert set(body) <= set(USER_CODE_ALPHABET)
        for ambiguous in "0O1IL2Z5S8B":
            assert ambiguous not in USER_CODE_ALPHABET

    def test_contains_no_vowels(self) -> None:
        """So a generated code can never spell something unfortunate."""
        for vowel in "AEIOU":
            assert vowel not in USER_CODE_ALPHABET

    def test_codes_vary(self) -> None:
        assert len({generate_user_code() for _ in range(50)}) > 45


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["CDFG-HJKM", "cdfg-hjkm", "CDFGHJKM", " CDFG-HJKM ", "cdfg hjkm", "CD-FG-HJ-KM"],
    )
    def test_accepts_the_ways_people_retype_it(self, raw: str) -> None:
        assert normalise_user_code(raw) == "CDFGHJKM"

    def test_does_not_substitute_characters(self) -> None:
        """The alphabet excludes ambiguous pairs, so a wrong character is a real
        typo and must not be silently 'corrected' into someone else's code."""
        assert normalise_user_code("0DFG-HJKM") != "CDFGHJKM"


class TestHashing:
    def test_is_stable(self) -> None:
        assert hash_device_code("abc") == hash_device_code("abc")

    def test_differs_per_input(self) -> None:
        assert hash_device_code("abc") != hash_device_code("abd")

    def test_does_not_contain_the_input(self) -> None:
        secret = generate_device_code()
        assert secret not in hash_device_code(secret)

    def test_is_hex_sha256(self) -> None:
        digest = hash_device_code("abc")
        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)


class TestExpiry:
    def test_a_fresh_grant_is_not_expired(self) -> None:
        assert make_grant().is_expired() is False

    def test_expiry_is_checked_in_code_not_left_to_dynamodb_ttl(self) -> None:
        """TTL deletion is asynchronous and can lag by up to 48 hours, so an
        expired row is still readable long after it should be usable."""
        now = int(time.time())
        assert make_grant(expires_at=now - 1).is_expired(now) is True

    def test_ttl_is_bounded(self) -> None:
        assert 300 <= GRANT_TTL_SECONDS <= 900


class TestStateMachine:
    def test_pending_is_approvable(self) -> None:
        assert make_grant().is_approvable() is True

    def test_pending_is_not_claimable(self) -> None:
        assert make_grant().is_claimable() is False

    def test_approved_with_a_session_is_claimable(self) -> None:
        grant = make_grant(status=GrantStatus.APPROVED, session_id="sess-1")
        assert grant.is_claimable() is True

    def test_approved_without_a_session_is_not_claimable(self) -> None:
        """Defensive: an approval that failed to record a session must not read
        as claimable, or the poll would hand back nothing."""
        assert make_grant(status=GrantStatus.APPROVED).is_claimable() is False

    def test_expired_is_neither_approvable_nor_claimable(self) -> None:
        now = int(time.time())
        expired = make_grant(status=GrantStatus.APPROVED, session_id="s", expires_at=now - 1)
        assert expired.is_claimable(now) is False
        assert make_grant(expires_at=now - 1).is_approvable(now) is False

    @pytest.mark.parametrize("status", [GrantStatus.CLAIMED, GrantStatus.DENIED])
    def test_terminal_states_are_final(self, status: GrantStatus) -> None:
        """A claimed grant must not be claimable twice, and a denial must not be
        approvable afterwards."""
        grant = make_grant(status=status, session_id="sess-1")
        assert grant.is_claimable() is False
        assert grant.is_approvable() is False


class TestPollThrottle:
    def test_first_poll_is_never_throttled(self) -> None:
        assert make_grant().should_slow_down() is False

    def test_a_poll_inside_the_gap_is_throttled(self) -> None:
        now = int(time.time())
        assert make_grant(last_polled_at=now).should_slow_down(now) is True

    def test_a_poll_after_the_gap_is_allowed(self) -> None:
        now = int(time.time())
        grant = make_grant(last_polled_at=now - MIN_POLL_GAP_SECONDS)
        assert grant.should_slow_down(now) is False

    def test_the_gap_does_not_punish_ordinary_jitter(self) -> None:
        """The advertised interval must be at least the enforced gap, or a
        well-behaved client polling on schedule would be told to slow down."""
        from apis.shared.auth.device_grants.models import POLL_INTERVAL_SECONDS

        assert POLL_INTERVAL_SECONDS >= MIN_POLL_GAP_SECONDS

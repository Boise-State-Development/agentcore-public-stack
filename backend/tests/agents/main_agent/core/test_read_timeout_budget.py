"""The model read timeout must fit inside app-api's proxy read timeout.

Nothing reaches app-api while a model thinks silently (Opus 5.5's thinking
display defaults to "omitted"), so the Runtime-to-app-api hop sees the same
silence as the Bedrock socket. If the Bedrock read timeout were the longer of
the two, the proxy would cut the stream first and the user would get a lost
connection instead of the explained timeout error.
"""

from agents.main_agent.config.constants import Defaults
from agents.main_agent.core.model_config import RetryConfig
from apis.app_api.chat.proxy_routes import (
    _PROXY_READ_TIMEOUT_SECONDS,
    _PROXY_TIMEOUT_SECONDS,
    _build_upstream_client,
)

# The Runtime needs a moment after the timeout to build, persist and stream
# the error before the proxy gives up on it.
_ERROR_DELIVERY_MARGIN_SECONDS = 30


def test_default_read_timeout_leaves_room_for_the_error_to_reach_app_api():
    assert RetryConfig().read_timeout + _ERROR_DELIVERY_MARGIN_SECONDS <= _PROXY_READ_TIMEOUT_SECONDS


def test_env_default_matches_the_dataclass_default():
    assert Defaults.RETRY_READ_TIMEOUT == RetryConfig().read_timeout


def test_upstream_client_raises_only_the_read_timeout():
    client = _build_upstream_client()
    assert client.timeout.read == _PROXY_READ_TIMEOUT_SECONDS
    assert client.timeout.connect == _PROXY_TIMEOUT_SECONDS
    assert client.timeout.write == _PROXY_TIMEOUT_SECONDS
    assert client.timeout.pool == _PROXY_TIMEOUT_SECONDS

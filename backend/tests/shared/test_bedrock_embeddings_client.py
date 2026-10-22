"""generate_embeddings shares one bedrock-runtime client across calls."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.embeddings import bedrock_embeddings as be


@pytest.fixture(autouse=True)
def _fresh_bedrock_client(monkeypatch):
    """The Bedrock client is cached per process; each test builds its own."""
    monkeypatch.setattr(be, "_bedrock_runtime_client", None)


def _client() -> MagicMock:
    client = MagicMock()
    client.invoke_model.side_effect = lambda **kwargs: {
        "body": io.BytesIO(json.dumps({"embedding": [0.1, 0.2]}).encode())
    }
    return client


@pytest.mark.asyncio
async def test_calls_reuse_one_bedrock_client():
    """A client per call paid botocore's model load and a fresh TLS handshake on
    every knowledge base search, ahead of the model's first token."""
    client = _client()
    with patch.object(be.boto3, "client", return_value=client) as factory:
        for _ in range(3):
            assert await be.generate_embeddings(["a", "b"]) == [[0.1, 0.2], [0.1, 0.2]]

    factory.assert_called_once_with("bedrock-runtime", region_name=be.AWS_REGION)
    assert client.invoke_model.call_count == 6

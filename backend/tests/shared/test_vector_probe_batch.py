"""delete_vectors_for_document probes within the GetVectors key limit."""

import asyncio
from unittest.mock import MagicMock, patch

from apis.shared.embeddings import bedrock_embeddings as be


def test_probe_batches_stay_within_the_get_vectors_limit_and_find_every_chunk():
    """At 500 keys per probe every call was rejected by the API (limit 100) and each
    delete fell back to listing the entire index."""
    stored = {f"DOC-a1b2#{i}" for i in range(150)}
    client = MagicMock()

    def get_vectors(vectorBucketName, indexName, keys):
        assert len(keys) <= be.GET_VECTORS_MAX_KEYS
        return {"vectors": [{"key": k} for k in keys if k in stored]}

    client.get_vectors.side_effect = get_vectors
    with patch.object(be.boto3, "client", return_value=client), patch.object(
        be, "_get_vector_store_bucket", return_value="b"
    ), patch.object(be, "_get_vector_store_index", return_value="i"):
        deleted = asyncio.run(be.delete_vectors_for_document("DOC-a1b2"))

    assert deleted == 150
    client.list_vectors.assert_not_called()
    sent = [k for call in client.delete_vectors.call_args_list for k in call.kwargs["keys"]]
    assert set(sent) == stored

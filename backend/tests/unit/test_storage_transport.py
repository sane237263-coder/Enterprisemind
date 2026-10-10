"""Storage transport failures and streaming resource/integrity boundaries."""

import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from urllib3.exceptions import NewConnectionError, ReadTimeoutError

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.ingestion.storage import ObjectStorage


def storage(client):
    result = ObjectStorage.__new__(ObjectStorage)
    result.bucket = "synthetic-bucket"
    result.client = client
    return result


@pytest.mark.parametrize("operation", ["upload", "blocks", "verify"])
@pytest.mark.parametrize(
    "failure",
    [
        ReadTimeoutError(None, "/private-object", "synthetic private details"),
        NewConnectionError(None, "synthetic private endpoint"),
    ],
)
def test_transport_errors_use_safe_storage_code(operation, failure):
    client = SimpleNamespace(
        put_object=Mock(side_effect=failure),
        get_object=Mock(side_effect=failure),
        stat_object=Mock(side_effect=failure),
    )
    target = storage(client)
    with pytest.raises(APIError) as error:
        if operation == "upload":
            target.upload("synthetic-key", BytesIO(b"abc"), "text/plain")
        elif operation == "blocks":
            list(target.blocks("synthetic-key"))
        else:
            target.verify("synthetic-key", 3, hashlib.sha256(b"abc").hexdigest())
    assert error.value.code == ErrorCode.STORAGE_UNAVAILABLE
    assert error.value.details == {}
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


def test_midstream_failure_closes_connection_and_never_returns_download(tmp_path):
    def interrupted(_):
        yield b"prefix"
        raise ReadTimeoutError(None, "/private-object", "synthetic private details")

    response = SimpleNamespace(stream=interrupted, close=Mock(), release_conn=Mock())
    target = storage(SimpleNamespace(get_object=Mock(return_value=response)))
    with pytest.raises(APIError) as error:
        target.download("synthetic-key", tmp_path / "partial", 12, "expected-digest")
    assert error.value.code == ErrorCode.STORAGE_UNAVAILABLE
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_consumer_cancellation_closes_stream_without_becoming_storage_failure():
    response = SimpleNamespace(
        stream=lambda _: iter([b"first", b"second"]),
        close=Mock(),
        release_conn=Mock(),
    )
    target = storage(SimpleNamespace(get_object=Mock(return_value=response)))
    blocks = target.blocks("synthetic-key")
    assert next(blocks) == b"first"
    blocks.close()
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_wrong_download_hash_retains_content_mismatch_code(tmp_path):
    response = SimpleNamespace(stream=lambda _: iter([b"abc"]), close=Mock(), release_conn=Mock())
    target = storage(SimpleNamespace(get_object=Mock(return_value=response)))
    with pytest.raises(APIError) as error:
        target.download(
            "synthetic-key", tmp_path / "content", 3, hashlib.sha256(b"xyz").hexdigest()
        )
    assert error.value.code == ErrorCode.FILE_CONTENT_MISMATCH
    response.close.assert_called_once()
    response.release_conn.assert_called_once()


def test_nontransport_errors_are_not_reclassified():
    target = storage(SimpleNamespace(put_object=Mock(side_effect=ValueError("invalid upload"))))
    with pytest.raises(ValueError, match="invalid upload"):
        target.upload("synthetic-key", BytesIO(b"abc"), "text/plain")

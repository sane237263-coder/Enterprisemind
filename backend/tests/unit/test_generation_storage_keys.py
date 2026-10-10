import re
from datetime import datetime

import pytest

from enterprise_mind.ingestion.storage import ObjectStorage

OWNER = "a" * 64
MAX_SIGNED_BIGINT = (1 << 63) - 1


@pytest.mark.parametrize("prefix", ["generated", "export"])
def test_generation_keys_encode_task_attempt_and_unique_object(prefix: str) -> None:
    first = ObjectStorage.new_key(prefix, task_id=17, owner_token=OWNER)
    second = ObjectStorage.new_key(prefix, task_id=17, owner_token=OWNER)

    assert first != second
    parts = first.split("/")
    assert len(parts) == 5
    assert parts[0] == prefix
    datetime.strptime(parts[1], "%Y%m%dT%H%M%SZ")
    assert parts[2] == "task-17"
    assert parts[3] == f"attempt-{OWNER}"
    assert re.fullmatch(r"[0-9a-f]{32}", parts[4])


def test_generation_key_accepts_maximum_signed_bigint_task_id() -> None:
    key = ObjectStorage.new_key("generated", task_id=MAX_SIGNED_BIGINT, owner_token=OWNER)

    assert key.split("/")[2] == f"task-{MAX_SIGNED_BIGINT}"


def test_legacy_default_key_shape_remains_available() -> None:
    key = ObjectStorage.new_key()

    prefix, timestamp, object_id = key.split("/")
    assert prefix == "original"
    datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ")
    assert re.fullmatch(r"[0-9a-f]{32}", object_id)


@pytest.mark.parametrize(
    ("task_id", "owner_token"),
    [(17, None), (None, OWNER)],
)
def test_generation_identity_requires_both_task_and_owner(
    task_id: int | None, owner_token: str | None
) -> None:
    with pytest.raises(ValueError):
        ObjectStorage.new_key("generated", task_id=task_id, owner_token=owner_token)


@pytest.mark.parametrize("prefix", ["original", "unsupported"])
def test_generation_identity_rejects_non_generation_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError):
        ObjectStorage.new_key(prefix, task_id=17, owner_token=OWNER)


def test_legacy_key_rejects_unknown_prefix() -> None:
    with pytest.raises(ValueError):
        ObjectStorage.new_key("unsupported")


@pytest.mark.parametrize("task_id", [True, 0, -1, 1 << 63])
def test_generation_identity_rejects_invalid_task_id(task_id: int) -> None:
    with pytest.raises(ValueError):
        ObjectStorage.new_key("generated", task_id=task_id, owner_token=OWNER)


@pytest.mark.parametrize("owner_token", ["a" * 63, "g" * 64, "A" * 64])
def test_generation_identity_rejects_invalid_owner_token(owner_token: str) -> None:
    with pytest.raises(ValueError):
        ObjectStorage.new_key("export", task_id=17, owner_token=owner_token)

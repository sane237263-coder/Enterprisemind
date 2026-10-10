import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import urllib3
from minio import Minio
from minio.credentials.providers import StaticProvider
from minio.minioadmin import MinioAdmin

from enterprise_mind.api.errors import APIError, ErrorCode
from enterprise_mind.infrastructure.settings import IngestionSettings, StorageConfig, read_secret

BLOCK_SIZE = 65536
_GENERATION_OWNER_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SIGNED_BIGINT = (1 << 63) - 1


class ObjectStorage:
    def __init__(self, settings: IngestionSettings, config: StorageConfig) -> None:
        self.bucket = settings.minio_bucket
        self.client = Minio(
            settings.minio_endpoint,
            access_key=read_secret(settings.minio_access_key_file).get_secret_value(),
            secret_key=read_secret(settings.minio_secret_key_file).get_secret_value(),
            secure=settings.minio_secure,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(
                    connect=config.connection_timeout_seconds, read=config.read_timeout_seconds
                ),
                retries=False,
            ),
        )

    def check(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)

    @staticmethod
    def new_key(
        prefix: str = "original",
        *,
        task_id: int | None = None,
        owner_token: str | None = None,
    ) -> str:
        if prefix not in {"original", "generated", "export", "derived", "preview"}:
            raise ValueError("unsupported storage object prefix")
        if (task_id is None) != (owner_token is None):
            raise ValueError("generation storage identity requires both task and owner")
        if task_id is None:
            created = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            return prefix + "/" + created + "/" + uuid4().hex
        if prefix not in {"generated", "export"}:
            raise ValueError("generation storage identity requires generated or export prefix")
        if type(task_id) is not int or not 1 <= task_id <= _MAX_SIGNED_BIGINT:
            raise ValueError("generation task id must be a positive signed bigint")
        if (
            not isinstance(owner_token, str)
            or _GENERATION_OWNER_TOKEN.fullmatch(owner_token) is None
        ):
            raise ValueError("generation owner token must be 64 lowercase hex characters")
        created = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}/{created}/task-{task_id}/attempt-{owner_token}/{uuid4().hex}"

    def upload(self, key: str, stream: BinaryIO, mime: str) -> None:
        try:
            self.client.put_object(
                self.bucket,
                key,
                stream,
                length=-1,
                content_type=mime,
                part_size=5 * 1024 * 1024,
                num_parallel_uploads=1,
            )
        except urllib3.exceptions.HTTPError:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE) from None

    def verify(self, key: str, size: int, digest: str) -> None:
        try:
            metadata = self.client.stat_object(self.bucket, key)
        except urllib3.exceptions.HTTPError:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE) from None
        if metadata.size != size:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
        measured = hashlib.sha256()
        total = 0
        for block in self.blocks(key):
            total += len(block)
            if total > size:
                raise APIError(ErrorCode.STORAGE_UNAVAILABLE)
            measured.update(block)
        if total != size or measured.hexdigest() != digest:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE)

    def blocks(self, key: str) -> Iterator[bytes]:
        try:
            response = self.client.get_object(self.bucket, key)
            try:
                yield from response.stream(BLOCK_SIZE)
            finally:
                response.close()
                response.release_conn()
        except urllib3.exceptions.HTTPError:
            raise APIError(ErrorCode.STORAGE_UNAVAILABLE) from None

    def download(self, key: str, destination: Path, size: int, digest: str) -> None:
        checksum = hashlib.sha256()
        total = 0
        with destination.open("wb") as output:
            for data in self.blocks(key):
                total += len(data)
                if total > size:
                    raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)
                checksum.update(data)
                output.write(data)
        if total != size or checksum.hexdigest() != digest:
            raise APIError(ErrorCode.FILE_CONTENT_MISMATCH)

    def remove(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)


def initialize_storage(
    settings: IngestionSettings,
    config: StorageConfig,
    root_access_file: Path,
    root_secret_file: Path,
) -> None:
    access = read_secret(root_access_file).get_secret_value()
    secret = read_secret(root_secret_file).get_secret_value()
    app_access = read_secret(settings.minio_access_key_file).get_secret_value()
    app_secret = read_secret(settings.minio_secret_key_file).get_secret_value()
    if access == app_access:
        raise ValueError("Application storage identity must differ from root")
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(
            connect=config.connection_timeout_seconds, read=config.read_timeout_seconds
        ),
        retries=False,
    )
    root = Minio(
        settings.minio_endpoint,
        access_key=access,
        secret_key=secret,
        secure=settings.minio_secure,
        http_client=http,
    )
    if not root.bucket_exists(settings.minio_bucket):
        root.make_bucket(settings.minio_bucket)
    root.delete_bucket_policy(settings.minio_bucket)
    admin = MinioAdmin(
        endpoint=settings.minio_endpoint,
        credentials=StaticProvider(access, secret),
        secure=settings.minio_secure,
        http_client=http,
    )
    users = json.loads(admin.user_list())
    if app_access not in users:
        admin.user_add(app_access, app_secret)
    # MinIO canned policies are global to the server.  Derive the policy name
    # from the exact bucket so independent test/deployment buckets cannot
    # overwrite each other's resource scope.
    policy_name = (
        "enterprise-mind-bucket-"
        + hashlib.sha256(settings.minio_bucket.encode("utf-8")).hexdigest()[:16]
    )
    admin.policy_add(
        policy_name,
        policy={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetBucketLocation",
                        "s3:ListBucket",
                        "s3:ListBucketMultipartUploads",
                    ],
                    "Resource": ["arn:aws:s3:::" + settings.minio_bucket],
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": ["arn:aws:s3:::" + settings.minio_bucket + "/*"],
                },
            ],
        },
    )
    admin.policy_set(policy_name, user=app_access)
    ObjectStorage(settings, config).check()

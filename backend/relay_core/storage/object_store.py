"""R2 (S3-compatible) object storage client. `boto3` is synchronous; every call runs in a
thread via `asyncio.to_thread` so it doesn't block the event loop the rest of the API/worker
runs on. `region_name="auto"` and the `path` addressing style are required against R2's
endpoint (docs/system-design.md section 5.3).

The underlying boto3 client is built lazily, on first actual use, not in `build_object_store`:
`botocore` validates `endpoint_url` at client-construction time and raises if it's empty, but
`ObjectStore` is constructed on every agent run regardless of route (`relay_core.agent.runner`),
including `direct`/`blocked` runs and `task` runs with no attachments that never touch it. Eager
construction would mean R2 must be configured even to run the test suite or try the demo without
ever uploading a CSV — deferring it means that's only required once something actually calls
`put_bytes`/`get_bytes`.
"""

import asyncio
from functools import lru_cache
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.client import Config as BotoConfig  # type: ignore[import-untyped]

from relay_core.config import Settings


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _client(self) -> Any:
        return _client_for(
            self._settings.r2_endpoint_url,
            self._settings.r2_access_key_id,
            self._settings.r2_secret_access_key,
        )

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        await asyncio.to_thread(
            self._client().put_object,
            Bucket=self._settings.r2_bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def get_bytes(self, key: str) -> bytes:
        response = await asyncio.to_thread(
            self._client().get_object, Bucket=self._settings.r2_bucket, Key=key
        )
        body: bytes = await asyncio.to_thread(response["Body"].read)
        return body

    async def delete(self, key: str) -> None:
        """Idempotent: S3/R2 answer a missing key with success."""
        await asyncio.to_thread(
            self._client().delete_object, Bucket=self._settings.r2_bucket, Key=key
        )


@lru_cache
def _client_for(endpoint_url: str, access_key_id: str, secret_access_key: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=BotoConfig(s3={"addressing_style": "path"}),
    )


def build_object_store(settings: Settings) -> ObjectStore:
    return ObjectStore(settings)

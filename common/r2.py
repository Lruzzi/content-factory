"""Cloudflare R2 (an S3-compatible store) as a short-lived place for a clip: upload it, hand out an expiring link,
delete it. Used only for assisted TikTok posting, where Telegram's 50 MB bot limit would otherwise force a smaller
copy. The secret key stays inside the S3 client; it is never in an address, an error message or a repr."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

MAX_LINK_S = 7 * 86400  # what R2 allows for a signed link


class R2ConfigError(RuntimeError):
    pass


class R2:
    def __init__(
        self, *, account_id: str, bucket: str, access_key: str, secret_key: str, endpoint: str | None = None
    ) -> None:
        self.bucket = bucket
        self.endpoint = endpoint or f"https://{account_id}.r2.cloudflarestorage.com"
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",  # R2 does not take the newer default checksums
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def __repr__(self) -> str:
        return f"R2(bucket={self.bucket!r}, endpoint={self.endpoint!r})"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        account_id_env: str,
        bucket_env: str,
        access_key_env: str,
        secret_key_env: str,
    ) -> R2:
        names = {
            "account_id": account_id_env,
            "bucket": bucket_env,
            "access_key": access_key_env,
            "secret_key": secret_key_env,
        }
        values = {field: env.get(name, "") for field, name in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise R2ConfigError(f"R2 needs these variables in .env: {', '.join(missing)}")
        return cls(**values)

    def upload(self, key: str, path: Path, *, content_type: str = "video/mp4") -> int:
        """Store the file under `key` (replacing what was there). Returns its size in bytes."""
        self._client.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})
        return path.stat().st_size

    def presign(self, key: str, *, ttl_s: int, filename: str | None = None) -> str:
        """A link that downloads the file for `ttl_s` seconds, then stops working."""
        if ttl_s <= 0:
            raise ValueError("a link must be valid for a positive number of seconds")
        if ttl_s > MAX_LINK_S:
            raise ValueError("R2 links cannot be valid for more than 7 days")
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return str(self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl_s))

    def exists(self, key: str) -> bool:
        """Whether the file is stored. Anything but "not found" (a refused key, an outage) is raised, not guessed."""
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        """Remove the file. Removing one that is already gone is not an error."""
        self._client.delete_object(Bucket=self.bucket, Key=key)

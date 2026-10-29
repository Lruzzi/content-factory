"""Cloudflare R2 as a short-lived place for a clip: upload it, hand out an expiring link, delete it. The secret key
never shows up in an address, an error or a repr."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fake_s3_server import fake_s3

from common.r2 import R2, R2ConfigError

SECRET = "test-secret-key-0123456789"  # noqa: S105  # gitleaks:allow
ACCESS = "test-access-key-id"


@pytest.fixture
def store(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(bytes(range(256)) * 400)
    with fake_s3() as (fake, url):
        yield R2(account_id="acct", bucket="clips", access_key=ACCESS, secret_key=SECRET, endpoint=url), fake, clip


def test_a_file_is_uploaded_under_its_key_and_its_size_is_returned(store):
    r2, fake, clip = store
    assert r2.upload("tiktok/abc.mp4", clip) == clip.stat().st_size
    assert fake.objects["clips/tiktok/abc.mp4"] == clip.read_bytes()


def test_the_link_is_signed_expires_when_asked_and_names_the_file_for_the_download(store):
    r2, _, _ = store
    url = r2.presign("tiktok/abc.mp4", ttl_s=3600, filename="clip-1.mp4")
    query = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/clips/tiktok/abc.mp4" and query["X-Amz-Expires"] == ["3600"]
    assert "X-Amz-Signature" in query and "clip-1.mp4" in query["response-content-disposition"][0]
    assert SECRET not in url


def test_a_link_is_never_valid_for_more_than_the_seven_days_r2_allows(store):
    r2, _, _ = store
    with pytest.raises(ValueError, match="7 days"):
        r2.presign("k", ttl_s=8 * 86400)
    with pytest.raises(ValueError, match="positive"):
        r2.presign("k", ttl_s=0)


def test_a_file_is_deleted_and_deleting_it_again_is_not_an_error(store):
    r2, fake, clip = store
    r2.upload("tiktok/abc.mp4", clip)
    r2.delete("tiktok/abc.mp4")
    r2.delete("tiktok/abc.mp4")
    assert "clips/tiktok/abc.mp4" not in fake.objects


def test_a_refusal_from_the_store_is_a_clear_error_without_the_secret(store):
    r2, fake, clip = store
    fake.fail["PUT"] = 403
    with pytest.raises(Exception) as caught:
        r2.upload("k.mp4", clip)
    assert SECRET not in str(caught.value) and SECRET not in repr(r2)


def test_the_store_is_built_from_the_variables_named_in_the_config():
    env = {"R_ACCT": "acct", "R_BUCKET": "clips", "R_KEY": ACCESS, "R_SECRET": SECRET}
    r2 = R2.from_env(
        env,
        account_id_env="R_ACCT",
        bucket_env="R_BUCKET",
        access_key_env="R_KEY",
        secret_key_env="R_SECRET",  # noqa: S106
    )
    assert "acct.r2.cloudflarestorage.com" in r2.endpoint and r2.bucket == "clips"


def test_a_missing_variable_is_named_in_the_error():
    with pytest.raises(R2ConfigError, match="R_SECRET"):
        R2.from_env(
            {"R_ACCT": "a", "R_BUCKET": "b", "R_KEY": "k"},
            account_id_env="R_ACCT", bucket_env="R_BUCKET", access_key_env="R_KEY", secret_key_env="R_SECRET"  # noqa: S106,
        )  # fmt: skip

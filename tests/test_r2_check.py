"""`python -m common.r2_check`: proves the R2 keys work, that a signed link downloads the file, that an unsigned
request is refused (the bucket is private), and that deleting works. It leaves nothing behind and prints no secret."""

from __future__ import annotations

import io

import pytest
from fake_s3_server import fake_s3

from common import r2_check
from common.r2 import R2

SECRET = "test-secret-key-0123456789"  # noqa: S105  # gitleaks:allow


@pytest.fixture
def store():
    with fake_s3() as (fake, url):
        yield R2(account_id="acct", bucket="clips", access_key="ak", secret_key=SECRET, endpoint=url), fake


def run(store, **kw):
    out = io.StringIO()
    return r2_check.run(store, out=out, **kw), out.getvalue()


def test_a_working_private_bucket_passes_every_step_and_is_left_empty(store):
    r2, fake = store
    ok, text = run(r2)
    assert ok and text.count("PASS") >= 4 and "FAIL" not in text
    assert fake.objects == {}  # the test file is gone again


def test_the_check_shows_that_an_unsigned_request_is_refused(store):
    r2, fake = store
    ok, text = run(r2)
    assert any(m == "GET" for m, _ in fake.calls) and "refused" in text.lower()


def test_a_bucket_that_answers_without_a_signature_is_reported_as_public_and_still_cleaned_up(store):
    r2, fake = store
    fake.private = False
    ok, text = run(r2)
    assert not ok and "PUBLIC" in text and fake.objects == {}


def test_a_store_that_refuses_the_upload_is_a_clear_failure_without_the_secret(store):
    r2, fake = store
    fake.fail["PUT"] = 403
    ok, text = run(r2)
    assert not ok and "FAIL" in text and "upload" in text.lower() and SECRET not in text


def test_a_deleted_file_no_longer_downloads_through_its_old_link(store):
    r2, fake = store
    ok, text = run(r2)
    assert ok and "no longer downloads" in text


def test_the_public_address_you_give_is_tried_without_a_signature_and_must_be_refused(store, monkeypatch):
    r2, fake = store
    seen: list[str] = []

    class Client:
        def get(self, url, **kw):
            seen.append(url)
            return type(
                "R",
                (),
                {"status_code": 200 if "pub-test" not in url and "X-Amz-Signature" in url else 403, "content": b""},
            )()

        def close(self):
            pass

    ok, text = run(r2, public_url="https://pub-test.r2.dev", http=Client())
    assert any(u.startswith("https://pub-test.r2.dev/") for u in seen)
    assert "r2.dev" in text


def test_missing_keys_are_named_and_nothing_is_tried(monkeypatch, capsys):
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID"):
        monkeypatch.setenv(name, "x")
    assert r2_check.main([]) == 1
    assert "R2_SECRET_ACCESS_KEY" in capsys.readouterr().out


def test_any_error_answer_without_a_signature_counts_as_refused_only_a_download_counts_as_public(store):
    r2, fake = store

    class Client:
        """Answers 400 to an unsigned request, as R2's own address does, and serves a signed one."""

        def __init__(self, real):
            self.real = real

        def get(self, url, **kw):
            if "X-Amz-Signature" not in url and not url.startswith("https://pub-"):
                return type("R", (), {"status_code": 400, "content": b"Bad Request"})()
            return self.real.get(url, **kw)

        def close(self):
            pass

    import httpx2

    ok, text = run(r2, http=Client(httpx2.Client()))
    assert ok and "refused" in text and "PUBLIC" not in text

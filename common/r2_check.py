"""Try the R2 keys for real: `python -m common.r2_check [https://pub-....r2.dev]` (or `make r2-check`). It uploads a
tiny test file, downloads it through a signed link, checks that the same address WITHOUT a signature is refused (so
the bucket is private; give your r2.dev address to test that door too), deletes the file and checks it is gone.
Nothing is left behind, and no secret is printed."""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import IO, Any

import httpx2

from common.r2 import R2, R2ConfigError

TIMEOUT_S = 30


def run(r2: R2, *, out: IO[str] = sys.stdout, public_url: str | None = None, http: Any = None) -> bool:
    """Run every step; True only if all of them are as they should be."""
    client = http or httpx2.Client(timeout=TIMEOUT_S, follow_redirects=False)
    key = f"check/{uuid.uuid4().hex}.txt"
    content = f"content-factory r2 check {uuid.uuid4().hex}".encode()
    good = True

    def step(ok: bool, text: str) -> None:
        nonlocal good
        good = good and ok
        out.write(f"{'PASS' if ok else 'FAIL'}  {text}\n")

    uploaded = False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "check.txt"
            path.write_bytes(content)
            try:
                r2.upload(key, path, content_type="text/plain")
                uploaded = True
                step(True, f"upload to bucket {r2.bucket!r} works")
            except Exception as exc:
                step(
                    False,
                    f"upload failed ({type(exc).__name__}): check the keys, bucket name and the token's bucket scope",
                )
                return False
        link = r2.presign(key, ttl_s=300)
        signed = client.get(link)
        step(
            signed.status_code == 200 and signed.content == content,
            f"a signed link downloads the file (HTTP {signed.status_code})",
        )
        bare = f"{r2.endpoint}/{r2.bucket}/{key}"
        unsigned = client.get(bare)
        refused = not (unsigned.status_code < 300 and unsigned.content == content)  # only a real download is public
        step(
            refused,
            "the same address without a signature is refused"
            if refused
            else f"PUBLIC: it answered HTTP {unsigned.status_code} without a signature",
        )
        if public_url:
            open_door = client.get(f"{public_url.rstrip('/')}/{key}")
            ok = not (open_door.status_code < 300 and open_door.content == content)
            step(
                ok,
                f"{public_url} refuses it"
                if ok
                else f"PUBLIC: {public_url} served the file (HTTP {open_door.status_code}): turn off the public URL",
            )
        else:
            out.write(
                "note  the r2.dev address was not tested: pass it as an argument, or check Settings in the dashboard\n"
            )
    finally:
        if uploaded:
            try:
                r2.delete(key)
            except Exception as exc:
                step(False, f"could not delete the test file {key!r} ({type(exc).__name__}): remove it by hand")
                uploaded = False
    if uploaded:
        gone = client.get(link)
        step(
            gone.status_code in (403, 404),
            f"after deleting, the old link no longer downloads (HTTP {gone.status_code})",
        )
    return good


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    values = dict(os.environ if env is None else env)
    try:
        r2 = R2.from_env(
            values, account_id_env="R2_ACCOUNT_ID", bucket_env="R2_BUCKET",
            access_key_env="R2_ACCESS_KEY_ID", secret_key_env="R2_SECRET_ACCESS_KEY",  # noqa: S106
        )  # fmt: skip
    except R2ConfigError as exc:
        print(exc)
        return 1
    return 0 if run(r2, public_url=args[0] if args else None) else 1


if __name__ == "__main__":
    sys.exit(main())

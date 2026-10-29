"""Sign in one YouTube account: `make youtube-auth ACCOUNT=yt_test`.

Prints a link, you open it in a browser and approve with the Google account that owns the channel, and the refresh token
is saved to the account's `token_file` (accounts.yaml). Then it names the channel, so you can check it is the right one:
Google asks which account and channel, and a test account must never be signed in with a brand channel.

If the Google Cloud app is in "Testing" status the sign-in expires after 7 days: run this again."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

import common  # noqa: F401  # puts config/ on the import path, which config_schema needs
from config_schema import ROOT
from services.publisher import google_oauth as g
from services.publisher.youtube import API, YouTube, YouTubeError

REPO = Path(__file__).resolve().parents[2]  # token files in accounts.yaml are relative to the repository


def main(
    argv: list[str] | None = None,
    *,
    accounts_file: Path = ROOT / "accounts.yaml",
    base_dir: Path = REPO,
    api_base: str | None = None,
    show: Callable[[str], None] | None = None,
    timeout_s: float = 300,
) -> int:
    parser = argparse.ArgumentParser(description="Sign in one YouTube account.")
    parser.add_argument("--account", required=True, help="an account id from accounts.yaml, such as yt_test")
    parser.add_argument(
        "--port", type=int, default=0, help="a fixed local port for Google's answer (default: any free one)"
    )
    parser.add_argument("--wait-min", type=float, help="how long to wait for you to approve (default 5 minutes)")
    args = parser.parse_args(argv)
    accounts = {a["id"]: a for a in yaml.safe_load(accounts_file.read_text())["accounts"]}
    account = accounts.get(args.account)
    if account is None:
        print(f"unknown account {args.account!r}; accounts: {', '.join(sorted(accounts))}", file=sys.stderr)
        return 2
    credentials = account.get("credentials", {})
    if account.get("platform") != "shorts" or "client_secrets_file" not in credentials:
        print(f"{args.account} is not a YouTube account (platform shorts with a client_secrets_file)", file=sys.stderr)
        return 2
    try:
        client = g.load_client_secrets(base_dir / credentials["client_secrets_file"])
    except g.GoogleAuthError as exc:
        print(
            f"{exc}. Expected the Google Cloud Desktop app client file at {credentials['client_secrets_file']} "
            "(Google Cloud > Clients > Create client > Desktop app > download the JSON).",
            file=sys.stderr,
        )
        return 2
    store = g.TokenStore(base_dir / credentials["token_file"])
    if show is None:

        def show(link: str) -> None:
            print(
                "\nOpen this link in a browser, sign in with the Google account that owns the channel, and approve:\n"
            )
            print(link, "\n")
            print("Waiting for Google's answer...", flush=True)

    try:
        g.authorize(
            client, store, show=show, timeout_s=args.wait_min * 60 if args.wait_min else timeout_s, port=args.port
        )
        tokens = g.AccessTokens(client, store)
        who = YouTube(tokens.get, base_url=api_base or API).channel()
    except (g.GoogleAuthError, YouTubeError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(f"\nSigned in {args.account} as the YouTube channel: {who['title']} ({who['id']}).")
    print(f"The sign-in is saved in {credentials['token_file']}. If that is not the channel you meant, run this again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

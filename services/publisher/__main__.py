"""Run a publisher: `python -m services.publisher shorts|facebook|reels|tiktok`. Needs DATABASE_URL and REDIS_URL, and
for facebook and reels the token and id variables named in accounts.yaml; MEDIA_ROOT (default ./media) is optional.
TikTok is assisted (clip and caption go to your Telegram): it needs TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from common import heartbeat, pipeline
from common.alerts import make_alerter
from common.db import make_engine
from common.media import LocalStore
from common.r2 import R2
from common.rooms import from_config as rooms_from_config
from common.streams import Consumer, connect, consumer_name
from common.telegram import Telegram
from config_schema import ROOT
from services.publisher import accounts as acc
from services.publisher import google_oauth as g
from services.publisher.base import PlatformPublisher
from services.publisher.clipstore import ClipStore
from services.publisher.facebook import FacebookPublisher
from services.publisher.instagram import InstagramPublisher
from services.publisher.meta import Meta
from services.publisher.service import PublisherService
from services.publisher.shorts import ShortsPublisher
from services.publisher.tiktok_assisted import TikTokAssistedPublisher
from services.publisher.youtube import YouTube

log = logging.getLogger("publisher")
BLOCK_MS = 5000
BASE = Path(__file__).resolve().parents[2]  # token and client files in accounts.yaml are relative to the repository


_CLIENTS: dict[str, YouTube] = {}  # one per account, so access tokens are reused


def youtube_for(rules: acc.AccountRules) -> YouTube:
    if rules.id not in _CLIENTS:
        credentials = rules.credentials
        client = g.load_client_secrets(BASE / credentials["client_secrets_file"])
        tokens = g.AccessTokens(client, g.TokenStore(BASE / credentials["token_file"]))
        _CLIENTS[rules.id] = YouTube(tokens.get)
    return _CLIENTS[rules.id]


def meta_for(rules: acc.AccountRules) -> Meta:
    return Meta(os.environ[rules.credentials["token_env"]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a publisher service.")
    parser.add_argument("platform", choices=["shorts", "facebook", "reels", "tiktok"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    engine, client = make_engine(), connect(os.environ["REDIS_URL"], block_ms=BLOCK_MS)
    heartbeat.start(engine, f"publisher-{args.platform}")
    store, accounts = LocalStore(Path(os.environ.get("MEDIA_ROOT", "media"))), acc.load(ROOT / "accounts.yaml")
    alert = make_alerter(f"publisher-{args.platform}", os.environ, rooms=rooms_from_config())
    publisher: PlatformPublisher
    if args.platform == "shorts":
        publisher = ShortsPublisher(engine, store, accounts, youtube_for, alert=alert)
    elif args.platform == "facebook":
        publisher = FacebookPublisher(engine, store, accounts, meta_for, env=os.environ, alert=alert)
    elif args.platform == "tiktok":
        if acc.tiktok_posting(ROOT / "accounts.yaml") != "assisted":
            raise SystemExit(
                "TikTok posting through the API is not built: set tiktok.posting: assisted in accounts.yaml"
            )
        chats = [int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(" ", "").split(",") if x]
        if not chats or not os.environ.get("TELEGRAM_BOT_TOKEN"):
            raise SystemExit("assisted TikTok needs TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS")
        host = acc.media_host(ROOT / "accounts.yaml")
        store_r2 = None
        if host is not None and os.environ.get(host.bucket_env):  # R2 is used once its variables are in .env
            store_r2 = R2.from_env(
                os.environ,
                account_id_env=host.account_id_env,
                bucket_env=host.bucket_env,
                access_key_env=host.access_key_env,
                secret_key_env=host.secret_key_env,
            )
        publisher = TikTokAssistedPublisher(
            engine, store, accounts, Telegram(os.environ["TELEGRAM_BOT_TOKEN"]), chats, alert=alert,
            r2=store_r2, host=host if store_r2 else None, rooms=rooms_from_config(),
        )  # fmt: skip
        log.info("TikTok clips go %s", "as an R2 link with a delete button" if store_r2 else "as Telegram files")
    else:
        link_clips = None
        if acc.instagram_upload(ROOT / "accounts.yaml") == "r2":
            host = acc.media_host(ROOT / "accounts.yaml")
            if host is not None and os.environ.get(host.bucket_env):
                link_clips = ClipStore(
                    engine,
                    R2.from_env(
                        os.environ,
                        account_id_env=host.account_id_env,
                        bucket_env=host.bucket_env,
                        access_key_env=host.access_key_env,
                        secret_key_env=host.secret_key_env,
                    ),
                    host,
                )
            else:
                log.warning("instagram.upload is r2 but the R2 variables are not set: using the resumable upload")
        publisher = InstagramPublisher(engine, store, accounts, meta_for, env=os.environ, alert=alert, clips=link_clips)
        log.info("Instagram clips go %s", "as a signed R2 link" if link_clips else "as a resumable upload")
    service = PublisherService(
        engine,
        Consumer(
            client, pipeline.stream_for("publish"), f"publisher-{args.platform}", consumer_name(),
            visibility_timeout_s=1800, dlq="dlq",
        ),
        publisher,
        alert=alert,
    )  # fmt: skip
    log.info("publisher %s waiting for approved clips", args.platform)
    while not stop.is_set():
        try:
            service.step(block_ms=BLOCK_MS)
        except Exception:
            log.exception("publisher loop failed; trying again")
            stop.wait(5)


if __name__ == "__main__":
    main()

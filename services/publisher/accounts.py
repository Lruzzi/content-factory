"""The rules of each publishing account, read from accounts.yaml: daily cap, spacing, posting windows, visibility.
Each account gets the file's `defaults` for what it does not set itself. Credentials are only named here (a token
file), never read: the platform code that uses an account reads its own token."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

FALLBACK = {"daily_cap": 3, "min_spacing_min": 120, "post_windows": [], "visibility": "public"}


@dataclass(frozen=True)
class AccountRules:
    id: str
    platform: str
    daily_cap: int
    min_spacing_min: int
    post_windows: list[str]
    visibility: str
    test: bool
    credentials: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Accounts:
    timezone: ZoneInfo
    by_id: dict[str, AccountRules]


def load(path: Path) -> Accounts:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    defaults = FALLBACK | (raw.get("defaults") or {})
    rules = {}
    for row in raw["accounts"]:
        merged = defaults | row
        rules[row["id"]] = AccountRules(
            id=row["id"],
            platform=row["platform"],
            daily_cap=int(merged["daily_cap"]),
            min_spacing_min=int(merged["min_spacing_min"]),
            post_windows=list(merged["post_windows"]),
            visibility=str(merged["visibility"]),
            test=bool(row.get("test", False)),
            credentials=dict(row.get("credentials") or {}),
        )
    return Accounts(ZoneInfo(raw.get("timezone", "UTC")), rules)


def tiktok_posting(path: Path) -> str:
    """How TikTok clips are posted: `assisted` (the clip and its caption are sent to Telegram, you post from the app) or
    `api` (TikTok's Content Posting API, which posts publicly only for audited apps). `assisted` when the file says
    nothing."""
    mode = (yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("tiktok") or {}).get("posting", "assisted")
    if mode not in ("assisted", "api"):
        raise ValueError(f"tiktok.posting must be assisted or api, not {mode!r}")
    return str(mode)


def instagram_upload(path: Path) -> str:
    """How Instagram clips reach Instagram: `r2` (the clip is stored in R2 and Instagram fetches it through a signed
    link and processes it in the background: reliable for long, heavy clips) or `resumable` (Instagram's own upload;
    its final request has a deadline of about 48 s that large clips miss). `resumable` when the file says nothing."""
    mode = (yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("instagram") or {}).get("upload", "resumable")
    if mode not in ("r2", "resumable"):
        raise ValueError(f"instagram.upload must be r2 or resumable, not {mode!r}")
    return str(mode)


@dataclass(frozen=True)
class MediaHost:
    """Where clips are kept for a while so they can be downloaded (Cloudflare R2). The `*_env` fields name the
    variables in .env that hold the bucket and the keys; nothing secret is in accounts.yaml."""

    bucket_env: str
    account_id_env: str
    access_key_env: str
    secret_key_env: str
    link_ttl_min: int  # how long a download link works
    keep_after_approval_h: float  # an approved clip stays stored this long after you approved it
    max_age_h: float  # a file is deleted when it is older than this, whatever else happens
    max_total_gb: float  # above this much stored, clips are sent as files instead (R2's free tier is 10 GB)


def media_host(path: Path) -> MediaHost | None:
    """The `media_host` section, or None when there is none or it names another provider than r2."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("media_host") or {}
    if raw.get("provider") != "r2":
        return None
    return MediaHost(
        bucket_env=str(raw["bucket_env"]),
        account_id_env=str(raw["account_id_env"]),
        access_key_env=str(raw["access_key_env"]),
        secret_key_env=str(raw["secret_key_env"]),
        link_ttl_min=int(raw.get("link_ttl_min", 2880)),
        keep_after_approval_h=float(raw.get("keep_after_approval_h", 24)),
        max_age_h=float(raw.get("max_age_h", 48)),
        max_total_gb=float(raw.get("max_total_gb", 8)),
    )

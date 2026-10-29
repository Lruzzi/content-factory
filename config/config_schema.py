"""Load and validate Content Factory config (schema version 2).

Merge order: profiles/_defaults.yaml -> profiles/<name>.yaml -> sources[i].overrides
Usage:  python config_schema.py        # validates every profile, accounts.yaml, workers.yaml
Deps:   pydantic>=2 (MIT), PyYAML (MIT)
"""

from __future__ import annotations

import copy
import fnmatch
import os
import re
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# CONFIG_DIR lets the real profiles, accounts and workers live outside the repo (default: this folder)
ROOT = Path(os.environ.get("CONFIG_DIR") or Path(__file__).parent).resolve()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown key = typo = error


# ---------- live capture / discover / scout ---------------------------------
class Live(Strict):
    capture: bool = True
    check_every_min: int = Field(4, ge=1, le=60)
    segment_min: int = Field(10, ge=2, le=30)
    segment_overlap_s: int = Field(120, ge=0, le=600)
    quality: Literal["480p", "720p", "1080p"] = "720p"
    # Never record below this: a stream that offers nothing at or above it (a recording still being processed shows only
    # 360p for hours) is left alone, so those hours come from the download path in full quality instead.
    min_quality: Literal["480p", "720p", "1080p"] = "720p"
    max_buffer_h: int = Field(3, ge=1, le=24)
    max_hold_h: int = Field(12, ge=1, le=72)  # ceiling for segments a pending job still needs
    chat_retention_days: int = Field(14, ge=1, le=365)  # raw chat rows; per-window counts are kept longer
    vod_fallback: bool = True
    vod_wait_h: int = Field(24, ge=1, le=72)
    skip_members_only: bool = True

    @model_validator(mode="after")
    def minimum_not_above_quality(self):
        if int(self.min_quality[:-1]) > int(self.quality[:-1]):
            raise ValueError("live.min_quality must not be above live.quality")
        return self

    @model_validator(mode="after")
    def overlap_fits(self):
        if self.segment_overlap_s >= self.segment_min * 60:
            raise ValueError("live.segment_overlap_s must be shorter than one segment")
        return self

    @model_validator(mode="after")
    def hold_covers_buffer(self):
        if self.max_hold_h < self.max_buffer_h:
            raise ValueError("live.max_hold_h must be at least live.max_buffer_h")
        return self


class Discover(Strict):
    poll_every_min: int = Field(30, ge=5)
    max_age_days: int = Field(60, ge=1, le=365)
    backfill_per_source: int = Field(5, ge=0, le=50)
    daily_per_source: int = Field(2, ge=0, le=20)
    min_duration_s: int = 300
    max_duration_s: int = 43200
    include_shorts: bool = False
    title_include: list[str] = []
    title_exclude: list[str] = []
    # An old video is only transcribed in part: the busiest stretch of this length (YouTube's "most replayed" graph
    # when it has one), so a five-hour stream does not use up a day of Groq audio.
    max_window_min: int = Field(60, ge=10, le=600)
    # Nothing is fetched while the last 24 hours of Groq audio are above this share of the daily limit, and nothing
    # while this many jobs wait to be downloaded or transcribed: live capture comes first.
    max_groq_day_share: float = Field(0.5, ge=0, le=1)
    max_backlog: int = Field(6, ge=0, le=100)


class Scout(Strict):
    enabled: bool = False
    run_every_days: int = Field(7, ge=1)
    keywords: list[str] = []
    region: str = "ID"
    lookback_days: int = Field(30, ge=1, le=90)
    min_channel_views: int = 0
    max_suggestions: int = Field(10, ge=1, le=50)

    @model_validator(mode="after")
    def needs_keywords(self):
        if self.enabled and not self.keywords:
            raise ValueError("scout.enabled needs at least one keyword")
        return self


# ---------- signals ----------------------------------------------------------
class Signal(Strict):
    enabled: bool = False
    weight: float = Field(0.0, ge=0, le=1)
    window_s: int | None = None
    percentile: int | None = None
    terms: list[str] = []


class ChatSignal(Signal):
    """Live chat as a signal. A stretch where the chat comes in much faster than usual is a candidate moment: at least
    `min_messages` within two buckets of `window_s` seconds AND `min_ratio` times the stream's usual rate. People
    react after seeing something, so the event is taken `lag_s` seconds before the burst, and the clip runs from
    `before_s` ahead of that to `after_s` after the burst."""

    min_messages: int = Field(5, ge=2)
    min_ratio: float = Field(3.0, ge=1)
    lag_s: int = Field(12, ge=0, le=60)
    before_s: int = Field(20, ge=0, le=60)
    after_s: int = Field(8, ge=0, le=60)
    max_per_segment: int = Field(3, ge=1, le=10)


class Transcript(Strict):
    enabled: bool = True
    mode: Literal["candidates_only", "full"] = "candidates_only"  # live segments
    vod_mode: Literal["candidates_only", "full"] = "candidates_only"  # downloaded videos and ended streams
    margin_s: int = 60
    engine: Literal["groq_api", "faster_whisper", "openai_api"] = "groq_api"
    model: str = "whisper-large-v3-turbo"
    # Used when Groq's free limit is reached (only "openrouter" is built; "none" makes the segment wait for Groq)
    fallback_engine: Literal["openrouter", "faster_whisper", "openai_api", "none"] = "openrouter"
    fallback_model: str = "openai/whisper-large-v3-turbo"
    # Written the way the speakers talk, mixed words included: Whisper takes the spelling and style from it (it accepts
    # one language only, so this is how English slang inside Indonesian speech is kept right). About 200 tokens at most.
    prompt: str = Field("", max_length=600)
    # Sentences Whisper makes up from silence and game sound: one that begins with any of these is dropped, with its
    # words. Never a phrase a streamer really says to a person ("terima kasih donasinya" is real, so it is not here).
    ignore_phrases: list[str] = []
    # Names a speaker says that Whisper often writes wrongly (hero names, items, game terms). A clip's words are checked
    # against this list by the `transcript_fix` model, which may only change a word into one of these entries.
    glossary: list[str] = Field([], max_length=400)
    fix_prompt: str = "prompts/transcript_fix.md"


class Signals(Strict):
    heatmap: Signal
    chat: ChatSignal
    loudness: Signal
    scene_cut: Signal
    keywords: Signal
    transcript: Transcript

    @model_validator(mode="after")
    def at_least_one(self):
        if not any(
            s.enabled and s.weight > 0 for s in (self.heatmap, self.chat, self.loudness, self.scene_cut, self.keywords)
        ):
            raise ValueError("enable at least one weighted signal")
        return self


# ---------- selection / render -----------------------------------------------
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY"}


class LLMSpec(Strict):
    """Which LLM to call and how. `anthropic` talks to Claude; `openai_compatible` talks to any API that speaks the
    OpenAI chat format: OpenRouter, Groq, Gemini's compatible endpoint, or a local server such as Ollama.
    The key is never in config: `api_key_env` names the environment variable that holds it."""

    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    base_url: str | None = None  # openai_compatible only
    max_output_tokens: int = Field(2048, ge=64, le=64000)
    timeout_s: float = Field(120, gt=0, le=900)
    max_retries: int = Field(2, ge=0, le=5)  # the SDK's own retries on 429 and 5xx; the job retries sit on top
    effort: Literal["low", "medium", "high"] | None = None  # anthropic only
    json_mode: bool | None = None  # openai_compatible only: ask for JSON output (not every model supports it)

    @model_validator(mode="after")
    def provider_rules(self):
        if self.provider == "anthropic":
            for name in ("base_url", "json_mode"):
                if getattr(self, name) is not None:
                    raise ValueError(f"llm {name} is only for provider openai_compatible")
            self.api_key_env = self.api_key_env or DEFAULT_KEY_ENV["anthropic"]
        else:
            if self.effort is not None:
                raise ValueError("llm effort is only for provider anthropic")
            if not self.base_url:
                raise ValueError("llm base_url is required for provider openai_compatible")
            if not self.api_key_env:
                raise ValueError("llm api_key_env is required for provider openai_compatible")
            _check_base_url(self.base_url)
        if not ENV_NAME.fullmatch(self.api_key_env or ""):
            raise ValueError(
                "llm api_key_env must be the NAME of an environment variable such as OPENROUTER_API_KEY, "
                "never the key itself"
            )
        return self


def _check_base_url(url: str) -> None:
    parts = urlparse(url)
    local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
    if not (parts.scheme == "https" or local) or not parts.hostname or parts.username or parts.password:
        raise ValueError(
            f"llm base_url {url!r} must be an https URL (http only for localhost) with no user or password in it"
        )


class LLMJudge(LLMSpec):
    enabled: bool = True
    prompt: str
    max_candidates_sent: int = 8
    # Backups, tried in order when the primary fails (rate limit, outage, bad key); each is checked like the primary.
    fallbacks: list[LLMSpec] = Field(default_factory=list, max_length=3)
    cooldown_s: float = Field(60, ge=0, le=3600)  # a failed provider is skipped for this long

    @model_validator(mode="after")
    def chain_has_no_repeats(self):
        keys = [llm_price_key(self), *(llm_price_key(f) for f in self.fallbacks)]
        if len(set(keys)) != len(keys):
            raise ValueError("llm_judge lists the same provider and model twice")
        return self


class LLMTask(LLMJudge):
    """A model of its own for one task (see `Selection.llm_tasks`): the judge's fields, minus the prompt, which the task
    already has."""

    prompt: str = ""


Task = Literal["recap_match", "recap_pick", "package", "transcript_fix"]  # selection itself is the judge's


class Price(Strict):
    """US dollars per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)


Kind = Literal["highlight", "donation", "tutorial", "news", "funny"]


def check_clip_length(length: tuple[int, int]) -> None:
    lo, hi = length
    if not 5 <= lo < hi <= 180:
        raise ValueError(f"clip_length_s {length} must be 5 <= min < max <= 180")


class KindRule(Strict):
    clip_length_s: tuple[int, int] | None = None  # None = use selection.clip_length_s

    @model_validator(mode="after")
    def length_ok(self):
        if self.clip_length_s:
            check_clip_length(self.clip_length_s)
        return self


class Callouts(Strict):
    """Words a streamer shouts at a big play (savage, maniac). Each one found in the transcript becomes a highlight
    candidate whether or not the LLM judge proposed it: the judge sees only words, and a lone "Maniac!" gives it
    nothing to go on."""

    terms: list[str] = Field(min_length=1)
    ignore_near: list[str] = []  # a sentence with one of these is a viewer's message being read, not a play
    before_s: float = Field(20, ge=0, le=60)  # the fight that led to it
    after_s: float = Field(10, ge=0, le=60)  # the reaction
    score: float = Field(0.6, ge=0, le=1)  # what the candidate is worth against the judge's own (selection.min_score)


class Trim(Strict):
    """Where a clip is cut. The sentence times Whisper gives begin well before the first word when there is silence or
    game sound first, and end while sound goes on, so a clip is cut to the words: it starts `lead_s` before the first
    one and ends `tail_s` after the last one (never into the next word), and never gets shorter than its kind allows."""

    lead_s: float = Field(0.4, ge=0, le=3)
    tail_s: float = Field(0.6, ge=0, le=3)


class Selection(Strict):
    mode: Literal["signals", "hybrid", "transcript"] = "signals"
    slice_s: int = 10
    top_k: int = 5
    min_score: float = Field(0.55, ge=0, le=1)
    min_gap_s: int = 120
    clip_length_s: tuple[int, int]
    snap_to_sentence: bool = True
    dedupe_overlap: float = Field(0.5, ge=0, le=1)
    kinds: dict[Kind, KindRule] = {}
    callouts: Callouts | None = None
    # At most this many clips are made in 24 hours (all sources together; a rejected one gives its place back). 0: no
    # limit. A value set from Telegram (/clips) replaces it. Clips go to every account, so this is also what each
    # account has to post: keep it at the accounts' cap.
    clips_per_day: int = Field(0, ge=0, le=100)
    trim: Trim | None = Trim()  # null: cut at the sentence times
    llm_judge: LLMJudge
    llm_tasks: dict[Task, LLMTask] = {}  # a model per task; a task not listed here uses llm_judge

    def llm_for(self, task: Task) -> LLMJudge:
        return self.llm_tasks.get(task, self.llm_judge)

    @model_validator(mode="after")
    def length_ok(self):
        check_clip_length(self.clip_length_s)
        return self


class Compilation(Strict):
    """One video from the best moments of one finished match (a recap). Off unless a profile turns it on."""

    enabled: bool = False
    match_window_min: int = Field(45, ge=10, le=120)  # how much of the stream the LLM reads to find finished matches
    min_match_s: int = Field(480, ge=60)  # a stretch shorter than this is not a match
    # The match finder (the call that reads the most transcript) is asked once this many minutes of stream have passed
    # since it last looked, and once more on the last segment of a finished stream; 0 asks after every segment.
    check_every_min: int = Field(20, ge=0, le=120)
    length_s: tuple[int, int] = (
        60,
        170,
    )  # the whole recap; platforms differ (YouTube Shorts 180 s, Facebook tested 300 s)
    moment_s: tuple[int, int] = (8, 35)  # each moment in it
    min_moments: int = Field(3, ge=1, le=20)
    max_moments: int = Field(8, ge=1, le=20)
    match_prompt: str = "prompts/match_finder.md"
    recap_prompt: str = "prompts/recap.md"

    @model_validator(mode="after")
    def workable(self):
        (lo, hi), (m_lo, m_hi) = self.length_s, self.moment_s
        if not 10 <= lo < hi <= 300:
            raise ValueError(f"compilation.length_s {self.length_s} must be 10 <= min < max <= 300")
        if not 3 <= m_lo < m_hi <= hi:
            raise ValueError(
                f"compilation.moment_s {self.moment_s} must be 3 <= min < max and not longer than length_s"
            )
        if self.min_moments > self.max_moments:
            raise ValueError("compilation.min_moments must not be more than max_moments")
        if self.max_moments * m_hi < lo:
            raise ValueError(f"compilation: {self.max_moments} moments of at most {m_hi} s can never reach {lo} s")
        if self.min_moments * m_lo > hi:
            raise ValueError(f"compilation: {self.min_moments} moments of at least {m_lo} s cannot fit in {hi} s")
        return self


class Box(Strict):
    x: int
    y: int
    w: int
    h: int


class Wide(Strict):
    """Full-screen menus (draft pick, loading screen) have no green: while one shows, the gameplay panel shows the whole
    width instead of the middle square, so the team lists and rank popups at the sides can be read. `fit` (the default)
    shows the whole frame at once, scaled to the panel's width over a blurred copy of itself; `pan` moves a window
    across the width instead."""

    enabled: bool = True
    mode: Literal["fit", "pan"] = "fit"
    green_below: float = Field(0.10, ge=0, le=1)  # a frame with less green than this (share of the game area) is a menu
    pan: Literal["left_to_right", "right_to_left"] = "left_to_right"


class Layout(Strict):
    type: Literal["facetrack", "split", "center", "blur_fill"]
    cam: Box | None = None
    main: Box | None = None
    split_ratio: float = 0.36
    # How the two panels are enlarged: `lanczos` keeps edges sharper than the default; `sharpen` (0 to 2) adds a light
    # sharpen to the face cam panel only, the one enlarged the most (a 572 px wide cam is stretched to 1080).
    scaler: Literal["bicubic", "lanczos"] = "bicubic"
    sharpen: float = Field(0, ge=0, le=2)
    smoothing: float = 0.85
    max_pan_px_per_s: int = 400
    # Where the stream's donation alert (card, amount, sender and message) is on the 1080-high frame, measured per
    # source. In a donation clip it is shown for as long as its text is on screen, centred, `donation_width` of the
    # frame wide: on a split layout just under the captions, which sit below the cam panel (the crop cuts the alert
    # off), on any other layout `donation_y` of the way down (clear of the hook at the top and the captions at 62%).
    donation: Box | None = None
    donation_width: float = Field(0.72, gt=0, le=1)
    donation_y: float = Field(0.18, ge=0, le=0.8)
    # The alert is shown only while its text is on screen: the share of bright yellow pixels in the lower part of the
    # box (where the amount and the message are) that says so.
    donation_min_share: float = Field(0.0005, ge=0, le=1)
    wide: Wide | None = None

    @model_validator(mode="after")
    def menu_scenes_need_split(self):
        if self.type != "split" and self.wide is not None:
            raise ValueError("layout.wide (menu scenes) belongs to the split layout only")
        return self


# ---------- sources -----------------------------------------------------------
_URL_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "youtube",
        "playlist",
        re.compile(r"^https://(?:www\.|m\.)?youtube\.com/(?:playlist|watch)\?(?:.*&)?list=(?P<key>[\w-]+)"),
    ),
    (
        "youtube",
        "channel",
        re.compile(
            r"^https://(?:www\.|m\.)?youtube\.com/(?P<key>@[\w.\-]+)(?:/(?P<tab>videos|streams|shorts|featured|live))?/?$"
        ),
    ),
    (
        "youtube",
        "channel",
        re.compile(
            r"^https://(?:www\.|m\.)?youtube\.com/channel/(?P<key>UC[\w-]{22})(?:/(?P<tab>videos|streams|shorts|featured|live))?/?$"
        ),
    ),
    (
        "youtube",
        "channel",
        re.compile(
            r"^https://(?:www\.|m\.)?youtube\.com/(?:c|user)/(?P<key>[\w.\-]+)(?:/(?P<tab>videos|streams|shorts|featured|live))?/?$"
        ),
    ),
    ("twitch", "channel", re.compile(r"^https://(?:www\.)?twitch\.tv/(?P<key>\w+)(?:/videos)?/?$")),
    ("kick", "channel", re.compile(r"^https://(?:www\.)?kick\.com/(?P<key>[\w-]+)/?$")),
]


def parse_source_url(url: str) -> dict:
    """Return {platform, kind, key, tab} or raise ValueError."""
    url = url.strip().split("#")[0]
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    for platform, kind, pat in _URL_PATTERNS:
        m = pat.match(url)
        if m:
            g = m.groupdict()
            return {"platform": platform, "kind": kind, "key": g["key"], "tab": g.get("tab")}
    raise ValueError(
        f"unsupported source url {url!r}. Use a channel link (youtube.com/@name, /channel/UC…, /c/…), "
        "a playlist link (…?list=…), twitch.tv/name or kick.com/name"
    )


class Source(Strict):
    url: str
    name: str | None = None
    id: str | None = None
    enabled: bool = True
    clip_policy: Literal["open", "blocked"] = "open"
    overrides: dict = {}
    # derived from url — never set by hand
    platform: str = ""
    kind: str = ""
    key: str = ""
    tab: str | None = None

    @model_validator(mode="after")
    def derive(self):
        info = parse_source_url(self.url)
        self.platform, self.kind, self.key, self.tab = info["platform"], info["kind"], info["key"], info["tab"]
        if self.id is None:
            self.id = re.sub(r"[^a-z0-9]+", "_", self.key.lower().lstrip("@")).strip("_")
        if self.name is None:
            self.name = self.key
        return self

    def live_url(self) -> str | None:
        """Page the live-watcher polls (None for playlists)."""
        if self.kind == "playlist":
            return None
        if self.platform == "youtube":
            base = re.sub(r"/(videos|streams|shorts|featured|live)/?$", "", self.url.rstrip("/"))
            return f"{base}/live"
        return self.url  # streamlink resolves twitch/kick channel pages directly

    def discover_urls(self, include_shorts: bool) -> list[str]:
        """Pages the discover service lists for uploads and ended streams."""
        if self.platform != "youtube" or self.kind == "playlist" or (self.tab and self.tab != "live"):
            return [self.url]
        base = re.sub(r"/live/?$", "", self.url.rstrip("/"))
        return [f"{base}/videos", f"{base}/streams"] + ([f"{base}/shorts"] if include_shorts else [])


class Target(Strict):
    account: str


LOGO_POSITIONS = {"top_right", "top_left", "bottom_right", "bottom_left"}


def _number_between(value: object, low: float, high: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high


class Profanity(Strict):
    """Words to take out of a clip. Each one the transcript has is muted in the audio (the times of Whisper's words are
    not exact, so a little before and after is muted too) and shown as stars in the captions. Off with no `terms`."""

    terms: list[str] = []
    mute: bool = True
    mask_captions: bool = True
    pad_before_s: float = Field(0.10, ge=0, le=1)
    pad_after_s: float = Field(0.15, ge=0, le=1)


class Profile(Strict):
    """Fully merged profile for ONE source (profile + that source's overrides)."""

    version: int
    name: str
    enabled: bool = True
    niche: str
    mode: Literal["rnd", "publish"]
    language: str
    live: Live
    discover: Discover
    scout: Scout
    download: dict
    signals: Signals
    selection: Selection
    compilation: Compilation = Field(default_factory=Compilation)
    profanity: Profanity = Field(default_factory=Profanity)
    output: dict
    layout: Layout
    captions: dict
    branding: dict
    safe_zone: dict
    package: dict
    review: dict
    targets: list[Target]
    test_targets: list[Target]
    limits: dict
    source: Source

    @model_validator(mode="after")
    def rules(self):
        self._check_basics()
        self._check_transcript_rules()
        self._check_targets()
        self._check_branding()
        return self

    def _check_branding(self) -> None:
        logo, hook = self.branding.get("logo", {}), self.branding.get("hook_overlay", {})
        if logo.get("position", "top_right") not in LOGO_POSITIONS:
            raise ValueError(f"{self.name}: branding.logo.position must be one of {sorted(LOGO_POSITIONS)}")
        if not _number_between(logo.get("opacity", 0.9), 0, 1):
            raise ValueError(f"{self.name}: branding.logo.opacity must be a number from 0 to 1")
        if not _number_between(logo.get("width", 140), 16, 540):
            raise ValueError(f"{self.name}: branding.logo.width must be 16 to 540 pixels")
        if not _number_between(hook.get("y", 0.14), 0, 1) or not _number_between(hook.get("seconds", 2.5), 0, 10):
            raise ValueError(f"{self.name}: branding.hook_overlay.y must be 0 to 1 and seconds 0 to 10")
        zone = self.safe_zone
        edges_ok = all(_number_between(zone.get(k, 0), 0, 0.9) for k in ("top", "bottom", "left", "right"))
        if (
            not edges_ok
            or zone.get("top", 0) + zone.get("bottom", 0) >= 1
            or zone.get("left", 0) + zone.get("right", 0) >= 1
        ):
            raise ValueError(f"{self.name}: safe_zone edges must each be 0 to 0.9 and leave room for the content")

    def _check_basics(self) -> None:
        if self.version != 2:
            raise ValueError(f"{self.name}: config version {self.version}, expected 2")
        if self.layout.type == "split" and self.source.enabled and not self.layout.cam:
            raise ValueError(f"{self.name}/{self.source.id}: split layout needs layout.cam")
        if not isinstance(self.review.get("required", True), bool):
            raise ValueError(f"{self.name}: review.required must be true or false")

    def _check_transcript_rules(self) -> None:
        t, mode = self.signals.transcript, self.selection.mode
        if mode != "signals":
            if not t.enabled:
                raise ValueError(f"{self.name}: selection.mode {mode} needs signals.transcript.enabled")
            if t.mode != "full":
                raise ValueError(f"{self.name}: selection.mode {mode} needs signals.transcript.mode: full")
            if not self.selection.llm_judge.enabled:
                raise ValueError(f"{self.name}: selection.mode {mode} needs selection.llm_judge.enabled")
        if self.compilation.enabled and not (t.enabled and t.mode == "full" and self.selection.llm_judge.enabled):
            raise ValueError(
                f"{self.name}: compilation needs signals.transcript.mode: full and selection.llm_judge.enabled "
                "(the LLM finds the matches and picks the moments)"
            )
        if t.vod_mode == "full" and self.download.get("sections_only", True):
            raise ValueError(
                f"{self.name}: signals.transcript.vod_mode full needs download.sections_only: false "
                "(a full transcript needs the whole audio)"
            )

    def _check_targets(self) -> None:
        if self.mode == "rnd" and not self.test_targets:
            raise ValueError(f"{self.name}: rnd mode needs test_targets")
        if self.mode == "publish" and not self.targets:
            raise ValueError(f"{self.name}: publish mode needs targets")

    def needs_approval(self) -> bool:
        """True: the review service waits for your Telegram tap. False: clips go out without it."""
        return self.review.get("required", True)

    def upload_accounts(self) -> list[str]:
        """Accounts the publisher may post this source's clips to. Empty = do not post.

        The publisher must call this, and must also skip sources paused in Postgres
        (sources.paused_at set after a copyright claim / strike).
        """
        if not self.enabled or not self.source.enabled or self.source.clip_policy == "blocked":
            return []
        chosen = self.targets if self.mode == "publish" else self.test_targets
        return [t.account for t in chosen]


# ---------- loading ----------------------------------------------------------
def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_profile(path: Path, sources: list[dict] | None = None) -> list[Profile]:
    """Merge one profile with its sources, one Profile per source.

    The database owns sources: pass its rows as `sources`. None means "use the profile's
    `sources:` list", which is only the seed for an empty database (and the examples).
    """
    defaults = yaml.safe_load((path.parent / "_defaults.yaml").read_text())
    raw = yaml.safe_load(path.read_text())
    seed = raw.pop("sources", [])
    if sources is None:
        sources = seed
        if not sources:
            raise ValueError(f"{path.name}: no sources")
    base = deep_merge(defaults, raw)
    result, seen = [], set()
    for s in sources:
        src = Source(**s)
        if src.id in seen:
            raise ValueError(f"{path.name}: duplicate source {src.id} ({src.url})")
        seen.add(src.id)
        result.append(Profile(**deep_merge(base, s.get("overrides", {})), source=src))
    return result


def validate_accounts(profiles: list[Profile], accounts_file: Path) -> None:
    accounts = {a["id"]: a for a in yaml.safe_load(accounts_file.read_text())["accounts"]}
    for p in profiles:
        for t in p.targets:
            a = accounts.get(t.account)
            if a is None:
                raise ValueError(f"{p.name}: unknown account {t.account}")
            if a.get("test"):
                raise ValueError(f"{p.name}: targets must be brand accounts, {t.account} is a test account")
        for t in p.test_targets:
            a = accounts.get(t.account)
            if a is None:
                raise ValueError(f"{p.name}: unknown account {t.account}")
            if not a.get("test"):
                raise ValueError(f"{p.name}: test_targets must be test accounts, {t.account} is not")


# Services that read one message, do bounded work and ack. Recorder and chat-listener hold a lease
# instead; review acks after sending the preview and waits for you in Postgres.
SHORT_JOB_SERVICES = {"download", "understand", "select", "render", "package", "publisher"}
NFR_11_MAX_RECOVERY_S = 120  # PRODUCT.md NFR-11: at most 2 min lost when a recorder dies


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _check_groq(w: dict) -> None:
    groq = (w.get("rate_limits") or {}).get("groq")
    if groq is None:
        raise ValueError("workers.yaml: rate_limits.groq is missing")
    alert = groq.get("alert_at")
    if isinstance(alert, bool) or not isinstance(alert, (int, float)) or not 0 < alert <= 1:
        raise ValueError("workers.yaml: rate_limits.groq.alert_at must be a share of the daily limit, 0 < x <= 1")
    if "stop_at" in groq:
        stop = groq["stop_at"]
        if isinstance(stop, bool) or not isinstance(stop, (int, float)) or not alert <= stop <= 1:
            raise ValueError("workers.yaml: rate_limits.groq.stop_at must be at least alert_at and at most 1")


def _check_budget(w: dict) -> None:
    budget = (w.get("budget") or {}).get("openrouter")
    if budget is None:
        return
    fields = {"cap_ceiling_usd", "cap_usd", "alert_at", "stop_at"}
    if not isinstance(budget, dict) or set(budget) != fields:
        raise ValueError(f"workers.yaml: budget.openrouter must have exactly {sorted(fields)}")
    if not all(_positive(budget[k]) for k in ("cap_ceiling_usd", "cap_usd")):
        raise ValueError("workers.yaml: budget.openrouter cap_ceiling_usd and cap_usd must be above 0")
    if budget["cap_usd"] > budget["cap_ceiling_usd"]:
        raise ValueError("workers.yaml: budget.openrouter.cap_usd must not be above cap_ceiling_usd")
    alert, stop = budget["alert_at"], budget["stop_at"]
    if not all(_positive(x) for x in (alert, stop)) or not alert <= stop <= 1:
        raise ValueError("workers.yaml: budget.openrouter needs 0 < alert_at <= stop_at <= 1 (shares of the cap)")


def _check_rules(w: dict) -> None:
    section = (w.get("rules") or {}).get("ceilings")
    if section is None:
        return
    fields = {"daily_cap", "min_spacing_min", "clips_per_day"}
    if not isinstance(section, dict) or set(section) != fields:
        raise ValueError(f"workers.yaml: rules.ceilings must have exactly {sorted(fields)}")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in section.values()):
        raise ValueError("workers.yaml: rules.ceilings values must be whole numbers of 1 or more")


def _check_planner(w: dict) -> None:
    planner = w.get("planner")
    if planner is None:
        return
    fields = {"enabled", "assumed_cost_per_audio_hour_usd", "min_measured_hours"}
    if not isinstance(planner, dict) or set(planner) != fields:
        raise ValueError(f"workers.yaml: planner must have exactly {sorted(fields)}")
    if not isinstance(planner["enabled"], bool):
        raise ValueError("workers.yaml: planner.enabled must be true or false")
    if not _positive(planner["assumed_cost_per_audio_hour_usd"]):
        raise ValueError("workers.yaml: planner.assumed_cost_per_audio_hour_usd must be above 0")
    hours = planner["min_measured_hours"]
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        raise ValueError("workers.yaml: planner.min_measured_hours must be 0 or more")


def _check_health(w: dict) -> None:
    health = w.get("health")
    if health is None:
        return
    if not isinstance(health, dict) or set(health) - {"stale_after_s", "digest_every_h", "expected"}:
        raise ValueError("workers.yaml: health may only have stale_after_s, digest_every_h and expected")
    stale = health.get("stale_after_s")
    if isinstance(stale, bool) or not isinstance(stale, (int, float)) or stale < 60:
        raise ValueError("workers.yaml: health.stale_after_s must be at least 60 seconds (a beat comes every 30)")
    if not _positive(health.get("digest_every_h")):
        raise ValueError("workers.yaml: health.digest_every_h must be a positive number of hours")
    expected = health.get("expected")
    if not isinstance(expected, list) or not expected or not all(isinstance(x, str) and x for x in expected):
        raise ValueError("workers.yaml: health.expected must be a list of service names")


def _check_broker(w: dict) -> None:
    broker = w.get("broker") or {}
    for key in ("stream_maxlen", "dead_letter_maxlen"):
        if not _positive(broker.get(key)):
            raise ValueError(
                f"workers.yaml: broker.{key} must be a positive number (streams are trimmed, never unbounded)"
            )


def _check_recovery(w: dict) -> None:
    for host, block in w["hosts"].items():
        for svc in sorted(SHORT_JOB_SERVICES & set(block["services"])):
            if not _positive(block["services"][svc].get("visibility_timeout_s")):
                raise ValueError(
                    f"workers.yaml: {host}.{svc} needs visibility_timeout_s > 0 "
                    "(seconds of silence before its job is reclaimed)"
                )
    lease, sweep = w.get("lease") or {}, w.get("reconciler") or {}
    if not (_positive(lease.get("heartbeat_s")) and _positive(lease.get("expires_s"))):
        raise ValueError("workers.yaml: lease.heartbeat_s and lease.expires_s must be positive")
    if lease["expires_s"] < 2 * lease["heartbeat_s"]:
        raise ValueError("workers.yaml: lease.expires_s must be at least twice lease.heartbeat_s")
    for key in ("every_s", "stalled_after_min", "running_after_min"):
        if not _positive(sweep.get(key)):
            raise ValueError(f"workers.yaml: reconciler.{key} must be positive")
    slowest = max(
        (svc.get("visibility_timeout_s", 0) for block in w["hosts"].values() for svc in block["services"].values()),
        default=0,
    )
    if sweep["running_after_min"] * 60 <= slowest:
        raise ValueError(
            f"workers.yaml: reconciler.running_after_min must be longer than the slowest visibility_timeout_s "
            f"({slowest} s): a job that is still within its time may not be called lost"
        )
    if lease["expires_s"] + sweep["every_s"] > NFR_11_MAX_RECOVERY_S:
        raise ValueError(
            f"workers.yaml: lease.expires_s + reconciler.every_s must be <= {NFR_11_MAX_RECOVERY_S} s (NFR-11)"
        )


def _check_retention(w: dict) -> None:
    keep = w.get("retention") or {}
    for key in ("every_min", "failed_keep_days", "approved_max_hold_days"):
        if not _positive(keep.get(key)):
            raise ValueError(
                f"workers.yaml: retention.{key} must be positive (files are cleaned up, never kept forever)"
            )
    if not isinstance(keep.get("published_keep_h"), int | float) or keep["published_keep_h"] < 0:
        raise ValueError("workers.yaml: retention.published_keep_h must be zero or more")


PRICE_KEY = re.compile(r"(anthropic|openai_compatible)/\S+")


def llm_price_key(spec: LLMSpec) -> str:
    return f"{spec.provider}/{spec.model}"


def load_llm_prices(path: Path) -> dict[str, Price]:
    """llm_prices.yaml: dollars per million tokens, keyed `<provider>/<model>`."""
    raw = yaml.safe_load(path.read_text()) or {}
    models = raw.get("models")
    unknown = set(raw) - {"checked", "models"}
    if not isinstance(models, dict) or unknown:
        raise ValueError(f"llm_prices.yaml: expected only `checked` and `models`, got {sorted(raw)}")
    prices: dict[str, Price] = {}
    for key, value in models.items():
        if not PRICE_KEY.fullmatch(str(key)):
            raise ValueError(f"llm_prices.yaml: {key!r} must look like anthropic/<model> or openai_compatible/<model>")
        try:
            prices[key] = Price(**(value or {}))
        except ValueError as exc:
            raise ValueError(f"llm_prices.yaml: {key}: {exc}") from exc
    return prices


def validate_llm_prices(path: Path) -> None:
    load_llm_prices(path)


def check_llm_prices(profiles: list[Profile], prices: dict[str, Price]) -> None:
    """Every enabled judge model needs a price (0 for a free one), or the daily budget cannot see its cost."""
    for profile in profiles:
        judge = profile.selection.llm_judge
        if not judge.enabled:
            continue
        for chain in (judge, *profile.selection.llm_tasks.values()):
            for spec in (chain, *chain.fallbacks):
                if llm_price_key(spec) not in prices:
                    raise ValueError(
                        f"{profile.name}: no price for {llm_price_key(spec)} in llm_prices.yaml "
                        "(use 0 for a free model)"
                    )


# What Hermes may never be allowed to edit, whatever hermes.yaml says: approval, publishing and the
# files that name accounts, workers or Hermes's own limits.
HERMES_PROTECTED_FILES = ["profiles/_defaults.yaml", "accounts.yaml", "workers.yaml", "hermes.yaml"]
HERMES_ALWAYS_DENIED_KEYS = {"mode", "review", "targets", "test_targets"}
HERMES_SOURCE_ACTIONS = {"add", "update", "pause", "resume", "disable"}


def validate_hermes(hermes_file: Path) -> None:
    h = yaml.safe_load(hermes_file.read_text())
    for pattern in h.get("edit_files") or []:
        for protected in HERMES_PROTECTED_FILES:
            if fnmatch.fnmatch(protected, pattern):
                raise ValueError(f"hermes.yaml: edit_files pattern {pattern!r} matches protected file {protected}")
    missing = HERMES_ALWAYS_DENIED_KEYS - set(h.get("deny_keys") or [])
    if missing:
        raise ValueError(f"hermes.yaml: deny_keys must include {sorted(missing)}")
    unknown = set(h.get("source_actions") or []) - HERMES_SOURCE_ACTIONS
    if unknown:
        raise ValueError(f"hermes.yaml: source_actions has unknown actions {sorted(unknown)}")
    if not _positive(h.get("keep_backups")):
        raise ValueError(
            "hermes.yaml: keep_backups must be a positive number (Hermes backs up every file it overwrites)"
        )


def validate_workers(workers_file: Path) -> None:
    w = yaml.safe_load(workers_file.read_text())
    host = w["host_profile"]
    if host not in w["hosts"]:
        raise ValueError(f"workers.yaml: host_profile {host!r} has no block under hosts")
    required = {
        "live-watcher",
        "recorder",
        "chat-listener",
        "discover",
        "download",
        "understand",
        "select",
        "render",
        "package",
        "review",
        "publisher",
        "stats",
        "scout",
    }
    missing = required - set(w["hosts"][host]["services"])
    if missing:
        raise ValueError(f"workers.yaml: {host} is missing services {sorted(missing)}")
    _check_broker(w)
    _check_health(w)
    _check_budget(w)
    _check_rules(w)
    _check_planner(w)
    _check_groq(w)
    _check_recovery(w)
    _check_retention(w)


if __name__ == "__main__":
    ok = True
    for name, check in (
        ("workers.yaml", validate_workers),
        ("hermes.yaml", validate_hermes),
        ("llm_prices.yaml", validate_llm_prices),
    ):
        try:
            check(ROOT / name)
            print(f"OK  {name}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"ERR {name}: {e}")
    try:
        prices: dict[str, Price] | None = load_llm_prices(ROOT / "llm_prices.yaml")
    except Exception:  # noqa: BLE001 - already reported above
        prices = None
    for f in sorted((ROOT / "profiles").glob("[!_]*.yaml")):
        try:
            ps = load_profile(f)
            validate_accounts(ps, ROOT / "accounts.yaml")
            if prices is not None:
                check_llm_prices(ps, prices)
            for p in ps:
                live = "live" if p.live.capture else "no-live"
                print(
                    f"OK  {p.name:<11} {p.source.id:<13} {p.layout.type:<9} {p.mode:<7} {live:<7} "
                    f"-> {','.join(p.upload_accounts()) or 'no uploads'}"
                )
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"ERR {f.name}: {e}")
    sys.exit(0 if ok else 1)

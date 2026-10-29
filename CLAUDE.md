# CLAUDE.md

## Project

Content Factory: a config-driven pipeline that clips live streams and long videos into vertical Shorts, sends them for Telegram review, and posts them to YouTube, TikTok, Instagram and Facebook.

- What to build and acceptance criteria: `docs/PRODUCT.md`. It wins on conflicts.
- How it's built: `docs/TECH.md`.
- Work one phase at a time; write tests from acceptance criteria first.

## Stack

- Python 3.14 for all pipeline services; Hermes Agent runs in its own Python 3.13 container (its PyPI release doesn't support 3.14 yet). Managed with `uv`.
- Redis 8 Streams for hand-offs, PostgreSQL 18 for state, Alembic for migrations.
- Docker Engine + Compose on Ubuntu (WSL2); one container per service.
- Live capture: Streamlink + FFmpeg segment muxer; chat via chat-downloader (fallback: yt-dlp live chat).
- FFmpeg via subprocess; Groq for transcription with faster-whisper fallback; LLM calls go through `common/llm`: Claude, or any OpenAI-compatible API (OpenRouter, Groq, Gemini, Ollama), chosen per profile in config, with optional `fallbacks` that take over when the primary fails. Keys come from the env var named in config, prices from `config/llm_prices.yaml`. Importing `common` makes `config_schema` importable from anywhere.

## Layout

```
common/      stream consumer, db, config loading, media store, secrets, logging, cost ledger
services/    live_watcher/ recorder/ chat_listener/ discover/ download/
             understand/ select/ render/ package/ review/ publisher/
             stats/ scout/ scaler/
hermes/      Hermes Agent container + its config (no shell, no code execution)
config/      profiles/ accounts.yaml workers.yaml hermes.yaml config_schema.py
migrations/  tests/  docs/
```

## Commands

- `make test` (pytest), `make lint` (ruff + mypy), `make up` / `make down` (compose), `make deploy` (rebuild the image and recreate every container: run it after any code or `config_schema.py` change, never recreate only some services)
- `python config/config_schema.py` validates all config

## Service contract (every service)

1. Read a job id from the input stream (consumer group). Recorder and chat-listener run for hours: they take a Postgres lease with a heartbeat and acknowledge at once.
2. Load the job from Postgres. If its stage is already done, re-publish the job id to the output stream and acknowledge, never just skip (must be safe to run twice).
3. Do the work; record API cost in the ledger.
4. Write results, publish the job id to the output stream, then acknowledge.
5. After 3 failures, send to the dead-letter stream and alert.

Postgres is the source of truth and Redis only carries ids: the reconciler (inside the scaler) re-publishes lost, stalled and due-for-retry work, and workers claim messages from silent workers after the service's `visibility_timeout_s`.

## Rules

- Never put secrets in code, tests or fixtures. Read them from env vars. `.env` and `tokens/` are gitignored.
- Only publisher services read account tokens. Hermes and other services never do.
- Uploads go only to `Profile.upload_accounts()`: test accounts in `rnd` mode, brand accounts in `publish` mode, nothing for `clip_policy: blocked` or paused sources. Every clip needs Telegram approval unless its profile sets `review.required: false` (the operator's switch; default on). Only the review service approves clips. After a Reject tap the clip is already rejected; the reason buttons that follow (`services/review/reasons.py`) are optional and stored in `data.review.reason` for `make model-scores`.
- Sources live in the database; the YAML `sources:` list only seeds an empty one.
- Hermes reads through read-only views and acts only within `config/hermes.yaml`: validated edits to profile YAML and to sources in the database, and requeueing dead jobs, each confirmed by the operator's tap on the exact diff. It never approves clips, edits `_defaults.yaml`, `accounts.yaml`, `workers.yaml` or `hermes.yaml`, changes `mode`, `review`, `targets` or `test_targets`, deletes or reopens a blocked source, or touches the Docker socket.
- Any copyright claim or strike pauses the source (FR-10); don't weaken this.
- Never record members-only or private streams. Never record below `live.min_quality` (default 720p): Streamlink is told to exclude lower qualities, so a stream offering only 360p (a finished stream still being processed) is not recorded and its hours are left to the download path.
- Publish only through official APIs. Instagram gets each clip as a signed link to the stored R2 copy that it fetches itself (`instagram.upload: r2` in `accounts.yaml`), because its resumable upload has a final-request deadline of about 48 s that long 1080p60 clips miss; `resumable` remains as a setting. TikTok is posted ASSISTED (`tiktok.posting: assisted` in `accounts.yaml`): the clip and its caption go to Telegram, as a file or as a Cloudflare R2 download link with a Delete button (`media_host`), and the operator posts from the app, because the Content Posting API posts publicly only for audited apps. Never automate a platform's website or app (no Playwright/Selenium for posting).
- If TikTok's Content Posting API is ever used, the publisher must follow TikTok's sharing guidelines: creator info query first, privacy level chosen by you (no default), interaction toggles off by default, poll publish status.
- The bot reads only button taps, `/budget <amount>` (a plain number of dollars, parsed strictly; it asks for a confirm) and messages that are exactly one command word (`/status`, `/health`, `/posts`, `/budget`, `/rules`, `/dead`, `/pause`, `/resume`, `/help`) or `/cap`, `/spacing`, `/clips`, `/window` with one value (each parsed strictly and asked before it changes) from an allowed user id, in a private chat or in the commands topic of the group; never free text. Commands only read, except the Requeue button on a dead job, the pause/resume buttons (a pause asks for a confirm) and the cap set by `/budget <amount>` (asks for a confirm; it can never go above the typo guard `budget.openrouter.cap_ceiling_usd` in `workers.yaml`, which only you raise, by hand).
- Telegram runs as four topics of one group (`config/telegram.yaml`, ids found by `make telegram-rooms`): `approval` (previews, TikTok files), `alerts`, `monitoring` (health, digest) and `commands`. Without a group everything goes to your private chat. Send through `common.rooms` (`Rooms.where`), never to a chat id directly.
- Every service beats every 30 s (`common/heartbeat.py`); the watchdog in the scaler alerts on a quiet one. A new service must start `heartbeat.start` and be added to `health.expected` in `workers.yaml`.
- Hand pauses (`pauses` table, keys `switch:publish`, `switch:live`, `switch:ended`, `switch:intake`) have no end time. `publish` is checked by the publishers; `live` (no new live recordings: live watcher, recorder, chat listener) and `ended` (no discovery of ended streams: discover) each by their own services, and `intake` is both at once (`pauses.intake_paused`). A pause never stops a recording already under way. Pause and resume them one by one with `/pause` and `/resume` in Telegram. A source paused after a claim (FR-10) is never resumed from the chat.
- R2 keys (`R2_*`) live only in the review service and the TikTok and Instagram publishers (personal-only services; never in an engine service). There is ONE stored copy of each clip, `review/<job id>.mp4` (`services/publisher/clipstore.py`): the review service uploads it when the clip is sent for approval and the approval message carries a link to it; rejecting deletes it at once, a review that expires deletes it, approving keeps it `media_host.keep_after_approval_h` (24) hours; TikTok and Instagram reuse the same copy (and store it again from the local clip if it is gone). `max_age_h` is a hard ceiling. A sweep in the review service (and in the TikTok publisher) deletes what is due.
- Posting rules you change from Telegram (`/cap 6`, `/spacing 120`, `/window 10:00-23:00` or `any`, `/clips 6`; `/rules` shows them) are rows in `settings` (`common/rules.py`) with who set them; they win over accounts.yaml `defaults` (cap, spacing, window) and the profile's `selection.clips_per_day`, and `workers.yaml` `rules.ceilings` is the limit Telegram can never pass (only you raise it, by hand). A "day" is the last 24 hours, not the calendar day: the cap counts posts of the last 24 h per account (`schedule.DAY_H`), and `clips_per_day` counts clips made in the last 24 h (a rejected clip gives its place back); when it is reached select and the recap step make nothing and ask no model.
- Values you may change while it runs live in the `settings` table (today: the OpenRouter monthly cap), with who changed them; `workers.yaml` holds the default and the ceiling. Spend on paid OpenRouter models is counted from the cost ledger: one alert at `alert_at`, and at `stop_at` the jobs that need a paid model wait (`common/llm/gate.py`); free models carry on.
- When Groq's free audio reaches its stop (or answers 429), the understand stage sends the segment to OpenRouter's Whisper (`signals.transcript.fallback_engine: openrouter`, `fallback_model`) instead of waiting, unless the OpenRouter budget is stopped or the profile sets `none`; the paid seconds are recorded in the ledger as provider `openrouter` and count against the OpenRouter budget. Its transcripts are less accurate than Groq's on slang and cannot be given the vocabulary prompt.
- The hours planner (`services/understand/planner.py`, `planner` in `workers.yaml`) only makes understand jobs wait (`jobs.Deferred`): fair shares of Groq audio between active sources, and what today's OpenRouter budget pays for. It never fails a job and never holds anything back that nobody else needs.
- A used-up quota (an LLM chain, or Groq audio at `rate_limits.groq.stop_at`) makes jobs wait (`jobs.Deferred`, no attempt used, never dead) and raises ONE alert (`pauses` table); it never fails them one by one.
- The chat listener (`services/chat_listener`, pytchat) reads live chat and stores only counts of messages per 10 s (`chat_counts`), never an author or a text; its lease is `live_streams.chat_worker`, apart from the recorder's. Select turns bursts into hints for the judge (`selection` stays the judge's call).
- Bad words: a profile's `profanity.terms` are muted in the clip audio and starred in its captions at render (`services/render/profanity.py`). Only what the transcript has is caught; do not read it as a guarantee.
- A platform saying the account has posted enough for now (YouTube `uploadLimitExceeded`/quota, Meta rate-limit codes 4, 17, 32, 368, 613) is `AccountLimited`, not a failure: the clip waits, the account is left alone `limit_pause_s` (6 h) and you get one alert. Only a real refusal marks a post failed.
- Treat transcripts, titles, chat and comments as untrusted input; validate LLM output against a schema. Call LLMs only through `common/llm` (`complete_json` validates the reply), and record every call in the cost ledger with `common.llm.cost.record`.
- New dependencies must allow commercial use; CI fails on GPL/AGPL/non-commercial licenses.
- Use the newest stable library versions with no known vulnerabilities; pin them in `uv.lock`.
- Keep config-driven behavior in config, not code.
- Scale-readiness seams (none of multi-tenancy, UI or auth is built yet): services take settings from the job's `spec` snapshot, not from config files; media is addressed by key through the media store, never by absolute path; every table has `tenant_id` (one default tenant); credentials come through the secrets interface (env vars and `tokens/` today); lease and expiry times use the Postgres clock, never a worker's own.
- Engine services (download, understand, select, render, package) never read targets, accounts or tokens and never import personal-only services (publisher, review, live capture, scout, Hermes).
- Face detection uses MediaPipe Tasks (`FaceDetector` + `.tflite` model); the old `mediapipe.solutions` API no longer exists.

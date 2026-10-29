# PRD – Content Factory clipper

What the system must do and how we know it works. How it's built lives in `docs/TECH.md`; if the two disagree, this file wins.

## Problem and goal

Clipping long streams and podcasts by hand takes hours per clip, so one person can't run several accounts on four platforms.

**Goal:** one person runs a clip channel per niche across YouTube Shorts, TikTok, Reels and Facebook, spending under 15 minutes a day on review. Everything else is automated.

## Persona

| Who | Needs |
| --- | --- |
| **Operator (you):** one developer, one laptop, reviews from a phone | Add niches and sources without code; approve clips fast; see cost and performance daily; never lose an account to one bad source |
| **Viewers** on each platform (indirect) | Short, self-contained moments with readable captions, posted at times they're online |

## User stories

As the operator, I want to:

1. Start a new niche by copying a profile and adding channel links (by asking Hermes in Telegram), with no code change.
2. Get suggested channels that had the most views in the last 30 days.
3. Have live streams, new videos and recent videos found and clipped without touching anything.
4. Get each clip in Telegram with a preview, score and reason, and approve or reject it in one tap, or turn approval off per profile and let clips go out on their own.
5. Have approved clips posted to all four platforms at good times, within daily caps.
6. Have a source paused automatically when it gets a copyright claim, strike or objection.
7. Get a daily report: clips made, approved, posted, cost, top performers.
8. Have the pipeline recover by itself after the laptop restarts or a service crashes.
9. Stop spending automatically once the daily budget is reached.
10. Have tutorials, donation reads and announcements clipped too, not only gameplay peaks.

## Functional requirements

Each requirement's acceptance criteria become automated tests.

| ID | Requirement | Acceptance criteria | Phase |
| --- | --- | --- | --- |
| FR-1 | Load and validate profiles, accounts and workers config | Invalid key, bad link or unknown account in the YAML fails startup with a clear message. Sources live in the database: a row with a bad link is rejected when written, and one that slips in is skipped with an alert and never stops the stack | 0 |
| FR-2 | Discover new and backlog videos per source | First run picks up to 5 most-viewed unprocessed videos ≤ 60 days old; no video processed twice | 1 |
| FR-3 | Download only candidate sections | Downloaded duration ≤ candidate windows + margins | 1 |
| FR-4 | Score the video with configurable signals | Disabled or missing signals are skipped and weights rescale; transcript has word timestamps | 1 |
| FR-5 | Select clip windows | Each clip is within its min/max length (a kind may override it), starts and ends on sentence boundaries, overlaps no earlier clip by > 50% | 1 |
| FR-6 | Render 1080×1920 clips | Layout, captions, logo and credit inside the safe zone; audio normalized; plays on all 4 platforms | 1 |
| FR-7 | Package text per platform | Title within each platform's limit; hashtags ≤ configured max | 2 |
| FR-8 | Telegram review | Preview, score, kind, reason, title; only your Telegram ID can approve; unapproved clips expire after 24 h. With `review.required: false` (per profile or per source) clips skip approval and go straight to publishing; the default is on | 2 |
| FR-9 | Publish to each platform | Uploads only to `Profile.upload_accounts()`; respects daily cap, spacing and posting windows (counted per destination Page or channel, so two account entries for one Page share a cap); one row per clip per account; retries without double posting | 3 |
| FR-10 | Pause sources on claims or objections | A claim, strike or `blocked` policy stops new clips from that source within one cycle and sends an alert | 3 |
| FR-11 | Collect stats | Views and likes stored every 6 h per post | 3 |
| FR-12 | Scout channels | Weekly per niche; suggestions in Telegram; approved channels added to the profile | 2 |
| FR-13 | Cost ledger and budget stop | Each API call is recorded; a profile pauses at its daily budget | 2 |
| FR-14 | Daily report | Sent to Telegram at a set time with counts, cost and top 3 clips | 2 |
| FR-15 | Capture live streams while live (primary path); handle uploads and ended streams | A live stream is detected within 5 min of starting and recorded in 10-min segments; each segment reaches Understand within 2 min of closing; moments across segment edges are kept; ended recordings only fill gaps; no window is clipped twice; a segment is kept while any pending job needs it, up to `live.max_hold_h`; members-only streams skipped | 1 |
| FR-16 | Transcript-first selection (`selection.mode: hybrid`) | Live segments are transcribed in full with word timestamps, each stretch once (the segment overlap reuses the earlier transcript); the LLM returns sentence ids and a kind (`highlight`, `donation`, `tutorial`, `news`, `funny`), validated against a schema, and clip times are derived from the transcript, never taken from the LLM; invalid output is dropped and logged; signal peaks and transcript moments form one pool judged together; every clip keeps its kind and reason | 1 |
| FR-17 | Transcription budget | Audio seconds sent to Groq are counted per day in the cost ledger; a Telegram alert fires at `alert_at` of the daily limit; at the limit transcription uses `fallback_engine`, or selection runs on signals only if that is `none`; the limit never stops a recording | 1 |
| FR-18 | Job recovery | A message whose worker goes silent is claimed by another worker after the service's `visibility_timeout_s`; a job that fails 3 times goes to the dead-letter stream with a Telegram alert; a worker that finds its stage already done re-publishes the job id; a stage done with no progress after `reconciler.stalled_after_min` is re-published; after a Redis restart no job is lost (state is rebuilt from Postgres); a recorder whose lease expires is replaced and recording resumes within 2 min; a publisher never posts the same clip to the same account twice | 0 |
| FR-19 | Operator interface (Hermes) | Hermes reads status through read-only views and acts only within `config/hermes.yaml`: it edits profile YAML and sources in the database (add, update, pause, resume, disable) and requeues dead jobs, each as a validated diff you confirm with a tap in Telegram, with a backup of every file it overwrites. It can never edit `_defaults.yaml`, `accounts.yaml`, `workers.yaml` or `hermes.yaml`, change `mode`, `review`, `targets` or `test_targets`, delete a source, or reopen a source that is blocked or paused after a claim. It cannot read tokens, publish, approve clips or restart containers | 2 |

## Non-functional requirements

Targets for version 1 on the laptop; adjust after week-1 measurements.

| ID | Area | Target |
| --- | --- | --- |
| NFR-1 | Throughput | ≥ 20 rendered clips a day per niche |
| NFR-2 | Speed | New upload to Telegram preview in ≤ 6 h |
| NFR-3 | Recovery | Full stack back up after a reboot with no manual steps; no job lost |
| NFR-4 | Reliability | ≤ 5% of jobs end in the dead-letter stream |
| NFR-5 | Cost | ≤ $15 a month total API spend |
| NFR-6 | Resources | ≤ 10 GB RAM inside WSL at peak |
| NFR-7 | Security | No secrets in the repo; CI security checks pass on every push |
| NFR-8 | Licenses | Every dependency allows commercial use |
| NFR-9 | Portability | Moving to a mini PC or VPS needs config changes only |
| NFR-10 | Live latency | Moment in a live stream to Telegram preview in ≤ 30 min while transcription keeps up; best effort when it is backlogged (the clip is still made, later) |
| NFR-11 | Live resilience | ≤ 2 min lost per connection drop; recording resumes without manual steps |
| NFR-12 | Scale-readiness | A job carries its own settings snapshot (`jobs.spec`); media is addressed by key, never by absolute path; every table has `tenant_id`; lease and expiry times come from the Postgres clock; engine services never read targets, accounts or tokens |

## KPIs

The first targets are guesses to beat, not commitments; reset them after 4 weeks of data.

| KPI | What it shows | First target |
| --- | --- | --- |
| Approval rate | Moment-picking quality | ≥ 60% of clips approved (with approval off, judge by 7-day views instead) |
| Approval rate per kind | Which moment types earn their review time | Set after week 4 |
| Median views per clip (7 days) | Clip appeal | Set after week 4 |
| Average watch time / retention | Cut quality | Set after week 4 |
| Follower growth per account per week | Channel traction | Set after week 4 |
| Cost per 1,000 views | Unit economics | Below the platform payout per 1,000 views |
| Claims and strikes per 100 clips | Copyright risk | 0; any claim triggers a review |
| Your review time per day | Automation level | ≤ 15 min with approval on; none with it off |

## Assumptions and dependencies

**Assumptions** (if one proves wrong, the plan changes)

- Most MLBB creators openly allow clipping of their streams.
- Groq's free plan (~8 h of audio a day) is shared by version 1's two MLBB creators (Leo Kabuki, Jeje Adriel); the budget guard waits when it runs low. More creators need a paid plan, a second engine or fewer transcribed hours.
- MLBB streamers read donation messages aloud, announce news and teach during the stream, so the transcript holds moments that chat and loudness can't see.
- The laptop's home IP can download and record from YouTube without bot blocks.
- The laptop (or a later always-on machine) is running during creators' streaming hours.
- The platforms approve the upload API apps within ~4 weeks.
- A banned channel is an accepted loss: test accounts come first and a new channel can be made. FR-10 still pauses the source after the first claim.

**Dependencies**

| Dependency | Used for | Fallback |
| --- | --- | --- |
| YouTube via yt-dlp / Streamlink | Source videos and live capture | Update tools; slow down polling |
| Groq API | Transcription | Local faster-whisper on the GTX 1650 |
| LLM API: Claude, or any OpenAI-compatible provider such as OpenRouter | Moments, titles, Hermes | Switch provider in config; pause the pipeline at the budget |
| chat-downloader | Live chat signal | yt-dlp live chat; else run without chat (weights rescale) |
| Telegram Bot API | Review and alerts | Review queue waits; nothing posts |
| YouTube, Meta upload APIs | Publishing | Post by hand from the rendered files |
| TikTok Content Posting API (audit required) | TikTok publishing | Assisted posting: clip + caption sent to Telegram, posted from the app |
| Cloudflare R2 | Public signed links for Instagram media | Google Drive direct links (less reliable) |
| Hermes Agent | Reports, status questions, allow-listed actions, config edits | Services run without it; the review service keeps handling approvals |

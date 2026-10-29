# Content Factory — config (schema version 2)

```
config/
  profiles/
    _defaults.yaml     every key, with defaults and comments (the reference)
    mlbb.yaml          MLBB live streams -> MLBB brand (enabled, first niche)
    podcast_id.yaml    podcasts, uploads only (disabled until MLBB passes its gate)
    irl_global.yaml    IRL live streams (disabled, third)
  accounts.yaml        test + brand accounts, caps, posting windows (no secrets)
  workers.yaml         worker counts per service and host, Groq limits
  hermes.yaml          what Hermes may edit (files, denied keys, source actions)
  config_schema.py     merges + validates everything: `python config_schema.py`
```

## Where config lives

By default this folder. Set `CONFIG_DIR` to load profiles, accounts and workers from somewhere else
(for example a gitignored `config.local/` or a private repo). Plan: before the repo goes public, your real
profiles, prompts and accounts move there and only placeholders stay here.

## Two kinds of context

| Context | Lives in | Changes |
| --- | --- | --- |
| Content: what to clip, how it looks, where it goes | `profiles/*.yaml` | Number of profiles and sources |
| Load: how much work is queued | `workers.yaml` | Number of running workers |

- Add a niche: copy a profile, change `sources`, `live`, `signals`, `layout`, `targets`.
- Add a creator: ask Hermes in Telegram to add the link (plus `cam` for `split`). Sources live in the database;
  the `sources:` list in a profile only seeds an empty database.
- Neither starts new processes; the generic services pick the work up.

## Video sources

- **Live streams (primary):** live-watcher checks `<channel>/live` every `live.check_every_min`;
  the recorder saves `live.segment_min` segments; each segment enters the pipeline at Understand.
- **Uploads and ended streams:** discover lists `/videos` and `/streams` within `discover.max_age_days`;
  first run takes `backfill_per_source`, then new uploads first, else `daily_per_source` from the window.
- Members-only and private streams are never recorded.

Accepted links: `youtube.com/@name` (and `/live`, `/videos`, `/streams`), `/channel/UC…`, `/c/…`,
`playlist?list=…`, `twitch.tv/name`, `kick.com/name`.

## How moments are picked

`selection.mode`: `signals` (chat, loudness, heatmap peaks), `hybrid` (peaks plus moments the LLM finds
in the full transcript) or `transcript`. `hybrid` and `transcript` need `signals.transcript.mode: full`
and `selection.llm_judge.enabled`. `selection.kinds` can give a kind (`highlight`, `donation`, `tutorial`,
`news`, `funny`) its own `clip_length_s`. MLBB runs `hybrid`.

Version 1 runs two creators on one Groq free plan (about 8 h of audio a day, shared). Groq limits live in `workers.yaml`
(`rate_limits.groq`, alert at `alert_at`); Hermes does not edit that file.

## Choosing the LLM

`selection.llm_judge` picks the model that judges moments (and later writes titles). Two providers:

| `provider` | Talks to | Also needs |
| --- | --- | --- |
| `anthropic` | Claude | nothing: the key is read from `ANTHROPIC_API_KEY` |
| `openai_compatible` | OpenRouter, Groq, Gemini's compatible endpoint, a local Ollama | `base_url` (https, or http on localhost) and `api_key_env` |

The key is never in config: `api_key_env` is the NAME of an environment variable (a pasted key is rejected). Put the key
in `.env`. Add the model and its price to `llm_prices.yaml` (0 for a free model) or validation fails: the daily budget
stop can only count what it has a price for. Switch provider per profile, or per source under `overrides`.

`fallbacks` is an ordered list of backups (up to 3, each with its own provider, model, endpoint and key variable). When the
primary fails (rate limit, outage, rejected key, a model that no longer exists) the next one answers and you are told; a
failed provider is skipped for `cooldown_s` seconds. The cost ledger prices each call as the model that answered.
`make llm-check PROFILE=<name>` makes one tiny call to every provider in the chain and prints which ones answer today.

## Facebook and Instagram tokens

Meta's Graph API Explorer gives a token that lasts an hour or two. The publisher needs Page tokens that last. One-time setup:

1. Put the Meta app's `META_APP_ID` and `META_APP_SECRET` (App settings > Basic) in `.env`.
2. In the Graph API Explorer, choose your app and generate a user token with `pages_show_list`, `pages_manage_posts`,
   `pages_read_engagement`, `instagram_basic` and `instagram_content_publish`, and pick your Page and Instagram account in the dialog.
3. `make meta-tokens` and paste the token when asked (input is hidden). It lists your Pages, their Instagram accounts and how
   long each Page token lasts. Nothing secret is printed.
4. `make meta-tokens ARGS="--page <id or name> --as test --write"` saves that Page's id and token into the `FB_TEST_*` and
   `IG_TEST_*` variables that `accounts.yaml` reads (`--as mlbb` for the brand Page). The old `.env` is kept as `.env.bak`.

The user token is not saved; only Page tokens are. The listing also shows what the profile may do on each Page (its tasks) and flags any Page where it lacks
`CREATE_CONTENT` or `MANAGE`, because seeing a Page is not the same as being allowed to post to it.

If it says the token can reach no Pages, it prints who the token belongs to, which permissions were granted and which Page ids
were ticked in the login dialog, then the likely causes. The usual ones: no Page was ticked in the dialog; the Page is managed
through a business portfolio (add the `business_management` permission, and give that person full access to the Page in Business
settings); or the token belongs to a different Facebook profile than the one that administers the Page. The tool already looks
Pages up by the ids the dialog granted, which finds some that Meta's `/me/accounts` list leaves out.

If you know the Page's numeric id (Page > About > Page transparency, or Business settings), add it and Meta's own answer for that
Page is shown, which names the real problem: `make meta-tokens ARGS="--page-id <id>"`.

For Pages owned by a Business portfolio the sturdier route is a **system user** token (Business settings > Users > System users:
add one, assign the Page and the Instagram account as assets with full control, then generate a token for your app with the
permissions above and the longest expiry offered). That token is not tied to a person's login. Use it as given:
`make meta-tokens ARGS="--no-exchange --page-id <id> --page <id> --as test --write"`.

## Where clips are uploaded

`Profile.upload_accounts()` decides; the publisher must call it.

| Condition | Uploads to |
| --- | --- |
| `mode: rnd` | `test_targets` (accounts with `test: true`), public |
| `mode: publish` | `targets` (brand accounts) |
| source `clip_policy: blocked`, or source/profile disabled | nowhere |
| source paused in Postgres after a claim or strike | nowhere (checked by the publisher) |

Every clip waits for your Telegram approval while `review.required` is true (the default). Set it to
false, for a whole profile or for one source under `overrides`, and clips go out without your tap.

## Hermes Agent's role

- Create or edit profiles and sources from plain requests, within `hermes.yaml`: validated by `config_schema.py`,
  shown as a diff, applied after your tap, with a backup of every file it overwrites.
- Daily report; scout suggestions; status questions; allow-listed actions you confirm (pause a source,
  requeue a dead job). Approvals belong to the review service, not Hermes.
- It does **not** scale workers or hold platform tokens.

## Licenses (commercial use)

PyYAML (MIT), pydantic (MIT). Caption font must be OFL or commercially licensed.

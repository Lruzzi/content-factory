# Content Factory

Config-driven pipeline that clips live streams and long videos into vertical Shorts, reviewed in Telegram, posted to YouTube, TikTok, Instagram and Facebook.

- `CLAUDE.md` — rules for Claude Code (read every session)
- `docs/PRODUCT.md` — requirements and acceptance criteria (wins on conflicts)
- `docs/TECH.md` — architecture, services, data, infrastructure, roadmap
- `config/` — niche profiles, accounts, workers, validator (`python config/config_schema.py`)

Start with Phase 0 in `docs/TECH.md` → Roadmap.

## Getting started

Needs `uv` and Docker.

```
uv sync --locked            # Python 3.14 and the pinned dependencies
cp .env.example .env        # set POSTGRES_PASSWORD and REDIS_PASSWORD, and the same values in the two URLs
make up                     # Postgres 18 and Redis 8, listening on localhost only
make check                  # lint, types, tests, config, security audit, licenses
make itest                  # the same tests plus the ones that need Postgres and Redis (starts them, reads .env)
uv run pre-commit install   # gitleaks and ruff on every commit
```

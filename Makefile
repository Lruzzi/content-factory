UV ?= uv

.PHONY: install test itest llm-check meta-tokens youtube-auth build deploy groq-usage model-scores telegram-rooms r2-check lint format config security licenses check up down logs

install:
	$(UV) sync --locked

test:
	$(UV) run pytest

# tests that need Postgres and Redis: starts them with docker compose and reads the passwords from .env
itest:
	docker compose up -d --wait
	set -a; . ./.env; set +a; \
	TEST_REDIS_URL="redis://:$$REDIS_PASSWORD@localhost:6379/0" \
	TEST_DATABASE_URL="postgresql+pg8000://factory:$$POSTGRES_PASSWORD@localhost:5432/factory" \
	$(UV) run pytest

# try the LLM a profile is set up for with one tiny call: make llm-check PROFILE=mlbb (loads .env first)
llm-check:
	set -a; . ./.env; set +a; $(UV) run python -m common.llm $(PROFILE)

# long-lived Facebook Page and Instagram tokens from a short-lived one:
#   make meta-tokens                                               list the Pages the token can publish to
#   make meta-tokens ARGS="--page <id or name> --as test --write"   save one Page's values to .env
meta-tokens:
	set -a; . ./.env; set +a; $(UV) run python -m services.publisher.meta_tokens $(ARGS)

# sign in one YouTube account (opens a Google sign-in link; saves the token to the account's token_file):
#   make youtube-auth ACCOUNT=yt_test
youtube-auth:
	$(UV) run python -m services.publisher.youtube_auth --account $(ACCOUNT)

# the one image every service container runs (Dockerfile); `make up` builds it when it is missing
build:
	docker compose build

# how much of Groq's free audio limit the ledger says is used (needs the database to be reachable: make up)
groq-usage:
	set -a; . ./.env; set +a; $(UV) run python -m services.understand.usage

# which LLM and transcription engine made the clips you approved or rejected (the verdict is your Telegram taps)
model-scores:
	set -a; . ./.env; set +a; $(UV) run python -m services.review.scores

# find the Telegram group and its topics and write config/telegram.yaml (stop the review service first).
# make telegram-rooms [GROUP="content factory"] [FLAGS=--create]
telegram-rooms:
	set -a; . ./.env; set +a; $(UV) run python -m services.review.rooms_setup $(GROUP) $(FLAGS)

# try the R2 keys for real: upload, signed link, unsigned refused, delete (give your r2.dev address to test it too)
r2-check:
	set -a; . ./.env; set +a; $(UV) run python -m common.r2_check $(PUBLIC_URL)

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

# validates every profile, workers.yaml and hermes.yaml
config:
	$(UV) run python config/config_schema.py

security:
	$(UV) run bandit -q -r common services migrations config scripts
	$(UV) run pip-audit

licenses:
	$(UV) run pip-licenses --from=mixed --format=json | $(UV) run python scripts/check_licenses.py

# everything the CI "checks" job runs
check: lint test config security licenses

up:
	docker compose up -d --wait

# After ANY code or config_schema.py change: rebuild the image and recreate every container on it. The config folder is
# mounted live but each service loads config_schema.py once at start, so a service left on the old code rejects new
# YAML keys (it logged "Input should be ..." for hours once) and old code runs on until it is recreated.
deploy:
	docker compose build
	docker compose up -d --wait

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

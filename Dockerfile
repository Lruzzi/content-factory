# One image for every Python service: each container runs a different `python -m services.<name>`.
# Base images are pinned by digest (checked 2026-10-23); Dependabot proposes updates.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.14-slim-trixie@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1

# FFmpeg is run as a separate program, never linked into our code. Debian's build includes GPL parts (x264): fine for
# private use; if the image is ever distributed, check those terms first.
# libgles2 and libegl1: MediaPipe's face detector loads them (the render service follows faces).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgles2 libegl1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# uv installs everything, so the base image's own pip is not needed. It also carries vendored copies of urllib3,
# msgpack and setuptools that scanners flag, and removing it removes them.
RUN /usr/local/bin/python -m pip uninstall -y pip

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
# dependencies first, so a code change does not reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY common ./common
COPY services ./services
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY assets ./assets

# a user without rights of its own; /media is where recordings, clips and previews live (a volume in compose)
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /media && chown app /media
USER app
ENV MEDIA_ROOT=/media

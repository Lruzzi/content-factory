"""The package stage as a worker handler: ask the LLM for a title, description and hashtags, then build the text each
platform gets (see compose.py) and hand the clip on to review. The engine does not know which accounts will post it:
all four platform texts are written and the publisher takes the one it needs. Everything about HOW comes from the
job's `spec` snapshot. A job run twice keeps its first text."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

import config_schema as cs
from common import jobs
from common.llm import cost
from common.llm.base import LLMClient, LLMError, complete_json
from common.llm.gate import Gate
from services.package import compose

MAX_TRANSCRIPT_CHARS = 1500
CONTEXT_S = 45  # how much of the stream before and after the clip the model may read, to know what the moment is about
CONTEXT_CHARS = 700
SYSTEM_MAX_CHARS = 20_000
DEFAULT_PROMPT = "prompts/package_default.md"


class PackageError(RuntimeError):
    pass


def _system_prompt(config_dir: Path, name: str) -> str:
    path = (config_dir / name).resolve()
    if not path.is_relative_to(config_dir.resolve()) or not path.is_file():
        raise PackageError(f"prompt file {name!r} is not inside the config folder")
    return path.read_text(encoding="utf-8")[:SYSTEM_MAX_CHARS]


def _words_text(words: list[dict]) -> str:
    text = " ".join(str(w.get("text", "")) for w in words)
    return " ".join(text.split())[:MAX_TRANSCRIPT_CHARS].replace("<", "‹").replace(">", "›")


def _context(engine: Engine, clip: dict[str, Any]) -> tuple[str, str]:
    """What was said just before and just after the clip on the stream (not in the video): who did what, what led up to
    it. Empty for a clip with no stream behind it (a recap, a test) and when nothing was transcribed there."""
    try:
        stream_id = uuid.UUID(str(clip["stream_id"]))
        start, end = float(clip["start_s"]), float(clip["end_s"])
    except KeyError, ValueError, TypeError:
        return "", ""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT s->>'text', (s->>'start_s')::float, (s->>'end_s')::float FROM transcripts t, "
                "jsonb_array_elements(t.sentences) s WHERE t.stream_id = :i AND t.start_s < :b "
                "AND t.start_s + t.duration_s > :a "
                "ORDER BY (s->>'start_s')::float"
            ),
            {"i": stream_id, "a": start - CONTEXT_S, "b": end + CONTEXT_S},
        ).all()

    def clean(texts: list[str]) -> str:
        return " ".join(" ".join(texts).split())[:CONTEXT_CHARS].replace("<", "‹").replace(">", "›")

    before = [r[0] for r in rows if r[2] <= start + 0.5 and r[1] >= start - CONTEXT_S]
    after = [r[0] for r in rows if r[1] >= end - 0.5 and r[2] <= end + CONTEXT_S]
    return clean(before[-12:]), clean(after[:12])


def _prompt(profile: cs.Profile, clip: dict, render: dict, context: tuple[str, str] = ("", "")) -> str:
    hook = f"On-screen hook: {render['hook']}\n" if render.get("hook") else ""
    before, after = context
    around = (f"<before>\n{before}\n</before>\n" if before else "") + (f"<after>\n{after}\n</after>\n" if after else "")
    return (
        f"Language: {profile.language}. Niche: {profile.niche}.\n"
        f"Kind of moment: {clip['kind']}. Why it was picked: {' '.join(str(clip.get('reason', '')).split())[:300]}\n"
        f"{hook}"
        "<transcript>\n" + _words_text(render.get("words", [])) + "\n</transcript>\n"
        f"{around}"
        'Reply with only JSON: {"title": "...", "description": "...", "hashtags": ["#..."]}'
    )


def make_handler(
    engine: Engine,
    client_for: Callable[[cs.LLMJudge], LLMClient],
    prices: dict[str, cs.Price],
    config_dir: Path,
    *,
    next_stage: str = "review",
    gate: Gate | None = None,
) -> Callable[[jobs.Job], str]:
    def handle(job: jobs.Job) -> str:
        data = job["data"] or {}
        if "package" in data:
            return next_stage
        profile = cs.Profile.model_validate(job["spec"])
        if not profile.selection.llm_judge.enabled:
            raise PackageError("packaging needs selection.llm_judge to be enabled")
        judge = profile.selection.llm_for("package")
        settings = profile.package
        clip, render = data["clip"], data["render"]
        if gate is not None:
            gate.check(judge)  # out of quota: wait, do not ask
        try:
            answer, results = complete_json(
                client_for(judge),
                compose.Answer,
                system=_system_prompt(config_dir, settings.get("prompt", DEFAULT_PROMPT)),
                prompt=_prompt(profile, clip, render, _context(engine, clip)),
                max_tokens=judge.max_output_tokens,
            )
        except LLMError as exc:
            if gate is not None and exc.rate_limited:
                raise gate.trip(judge, exc) from exc  # one alert for everyone, then the job waits for the reset
            raise
        link = None
        if settings.get("include_source_link", True) and clip.get("video_id"):
            link = f"https://www.youtube.com/watch?v={clip['video_id']}"
        hashtags = settings.get("hashtags", {})
        texts = compose.compose(
            title=answer.title,
            description=answer.description,
            generated=answer.hashtags,
            fixed=hashtags.get("fixed", []),
            platform_fixed=hashtags.get("platform_fixed", {}),
            generated_max=hashtags.get("generated_max", 4),
            source_link=link,
            ai_disclosure=settings.get("ai_disclosure", False),
        )
        with engine.begin() as conn:
            for result in results:
                cost.record(
                    conn,
                    profile=profile.name,
                    service="package",
                    result=result,
                    prices=prices,
                    spec=judge,
                    job_id=job["id"],
                )
            jobs.set_result(
                conn,
                job["id"],
                data={
                    "package": {p: {"title": t.title, "text": t.text, "hashtags": t.hashtags} for p, t in texts.items()}
                },
            )
        return next_stage

    return handle

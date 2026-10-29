"""The select stage as a worker handler: read a segment's transcript, ask the LLM for moments, and make one render job
per accepted clip. The segment job itself is finished (the handler returns None); the new jobs are published here and
the reconciler republishes any that were missed. Everything about HOW comes from the job's `spec` snapshot."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

import sqlalchemy as sa
from sqlalchemy import Engine

import config_schema as cs
from common import jobs
from common import rules as rules_mod
from common.llm import cost
from common.llm.base import LLMClient, LLMError, complete_json
from common.llm.gate import Gate
from services.select import chatspikes, moments
from services.select import glossary as glossary_mod

log = logging.getLogger(__name__)
SYSTEM_MAX_CHARS = 20_000


class SelectError(RuntimeError):
    pass


def system_prompt(config_dir: Path, name: str) -> str:
    path = (config_dir / name).resolve()
    if not path.is_relative_to(config_dir.resolve()) or not path.is_file():
        raise SelectError(f"prompt file {name!r} is not inside the config folder")
    return path.read_text(encoding="utf-8")[:SYSTEM_MAX_CHARS]


def make_handler(
    engine: Engine,
    client_for: Callable[[cs.LLMJudge], LLMClient],
    prices: dict[str, cs.Price],
    config_dir: Path,
    *,
    publish_job: Callable[[Any], object] | None = None,
    recap: Callable[[jobs.Job, cs.Profile], int] | None = None,
    alert: Callable[[str], object] | None = None,
    gate: Gate | None = None,
) -> Callable[[jobs.Job], None]:
    def handle(job: jobs.Job) -> None:
        profile = cs.Profile.model_validate(job["spec"])
        sel, transcript = profile.selection, profile.signals.transcript
        if sel.mode == "signals" or not sel.llm_judge.enabled or transcript.mode != "full":
            return None  # nothing to read: this profile selects from signals, which are not built yet
        data = job["data"] or {}
        start, end = float(data["start_s"]), float(data["end_s"])
        with engine.connect() as conn:
            if conn.execute(
                sa.text("SELECT 1 FROM jobs WHERE data->'clip'->>'segment_job' = :j LIMIT 1"), {"j": str(job["id"])}
            ).first():
                return None  # this segment was already handled
            current = conn.execute(
                sa.text("SELECT sentences, words FROM transcripts WHERE media_key = :k"), {"k": job["media_key"]}
            ).first()
            if current is None:
                raise SelectError(f"no transcript for {job['media_key']!r} yet")
            previous = conn.execute(
                sa.text(
                    "SELECT sentences FROM transcripts "
                    "WHERE stream_id = :s AND start_s < :a ORDER BY start_s DESC LIMIT 1"
                ),
                {"s": data.get("stream_id"), "a": start},
            ).first()
            taken = [
                (float(a), float(b))
                for a, b in conn.execute(
                    sa.text(
                        "SELECT (data->'clip'->>'start_s')::float, (data->'clip'->>'end_s')::float FROM jobs "
                        "WHERE data->'clip'->>'stream_id' = :s"
                    ),
                    {"s": str(data.get("stream_id"))},
                )
            ]
        with engine.connect() as conn:
            left = rules_mod.clips_left(
                conn, sel.clips_per_day
            )  # the day's clips: what you set, or the profile's number
        if left == 0:
            log.info("segment %s: the limit of clips for 24 hours is reached, so nothing is made", job["media_key"])
            return None
        context = [x for x in (previous[0] if previous else []) if x["end_s"] > start - profile.live.segment_overlap_s]
        spikes: list[chatspikes.Spike] = []
        if profile.signals.chat.enabled and data.get("segment") is not None and data.get("stream_id"):
            with engine.connect() as conn:
                spikes = chatspikes.load(
                    conn, uuid.UUID(str(data["stream_id"])), int(data["segment"]), profile.signals.chat
                )
        if not current[0] and not spikes:
            return None  # a silent segment
        spoken = chatspikes.mark([*context, *current[0]], spikes, profile.signals.chat, (start, end))
        sentences = [moments.Sentence(i, x["text"], x["start_s"], x["end_s"]) for i, x in enumerate(spoken)]
        lengths = {
            kind: (sel.kinds[kind].clip_length_s if kind in sel.kinds and sel.kinds[kind].clip_length_s else None)
            or sel.clip_length_s
            for kind in get_args(cs.Kind)
        }
        judge = sel.llm_judge
        if gate is not None:
            gate.check(judge)  # out of quota: wait, do not ask
        try:
            answer, results = complete_json(
                client_for(judge),
                moments.Answer,
                system=system_prompt(config_dir, judge.prompt),
                prompt=moments.build_prompt(sentences, window=(start, end), language=profile.language, lengths=lengths),
                max_tokens=judge.max_output_tokens,
            )
        except LLMError as exc:
            if gate is not None and exc.rate_limited:
                raise gate.trip(judge, exc) from exc  # one alert for everyone, then the job waits for the reset
            raise
        proposed = answer.candidates
        if sel.callouts is not None:
            proposed = [
                *proposed,
                *moments.callouts(
                    sentences, window=(start, end), rules=sel.callouts, proposed=proposed,
                    max_s=lengths["highlight"][1],
                ),
            ]  # fmt: skip
        clips, dropped = moments.choose(
            proposed,
            sentences=sentences,
            window=(start, end),
            length_for=lambda kind: lengths[kind],
            min_score=sel.min_score,
            top_k=sel.top_k if left is None else min(sel.top_k, left),
            taken=taken,
            dedupe_overlap=sel.dedupe_overlap,
            min_gap_s=sel.min_gap_s,
        )
        for reason in dropped:
            log.info("segment %s: dropped candidate %s", job["media_key"], reason)
        if sel.trim is not None:  # cut to the words, not to the sentence times (see Trim)
            clips = [
                replace(c, start_s=cut[0], end_s=cut[1])
                for c in clips
                for cut in [
                    moments.trim_to_speech(
                        current[1], start=c.start_s, end=c.end_s, lead_s=sel.trim.lead_s, tail_s=sel.trim.tail_s,
                        min_len=lengths[c.kind][0], max_len=lengths[c.kind][1], window_end=end,
                    )
                ]
            ]  # fmt: skip
        clip_words = [
            [
                {"text": w["text"], "start_s": w["start_s"] - start, "end_s": w["end_s"] - start}
                for w in current[1]
                if clip.start_s <= w["start_s"] and w["end_s"] <= clip.end_s + 0.01
            ]
            for clip in clips
        ]
        fix_results: list[Any] = []
        glossary = transcript.glossary
        if (
            clips and glossary
        ):  # hero names and game terms Whisper misheard: one small question per clip, outside the database
            fixer = sel.llm_for("transcript_fix")
            system = glossary_mod.system_prompt(config_dir, transcript.fix_prompt)

            def ask(schema: Any, _system: str, prompt: str) -> tuple[Any, list[Any]]:
                if gate is not None:
                    gate.check(fixer)  # a chain out of quota is skipped: this is only an improvement
                return complete_json(
                    client_for(fixer), schema, system=_system, prompt=prompt, max_tokens=fixer.max_output_tokens
                )

            for index, words in enumerate(clip_words):
                clip_words[index], paid = glossary_mod.correct(
                    words, glossary, language=profile.language, system=system, ask=ask
                )
                fix_results += paid
        new_ids = []
        with engine.begin() as conn:
            for result in fix_results:
                cost.record(
                    conn, profile=profile.name, service="select", result=result, prices=prices,
                    spec=sel.llm_for("transcript_fix"), job_id=job["id"],
                )  # fmt: skip
            for result in results:
                cost.record(
                    conn,
                    profile=profile.name,
                    service="select",
                    result=result,
                    prices=prices,
                    spec=judge,
                    job_id=job["id"],
                )
            for clip, words in zip(clips, clip_words, strict=True):
                new_ids.append(
                    jobs.create_job(
                        conn,
                        profile,
                        stage="render",
                        source_id=job["source_id"],
                        media_key=job["media_key"],
                        kind=clip.kind,
                        data={
                            "clip": {
                                "stream_id": str(data.get("stream_id")),
                                "video_id": data.get("video_id"),
                                "segment_job": str(job["id"]),
                                "start_s": clip.start_s,
                                "end_s": clip.end_s,
                                "kind": clip.kind,
                                "score": clip.score,
                                "reason": clip.reason,
                                # who chose it and who transcribed it, so approvals can be counted per model
                                "llm": results[-1].price_key or results[-1].model,
                                "stt": f"{transcript.engine}/{transcript.model}",
                            },
                            "render": {
                                "source_key": job["media_key"],
                                "start_s": clip.start_s - start,
                                "end_s": clip.end_s - start,
                                "hook": clip.hook,
                                "words": words,
                            },
                        },
                    )
                )
        for job_id in new_ids:
            if publish_job is not None:
                try:
                    publish_job(job_id)
                except Exception:
                    log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
        if recap is not None and profile.compilation.enabled:
            try:  # best effort: the ordinary clips above are kept whatever happens here, the next segment tries again
                recap(job, profile)  # the step asks its own models, each behind the gate of its own chain
            except jobs.Deferred:
                log.info("the recap step for %s waits: the LLM quota is used up", job["media_key"])
            except Exception as exc:
                log.exception("the recap step failed for %s", job["media_key"])
                if alert:
                    alert(f"the recap step failed for {job['media_key']}: {type(exc).__name__}: {exc}")
        return None

    return handle

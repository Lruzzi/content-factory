"""After a segment is transcribed: find the matches that have finished in the recent transcript and make one recap job
for each new one (see recap.py for the rules). The LLM is asked twice: which matches finished, then which moments of a
new match to use. A match is remembered in `matches` whatever the outcome, so it is never asked about twice. The step
is best effort: the caller keeps the segment's ordinary clips even if this fails, and the next segment tries again."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common import jobs
from common import rules as rules_mod
from common.llm import cost
from common.llm.base import LLMClient, LLMError, complete_json
from common.llm.gate import Gate
from services.select import glossary as glossary_mod
from services.select import moments, recap
from services.select.handler import system_prompt

log = logging.getLogger(__name__)
MIN_SENTENCES = 20  # less than this is not enough transcript to find a match in


class RecapStep:
    def __init__(
        self,
        engine: sa.Engine,
        client_for: Callable[[cs.LLMJudge], LLMClient],
        prices: dict[str, cs.Price],
        config_dir: Path,
        *,
        publish_job: Callable[[Any], object] | None = None,
        max_per_run: int = 2,
        gate: Gate | None = None,
    ) -> None:
        self.engine, self.client_for, self.prices, self.config_dir = engine, client_for, prices, config_dir
        self.publish_job, self.max_per_run, self.gate = publish_job, max_per_run, gate

    def _due(self, stream_id: str, end_s: float, every_min: int) -> bool:
        """Is it time to ask which matches have finished? Once `every_min` minutes of stream have passed since the last
        look, and once more on the last segment of a finished stream (nothing comes after it to ask again)."""
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT recap_checked_s, status, (SELECT max(end_s) FROM segments WHERE stream_id = l.id) "
                    "FROM live_streams l WHERE id = :s"
                ),
                {"s": stream_id},
            ).first()
        if row is None or row[0] is None or every_min <= 0:
            return True
        checked, status, last_end = float(row[0]), row[1], row[2]
        if end_s - checked >= every_min * 60:
            return True
        return status == "ended" and last_end is not None and end_s >= float(last_end) - 1 and end_s > checked

    def _checked(self, stream_id: str, end_s: float) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE live_streams SET recap_checked_s = GREATEST(COALESCE(recap_checked_s, 0), :e) WHERE id = :s"
                ),
                {"e": end_s, "s": stream_id},
            )

    def _ask(self, judge: cs.LLMJudge, schema: Any, *, system: str, prompt: str) -> tuple[Any, list[Any]]:
        """One question to the model of that task, behind the gate of its own chain: a chain that is out of quota is
        not called, and one that runs out now is paused and the step waits (`jobs.Deferred`)."""
        if self.gate is not None:
            self.gate.check(judge)
        try:
            return complete_json(
                self.client_for(judge), schema, system=system, prompt=prompt, max_tokens=judge.max_output_tokens
            )
        except LLMError as exc:
            if self.gate is not None and exc.rate_limited:
                raise self.gate.trip(judge, exc) from exc
            raise

    def __call__(self, job: jobs.Job, profile: cs.Profile) -> int:
        """Returns how many recap jobs were made."""
        cfg, data = profile.compilation, job["data"] or {}
        if not cfg.enabled or not data.get("stream_id"):
            return 0
        stream_id = data["stream_id"]
        end_s = float(data["end_s"])
        if not self._due(stream_id, end_s, cfg.check_every_min):
            return 0
        with self.engine.connect() as conn:
            left = rules_mod.clips_left(conn, profile.selection.clips_per_day)
        if left == 0:
            return 0  # the limit of clips for 24 hours is reached: no recap either (it would not even be asked for)
        window_start = end_s - cfg.match_window_min * 60
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT sentences, words FROM transcripts WHERE stream_id = :s AND start_s + duration_s > :w "
                    "ORDER BY start_s"
                ),
                {"s": stream_id, "w": window_start},
            ).all()
            known = [
                (float(a), float(b))
                for a, b in conn.execute(
                    sa.text("SELECT start_s, end_s FROM matches WHERE stream_id = :s"), {"s": stream_id}
                )
            ]
            files = [
                recap.Segment(k, float(a), float(b))
                for k, a, b in conn.execute(
                    sa.text(
                        "SELECT key, start_s, end_s FROM segments WHERE stream_id = :s AND deleted_at IS NULL "
                        "ORDER BY idx"
                    ),
                    {"s": stream_id},
                )
            ]
        flat = [x for sentences, _ in rows for x in sentences if x["end_s"] > window_start]
        sentences = [moments.Sentence(i, x["text"], x["start_s"], x["end_s"]) for i, x in enumerate(flat)]
        if len(sentences) < MIN_SENTENCES:
            return 0
        words = [w for _, ws in rows for w in ws]
        judge = profile.selection.llm_for("recap_match")
        answer, spent = self._ask(
            judge,
            recap.MatchAnswer,
            system=system_prompt(self.config_dir, cfg.match_prompt),
            prompt=recap.build_match_prompt(sentences, language=profile.language),
        )
        self._pay(profile, judge, job, spent)
        self._checked(stream_id, end_s)
        found, dropped = recap.find_matches(answer, sentences, min_match_s=cfg.min_match_s)
        for why in dropped:
            log.info("match candidate dropped: %s", why)
        made: list[Any] = []
        for match in recap.fresh(found, known)[: self.max_per_run if left is None else min(self.max_per_run, left)]:
            job_id = self._recap(job, profile, match, sentences, words, files)
            if job_id is not None:
                made.append(job_id)
        for job_id in made:
            if self.publish_job is not None:
                try:
                    self.publish_job(job_id)
                except Exception:
                    log.exception("could not queue job %s; it stays queued in Postgres for the reconciler", job_id)
        return len(made)

    def _pay(self, profile: cs.Profile, judge: cs.LLMJudge, job: jobs.Job, results: list[Any]) -> None:
        with self.engine.begin() as conn:
            for result in results:
                cost.record(
                    conn, profile=profile.name, service="select", result=result, prices=self.prices, spec=judge,
                    job_id=job["id"],
                )  # fmt: skip

    def _remember(self, stream_id: str, match: recap.Match, status: str, note: str) -> Any:
        with self.engine.begin() as conn:
            return conn.execute(
                sa.text(
                    "INSERT INTO matches (stream_id, start_s, end_s, summary, status) VALUES (:s, :a, :b, :t, :st) "
                    "ON CONFLICT (tenant_id, stream_id, start_s) DO NOTHING RETURNING id"
                ),
                {
                    "s": stream_id,
                    "a": match.start_s,
                    "b": match.end_s,
                    "t": f"{match.summary} ({note})"[:300],
                    "st": status,
                },
            ).scalar_one_or_none()

    def _recap(
        self,
        job: jobs.Job,
        profile: cs.Profile,
        match: recap.Match,
        sentences: list[moments.Sentence],
        words: list[dict[str, Any]],
        files: list[recap.Segment],
    ) -> Any:
        cfg, judge, data = profile.compilation, profile.selection.llm_for("recap_pick"), job["data"]
        answer, spent = self._ask(
            judge,
            recap.RecapAnswer,
            system=system_prompt(self.config_dir, cfg.recap_prompt),
            prompt=recap.build_recap_prompt(
                sentences, match=match, moment_s=cfg.moment_s, length_s=cfg.length_s, max_moments=cfg.max_moments,
                language=profile.language,
            ),
        )  # fmt: skip
        self._pay(profile, judge, job, spent)
        answered = spent[-1].price_key or spent[-1].model
        chosen, dropped = recap.pick_moments(
            answer, sentences, match=match, moment_s=cfg.moment_s, length_s=cfg.length_s,
            min_moments=cfg.min_moments, max_moments=cfg.max_moments,
        )  # fmt: skip
        for why in dropped:
            log.info("recap moment dropped: %s", why)
        if chosen and profile.selection.trim is not None:  # each moment is cut to its words, as an ordinary clip is
            trim = profile.selection.trim
            chosen = [
                replace(c, start_s=cut[0], end_s=cut[1])
                for c in chosen
                for cut in [
                    moments.trim_to_speech(
                        words, start=c.start_s, end=c.end_s, lead_s=trim.lead_s, tail_s=trim.tail_s,
                        min_len=cfg.moment_s[0], max_len=cfg.moment_s[1], window_end=match.end_s,
                    )
                ]
            ]  # fmt: skip
        if not chosen:
            self._remember(data["stream_id"], match, "skipped", dropped[-1] if dropped else "no usable moments")
            return None
        pieces = [recap.resolve_pieces(files, c.start_s, c.end_s) for c in chosen]
        if any(p is None for p in pieces):
            self._remember(data["stream_id"], match, "skipped", "part of the recording is not available")
            return None
        flat_pieces = [
            {"source_key": p.source_key, "start_s": p.start_s, "end_s": p.end_s}
            for group in pieces
            for p in group or []
        ]
        in_match = [w for w in words if match.start_s <= w["start_s"] and w["end_s"] <= match.end_s]
        recap_words = recap.timeline(chosen, in_match)
        glossary = profile.signals.transcript.glossary
        if glossary:  # hero names and game terms Whisper misheard, fixed before the captions are drawn from them
            fixer = profile.selection.llm_for("transcript_fix")
            recap_words, paid = glossary_mod.correct(
                recap_words, glossary, language=profile.language,
                system=glossary_mod.system_prompt(self.config_dir, profile.signals.transcript.fix_prompt),
                ask=lambda schema, system, prompt: self._ask(fixer, schema, system=system, prompt=prompt),
            )  # fmt: skip
            self._pay(profile, fixer, job, paid)
        with self.engine.begin() as conn:
            made = conn.execute(
                sa.text(
                    "INSERT INTO matches (stream_id, start_s, end_s, summary, status) "
                    "VALUES (:s, :a, :b, :t, 'compiled') "
                    "ON CONFLICT (tenant_id, stream_id, start_s) DO NOTHING RETURNING id"
                ),
                {"s": data["stream_id"], "a": match.start_s, "b": match.end_s, "t": match.summary[:300]},
            ).scalar_one_or_none()
            if made is None:
                return None  # another run got there first
            job_id = jobs.create_job(
                conn,
                profile,
                stage="render",
                source_id=job["source_id"],
                media_key=str(flat_pieces[0]["source_key"]),
                kind="recap",
                data={
                    "clip": {
                        "kind": "recap",
                        "video_id": data.get("video_id"),
                        "segment_job": str(job["id"]),
                        "reason": match.summary,
                        "llm": answered,
                        "stt": f"{profile.signals.transcript.engine}/{profile.signals.transcript.model}",
                        "score": sum(c.score for c in chosen) / len(chosen),
                        "moments": [
                            {"start_s": c.start_s, "end_s": c.end_s, "score": c.score, "reason": c.reason}
                            for c in chosen
                        ],
                    },
                    "render": {"pieces": flat_pieces, "hook": answer.title, "words": recap_words},
                },
            )
            conn.execute(sa.text("UPDATE matches SET job_id = :j WHERE id = :m"), {"j": job_id, "m": made})
        return job_id

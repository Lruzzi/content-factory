"""The render stage as a worker handler: read the clip window from the job, render, hand on to packaging.

The job's `data["render"]` is written by the select stage; everything about HOW to render comes from the job's
`spec` snapshot, never from the YAML (NFR-12). Any exception here is recorded by the worker as a failed attempt.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine

import config_schema as cs
from common import jobs
from common.media import LocalStore
from services.render import assemble, captions
from services.render import ffmpeg as ff
from services.render.assemble import PieceIn

MAX_WORDS = 2_000


class WordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    start_s: float
    end_s: float


MAX_RECAP_S = 300  # the longest a recap may be (what Facebook was tested with); an ordinary clip stays under 180 s
MAX_PIECES = 40


class RenderInput(BaseModel):
    """What select hands to render: one window of one source file, or a list of pieces to be joined into one video (a
    recap). Times are on the clock of the file they refer to."""

    model_config = ConfigDict(extra="forbid")
    source_key: str | None = None
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = None
    pieces: list[PieceIn] | None = Field(default=None, min_length=1, max_length=MAX_PIECES)
    hook: str | None = Field(default=None, max_length=300)
    words: list[WordIn] = Field(default_factory=list, max_length=MAX_WORDS)
    output: dict[str, Any] | None = None  # filled in by this stage; accepted so a re-run can read its own record

    @model_validator(mode="after")
    def one_form_only(self) -> RenderInput:
        window = (self.source_key, self.start_s, self.end_s)
        if self.pieces is not None:
            if any(v is not None for v in window):
                raise ValueError("render takes one window or a list of pieces, not both")
            if any(p.end_s <= p.start_s for p in self.pieces):
                raise ValueError("every piece must end after it starts")
            if sum(p.length_s for p in self.pieces) > MAX_RECAP_S:
                raise ValueError(f"the pieces together are longer than {MAX_RECAP_S} s")
        elif any(v is None for v in window):
            raise ValueError("render needs a window (source_key, start_s, end_s) or a list of pieces")
        elif self.end_s <= self.start_s:  # type: ignore[operator]
            raise ValueError("render window must end after it starts")
        return self


def make_handler(engine: Engine, store: LocalStore, *, next_stage: str = "package") -> Callable[[jobs.Job], str]:
    def handle(job: jobs.Job) -> str:
        profile = cs.Profile.model_validate(job["spec"])
        data = RenderInput.model_validate((job["data"] or {}).get("render") or {})
        clip_key = f"clips/{job['id']}.mp4"  # one key per job: a re-run replaces the clip, never adds one
        words = [captions.Word(w.text, w.start_s, w.end_s) for w in data.words]
        if data.pieces is not None:  # a recap: join the pieces into one source, then render it like any other clip
            with tempfile.TemporaryDirectory(prefix="assemble-") as tmp:
                joined = assemble.join(store, data.pieces, Path(tmp) / "recap-source.mp4")
                length = min(sum(p.length_s for p in data.pieces), ff.probe(joined).duration_s)
                out = ff.render_clip(
                    profile,
                    joined,
                    0.0,
                    length,
                    store.path(clip_key),
                    words=words,
                    hook=data.hook,
                    max_s=MAX_RECAP_S,
                    kind="recap",
                )
        else:
            source = store.path(data.source_key)  # type: ignore[arg-type]
            if not source.is_file():
                raise ff.RenderError(f"source {data.source_key!r} is not in the media store")
            out = ff.render_clip(
                profile,
                source,
                data.start_s,  # type: ignore[arg-type]
                data.end_s,  # type: ignore[arg-type]
                store.path(clip_key),
                words=words,
                hook=data.hook,
                kind=job.get("kind"),
            )
        info = ff.probe(out)
        record = data.model_dump()
        record["output"] = {
            "key": clip_key,
            "duration_s": round(info.duration_s, 3),
            "width": info.width,
            "height": info.height,
            "bytes": out.stat().st_size,
        }
        with engine.begin() as conn:
            jobs.set_result(conn, job["id"], media_key=clip_key, data={"render": record})
        return next_stage

    return handle

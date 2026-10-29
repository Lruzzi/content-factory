"""The stages a job moves through, and the stream each stage reads from.

A job waits in the stream of the stage that will work on it next. Stream names are internal: the
names in TECH.md (video.found, clip.selected, ...) describe the same hand-offs.
"""

from __future__ import annotations

STAGES = ("download", "understand", "select", "render", "package", "review", "publish")


def check_stage(stage: str) -> str:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r} (stages: {', '.join(STAGES)})")
    return stage


def next_stage(stage: str) -> str | None:
    """The stage after this one, or None after the last."""
    position = STAGES.index(check_stage(stage)) + 1
    return STAGES[position] if position < len(STAGES) else None


def stream_for(stage: str, prefix: str = "") -> str:
    """The stream that feeds a stage. A prefix isolates tests, or one host's queues from another's."""
    name = f"jobs.{check_stage(stage)}"
    return f"{prefix}.{name}" if prefix else name

"""Which model's clips get approved. Every clip records the LLM that chose it and the engine that transcribed it;
your taps in Telegram are the verdict. `python -m services.review.scores` (or `make model-scores`) prints the counts,
so a model can be kept or dropped by evidence. Needs DATABASE_URL."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

DECIDED = ("approved", "rejected", "expired")


@dataclass(frozen=True)
class Score:
    llm: str
    stt: str
    approved: int
    rejected: int
    waiting: int

    @property
    def rate(self) -> float | None:
        """Approved clips as a share of the ones you decided on (a clip you let expire counts as a no)."""
        decided = self.approved + self.rejected
        return self.approved / decided if decided else None


_SQL = (
    "SELECT COALESCE(data->'clip'->>'llm', 'unknown') AS llm, COALESCE(data->'clip'->>'stt', 'unknown') AS stt, "
    "count(*) FILTER (WHERE data->'review'->>'state' IN ('approved', 'auto')) AS approved, "
    "count(*) FILTER (WHERE data->'review'->>'state' IN ('rejected', 'expired')) AS rejected, "
    "count(*) FILTER (WHERE data->'review'->>'state' = 'pending') AS waiting "
    "FROM jobs WHERE data ? 'clip' AND data->'review'->>'state' IS NOT NULL "
    "GROUP BY 1, 2 ORDER BY 1, 2"
)


def _rows(conn: sa.Connection) -> list[Score]:
    return [Score(r[0], r[1], int(r[2]), int(r[3]), int(r[4])) for r in conn.execute(sa.text(_SQL))]


def _merge(scores: list[Score], by: str) -> list[Score]:
    merged: dict[str, Score] = {}
    for s in scores:
        key = s.llm if by == "llm" else s.stt
        old = merged.get(key)
        merged[key] = Score(
            key if by == "llm" else "*", key if by == "stt" else "*",
            (old.approved if old else 0) + s.approved, (old.rejected if old else 0) + s.rejected,
            (old.waiting if old else 0) + s.waiting,
        )  # fmt: skip
    return list(merged.values())


def by_model(conn: sa.Connection) -> list[Score]:
    return _merge(_rows(conn), "llm")


def by_stt(conn: sa.Connection) -> list[Score]:
    return _merge(_rows(conn), "stt")


@dataclass(frozen=True)
class RejectReason:
    llm: str
    reason: str
    count: int


def reject_reasons(conn: sa.Connection) -> list[RejectReason]:
    """Why rejected clips were rejected, per LLM; a rejection tapped without a reason counts as 'none given'."""
    rows = conn.execute(
        sa.text(
            "SELECT COALESCE(data->'clip'->>'llm', 'unknown'), COALESCE(data->'review'->>'reason', 'none given'), "
            "count(*) FROM jobs WHERE data ? 'clip' AND data->'review'->>'state' = 'rejected' "
            "GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2"
        )
    )
    return [RejectReason(r[0], r[1], int(r[2])) for r in rows]


def _line(label: str, s: Score) -> str:
    rate = "n/a" if s.rate is None else f"{s.rate:.0%}"
    return f"  {label:<55} approved {s.approved:>3}  rejected {s.rejected:>3}  waiting {s.waiting:>3}  rate {rate}"


def report(conn: sa.Connection) -> str:
    lines = ["Clips by the LLM that chose them (approved / decided; an expired clip counts as rejected):"]
    lines += [_line(s.llm, s) for s in by_model(conn)] or ["  nothing reviewed yet"]
    lines.append("Clips by the transcription engine:")
    lines += [_line(s.stt, s) for s in by_stt(conn)] or ["  nothing reviewed yet"]
    lines.append("Why clips were rejected, by the LLM that chose them:")
    grouped: dict[str, list[str]] = {}
    for r in reject_reasons(conn):
        grouped.setdefault(r.llm, []).append(f"{r.reason} {r.count}")
    lines += [f"  {llm:<55} {', '.join(items)}" for llm, items in grouped.items()] or ["  no rejections yet"]
    return "\n".join(lines)


def main() -> None:
    from common.db import make_engine

    with make_engine().connect() as conn:
        print(report(conn))


if __name__ == "__main__":
    main()

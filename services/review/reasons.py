"""Why a clip was rejected: a short, fixed list of reasons offered after you tap Reject, so the numbers can say what is
going wrong (a model that picks boring moments, a layout that crops badly) instead of only how many were rejected. The
codes are what is stored, so they stay stable; the labels are what you read."""

from __future__ import annotations

REASONS = {
    "boring": "😴 Boring, no real moment",
    "cut": "✂️ Starts or ends badly",
    "layout": "🖼 Bad crop or layout",
    "audio": "🔇 Unclear speech or audio",
    "topic": "🚫 Not suitable (topic)",
    "dupe": "♻️ Same as another clip",
    "other": "🤷 Other",
}
SKIP = "skip"


def buttons(job_id: object) -> list[list[tuple[str, str]]]:
    """The reasons two to a row, then Skip; each button carries the reason code and the clip."""
    items = [(label, f"w:{code}:{job_id}") for code, label in REASONS.items()]
    rows = [items[i : i + 2] for i in range(0, len(items), 2)]
    return [*rows, [("Skip", f"w:{SKIP}:{job_id}")]]

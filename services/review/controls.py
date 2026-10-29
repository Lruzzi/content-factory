"""Pause and resume from the chat. What you can pause: posting (`publish`), intake (`intake`: no new recordings, no
discovery; recordings under way go on) and a single source (`src:<key>`). A switch is a row in `pauses` with no end
time, so it survives a restart and every service sees it; a source pause is the one the sources table already has.
A source paused after a copyright claim cannot be resumed from here (FR-10): you look at the claim first."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from common import pauses, sources, spend
from common import rules as rules_mod

LABELS = {
    "publish": "posting",
    "live": "new live recordings",
    "ended": "discovery of ended streams",
    "intake": "both: new live recordings and discovery of ended streams",
}
CLAIM_NOTE = (
    "It was paused after a copyright claim, so it cannot be resumed from here: check the claim first, then resume it "
    "from the machine."
)


SHORT = {"publish": "posting", "live": "live recordings", "ended": "ended-stream discovery", "intake": "intake"}


def label(what: str) -> str:
    return LABELS.get(what) or f"source {what.removeprefix('src:')}"


def question(what: str) -> str:
    effect = {
        "publish": "Approved clips wait and nothing is posted; nothing is lost.",
        "live": "No new live stream is recorded; recordings under way go on. Ended streams are still looked for.",
        "ended": "No ended stream or past video is looked for or fetched. Live streams are still recorded.",
        "intake": "No new recordings are started and nothing new is fetched; recordings under way go on.",
    }.get(what, "It is not recorded or fetched until you resume it.")
    return f"Pause {label(what)}?\n{effect}"


def _source(conn: sa.Connection, key: str) -> sa.Row | None:
    return conn.execute(
        sa.text("SELECT id, paused_at, pause_reason FROM sources WHERE source_key = :k ORDER BY created_at LIMIT 1"),
        {"k": key},
    ).first()


def pause(engine: sa.Engine, what: str, by: int) -> tuple[bool, str]:
    """Pause something. Returns (did it, what to tell you)."""
    with engine.begin() as conn:
        if what in LABELS:
            if pauses.active(conn, pauses.switch_key(what)) is not None:
                return False, f"Already paused: {label(what)}"
            pauses.hold(
                conn, pauses.switch_key(what), f"paused from Telegram by {by} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC"
            )
            return True, f"{label(what).capitalize()} is paused. Use /resume to carry on."
        row = _source(conn, what.removeprefix("src:"))
        if row is None:
            return False, "No such source"
        if row[1] is not None:
            return False, f"Already paused: {label(what)}"
        sources.pause(conn, row[0], updated_by=f"telegram:{by}")
    return True, f"{label(what).capitalize()} is paused. Use /resume to carry on."


def resume(engine: sa.Engine, what: str, by: int) -> tuple[bool, str]:
    """Resume something. Returns (did it, what to tell you)."""
    with engine.begin() as conn:
        if what in LABELS:
            if not pauses.release(conn, pauses.switch_key(what)):
                return False, f"Not paused: {label(what)}"
            return True, f"{label(what).capitalize()} resumed."
        row = _source(conn, what.removeprefix("src:"))
        if row is None:
            return False, "No such source"
        if row[1] is None:
            return False, f"Not paused: {label(what)}"
        if row[2] == "claim":
            return False, f"{label(what).capitalize()}: {CLAIM_NOTE}"
        sources.resume(conn, row[0], updated_by=f"telegram:{by}")
    return True, f"{label(what).capitalize()} resumed."


def budget_question(engine: sa.Engine, limits: spend.Limits, new: float) -> tuple[bool, str]:
    """(can it be asked, the question or the reason it cannot): a cap the ceiling refuses is not even offered."""
    try:
        spend.check_cap(limits, new)
    except ValueError as exc:
        return False, str(exc)
    with engine.connect() as conn:
        now = spend.load(conn, limits).cap_usd
    return True, f"Change the monthly OpenRouter cap from ${now:.2f} to ${new:.2f}?"


def set_budget(engine: sa.Engine, limits: spend.Limits, new: float, by: int) -> tuple[bool, str]:
    """Keep a new cap. Returns (did it, what to tell you); the ceiling in workers.yaml is enforced by `spend`."""
    try:
        with engine.begin() as conn:
            spend.set_cap(conn, limits, new, by=f"telegram:{by}")
    except ValueError as exc:
        return False, str(exc)
    return True, f"The cap is now ${new:.2f}."


RULE_WORDS = {
    "cap": "posts per account in 24 hours",
    "spacing": "minutes between posts",
    "clips": "clips made in 24 hours (0: no limit)",
    "window": "posting window",
}


def rule_question(engine: sa.Engine, ceilings: rules_mod.Ceilings | None, name: str, raw: str) -> tuple[bool, str, str]:
    """(can it be asked, the question or the reason it cannot, the value for the button): a value the ceilings refuse
    is not even offered."""
    if ceilings is None:
        return False, "The rules are not set up (workers.yaml: rules.ceilings).", ""
    try:
        value = rules_mod.parse(name, raw, ceilings)
    except ValueError as exc:
        return False, str(exc), ""
    shown = (", ".join(value) or "any time") if isinstance(value, list) else str(value)
    return True, f"Change {RULE_WORDS[name]} to {shown}?", rules_mod.encode(name, value) or "any"


def set_rule(
    engine: sa.Engine, ceilings: rules_mod.Ceilings | None, name: str, token: str, by: int
) -> tuple[bool, str]:
    """Keep a rule (the Yes tap). Returns (did it, what to tell you)."""
    if ceilings is None:
        return False, "The rules are not set up."
    try:
        value = rules_mod.decode(name, token, ceilings)
        with engine.begin() as conn:
            rules_mod.set_value(conn, name, value, ceilings, f"telegram:{by}")
    except ValueError as exc:
        return False, str(exc)
    return True, "Done."

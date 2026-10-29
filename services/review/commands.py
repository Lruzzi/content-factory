"""What you can ask the bot in the chat: /status, /posts, /dead, /help. Read-only: the only action is the Requeue button
on a dead job (the listener handles that). Answers are plain text built from Postgres and the ledger; nothing you type
other than an exact command word is ever read (see `parse`)."""

from __future__ import annotations

import html
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

import config_schema as cs
from common import heartbeat, pauses, pipeline, spend
from common import rules as rules_mod
from services.publisher import accounts as acc
from services.publisher import posts, schedule
from services.review import controls
from services.understand import budget, planner

MENU = [
    ("status", "Queue, quotas and what is waiting"),
    ("health", "Which services are alive"),
    ("posts", "Today's posts per account"),
    ("budget", "Monthly OpenRouter budget and its cap"),
    ("rules", "Posting rules: cap, spacing, window, clips a day"),
    ("dead", "Failed jobs you can requeue"),
    ("pause", "Pause posting, intake or a source"),
    ("resume", "Resume what is paused"),
    ("help", "What I can do"),
]
COMMAND = re.compile(r"/([a-z_]{1,32})(?:@\w{1,64})?")
BUDGET_AMOUNT = re.compile(r"/budget(?:@\w{1,64})? (\d{1,5}(?:\.\d{1,2})?)")  # `/budget 25`: dollars, up to cents
RULE_COMMAND = re.compile(
    r"/(cap|spacing|clips|window)(?:@\w{1,64})? ([0-9:,.\- a-zA-Z]{1,60})"
)  # `/cap 6`, `/window 10:00-23:00`
RULE_NAMES = ("cap", "spacing", "clips", "window")
ERROR_CHARS = 200
FINAL = ("posted", "failed", "skipped")
SERVICES_LISTED = 3  # more services than this are down: say how many and leave the names to /health


ICONS = {
    "Jobs": "🔧",
    "Waiting for you": "👀",
    "Groq audio": "🎙",
    "LLM": "🤖",
    "OpenRouter": "💰",
    "Paused": "⏸",
    "Services": "🖥",
    "Recording now": "🔴",
    "Live but not being recorded": "⚠️",
    "Waiting to post": "📤",
}
KEY = re.compile(r"(\s*)((?:[^\w\s:]+ )?)([^:\n]{1,40}):(?=\s|$)")


def _icon(line: str) -> str:
    return f"{ICONS[key]} {line}" if (key := line.split(":", 1)[0]) in ICONS else line


def to_html(text: str) -> str:
    """The status text as Telegram HTML: the first line and each `key:` in bold, every character escaped."""
    out = []
    for n, line in enumerate(text.split("\n")):
        if n == 0:
            out.append(f"<b>{html.escape(line)}</b>")
        elif m := KEY.match(line):
            out.append(f"{html.escape(m[1] + m[2])}<b>{html.escape(m[3])}:</b>{html.escape(line[m.end() :])}")
        else:
            out.append(html.escape(line))
    return "\n".join(out)


def budget_amount(text: Any) -> float | None:
    """The dollars of a message that is exactly `/budget <amount>` ("/budget 25", "/budget@TheBot 12.50"), else None."""
    if not isinstance(text, str) or len(text) > 100:
        return None
    match = BUDGET_AMOUNT.fullmatch(text.strip())
    return float(match.group(1)) if match else None


def rule_change(text: Any) -> tuple[str, str] | None:
    """(rule, value) of a message that is exactly `/cap 6`, `/spacing 120`, `/clips 6` or `/window 10:00-23:00`, else
    None. The value is only checked later, by `common.rules.parse`, against the ceilings in workers.yaml."""
    if not isinstance(text, str) or len(text) > 100:
        return None
    match = RULE_COMMAND.fullmatch(text.strip())
    return (match.group(1), match.group(2).strip()) if match else None


def parse(text: Any) -> str | None:
    """The command word of a message that is exactly one command ("/status" or "/status@TheBot"), else None."""
    if not isinstance(text, str) or len(text) > 100:
        return None
    match = COMMAND.fullmatch(text.strip())
    return match.group(1) if match else None


def help_text() -> str:
    """The commands, one line each. Type them in the Commands topic (or in a private chat with the bot)."""
    return "I answer these (nothing else you type is read):\n" + "\n".join(f"/{c} - {d}" for c, d in MENU)


@dataclass(frozen=True)
class DeadJob:
    id: uuid.UUID
    stage: str
    kind: str | None
    error: str


class Commands:
    def __init__(
        self,
        engine: sa.Engine,
        limits: budget.Limits | None,
        accounts: acc.Accounts,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        expected: list[str] | None = None,
        stale_after_s: float = 120,
        spend_limits: spend.Limits | None = None,
        planner_cfg: planner.Config | None = None,
        rule_ceilings: rules_mod.Ceilings | None = None,
        clips_default: int = 6,
    ) -> None:
        self.engine, self.limits, self.accounts, self.now = engine, limits, accounts, now
        self.expected, self.stale_after_s, self.spend_limits = expected, stale_after_s, spend_limits
        self.planner_cfg = planner_cfg
        self.rule_ceilings, self.clips_default = rule_ceilings, clips_default

    def help(self) -> str:
        return help_text()

    # ---------- /status ----------------------------------------------------------------------------------------------
    def status(self) -> str:
        with self.engine.connect() as conn:
            head = f"📊 Status\n{self._summary(conn)}"
            groups = [
                self._jobs(conn),
                [self._groq(conn), self._llm(conn), *self._openrouter(conn)],
                [*self._paused(conn), *self._services(conn), *self._recording(conn)],
                self._waiting_to_post(conn),
            ]
        return "\n\n".join([head, *("\n".join(map(_icon, g)) for g in groups if g)])

    def status_html(self) -> str:
        return to_html(self.status())

    def _summary(self, conn: sa.Connection) -> str:
        """One line: what needs a look (hand pauses are not listed, they show in the Paused line)."""
        dead = int(conn.execute(sa.text("SELECT count(*) FROM jobs WHERE status = 'dead'")).scalar_one())
        items = [f"{dead} dead job{'s' * (dead != 1)}"] if dead else []
        if self.expected:
            down = sum(1 for s in heartbeat.states(conn, self.expected, stale_after_s=self.stale_after_s) if not s.up)
            items += [f"{down} service{'s' * (down != 1)} down"] if down else []
        if self.limits is not None and self._groq_state(conn, self.limits)[2]:
            items.append("Groq stopped")
        if self._llm_pause(conn) is not None:
            items.append("LLM paused")
        if self.spend_limits is not None and spend.load(conn, self.spend_limits).stopped:
            items.append("OpenRouter budget stopped")
        return f"⚠️ Needs attention: {' · '.join(items)}" if items else "✅ Nothing needs attention"

    def _paused(self, conn: sa.Connection) -> list[str]:
        items = [controls.SHORT.get(name, name) for name in pauses.switches_on(conn)]
        rows = conn.execute(
            sa.text(
                "SELECT source_key, COALESCE(pause_reason, 'manual') FROM sources "
                "WHERE paused_at IS NOT NULL ORDER BY 1"
            )
        ).all()
        items += [f"source {key} ({why})" for key, why in rows]
        return [f"Paused: {', '.join(items)}"] if items else []

    def pause_menu(self) -> tuple[str, list[list[tuple[str, str]]]]:
        """What can be paused right now, as buttons (each asks for a confirm before anything changes)."""
        with self.engine.connect() as conn:
            on = pauses.switches_on(conn)
            running = conn.execute(
                sa.text(
                    "SELECT source_key FROM sources WHERE enabled AND paused_at IS NULL AND clip_policy <> 'blocked' "
                    "ORDER BY source_key LIMIT 8"
                )
            ).scalars()
            rows = [[(f"⏸ Pause {controls.label(n)}", f"ps:{n}")] for n in controls.LABELS if n not in on]
            rows += [[(f"⏸ {key}", f"ps:src:{key}")] for key in running]
        if not rows:
            return "Everything is paused already. /resume brings things back.", []
        return "What do you want to pause? (You are asked to confirm.)", rows

    def resume_menu(self) -> tuple[str, list[list[tuple[str, str]]]]:
        """What is paused, as one-tap Resume buttons; a source paused after a claim is only explained."""
        with self.engine.connect() as conn:
            rows = [[(f"▶ Resume {controls.label(n)}", f"rs:{n}")] for n in pauses.switches_on(conn)]
            paused = conn.execute(
                sa.text(
                    "SELECT source_key, COALESCE(pause_reason, 'manual') FROM sources WHERE paused_at IS NOT NULL "
                    "ORDER BY source_key"
                )
            ).all()
        notes = []
        for key, why in paused:
            if why == "claim":
                notes.append(f"{key}: paused after a copyright claim; it cannot be resumed from here.")
            else:
                rows.append([(f"▶ Resume {key}", f"rs:src:{key}")])
        if not rows and not notes:
            return "Nothing is paused.", []
        return "\n".join(["What do you want to resume?", *notes]), rows

    def _services(self, conn: sa.Connection) -> list[str]:
        if not self.expected:
            return []
        found = heartbeat.states(conn, self.expected, stale_after_s=self.stale_after_s)
        down = [s.name for s in found if not s.up]
        line = f"Services: {len(found) - len(down)} of {len(found)} up"
        if len(down) > SERVICES_LISTED:
            return [f"{line} ({len(down)} not up, see /health)"]
        return [line + (f" (not up: {', '.join(down)})" if down else "")]

    def health(self) -> str:
        if not self.expected:
            return "Health checks are not set up (workers.yaml: health.expected)."
        with self.engine.connect() as conn:
            found = heartbeat.states(conn, self.expected, stale_after_s=self.stale_after_s)
        lines = ["Services:"]
        for s in found:
            if s.up is None:
                lines.append(f"  {s.name}: never seen")
            elif s.up:
                lines.append(f"  {s.name}: up ({s.age_s:.0f} s ago)")
            else:
                lines.append(f"  {s.name}: DOWN (no sign of life for {(s.age_s or 0) / 60:.0f} min)")
        return "\n".join(lines)

    def _recording(self, conn: sa.Connection) -> list[str]:
        """Who is being recorded, trusting only a recorder that has checked in lately (a stream row can outlive it)."""
        rows = conn.execute(
            sa.text(
                "SELECT s.source_key, COALESCE(l.lease_heartbeat_at > clock_timestamp() - interval '2 minutes', false) "
                "FROM live_streams l JOIN sources s ON s.id = l.source_id WHERE l.status = 'live' ORDER BY s.source_key"
            )
        ).all()
        recording = sorted({r[0] for r in rows if r[1]})
        stale: dict[str, int] = {}
        for name, held in rows:
            if not held:
                stale[name] = stale.get(name, 0) + 1
        out = ["Recording now: " + (", ".join(recording) or "nobody")]
        if stale:
            out.append(
                "Live but not being recorded: "
                + ", ".join(f"{k} ({n} stream{'s' * (n != 1)})" for k, n in sorted(stale.items()))
            )
        return out

    def _jobs(self, conn: sa.Connection) -> list[str]:
        rows = conn.execute(
            sa.text(
                "SELECT stage, status, count(*), count(*) FILTER (WHERE retry_at IS NOT NULL) "
                "FROM jobs WHERE status <> 'done' GROUP BY stage, status"
            )
        ).all()
        by_stage: dict[str, dict[str, int]] = {}
        for stage, status, n, with_retry in rows:
            counts = by_stage.setdefault(stage, {})
            if status == "failed" and with_retry:
                counts["waiting to retry"] = counts.get("waiting to retry", 0) + int(n)
            else:
                counts[status] = counts.get(status, 0) + int(n)
        review = by_stage.get("review", {}).pop("waiting", 0)
        out = []
        for stage in pipeline.STAGES:
            counts = by_stage.get(stage, {})
            order = ["queued", "running", "waiting", "waiting to retry", "failed", "dead"]
            parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
            if parts:
                out.append(f"  {stage}: " + ", ".join(parts))
        head = ["Jobs:"] + out if out else ["Nothing is queued."]
        return [*head, f"Waiting for you: {review} clip(s) to review"]

    def _groq(self, conn: sa.Connection) -> str:
        if self.limits is None:
            return "Groq audio: not counted here"
        used, share, stopped = self._groq_state(conn, self.limits)
        state = f"STOPPED (it waits above {self.limits.stop_at:.0%})" if stopped else "running"
        return (
            f"Groq audio: {share:.0%} of the daily limit ({used:,.0f} of {self.limits.audio_seconds_per_day:,} s); "
            f"transcription {state}"
        )

    def _openrouter(self, conn: sa.Connection) -> list[str]:
        if self.spend_limits is None:
            return []
        b = spend.load(conn, self.spend_limits)
        return [f"OpenRouter: ${float(b.spent_usd):.2f} of ${b.cap_usd:.2f} ({b.share:.0%})"]

    def budget_text(self) -> str:
        """The month's budget; the cap is changed by sending `/budget 25` (it asks before it changes anything)."""
        if self.spend_limits is None:
            return "The budget is not set up (workers.yaml: budget.openrouter)."
        with self.engine.connect() as conn:
            b = spend.load(conn, self.spend_limits)
            plan = (
                planner.plan(conn, self.planner_cfg, self.limits, self.spend_limits, None)
                if self.planner_cfg is not None and self.planner_cfg.enabled and self.limits is not None
                else None
            )
        state = (
            "STOPPED: jobs that need a paid model wait" if b.stopped else "ALERT: close to the stop" if b.alert_due
            else "running"
        )  # fmt: skip
        return "\n".join(
            [
                f"💰 OpenRouter budget ({b.month})",
                f"Cap: ${b.cap_usd:.2f}",
                f"Spent: ${float(b.spent_usd):.2f} ({b.share:.0%}), "
                f"stops at ${b.stop_usd:.2f} ({b.limits.stop_at:.0%})",
                f"Left per day: ${b.daily_allowance:.2f} for {b.days_left} day{'s' * (b.days_left != 1)}",
                f"Status: {state}",
                *self._plan_lines(plan),
                "To change the cap send /budget 25 (any amount; I ask you to confirm).",
            ]
        )

    @staticmethod
    def _plan_lines(plan: planner.Plan | None) -> list[str]:
        if plan is None:
            return []
        lines = [
            f"Audio plan: Groq allows {plan.groq_cap_s / 3600:.1f} h a day, shared by {plan.sources_active} active "
            f"source{'s' * (plan.sources_active != 1)} ({plan.fair_s / 3600:.1f} h each)"
        ]
        if plan.money_s_left is not None:
            lines.append(
                f"The budget pays for about {plan.money_s_left / 3600:.1f} more hours today "
                f"(${plan.cost_per_hour_usd:.3f} per hour of audio)"
            )
        return lines

    @staticmethod
    def _groq_state(conn: sa.Connection, limits: budget.Limits) -> tuple[float, float, bool]:
        """Seconds used today, their share of the daily limit, and whether transcription is stopped."""
        used = budget.used(conn, budget.DAY_S)
        share = used / limits.audio_seconds_per_day
        return used, share, limits.stop_at < 1 and share >= limits.stop_at

    @staticmethod
    def _llm_pause(conn: sa.Connection) -> Any:
        return conn.execute(
            sa.text(
                "SELECT reason, EXTRACT(EPOCH FROM until - clock_timestamp()) FROM pauses "
                "WHERE key LIKE 'llm:%' AND until > clock_timestamp() ORDER BY until DESC LIMIT 1"
            )
        ).first()

    def _llm(self, conn: sa.Connection) -> str:
        paused = self._llm_pause(conn)
        if paused is None:
            return "LLM: ok"
        return f"LLM: paused for about {float(paused[1]) / 60:.0f} min ({paused[0]})"

    def _waiting_to_post(self, conn: sa.Connection) -> list[str]:
        queued = conn.execute(
            sa.text("SELECT id, spec FROM jobs WHERE stage = 'publish' AND status = 'queued' LIMIT 500")
        ).all()
        if not queued:
            return []
        done = {
            (r[0], r[1])
            for r in conn.execute(
                sa.text("SELECT job_id, account_id FROM posts WHERE job_id = ANY(:ids) AND state = ANY(:final)"),
                {"ids": [r[0] for r in queued], "final": list(FINAL)},
            )
        }
        waiting: dict[str, int] = {}
        for job_id, spec in queued:
            for account in cs.Profile.model_validate(spec).upload_accounts():
                if (job_id, account) not in done:
                    waiting[account] = waiting.get(account, 0) + 1
        out = ["Waiting to post:"] if waiting else []
        for account, n in sorted(waiting.items()):
            out.append(f"  {account}: {n} waiting ({self._why_not_yet(conn, account)})")
        return out

    def _why_not_yet(self, conn: sa.Connection, account: str) -> str:
        rules = self.accounts.by_id.get(account)
        if rules is None:
            return "no such account in accounts.yaml"
        rules = rules_mod.apply(conn, rules)
        reason = schedule.check(
            now=self.now(),
            posts=posts.recent_times(conn, account),
            cap=rules.daily_cap,
            spacing_min=rules.min_spacing_min,
            windows=rules.post_windows,
            tz=self.accounts.timezone,
        )
        return reason or "ready: goes on the next sweep"

    # ---------- /posts -----------------------------------------------------------------------------------------------
    def posts(self) -> str:
        tz = self.accounts.timezone
        now = self.now().astimezone(tz)
        lines = [f"Posts in the last {schedule.DAY_H} hours ({tz}):"]
        with self.engine.connect() as conn:
            for account in self.accounts.by_id.values():
                rules = rules_mod.apply(conn, account)
                mine = [t.astimezone(tz) for t in posts.recent_times(conn, rules.id)]
                mine = [t for t in mine if now - timedelta(hours=schedule.DAY_H) < t <= now]
                last = f", last {max(mine):%H:%M}" if mine else ""
                lines.append(f"  {rules.id} {len(mine)}/{rules.daily_cap}{last}")
        return "\n".join(lines)

    # ---------- /rules -----------------------------------------------------------------------------------------------
    def rules_text(self) -> str:
        """The rules in force and how to change them (a change asks for a Yes tap first)."""
        if not self.accounts.by_id:
            return "There are no accounts in accounts.yaml."
        first = next(iter(self.accounts.by_id.values()))
        with self.engine.connect() as conn:
            now = rules_mod.apply(conn, first)
            set_by = rules_mod.overrides(conn)
            clips = rules_mod.clips_per_day(conn, self.clips_default)
            made = rules_mod.clips_made(conn)

        def who(name: str) -> str:
            return f" (set by {set_by[name][1]})" if name in set_by else " (from config)"

        window = ", ".join(now.post_windows) if now.post_windows else "any time"
        clip_line = f"up to {clips} in {schedule.DAY_H} h" if clips else "no limit"
        return "\n".join(
            [
                "⚙️ Posting rules",
                f"Cap: {now.daily_cap} posts per account in {schedule.DAY_H} h{who('cap')}",
                f"Spacing: {now.min_spacing_min} min between posts of an account{who('spacing')}",
                f"Window: {window} ({self.accounts.timezone}){who('window')}",
                f"Clips made: {clip_line}, {made} so far{who('clips')}",
                "Change: /cap 6  /spacing 120  /window 10:00-23:00 (or /window any)  /clips 6 (0: no limit).",
                "I ask you to confirm before anything changes.",
            ]
        )

    # ---------- /dead ------------------------------------------------------------------------------------------------
    def dead(self, limit: int = 5) -> tuple[list[DeadJob], int]:
        with self.engine.connect() as conn:
            total = int(conn.execute(sa.text("SELECT count(*) FROM jobs WHERE status = 'dead'")).scalar_one())
            rows = conn.execute(
                sa.text(
                    "SELECT id, stage, kind, COALESCE(last_error, '') FROM jobs WHERE status = 'dead' "
                    "ORDER BY updated_at DESC LIMIT :n"
                ),
                {"n": limit},
            ).all()
        return [DeadJob(r[0], r[1], r[2], " ".join(str(r[3]).split())[:ERROR_CHARS]) for r in rows], total

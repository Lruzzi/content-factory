"""The hours planner: the day's audio is shared fairly between the sources that are active, and what is left of the
OpenRouter budget caps it. A source over its fair share waits only while another source that is below its share has
work waiting, so nothing is held back when nobody needs it."""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

import config_schema as cs
from common import jobs, ledger, sources, spend
from services.understand import budget, planner

pytestmark = pytest.mark.integration

GROQ = budget.Limits(audio_seconds_per_hour=7200, audio_seconds_per_day=28800, alert_at=0.8, stop_at=0.9)
CAP_S = 28800 * 0.9  # what the stop leaves: 25,920 s a day
CFG = planner.Config(enabled=True, assumed_cost_per_audio_hour_usd=0.03, min_measured_hours=2)
SPEND = spend.Limits(cap_ceiling_usd=100.0, cap_usd=10.0, alert_at=0.8, stop_at=0.9)


@pytest.fixture
def world(db):
    with db.begin() as conn:
        sources.seed_from_yaml(conn, cs.ROOT)
        profiles, _ = sources.load_profiles(conn, cs.ROOT)
        ids = {
            key: conn.execute(sa.text("SELECT id FROM sources WHERE source_key = :k"), {"k": key}).scalar_one()
            for key in ("leokabuki", "jejeadriel20")
        }
        by_key = {p.source.id: p for p in profiles}
    return ids, by_key


def job(db, world, key, *, status="queued", stage="understand"):
    ids, profiles = world
    with db.begin() as conn:
        job_id = jobs.create_job(conn, profiles[key], stage=stage, source_id=ids[key], media_key=f"segments/{key}/1.ts")
        conn.execute(sa.text("UPDATE jobs SET status = :s WHERE id = :i"), {"s": status, "i": job_id})
    return job_id


def sent(db, world, key, seconds, *, ago_h=0.0):
    """`seconds` of audio that went to Groq for this source, `ago_h` hours ago (the job is finished)."""
    job_id = job(db, world, key, status="done", stage="select")
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="understand", provider="groq", units=Decimal(seconds), unit="audio_s",
            usd=Decimal(0), job_id=job_id,
        )  # fmt: skip
        conn.execute(
            sa.text("UPDATE costs SET created_at = clock_timestamp() - make_interval(secs => :s) WHERE job_id = :j"),
            {"s": ago_h * 3600, "j": job_id},
        )


def check(db, world, key, needed=600.0, spend_limits=None, cfg=CFG):
    with db.connect() as conn:
        return planner.check(conn, cfg, GROQ, spend_limits, world[0][key], needed)


def paid(db, dollars, *, ago_days=0.0):
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="select", provider="openai_compatible", units=Decimal(1),
            unit="input_tokens", usd=Decimal(str(dollars)),
        )  # fmt: skip
        conn.execute(
            sa.text("UPDATE costs SET created_at = clock_timestamp() - make_interval(secs => :s) WHERE provider = :p"),
            {"s": ago_days * 86400, "p": "openai_compatible"},
        )


# ---------- sharing the audio between the sources that are active ----------
def test_a_lone_source_is_never_held_to_a_share(db, world):
    sent(db, world, "leokabuki", 24000)
    assert check(db, world, "leokabuki", 600) is None


def test_a_source_over_its_share_waits_while_another_below_its_share_has_work_waiting(db, world):
    sent(db, world, "leokabuki", 14000)  # fair share is 25,920 / 2 = 12,960
    job(db, world, "jejeadriel20")  # Jeje has a segment waiting and has used nothing
    wait = check(db, world, "leokabuki")
    assert wait is not None and 60 <= wait.wait_s <= 900 and "share" in wait.reason.lower()
    assert check(db, world, "jejeadriel20") is None  # the one below its share goes ahead


def test_a_source_over_its_share_carries_on_when_nobody_else_is_waiting(db, world):
    sent(db, world, "leokabuki", 14000)
    sent(db, world, "jejeadriel20", 100)  # active in the last day, but nothing is waiting
    assert check(db, world, "leokabuki") is None


def test_it_does_not_wait_for_a_source_that_is_also_over_its_share(db, world):
    sent(db, world, "leokabuki", 14000)
    sent(db, world, "jejeadriel20", 13500)
    job(db, world, "jejeadriel20")
    assert check(db, world, "leokabuki") is None


def test_a_source_that_streamed_only_yesterday_does_not_shrink_todays_shares(db, world):
    sent(db, world, "jejeadriel20", 9000, ago_h=30)  # more than 24 hours ago: not active now
    sent(db, world, "leokabuki", 20000)
    assert check(db, world, "leokabuki") is None  # alone, so the whole day is its share


def test_the_segment_that_would_cross_the_share_is_the_one_that_waits(db, world):
    sent(db, world, "leokabuki", 12500)
    job(db, world, "jejeadriel20")
    assert check(db, world, "leokabuki", needed=300) is None  # 12,800 is under 12,960
    assert check(db, world, "leokabuki", needed=600) is not None  # 13,100 is over


def test_a_paused_planner_never_waits(db, world):
    sent(db, world, "leokabuki", 20000)
    job(db, world, "jejeadriel20")
    off = planner.Config(enabled=False, assumed_cost_per_audio_hour_usd=0.03, min_measured_hours=2)
    assert check(db, world, "leokabuki", cfg=off) is None


# ---------- the money limit ----------
def test_what_is_left_of_todays_budget_caps_the_audio_by_the_cost_of_an_hour(db, world):
    paid(db, 9.0)  # of a 10 dollar cap: stops at 9, so nothing is left
    wait = check(db, world, "leokabuki", spend_limits=SPEND)
    assert wait is not None and "budget" in wait.reason.lower() and wait.wait_s >= 600


def test_with_room_in_the_budget_the_money_does_not_hold_anything_back(db, world):
    paid(db, 0.01)  # a cent: whatever day of the month it is, today's share is far above that
    assert check(db, world, "leokabuki", needed=1800, spend_limits=SPEND) is None


def test_the_money_limit_is_in_audio_seconds_at_the_assumed_cost_per_hour(db, world):
    plan = plan_for(db, world, spend_limits=SPEND)
    # nothing spent: 9 dollars over the days left, at 0.03 dollars an hour
    expected = (9.0 / plan.days_left) / 0.03 * 3600
    assert plan.money_s_left == pytest.approx(expected, rel=1e-6)


def plan_for(db, world, spend_limits=None, key="leokabuki"):
    with db.connect() as conn:
        return planner.plan(conn, CFG, GROQ, spend_limits, world[0][key])


def test_without_a_budget_there_is_no_money_limit(db, world):
    assert plan_for(db, world).money_s_left is None


# ---------- what an hour of audio costs ----------
def test_until_enough_audio_has_been_measured_the_assumed_cost_is_used(db, world):
    sent(db, world, "leokabuki", 3600)  # one hour: under the two the cost needs
    paid(db, 0.5)
    with db.connect() as conn:
        assert planner.cost_per_audio_hour(conn, CFG) == 0.03


def test_with_enough_audio_the_cost_is_what_the_ledger_shows(db, world):
    sent(db, world, "leokabuki", 3 * 3600)  # three hours in the last week
    paid(db, 0.6, ago_days=2)  # 0.60 over 3 hours
    with db.connect() as conn:
        assert planner.cost_per_audio_hour(conn, CFG) == pytest.approx(0.2)


def test_free_models_make_the_measured_cost_tiny_but_never_zero(db, world):
    sent(db, world, "leokabuki", 3 * 3600)
    with db.connect() as conn:
        assert 0 < planner.cost_per_audio_hour(conn, CFG) <= 0.001


# ---------- what the plan says ----------
def test_the_plan_names_the_active_sources_and_each_ones_share(db, world):
    sent(db, world, "leokabuki", 100)
    job(db, world, "jejeadriel20")
    plan = plan_for(db, world)
    assert plan.sources_active == 2 and plan.fair_s == pytest.approx(CAP_S / 2) and plan.groq_cap_s == CAP_S


def test_the_source_asking_counts_as_active_even_before_it_has_anything(db, world):
    assert plan_for(db, world).sources_active == 1


# ---------- the config ----------
def test_the_config_comes_from_the_planner_section_of_workers_yaml():
    cfg = planner.config_from(
        {"planner": {"enabled": True, "assumed_cost_per_audio_hour_usd": 0.05, "min_measured_hours": 3}}
    )
    assert cfg == planner.Config(True, 0.05, 3)
    assert planner.config_from({}) is None


def test_the_repos_workers_yaml_has_a_planner_that_is_on():
    import yaml

    cfg = planner.config_from(yaml.safe_load((cs.ROOT / "workers.yaml").read_text()))
    assert cfg is not None and cfg.enabled and cfg.assumed_cost_per_audio_hour_usd > 0


@pytest.mark.parametrize(
    "bad",
    [{"enabled": "yes"}, {"assumed_cost_per_audio_hour_usd": 0}, {"min_measured_hours": -1}, {"extra": 1}],
)
def test_a_wrong_planner_section_is_refused(tmp_path, bad):
    from profile_factory import workers_file

    path = workers_file(tmp_path, lambda w: w["planner"].update(bad))
    with pytest.raises(ValueError, match="planner"):
        cs.validate_workers(path)


def test_the_cost_of_an_hour_counts_audio_sent_to_any_provider(db, world):
    """Overflow seconds go to OpenRouter, not Groq: dividing paid dollars by Groq's hours alone overstates it."""
    sent(db, world, "leokabuki", 2 * 3600)  # two hours to Groq
    with db.begin() as conn:
        ledger.record_cost(
            conn, profile="mlbb", service="understand", provider="openrouter", units=Decimal(2 * 3600), unit="audio_s",
            usd=Decimal("0.5"),
        )  # fmt: skip
    with db.connect() as conn:
        assert planner.cost_per_audio_hour(conn, CFG) == pytest.approx(0.5 / 4)

"""Approval switch (FR-8) and job recovery settings (FR-18, NFR-11)."""

from __future__ import annotations

import pytest

import config_schema as cs
from profile_factory import build, build_all, workers_file

SHORT_JOB = ["download", "understand", "select", "render", "package", "publisher"]
HOSTS = ["laptop", "mini_pc"]


# ---------- FR-8: your approval is a switch -----------------------------------------
def test_approval_is_required_by_default(tmp_path):
    assert build(tmp_path).needs_approval() is True


def test_approval_can_be_turned_off(tmp_path):
    assert build(tmp_path, review={"required": False}).needs_approval() is False


@pytest.mark.parametrize("bad", ["no", 0, None])
def test_approval_switch_must_be_a_bool(tmp_path, bad):
    with pytest.raises(ValueError, match=r"review\.required"):
        build(tmp_path, review={"required": bad})


def test_one_source_can_turn_approval_off_alone(tmp_path):
    sources = [
        {"url": "https://www.youtube.com/@one"},
        {"url": "https://www.youtube.com/@two", "overrides": {"review": {"required": False}}},
    ]
    one, two = build_all(tmp_path, sources=sources)
    assert (one.needs_approval(), two.needs_approval()) == (True, False)


def test_repo_profiles_keep_approval_on():
    for f in sorted((cs.ROOT / "profiles").glob("[!_]*.yaml")):
        assert all(p.needs_approval() for p in cs.load_profile(f)), f.name


# ---------- FR-18: stuck messages are reclaimed ----------------------------------------
@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("service", SHORT_JOB)
def test_short_job_services_need_a_visibility_timeout(tmp_path, host, service):
    path = workers_file(tmp_path, lambda w: w["hosts"][host]["services"][service].pop("visibility_timeout_s", None))
    with pytest.raises(ValueError, match="visibility_timeout_s"):
        cs.validate_workers(path)


@pytest.mark.parametrize("bad", [0, -5, "soon", True])
def test_visibility_timeout_must_be_a_positive_number(tmp_path, bad):
    path = workers_file(tmp_path, lambda w: w["hosts"]["laptop"]["services"]["render"].update(visibility_timeout_s=bad))
    with pytest.raises(ValueError, match="visibility_timeout_s"):
        cs.validate_workers(path)


# ---------- FR-18 / NFR-11: long-running services hold a lease ---------------------------
@pytest.mark.parametrize("key", ["lease", "reconciler"])
def test_lease_and_reconciler_settings_are_required(tmp_path, key):
    path = workers_file(tmp_path, lambda w: w.pop(key))
    with pytest.raises(ValueError, match=key):
        cs.validate_workers(path)


def test_lease_must_outlive_two_heartbeats(tmp_path):
    path = workers_file(tmp_path, lambda w: w["lease"].update(heartbeat_s=15, expires_s=20))
    with pytest.raises(ValueError, match="lease"):
        cs.validate_workers(path)


@pytest.mark.parametrize("key", ["every_s", "stalled_after_min", "running_after_min"])
def test_reconciler_values_must_be_positive(tmp_path, key):
    path = workers_file(tmp_path, lambda w: w["reconciler"].update({key: 0}))
    with pytest.raises(ValueError, match="reconciler"):
        cs.validate_workers(path)


def test_lease_expiry_plus_sweep_must_fit_nfr_11(tmp_path):
    # a dead recorder is noticed after expires_s, then picked up by the next sweep
    path = workers_file(tmp_path, lambda w: (w["lease"].update(expires_s=100), w["reconciler"].update(every_s=30)))
    with pytest.raises(ValueError, match="NFR-11"):
        cs.validate_workers(path)


def test_repo_recovery_settings_are_valid():
    cs.validate_workers(cs.ROOT / "workers.yaml")


def test_a_job_is_not_called_lost_before_its_own_service_would_give_up_on_it(tmp_path):
    # publisher.visibility_timeout_s is 1800 s: a "running" job younger than that may still be working
    path = workers_file(tmp_path, lambda w: w["reconciler"].update(running_after_min=10))
    with pytest.raises(ValueError, match="running_after_min"):
        cs.validate_workers(path)


# ---------- retention: disk is cleaned up, never left to grow -------------------------------------
def test_the_retention_block_is_required(tmp_path):
    path = workers_file(tmp_path, lambda w: w.pop("retention"))
    with pytest.raises(ValueError, match="retention"):
        cs.validate_workers(path)


@pytest.mark.parametrize("key", ["every_min", "failed_keep_days", "approved_max_hold_days"])
def test_retention_times_must_be_positive(tmp_path, key):
    path = workers_file(tmp_path, lambda w: w["retention"].update({key: 0}))
    with pytest.raises(ValueError, match=key):
        cs.validate_workers(path)


def test_the_grace_period_after_publishing_may_be_zero_but_not_negative(tmp_path):
    cs.validate_workers(workers_file(tmp_path, lambda w: w["retention"].update(published_keep_h=0)))
    with pytest.raises(ValueError, match="published_keep_h"):
        cs.validate_workers(workers_file(tmp_path, lambda w: w["retention"].update(published_keep_h=-1)))

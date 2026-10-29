"""Config rules for transcript-first selection (FR-1, FR-4, FR-5, FR-16, FR-17)."""

from __future__ import annotations

import pytest

import config_schema as cs
from profile_factory import build, workers_file

FULL = {"transcript": {"mode": "full"}}


# ---------- FR-16: selection mode ---------------------------------------------
def test_selection_mode_defaults_to_signals(tmp_path):
    assert build(tmp_path).selection.mode == "signals"


def test_unknown_selection_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="mode"):
        build(tmp_path, selection={"mode": "magic"})


@pytest.mark.parametrize("mode", ["hybrid", "transcript"])
def test_transcript_modes_need_full_transcription(tmp_path, mode):
    with pytest.raises(ValueError, match=r"signals\.transcript\.mode"):
        build(tmp_path, selection={"mode": mode})


@pytest.mark.parametrize("mode", ["hybrid", "transcript"])
def test_transcript_modes_need_transcript_enabled(tmp_path, mode):
    with pytest.raises(ValueError, match=r"signals\.transcript\.enabled"):
        build(tmp_path, selection={"mode": mode}, signals={"transcript": {"enabled": False, "mode": "full"}})


@pytest.mark.parametrize("mode", ["hybrid", "transcript"])
def test_transcript_modes_need_the_llm_judge(tmp_path, mode):
    with pytest.raises(ValueError, match="llm_judge"):
        build(tmp_path, selection={"mode": mode, "llm_judge": {"enabled": False}}, signals=FULL)


@pytest.mark.parametrize("mode", ["hybrid", "transcript"])
def test_transcript_modes_accept_full_transcription(tmp_path, mode):
    assert build(tmp_path, selection={"mode": mode}, signals=FULL).selection.mode == mode


def test_llm_judge_model_is_pinned_in_defaults(tmp_path):
    assert "latest" not in build(tmp_path).selection.llm_judge.model


# ---------- FR-16: kinds ----------------------------------------------------------
def test_kind_can_override_clip_length(tmp_path):
    p = build(tmp_path, selection={"kinds": {"tutorial": {"clip_length_s": [30, 75]}}})
    assert p.selection.kinds["tutorial"].clip_length_s == (30, 75)


def test_kind_without_length_inherits_selection_length(tmp_path):
    p = build(tmp_path, selection={"kinds": {"highlight": {}}})
    assert p.selection.kinds["highlight"].clip_length_s is None


def test_unknown_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="gossip"):
        build(tmp_path, selection={"kinds": {"gossip": {}}})


@pytest.mark.parametrize("bad", [[75, 30], [2, 30], [30, 400]])
def test_kind_clip_length_is_range_checked(tmp_path, bad):
    with pytest.raises(ValueError, match="clip_length_s"):
        build(tmp_path, selection={"kinds": {"tutorial": {"clip_length_s": bad}}})


# ---------- FR-15: segments are held while a job needs them ---------------------------
def test_hold_ceiling_defaults_above_the_rolling_buffer(tmp_path):
    live = build(tmp_path).live
    assert live.max_hold_h == 12
    assert live.max_hold_h >= live.max_buffer_h


def test_hold_ceiling_must_cover_the_buffer(tmp_path):
    with pytest.raises(ValueError, match="max_hold_h"):
        build(tmp_path, live={"max_buffer_h": 6, "max_hold_h": 3})


# ---------- FR-17: Groq budget -------------------------------------------------------
def test_repo_workers_file_is_valid():
    cs.validate_workers(cs.ROOT / "workers.yaml")


@pytest.mark.parametrize("bad", [0, 1.5, -0.1])
def test_groq_alert_threshold_must_be_a_fraction(tmp_path, bad):
    path = workers_file(tmp_path, lambda w: w["rate_limits"]["groq"].update(alert_at=bad))
    with pytest.raises(ValueError, match="alert_at"):
        cs.validate_workers(path)


def test_groq_alert_threshold_is_required(tmp_path):
    path = workers_file(tmp_path, lambda w: w["rate_limits"]["groq"].pop("alert_at"))
    with pytest.raises(ValueError, match="alert_at"):
        cs.validate_workers(path)


def test_groq_stop_threshold_is_between_the_alert_and_the_limit(tmp_path):
    for bad in (0.7, 1.2, True):
        path = workers_file(tmp_path, lambda w, bad=bad: w["rate_limits"]["groq"].update(stop_at=bad))
        with pytest.raises(ValueError, match="stop_at"):
            cs.validate_workers(path)
    cs.validate_workers(workers_file(tmp_path, lambda w: w["rate_limits"]["groq"].update(stop_at=0.9)))
    cs.validate_workers(workers_file(tmp_path, lambda w: w["rate_limits"]["groq"].pop("stop_at", None)))  # optional


def test_the_repo_stops_transcription_at_ninety_percent():
    import yaml

    groq = yaml.safe_load((cs.ROOT / "workers.yaml").read_text())["rate_limits"]["groq"]
    assert groq["stop_at"] == 0.9 and groq["alert_at"] < groq["stop_at"]


def test_groq_limits_are_required(tmp_path):
    path = workers_file(tmp_path, lambda w: w["rate_limits"].pop("groq"))
    with pytest.raises(ValueError, match="groq"):
        cs.validate_workers(path)


# ---------- FR-1: the repo's own config ---------------------------------------------------
def repo_profiles(name):
    return cs.load_profile(cs.ROOT / "profiles" / f"{name}.yaml")


def test_all_repo_profiles_validate():
    for f in sorted((cs.ROOT / "profiles").glob("[!_]*.yaml")):
        cs.validate_accounts(cs.load_profile(f), cs.ROOT / "accounts.yaml")


def test_mlbb_uses_hybrid_selection_with_full_transcripts():
    for p in repo_profiles("mlbb"):
        assert p.selection.mode == "hybrid"
        assert p.signals.transcript.mode == "full"


def test_mlbb_tutorials_may_run_longer_than_highlights():
    p = repo_profiles("mlbb")[0]
    assert p.selection.kinds["tutorial"].clip_length_s[1] > p.selection.clip_length_s[1]


def test_mlbb_runs_the_three_chosen_creators_and_no_more():
    # Groq's free plan (~8 h of audio a day) is shared by their streams, see PRODUCT.md assumptions
    assert {p.source.id for p in repo_profiles("mlbb") if p.source.enabled} == {
        "leokabuki",
        "jejeadriel20",
        "rineeeloyyyeh",
    }


def test_mlbb_keeps_the_streams_sixty_frames_and_a_finer_encode():
    out = repo_profiles("mlbb")[0].output
    assert (out["fps"], out["crf"], out["preset"]) == (60, 18, "medium")


def test_a_render_may_run_as_long_as_the_slower_encode_needs_but_not_past_its_message_timeout():
    import yaml

    from services.render import ffmpeg as ff

    workers = yaml.safe_load((cs.ROOT / "workers.yaml").read_text())
    for host in workers["hosts"].values():
        assert ff.DEFAULT_RENDER_TIMEOUT_S < host["services"]["render"]["visibility_timeout_s"]
    assert ff.DEFAULT_RENDER_TIMEOUT_S >= 780  # a 5-minute recap at 60 fps, medium preset: about 8 minutes


def test_the_health_check_expects_exactly_the_services_compose_runs():
    import yaml

    workers = yaml.safe_load((cs.ROOT / "workers.yaml").read_text())
    compose = yaml.safe_load((cs.ROOT.parent / "docker-compose.yml").read_text())
    running = set(compose["services"]) - {"postgres", "redis", "migrate", "replay"}  # databases, one-shots, tools
    health = workers["health"]
    assert set(health["expected"]) == running
    assert health["stale_after_s"] >= 60 and health["digest_every_h"] > 0


@pytest.mark.parametrize(
    "bad", [{"stale_after_s": 5}, {"digest_every_h": 0}, {"expected": "select"}, {"expected": []}, {"extra": 1}]
)
def test_a_wrong_health_section_is_refused(tmp_path, bad):
    path = workers_file(tmp_path, lambda w: w["health"].update(bad))
    with pytest.raises(ValueError, match="health"):
        cs.validate_workers(path)


# ---------- live.min_quality: never record below it ----------------------------------------------------------
def test_the_default_minimum_is_720p_and_mlbb_records_from_720p_up_to_1080p(tmp_path):
    assert build(tmp_path).live.min_quality == "720p"
    mlbb = repo_profiles("mlbb")[0].live
    assert (mlbb.min_quality, mlbb.quality) == ("720p", "1080p")


def test_the_minimum_may_not_be_above_the_quality_asked_for(tmp_path):
    with pytest.raises(ValueError, match="min_quality"):
        build(tmp_path, live={"quality": "720p", "min_quality": "1080p"})
    assert build(tmp_path, live={"quality": "480p", "min_quality": "480p"}).live.min_quality == "480p"


@pytest.mark.parametrize("bad", ["360p", "4k", "", None, 720])
def test_an_unknown_minimum_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="min_quality"):
        build(tmp_path, live={"min_quality": bad})


# ---------- the transcription fallback ----------
def test_the_default_fallback_is_openrouters_whisper_and_names_its_model(tmp_path):
    t = build(tmp_path).signals.transcript
    assert (t.fallback_engine, t.fallback_model) == ("openrouter", "openai/whisper-large-v3-turbo")


def test_the_fallback_engine_may_be_switched_off_and_unknown_ones_are_refused(tmp_path):
    assert (
        build(tmp_path, signals={"transcript": {"fallback_engine": "none"}}).signals.transcript.fallback_engine
        == "none"
    )
    with pytest.raises(ValueError, match="fallback_engine"):
        build(tmp_path, signals={"transcript": {"fallback_engine": "magic"}})

"""Sources owned by the database, the VOD transcript mode and Hermes's edit limits (FR-1, FR-19)."""

from __future__ import annotations

import pytest
import yaml

import config_schema as cs
from profile_factory import build, write_profile

DB_ROWS = [{"url": "https://www.youtube.com/@fromdb", "name": "From DB"}]


# ---------- FR-1: the database owns sources, YAML only seeds them -------------------------
def test_database_sources_replace_the_yaml_seed(tmp_path):
    path = write_profile(tmp_path)  # the YAML seed lists @someone
    assert [p.source.id for p in cs.load_profile(path, sources=DB_ROWS)] == ["fromdb"]


def test_profile_yaml_needs_no_sources_when_the_database_has_them(tmp_path):
    path = write_profile(tmp_path, sources=[])
    assert len(cs.load_profile(path, sources=DB_ROWS)) == 1


def test_an_empty_database_gives_no_profiles(tmp_path):
    assert cs.load_profile(write_profile(tmp_path), sources=[]) == []


def test_yaml_without_sources_and_no_database_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no sources"):
        cs.load_profile(write_profile(tmp_path, sources=[]))


def test_database_row_with_a_bad_link_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported source url"):
        cs.load_profile(write_profile(tmp_path), sources=[{"url": "https://example.com/x"}])


def test_database_rows_get_the_same_profile_checks(tmp_path):
    path = write_profile(tmp_path, layout={"type": "split"})
    with pytest.raises(ValueError, match=r"layout\.cam"):
        cs.load_profile(path, sources=DB_ROWS)
    row = {**DB_ROWS[0], "overrides": {"layout": {"cam": {"x": 0, "y": 0, "w": 400, "h": 300}}}}
    assert len(cs.load_profile(path, sources=[row])) == 1


def test_two_database_rows_cannot_share_an_id(tmp_path):
    rows = [{"url": "https://www.youtube.com/@same"}, {"url": "https://www.youtube.com/@same/streams"}]
    with pytest.raises(ValueError, match="duplicate source"):
        cs.load_profile(write_profile(tmp_path), sources=rows)


def test_a_database_row_can_override_profile_settings(tmp_path):
    row = {**DB_ROWS[0], "overrides": {"selection": {"top_k": 9}}}
    (profile,) = cs.load_profile(write_profile(tmp_path), sources=[row])
    assert profile.selection.top_k == 9


# ---------- live segments and downloaded videos transcribe differently ------------------------
def test_downloaded_videos_default_to_candidates_only(tmp_path):
    assert build(tmp_path).signals.transcript.vod_mode == "candidates_only"


def test_unknown_vod_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="vod_mode"):
        build(tmp_path, signals={"transcript": {"vod_mode": "always"}})


def test_full_vod_transcription_conflicts_with_section_downloads(tmp_path):
    with pytest.raises(ValueError, match="sections_only"):
        build(tmp_path, signals={"transcript": {"vod_mode": "full"}})


def test_full_vod_transcription_is_allowed_when_whole_videos_are_downloaded(tmp_path):
    p = build(tmp_path, signals={"transcript": {"vod_mode": "full"}}, download={"sections_only": False})
    assert p.signals.transcript.vod_mode == "full"


def test_mlbb_transcribes_live_in_full_but_videos_by_candidates():
    (first, *_) = cs.load_profile(cs.ROOT / "profiles" / "mlbb.yaml")
    assert first.signals.transcript.mode == "full"
    assert first.signals.transcript.vod_mode == "candidates_only"


# ---------- FR-19: what Hermes may change ----------------------------------------------------
def hermes_file(tmp_path, edit):
    h = yaml.safe_load((cs.ROOT / "hermes.yaml").read_text())
    edit(h)
    path = tmp_path / "hermes.yaml"
    path.write_text(yaml.safe_dump(h))
    return path


def test_repo_hermes_file_is_valid():
    cs.validate_hermes(cs.ROOT / "hermes.yaml")


@pytest.mark.parametrize("key", ["mode", "review", "targets", "test_targets"])
def test_hermes_can_never_touch_approval_or_publish_keys(tmp_path, key):
    path = hermes_file(tmp_path, lambda h: h["deny_keys"].remove(key))
    with pytest.raises(ValueError, match=key):
        cs.validate_hermes(path)


@pytest.mark.parametrize("pattern", ["*.yaml", "profiles/*.yaml", "accounts.yaml", "workers.yaml", "hermes.yaml", "**"])
def test_hermes_cannot_edit_protected_files(tmp_path, pattern):
    path = hermes_file(tmp_path, lambda h: h["edit_files"].append(pattern))
    with pytest.raises(ValueError, match="protected"):
        cs.validate_hermes(path)


@pytest.mark.parametrize("action", ["delete", "unblock", "drop"])
def test_hermes_source_actions_are_a_known_list(tmp_path, action):
    path = hermes_file(tmp_path, lambda h: h["source_actions"].append(action))
    with pytest.raises(ValueError, match="source_actions"):
        cs.validate_hermes(path)


@pytest.mark.parametrize("bad", [0, -1, "some"])
def test_hermes_keeps_backups_of_what_it_overwrites(tmp_path, bad):
    path = hermes_file(tmp_path, lambda h: h.update(keep_backups=bad))
    with pytest.raises(ValueError, match="keep_backups"):
        cs.validate_hermes(path)

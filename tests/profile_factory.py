"""Helpers that build merged profiles and workers files for config tests."""

from __future__ import annotations

import yaml

import config_schema as cs


def write_profile(tmp_path, **sections):
    """Write _defaults.yaml and a profile built from the given sections; return the profile path."""
    profiles = tmp_path / "profiles"
    profiles.mkdir(exist_ok=True)
    (profiles / "_defaults.yaml").write_text((cs.ROOT / "profiles" / "_defaults.yaml").read_text())
    raw = {
        "name": "t",
        "niche": "gaming",
        "mode": "rnd",
        "test_targets": [{"account": "yt_test"}],
        "sources": [{"url": "https://www.youtube.com/@someone"}],
    }
    path = profiles / "t.yaml"
    path.write_text(yaml.safe_dump(cs.deep_merge(raw, sections)))
    return path


def build_all(tmp_path, **sections):
    """Load every source's merged profile from _defaults.yaml plus the given sections."""
    return cs.load_profile(write_profile(tmp_path, **sections))


def build(tmp_path, **sections):
    return build_all(tmp_path, **sections)[0]


def workers_file(tmp_path, edit):
    """Copy workers.yaml, apply `edit` to the parsed dict, and return the new path."""
    w = yaml.safe_load((cs.ROOT / "workers.yaml").read_text())
    edit(w)
    path = tmp_path / "workers.yaml"
    path.write_text(yaml.safe_dump(w))
    return path

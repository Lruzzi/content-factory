"""Scale-readiness seams that exist in config today (NFR-12, FR-18)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import config_schema as cs
from profile_factory import build, workers_file

SCHEMA = Path(cs.__file__).resolve()


def run_validator(config_dir):
    env = {**os.environ, "CONFIG_DIR": str(config_dir)}
    return subprocess.run([sys.executable, "-B", str(SCHEMA)], env=env, capture_output=True, text=True, timeout=60)


def copy_config(tmp_path):
    dest = tmp_path / "cfg"
    shutil.copytree(cs.ROOT, dest, ignore=shutil.ignore_patterns("__pycache__", "*Zone.Identifier"))
    return dest


# ---------- config can live outside the repo ------------------------------------------
def test_validator_reads_config_from_config_dir(tmp_path):
    result = run_validator(copy_config(tmp_path))
    assert result.returncode == 0, result.stdout
    assert "OK  workers.yaml" in result.stdout


def test_config_dir_really_is_the_one_read(tmp_path):
    cfg = copy_config(tmp_path)
    path = cfg / "workers.yaml"
    w = yaml.safe_load(path.read_text())
    w.pop("lease")
    path.write_text(yaml.safe_dump(w))
    # the repo's own config is valid, so a failure proves the copy was read
    result = run_validator(cfg)
    assert result.returncode == 1
    assert "ERR workers.yaml" in result.stdout


# ---------- chat retention (raw chat grows fast) ------------------------------------------
def test_raw_chat_retention_defaults_to_two_weeks(tmp_path):
    assert build(tmp_path).live.chat_retention_days == 14


@pytest.mark.parametrize("bad", [0, 400])
def test_raw_chat_retention_is_bounded(tmp_path, bad):
    with pytest.raises(ValueError, match="chat_retention_days"):
        build(tmp_path, live={"chat_retention_days": bad})


# ---------- Redis streams are trimmed ---------------------------------------------------------
@pytest.mark.parametrize("key", ["stream_maxlen", "dead_letter_maxlen"])
def test_stream_trim_limits_are_required(tmp_path, key):
    path = workers_file(tmp_path, lambda w: w["broker"].pop(key, None))
    with pytest.raises(ValueError, match=key):
        cs.validate_workers(path)


@pytest.mark.parametrize("key", ["stream_maxlen", "dead_letter_maxlen"])
@pytest.mark.parametrize("bad", [0, -1, "many", True])
def test_stream_trim_limits_must_be_positive_numbers(tmp_path, key, bad):
    path = workers_file(tmp_path, lambda w: w["broker"].update({key: bad}))
    with pytest.raises(ValueError, match=key):
        cs.validate_workers(path)

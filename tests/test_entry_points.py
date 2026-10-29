"""Every command and service can be started: its module imports in a fresh interpreter, as it would from the shell.
pytest adds config/ to the import path for itself, which once hid a start-up failure (config_schema imported before
common had put config/ on the path), so this runs each module in its own process."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted(
    [".".join(p.relative_to(ROOT).with_suffix("").parts) for p in (ROOT / "services").glob("*/__main__.py")]
    + [".".join(p.relative_to(ROOT).with_suffix("").parts) for p in (ROOT / "services").glob("*/*_auth.py")]
    + ["services.publisher.meta_tokens", "common.llm"]
)


@pytest.mark.parametrize("module", MODULES)
def test_the_module_imports_when_started_on_its_own(module):
    done = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", f"import importlib; importlib.import_module({module!r})"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=False, env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-500:]

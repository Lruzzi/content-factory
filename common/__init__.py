"""Shared library for every service: streams, database, leases, ledger, media store, sources, LLM calls.

`config/` is a folder of YAML plus `config_schema.py`, not a package. Importing `common` makes that module importable
from anywhere (a service, `python -m common.llm`, a test), so nothing depends on how the process was started.
"""

import sys
from pathlib import Path

_CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")
if _CONFIG_DIR not in sys.path:
    sys.path.append(_CONFIG_DIR)

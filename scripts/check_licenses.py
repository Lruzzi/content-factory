"""Fail CI when a dependency's license forbids commercial use (CLAUDE.md rule, NFR-8).

Reads `pip-licenses --from=mixed --format=json` on stdin. GPL, AGPL, SSPL, BUSL and non-commercial
licenses fail. LGPL, MPL, EPL and unknown licenses print a warning and pass: read them before using one.
Packages named in scripts/license_allowlist.txt (one per line, # for comments) are skipped.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

FAIL = re.compile(
    r"\bA?GPL|GNU (Affero )?General Public License|\bSSPL|\bBUSL|business source|non[- ]?commercial|\bCC[- ]BY[- ]NC",
    re.IGNORECASE,
)
WARN = re.compile(
    r"\bLGPL|Lesser General Public|Library General Public|\bMPL|Mozilla|\bEPL|Eclipse|\bCDDL", re.IGNORECASE
)
RANK = {"ok": 0, "warn": 1, "fail": 2}
ALLOWLIST = Path(__file__).with_name("license_allowlist.txt")

Package = dict[str, str]


def classify_one(text: str) -> str:
    if not text.strip() or text.strip().upper() == "UNKNOWN":
        return "warn"
    if FAIL.search(text):
        return "fail"
    return "warn" if WARN.search(text) else "ok"


def _all_of(text: str) -> str:
    parts = [p for p in re.split(r"\s+AND\s+|;", text) if p.strip()] or [text]
    return max((classify_one(p) for p in parts), key=RANK.__getitem__)


def classify(text: str) -> str:
    """ok, warn or fail. "A OR B": you pick one, so the best choice counts. "A AND B" and ";" mean all apply."""
    return min((_all_of(choice) for choice in re.split(r"\s+OR\s+", text)), key=RANK.__getitem__)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check(packages: Iterable[Package], allow: Iterable[str] = ()) -> tuple[list[Package], list[Package]]:
    allowed = {normalize(a) for a in allow}
    fails: list[Package] = []
    warns: list[Package] = []
    for package in packages:
        if normalize(package["Name"]) in allowed:
            continue
        verdict = classify(package.get("License", ""))
        if verdict == "fail":
            fails.append(package)
        elif verdict == "warn":
            warns.append(package)
    return fails, warns


def load_allowlist(path: Path = ALLOWLIST) -> list[str]:
    if not path.exists():
        return []
    lines = (line.split("#")[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def main(raw: str, allow: Iterable[str] = ()) -> int:
    fails, warns = check(json.loads(raw), allow)
    for package in warns:
        print(f"WARN {package['Name']} {package['Version']}: {package.get('License') or 'unknown'}")
    for package in fails:
        print(f"FAIL {package['Name']} {package['Version']}: {package['License']}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read(), load_allowlist()))

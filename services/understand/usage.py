"""How much of Groq's free limit has been used: `make groq-usage`. Counts the audio seconds in the cost ledger, which is
an estimate (see budget.py); the Groq console has the official figures. Needs DATABASE_URL."""

from __future__ import annotations

import yaml

from common.db import make_engine
from config_schema import ROOT
from services.understand import budget
from services.understand.__main__ import groq_limits


def main() -> None:
    limits = groq_limits(yaml.safe_load((ROOT / "workers.yaml").read_text()))
    with make_engine().connect() as conn:
        print(budget.report(conn, limits))
        print(f"  you are told at {limits.alert_at:.0%} of the daily limit")


if __name__ == "__main__":
    main()

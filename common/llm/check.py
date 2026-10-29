"""Try the LLMs a profile is set up to use: one tiny call to each provider in its chain, so a new key, model or
provider is checked before any job needs it.

    make llm-check PROFILE=mlbb          (reads the keys from the environment: the Makefile loads .env first)

Prints one line per provider (the primary, then each backup): the model, tokens used and what the call cost, or why it
failed. Exits 1 if any provider failed. Never prints a key.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, ConfigDict

import config_schema as cs
from common.llm import cost
from common.llm.base import LLMError, complete_json
from common.llm.factory import _single


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


def _try(label: str, spec: cs.LLMSpec, environ: Mapping[str, str], prices: dict[str, cs.Price]) -> tuple[bool, str]:
    try:
        client = _single(spec, environ)
        _, results = complete_json(
            client,
            Ping,
            system="You answer with JSON only.",
            prompt='Reply with exactly this JSON and nothing else: {"ok": true}',
            max_tokens=spec.max_output_tokens,
        )
    except LLMError as exc:
        return False, f"FAILED {label:<10} {spec.provider} {spec.model}: {' '.join(str(exc).split())}"
    tokens_in, tokens_out = sum(r.input_tokens for r in results), sum(r.output_tokens for r in results)
    try:
        price = cost.price_for(spec, prices)
        money = f"${sum(sum(cost.usd(r, price)) for r in results):.4f}"
    except LLMError as exc:
        money = f"unknown ({exc})"
    return True, f"OK {label:<10} {spec.provider} {spec.model}  tokens in/out {tokens_in}/{tokens_out}  cost {money}"


def main(argv: list[str], environ: Mapping[str, str] = os.environ, out: TextIO = sys.stdout) -> int:
    if len(argv) != 1:
        print("usage: python -m common.llm <profile>   (for example: mlbb)", file=out)
        return 2
    path = cs.ROOT / "profiles" / f"{argv[0]}.yaml"
    if argv[0].startswith("_") or "/" in argv[0] or not path.is_file():
        print(f"no such profile: {argv[0]!r} (looked for {path})", file=out)
        return 2
    selection = cs.load_profile(path)[0].selection
    prices = cs.load_llm_prices(Path(cs.ROOT) / "llm_prices.yaml")
    chains = [("", selection.llm_judge), *((f"{task} ", chain) for task, chain in sorted(selection.llm_tasks.items()))]
    failed = False
    for prefix, judge in chains:
        chain = [("primary", judge), *((f"fallback {i}", spec) for i, spec in enumerate(judge.fallbacks, start=1))]
        for label, spec in chain:
            ok, line = _try(prefix + label, spec, environ, prices)
            print(line, file=out)
            failed = failed or not ok
    return 1 if failed else 0

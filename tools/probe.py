"""Pull a real log through the API and show what the bot would post. No Discord needed.

    python -m tools.probe <warcraftlogs report link> [more links...]

Reads WCL_CLIENT_ID / WCL_CLIENT_SECRET from .env. For each log it:
  1. prints each player's night average under every rankings option WCL offers, side by side,
     so they can be compared with the report page to pick the one that matches the site;
  2. prints the recap exactly as the bot would post it (with nobody linked);
  3. saves the raw JSON to tools/probe-out/ for building test fixtures.
"""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from bot.config import RecapSettings
from bot.recap import build_recap
from bot.render import render
from bot.wcl import WCLClient, find_report_links

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "probe-out"

VARIANTS = {
    "API default": RecapSettings(),
    "Parses/Today": RecapSettings(compare="Parses", timeframe="Today"),
    "Parses/Hist": RecapSettings(compare="Parses", timeframe="Historical"),
    "Rankings/Today": RecapSettings(compare="Rankings", timeframe="Today"),
    "Rankings/Hist": RecapSettings(compare="Rankings", timeframe="Historical"),
}


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def probe(url: str) -> None:
    refs = find_report_links(url)
    if not refs:
        print(f"Not a report link: {url}")
        return
    ref = refs[0]
    wcl = WCLClient()
    try:
        settings = RecapSettings.from_env()
        averages: dict[str, dict[str, float]] = {}
        roles: dict[str, str] = {}
        default_report = None
        for label, variant in VARIANTS.items():
            variant = replace(settings, compare=variant.compare, timeframe=variant.timeframe)
            report = await wcl.report_full(ref, variant)
            default_report = default_report or report
            out = OUT / ref.code
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{label.replace('/', '-').replace(' ', '_')}.json").write_text(json.dumps(report, indent=2))
            # Everyone, not just the callouts: compare every row with the site.
            recap = build_recap(report, replace(variant, parse_high=-1))
            for p in recap.high:
                averages.setdefault(p.char.label, {})[label] = p.average
                roles[p.char.label] = p.role

        print(f"\n=== {ref.url}")
        print(f"visibility={default_report.get('visibility')} segments={default_report.get('exportedSegments')}"
              f"/{default_report.get('segments')} zone={(default_report.get('zone') or {}).get('name')}\n")
        header = f"{'Character':<28}{'role':<8}" + "".join(f"{label:>16}" for label in VARIANTS)
        print(header)
        print("-" * len(header))
        for char in sorted(averages, key=lambda c: -averages[c].get("API default", 0)):
            row = f"{char:<28}{roles[char]:<8}"
            row += "".join(f"{averages[char].get(label, float('nan')):>16.1f}" for label in VARIANTS)
            print(row)

        recap = build_recap(default_report, settings)
        text, _ = render(recap, ref.url, settings, lambda c: None)
        print("\n--- The bot would post (nobody linked yet):\n")
        print(text)
        print(f"\nRaw JSON saved to {OUT / ref.code}")
    finally:
        await wcl.close()


async def main(urls: list[str]) -> None:
    for url in urls:
        await probe(url)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    load_env()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(sys.argv[1:]))

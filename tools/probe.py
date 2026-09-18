"""Pull real logs through the API and print the report the bot would post. No Discord needed.

    python -m tools.probe <warcraftlogs link> [more links...] [--compare]

Reads WCL_CLIENT_ID / WCL_CLIENT_SECRET (and optionally TIMEZONE) from .env. With several links,
the nights are replayed oldest first into a throwaway history, so later reports show "last raid's
best" and "N raids running" the way the bot would. Nobody is linked, so names print in bold.

--compare also prints each player's night average under both WCL parse comparisons (Rankings and
Parses), for checking against the report page. Raw JSON is saved to tools/probe-out/.
"""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from bot.awards import boss_results, build_lines, winners_by_key
from bot.config import RecapSettings, _zone
from bot.recap import analyze
from bot.render import card_text, render_report
from bot.store import Store
from bot.wcl import WCLClient, find_report_links

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "probe-out"


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def compare(wcl: WCLClient, ref, settings: RecapSettings) -> None:
    averages: dict[str, dict[str, float]] = {}
    for label in ("Rankings", "Parses"):
        report = await wcl.report_full(ref, replace(settings, compare=label))
        for p in analyze(report, settings).parses:
            averages.setdefault(p.char.label, {})[label] = p.average
    print(f"\n{'Character':<28}{'Rankings':>10}{'Parses':>10}")
    for char, row in sorted(averages.items(), key=lambda kv: -kv[1].get("Rankings", 0)):
        print(f"{char:<28}{row.get('Rankings', float('nan')):>10.1f}{row.get('Parses', float('nan')):>10.1f}")


async def main(urls: list[str], show_compare: bool) -> None:
    settings = RecapSettings.from_env()
    tz = _zone("TIMEZONE")
    wcl = WCLClient()
    history = Store(":memory:")
    try:
        nights = []
        for url in urls:
            refs = find_report_links(url)
            if not refs:
                print(f"Not a report link: {url}")
                continue
            ref = refs[0]
            report = await wcl.report_full(ref, settings)
            out = OUT / ref.code
            out.mkdir(parents=True, exist_ok=True)
            (out / "full.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            nights.append((ref, analyze(report, settings)))

        for ref, night in sorted(nights, key=lambda rn: rn[1].start_ms):
            lines = build_lines(night, settings, history)
            rendered = render_report(night, lines, ref.url, lambda c: None, tz)
            history.save_history(ref, night.start_ms, boss_results(night), winners_by_key(lines))

            print(f"\n{'=' * 100}\n{ref.url}  (thread: {rendered.thread_title})\n{'=' * 100}")
            print(card_text(rendered.headline))
            for card in rendered.thread:
                print(f"\n----- thread card ({len(card_text(card))} chars) -----")
                print(card_text(card))
            if show_compare:
                await compare(wcl, ref, settings)
        print(f"\nRaw JSON saved under {OUT}")
    finally:
        await wcl.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--compare"]
    if not args:
        sys.exit(__doc__)
    load_env()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(args, "--compare" in sys.argv))

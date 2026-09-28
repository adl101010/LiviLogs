"""Proof of concept: sim each raider's gear, then compare it with what they actually did.

    python -m tools.simcheck <warcraftlogs link> [--min-alive 95] [--iterations 3000]

For every boss the raid killed, it picks the DPS who were alive for at least 95% of the fight
(a battle rez counts as alive again: a fast one keeps you over the line, a slow one doesn't),
builds a SimulationCraft profile from the gear they were wearing on that pull, and prints their
real DPS next to it.

The sim half needs a simc binary: set SIMC_PATH, or pass --simc <path>. Without one the profiles
are still written to tools/simcheck-out/<code>/ and the sim column stays empty, so they can be
pasted into localbots by hand.

Two things a WCL log doesn't carry, both of which move a sim:
  - race. Defaulted per faction; tools/sim-races.json ({"Charname": "orc"}) overrides.
  - talents. The log has node ids and ranks, not the loadout string simc wants, so profiles carry
    them as a comment. Add a `talents=` line before simming, or accept simc's spec default.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from bot.config import RecapSettings
from bot.recap import DPS, Char, Night, Pull, analyze
from bot.wcl import WCLClient, find_report_links
from tools import simtalents
from tools.probe import load_env

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "simcheck-out"
RACES = ROOT / "tools" / "sim-races.json"

LEVEL = 90  # Midnight's cap; a sim at the wrong level is wrong by miles
DEFAULT_RACE = {1: "human", 2: "orc", 0: "human"}  # WCL faction id -> a middling racial

# WoW spec id -> (simc class, simc spec). Only what a raid log can hold.
SPECS = {
    250: ("deathknight", "blood"), 251: ("deathknight", "frost"), 252: ("deathknight", "unholy"),
    577: ("demonhunter", "havoc"), 581: ("demonhunter", "vengeance"),
    102: ("druid", "balance"), 103: ("druid", "feral"), 104: ("druid", "guardian"),
    105: ("druid", "restoration"),
    1467: ("evoker", "devastation"), 1468: ("evoker", "preservation"), 1473: ("evoker", "augmentation"),
    253: ("hunter", "beast_mastery"), 254: ("hunter", "marksmanship"), 255: ("hunter", "survival"),
    62: ("mage", "arcane"), 63: ("mage", "fire"), 64: ("mage", "frost"),
    268: ("monk", "brewmaster"), 269: ("monk", "windwalker"), 270: ("monk", "mistweaver"),
    65: ("paladin", "holy"), 66: ("paladin", "protection"), 70: ("paladin", "retribution"),
    256: ("priest", "discipline"), 257: ("priest", "holy"), 258: ("priest", "shadow"),
    259: ("rogue", "assassination"), 260: ("rogue", "outlaw"), 261: ("rogue", "subtlety"),
    262: ("shaman", "elemental"), 263: ("shaman", "enhancement"), 264: ("shaman", "restoration"),
    265: ("warlock", "affliction"), 266: ("warlock", "demonology"), 267: ("warlock", "destruction"),
    71: ("warrior", "arms"), 72: ("warrior", "fury"), 73: ("warrior", "protection"),
}

# WCL's gear array is in slot order; simc names them. Shirt and tabard don't sim.
SLOTS = {0: "head", 1: "neck", 2: "shoulder", 4: "chest", 5: "waist", 6: "legs", 7: "feet",
         8: "wrist", 9: "hands", 10: "finger1", 11: "finger2", 12: "trinket1", 13: "trinket2",
         14: "back", 15: "main_hand", 16: "off_hand"}


# --- who counts -----------------------------------------------------------------------------

def alive_share(night: Night, rezzes: dict, pull: Pull, char: Char, actor_id: int | None) -> float:
    """How much of the fight they spent on their feet. A battle rez puts them back on them."""
    length = pull.end - pull.start
    if length <= 0:
        return 0.0
    dead, back = 0, -1
    for death in night.deaths.get(pull.id, []):
        if death.char != char or death.time < back:
            continue
        later = [t for t in rezzes.get((pull.id, actor_id), ()) if t > death.time]
        revived = min(later) if later else pull.end
        dead += max(0, revived - death.time)
        back = revived
    return max(0.0, (length - dead) / length)


def actual_dps(report: dict, pull_id: int) -> dict[str, float]:
    """Each DPS's real damage per second on that kill, straight out of the rankings."""
    for ranking in (report.get("dpsRankings") or {}).get("data") or []:
        if ranking.get("fightID") != pull_id:
            continue
        players = ((ranking.get("roles") or {}).get("dps") or {}).get("characters") or []
        return {p.get("name"): p.get("amount") or 0 for p in players}
    return {}


# --- the profile ----------------------------------------------------------------------------

def gear_lines(gear: list) -> list[str]:
    lines = []
    for index, item in enumerate(gear or []):
        slot = SLOTS.get(index)
        if not slot or not isinstance(item, dict) or not item.get("id"):
            continue
        parts = [f"{slot}=,id={item['id']}"]
        if item.get("bonusIDs"):
            parts.append("bonus_id=" + "/".join(str(b) for b in item["bonusIDs"]))
        if item.get("permanentEnchant"):
            parts.append(f"enchant_id={item['permanentEnchant']}")
        gems = [g.get("id") for g in item.get("gems") or [] if g.get("id")]
        if gems:
            parts.append("gem_id=" + "/".join(str(g) for g in gems))
        lines.append(",".join(parts))
    return lines


def profile(char: Char, info: dict, race: str, ilvl: int | None, traits: list | None) -> tuple[str, bool] | None:
    """A SimulationCraft profile for the gear they had on at that pull, and whether the talents in
    it are theirs. Without the trait table (or if the encode fails) they're left as a comment, and
    simc falls back to nothing, which is worth knowing before trusting the number."""
    spec = SPECS.get(info.get("specID"))
    if not spec:
        return None
    wow_class, spec_name = spec
    tree = info.get("talentTree") or []
    talents, why = None, "no trait table: pass --traits or --simc from a source build"
    if traits:
        try:
            talents = simtalents.talents_line(tree, info["specID"], traits)
        except simtalents.TalentError as err:
            why = str(err)
    lines = [
        f'{wow_class}="{char.name}"',
        f"level={LEVEL}",
        f"race={race}",
        f"spec={spec_name}",
    ]
    if talents:
        lines.append(f"talents={talents}")
    lines += ["", f"# Built from a Warcraft Logs pull{f' · item level {ilvl}' if ilvl else ''}."]
    if not talents:
        lines += [f"# No talents: {why}.",
                  "# What the log recorded, as node id:rank —",
                  "# " + " ".join(f"{t.get('nodeID')}:{t.get('rank')}" for t in tree)]
    lines += ["", *gear_lines(info.get("gear"))]
    return "\n".join(lines) + "\n", bool(talents)


# --- the sim --------------------------------------------------------------------------------

def run_simc(simc: str, path: Path, seconds: float, iterations: int) -> float | None:
    out = path.with_suffix(".json")
    command = [simc, str(path), f"iterations={iterations}", f"max_time={seconds:.0f}",
               "fight_style=Patchwerk", "single_actor_batch=1", f"json2={out}"]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=900)
        data = json.loads(out.read_text(encoding="utf-8"))
        return data["sim"]["players"][0]["collected_data"]["dps"]["mean"]
    except (subprocess.SubprocessError, OSError, KeyError, ValueError) as err:
        print(f"    simc failed for {path.name}: {err}", file=sys.stderr)
        return None


# --- putting it together --------------------------------------------------------------------

async def fetch(url: str) -> tuple[str, dict]:
    refs = find_report_links(url)
    if not refs:
        raise SystemExit(f"not a Warcraft Logs link: {url}")
    client = WCLClient()
    try:
        return refs[0].code, await client.report_full(refs[0], RecapSettings.from_env())
    finally:
        await client.close()


def trait_table(traits: str | None, simc: str | None) -> list | None:
    """The simc build's own trait table: named with --traits, or found in the source tree that
    sits next to the binary, the way livibots finds it."""
    path = Path(traits) if traits else None
    if path is None and simc:
        source = Path(simc).resolve().parent.parent
        path = source / "engine" / "dbc" / "generated" / "trait_data.inc"
    if path is None or not path.exists():
        if traits:
            print(f"no trait table at {path}", file=sys.stderr)
        return None
    try:
        return simtalents.load_traits(path)
    except (simtalents.TalentError, OSError) as err:
        print(f"couldn't read the trait table: {err}", file=sys.stderr)
        return None


def main(url: str, min_alive: float, iterations: int, simc: str | None, traits_path: str | None) -> None:
    load_env()
    code, report = asyncio.run(fetch(url))
    settings = RecapSettings.from_env()
    night = analyze(report, settings)

    actors = {a["id"]: a["name"] for a in (report.get("masterData") or {}).get("actors") or []}
    by_name = {name: actor for actor, name in actors.items()}
    rezzes: dict = defaultdict(list)
    for event in report.get("resurrects") or []:
        rezzes[(event.get("fight"), event.get("targetID"))].append(event.get("timestamp"))
    snapshots = {(e.get("fight"), e.get("sourceID")): e for e in report.get("combatantInfo") or []}
    races = json.loads(RACES.read_text(encoding="utf-8")) if RACES.exists() else {}
    traits = trait_table(traits_path, simc)
    print(f"talents: {'from the log, encoded against ' + str(len(traits)) + ' trait rows' if traits else 'OFF — no trait table, sims will use no talents at all'}")
    with_talents = 0

    out = OUT / code
    out.mkdir(parents=True, exist_ok=True)
    for pull in night.pulls:
        if not pull.kill:
            continue  # a wipe is people dying: almost nobody clears the gate, and the DPS means less
        real = actual_dps(report, pull.id)
        print(f"\n{pull.boss} · {int(pull.seconds // 60)}m{int(pull.seconds % 60):02d}s")
        print(f"    {'raider':16}{'alive':>7}{'actual':>10}{'sim':>10}{'of sim':>9}")
        rows = []
        for char in sorted(night.roster, key=lambda c: c.name.casefold()):
            if night.roles.get(char) != DPS or pull.id not in night.pulls_in.get(char, set()):
                continue
            actor = by_name.get(char.name)
            share = alive_share(night, rezzes, pull, char, actor)
            if share < min_alive:
                continue
            info = snapshots.get((pull.id, actor)) or {}
            built = profile(char, info, races.get(char.name) or DEFAULT_RACE.get(info.get("faction"), "human"),
                            max((i.get("itemLevel") or 0) for i in info.get("gear") or [{}]) or None, traits)
            if not built:
                continue
            text, talented = built
            with_talents += talented
            path = out / f"{pull.id}-{char.name}.simc"
            path.write_text(text, encoding="utf-8")
            simmed = run_simc(simc, path, pull.seconds, iterations) if simc else None
            rows.append((char.name, share, real.get(char.name), simmed))
        for name, share, mine, simmed in sorted(rows, key=lambda r: -(r[3] and r[2] / r[3] or 0)):
            ratio = f"{mine / simmed * 100:.0f}%" if simmed and mine else "-"
            print(f"    {name:16}{share * 100:>6.0f}%{(mine or 0) / 1000:>9.1f}k"
                  f"{(simmed or 0) / 1000:>9.1f}k{ratio:>9}")
        if not simc:
            print(f"    profiles written to {out} · set SIMC_PATH or pass --simc to fill the sim column")
    print(f"\n{with_talents} profiles carry the raider's own talents.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--min-alive", type=float, default=95, help="percent of the fight alive (default 95)")
    parser.add_argument("--iterations", type=int, default=3000)
    parser.add_argument("--simc", default=os.environ.get("SIMC_PATH"))
    parser.add_argument("--traits", default=os.environ.get("SIMC_TRAITS"),
                        help="trait_data.inc from the simc build (found next to --simc if omitted)")
    args = parser.parse_args()
    main(args.url, args.min_alive / 100, args.iterations, args.simc, args.traits)

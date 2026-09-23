"""The night -> the lines of the report: headline callouts, awards, and the night summary.

Every award checks whether tonight gives it something worth saying and stays silent otherwise, so
the report is as long as the night was eventful. A line is content, not layout: a title ("🍺 Potion
seller"), an optional note in small print, and a body of names and numbers. Bodies hold Chars
rather than names; the renderer turns those into Discord mentions and decides how it all looks.
"""

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .config import RecapSettings
from .gear import describe, gear_rows
from .recap import (
    DPS, FLASK_PREFIXES, FOOD_PATTERN, HEALER, TANK, VANTUS_PREFIX, Boss, Char, Night, ParseLine,
)

HEADLINE = "headline"
NIGHT = "night"
BOARD = "board"
HIGHLIGHTS = "highlights"
LOWLIGHTS = "lowlights"
DEATHS = "deaths"
CONSUMABLES = "consumables"
GEAR = "gear"

Part = str | Char


@dataclass
class Line:
    section: str
    parts: list[Part]  # the body: names and numbers
    key: str | None = None  # which award this is; its winners are saved with the night
    winners: list[Char] = field(default_factory=list)
    title: str | None = None  # "🍺 Potion seller"; None for plain lines
    note: str | None = None  # small print under the title, e.g. "Void-Touched rune"
    cluster: str = ""  # related lines share a block; the card puts a divider between blocks
    stacked: bool | None = None  # body under the title rather than beside it; None = decide by content
    chart: str | None = None  # the chart that shows the same thing ("parses", "consumables", "progress:2")

    @property
    def is_stacked(self) -> bool:
        if self.stacked is not None:
            return self.stacked
        return self.note is not None or len(self.winners) > 1


# --- formatting --------------------------------------------------------------------------------


def fmt_rate(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:.0f}"


def fmt_big(value: float) -> str:
    return f"{value / 1_000_000:.0f}M" if value >= 1_000_000 else fmt_rate(value)


def fmt_pct(pct: float) -> str:
    """Near a kill every tenth counts (1.5%, not 2%); further out, whole numbers."""
    return f"{pct:.1f}".rstrip("0").rstrip(".") if pct < 10 else f"{pct:.0f}"


def fmt_health(pct: float | None, phase: int | None) -> str:
    """How close a wipe got, the way raiders say it: "P3 at 5%"."""
    if pct is None:
        return "unknown"
    return f"P{phase} at {fmt_pct(pct)}%" if phase and phase > 1 else f"{fmt_pct(pct)}%"


def fmt_seconds(seconds: float) -> str:
    """Exact time, for time spent dead: "8m 47s", "45s", "1h 02m 15s"."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def fmt_duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def ability_name(name: str) -> str:
    return "boss melee" if name.lower() == "melee" else name


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def joined(chars: list[Char]) -> list[Part]:
    """[a] / [a, " and ", b] / [a, ", ", b, " and ", c]"""
    parts: list[Part] = []
    for i, c in enumerate(chars):
        if i:
            parts.append(" and " if i == len(chars) - 1 else ", ")
        parts.append(c)
    return parts


def leaders(counts: Counter, minimum: float, max_names: int = 3) -> tuple[list[Char], float]:
    """Everyone tied for first, if first is at least `minimum` and the tie isn't a crowd."""
    if not counts:
        return [], 0
    top = max(counts.values())
    names = sorted((c for c, v in counts.items() if v == top), key=lambda c: c.name.casefold())
    if top < minimum or len(names) > max_names:
        return [], 0
    return names, top


# WCL's parse colours, for the leaderboard.
def parse_colour(pct: float) -> str:
    if pct >= 100:
        return "🟨"
    if pct >= 99:
        return "🩷"
    if pct >= 95:
        return "🟧"
    if pct >= 75:
        return "🟪"
    if pct >= 50:
        return "🟦"
    if pct >= 25:
        return "🟩"
    return "⬜"


ROLE_EMOJI = {DPS: "⚔️", HEALER: "💚", TANK: "🛡️"}
ROLE_NAMES = {DPS: "Damage", HEALER: "Healing", TANK: "Tanks"}
PI_MIN = 3  # fewer than this to the top target isn't a pattern worth listing
PI_MAX_NAMES = 8
RAID_FOOD_MIN = 5  # this many raiders' food running out on one pull is one feast wearing off
IDLE_GAP = 0.10  # 10 points under the raid's usual active time
IDLE_MIN_RAIDERS = 5  # too few DPS and tanks to say what's usual
PHASE_BARS = "▁▂▃▄▅▆▇█"


class Builder:
    def __init__(self, night: Night, settings: RecapSettings):
        self.night = night
        self.settings = settings
        self.lines: list[Line] = []

    def add(self, section: str, parts: list[Part], key: str | None = None,
            winners: list[Char] | None = None, *, title: str | None = None, note: str | None = None,
            cluster: str = "", stacked: bool | None = None, chart: str | None = None) -> None:
        parts = [part for part in parts if part != ""]
        self.lines.append(Line(section, parts, key, winners or [], title, note, cluster, stacked, chart))

    # --- headline ------------------------------------------------------------------------------

    def headline(self) -> None:
        night = self.night
        unkilled = [b for b in night.bosses if not b.killed and b.wipes]
        prog_night = not night.kills
        if prog_night:  # on a prog night the best pull is the story, so it leads
            self.progress_lines(unkilled)

        if night.has_parses:
            for role, title, key in ((DPS, "🏆 Top DPS", "top_dps"), (HEALER, "💚 Top healer", "top_healer"),
                                     (TANK, "🛡️ Top tank", "top_tank")):
                lines = [p for p in night.parses if p.role == role]
                if lines:
                    best = max(p.average for p in lines)
                    top = [p.char for p in lines if p.average == best]
                    self.top_line(title, key, top, f"{best:.1f}")
        else:
            for role, title, key, unit in ((DPS, "🏆 Top DPS", "top_dps", ""),
                                           (HEALER, "💚 Top healer", "top_healer", " HPS")):
                rates = [r for r in night.rates if r.role == role]
                if rates:
                    self.top_line(title, key, [rates[0].char], f"{fmt_rate(rates[0].per_second)}{unit}")
        if not prog_night:
            self.progress_lines(unkilled)

        if night.high:
            self.add(HEADLINE, self.parse_list(night.high, "high"), "high", [p.char for p in night.high],
                     title=f"🌟 {self.settings.parse_high:g}+", cluster="calls", stacked=False)
        if night.grey:
            self.add(HEADLINE, self.parse_list(night.grey, "grey"), "grey", [p.char for p in night.grey],
                     title="🗑️ Grey", cluster="calls", stacked=False)
        top = [d for d in night.floor if d.deaths == night.floor[0].deaths] if night.floor else []
        if top and top[0].deaths >= 2:
            each = " each" if len(top) > 1 else ""
            self.add(HEADLINE, [*joined([d.char for d in top]), f" {top[0].deaths} deaths{each}"],
                     "floor", [d.char for d in top], title="💀 Floor inspector", cluster="calls", stacked=False)

    def progress_lines(self, unkilled: list[Boss]) -> None:
        prog_night = not self.night.kills
        for boss in unkilled:
            best = boss.best_wipe
            if prog_night and len(unkilled) == 1:
                final = " · the last pull of the night" if best is boss.pulls[-1] and len(boss.pulls) > 1 else ""
                self.add(HEADLINE, [f"{fmt_health(best.boss_pct, best.phase)}{final}"],
                         title="📈 Best pull", cluster="tops")
            else:
                best_text = f" · best {fmt_health(best.boss_pct, best.phase)}" if best else ""
                self.add(HEADLINE, [f"{plural(len(boss.wipes), 'wipe')}{best_text}"],
                         title=f"📈 {boss.name}", cluster="tops")

    def top_line(self, title: str, key: str, chars: list[Char], value: str) -> None:
        parts: list[Part] = [*joined(chars), f" {value}"]
        self.add(HEADLINE, parts, key, chars, title=title, cluster="tops", stacked=False)

    def parse_list(self, lines: list[ParseLine], key: str) -> list[Part]:
        parts: list[Part] = []
        for i, p in enumerate(lines):
            if i:
                parts.append(" · ")
            kind = "healing" if p.role == HEALER else "damage"
            parts += [p.char, f" {kind} {p.average:.1f}"]
        return parts

    # --- the night -----------------------------------------------------------------------------

    def the_night(self) -> None:
        night = self.night
        if night.bosses:
            entries = []
            for boss in night.bosses:
                if boss.killed:
                    count = "" if len(boss.pulls) == 1 else f" ({len(boss.pulls)} pulls)"
                    entries.append(f"✅ {boss.name}{count}")
                else:
                    entries.append(f"❌ {boss.name} ({plural(len(boss.wipes), 'wipe')})")
            time = f"{fmt_duration(night.boss_seconds)} on bosses across a {fmt_duration(night.span_seconds)} night"
            first_pull = "Bosses without a count died on the first pull · " if any(
                b.killed and len(b.pulls) == 1 for b in night.bosses) else ""
            self.add(NIGHT, [" · ".join(entries)], cluster="bosses")
            self.add(NIGHT, [f"-# {first_pull}{time}"], cluster="bosses")

        for index, boss in enumerate(night.bosses):
            facts = []
            if len(boss.pulls) >= 5 and all(p.boss_pct is not None for p in boss.pulls):
                facts.append("`" + "".join(PHASE_BARS[min(7, max(0, math.ceil(p.boss_pct / 12.5) - 1))]
                                           for p in boss.pulls) + "`")
            best = boss.best_wipe
            if not boss.killed and best:
                facts.append(f"best {fmt_health(best.boss_pct, best.phase)}")
            phases = [p.phase for p in boss.wipes if p.phase]
            if not boss.killed and len(boss.wipes) >= 3 and phases and max(phases) > 1:
                top = max(phases)
                facts.append(f"reached P{top} on {phases.count(top)} of {len(boss.wipes)} pulls")
            if facts and (not boss.killed or len(boss.pulls) >= 5):
                bar_note = "Boss health by pull: the shorter the bar, the closer to a kill" if facts[0].startswith("`") else None
                self.add(NIGHT, [" · ".join(facts)], title=f"📉 {boss.name} progress", note=bar_note,
                         cluster="progress", stacked=True, chart=progress_chart_key(boss, index))

        close = [(b, b.best_wipe) for b in night.bosses if b.killed and b.best_wipe]
        close = [(b, w) for b, w in close if w.boss_pct is not None and w.boss_pct <= 10]
        if close:
            boss, wipe = min(close, key=lambda bw: bw[1].boss_pct)
            phase = f" in P{wipe.phase}" if wipe.phase and wipe.phase > 1 else ""
            self.add(NIGHT, [f"{boss.name} wiped with the boss at **{fmt_pct(wipe.boss_pct)}%**{phase} before going down"],
                     title="💔 Heartbreaker", cluster="facts", stacked=True)

        wiped = [b for b in night.bosses if len(b.wipes) >= 3]
        if len(night.bosses) >= 2 and wiped:
            wall = max(wiped, key=lambda b: len(b.wipes))
            self.add(NIGHT, [f"{wall.name} · {len(wall.wipes)} wipes"], title="🧱 Wall of the night",
                     cluster="facts", stacked=True)

        killers = Counter(d.ability for ds in night.counted.values() for d in ds if d.ability)
        if killers:
            name, n = killers.most_common(1)[0]
            if n >= 5:
                self.add(NIGHT, [f"{ability_name(name)} · {n} deaths"], title="☠️ Raid's nemesis",
                         cluster="facts", stacked=True)
        starters = Counter(ds[0].ability for ds in night.counted.values() if ds and ds[0].ability)
        if starters:
            name, n = starters.most_common(1)[0]
            if n >= 3:
                self.add(NIGHT, [f"{ability_name(name)} caused the first death on {n} pulls"],
                         title="🧨 Wipe starter", cluster="facts", stacked=True)

    # --- leaderboard ---------------------------------------------------------------------------

    def board(self) -> None:
        night = self.night
        total_pulls = len(night.pulls)
        for role in (DPS, HEALER, TANK):
            parts: list[Part] = []
            if night.has_parses:
                # The top parse on its own line, then one line per WCL colour band.
                entries = sorted((p for p in night.parses if p.role == role), key=lambda p: -p.average)
                colour = None
                for i, p in enumerate(entries):
                    now = parse_colour(p.average)
                    if i == 0:
                        parts += [f"{now} - ", p.char, f" **{p.average:.1f}** 👑"]
                    elif now != colour or i == 1:
                        parts += [f"\n{now} - ", p.char, f" {p.average:.1f}"]
                    else:
                        parts += [" · ", p.char, f" {p.average:.1f}"]
                    colour = now
            else:
                entries = [r for r in night.rates if r.role == role]
                for i, r in enumerate(entries):
                    missed = f" ({r.pulls} pulls)" if r.pulls < total_pulls else ""
                    if i == 0:
                        parts += ["👑 - ", r.char, f" **{fmt_rate(r.per_second)}**{missed}"]
                    else:
                        parts += ["\n" if i == 1 else " · ", r.char, f" {fmt_rate(r.per_second)}{missed}"]
            if entries:
                self.add(BOARD, parts, title=f"{ROLE_EMOJI[role]} {ROLE_NAMES[role]}", cluster=role, stacked=True,
                         chart="parses" if night.has_parses else None)

    # --- highlights ----------------------------------------------------------------------------

    def highlights(self) -> None:
        night = self.night
        pinks = [(p, [(pct, boss) for pct, boss in p.parses if pct >= 99]) for p in night.parses]
        pinks = [(p, hits) for p, hits in pinks if hits]
        if pinks:
            gold = any(pct >= 100 for _, hits in pinks for pct, _ in hits)
            parts: list[Part] = []
            for i, (p, hits) in enumerate(sorted(pinks, key=lambda ph: -len(ph[1]))):
                if i:
                    parts.append("\n")
                by_value: dict[int, list[str]] = {}
                for pct, boss in hits:
                    by_value.setdefault(int(pct), []).append(boss)
                text = ", ".join(f"{v} on {' and '.join(dict.fromkeys(bosses))}"
                                 for v, bosses in sorted(by_value.items(), reverse=True))
                parts += [p.char, f" · {text}"]
            self.add(HIGHLIGHTS, parts, "pink", [p.char for p, _ in pinks],
                     title="🟨 Gold parse" if gold else "🩷 Pink parse")

        steady = [p for p in night.parses if p.kills >= 4]
        spreads = [(max(v for v, _ in p.parses) - min(v for v, _ in p.parses), p) for p in steady]
        spreads = [(s, p) for s, p in spreads if s <= 15 and p.average >= 50]
        if spreads:
            s, p = min(spreads, key=lambda sp: (sp[0], -sp[1].average))
            lo, hi = min(v for v, _ in p.parses), max(v for v, _ in p.parses)
            self.add(HIGHLIGHTS, [p.char, f" · {lo:.0f} to {hi:.0f} on every boss"], "metronome", [p.char],
                     title="🎵 Metronome")

        self.counter_award(HIGHLIGHTS, "kick", "🦶 Kick captain", night.interrupts, 5, "interrupt", next_best=True)
        self.counter_award(HIGHLIGHTS, "dispel", "🧼 Dispel machine", night.dispels, 10, "dispel")
        self.counter_award(HIGHLIGHTS, "necromancer", "🪄 Necromancer", night.brez_given, 3, "battle rez", "battle rezzes")

        last: Counter = Counter()
        wipes_with_deaths = 0
        for pull in night.pulls:
            deaths = night.deaths.get(pull.id)
            if not pull.kill and deaths:
                wipes_with_deaths += 1
                last[deaths[-1].char] += 1
        self.power_infusion()

        chars, n = leaders(last, 3, max_names=1)
        if chars and wipes_with_deaths >= 3:
            self.add(HIGHLIGHTS, [chars[0], f" · last to die on {int(n)} of {wipes_with_deaths} wipes"],
                     "last_standing", chars,
                     title="🧍 Last one standing")

    def counter_award(self, section: str, key: str, title: str, counts: Counter, minimum: int,
                      noun: str, plural_noun: str | None = None, next_best: bool = False) -> None:
        chars, n = leaders(counts, minimum, max_names=2)
        if not chars:
            return
        word = noun if n == 1 else (plural_noun or f"{noun}s")
        tail = f" · {int(n)} {word}" + (" each" if len(chars) > 1 else "")
        if next_best:
            rest = sorted((v for c, v in counts.items() if c not in chars), reverse=True)
            if rest:
                tail += f" (next best: {int(rest[0])})"
        self.add(section, [*joined(chars), tail], key, chars, title=title)

    def power_infusion(self) -> None:
        """One line per priest: who they infused, most to least. Priests infusing themselves don't
        count. Two priests landing it on the same person at once is called out as wasted."""
        night = self.night
        if not night.power_infusion or max(night.power_infusion.values()) < PI_MIN:
            self.pi_overwritten()  # worth saying even when nobody was infused much
            return
        by_giver: dict[Char, Counter] = {}
        for (giver, receiver), given in night.power_infusion.items():
            by_giver.setdefault(giver, Counter())[receiver] += given
        parts: list[Part] = []
        winners: list[Char] = []
        for i, (giver, targets) in enumerate(sorted(by_giver.items(),
                                                    key=lambda gt: (-sum(gt[1].values()), gt[0].name.casefold()))):
            if i:
                parts.append("\n")
            ordered = sorted(targets.items(), key=lambda rn: (-rn[1], rn[0].name.casefold()))
            shown, rest = ordered[:PI_MAX_NAMES], ordered[PI_MAX_NAMES:]
            parts += [giver, " · "] if len(by_giver) > 1 else []
            for j, (receiver, given) in enumerate(shown):
                parts += [" · "] if j else []
                parts += [receiver, f" {int(given)}"]
                winners.append(receiver)
            if rest:
                parts.append(f" · +{len(rest)} more")
        note = "Who each priest infused, most to least" if len(by_giver) > 1 else \
            f"Who got it, most to least, all from {next(iter(by_giver)).name}"
        self.add(HIGHLIGHTS, parts, "pi", list(dict.fromkeys(winners)), title="💜 Power Infusion",
                 note=note, stacked=True, cluster="pi")
        self.pi_overwritten()

    def pi_overwritten(self) -> None:
        """Power Infusions replaced by another priest's. The buff doesn't stack, so the first one
        ends as the second lands and the rest of it is wasted."""
        gone = self.night.pi_overwritten
        if not gone:
            return
        parts: list[Part] = []
        for i, o in enumerate(gone):
            if i:
                parts.append("\n")
            parts += [o.receiver, " · ", o.cut, " → ", o.by, f" after {o.ran:.0f}s · {o.lost:.0f}s lost"]
        wasted = sum(o.lost for o in gone)
        self.add(HIGHLIGHTS, parts, "pi_overwritten", [o.receiver for o in gone],
                 title=f"🪫 Overwritten · {wasted:.0f}s of Power Infusion wasted",
                 note="One priest's Power Infusion replaced by another's on the same player",
                 cluster="pi", stacked=True)

    # --- lowlights -----------------------------------------------------------------------------

    def lowlights(self) -> None:
        night = self.night
        worst = [(pct, boss, p) for p in night.parses if p.role != TANK for pct, boss in p.parses]
        if worst:
            pct, boss, p = min(worst, key=lambda w: (w[0], w[2].char.name.casefold()))
            if pct <= 10:
                self.add(LOWLIGHTS, [p.char, f" · {pct:.0f} on {boss}"],
                         "shame", [p.char], title="🚽 Parse of shame")

        swings = []
        for p in night.parses:
            if p.kills >= 3:
                lo = min(p.parses)
                hi = max(p.parses)
                swings.append((hi[0] - lo[0], p, lo, hi))
        swings = [s for s in swings if s[0] >= 50]
        if swings:
            _, p, lo, hi = max(swings, key=lambda s: s[0])
            self.add(LOWLIGHTS, [p.char, f" · {lo[0]:.0f} on {lo[1]} but {hi[0]:.0f} on {hi[1]}"],
                     "rollercoaster", [p.char], title="🎢 Rollercoaster")

        taken = sorted(((v, c) for c, v in night.damage_taken.items() if night.roles.get(c) != TANK),
                       key=lambda vc: -vc[0])
        if taken:
            v, char = taken[0]
            self.add(LOWLIGHTS, [char, f" · {fmt_big(v)} damage taken"],
                     "sponge", [char], title="🧽 Damage sponge")

        damage_rates = {r.char: r.per_second for r in night.rates if r.role in (DPS, TANK)}
        tanks = [(damage_rates[c], c) for c, role in night.roles.items() if role == TANK and c in damage_rates]
        if tanks:
            best_tank, tank = max(tanks, key=lambda tc: tc[0])
            beaten = sorted((c for c, role in night.roles.items()
                             if role == DPS and c in damage_rates and damage_rates[c] < best_tank),
                            key=lambda c: damage_rates[c])
            if beaten:
                self.add(LOWLIGHTS, [*joined(beaten), " did less damage than ", tank], "outdamaged", beaten,
                         title="🛡️ Outdamaged by a tank")

        # Idle: DPS and tanks well under the raid's own usual active time. Compared with the raid
        # rather than a fixed bar, because movement-heavy prog wipes pull everyone down together.
        if len(night.active) >= IDLE_MIN_RAIDERS:
            usual = statistics.median(night.active.values())
            idle = sorted((c for c, share in night.active.items() if share <= usual - IDLE_GAP),
                          key=lambda c: (night.active[c], c.name.casefold()))[:3]
            if idle:
                parts = []
                for i, c in enumerate(idle):
                    if i:
                        parts.append(" · ")
                    parts += [c, f" {night.active[c] * 100:.0f}%"]
                self.add(LOWLIGHTS, parts, "idle", idle, title="💤 Idle",
                         note=f"Time alive in pulls spent dealing damage; the raid's usual is {usual * 100:.0f}%",
                         stacked=True)

    # --- deaths --------------------------------------------------------------------------------

    def deaths(self) -> None:
        night = self.night
        if night.floor:
            parts: list[Part] = []
            for i, d in enumerate(night.floor):
                if i:
                    parts.append(" · ")
                parts += [d.char, f" {d.deaths}"]
            if night.floor_tied_more:
                parts.append(f" · +{night.floor_tied_more} more tied at {night.floor[-1].deaths}")
            self.add(DEATHS, parts, title="💀 Floor inspector", note="Deaths before the wipe was called",
                     stacked=True)

        firsts = Counter(ds[0].char for ds in night.counted.values() if ds)
        chars, n = leaders(firsts, 3)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(DEATHS, [*joined(chars), f" · first to die {times(int(n))}{each}"], "canary", chars,
                     title="🐤 Canary")

        kill_ids = {p.id for p in night.pulls if p.kill}
        on_kills = Counter(d.char for pid in kill_ids for d in night.deaths.get(pid, []))
        chars, n = leaders(on_kills, 2)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(DEATHS, [*joined(chars), f" · {int(n)} deaths{each} on kills"], "loot", chars,
                     title="🎁 Couldn't wait for loot")

        # Every death counts here, not just those before the wipe call: it's a fun fact, not blame.
        pairs = Counter((d.char, d.ability) for ds in night.deaths.values() for d in ds if d.ability)
        ranked = pairs.most_common(2)
        if ranked and ranked[0][1] >= 3 and (len(ranked) == 1 or ranked[1][1] < ranked[0][1]):
            (char, name), n = ranked[0]
            self.add(DEATHS, [char, f" died to {ability_name(name)} {times(n)}"], "nemesis", [char], title="🎯 Nemesis")

        chars, n = leaders(night.brezzed, 3)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(DEATHS, [*joined(chars), f" · rezzed {times(int(n))}{each}"],
                     "brez_magnet", chars, title="🧲 Brez magnet")

        # Ghost: the most time spent dead, from each death until a battle rez or the end of the pull.
        chars, seconds = leaders(night.dead_seconds, 180, max_names=1)
        if chars:
            self.add(DEATHS, [chars[0], f" · spent {fmt_seconds(seconds)} dead"], "ghost", chars, title="👻 Ghost")

        early = Counter()
        starts = {p.id: p.start for p in night.pulls}
        for pid, ds in night.deaths.items():
            for d in ds:
                if pid in starts and d.time - starts[pid] < 30_000:
                    early[d.char] += 1
        chars, n = leaders(early, 2)
        if chars:
            self.add(DEATHS, [*joined(chars), f" · dead within 30 seconds of the pull {times(int(n))}"],
                     "speedrunner", chars, title="⏱️ Speedrunner")

    # --- consumables ---------------------------------------------------------------------------

    def ran_out(self) -> None:
        """Flasks and food that expired mid-pull, and weapon oil that ran out between pulls. When
        most of the raid's food goes at once (one feast wearing off), it's one line, not a list."""
        night = self.night
        entries: list[tuple[int, float, list[Part]]] = []  # (pull, seconds in, text) to sort by time
        winners: list[Char] = []
        food: dict[int, list] = {}
        for r in night.ran_out:
            if r.kind == "food":
                food.setdefault(r.pull, []).append(r)
        grouped = {pull for pull, gone in food.items() if len(gone) >= RAID_FOOD_MIN}
        for r in night.ran_out:
            if r.kind == "food" and r.pull in grouped:
                continue
            entries.append((r.pull, r.seconds_in, [r.char, f" {r.kind} · {pull_label(night, r.pull)}, "
                                                            f"{fmt_seconds(r.seconds_in)} in"]))
            winners.append(r.char)
        for pull in sorted(grouped):
            gone = food[pull]
            at = statistics.median(r.seconds_in for r in gone)
            entries.append((pull, at, [f"Raid food ran out for {len(gone)} raiders · {pull_label(night, pull)}, "
                                       f"about {fmt_seconds(round(at / 10) * 10)} in"]))
        oil = sorted((r for r in consumable_rows(night, self.settings) if r.oil_gone_pull and "no_oil" in r.flags),
                     key=lambda r: r.char.name.casefold())
        for r in oil:
            gone = pull_label(night, r.oil_gone_pull)
            without = plural(r.oil_pulls - r.oil, "pull")
            entries.append((r.oil_gone_pull, -1,
                            [r.char, f" weapon oil · none from {gone} onwards ({without})"]))
            winners.append(r.char)
        if not entries:
            return
        order = {p.id: i for i, p in enumerate(night.pulls)}
        parts: list[Part] = []
        for i, (_, _, text) in enumerate(sorted(entries, key=lambda e: (order.get(e[0], 0), e[1]))):
            if i:
                parts.append("\n")
            parts += text
        # A short tally, so a two-line list plainly ends rather than looking cut off.
        tally = []
        flasks = sum(1 for r in night.ran_out if r.kind == "flask")
        meals = sum(1 for r in night.ran_out if r.kind == "food")
        if flasks:
            tally.append(f"{plural(flasks, 'flask')} expired mid-pull")
        if meals:
            tally.append(f"{plural(meals, 'raider')} lost their food")
        if oil:
            tally.append(f"{plural(len(oil), 'raider')} lost their weapon oil")
        parts.append("\n-# That's everyone: " + " · ".join(tally))
        self.add(CONSUMABLES, parts, "ran_out", list(dict.fromkeys(winners)), title="⌛ Ran out mid-pull",
                 note="Flask or food that expired during a boss pull"
                      + (", and weapon oil between pulls" if oil else ""),
                 cluster="upkeep", stacked=True)

    # --- gear check ----------------------------------------------------------------------------

    def gear(self) -> None:
        rows = gear_rows(self.night, self.settings)
        if not rows:
            return
        ready = sum(1 for r in rows if r.ready)
        # Stands in for the chart when there is one (the chart shows this raider by raider).
        self.add(GEAR, [f"{ready} of {len(rows)} raiders fully enchanted"], cluster="chart", chart="gear")

        missing = sorted((r for r in rows if r.missing), key=lambda r: (-len(r.missing), r.char.name.casefold()))
        if missing:
            parts: list[Part] = []
            for i, r in enumerate(missing):
                if i:
                    parts.append(" · ")
                parts += [r.char, " " + ", ".join(describe(r.missing, r))]
            self.add(GEAR, parts, "no_enchant", [r.char for r in missing], title="🔧 Missing enchants",
                     note="Not enchanted on any pull: helm, shoulders, chest, legs, boots, rings, weapons",
                     cluster="calls", stacked=True)

        unwrapped = [r for r in rows if r.unwrapped]
        if unwrapped:
            parts = []
            for i, r in enumerate(unwrapped):
                if i:
                    parts.append(" · ")
                bits = []
                for check in r.unwrapped:
                    slot = describe([check], r)[0]
                    where = f"new {slot} from {check.boss}" if check.boss else slot
                    bits.append(f"{where}, bare for {plural(check.bare_pulls, 'pull')}")
                parts += [r.char, " " + "; ".join(bits)]
            self.add(GEAR, parts, "unwrapped", [r.char for r in unwrapped], title="🎁 Unwrapped loot",
                     note="Unenchanted for part of the night, usually new loot put on and never enchanted", cluster="calls", stacked=True)

        sockets = sorted((r for r in rows if r.empty_sockets), key=lambda r: (-r.empty_sockets, r.char.name.casefold()))
        if sockets:
            parts = []
            for i, r in enumerate(sockets):
                if i:
                    parts.append(" · ")
                parts += [r.char, f" {plural(r.empty_sockets, 'empty socket')}"]
            self.add(GEAR, parts, "empty_socket", [r.char for r in sockets], title="💎 Empty sockets",
                     cluster="calls", stacked=True)

        if ready == len(rows):
            self.add(GEAR, ["✅ Everyone was fully enchanted and gemmed. Nice."], cluster="calls")

    def consumables(self) -> None:
        night = self.night
        if not night.retail or not night.auras_at_pull:
            return  # Classic's potions and elixirs need their own lists; not supported yet
        snapshots = Counter(char for players in night.auras_at_pull.values() for char in players)

        def missing(check) -> Counter:
            """Pulls on which each player didn't have a buff matching `check` as the pull started."""
            out: Counter = Counter()
            for players in night.auras_at_pull.values():
                for char, auras in players.items():
                    if not any(check(a) for a in auras):
                        out[char] += 1
            return out

        def of(n: int, total: int, first: bool) -> str:
            return f"{n} of {total} pulls" if first else f"{n} of {total}"

        # Tryhards: the consumable augment rune on at least half their pulls.
        runes = set(self.settings.tryhard_runes)
        if runes:
            used = Counter({c: snapshots[c] - n for c, n in missing(lambda a: a in runes).items()})
            used.update({c: n for c, n in snapshots.items() if c not in used})
            tryhards = sorted((c for c in used if used[c] and used[c] * 2 >= snapshots[c]),
                              key=lambda c: (-used[c] / snapshots[c], -used[c], c.name.casefold()))
            if tryhards:
                parts: list[Part] = []
                for i, c in enumerate(tryhards):
                    if i:
                        parts.append(" · ")
                    parts += [c, " every pull" if used[c] == snapshots[c] else f" {used[c]} of {snapshots[c]}"]
                self.add(CONSUMABLES, parts, "tryhard", tryhards, title="🔮 Tryhards",
                         note=f"{' / '.join(sorted(runes))} rune", cluster="shoutouts", chart="consumables")

        chars, n = leaders(night.potions, 5, max_names=2)
        if chars:
            pulls = len(night.pulls_in.get(chars[0], ()))
            tail = f" · {int(n)} combat potions each" if len(chars) > 1 else f" · {int(n)} combat potions in {pulls} pulls"
            self.add(CONSUMABLES, [*joined(chars), tail],
                     "potion_seller", chars, title="🍺 Potion seller", cluster="shoutouts")

        healer_mana = Counter({c: v for c, v in night.mana_potions.items() if night.roles.get(c) == HEALER})
        chars, n = leaders(healer_mana, 3, max_names=2)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(CONSUMABLES, [*joined(chars), f" · {int(n)} mana potions{each}"],
                     "mana_chugger", chars, title="🫗 Mana chugger", cluster="shoutouts")

        chars, n = leaders(night.health_items, 10, max_names=2)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(CONSUMABLES, [*joined(chars), f" · {int(n)} healthstones and health potions{each}"],
                     "cookie", chars, title="🍪 Cookie monster", cluster="shoutouts")

        # Potion hoarders. DPS and tanks: no combat potion on more than half their pulls. Healers
        # drink mana potions when they need them, so they only count if they drank nothing at all.
        hoarders = []
        for char, pulls in night.pulls_in.items():
            total = len(pulls)
            potted = len(night.potion_pulls.get(char, ()))
            if total < 3:
                continue
            if night.roles.get(char) == HEALER:
                if not potted and not night.mana_potions.get(char):
                    hoarders.append((1.0, char, "healer", total))
            elif (total - potted) * 2 > total:
                hoarders.append(((total - potted) / total, char, total - potted, total))
        if hoarders:
            parts = []
            # DPS and tanks first, worst first; healers (who drank nothing at all) after them.
            ordered = sorted(hoarders, key=lambda h: (h[2] == "healer", -h[0], h[1].name.casefold()))
            numbered = 0
            for i, (_, char, skipped, total) in enumerate(ordered):
                if i:
                    parts.append(" · ")
                if skipped == "healer":
                    parts += [char, " no potion of any kind all night"]
                elif skipped == total:
                    parts += [char, " not a single one all night"]
                else:
                    parts += [char, " " + of(skipped, total, first=numbered == 0)]
                    numbered += 1
            self.add(CONSUMABLES, parts, "hoarder", [h[1] for h in hoarders], title="🧪 Potion hoarders",
                     note="No combat potion on most of their pulls (healers: no potion of any kind)",
                     cluster="shame", chart="consumables")

        # Died with a healthstone in the bag: not one healthstone or health potion all night, and
        # died at least twice (deaths before the wipe call, the same count as the floor inspector).
        if night.casts_known:
            counted = Counter(d.char for ds in night.counted.values() for d in ds)
            bag = sorted((c for c, n in counted.items() if n >= 2 and not night.health_items.get(c)),
                         key=lambda c: (-counted[c], c.name.casefold()))
            if bag:
                parts = []
                for i, c in enumerate(bag):
                    if i:
                        parts.append(" · ")
                    parts += [c, (" died " if i == 0 else " ") + times(counted[c])]
                self.add(CONSUMABLES, parts, "healthstone_bag", bag, title="🪦 Died with a healthstone in the bag",
                         note="Not one healthstone or health potion all night", cluster="shame", chart="consumables")

        for key, title, check in (
            ("no_flask", "⚗️ No flask", lambda a: a.startswith(FLASK_PREFIXES)),
            ("no_food", "🍗 Forgot to eat", lambda a: FOOD_PATTERN in a),
        ):
            gaps = missing(check)
            offenders = sorted((c for c, n in gaps.items() if n >= 2), key=lambda c: (-gaps[c], c.name.casefold()))
            if offenders:
                parts = []
                for i, c in enumerate(offenders):
                    if i:
                        parts.append(" · ")
                    parts += [c, " " + of(gaps[c], snapshots[c], first=i == 0)]
                self.add(CONSUMABLES, parts, key, offenders, title=title, cluster="prep", chart="consumables")

        # No weapon oil: read from the main hand in each pull's gear snapshot (oils, sharpening
        # stones and shaman imbues are all temporary enchants), by the flask and food rule.
        unoiled = sorted((r for r in consumable_rows(night, self.settings) if "no_oil" in r.flags),
                         key=lambda r: (-(r.oil_pulls - r.oil), r.char.name.casefold()))
        if unoiled:
            parts = []
            for i, r in enumerate(unoiled):
                if i:
                    parts.append(" · ")
                parts += [r.char, " " + of(r.oil_pulls - r.oil, r.oil_pulls, first=i == 0)]
            self.add(CONSUMABLES, parts, "no_oil", [r.char for r in unoiled], title="🛢️ No weapon oil",
                     cluster="prep", chart="consumables")

        self.ran_out()

        # No vantus: only pulls where at least half the raid had one, so skipping it on farm
        # bosses is never called out.
        vantus_pulls: Counter = Counter()
        without: Counter = Counter()
        for players in night.auras_at_pull.values():
            has = {c for c, auras in players.items() if any(a.startswith(VANTUS_PREFIX) for a in auras)}
            if players and len(has) * 2 >= len(players):
                for c in players:
                    vantus_pulls[c] += 1
                    if c not in has:
                        without[c] += 1
        skipped = sorted((c for c, n in without.items() if n * 2 >= vantus_pulls[c]),
                         key=lambda c: (-without[c], c.name.casefold()))
        if skipped:
            parts = []
            # People with the same count share one entry: "A, B and C, all 4 pulls".
            runs: list[tuple[tuple[int, int], list[Char]]] = []
            for c in skipped:
                count = (without[c], vantus_pulls[c])
                if runs and runs[-1][0] == count:
                    runs[-1][1].append(c)
                else:
                    runs.append((count, [c]))
            for i, ((n, total), chars) in enumerate(runs):
                if i:
                    parts.append(" · ")
                comma = "," if len(chars) > 1 else ""
                parts += [*joined(chars), f"{comma} all {total} pulls" if n == total else f"{comma} " + of(n, total, first=i == 0)]
            self.add(CONSUMABLES, parts, "no_vantus", skipped, title="📜 No vantus",
                     note="On pulls where most of the raid had one", cluster="prep", chart="consumables")


def pull_label(night: Night, pull_id: int) -> str:
    """'Ula'tek pull 4': the boss, and which of its pulls tonight."""
    for boss in night.bosses:
        for i, pull in enumerate(boss.pulls):
            if pull.id == pull_id:
                return f"{boss.name} pull {i + 1}" if len(boss.pulls) > 1 else boss.name
    return "a pull"


def progress_chart_key(boss: Boss, index: int) -> str | None:
    """Bosses pulled 3+ times with the boss's health known on every pull get a chart."""
    if len(boss.pulls) >= 3 and all(p.boss_pct is not None for p in boss.pulls):
        return f"progress:{index}"
    return None


@dataclass
class ConsumableRow:
    """One raider's consumables for the night, and which of them the report would call out. The
    rules are the Consumables callouts' rules, so the grid and the text never disagree."""
    char: Char
    role: str
    snapshots: int  # pulls with a buff snapshot
    pulls: int  # boss pulls they were in
    flask: int
    food: int
    rune: int
    potion_pulls: int  # pulls with a combat potion
    mana_potions: int
    health_items: int
    vantus: int  # of vantus_pulls
    vantus_pulls: int  # pulls where most of the raid had a vantus rune
    flags: set[str] = field(default_factory=set)  # tryhard, hoarder, healthstone_bag, no_flask, no_food,
    # no_oil, no_vantus
    combat_potions: int = 0  # every combat potion drunk
    potted: int = 0  # "pulls potted": pulls with at least one potion (for healers, a mana potion counts)
    oil: int = 0  # pulls with a weapon oil (or stone, or shaman imbue) on the main hand
    oil_pulls: int = 0  # pulls with a main-hand weapon in the log's gear snapshot
    oil_gone_pull: int | None = None  # first pull without weapon oil after having it: it ran out between pulls


def consumable_rows(night: Night, settings: RecapSettings) -> list[ConsumableRow]:
    if not night.retail or not night.auras_at_pull:
        return []
    runes = set(settings.tryhard_runes)
    rows: dict[Char, ConsumableRow] = {}
    for char in night.roster:
        rows[char] = ConsumableRow(char, night.roles.get(char, DPS), 0, len(night.pulls_in.get(char, ())),
                                   0, 0, 0, len(night.potion_pulls.get(char, ())), night.mana_potions.get(char, 0),
                                   night.health_items.get(char, 0), 0, 0)
    for char, row in rows.items():
        row.combat_potions = night.potions.get(char, 0)
        pulls = set(night.potions_by_pull.get(char, ()))
        if row.role == HEALER:
            pulls |= set(night.mana_by_pull.get(char, ()))
        row.potted = len(pulls)
    for players in night.auras_at_pull.values():
        has_vantus = {c for c, auras in players.items() if any(a.startswith(VANTUS_PREFIX) for a in auras)}
        vantus_pull = bool(players) and len(has_vantus) * 2 >= len(players)
        for char, auras in players.items():
            row = rows.get(char)
            if row is None:
                continue
            row.snapshots += 1
            row.flask += any(a.startswith(FLASK_PREFIXES) for a in auras)
            row.food += any(FOOD_PATTERN in a for a in auras)
            row.rune += any(a in runes for a in auras)
            if vantus_pull:
                row.vantus_pulls += 1
                row.vantus += char in has_vantus
    for players in night.gear_at_pull.values():
        for char, gear in players.items():
            row = rows.get(char)
            weapon = gear[15] if row and len(gear) > 15 else None  # 15 = main hand
            if weapon:
                row.oil_pulls += 1
                row.oil += weapon.oiled
    for char, row in rows.items():
        had = False
        for pull in night.pulls:
            gear = night.gear_at_pull.get(pull.id, {}).get(char)
            weapon = gear[15] if gear and len(gear) > 15 else None
            if not weapon:
                continue
            if weapon.oiled:
                had = True
            elif had:
                row.oil_gone_pull = pull.id
                break
    counted = Counter(d.char for ds in night.counted.values() for d in ds)
    for row in rows.values():
        if row.oil_pulls - row.oil >= 2:
            row.flags.add("no_oil")
        if runes and row.rune and row.rune * 2 >= row.snapshots:
            row.flags.add("tryhard")
        if row.pulls >= 3:
            if row.role == HEALER:
                if not row.potion_pulls and not row.mana_potions:
                    row.flags.add("hoarder")
            elif (row.pulls - row.potion_pulls) * 2 > row.pulls:
                row.flags.add("hoarder")
        if night.casts_known and counted[row.char] >= 2 and not row.health_items:
            row.flags.add("healthstone_bag")
        if row.snapshots - row.flask >= 2:
            row.flags.add("no_flask")
        if row.snapshots - row.food >= 2:
            row.flags.add("no_food")
        missed = row.vantus_pulls - row.vantus
        if missed and missed * 2 >= row.vantus_pulls:
            row.flags.add("no_vantus")
    order = {TANK: 0, HEALER: 1, DPS: 2}
    return sorted((r for r in rows.values() if r.snapshots),
                  key=lambda r: (order.get(r.role, 3), r.char.name.casefold()))


def build_lines(night: Night, settings: RecapSettings) -> list[Line]:
    builder = Builder(night, settings)
    builder.headline()
    builder.the_night()
    builder.board()
    builder.highlights()
    builder.lowlights()
    builder.deaths()
    builder.consumables()
    builder.gear()
    return builder.lines


def winners_by_key(lines: list[Line]) -> dict[str, list[Char]]:
    return {line.key: line.winners for line in lines if line.key and line.winners}

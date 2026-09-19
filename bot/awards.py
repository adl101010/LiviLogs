"""The night -> the lines of the report: headline callouts, awards, and the night summary.

Every award checks whether tonight gives it something worth saying and stays silent otherwise, so
the report is as long as the night was eventful. A line is content, not layout: a title ("🍺 Potion
seller"), an optional note in small print, and a body of names and numbers. Bodies hold Chars
rather than names; the renderer turns those into Discord mentions and decides how it all looks.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from .config import RecapSettings
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

Part = str | Char


@dataclass
class Line:
    section: str
    parts: list[Part]  # the body: names and numbers
    key: str | None = None  # awards remember their winners, for "3 raids running"
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


@dataclass(frozen=True)
class BossResult:
    killed: bool
    boss_pct: float | None
    phase: int | None


class History(Protocol):
    def last_result(self, encounter_id: int, difficulty: int | None, before_ms: int) -> BossResult | None: ...

    def streak(self, key: str, char: Char, before_ms: int) -> int: ...


class NoHistory:
    def last_result(self, encounter_id, difficulty, before_ms):
        return None

    def streak(self, key, char, before_ms):
        return 0


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
_RUNNING = re.compile(r"^ \((\d+ raids running)\)$")
PHASE_BARS = "▁▂▃▄▅▆▇█"


class Builder:
    def __init__(self, night: Night, settings: RecapSettings, history: History):
        self.night = night
        self.settings = settings
        self.history = history
        self.lines: list[Line] = []

    def add(self, section: str, parts: list[Part], key: str | None = None,
            winners: list[Char] | None = None, *, title: str | None = None, note: str | None = None,
            cluster: str = "", stacked: bool | None = None, chart: str | None = None) -> None:
        # "(10 deaths) (2 raids running)" reads better as "(10 deaths, 2 raids running)".
        merged: list[Part] = []
        for part in parts:
            match = _RUNNING.match(part) if isinstance(part, str) else None
            if match and merged and isinstance(merged[-1], str) and merged[-1].endswith(")"):
                merged[-1] = f"{merged[-1][:-1]}, {match.group(1)})"
            elif part != "":
                merged.append(part)
        self.lines.append(Line(section, merged, key, winners or [], title, note, cluster, stacked, chart))

    def running(self, key: str, char: Char) -> str:
        streak = self.history.streak(key, char, self.night.start_ms)
        return f" ({streak + 1} raids running)" if streak else ""

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
            self.add(HEADLINE, [*joined([d.char for d in top]), f" {top[0].deaths} deaths{each}",
                                self.running("floor", top[0].char) if len(top) == 1 else ""],
                     "floor", [d.char for d in top], title="💀 Floor inspector", cluster="calls", stacked=False)

    def progress_lines(self, unkilled: list[Boss]) -> None:
        prog_night = not self.night.kills
        for boss in unkilled:
            best = boss.best_wipe
            last_note = self.last_raid(boss)
            if prog_night and len(unkilled) == 1:
                final = " · the last pull of the night" if best is boss.pulls[-1] and len(boss.pulls) > 1 else ""
                self.add(HEADLINE, [f"{fmt_health(best.boss_pct, best.phase)}{final}{last_note}"],
                         title="📈 Best pull", cluster="tops")
            else:
                best_text = f" · best {fmt_health(best.boss_pct, best.phase)}" if best else ""
                self.add(HEADLINE, [f"{plural(len(boss.wipes), 'wipe')}{best_text}{last_note}"],
                         title=f"📈 {boss.name}", cluster="tops")

    def top_line(self, title: str, key: str, chars: list[Char], value: str) -> None:
        parts: list[Part] = [*joined(chars), f" {value}"]
        if len(chars) == 1:
            parts.append(self.running(key, chars[0]))
        self.add(HEADLINE, parts, key, chars, title=title, cluster="tops", stacked=False)

    def parse_list(self, lines: list[ParseLine], key: str) -> list[Part]:
        parts: list[Part] = []
        for i, p in enumerate(lines):
            if i:
                parts.append(" · ")
            kind = "healing" if p.role == HEALER else "damage"
            streak = self.history.streak(key, p.char, self.night.start_ms)
            running = f" ({streak + 1} raids running)" if streak else ""
            parts += [p.char, f" {kind} {p.average:.1f}{running}"]
        return parts

    def last_raid(self, boss: Boss) -> str:
        last = self.history.last_result(boss.encounter_id, boss.difficulty, self.night.start_ms)
        if last is None:
            return ""
        if last.killed:
            return " · killed it last raid"
        return f" · last raid's best: {fmt_health(last.boss_pct, last.phase)}"

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
                        parts += [r.char, f" **{fmt_rate(r.per_second)}**{missed} 👑"]
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
            self.add(HIGHLIGHTS, [p.char, f" · {lo:.0f} to {hi:.0f} on every boss",
                                  self.running("metronome", p.char)], "metronome", [p.char], title="🎵 Metronome")

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
        # PI's favourite: whoever a priest kept giving Power Infusion to (priests on themselves don't count).
        received = Counter()
        for (giver, receiver), times_given in night.power_infusion.items():
            received[receiver] += times_given
        chars, n = leaders(received, 3, max_names=1)
        if chars:
            givers = Counter({g: t for (g, r), t in night.power_infusion.items() if r == chars[0]})
            giver, from_them = givers.most_common(1)[0]
            source: list[Part] = [" from ", giver] if from_them == n else [" from ", giver, " and others"]
            self.add(HIGHLIGHTS, [chars[0], " · got Power Infusion", *source, f" {times(int(n))}",
                                  self.running("pi", chars[0])], "pi", chars, title="💜 PI's favorite")

        chars, n = leaders(last, 3, max_names=1)
        if chars and wipes_with_deaths >= 3:
            self.add(HIGHLIGHTS, [chars[0], f" · last to die on {int(n)} of {wipes_with_deaths} wipes",
                                  self.running("last_standing", chars[0])], "last_standing", chars,
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
        running = self.running(key, chars[0]) if len(chars) == 1 else ""
        self.add(section, [*joined(chars), tail, running], key, chars, title=title)

    # --- lowlights -----------------------------------------------------------------------------

    def lowlights(self) -> None:
        night = self.night
        worst = [(pct, boss, p) for p in night.parses if p.role != TANK for pct, boss in p.parses]
        if worst:
            pct, boss, p = min(worst, key=lambda w: (w[0], w[2].char.name.casefold()))
            if pct <= 10:
                self.add(LOWLIGHTS, [p.char, f" · {pct:.0f} on {boss}", self.running("shame", p.char)],
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

        healers = sorted(((night.damage_done.get(c, 0), c) for c, r in night.roles.items() if r == HEALER),
                         key=lambda dc: -dc[0])
        if len(healers) >= 2 and healers[0][0] >= 1_000_000 and healers[0][0] >= 1.5 * max(healers[1][0], 1):
            dmg, char = healers[0]
            ratio = dmg / max(healers[1][0], 1)
            ratio_text = f"{ratio:.0f}×" if ratio >= 2.95 else f"{ratio:.1f}×"
            grey = next((p for p in night.grey if p.char == char), None)
            grey_text = f" with a {grey.average:.1f} healing parse" if grey else ""
            self.add(LOWLIGHTS, [char, f" · {fmt_big(dmg)} damage ({ratio_text} the next healer){grey_text}",
                                 self.running("battle_healer", char)], "battle_healer", [char], title="⚔️ Battle healer")

        taken = sorted(((v, c) for c, v in night.damage_taken.items() if night.roles.get(c) != TANK),
                       key=lambda vc: -vc[0])
        if taken:
            v, char = taken[0]
            self.add(LOWLIGHTS, [char, f" · {fmt_big(v)} damage taken", self.running("sponge", char)],
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
            self.add(DEATHS, [*joined(chars), f" · first to die {times(int(n))}{each}",
                              self.running("canary", chars[0]) if len(chars) == 1 else ""], "canary", chars,
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
            self.add(DEATHS, [*joined(chars), f" · rezzed {times(int(n))}{each}",
                              self.running("brez_magnet", chars[0]) if len(chars) == 1 else ""],
                     "brez_magnet", chars, title="🧲 Brez magnet")

        # Ghost: the most time spent dead, from each death until a battle rez or the end of the pull.
        chars, seconds = leaders(night.dead_seconds, 180, max_names=1)
        if chars:
            self.add(DEATHS, [chars[0], f" · spent {fmt_duration(seconds)} dead",
                              self.running("ghost", chars[0])], "ghost", chars, title="👻 Ghost")

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
            self.add(CONSUMABLES, [*joined(chars), tail,
                                   self.running("potion_seller", chars[0]) if len(chars) == 1 else ""],
                     "potion_seller", chars, title="🍺 Potion seller", cluster="shoutouts")

        healer_mana = Counter({c: v for c, v in night.mana_potions.items() if night.roles.get(c) == HEALER})
        chars, n = leaders(healer_mana, 3, max_names=2)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(CONSUMABLES, [*joined(chars), f" · {int(n)} mana potions{each}",
                                   self.running("mana_chugger", chars[0]) if len(chars) == 1 else ""],
                     "mana_chugger", chars, title="🫗 Mana chugger", cluster="shoutouts")

        chars, n = leaders(night.health_items, 10, max_names=2)
        if chars:
            each = " each" if len(chars) > 1 else ""
            self.add(CONSUMABLES, [*joined(chars), f" · {int(n)} healthstones and health potions{each}",
                                   self.running("cookie", chars[0]) if len(chars) == 1 else ""],
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
    flags: set[str] = field(default_factory=set)  # tryhard, hoarder, healthstone_bag, no_flask, no_food, no_vantus
    combat_potions: int = 0  # every combat potion drunk
    potted: int = 0  # "pulls potted": pulls with at least one potion (for healers, a mana potion counts)


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
    counted = Counter(d.char for ds in night.counted.values() for d in ds)
    for row in rows.values():
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


def build_lines(night: Night, settings: RecapSettings, history: History | None = None) -> list[Line]:
    builder = Builder(night, settings, history or NoHistory())
    builder.headline()
    builder.the_night()
    builder.board()
    builder.highlights()
    builder.lowlights()
    builder.deaths()
    builder.consumables()
    return builder.lines


def winners_by_key(lines: list[Line]) -> dict[str, list[Char]]:
    return {line.key: line.winners for line in lines if line.key and line.winners}


def boss_results(night: Night) -> list[tuple[int, int | None, str, BossResult]]:
    results = []
    for boss in night.bosses:
        best = boss.best_wipe
        results.append((boss.encounter_id, boss.difficulty, boss.name, BossResult(
            killed=boss.killed,
            boss_pct=None if boss.killed or not best else best.boss_pct,
            phase=None if boss.killed or not best else best.phase,
        )))
    return results

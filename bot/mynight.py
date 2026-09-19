"""The "My night" card: one raider's own night, shown only to them when they press the button.

Built from the same Night the report was, so its numbers always match the report's.
"""

from collections import Counter

from .awards import Line, consumable_rows, fmt_big, fmt_rate, fmt_seconds, parse_colour, plural, times
from .config import RecapSettings
from .gear import describe, gear_rows
from .recap import DPS, HEALER, TANK, Char, Night
from .render import BLURPLE, Card

ROLE_LABEL = {DPS: "Damage", HEALER: "Healer", TANK: "Tank"}


def my_night_card(night: Night, lines: list[Line], char: Char, settings: RecapSettings, url: str) -> Card:
    role = night.roles.get(char, DPS)
    total = len(night.pulls)
    played = len(night.pulls_in.get(char, ()))
    facts = [ROLE_LABEL.get(role, "Damage")]
    if night.start_ms:
        facts.append(f"<t:{night.start_ms // 1000}:D>")
    facts.append(f"all {total} pulls" if played == total else f"{played} of {total} pulls")

    blocks = [b for b in (_performance(night, char, role), _deaths(night, char),
                          _consumables(night, char, settings), _gear(night, char, settings),
                          _extras(night, char), _awards(lines, char)) if b]
    return Card(f"👤 Your night · {char.name}", BLURPLE, blocks, subtitle=" · ".join(facts),
                button=("View log", url), pings=False)


def _performance(night: Night, char: Char, role: str) -> str | None:
    parse = next((p for p in night.parses if p.char == char), None)
    if parse:
        kind = "healing" if parse.role == HEALER else "damage"
        text = f"**📊 {parse.average:.1f} average** {kind} parse"
        by_boss: dict[str, list[float]] = {}
        for pct, boss in parse.parses:
            by_boss.setdefault(boss, []).append(pct)
        for boss in night.bosses:
            if boss.name in by_boss:
                pct = sum(by_boss[boss.name]) / len(by_boss[boss.name])
                text += f"\n{parse_colour(pct)} - {boss.name} {pct:.0f}"
        return text
    rates = [r for r in night.rates if r.role == role]
    mine = next((r for r in rates if r.char == char), None)
    if mine is None:
        return None
    unit = " HPS" if role == HEALER else " DPS"
    group = {HEALER: "healers", TANK: "tanks"}.get(role, "DPS")
    rank = rates.index(mine) + 1
    return f"**📊 {fmt_rate(mine.per_second)}{unit}** · {_ordinal(rank)} of {len(rates)} {group}\n" \
           f"-# Wipes don't get parses, so this is raw throughput"


def _deaths(night: Night, char: Char) -> str:
    counted = sum(1 for ds in night.counted.values() for d in ds if d.char == char)
    every = [d for ds in night.deaths.values() for d in ds if d.char == char]
    if not every:
        return "**💀 No deaths** · not one, all night"
    text = f"**💀 {plural(counted, 'death')}** before the wipe was called"
    if len(every) != counted:
        text += f" ({len(every)} in all)"
    dead = night.dead_seconds.get(char, 0)
    if dead >= 60:
        text += f" · {fmt_seconds(dead)} dead"
    notes = []
    killers = Counter(d.ability for d in every if d.ability)
    repeat = [(name, n) for name, n in killers.most_common() if n >= 2][:2]
    if repeat:
        if len(repeat) == 2 and repeat[0][1] == repeat[1][1]:
            notes.append(f"{repeat[0][0]} and {repeat[1][0]} got you {times(repeat[0][1])} each")
        else:
            notes.append(f"{repeat[0][0]} got you {times(repeat[0][1])}")
    firsts = sum(1 for ds in night.counted.values() if ds and ds[0].char == char)
    if firsts:
        notes.append(f"first to die {times(firsts)}")
    if notes:
        text += "\n-# " + " · ".join(notes)
    return text


def _consumables(night: Night, char: Char, settings: RecapSettings) -> str | None:
    row = next((r for r in consumable_rows(night, settings) if r.char == char), None)
    if row is None:
        return None

    def item(label: str, value: str, flag: str | None = None) -> str:
        return f"⚠️ {label} {value}" if flag and flag in row.flags else f"{label} {value}"

    items = [item("Flask", f"{row.flask}/{row.snapshots}", "no_flask"),
             item("Food", f"{row.food}/{row.snapshots}", "no_food"),
             item("Healthstones", str(row.health_items), "healthstone_bag")]
    if row.vantus_pulls:
        items.append(item("Vantus", f"{row.vantus}/{row.vantus_pulls}", "no_vantus"))
    if row.rune:
        rune = " / ".join(settings.tryhard_runes)
        items.append(("🔮 " if "tryhard" in row.flags else "") + f"{rune} {row.rune}/{row.snapshots}")
    text = "**🧪 Consumables**\n" + " · ".join(items)

    # Potions: one dot per pull, in order, then the two numbers the grid shows.
    used = f"{row.combat_potions} used"
    if row.role == HEALER and row.mana_potions:
        used = (f"{row.combat_potions} + {row.mana_potions} mana used" if row.combat_potions
                else f"{row.mana_potions} used (all mana)")
    potions = f"{row.potted}/{row.pulls} pulls potted · {used}"
    text += "\n" + ("⚠️ " if "hoarder" in row.flags else "") + f"**Potions** · {potions}"
    text += "\n" + _potion_dots(night, char, row.role == HEALER)
    note = "🟢 one potion · 🟣 two or more · ⚫ none, one dot per pull in order"
    if any(f in row.flags for f in ("no_flask", "no_food", "hoarder", "healthstone_bag", "no_vantus")):
        note += " · ⚠️ = the report called it out"
    return text + f"\n-# {note}"


def _potion_dots(night: Night, char: Char, healer: bool) -> str:
    combat = night.potions_by_pull.get(char, {})
    mana = night.mana_by_pull.get(char, {}) if healer else {}
    mine = night.pulls_in.get(char, set())
    dots = []
    for pull in night.pulls:
        if pull.id in mine:
            n = combat.get(pull.id, 0) + mana.get(pull.id, 0)
            dots.append("⚫" if n == 0 else "🟢" if n == 1 else "🟣")
    return "".join(dots)


def _gear(night: Night, char: Char, settings: RecapSettings) -> str | None:
    row = next((r for r in gear_rows(night, settings) if r.char == char), None)
    if row is None:
        return None
    gems = f"{row.gems} gems" if row.gems != 1 else "1 gem"
    if row.ready:
        return f"**🛠️ Gear** · ✅ fully enchanted · {gems}"
    bits = []
    if row.missing:
        bits.append("⚠️ missing enchants: " + ", ".join(describe(row.missing, row)))
    for check in row.unwrapped:
        slot = describe([check], row)[0]
        bits.append(f"⚠️ {'new ' if check.boss else ''}{slot} unenchanted for {plural(check.bare_pulls, 'pull')}")
    if row.empty_sockets:
        bits.append(f"⚠️ {plural(row.empty_sockets, 'empty socket')}")
    late = [c for c in row.checks if c.state == "late"]
    if late and not bits:
        return f"**🛠️ Gear** · ✅ fully enchanted from pull {max(c.from_pull for c in late)} · {gems}"
    return "**🛠️ Gear**\n" + " · ".join(bits)


def _extras(night: Night, char: Char) -> str | None:
    parts = []
    if night.damage_done.get(char):
        parts.append(f"⚔️ {fmt_big(night.damage_done[char])} damage")
    if night.damage_taken.get(char):
        parts.append(f"🧽 {fmt_big(night.damage_taken[char])} taken")
    if night.interrupts.get(char):
        parts.append(f"🦶 {plural(night.interrupts[char], 'interrupt')}")
    if night.dispels.get(char):
        parts.append(f"🧼 {plural(night.dispels[char], 'dispel')}")
    if night.brez_given.get(char):
        n = night.brez_given[char]
        parts.append(f"🪄 {n} battle {'rez' if n == 1 else 'rezzes'}")
    infused = sum(n for (giver, receiver), n in night.power_infusion.items() if receiver == char)
    if infused:
        parts.append(f"💜 Power Infusion {times(infused)}")
    return " · ".join(parts) or None


def _awards(lines: list[Line], char: Char) -> str | None:
    titles = list(dict.fromkeys(line.title for line in lines if line.title and char in line.winners))
    return f"**🏅 In tonight's report** · {', '.join(titles)}" if titles else None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

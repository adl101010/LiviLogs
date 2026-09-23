"""Gear check: is every enchantable slot enchanted, and every socket filled?

WCL snapshots each player's gear as every pull starts, so each slot is checked on every pull. That
catches a piece looted mid-raid and never enchanted, as well as enchants that were missing all
night. Retail only: the slots below are the ones Midnight lets you enchant.
"""

from dataclasses import dataclass, field

from .config import RecapSettings
from .recap import DPS, HEALER, TANK, Char, Night

# WoW's equipment slot numbers.
HEAD, SHOULDER, CHEST, LEGS, FEET, FINGER_1, FINGER_2, MAIN_HAND, OFF_HAND = 0, 2, 4, 6, 7, 10, 11, 15, 16
COLUMNS = [("Helm", [HEAD]), ("Shoulders", [SHOULDER]), ("Chest", [CHEST]), ("Legs", [LEGS]), ("Boots", [FEET]),
           ("Rings", [FINGER_1, FINGER_2]), ("Weapons", [MAIN_HAND, OFF_HAND])]

# Specs that fight with a weapon in each hand. Anyone else's off hand is a shield or a held item,
# which can't be enchanted. (An off hand someone did enchant always counts, whatever the spec.)
DUAL_WIELD_SPECS = {
    251,  # Frost Death Knight
    72,  # Fury Warrior
    259, 260, 261,  # Rogues
    263,  # Enhancement Shaman
    268, 269,  # Brewmaster, Windwalker Monk
    577, 581,  # Havoc, Vengeance Demon Hunter
}

OK, MISSING, NEW, LATE, PARTLY = "ok", "missing", "new", "late", "partly"


@dataclass
class SlotCheck:
    slot: int
    state: str  # ok, missing (never enchanted), new (new piece left bare), late (enchanted mid-raid), partly
    bare_pulls: int = 0  # pulls it was worn without an enchant
    from_pull: int = 0  # late: the pull it was first enchanted on (1 = the player's first pull)
    boss: str | None = None  # new: the boss whose pull the new piece first showed up on


@dataclass
class GearRow:
    char: Char
    role: str
    columns: list[tuple[str, list[SlotCheck]]]  # (column, checks); no checks = nothing to enchant there
    gems: int  # gems worn on their last pull
    empty_sockets: int  # sockets without a gem on their last pull
    pulls: int
    checks: list[SlotCheck] = field(default_factory=list)

    @property
    def missing(self) -> list[SlotCheck]:
        return [c for c in self.checks if c.state == MISSING]

    @property
    def unwrapped(self) -> list[SlotCheck]:
        return [c for c in self.checks if c.state in (NEW, PARTLY)]

    @property
    def sockets(self) -> int:
        """Sockets we can prove: every gem sits in one, and every empty socket we can see is one."""
        return self.gems + self.empty_sockets

    @property
    def ready(self) -> bool:
        return all(c.state == OK for c in self.checks) and not self.empty_sockets


def gear_rows(night: Night, settings: RecapSettings) -> list[GearRow]:
    if not night.retail or not night.gear_at_pull:
        return []
    order = {p.id: i for i, p in enumerate(night.pulls)}
    boss_of = {p.id: p.boss for p in night.pulls}
    sockets = set(settings.socket_bonus_ids)
    rows = []
    for char in night.roster:
        pulls = sorted((pid for pid, players in night.gear_at_pull.items() if char in players), key=order.get)
        if not pulls:
            continue
        spec = night.specs.get(char)
        columns = []
        checks = []
        for label, slots in COLUMNS:
            column = []
            for slot in slots:
                worn = [(pid, _item(night, pid, char, slot)) for pid in pulls]
                worn = [(pid, item) for pid, item in worn if item]
                if not worn:
                    continue  # an empty slot: an off hand behind a two-hander
                if slot == OFF_HAND and spec not in DUAL_WIELD_SPECS and not any(i.enchanted for _, i in worn):
                    continue  # a shield or held item
                column.append(_check(slot, worn, boss_of))
            columns.append((label, column))
            checks += column
        last = night.gear_at_pull[pulls[-1]][char]
        gems = sum(item.gems for item in last if item)
        empty = sum(1 for item in last if item and not item.gems and item.bonus_ids & sockets)
        rows.append(GearRow(char, night.roles.get(char, DPS), columns, gems, empty, len(pulls), checks))
    role_order = {TANK: 0, HEALER: 1, DPS: 2}
    return sorted(rows, key=lambda r: (role_order.get(r.role, 3), r.char.name.casefold()))


def _item(night: Night, pull: int, char: Char, slot: int):
    gear = night.gear_at_pull[pull][char]
    return gear[slot] if slot < len(gear) else None


def _check(slot: int, worn: list, boss_of: dict[int, str]) -> SlotCheck:
    bare = [i for i, (_, item) in enumerate(worn) if not item.enchanted]
    if not bare:
        return SlotCheck(slot, OK)
    if len(bare) == len(worn):
        return SlotCheck(slot, MISSING, bare_pulls=len(bare))
    first_done = next(i for i, (_, item) in enumerate(worn) if item.enchanted)
    if bare[-1] < first_done:  # bare at the start, enchanted from then on
        return SlotCheck(slot, LATE, bare_pulls=len(bare), from_pull=first_done + 1)
    # Enchanted, then bare: usually new loot put on and never enchanted.
    first_bare = next(i for i in bare if i > first_done)
    swapped = worn[first_bare][1].id != worn[first_bare - 1][1].id
    return SlotCheck(slot, NEW if swapped else PARTLY, bare_pulls=len(bare),
                     boss=boss_of.get(worn[first_bare][0]) if swapped else None)


SLOT_NAMES = {HEAD: "helm", SHOULDER: "shoulders", CHEST: "chest", LEGS: "legs", FEET: "boots",
              FINGER_1: "ring", FINGER_2: "ring", MAIN_HAND: "main hand", OFF_HAND: "off hand"}


def describe(checks: list[SlotCheck], row: GearRow) -> list[str]:
    """Slots in words: "helm", "one ring", "both rings", "weapon", "off hand"."""
    out = []
    for label, column in row.columns:
        hit = [c for c in column if any(c is x for x in checks)]
        if not hit:
            continue
        if label == "Rings":
            out.append("both rings" if len(hit) == 2 else "one ring")
        elif label == "Weapons":
            if len(column) == 1:
                out.append("weapon")
            else:
                out.append("both weapons" if len(hit) == 2 else SLOT_NAMES[hit[0].slot])
        else:
            out.append(SLOT_NAMES[hit[0].slot])
    return out

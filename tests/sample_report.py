"""A small hand-built report, in the shape the real WCL API returns (checked 2026-09-18).

tests/fixtures/ holds real logs; this one pins down edge cases by hand. The night:
  Boss A: a wipe with the boss at 8% in P2 (a heartbreaker), then a kill
  Boss B: killed first pull
  Boss C: two wipes, never killed (progress)
Six raiders, plus a bystander who shows up in the log but never pulls a boss.

Consumables (5 boss pulls):
  Pumper: Void-Touched rune every pull, a combat potion every pull plus one extra (6), a healthstone
  Middling: rune on 3 of 5 pulls, potions on 4, 10 health potions (cookie monster)
  Greyson: no flask on 2 pulls, no vantus, one combat potion (hoarder), no healthstone, died twice
  Dyer: no food on 2 pulls, rune once, potions on 3 of 5, healthstones
  Tanky: no combat potion at all
  Healz: healer, no combat potion but 4 mana potions (not a hoarder; the mana chugger)
The raid used vantus runes on Boss C (pulls 5 and 6) only.
"""

RAID = [1, 2, 3, 4, 5, 6]


def _char(name, pct, server="Area 52"):
    return {"name": name, "server": {"name": server, "region": "US"}, "rankPercent": pct}


def _ranked(fight_id, boss, tanks, healers, dps, kill=1):
    return {
        "fightID": fight_id,
        "kill": kill,
        "encounter": {"id": 3000 + fight_id, "name": boss},
        "roles": {
            "tanks": {"name": "Tanks", "characters": tanks},
            "healers": {"name": "Healers", "characters": healers},
            "dps": {"name": "DPS", "characters": dps},
        },
    }


def _fight(fid, eid, name, kill, start, end, boss_pct=None, phase=1, players=RAID):
    return {"id": fid, "encounterID": eid, "name": name, "kill": kill, "difficulty": 4,
            "startTime": start, "endTime": end, "bossPercentage": 0 if kill else boss_pct,
            "lastPhase": phase, "friendlyPlayers": players}


ACTORS = [
    {"id": 1, "name": "Tanky", "server": "Area 52", "subType": "Warrior"},
    {"id": 2, "name": "Healz", "server": "Area 52", "subType": "Priest"},
    {"id": 3, "name": "Pumper", "server": "Area 52", "subType": "Mage"},
    {"id": 4, "name": "Greyson", "server": "Area 52", "subType": "Hunter"},
    {"id": 5, "name": "Dyer", "server": "Area 52", "subType": "Rogue"},
    {"id": 6, "name": "Middling", "server": "Area 52", "subType": "Shaman"},
    {"id": 7, "name": "Bystander", "server": "Area 52", "subType": "Druid"},  # never in a boss pull
]

FIRE, MELEE = 100, 101


def _death(target, fight, time, ability):
    return {"type": "death", "targetID": target, "fight": fight, "timestamp": time, "killingAbilityGameID": ability}


def _table(**totals):
    ids = {a["name"]: a["id"] for a in ACTORS}
    return {"data": {"entries": [{"name": n, "id": ids[n], "total": t} for n, t in totals.items()],
                     "totalTime": 800_000, "gameVersion": 1}}  # 1 = retail


BOSS_PULLS = [1, 2, 3, 5, 6]


AURA_IDS = {name: 1000 + i for i, name in enumerate(
    ["Arcane Intellect", "Flask of the Magisters", "Hearty Well Fed", "Void-Touched", "Vantus Rune: Boss C"])}


def _snapshot(pid, fight):
    """What a player had on as the pull started."""
    auras = ["Arcane Intellect"]
    if not (pid == 4 and fight in (5, 6)):
        auras.append("Flask of the Magisters")  # Greyson skipped his flask on Boss C
    if not (pid == 5 and fight in (1, 2)):
        auras.append("Hearty Well Fed")  # Dyer forgot to eat twice
    if pid == 3 or (pid == 6 and fight in (1, 2, 3)) or (pid == 5 and fight == 1):
        auras.append("Void-Touched")
    if fight in (5, 6) and pid != 4:
        auras.append("Vantus Rune: Boss C")
    return {"type": "combatantinfo", "fight": fight, "sourceID": pid,
            "auras": [{"name": a, "ability": AURA_IDS[a]} for a in auras]}


def _buff_end(pid, fight, time, name):
    return {"type": "removebuff", "sourceID": pid, "targetID": pid, "fight": fight, "timestamp": time,
            "abilityGameID": AURA_IDS[name]}


def _potions():
    drank = {3: BOSS_PULLS + [6], 6: [1, 2, 3, 5], 5: [1, 2, 3], 4: [1]}  # Tanky and Healz: none
    return [{"type": "applybuff", "sourceID": pid, "targetID": pid, "fight": f, "abilityGameID": 1236994}
            for pid, fights in drank.items() for f in fights]


def _cast_table():
    def entry(name, **by_player):
        return {"name": name, "guid": sum(map(ord, name)), "total": sum(by_player.values()),
                "sources": [{"name": n, "total": t} for n, t in by_player.items()]}
    return {"data": {"gameVersion": 1, "entries": [
        entry("Healthstone", Pumper=1, Dyer=2),
        entry("Silvermoon Health Potion", Middling=10),
        entry("Lightfused Mana Potion", Healz=4),
        entry("Potion of Recklessness", Pumper=6),  # a combat potion: counted from buffs, not here
        entry("Fortifying Brew", Greyson=5),  # a class ability, not a consumable
    ]}}


def _casts(**counts):
    ids = {a["name"]: a["id"] for a in ACTORS}
    details = [{"name": n, "id": ids[n], "total": c} for n, c in counts.items()]
    return {"data": {"entries": [{"entries": [{"name": "Some Spell", "details": details}]}]}}


def _details(role, name, pid, spec, count=5):
    return {"name": name, "id": pid, "specs": [{"spec": spec, "count": count}]}


def report():
    return {
        "code": "AbCdEf1234567890",
        "title": "Heroic night",
        "startTime": 1_758_070_800_000,  # 2025-09-17 01:00 UTC: still Sep 16 in the US
        "endTime": 1_758_082_000_000,
        "segments": 3,
        "exportedSegments": 3,
        "zone": {"id": 42, "name": "Liberation of Undermine"},
        "fights": [
            _fight(1, 3009, "Boss A", False, 0, 100_000, boss_pct=8, phase=2),
            _fight(2, 3009, "Boss A", True, 200_000, 400_000),
            _fight(3, 3010, "Boss B", True, 500_000, 700_000),
            {"id": 4, "encounterID": 0, "name": "Trash", "kill": True, "startTime": 750_000, "endTime": 760_000},
            _fight(5, 3011, "Boss C", False, 800_000, 900_000, boss_pct=60, phase=1),
            _fight(6, 3011, "Boss C", False, 1_000_000, 1_200_000, boss_pct=30, phase=2),
        ],
        "masterData": {
            "actors": ACTORS,
            "abilities": [{"gameID": FIRE, "name": "Fire"}, {"gameID": MELEE, "name": "Melee"}],
        },
        # Damage rankings: tanks and DPS are read from here. The healer's damage parse (3) must be ignored.
        "dpsRankings": {"data": [
            _ranked(2, "Boss A", tanks=[_char("Tanky", 5)], healers=[_char("Healz", 3)],
                    dps=[_char("Pumper", 99), _char("Greyson", 10), _char("Dyer", 50), _char("Middling", 89)]),
            _ranked(3, "Boss B", tanks=[_char("Tanky", 8)], healers=[_char("Healz", 3)],
                    dps=[_char("Pumper", 90), _char("Greyson", 30), _char("Dyer", 40), _char("Middling", 91)]),
        ]},
        # Healing rankings: only healers are read from here.
        "hpsRankings": {"data": [
            _ranked(2, "Boss A", tanks=[_char("Tanky", 99)], healers=[_char("Healz", 60)], dps=[_char("Greyson", 99)]),
            _ranked(3, "Boss B", tanks=[_char("Tanky", 99)], healers=[_char("Healz", 70)], dps=[_char("Greyson", "-")]),
        ]},
        "playerDetails": {"data": {"playerDetails": {
            "tanks": [_details("tanks", "Tanky", 1, "Protection")],
            "healers": [_details("healers", "Healz", 2, "Holy")],
            "dps": [_details("dps", n, i, "Spec") for i, n in ((3, "Pumper"), (4, "Greyson"), (5, "Dyer"), (6, "Middling"))],
        }}},
        # Everyone was in all 5 boss pulls: 800 seconds.
        "damageDone": _table(Tanky=30_000_000, Healz=4_000_000, Pumper=90_000_000, Greyson=20_000_000,
                             Dyer=60_000_000, Middling=70_000_000),
        "healing": _table(Healz=50_000_000, Tanky=5_000_000),
        "damageTaken": _table(Tanky=500_000_000, Healz=50_000_000, Pumper=50_000_000, Greyson=50_000_000,
                              Dyer=100_000_000, Middling=50_000_000),
        "interrupts": _casts(Pumper=6, Dyer=2),
        "dispels": _casts(Healz=12),
        # Death events: targetID is the report-local actor id from masterData.
        "deaths": [
            # Wipe on fight 1: six deaths, the 6th is after the wipe call (cutoff 5).
            _death(5, 1, 40_100, FIRE),
            _death(4, 1, 40_200, MELEE),
            _death(5, 1, 40_050, FIRE),  # out of order on purpose
            _death(2, 1, 40_300, MELEE),
            _death(3, 1, 40_400, MELEE),
            _death(6, 1, 40_500, MELEE),
            # Kill: Greyson dies first.
            _death(4, 2, 201_000, MELEE),
            _death(5, 2, 201_100, FIRE),
            # Trash death: ignored by default.
            _death(1, 4, 755_000, MELEE),
            # A pet: not a player actor, ignored.
            _death(99, 2, 201_200, MELEE),
        ],
        "resurrects": [
            {"type": "resurrect", "sourceID": 2, "targetID": 5, "fight": 1},
            {"type": "resurrect", "sourceID": 2, "targetID": 5, "fight": 2, "timestamp": 250_000},
            {"type": "resurrect", "sourceID": 2, "targetID": 5, "fight": 5},
        ],
        "combatantInfo": [_snapshot(pid, f) for f in BOSS_PULLS for pid in RAID],
        "powerInfusion": [{"type": "applybuff", "sourceID": 2, "targetID": 3, "fight": f} for f in (1, 2, 3, 5)]
                         + [{"type": "applybuff", "sourceID": 2, "targetID": 2, "fight": f} for f in BOSS_PULLS],
        "potions": _potions(),
        # Healz's 4 mana potions as casts, per pull: two on pull 6, one each on 1 and 5.
        "manaPotions": [{"type": "cast", "sourceID": 2, "fight": f, "abilityGameID": 1236648} for f in (1, 5, 6, 6)],
        # Buffs coming off mid-pull: Pumper's flask ran out 100 s into Boss C's second pull; Dyer's
        # food came off with a death on pull 1 (not counted); a feast ran out for five raiders on
        # pull 6 (one "raid food" line).
        "buffEnds": [_buff_end(3, 6, 1_100_000, "Flask of the Magisters"),
                     _buff_end(5, 1, 40_100, "Hearty Well Fed")]
                    + [_buff_end(pid, 6, 1_150_000 + pid * 1000, "Hearty Well Fed") for pid in (1, 2, 3, 5, 6)],
        "casts": _cast_table(),
    }

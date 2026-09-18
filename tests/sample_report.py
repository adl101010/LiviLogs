"""A small hand-built report, in the shape the real WCL API returns (checked 2026-09-18).

tests/fixtures/ holds real logs; this one exists to pin down edge cases by hand.
"""


def _char(name, pct, server="Area 52"):
    return {"name": name, "server": {"name": server, "region": "US"}, "rankPercent": pct}


def _fight(fight_id, tanks, healers, dps, kill=1):
    return {
        "fightID": fight_id,
        "kill": kill,
        "roles": {
            "tanks": {"name": "Tanks", "characters": tanks},
            "healers": {"name": "Healers", "characters": healers},
            "dps": {"name": "DPS", "characters": dps},
        },
    }


ACTORS = [
    {"id": 1, "name": "Tanky", "server": "Area 52", "subType": "Warrior"},
    {"id": 2, "name": "Healz", "server": "Area 52", "subType": "Priest"},
    {"id": 3, "name": "Pumper", "server": "Area 52", "subType": "Mage"},
    {"id": 4, "name": "Greyson", "server": "Area 52", "subType": "Hunter"},
    {"id": 5, "name": "Dyer", "server": "Area 52", "subType": "Rogue"},
    {"id": 6, "name": "Middling", "server": "Area 52", "subType": "Shaman"},
]


def report():
    return {
        "code": "AbCdEf1234567890",
        "title": "Heroic night",
        "startTime": 1_758_070_800_000,
        "endTime": 1_758_082_000_000,
        "segments": 3,
        "exportedSegments": 3,
        "zone": {"id": 42, "name": "Liberation of Undermine"},
        "fights": [
            {"id": 1, "encounterID": 3009, "name": "Boss A", "kill": False, "difficulty": 4},
            {"id": 2, "encounterID": 3009, "name": "Boss A", "kill": True, "difficulty": 4},
            {"id": 3, "encounterID": 3010, "name": "Boss B", "kill": True, "difficulty": 4},
            {"id": 4, "encounterID": 0, "name": "Trash", "kill": True, "difficulty": 4},
        ],
        "masterData": {"actors": ACTORS},
        # Damage rankings: tanks and DPS are read from here. The healer's damage parse (3) must be ignored.
        "dpsRankings": {
            "data": [
                _fight(
                    2,
                    tanks=[_char("Tanky", 5)],
                    healers=[_char("Healz", 3)],
                    dps=[_char("Pumper", 99), _char("Greyson", 10), _char("Dyer", 50), _char("Middling", 89)],
                ),
                _fight(
                    3,
                    tanks=[_char("Tanky", 8)],
                    healers=[_char("Healz", 3)],
                    dps=[_char("Pumper", 90), _char("Greyson", 30), _char("Dyer", 40), _char("Middling", 91)],
                ),
            ]
        },
        # Healing rankings: only healers are read from here.
        "hpsRankings": {
            "data": [
                _fight(2, tanks=[_char("Tanky", 99)], healers=[_char("Healz", 60)], dps=[_char("Greyson", 99)]),
                _fight(3, tanks=[_char("Tanky", 99)], healers=[_char("Healz", 70)], dps=[_char("Greyson", "-")]),
            ]
        },
        # Death events: targetID is the report-local actor id from masterData.
        "deaths": [
            # Wipe on fight 1: six deaths, the 6th is after the wipe call (cutoff 5).
            {"type": "death", "targetID": 5, "fight": 1, "timestamp": 100},
            {"type": "death", "targetID": 4, "fight": 1, "timestamp": 200},
            {"type": "death", "targetID": 5, "fight": 1, "timestamp": 50},  # out of order on purpose
            {"type": "death", "targetID": 2, "fight": 1, "timestamp": 300},
            {"type": "death", "targetID": 3, "fight": 1, "timestamp": 400},
            {"type": "death", "targetID": 6, "fight": 1, "timestamp": 500},
            # Kill: Greyson dies first.
            {"type": "death", "targetID": 4, "fight": 2, "timestamp": 1000},
            {"type": "death", "targetID": 5, "fight": 2, "timestamp": 1100},
            # Trash death: ignored by default.
            {"type": "death", "targetID": 1, "fight": 4, "timestamp": 2000},
            # A pet: not a player actor, ignored.
            {"type": "death", "targetID": 99, "fight": 2, "timestamp": 1200},
        ],
    }

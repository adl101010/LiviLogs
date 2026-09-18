"""Real logs pulled through the API on 2026-09-18 (names replaced by tools/make_fixture.py).

They pin the real JSON shape: if WCL changes it, or a change here breaks reading it, these fail.
The expected numbers are what the bot produced after checking the logic against the raw data.
"""

import json
from pathlib import Path

from bot.config import RecapSettings
from bot.recap import build_recap

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def everyone(report, role):
    recap = build_recap(report, RecapSettings(parse_high=-1))
    return sorted((p.average for p in recap.high if p.role == role), reverse=True)


def test_retail_heroic():
    data = load("retail_heroic")
    recap = build_recap(data, RecapSettings())
    assert (recap.zone, recap.difficulty, recap.kills, recap.wipes) == ("The Venomous Abyss", 4, 8, 7)
    assert [p.average for p in recap.high] == [93.9, 93.7, 92.8]
    assert len(recap.grey) == 16 and all(p.role != "tanks" for p in recap.grey)
    assert [(d.deaths, d.first_deaths) for d in recap.deaths] == [(6, 3), (6, 1), (4, 0), (4, 0)]
    # Healers ranked on healing: on damage every one of them would be near the bottom.
    assert everyone(data, "healers") == [66.0, 37.9, 35.6, 32.5, 32.5, 28.4]


def test_retail_death_events_are_all_there():
    # The deaths *table* stops at 200; the events feed returned all 223 for this log.
    assert len(load("retail_heroic")["deaths"]) == 223


def test_classic_tbc():
    data = load("classic_tbc")
    recap = build_recap(data, RecapSettings())
    assert (recap.zone, recap.difficulty, recap.kills, recap.wipes) == ("ZA / SWP", 3, 7, 0)
    assert recap.high == []
    assert [(p.average, p.role) for p in recap.grey] == [
        (6.2, "healers"), (11.1, "dps"), (21.2, "dps"), (23.1, "healers"),
    ]
    # One player died twice; five more died once. The five-way tie isn't worth listing.
    assert [(d.deaths, d.first_deaths) for d in recap.deaths] == [(2, 2)]
    assert everyone(data, "tanks") == [35.6, 33.9]

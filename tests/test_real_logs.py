"""Real logs pulled through the API on 2026-09-18, names replaced by tools/make_fixture.py.

They pin the real JSON shape: if WCL changes it, or a change here breaks reading it, these fail.
guild_kill and guild_prog are one guild's consecutive nights (Sep 15 and 16), so they also test
history: "last raid's best" and "2 raids running".
"""

import json
from pathlib import Path

from bot.awards import boss_results, build_lines, winners_by_key
from bot.config import RecapSettings
from bot.recap import analyze
from bot.render import card_text, render_report
from bot.store import Store
from bot.wcl import ReportRef

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = RecapSettings()


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def text_of(night, history=None):
    lines = build_lines(night, SETTINGS, history)
    r = render_report(night, lines, "u", lambda c: None)
    return r, "\n".join(card_text(c) for c in [r.headline, *r.thread]), lines


def test_guild_kill_night():
    data = load("guild_kill")
    night = analyze(data, SETTINGS)
    # WCL lists 105 players (everyone seen in the log); only the 16 in boss pulls are the raid.
    assert len(data["masterData"]["actors"]) == 105 and len(night.roster) == 16
    assert (night.difficulty, night.kills, night.wipes) == (4, 7, 11)
    assert [p.average for p in night.high] == [95.9]  # refetched 2026-09-18 10:00; parses drift
    assert [(p.average, p.role) for p in night.grey] == [(20.3, "healers")]
    assert [(d.deaths, d.first_deaths) for d in night.floor] == [(11, 5), (9, 1), (7, 1), (7, 0)]

    r, text, _ = text_of(night)
    assert r.headline.subtitle.endswith("7 bosses down · 18 pulls · 2h 27m")
    assert "**📈 Ula'tek** 8 wipes · best P3 at 44%" in card_text(r.headline)
    assert "wiped with the boss at **1.5%** in P3" in text  # The Coiled Altar
    assert "**🦶 Kick captain**" in text and "17 interrupts (next best: 8)" in text
    assert "**🧼 Dispel machine**" in text and "177 dispels" in text
    assert "72M damage (3× the next healer) with a 20.3 healing parse" in text
    assert "**🎁 Couldn't wait for loot**" in text and "3 deaths each on kills" in text
    assert "Nemesis" not in text  # a three-way tie at 3: nobody stands out

    consumables = card_text(next(c for c in r.thread if c.title.startswith("🧪")))
    assert "every pull · " in consumables and " 13 of 18 · " in consumables and " 11 of 18" in consumables
    assert "23 combat potions each" in consumables
    assert "22 mana potions" in consumables
    assert "24 healthstones and health potions" in consumables
    assert " 14 of 17 pulls · " in consumables and " 10 of 18" in consumables  # hoarders
    assert "died 7 times · " in consumables and " twice" in consumables  # healthstone in the bag
    assert "**⚗️ No flask**" in consumables and " 4 of 17 pulls" in consumables
    assert ", all 4 pulls" in consumables  # six people skipped vantus on the 4 pulls the raid used it


def test_guild_prog_night():
    night = analyze(load("guild_prog"), SETTINGS)
    assert (night.kills, night.wipes, len(night.roster)) == (0, 18, 19)
    assert not night.has_parses
    r, text, _ = text_of(night)
    assert r.headline.title == "Prog report · Ula'tek" and r.headline.subtitle.startswith("Heroic")
    assert r.headline.blocks[0].startswith("**📈 Best pull** P3 at 5.1% · the last pull of the night")
    assert "**🏆 Top DPS** **P-" in r.headline.blocks[0] and "268k HPS" in r.headline.blocks[0]
    assert "`▇▅▅▄▅▅▃▄▃▃▇▂▂▂▂▇▅▁`" in text
    assert "reached P3 on 10 of 18 pulls" in text
    assert "(6 pulls)" in text and "(12 pulls)" in text  # people who sat out, not made to look bad
    assert "38 interrupts (next best: 21)" in text
    assert "9 battle rezzes" in text
    assert "died to Necrotic Vapors 6 times" in text

    consumables = card_text(next(c for c in r.thread if c.title.startswith("🧪")))
    assert "28 combat potions in 18 pulls" in consumables
    assert "38 healthstones and health potions" in consumables
    assert " 14 of 15 pulls · " in consumables and consumables.count("no potion of any kind all night") == 2
    assert "died 7 times · " in consumables and " 6 times · " in consumables
    assert " 5 of 15 pulls" in consumables  # forgot to eat
    assert " all 15 pulls · " in consumables and " all 6 pulls" in consumables  # no vantus


def test_consecutive_nights_build_history():
    store = Store(":memory:")
    for name in ("guild_kill", "guild_prog"):
        night = analyze(load(name), SETTINGS)
        r, text, lines = text_of(night, store)
        store.save_history(ReportRef("www.warcraftlogs.com", name), night.start_ms, boss_results(night),
                           winners_by_key(lines))
    headline = card_text(r.headline)
    assert "· last raid's best: P3 at 44%" in headline
    assert "10 deaths (2 raids running)" in headline  # floor inspector both nights
    assert "9 battle rezzes (2 raids running)" in text
    assert "(1.5× the next healer, 2 raids running)" in text


def test_classic_tbc():
    night = analyze(load("classic_tbc"), SETTINGS)
    assert (night.zone, night.difficulty, night.kills, night.wipes) == ("ZA / SWP", 3, 12, 0)
    assert len(night.roster) == 25
    assert [p.average for p in night.grey] == [4.8, 12.6, 19.4, 22.8, 24.2]
    r, text, _ = text_of(night)
    assert "12 bosses down" in r.headline.subtitle
    assert "**🧼 Dispel machine**" in text
    assert not night.retail and "Consumables" not in text  # Classic potions need their own list


def test_old_shape_without_new_fields_still_works():
    # An earlier fixture from before the report grew: no friendlyPlayers, tables or player details.
    night = analyze(load("retail_heroic"), SETTINGS)
    assert [p.average for p in night.high] == [93.9, 93.7, 92.8]
    assert len(night.grey) == 16
    r, text, _ = text_of(night)
    assert "Top DPS" in card_text(r.headline) and "Floor inspector" in text


def test_retail_death_events_are_all_there():
    # The deaths *table* stops at 200; the events feed returned all 223 for this log.
    assert len(load("retail_heroic")["deaths"]) == 223

"""The fun pass: deathless kills, WCL speed and execution ranks, prog milestones, biggest hit."""

import json
from pathlib import Path

from bot.awards import build_lines
from bot.charts import progress_chart
from bot.config import RecapSettings
from bot.recap import Boss, Char, Pull, _biggest_hit, _kill_ranks, analyze, single_cast_hits
from bot.render import card_text, render_report

from .sample_report import report

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = RecapSettings()


def rendered(data):
    night = analyze(data, SETTINGS)
    r = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    return night, card_text(r.headline), "\n".join(card_text(c) for c in r.thread)


# --- biggest hit -----------------------------------------------------------------------------

def hit(t, source, ability, amount, target=900, fight=1, crit=False):
    return {"type": "damage", "timestamp": t, "sourceID": source, "abilityGameID": ability, "amount": amount,
            "targetID": target, "fight": fight, "hitType": 2 if crit else 1}


def cast(t, source, ability):
    return {"type": "cast", "timestamp": t, "sourceID": source, "abilityGameID": ability}


def test_only_a_hit_straight_from_a_cast_counts():
    """A detonation of stored damage has no cast of its own just before it: Touch of the Magi's
    3.58M on the Sept 15 log was one, the 1.83M Prismatic Bolt a second earlier was a cast."""
    bolt, detonation = hit(10_000, 3, 1, 1_832_072), hit(11_000, 3, 2, 3_581_629)
    assert single_cast_hits([bolt, detonation], [cast(9_700, 3, 1)]) == [bolt]
    assert single_cast_hits([bolt], [cast(4_000, 3, 1)]) == []  # cast too long before: a delayed blast
    assert single_cast_hits([bolt], [cast(9_700, 4, 1)]) == []  # someone else's cast


def test_biggest_hit_is_the_biggest_single_cast_on_an_enemy():
    data = report()
    data["masterData"]["pets"] = [{"id": 50, "petOwner": 1}]
    data["masterData"]["npcs"] = [{"id": 900, "name": "Venomous Heart"}]
    data["masterData"]["abilities"] += [{"gameID": 1, "name": "Prismatic Bolt"}, {"gameID": 2, "name": "Bite"},
                                        {"gameID": 3, "name": "Touch of the Magi"}]
    data["bigHits"] = [hit(130_000, 3, 1, 1_000_000, crit=True), hit(131_000, 3, 3, 5_000_000),
                       hit(132_000, 50, 2, 1_200_000), hit(133_000, 3, 1, 9_000_000, target=5)]  # 5 is a raider
    data["bigHitCasts"] = [cast(129_500, 3, 1), cast(131_500, 50, 2), cast(132_500, 3, 1)]
    night = analyze(data, SETTINGS)
    big = night.biggest_hit
    # The pet's Bite wins and goes to its owner; the 5M detonation and the hit on a raider don't count.
    assert (big.char.name, big.ability, big.amount, big.target) == ("Tanky", "Bite", 1_200_000, "Venomous Heart")
    _, _, thread = rendered(data)
    assert "**💥 Biggest hit**\n-# On Venomous Heart, Boss A pull 1, " in thread
    assert "**Tanky** · Bite for 1,200,000" in thread


def test_no_big_hit_data_no_callout():
    data = report()
    assert analyze(data, SETTINGS).biggest_hit is None
    assert "💥" not in rendered(data)[2]


# --- speed and execution ---------------------------------------------------------------------

def with_ranks(data, ranks):
    for fight in data["dpsRankings"]["data"]:
        speed, execution = ranks.get(fight["fightID"], (None, None))
        fight["speed"] = {"rankPercent": speed}
        fight["execution"] = {"rankPercent": execution}
    return data


def test_kill_ranks_come_from_the_rankings():
    data = with_ranks(report(), {2: (57, 45), 3: (73, 90)})
    assert _kill_ranks(data) == {2: (57, 45), 3: (73, 90)}


def test_every_kill_shows_its_time_and_ranks_and_the_best_are_called_out():
    data = with_ranks(report(), {2: (57, 45), 3: (73, 90)})
    _, headline, thread = rendered(data)
    assert "✅ Boss A (2 pulls) · 3m 20s · ⚡ 57 · 🎯 45\n" in thread
    assert "✅ Boss B · 3m 20s · ⚡ 73 · 🎯 90 · 😇\n" in thread
    assert "-# ⚡ speed and 🎯 execution: WCL's rank for the kill against other logged kills" in thread
    assert "**⚡ Fastest kill** - Boss B · 3m 20s, speed rank 73" in thread
    assert "**🎯 Cleanest kill** - Boss B · 3m 20s, execution rank 90, nobody died" in thread
    assert "**😇 Deathless** - Boss B" in headline


def test_a_tie_names_both_kills():
    data = with_ranks(report(), {2: (60, 45), 3: (60, 90)})
    assert "**⚡ Fastest kill** - Boss A (3m 20s) and Boss B (3m 20s) · speed rank 60 each" in rendered(data)[2]


# --- prog milestones -------------------------------------------------------------------------

def pull(pid, progress, phase=1, kill=False):
    return Pull(pid, 1, "Boss", kill, 5, pid * 1000, pid * 1000 + 500, None if kill else progress, phase,
                frozenset(), fight_pct=None if kill else progress)


def test_new_bests_and_phase_firsts():
    boss = Boss(1, "Boss", 5, [pull(1, 76), pull(2, 58, 2), pull(3, 60, 2), pull(4, 39, 3), pull(5, 39, 3),
                               pull(6, 5.1, 3)])
    assert [p.id for p in boss.new_bests] == [2, 4, 6]  # the first pull sets the bar; a tie isn't a new best
    assert boss.phase_firsts == {2: 2, 4: 3}


def test_a_prog_night_tells_its_staircase():
    """Sept 16: 18 wipes on Ula'tek, seven new bests from 58% down to 5.1%."""
    night = analyze(json.loads((FIXTURES / "guild_prog.json").read_text(encoding="utf-8")), SETTINGS)
    r = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    assert "**🗺️ 7 new bests** - 58% → 39% → 33% → 28% → 22% → 13% → 5.1%" in card_text(r.headline)
    assert progress_chart(night, night.bosses[0]).startswith(b"\x89PNG")

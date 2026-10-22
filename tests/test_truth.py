"""Every number a callout prints has to be the number it claims to be.

Each test here is a falsehood an audit against raw WCL events found in a real report, pinned so it
can't come back.
"""

import json
from pathlib import Path

from bot.awards import build_lines, consumable_rows
from bot.config import RecapSettings
from bot.recap import CLASS_IMBUES, Char, Death, _counted, _resurrects, analyze
from bot.render import card_text, render_report
from bot.wcl import WCLClient, WCLError

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = RecapSettings()
A, B, C = Char("A", "R"), Char("B", "R"), Char("C", "R")


def load(name):
    return analyze(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")), SETTINGS)


def thread(night):
    rendered = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    return card_text(rendered.headline) + "\n" + "\n".join(card_text(c) for c in rendered.thread)


# --- deaths that count -----------------------------------------------------------------------

def deaths(*rows):
    return [Death(1, t, char, None) for t, char in rows]


def test_deaths_in_the_same_instant_as_the_last_counted_one_all_count():
    """Two raiders died at the same millisecond as 5th and 6th; the order WCL wrote them in
    decided which one was blamed."""
    pull = deaths((1, A), (2, A), (3, A), (4, A), (5, B), (5, C), (9, A))
    assert [d.char for d in _counted(pull, 5, kill=False)] == [A, A, A, A, B, C]


def test_every_death_on_a_kill_counts():
    """There's no wipe to call on a kill, so a 6th death there used to vanish from the floor."""
    pull = deaths(*[(t, A) for t in range(1, 8)])
    assert len(_counted(pull, 5, kill=True)) == 7
    assert len(_counted(pull, 5, kill=False)) == 5


def test_a_death_line_never_prints_less_than_everything_as_everything():
    text = thread(load("guild_kill"))
    assert "-# Every death on a kill, and the first 5 of each wipe" in text  # floor inspector's note
    assert "**P-04d994** died 15 times" in text  # healthstone bag: 15 deaths, 8 of them counted


def test_raids_nemesis_gives_the_real_total():
    """It printed "Mother's Wrath · 9 deaths" when Mother's Wrath killed 29."""
    night = load("guild_kill")
    every = sum(1 for ds in night.deaths.values() for d in ds if d.ability == "Mother's Wrath")
    counted = sum(1 for ds in night.counted.values() for d in ds if d.ability == "Mother's Wrath")
    assert (counted, every) == (10, 29)
    assert ("**☠️ Raid's nemesis**\n-# Every death on a kill, and the first 5 of each wipe\n"
            "Mother's Wrath · 10 deaths (29 in all)") in thread(night)


def test_wipe_starter_counts_wipes_only():
    night = load("guild_kill")
    kills = {p.id for p in night.pulls if p.kill}
    text = thread(night)
    if "🧨 Wipe starter" in text:
        line = text.split("🧨 Wipe starter**\n")[1].split("\n")[0]
        n = int(line.split("first death on ")[1].split(" ")[0])
        assert n <= len(night.pulls) - len(kills) and line.endswith(" wipes")


# --- weapon oil ------------------------------------------------------------------------------

def test_a_class_imbue_is_not_weapon_oil():
    """Flametongue, Earthliving and the paladin rites sit in the same slot as oil; they were being
    drawn as "oil every pull" for raiders who used none."""
    assert {5400, 6498, 7143} <= CLASS_IMBUES
    night = load("gear_night")
    for row in consumable_rows(night, SETTINGS):
        if night.specs.get(row.char) in (264, 66):  # resto shaman, protection paladin
            assert row.oil_pulls == 0 and "no_oil" not in row.flags


# --- idle ------------------------------------------------------------------------------------

def test_active_time_is_wcls_own_number_and_only_for_the_living():
    """WCL's active time keeps running after a death (pets, damage over time), so "active X% of
    your time alive" came out over 100% and got capped. Now it's active time over time in pulls,
    for raiders alive at least 95% of it, which is exactly WCL's Active %."""
    night = load("gear_night")
    length = {p.id: p.end - p.start for p in night.pulls}
    for char, share in night.active.items():
        in_pulls = sum(length[i] for i in night.pulls_in[char])
        assert night.dead_seconds.get(char, 0) * 1000 <= 0.05 * in_pulls
        assert 0 < share <= 1


# --- rezzes ----------------------------------------------------------------------------------

def test_a_self_rez_isnt_a_rez_given():
    players = {1: A, 2: B}
    report = {"resurrects": [{"fight": 1, "sourceID": 1, "targetID": 1},
                             {"fight": 1, "sourceID": 2, "targetID": 1}]}
    brezzed, given = _resurrects(report, players, {1})
    assert brezzed[A] == 2 and given[B] == 1 and given[A] == 0


# --- errors ----------------------------------------------------------------------------------

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _Http:
    def __init__(self, code):
        self.code = code

    async def post(self, *args, **kwargs):
        return _Resp(self.code)


def test_an_outage_isnt_blamed_on_the_credentials():
    import asyncio

    async def login(code):
        client = WCLClient.__new__(WCLClient)
        client._tokens, client._http = {}, _Http(code)
        client._credentials = lambda host: ("id", "secret")
        try:
            await client._token("www.warcraftlogs.com")
        except WCLError as err:
            return str(err)

    assert "Check the client ID/secret" in asyncio.run(login(401))
    down = asyncio.run(login(503))
    assert "unavailable" in down and "secret" not in down


# --- the night itself ------------------------------------------------------------------------

def _pull(pid, start_h, end_h, kill=True, boss_pct=None, fight_pct=None, phase=None):
    from bot.recap import Pull
    hour = 3600 * 1000
    return Pull(pid, 1, "Boss", kill, 5, int(start_h * hour), int(end_h * hour),
                0.0 if kill else boss_pct, phase, frozenset(), fight_pct=0.0 if kill else fight_pct)


def test_a_log_spanning_two_raid_nights_isnt_one_long_night():
    """A rolling log covered Sept 22 and 23; the report called it "a 26h 51m night"."""
    from bot.recap import Night
    from bot.render import night_length
    pulls = [_pull(1, 0, 1), _pull(2, 1.5, 3), _pull(3, 24, 26)]  # 21 hours between nights
    night = Night("Log", None, 1_000_000, False, pulls, [], [], {}, [], [], {}, {}, [], 0,
                  None, None, {}, {}, None, None)
    assert [len(n) for n in night.sessions] == [2, 1]
    assert night.span_seconds == 5 * 3600  # 3h + 2h of raiding, not 26h
    assert night_length(night) == "5h 00m over 2 raid nights"


def test_best_pull_follows_fight_progress_not_boss_health_alone():
    """On a boss whose health resets between phases, boss health ranks a phase 1 wipe at 30%
    above a phase 3 wipe at 44%."""
    from bot.recap import Boss
    early = _pull(1, 0, 1, kill=False, boss_pct=30.0, fight_pct=80.0, phase=1)
    late = _pull(2, 1, 2, kill=False, boss_pct=44.0, fight_pct=15.0, phase=3)
    assert Boss(1, "Boss", 5, [early, late]).best_wipe is late


def test_a_parse_is_printed_with_its_decimals_dropped_like_wcl():
    """Rounding printed a 98.6 as an orange 99; WCL and the parse grid show 98."""
    import inspect
    from bot import awards, mynight
    for source in (inspect.getsource(awards.Builder.highlights), inspect.getsource(awards.Builder.lowlights),
                   inspect.getsource(mynight._performance)):
        assert ":.0f} on" not in source and "{pct:.0f}" not in source and "lo:.0f" not in source

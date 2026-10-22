"""Weapon oil in the consumables grid, and the idle-time callout."""

import json
from pathlib import Path

from bot.awards import build_lines, consumable_rows, winners_by_key
from bot.charts import _consumable_cells, draw_charts
from bot.config import RecapSettings
from bot.mynight import my_night_card
from bot.recap import HEALER, analyze
from bot.render import card_text, render_report

SETTINGS = RecapSettings()
FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return analyze(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")), SETTINGS)


def thread_text(night):
    rendered = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    return "\n".join(card_text(c) for c in rendered.thread)


# --- weapon oil ----------------------------------------------------------------------------------

def test_weapon_oil_is_read_from_the_main_hand_every_pull():
    rows = consumable_rows(load("gear_night"), SETTINGS)
    oiled = sorted((r.oil_pulls - r.oil) for r in rows)
    assert oiled[-1] == 5 and oiled[:-1] == [0] * (len(rows) - 1)  # one raider skipped it on 5 pulls
    [bare] = [r for r in rows if "no_oil" in r.flags]
    assert (bare.oil, bare.oil_pulls) == (13, 18)


def test_no_weapon_oil_callout_and_grid_column_agree():
    night = load("gear_night")
    rows = consumable_rows(night, SETTINGS)
    winners = winners_by_key(build_lines(night, SETTINGS))
    assert {r.char for r in rows if "no_oil" in r.flags} == set(winners["no_oil"])
    assert "**🛢️ No weapon oil** - " in thread_text(night) and " 5 of 18 pulls" in thread_text(night)
    cells = {r.char: _consumable_cells(r, True, True, show_oil=True) for r in rows}
    bare = winners["no_oil"][0]
    assert cells[bare][2] == ("13/18", "warn")  # flask, food, then weapon oil
    # Everyone else oiled every pull, except the specs whose class imbue fills the slot
    # (resto shaman, protection paladin): oil can't go on, so their column says so rather than
    # crediting the imbue as oil.
    imbued = {r.char for r in rows if r.oil_pulls == 0}
    assert {night.specs[c] for c in imbued} == {264, 66}
    assert all(cells[c][2] == ("–", "dim") for c in imbued)
    assert all(cells[r.char][2][1] == "ok" for r in rows if r.char != bare and r.char not in imbued)
    assert draw_charts(night, {"consumables"}, SETTINGS)["consumables"].startswith(b"\x89PNG")


def test_logs_without_gear_have_no_oil_column_or_callout():
    night = load("retail_heroic")  # saved before gear was kept
    assert all(r.oil_pulls == 0 and "no_oil" not in r.flags for r in consumable_rows(night, SETTINGS))
    assert "No weapon oil" not in thread_text(night)


# --- idle ------------------------------------------------------------------------------------------

def test_idle_time_leaves_healers_out():
    night = load("gear_night")
    assert night.active and not any(night.roles.get(c) == HEALER for c in night.active)
    assert all(0 < share <= 1 for share in night.active.values())


def test_idle_callout_on_a_kill_night():
    text = thread_text(load("gear_night"))
    # WCL's active time over time in boss pulls, judged only for those alive 95% of it: here one
    # raider, 983 s active in 1,189 s of pulls (alive 98.8% of them).
    assert ("**💤 Idle**\n-# WCL active time over time in boss pulls, for DPS and tanks alive for 95% "
            "of them. The raid's usual is 93%\n**P-31a3b4** 83%\n") in text


def test_prog_night_movement_doesnt_make_everyone_idle():
    # Everyone's lower on a wipe-heavy prog night; nobody is 10 points under the raid's usual.
    assert "💤 Idle" not in thread_text(load("guild_prog"))


def test_my_night_shows_active_time_and_oil():
    night = load("gear_night")
    lines = build_lines(night, SETTINGS)
    idle = winners_by_key(lines)["idle"][0]
    text = card_text(my_night_card(night, lines, idle, SETTINGS, "u"))
    assert "⏱️ active 83% of your time in boss pulls (raid: 93%)" in text
    bare = winners_by_key(lines)["no_oil"][0]
    assert "⚠️ Weapon oil 13/18" in card_text(my_night_card(night, lines, bare, SETTINGS, "u"))

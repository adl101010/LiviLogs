"""Flasks and food that ran out mid-pull, and weapon oil that ran out between pulls."""

import json
from pathlib import Path

from bot.awards import build_lines, consumable_rows, winners_by_key
from bot.config import RecapSettings
from bot.mynight import my_night_card
from bot.recap import analyze
from bot.render import card_text, render_report
from bot.wcl import _buff_end_filter

from .sample_report import report

SETTINGS = RecapSettings()
FIXTURES = Path(__file__).parent / "fixtures"


def consumables_text(night):
    rendered = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    return card_text(next(c for c in rendered.thread if c.title.startswith("🧪")))


def test_a_buff_lost_with_a_death_doesnt_count():
    night = analyze(report(), SETTINGS)
    gone = [(r.char.name, r.kind, r.pull) for r in night.ran_out]
    assert ("Dyer", "food", 1) not in gone  # it came off 50 ms after Dyer died
    assert ("Pumper", "flask", 6) in gone


def test_buffs_that_come_off_outside_a_boss_pull_dont_count():
    data = report()
    data["buffEnds"][0]["fight"] = 4  # trash
    assert not [r for r in analyze(data, SETTINGS).ran_out if r.kind == "flask"]


def test_a_few_raiders_food_is_listed_by_name():
    data = report()
    data["buffEnds"] = [e for e in data["buffEnds"] if e["targetID"] in (1, 2) or e["fight"] != 6]
    text = consumables_text(analyze(data, SETTINGS))
    assert "Raid food" not in text
    assert "**Tanky** food · Boss C pull 2, 2m 31s in" in text


def test_real_night():
    night = analyze(json.loads((FIXTURES / "gear_night.json").read_text(encoding="utf-8")), SETTINGS)
    text = consumables_text(night)
    assert "**⌛ Ran out mid-pull**\n-# Flask or food that expired during a boss pull, and weapon oil between pulls\n" in text
    assert " weapon oil · none from Ula'tek pull 4 onwards (5 pulls)\n" in text
    assert "-# That's everyone: 1 flask expired mid-pull · 14 raiders lost their food · " \
           "1 raider lost their weapon oil" in text
    assert " flask · Ula'tek pull 4, 2m 01s in\n" in text
    # Hearty food from one feast ran out on most of the raid 3 minutes into the same pull. The
    # regular food lost when people died on the last wipe isn't counted.
    assert "Raid food ran out for 14 raiders · Ula'tek pull 4, about 3m 10s in" in text
    assert len(night.ran_out) == 15


def test_my_night_lists_your_own():
    night = analyze(report(), SETTINGS)
    lines = build_lines(night, SETTINGS)
    pumper = next(c for c in night.roster if c.name == "Pumper")
    text = card_text(my_night_card(night, lines, pumper, SETTINGS, "u"))
    assert "⌛ Flask ran out mid-pull · Boss C pull 2, 1m 40s in" in text
    assert "⌛ Food ran out mid-pull · Boss C pull 2, 2m 33s in" in text


def test_oil_that_ran_out_between_pulls():
    night = analyze(json.loads((FIXTURES / "gear_night.json").read_text(encoding="utf-8")), SETTINGS)
    [row] = [r for r in consumable_rows(night, SETTINGS) if r.oil_gone_pull]
    assert "no_oil" in row.flags
    assert row.char in winners_by_key(build_lines(night, SETTINGS))["ran_out"]


def test_the_query_asks_only_for_this_logs_flask_and_food_buffs():
    data = {"combatantInfo": [{"auras": [{"name": "Flask of the Magisters", "ability": 11},
                                         {"name": "Hearty Well Fed", "ability": 22},
                                         {"name": "Arcane Intellect", "ability": 33}]}]}
    assert _buff_end_filter(data) == "type = 'removebuff' and ability.id in (11, 22)"
    assert _buff_end_filter({"combatantInfo": []}) is None

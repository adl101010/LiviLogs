"""Gear check: enchants on every pull, new loot left bare, empty sockets, and what gets called out.

gear_night is a real retail night (anonymised) with full gear snapshots. Edge cases are made by
editing its snapshots, so each test starts from real item data.
"""

import copy
import json
from pathlib import Path

from bot.awards import build_lines
from bot.charts import draw_charts
from bot.config import RecapSettings
from bot.gear import FINGER_2, HEAD, MAIN_HAND, OFF_HAND, gear_rows
from bot.mynight import my_night_card
from bot.recap import analyze
from bot.render import Chart, card_text, render_report

SETTINGS = RecapSettings()
RAW = json.loads((Path(__file__).parent / "fixtures" / "gear_night.json").read_text(encoding="utf-8"))
PNG = b"\x89PNG\r\n\x1a\n"


def night(edit=None):
    data = copy.deepcopy(RAW)
    if edit:
        edit(data)
    return analyze(data, SETTINGS)


def rows_by_name(n):
    return {r.char.name: r for r in gear_rows(n, SETTINGS)}


def gear_text(n):
    lines = build_lines(n, SETTINGS)
    rendered = render_report(n, lines, "u", lambda c: None)
    return card_text(next(c for c in rendered.thread if c.title == "🛠️ Gear check"))


def edit_gear(data, name, slot, change):
    """Apply `change` to one player's item in one slot, on every pull."""
    pid = next(a["id"] for a in data["masterData"]["actors"] if a["name"] == name)
    for e in data["combatantInfo"]:
        if e["sourceID"] == pid:
            change(e["gear"][slot], e)


def someone(state):
    """The name of a raider whose gear is in `state` on the real night, for tests that need one."""
    for r in gear_rows(night(), SETTINGS):
        if any(c.state == state for c in r.checks):
            return r.char.name
    raise AssertionError(state)


def test_real_night_finds_missing_enchants_and_new_loot():
    rows = gear_rows(night(), SETTINGS)
    assert len(rows) == 16 and sum(r.ready for r in rows) == 11
    missing = sorted(len(r.missing) for r in rows if r.missing)
    assert missing == [1, 1, 1, 3]  # three raiders missing one enchant, one missing three
    [unwrapped] = [r for r in rows if r.unwrapped]
    [check] = unwrapped.unwrapped
    assert (check.state, check.bare_pulls, check.boss) == ("new", 11, "The Coiled Altar")


def test_the_card_calls_out_whoever_isnt_ready():
    text = gear_text(night())
    assert "11 of 16 raiders fully enchanted" in text  # the chart's stand-in text
    assert "**🔧 Missing enchants**" in text and " helm, shoulders, boots · " in text
    assert "**🎁 Unwrapped loot**" in text and " new boots from The Coiled Altar, bare for 11 pulls" in text
    assert "Empty sockets" not in text  # nobody had one


def test_shields_and_held_items_are_never_counted():
    rows = gear_rows(night(), SETTINGS)
    # Healers and a tank carry off-hand items nobody can enchant: only the main hand is checked.
    for r in rows:
        weapons = dict(r.columns)["Weapons"]
        if r.role != "dps":
            assert [c.slot for c in weapons] == [MAIN_HAND]
    # The two dual-wielders (Frost Death Knights) have both hands checked.
    assert sum(1 for r in rows if len(dict(r.columns)["Weapons"]) == 2) == 2


def test_one_missing_ring_is_its_own_half():
    name = someone("ok")

    def bare_ring(data):
        edit_gear(data, name, FINGER_2, lambda item, e: item.pop("permanentEnchant", None))

    n = night(bare_ring)
    row = rows_by_name(n)[name]
    assert [c.state for c in dict(row.columns)["Rings"]] == ["ok", "missing"]
    assert f"**{name}** one ring" in gear_text(n)


def test_an_off_hand_someone_enchanted_counts_whatever_the_spec():
    # A spec the bot doesn't know dual-wields still gets its off hand checked once it's enchanted.
    dk = next(r.char.name for r in gear_rows(night(), SETTINGS) if len(dict(r.columns)["Weapons"]) == 2)

    def unknown_spec(data):
        edit_gear(data, dk, OFF_HAND, lambda item, e: e.update(specID=999999))

    assert len(dict(rows_by_name(night(unknown_spec))[dk].columns)["Weapons"]) == 2


def test_enchanting_mid_raid_isnt_called_out():
    name = someone("ok")

    def enchant_late(data):
        pid = next(a["id"] for a in data["masterData"]["actors"] if a["name"] == name)
        first = min((e for e in data["combatantInfo"] if e["sourceID"] == pid), key=lambda e: e["fight"])
        first["gear"][HEAD].pop("permanentEnchant", None)

    n = night(enchant_late)
    [helm] = dict(rows_by_name(n)[name].columns)["Helm"]
    assert (helm.state, helm.from_pull) == ("late", 2)
    callouts = gear_text(n).split("Missing enchants")[-1].split("-# Not linked")[0]
    assert name not in callouts


def test_empty_sockets_are_called_out():
    name = someone("ok")

    def pop_gems(data):
        socketed = next(i for i, item in enumerate(next(e for e in data["combatantInfo"]
                                                         if e["sourceID"] == _id(data, name))["gear"])
                        if 13668 in (item.get("bonusIDs") or []))
        edit_gear(data, name, socketed, lambda item, e: item.update(gems=[]))

    n = night(pop_gems)
    assert rows_by_name(n)[name].empty_sockets == 1
    assert f"**💎 Empty sockets**\n**{name}** 1 empty socket" in gear_text(n)


def _id(data, name):
    return next(a["id"] for a in data["masterData"]["actors"] if a["name"] == name)


def test_a_fully_ready_raid_gets_a_thumbs_up():
    def fix_everything(data):
        for e in data["combatantInfo"]:
            for item in e["gear"]:
                if item.get("id"):
                    item["permanentEnchant"] = 1

    text = gear_text(night(fix_everything))
    assert "16 of 16 raiders fully enchanted" in text
    assert "✅ Everyone was fully enchanted and gemmed. Nice." in text
    assert "Missing enchants" not in text


def test_gear_chart_replaces_only_the_summary():
    n = night()
    lines = build_lines(n, SETTINGS)
    charts = draw_charts(n, {"gear"}, SETTINGS)
    assert charts["gear"].startswith(PNG)
    card = next(c for c in render_report(n, lines, "u", lambda c: None, charts=charts).thread
                if c.title == "🛠️ Gear check")
    assert isinstance(card.blocks[0], Chart) and card.blocks[0].filename == "gear.png"
    assert "Missing enchants" in card.blocks[1]  # the callouts stay, so people still get pinged


def test_no_gear_data_no_card():
    from .sample_report import report

    n = analyze(report(), SETTINGS)  # the hand-built report has no gear snapshots
    assert gear_rows(n, SETTINGS) == []
    assert not [line for line in build_lines(n, SETTINGS) if line.section == "gear"]


def test_my_night_has_a_gear_line():
    n = night()
    rows = gear_rows(n, SETTINGS)
    worst = max(rows, key=lambda r: len(r.missing))
    ready = next(r for r in rows if r.ready)
    lines = build_lines(n, SETTINGS)
    assert "**🛠️ Gear**\n⚠️ missing enchants: helm, shoulders, boots" in \
        card_text(my_night_card(n, lines, worst.char, SETTINGS, "u"))
    assert f"**🛠️ Gear** · ✅ fully enchanted · {ready.gems} gems" in \
        card_text(my_night_card(n, lines, ready.char, SETTINGS, "u"))
    new = next(r for r in rows if r.unwrapped)
    assert "⚠️ new boots unenchanted for 11 pulls" in card_text(my_night_card(n, lines, new.char, SETTINGS, "u"))


def test_every_socket_bonus_id_really_grants_a_socket():
    """A bonus id earns its place only if no wearer of it ever goes ungemmed.

    13454 was on the list and accused three raiders in a real log of an empty socket; every gemmed
    item carrying it also carried 13695, so it grants nothing. This is that check, over real gear.
    """
    for name in ("mixed_night", "two_difficulties", "gear_night"):
        data = json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
        n = analyze(data, SETTINGS)
        for pull in n.gear_at_pull.values():
            for items in pull.values():
                for item in items:
                    if item and not item.gems:
                        assert not item.bonus_ids & set(SETTINGS.socket_bonus_ids), \
                            (name, item.bonus_ids & set(SETTINGS.socket_bonus_ids))

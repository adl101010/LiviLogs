from dataclasses import replace
from zoneinfo import ZoneInfo

from bot.awards import build_lines, fmt_health
from bot.config import RecapSettings
from bot.recap import Char, DeathLine, _top_with_ties, analyze, norm_name, norm_realm
from bot.render import Card, card_text, render_report, split_card, split_message

from .sample_report import report

SETTINGS = RecapSettings()
URL = "https://www.warcraftlogs.com/reports/AbCdEf1234567890"


def names(lines):
    return [line.char.name for line in lines]


def full_text(data=None, settings=SETTINGS, links=None, tz=ZoneInfo("UTC")):
    night = analyze(data or report(), settings)
    lines = build_lines(night, settings)
    links = links or {}
    return render_report(night, lines, URL, lambda c: links.get(c.name), tz)


def all_text(rendered):
    return "\n".join(card_text(card) for card in [rendered.headline, *rendered.thread])


# --- analysis ------------------------------------------------------------------------------------


def test_roster_is_only_people_in_boss_pulls():
    night = analyze(report(), SETTINGS)
    assert "Bystander" not in names_of(night.roster)
    assert len(night.roster) == 6


def names_of(chars):
    return [c.name for c in chars]


def test_averages_are_per_night():
    night = analyze(report(), SETTINGS)
    assert [(p.char.name, p.average, p.kills) for p in night.high] == [("Pumper", 94.5, 2), ("Middling", 90.0, 2)]


def test_healers_use_healing_parses_and_others_use_damage():
    night = analyze(report(), SETTINGS)
    by_name = {p.char.name: p for p in night.parses}
    assert by_name["Healz"].average == 65.0  # 60 and 70 healing, not 3 damage
    assert by_name["Tanky"].average == 6.5  # damage, not the 99 in the healing table
    assert by_name["Greyson"].average == 20.0  # "-" in the healing table is ignored


def test_grey_excludes_tanks_by_default():
    assert names(analyze(report(), SETTINGS).grey) == ["Greyson"]
    assert names(analyze(report(), replace(SETTINGS, grey_include_tanks=True)).grey) == ["Tanky", "Greyson"]


def test_deaths_respect_wipe_cutoff():
    night = analyze(report(), SETTINGS)
    # Fight 1 (cutoff 5): Dyer, Dyer, Greyson, Healz, Pumper count; Middling (6th) doesn't.
    # Fight 2: Greyson first, then Dyer. Trash and the pet are ignored.
    assert [(d.char.name, d.deaths, d.first_deaths) for d in night.floor] == [
        ("Dyer", 3, 1), ("Greyson", 2, 1), ("Healz", 1, 0), ("Pumper", 1, 0),
    ]
    assert len(night.deaths[1]) == 6 and len(night.counted[1]) == 5


def test_trash_deaths_only_when_asked():
    night = analyze(report(), replace(SETTINGS, deaths_include_trash=True, deaths_top_n=10))
    assert "Tanky" in [d.char.name for d in night.floor]


def _deaths(*counts):
    return [DeathLine(Char(f"P{i}", "R"), n, 0) for i, n in enumerate(counts)]


def test_ties_at_the_bottom_of_the_floor_list():
    assert [d.deaths for d in _top_with_ties(_deaths(2, 1, 1, 1, 1, 1), 3)[0]] == [2]  # big tie dropped
    assert [d.deaths for d in _top_with_ties(_deaths(6, 6, 4, 4, 1), 3)[0]] == [6, 6, 4, 4]  # small tie kept
    shown, more = _top_with_ties(_deaths(1, 1, 1, 1, 1, 1, 1), 3)
    assert len(shown) == 3 and more == 4


def test_pulls_bosses_and_progress():
    night = analyze(report(), SETTINGS)
    assert (night.kills, night.wipes, night.difficulty) == (2, 3, 4)
    boss_c = next(b for b in night.bosses if b.name == "Boss C")
    assert not boss_c.killed and boss_c.best_wipe.boss_pct == 30 and boss_c.best_wipe.phase == 2


def test_time_spent_dead_stops_at_a_battle_rez_or_the_end_of_the_pull():
    night = analyze(report(), SETTINGS)
    dead = {c.name: round(s, 1) for c, s in night.dead_seconds.items()}
    # Greyson: dead 40.2 s -> 100 s on Boss A's wipe, then 201 s -> 400 s on the kill.
    assert dead["Greyson"] == round((100_000 - 40_200 + 400_000 - 201_000) / 1000, 1)
    # Dyer died twice on the wipe without a rez in between: only the first death counts. Then a
    # battle rez at 250 s cut the second pull short.
    assert dead["Dyer"] == round((100_000 - 40_050 + 250_000 - 201_100) / 1000, 1)


def test_power_infusion_ignores_priests_on_themselves():
    night = analyze(report(), SETTINGS)
    assert {(g.name, r.name): n for (g, r), n in night.power_infusion.items()} == {("Healz", "Pumper"): 4}


def test_rates_are_per_second_of_the_pulls_each_player_was_in():
    night = analyze(report(), SETTINGS)
    rates = {r.char.name: r for r in night.rates}
    assert rates["Pumper"].per_second == 90_000_000 / 800  # 5 boss pulls, 800 s
    assert rates["Healz"].per_second == 50_000_000 / 800  # healers: healing, not damage
    data = report()
    data["fights"][4]["friendlyPlayers"] = [1, 2, 4, 5, 6]  # Pumper sat out Boss C's first pull
    rates = {r.char.name: r for r in analyze(data, SETTINGS).rates}
    assert rates["Pumper"].pulls == 4 and rates["Pumper"].per_second == 90_000_000 / 700


def test_roles_come_from_player_details_even_without_parses():
    data = report()
    data["dpsRankings"] = data["hpsRankings"] = {"data": []}
    night = analyze(data, SETTINGS)
    assert night.roles[Char("Tanky", "Area 52")] == "tanks"
    assert night.roles[Char("Healz", "Area 52")] == "healers"


def test_processing_flag():
    data = report()
    data["exportedSegments"] = 2
    assert analyze(data, SETTINGS).processing


def test_missing_fields_do_not_crash():
    night = analyze({}, SETTINGS)
    assert night.kills == 0 and not night.has_parses and night.floor == []
    assert build_lines(night, SETTINGS) == []


def test_realm_filled_from_master_data_when_rankings_lack_it():
    data = report()
    for fight in data["dpsRankings"]["data"] + data["hpsRankings"]["data"]:
        for role in fight["roles"].values():
            for c in role["characters"]:
                c.pop("server", None)
    assert analyze(data, SETTINGS).high[0].char == Char("Pumper", "Area 52")


def test_name_and_realm_normalising():
    assert norm_name("Tøm") == norm_name("tom")
    assert norm_name("Élune") == "elune"
    assert norm_realm("Area 52") == norm_realm("area52")
    assert norm_realm("Azjol-Nerub") == norm_realm("AzjolNerub")
    assert norm_realm("Kel'Thuzad") == "kelthuzad"


# --- the report ----------------------------------------------------------------------------------


def test_headline_card():
    r = full_text(links={"Pumper": 111, "Greyson": 222})
    card = r.headline
    assert card.title == "Raid report · Liberation of Undermine"
    assert card.subtitle == "Heroic · <t:1758070800:D> · 2 bosses down · 5 pulls · 20m"
    assert card.accent == 0xF0B232  # gold for a kill night
    assert card.button == ("View log on Warcraft Logs", URL)
    assert card.blocks == [
        "**🏆 Top DPS** - <@111> 94.5\n"
        "**💚 Top healer** - **Healz** 65.0\n"
        "**🛡️ Top tank** - **Tanky** 6.5\n"
        "**📈 Boss C** - 2 wipes · best P2 at 30%",
        "**🌟 90+** - <@111> damage 94.5 · **Middling** damage 90.0\n"
        "**🗑️ Grey** - <@222> damage 20.0\n"
        "**💀 Floor inspector** - **Dyer** 3 deaths",
    ]
    assert card.footer == "🧵 Full report in the thread"


def test_thread_cards_in_order_with_their_colours():
    r = full_text()
    assert [(c.title, c.accent) for c in r.thread] == [
        ("🗺️ The night", 0x5865F2), ("📊 Parses", 0xA335EE), ("🌟 Highlights", 0x2ECC71),
        ("🤡 Lowlights", 0xED4245), ("💀 Deaths", 0x99AAB5), ("🧪 Consumables", 0x1ABC9C),
    ]


def test_the_night():
    card = full_text().thread[0]
    assert card.blocks[0] == (
        "✅ Boss A (2 pulls) · ✅ Boss B · ❌ Boss C (2 wipes)\n"
        "-# Bosses without a count died on the first pull · 13m on bosses across a 20m night"
    )
    assert card.blocks[1] == "**📉 Boss C progress**\nbest P2 at 30%"
    assert card.blocks[2] == "**💔 Heartbreaker**\nBoss A wiped with the boss at **8%** in P2 before going down"


def test_leaderboard_one_block_per_role():
    card = full_text().thread[1]
    assert card.blocks == [
        "**⚔️ Damage**\n🟪 - **Pumper** **94.5** 👑\n🟪 - **Middling** 90.0\n🟩 - **Dyer** 45.0\n⬜ - **Greyson** 20.0",
        "**💚 Healing**\n🟦 - **Healz** **65.0** 👑",
        "**🛡️ Tanks**\n⬜ - **Tanky** **6.5** 👑",
    ]


def test_awards():
    text = all_text(full_text())
    assert "**🩷 Pink parse** - **Pumper** · 99 on Boss A" in text
    assert "**🦶 Kick captain** - **Pumper** · 6 interrupts (next best: 2)" in text
    assert "**🧼 Dispel machine** - **Healz** · 12 dispels" in text
    assert "**🪄 Necromancer** - **Healz** · 3 battle rezzes" in text
    assert "**🚽 Parse of shame** - **Greyson** · 10 on Boss A" in text
    assert "**🧽 Damage sponge** - **Dyer** · 100M damage taken" in text
    assert "**🛡️ Outdamaged by a tank** - **Greyson** did less damage than **Tanky**" in text
    assert "**🎯 Nemesis** - **Dyer** died to Fire 3 times" in text
    assert "**🧲 Brez magnet** - **Dyer** · rezzed 3 times" in text
    assert "**👻 Ghost** - **Greyson** · spent 4m 19s dead" in text
    assert "**💜 Power Infusion**\n-# Who got it, most to least, all from Healz\n**Pumper** 4" in text
    assert "**💀 Floor inspector**\n-# Deaths before the wipe was called\n**Dyer** 3 · **Greyson** 2" in text
    # Nothing worth saying tonight: these stay silent rather than print a weak line.
    for quiet in ("Metronome", "Rollercoaster", "Canary", "Couldn't wait for loot",
                  "Speedrunner", "Last one standing", "Wall of the night", "Raid's nemesis", "Wipe starter"):
        assert quiet not in text, quiet


def test_consumables_card():
    card = full_text().thread[-1]
    assert card.blocks == [
        "**🔮 Tryhards**\n-# Void-Touched rune\n**Pumper** every pull · **Middling** 3 of 5\n\n"
        "**🍺 Potion seller** - **Pumper** · 6 combat potions in 5 pulls\n"
        "**🫗 Mana chugger** - **Healz** · 4 mana potions\n"
        "**🍪 Cookie monster** - **Middling** · 10 healthstones and health potions",
        # Tanky drank none; Greyson one in 5. Healz drank mana potions, so isn't a hoarder. Dyer
        # died 3 times but used healthstones; Greyson died twice and used nothing (Fortifying
        # Brew is a class ability, not a consumable).
        "**🧪 Potion hoarders**\n-# No combat potion on most of their pulls (healers: no potion of any kind)\n"
        "**Tanky** not a single one all night · **Greyson** 4 of 5 pulls\n\n"
        "**🪦 Died with a healthstone in the bag**\n-# Not one healthstone or health potion all night\n"
        "**Greyson** died twice",
        # Only Boss C's pulls count for vantus: nobody used one on A or B.
        "**⚗️ No flask** - **Greyson** 2 of 5 pulls\n**🍗 Forgot to eat** - **Dyer** 2 of 5 pulls\n\n"
        "**📜 No vantus**\n-# On pulls where most of the raid had one\n**Greyson** all 2 pulls",
        # Dyer's food came off with a death, so it isn't here; five raiders' food going at once is
        # one feast wearing off.
        "**⌛ Ran out mid-pull**\n-# Flask or food that expired during a boss pull\n"
        "**Pumper** flask · Boss C pull 2, 1m 40s in\n"
        "Raid food ran out for 5 raiders · Boss C pull 2, about 2m 30s in\n"
        "-# That's everyone: 1 flask expired mid-pull · 5 raiders lost their food",
    ]


def test_healer_with_no_potions_of_any_kind_is_a_hoarder():
    data = report()
    data["manaPotions"] = []
    assert "**Healz** no potion of any kind all night" in all_text(full_text(data))


def test_consumables_are_retail_only():
    data = report()
    for key in ("damageDone", "healing", "damageTaken"):
        data[key]["data"]["gameVersion"] = 4  # a Classic log
    assert "Consumables" not in all_text(full_text(data))


def test_no_health_item_data_means_no_healthstone_accusations():
    data = report()
    del data["healthItems"]  # an older report, saved before these were fetched
    text = all_text(full_text(data))
    assert "Died with a healthstone" not in text and "Tryhards" in text


def test_combat_potion_names_are_a_setting():
    from bot.wcl import _potion_filter
    assert _potion_filter(SETTINGS) == (
        "type = 'applybuff' and ability.name in ('Potion of Recklessness', 'Light''s Potential')"
    )
    assert "ability.id = 0" in _potion_filter(replace(SETTINGS, combat_potions=()))


def test_prog_night_swaps_parses_for_throughput():
    data = report()
    for f in data["fights"]:
        f["kill"] = False
        f["bossPercentage"] = f.get("bossPercentage") or 50
    data["dpsRankings"] = data["hpsRankings"] = {"data": []}
    r = full_text(data)
    assert r.headline.title == "Prog report · Liberation of Undermine"
    assert r.headline.accent == 0xE67E22  # orange for a prog night
    assert "no kill yet" in r.headline.subtitle
    assert "**🏆 Top DPS** - **Pumper** 112k\n**💚 Top healer** - **Healz** 62k HPS" in r.headline.blocks[0]
    assert "90+" not in card_text(r.headline) and "Grey" not in card_text(r.headline)
    board = r.thread[1]
    assert (board.title, board.subtitle) == ("📊 Throughput", "Raw numbers: wipes don't get parses")
    assert "Pink parse" not in all_text(r)


def test_single_boss_prog_night_headline():
    data = report()
    data["fights"] = [f for f in data["fights"] if f.get("encounterID") == 3011]
    data["dpsRankings"] = data["hpsRankings"] = {"data": []}
    r = full_text(data)
    assert r.headline.title == "Prog report · Boss C"
    # On a prog night the best pull leads the headline.
    assert r.headline.blocks[0].startswith("**📈 Best pull** - P2 at 30% · the last pull of the night")
    assert r.thread_title == "Prog report · Sep 17 · Boss C"


def test_featured_boss_for_the_thumbnail():
    from bot.render import featured_boss
    assert featured_boss(analyze(report(), SETTINGS)) == 3010  # the last boss killed
    data = report()
    data["fights"] = [f for f in data["fights"] if f.get("encounterID") == 3011]
    assert featured_boss(analyze(data, SETTINGS)) == 3011  # the prog boss


def test_the_report_is_only_about_tonight():
    text = all_text(full_text())
    assert "**📈 Boss C** - 2 wipes · best P2 at 30%" in text  # nothing about last raid's best
    assert "**💀 Floor inspector** - **Dyer** 3 deaths\n" in text
    for phrase in ("last raid", "raids running", "last week"):
        assert phrase not in text


def test_thread_title_uses_the_guilds_timezone():
    # The raid started 01:00 UTC on the 17th: that's still the 16th in the US.
    assert full_text(tz=ZoneInfo("America/Los_Angeles")).thread_title == "Raid report · Sep 16 · Liberation of Undermine"
    assert full_text().thread_title == "Raid report · Sep 17 · Liberation of Undermine"


def test_unlinked_nudge_goes_at_the_end_of_the_thread():
    r = full_text(links={n: i for i, n in enumerate(["Tanky", "Healz", "Pumper", "Greyson", "Dyer"], 1)})
    assert r.thread[-1].footer == "Not linked: Middling. An admin can link them with /link-raid so the bot can tag them."
    assert "Not linked" not in card_text(r.headline)


def test_leaderboard_ping_setting():
    night = analyze(report(), SETTINGS)
    lines = build_lines(night, SETTINGS)
    quiet = render_report(night, lines, URL, lambda c: None, thread_ping_everyone=False)
    assert [c.pings for c in quiet.thread] == [True, False, True, True, True, True]


def test_card_text_is_the_plain_fallback():
    text = card_text(full_text().headline)
    assert text.startswith("### Raid report · Liberation of Undermine\n-# Heroic")
    assert text.endswith(f"[View log on Warcraft Logs](<{URL}>)")


def test_oversized_cards_continue_in_a_second_card():
    card = Card("🧪 Consumables", 1, [f"block {i} " + "x" * 900 for i in range(6)], footer="note", button=("b", "u"))
    parts = split_card(card)
    assert len(parts) == 2
    assert parts[0].title == "🧪 Consumables" and parts[1].title == "🧪 Consumables (continued)"
    assert parts[0].footer is None and parts[1].footer == "note"  # small print stays at the very end
    assert all(sum(len(b) for b in p.blocks) < 3800 for p in parts)
    assert [b for p in parts for b in p.blocks] == card.blocks
    many = split_card(Card("t", 1, ["x"] * 30))
    assert all(len(p.blocks) <= 15 for p in many)  # keeps well inside Discord's 40 parts per card


def test_one_giant_list_is_split_between_names():
    names = " · ".join(f"**Player{i}** 1" for i in range(400))
    parts = split_card(Card("t", 1, [names]))
    assert len(parts) >= 2 and all(len(b) < 3800 for p in parts for b in p.blocks)
    assert all(not b.startswith(" ·") for p in parts for b in p.blocks)


def test_fmt_health():
    assert fmt_health(5.07, 3) == "P3 at 5.1%"
    assert fmt_health(44.2, 3) == "P3 at 44%"
    assert fmt_health(0.62, 1) == "0.6%"
    assert fmt_health(30, None) == "30%"


def test_split_message_respects_limit():
    text = "\n".join("x" * 300 for _ in range(20))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_time_dead_has_seconds():
    from bot.awards import fmt_seconds

    assert [fmt_seconds(s) for s in (45, 60, 526.98, 3735)] == ["45s", "1m 00s", "8m 47s", "1h 02m 15s"]


def test_power_infusion_lists_each_priests_targets():
    data = report()
    # Healz already infused Pumper 4 times; add two more targets, one of them from a second priest.
    data["powerInfusion"] += [{"type": "applybuff", "sourceID": 2, "targetID": 6, "fight": f}
                              for f in (1, 2, 3, 5, 6, 6)]
    data["powerInfusion"] += [{"type": "applybuff", "sourceID": 1, "targetID": 5, "fight": f} for f in (1, 2)]
    text = all_text(full_text(data))
    assert "**💜 Power Infusion**\n-# Who each priest infused, most to least\n" in text
    # A line per priest, the busier one first, with each one's targets most to least.
    assert "**Healz** · **Middling** 6 · **Pumper** 4\n**Tanky** · **Dyer** 2" in text


def test_a_priest_topping_up_one_person_isnt_listed():
    data = report()
    data["powerInfusion"] = data["powerInfusion"][:2]  # twice, on one target: not a pattern
    assert "Power Infusion" not in all_text(full_text(data))


def _pi(source, target, start, end=None, fight=6):
    """A Power Infusion landing, and coming off 15 s later unless told otherwise."""
    at = [{"type": "applybuff", "sourceID": source, "targetID": target, "fight": fight, "timestamp": start}]
    return at + [{"type": "removebuff", "sourceID": source, "targetID": target, "fight": fight,
                  "timestamp": end or start + 15_000}]


def test_a_priest_overwriting_another_priests_infusion():
    data = report()
    # Healz infuses Pumper; 2 s later Tanky lands one on Pumper, which ends Healz's early.
    # Power Infusion doesn't stack, so the log shows the first buff off as the second goes on.
    data["powerInfusion"] = (_pi(2, 3, 1_100_000, end=1_102_000) + _pi(1, 3, 1_102_000)
                             + _pi(2, 6, 1_150_000))
    night = analyze(data, SETTINGS)
    [gone] = night.pi_overwritten
    assert (gone.receiver.name, gone.cut.name, gone.by.name) == ("Pumper", "Healz", "Tanky")
    assert (gone.ran, gone.lost) == (2.0, 13.0)  # 15 s is the full length elsewhere in this log
    text = all_text(full_text(data))
    assert "**🪫 Overwritten · 13s of Power Infusion wasted**\n" in text
    assert "-# One priest's Power Infusion replaced by another's on the same player\n" in text
    assert "**Pumper** · **Healz** → **Tanky** after 2s · 13s lost" in text


def test_losing_a_second_or_two_isnt_worth_saying():
    data = report()
    data["powerInfusion"] = (_pi(2, 3, 1_100_000, end=1_113_000) + _pi(1, 3, 1_113_000)
                             + _pi(2, 6, 1_150_000))
    assert analyze(data, SETTINGS).pi_overwritten == []  # 2 s lost


def test_one_priest_reinfusing_the_same_player_isnt_overwriting():
    data = report()
    data["powerInfusion"] = (_pi(2, 3, 1_100_000, end=1_102_000) + _pi(2, 3, 1_102_000)
                             + _pi(2, 6, 1_150_000))
    assert analyze(data, SETTINGS).pi_overwritten == []


def test_infusion_windows_need_both_ends():
    data = report()  # the sample's events have no "came off", so there are no windows to compare
    night = analyze(data, SETTINGS)
    assert night.infusions == [] and night.pi_overwritten == []
    assert sum(night.power_infusion.values()) == 4  # the count still works

from dataclasses import replace
from zoneinfo import ZoneInfo

from bot.awards import BossResult, Line, build_lines, fmt_health
from bot.config import RecapSettings
from bot.recap import Char, DeathLine, _top_with_ties, analyze, norm_name, norm_realm
from bot.render import render_report, split_message

from .sample_report import report

SETTINGS = RecapSettings()
URL = "https://www.warcraftlogs.com/reports/AbCdEf1234567890"


def names(lines):
    return [line.char.name for line in lines]


def full_text(data=None, settings=SETTINGS, history=None, links=None, tz=ZoneInfo("UTC")):
    night = analyze(data or report(), settings)
    lines = build_lines(night, settings, history)
    links = links or {}
    return render_report(night, lines, URL, lambda c: links.get(c.name), tz)


def all_text(rendered):
    return "\n".join([rendered.headline] + [m.text for m in rendered.thread])


# --- analysis ------------------------------------------------------------------------------------


def test_roster_is_only_people_in_boss_pulls():
    night = analyze(report(), SETTINGS)
    assert "Bystander" not in names_of(night.roster)
    assert len(night.roster) == 6


def names_of(chars):
    return [c.name for c in chars]


def test_averages_are_per_night():
    night = analyze(report(), SETTINGS)
    # WCL drops the decimals: Pumper's 94.5 shows as 94.
    assert [(p.char.name, p.average, p.kills) for p in night.high] == [("Pumper", 94, 2), ("Middling", 90, 2)]


def test_healers_use_healing_parses_and_others_use_damage():
    night = analyze(report(), SETTINGS)
    by_name = {p.char.name: p for p in night.parses}
    assert by_name["Healz"].average == 65  # 60 and 70 healing, not 3 damage
    assert by_name["Tanky"].average == 6  # damage (5 and 8), not the 99 in the healing table
    assert by_name["Greyson"].average == 20  # "-" in the healing table is ignored


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


def test_headline():
    r = full_text(links={"Pumper": 111, "Greyson": 222})
    assert r.headline.splitlines() == [
        "📜 **Raid report** · Liberation of Undermine · Heroic · <t:1758070800:D> · 2 bosses down · 5 pulls · 20m",
        f"<{URL}>",
        "📈 **Boss C:** 2 wipes, best P2 at 30%",
        "🏆 **Top DPS:** <@111> 94 · **Top healer:** **Healz** 65 · **Top tank:** **Tanky** 6",
        "🌟 **90+:** <@111> (damage 94) · **Middling** (damage 90)",
        "🗑️ **Grey:** <@222> (damage 20)",
        "💀 **Floor inspector:** **Dyer** (3 deaths)",
        "🧵 Full report in the thread ↓",
    ]


def test_thread_sections_in_order():
    r = full_text()
    assert [m.text.splitlines()[0] for m in r.thread] == [
        "🗺️ **The night**", "📊 **Parses**", "🌟 **Highlights**", "🤡 **Lowlights**", "💀 **Deaths**",
    ]


def test_the_night():
    night = full_text().thread[0].text
    assert "✅ Boss A, 2 pulls" in night
    assert "✅ Boss B, first pull" in night
    assert "❌ Boss C, 2 wipes, best P2 at 30%" in night
    assert "💔 **Heartbreaker:** Boss A wiped with the boss at **8%** in P2, before going down" in night


def test_leaderboard_uses_wcl_colours_and_crowns_the_top():
    board = full_text().thread[1].text.splitlines()
    assert board[1] == "⚔️ 👑 🟪 **Pumper** 94 · **Middling** 90 · 🟩 **Dyer** 45 · ⬜ **Greyson** 20"
    assert board[2] == "💚 👑 🟦 **Healz** 65"
    assert board[3] == "🛡️ 👑 ⬜ **Tanky** 6"


def test_awards():
    text = all_text(full_text())
    assert "🩷 **Pink parse:** **Pumper**, 99 on Boss A" in text
    assert "🦶 **Kick captain:** **Pumper**, 6 interrupts (next best: 2)" in text
    assert "🧼 **Dispel machine:** **Healz**, 12 dispels" in text
    assert "🪄 **Necromancer:** **Healz**, 3 battle rezzes" in text
    assert "🚽 **Parse of shame:** **Greyson**, 10 on Boss A" in text
    assert "🧽 **Damage sponge:** **Dyer**, 100M damage taken" in text
    assert "🛡️ **Outdamaged by a tank:** **Greyson** did less damage than **Tanky**" in text
    assert "🎯 **Nemesis:** **Dyer** died to Fire 3 times" in text
    assert "🧲 **Brez magnet:** **Dyer**, rezzed 3 times" in text
    # Nothing worth saying tonight: these stay silent rather than print a weak line.
    for quiet in ("Metronome", "Rollercoaster", "Battle healer", "Canary", "Couldn't wait for loot",
                  "Speedrunner", "Last one standing", "Wall of the night", "Raid's nemesis", "Wipe starter"):
        assert quiet not in text, quiet


def test_prog_night_swaps_parses_for_throughput():
    data = report()
    for f in data["fights"]:
        f["kill"] = False
        f["bossPercentage"] = f.get("bossPercentage") or 50
    data["dpsRankings"] = data["hpsRankings"] = {"data": []}
    r = full_text(data)
    assert r.headline.startswith("📜 **Prog report** · Liberation of Undermine · Heroic")
    assert "no kill yet" in r.headline
    assert "🏆 **Top DPS:** **Pumper** 112k · **Top healer:** **Healz** 62k HPS" in r.headline
    assert "90+" not in r.headline and "Grey" not in r.headline
    assert r.thread[1].text.startswith("📊 **Throughput** (raw numbers, since wipes don't get parses)")
    assert "Pink parse" not in all_text(r)


def test_single_boss_prog_night_headline():
    data = report()
    data["fights"] = [f for f in data["fights"] if f.get("encounterID") == 3011]
    data["dpsRankings"] = data["hpsRankings"] = {"data": []}
    r = full_text(data)
    assert r.headline.startswith("📜 **Prog report** · Boss C · Heroic")
    assert "📈 **Best pull:** P2 at 30% (the last pull of the night)" in r.headline
    assert r.thread_title == "Prog report · Sep 17 · Boss C"


class FakeHistory:
    def __init__(self, last=None, streaks=None):
        self.last = last
        self.streaks = streaks or {}

    def last_result(self, encounter_id, difficulty, before_ms):
        return self.last

    def streak(self, key, char, before_ms):
        return self.streaks.get((key, char.name), 0)


def test_history_adds_progress_and_streaks():
    history = FakeHistory(
        last=BossResult(killed=False, boss_pct=44, phase=3),
        streaks={("floor", "Dyer"): 2, ("grey", "Greyson"): 1, ("sponge", "Dyer"): 1},
    )
    text = all_text(full_text(history=history))
    assert "📈 **Boss C:** 2 wipes, best P2 at 30% · last raid's best: P3 at 44%" in text
    assert "💀 **Floor inspector:** **Dyer** (3 deaths, 3 raids running)" in text
    assert "🗑️ **Grey:** **Greyson** (damage 20, 2 raids running)" in text
    assert "🧽 **Damage sponge:** **Dyer**, 100M damage taken (2 raids running)" in text


def test_thread_title_uses_the_guilds_timezone():
    # The raid started 01:00 UTC on the 17th: that's still the 16th in the US.
    assert full_text(tz=ZoneInfo("America/Los_Angeles")).thread_title == "Raid report · Sep 16 · Liberation of Undermine"
    assert full_text().thread_title == "Raid report · Sep 17 · Liberation of Undermine"


def test_unlinked_nudge_goes_at_the_end_of_the_thread():
    r = full_text(links={n: i for i, n in enumerate(["Tanky", "Healz", "Pumper", "Greyson", "Dyer"], 1)})
    assert r.thread[-1].text.endswith("-# Not linked: Middling. Use `/link` so the bot can tag you.")
    assert "Not linked" not in r.headline


def test_leaderboard_ping_setting():
    night = analyze(report(), SETTINGS)
    lines = build_lines(night, SETTINGS)
    quiet = render_report(night, lines, URL, lambda c: None, thread_ping_everyone=False)
    assert [m.pings for m in quiet.thread] == [True, False, True, True, True]


def test_parses_show_like_wcl():
    from bot.recap import whole
    # Checked against the site: Holyshtter's 95.86 average shows as 95, Fstingnemo's 46.71 as 46.
    assert whole(95.857) == 95 and whole(46.714) == 46
    assert whole(89.99) == 89  # so 89.99 is not in the 90+ club, just like on WCL
    assert whole(90.0) == 90 and whole(270 / 3) == 90  # float noise doesn't knock 90 down to 89


def test_fmt_health():
    assert fmt_health(5.07, 3) == "P3 at 5.1%"
    assert fmt_health(44.2, 3) == "P3 at 44%"
    assert fmt_health(0.62, 1) == "0.6%"
    assert fmt_health(30, None) == "30%"


def test_group_lines_share_one_line():
    from bot.render import _Names, _text
    lines = [Line("headline", ["a"], group="g"), Line("headline", ["b"], group="g"), Line("headline", ["c"])]
    assert _text(lines, _Names(lambda c: None)) == ["a · b", "c"]


def test_split_message_respects_limit():
    text = "\n".join("x" * 300 for _ in range(20))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]

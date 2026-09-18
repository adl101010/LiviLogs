from dataclasses import replace

from bot.config import RecapSettings
from bot.recap import Char, build_recap, norm_name, norm_realm
from bot.render import render, split_message

from .sample_report import report

SETTINGS = RecapSettings()


def names(lines):
    return [line.char.name for line in lines]


def test_averages_are_per_night():
    recap = build_recap(report(), SETTINGS)
    assert [(p.char.name, p.average, p.kills) for p in recap.high] == [
        ("Pumper", 94.5, 2),
        ("Middling", 90.0, 2),  # 89 and 91: average exactly on the line counts
    ]


def test_grey_excludes_tanks_by_default():
    recap = build_recap(report(), SETTINGS)
    assert names(recap.grey) == ["Greyson"]
    assert recap.grey[0].average == 20.0


def test_grey_can_include_tanks():
    recap = build_recap(report(), replace(SETTINGS, grey_include_tanks=True))
    assert names(recap.grey) == ["Tanky", "Greyson"]  # worst first


def test_deaths_respect_wipe_cutoff_and_count_first_deaths():
    recap = build_recap(report(), SETTINGS)
    # Fight 1 (cutoff 5): Dyer, Dyer, Greyson, Healz, Pumper counted; Middling (6th) is not.
    # Fight 2: Greyson first, then Dyer. Trash and the pet are ignored.
    assert [(d.char.name, d.deaths, d.first_deaths) for d in recap.deaths] == [
        ("Dyer", 3, 1),
        ("Greyson", 2, 1),
        ("Healz", 1, 0),
        ("Pumper", 1, 0),  # tied with Healz for 3rd, so both are called out
    ]


def test_deaths_can_include_trash():
    recap = build_recap(report(), replace(SETTINGS, deaths_include_trash=True, deaths_top_n=10))
    assert "Tanky" in names(recap.deaths)


def test_fight_counts_and_difficulty():
    recap = build_recap(report(), SETTINGS)
    assert (recap.kills, recap.wipes, recap.difficulty) == (2, 1, 4)
    assert not recap.processing


def test_processing_flag():
    data = report()
    data["exportedSegments"] = 2
    assert build_recap(data, SETTINGS).processing


def test_no_kills_means_no_parses():
    data = report()
    data["rankings"] = {"data": []}
    recap = build_recap(data, SETTINGS)
    assert not recap.has_parses
    assert recap.deaths  # deaths still reported


def test_missing_fields_do_not_crash():
    recap = build_recap({}, SETTINGS)
    assert recap.kills == 0 and not recap.has_parses and recap.deaths == []


def test_realm_filled_from_master_data_when_rankings_lack_it():
    data = report()
    for fight in data["rankings"]["data"]:
        for role in fight["roles"].values():
            for c in role["characters"]:
                del c["server"]
    recap = build_recap(data, SETTINGS)
    assert recap.high[0].char == Char("Pumper", "Area 52")


def test_name_and_realm_normalising():
    assert norm_name("Tøm") == norm_name("tom")
    assert norm_name("Élune") == "elune"
    assert norm_realm("Area 52") == norm_realm("area52")
    assert norm_realm("Azjol-Nerub") == norm_realm("AzjolNerub")
    assert norm_realm("Kel'Thuzad") == "kelthuzad"


def test_render_mentions_linked_and_bolds_unlinked():
    recap = build_recap(report(), SETTINGS)
    links = {Char("Pumper", "Area 52").key: 111, Char("Greyson", "Area52").key: 222}
    text, unlinked = render(recap, "https://www.warcraftlogs.com/reports/x", SETTINGS, lambda c: links.get(c.key))
    assert "🏆 **90+ club:** <@111> 94.5 · **Middling** 90.0" in text
    assert "⚪ **Grey parses:** <@222> 20.0" in text
    assert "💀 **Most deaths:** **Dyer** 3 (first to die ×1) · <@222> 2 (first to die ×1)" in text
    assert "Heroic" in text and "<t:1758070800:D>" in text and "2 kills, 1 wipe" in text
    assert [c.name for c in unlinked] == ["Middling", "Dyer", "Healz"]
    assert text.endswith("Use `/link` so the bot can tag you.")


def test_render_empty_sections():
    data = report()
    data["deaths"] = {"data": {"entries": []}}
    recap = build_recap(data, replace(SETTINGS, parse_high=100, parse_grey=0))
    text, _ = render(recap, "u", replace(SETTINGS, parse_high=100, parse_grey=0), lambda c: None)
    assert "100+ club:** nobody tonight" in text
    assert "Grey parses:** nobody" in text
    assert "nobody died" in text


def test_split_message_respects_limit():
    text = "\n".join("x" * 300 for _ in range(20))
    chunks = split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]

"""Chart pictures, and how cards carry them: the picture replaces its lines, the lines stay as the
fallback text, and nobody is pinged by text that isn't on screen."""

import json
from pathlib import Path

from bot.awards import build_lines, consumable_rows, winners_by_key
from bot.charts import draw_charts
from bot.config import RecapSettings
from bot.recap import analyze
from bot.render import Chart, card_text, render_report, visible_text

from .sample_report import report

SETTINGS = RecapSettings()
FIXTURES = Path(__file__).parent / "fixtures"
PNG = b"\x89PNG\r\n\x1a\n"


def load(name):
    return analyze(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")), SETTINGS)


def chart_keys(lines):
    return {line.chart for line in lines if line.chart}


def rendered_with_charts(night, links=None, ping_everyone=True):
    lines = build_lines(night, SETTINGS)
    charts = draw_charts(night, chart_keys(lines), SETTINGS)
    links = links or {}
    return render_report(night, lines, "u", lambda c: links.get(c.name), thread_ping_everyone=ping_everyone,
                         charts=charts), charts


def card(rendered, title):
    return next(c for c in rendered.thread if c.title.startswith(title))


def test_kill_night_draws_parses_consumables_and_the_prog_boss():
    night = load("guild_kill")
    lines = build_lines(night, SETTINGS)
    # Ula'tek (8 wipes) gets a progress chart; bosses killed in 1-3 pulls don't.
    assert chart_keys(lines) == {"parses", "consumables", "progress:7"}
    charts = draw_charts(night, chart_keys(lines), SETTINGS)
    assert set(charts) == {"parses", "consumables", "progress:7"}
    assert all(png.startswith(PNG) for png in charts.values())


def test_prog_night_has_no_parse_chart():
    night = load("guild_prog")
    keys = chart_keys(build_lines(night, SETTINGS))
    assert keys == {"consumables", "progress:0"}  # wipes have no parses; the throughput stays text


def test_classic_has_parses_but_no_consumables():
    night = load("classic_tbc")
    assert set(draw_charts(night, chart_keys(build_lines(night, SETTINGS)), SETTINGS)) == {"parses"}


def test_a_chart_that_fails_is_skipped():
    night = load("guild_kill")
    assert draw_charts(night, {"progress:99", "nonsense"}, SETTINGS) == {}


def test_consumables_grid_flags_match_the_callouts():
    # The grid colours a cell yellow by the same rules the text callouts use.
    for name in ("guild_kill", "guild_prog"):
        night = load(name)
        rows = consumable_rows(night, SETTINGS)
        winners = winners_by_key(build_lines(night, SETTINGS))
        for flag in ("tryhard", "hoarder", "healthstone_bag", "no_flask", "no_food", "no_vantus"):
            assert {r.char for r in rows if flag in r.flags} == set(winners.get(flag, [])), (name, flag)


def test_charts_replace_their_lines_in_the_card_but_not_in_the_fallback():
    night = analyze(report(), SETTINGS)
    rendered, charts = rendered_with_charts(night, links={"Pumper": 111})
    assert set(charts) == {"parses", "consumables"}

    parses = card(rendered, "📊 Parses")
    assert isinstance(parses.blocks[0], Chart) and parses.blocks[0].filename == "parses.png"
    assert "**⚔️ Damage**" in card_text(parses)  # the fallback still has the leaderboard
    assert "Damage" not in visible_text(parses)
    assert parses.blocks[-1] == "-# <@111>"  # a picture can't ping, so the raid is tagged under it

    consumables = card(rendered, "🧪 Consumables")
    assert isinstance(consumables.blocks[0], Chart)  # the grid first, the shoutouts under it
    shown = visible_text(consumables)
    assert "Potion seller" in shown and "Potion hoarders" not in shown and "Tryhards" not in shown
    assert "Potion hoarders" in card_text(consumables)


def test_no_tags_under_the_parse_chart_when_the_leaderboard_shouldnt_ping():
    night = analyze(report(), SETTINGS)
    rendered, _ = rendered_with_charts(night, links={"Pumper": 111}, ping_everyone=False)
    assert "<@111>" not in visible_text(card(rendered, "📊 Parses"))


def test_progress_chart_sits_after_the_boss_list():
    night = load("guild_prog")
    rendered, _ = rendered_with_charts(night)
    blocks = card(rendered, "🗺️ The night").blocks
    assert isinstance(blocks[1], Chart) and blocks[1].filename == "progress-0.png"
    assert "`▇▅▅▄" in blocks[1].text  # the bar strip is the fallback
    assert isinstance(blocks[0], str) and isinstance(blocks[2], str)


def test_without_charts_nothing_changes():
    night = analyze(report(), SETTINGS)
    lines = build_lines(night, SETTINGS)
    plain = render_report(night, lines, "u", lambda c: None)
    assert not any(isinstance(b, Chart) for c in plain.thread for b in c.blocks)

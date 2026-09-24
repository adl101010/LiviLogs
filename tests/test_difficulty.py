"""A night that killed bosses on two difficulties (real log, Sept 22, anonymised).

Nek'zali died on Heroic and again on Mythic, and one raider DPSed Heroic then healed Mythic. Parses
from the two difficulties are different ladders, so nothing may average across them.
"""

import json
from pathlib import Path

from bot.awards import build_lines
from bot.charts import draw_charts, parse_chart
from bot.config import RecapSettings
from bot.mynight import my_night_card
from bot.recap import analyze
from bot.render import Chart, card_text, render_report

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = RecapSettings()


def night():
    return analyze(json.loads((FIXTURES / "two_difficulties.json").read_text(encoding="utf-8")), SETTINGS)


def report(n):
    lines = build_lines(n, SETTINGS)
    return render_report(n, lines, "u", lambda c: None), lines


def test_both_difficulties_are_kept_hardest_first():
    n = night()
    assert n.difficulties == [5, 4]  # Mythic, Heroic
    killed = [(b.name, b.difficulty) for b in n.bosses if b.killed]
    assert killed.count(("Nek'zali the Soulcoiler", 5)) == 1
    assert killed.count(("Nek'zali the Soulcoiler", 4)) == 1


def test_a_player_gets_a_line_per_role_and_difficulty():
    n = night()
    swapper = [p for p in n.parses if p.char.name == "P-c016da"]
    assert sorted((p.role, p.difficulty, round(p.average, 1)) for p in swapper) == [
        ("dps", 4, 84.3), ("healers", 5, 49.0),
    ]
    # Every line's parses come from that difficulty's kills only.
    for p in n.parses:
        bosses = {b.name for b in n.bosses if b.killed and b.difficulty == p.difficulty}
        assert {boss for _, boss in p.parses} <= bosses


def test_callouts_say_which_difficulty():
    n = night()
    r, _ = report(n)
    head = card_text(r.headline)
    assert r.headline.subtitle.startswith("Mythic and Heroic")
    assert "**🏆 Top DPS** - **P-6aead5** 96.0 (Heroic)" in head
    assert "**P-4da6a0** damage 93.5 (Mythic)" in head  # 90+, tagged per line
    assert "**P-a15cd2** healing 6.0 (Mythic)" in head  # grey, on the Mythic ladder only


def test_the_night_card_groups_by_difficulty():
    n = night()
    r, _ = report(n)
    night_card = card_text(r.thread[0])
    assert "**Mythic** · ✅ Nek'zali the Soulcoiler · ✅ Entombed Sentinels (4 pulls)" in night_card
    assert "**Heroic** · ✅ Nek'zali the Soulcoiler · ✅ The Coiled Altar (2 pulls) · ✅ Ula'tek" in night_card


def test_one_parse_grid_per_difficulty():
    n = night()
    r, _ = report(n)
    board = next(c for c in r.thread if c.title.startswith("📊"))
    titles = [b.split("**")[1] for b in board.blocks if isinstance(b, str)]
    assert titles == ["⚔️ Damage · Mythic", "💚 Healing · Mythic", "🛡️ Tanks · Mythic",
                      "⚔️ Damage · Heroic", "💚 Healing · Heroic", "🛡️ Tanks · Heroic"]
    # The swapper is a healer on one grid and a DPS on the other, never averaged together.
    assert "**P-c016da** **49.0**" in board.blocks[1]
    assert "**P-c016da** 84.3" in board.blocks[3]


def test_charts_are_drawn_one_per_difficulty():
    n = night()
    lines = build_lines(n, SETTINGS)
    charts = draw_charts(n, {line.chart for line in lines if line.chart}, SETTINGS)
    assert charts["parses:5"] and charts["parses:4"]
    assert parse_chart(n, 5) != parse_chart(n, 4)
    r = render_report(n, lines, "u", lambda c: None, charts=charts)
    board = next(c for c in r.thread if c.title.startswith("📊"))
    assert [b.filename for b in board.blocks if isinstance(b, Chart)] == ["parses-5.png", "parses-4.png"]


def test_my_night_lists_every_row_the_player_has():
    n = night()
    lines = build_lines(n, SETTINGS)
    char = next(c for c in n.roster if c.name == "P-c016da")
    text = card_text(my_night_card(n, lines, char, SETTINGS, "u"))
    assert "**📊 49.0 average** healing parse on Mythic" in text
    assert "**📊 84.3 average** damage parse on Heroic" in text
    assert text.index("healing parse") < text.index("damage parse")  # hardest first
    assert text.count("Nek'zali the Soulcoiler") == 2  # once under each block, different numbers

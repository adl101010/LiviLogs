"""Pictures for the report: the parse grid, the consumables grid, the gear check and a boss's
progress by pull.

Each chart is drawn with Pillow and returned as PNG bytes. The report lines they replace are kept,
so a card Discord refuses (and the probe) still shows the same facts as text. A chart that fails to
draw is skipped and its lines are shown instead; a chart must never cost the report.
"""

import io
import logging
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .awards import ConsumableRow, consumable_rows
from .gear import COLUMNS, gear_rows
from .config import RecapSettings
from .recap import DPS, HEALER, TANK, Boss, Night

log = logging.getLogger(__name__)

SCALE = 2  # drawn at twice the size so it stays sharp on phones and high-DPI screens

BG = (22, 23, 26)
CELL = (32, 33, 37)
CELL_AVG = (38, 39, 44)
GRID = (42, 43, 48)
TEXT = (219, 222, 225)
BRIGHT = (242, 243, 245)
MUTED = (148, 155, 164)
FAINT = (109, 111, 120)
OK = (87, 209, 138)
WARN = (240, 178, 50)
WARN_BG = (58, 49, 30)
STAR = (201, 205, 251)
STAR_BG = (45, 48, 84)
GOLD = (229, 204, 128)
KILL = (35, 165, 90)

# WCL's parse colours, lightened a little where the original is too dark on a dark background.
PARSE_BANDS = [
    (100, (229, 204, 128)),
    (99, (226, 104, 168)),
    (95, (255, 128, 0)),
    (75, (192, 124, 245)),
    (50, (74, 155, 255)),
    (25, (30, 255, 0)),
    (0, (157, 157, 157)),
]
# WoW's class colours: how raiders expect to see each other's names.
CLASS_COLOURS = {
    "deathknight": (196, 30, 58), "demonhunter": (163, 48, 201), "druid": (255, 124, 10),
    "evoker": (51, 147, 127), "hunter": (170, 211, 114), "mage": (63, 199, 235),
    "monk": (0, 255, 152), "paladin": (244, 140, 186), "priest": (255, 255, 255),
    "rogue": (255, 244, 104), "shaman": (46, 134, 232),  # shaman lifted a little to read on dark
    "warlock": (135, 136, 238),
    "warrior": (198, 155, 109),
}
PHASE_COLOURS = [(109, 111, 120), (74, 155, 255), (192, 124, 245), (255, 128, 0), (226, 104, 168)]
ROLE_ORDER = [(TANK, "TANKS"), (HEALER, "HEALERS"), (DPS, "DAMAGE")]
DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}

# Fonts with wide character coverage, tried in order: the Docker image installs DejaVu; the others
# cover running the probe on Windows or a Mac. Pillow's built-in font is the last resort.
_FONTS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for regular, heavy in _FONTS:
        try:
            return ImageFont.truetype(heavy if bold else regular, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default(size * SCALE)


def name_colour(night: Night, char) -> tuple[int, int, int]:
    """A raider's name in their class colour; plain text if the log doesn't say their class."""
    wow_class = (night.class_of(char) or "").replace(" ", "").casefold()
    return CLASS_COLOURS.get(wow_class, TEXT)


def parse_colour(pct: float) -> tuple[int, int, int]:
    return next(colour for floor, colour in PARSE_BANDS if pct >= floor)


class Canvas:
    """A Pillow image that takes coordinates in unscaled pixels."""

    def __init__(self, width: int, height: int):
        self.image = Image.new("RGB", (width * SCALE, height * SCALE), BG)
        self.draw = ImageDraw.Draw(self.image)

    def rect(self, x, y, w, h, fill, radius=3, outline=None, width=1) -> None:
        self.draw.rounded_rectangle(
            (x * SCALE, y * SCALE, (x + w) * SCALE - 1, (y + h) * SCALE - 1),
            radius=radius * SCALE, fill=fill, outline=outline, width=width * SCALE,
        )

    def line(self, x1, y1, x2, y2, fill, width=1) -> None:
        self.draw.line((x1 * SCALE, y1 * SCALE, x2 * SCALE, y2 * SCALE), fill=fill, width=width * SCALE)

    def text(self, x, y, text, size=13, fill=TEXT, bold=False, anchor="la") -> None:
        self.draw.text((x * SCALE, y * SCALE), text, font=font(size, bold), fill=fill, anchor=anchor)

    def png(self) -> bytes:
        out = io.BytesIO()
        self.image.save(out, format="PNG", optimize=True)
        return out.getvalue()


def text_width(text: str, size: int = 13, bold: bool = False) -> int:
    return int(font(size, bold).getlength(text) / SCALE) + 1


def wrap(text: str, width: int, size: int, lines: int = 2) -> list[str]:
    """Word-wrap into at most `lines` lines, shortening the last with an ellipsis if needed."""
    out: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if text_width(candidate, size) <= width or not current:
            current = candidate
        else:
            out.append(current)
            current = word
    out.append(current)
    if len(out) > lines:
        out = out[:lines - 1] + [" ".join(out[lines - 1:])]
    last = out[-1]
    while text_width(last, size) > width and len(last) > 1:
        last = last[:-2] + "…"
    out[-1] = last
    return out


def _header(canvas: Canvas, title: str, subtitle: str) -> None:
    canvas.text(16, 14, title, 15, BRIGHT, bold=True)
    canvas.text(16, 36, subtitle, 11, FAINT)


def _legend(canvas: Canvas, x: int, y: int, items: list[tuple[str, tuple]]) -> None:
    for label, colour in items:
        canvas.rect(x, y + 3, 9, 9, colour, radius=2)
        canvas.text(x + 13, y, label, 11, MUTED)
        x += 13 + text_width(label, 11) + 14


# --- parses ------------------------------------------------------------------------------------

def parse_chart(night: Night, difficulty: int | None = None) -> bytes | None:
    """Everyone's parse on every kill, coloured like WCL, with the night's average at the end. One
    chart per difficulty when a night raided more than one: the ladders are separate."""
    bosses = [b for b in night.bosses if b.killed and (difficulty is None or b.difficulty == difficulty)]
    if not night.has_parses or not bosses:
        return None
    names = [b.name for b in bosses]
    rows_by_role = []
    for role, label in ROLE_ORDER:
        lines = sorted((p for p in night.parses if p.role == role
                        and (difficulty is None or p.difficulty == difficulty)), key=lambda p: -p.average)
        if lines:
            rows_by_role.append((label, lines))

    name_w = max(text_width(p.char.name, 13) for _, lines in rows_by_role for p in lines) + 14
    cell_w, cell_h, gap = 66, 24, 3
    left, top = 16, 60
    header_h = 34
    width = left + name_w + (len(names) + 1) * (cell_w + gap) + 16
    rows = sum(len(lines) for _, lines in rows_by_role)
    height = top + header_h + len(rows_by_role) * 22 + rows * (cell_h + gap) + 44
    width = max(width, 420)

    canvas = Canvas(width, height)
    named = DIFFICULTY.get(difficulty if difficulty is not None else (night.difficulty or 0))
    where = night.zone or night.title
    _header(canvas, f"Parses · {where}" + (f" ({named})" if named else ""),
            f"{len(bosses)} {'kill' if len(bosses) == 1 else 'kills'} · each boss, then the night's average")

    x0 = left + name_w
    for i, name in enumerate([*names, "Avg"]):
        cx = x0 + i * (cell_w + gap) + cell_w / 2
        wrapped = wrap(name, cell_w, 10)
        for j, piece in enumerate(wrapped):
            y = top + header_h - 4 - (len(wrapped) - j) * 13
            canvas.text(cx, y, piece, 10, MUTED, bold=name == "Avg", anchor="ma")

    y = top + header_h
    for label, lines in rows_by_role:
        canvas.text(left, y + 6, label, 10, FAINT, bold=True)
        y += 22
        for p in lines:
            canvas.text(left, y + cell_h / 2, p.char.name, 13, name_colour(night, p.char), anchor="lm")
            by_boss: dict[str, list[float]] = {}
            for pct, boss in p.parses:
                by_boss.setdefault(boss, []).append(pct)
            for i, name in enumerate(names):
                x = x0 + i * (cell_w + gap)
                canvas.rect(x, y, cell_w, cell_h, CELL)
                values = by_boss.get(name)
                if values:
                    value = sum(values) / len(values)
                    canvas.text(x + cell_w / 2, y + cell_h / 2, f"{int(value)}", 13, parse_colour(value),
                                anchor="mm")
                else:
                    canvas.text(x + cell_w / 2, y + cell_h / 2, "–", 13, FAINT, anchor="mm")
            x = x0 + len(names) * (cell_w + gap)
            canvas.rect(x, y, cell_w, cell_h, CELL_AVG)
            canvas.text(x + cell_w / 2, y + cell_h / 2, f"{p.average:.1f}", 13, parse_colour(p.average),
                        bold=True, anchor="mm")
            y += cell_h + gap

    _legend(canvas, left, y + 14, [("0–24", PARSE_BANDS[6][1]), ("25–49", PARSE_BANDS[5][1]),
                                   ("50–74", PARSE_BANDS[4][1]), ("75–94", PARSE_BANDS[3][1]),
                                   ("95–98", PARSE_BANDS[2][1]), ("99", PARSE_BANDS[1][1]),
                                   ("100", PARSE_BANDS[0][1])])
    return canvas.png()


# --- consumables -------------------------------------------------------------------------------

def potions_used(row: ConsumableRow) -> tuple[str, str]:
    """Every potion drunk: "23", or for a healer "22 mana" / "3 + 22 mana"."""
    if row.role == HEALER and row.mana_potions:
        text = f"{row.combat_potions} + {row.mana_potions} mana" if row.combat_potions else f"{row.mana_potions} mana"
        return text, "plain"
    return str(row.combat_potions), "plain" if row.combat_potions else "dim"


def _consumable_cells(row: ConsumableRow, show_vantus: bool, show_rune: bool,
                      show_oil: bool = False) -> list[tuple[str, str]]:
    """(text, style) per column. Styles: ok, warn, star, dim, plain."""
    def fraction(have: int, total: int, flag: str) -> tuple[str, str]:
        style = "warn" if flag in row.flags else "ok" if have == total else "plain"
        return f"{have}/{total}", style

    cells = [fraction(row.flask, row.snapshots, "no_flask"), fraction(row.food, row.snapshots, "no_food")]
    if show_oil:
        cells.append(fraction(row.oil, row.oil_pulls, "no_oil") if row.oil_pulls else ("–", "dim"))
    cells += [fraction(row.potted, row.pulls, "hoarder"), potions_used(row)]
    cells.append((str(row.health_items),
                  "warn" if "healthstone_bag" in row.flags else "plain" if row.health_items else "dim"))
    if show_vantus:
        if row.vantus_pulls:
            cells.append(fraction(row.vantus, row.vantus_pulls, "no_vantus"))
        else:
            cells.append(("–", "dim"))
    if show_rune:
        cells.append((f"{row.rune}/{row.snapshots}" if row.rune else "–", "star" if "tryhard" in row.flags else "dim"))
    return cells


def consumables_chart(night: Night, settings: RecapSettings) -> bytes | None:
    """One row per raider: pulls with each consumable. Yellow is what the report would call out."""
    rows = consumable_rows(night, settings)
    if not rows:
        return None
    show_vantus = any(r.vantus_pulls for r in rows)
    show_rune = bool(settings.tryhard_runes)
    show_oil = any(r.oil_pulls for r in rows)  # needs gear snapshots
    columns = [("Flask", 76), ("Food", 76)]
    if show_oil:
        columns.append(("Weapon oil", 76))
    columns += [("Pulls potted", 76), ("Potions used", 96), ("Healthstone / potion", 76)]
    if show_vantus:
        columns.append(("Vantus", 76))
    if show_rune:
        columns.append((" / ".join(settings.tryhard_runes), 76))
    starts = []
    x = 0
    for _, w in columns:
        starts.append(x)
        x += w + 3
    notes = ["Pulls potted: pulls with at least one potion, out of pulls they were in (healers: mana potions count).",
             "Potions used: every potion drunk all night. Two on one pull counts as two."]

    name_w = max(text_width(r.char.name, 13) for r in rows) + 14
    cell_h, gap = 24, 3
    left, top, header_h = 16, 60, 34
    groups = [(label, [r for r in rows if r.role == role]) for role, label in ROLE_ORDER]
    groups = [(label, members) for label, members in groups if members]
    width = max(left + name_w + x + 16, max(text_width(n, 11) for n in notes) + 32)
    height = top + header_h + len(groups) * 22 + len(rows) * (cell_h + gap) + 44 + 17 * len(notes)

    canvas = Canvas(width, height)
    _header(canvas, "Consumables", "flask, food, oil, vantus and rune: pulls they had it on, out of pulls they were in")
    x0 = left + name_w
    for (name, cell_w), start in zip(columns, starts):
        wrapped = wrap(name, cell_w, 10)
        for j, piece in enumerate(wrapped):
            y = top + header_h - 4 - (len(wrapped) - j) * 13
            canvas.text(x0 + start + cell_w / 2, y, piece, 10, MUTED, anchor="ma")

    y = top + header_h
    fills = {"ok": (CELL, OK), "warn": (WARN_BG, WARN), "star": (STAR_BG, STAR), "dim": (CELL, FAINT),
             "plain": (CELL, TEXT)}
    for label, members in groups:
        canvas.text(left, y + 6, label, 10, FAINT, bold=True)
        y += 22
        for row in members:
            canvas.text(left, y + cell_h / 2, row.char.name, 13, name_colour(night, row.char), anchor="lm")
            cells = _consumable_cells(row, show_vantus, show_rune, show_oil)
            for (text, style), (_, cell_w), start in zip(cells, columns, starts):
                x = x0 + start
                background, colour = fills[style]
                canvas.rect(x, y, cell_w, cell_h, background)
                canvas.text(x + cell_w / 2, y + cell_h / 2, text, 12, colour, bold=style in ("warn", "star"),
                            anchor="mm")
            y += cell_h + gap

    items = [("would be called out", WARN), ("every pull", OK)]
    if show_rune:
        items.append(("tryhard", STAR))
    _legend(canvas, left, y + 14, items)
    for i, note in enumerate(notes):
        canvas.text(left, y + 38 + i * 17, note, 11, MUTED)
    return canvas.png()


# --- gear check --------------------------------------------------------------------------------

GEAR_STYLES = {  # state -> (background, foreground)
    "ok": (CELL, OK),
    "missing": (WARN_BG, WARN),
    "new": ((52, 44, 30), (230, 190, 110)),
    "partly": ((52, 44, 30), (230, 190, 110)),
    "late": ((42, 45, 60), (150, 170, 255)),
}


def _tick(canvas: Canvas, cx: float, cy: float, colour) -> None:
    s = SCALE
    canvas.draw.line([(cx - 5) * s, cy * s, (cx - 1.5) * s, (cy + 4) * s, (cx + 6) * s, (cy - 5) * s],
                     fill=colour, width=2 * s, joint="curve")


def _cross(canvas: Canvas, cx: float, cy: float, colour) -> None:
    s = SCALE
    for dy in (-4.5, 4.5):
        canvas.draw.line([(cx - 4.5) * s, (cy - dy) * s, (cx + 4.5) * s, (cy + dy) * s], fill=colour, width=2 * s)


def _gear_mark(canvas: Canvas, check, x: float, y: float, w: float, h: float, whole: bool) -> None:
    """One slot: a tick, a cross, or a word for the in-between cases ("new: 11 pulls", "pull 4")."""
    background, colour = GEAR_STYLES[check.state]
    canvas.rect(x, y, w, h, background)
    cx, cy = x + w / 2, y + h / 2
    if check.state == "ok":
        _tick(canvas, cx, cy, colour)
    elif check.state == "missing":
        if whole:
            canvas.text(cx, cy, "none", 12, colour, bold=True, anchor="mm")
        else:
            _cross(canvas, cx, cy, colour)
    elif check.state == "late":
        canvas.text(cx, cy, f"pull {check.from_pull}" if whole else f"p{check.from_pull}", 11, colour, anchor="mm")
    else:  # new or partly
        text = f"new: {check.bare_pulls} pulls" if check.state == "new" else f"bare: {check.bare_pulls}"
        canvas.text(cx, cy, text if whole else "new" if check.state == "new" else "bare", 11, colour, anchor="mm")


def gear_chart(night: Night, settings: RecapSettings) -> bytes | None:
    """One row per raider, a column per enchantable slot plus gems. Rings, and weapons for anyone
    dual-wielding, are split in two so one missing ring is visible."""
    rows = gear_rows(night, settings)
    if not rows:
        return None
    columns = [label for label, _ in COLUMNS] + ["Gems"]
    notes = ["Checked on every pull. \"new: 11 pulls\" = a new piece worn unenchanted for 11 pulls; "
             "\"pull 4\" = enchanted from pull 4.",
             "Rings, and weapons for dual-wielders, are split in two: first ring / main hand on the left.",
             "Gems: sockets the log can see. Sockets added by crafting don't show up, so they can't be checked."]
    name_w = max(text_width(r.char.name, 13) for r in rows) + 14
    cell_w, cell_h, gap = 72, 24, 3
    left, top, header_h = 16, 60, 26
    groups = [(label, [r for r in rows if r.role == role]) for role, label in ROLE_ORDER]
    groups = [(label, members) for label, members in groups if members]
    width = max(left + name_w + len(columns) * (cell_w + gap) + 16, max(text_width(n, 11) for n in notes) + 32)
    height = top + header_h + len(groups) * 22 + len(rows) * (cell_h + gap) + 44 + 17 * len(notes)

    canvas = Canvas(width, height)
    ready = sum(1 for r in rows if r.ready)
    _header(canvas, "Gear check", f"enchants and gems, checked on every pull · {ready} of {len(rows)} fully ready")
    x0 = left + name_w
    for i, name in enumerate(columns):
        canvas.text(x0 + i * (cell_w + gap) + cell_w / 2, top + header_h - 17, name, 10, MUTED, anchor="ma")

    y = top + header_h
    for label, members in groups:
        canvas.text(left, y + 6, label, 10, FAINT, bold=True)
        y += 22
        for row in members:
            canvas.text(left, y + cell_h / 2, row.char.name, 13, name_colour(night, row.char), anchor="lm")
            for i, (_, checks) in enumerate(row.columns):
                x = x0 + i * (cell_w + gap)
                if not checks:
                    canvas.rect(x, y, cell_w, cell_h, CELL)
                    canvas.text(x + cell_w / 2, y + cell_h / 2, "–", 12, FAINT, anchor="mm")
                elif len(checks) == 1:
                    _gear_mark(canvas, checks[0], x, y, cell_w, cell_h, whole=True)
                else:
                    half = (cell_w - 2) / 2
                    for j, check in enumerate(checks):
                        _gear_mark(canvas, check, x + j * (half + 2), y, half, cell_h, whole=False)
            x = x0 + len(row.columns) * (cell_w + gap)
            if row.empty_sockets:
                canvas.rect(x, y, cell_w, cell_h, WARN_BG)
                canvas.text(x + cell_w / 2, y + cell_h / 2, f"{row.empty_sockets} empty", 12, WARN, bold=True,
                            anchor="mm")
            else:
                canvas.rect(x, y, cell_w, cell_h, CELL)
                canvas.text(x + cell_w / 2 - 5, y + cell_h / 2, str(row.gems), 12, OK, anchor="rm")
                _tick(canvas, x + cell_w / 2 + 5, y + cell_h / 2, OK)
            y += cell_h + gap

    _legend(canvas, left, y + 14, [("enchanted", OK), ("missing", WARN),
                                   ("new loot, not enchanted", GEAR_STYLES["new"][1]),
                                   ("enchanted mid-raid", GEAR_STYLES["late"][1]), ("nothing to enchant", FAINT)])
    for i, note in enumerate(notes):
        canvas.text(left, y + 38 + i * 17, note, 11, MUTED)
    return canvas.png()


# --- progress ----------------------------------------------------------------------------------

def progress_chart(night: Night, boss: Boss) -> bytes | None:
    """One bar per pull: the boss's health when it ended (lower is better), coloured by phase."""
    pulls = boss.pulls
    if not pulls or any(p.boss_pct is None for p in pulls):
        return None
    left, right, top, bottom = 52, 16, 64, 62
    plot_h = 190
    width = max(left + right + 22 * len(pulls), 560)
    step = (width - left - right) / len(pulls)  # few pulls: wider slots, so the bars fill the chart
    height = top + plot_h + bottom

    canvas = Canvas(width, height)
    difficulty = DIFFICULTY.get(boss.difficulty or 0)
    _header(canvas, boss.name + (f" · {difficulty}" if difficulty else ""),
            "boss health when each pull ended · lower is better")

    def y_of(pct: float) -> float:
        return top + (1 - pct / 100) * plot_h

    for tick in (0, 25, 50, 75, 100):
        canvas.line(left, y_of(tick), width - right, y_of(tick), GRID)
        canvas.text(left - 6, y_of(tick), f"{tick}%", 10, FAINT, anchor="rm")

    best = boss.best_wipe
    label_every = 1 if len(pulls) <= 30 else 5
    phases_seen: set[int] = set()
    for i, pull in enumerate(pulls):
        w = min(step * 0.64, 40)
        x = left + i * step + (step - w) / 2
        cx = x + w / 2
        if pull.kill:
            canvas.text(cx, y_of(0) - 4, "Kill", 10, KILL, bold=True, anchor="md")
        else:
            phase = max(1, pull.phase or 1)
            phases_seen.add(phase)
            colour = PHASE_COLOURS[min(phase, len(PHASE_COLOURS)) - 1]
            top_y = y_of(pull.boss_pct)
            if pull is best:
                canvas.rect(x, top_y, w, y_of(0) - top_y, colour, radius=2, outline=GOLD, width=2)
                canvas.text(cx, top_y - 5, f"{_pct(pull.boss_pct)}%", 11, GOLD, bold=True, anchor="md")
            else:
                canvas.rect(x, top_y, w, max(1, y_of(0) - top_y), colour, radius=2)
        if (i + 1) % label_every == 0 or i == 0:
            canvas.text(cx, y_of(0) + 6, str(i + 1), 10, MUTED, anchor="ma")
    canvas.text((left + width - right) / 2, y_of(0) + 22, "pull", 10, FAINT, anchor="ma")

    legend = [(f"ended in P{p}" if p < max(phases_seen) or p == 1 else f"reached P{p}",
               PHASE_COLOURS[min(p, len(PHASE_COLOURS)) - 1]) for p in sorted(phases_seen)]
    if best:
        legend.append(("best pull", GOLD))
    if boss.killed:
        legend.append(("kill", KILL))
    _legend(canvas, 16, height - 18, legend)
    return canvas.png()


def _pct(pct: float) -> str:
    return f"{pct:.1f}".rstrip("0").rstrip(".") if pct < 10 else f"{pct:.0f}"


# --- entry point -------------------------------------------------------------------------------

def draw_charts(night: Night, keys: set[str], settings: RecapSettings) -> dict[str, bytes]:
    """PNGs for the chart keys the report's lines name ("parses", "consumables", "progress:<i>")."""
    out: dict[str, bytes] = {}
    for key in sorted(keys):
        try:
            if key == "parses":
                png = parse_chart(night)
            elif key.startswith("parses:"):
                png = parse_chart(night, int(key.split(":", 1)[1]))
            elif key == "consumables":
                png = consumables_chart(night, settings)
            elif key == "gear":
                png = gear_chart(night, settings)
            elif key.startswith("progress:"):
                png = progress_chart(night, night.bosses[int(key.split(":", 1)[1])])
            else:
                png = None
        except Exception:
            log.exception("Couldn't draw the %s chart; showing it as text", key)
            png = None
        if png:
            out[key] = png
    return out

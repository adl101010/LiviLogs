"""Pictures for the report: the parse grid, the consumables grid and a boss's progress by pull.

Each chart is drawn with Pillow and returned as PNG bytes. The report lines they replace are kept,
so a card Discord refuses (and the probe) still shows the same facts as text. A chart that fails to
draw is skipped and its lines are shown instead; a chart must never cost the report.
"""

import io
import logging
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .awards import ConsumableRow, consumable_rows
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

def parse_chart(night: Night) -> bytes | None:
    """Everyone's parse on every kill, coloured like WCL, with the night's average at the end."""
    bosses = [b for b in night.bosses if b.killed]
    if not night.has_parses or not bosses:
        return None
    names = [b.name for b in bosses]
    rows_by_role = []
    for role, label in ROLE_ORDER:
        lines = sorted((p for p in night.parses if p.role == role), key=lambda p: -p.average)
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
    difficulty = DIFFICULTY.get(night.difficulty or 0)
    where = night.zone or night.title
    _header(canvas, f"Parses · {where}" + (f" ({difficulty})" if difficulty else ""),
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
            canvas.text(left, y + cell_h / 2, p.char.name, 13, TEXT, anchor="lm")
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

def _consumable_cells(row: ConsumableRow, show_vantus: bool, show_rune: bool) -> list[tuple[str, str]]:
    """(text, style) per column. Styles: ok, warn, star, dim, plain."""
    def fraction(have: int, total: int, flag: str) -> tuple[str, str]:
        style = "warn" if flag in row.flags else "ok" if have == total else "plain"
        return f"{have}/{total}", style

    cells = [fraction(row.flask, row.snapshots, "no_flask"), fraction(row.food, row.snapshots, "no_food")]
    if row.role == HEALER:
        text = f"{row.mana_potions} mana" if row.mana_potions else "none"
        cells.append((text, "warn" if "hoarder" in row.flags else "plain" if row.mana_potions else "dim"))
    else:
        cells.append(fraction(row.potion_pulls, row.pulls, "hoarder"))
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
    columns = ["Flask", "Food", "Combat potion", "Healthstone / potion"]
    if show_vantus:
        columns.append("Vantus")
    if show_rune:
        columns.append(" / ".join(settings.tryhard_runes))

    name_w = max(text_width(r.char.name, 13) for r in rows) + 14
    cell_w, cell_h, gap = 76, 24, 3
    left, top, header_h = 16, 60, 34
    groups = [(label, [r for r in rows if r.role == role]) for role, label in ROLE_ORDER]
    groups = [(label, members) for label, members in groups if members]
    width = max(left + name_w + len(columns) * (cell_w + gap) + 16, 420)
    height = top + header_h + len(groups) * 22 + len(rows) * (cell_h + gap) + 44

    canvas = Canvas(width, height)
    _header(canvas, "Consumables", "pulls they had it on, out of pulls they were in")
    x0 = left + name_w
    for i, name in enumerate(columns):
        wrapped = wrap(name, cell_w, 10)
        for j, piece in enumerate(wrapped):
            y = top + header_h - 4 - (len(wrapped) - j) * 13
            canvas.text(x0 + i * (cell_w + gap) + cell_w / 2, y, piece, 10, MUTED, anchor="ma")

    y = top + header_h
    fills = {"ok": (CELL, OK), "warn": (WARN_BG, WARN), "star": (STAR_BG, STAR), "dim": (CELL, FAINT),
             "plain": (CELL, TEXT)}
    for label, members in groups:
        canvas.text(left, y + 6, label, 10, FAINT, bold=True)
        y += 22
        for row in members:
            canvas.text(left, y + cell_h / 2, row.char.name, 13, TEXT, anchor="lm")
            for i, (text, style) in enumerate(_consumable_cells(row, show_vantus, show_rune)):
                x = x0 + i * (cell_w + gap)
                background, colour = fills[style]
                canvas.rect(x, y, cell_w, cell_h, background)
                canvas.text(x + cell_w / 2, y + cell_h / 2, text, 12, colour, bold=style in ("warn", "star"),
                            anchor="mm")
            y += cell_h + gap

    items = [("would be called out", WARN), ("every pull", OK)]
    if show_rune:
        items.append(("tryhard", STAR))
    _legend(canvas, left, y + 14, items)
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
            elif key == "consumables":
                png = consumables_chart(night, settings)
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

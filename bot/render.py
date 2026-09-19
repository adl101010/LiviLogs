"""Report lines -> cards: a headline card for #logs and one card per section in the thread under it.

A card is plain data (title, blocks of markdown or charts, colour, optional thumbnail and buttons).
The Discord side turns it into a components-v2 container; card_text() gives the same content as plain
markdown for the probe and as a fallback, with each chart replaced by the lines it stands for. Mentions inside a container's text still ping (unlike
embeds), so the ping rules are the same as for ordinary messages.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .awards import (
    BOARD, CONSUMABLES, DEATHS, GEAR, HEADLINE, HIGHLIGHTS, LOWLIGHTS, NIGHT, Line, fmt_duration, plural,
)
from .recap import Char, Night

# Discord's limits for one components-v2 message: 4,000 characters of text across the whole
# message and 40 components. A card uses 1 + 2 per block + a few for the header, footer and
# button, so the block cap keeps well clear of 40.
TEXT_LIMIT = 3800
MAX_BLOCKS = 15
THREAD_NAME_LIMIT = 100

# WCL difficulty ids. Classic uses the same numbers for normal/heroic.
DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}

GOLD, ORANGE, BLURPLE, PURPLE, GREEN, RED, GREY, TEAL = (
    0xF0B232, 0xE67E22, 0x5865F2, 0xA335EE, 0x2ECC71, 0xED4245, 0x99AAB5, 0x1ABC9C,
)

SECTIONS = [
    (NIGHT, "🗺️ The night", BLURPLE),
    (BOARD, None, PURPLE),  # title depends on whether there were kills
    (HIGHLIGHTS, "🌟 Highlights", GREEN),
    (LOWLIGHTS, "🤡 Lowlights", RED),
    (DEATHS, "💀 Deaths", GREY),
    (CONSUMABLES, "🧪 Consumables", TEAL),
    (GEAR, "🛠️ Gear check", ORANGE),
]

_MENTION = re.compile(r"<@(\d+)>")


@dataclass
class Chart:
    """A picture in a card, and the text it replaces (used wherever the picture can't be shown)."""
    filename: str
    png: bytes
    text: str


Block = str | Chart


@dataclass
class Card:
    title: str
    accent: int
    blocks: list[Block]  # markdown or a chart; the card draws a divider between blocks
    subtitle: str | None = None
    thumbnail: str | None = None  # image URL shown beside the title
    button: tuple[str, str] | None = None  # (label, url) link button at the bottom
    actions: list[tuple[str, str]] = field(default_factory=list)  # (label, custom_id) buttons for the bot
    footer: str | None = None  # small print at the bottom
    pings: bool = True  # False: mentions show as names but notify nobody


@dataclass
class Rendered:
    headline: Card
    thread_title: str
    thread: list[Card]
    unlinked: list[Char] = field(default_factory=list)


def mentioned_ids(text: str) -> list[int]:
    return [int(i) for i in dict.fromkeys(_MENTION.findall(text))]


class _Names:
    def __init__(self, user_for: Callable[[Char], int | None]):
        self.user_for = user_for
        self.unlinked: list[Char] = []

    def __call__(self, char: Char) -> str:
        user_id = self.user_for(char)
        if user_id:
            return f"<@{user_id}>"
        if char not in self.unlinked:
            self.unlinked.append(char)
        return f"**{char.name}**"


def line_text(line: Line, who: _Names) -> str:
    body = "".join(who(p) if isinstance(p, Char) else p for p in line.parts)
    if not line.title:
        return body
    if line.is_stacked:
        note = f"\n-# {line.note}" if line.note else ""
        return f"**{line.title}**{note}\n{body}"
    return f"**{line.title}** - {body}"


def _blocks(lines: list[Line], who: _Names, charts: dict[str, bytes] | None = None) -> list[Block]:
    """One block per cluster, in the order clusters first appear; stacked callouts get a blank line
    between them so each reads as its own item.

    Lines a chart covers become that chart, placed before the first block that followed them (or
    at the end). Their text goes with the chart for the fallback.
    """
    charts = charts or {}
    covered: dict[str, list[Line]] = {}
    follows: dict[str, Line | None] = {}
    rest: list[Line] = []
    for line in lines:
        if line.chart in charts:
            if line.chart not in covered:
                covered[line.chart] = []
                follows[line.chart] = None
            covered[line.chart].append(line)
        else:
            rest.append(line)
            for key, after in follows.items():
                if after is None:
                    follows[key] = line
    clusters = list(dict.fromkeys(line.cluster for line in rest))  # _text_blocks makes one block each
    blocks: list[Block] = list(_text_blocks(rest, who))
    placed: list[tuple[int, Chart]] = []
    for key, chart_lines in covered.items():
        text = "\n\n".join(_text_blocks(chart_lines, who))
        after = follows[key]
        placed.append((clusters.index(after.cluster) if after else len(clusters),
                       Chart(key.replace(":", "-") + ".png", charts[key], text)))
    # From the back, so indexes stay right; two charts at one spot keep their order.
    for index, chart in sorted(reversed(placed), key=lambda ic: ic[0], reverse=True):
        blocks.insert(index, chart)
    return blocks


def _text_blocks(lines: list[Line], who: _Names) -> list[str]:
    order: list[str] = []
    grouped: dict[str, list[Line]] = {}
    for line in lines:
        if line.cluster not in grouped:
            order.append(line.cluster)
            grouped[line.cluster] = []
        grouped[line.cluster].append(line)
    blocks = []
    for cluster in order:
        text = ""
        previous: Line | None = None
        for line in grouped[cluster]:
            piece = line_text(line, who)
            if text:
                spaced = any(item is not None and item.title and item.is_stacked for item in (line, previous))
                text += "\n\n" if spaced else "\n"
            text += piece
            previous = line
        blocks.append(text)
    return blocks


def _local_date(night: Night, tz: ZoneInfo) -> str:
    when = datetime.fromtimestamp(night.start_ms / 1000, timezone.utc).astimezone(tz)
    return f"{when:%b} {when.day}"


def _subject(night: Night) -> str:
    if not night.kills and len(night.bosses) == 1:
        return night.bosses[0].name
    return night.zone or night.title


def featured_boss(night: Night) -> int | None:
    """The boss whose picture goes on the headline: the prog boss on a night without kills (the
    most pulled), otherwise the last boss killed."""
    if not night.bosses:
        return None
    if not night.kills:
        return max(night.bosses, key=lambda b: len(b.pulls)).encounter_id
    killed = [b for b in night.bosses if b.killed]
    return max(killed, key=lambda b: max(p.end for p in b.pulls)).encounter_id


def render_report(
    night: Night,
    lines: list[Line],
    url: str,
    user_for: Callable[[Char], int | None],
    tz: ZoneInfo = ZoneInfo("UTC"),
    thread_ping_everyone: bool = True,
    thumbnail: str | None = None,
    charts: dict[str, bytes] | None = None,
    actions: list[tuple[str, str]] | None = None,
) -> Rendered:
    who = _Names(user_for)
    prog = not night.kills
    kind = "Prog report" if prog else "Raid report"

    facts = []
    if night.difficulty in DIFFICULTY:
        facts.append(DIFFICULTY[night.difficulty])
    if night.start_ms:
        facts.append(f"<t:{night.start_ms // 1000}:D>")  # shows in each reader's own timezone
    if prog:
        facts.append(plural(len(night.pulls), "pull"))
        facts.append("no kill yet" if night.pulls else "no boss pulls")
    else:
        killed = sum(1 for b in night.bosses if b.killed)
        facts.append(f"{killed} {'boss' if killed == 1 else 'bosses'} down")
        facts.append(plural(len(night.pulls), "pull"))
        if night.span_seconds:
            facts.append(fmt_duration(night.span_seconds))

    headline_blocks = _blocks([line for line in lines if line.section == HEADLINE], who)
    notes = []
    if night.processing:
        notes.append("⏳ WCL is still processing this log, so parses may still change.")

    thread: list[Card] = []
    for section, title, accent in SECTIONS:
        section_lines = [line for line in lines if line.section == section]
        if not section_lines:
            continue
        subtitle = None
        if section == BOARD:
            title = "📊 Parses" if night.has_parses else "📊 Throughput"
            subtitle = None if night.has_parses else "Raw numbers: wipes don't get parses"
        blocks = _blocks(section_lines, who, charts)
        if section == BOARD and thread_ping_everyone and any(isinstance(b, Chart) for b in blocks):
            # A picture can't ping anyone, so the raid gets tagged in a line under it instead.
            chars = [p for line in section_lines for p in line.parts if isinstance(p, Char)]
            tags = [f"<@{uid}>" for uid in dict.fromkeys(user_for(c) for c in chars) if uid]
            if tags:
                blocks.append("-# " + " ".join(tags))
        thread.append(Card(title, accent, blocks, subtitle=subtitle,
                           pings=thread_ping_everyone or section != BOARD))

    if thread:
        notes.append("🧵 Full report in the thread")
    if who.unlinked:
        nudge = f"Not linked: {', '.join(c.name for c in who.unlinked)}. An admin can /link them so the bot can tag them."
        if thread:
            thread[-1].footer = nudge
        else:
            notes.append(nudge)

    headline = Card(
        f"{kind} · {_subject(night)}",
        ORANGE if prog else GOLD,
        headline_blocks,
        subtitle=" · ".join(facts),
        thumbnail=thumbnail,
        button=("View log on Warcraft Logs", url),
        footer=" · ".join(notes) or None,
        actions=actions or [],
    )
    thread_title = f"{kind} · {_local_date(night, tz)} · {_subject(night)}" if night.start_ms else kind
    return Rendered(headline, thread_title[:THREAD_NAME_LIMIT], [c for card in thread for c in split_card(card)],
                    who.unlinked)


def card_text(card: Card) -> str:
    """The card as plain markdown: what the probe prints, and the fallback if a card is refused.
    Charts are replaced by the lines they stand for."""
    parts = [f"### {card.title}"]
    if card.subtitle:
        parts[0] += f"\n-# {card.subtitle}"
    parts += [b.text if isinstance(b, Chart) else b for b in card.blocks]
    if card.footer:
        parts.append(f"-# {card.footer}")
    if card.button:
        parts.append(f"[{card.button[0]}](<{card.button[1]}>)")
    return "\n\n".join(parts)


def visible_text(card: Card) -> str:
    """What a card actually shows (a chart's stand-in text isn't), for working out who it pings."""
    parts = [card.title, card.subtitle or "", card.footer or ""]
    return "\n".join(parts + [b for b in card.blocks if isinstance(b, str)])


def split_card(card: Card, limit: int = TEXT_LIMIT, max_blocks: int = MAX_BLOCKS) -> list[Card]:
    """Keep each card inside one Discord message's limits; an overflowing card continues in a
    second card with the same colour. A single block that is itself too long is split on lines."""
    blocks: list[Block] = []
    for block in card.blocks:
        blocks += [block] if isinstance(block, Chart) else _split_block(block, limit - 300)
    cards: list[Card] = []
    current: list[Block] = []
    fixed = len(card.title) + len(card.subtitle or "") + len(card.footer or "") + 200

    def size_of(block: Block) -> int:
        return 0 if isinstance(block, Chart) else len(block)

    for block in blocks:
        size = fixed + sum(size_of(b) for b in current) + size_of(block)
        if current and (size > limit or len(current) >= max_blocks):
            cards.append(replace(card, blocks=current, footer=None, button=None, actions=[]))
            current = []
        current.append(block)
    cards.append(replace(card, blocks=current))
    for i, c in enumerate(cards[1:], 1):
        cards[i] = replace(c, title=f"{card.title} (continued)", subtitle=None, thumbnail=None)
    return cards


def _split_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    out, current = [], ""
    for line in block.split("\n"):
        while len(line) > limit:  # one enormous line (a very long list of names): cut it
            if current:
                out.append(current)
                current = ""
            cut = line.rfind(" · ", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(line[:cut])
            line = line[cut:].lstrip(" ·")
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def split_message(text: str, limit: int = 2000) -> list[str]:
    """Split on line breaks so no plain message passes Discord's 2,000-character limit."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single monster line; hard-cut it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

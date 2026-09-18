"""Report lines -> Discord messages: a headline for #logs, and a thread of sections under it.

Mentions go in message text, never in embeds: Discord shows @names inside embeds but doesn't
notify anyone, which would defeat the point.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .awards import BOARD, DEATHS, HEADLINE, HIGHLIGHTS, LOWLIGHTS, NIGHT, Line, fmt_duration, plural
from .recap import Char, Night

DISCORD_LIMIT = 2000
THREAD_NAME_LIMIT = 100

# WCL difficulty ids. Classic uses the same numbers for normal/heroic.
DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}

SECTIONS = [
    (NIGHT, "🗺️ **The night**"),
    (BOARD, None),  # title depends on whether there were kills
    (HIGHLIGHTS, "🌟 **Highlights**"),
    (LOWLIGHTS, "🤡 **Lowlights**"),
    (DEATHS, "💀 **Deaths**"),
]

_MENTION = re.compile(r"<@(\d+)>")


@dataclass
class Message:
    text: str
    pings: bool = True  # False: mentions show as names but notify nobody


@dataclass
class Rendered:
    headline: str
    thread_title: str
    thread: list[Message]
    unlinked: list[Char]


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


def _text(lines: list[Line], who: _Names) -> list[str]:
    """Lines to text; consecutive lines in the same group share one line."""
    out: list[str] = []
    group = None
    for line in lines:
        text = "".join(who(p) if isinstance(p, Char) else p for p in line.parts)
        if line.group and line.group == group:
            out[-1] += " · " + text
        else:
            out.append(text)
        group = line.group
    return out


def _local_date(night: Night, tz: ZoneInfo) -> str:
    when = datetime.fromtimestamp(night.start_ms / 1000, timezone.utc).astimezone(tz)
    return f"{when:%b} {when.day}"


def _title(night: Night) -> str:
    if not night.kills and len(night.bosses) == 1:
        return night.bosses[0].name
    return night.zone or night.title


def render_report(
    night: Night,
    lines: list[Line],
    url: str,
    user_for: Callable[[Char], int | None],
    tz: ZoneInfo = ZoneInfo("UTC"),
    thread_ping_everyone: bool = True,
) -> Rendered:
    who = _Names(user_for)
    prog = not night.kills
    kind = "Prog report" if prog else "Raid report"

    header = [f"📜 **{kind}** · {_title(night)}"]
    if night.difficulty in DIFFICULTY:
        header.append(DIFFICULTY[night.difficulty])
    if night.start_ms:
        header.append(f"<t:{night.start_ms // 1000}:D>")  # shows in each reader's own timezone
    header.append(plural(len(night.pulls), "pull"))
    if prog:
        header.append("no kill yet" if night.pulls else "no boss pulls")
    else:
        killed = sum(1 for b in night.bosses if b.killed)
        header.insert(-1, f"{killed} {'boss' if killed == 1 else 'bosses'} down")
        if night.span_seconds:
            header.append(fmt_duration(night.span_seconds))
    headline = [" · ".join(header), f"<{url}>"]
    if night.processing:
        headline.append("⏳ WCL is still processing this log, so parses may still change.")
    headline += _text([line for line in lines if line.section == HEADLINE], who)

    thread: list[Message] = []
    for section, title in SECTIONS:
        section_lines = [line for line in lines if line.section == section]
        if not section_lines:
            continue
        if section == BOARD:
            title = ("📊 **Parses**" if night.has_parses
                     else "📊 **Throughput** (raw numbers, since wipes don't get parses)")
        body = _text(section_lines, who)
        thread.append(Message("\n".join([title, *body]), pings=thread_ping_everyone or section != BOARD))

    if thread:
        headline.append("🧵 Full report in the thread ↓")
    if who.unlinked:
        names = ", ".join(c.name for c in who.unlinked)
        note = f"-# Not linked: {names}. Use `/link` so the bot can tag you."
        if thread:
            thread[-1].text += "\n" + note
        else:
            headline.append(note)

    thread_title = f"{kind} · {_local_date(night, tz)} · {_title(night)}" if night.start_ms else kind
    return Rendered("\n".join(headline), thread_title[:THREAD_NAME_LIMIT], thread, who.unlinked)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line breaks so no chunk passes Discord's length limit."""
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

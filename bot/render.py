"""Recap -> Discord message text.

Mentions go in the message text, not an embed: Discord shows @names inside embeds but never
notifies anyone, which would defeat the point.
"""

from typing import Callable

from .config import RecapSettings
from .recap import Char, Recap

DISCORD_LIMIT = 2000

# WCL difficulty ids. Classic uses the same numbers for normal/heroic.
DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}


def render(
    recap: Recap,
    url: str,
    settings: RecapSettings,
    user_for: Callable[[Char], int | None],
) -> tuple[str, list[Char]]:
    """Returns the message text and the characters in it that aren't linked to anyone."""
    unlinked: list[Char] = []

    def who(char: Char) -> str:
        user_id = user_for(char)
        if user_id:
            return f"<@{user_id}>"
        if char not in unlinked:
            unlinked.append(char)
        return f"**{char.name}**"

    header = [f"📜 **Raid recap** · {recap.zone or recap.title}"]
    if recap.difficulty in DIFFICULTY:
        header.append(DIFFICULTY[recap.difficulty])
    if recap.start_ms:
        header.append(f"<t:{recap.start_ms // 1000}:D>")  # shows in each reader's own timezone
    header.append(f"{recap.kills} {'kill' if recap.kills == 1 else 'kills'}, "
                  f"{recap.wipes} {'wipe' if recap.wipes == 1 else 'wipes'}")
    lines = [" · ".join(header), f"<{url}>"]

    if recap.processing:
        lines.append("⏳ WCL is still processing this log, so parses may still change.")

    if not recap.has_parses:
        lines.append("No ranked boss kills in this log, so no parses tonight.")
    else:
        high = " · ".join(f"{who(p.char)} {p.average:.1f}" for p in recap.high)
        lines.append(f"🏆 **{settings.parse_high:g}+ club:** {high or 'nobody tonight'}")
        grey = " · ".join(f"{who(p.char)} {p.average:.1f}" for p in recap.grey)
        lines.append(f"⚪ **Grey parses:** {grey or 'nobody. Suspicious.'}")

    if recap.deaths:
        parts = []
        for d in recap.deaths:
            first = f" (first to die ×{d.first_deaths})" if d.first_deaths else ""
            parts.append(f"{who(d.char)} {d.deaths}{first}")
        lines.append(f"💀 **Most deaths:** {' · '.join(parts)}")
    else:
        lines.append("💀 **Most deaths:** nobody died. Who are you people?")

    if unlinked:
        names = ", ".join(c.name for c in unlinked)
        lines.append(f"-# Not linked: {names}. Use `/link` so the bot can tag you.")

    return "\n".join(lines), unlinked


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

"""Linking raiders to Discord members without typing: the /link-raid wizard and the right-click
"Link characters" menu. Both are admin-only (Discord hides the commands from everyone else) and
answer privately.

Nothing is kept between clicks: each menu's id says which report, page and character it is for,
so the pages keep working after a restart.

    lr-pick:<page>:<u|a>:<site>:<code>:<Name-Realm>   pick a member for one raider (u = unlinked only)
    lr-page:<page>:<u|a>:<site>:<code>                 change page
    ml-add:<member id> / ml-del:<member id>            add / remove characters on the right-click menu
"""

import discord
from discord import ui

from .recap import Char
from .wcl import ReportRef, find_report_links

PICK, PAGE, ADD, REMOVE = "lr-pick", "lr-page", "ml-add", "ml-del"
KINDS = (PICK, PAGE, ADD, REMOVE)
PER_PAGE = 10  # 3 components a raider; a message holds 40
ID_LIMIT = 100  # Discord's longest custom id
TEAL = 0x1ABC9C
NO_PINGS = discord.AllowedMentions.none()


def char_from_label(label: str) -> Char:
    """'Name-Realm' back into a Char. Names can't contain hyphens; realms can (Azjol-Nerub)."""
    name, _, realm = label.partition("-")
    return Char(name, realm)


def report_id(ref: ReportRef) -> str:
    return f"{ref.host.split('.')[0]}:{ref.code}"


def ref_from(site: str, code: str) -> ReportRef | None:
    """Rebuilt through the link parser, so an id can only ever name a warcraftlogs.com report."""
    refs = find_report_links(f"https://{site}.warcraftlogs.com/reports/{code}")
    return refs[0] if refs else None


# --- /link-raid ---------------------------------------------------------------------------------

def roster_view(chars: list[Char], user_for, ref: ReportRef, page: int, everyone: bool,
                note: str | None = None) -> ui.LayoutView:
    """One page of raiders, each with a member picker. Unlinked only, unless `everyone`."""
    raiders = sorted(chars, key=lambda c: c.name.casefold())
    unlinked = [c for c in raiders if not user_for(c)]
    shown = raiders if everyone else unlinked
    pages = max(1, -(-len(shown) // PER_PAGE))
    page = min(max(page, 0), pages - 1)
    mode = "a" if everyone else "u"

    header = "### 🔗 Link raiders"
    facts = [f"{len(unlinked)} of {len(raiders)} not linked"]
    if pages > 1:
        facts.append(f"page {page + 1} of {pages}")
    header += "\n-# " + " · ".join(facts)
    items: list[ui.Item] = [ui.TextDisplay(header)]
    if note:
        items.append(ui.TextDisplay(note))
    if not shown:
        items.append(ui.TextDisplay("✅ Everyone from this raid is linked. "
                                    "Run `/link-raid everyone:True` to review or change links."))
    for char in shown[page * PER_PAGE:(page + 1) * PER_PAGE]:
        owner = user_for(char)
        line = f"**{char.name}** · {char.realm}" + (f" → <@{owner}>" if owner else "")
        custom_id = f"{PICK}:{page}:{mode}:{report_id(ref)}:{char.label}"
        if len(custom_id) > ID_LIMIT:
            items.append(ui.TextDisplay(line + "\n-# Name too long for a menu: use /link for this one"))
            continue
        picker = ui.UserSelect(custom_id=custom_id, placeholder="Pick a member…", min_values=0, max_values=1,
                               default_values=[discord.Object(owner)] if owner else [])
        items += [ui.TextDisplay(line), ui.ActionRow(picker)]
    if pages > 1:
        base = f"{PAGE}:{{}}:{mode}:{report_id(ref)}"
        items.append(ui.ActionRow(
            ui.Button(label="◀ Previous", custom_id=base.format(page - 1), disabled=page == 0),
            ui.Button(label="Next ▶", custom_id=base.format(page + 1), disabled=page >= pages - 1),
        ))
    items.append(ui.TextDisplay("-# Picking a member links them straight away. Clear a menu to unlink."))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=TEAL))
    return view


# --- right-click "Link characters" --------------------------------------------------------------

def member_view(member_id: int, store, note: str | None = None) -> ui.LayoutView:
    """A member's linked characters, a menu of recent raiders to add and one of theirs to remove."""
    mine = store.characters_of(member_id)
    mine_keys = {c.key for c in mine}
    candidates = [c for c in store.recent_raiders() if c.key not in mine_keys]
    # People nobody has claimed first: they're the likely ones.
    candidates.sort(key=lambda c: (store.user_for(c) is not None, c.name.casefold()))

    items: list[ui.Item] = [ui.TextDisplay(f"### 🔗 Link characters · <@{member_id}>")]
    if note:
        items.append(ui.TextDisplay(note))
    items.append(ui.TextDisplay("Linked now: " + ", ".join(f"**{c.label}**" for c in mine) if mine
                                else "No characters linked yet."))
    if candidates:
        options = [discord.SelectOption(label=c.name[:100], value=c.label[:100],
                                        description=(c.realm + (" · linked to someone else" if store.user_for(c)
                                                                else ""))[:100])
                   for c in candidates[:25]]
        items.append(ui.ActionRow(ui.Select(custom_id=f"{ADD}:{member_id}", placeholder="Add characters…",
                                            min_values=1, max_values=len(options), options=options)))
    if mine:
        options = [discord.SelectOption(label=c.name[:100], value=c.label[:100], description=c.realm[:100] or None)
                   for c in mine[:25]]
        items.append(ui.ActionRow(ui.Select(custom_id=f"{REMOVE}:{member_id}", placeholder="Remove characters…",
                                            min_values=1, max_values=len(options), options=options)))
    items.append(ui.TextDisplay("-# The list is everyone from the last 3 raids. Someone not in it? "
                                "Use /link with their character's name."))
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*items, accent_colour=TEAL))
    return view


# --- clicks ---------------------------------------------------------------------------------------

async def roster_for(bot, ref: ReportRef) -> list[Char]:
    chars = bot.store.report_characters(ref)
    if chars is None:
        night, _ = await bot.night_for(ref)  # a report that was never posted: read it from WCL
        chars = night.roster
    return chars


async def handle(bot, interaction: discord.Interaction, kind: str, rest: str) -> None:
    await interaction.response.defer()  # "update the message": the new page is sent below
    data = interaction.data or {}
    if kind in (ADD, REMOVE):
        member_id = int(rest)
        wanted = [char_from_label(v) for v in data.get("values") or []]
        if kind == ADD:
            moved = []
            for char in wanted:
                owner = bot.store.user_for(char)
                bot.store.link(char, member_id, interaction.user.id)
                if owner and owner != member_id:
                    moved.append(f"**{char.label}** (was <@{owner}>'s)")
            note = "✅ Linked " + ", ".join(f"**{c.label}**" for c in wanted)
            if moved:
                note += "\n-# Moved: " + ", ".join(moved)
        else:
            for char in wanted:
                bot.store.unlink(char)
            note = "Unlinked " + ", ".join(f"**{c.label}**" for c in wanted)
        await interaction.edit_original_response(view=_stopped(member_view(member_id, bot.store, note)),
                                                 allowed_mentions=NO_PINGS)
        return

    page, mode, site, code, *label = rest.split(":", 4)
    ref = ref_from(site, code)
    if ref is None:
        return
    note = None
    if kind == PICK and label:
        char = char_from_label(label[0])
        picked = data.get("values") or []
        users = ((data.get("resolved") or {}).get("users") or {})
        if picked and users.get(picked[0], {}).get("bot"):
            note = "That's a bot. Pick a person."
        elif picked:
            bot.store.link(char, int(picked[0]), interaction.user.id)
            note = f"✅ Linked **{char.label}** to <@{picked[0]}>"
        elif bot.store.unlink(char):
            note = f"Unlinked **{char.label}**"
    view = roster_view(await roster_for(bot, ref), bot.store.user_for, ref, int(page), mode == "a", note)
    await interaction.edit_original_response(view=_stopped(view), allowed_mentions=NO_PINGS)


def _stopped(view: ui.LayoutView) -> ui.LayoutView:
    """The bot answers these menus in on_interaction; discord.py needn't keep the view around."""
    view.stop()
    return view

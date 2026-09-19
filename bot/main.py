"""The Discord side: watches the logs channel, runs slash commands, posts reports as cards."""

import asyncio
import io
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import discord
import httpx
from discord import app_commands, ui

from .awards import Line, boss_results, build_lines, winners_by_key
from . import linking
from .charts import draw_charts
from .config import Config
from .mynight import my_night_card
from .recap import Char, Night, analyze, norm_realm
from .render import (
    Card, Chart, Rendered, card_text, featured_boss, mentioned_ids, render_report, split_message, visible_text,
)
from .store import PendingReport, Store
from .watch import check_ready
from .wcl import ReportRef, ReportUnavailable, WCLClient, WCLError, find_report_links

log = logging.getLogger("livilogs")

NO_PINGS = discord.AllowedMentions.none()
BOSS_ICON = "https://assets.rpglogs.com/img/warcraft/bosses/{}-icon.jpg"

# Buttons and menus the bot answers itself. The report is in the id, so they keep working after a
# restart: "mynight:www.warcraftlogs.com:AbCd1234".
MY_NIGHT = "mynight"
MY_NIGHT_PICK = "mynight-pick"
NIGHTS_KEPT = 8  # recent nights kept in memory for the button; older ones are fetched again


def pings_for(user_ids) -> discord.AllowedMentions:
    """Ping exactly these users and nobody else: a character name can never become @everyone."""
    return discord.AllowedMentions(
        everyone=False, roles=False, replied_user=False, users=[discord.Object(i) for i in user_ids]
    )


PRIVATE_LOG = (
    "❌ I can't read <{url}>. It's probably uploaded as **Private**; "
    "I can only read Public or Unlisted logs."
)


def parse_character(text: str, realm: str | None) -> tuple[str, str | None]:
    """Accepts 'Name' or 'Name-Realm'. Names can't contain hyphens; realms can (Azjol-Nerub)."""
    name, _, rest = text.strip().partition("-")
    return name.strip(), (realm or rest).strip() or None


def parse_characters(text: str, realm: str | None) -> list[tuple[str, str | None]]:
    """'Bob, Bobalt-Area 52' -> [('Bob', realm), ('Bobalt', 'Area 52')]: a realm typed with a
    character wins; `realm` fills in for the rest."""
    out = []
    for piece in text.split(","):
        if piece.strip():
            name, _, rest = piece.strip().partition("-")
            out.append((name.strip(), rest.strip() or (realm or "").strip() or None))
    return out


def message_text(message: discord.Message) -> str:
    """Content plus embeds, so links posted by other bots or webhooks are found too."""
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.url, embed.title, embed.description]
        parts += [f.value for f in embed.fields]
        if embed.author:
            parts.append(embed.author.url)
    return "\n".join(p for p in parts if p)


def card_view(card: Card) -> ui.LayoutView:
    """A card as a Discord components-v2 container: coloured edge, title (with the boss picture
    beside it if there is one), a divider between blocks, small print and a link button."""
    view = ui.LayoutView(timeout=None)
    header = f"### {card.title}" + (f"\n-# {card.subtitle}" if card.subtitle else "")
    items: list[ui.Item] = [
        ui.Section(ui.TextDisplay(header), accessory=ui.Thumbnail(card.thumbnail)) if card.thumbnail
        else ui.TextDisplay(header)
    ]
    for block in card.blocks:
        if isinstance(block, Chart):
            items += [ui.Separator(), ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{block.filename}"))]
        else:
            items += [ui.Separator(), ui.TextDisplay(block)]
    if card.footer:
        items.append(ui.TextDisplay(f"-# {card.footer}"))
    buttons: list[ui.Item] = [ui.Button(style=discord.ButtonStyle.primary, label=label, custom_id=custom_id)
                              for label, custom_id in card.actions]
    if card.button:
        buttons.append(ui.Button(style=discord.ButtonStyle.link, label=card.button[0], url=card.button[1]))
    if buttons:
        items.append(ui.ActionRow(*buttons))
    view.add_item(ui.Container(*items, accent_colour=card.accent))
    return view


def character_picker(night: Night, ref: ReportRef) -> ui.View:
    """Menus of everyone in the raid, for someone who isn't linked yet (25 names per menu)."""
    view = ui.View(timeout=None)
    roster = sorted(night.roster, key=lambda c: c.name.casefold())
    for i in range(0, min(len(roster), 100), 25):
        chunk = roster[i:i + 25]
        options = [discord.SelectOption(label=c.name, value=c.label, description=c.realm or None) for c in chunk]
        placeholder = "Pick your character" if len(roster) <= 25 else f"{chunk[0].name} – {chunk[-1].name}"
        view.add_item(ui.Select(custom_id=f"{MY_NIGHT_PICK}:{ref.host}:{ref.code}:{i // 25}",
                                placeholder=placeholder, options=options))
    return view


def card_files(card: Card) -> list[discord.File]:
    """The chart pictures a card's view points at (attachment://...)."""
    return [discord.File(io.BytesIO(b.png), filename=b.filename) for b in card.blocks if isinstance(b, Chart)]


@dataclass
class Built:
    rendered: Rendered
    night: Night
    lines: list[Line]


class RecapBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to see links in messages
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.config = config
        self.store = Store(config.db_path)
        self.wcl = WCLClient()
        self.tree = app_commands.CommandTree(self)
        for command in (link_command, link_raid_command, link_member_menu, unlink_command, links_command,
                        recap_command):
            self.tree.add_command(command)
        self._busy: set[ReportRef] = set()
        self._tasks: set[asyncio.Task] = set()  # keeps background checks from being garbage-collected
        self._icons: dict[int, bool] = {}  # boss id -> WCL has a picture for it
        self._nights: OrderedDict[ReportRef, tuple[Night, list[Line]]] = OrderedDict()  # for My night
        self._night_locks: dict[ReportRef, asyncio.Lock] = {}

    async def setup_hook(self) -> None:
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # shows up instantly in that server
        else:
            await self.tree.sync()  # global: can take up to an hour to appear
        self._poller = asyncio.create_task(self._poll_forever())

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        if not self.config.watch_channel_ids:
            log.warning("WATCH_CHANNEL_IDS is empty: only /recap will work, nothing is auto-posted")

    async def close(self) -> None:
        await self.wcl.close()
        await super().close()

    def resolve_character(self, name: str, realm: str | None) -> Char | str:
        """A Char, or a message saying what's missing."""
        if not name:
            return "Give a character name."
        seen = self.store.seen_named(name)
        if realm:
            for char in seen:
                if norm_realm(char.realm) == norm_realm(realm):
                    return char  # use the spelling from the log
            return Char(name, realm)
        if len(seen) == 1:
            return seen[0]
        if len(seen) > 1:
            realms = ", ".join(c.realm for c in seen)
            return f"I've seen **{name}** on more than one realm ({realms}). Type it as {name}-Realm."
        if self.config.default_realm:
            return Char(name, self.config.default_realm)
        return f"I haven't seen **{name}** in a log yet, so type it with the realm: {name}-Realm."

    # --- watching the channel ----------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        # Other bots and webhooks are fine: the log link is often posted by one.
        if message.author == self.user or message.channel.id not in self.config.watch_channel_ids:
            return
        for ref in find_report_links(message_text(message)):
            if self.store.add_pending(ref, message.channel.id, message.id):
                log.info("Watching %s", ref.url)
                await self._react(message.channel, message.id, add="👀")
                task = asyncio.create_task(self._check_now(ref))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def pending_for(self, ref: ReportRef) -> PendingReport | None:
        return next((p for p in self.store.pending() if p.ref == ref), None)

    async def _check_now(self, ref: ReportRef) -> None:
        # Links posted after raid are usually ready already; don't make people wait for the next poll.
        try:
            pending = self.pending_for(ref)
            if pending:
                await self.process(pending)
        except Exception:
            log.exception("Unexpected error on %s", ref.url)

    async def _poll_forever(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            for pending in self.store.pending():
                try:
                    await self.process(pending)
                except Exception:
                    log.exception("Unexpected error on %s", pending.ref.url)
            await asyncio.sleep(self.config.poll_minutes * 60)

    async def process(self, pending: PendingReport) -> None:
        ref = pending.ref
        if ref in self._busy or self.store.status_of(ref) != "pending":
            return
        self._busy.add(ref)
        try:
            now = int(time.time())
            timed_out = now - pending.first_seen >= self.config.max_wait_hours * 3600
            try:
                status = await self.wcl.report_status(ref)
                end_time = int(status.get("endTime") or 0)
                self.store.note_end_time(ref, end_time)
                changed = pending.last_end_time != end_time
                ready = check_ready(
                    status,
                    now,
                    self.config.quiet_minutes * 60,
                    end_time if changed else pending.last_end_time,
                    now if changed else pending.last_change_at,
                )
                if not ready.ready and not timed_out:
                    log.info("%s not ready: %s", ref.url, ready.reason)
                    return
                built = await self.build_report(ref)
            except ReportUnavailable as e:
                self.store.mark_failed(ref, str(e))
                await self.send(pending.channel_id, pending.source_message_id, PRIVATE_LOG.format(url=ref.url))
                await self._react_done(pending, "❌")
                return
            except (WCLError, httpx.HTTPError) as e:
                if not timed_out:
                    log.warning("%s: %s (will retry)", ref.url, e)
                    return
                self.store.mark_failed(ref, str(e))
                await self.send(pending.channel_id, pending.source_message_id,
                                f"❌ Gave up on <{ref.url}>: {e}")
                await self._react_done(pending, "❌")
                return

            try:
                await self.post_report(pending.channel_id, pending.source_message_id, built.rendered)
            except discord.HTTPException as e:
                # Usually a missing permission in that channel. Retrying won't fix it.
                log.error("Couldn't post the report for %s in channel %s: %s", ref.url, pending.channel_id, e)
                self.store.mark_failed(ref, f"Discord: {e}")
                return
            self.record(ref, built, pending.channel_id)
            await self._react_done(pending, "✅")
            log.info("Posted report for %s", ref.url)
        finally:
            self._busy.discard(ref)

    async def build_report(self, ref: ReportRef) -> Built:
        report = await self.wcl.report_full(ref, self.config.recap)
        night = analyze(report, self.config.recap)
        lines = build_lines(night, self.config.recap, self.store)  # the store remembers past nights
        self.store.remember(night.roster)
        self.keep_night(ref, night, lines)
        charts = {}
        if self.config.charts:
            keys = {line.chart for line in lines if line.chart}
            charts = await asyncio.to_thread(draw_charts, night, keys, self.config.recap)
        actions = [("👤 My night", f"{MY_NIGHT}:{ref.host}:{ref.code}")] if self.config.my_night else []
        rendered = render_report(
            night, lines, ref.url, self.store.user_for, self.config.timezone, self.config.thread_ping_everyone,
            thumbnail=await self.boss_picture(night), charts=charts, actions=actions,
        )
        return Built(rendered, night, lines)

    def keep_night(self, ref: ReportRef, night: Night, lines: list[Line]) -> None:
        self._nights[ref] = (night, lines)
        self._nights.move_to_end(ref)
        while len(self._nights) > NIGHTS_KEPT:
            self._nights.popitem(last=False)

    async def night_for(self, ref: ReportRef) -> tuple[Night, list[Line]]:
        """A recent night from memory, or fetched again (after a restart, or an old report). One
        fetch per report even if the whole raid presses the button at once."""
        if ref in self._nights:
            self._nights.move_to_end(ref)
            return self._nights[ref]
        lock = self._night_locks.setdefault(ref, asyncio.Lock())
        async with lock:
            kept = self._nights.get(ref)
            if kept is None:
                report = await self.wcl.report_full(ref, self.config.recap)
                night = analyze(report, self.config.recap)
                kept = (night, build_lines(night, self.config.recap, self.store))
                self.keep_night(ref, *kept)
        self._night_locks.pop(ref, None)
        return kept

    async def boss_picture(self, night: Night) -> str | None:
        """WCL's picture of the night's featured boss, if WCL has one (checked once per boss, so a
        missing picture never breaks a card)."""
        boss = featured_boss(night)
        if boss is None:
            return None
        if boss not in self._icons:
            self._icons[boss] = await self.wcl.image_exists(BOSS_ICON.format(boss))
        return BOSS_ICON.format(boss) if self._icons[boss] else None

    def record(self, ref: ReportRef, built: Built, channel_id: int | None) -> None:
        """Remember the night for next time ("last raid's best", "3 raids running")."""
        self.store.save_history(ref, built.night.start_ms, boss_results(built.night), winners_by_key(built.lines))
        self.store.mark_posted(ref, built.night.roster, channel_id)

    async def post_report(self, channel_id: int, reply_to: int | None, rendered: Rendered) -> None:
        """Headline card in the channel (as a reply to the log link), one card per section in a
        thread under it.

        The headline pings everyone it names. In the thread each person is pinged once, on their
        first mention, so being in five callouts doesn't mean five notifications.
        """
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        reference = (
            discord.MessageReference(message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False)
            if reply_to else None
        )
        headline = await self.send_card(channel, rendered.headline, mentioned_ids(visible_text(rendered.headline)),
                                        reference)
        if not rendered.thread:
            return

        target = channel
        if not isinstance(channel, discord.Thread):  # can't open a thread inside a thread
            try:
                target = await headline.create_thread(name=rendered.thread_title, auto_archive_duration=1440)
            except discord.HTTPException as e:
                log.warning("Couldn't open a thread (%s); posting the report in the channel instead", e)
        pinged: set[int] = set()
        for card in rendered.thread:
            # Only people the card visibly names: a chart's stand-in text isn't shown, so no pings from it.
            ids = [i for i in mentioned_ids(visible_text(card)) if i not in pinged] if card.pings else []
            await self.send_card(target, card, ids)
            pinged.update(ids)

    async def send_card(self, target, card: Card, ping_ids: list[int], reference=None) -> discord.Message:
        try:
            view = card_view(card)
            files = card_files(card)
            extra = {"files": files} if files else {}
            sent = await target.send(view=view, reference=reference, allowed_mentions=pings_for(ping_ids), **extra)
            view.stop()  # the bot answers its buttons in on_interaction; nothing to keep in memory
            return sent
        except discord.HTTPException as e:
            if e.status != 400:
                raise
            # Discord refused the card layout itself (a malformed part, a limit): never lose the
            # report over it, send the same content as ordinary messages.
            log.warning("Discord refused a card (%s); sending it as plain text", e)
            first = None
            for i, chunk in enumerate(split_message(card_text(card))):
                ids = [u for u in mentioned_ids(chunk) if u in ping_ids]
                sent = await target.send(chunk, reference=reference if i == 0 else None,
                                         allowed_mentions=pings_for(ids))
                first = first or sent
            return first

    async def send(self, channel_id: int, reply_to: int | None, text: str) -> None:
        """Plain notices (errors, "can't read this log"). They ping nobody."""
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        reference = (
            discord.MessageReference(message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False)
            if reply_to else None
        )
        await channel.send(text, reference=reference, allowed_mentions=NO_PINGS)

    async def _react(self, channel, message_id: int, add: str | None = None, remove: str | None = None) -> None:
        try:
            message = channel.get_partial_message(message_id)
            if remove:
                await message.remove_reaction(remove, self.user)
            if add:
                await message.add_reaction(add)
        except (discord.HTTPException, AttributeError):
            pass  # reactions are a nicety; missing permission shouldn't stop the report

    async def _react_done(self, pending: PendingReport, emoji: str) -> None:
        if pending.source_message_id:
            channel = self.get_channel(pending.channel_id)
            if channel:
                await self._react(channel, pending.source_message_id, add=emoji, remove="👀")

    # --- My night ------------------------------------------------------------------------------

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return  # slash commands are the command tree's
        data = interaction.data or {}
        kind, _, rest = str(data.get("custom_id", "")).partition(":")
        if kind in linking.KINDS:
            try:
                await linking.handle(self, interaction, kind, rest)
            except Exception:
                log.exception("Linking menu failed (%s)", kind)
                await self._reply(interaction, "❌ Something went wrong. Nothing was changed; try again.")
            return
        if kind not in (MY_NIGHT, MY_NIGHT_PICK):
            return
        host, _, code = rest.partition(":")
        code = code.partition(":")[0]  # pickers add ":<n>" so each menu's id is unique
        # Rebuilt through the link parser, so the id can only ever name a warcraftlogs.com report.
        refs = find_report_links(f"https://{host}/reports/{code}")
        if not refs:
            await interaction.response.send_message("That button is broken. Sorry!", ephemeral=True)
            return
        try:
            await self.show_my_night(interaction, refs[0], kind == MY_NIGHT_PICK)
        except Exception:
            log.exception("My night failed for %s", refs[0].url)
            await self._reply(interaction, "❌ Something went wrong. Try again in a minute.")

    async def show_my_night(self, interaction: discord.Interaction, ref: ReportRef, picked: bool) -> None:
        if ref not in self._nights:  # fetching takes a few seconds; Discord wants an answer within 3
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            night, lines = await self.night_for(ref)
        except ReportUnavailable:
            await self._reply(interaction, PRIVATE_LOG.format(url=ref.url))
            return
        except (WCLError, httpx.HTTPError) as e:
            await self._reply(interaction, f"❌ Couldn't get that log from Warcraft Logs: {e}")
            return

        if picked:
            wanted = set((interaction.data or {}).get("values") or [])
            chars = [c for c in night.roster if c.label in wanted]
        else:
            chars = [c for c in night.roster if self.store.user_for(c) == interaction.user.id]
        if not chars:
            await self._reply(interaction, "You aren't linked to anyone in this raid. Which one is you?\n"
                              "-# An admin can /link your characters so this is one click next time.",
                              view=character_picker(night, ref))
            return
        for char in chars[:3]:  # someone who swapped to an alt mid-raid gets one card each
            card = my_night_card(night, lines, char, self.config.recap, ref.url)
            await self._reply(interaction, view=card_view(card))

    async def _reply(self, interaction: discord.Interaction, content: str | None = None, view=None) -> None:
        """An answer only the person who pressed the button sees."""
        extra = {"view": view} if view is not None else {}
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, allowed_mentions=NO_PINGS, **extra)
        else:
            await interaction.response.send_message(content, ephemeral=True, allowed_mentions=NO_PINGS, **extra)
        if view is not None:
            view.stop()  # answered in on_interaction, not by discord.py's view store


def _bot(interaction: discord.Interaction) -> RecapBot:
    return interaction.client  # type: ignore[return-value]


async def _characters_autocomplete(interaction: discord.Interaction, current: str):
    """Completes the last name in a comma-separated list from characters seen in past logs."""
    done, _, last = current.rpartition(",")
    prefix = f"{done.strip()}, " if done.strip() else ""
    chars = _bot(interaction).store.search_seen(last.strip().partition("-")[0])
    values = [prefix + c.label for c in chars]
    return [app_commands.Choice(name=v, value=v) for v in values if len(v) <= 100]


async def _linked_autocomplete(interaction: discord.Interaction, current: str):
    links = _bot(interaction).store.search_links(current.partition("-")[0])
    return [app_commands.Choice(name=c.label, value=c.label) for c, _ in links]


# Linking is for admins: Discord hides these commands from anyone without Manage Server. A server
# admin can let another role use them under Server Settings -> Integrations -> LiviLogs.
@app_commands.command(name="link", description="Link WoW characters to a Discord member (admins)")
@app_commands.describe(
    member="Who the characters belong to",
    characters="One or more characters, comma-separated: Name or Name-Realm",
    realm="Realm for any character typed without one. Optional if the bot has seen them in a log",
)
@app_commands.autocomplete(characters=_characters_autocomplete)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def link_command(
    interaction: discord.Interaction,
    member: discord.Member,
    characters: str,
    realm: str | None = None,
):
    bot = _bot(interaction)
    linked: list[Char] = []
    moved: list[tuple[Char, int]] = []
    problems: list[str] = []
    for name, char_realm in parse_characters(characters, realm):
        char = bot.resolve_character(name, char_realm)
        if isinstance(char, str):
            problems.append(char)
            continue
        owner = bot.store.user_for(char)
        bot.store.link(char, member.id, interaction.user.id)
        if owner and owner != member.id:
            moved.append((char, owner))
        else:
            linked.append(char)

    lines = []
    if linked:
        lines.append(f"Linked to {member.mention}: " + ", ".join(f"**{c.label}**" for c in linked))
    lines += [f"Moved **{c.label}** from <@{old}> to {member.mention}" for c, old in moved]
    lines += problems
    if linked or moved:
        mine = bot.store.characters_of(member.id)
        lines.append(f"-# {member.display_name}'s characters: " + ", ".join(c.label for c in mine))
    await interaction.response.send_message("\n".join(lines) or "No characters given.", ephemeral=True,
                                            allowed_mentions=NO_PINGS)


@app_commands.command(name="link-raid", description="Link everyone from a raid to their Discord members (admins)")
@app_commands.describe(
    link="A Warcraft Logs report. Leave empty for the last report the bot posted",
    everyone="Also show raiders who are already linked, to review or change their links",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def link_raid_command(interaction: discord.Interaction, link: str | None = None, everyone: bool = False):
    bot = _bot(interaction)
    if link:
        refs = find_report_links(link)
        if not refs:
            await interaction.response.send_message("That doesn't look like a Warcraft Logs report link.",
                                                    ephemeral=True)
            return
        ref = refs[0]
    else:
        ref = bot.store.last_posted_report()
        if ref is None:
            await interaction.response.send_message("No report has been posted yet. Give me a report link.",
                                                    ephemeral=True)
            return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        chars = await linking.roster_for(bot, ref)
    except ReportUnavailable:
        await interaction.followup.send(PRIVATE_LOG.format(url=ref.url), ephemeral=True)
        return
    except (WCLError, httpx.HTTPError) as e:
        await interaction.followup.send(f"❌ Couldn't get that log from Warcraft Logs: {e}", ephemeral=True)
        return
    view = linking.roster_view(chars, bot.store.user_for, ref, 0, everyone)
    await interaction.followup.send(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
    view.stop()


@app_commands.context_menu(name="Link characters")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def link_member_menu(interaction: discord.Interaction, member: discord.Member):
    """Right-click a member -> Apps -> Link characters."""
    bot = _bot(interaction)
    if member.bot:
        await interaction.response.send_message("That's a bot. Pick a person.", ephemeral=True)
        return
    view = linking.member_view(member.id, bot.store)
    await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=NO_PINGS)
    view.stop()


@app_commands.command(name="unlink", description="Remove a character link (admins)")
@app_commands.describe(character="Character name, or Name-Realm", realm="Realm, if the name is linked on more than one")
@app_commands.autocomplete(character=_linked_autocomplete)
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def unlink_command(interaction: discord.Interaction, character: str, realm: str | None = None):
    bot = _bot(interaction)
    name, realm = parse_character(character, realm)
    matches = [(c, uid) for c, uid in bot.store.links_named(name)
               if not realm or norm_realm(c.realm) == norm_realm(realm)]
    if not matches:
        text = f"**{character}** isn't linked to anyone."
    elif len(matches) > 1:
        text = f"**{name}** is linked on {', '.join(c.realm for c, _ in matches)}. Type it as {name}-Realm."
    else:
        char, uid = matches[0]
        bot.store.unlink(char)
        text = f"Unlinked **{char.label}** from <@{uid}>."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


@app_commands.command(name="links", description="Show someone's linked characters, or who from the last report isn't linked")
@app_commands.describe(member="Whose characters to show. Leave empty for unlinked characters from the last report")
@app_commands.guild_only()
async def links_command(interaction: discord.Interaction, member: discord.Member | None = None):
    bot = _bot(interaction)
    if member:
        chars = bot.store.characters_of(member.id)
        text = (f"{member.mention}: " + ", ".join(f"**{c.label}**" for c in chars)) if chars else \
            f"{member.mention} has no linked characters."
    else:
        last = bot.store.last_recap_characters()
        unlinked = [c for c in last if bot.store.user_for(c) is None]
        if not last:
            text = "No report has been posted yet."
        elif unlinked:
            text = ("Not linked from the last report: " + ", ".join(f"**{c.label}**" for c in unlinked)
                    + "\n-# An admin can link them with /link-raid.")
        else:
            text = "Everyone from the last report is linked."
        mine = bot.store.characters_of(interaction.user.id)
        if mine:
            text += "\nYours: " + ", ".join(f"**{c.label}**" for c in mine)
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


@app_commands.command(name="recap", description="Post the raid report for a Warcraft Logs link right now")
@app_commands.describe(
    link="The Warcraft Logs report link",
    record_only="Only add the night to history (for streaks and progress) without posting it",
)
@app_commands.guild_only()
async def recap_command(interaction: discord.Interaction, link: str, record_only: bool = False):
    bot = _bot(interaction)
    refs = find_report_links(link)
    if not refs:
        await interaction.response.send_message("That doesn't look like a Warcraft Logs report link.", ephemeral=True)
        return
    ref = refs[0]
    pending = bot.pending_for(ref)  # someone may have run /recap while the auto-post was waiting
    if ref in bot._busy:
        await interaction.response.send_message("I'm already working on that log.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    bot._busy.add(ref)
    try:
        built = await bot.build_report(ref)
        if not record_only:
            await bot.post_report(interaction.channel_id, None, built.rendered)
    except ReportUnavailable:
        await interaction.followup.send(PRIVATE_LOG.format(url=ref.url), ephemeral=True)
        return
    except (WCLError, httpx.HTTPError) as e:
        await interaction.followup.send(f"❌ Couldn't get that log from Warcraft Logs: {e}", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Couldn't post in this channel: {e}", ephemeral=True)
        return
    finally:
        bot._busy.discard(ref)
    bot.record(ref, built, interaction.channel_id)
    if pending:
        await bot._react_done(pending, "✅")
    done = "Added that night to history without posting it." if record_only else "Posted."
    await interaction.followup.send(done, ephemeral=True)

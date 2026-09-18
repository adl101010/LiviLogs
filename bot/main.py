"""The Discord side: watches the logs channel, runs slash commands, posts recaps."""

import asyncio
import logging
import time

import discord
import httpx
from discord import app_commands

from .config import Config
from .recap import Char, build_recap, norm_realm
from .render import render, split_message
from .store import PendingReport, Store
from .watch import check_ready
from .wcl import ReportRef, ReportUnavailable, WCLClient, WCLError, find_report_links

log = logging.getLogger("wcl-bot")

# Recaps ping the people they name and nobody else: a character name can never become @everyone.
USER_PINGS_ONLY = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
NO_PINGS = discord.AllowedMentions.none()

PRIVATE_LOG = (
    "❌ I can't read <{url}>. It's probably uploaded as **Private**; "
    "I can only read Public or Unlisted logs."
)


def parse_character(text: str, realm: str | None) -> tuple[str, str | None]:
    """Accepts 'Name' or 'Name-Realm'. Names can't contain hyphens; realms can (Azjol-Nerub)."""
    name, _, rest = text.strip().partition("-")
    return name.strip(), (realm or rest).strip() or None


def message_text(message: discord.Message) -> str:
    """Content plus embeds, so links posted by other bots or webhooks are found too."""
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.url, embed.title, embed.description]
        parts += [f.value for f in embed.fields]
        if embed.author:
            parts.append(embed.author.url)
    return "\n".join(p for p in parts if p)


class RecapBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to see links in messages
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.config = config
        self.store = Store(config.db_path)
        self.wcl = WCLClient()
        self.tree = app_commands.CommandTree(self)
        for command in (link_command, unlink_command, links_command, recap_command):
            self.tree.add_command(command)
        self._busy: set[ReportRef] = set()
        self._tasks: set[asyncio.Task] = set()  # keeps background checks from being garbage-collected

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

    def is_officer(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        return self.config.officer_role_id is not None and any(
            role.id == self.config.officer_role_id for role in member.roles
        )

    def resolve_character(self, name: str, realm: str | None) -> Char | str:
        """A Char, or an error message saying what's missing."""
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
            return f"I've seen **{name}** on more than one realm ({realms}). Add the realm."
        if self.config.default_realm:
            return Char(name, self.config.default_realm)
        return f"I haven't seen **{name}** in a log yet, so add the realm: `/link {name} <realm>`."

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
                text, players = await self.build_message(ref)
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
                await self.send(pending.channel_id, pending.source_message_id, text, pings=True)
            except discord.HTTPException as e:
                # Usually a missing permission in that channel. Retrying won't fix it.
                log.error("Couldn't post the recap for %s in channel %s: %s", ref.url, pending.channel_id, e)
                self.store.mark_failed(ref, f"Discord: {e}")
                return
            self.store.mark_posted(ref, players, pending.channel_id)
            await self._react_done(pending, "✅")
            log.info("Posted recap for %s", ref.url)
        finally:
            self._busy.discard(ref)

    async def build_message(self, ref: ReportRef) -> tuple[str, list[Char]]:
        report = await self.wcl.report_full(ref, self.config.recap)
        recap = build_recap(report, self.config.recap)
        self.store.remember(recap.players)
        text, _ = render(recap, ref.url, self.config.recap, self.store.user_for)
        return text, recap.players

    async def send(self, channel_id: int, reply_to: int | None, text: str, pings: bool = False) -> None:
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        reference = (
            discord.MessageReference(message_id=reply_to, channel_id=channel_id, fail_if_not_exists=False)
            if reply_to else None
        )
        for i, chunk in enumerate(split_message(text)):
            await channel.send(
                chunk,
                reference=reference if i == 0 else None,
                allowed_mentions=USER_PINGS_ONLY if pings else NO_PINGS,
            )

    async def _react(self, channel, message_id: int, add: str | None = None, remove: str | None = None) -> None:
        try:
            message = channel.get_partial_message(message_id)
            if remove:
                await message.remove_reaction(remove, self.user)
            if add:
                await message.add_reaction(add)
        except (discord.HTTPException, AttributeError):
            pass  # reactions are a nicety; missing permission shouldn't stop the recap

    async def _react_done(self, pending: PendingReport, emoji: str) -> None:
        if pending.source_message_id:
            channel = self.get_channel(pending.channel_id)
            if channel:
                await self._react(channel, pending.source_message_id, add=emoji, remove="👀")


def _bot(interaction: discord.Interaction) -> RecapBot:
    return interaction.client  # type: ignore[return-value]


async def _character_autocomplete(interaction: discord.Interaction, current: str):
    chars = _bot(interaction).store.search_seen(current.partition("-")[0])
    return [app_commands.Choice(name=c.label, value=c.label) for c in chars]


@app_commands.command(name="link", description="Link a WoW character to a Discord member so recaps can tag them")
@app_commands.describe(
    character="Character name, or Name-Realm",
    realm="Realm. Optional if the bot has already seen the character in a log",
    member="Officers only: link the character to someone else",
)
@app_commands.autocomplete(character=_character_autocomplete)
@app_commands.guild_only()
async def link_command(
    interaction: discord.Interaction,
    character: str,
    realm: str | None = None,
    member: discord.Member | None = None,
):
    bot = _bot(interaction)
    target = member or interaction.user
    officer = bot.is_officer(interaction.user)
    if target.id != interaction.user.id and not officer:
        await interaction.response.send_message("Only officers can link characters for other people.", ephemeral=True)
        return
    char = bot.resolve_character(*parse_character(character, realm))
    if isinstance(char, str):
        await interaction.response.send_message(char, ephemeral=True)
        return
    owner = bot.store.user_for(char)
    if owner and owner != target.id and not officer:
        await interaction.response.send_message(
            f"**{char.label}** is already linked to <@{owner}>. Ask an officer to move it.", ephemeral=True
        )
        return
    bot.store.link(char, target.id, interaction.user.id)
    await interaction.response.send_message(f"Linked **{char.label}** to {target.mention}.", ephemeral=True)


@app_commands.command(name="unlink", description="Remove a character link")
@app_commands.describe(character="Character name, or Name-Realm", realm="Realm, if the name is linked on more than one")
@app_commands.guild_only()
async def unlink_command(interaction: discord.Interaction, character: str, realm: str | None = None):
    bot = _bot(interaction)
    name, realm = parse_character(character, realm)
    officer = bot.is_officer(interaction.user)
    matches = [
        (c, uid) for c, uid in bot.store.links_named(name)
        if (not realm or norm_realm(c.realm) == norm_realm(realm)) and (officer or uid == interaction.user.id)
    ]
    if not matches:
        await interaction.response.send_message(f"No link for **{character}** that you can remove.", ephemeral=True)
        return
    if len(matches) > 1:
        realms = ", ".join(c.realm for c, _ in matches)
        await interaction.response.send_message(f"**{name}** is linked on {realms}. Add the realm.", ephemeral=True)
        return
    char, uid = matches[0]
    bot.store.unlink(char)
    await interaction.response.send_message(f"Unlinked **{char.label}** from <@{uid}>.", ephemeral=True)


@app_commands.command(name="links", description="Show someone's linked characters, or who from the last recap isn't linked")
@app_commands.describe(member="Whose characters to show. Leave empty for unlinked characters from the last recap")
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
            text = "No recap has been posted yet."
        elif unlinked:
            text = "Not linked from the last recap: " + ", ".join(f"**{c.label}**" for c in unlinked)
        else:
            text = "Everyone from the last recap is linked."
        mine = bot.store.characters_of(interaction.user.id)
        if mine:
            text += "\nYours: " + ", ".join(f"**{c.label}**" for c in mine)
    await interaction.response.send_message(text, ephemeral=True)


@app_commands.command(name="recap", description="Post a raid recap for a Warcraft Logs report right now")
@app_commands.describe(link="The Warcraft Logs report link")
@app_commands.guild_only()
async def recap_command(interaction: discord.Interaction, link: str):
    bot = _bot(interaction)
    refs = find_report_links(link)
    if not refs:
        await interaction.response.send_message("That doesn't look like a Warcraft Logs report link.", ephemeral=True)
        return
    ref = refs[0]
    pending = bot.pending_for(ref)  # someone may have run /recap while the auto-post was waiting
    if ref in bot._busy:
        await interaction.response.send_message("I'm already posting a recap for that log.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    bot._busy.add(ref)
    try:
        text, players = await bot.build_message(ref)
    except ReportUnavailable:
        await interaction.followup.send(PRIVATE_LOG.format(url=ref.url))
        return
    except (WCLError, httpx.HTTPError) as e:
        await interaction.followup.send(f"❌ Couldn't get that log from Warcraft Logs: {e}")
        return
    finally:
        bot._busy.discard(ref)
    for chunk in split_message(text):
        await interaction.followup.send(chunk, allowed_mentions=USER_PINGS_ONLY)
    bot.store.mark_posted(ref, players, interaction.channel_id)
    if pending:
        await bot._react_done(pending, "✅")

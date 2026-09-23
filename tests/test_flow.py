"""The auto-post path end to end, with Discord and WCL faked out."""

import asyncio
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.config import Config, RecapSettings
from bot.main import RecapBot
from bot.recap import Char
from bot.wcl import ReportUnavailable

from .sample_report import report

LINK = "https://www.warcraftlogs.com/reports/AbCdEf1234567890"
CHANNEL = 10


def _content(content, view):
    """What a message says: its text, or the text of every card part."""
    if view is None:
        return content
    return "\n".join(item.content for item in view.walk_children() if hasattr(item, "content"))


class FakeThread:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, content=None, view=None, allowed_mentions=None, **_):
        self.sent.append(SimpleNamespace(content=_content(content, view), view=view, allowed=allowed_mentions))


class FakeMessage:
    def __init__(self, channel, content, reference, allowed, view=None):
        self.channel, self.content, self.reference, self.allowed = channel, content, reference, allowed
        self.view = view

    async def create_thread(self, name, auto_archive_duration=None):
        self.channel.thread = FakeThread(name)
        return self.channel.thread


class FakeChannel:
    id = CHANNEL

    def __init__(self, can_thread=True, refuse_cards=False):
        self.sent = []
        self.reactions = []
        self.thread = None
        self.can_thread = can_thread
        self.refuse_cards = refuse_cards

    async def send(self, content=None, reference=None, allowed_mentions=None, view=None, **_):
        if view is not None and self.refuse_cards:
            import discord
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Invalid Form Body")
        message = FakeMessage(self, _content(content, view), reference, allowed_mentions, view)
        if not self.can_thread:
            async def refuse(*a, **k):
                import discord
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
            message.create_thread = refuse
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id):
        reactions = self.reactions

        class Partial:
            async def add_reaction(self, emoji):
                reactions.append(("+", emoji))

            async def remove_reaction(self, emoji, user):
                reactions.append(("-", emoji))

        return Partial()


class FakeWCL:
    def __init__(self, end_offset_s=3600, unavailable=False):
        self.end_time = int((time.time() - end_offset_s) * 1000)
        self.unavailable = unavailable

    async def report_status(self, ref):
        if self.unavailable:
            raise ReportUnavailable("You do not have permission to view this report.")
        return {"segments": 3, "exportedSegments": 3, "endTime": self.end_time}

    async def report_full(self, ref, settings):
        return report()

    async def image_exists(self, url):
        self.checked = url
        return True

    async def close(self):
        pass


def make_bot(wcl, channel=None):
    config = Config("token", frozenset({CHANNEL}), None, None, ":memory:", 5, 20, 8,
                    ZoneInfo("UTC"), True, RecapSettings())
    bot = RecapBot(config)
    bot.wcl = wcl
    channel = channel or FakeChannel()
    bot.get_channel = lambda cid: channel
    return bot, channel


def post_link(bot, channel, content=LINK, message_id=500):
    message = SimpleNamespace(
        author=SimpleNamespace(name="someone"), channel=channel, content=content, embeds=[], id=message_id
    )

    async def run():
        await bot.on_message(message)
        await asyncio.gather(*list(bot._tasks))

    asyncio.run(run())


def pinged(allowed):
    users = allowed.users
    return sorted(u.id for u in users) if isinstance(users, list) else users


def test_finished_log_posts_headline_and_thread():
    bot, channel = make_bot(FakeWCL(end_offset_s=3600))
    bot.store.set_setting("mentions", "on")
    bot.store.link(Char("Pumper", "Area 52"), 111, 111)
    bot.store.link(Char("Dyer", "Area 52"), 555, 555)
    post_link(bot, channel)

    assert len(channel.sent) == 1
    headline = channel.sent[0]
    assert headline.view is not None  # a card, not plain text
    assert headline.content.startswith("### Raid report · Liberation of Undermine")
    pictures = [i.media.url for i in headline.view.walk_children() if type(i).__name__ == "Thumbnail"]
    assert pictures == ["https://assets.rpglogs.com/img/warcraft/bosses/3010-icon.jpg"]  # last boss killed
    assert headline.reference.message_id == 500  # replies to the link
    assert pinged(headline.allowed) == [111, 555]  # everyone named in the headline
    assert headline.allowed.everyone is False and headline.allowed.roles is False

    thread = channel.thread
    assert thread.name == "Raid report · Sep 17 · Liberation of Undermine"
    assert [m.content.splitlines()[0] for m in thread.sent] == [
        "### 🗺️ The night", "### 📊 Parses", "### 🌟 Highlights", "### 🤡 Lowlights", "### 💀 Deaths",
        "### 🧪 Consumables",
    ]
    # In the thread each person is pinged once, on their first mention.
    pings = [pinged(m.allowed) for m in thread.sent]
    assert pings[1] == [111, 555]  # the leaderboard names both
    assert all(p == [] for p in pings[2:])  # Pumper and Dyer show up again but aren't re-pinged
    assert "<@111>" in thread.sent[2].content

    assert channel.reactions == [("+", "👀"), ("-", "👀"), ("+", "✅")]
    assert bot.store.pending() == []


def test_posting_remembers_who_raided():
    bot, channel = make_bot(FakeWCL())
    post_link(bot, channel)
    # For /link-raid and the My night button; nothing about the night is kept for later reports.
    assert [c.name for c in bot.store.last_recap_characters()] == [
        "Dyer", "Greyson", "Healz", "Middling", "Pumper", "Tanky",
    ]


def test_without_thread_permission_the_report_goes_in_the_channel():
    bot, channel = make_bot(FakeWCL(), FakeChannel(can_thread=False))
    post_link(bot, channel)
    assert len(channel.sent) == 7  # headline + 6 sections
    assert channel.sent[1].content.startswith("### 🗺️ The night")


def test_a_refused_card_is_sent_as_plain_text_instead():
    bot, channel = make_bot(FakeWCL(), FakeChannel(refuse_cards=True))
    bot.store.set_setting("mentions", "on")
    bot.store.link(Char("Pumper", "Area 52"), 111, 111)
    post_link(bot, channel)
    headline = channel.sent[0]
    assert headline.view is None and headline.content.startswith("### Raid report")
    assert pinged(headline.allowed) == [111]  # pings still go out
    assert "[View log on Warcraft Logs]" in headline.content


def test_live_log_waits():
    bot, channel = make_bot(FakeWCL(end_offset_s=30))
    post_link(bot, channel)
    assert channel.sent == []
    assert len(bot.store.pending()) == 1


def test_same_link_twice_posts_once():
    bot, channel = make_bot(FakeWCL())
    post_link(bot, channel)
    post_link(bot, channel, content=f"again {LINK}", message_id=501)
    assert len(channel.sent) == 1


def test_link_inside_another_bots_embed_is_found():
    bot, channel = make_bot(FakeWCL())
    message = SimpleNamespace(
        author=SimpleNamespace(name="LogBot"), channel=channel, content="New log uploaded!",
        embeds=[SimpleNamespace(url=LINK, title="Raid", description=None, fields=[], author=None)], id=502,
    )

    async def run():
        await bot.on_message(message)
        await asyncio.gather(*list(bot._tasks))

    asyncio.run(run())
    assert len(channel.sent) == 1


def test_private_log_says_so_and_stops():
    bot, channel = make_bot(FakeWCL(unavailable=True))
    post_link(bot, channel)
    assert "Private" in channel.sent[0].content
    assert channel.sent[0].allowed.users is False  # error messages ping nobody
    assert bot.store.pending() == []


def test_other_channels_are_ignored():
    bot, channel = make_bot(FakeWCL())
    channel.id = 999
    post_link(bot, channel)
    assert channel.sent == [] and bot.store.pending() == []


def test_by_default_nobody_is_mentioned_or_pinged():
    bot, channel = make_bot(FakeWCL())
    bot.store.link(Char("Pumper", "Area 52"), 111, 111)  # linked, but mentions are off
    post_link(bot, channel)
    everything = [channel.sent[0], *channel.thread.sent]
    assert all(pinged(m.allowed) == [] for m in everything)
    assert "<@" not in "\n".join(m.content for m in everything)
    assert "**🏆 Top DPS** - **Pumper** 94.5" in channel.sent[0].content
    assert "Not linked" not in channel.thread.sent[-1].content  # no nudge to link people nobody will ping


def test_settings_command_flips_mentions():
    from bot.main import MENTION_CHOICES, settings_command

    bot, _ = make_bot(FakeWCL())
    replies = []

    async def send_message(content=None, **_):
        replies.append(content)

    interaction = SimpleNamespace(client=bot, response=SimpleNamespace(send_message=send_message))
    asyncio.run(settings_command.callback(interaction))
    assert "**Mentions** · **Off**" in replies[-1] and not bot.store.mentions
    on = next(c for c in MENTION_CHOICES if c.value == "on")
    asyncio.run(settings_command.callback(interaction, mentions=on))
    assert "**Mentions** · **On**" in replies[-1] and bot.store.mentions

"""The My night button: the card it shows, and the button round trip with Discord faked out."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord

from bot.awards import build_lines
from bot.config import RecapSettings
from bot.main import MY_NIGHT, MY_NIGHT_PICK
from bot.mynight import my_night_card
from bot.recap import Char, analyze
from bot.render import card_text

from .sample_report import report
from .test_flow import FakeWCL, make_bot, post_link

SETTINGS = RecapSettings()
FIXTURES = Path(__file__).parent / "fixtures"
BUTTON = f"{MY_NIGHT}:www.warcraftlogs.com:AbCdEf1234567890"


def card_for(name, data=None):
    night = analyze(data or report(), SETTINGS)
    char = next(c for c in night.roster if c.name == name)
    return card_text(my_night_card(night, build_lines(night, SETTINGS), char, SETTINGS, "u"))


def test_card_shows_parses_deaths_consumables_and_awards():
    text = card_for("Greyson")
    assert "### 👤 Your night · Greyson" in text
    assert "all 5 pulls" in text
    assert "**📊 20.0 average** damage parse" in text
    assert "⬜ - Boss A 10" in text and "🟩 - Boss B 30" in text
    assert "**💀 2 deaths** before the wipe was called" in text
    assert "first to die once" in text
    assert "⚠️ Flask 3/5" in text and "⚠️ Vantus 0/2" in text and "⚠️ Combat potion 1/5" in text
    assert "-# ⚠️ = the report called it out" in text
    assert "**🏅 In tonight's report** · " in text and "🗑️ Grey" in text


def test_healer_card():
    text = card_for("Healz")
    assert "healing parse" in text
    assert "Mana potions 4" in text and "⚠️ Mana" not in text  # drank mana potions: not a hoarder
    assert "🪄 3 battle rezzes" in text


def test_card_on_a_prog_night_shows_throughput_rank():
    data = json.loads((FIXTURES / "guild_prog.json").read_text(encoding="utf-8"))
    night = analyze(data, SETTINGS)
    healer = next(r.char for r in night.rates if r.role == "healers")
    text = card_text(my_night_card(night, build_lines(night, SETTINGS), healer, SETTINGS, "u"))
    assert " HPS** · 1st of " in text and "Wipes don't get parses" in text


# --- the button, end to end --------------------------------------------------------------------


class FakeResponse:
    def __init__(self, sent):
        self.sent = sent
        self.done = False
        self.deferred = False

    def is_done(self):
        return self.done

    async def defer(self, ephemeral=False, thinking=False):
        self.done = self.deferred = True

    async def send_message(self, content=None, ephemeral=False, view=None, **_):
        self.done = True
        self.sent.append(SimpleNamespace(content=content, view=view, ephemeral=ephemeral))


class FakeFollowup:
    def __init__(self, sent):
        self.sent = sent

    async def send(self, content=None, ephemeral=False, view=None, **_):
        self.sent.append(SimpleNamespace(content=content, view=view, ephemeral=ephemeral))


def press(bot, custom_id, user_id, values=None):
    sent = []
    interaction = SimpleNamespace(
        type=discord.InteractionType.component,
        data={"custom_id": custom_id, **({"values": values} if values else {})},
        user=SimpleNamespace(id=user_id),
    )
    interaction.response = FakeResponse(sent)
    interaction.followup = FakeFollowup(sent)
    asyncio.run(bot.on_interaction(interaction))
    return sent, interaction.response


def texts(view):
    return "\n".join(getattr(i, "content", "") or "" for i in view.walk_children())


def test_the_headline_has_the_button():
    bot, channel = make_bot(FakeWCL())
    post_link(bot, channel)
    ids = [i.custom_id for i in channel.sent[0].view.walk_children() if isinstance(i, discord.ui.Button)
           and i.url is None]
    assert ids == [BUTTON]


def test_thread_cards_upload_their_charts():
    bot, channel = make_bot(FakeWCL())
    uploads = []

    async def create_thread(name, auto_archive_duration=None):
        thread = channel.thread = SimpleNamespace(name=name, sent=[])

        async def send(content=None, view=None, allowed_mentions=None, files=None, **_):
            uploads.append([f.filename for f in files or []])
            thread.sent.append(view)
        thread.send = send
        return thread

    original = channel.send

    async def send(*a, **k):
        message = await original(*a, **k)
        message.create_thread = create_thread
        return message
    channel.send = send
    post_link(bot, channel)
    assert ["parses.png"] in uploads and ["consumables.png"] in uploads


def test_linked_raider_gets_their_card_privately():
    bot, channel = make_bot(FakeWCL())
    bot.store.link(Char("Greyson", "Area 52"), 444, 1)
    post_link(bot, channel)  # the night is now in memory
    sent, response = press(bot, BUTTON, 444)
    assert not response.deferred  # answered straight away, no WCL call
    assert len(sent) == 1 and sent[0].ephemeral
    assert "Your night · Greyson" in texts(sent[0].view)


def test_after_a_restart_the_log_is_fetched_again():
    bot, _ = make_bot(FakeWCL())
    bot.store.link(Char("Greyson", "Area 52"), 444, 1)
    sent, response = press(bot, BUTTON, 444)
    assert response.deferred  # "thinking..." while it fetches
    assert "Your night · Greyson" in texts(sent[0].view)


def test_someone_not_linked_picks_their_character():
    bot, channel = make_bot(FakeWCL())
    post_link(bot, channel)
    sent, _ = press(bot, BUTTON, 999)
    assert "Which one is you?" in sent[0].content and sent[0].ephemeral
    menu = next(i for i in sent[0].view.children if isinstance(i, discord.ui.Select))
    assert menu.custom_id.startswith(f"{MY_NIGHT_PICK}:www.warcraftlogs.com:AbCdEf1234567890")
    assert [o.label for o in menu.options] == ["Dyer", "Greyson", "Healz", "Middling", "Pumper", "Tanky"]

    sent, _ = press(bot, menu.custom_id, 999, values=["Pumper-Area 52"])
    assert "Your night · Pumper" in texts(sent[0].view)


def test_a_forged_button_cant_point_the_bot_at_another_site():
    bot, _ = make_bot(FakeWCL())
    sent, _ = press(bot, f"{MY_NIGHT}:evil.example.com:AbCdEf1234567890", 444)
    assert sent[0].content == "That button is broken. Sorry!"


def test_other_buttons_are_left_alone():
    bot, _ = make_bot(FakeWCL())
    sent, response = press(bot, "someone-elses-button", 444)
    assert sent == [] and not response.done

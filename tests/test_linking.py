"""Linking characters to people: admin-only, several characters (alts) per person."""

import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.awards import build_lines
from bot.config import Config, RecapSettings
from bot.main import RecapBot, link_command, links_command, parse_characters, unlink_command
from bot.recap import Char, analyze
from bot.render import card_text, render_report

from .sample_report import report


def make_bot(default_realm=None):
    config = Config("token", frozenset(), None, default_realm, ":memory:", 5, 20, 8,
                    ZoneInfo("UTC"), True, RecapSettings())
    return RecapBot(config)


class FakeResponse:
    def __init__(self):
        self.text = None

    async def send_message(self, text, **_):
        self.text = text


def run(command, bot, *args, user_id=1):
    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=user_id), response=FakeResponse())
    asyncio.run(command.callback(interaction, *args))
    return interaction.response.text


def member(uid, name):
    return SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=name)


def test_linking_is_for_admins_only():
    # Discord itself hides these from anyone without Manage Server and refuses them if called.
    assert link_command.default_permissions.manage_guild
    assert unlink_command.default_permissions.manage_guild
    assert links_command.default_permissions is None  # anyone can look


def test_parse_characters():
    assert parse_characters("Bob, Bobalt-Area 52 , Bobdruid", "Argent Dawn") == [
        ("Bob", "Argent Dawn"), ("Bobalt", "Area 52"), ("Bobdruid", "Argent Dawn"),
    ]
    assert parse_characters("Bob", None) == [("Bob", None)]
    assert parse_characters(" , ", None) == []


def test_one_person_many_characters():
    bot = make_bot()
    bot.store.remember([Char("Pumper", "Area 52"), Char("Pumpalt", "Area 52")])
    text = run(link_command, bot, member(42, "Bob"), "Pumper, Pumpalt, Pumpdruid-Argent Dawn", None)
    assert text.splitlines()[0] == "Linked to <@42>: **Pumper-Area 52**, **Pumpalt-Area 52**, **Pumpdruid-Argent Dawn**"
    assert text.splitlines()[-1] == "-# Bob's characters: Pumpalt-Area 52, Pumpdruid-Argent Dawn, Pumper-Area 52"
    assert [c.name for c in bot.store.characters_of(42)] == ["Pumpalt", "Pumpdruid", "Pumper"]


def test_whichever_alt_shows_up_tags_the_same_person():
    bot = make_bot()
    bot.store.link(Char("Pumper", "Area 52"), 42, 1)
    bot.store.link(Char("Middling", "Area 52"), 42, 1)  # same player, second character
    night = analyze(report(), RecapSettings())
    r = render_report(night, build_lines(night, RecapSettings()), "u", bot.store.user_for)
    headline = card_text(r.headline)
    assert "<@42> 94.5" in headline and "**Middling**" not in headline
    assert "<@42> damage 94.5 · <@42> damage 90.0" in headline


def test_relinking_moves_a_character_and_says_so():
    bot = make_bot()
    bot.store.remember([Char("Pumper", "Area 52")])
    run(link_command, bot, member(42, "Bob"), "Pumper", None)
    text = run(link_command, bot, member(7, "Sue"), "Pumper", None)
    assert text.splitlines()[0] == "Moved **Pumper-Area 52** from <@42> to <@7>"
    assert bot.store.user_for(Char("Pumper", "Area 52")) == 7
    assert bot.store.characters_of(42) == []


def test_unknown_characters_are_reported_not_guessed():
    bot = make_bot()
    bot.store.remember([Char("Pumper", "Area 52")])
    text = run(link_command, bot, member(42, "Bob"), "Pumper, Nobody", None)
    assert "Linked to <@42>: **Pumper-Area 52**" in text
    assert "I haven't seen **Nobody** in a log yet, so type it with the realm: Nobody-Realm." in text
    assert [c.name for c in bot.store.characters_of(42)] == ["Pumper"]


def test_default_realm_fills_in():
    bot = make_bot(default_realm="Moon Guard")
    run(link_command, bot, member(42, "Bob"), "Newbie", None)
    assert bot.store.characters_of(42) == [Char("Newbie", "Moon Guard")]


def test_unlink():
    bot = make_bot()
    bot.store.link(Char("Pumper", "Area 52"), 42, 1)
    bot.store.link(Char("Pumper", "Argent Dawn"), 43, 1)
    assert "linked on" in run(unlink_command, bot, "Pumper", None)  # two realms: ask which
    assert run(unlink_command, bot, "Pumper-Argent Dawn", None) == "Unlinked **Pumper-Argent Dawn** from <@43>."
    assert run(unlink_command, bot, "Nobody", None) == "**Nobody** isn't linked to anyone."

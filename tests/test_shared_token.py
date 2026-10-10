"""LiviLogs on a Discord application (and bot token) shared with other programs.

Other programs register their own slash commands and send their own buttons on the same
application, and every program receives every interaction. LiviLogs must only ever create its own
commands one by one, never bulk-replace or touch the global scope, and must ignore interactions that
aren't its own without replying.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from bot import ids, linking
from bot.commands_sync import STALE_COMMAND_NAMES, commands_equal, local_payloads, reconcile_guild_commands
from bot.main import LEGACY_KINDS, MY_NIGHT, MY_NIGHT_PICK, QuietTree, RecapBot
from bot.recap import Char
from bot.wcl import ReportRef

from .test_flow import FakeWCL, make_bot

APP = 1000
GUILD = 2000
OWN_COMMANDS = {"link", "link-raid", "Link characters", "unlink", "links", "recap", "settings"}


class FakeHTTP:
    """The handful of Discord calls we're allowed to make. Anything else fails the test.

    Commands are kept per server, as on Discord; `existing` seeds every server with the same set.
    """

    def __init__(self, existing=None):
        self._seed = list(existing or [])
        self.by_guild: dict[int, list[dict]] = {}
        self.upserts: list[dict] = []
        self.deleted: list[int] = []
        self.global_calls: list[str] = []

    def _commands(self, guild_id):
        return self.by_guild.setdefault(guild_id, [dict(command) for command in self._seed])

    @property
    def existing(self):
        """The commands in the test server."""
        return self._commands(GUILD)

    async def get_guild_commands(self, application_id, guild_id):
        return [dict(command) for command in self._commands(guild_id)]

    async def upsert_guild_command(self, application_id, guild_id, payload):
        self.upserts.append(payload)
        # Discord answers with the command plus the fields it adds.
        stored = {**payload, "id": str(9000 + len(self.upserts)), "application_id": str(application_id),
                  "guild_id": str(guild_id), "version": "1"}
        if payload.get("default_member_permissions") is not None:
            stored["default_member_permissions"] = str(payload["default_member_permissions"])
        commands = self._commands(guild_id)
        commands[:] = [c for c in commands if (c.get("type", 1), c["name"]) != (payload.get("type", 1), payload["name"])]
        commands.append(stored)
        return stored

    async def delete_guild_command(self, application_id, guild_id, command_id):
        self.deleted.append(command_id)
        commands = self._commands(guild_id)
        commands[:] = [c for c in commands if int(c["id"]) != command_id]

    # Anything that writes or reads the global scope, or replaces a scope in bulk, is off limits.
    def __getattr__(self, name):
        if "global" in name or "bulk" in name:
            def forbidden(*_a, **_k):
                self.global_calls.append(name)
                raise AssertionError(f"{name} must never be called")
            return forbidden
        raise AttributeError(name)


def foreign(name, id="1", type=1, **extra):
    return {"id": id, "name": name, "description": f"{name} (another program)", "type": type,
            "application_id": str(APP), "version": "1", **extra}


def run(coro):
    return asyncio.run(coro)


# --- ids ---------------------------------------------------------------------------------------

def test_new_ids_are_prefixed_and_old_ones_still_recognised():
    assert ids.tag("mynight:www.warcraftlogs.com:AbCd") == "logs:mynight:www.warcraftlogs.com:AbCd"
    assert ids.untag("logs:mynight:www.warcraftlogs.com:AbCd", LEGACY_KINDS) == "mynight:www.warcraftlogs.com:AbCd"
    # Cards posted before the prefix existed.
    assert ids.untag("mynight:www.warcraftlogs.com:AbCd", LEGACY_KINDS) == "mynight:www.warcraftlogs.com:AbCd"
    assert ids.untag("lr-pick:0:u:www:AbCd:Name-Realm", LEGACY_KINDS) == "lr-pick:0:u:www:AbCd:Name-Realm"


@pytest.mark.parametrize("custom_id", ["muse:skip", "livimuse:play-pause", "previous", "pagination:next", "", "mynightly:x"])
def test_other_programs_ids_are_not_ours(custom_id):
    assert ids.untag(custom_id, LEGACY_KINDS) is None


def test_every_component_we_create_carries_the_prefix():
    ref = ReportRef("www.warcraftlogs.com", "AbCdEf1234567890")
    chars = [Char("Pumper", "Area 52"), Char("Tanky", "Area 52"), Char("Healz", "Area 52")]
    roster = linking.roster_view(chars, lambda c: None, ref, 0, False)
    store = SimpleNamespace(characters_of=lambda member: [chars[0]], recent_raiders=lambda: chars[1:],
                            user_for=lambda c: None)
    member = linking.member_view(5, store)

    custom_ids = [i.custom_id for view in (roster, member) for i in view.walk_children()
                  if getattr(i, "custom_id", None)]

    assert custom_ids, "expected menus with ids"
    assert all(custom_id.startswith("logs:") for custom_id in custom_ids), custom_ids
    assert all(len(custom_id) <= 100 for custom_id in custom_ids)


# --- interactions that aren't ours ---------------------------------------------------------------

class Reply:
    def __init__(self):
        self.sent: list = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def defer(self, **_):
        self.done = True


def component(custom_id):
    interaction = SimpleNamespace(type=discord.InteractionType.component, data={"custom_id": custom_id},
                                  user=SimpleNamespace(id=1))
    interaction.response = Reply()
    interaction.followup = SimpleNamespace(send=lambda *a, **k: asyncio.sleep(0))
    return interaction


@pytest.mark.parametrize("custom_id", ["muse:skip", "livimuse:volume-up", "next-page", "logs:unknown-kind:1"])
def test_buttons_that_are_not_ours_get_no_reply(custom_id, monkeypatch):
    bot, _ = make_bot(FakeWCL())
    handled = []

    async def fail_handle(*args, **kwargs):
        handled.append(args)

    async def fail_show(*args, **kwargs):
        handled.append(args)

    monkeypatch.setattr(linking, "handle", fail_handle)
    monkeypatch.setattr(bot, "show_my_night", fail_show)
    interaction = component(custom_id)

    asyncio.run(bot.on_interaction(interaction))

    assert interaction.response.sent == []
    assert handled == []


def test_prefixed_and_legacy_my_night_buttons_both_reach_the_handler(monkeypatch):
    bot, _ = make_bot(FakeWCL())
    shown = []

    async def show(interaction, ref, picked):
        shown.append((ref.code, picked))

    monkeypatch.setattr(bot, "show_my_night", show)

    for custom_id in (f"logs:{MY_NIGHT}:www.warcraftlogs.com:AbCdEf1234567890",
                      f"{MY_NIGHT}:www.warcraftlogs.com:AbCdEf1234567890",
                      f"logs:{MY_NIGHT_PICK}:www.warcraftlogs.com:AbCdEf1234567890:0"):
        asyncio.run(bot.on_interaction(component(custom_id)))

    assert shown == [("AbCdEf1234567890", False), ("AbCdEf1234567890", False), ("AbCdEf1234567890", True)]


def test_quiet_tree_ignores_commands_that_are_not_ours(monkeypatch):
    bot, _ = make_bot(FakeWCL())
    calls = []

    async def base_on_error(self, interaction, error):
        calls.append(error)

    monkeypatch.setattr(app_commands.CommandTree, "on_error", base_on_error)
    tree = bot.tree  # a client can only have one command tree

    not_found = app_commands.CommandNotFound("recap-from-another-program", [])
    real_failure = app_commands.AppCommandError("boom")

    asyncio.run(tree.on_error(SimpleNamespace(), not_found))
    asyncio.run(tree.on_error(SimpleNamespace(), real_failure))

    assert calls == [real_failure]  # only the real failure is handed on
    assert isinstance(bot.tree, QuietTree)


# --- command registration ------------------------------------------------------------------------

def test_the_tree_knows_all_seven_commands_including_the_context_menu():
    bot, _ = make_bot(FakeWCL())

    payloads = local_payloads(bot.tree)

    assert {p["name"] for p in payloads} == OWN_COMMANDS
    assert {p["type"] for p in payloads if p["name"] == "Link characters"} == {2}


def test_creates_each_command_individually_and_never_uses_bulk_or_global_calls():
    bot, _ = make_bot(FakeWCL())
    http = FakeHTTP()

    result = run(reconcile_guild_commands(http, APP, GUILD, local_payloads(bot.tree)))

    assert {p["name"] for p in http.upserts} == OWN_COMMANDS
    assert sorted(result.created) == sorted(OWN_COMMANDS)
    assert http.global_calls == []
    assert http.deleted == []


def test_a_second_run_changes_nothing():
    """Discord echoes commands back with extra fields; they must compare equal to what we send."""
    bot, _ = make_bot(FakeWCL())
    http = FakeHTTP()
    payloads = local_payloads(bot.tree)

    run(reconcile_guild_commands(http, APP, GUILD, payloads))
    http.upserts.clear()
    again = run(reconcile_guild_commands(http, APP, GUILD, payloads))

    assert http.upserts == []
    assert sorted(again.unchanged) == sorted(OWN_COMMANDS)


def test_other_programs_commands_survive_in_any_start_order():
    bot, _ = make_bot(FakeWCL())
    http = FakeHTTP([foreign("play", "11"), foreign("skip", "12"), foreign("Mute", "13", type=2)])
    payloads = local_payloads(bot.tree)

    run(reconcile_guild_commands(http, APP, GUILD, payloads))
    names = {c["name"] for c in http.existing}

    assert {"play", "skip", "Mute"} <= names
    assert OWN_COMMANDS <= names
    assert http.deleted == []


def test_only_changed_commands_are_overwritten():
    bot, _ = make_bot(FakeWCL())
    http = FakeHTTP()
    payloads = local_payloads(bot.tree)
    run(reconcile_guild_commands(http, APP, GUILD, payloads))
    http.upserts.clear()
    # Someone edited /recap's description by hand.
    for command in http.existing:
        if command["name"] == "recap":
            command["description"] = "changed"

    result = run(reconcile_guild_commands(http, APP, GUILD, payloads))

    assert [p["name"] for p in http.upserts] == ["recap"]
    assert result.updated == ["recap"]


def test_stale_names_are_deleted_but_other_programs_names_never_are():
    bot, _ = make_bot(FakeWCL())
    http = FakeHTTP([foreign("old-linker", "21"), foreign("play", "22")])

    result = run(reconcile_guild_commands(http, APP, GUILD, local_payloads(bot.tree),
                                          stale_names=frozenset({"old-linker"})))

    assert http.deleted == [21]
    assert result.deleted == ["old-linker"]
    assert "play" in {c["name"] for c in http.existing}


def test_nothing_is_stale_by_default():
    # Only the current seven commands have ever existed, so there's nothing to retire.
    assert STALE_COMMAND_NAMES == frozenset()


def test_a_slash_command_and_a_context_menu_with_the_same_name_are_different_commands():
    http = FakeHTTP([foreign("link", "31", type=2)])

    result = run(reconcile_guild_commands(http, APP, GUILD, [{"name": "link", "description": "x", "type": 1}]))

    assert result.created == ["link"]
    assert http.deleted == []


def test_permission_bits_compare_whether_an_int_or_a_string():
    ours = {"name": "settings", "description": "x", "type": 1, "default_member_permissions": 32}
    theirs = {**ours, "id": "5", "default_member_permissions": "32", "dm_permission": True, "nsfw": False}

    assert commands_equal(ours, theirs)
    assert not commands_equal(ours, {**theirs, "default_member_permissions": "8"})
    assert not commands_equal({**ours, "default_member_permissions": None}, theirs)


# --- when registration happens -------------------------------------------------------------------

def bot_in(monkeypatch, guild_ids=(), joined=(GUILD,)):
    bot, _ = make_bot(FakeWCL())
    bot.config = replace(bot.config, guild_ids=frozenset(guild_ids))
    bot.http = FakeHTTP()
    bot._connection.application_id = APP
    joined_guilds = [SimpleNamespace(id=g) for g in joined]
    monkeypatch.setattr(RecapBot, "guilds", property(lambda self: joined_guilds))
    bot.get_guild = lambda guild_id: next((g for g in joined_guilds if g.id == guild_id), None)
    return bot


def test_on_ready_registers_every_server_when_none_is_configured(monkeypatch):
    bot = bot_in(monkeypatch, joined=(GUILD, GUILD + 1))

    asyncio.run(bot.on_ready())

    assert {p["name"] for p in bot.http.upserts} == OWN_COMMANDS
    assert len(bot.http.upserts) == len(OWN_COMMANDS) * 2
    assert bot.http.global_calls == []


def test_on_ready_only_registers_configured_servers(monkeypatch):
    bot = bot_in(monkeypatch, guild_ids=(GUILD,), joined=(GUILD, GUILD + 1))

    asyncio.run(bot.on_ready())

    assert len(bot.http.upserts) == len(OWN_COMMANDS)
    assert bot._synced_guilds == {GUILD}


def test_comma_separated_server_ids_are_supported(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DISCORD_GUILD_ID", "111, 222")
    from bot.config import Config

    assert Config.from_env().guild_ids == frozenset({111, 222})


def test_a_reconnect_does_not_register_again(monkeypatch):
    bot = bot_in(monkeypatch)

    asyncio.run(bot.on_ready())
    first = len(bot.http.upserts)
    asyncio.run(bot.on_ready())

    assert first == len(OWN_COMMANDS)
    assert len(bot.http.upserts) == first


def test_joining_a_server_registers_there(monkeypatch):
    bot = bot_in(monkeypatch, joined=())

    asyncio.run(bot.on_guild_join(SimpleNamespace(id=GUILD + 5)))

    assert len(bot.http.upserts) == len(OWN_COMMANDS)


def test_joining_an_unconfigured_server_registers_nothing(monkeypatch):
    bot = bot_in(monkeypatch, guild_ids=(GUILD,), joined=())

    asyncio.run(bot.on_guild_join(SimpleNamespace(id=GUILD + 5)))

    assert bot.http.upserts == []


def test_setup_hook_no_longer_syncs_the_tree(monkeypatch):
    bot, _ = make_bot(FakeWCL())
    calls = []

    async def no_sync(*args, **kwargs):
        calls.append("sync")

    async def no_copy(*args, **kwargs):
        calls.append("copy")

    monkeypatch.setattr(bot.tree, "sync", no_sync)
    monkeypatch.setattr(bot.tree, "copy_global_to", no_copy)

    async def run_hook():
        await bot.setup_hook()
        bot._poller.cancel()

    asyncio.run(run_hook())

    assert calls == []

"""/link-raid and the right-click "Link characters" menu, with Discord faked out."""

import asyncio
from types import SimpleNamespace

import discord

from bot import linking
from bot.main import link_member_menu, link_raid_command
from bot.recap import Char
from bot.wcl import ReportRef

from .test_flow import FakeWCL, make_bot, post_link

REF = ReportRef("www.warcraftlogs.com", "AbCdEf1234567890")
RAID = ["Dyer", "Greyson", "Healz", "Middling", "Pumper", "Tanky"]  # the sample report's roster


class Response:
    def __init__(self, sent):
        self.sent, self.done = sent, False

    def is_done(self):
        return self.done

    async def defer(self, **_):
        self.done = True

    async def send_message(self, content=None, view=None, ephemeral=False, **_):
        self.done = True
        self.sent.append(SimpleNamespace(content=content, view=view, ephemeral=ephemeral))


def interaction(bot, custom_id=None, values=None, resolved=None, user_id=1):
    sent = []
    data = {"custom_id": custom_id} if custom_id else {}
    if values is not None:
        data["values"] = values
    if resolved:
        data["resolved"] = {"users": resolved}

    async def followup(content=None, view=None, ephemeral=False, **_):
        sent.append(SimpleNamespace(content=content, view=view, ephemeral=ephemeral))

    async def edit(view=None, **_):
        sent.append(SimpleNamespace(content=None, view=view, ephemeral=True))

    it = SimpleNamespace(type=discord.InteractionType.component, data=data, client=bot,
                         user=SimpleNamespace(id=user_id), followup=SimpleNamespace(send=followup),
                         edit_original_response=edit)
    it.response = Response(sent)
    return it, sent


def texts(view):
    return "\n".join(getattr(i, "content", "") or "" for i in view.walk_children())


def pickers(view):
    return [i for i in view.walk_children() if isinstance(i, discord.ui.UserSelect)]


def posted_bot():
    bot, channel = make_bot(FakeWCL())
    post_link(bot, channel)  # the sample report is posted, so the bot knows who was in it
    return bot


def test_a_page_lists_unlinked_raiders_with_a_member_picker_each():
    bot = posted_bot()
    bot.store.link(Char("Tanky", "Area 52"), 77, 1)
    view = linking.roster_view(bot.store.report_characters(REF), bot.store.user_for, REF, 0, everyone=False)
    names = [p.custom_id.rsplit(":", 1)[1].split("-")[0] for p in pickers(view)]
    assert names == [n for n in RAID if n != "Tanky"]
    assert "1 of 6 not linked" not in texts(view) and "5 of 6 not linked" in texts(view)


def test_everyone_shows_linked_raiders_with_their_member_filled_in():
    bot = posted_bot()
    bot.store.link(Char("Tanky", "Area 52"), 77, 1)
    view = linking.roster_view(bot.store.report_characters(REF), bot.store.user_for, REF, 0, everyone=True)
    tanky = next(p for p in pickers(view) if ":Tanky-" in p.custom_id)
    assert [d.id for d in tanky.default_values] == [77]
    assert "**Tanky** · Area 52 → <@77>" in texts(view)


def test_big_raids_page_and_stay_inside_discords_limits():
    chars = [Char(f"Raider{i:02d}", "Moon Guard") for i in range(25)]
    view = linking.roster_view(chars, lambda c: None, REF, 2, everyone=False)
    assert "page 3 of 3" in texts(view) and len(pickers(view)) == 5
    for page in range(3):
        view = linking.roster_view(chars, lambda c: None, REF, page, everyone=False)
        assert 1 + sum(1 for _ in view.walk_children()) <= 40  # Discord's component limit
        assert all(len(i.custom_id) <= 100 for i in view.walk_children() if hasattr(i, "custom_id") and i.custom_id)


def test_picking_a_member_links_them_and_the_row_goes_away():
    bot = posted_bot()
    it, sent = interaction(bot, f"{linking.PICK}:0:u:www:AbCdEf1234567890:Greyson-Area 52", ["444"],
                           {"444": {"id": "444"}})
    asyncio.run(bot.on_interaction(it))
    assert bot.store.user_for(Char("Greyson", "Area 52")) == 444
    shown = texts(sent[-1].view)
    assert "✅ Linked **Greyson-Area 52** to <@444>" in shown and "**Greyson**" not in shown
    assert "5 of 6 not linked" in shown


def test_clearing_a_picker_unlinks():
    bot = posted_bot()
    bot.store.link(Char("Pumper", "Area 52"), 111, 1)
    it, sent = interaction(bot, f"{linking.PICK}:0:a:www:AbCdEf1234567890:Pumper-Area 52", [])
    asyncio.run(bot.on_interaction(it))
    assert bot.store.user_for(Char("Pumper", "Area 52")) is None
    assert "Unlinked **Pumper-Area 52**" in texts(sent[-1].view)


def test_bots_cant_be_linked():
    bot = posted_bot()
    it, sent = interaction(bot, f"{linking.PICK}:0:u:www:AbCdEf1234567890:Dyer-Area 52", ["9"],
                           {"9": {"id": "9", "bot": True}})
    asyncio.run(bot.on_interaction(it))
    assert bot.store.user_for(Char("Dyer", "Area 52")) is None
    assert "That's a bot" in texts(sent[-1].view)


def test_everyone_linked():
    bot = posted_bot()
    for i, name in enumerate(RAID):
        bot.store.link(Char(name, "Area 52"), 100 + i, 1)
    view = linking.roster_view(bot.store.report_characters(REF), bot.store.user_for, REF, 0, everyone=False)
    assert "✅ Everyone from this raid is linked" in texts(view) and not pickers(view)


def test_link_raid_uses_the_last_posted_report():
    bot = posted_bot()
    it, sent = interaction(bot)
    asyncio.run(link_raid_command.callback(it))
    assert sent[0].ephemeral and "6 of 6 not linked" in texts(sent[0].view)


def test_link_raid_before_any_report():
    bot, _ = make_bot(FakeWCL())
    it, sent = interaction(bot)
    asyncio.run(link_raid_command.callback(it))
    assert sent[0].content.startswith("No report has been posted yet")


def test_link_raid_with_a_report_that_was_never_posted_reads_it_from_wcl():
    bot, _ = make_bot(FakeWCL())
    it, sent = interaction(bot)
    asyncio.run(link_raid_command.callback(it, link="https://www.warcraftlogs.com/reports/AbCdEf1234567890"))
    assert "6 of 6 not linked" in texts(sent[0].view)


def test_right_click_adds_and_removes_characters():
    bot = posted_bot()
    bot.store.link(Char("Healz", "Area 52"), 555, 1)  # someone else's
    member = SimpleNamespace(id=444, bot=False)
    it, sent = interaction(bot)
    asyncio.run(link_member_menu.callback(it, member))
    view = sent[0].view
    assert "No characters linked yet." in texts(view)
    add = next(i for i in view.walk_children() if isinstance(i, discord.ui.Select))
    # Unclaimed raiders first; Healz is someone else's, so last.
    assert [o.label for o in add.options] == ["Dyer", "Greyson", "Middling", "Pumper", "Tanky", "Healz"]
    assert add.options[-1].description == "Area 52 · linked to someone else"

    it, sent = interaction(bot, f"{linking.ADD}:444", ["Pumper-Area 52", "Healz-Area 52"])
    asyncio.run(bot.on_interaction(it))
    assert bot.store.user_for(Char("Pumper", "Area 52")) == 444 == bot.store.user_for(Char("Healz", "Area 52"))
    shown = texts(sent[-1].view)
    assert "Moved: **Healz-Area 52** (was <@555>'s)" in shown
    assert "Linked now: **Healz-Area 52**, **Pumper-Area 52**" in shown

    it, sent = interaction(bot, f"{linking.REMOVE}:444", ["Healz-Area 52"])
    asyncio.run(bot.on_interaction(it))
    assert bot.store.characters_of(444) == [Char("Pumper", "Area 52")]


def test_right_clicking_a_bot():
    bot = posted_bot()
    it, sent = interaction(bot)
    asyncio.run(link_member_menu.callback(it, SimpleNamespace(id=9, bot=True)))
    assert sent[0].content == "That's a bot. Pick a person."


def test_a_forged_menu_cant_name_another_site():
    assert linking.ref_from("evil.example.com/x", "AbCdEf1234567890") is None
    assert linking.ref_from("classic", "AbCdEf1234567890") == ReportRef("classic.warcraftlogs.com", "AbCdEf1234567890")

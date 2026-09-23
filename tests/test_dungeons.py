"""Dungeon runs in a raid log: a Mythic+ key in the same log is left out of the report."""

import asyncio
import json
from pathlib import Path

import pytest

from bot.awards import build_lines
from bot.config import RecapSettings
from bot.recap import analyze, is_dungeon, raid_fights
from bot.render import card_text, render_report
from bot.wcl import NoRaidBosses, ReportRef, WCLClient, _fight_ids, _raid_fight_ids

SETTINGS = RecapSettings()
FIXTURES = Path(__file__).parent / "fixtures"
REF = ReportRef("www.warcraftlogs.com", "AbCdEf1234567890")

RAID = {"id": 7, "encounterID": 3429, "name": "The Coiled Altar", "difficulty": 4, "size": 24,
        "keystoneLevel": None, "gameZone": {"id": 3004, "name": "The Venomous Abyss"}}
KEY = {"id": 1, "encounterID": 61762, "name": "Kings' Rest", "difficulty": 10, "size": 5,
       "keystoneLevel": 15, "gameZone": {"id": 1762, "name": "Kings' Rest"}}


def mixed():
    return json.loads((FIXTURES / "mixed_night.json").read_text(encoding="utf-8"))


def test_what_counts_as_a_dungeon():
    assert is_dungeon(KEY) and not is_dungeon(RAID)
    assert is_dungeon({"size": 5})  # a five-player fight, even without a key
    assert is_dungeon({"difficulty": 10})  # Mythic+
    assert not is_dungeon({"difficulty": 5, "size": 20})  # Mythic raid


def test_a_mixed_log_reports_only_the_raid():
    night = analyze(mixed(), SETTINGS)
    assert len(night.pulls) == 17
    assert not any("Rest" in b.name or "Nalorakk" in b.name for b in night.bosses)
    # The report's own zone says "Mythic+ Season 2"; the raid's fights say where the raid was.
    assert night.zone == "The Venomous Abyss"
    assert night.dungeons == ["Kings' Rest", "Den of Nalorakk"]


def test_the_headline_says_what_was_left_out():
    night = analyze(mixed(), SETTINGS)
    rendered = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    assert "🗝️ 2 dungeon runs in this log left out: Kings' Rest, Den of Nalorakk" in rendered.headline.footer
    whole = "\n".join(card_text(c) for c in [rendered.headline, *rendered.thread])
    assert "Kings' Rest" in rendered.headline.footer and whole.count("Kings' Rest") == 1


def test_a_normal_log_says_nothing_about_dungeons():
    night = analyze(json.loads((FIXTURES / "gear_night.json").read_text(encoding="utf-8")), SETTINGS)
    assert night.dungeons == []
    rendered = render_report(night, build_lines(night, SETTINGS), "u", lambda c: None)
    assert "dungeon" not in (rendered.headline.footer or "")


def test_raid_fights_and_the_ids_sent_to_wcl():
    report = {"fights": [KEY, RAID, {"id": 8, "encounterID": 0, "name": "Trash", "size": 24,
                                     "gameZone": {"id": 3004, "name": "The Venomous Abyss"}},
                         {"id": 2, "encounterID": 0, "name": "Trash", "size": 5, "keystoneLevel": 15,
                          "gameZone": {"id": 1762, "name": "Kings' Rest"}}]}
    assert [f["id"] for f in raid_fights(report)] == [7]
    assert _fight_ids([7, 3, 11]) == "fightIDs: [3, 7, 11]"
    # With DEATHS_INCLUDE_TRASH the raid's trash comes too, but never the dungeon's.
    assert _raid_fight_ids(report["fights"], [RAID]) == [7, 8]


class FakeClient(WCLClient):
    def __init__(self, fights):
        super().__init__(http=object(), credentials=lambda host: ("id", "secret"))
        self.fights = fights
        self.queries = []

    async def query(self, host, query, variables):
        self.queries.append(query)
        return {"fights": self.fights} if len(self.queries) == 1 else {"code": "x"}


def test_a_log_with_only_a_key_run_is_refused():
    client = FakeClient([KEY])
    with pytest.raises(NoRaidBosses):
        asyncio.run(client.report_full(REF, SETTINGS))


def test_every_table_and_feed_is_asked_for_the_raids_fights_only():
    client = FakeClient([KEY, RAID])
    report = asyncio.run(client.report_full(REF, SETTINGS))
    big = client.queries[1]
    assert "fightIDs: [7]" in big and "fightIDs: [1" not in big
    assert big.count("fightIDs: [7]") >= 8  # rankings, player details, every table and event feed
    assert report["fights"] == [KEY, RAID]  # the report keeps them all, so it can say what it skipped

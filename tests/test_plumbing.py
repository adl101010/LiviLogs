import pytest

from bot.config import RecapSettings, wcl_credentials
from bot.main import parse_character
from bot.recap import Char
from bot.store import Store
from bot.watch import check_ready
from bot.wcl import ReportRef, ReportUnavailable, WCLClient, WCLError, _rankings_args, find_report_links


def test_finds_retail_and_classic_links():
    text = (
        "logs: https://www.warcraftlogs.com/reports/AbCdEf1234567890#fight=last "
        "and https://classic.warcraftlogs.com/reports/ZyXwVu0987654321?type=deaths "
        "again https://www.warcraftlogs.com/reports/AbCdEf1234567890 "
        "bare https://warcraftlogs.com/reports/QqQqQqQqQqQqQqQq"
    )
    assert find_report_links(text) == [
        ReportRef("www.warcraftlogs.com", "AbCdEf1234567890"),
        ReportRef("classic.warcraftlogs.com", "ZyXwVu0987654321"),
        ReportRef("www.warcraftlogs.com", "QqQqQqQqQqQqQqQq"),
    ]


def test_ignores_non_report_links():
    assert find_report_links("https://www.warcraftlogs.com/character/us/area-52/bob") == []
    assert find_report_links("https://evil.example/warcraftlogs.com/reports/AbCdEf1234567890") == []
    assert find_report_links("https://warcraftlogs.com.evil.example/reports/AbCdEf1234567890") == []


def test_parse_character():
    assert parse_character("Bob", None) == ("Bob", None)
    assert parse_character("Bob-Azjol-Nerub", None) == ("Bob", "Azjol-Nerub")
    assert parse_character("Bob-Area 52", "Stormrage") == ("Bob", "Stormrage")


def test_rankings_args():
    assert _rankings_args(RecapSettings()) == ""
    assert _rankings_args(RecapSettings(compare="Parses", timeframe="Today")) == ", compare: Parses, timeframe: Today"


def test_site_specific_credentials(monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "shared-id")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "shared-secret")
    monkeypatch.setenv("WCL_CLIENT_ID_CLASSIC", "classic-id")
    monkeypatch.setenv("WCL_CLIENT_SECRET_CLASSIC", "classic-secret")
    assert wcl_credentials("www.warcraftlogs.com") == ("shared-id", "shared-secret")
    assert wcl_credentials("classic.warcraftlogs.com") == ("classic-id", "classic-secret")


def test_unwrap_errors():
    with pytest.raises(ReportUnavailable):
        WCLClient._unwrap({"data": {"reportData": {"report": None}}, "errors": [
            {"message": "You do not have permission to view this report."}]})
    with pytest.raises(ReportUnavailable):
        WCLClient._unwrap({"data": {"reportData": {"report": None}}})
    with pytest.raises(ReportUnavailable):
        WCLClient._unwrap({"data": {"reportData": {"report": None}}, "errors": [
            {"message": "This report does not exist."}]})
    with pytest.raises(WCLError):
        WCLClient._unwrap({"errors": [{"message": "Internal server error"}]})
    # A query mistake is our bug, not a private log.
    with pytest.raises(WCLError) as err:
        WCLClient._unwrap({"errors": [{"message": 'Value "X" does not exist in "EventDataType" enum.'}]})
    assert not isinstance(err.value, ReportUnavailable)
    assert WCLClient._unwrap({"data": {"reportData": {"report": {"code": "x"}}}}) == {"code": "x"}


NOW = 1_758_100_000


def status(end_offset_s, segments=3, exported=3):
    return {"segments": segments, "exportedSegments": exported, "endTime": (NOW - end_offset_s) * 1000}


def test_ready_when_log_ended_a_while_ago():
    assert check_ready(status(3600), NOW, 1200, None, None).ready


def test_not_ready_while_processing_or_live():
    assert not check_ready(status(3600, exported=2), NOW, 1200, None, None).ready
    assert not check_ready(status(60), NOW, 1200, None, None).ready
    assert not check_ready({"segments": 0}, NOW, 1200, None, None).ready


def test_ready_when_growth_stops_even_if_log_clock_runs_ahead():
    s = status(-7200)  # uploader's clock two hours fast
    end = s["endTime"]
    assert not check_ready(s, NOW, 1200, end, NOW - 600).ready
    assert check_ready(s, NOW, 1200, end, NOW - 1300).ready


def test_store_links_and_reports():
    store = Store(":memory:")
    bob = Char("Bob", "Area 52")
    store.link(bob, 1, 1)
    assert store.user_for(Char("bob", "area52")) == 1
    assert store.user_for(Char("Bob", "")) == 1  # realm unknown, one link with that name
    assert store.characters_of(1) == [bob]

    store.remember([bob, Char("Alice", "Area 52")])
    assert [c.name for c in store.search_seen("al")] == ["Alice"]
    assert store.seen_named("BOB") == [bob]

    ref = ReportRef("www.warcraftlogs.com", "AbCdEf1234567890")
    assert store.add_pending(ref, 10, 20)
    assert not store.add_pending(ref, 10, 21)  # re-pasting the link doesn't queue it twice
    assert [p.ref for p in store.pending()] == [ref]
    store.note_end_time(ref, 5)
    assert store.pending()[0].last_end_time == 5

    store.mark_posted(ref, [bob, Char("Alice", "Area 52")], 10)
    assert store.pending() == []
    assert store.status_of(ref) == "posted"
    assert [c.name for c in store.last_recap_characters()] == ["Bob", "Alice"]

    assert store.unlink(bob) and store.user_for(bob) is None


def test_mana_potion_filter_uses_the_logs_own_ability_ids():
    from bot.wcl import _mana_potion_filter

    report = {"masterData": {"abilities": [
        {"gameID": 1236648, "name": "Lightfused Mana Potion"},
        {"gameID": 55, "name": "Silvermoon Health Potion"},
        {"gameID": 1200, "name": "Algari Mana Potion"},
    ]}}
    assert _mana_potion_filter(report) == "type = 'cast' and ability.id in (1200, 1236648)"
    assert _mana_potion_filter({"masterData": {"abilities": []}}) is None


def test_mana_potions_per_pull_replace_the_cast_table_count():
    from bot.config import RecapSettings
    from bot.recap import analyze

    from .sample_report import report

    data = report()
    night = analyze(data, RecapSettings())
    healz = next(c for c in night.roster if c.name == "Healz")
    assert night.mana_by_pull[healz] == {1: 1, 5: 1, 6: 2}
    assert night.mana_potions[healz] == 4

    del data["manaPotions"]  # a report saved before per-pull mana potions: the table total still works
    night = analyze(data, RecapSettings())
    assert night.mana_by_pull == {} and night.mana_potions[healz] == 4

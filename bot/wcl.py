"""Warcraft Logs API client.

Retail and every Classic flavour run the same GraphQL API on their own host (www., classic.,
fresh., vanilla.), so the host is taken from the posted link and used for every call.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import RecapSettings, wcl_credentials
from .recap import (
    CREATE_HEALTHSTONE, FLASK_PREFIXES, FOOD_PATTERN, MANA_POTION_PATTERN, is_dungeon, is_health_item,
)

log = logging.getLogger(__name__)

# The subdomain is letters only, so the host we call is always somewhere under warcraftlogs.com.
_REPORT_URL = re.compile(
    r"https?://(?:([a-z]+)\.)?warcraftlogs\.com/reports/([A-Za-z0-9]{8,32})(?![A-Za-z0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReportRef:
    host: str
    code: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/reports/{self.code}"


def find_report_links(text: str) -> list[ReportRef]:
    refs: list[ReportRef] = []
    for sub, code in _REPORT_URL.findall(text or ""):
        ref = ReportRef(host=f"{(sub or 'www').lower()}.warcraftlogs.com", code=code)
        if ref not in refs:
            refs.append(ref)
    return refs


class WCLError(Exception):
    pass


class ReportUnavailable(WCLError):
    """The report doesn't exist or is private. Retrying won't help."""


class NoRaidBosses(WCLError):
    """Nothing in this log but dungeon runs. Retrying won't help."""


_STATUS_QUERY = """
query Status($code: String!) {
  reportData {
    report(code: $code) {
      code title startTime endTime segments exportedSegments
    }
  }
}
"""

# Event times are milliseconds from the start of the report; this is "until the end".
_END = "1000000000000"

# Every fight in the log, so raid bosses can be told apart from a Mythic+ run in the same log.
_FIGHTS_QUERY = """
query Fights($code: String!) {
  reportData {
    report(code: $code) {
      fights(killType: All) {
        id encounterID name kill difficulty size keystoneLevel startTime endTime
        bossPercentage fightPercentage lastPhase friendlyPlayers
        gameZone { id name }
      }
    }
  }
}
"""

# One query for the whole report.
# - DPS and tanks are ranked on damage, healers on healing. WCL's default ranks everyone on damage,
#   which would put nearly every healer in the grey list, so both are fetched explicitly.
# - Deaths come from the events feed, not the deaths table: the table silently stops at 200.
# - Wipes have no parses, so prog nights use the damage/healing tables instead.
# - Consumables: combatantInfo is each player's buffs at the start of every pull (flask, food, rune,
#   vantus); combat potions are their buffs. Healthstones and health/mana potions are casts, and
#   come from the events feed: the casts table only lists the top five users of each ability.
_FULL_QUERY = """
query Recap($code: String!) {
  reportData {
    report(code: $code) {
      code title startTime endTime segments exportedSegments visibility
      zone { id name }
      masterData { actors(type: "Player") { id name server subType } abilities { gameID name } }
      dpsRankings: rankings(playerMetric: dps%(rankings_args)s)
      hpsRankings: rankings(playerMetric: hps%(rankings_args)s)
      playerDetails(%(night)s, includeCombatantInfo: false)
      damageDone: table(dataType: DamageDone, %(night)s)
      healing: table(dataType: Healing, %(night)s)
      damageTaken: table(dataType: DamageTaken, %(night)s)
      interrupts: table(dataType: Interrupts, %(night)s)
      dispels: table(dataType: Dispels, %(night)s)
      deathEvents: events(dataType: Deaths, %(death_fights)s, startTime: 0, endTime: %(end)s, limit: 10000) {
        data nextPageTimestamp
      }
      resurrectEvents: events(filterExpression: "type = 'resurrect'", %(night)s, limit: 10000) {
        data nextPageTimestamp
      }
      combatantInfo: events(dataType: CombatantInfo, %(night)s, limit: 10000) { data nextPageTimestamp }
      potionEvents: events(filterExpression: "%(potion_filter)s", %(night)s, limit: 10000) {
        data nextPageTimestamp
      }
      powerInfusion: events(filterExpression: "%(pi_filter)s", %(night)s, limit: 10000) {
        data nextPageTimestamp
      }
    }
  }
}
"""

# Follow-up pages for either events feed (only on enormous nights: 10,000 per page).
_MORE_EVENTS_QUERY = """
query MoreEvents(%(params)s) {
  reportData {
    report(code: $code) {
      page: events(%(filter)s, startTime: $start, endTime: %(end)s, limit: 10000) { data nextPageTimestamp }
    }
  }
}
"""
# Both ends of the buff, so the report knows when each Power Infusion started and finished.
PI_FILTER = "type in ('applybuff', 'removebuff') and ability.name = 'Power Infusion'"

_EVENT_PAGES = {
    "deathEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "dataType: Deaths, %(death_fights)s",
    },
    "resurrectEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"type = 'resurrect'\", %(fights)s",
    },
    "combatantInfo": {
        "params": "$code: String!, $start: Float!",
        "filter": "dataType: CombatantInfo, %(fights)s",
    },
    "powerInfusion": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"%(pi_filter)s\", %(fights)s",
    },
    "potionEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"%(potion_filter)s\", %(fights)s",
    },
    "manaPotionEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"%(mana_filter)s\", %(fights)s",
    },
    "healthItemEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"%(health_filter)s\", %(fights)s",
    },
    "buffEndEvents": {
        "params": "$code: String!, $start: Float!",
        "filter": "filterExpression: \"%(buff_end_filter)s\", %(fights)s",
    },
}


def _buff_end_filter(report: dict) -> str | None:
    """WCL filter for flask and food buffs coming off, by the ids those buffs have in this log's
    pull snapshots, or None if nobody had one."""
    ids = set()
    for snapshot in report.get("combatantInfo") or []:
        for aura in snapshot.get("auras") or []:
            name = aura.get("name") or ""
            if isinstance(aura.get("ability"), int) and (name.startswith(FLASK_PREFIXES) or FOOD_PATTERN in name):
                ids.add(aura["ability"])
    if not ids:
        return None
    return f"type = 'removebuff' and ability.id in ({', '.join(map(str, sorted(ids)))})"


# Mana potions change name every expansion, and WCL's filters can't match part of a name. So the
# report's own list of abilities is searched for MANA_POTION_PATTERN, and the casts fetched by id.
def _mana_potion_filter(report: dict) -> str | None:
    """WCL filter for casts of any mana potion that appears in this report, or None if none does."""
    abilities = (report.get("masterData") or {}).get("abilities") or []
    ids = sorted({a["gameID"] for a in abilities
                  if MANA_POTION_PATTERN in (a.get("name") or "") and isinstance(a.get("gameID"), int)})
    if not ids:
        return None
    return f"type = 'cast' and ability.id in ({', '.join(map(str, ids))})"


# Healthstones and health potions are fetched the same way, and for a second reason: the casts
# table only ever lists the top five users of an ability, so everyone else read as zero and got
# called out for never using one.
def _health_item_filter(report: dict, settings: RecapSettings) -> str | None:
    """WCL filter for casts of any healthstone or health potion in this report, or None if none.
    "Create Healthstone" is the warlock making them, not anyone using one."""
    abilities = (report.get("masterData") or {}).get("abilities") or []
    ids = sorted({a["gameID"] for a in abilities
                  if isinstance(a.get("gameID"), int) and (a.get("name") or "") != CREATE_HEALTHSTONE
                  and is_health_item(a.get("name") or "", settings.extra_health_items)})
    if not ids:
        return None
    return f"type = 'cast' and ability.id in ({', '.join(map(str, ids))})"


def _potion_filter(settings: RecapSettings) -> str:
    """WCL filter for the combat potion buffs named in COMBAT_POTIONS, escaped for a GraphQL string.
    Names go in as quoted strings, so Light's Potential becomes 'Light''s Potential'."""
    if not settings.combat_potions:
        return "type = 'applybuff' and ability.id = 0"  # matches nothing
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in settings.combat_potions)
    expression = f"type = 'applybuff' and ability.name in ({names})"
    return expression.replace("\\", "\\\\").replace('"', '\\"')


def _rankings_args(settings: RecapSettings, fights: str) -> str:
    # Enum values go in as literals; leaving an argument out means "same as the report page".
    args = f", {fights}"
    if settings.compare:
        args += f", compare: {settings.compare}"
    if settings.timeframe:
        args += f", timeframe: {settings.timeframe}"
    return args


def _fight_ids(ids) -> str:
    """The fights every table, ranking and event feed is asked for: "fightIDs: [1, 2, 3]"."""
    return f"fightIDs: [{', '.join(str(i) for i in sorted(ids))}]"


def _raid_fight_ids(fights: list[dict], bosses: list[dict]) -> list[int]:
    """Boss pulls plus the trash between them, for DEATHS_INCLUDE_TRASH. Trash in a dungeon run is
    left out by matching the raid's own zones."""
    zones = {(f.get("gameZone") or {}).get("id") for f in bosses}
    boss_ids = {f["id"] for f in bosses}
    return [f["id"] for f in fights
            if f.get("id") is not None and (f["id"] in boss_ids
                                            or (not is_dungeon(f) and (f.get("gameZone") or {}).get("id") in zones))]


class WCLClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        credentials: Callable[[str], tuple[str, str]] = wcl_credentials,
    ):
        self._http = http or httpx.AsyncClient(timeout=60)
        self._credentials = credentials
        self._tokens: dict[str, tuple[str, float]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _token(self, host: str, refresh: bool = False) -> str:
        cached = self._tokens.get(host)
        if cached and not refresh and cached[1] > time.time() + 60:
            return cached[0]
        client_id, secret = self._credentials(host)
        resp = await self._http.post(
            f"https://{host}/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, secret),
        )
        if resp.status_code != 200:
            raise WCLError(f"WCL login failed on {host} (HTTP {resp.status_code}). Check the client ID/secret.")
        body = resp.json()
        self._tokens[host] = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return body["access_token"]

    async def query(self, host: str, query: str, variables: dict) -> dict:
        for attempt in range(2):
            token = await self._token(host, refresh=attempt > 0)
            resp = await self._http.post(
                f"https://{host}/api/v2/client",
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                continue  # token expired early; get a new one and retry once
            if resp.status_code == 429:
                raise WCLError("WCL rate limit hit. Try again later.")
            if resp.status_code != 200:
                raise WCLError(f"WCL returned HTTP {resp.status_code}")
            body = resp.json()
            return self._unwrap(body)
        raise WCLError("WCL rejected the login token twice")

    @staticmethod
    def _unwrap(body: dict) -> dict:
        report = ((body.get("data") or {}).get("reportData") or {}).get("report")
        errors = body.get("errors") or []
        if report is None:
            message = "; ".join(e.get("message", "") for e in errors) or "report not found"
            # Only WCL's own "no such report" / "not allowed" answers. A query mistake also says
            # things like "does not exist in enum" and must not be reported as a private log.
            lowered = message.lower()
            if not errors or "this report does not exist" in lowered or "permission" in lowered:
                raise ReportUnavailable(message)
            raise WCLError(message)
        if errors:
            # Partial data would mean a recap with a section quietly missing. Fail and retry instead.
            raise WCLError("; ".join(e.get("message", "") for e in errors))
        return report

    async def image_exists(self, url: str) -> bool:
        try:
            resp = await self._http.head(url, timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/")

    async def report_status(self, ref: ReportRef) -> dict:
        return await self.query(ref.host, _STATUS_QUERY, {"code": ref.code})

    async def report_full(self, ref: ReportRef, settings: RecapSettings) -> dict:
        # The fight list comes first: a raid log often has a Mythic+ run in it, and every table,
        # ranking and event below is then asked for the raid's fights only.
        fights = (await self.query(ref.host, _FIGHTS_QUERY, {"code": ref.code})).get("fights") or []
        bosses = [f for f in fights if (f.get("encounterID") or 0) > 0 and not is_dungeon(f)]
        if not bosses:
            raise NoRaidBosses("no raid bosses in this log")
        raid = _fight_ids(f["id"] for f in bosses)
        with_trash = _fight_ids(_raid_fight_ids(fights, bosses)) if settings.deaths_include_trash else raid

        potion_filter = _potion_filter(settings)
        query = _FULL_QUERY % {"rankings_args": _rankings_args(settings, raid), "end": _END, "night": raid,
                               "death_fights": with_trash, "potion_filter": potion_filter, "pi_filter": PI_FILTER}
        variables = {"code": ref.code}
        report = await self.query(ref.host, query, variables)
        report["fights"] = fights
        filters = {"potion_filter": potion_filter, "mana_filter": _mana_potion_filter(report),
                   "pi_filter": PI_FILTER, "fights": raid, "death_fights": with_trash}
        for field, key in (("deathEvents", "deaths"), ("resurrectEvents", "resurrects"),
                           ("combatantInfo", "combatantInfo"), ("potionEvents", "potions"),
                           ("powerInfusion", "powerInfusion")):
            report[key] = await self._all_events(ref, report.pop(field, None), field, variables, filters)
        # Healthstones, health potions and mana potions: small follow-up queries, since their
        # ability ids have to be read out of the report first.
        filters["health_filter"] = _health_item_filter(report, settings)
        report["healthItems"] = (
            await self._all_events(ref, {"nextPageTimestamp": 0}, "healthItemEvents", variables, filters)
            if filters["health_filter"] else []
        )
        report["manaPotions"] = (
            await self._all_events(ref, {"nextPageTimestamp": 0}, "manaPotionEvents", variables, filters)
            if filters["mana_filter"] else []
        )
        # Flasks and food running out mid-pull: their buffs' ids come from the pull snapshots.
        filters["buff_end_filter"] = _buff_end_filter(report)
        report["buffEnds"] = (
            await self._all_events(ref, {"nextPageTimestamp": 0}, "buffEndEvents", variables, filters)
            if filters["buff_end_filter"] else []
        )
        return report

    async def _all_events(self, ref: ReportRef, page: dict | None, field: str, variables: dict,
                          filters: dict[str, str | None]) -> list:
        """Every event of one feed: the page already fetched, then any further pages. Passing
        {"nextPageTimestamp": 0} as the page fetches the whole feed from the start."""
        page = page or {}
        events = list(page.get("data") or [])
        spec = _EVENT_PAGES[field]
        while page.get("nextPageTimestamp") is not None:
            wanted = {k: v for k, v in variables.items() if f"${k}" in spec["params"]}
            filter_text = spec["filter"] % filters if "%(" in spec["filter"] else spec["filter"]
            more = await self.query(
                ref.host,
                _MORE_EVENTS_QUERY % {"params": spec["params"], "filter": filter_text, "end": _END},
                {**wanted, "start": page["nextPageTimestamp"]},
            )
            page = more.get("page") or {}
            events += page.get("data") or []
        return events

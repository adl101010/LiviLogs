"""Turns raw WCL report JSON into the recap numbers. No network, no Discord: easy to test.

WCL marks the rankings and table JSON as "not frozen", so every read of that JSON lives here and
tolerates missing fields rather than crashing a raid-night post.
"""

import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import RecapSettings

TANK = "tanks"

# Letters that don't decompose into base letter + accent.
_FOLD = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ð": "d", "þ": "th", "ł": "l", "đ": "d"})


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold().translate(_FOLD))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_name(name: str) -> str:
    return _fold(name).strip()


def norm_realm(realm: str) -> str:
    # "Area 52", "Area52", "area-52" all match; so do "Azjol-Nerub" and "AzjolNerub".
    return "".join(ch for ch in _fold(realm) if ch.isalnum())


@dataclass(frozen=True)
class Char:
    name: str
    realm: str

    @property
    def key(self) -> tuple[str, str]:
        return norm_name(self.name), norm_realm(self.realm)

    @property
    def label(self) -> str:
        return f"{self.name}-{self.realm}" if self.realm else self.name


@dataclass
class ParseLine:
    char: Char
    average: float  # rounded to 1 decimal, the number people see
    kills: int
    role: str


@dataclass
class DeathLine:
    char: Char
    deaths: int
    first_deaths: int


@dataclass
class Recap:
    title: str
    zone: str | None
    start_ms: int
    difficulty: int | None
    kills: int
    wipes: int
    processing: bool
    has_parses: bool
    high: list[ParseLine] = field(default_factory=list)
    grey: list[ParseLine] = field(default_factory=list)
    deaths: list[DeathLine] = field(default_factory=list)
    players: list[Char] = field(default_factory=list)


def _server_name(server) -> str:
    if isinstance(server, dict):
        return server.get("name") or ""
    return server or ""


def _players(report: dict) -> dict[int, Char]:
    actors = ((report.get("masterData") or {}).get("actors")) or []
    return {
        a["id"]: Char(a.get("name") or "?", _server_name(a.get("server")))
        for a in actors
        if a.get("id") is not None and (a.get("type") in (None, "Player"))
    }


def _ranking_fights(rankings) -> list[dict]:
    if isinstance(rankings, dict):
        rankings = rankings.get("data", rankings)
    return rankings if isinstance(rankings, list) else []


def _parses(report: dict, players: dict[int, Char]) -> dict[Char, list[tuple[float, str]]]:
    realm_by_name: dict[str, set[str]] = defaultdict(set)
    for char in players.values():
        realm_by_name[norm_name(char.name)].add(char.realm)

    parses: dict[Char, list[tuple[float, str]]] = defaultdict(list)
    for fight in _ranking_fights(report.get("rankings")):
        if fight.get("kill") in (0, False):
            continue
        roles = fight.get("roles") or {}
        for role_key, role in roles.items():
            characters = role.get("characters") if isinstance(role, dict) else None
            for c in characters or []:
                pct = c.get("rankPercent")
                name = c.get("name")
                if pct is None or not name:
                    continue
                realm = _server_name(c.get("server"))
                if not realm:
                    known = realm_by_name.get(norm_name(name), set())
                    realm = next(iter(known)) if len(known) == 1 else ""
                parses[Char(name, realm)].append((float(pct), role_key.lower()))
    return parses


def _parse_lines(parses: dict[Char, list[tuple[float, str]]]) -> list[ParseLine]:
    lines = []
    for char, entries in parses.items():
        roles = Counter(role for _, role in entries)
        # A player counts as a tank for the night only if they tanked most of their kills.
        others = Counter({r: n for r, n in roles.items() if r != TANK})
        role = TANK if roles[TANK] * 2 > len(entries) or not others else others.most_common(1)[0][0]
        average = round(sum(pct for pct, _ in entries) / len(entries), 1)
        lines.append(ParseLine(char, average, len(entries), role))
    return lines


def _death_lines(report: dict, players: dict[int, Char], fight_ids: set[int], settings: RecapSettings) -> list[DeathLine]:
    table = report.get("deaths") or {}
    table = table.get("data", table) if isinstance(table, dict) else {}
    entries = table.get("entries") or []
    by_name = {norm_name(c.name): c for c in players.values()}

    per_fight: dict[int, list[tuple[float, Char]]] = defaultdict(list)
    for e in entries:
        fight = e.get("fight")
        if fight_ids and fight not in fight_ids and not settings.deaths_include_trash:
            continue
        char = players.get(e.get("id")) or by_name.get(norm_name(e.get("name") or ""))
        if char is None:
            continue  # pets, NPCs
        per_fight[fight].append((e.get("timestamp") or 0, char))

    deaths: Counter[Char] = Counter()
    firsts: Counter[Char] = Counter()
    for fight_deaths in per_fight.values():
        fight_deaths.sort(key=lambda d: d[0])
        # Deaths after the wipe is called don't count against anyone.
        counted = fight_deaths[: settings.wipe_cutoff] if settings.wipe_cutoff > 0 else fight_deaths
        for _, char in counted:
            deaths[char] += 1
        if counted:
            firsts[counted[0][1]] += 1

    ranked = sorted(deaths, key=lambda c: (-deaths[c], -firsts[c], c.name.casefold()))
    top: list[DeathLine] = []
    for char in ranked:
        if len(top) >= settings.deaths_top_n and deaths[char] < top[-1].deaths:
            break  # ties with the last place still get called out
        top.append(DeathLine(char, deaths[char], firsts[char]))
    return top


def build_recap(report: dict, settings: RecapSettings) -> Recap:
    players = _players(report)
    fights = [f for f in (report.get("fights") or []) if (f.get("encounterID") or 0) > 0]
    kills = [f for f in fights if f.get("kill")]
    difficulties = Counter(f.get("difficulty") for f in kills or fights if f.get("difficulty"))

    lines = _parse_lines(_parses(report, players))
    high = sorted(
        (p for p in lines if p.average >= settings.parse_high),
        key=lambda p: (-p.average, p.char.name.casefold()),
    )
    grey = sorted(
        (
            p for p in lines
            if p.average < settings.parse_grey and (settings.grey_include_tanks or p.role != TANK)
        ),
        key=lambda p: (p.average, p.char.name.casefold()),
    )

    segments = report.get("segments") or 0
    exported = report.get("exportedSegments") or 0
    zone = report.get("zone") or {}

    seen = {c.key: c for c in players.values()}
    for p in lines:
        seen.setdefault(p.char.key, p.char)

    return Recap(
        title=report.get("title") or "Raid",
        zone=zone.get("name"),
        start_ms=int(report.get("startTime") or 0),
        difficulty=difficulties.most_common(1)[0][0] if difficulties else None,
        kills=len(kills),
        wipes=len(fights) - len(kills),
        processing=exported < segments,
        has_parses=bool(lines),
        high=high,
        grey=grey,
        deaths=_death_lines(report, players, {f["id"] for f in fights if "id" in f}, settings),
        players=sorted(seen.values(), key=lambda c: c.name.casefold()),
    )

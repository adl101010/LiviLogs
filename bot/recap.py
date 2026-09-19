"""Raw WCL report JSON -> facts about the night. No network, no Discord: easy to test.

WCL marks its rankings, tables and events JSON as "not frozen", so every read of that JSON lives
here and tolerates missing fields rather than crashing a raid-night post. Shape checked against
real retail and Classic logs on 2026-09-18 (see tests/fixtures).
"""

import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .config import RecapSettings

TANK = "tanks"
HEALER = "healers"
DPS = "dps"

# Consumables whose names follow a pattern that has held across expansions. The ones that don't
# (combat potions, the tryhard augment rune) are listed in settings instead.
HEALTH_ITEM_PATTERNS = ("Healthstone", "Health Potion", "Healing Potion")
MANA_POTION_PATTERN = "Mana Potion"
FLASK_PREFIXES = ("Flask of", "Phial of")
FOOD_PATTERN = "Well Fed"
VANTUS_PREFIX = "Vantus Rune"
RETAIL = 1  # WCL's gameVersion for retail; Classic flavours have their own numbers

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
class Pull:
    id: int
    encounter_id: int
    boss: str
    kill: bool
    difficulty: int | None
    start: int  # ms from the start of the report
    end: int
    boss_pct: float | None  # boss health left when it ended; 0 on a kill
    phase: int | None
    players: frozenset[int]

    @property
    def seconds(self) -> float:
        return max(0, self.end - self.start) / 1000


@dataclass
class Boss:
    encounter_id: int
    name: str
    difficulty: int | None
    pulls: list[Pull]

    @property
    def killed(self) -> bool:
        return any(p.kill for p in self.pulls)

    @property
    def wipes(self) -> list[Pull]:
        return [p for p in self.pulls if not p.kill]

    @property
    def best_wipe(self) -> Pull | None:
        wipes = [p for p in self.wipes if p.boss_pct is not None]
        return min(wipes, key=lambda p: (p.boss_pct, p.start)) if wipes else None


@dataclass
class ParseLine:
    char: Char
    average: float  # rounded to 1 decimal. WCL's site shows the same average with the decimals dropped
    role: str
    parses: list[tuple[float, str]]  # (percent, boss) per kill

    @property
    def kills(self) -> int:
        return len(self.parses)


@dataclass
class Rate:
    """Raw damage (DPS, tanks) or healing (healers) per second, over the pulls they were in."""

    char: Char
    role: str
    per_second: float
    pulls: int


@dataclass
class Death:
    pull: int
    time: int
    char: Char
    ability: str | None


@dataclass
class DeathLine:
    char: Char
    deaths: int
    first_deaths: int


@dataclass
class Night:
    title: str
    zone: str | None
    start_ms: int
    processing: bool
    pulls: list[Pull]
    bosses: list[Boss]
    roster: list[Char]  # everyone who was in a boss pull
    roles: dict[Char, str]
    parses: list[ParseLine]
    rates: list[Rate]
    deaths: dict[int, list[Death]]  # every death, per pull, in order
    counted: dict[int, list[Death]]  # the first WIPE_CUTOFF deaths of each pull
    floor: list[DeathLine]
    floor_tied_more: int
    interrupts: Counter
    dispels: Counter
    damage_done: dict[Char, float]
    damage_taken: dict[Char, float]
    brezzed: Counter
    brez_given: Counter
    high: list[ParseLine] = field(default_factory=list)
    grey: list[ParseLine] = field(default_factory=list)
    # Consumables (retail only: Classic's potions and elixirs need their own lists).
    retail: bool = False
    pulls_in: dict[Char, set[int]] = field(default_factory=dict)
    auras_at_pull: dict[int, dict[Char, list[str]]] = field(default_factory=dict)  # pull -> player -> buffs
    potion_pulls: dict[Char, set[int]] = field(default_factory=dict)  # pulls with a combat potion
    potions: Counter = field(default_factory=Counter)  # combat potions
    casts_known: bool = False  # False: no casts data, so "never used a healthstone" can't be judged
    health_items: Counter = field(default_factory=Counter)  # healthstones and health potions
    mana_potions: Counter = field(default_factory=Counter)
    dead_seconds: Counter = field(default_factory=Counter)  # time spent dead in boss pulls
    power_infusion: Counter = field(default_factory=Counter)  # (giver, receiver) -> times, self-casts excluded
    potions_by_pull: dict[Char, Counter] = field(default_factory=dict)  # combat potions: pull -> how many
    mana_by_pull: dict[Char, Counter] = field(default_factory=dict)  # mana potions: pull -> how many (if known)
    gear_at_pull: dict[int, dict[Char, list["Item | None"]]] = field(default_factory=dict)  # pull -> player -> slots
    specs: dict[Char, int] = field(default_factory=dict)  # WoW spec id
    active: dict[Char, float] = field(default_factory=dict)  # share of time alive in pulls spent acting
    ran_out: list["RanOut"] = field(default_factory=list)  # flask/food buffs that expired mid-pull

    @property
    def has_parses(self) -> bool:
        return bool(self.parses)

    @property
    def kills(self) -> int:
        return sum(1 for p in self.pulls if p.kill)

    @property
    def wipes(self) -> int:
        return sum(1 for p in self.pulls if not p.kill)

    @property
    def difficulty(self) -> int | None:
        kills = [p for p in self.pulls if p.kill]
        counts = Counter(p.difficulty for p in (kills or self.pulls) if p.difficulty)
        return counts.most_common(1)[0][0] if counts else None

    @property
    def boss_seconds(self) -> float:
        return sum(p.seconds for p in self.pulls)

    @property
    def span_seconds(self) -> float:
        if not self.pulls:
            return 0
        return (max(p.end for p in self.pulls) - min(p.start for p in self.pulls)) / 1000


# --- reading WCL's JSON ------------------------------------------------------------------------


def _server_name(server) -> str:
    if isinstance(server, dict):
        return server.get("name") or ""
    return server or ""


def _data(node):
    """Tables, rankings and playerDetails wrap their payload in {"data": ...}."""
    return node.get("data", node) if isinstance(node, dict) else node


def _pulls(report: dict) -> list[Pull]:
    pulls = []
    for f in report.get("fights") or []:
        if (f.get("encounterID") or 0) <= 0 or f.get("id") is None:
            continue
        kill = bool(f.get("kill"))
        boss_pct = f.get("bossPercentage")
        pulls.append(Pull(
            id=f["id"],
            encounter_id=f["encounterID"],
            boss=f.get("name") or "Boss",
            kill=kill,
            difficulty=f.get("difficulty"),
            start=int(f.get("startTime") or 0),
            end=int(f.get("endTime") or 0),
            boss_pct=0.0 if kill else (float(boss_pct) if isinstance(boss_pct, (int, float)) else None),
            phase=f.get("lastPhase") or None,
            players=frozenset(f.get("friendlyPlayers") or []),
        ))
    return sorted(pulls, key=lambda p: p.start)


def _bosses(pulls: list[Pull]) -> list[Boss]:
    grouped: dict[tuple, list[Pull]] = {}
    for p in pulls:
        grouped.setdefault((p.encounter_id, p.difficulty), []).append(p)
    return [Boss(eid, ps[0].boss, diff, ps) for (eid, diff), ps in grouped.items()]


def _players(report: dict, pulls: list[Pull]) -> dict[int, Char]:
    """Players who were in at least one boss pull.

    WCL's actor list also has everyone who merely walked past in the log (105 "players" for a
    16-person raid), so it's narrowed to each pull's friendlyPlayers when WCL provides them.
    """
    actors = ((report.get("masterData") or {}).get("actors")) or []
    raiders: set[int] = set()
    for p in pulls:
        raiders |= p.players
    return {
        a["id"]: Char(a.get("name") or "?", _server_name(a.get("server")))
        for a in actors
        if a.get("id") is not None
        and a.get("type") in (None, "Player")
        and (not raiders or a["id"] in raiders)
    }


def _ranking_fights(rankings) -> list[dict]:
    rankings = _data(rankings)
    return rankings if isinstance(rankings, list) else []


def _parses(report: dict, players: dict[int, Char]) -> dict[Char, list[tuple[float, str, str]]]:
    realm_by_name: dict[str, set[str]] = defaultdict(set)
    for char in players.values():
        realm_by_name[norm_name(char.name)].add(char.realm)

    # Healers' parses come from the healing rankings, everyone else's from the damage rankings.
    sources = [
        (report.get("dpsRankings"), lambda role: role != HEALER),
        (report.get("hpsRankings"), lambda role: role == HEALER),
    ]
    parses: dict[Char, list[tuple[float, str, str]]] = defaultdict(list)
    for rankings, wanted in sources:
        for fight in _ranking_fights(rankings):
            if fight.get("kill") in (0, False):
                continue
            boss = (fight.get("encounter") or {}).get("name") or "a boss"
            for role_key, role in (fight.get("roles") or {}).items():
                role_key = role_key.lower()
                if not wanted(role_key) or not isinstance(role, dict):
                    continue
                for c in role.get("characters") or []:
                    pct = c.get("rankPercent")
                    name = c.get("name")
                    if not isinstance(pct, (int, float)) or not name:
                        continue  # "-" when WCL has nothing to rank against
                    realm = _server_name(c.get("server"))
                    if not realm:
                        known = realm_by_name.get(norm_name(name), set())
                        realm = next(iter(known)) if len(known) == 1 else ""
                    parses[Char(name, realm)].append((float(pct), role_key, boss))
    return parses


def _parse_lines(parses: dict[Char, list[tuple[float, str, str]]]) -> list[ParseLine]:
    lines = []
    for char, entries in parses.items():
        roles = Counter(role for _, role, _ in entries)
        # A player counts as a tank for the night only if they tanked most of their kills.
        others = Counter({r: n for r, n in roles.items() if r != TANK})
        role = TANK if roles[TANK] * 2 > len(entries) or not others else others.most_common(1)[0][0]
        average = round(sum(pct for pct, _, _ in entries) / len(entries), 1)
        lines.append(ParseLine(char, average, role, [(pct, boss) for pct, _, boss in entries]))
    return lines


def _roles(report: dict, players: dict[int, Char], lines: list[ParseLine]) -> dict[Char, str]:
    """The role each player mostly played tonight. WCL's player details cover wipes too, and a
    player who swapped roles is listed under each, with how many pulls they spent in each spec."""
    by_id: dict[int, Counter] = defaultdict(Counter)
    details = (_data(report.get("playerDetails")) or {}).get("playerDetails") or {}
    for role, entries in details.items():
        for p in entries or []:
            count = sum(s.get("count", 1) for s in p.get("specs") or []) or 1
            by_id[p.get("id")][role.lower()] += count
    roles = {players[pid]: counts.most_common(1)[0][0] for pid, counts in by_id.items() if pid in players}
    for line in lines:
        roles.setdefault(line.char, line.role)
    for char in players.values():
        roles.setdefault(char, DPS)
    return roles


def _table_entries(report: dict, key: str) -> list[dict]:
    return (_data(report.get(key)) or {}).get("entries") or []


def _by_player(report: dict, key: str, players: dict[int, Char]) -> dict[Char, float]:
    totals: dict[Char, float] = {}
    for e in _table_entries(report, key):
        char = players.get(e.get("id"))
        if char and isinstance(e.get("total"), (int, float)):
            totals[char] = totals.get(char, 0) + e["total"]
    return totals


def _active_share(report: dict, players: dict[int, Char], roles: dict[Char, str],
                  pulls_in: dict[Char, set[int]], pulls: list, dead: Counter) -> dict[Char, float]:
    """How much of their time alive in boss pulls each DPS and tank was actually dealing damage:
    WCL's active time over time in pulls minus time dead. 0.9 = active 90% of the time.

    Healers are left out: WCL counts heals over time and shields ticking as healing activity, so
    every healer comes out near 100% however much they cast. Anyone alive under a minute is too."""
    busy: dict[Char, float] = {}
    for e in _table_entries(report, "damageDone"):
        char = players.get(e.get("id"))
        if char and isinstance(e.get("activeTime"), (int, float)):
            busy[char] = busy.get(char, 0) + e["activeTime"]
    length = {p.id: (p.end - p.start) for p in pulls}
    share = {}
    for char, ids in pulls_in.items():
        alive = sum(length.get(i, 0) for i in ids) - dead.get(char, 0) * 1000
        if roles.get(char) != HEALER and char in busy and alive >= 60_000:
            share[char] = min(1.0, busy[char] / alive)
    return share


def _cast_tally(report: dict, key: str, players: dict[int, Char]) -> Counter:
    """Interrupts and dispels: nested per interrupted/dispelled spell, then per player."""
    tally: Counter = Counter()
    for outer in _table_entries(report, key):
        for spell in outer.get("entries") or []:
            for d in spell.get("details") or []:
                char = players.get(d.get("id"))
                if char:
                    tally[char] += d.get("total") or 0
    return tally


def _rates(pulls: list[Pull], players: dict[int, Char], roles: dict[Char, str],
           damage: dict[Char, float], healing: dict[Char, float]) -> list[Rate]:
    # Divided by each player's own time in pulls: someone who sat out half the night isn't
    # made to look half as good.
    seconds: Counter = Counter()
    count: Counter = Counter()
    for p in pulls:
        for pid in p.players:
            if pid in players:
                seconds[players[pid]] += p.seconds
                count[players[pid]] += 1
    rates = []
    for char, role in roles.items():
        total = (healing if role == HEALER else damage).get(char)
        if total and seconds[char]:
            rates.append(Rate(char, role, total / seconds[char], count[char]))
    return sorted(rates, key=lambda r: -r.per_second)


def _deaths(report: dict, players: dict[int, Char], pulls: list[Pull],
            settings: RecapSettings) -> dict[int, list[Death]]:
    abilities = (report.get("masterData") or {}).get("abilities") or []
    names = {a.get("gameID"): a.get("name") for a in abilities}
    pull_ids = {p.id for p in pulls}
    events = report.get("deaths")
    per_pull: dict[int, list[Death]] = defaultdict(list)
    for e in events if isinstance(events, list) else []:
        fight = e.get("fight")
        if pull_ids and fight not in pull_ids and not settings.deaths_include_trash:
            continue
        char = players.get(e.get("targetID"))
        if char is None:
            continue  # pets, NPCs
        ability = names.get(e.get("killingAbilityGameID"))
        per_pull[fight].append(Death(fight, e.get("timestamp") or 0, char, ability))
    for deaths in per_pull.values():
        deaths.sort(key=lambda d: d.time)
    return dict(per_pull)


def _floor(counted: dict[int, list[Death]], top_n: int) -> tuple[list[DeathLine], int]:
    deaths: Counter = Counter()
    firsts: Counter = Counter()
    for pull_deaths in counted.values():
        for d in pull_deaths:
            deaths[d.char] += 1
        if pull_deaths:
            firsts[pull_deaths[0].char] += 1
    ranked = [
        DeathLine(c, deaths[c], firsts[c])
        for c in sorted(deaths, key=lambda c: (-deaths[c], -firsts[c], c.name.casefold()))
    ]
    return _top_with_ties(ranked, top_n)


def _top_with_ties(ranked: list[DeathLine], n: int) -> tuple[list[DeathLine], int]:
    """Top n, plus anyone tied with last place, unless the tie is big (a clean night where five
    people died once). Returns the lines to show and how many tied players were left out."""
    shown: list[DeathLine] = []
    i = 0
    while i < len(ranked) and len(shown) < n:
        group = [d for d in ranked[i:] if d.deaths == ranked[i].deaths]
        if shown and len(shown) + len(group) > n + 2:
            break  # the tie would crowd the list; the players above it are the story
        if not shown and len(group) > n + 2:
            return group[:n], len(group) - n  # everyone tied at the top: show some, count the rest
        shown += group
        i += len(group)
    return shown, 0


def _game_version(report: dict) -> int | None:
    for key in ("damageDone", "healing", "damageTaken", "casts"):
        version = (_data(report.get(key)) or {}).get("gameVersion")
        if version is not None:
            return version
    return None


@dataclass(frozen=True)
class Item:
    """One equipped item as a pull started: enough to check enchants and gems."""
    id: int
    enchanted: bool
    gems: int
    bonus_ids: frozenset[int]
    oiled: bool = False  # a temporary enchant: weapon oil, sharpening stone, shaman imbue


def _gear_at_pull(report: dict, players: dict[int, Char],
                  pull_ids: set[int]) -> tuple[dict[int, dict[Char, list[Item | None]]], dict[Char, int]]:
    """Every player's gear as each pull started (a list by WoW slot number, None for an empty slot),
    and their spec id."""
    out: dict[int, dict[Char, list[Item | None]]] = defaultdict(dict)
    specs: dict[Char, int] = {}
    events = report.get("combatantInfo")
    for e in events if isinstance(events, list) else []:
        char = players.get(e.get("sourceID"))
        if not char or e.get("fight") not in pull_ids:
            continue
        if isinstance(e.get("specID"), int):
            specs[char] = e["specID"]
        gear = e.get("gear")
        if not isinstance(gear, list) or not gear:
            continue
        out[e["fight"]][char] = [
            Item(it["id"], bool(it.get("permanentEnchant")), len(it.get("gems") or []),
                 frozenset(b for b in it.get("bonusIDs") or [] if isinstance(b, int)),
                 bool(it.get("temporaryEnchant")))
            if isinstance(it, dict) and it.get("id") else None
            for it in gear
        ]
    return dict(out), specs


@dataclass(frozen=True)
class RanOut:
    """A flask or food buff that expired during a boss pull."""
    char: Char
    kind: str  # "flask" or "food"
    buff: str
    pull: int
    seconds_in: float


DEATH_GRACE_MS = 3000  # a buff lost this close to its owner's death went with the death, not the clock


def _ran_out(report: dict, players: dict[int, Char], pulls: list[Pull],
             deaths: dict[int, list["Death"]]) -> list[RanOut]:
    """Flask and food buffs that came off mid-pull. Normal food falls off on death (Hearty food
    doesn't), so anything lost within a few seconds of its owner's death isn't counted."""
    names: dict[int, str] = {}
    for snapshot in report.get("combatantInfo") or []:
        for aura in snapshot.get("auras") or []:
            if isinstance(aura.get("ability"), int):
                names[aura["ability"]] = aura.get("name") or ""
    by_id = {p.id: p for p in pulls}
    died = defaultdict(list)
    for pull_id, ds in deaths.items():
        for d in ds:
            died[(pull_id, d.char)].append(d.time)
    out = []
    events = report.get("buffEnds")
    for e in events if isinstance(events, list) else []:
        char = players.get(e.get("targetID"))
        pull = by_id.get(e.get("fight"))
        name = names.get(e.get("abilityGameID"), "")
        if not char or not pull or e.get("type") != "removebuff":
            continue
        kind = "flask" if name.startswith(FLASK_PREFIXES) else "food" if FOOD_PATTERN in name else None
        at = e.get("timestamp") or 0
        if kind is None or not pull.start <= at <= pull.end:
            continue
        if any(-1000 <= at - t <= DEATH_GRACE_MS for t in died.get((pull.id, char), [])):
            continue
        out.append(RanOut(char, kind, name, pull.id, (at - pull.start) / 1000))
    return sorted(out, key=lambda r: (r.pull, r.seconds_in, r.char.name.casefold()))


def _auras_at_pull(report: dict, players: dict[int, Char], pull_ids: set[int]) -> dict[int, dict[Char, list[str]]]:
    """WCL snapshots every player's buffs as each pull starts: flask, food, rune, vantus."""
    out: dict[int, dict[Char, list[str]]] = defaultdict(dict)
    events = report.get("combatantInfo")
    for e in events if isinstance(events, list) else []:
        char = players.get(e.get("sourceID"))
        if char and e.get("fight") in pull_ids:
            out[e["fight"]][char] = [a.get("name") or "" for a in e.get("auras") or []]
    return dict(out)


def _combat_potions(report: dict, players: dict[int, Char], pull_ids: set[int]) -> dict[Char, Counter]:
    """Combat potion buffs, per player per pull."""
    by_pull: dict[Char, Counter] = defaultdict(Counter)
    events = report.get("potions")
    for e in events if isinstance(events, list) else []:
        char = players.get(e.get("targetID"))
        if char and e.get("fight") in pull_ids:
            by_pull[char][e["fight"]] += 1
    return dict(by_pull)


def _mana_potions(report: dict, players: dict[int, Char], pull_ids: set[int]) -> dict[Char, Counter] | None:
    """Mana potion casts, per player per pull. None for reports fetched before these were."""
    events = report.get("manaPotions")
    if not isinstance(events, list):
        return None
    by_pull: dict[Char, Counter] = defaultdict(Counter)
    for e in events:
        char = players.get(e.get("sourceID"))
        if char and e.get("fight") in pull_ids and e.get("type") == "cast":
            by_pull[char][e["fight"]] += 1
    return dict(by_pull)


def is_health_item(name: str, extra: tuple[str, ...] = ()) -> bool:
    return any(pattern in name for pattern in HEALTH_ITEM_PATTERNS) or name in extra


def _consumable_casts(report: dict, players: dict[int, Char], extra: tuple[str, ...]) -> tuple[Counter, Counter]:
    """Healthstones, health potions and mana potions are casts. The casts table names players
    rather than giving ids, so names are matched against the raid (skipping any ambiguous name)."""
    by_name: dict[str, list[Char]] = defaultdict(list)
    for char in players.values():
        by_name[norm_name(char.name)].append(char)
    health: Counter = Counter()
    mana: Counter = Counter()
    for e in _table_entries(report, "casts"):
        name = e.get("name") or ""
        tally = health if is_health_item(name, extra) else mana if MANA_POTION_PATTERN in name else None
        if tally is None:
            continue
        for source in e.get("sources") or []:
            chars = by_name.get(norm_name(source.get("name") or ""), [])
            if len(chars) == 1:
                tally[chars[0]] += source.get("total") or 0
    return health, mana


def _time_dead(report: dict, players: dict[int, Char], pulls: list[Pull],
               deaths: dict[int, list[Death]]) -> Counter:
    """Seconds each player spent dead: from each death until a battle rez or the end of the pull."""
    ends = {p.id: p.end for p in pulls}
    rezzes: dict[tuple[int, Char], list[int]] = defaultdict(list)
    events = report.get("resurrects")
    for e in events if isinstance(events, list) else []:
        char = players.get(e.get("targetID"))
        if char and isinstance(e.get("timestamp"), (int, float)):
            rezzes[(e.get("fight"), char)].append(e["timestamp"])
    dead: Counter = Counter()
    for pull_id, pull_deaths in deaths.items():
        if pull_id not in ends:
            continue
        back_at: dict[Char, float] = {}  # still dead until this time (guards against double deaths)
        for d in pull_deaths:
            if d.time < back_at.get(d.char, -1):
                continue
            later = [t for t in rezzes[(pull_id, d.char)] if t > d.time]
            revived = min(later) if later else ends[pull_id]
            back_at[d.char] = revived
            dead[d.char] += max(0, revived - d.time) / 1000
    return dead


def _power_infusion(report: dict, players: dict[int, Char], pull_ids: set[int]) -> Counter:
    given: Counter = Counter()
    events = report.get("powerInfusion")
    for e in events if isinstance(events, list) else []:
        giver, receiver = players.get(e.get("sourceID")), players.get(e.get("targetID"))
        if giver and receiver and giver != receiver and e.get("fight") in pull_ids:
            given[(giver, receiver)] += 1
    return given


def _resurrects(report: dict, players: dict[int, Char], pull_ids: set[int]) -> tuple[Counter, Counter]:
    brezzed: Counter = Counter()
    given: Counter = Counter()
    events = report.get("resurrects")
    for e in events if isinstance(events, list) else []:
        if pull_ids and e.get("fight") not in pull_ids:
            continue
        if e.get("targetID") in players:
            brezzed[players[e["targetID"]]] += 1
        if e.get("sourceID") in players:
            given[players[e["sourceID"]]] += 1
    return brezzed, given


def analyze(report: dict, settings: RecapSettings) -> Night:
    pulls = _pulls(report)
    players = _players(report, pulls)
    lines = _parse_lines(_parses(report, players))
    roles = _roles(report, players, lines)
    damage = _by_player(report, "damageDone", players)
    healing = _by_player(report, "healing", players)
    deaths = _deaths(report, players, pulls, settings)
    cutoff = settings.wipe_cutoff
    # Deaths after the wipe is called don't count against anyone.
    counted = {pid: (ds[:cutoff] if cutoff > 0 else ds) for pid, ds in deaths.items()}
    floor, tied_more = _floor(counted, settings.deaths_top_n)
    brezzed, given = _resurrects(report, players, {p.id for p in pulls})

    roster = {c.key: c for c in players.values()}
    for line in lines:
        roster.setdefault(line.char.key, line.char)

    pull_ids = {p.id for p in pulls}
    pulls_in: dict[Char, set[int]] = defaultdict(set)
    for p in pulls:
        for pid in p.players:
            if pid in players:
                pulls_in[players[pid]].add(p.id)
    potions_by_pull = _combat_potions(report, players, pull_ids)
    potion_pulls = {c: set(n) for c, n in potions_by_pull.items()}
    potions = Counter({c: sum(n.values()) for c, n in potions_by_pull.items()})
    health, mana = _consumable_casts(report, players, settings.extra_health_items)
    mana_by_pull = _mana_potions(report, players, pull_ids)
    gear_at_pull, specs = _gear_at_pull(report, players, pull_ids)
    dead_seconds = _time_dead(report, players, pulls, deaths)
    if mana_by_pull is not None:  # the per-pull casts are the same count, limited to boss pulls
        mana = Counter({c: sum(n.values()) for c, n in mana_by_pull.items()})

    return Night(
        title=report.get("title") or "Raid",
        zone=(report.get("zone") or {}).get("name"),
        start_ms=int(report.get("startTime") or 0),
        processing=(report.get("exportedSegments") or 0) < (report.get("segments") or 0),
        pulls=pulls,
        bosses=_bosses(pulls),
        roster=sorted(roster.values(), key=lambda c: c.name.casefold()),
        roles=roles,
        parses=lines,
        rates=_rates(pulls, players, roles, damage, healing),
        deaths=deaths,
        counted=counted,
        floor=floor,
        floor_tied_more=tied_more,
        interrupts=_cast_tally(report, "interrupts", players),
        dispels=_cast_tally(report, "dispels", players),
        damage_done=damage,
        damage_taken=_by_player(report, "damageTaken", players),
        brezzed=brezzed,
        brez_given=given,
        high=sorted(
            (p for p in lines if p.average >= settings.parse_high),
            key=lambda p: (-p.average, p.char.name.casefold()),
        ),
        grey=sorted(
            (p for p in lines
             if p.average < settings.parse_grey and (settings.grey_include_tanks or p.role != TANK)),
            key=lambda p: (p.average, p.char.name.casefold()),
        ),
        retail=_game_version(report) == RETAIL,
        pulls_in=dict(pulls_in),
        auras_at_pull=_auras_at_pull(report, players, pull_ids),
        potion_pulls=potion_pulls,
        potions=potions,
        casts_known="casts" in report,
        health_items=health,
        mana_potions=mana,
        dead_seconds=dead_seconds,
        power_infusion=_power_infusion(report, players, pull_ids),
        potions_by_pull=potions_by_pull,
        mana_by_pull=mana_by_pull or {},
        gear_at_pull=gear_at_pull,
        specs=specs,
        active=_active_share(report, players, roles, pulls_in, pulls, dead_seconds),
        ran_out=_ran_out(report, players, pulls, deaths),
    )

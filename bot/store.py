"""SQLite: character links, characters seen in logs, report status, and the history of past nights."""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .awards import BossResult
from .recap import Char, norm_name
from .wcl import ReportRef

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    name_norm TEXT NOT NULL,
    realm_norm TEXT NOT NULL,
    name TEXT NOT NULL,
    realm TEXT NOT NULL,
    discord_user_id INTEGER NOT NULL,
    linked_by INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (name_norm, realm_norm)
);
CREATE TABLE IF NOT EXISTS seen_characters (
    name_norm TEXT NOT NULL,
    realm_norm TEXT NOT NULL,
    name TEXT NOT NULL,
    realm TEXT NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (name_norm, realm_norm)
);
CREATE TABLE IF NOT EXISTS reports (
    host TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | posted | failed
    channel_id INTEGER,
    source_message_id INTEGER,
    first_seen INTEGER NOT NULL,
    last_end_time INTEGER,
    last_change_at INTEGER,
    posted_at INTEGER,
    characters TEXT,                   -- JSON [[name, realm], ...] from the posted recap
    error TEXT,
    PRIMARY KEY (host, code)
);
-- One row per recapped night, for "last raid's best".
CREATE TABLE IF NOT EXISTS history (
    host TEXT NOT NULL,
    code TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    bosses TEXT NOT NULL,              -- JSON [{encounter_id, difficulty, name, killed, boss_pct, phase}]
    winners TEXT NOT NULL,             -- JSON {award key: [[name, realm], ...]}
    PRIMARY KEY (host, code)
);
-- Settings admins change from Discord with /settings.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class PendingReport:
    ref: ReportRef
    channel_id: int
    source_message_id: int | None
    first_seen: int
    last_end_time: int | None
    last_change_at: int | None


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)

    # --- settings ----------------------------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    @property
    def mentions(self) -> bool:
        """@ mention (and ping) linked raiders in reports. Off unless an admin turns it on."""
        return self.get_setting("mentions", "off") == "on"

    # --- links -------------------------------------------------------------------------------

    def link(self, char: Char, user_id: int, linked_by: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO links VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*char.key, char.name, char.realm, user_id, linked_by, _now()),
            )

    def unlink(self, char: Char) -> bool:
        with self._db:
            cur = self._db.execute(
                "DELETE FROM links WHERE name_norm = ? AND realm_norm = ?", char.key
            )
        return cur.rowcount > 0

    def user_for(self, char: Char) -> int | None:
        row = self._db.execute(
            "SELECT discord_user_id FROM links WHERE name_norm = ? AND realm_norm = ?", char.key
        ).fetchone()
        if row:
            return row[0]
        if not char.key[1]:
            # WCL didn't give a realm; fall back to the name if only one link has it.
            rows = self._db.execute(
                "SELECT DISTINCT discord_user_id FROM links WHERE name_norm = ?", (char.key[0],)
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]
        return None

    def characters_of(self, user_id: int) -> list[Char]:
        rows = self._db.execute(
            "SELECT name, realm FROM links WHERE discord_user_id = ? ORDER BY name", (user_id,)
        ).fetchall()
        return [Char(r["name"], r["realm"]) for r in rows]

    # --- characters seen in logs -------------------------------------------------------------

    def remember(self, chars: list[Char]) -> None:
        now = _now()
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO seen_characters VALUES (?, ?, ?, ?, ?)",
                [(*c.key, c.name, c.realm, now) for c in chars if c.realm],
            )

    def search_seen(self, text: str, limit: int = 25) -> list[Char]:
        rows = self._db.execute(
            "SELECT name, realm FROM seen_characters WHERE name_norm LIKE ? "
            "ORDER BY last_seen DESC, name LIMIT ?",
            (norm_name(text).replace("%", "").replace("_", "") + "%", limit),
        ).fetchall()
        return [Char(r["name"], r["realm"]) for r in rows]

    def seen_named(self, name: str) -> list[Char]:
        rows = self._db.execute(
            "SELECT name, realm FROM seen_characters WHERE name_norm = ? ORDER BY last_seen DESC",
            (norm_name(name),),
        ).fetchall()
        return [Char(r["name"], r["realm"]) for r in rows]

    def search_links(self, text: str, limit: int = 25) -> list[tuple[Char, int]]:
        rows = self._db.execute(
            "SELECT name, realm, discord_user_id FROM links WHERE name_norm LIKE ? ORDER BY name LIMIT ?",
            (norm_name(text).replace("%", "").replace("_", "") + "%", limit),
        ).fetchall()
        return [(Char(r["name"], r["realm"]), r["discord_user_id"]) for r in rows]

    def links_named(self, name: str) -> list[tuple[Char, int]]:
        rows = self._db.execute(
            "SELECT name, realm, discord_user_id FROM links WHERE name_norm = ?", (norm_name(name),)
        ).fetchall()
        return [(Char(r["name"], r["realm"]), r["discord_user_id"]) for r in rows]

    # --- reports -----------------------------------------------------------------------------

    def status_of(self, ref: ReportRef) -> str | None:
        row = self._db.execute(
            "SELECT status FROM reports WHERE host = ? AND code = ?", (ref.host, ref.code)
        ).fetchone()
        return row["status"] if row else None

    def add_pending(self, ref: ReportRef, channel_id: int, message_id: int | None) -> bool:
        """False if this report was already seen, so re-pasting a link never double-posts."""
        with self._db:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO reports (host, code, status, channel_id, source_message_id, first_seen) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (ref.host, ref.code, channel_id, message_id, _now()),
            )
        return cur.rowcount > 0

    def pending(self) -> list[PendingReport]:
        rows = self._db.execute(
            "SELECT * FROM reports WHERE status = 'pending' ORDER BY first_seen"
        ).fetchall()
        return [
            PendingReport(
                ref=ReportRef(r["host"], r["code"]),
                channel_id=r["channel_id"],
                source_message_id=r["source_message_id"],
                first_seen=r["first_seen"],
                last_end_time=r["last_end_time"],
                last_change_at=r["last_change_at"],
            )
            for r in rows
        ]

    def note_end_time(self, ref: ReportRef, end_time: int) -> None:
        """Remember when the log last grew, for spotting a live log that has stopped."""
        with self._db:
            self._db.execute(
                "UPDATE reports SET last_end_time = ?, last_change_at = ? "
                "WHERE host = ? AND code = ? AND (last_end_time IS NULL OR last_end_time != ?)",
                (end_time, _now(), ref.host, ref.code, end_time),
            )

    def mark_posted(self, ref: ReportRef, characters: list[Char], channel_id: int | None = None) -> None:
        chars = json.dumps([[c.name, c.realm] for c in characters])
        with self._db:
            self._db.execute(
                "INSERT INTO reports (host, code, status, channel_id, first_seen, posted_at, characters) "
                "VALUES (?, ?, 'posted', ?, ?, ?, ?) "
                "ON CONFLICT (host, code) DO UPDATE SET status = 'posted', "
                "posted_at = excluded.posted_at, characters = excluded.characters, error = NULL",
                (ref.host, ref.code, channel_id, _now(), _now(), chars),
            )

    def mark_failed(self, ref: ReportRef, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE reports SET status = 'failed', error = ? WHERE host = ? AND code = ?",
                (error, ref.host, ref.code),
            )

    def report_characters(self, ref: ReportRef) -> list[Char] | None:
        """Everyone in a posted report, or None if it was never posted."""
        row = self._db.execute(
            "SELECT characters FROM reports WHERE host = ? AND code = ? AND characters IS NOT NULL",
            (ref.host, ref.code),
        ).fetchone()
        return [Char(n, r) for n, r in json.loads(row["characters"])] if row else None

    def last_posted_report(self) -> ReportRef | None:
        row = self._db.execute(
            "SELECT host, code FROM reports WHERE status = 'posted' AND characters IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 1"
        ).fetchone()
        return ReportRef(row["host"], row["code"]) if row else None

    def recent_raiders(self, reports: int = 3) -> list[Char]:
        """Everyone from the last few posted reports, alphabetical."""
        rows = self._db.execute(
            "SELECT characters FROM reports WHERE status = 'posted' AND characters IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT ?", (reports,)
        ).fetchall()
        chars = {Char(n, r).key: Char(n, r) for row in rows for n, r in json.loads(row["characters"])}
        return sorted(chars.values(), key=lambda c: (c.name.casefold(), c.realm.casefold()))

    def last_recap_characters(self) -> list[Char]:
        row = self._db.execute(
            "SELECT characters FROM reports WHERE status = 'posted' AND characters IS NOT NULL "
            "ORDER BY posted_at DESC LIMIT 1"
        ).fetchone()
        return [Char(n, r) for n, r in json.loads(row["characters"])] if row else []

    # --- history -----------------------------------------------------------------------------

    def save_history(self, ref: ReportRef, start_ms: int, bosses: list, winners: dict[str, list[Char]]) -> None:
        """Re-running a report replaces its row, so history never double counts a night."""
        boss_json = json.dumps([
            {"encounter_id": eid, "difficulty": diff, "name": name, "killed": r.killed,
             "boss_pct": r.boss_pct, "phase": r.phase}
            for eid, diff, name, r in bosses
        ])
        winner_json = json.dumps({key: [[c.name, c.realm] for c in chars] for key, chars in winners.items()})
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO history VALUES (?, ?, ?, ?, ?)",
                (ref.host, ref.code, start_ms, boss_json, winner_json),
            )

    def _history_before(self, before_ms: int):
        # Only nights before this one: re-running an old report compares it with its own past.
        return self._db.execute(
            "SELECT bosses, winners FROM history WHERE start_ms < ? ORDER BY start_ms DESC", (before_ms,)
        )

    def last_result(self, encounter_id: int, difficulty: int | None, before_ms: int) -> BossResult | None:
        for row in self._history_before(before_ms):
            for boss in json.loads(row["bosses"]):
                if boss["encounter_id"] == encounter_id and boss["difficulty"] == difficulty:
                    return BossResult(boss["killed"], boss["boss_pct"], boss["phase"])
        return None

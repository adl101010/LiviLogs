"""Turn a raw report JSON (from tools/probe.py) into an anonymised test fixture.

    python -m tools.make_fixture tools/probe-out/<code>/full.json tests/fixtures/<name>.json

Every player name and realm is replaced wherever it appears as a JSON string (rankings, tables,
dispel targets...). Replacements are derived from the real name, so the same player gets the same
placeholder in every fixture. Guild names are dropped, and bulky per-ability breakdowns the bot
never reads are trimmed so fixtures stay small.
"""

import hashlib
import json
import sys

_TRIM = {"abilities", "damageAbilities", "targets", "gear", "talents", "pets", "sources", "combatantInfo",
         "talentTree", "events", "missedCasts"}


def _walk(node, fn):
    if isinstance(node, dict):
        fn(node)
        for value in node.values():
            _walk(value, fn)
    elif isinstance(node, list):
        for value in node:
            _walk(value, fn)


def anonymise(report: dict) -> dict:
    names: set[str] = set()
    realms: set[str] = set()

    def collect(d):
        server = d.get("server")
        if isinstance(server, dict) and server.get("name"):
            realms.add(server["name"])
        elif isinstance(server, str) and server:
            realms.add(server)
        d.pop("guild", None)
        d.pop("reportsBlacklistForCharacters", None)

    _walk(report, collect)
    for actor in report.get("masterData", {}).get("actors", []):
        names.add(actor["name"])
    for role in ((report.get("playerDetails") or {}).get("data", {}).get("playerDetails") or {}).values():
        names.update(p["name"] for p in role)
    for key in ("dpsRankings", "hpsRankings"):
        for fight in (report.get(key) or {}).get("data", []):
            for role in fight.get("roles", {}).values():
                names.update(c["name"] for c in role.get("characters", []))

    def trim(d):
        for key in _TRIM & d.keys():
            if key != "abilities" or "gameID" not in str(d[key])[:200]:
                d.pop(key)
        if "total" in d:
            d.pop("actors", None)  # per-target breakdown inside interrupt/dispel details

    # Consumables: keep only what the bot reads. The generic trim above would drop the casts
    # table's per-player "sources", and each combatantInfo snapshot carries full gear and talents.
    # Gear keeps only what the bot reads: item, enchant, weapon oil, gems, bonus ids.
    snapshots = [
        {"type": e.get("type"), "fight": e.get("fight"), "sourceID": e.get("sourceID"), "specID": e.get("specID"),
         "auras": [{"name": a.get("name"), "ability": a.get("ability")} for a in e.get("auras") or []],
         "gear": [{"id": g.get("id"), "permanentEnchant": g.get("permanentEnchant"),
                   "temporaryEnchant": g.get("temporaryEnchant"),
                   "bonusIDs": g.get("bonusIDs") or [], "gems": [{"id": x.get("id")} for x in g.get("gems") or []]}
                  for g in e.get("gear") or []]}
        for e in report.pop("combatantInfo", None) or []
    ]
    casts = report.pop("casts", None)
    if casts:
        data = casts.get("data", {})
        keep = ("Potion", "Healthstone", "Brew", "Food")  # consumables, plus a few look-alikes that must not count
        data["entries"] = [
            {"name": e.get("name"), "guid": e.get("guid"), "total": e.get("total"),
             "sources": [{"name": x.get("name"), "total": x.get("total")} for x in e.get("sources") or []]}
            for e in data.get("entries", []) if any(k in (e.get("name") or "") for k in keep)
        ]
        casts = {"data": {k: data[k] for k in ("entries", "gameVersion", "totalTime") if k in data}}

    _walk(report, trim)
    if snapshots:
        report["combatantInfo"] = snapshots
    if casts:
        report["casts"] = casts
    # Keep only the ability names deaths refer to.
    used = {e.get("killingAbilityGameID") for e in report.get("deaths", [])}
    md = report.get("masterData", {})
    md["abilities"] = [a for a in md.get("abilities", []) if a["gameID"] in used]

    def stable(prefix: str, value: str, size: int) -> str:
        return f'"{prefix}{hashlib.sha256(value.encode()).hexdigest()[:size]}"'

    text = json.dumps(report, ensure_ascii=False)
    for name in sorted(names, key=lambda n: (-len(n), n)):
        text = text.replace(json.dumps(name, ensure_ascii=False), stable("P-", name, 6))
    for realm in sorted(realms, key=lambda r: (-len(r), r)):
        text = text.replace(json.dumps(realm, ensure_ascii=False), stable("Realm-", realm, 4))
    report = json.loads(text)
    report["title"] = "Anonymised"
    report["code"] = "Fixture000000000"
    return report


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    with open(src, encoding="utf-8") as f:
        data = anonymise(json.load(f))
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

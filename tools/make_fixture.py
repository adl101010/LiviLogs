"""Turn a probe-out JSON into an anonymised test fixture: real shape, placeholder names.

    python -m tools.make_fixture tools/probe-out/<code>/API_default.json tests/fixtures/<name>.json
"""

import json
import sys


def anonymise(report: dict) -> dict:
    names: dict[str, str] = {}
    realms: dict[str, str] = {}

    def name(n):
        return names.setdefault(n, f"Player{len(names) + 1:02d}")

    def realm(r):
        return realms.setdefault(r, f"Realm {chr(ord('A') + len(realms))}")

    for actor in report.get("masterData", {}).get("actors", []):
        actor["name"] = name(actor["name"])
        actor["server"] = realm(actor["server"]) if actor.get("server") else actor.get("server")
    for key in ("dpsRankings", "hpsRankings"):
        for fight in (report.get(key) or {}).get("data", []):
            fight.pop("reportsBlacklistForCharacters", None)
            fight.pop("guild", None)
            for role in fight.get("roles", {}).values():
                for i, c in enumerate(role.get("characters", [])):
                    c["name"] = name(c["name"])
                    c["id"] = i + 1
                    if isinstance(c.get("server"), dict):
                        c["server"]["name"] = realm(c["server"]["name"])
                        c["server"]["id"] = 0
    report["title"] = "Anonymised"
    report["code"] = "Fixture000000000"
    return report


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    with open(src, encoding="utf-8") as f:
        data = anonymise(json.load(f))
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

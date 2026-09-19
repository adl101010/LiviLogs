"""Settings, all read from environment variables."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

COMPARE_VALUES = {"Rankings", "Parses"}
TIMEFRAME_VALUES = {"Today", "Historical"}


def _str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _required(name: str) -> str:
    value = _str(name)
    if not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


def _int(name: str, default: int | None = None) -> int | None:
    value = _str(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = _str(name)
    return float(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = _str(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _ids(name: str) -> frozenset[int]:
    value = _str(name, "")
    return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)


def _zone(name: str) -> ZoneInfo:
    value = _str(name, "UTC")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"{name} must be a timezone like America/Chicago, got {value!r}")


def _names(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated list of in-game names, e.g. COMBAT_POTIONS=Potion of Recklessness,Light's Potential"""
    value = _str(name)
    if value is None:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _choice(name: str, allowed: set[str]) -> str | None:
    value = _str(name)
    if value is None:
        return None
    for option in allowed:
        if option.lower() == value.lower():
            return option
    raise SystemExit(f"{name} must be one of {sorted(allowed)}, got {value!r}")


@dataclass(frozen=True)
class RecapSettings:
    """The knobs that change what a recap says. Separate so tests and the probe can use them."""

    parse_high: float = 90
    parse_grey: float = 25
    grey_include_tanks: bool = False
    wipe_cutoff: int = 5
    deaths_top_n: int = 3
    deaths_include_trash: bool = False
    compare: str | None = None  # None = let WCL pick, same as the report page
    timeframe: str | None = None
    # Consumable names change every expansion; these are Midnight's. Healthstones, health/mana
    # potions, flasks, food and vantus runes are recognised by name pattern and need no list.
    combat_potions: tuple[str, ...] = ("Potion of Recklessness", "Light's Potential")
    tryhard_runes: tuple[str, ...] = ("Void-Touched",)
    extra_health_items: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "RecapSettings":
        return cls(
            parse_high=_float("PARSE_HIGH", 90),
            parse_grey=_float("PARSE_GREY", 25),
            grey_include_tanks=_bool("GREY_INCLUDE_TANKS", False),
            wipe_cutoff=_int("WIPE_CUTOFF", 5),
            deaths_top_n=_int("DEATHS_TOP_N", 3),
            deaths_include_trash=_bool("DEATHS_INCLUDE_TRASH", False),
            compare=_choice("WCL_COMPARE", COMPARE_VALUES),
            timeframe=_choice("WCL_TIMEFRAME", TIMEFRAME_VALUES),
            combat_potions=_names("COMBAT_POTIONS", cls.combat_potions),
            tryhard_runes=_names("TRYHARD_RUNES", cls.tryhard_runes),
            extra_health_items=_names("EXTRA_HEALTH_ITEMS", cls.extra_health_items),
        )


@dataclass(frozen=True)
class Config:
    discord_token: str
    watch_channel_ids: frozenset[int]
    guild_id: int | None
    default_realm: str | None
    db_path: str
    poll_minutes: int
    quiet_minutes: int
    max_wait_hours: int
    timezone: ZoneInfo  # only for the date in thread titles; message text uses Discord timestamps
    thread_ping_everyone: bool
    recap: RecapSettings
    charts: bool = True  # parse, consumables and progress pictures in the thread
    my_night: bool = True  # the "My night" button on the headline

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            watch_channel_ids=_ids("WATCH_CHANNEL_IDS"),
            guild_id=_int("DISCORD_GUILD_ID"),
            default_realm=_str("DEFAULT_REALM"),
            db_path=_str("DB_PATH", "/data/livilogs.sqlite3"),
            poll_minutes=_int("POLL_MINUTES", 5),
            quiet_minutes=_int("QUIET_MINUTES", 20),
            max_wait_hours=_int("MAX_WAIT_HOURS", 8),
            timezone=_zone("TIMEZONE"),
            thread_ping_everyone=_bool("THREAD_PING_EVERYONE", True),
            recap=RecapSettings.from_env(),
            charts=_bool("CHARTS", True),
            my_night=_bool("MY_NIGHT", True),
        )


def wcl_credentials(host: str) -> tuple[str, str]:
    """Client ID/secret for a WCL site. One pair normally works everywhere; a site-specific pair
    (WCL_CLIENT_ID_CLASSIC, ...) overrides it if WCL turns out to need separate clients."""
    site = host.split(".")[0].upper()
    client_id = _str(f"WCL_CLIENT_ID_{site}") or _required("WCL_CLIENT_ID")
    secret = _str(f"WCL_CLIENT_SECRET_{site}") or _required("WCL_CLIENT_SECRET")
    return client_id, secret

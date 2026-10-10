"""Registering slash commands without clobbering other programs.

The bot token (and so the Discord application) can be shared. Registering commands in bulk, as
`tree.sync()` does, replaces everything in that scope with LiviLogs' list, and whichever program
starts last wins. Here each command is created or overwritten on its own, unchanged ones are
skipped, and only names on LiviLogs' own list of retired commands are ever deleted. The global
scope is never touched.
"""

import json
import logging
from dataclasses import dataclass, field

from discord import app_commands

log = logging.getLogger("livilogs")

# Names LiviLogs defined in the past but no longer does, so they can be removed from servers.
# Never add a name another program owns.
STALE_COMMAND_NAMES: frozenset[str] = frozenset()

# Added by Discord, or not set by us, so they'd make an unchanged command look changed.
_SERVER_KEYS = frozenset({"id", "application_id", "guild_id", "version", "default_permission",
                          "permissions", "handler"})
# Compared only when we set them: Discord fills in defaults when we don't.
_OPTIONAL_KEYS = frozenset({"contexts", "integration_types"})


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _empty(value) -> bool:
    return value is None or value is False or value == [] or value == {}


def _comparable(value, top: bool = False, keep=frozenset()):
    """Reduce a command (ours or Discord's copy) to what defines it, in a stable order."""
    if isinstance(value, list):
        return [_comparable(item) for item in value]
    if isinstance(value, dict):
        out = {}
        for key in sorted(value):
            child = value[key]
            if key in _SERVER_KEYS or _empty(child):
                continue
            if top and key in _OPTIONAL_KEYS and key not in keep:
                continue
            if top and key == "type" and child == 1:
                continue  # chat input is the default
            out[key] = _comparable(child)
        return out
    return value


def _normalize(command: dict, keep=frozenset()) -> str:
    normalized = _comparable(command, top=True, keep=keep)
    # These have meaningful defaults, so compare them explicitly. Discord sends the
    # permission bits as a string; discord.py builds them as an int.
    permissions = command.get("default_member_permissions")
    normalized["default_member_permissions"] = None if permissions is None else str(permissions)
    normalized["dm_permission"] = command.get("dm_permission", True)
    normalized["nsfw"] = command.get("nsfw", False)
    return json.dumps(normalized, sort_keys=True)


def commands_equal(desired: dict, existing: dict) -> bool:
    keep = frozenset(key for key in _OPTIONAL_KEYS if key in desired and not _empty(desired[key]))
    return _normalize(desired, keep) == _normalize(existing, keep)


def _key(command: dict) -> tuple[int, str]:
    return command.get("type", 1), command["name"]


def local_payloads(tree: app_commands.CommandTree) -> list[dict]:
    """Every command the tree knows (slash commands and context menus) as Discord's payload."""
    return [command.to_dict(tree) for command in tree.get_commands()]


async def reconcile_guild_commands(http, application_id: int, guild_id: int, payloads: list[dict],
                                   stale_names=STALE_COMMAND_NAMES) -> SyncResult:
    """Make this guild have LiviLogs' commands, touching nothing else.

    `http` is discord.py's HTTPClient (or anything with the same three coroutines).
    """
    existing = await http.get_guild_commands(application_id, guild_id)
    existing_by_key = {_key(command): command for command in existing}
    result = SyncResult()
    desired_keys = set()

    for payload in payloads:
        key = _key(payload)
        desired_keys.add(key)
        current = existing_by_key.get(key)
        if current is not None and commands_equal(payload, current):
            result.unchanged.append(payload["name"])
            continue
        # Creating a command with an existing name overwrites that one and nothing else.
        await http.upsert_guild_command(application_id, guild_id, payload)
        (result.updated if current is not None else result.created).append(payload["name"])

    for command in existing:
        if command["name"] in stale_names and _key(command) not in desired_keys:
            await http.delete_guild_command(application_id, guild_id, int(command["id"]))
            result.deleted.append(command["name"])

    return result

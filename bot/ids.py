"""Component ids (button and menu custom_ids).

The bot token can be shared with other programs, which then receive every interaction, ours
included, and we receive theirs. Every id LiviLogs creates therefore starts with "logs:", and an id
without it is someone else's and is ignored without a reply.

Cards and menus posted before the prefix existed carry bare ids ("mynight:...", "lr-pick:..."). The
ids say everything they need (report, page, character), so those messages keep working: a bare id
is accepted when its kind is one we used to create.
"""

PREFIX = "logs:"


def tag(custom_id: str) -> str:
    """The id to put on a new button or menu."""
    return PREFIX + custom_id


def untag(custom_id: str, legacy_kinds) -> str | None:
    """The id without our prefix, or None when it isn't ours.

    `legacy_kinds` are the bare kinds LiviLogs created before the prefix existed.
    """
    if custom_id.startswith(PREFIX):
        return custom_id[len(PREFIX):]
    if custom_id.partition(":")[0] in legacy_kinds:
        return custom_id
    return None

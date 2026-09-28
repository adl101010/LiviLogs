"""Turn the talents a Warcraft Logs pull records into the loadout string SimulationCraft wants.

WCL gives talents as (nodeID, entry id, rank) rows. simc's `talents=` takes Blizzard's export
string. The bit layout below mirrors simc's own parse_traits_hash() (engine/player/player.cpp),
read backwards, and the node table comes from the simc build's generated trait data
(engine/dbc/generated/trait_data.inc) so the sim and this encoder can never disagree about which
node a bit belongs to.

Ported from the decoder in livibots (server/talentData.js), which was verified against simc's own
debug output. `decode()` is here too, so an encoded string can be round-tripped before it's used.
"""

import re
from dataclasses import dataclass
from pathlib import Path

B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
VERSION = 2  # serialization version simc reads
NODE_TIERED = 1  # ranks spill from one entry into the next
TREE_HERO, TREE_SELECTION = 3, 4

_ROW = re.compile(r'^\s*\{\s*([-\d\s,]+?),\s*"((?:[^"\\]|\\.)*)"\s*,\s*\{([^}]*)\}\s*,\s*\{([^}]*)\}'
                  r'\s*,\s*(\d+)\s*,\s*(\d+)\s*\}\s*,?\s*$')


class TalentError(Exception):
    """The trait table or the picks don't fit together. The caller drops the talents line."""


@dataclass(frozen=True)
class Trait:
    tree: int
    class_id: int
    entry: int
    node: int
    max_ranks: int
    spell: int
    name: str
    specs: tuple[int, ...]
    sub_tree: int
    node_type: int


def load_traits(path: str | Path) -> list[Trait]:
    """Every trait row in the simc build's generated table."""
    traits = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").split("\n"):
        if "trait_sub_tree_data" in line:
            break  # the sub-tree table follows, and has a different shape
        match = _ROW.match(line)
        if not match:
            continue
        numbers = [int(v) for v in match.group(1).split(",") if v.strip().lstrip("-").isdigit()]
        if len(numbers) < 13:
            continue
        specs = tuple(int(v) for v in match.group(3).split(",") if v.strip().lstrip("-").isdigit() and int(v))
        traits.append(Trait(tree=numbers[0], class_id=numbers[1], entry=numbers[2], node=numbers[3],
                            max_ranks=numbers[4], spell=numbers[7], name=match.group(2), specs=specs,
                            sub_tree=int(match.group(5)), node_type=int(match.group(6))))
    if not traits:
        raise TalentError(f"no trait rows in {path}")
    return traits


def nodes_for_class(traits: list[Trait], class_id: int) -> list[tuple[int, list[Trait]]]:
    """Every node of one class, node ids ascending: the order the string's bits are written in."""
    by_node: dict[int, list[Trait]] = {}
    for trait in traits:
        if trait.class_id == class_id:
            by_node.setdefault(trait.node, []).append(trait)
    return sorted(by_node.items())


def class_of_spec(traits: list[Trait], spec_id: int) -> int:
    for trait in traits:
        if spec_id in trait.specs:
            return trait.class_id
    raise TalentError(f"no class in this trait table has spec {spec_id}")


class _Writer:
    def __init__(self) -> None:
        self.bits: list[int] = []

    def write(self, value: int, count: int) -> None:
        for i in range(count):
            self.bits.append((value >> i) & 1)

    def text(self) -> str:
        out = []
        for start in range(0, len(self.bits), 6):
            chunk = self.bits[start:start + 6]
            out.append(B64[sum(bit << i for i, bit in enumerate(chunk))])
        return "".join(out)


class _Reader:
    def __init__(self, text: str) -> None:
        self.text, self.head = text, 0

    def read(self, count: int) -> int:
        value = 0
        for i in range(count):
            if self.head >= len(self.text) * 6:
                raise TalentError("the talent string ends unexpectedly")
            char = B64.find(self.text[self.head // 6])
            if char < 0:
                raise TalentError("the talent string has invalid characters")
            value |= ((char >> (self.head % 6)) & 1) << i
            self.head += 1
        return value


def encode(picks: dict[int, tuple[int | None, int]], spec_id: int, traits: list[Trait]) -> str:
    """A loadout string from {nodeID: (entry id or None, ranks)}.

    Ranks are what WCL recorded, written out explicitly rather than left to default, so a
    part-ranked talent survives. The tree hash Blizzard puts in the header is 16 zero bytes: simc
    skips it, and this string is only ever fed to simc.
    """
    class_id = class_of_spec(traits, spec_id)
    writer = _Writer()
    writer.write(VERSION, 8)
    writer.write(spec_id, 16)
    for _ in range(16):
        writer.write(0, 8)

    for node_id, entries in nodes_for_class(traits, class_id):
        pick = picks.get(node_id)
        if not pick or pick[1] <= 0:
            writer.write(0, 1)
            continue
        entry_id, rank = pick
        index = next((i for i, t in enumerate(entries) if t.entry == entry_id or t.spell == entry_id), 0)
        choice = len(entries) > 1 and entries[0].node_type != NODE_TIERED
        # A tiered node's ranks spill across its entries, so its maximum is their sum; a choice
        # node's is whichever side was taken.
        full = (sum(t.max_ranks for t in entries) if entries[0].node_type == NODE_TIERED
                else entries[index if choice else 0].max_ranks)
        rank = min(rank, full)
        writer.write(1, 1)  # selected
        writer.write(1, 1)  # purchased, rather than granted for free
        if rank < full:
            writer.write(1, 1)  # a partial rank, spelled out. simc refuses this flag on a full one
            writer.write(rank, 6)
        else:
            writer.write(0, 1)
        if choice:
            writer.write(1, 1)  # a choice node: say which side
            writer.write(min(index, 3), 2)
        else:
            writer.write(0, 1)
    return writer.text()


def decode(text: str, traits: list[Trait]) -> dict[int, int]:
    """{nodeID: ranks} from a loadout string, for checking what encode() produced."""
    reader = _Reader(text)
    if reader.read(8) != VERSION:
        raise TalentError("talent string in a format this build doesn't read")
    spec_id = reader.read(16)
    for _ in range(16):
        reader.read(8)
    picked: dict[int, int] = {}
    for node_id, entries in nodes_for_class(traits, class_of_spec(traits, spec_id)):
        if not reader.read(1):
            continue
        first = entries[0]
        rank = (sum(t.max_ranks for t in entries) if first.node_type == NODE_TIERED else first.max_ranks)
        if not reader.read(1):
            rank = 1  # granted, not purchased
        else:
            if reader.read(1):
                rank = reader.read(6)
            if reader.read(1):
                reader.read(2)  # which side of a choice node; the rank is what we're checking
        picked[node_id] = rank
    return picked


def picks_from_log(talent_tree: list[dict]) -> dict[int, tuple[int | None, int]]:
    """WCL's talentTree rows -> {nodeID: (entry id, ranks)}. A tiered node appears once per entry,
    so its ranks add up."""
    picks: dict[int, tuple[int | None, int]] = {}
    for row in talent_tree or []:
        node = row.get("nodeID")
        if not isinstance(node, int):
            continue
        entry, rank = row.get("id"), row.get("rank") or 1
        previous = picks.get(node)
        picks[node] = (previous[0] if previous else entry, (previous[1] if previous else 0) + rank)
    return picks


def talents_line(talent_tree: list[dict], spec_id: int, traits: list[Trait]) -> str:
    """The `talents=` value for a profile, checked by decoding it again."""
    picks = picks_from_log(talent_tree)
    if not picks:
        raise TalentError("the log recorded no talents for this pull")
    known = {node for node, _ in nodes_for_class(traits, class_of_spec(traits, spec_id))}
    wanted = {node for node in picks if node in known}
    if not wanted:
        raise TalentError("none of the log's nodes are in this simc build's trait table")
    text = encode(picks, spec_id, traits)
    back = decode(text, traits)
    missing = sorted(wanted - set(back))
    if missing:
        raise TalentError(f"{len(missing)} picked nodes didn't survive the round trip (e.g. {missing[:3]})")
    return text

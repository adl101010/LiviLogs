"""The talent encoder's bit layout, against a hand-built trait table.

The real table comes from a simc build (engine/dbc/generated/trait_data.inc), which isn't in this
repo, so these rows stand in for it: one plain node, one choice node, one tiered node, and one node
belonging to another class that must not take up any bits.
"""

import pytest

from tools.simtalents import (
    TalentError, Trait, class_of_spec, decode, encode, picks_from_log, talents_line,
)

MAGE, PRIEST = 8, 5
FIRE, SHADOW = 63, 258
SPECS = (63, 62, 64)

TRAITS = [
    # tree, class, entry, node, max ranks, spell, name, specs, sub tree, node type
    Trait(1, MAGE, 1001, 100, 2, 50001, "Frostbite", SPECS, 0, 0),
    Trait(2, MAGE, 1002, 200, 1, 50002, "Left", (FIRE,), 0, 3),   # a choice node: two entries,
    Trait(2, MAGE, 1003, 200, 1, 50003, "Right", (FIRE,), 0, 3),  # same node id
    Trait(2, MAGE, 1004, 300, 1, 50004, "Tier one", (FIRE,), 0, 1),   # tiered: ranks spill from
    Trait(2, MAGE, 1005, 300, 1, 50005, "Tier two", (FIRE,), 0, 1),   # one entry to the next
    Trait(1, PRIEST, 9001, 400, 1, 59001, "Not a mage", (SHADOW,), 0, 0),
]


def test_a_build_survives_the_round_trip():
    picks = {100: (1001, 2), 200: (1003, 1), 300: (1004, 2)}
    text = encode(picks, FIRE, TRAITS)
    assert decode(text, TRAITS) == {100: 2, 200: 1, 300: 2}


def test_only_picked_nodes_are_selected():
    text = encode({100: (1001, 1)}, FIRE, TRAITS)
    assert decode(text, TRAITS) == {100: 1}  # the choice and tiered nodes stay unselected


def test_another_class_takes_no_bits():
    """The priest node must not shift the mage's bits along: same string either way."""
    picks = {100: (1001, 2)}
    without = [t for t in TRAITS if t.class_id == MAGE]
    assert encode(picks, FIRE, TRAITS) == encode(picks, FIRE, without)


def test_the_header_carries_the_spec():
    text = encode({100: (1001, 1)}, FIRE, TRAITS)
    assert class_of_spec(TRAITS, FIRE) == MAGE
    assert decode(text, TRAITS) == {100: 1}
    with pytest.raises(TalentError):
        class_of_spec(TRAITS, 999)  # a spec this table has never heard of


def test_wcl_rows_become_picks():
    # A tiered node arrives once per entry; its ranks add up, and the first entry names the node.
    tree = [{"nodeID": 100, "id": 1001, "rank": 2},
            {"nodeID": 300, "id": 1004, "rank": 1},
            {"nodeID": 300, "id": 1005, "rank": 1}]
    assert picks_from_log(tree) == {100: (1001, 2), 300: (1004, 2)}


def test_a_line_is_only_returned_when_it_decodes_back():
    tree = [{"nodeID": 100, "id": 1001, "rank": 2}, {"nodeID": 200, "id": 1003, "rank": 1}]
    assert talents_line(tree, FIRE, TRAITS)
    with pytest.raises(TalentError):
        talents_line([], FIRE, TRAITS)  # the log recorded no talents
    with pytest.raises(TalentError):
        talents_line([{"nodeID": 7777, "id": 1, "rank": 1}], FIRE, TRAITS)  # nodes this build lacks

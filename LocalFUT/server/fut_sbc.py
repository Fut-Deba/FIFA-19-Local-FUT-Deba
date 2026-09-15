#!/usr/bin/env python3
"""Source-gated FIFA 19 Squad Building Challenge primitives.

This module deliberately separates three concerns which the early local SBC
stub mixed together:

* loading only catalogue records whose source and local reward mappings are
  complete;
* evaluating typed requirements without executing scraped English text; and
* producing deterministic DTOs while persistence stays in ``fut_state``.

Research captures are never runtime input.  An unresolved historical record is
valid research evidence, but it is not an executable challenge.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass


CATALOG_SCHEMA_VERSION = 1
RUNTIME_VERIFICATIONS = frozenset(("historical_verified","local_implemented"))
ICON_LEAGUE_ID = 2118


# historical_reference's archived FIFA 19 builder numbers starting-XI cards from attack to
# goalkeeper (1..11).  These graphs and positions are normalized from the
# hashed 19old builder source recorded in FUT19_SBC_CHEMISTRY_REFERENCE.md.
# They are deliberately source-slot data, not a claim about EA's native DTO
# indices; historical challenges remain disabled until that wire mapping is
# observed in the retail client.
FORMATION_ALIASES = {
    "3142": "3142", "f3142": "3142", "3-1-4-2": "3142",
    "3412": "3412", "f3412": "3412", "3-4-1-2": "3412",
    "3421": "3421", "f3421": "3421", "3-4-2-1": "3421",
    "343": "343", "f343": "343", "3-4-3": "343",
    "352": "352", "f352": "352", "3-5-2": "352",
    "41212": "41212", "f41212": "41212", "4-1-2-1-2": "41212",
    "4132": "4132", "f4132": "4132", "4-1-3-2": "4132",
    "4141": "4141", "f4141": "4141", "4-1-4-1": "4141",
    "4231": "4231", "f4231": "4231",
    # The pilot archive's unqualified 4-2-3-1 uses LM/CAM/RM and therefore
    # matches historical_reference's wide 4231-2 graph, not the three-CAM 4231 graph.
    "4231-2": "4231-2", "f4231-2": "4231-2",
    "4231_2": "4231-2", "f4231_2": "4231-2",
    "4-2-3-1": "4231-2", "4-2-3-1(2)": "4231-2",
    "4222": "4222", "f4222": "4222", "4-2-2-2": "4222",
    "424": "424", "f424": "424", "4-2-4": "424",
    "4312": "4312", "f4312": "4312", "4-3-1-2": "4312",
    "4321": "4321", "f4321": "4321", "4-3-2-1": "4321",
    "433": "433", "f433": "433", "4-3-3": "433",
    "433-2": "433-2", "f433-2": "433-2",
    "433_2": "433-2", "f433_2": "433-2", "4-3-3(2)": "433-2",
    "433-3": "433-3", "f433-3": "433-3",
    "433_3": "433-3", "f433_3": "433-3", "4-3-3(3)": "433-3",
    "433-4": "433-4", "f433-4": "433-4",
    "433_4": "433-4", "f433_4": "433-4", "4-3-3(4)": "433-4",
    "4411": "4411", "f4411": "4411", "4-4-1-1": "4411",
    "442": "442", "f442": "442", "4-4-2": "442",
    "451": "451", "f451": "451", "4-5-1": "451",
    "5212": "5212", "f5212": "5212", "5-2-1-2": "5212",
    "5221": "5221", "f5221": "5221", "5-2-2-1": "5221",
    "532": "532", "f532": "532", "5-3-2": "532",
}


def _edges(*pairs):
    return tuple(tuple(pair) for pair in pairs)


FORMATIONS = {
    "3142": {
        "positions": ("ST", "ST", "LM", "CM", "CM", "RM", "CDM",
                      "CB", "CB", "CB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,5),(2,6),(3,4),(3,7),
                        (3,8),(4,5),(4,7),(5,6),(5,7),(6,7),(6,10),
                        (7,8),(7,9),(7,10),(8,9),(8,11),(9,10),
                        (9,11),(10,11)),
    },
    "3412": {
        "positions": ("ST", "CAM", "ST", "LM", "CM", "CM", "RM",
                      "CB", "CB", "CB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,5),(2,6),(3,7),
                        (4,5),(4,8),(5,6),(5,9),(6,7),(6,9),(7,10),
                        (8,9),(8,11),(9,10),(9,11),(10,11)),
    },
    "3421": {
        "positions": ("LF", "ST", "RF", "LM", "CM", "CM", "RM",
                      "CB", "CB", "CB", "GK"),
        "edges": _edges((1,2),(1,4),(1,5),(2,3),(3,6),(3,7),(4,5),
                        (4,8),(5,6),(5,9),(6,7),(6,9),(7,10),(8,9),
                        (8,11),(9,10),(9,11),(10,11)),
    },
    "343": {
        "positions": ("LW", "ST", "RW", "LM", "CM", "CM", "RM",
                      "CB", "CB", "CB", "GK"),
        "edges": _edges((1,2),(1,4),(2,3),(2,5),(2,6),(3,7),(4,5),
                        (4,8),(5,6),(5,9),(6,7),(6,9),(7,10),(8,9),
                        (8,11),(9,10),(9,11),(10,11)),
    },
    "352": {
        "positions": ("ST", "CAM", "ST", "LM", "CDM", "CDM", "RM",
                      "CB", "CB", "CB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,5),(2,6),(3,7),
                        (4,5),(4,8),(5,6),(5,8),(5,9),(6,7),(6,9),
                        (6,10),(7,10),(8,9),(8,11),(9,10),(9,11),
                        (10,11)),
    },
    "41212": {
        "positions": ("ST", "CAM", "ST", "LM", "CDM", "RM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,4),(2,5),(2,6),
                        (3,6),(4,5),(4,7),(5,6),(5,8),(5,9),(6,10),
                        (7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "4132": {
        "positions": ("ST", "ST", "LM", "CM", "RM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,4),(2,5),(3,4),(3,6),
                        (3,7),(4,5),(4,6),(5,6),(5,10),(6,7),(6,8),
                        (6,9),(6,10),(7,8),(8,9),(8,11),(9,10),
                        (9,11)),
    },
    "4141": {
        "positions": ("ST", "LM", "CM", "CM", "RM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(1,5),(2,3),(2,7),(3,4),
                        (3,6),(3,8),(4,5),(4,6),(4,9),(5,10),(6,8),
                        (6,9),(7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "4231": {
        "positions": ("ST", "CAM", "CAM", "CAM", "CDM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,5),(3,4),(3,5),
                        (3,6),(4,6),(5,7),(5,8),(6,9),(6,10),(7,8),
                        (8,9),(8,11),(9,10),(9,11)),
    },
    "4231-2": {
        "positions": ("ST", "LM", "CAM", "RM", "CDM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,5),(2,7),(3,4),
                        (3,5),(3,6),(4,6),(4,10),(5,7),(5,8),(6,9),
                        (6,10),(7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "4222": {
        "positions": ("ST", "ST", "CAM", "CAM", "CDM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,5),(2,4),(2,6),(3,5),(3,7),
                        (4,6),(4,10),(5,6),(5,8),(6,9),(7,8),(8,9),
                        (8,11),(9,10),(9,11)),
    },
    "424": {
        "positions": ("LW", "ST", "ST", "RW", "CM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,5),(1,7),(2,3),(2,5),(3,4),(3,6),
                        (4,6),(4,10),(5,6),(5,7),(5,8),(6,9),(6,10),
                        (7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "4312": {
        "positions": ("ST", "CAM", "ST", "CM", "CM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,4),(2,5),(2,6),
                        (3,6),(4,5),(4,7),(4,8),(5,6),(5,8),(5,9),
                        (6,9),(6,10),(7,8),(8,9),(8,11),(9,10),
                        (9,11)),
    },
    "4321": {
        "positions": ("LF", "ST", "RF", "CM", "CM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,4),(1,5),(2,3),(2,5),(3,5),(3,6),
                        (4,5),(4,7),(4,8),(5,6),(5,8),(5,9),(6,9),
                        (6,10),(7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "433": {
        "positions": ("LW", "ST", "RW", "CM", "CM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,4),(2,3),(2,5),(3,6),(4,5),(4,7),
                        (5,6),(5,8),(5,9),(6,10),(7,8),(8,9),(8,11),
                        (9,10),(9,11)),
    },
    "433-2": {
        "positions": ("LW", "ST", "RW", "CM", "CDM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,4),(2,3),(2,5),(3,6),(4,5),(4,7),
                        (4,8),(5,6),(5,8),(5,9),(6,9),(6,10),(7,8),
                        (8,9),(8,11),(9,10),(9,11)),
    },
    "433-3": {
        "positions": ("LW", "ST", "RW", "CDM", "CM", "CDM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,4),(2,3),(2,5),(3,6),(4,5),(4,7),
                        (4,8),(5,6),(6,9),(6,10),(7,8),(8,9),(8,11),
                        (9,10),(9,11)),
    },
    "433-4": {
        "positions": ("LW", "ST", "RW", "CM", "CAM", "CM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,4),(1,5),(2,3),(2,5),(3,5),(3,6),
                        (4,5),(4,7),(4,8),(5,6),(5,8),(5,9),(6,9),
                        (6,10),(7,8),(8,9),(8,11),(9,10),(9,11)),
    },
    "4411": {
        "positions": ("ST", "CF", "LM", "CM", "CM", "RM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(2,3),(2,4),(2,5),(2,6),(3,4),(3,7),
                        (4,5),(4,8),(5,6),(5,9),(6,10),(7,8),(8,9),
                        (8,11),(9,10),(9,11)),
    },
    "442": {
        "positions": ("ST", "ST", "LM", "CM", "CM", "RM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,5),(2,6),(3,4),(3,7),
                        (4,5),(4,8),(5,6),(5,9),(6,10),(7,8),(8,9),
                        (8,11),(9,10),(9,11)),
    },
    "451": {
        "positions": ("ST", "LM", "CAM", "CM", "CAM", "RM", "LB",
                      "CB", "CB", "RB", "GK"),
        "edges": _edges((1,3),(1,5),(2,3),(2,7),(3,4),(3,5),(4,5),
                        (4,8),(4,9),(5,6),(6,10),(7,8),(8,9),(8,11),
                        (9,10),(9,11)),
    },
    "5212": {
        "positions": ("ST", "CAM", "ST", "CM", "CM", "LWB", "CB",
                      "CB", "CB", "RWB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,3),(2,4),(2,5),(3,5),
                        (4,5),(4,6),(4,8),(4,9),(5,9),(5,10),(6,8),
                        (7,8),(7,11),(8,9),(8,11),(9,10),(9,11)),
    },
    "5221": {
        "positions": ("LW", "ST", "RW", "CM", "CM", "LWB", "CB",
                      "CB", "CB", "RWB", "GK"),
        "edges": _edges((1,2),(1,4),(1,6),(2,3),(2,4),(2,5),(3,5),
                        (3,10),(4,5),(4,6),(4,9),(5,9),(5,10),(6,8),
                        (7,9),(7,10),(7,11),(8,9),(8,11),(9,11)),
    },
    "532": {
        "positions": ("ST", "ST", "CM", "CM", "CM", "LWB", "CB",
                      "CB", "CB", "RWB", "GK"),
        "edges": _edges((1,2),(1,3),(1,4),(2,4),(2,5),(3,4),(3,6),
                        (3,8),(4,5),(4,9),(5,7),(5,10),(6,8),(7,9),
                        (7,10),(7,11),(8,9),(8,11),(9,11)),
    },
}


# FIFA's native squad arrays are goalkeeper-first, but they are not a plain
# reversal of historical_reference's attack-first chemistry slots.  The retail controller
# enumerates every horizontal line from right to left and keeps central roles
# in their Frostbite order.  In 4-1-2-1-2, for example, native indexes 5/6 are
# CDM/RM and 8/9/10 are CAM/right-ST/left-ST; ``11-index`` incorrectly turns
# those into RM/CDM and ST/CAM/ST.  These source-slot orders are the explicit
# bridge between the two independently sourced layouts.
NATIVE_SOURCE_SLOT_ORDER = {
    "3142":   (11,10,9,8,7,6,5,4,3,2,1),
    "3412":   (11,10,9,8,7,6,5,4,2,3,1),
    "3421":   (11,10,9,8,7,6,5,4,3,1,2),
    "343":    (11,10,9,8,7,6,5,4,3,1,2),
    "352":    (11,10,9,8,6,5,7,4,2,3,1),
    "41212":  (11,10,9,8,7,5,6,4,2,3,1),
    "4132":   (11,10,9,8,7,6,5,4,3,2,1),
    "4141":   (11,10,9,8,7,6,5,4,3,2,1),
    "4231":   (11,10,9,8,7,6,5,4,3,2,1),
    "4231-2": (11,10,9,8,7,6,5,4,3,2,1),
    "4222":   (11,10,9,8,7,6,5,4,3,2,1),
    "424":    (11,10,9,8,7,6,5,4,3,2,1),
    "4312":   (11,10,9,8,7,6,5,4,2,3,1),
    "4321":   (11,10,9,8,7,6,5,4,3,1,2),
    "433":    (11,10,9,8,7,6,5,4,3,1,2),
    "433-2":  (11,10,9,8,7,6,5,4,3,1,2),
    "433-3":  (11,10,9,8,7,6,5,4,3,1,2),
    "433-4":  (11,10,9,8,7,6,5,4,3,1,2),
    "4411":   (11,10,9,8,7,6,5,4,3,2,1),
    "442":    (11,10,9,8,7,6,5,4,3,2,1),
    "451":    (11,10,9,8,7,6,5,4,3,2,1),
    "5212":   (11,10,9,8,7,6,5,4,2,3,1),
    "5221":   (11,10,9,8,7,6,5,4,3,1,2),
    "532":    (11,10,9,8,7,6,5,4,3,2,1),
}


def native_source_slot(formation, native_index):
    """Map one retail 0..10 squad index to a historical_reference chemistry slot."""
    formation_key = normalize_formation(formation)
    index = _integer(native_index, -1)
    order = NATIVE_SOURCE_SLOT_ORDER.get(formation_key)
    if order is None or index < 0 or index >= len(order):
        raise SbcValidationError("The native SBC slot mapping is not verified for this submission.")
    return int(order[index])


def native_mask_to_source(formation, native_mask):
    """Project a native goalkeeper-first OPEN/BRICK mask to source order."""
    mask = list(native_mask or [])
    if not mask:
        return []
    if len(mask) != 11:
        raise SbcValidationError("The native SBC slot mapping is not verified for this submission.")
    result = ["BRICK"] * 11
    for native_index, value in enumerate(mask):
        result[native_source_slot(formation, native_index) - 1] = value
    return result


RELATED_POSITION_PAIRS = frozenset({
    ("LWB","LB"),("RWB","RB"),("CM","CDM"),("CM","CAM"),
    ("CDM","CM"),("CAM","CM"),("CAM","CF"),("CF","ST"),
    ("CF","CAM"),("ST","CF"),("RF","RW"),("RW","RF"),
    ("RW","RM"),("RM","RW"),("LF","LW"),("LW","LF"),
    ("LW","LM"),("LM","LW"),("LB","LWB"),("RB","RWB"),
})

WEAK_POSITION_PAIRS = frozenset({
    ("CB","RB"),("CB","CDM"),("CB","LB"),
    ("LWB","LM"),("LWB","RWB"),("LWB","LW"),
    ("LB","LM"),("LB","RB"),("LB","CB"),
    ("RWB","RM"),("RWB","RW"),("RWB","LWB"),
    ("RB","RM"),("RB","LB"),("RB","CB"),
    ("CM","LM"),("CM","RM"),("CDM","CB"),("CDM","CAM"),
    ("CAM","CDM"),("CF","LF"),("CF","RF"),
    ("ST","LF"),("ST","RF"),
    ("RF","ST"),("RF","CF"),("RF","LF"),("RF","RM"),
    ("RW","LW"),("RW","RWB"),
    ("RM","RF"),("RM","RWB"),("RM","RB"),("RM","LM"),
    ("RM","CM"),("LF","ST"),("LF","CF"),("LF","RF"),
    ("LF","LM"),("LW","RW"),("LW","LWB"),
    ("LM","LF"),("LM","LB"),("LM","LWB"),("LM","CM"),
    ("LM","RM"),
})


class SbcCatalogueError(ValueError):
    pass


class SbcValidationError(ValueError):
    def __init__(self, message, results=None):
        super().__init__(message)
        self.results = list(results or [])


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _item_id(row):
    if not isinstance(row, dict):
        return 0
    item = row.get("itemData") if isinstance(row.get("itemData"), dict) else row
    return _integer(item.get("id", row.get("itemId", row.get("id", 0))))


def normalize_formation(value):
    key = str(value or "").strip().lower().replace(" ", "")
    formation = FORMATION_ALIASES.get(key)
    if formation not in FORMATIONS:
        raise SbcValidationError("Server-side chemistry is unavailable for this formation.")
    return formation


def position_chemistry_points(player_position, squad_position):
    """Return FIFA 19 position points: exact 3, related 2, weak 1, wrong -4."""
    pair = (str(squad_position or "").upper(),
            str(player_position or "").upper())
    if pair[0] and pair[0] == pair[1]:
        return 3
    if pair in RELATED_POSITION_PAIRS:
        return 2
    if pair in WEAK_POSITION_PAIRS:
        return 1
    return -4


def _chemistry_identity(item, subject):
    aliases = {
        "club": ("club", "clubId", "teamid", "teamId"),
        "league": ("league", "leagueId"),
        "nation": ("nation", "nationId"),
    }
    for key in aliases[subject]:
        value = _integer(item.get(key, 0))
        if value:
            return value
    return 0


def _player_position(item):
    for key in ("preferredPosition", "position", "pos"):
        value = str(item.get(key, "") or "").upper().strip()
        if value:
            return value
    return ""


def _loyalty_bonus(item):
    explicit = item.get("loyalty")
    if isinstance(explicit, str):
        explicit = explicit.lower() == "true"
    matches = max(_integer(item.get("gamesPlayed", 0)),
                  _integer(item.get("lifetimeGames", 0)),
                  _integer(item.get("matches", 0)))
    return int(bool(explicit) or _integer(item.get("loyaltyBonus", 0)) > 0 or
               matches >= 10)


def _manager_bonus(item, manager):
    if not isinstance(manager, dict):
        return 0
    same_nation = (_chemistry_identity(item, "nation") and
                   _chemistry_identity(item, "nation") ==
                   _chemistry_identity(manager, "nation"))
    same_league = (_chemistry_identity(item, "league") and
                   _chemistry_identity(item, "league") ==
                   _chemistry_identity(manager, "league"))
    # Matching both is still a single point in FIFA 19.
    return int(bool(same_nation or same_league))


def link_value(left, right):
    """Return the 0..3 club/league/nation value of one active link."""
    club = _chemistry_identity(left, "club")
    league = _chemistry_identity(left, "league")
    nation = _chemistry_identity(left, "nation")
    other_club = _chemistry_identity(right, "club")
    other_league = _chemistry_identity(right, "league")
    other_nation = _chemistry_identity(right, "nation")
    club_point = int(bool(club and club == other_club))
    # Archived historical_reference FIFA 19 code treats league 2118 (Icons) as matching every
    # active neighbor.  Same-nation and same-Icon-club points still stack.
    league_point = int(bool(
        league and other_league and
        (league == other_league or league == ICON_LEAGUE_ID or
         other_league == ICON_LEAGUE_ID)))
    nation_point = int(bool(nation and nation == other_nation))
    return club_point + league_point + nation_point


def _link_chemistry(link_sum, link_count):
    ratio = (float(link_sum) / link_count) if link_count else 0.0
    if ratio > 1.6:
        return 7
    if ratio >= 1.0:
        return 6
    if ratio > 0.32:
        return 3
    return 0


def calculate_squad_chemistry(formation, slot_items, manager=None):
    """Calculate authoritative FIFA 19 chemistry for active starting slots.

    ``slot_items`` is a mapping from historical_reference/source card slot 1..11 to the exact
    submitted item revision.  Empty/brick slots are absent and therefore do
    not participate in either the numerator or denominator of a player's link
    ratio.
    """
    formation_key = normalize_formation(formation)
    spec = FORMATIONS[formation_key]
    active = {}
    for raw_slot, raw_item in dict(slot_items or {}).items():
        slot = _integer(raw_slot)
        item = (raw_item.get("itemData") if isinstance(raw_item, dict) and
                isinstance(raw_item.get("itemData"), dict) else raw_item)
        if slot < 1 or slot > 11 or not isinstance(item, dict):
            raise SbcValidationError("The submitted squad uses an invalid source slot.")
        if slot in active:
            raise SbcValidationError("The submitted squad uses a duplicate source slot.")
        active[slot] = item

    neighbor_map = {slot: [] for slot in active}
    for left, right in spec["edges"]:
        if left in active and right in active:
            neighbor_map[left].append(right)
            neighbor_map[right].append(left)

    individual = {}
    diagnostics = {}
    for slot, item in active.items():
        link_sum = sum(link_value(item, active[other])
                       for other in neighbor_map[slot])
        link_count = len(neighbor_map[slot])
        line_points = _link_chemistry(link_sum, link_count)
        position_points = position_chemistry_points(
            _player_position(item), spec["positions"][slot - 1])
        adjusted_lines = line_points
        if position_points == 1 and line_points > 5:
            adjusted_lines = 4
        elif position_points == 1 and line_points == 3:
            adjusted_lines = 2
        elif position_points == -4 and line_points == 3:
            adjusted_lines = 5
        elif position_points == -4 and line_points == 7:
            adjusted_lines = 6
        chemistry = min(position_points + adjusted_lines, 10)
        if position_points == -4 and line_points == 0:
            chemistry = 0
        loyalty = _loyalty_bonus(item)
        manager_point = _manager_bonus(item, manager)
        chemistry = max(0, min(10, chemistry + loyalty + manager_point))
        individual[slot] = chemistry
        diagnostics[slot] = {
            "slotPosition": spec["positions"][slot - 1],
            "itemPosition": _player_position(item),
            "activeLinks": link_count,
            "linkValue": link_sum,
            "positionPoints": position_points,
            "linkPoints": line_points,
            "loyaltyBonus": loyalty,
            "managerBonus": manager_point,
            "chemistry": chemistry,
        }
    return {
        "formation": formation_key,
        "individual": individual,
        "total": min(100, sum(individual.values())),
        "diagnostics": diagnostics,
    }


NATIVE_FIRST_BENCH_INDEX = 11
NATIVE_LAST_RESERVE_INDEX = 22


def submission_chemistry(challenge, squad, items, manager=None):
    """Calculate chemistry from an SBC submission.

    ``slotMask`` is not a member name FIFA 19 knows: read-only inspection of
    ``CardsDLL_Win64_retail.dll`` shows ``slot`` and ``slotIndex`` in the
    sorted key table at +0x351200 but no mask member. The client therefore
    never receives the challenge's OPEN/BRICK mask, renders its own formation
    and lets the player use any pitch slot.

    A submission that identifies its cards by native ``index`` is consequently
    authoritative for the formation and for the positions used. Bench and
    reserve rows are not part of an SBC squad and are ignored rather than
    rejected. Callers that opt into the explicit source-slot scheme keep the
    former mask and formation behaviour.
    """
    rows = list((squad or {}).get("players", []) or [])
    by_id = {_item_id(item): item for item in items or [] if _item_id(item)}
    explicit_source_slots = any(
        isinstance(row, dict) and ("sourceSlot" in row or "cardId" in row)
        for row in rows)
    submitted_formation = str((squad or {}).get("formation", "") or "")
    formation = challenge.get("formation", (squad or {}).get("formation"))
    if not explicit_source_slots and submitted_formation:
        formation = submitted_formation
    slots = {}
    for row in rows:
        item_id = _item_id(row)
        if not item_id:
            continue
        item = by_id.get(item_id)
        if item is None:
            raise SbcValidationError("Every submitted player must be owned in My Club.")
        slot = _integer(row.get("sourceSlot", row.get("cardId", 0)))
        bench = False
        for key in ("index", "slot"):
            if slot or key not in row:
                continue
            native_index = _integer(row.get(key), -1)
            if 0 <= native_index <= 10:
                slot = native_source_slot(formation, native_index)
            elif (NATIVE_FIRST_BENCH_INDEX <= native_index <=
                  NATIVE_LAST_RESERVE_INDEX):
                bench = True
        if bench and not slot:
            continue
        if not slot:
            raise SbcValidationError(
                "The native SBC slot mapping is not verified for this submission.")
        if slot in slots:
            raise SbcValidationError("The submitted squad uses a duplicate source slot.")
        merged = dict(item)
        for key in ("loyalty", "loyaltyBonus", "gamesPlayed", "lifetimeGames"):
            if key in row:
                merged[key] = row[key]
        slots[slot] = merged

    source_mask=challenge.get("sourceSlotMask")
    if source_mask is None:
        # Runtime/native masks are goalkeeper-first; the chemistry graph is
        # attack-first.  Catalogue records normally retain both, while small
        # local fixtures may submit explicit sourceSlot/cardId values alongside
        # a source-order mask.  Keep that explicit scheme self-consistent.
        native_mask=list(challenge.get("slotMask",[]) or [])
        source_mask=(native_mask if explicit_source_slots
                     else native_mask_to_source(formation,native_mask))
    mask = list(source_mask or [])
    # The locked-slot rule can only bind a caller that opted into explicit
    # source slots. FIFA never sees the mask, so rejecting the player's own
    # placement would make a correct squad unsubmittable.
    if mask and explicit_source_slots:
        open_slots = {index + 1 for index, value in enumerate(mask)
                      if str(value).upper() == "OPEN"}
        if not set(slots).issubset(open_slots):
            raise SbcValidationError("A player was submitted in a locked SBC slot.")
    return calculate_squad_chemistry(formation, slots, manager=manager)


def extract_item_ids(squad):
    """Return the ordered owned-instance IDs encoded by a native squad DTO."""
    if not isinstance(squad, dict):
        return []
    result = []
    for row in squad.get("players", []) or []:
        item_id = _item_id(row)
        if item_id:
            result.append(item_id)
    return result


def quality_for(item):
    quality = str(item.get("quality", item.get("level", "")) or "").upper()
    if quality in {"BRONZE", "SILVER", "GOLD"}:
        return quality
    rating = _integer(item.get("rating", item.get("overall", 0)))
    if rating >= 75:
        return "GOLD"
    if rating >= 65:
        return "SILVER"
    return "BRONZE"


def is_rare(item):
    explicit = item.get("rare")
    if explicit is not None:
        return bool(_integer(explicit))
    # FIFA 19 uses rareflag 0 for a common base card, 1 for a normal rare and
    # higher source-backed rarity IDs for special designs.
    return _integer(item.get("rareflag", item.get("rarityId", 0))) != 0


def is_special(item):
    explicit=item.get("special")
    if explicit is not None:
        return bool(_integer(explicit))
    revision=" ".join(str(item.get(key,"")) for key in (
        "revision","cardType","itemVersion","rarityName","typeName"
    )).strip().upper()
    if revision and revision not in {"NORMAL","BASE","STANDARD"}:
        return True
    return _integer(item.get("rareflag",item.get("rarityId",0))) > 1


def _resource_identity(item):
    return _integer(item.get("resourceId",item.get("definitionId",0)))


def _base_identity(item):
    return _integer(item.get("assetId",item.get("baseId",
                    item.get("baseResourceId",_resource_identity(item)))))


def _card_type_matches(item,expected):
    expected=str(expected or "").strip().upper().replace("_"," ")
    tokens=" ".join(str(item.get(key,"")) for key in (
        "revision","cardRevision","cardType","itemVersion","rarityName","typeName",
        "campaign","promo"
    )).upper().replace("_"," ")
    if expected in {"SPECIAL","ANY SPECIAL"}:
        return is_special(item)
    if expected in {"NORMAL","BASE","STANDARD"}:
        return not is_special(item)
    if expected in {"ICON","ICONS"}:
        return _field(item,"league") == ICON_LEAGUE_ID or "ICON" in tokens
    if expected in {"TOTW","TEAM OF THE WEEK","IN FORM","INFORM"}:
        revision=str(item.get("revision",item.get("cardRevision",""))).upper()
        return any(value in tokens for value in
                   ("TOTW","TEAM OF THE WEEK","IN FORM","INFORM")) or \
               revision in {"IF","SIF","TIF","FIF"}
    if expected in {"UCL","CHAMPIONS LEAGUE","UEFA CHAMPIONS LEAGUE"}:
        return "UCL" in tokens or "CHAMPIONS LEAGUE" in tokens
    return bool(expected and expected in tokens)


def fut_squad_rating(items):
    """Return the FUT weighted squad rating used by SBC requirement checks.

    FUT weights cards above the arithmetic mean a second time.  Using the
    unrounded mean is important: it avoids the familiar one-rating boundary
    errors produced by a plain rounded average.
    """
    ratings = [_integer(item.get("rating", item.get("overall", 0))) for item in items]
    if not ratings:
        return 0
    mean = sum(ratings) / float(len(ratings))
    adjusted = sum(ratings) + sum(max(0.0, rating - mean) for rating in ratings)
    return int(adjusted / len(ratings) + 0.5)


def _field(item, subject):
    aliases = {
        "nation": ("nation", "nationId"),
        "league": ("league", "leagueId"),
        "club": ("club", "teamId", "teamid"),
    }
    for name in aliases[subject]:
        value = _integer(item.get(name, 0))
        if value:
            return value
    return 0


def _compare(operator, actual, target):
    if operator == "exact":
        return actual == target
    if operator == "min":
        return actual >= target
    if operator == "max":
        return actual <= target
    raise SbcCatalogueError("unsupported SBC operator: %s" % operator)


def evaluate_requirements(requirements, items, chemistry=None, squad=None,
                          individual_chemistry=None):
    """Evaluate typed catalogue rules against authoritative owned item data."""
    items = list(items or [])
    owned_by_id={_item_id(item):item for item in items if _item_id(item)}
    submitted_rows=[]
    for row in (squad or {}).get("players",[]) or []:
        item=owned_by_id.get(_item_id(row))
        if item is not None:
            submitted_rows.append((row,item))

    def slot_item(rule):
        source_slot=_integer(rule.get("sourceSlot",0))
        native_index=rule.get("nativeIndex")
        if native_index is not None:
            native_index=_integer(native_index,-1)
        elif "slot" in rule:
            if str(rule.get("slotIndexScheme","")).lower().startswith("source"):
                source_slot=_integer(rule.get("slot",0))
                native_index=None
            else:
                native_index=_integer(rule.get("slot",-1),-1)
        for row,item in submitted_rows:
            row_source=_integer(row.get("sourceSlot",row.get("cardId",0)))
            row_native=_integer(row.get("index",row.get("slot",-1)),-1)
            if source_slot and not row_source and 0 <= row_native <= 10:
                row_source=11-row_native
            if source_slot and row_source == source_slot:
                return item
            if native_index is not None and native_index == row_native:
                return item
        return None

    results = []
    for rule in requirements or []:
        if not isinstance(rule, dict):
            raise SbcCatalogueError("SBC requirement must be an object")
        kind = str(rule.get("type", "")).lower()
        operator = str(rule.get("operator", "exact")).lower()
        target = _integer(rule.get("value", rule.get("count", rule.get("rating", 0))))
        actual = None
        if kind == "player_count":
            actual = len(items)
        elif kind == "squad_rating":
            actual = fut_squad_rating(items)
        elif kind == "squad_chemistry":
            if chemistry is None:
                raise SbcValidationError(
                    "Server-side chemistry is unavailable for this formation.", results)
            actual = _integer(chemistry)
        elif kind == "player_quality":
            expected = str(rule.get("quality", "")).upper()
            if operator == "exact" or expected == "SPECIAL":
                actual = sum(1 for item in items if (
                    is_special(item) if expected == "SPECIAL" else
                    quality_for(item) == expected))
                target = len(items)
                operator = "exact"
            else:
                # "Min Silver" is a quality floor: Gold cards qualify too.
                # Counting only exact Silver cards made the authoritative
                # result contradict the native PLAYER_LEVEL/MIN row.
                levels = [_NATIVE_QUALITY_LEVELS.get(quality_for(item), 0)
                          for item in items]
                actual = min(levels) if levels else 0
                target = _NATIVE_QUALITY_LEVELS.get(expected, 0)
        elif kind in {"minimum_quality_count","maximum_quality_count",
                      "quality_count"}:
            expected = str(rule.get("quality", "")).upper()
            actual = sum(1 for item in items if (
                is_special(item) if expected == "SPECIAL" else
                quality_for(item) == expected))
            if kind == "minimum_quality_count": operator = "min"
            if kind == "maximum_quality_count": operator = "max"
        elif kind == "rare_count":
            actual = sum(1 for item in items if is_rare(item))
        elif kind == "special_count":
            actual = sum(1 for item in items if is_special(item))
        elif kind.startswith("unique_"):
            subject = kind[len("unique_"):]
            if subject not in {"nation", "league", "club"}:
                raise SbcCatalogueError("unsupported unique subject: %s" % subject)
            actual = len({_field(item, subject) for item in items if _field(item, subject)})
        elif kind.startswith("same_"):
            subject = kind[len("same_"):]
            if subject not in {"nation", "league", "club"}:
                raise SbcCatalogueError("unsupported same subject: %s" % subject)
            counts = {}
            for item in items:
                value = _field(item, subject)
                if value:
                    counts[value] = counts.get(value, 0) + 1
            actual = max(counts.values()) if counts else 0
        elif kind.startswith("specific_"):
            subject = kind[len("specific_"):]
            if subject in {"nation", "league", "club"}:
                identities={_integer(value) for value in
                            rule.get("identities",[]) if _integer(value)}
                identity = _integer(rule.get("identity", rule.get("id", 0)))
                if identity: identities.add(identity)
                if not identities:
                    raise SbcCatalogueError("specific SBC rule has no identity")
                actual = sum(1 for item in items
                             if _field(item, subject) in identities)
            elif subject in {"player","card"}:
                resource_id=_integer(rule.get("resourceId",0))
                base_id=_integer(rule.get("assetId",rule.get("baseId",0)))
                if not resource_id and not base_id:
                    raise SbcCatalogueError("specific player rule has no identity")
                actual=sum(1 for item in items if
                           (resource_id and _resource_identity(item)==resource_id) or
                           (base_id and _base_identity(item)==base_id))
            else:
                raise SbcCatalogueError("unsupported specific subject: %s" % subject)
        elif kind in {"card_type_count","campaign_count","icon_count",
                      "totw_count","ucl_count","swap_deals_count"}:
            expected=rule.get("cardType",rule.get("campaign",""))
            if kind == "icon_count": expected="ICON"
            if kind == "totw_count": expected="TOTW"
            if kind == "ucl_count": expected="UCL"
            if kind == "swap_deals_count": expected="SWAP DEALS"
            expected_values=[str(value) for value in
                             rule.get("cardTypes",[]) if str(value).strip()]
            if expected:
                expected_values.append(str(expected))
            if not expected_values:
                raise SbcCatalogueError("card-type SBC rule has no card type")
            actual=sum(1 for item in items if any(
                _card_type_matches(item,value) for value in expected_values))
        elif kind == "position_count":
            expected={str(value).upper() for value in
                      rule.get("positions",[]) if str(value).strip()}
            if rule.get("position"):
                expected.add(str(rule["position"]).upper())
            if not expected:
                raise SbcCatalogueError("position SBC rule has no position")
            actual=sum(1 for item in items if _player_position(item) in expected)
        elif kind == "player_rating_count":
            rating_target=_integer(rule.get("minimumRating",
                                   rule.get("playerRating",0)))
            rating_operator=str(rule.get("ratingOperator","min")).lower()
            actual=sum(1 for item in items if _compare(
                rating_operator,_integer(item.get("rating",item.get("overall",0))),
                rating_target))
        elif kind in {"individual_chemistry","minimum_individual_chemistry"}:
            values=dict(individual_chemistry or {})
            if not values:
                raise SbcValidationError(
                    "Server-side individual chemistry is unavailable.",results)
            requested_slot=_integer(rule.get("sourceSlot",0))
            actual=(_integer(values.get(requested_slot,0)) if requested_slot
                    else min(_integer(value) for value in values.values()))
            if kind == "minimum_individual_chemistry": operator="min"
        elif kind in {"slot_position","slot_requirement"}:
            item=slot_item(rule)
            matches=item is not None
            if matches and rule.get("position"):
                matches=_player_position(item)==str(rule["position"]).upper()
            if matches and rule.get("quality"):
                expected=str(rule["quality"]).upper()
                matches=(is_special(item) if expected == "SPECIAL" else
                         quality_for(item)==expected)
            if matches and rule.get("cardType"):
                matches=_card_type_matches(item,rule["cardType"])
            if matches and rule.get("resourceId"):
                matches=_resource_identity(item)==_integer(rule["resourceId"])
            for subject in ("nation","league","club"):
                if matches and rule.get(subject+"Id"):
                    matches=_field(item,subject)==_integer(rule[subject+"Id"])
            actual=int(bool(matches)); target=1; operator="exact"
        else:
            raise SbcCatalogueError("unsupported SBC rule type: %s" % kind)
        satisfied = _compare(operator, actual, target)
        results.append({
            "type": kind,
            "operator": operator,
            "actual": actual,
            "target": target,
            "satisfied": satisfied,
            "description": str(rule.get("description", "")),
        })
    return results


def validate_challenge_submission(challenge, squad, items, chemistry=None,
                                  manager=None):
    ids = extract_item_ids(squad)
    if len(ids) != len(set(ids)):
        raise SbcValidationError("The submitted squad contains a duplicate item instance.")
    needs_chemistry = any(
        str(rule.get("type", "")).lower() in {
            "squad_chemistry","individual_chemistry",
            "minimum_individual_chemistry"}
        for rule in challenge.get("requirements", []) if isinstance(rule, dict))
    chemistry_result=None
    if challenge.get("formation") and challenge.get("slotMask"):
        # This also enforces source/native slot uniqueness and locked slots for
        # challenges that do not happen to contain a chemistry threshold.
        chemistry_result=submission_chemistry(
            challenge,squad,items,manager=manager)
    if chemistry is None and needs_chemistry:
        if chemistry_result is None:
            raise SbcValidationError(
                "Server-side chemistry is unavailable for this formation.")
        chemistry=chemistry_result["total"]
    results = evaluate_requirements(
        challenge.get("requirements", []),items,chemistry,squad=squad,
        individual_chemistry=(chemistry_result or {}).get("individual"))
    failed = [result for result in results if not result["satisfied"]]
    if failed:
        message = failed[0].get("description") or "The squad does not meet all requirements."
        raise SbcValidationError(message, results)
    return results


def requirement_dto(rule):
    operator = str(rule.get("operator", "exact")).upper()
    target = _integer(rule.get("value", rule.get("count", rule.get("rating", 0))))
    row = {
        "scope": "SQUAD",
        "type": str(rule.get("nativeType", rule.get("type", ""))).upper(),
        "operator": operator,
        "count": target,
        "value": target,
        "description": str(rule.get("description", "")),
    }
    if str(rule.get("type", "")).lower() == "squad_rating":
        row["rating"] = target
    if rule.get("quality"):
        row["quality"] = str(rule["quality"]).upper()
    for key in ("identity","identities","resourceId","assetId","baseId",
                "position","positions","cardType","cardTypes","campaign","sourceSlot",
                "nativeIndex","slot","slotIndexScheme","minimumRating",
                "playerRating","ratingOperator","nationId","leagueId",
                "clubId"):
        if key in rule:
            row[key]=rule[key]
    return row


# CardsDLL's SBC challenge parser predates the richer typed schema used by the
# local validator.  The native wire format is a flat list of numeric
# eligibility rows grouped by ``eligibilitySlot``; comparison direction is a
# separate SCOPE row.  Sending the typed dictionaries as elgReq (and repeating
# them as requirements/eligibilities) made the retail client dereference an
# invalid rule object while opening a challenge.
_NATIVE_ELIGIBILITY_KEYS = {
    "TEAM_RATING":19, "TEAM_CHEMISTRY":1, "PLAYER_COUNT":2,
    "SAME_NATION_COUNT":4, "SAME_LEAGUE_COUNT":5,
    "SAME_CLUB_COUNT":6, "NATION_COUNT":7, "LEAGUE_COUNT":8,
    "CLUB_COUNT":9, "NATION_ID":10, "LEAGUE_ID":11,
    "CLUB_ID":12, "SCOPE":13, "LEGEND_COUNT":15,
    "PLAYER_LEVEL":17, "PLAYER_RARITY":18,
}
_NATIVE_SCOPE_VALUES = {"MIN":0, "MAX":1, "EXACT":2}
_NATIVE_QUALITY_LEVELS = {"BRONZE":1, "SILVER":2, "GOLD":3}


def _native_scope_row(slot, operator):
    return {"type":"SCOPE", "eligibilitySlot":int(slot),
            "eligibilityKey":_NATIVE_ELIGIBILITY_KEYS["SCOPE"],
            "eligibilityValue":_NATIVE_SCOPE_VALUES.get(
                str(operator or "MIN").upper(),0)}


def _append_native_rule(rows, slot, native_type, value, operator="MIN",
                        count=None, filters=None):
    """Append one parser-safe native eligibility group."""
    if count is not None:
        rows.append({"type":"PLAYER_COUNT", "eligibilitySlot":int(slot),
                     "eligibilityKey":_NATIVE_ELIGIBILITY_KEYS["PLAYER_COUNT"],
                     "eligibilityValue":int(count)})
    rows.append({"type":str(native_type), "eligibilitySlot":int(slot),
                 "eligibilityKey":_NATIVE_ELIGIBILITY_KEYS[native_type],
                 "eligibilityValue":int(value)})
    for filter_type, filter_value in filters or []:
        rows.append({"type":str(filter_type), "eligibilitySlot":int(slot),
                     "eligibilityKey":_NATIVE_ELIGIBILITY_KEYS[filter_type],
                     "eligibilityValue":int(filter_value)})
    rows.append(_native_scope_row(slot,operator))
    return int(slot)+1


def native_eligibility_dto(requirements, publish_standalone_player_count=True):
    """Translate typed runtime rules to FIFA's legacy ``elgReq`` contract.

    Rules with no verified CardsDLL key remain enforced authoritatively by the
    server and are shown through ``elgDesc``.  Inventing a numeric key is less
    useful than omitting it because unknown keys crash the native decoder.

    A player count which scopes another native rule is still emitted.  The
    flag controls only the standalone player-count eligibility group.
    """
    rules=[rule for rule in requirements or [] if isinstance(rule,dict)]
    total_rule=next((rule for rule in rules
                     if str(rule.get("type","")).lower()=="player_count"),{})
    total_players=_integer(total_rule.get(
        "value",total_rule.get("count",11)),11)
    rows=[]
    slot=1
    pending_player_count=None
    for rule in rules:
        kind=str(rule.get("type","")).lower()
        operator=str(rule.get("operator","exact")).upper()
        value=_integer(rule.get(
            "value",rule.get("count",rule.get("rating",0))))
        if kind == "player_count":
            # Partial challenges get their authoritative player-count row from
            # the BRICK playerRequirements layout. RC68 live showed that a
            # second standalone group counts brick slots, stays unsatisfied at
            # 4/4 or 3/3 and prevents FIFA from issuing the submit request.
            # Keep the typed rule above for server validation and for scoping
            # rules such as player_quality; suppress only that false duplicate.
            if publish_standalone_player_count:
                pending_player_count=(value,operator)
            continue
        if kind == "squad_rating":
            slot=_append_native_rule(
                rows,slot,"TEAM_RATING",value,operator)
        elif kind == "squad_chemistry":
            slot=_append_native_rule(
                rows,slot,"TEAM_CHEMISTRY",value,operator)
        elif kind in {"player_quality","minimum_quality_count",
                      "maximum_quality_count","quality_count"}:
            level=_NATIVE_QUALITY_LEVELS.get(
                str(rule.get("quality","")).upper())
            if level is None:
                continue
            count=(total_players if kind == "player_quality" else value)
            if kind == "minimum_quality_count":
                operator="MIN"
            elif kind == "maximum_quality_count":
                operator="MAX"
            slot=_append_native_rule(
                rows,slot,"PLAYER_LEVEL",level,operator,count=count)
        elif kind == "rare_count":
            compatibility_level=_NATIVE_QUALITY_LEVELS.get(str(
                rule.get("nativeCompatibilityQuality","")).upper())
            if compatibility_level is not None:
                slot=_append_native_rule(
                    rows,slot,"PLAYER_LEVEL",compatibility_level,
                    operator,count=value)
            else:
                slot=_append_native_rule(
                    rows,slot,"PLAYER_RARITY",1,operator,count=value)
        elif kind in {"icon_count"}:
            slot=_append_native_rule(
                rows,slot,"LEGEND_COUNT",value,operator)
        elif kind in {"totw_count"}:
            slot=_append_native_rule(
                rows,slot,"PLAYER_RARITY",3,operator,count=value)
        elif kind in {"card_type_count","campaign_count"}:
            rarity_values=[]
            card_types=[str(entry).strip().upper().replace("_"," ")
                        for entry in rule.get("cardTypes",[]) or []]
            if rule.get("cardType",rule.get("campaign")):
                card_types.append(str(rule.get(
                    "cardType",rule.get("campaign"))).strip().upper().replace(
                        "_"," "))
            for card_type in card_types:
                rarity=(3 if card_type in {
                    "TOTW","TEAM OF THE WEEK","IN FORM","INFORM"} else
                        66 if card_type=="TOTS" else
                        18 if card_type in {
                            "FUT CHAMP","FUT-CHAMP","FUT CHAMPIONS"} else
                        None)
                if rarity is not None and rarity not in rarity_values:
                    rarity_values.append(rarity)
            if rarity_values and len(rarity_values)==len(set(card_types)):
                # EA's grouped eligibility contract represents alternatives
                # by repeating one key in the same slot (the same shape used
                # by sourced multi-identity rules).  Keeping IF rarity 3 and
                # TOTS rarity 66 in one group is what makes "IF + TOTS" one
                # OR requirement instead of two impossible AND rows.
                slot=_append_native_rule(
                    rows,slot,"PLAYER_RARITY",rarity_values[0],operator,
                    count=value,filters=[("PLAYER_RARITY",entry)
                                         for entry in rarity_values[1:]])
        elif kind == "swap_deals_count":
            # FIFA 19 represents every monthly Swap token with rarity 52. The
            # month/phase is presentation text; the native eligibility rule is
            # the exact rarity plus a count, which is also what historical_reference's
            # "SWAP DEALS 4 Players" row resolves to in this client.
            slot=_append_native_rule(
                rows,slot,"PLAYER_RARITY",52,operator,count=value)
        elif kind.startswith("unique_"):
            subject=kind[len("unique_"):]
            native_type={"nation":"NATION_COUNT", "league":"LEAGUE_COUNT",
                         "club":"CLUB_COUNT"}.get(subject)
            if native_type:
                slot=_append_native_rule(
                    rows,slot,native_type,value,operator)
        elif kind.startswith("same_"):
            subject=kind[len("same_"):]
            native_type={"nation":"SAME_NATION_COUNT",
                         "league":"SAME_LEAGUE_COUNT",
                         "club":"SAME_CLUB_COUNT"}.get(subject)
            if native_type:
                slot=_append_native_rule(
                    rows,slot,native_type,value,operator)
        elif kind.startswith("specific_"):
            subject=kind[len("specific_"):]
            native_type={"nation":"NATION_ID", "league":"LEAGUE_ID",
                         "club":"CLUB_ID"}.get(subject)
            identity=_integer(rule.get("identity",rule.get("id",0)))
            identities=[]
            if identity:
                identities.append(identity)
            for entry in rule.get("identities",[]):
                value_identity=_integer(entry)
                if value_identity and value_identity not in identities:
                    identities.append(value_identity)
            if native_type and identities:
                # EA represents combined identity rows such as "Gabon +
                # Cameroon" by repeating the same identity key inside one
                # eligibility group.  Separate groups would mean AND and
                # incorrectly require a player from every listed nation.
                slot=_append_native_rule(
                    rows,slot,native_type,identities[0],operator,count=value,
                    filters=[(native_type,entry)
                             for entry in identities[1:]])
    if pending_player_count is not None:
        value,operator=pending_player_count
        already=any(row["type"]=="PLAYER_COUNT" and
                    int(row["eligibilityValue"])==int(value)
                    for row in rows)
        if not already:
            slot=_append_native_rule(
                rows,slot,"PLAYER_COUNT",value,operator)
    return rows


def native_reward_dto(reward):
    """Return the exact Award object consumed by the retail parser.

    The shared decoder at ``CardsDLL+0x273da0`` reads ``awardType``,
    ``awardValue``, ``awardCount``, ``halId``, ``loan``, ``untradeable`` and
    optional ``itemData``.  The project aliases ``type/value/count`` are not
    accepted by that decoder, which made both SBC Group Rewards and Squad
    Battles rank rewards exist in the backend but render as an empty panel.
    """
    source=reward_dto(reward)
    award_type=str(source.get("type","") or "")
    item_data=source.get("itemData")
    # Player Pick is not a native Award enum in this build.  Preview it as
    # EA's official FUT 19 Pick Item #3; the persisted pending pick and its
    # exact options are still granted by FutState when the SBC is completed.
    if award_type == "playerPick":
        pick_id=_integer(source.get(
            "playerPickId",source.get("value",source.get("id",0))))
        item_data={
            # A set-index preview has no real item yet and therefore keeps id
            # zero.  A submit receipt carries the persisted pending Pick Item
            # identity here so CardsDLL can move directly to Unassigned and
            # show the completion/reward flow without requiring a FUT relog.
            "id":pick_id,"itemId":pick_id,"formation":"f442",
            "untradeable":True,
            "assetId":5004045,"definitionId":5004045,
            "resourceId":5004045,"rating":99,"rareflag":1,
            "itemType":"misc","itemState":"free","cardsubtypeid":237,
            "owners":1,"discardValue":0,"lastSalePrice":0,"pile":6,
            "resourceGameYear":2019,"amount":3002,"value":3002,
            "playerPickId":pick_id,"playerPickDefinitionId":3002,
            "name":str(source.get("label",source.get("name",source.get(
                "displayName","Player Pick"))) or "Player Pick"),
            "description":str(source.get("description","") or
                "Choose one reward from this Player Pick."),
            "optionCount":_integer(source.get("optionCount",0)),
        }
        source["untradeable"]=True
        award_type="item"
        # Award.value is a definition/resource identity.  The concrete owned
        # instance belongs in itemData.id/playerPickId.  Publishing the large
        # instance id as awardValue made the submit transaction durable but
        # prevented CardsDLL's reward factory from resolving Pick Item #3 in
        # the immediate SBC completion flow.
        source["value"]=5004045
    row={
        "awardType":award_type,
        "awardValue":_integer(source.get("value",0)),
        "awardCount":max(1,_integer(source.get("count",1),1)),
        "halId":_integer(source.get("halId",0)),
        "loan":_integer(source.get("loan",0)),
        "untradeable":bool(source.get(
            "untradeable",source.get("isUntradeable",False))),
    }
    if award_type == "item" and isinstance(item_data,dict):
        row["itemData"]=item_data
    if source.get("type") == "item" and isinstance(source.get("itemData"),dict):
        row["itemData"]=source["itemData"]
    return row


_PACK_REWARD_CACHE=None


def _pack_reward_metadata(pack_id):
    """Resolve safe pack preview metadata without importing the pack runtime."""
    global _PACK_REWARD_CACHE
    if _PACK_REWARD_CACHE is None:
        path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data","pack_defs.json")
        try:
            with open(path,encoding="utf-8") as handle:
                payload=json.load(handle)
            _PACK_REWARD_CACHE=(payload if isinstance(payload,dict) else {})
        except (OSError,TypeError,ValueError,json.JSONDecodeError):
            _PACK_REWARD_CACHE={}
    return dict(_PACK_REWARD_CACHE.get(str(_integer(pack_id)),{}) or {})


def _player_reward_item(resource_id,untradeable=True,loan_games=0):
    """Build a complete display-only item for an exact player reward."""
    try:
        from fut_catalog import native_player_fields
    except ImportError:
        from .fut_catalog import native_player_fields
    resource_id=_integer(resource_id)
    loan_games=max(0,_integer(loan_games))
    if resource_id <= 0:
        return None
    item=native_player_fields(resource_id,{
        "id":resource_id,"itemId":resource_id,"timestamp":0,
        "pile":0,"itemState":"free","loan":bool(loan_games),
        "loans":loan_games,
        "untradeable":bool(untradeable),"tradeable":not bool(untradeable),
        "acquisitionSource":"SBC_REWARD",
    })
    item.setdefault("definitionId",resource_id)
    item.setdefault("resourceGameYear",2019)
    item.setdefault("contract",item.get("contracts",7))
    item.setdefault("loans",0)
    return item


def _club_item_reward_item(reward,untradeable=True):
    """Build the exact kit preview used by source-backed club-item rewards."""
    try:
        from fut_objects import kit_item_dto
    except ImportError:
        from .fut_objects import kit_item_dto
    team_id=_integer(reward.get("teamId",0))
    category=_integer(reward.get("category",2),2)
    if team_id <= 0 or category not in (2,3):
        return None
    item=kit_item_dto({"id":_integer(reward.get("resourceId",0)),
        "itemId":_integer(reward.get("resourceId",0)),
        "teamid":team_id,"category":category,"rating":75,"rareflag":1,
        "untradeable":bool(untradeable),"tradeable":not bool(untradeable),
        "pile":0,"inventoryType":"kit","timestamp":0})
    item["tradeable"]=not bool(untradeable)
    item["acquisitionSource"]="SBC_REWARD"
    return item


def reward_dto(reward):
    """Return the award spelling and metadata consumed by CardsDLL."""
    row=dict(reward)
    kind=str(row.get("type","")).lower()
    untradeable=bool(row.get("untradeable",row.get("isUntradeable",False)))
    if kind in ("player_pick","playerpick"):
        row["type"]="playerPick"
        row.setdefault("minRating",75)
        row.setdefault("optionCount",3)
    elif kind == "club_item":
        item=_club_item_reward_item(row,untradeable)
        if item is not None:
            resource_id=_integer(item.get("resourceId",row.get("resourceId",0)))
            row.update({"type":"item","resourceId":resource_id,
                        "assetId":_integer(item.get("assetId",resource_id)),
                        "itemData":item,"value":resource_id})
        else:
            row["type"]="clubItem"
    elif kind == "player":
        resource_id=_integer(row.get("resourceId",row.get("value",0)))
        item=(dict(row.get("itemData"))
              if isinstance(row.get("itemData"),dict)
              else _player_reward_item(
                  resource_id,untradeable,row.get("loan",0)))
        if item is not None:
            row.update({"type":"item","resourceId":resource_id,
                        "assetId":_integer(item.get("assetId",resource_id)),
                        "itemData":item,"value":resource_id})
    elif kind == "pack":
        pack_id=_integer(row.get("packId",row.get("value",0)))
        pack=_pack_reward_metadata(pack_id)
        pack_asset_id=_integer(pack.get("packAssetId",pack.get("assetId",4)),4)
        label=str(row.get("label",pack.get("name","Pack")) or "Pack")
        row.update({"value":pack_id,"id":pack_id,"packId":pack_id,
                    "packAssetId":pack_asset_id,"packImageId":pack_asset_id,
                    "assetId":pack_asset_id,"name":label,"displayName":label,
                    "packName":label,"title":label})
    elif kind == "coin":
        row["coins"]=_integer(row.get("value",0))
    row["count"]=max(1,_integer(row.get("count",1),1))
    row.setdefault("awardType",row.get("type",kind))
    row.setdefault("halId",_integer(row.get("value",0)) if kind == "pack" else 0)
    row["isUntradeable"]=untradeable
    row.setdefault("loan",0)
    row.setdefault("loanType","")
    return row


def sbc_preview_reward_dto(reward):
    """Return the reward contract used by SBC set/challenge previews.

    FIFA 19 does not use the submit/Squad Battles ``Award`` reader for the
    catalogue preview embedded in ``/sbs/sets`` and
    ``/sbs/setId/<id>/challenges``.  That older SBS reader consumes the
    original ``type/value/count`` spelling.  The claim response must continue
    to use :func:`native_reward_dto`, so keep this translation deliberately
    local to preview payloads.
    """
    source=reward_dto(reward)
    native=native_reward_dto(reward)
    row={
        "type":str(native.get("awardType",source.get("type","")) or ""),
        "value":_integer(native.get("awardValue",source.get("value",0))),
        "count":max(1,_integer(native.get("awardCount",source.get(
            "count",1)),1)),
        "halId":_integer(native.get("halId",source.get("halId",0))),
        "loan":_integer(native.get("loan",source.get("loan",0))),
        "isUntradeable":bool(native.get("untradeable",source.get(
            "isUntradeable",False))),
    }
    item=native.get("itemData")
    if isinstance(item,dict):
        row["itemData"]=item
        row["resourceId"]=_integer(item.get("definitionId",item.get(
            "resourceId",0)))
    if row["type"] == "pack":
        for key in ("packId","packAssetId","packImageId","assetId",
                    "name","displayName"):
            if key in source:
                row[key]=source[key]
    return row


def sbc_group_preview_reward_dtos(reward):
    """Return the one Award shape proved safe for a Player Pick preview.

    RC71 proved that extra zero-count player Awards do not populate the Pick
    outlines. CardsDLL aggregates every Item Award at ``+0x229b59`` and the
    concrete-player renderer resolves only the Award item's own identity at
    ``+0x22a24d``; no candidate-list member is read from this DTO.
    """
    source=dict(reward or {})
    kind=str(source.get("type","") or "").lower()
    if kind not in {"player_pick","playerpick"}:
        return [sbc_preview_reward_dto(source)]
    preview=sbc_preview_reward_dto(source)
    item=preview.get("itemData")
    if isinstance(item,dict):
        item=dict(item)
        # A catalogue preview is display-only, but Item.createItem still needs
        # a non-zero identity to resolve the official white Pick Item artwork.
        # The real owned instance ID is allocated atomically on completion.
        if _integer(item.get("id",0)) <= 0:
            item["id"]=_integer(item.get("resourceId",5004045),5004045)
            item["itemId"]=item["id"]
        item["name"]=str(source.get("label",item.get("name","Player Pick")))
        item["description"]=str(source.get(
            "description",item.get("description","") or
            "Complete the group to choose one of the advertised players."))
        preview["itemData"]=item
    return [preview]


def challenge_dto(challenge, status="NOT_STARTED"):
    challenge_id = _integer(challenge.get("challengeId", challenge.get("id")))
    image_id=_integer(challenge.get("challengeImageId",1),1)
    awards=[sbc_preview_reward_dto(reward)
            for reward in challenge.get("rewards", [])]
    requirements=list(challenge.get("requirements",[]) or [])
    repeatable=bool(challenge.get("repeatable",False))
    raw_end=challenge.get("endTime")
    end_time=_integer(raw_end,0) if raw_end not in (None,"") else 0
    slot_mask=list(challenge.get("slotMask",[]) or [])
    open_slots=sum(str(value).upper()=="OPEN" for value in slot_mask)
    partial_challenge=bool(slot_mask and open_slots<11)
    descriptions=[str(rule.get("description","")) for rule in requirements
                  if isinstance(rule,dict) and str(rule.get("description","")).strip()]
    return {
        # Keep this projection intentionally close to preserved EA responses.
        # Unknown aliases in this decoder are not benign: the old expanded
        # response crashed CardsDLL while opening First Exchange.
        "name":str(challenge.get("name","")),
        "priority":_integer(challenge.get("priority",1),1),
        "status":str(status),
        "setId":_integer(challenge.get("setId")),
        "description":str(challenge.get("description","")),
        "challengeId": challenge_id,
        "endTime":end_time,
        "repeatable":repeatable,
        "formation":str(challenge.get("formation","f442")),
        "timesCompleted":0,
        # The pitch itself publishes FIFA's own Number of players row. RC71
        # proved this is true for a full XI as well as a bricked layout: our
        # standalone PLAYER_COUNT stayed grey at 11/11 and disabled Submit.
        # Count rows which scope Gold/Rare/IF/TOTS rules are still serialized
        # by native_eligibility_dto; only the duplicate total is suppressed.
        "elgReq":native_eligibility_dto(
            requirements,publish_standalone_player_count=False),
        "elgOperation":"AND",
        "awards":awards,
        "tutorial":_integer(challenge.get("tutorial",0)),
        # FIFA 19 recognizes three challenge types, resolved read-only from
        # the challenges parser at CardsDLL+0x2a8380: "OPEN_CHALLENGE" (0),
        # "BRICK_CHALLENGE" (2) and "CUSTOM_BRICK_CHALLENGE" (3).
        #
        # Both brick types declare locked pitch slots, and the layout has to
        # travel with them. FutLoadSetChallengesResponse has no slotMask
        # member, so a mask can never reach the client: it keeps a full
        # eleven-slot squad and contributes its own "Number of players in the
        # Squad: 11" rule. That is why "The Third Step" showed both that row
        # and the real "Players: Exactly 4" at the same time on 2026-09-03.
        #
        # "BRICK_CHALLENGE" was wrong, and that is why every attempt to lock a
        # slot failed even though the marker really is read.
        #
        # The string-to-enum mapping stores 2 for the 15-byte
        # "BRICK_CHALLENGE" at CardsDLL+0x2a8450 and 3 for the 22-byte
        # "CUSTOM_BRICK_CHALLENGE" at +0x2a84b6. The routine that parses a
        # slot's position string does compare it against "BRICK" at +0x248472
        # and stores position code 2 - but the code that actually marks the
        # slot, at +0x2484bc, is gated one instruction earlier at +0x2484b2 by
        #     cmp dword ptr [rax + 0x40], 3
        # on the challenge's own type enum. Sending 2 meant that branch was
        # never entered on any build we have shipped, so the "BRICK" strings
        # published in RC44 were read and then discarded.
        #
        # That branch reads a definition id from the challenge at +0x34,
        # searches the 0x570-stride array at +0x40 for the entry whose +0x34
        # matches, and writes 4 into that entry's +0x40. When the id is -1 the
        # search is skipped and the default entry is marked instead.
        # Back to BRICK_CHALLENGE on 2026-09-04. Sending CUSTOM_BRICK_CHALLENGE
        # did open the branch at +0x2484bc, but it also cost the screen its
        # "PARTIAL SQUAD CHALLENGE" subtitle, which the retail screenshots of
        # these three challenges show and which BRICK_CHALLENGE produces. So
        # enum 2 is the right type for a partial squad, and that branch
        # belongs to the separate CUSTOM_BRICK kind of challenge.
        #
        # What the branch does is also now understood and is not a lock: the
        # routine at +0x248420 turns a slot's position string into a position
        # *code* - "BRICK" (5 bytes) into 2 at +0x2484a3, "CUSTOM_BRICK" (12
        # bytes) into 29 at +0x248594 - and stores it as the item's position.
        # It never chooses a presentation, so the padlock is decided somewhere
        # else entirely.
        "type":("BRICK_CHALLENGE" if slot_mask and open_slots<11
                else "OPEN_CHALLENGE"),
        # Not verified: `maskDefId` is a real client key (553), and the field
        # the branch above reads behaves exactly like it, but no parser has
        # been shown to dispatch it. -1 selects the "mark this slot" path. An
        # unread member is skipped safely, so publishing it costs nothing.
        "maskDefId":-1,
        "challengeImageId":str(image_id),
        "elgDesc":descriptions,
    }


def set_dto(set_spec, statuses=None, priority=1):
    """Return one native-shaped, completion-aware SBC set summary."""
    status_by_challenge={int(key):value for key,value in
                         dict(statuses or {}).items()}
    def status_for(challenge_id):
        value=status_by_challenge.get(challenge_id,"NOT_STARTED")
        return str(value.get("status","NOT_STARTED") if
                   isinstance(value,dict) else value)
    def completions_for(challenge_id):
        value=status_by_challenge.get(challenge_id,{})
        return _integer(value.get("completionCount",0)) if isinstance(value,dict) else 0
    def completed_at_for(challenge_id):
        value=status_by_challenge.get(challenge_id,{})
        return _integer(value.get("completedAt",0)) if isinstance(value,dict) else 0
    challenges=list(set_spec.get("challenges", []) or [])
    completion_counts=[completions_for(_integer(
        challenge.get("challengeId",challenge.get("id"))))
        for challenge in challenges]
    completion_times=[completed_at_for(_integer(
        challenge.get("challengeId",challenge.get("id"))))
        for challenge in challenges]
    set_id=_integer(set_spec.get("setId",set_spec.get("id")))
    asset_id=_integer(set_spec.get("assetId",1),1)
    image_name=str(set_spec.get("tileImage","SBS_SET_%d.png" % asset_id))
    group_rewards=list(set_spec.get("rewards",[]) or [])
    # Some historical groups grant only per-challenge rewards and therefore
    # carry no separate set-level award.  The retail Group Rewards drawer must
    # still explain what completing the group yields.  Aggregate those sourced
    # challenge rewards for this preview only; submit_sbc reads the untouched
    # set specification, so none of them can be granted a second time.
    if not group_rewards:
        group_rewards=[reward for challenge in challenges
                       for reward in challenge.get("rewards",[]) or []]
    awards=[award for reward in group_rewards
            for award in sbc_group_preview_reward_dtos(reward)]
    repeatable=bool(set_spec.get("repeatable",False))
    challenge_statuses=[status_for(_integer(challenge.get(
        "challengeId",challenge.get("id")))).upper()
        for challenge in challenges]
    completed=sum(status in {"COMPLETED","CLAIMED"}
                  for status in challenge_statuses)
    if repeatable and challenge_statuses and all(
            status == "CLAIMED" for status in challenge_statuses):
        # CLAIMED is the cycle boundary, not merely completion history. Before
        # it, the refresh after submit must still expose the finished group so
        # CardsDLL can enter its completion/reward presentation.
        completed=0
    start_time=_integer(set_spec.get("startTime",0))
    raw_end=set_spec.get("endTime")
    end_time=_integer(raw_end,0) if raw_end not in (None,"") else 0
    not_expirable=bool(set_spec.get("notExpirable",end_time <= 0))
    times_completed=min(completion_counts) if completion_counts else 0
    return {
        "setId":set_id,"id":set_id,
        "name":str(set_spec.get("name","")),
        "description":str(set_spec.get("description","")),
        "category":str(set_spec.get("category","BASIC")).upper(),
        "priority":_integer(set_spec.get("priority",priority),priority),
        "assetId":asset_id,"setImageId":asset_id,
        "previewImageId":asset_id,"rewardPreviewImageId":asset_id,
        "tileImage":image_name,
        "tileImageUrl":("/fut/sbc/gen4/sets/images/"
                        "sbc_set_image_%d.png" % asset_id),
        "imageUrl":("/fut/sbc/gen4/sets/images/"
                    "sbc_set_image_%d.png" % asset_id),
        "challengesCount":len(challenges),
        "challengesCompletedCount":completed,
        "completionCount":times_completed,"timesCompleted":times_completed,
        "isCompleted":bool(challenges) and completed == len(challenges),
        "repeatable":repeatable,"isRepeatable":repeatable,
        "repeatabilityMode":"UNLIMITED" if repeatable else "NONE",
        "notExpirable":not_expirable,"startTime":start_time,
        "releaseTime":start_time,"endTime":end_time,
        "hidden":False,"tagged":0,"tutorial":bool(set_spec.get(
            "tutorial",str(set_spec.get("name",""))=="Let's Get Started")),
        "taggedByProduction":False,"taggedByUser":False,
        "isFavourite":False,"repeats":0,"repeatRefreshInterval":0,
        "timesCompletedInInterval":0,
        "lastCompletedTime":max(completion_times or [0]),
        "isFeatured":bool(set_spec.get("isFeatured",False)),
        "isSingleChallenge":len(challenges)==1,"refreshInterval":0,
        "awards":awards,
    }


def _runtime_pack_ids(catalogue_path):
    pack_path=os.path.join(os.path.dirname(os.path.abspath(catalogue_path)),
                           "pack_defs.json")
    try:
        with open(pack_path,encoding="utf-8") as handle:
            return {int(value) for value in json.load(handle)}
    except (OSError,TypeError,ValueError,json.JSONDecodeError):
        return set()


def _runtime_object_catalog(catalogue_path):
    object_path=os.path.join(os.path.dirname(os.path.abspath(catalogue_path)),
                             "object_catalog.json")
    try:
        with open(object_path,encoding="utf-8") as handle:
            payload=json.load(handle)
        return payload if isinstance(payload,dict) else {}
    except (OSError,TypeError,ValueError,json.JSONDecodeError):
        return {}


def _runtime_player_ids(catalogue_path):
    data_dir=os.path.dirname(os.path.abspath(catalogue_path))
    result=set()
    for name in ("card_versions.json","players_meta.json"):
        path=os.path.join(data_dir,name)
        try:
            with open(path,encoding="utf-8") as handle:
                payload=json.load(handle)
            if isinstance(payload,dict):
                result.update(_integer(value) for value in payload
                              if _integer(value)>0)
        except (OSError,TypeError,ValueError,json.JSONDecodeError):
            continue
    return result


def _validate_runtime_set(set_row,pack_ids,object_catalog,player_ids):
    if str(set_row.get("verificationStatus","")) not in RUNTIME_VERIFICATIONS:
        raise SbcCatalogueError("enabled SBC set is not historically verified")
    if set_row.get("unresolved"):
        raise SbcCatalogueError("enabled SBC set still has unresolved fields")
    if not set_row.get("sourceProvenance") or not set_row.get("sourceSetId"):
        raise SbcCatalogueError("enabled SBC set has no source provenance")
    def validate_attempt_contract(row,label):
        if not bool(row.get("repeatable",False)):
            return
        contract=row.get("attemptContract")
        if (not isinstance(contract,dict) or
                str(contract.get("mode","")) != "saved_squad_attempt" or
                str(contract.get("retryScope","")) != "operation_key"):
            raise SbcCatalogueError(
                "enabled repeatable SBC %s has no verified attempt contract" % label)
    validate_attempt_contract(set_row,"set")
    challenges=list(set_row.get("challenges",[]))
    if not challenges:
        raise SbcCatalogueError("enabled SBC set has no challenges")

    def validate_rewards(rewards):
        for reward in rewards or []:
            kind=str(reward.get("type","")).lower()
            value=_integer(reward.get("value",reward.get("packId",0)))
            if kind == "pack" and value not in pack_ids:
                raise SbcCatalogueError(
                    "enabled SBC reward references an unavailable local pack")
            if kind == "coin" and value <= 0:
                raise SbcCatalogueError("enabled SBC coin reward is invalid")
            if kind == "consumable":
                pool=reward.get("resourcePool",[])
                if not isinstance(pool,list) or not pool:
                    raise SbcCatalogueError(
                        "enabled SBC consumable reward has no source pool")
                for resource_id in pool:
                    definition=object_catalog.get(str(_integer(resource_id)))
                    if (not isinstance(definition,dict) or
                            definition.get("_type") != "consumable" or
                            not str(definition.get("ItemType","")).startswith(
                                "TrainingPlayerPos") or
                            _integer(definition.get("Rare",0)) != 1):
                        raise SbcCatalogueError(
                            "enabled SBC consumable pool is not a verified "
                            "rare position-modifier pool")
            if kind in {"player_pick","playerpick"}:
                if (_integer(reward.get("optionCount",0)) < 2 or
                        str(reward.get("quality","")).upper() not in
                        {"BRONZE","SILVER","GOLD"}):
                    raise SbcCatalogueError(
                        "enabled SBC Player Pick has no executable filter")
            if kind == "player":
                resource_id=_integer(
                    reward.get("resourceId",reward.get("value",0)))
                if resource_id <= 0:
                    raise SbcCatalogueError(
                        "enabled SBC player reward has no resource ID")
                if resource_id not in player_ids:
                    raise SbcCatalogueError(
                        "enabled SBC player reward references an unknown player")
            if kind == "club_item" and (
                    _integer(reward.get("teamId",0)) <= 0 or
                    _integer(reward.get("category",0)) not in {2,3}):
                raise SbcCatalogueError(
                    "enabled SBC club-item reward has no executable kit identity")
            if kind in {"item","object"}:
                resource_id=_integer(reward.get("resourceId",value))
                if not isinstance(object_catalog.get(str(resource_id)),dict):
                    raise SbcCatalogueError(
                        "enabled SBC item reward references an unknown object")
            if kind not in {"pack","coin","consumable","player_pick",
                            "playerpick","player","club_item","item","object"}:
                raise SbcCatalogueError(
                    "enabled SBC reward type has no atomic runtime grant")
    validate_rewards(set_row.get("rewards",[]))
    for challenge in challenges:
        if not bool(challenge.get("enabled",False)):
            raise SbcCatalogueError("enabled SBC set contains a disabled challenge")
        if str(challenge.get("verificationStatus","")) not in RUNTIME_VERIFICATIONS:
            raise SbcCatalogueError("enabled SBC challenge is not historically verified")
        if challenge.get("unresolved"):
            raise SbcCatalogueError("enabled SBC challenge still has unresolved fields")
        if not challenge.get("sourceProvenance") or not challenge.get("sourceChallengeId"):
            raise SbcCatalogueError("enabled SBC challenge has no source provenance")
        validate_attempt_contract(challenge,"challenge")
        if not str(challenge.get("formation","")).strip():
            raise SbcCatalogueError("enabled SBC challenge has no formation")
        slot_mask=list(challenge.get("slotMask",[]))
        if len(slot_mask) != 11 or any(
                str(value).upper() not in {"OPEN","BRICK"} for value in slot_mask):
            raise SbcCatalogueError("enabled SBC challenge has no verified 11-slot mask")
        requirements=list(challenge.get("requirements",[]))
        if not requirements:
            raise SbcCatalogueError("enabled SBC challenge has no typed requirements")
        count_rule=next((rule for rule in requirements
                         if str(rule.get("type","")).lower()=="player_count"),None)
        if count_rule is None or str(count_rule.get("operator","exact")).lower()!="exact":
            raise SbcCatalogueError("enabled SBC challenge has no exact player count")
        if _integer(count_rule.get("value",count_rule.get("count",0))) != sum(
                str(value).upper()=="OPEN" for value in slot_mask):
            raise SbcCatalogueError("SBC player count conflicts with its slot mask")
        validate_rewards(challenge.get("rewards",[]))


@dataclass(frozen=True)
class SbcCatalogue:
    path: str
    payload: dict
    digest: str

    @classmethod
    def load(cls, path):
        with open(path, "rb") as handle:
            raw = handle.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SbcCatalogueError("invalid SBC catalogue JSON: %s" % exc)
        if _integer(payload.get("schemaVersion")) != CATALOG_SCHEMA_VERSION:
            raise SbcCatalogueError("unsupported SBC catalogue schema")
        if not isinstance(payload.get("sets", []), list):
            raise SbcCatalogueError("SBC catalogue sets must be a list")
        seen_sets = set()
        seen_challenges = set()
        pack_ids=_runtime_pack_ids(path)
        object_catalog=_runtime_object_catalog(path)
        player_ids=_runtime_player_ids(path)
        for set_row in payload.get("sets", []):
            set_id = _integer(set_row.get("setId"))
            if not set_id or set_id in seen_sets:
                raise SbcCatalogueError("missing or duplicate SBC set ID")
            seen_sets.add(set_id)
            for challenge in set_row.get("challenges", []):
                challenge_id = _integer(challenge.get("challengeId"))
                if not challenge_id or challenge_id in seen_challenges:
                    raise SbcCatalogueError("missing or duplicate SBC challenge ID")
                if _integer(challenge.get("setId", set_id)) != set_id:
                    raise SbcCatalogueError("SBC challenge references the wrong set")
                seen_challenges.add(challenge_id)
            if bool(set_row.get("enabled",False)):
                _validate_runtime_set(
                    set_row,pack_ids,object_catalog,player_ids)
        return cls(os.path.abspath(path), payload,
                   hashlib.sha256(raw).hexdigest())

    def active_sets(self):
        """Return only completely verified, explicitly enabled runtime sets."""
        active = []
        for set_row in self.payload.get("sets", []):
            if not bool(set_row.get("enabled", False)):
                continue
            if str(set_row.get("verificationStatus", "")) not in RUNTIME_VERIFICATIONS:
                continue
            challenges = list(set_row.get("challenges", []))
            if not challenges or any(
                    not bool(row.get("enabled", False)) or
                    str(row.get("verificationStatus", "")) not in
                    RUNTIME_VERIFICATIONS
                    for row in challenges):
                continue
            active.append(set_row)
        return active

    def active_set(self, set_id):
        requested=_integer(set_id)
        return next((row for row in self.active_sets()
                     if _integer(row.get("setId")) == requested),None)

    def active_challenge(self, challenge_id):
        requested=_integer(challenge_id)
        for set_row in self.active_sets():
            for challenge in set_row.get("challenges",[]) or []:
                if _integer(challenge.get("challengeId")) == requested:
                    return set_row,challenge
        return None


DEFAULT_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "fut19_sbc_catalog.json")

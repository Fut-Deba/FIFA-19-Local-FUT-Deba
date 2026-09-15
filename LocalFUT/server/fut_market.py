"""Deterministic, source-backed FUT 19 transfer-market helpers.

This module deliberately contains no account or SQLite mutations. Catalogue
filtering, pricing, AI snapshots and deterministic timing live here; durable
economy transitions live in :mod:`fut_state`.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from collections import OrderedDict
import hashlib
import json
import time
import unicodedata
from typing import Any, Mapping


_AI_LISTING_CACHE: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
_AI_LISTING_CACHE_LIMIT = 4096
MARKET_EPOCH_SECONDS = 900
AI_LISTING_SNAPSHOT_VERSION = 1

# Economy rebalance requested for the local transfer market.  The cutoff is
# evaluated against the former deterministic fair value, so a card is raised
# once by 40% and never oscillates around the boundary on later calls.
MARKET_REVALUATION_CUTOFF = 4_000_000
MARKET_REVALUATION_MULTIPLIER = 1.40

_AI_SNAPSHOT_KEYS = (
    "tradeId", "itemId", "resourceId", "startingBid", "currentBid",
    "buyNowPrice", "expires", "absoluteIndex", "marketEpoch",
    "filterHash", "listingKind",
)

# Local-market policy. The project intentionally keeps SBC and POTM player
# versions searchable, even though many of them were untradeable on EA's live
# service. FUT Swap tokens/rewards and loan-only cards are never real market
# supply and must not be materialized as synthetic auctions.
_BLOCKED_MARKET_REVISION_TOKENS = ("swap", "loan")

# This first object-market pass is intentionally bounded. Every identity is
# present in both the packaged object catalogue and the default grant, so the
# market cannot manufacture a card definition that the rest of the server has
# never rendered or persisted.
_OBJECT_MARKET_RESOURCE_IDS = (
    5001001,  # Player contract
    5002004,  # Team fitness
    5002028,  # Healing
    5003002,  # Goalkeeper training
    5003022,  # Player training
    5003071,  # Position modifier
    5003095,  # Chemistry style
    6000001,  # Badge
    6200001,  # Stadium
    1000067,  # Manager
    2000064,  # Head coach
    3000091,  # Fitness coach
    9000025,  # Goalkeeper coach
    4000158,  # Physio
)
_OBJECT_MARKET_KITS = ((1, "kit_home"), (1, "kit_away"))

# Continuous curves keep adjacent overall ratings related without collapsing
# every player in one tier to the same value. Values between anchors are then
# adjusted by face stats, position, card revision and player prestige.
_NORMAL_PRICE_ANCHORS = (
    (47, 200), (60, 250), (64, 300), (70, 400), (74, 500),
    (75, 600), (80, 1_100), (83, 2_500), (84, 4_500),
    (85, 7_500), (86, 12_500), (87, 22_000), (88, 35_000),
    (89, 60_000), (90, 100_000), (92, 250_000), (94, 600_000),
    (99, 2_000_000),
)
_SPECIAL_PRICE_ANCHORS = (
    (47, 10_000), (60, 10_000), (70, 10_000), (75, 11_000),
    (80, 15_000), (84, 35_000), (87, 100_000), (90, 350_000),
    (93, 900_000), (95, 1_600_000), (97, 2_800_000),
    (99, 4_500_000),
)

_PLAYER_PRESTIGE = {
    "cristiano ronaldo": 1.48,
    "lionel messi": 1.46,
    "ronaldo": 1.48,
    "pele": 1.45,
    "diego maradona": 1.42,
    "ruud gullit": 1.40,
    "johan cruyff": 1.38,
    "ronaldinho": 1.38,
    "eusebio": 1.37,
    "neymar jr": 1.37,
    "kylian mbappe lottin": 1.34,
    "paolo maldini": 1.30,
    "patrick vieira": 1.29,
    "thierry henry": 1.28,
    "kevin de bruyne": 1.25,
    "mohamed salah": 1.25,
    "luka modric": 1.24,
    "eden hazard": 1.23,
    "sergio ramos": 1.22,
    "luis suarez": 1.22,
    "virgil van dijk": 1.22,
    "antoine griezmann": 1.20,
    "paul pogba": 1.20,
}
_PLAYER_PRESTIGE_BY_ASSET = {
    20801: 1.48,    # Cristiano Ronaldo
    158023: 1.46,   # Lionel Messi
    37576: 1.48,    # Ronaldo Nazario
    190042: 1.42,   # Diego Maradona
    28130: 1.38,    # Ronaldinho
    190045: 1.38,   # Johan Cruyff
    214100: 1.40,   # Ruud Gullit
    190871: 1.37,   # Neymar Jr
    231747: 1.34,   # Kylian Mbappe
    192985: 1.25,   # Kevin De Bruyne
    183277: 1.23,   # Eden Hazard
    177003: 1.24,   # Luka Modric
    209331: 1.25,   # Mohamed Salah
}

try:
    from fut_catalog import (base_asset_id, card_revision, card_version_rows,
                             native_player_fields, player_wire_resource_id)
    from fut_objects import build_grant_item, default_grant, object_catalog
except ImportError:  # pragma: no cover - package import in tools/tests
    from .fut_catalog import (base_asset_id, card_revision, card_version_rows,
                              native_player_fields, player_wire_resource_id)
    from .fut_objects import build_grant_item, default_grant, object_catalog


@lru_cache(maxsize=1)
def _pim_market_wire_to_exact() -> dict[int, int]:
    return {
        int(player_wire_resource_id(row["resourceId"])):int(row["resourceId"])
        for row in card_version_rows(revisions="Prime Icon Moments")
    }


def market_exact_resource_id(resource_id: Any) -> int:
    """Resolve a client-facing PIM wire ID to its sourced card definition."""
    value=max(0,int(resource_id or 0))
    return _pim_market_wire_to_exact().get(value,value)


def _scalar(query: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in query.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, (list, tuple)):
            value = value[0] if value else default
        if value not in (None, ""):
            return value
    return default


def _integer(query: Mapping[str, Any], *names: str, default: int = 0) -> int:
    try:
        return int(_scalar(query, *names, default=default) or default)
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class PlayerSearch:
    item_type: str = "player"
    resource_id: int = 0
    asset_id: int = 0
    nation: int = 0
    league: int = 0
    club: int = 0
    position: str = ""
    quality: str = ""
    rare: int = -1
    min_bid: int = 0
    max_bid: int = 0
    min_buy_now: int = 0
    max_buy_now: int = 0
    start: int = 0
    num: int = 21

    def stable_filters(self) -> dict[str, Any]:
        values = dict(self.__dict__)
        values.pop("start", None)
        values.pop("num", None)
        return values


def normalize_player_search(query: Mapping[str, Any]) -> PlayerSearch:
    """Normalize native and captured compatibility aliases into one query."""
    quality = str(_scalar(query, "lev", "level", "quality", default="")).strip().lower()
    position = str(_scalar(query, "pos", "position", default="")).strip().upper()
    rare_raw = str(_scalar(query, "rare", "rarity", default="")).strip().lower()
    # Retail FIFA 19 sends ``rare=SP`` for the Transfer Market's Special
    # quality tile.  Treating it as an unknown rarity silently returned the
    # same Normal-card page as an unfiltered search.
    if rare_raw in {"sp", "special"}:
        quality = "special"
        rare = 1
    elif rare_raw in {"1", "true", "rare", "yes"}:
        rare = 1
    elif rare_raw in {"0", "false", "common", "no"}:
        rare = 0
    else:
        rare = -1
    # `maskedDefId` is the identity selected by the player-name picker.  FIFA
    # sends the base definition there even when the Special quality tile is
    # active, so it must match the player's base asset across every card
    # revision.  `definitionId`/`defId`/`resourceId`, on the other hand, are
    # exact-card selectors used by Compare Price and compatibility routes.
    exact_resource_id = market_exact_resource_id(_integer(
        query, "definitionid", "defid", "resourceid"))
    masked_asset_id = max(0, _integer(query, "maskeddefid"))
    explicit_asset_id = max(0, _integer(query, "assetid"))
    asset_id = explicit_asset_id or (base_asset_id(masked_asset_id)
                                     if masked_asset_id else 0)
    return PlayerSearch(
        item_type=str(_scalar(query,"type","itemtype",default="player")
                      or "player").strip().lower(),
        resource_id=exact_resource_id,
        asset_id=asset_id,
        nation=max(0, _integer(query, "nation", "nat")),
        league=max(0, _integer(query, "league", "leag")),
        club=max(0, _integer(query, "club", "team")),
        position=position,
        quality=quality,
        rare=rare,
        min_bid=max(0, _integer(query, "minbid", "micr", "minb")),
        max_bid=max(0, _integer(query, "maxbid", "macr", "maxb")),
        min_buy_now=max(0, _integer(query, "minbuynow", "minbuynowprice", "minbuy", "micn")),
        max_buy_now=max(0, _integer(query, "maxbuynow", "maxbuynowprice", "maxbuy", "macn")),
        start=max(0, _integer(query, "start", "offset")),
        num=min(50, max(1, _integer(query, "num", "count", default=21))),
    )


def _object_token(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold()
                   if character.isalnum())


@dataclass(frozen=True)
class ObjectSearch:
    item_type: str = ""
    category: str = ""
    resource_id: int = 0
    nation: int = 0
    league: int = 0
    club: int = 0
    quality: str = ""
    rare: int = -1
    min_bid: int = 0
    max_bid: int = 0
    min_buy_now: int = 0
    max_buy_now: int = 0
    start: int = 0
    num: int = 21

    def stable_filters(self) -> dict[str, Any]:
        values = dict(self.__dict__)
        values.pop("start", None)
        values.pop("num", None)
        return values


_DIRECT_OBJECT_TYPE_ALIASES = {
    "contract": ("development", "contract"),
    "fitness": ("development", "fitness"),
    "healing": ("development", "healing"),
    "health": ("development", "healing"),
    "badge": ("clubinfo", "badge"),
    "kit": ("clubinfo", "kit"),
    "stadium": ("clubinfo", "stadium"),
    "manager": ("staff", "manager"),
    "headcoach": ("staff", "headcoach"),
    "fitnesscoach": ("staff", "fitnesscoach"),
    "gkcoach": ("staff", "gkcoach"),
    "physio": ("staff", "physio"),
}
_OBJECT_CATEGORY_ALIASES = {
    "contracts": "contract",
    "health": "healing",
    "goalkeepertraining": "gktraining",
    "positioning": "position",
    "chemistrystyle": "playstyle",
    "kits": "kit",
    "badges": "badge",
    "stadiums": "stadium",
}


def normalize_object_search(query: Mapping[str, Any]) -> ObjectSearch:
    """Normalize the observed non-player market query families."""
    item_type = _object_token(_scalar(query, "type", "itemtype"))
    category = _object_token(_scalar(query, "cat", "category"))
    if item_type in _DIRECT_OBJECT_TYPE_ALIASES:
        item_type, implied_category = _DIRECT_OBJECT_TYPE_ALIASES[item_type]
        category = category or implied_category
    elif item_type in {"clubitem", "clubitems"}:
        item_type = "clubinfo"
    elif item_type in {"consumable", "consumables"}:
        item_type = "consumable"
    category = _OBJECT_CATEGORY_ALIASES.get(category, category)

    quality = str(_scalar(
        query, "lev", "level", "quality", default="")).strip().lower()
    rare_raw = _object_token(_scalar(query, "rare", "rarity"))
    if rare_raw in {"sp", "special"}:
        quality, rare = "special", 1
    elif rare_raw in {"1", "true", "rare", "yes"}:
        rare = 1
    elif rare_raw in {"0", "false", "common", "no"}:
        rare = 0
    else:
        rare = -1
    return ObjectSearch(
        item_type=item_type,
        category=category,
        resource_id=max(0, _integer(
            query, "definitionid", "defid", "resourceid", "maskeddefid")),
        nation=max(0, _integer(query, "nation", "nat")),
        league=max(0, _integer(query, "league", "leag")),
        club=max(0, _integer(query, "club", "team")),
        quality=quality,
        rare=rare,
        min_bid=max(0, _integer(query, "minbid", "micr", "minb")),
        max_bid=max(0, _integer(query, "maxbid", "macr", "maxb")),
        min_buy_now=max(0, _integer(
            query, "minbuynow", "minbuynowprice", "minbuy", "micn")),
        max_buy_now=max(0, _integer(
            query, "maxbuynow", "maxbuynowprice", "maxbuy", "macn")),
        start=max(0, _integer(query, "start", "offset")),
        num=min(50, max(1, _integer(query, "num", "count", default=21))),
    )


def snap_market_price(value: Any) -> int:
    """Snap a coin value to the FUT auction increment bands."""
    value = max(150, int(value or 0))
    if value <= 1_000:
        step = 50
    elif value <= 10_000:
        step = 100
    elif value <= 50_000:
        step = 250
    elif value <= 100_000:
        step = 500
    else:
        step = 1_000
    return max(150, int(round(float(value) / step)) * step)


def next_bid_price(value: Any) -> int:
    """Return the next legal FUT bid above ``value``."""
    current=max(0,int(value or 0))
    if current < 1_000:
        step=50
    elif current < 10_000:
        step=100
    elif current < 50_000:
        step=250
    elif current < 100_000:
        step=500
    else:
        step=1_000
    return snap_market_price(current+step)


def _interpolated_price(rating: int,
                        anchors: tuple[tuple[int, int], ...]) -> float:
    value=max(0,int(rating or 0))
    if value <= anchors[0][0]:
        return float(anchors[0][1])
    for (left_rating,left_price),(right_rating,right_price) in zip(
            anchors,anchors[1:]):
        if value <= right_rating:
            distance=max(1,right_rating-left_rating)
            ratio=float(value-left_rating)/float(distance)
            return left_price+(right_price-left_price)*ratio
    return float(anchors[-1][1])


def _normalized_player_name(value: Any) -> str:
    decomposed=unicodedata.normalize("NFKD",str(value or ""))
    ascii_name="".join(character for character in decomposed
                       if not unicodedata.combining(character))
    return " ".join("".join(character if character.isalnum() else " "
                            for character in ascii_name.casefold()).split())


def _player_prestige(source: Mapping[str, Any]) -> float:
    name=_normalized_player_name(source.get("name") or
                                 source.get("commonName") or
                                 source.get("lastName"))
    try: asset_id=int(source.get("assetId",0) or 0)
    except (TypeError,ValueError): asset_id=0
    return max(_PLAYER_PRESTIGE.get(name,1.0),
               _PLAYER_PRESTIGE_BY_ASSET.get(asset_id,1.0))


def _attribute_values(source: Mapping[str, Any], rating: int) -> list[int]:
    result=[max(0,int(rating or 0))]*6
    entries=source.get("attributeList") or []
    if not isinstance(entries,list):
        return result
    observed=False
    for entry in entries:
        try:
            index=int(entry.get("index",-1)); value=int(entry.get("value",0) or 0)
        except (AttributeError,TypeError,ValueError):
            continue
        if 0 <= index < len(result):
            result[index]=max(0,min(99,value)); observed=True
    return result if observed else [max(0,int(rating or 0))]*6


def _performance_multiplier(source: Mapping[str, Any], rating: int) -> float:
    values=_attribute_values(source,rating)
    position=str(source.get("preferredPosition","") or "").upper()
    if position == "GK":
        weights=(0.18,0.18,0.14,0.22,0.10,0.18)
    elif position in {"CB","LB","LWB","RB","RWB","CDM"}:
        weights=(0.12,0.04,0.12,0.12,0.34,0.26)
    elif position in {"CM","CAM","LM","RM"}:
        weights=(0.15,0.15,0.25,0.20,0.12,0.13)
    else:
        weights=(0.20,0.25,0.15,0.25,0.02,0.13)
    weighted=sum(value*weight for value,weight in zip(values,weights))
    top_two=sum(sorted(values,reverse=True)[:2])/2.0
    multiplier=(1.0+(weighted-rating)/70.0+
                max(-0.05,(top_two-rating)/160.0))
    try: skill_moves=int(source.get("skillmoves",3) or 3)
    except (TypeError,ValueError): skill_moves=3
    try: weak_foot=int(source.get("weakfootabilitytypecode",3) or 3)
    except (TypeError,ValueError): weak_foot=3
    multiplier+=(max(1,min(5,skill_moves))-3)*0.025
    multiplier+=(max(1,min(5,weak_foot))-3)*0.015
    return max(0.78,min(1.35,multiplier))


def _revision_multiplier(revision: Any) -> float:
    value=str(revision or "Normal").casefold()
    if value == "normal": return 1.0
    if "prime icon moments" in value: return 1.45
    if "toty" in value and "nominee" not in value: return 1.50
    if "tots" in value: return 1.35
    if value == "icon": return 1.35
    if "fut birthday" in value: return 1.28
    if "future stars" in value: return 1.23
    if "headliners" in value: return 1.20
    if "award winner" in value: return 1.20
    if "carniball" in value: return 1.16
    if "potm" in value: return 1.16
    if "live" in value: return 1.14
    if "totgs" in value or "tott" in value: return 1.12
    if "halloween" in value: return 1.12
    if "toty nominee" in value: return 1.12
    if "sbc" in value: return 1.10
    if "otw" in value: return 1.08
    if value == "tif": return 1.08
    if value == "sif": return 1.03
    if value == "if": return 0.95
    return 1.05


def _base_estimated_market_value(source: Mapping[str, Any]) -> int:
    """Return the pre-rebalance deterministic value for one card."""
    rid=int(source.get("resourceId",source.get("definitionId",
            source.get("assetId",0))) or 0)
    rating=max(0,int(source.get("rating",0) or 0))
    revision=str(source.get("cardRevision") or source.get("revision") or
                 source.get("rev") or card_revision(rid))
    anchors=(_NORMAL_PRICE_ANCHORS if revision == "Normal"
             else _SPECIAL_PRICE_ANCHORS)
    value=_interpolated_price(rating,anchors)
    if revision != "Normal": value*=_revision_multiplier(revision)
    value*=_performance_multiplier(source,rating)
    value*=_player_prestige(source)
    return snap_market_price(value)


def _market_revaluation_multiplier(source: Mapping[str, Any]) -> float:
    """Raise cards formerly valued below four million exactly once."""
    return (MARKET_REVALUATION_MULTIPLIER
            if _base_estimated_market_value(source) <
               MARKET_REVALUATION_CUTOFF else 1.0)


def _estimated_market_value(source: Mapping[str, Any]) -> int:
    base=_base_estimated_market_value(source)
    return snap_market_price(base*_market_revaluation_multiplier(source))


def market_card_is_eligible(source: Mapping[str, Any]) -> bool:
    """Return whether one sourced version may appear as synthetic supply."""
    revision=str(source.get("cardRevision") or source.get("revision") or "")
    lowered=revision.casefold()
    return not any(token in lowered for token in
                   _BLOCKED_MARKET_REVISION_TOKENS)


def _is_object_market_source(source: Any) -> bool:
    if not isinstance(source, Mapping):
        return False
    inventory_type = _object_token(source.get("inventoryType"))
    catalog_type = _object_token(source.get(
        "catalogType", source.get("_type")))
    return (inventory_type not in {"", "player", "players"} or
            catalog_type in {
                "manager", "headcoach", "fitnesscoach", "physio",
                "gkcoach", "badge", "stadium", "consumable",
                "kithome", "kitaway",
            } or bool(source.get("definitionItemType")))


def market_object_price_limits(source: Mapping[str, Any]) -> tuple[int, int]:
    """Return conservative local price limits for a native object card."""
    if not isinstance(source, Mapping):
        raise TypeError("object market pricing requires a mapping")
    explicit_min = max(0, int(source.get("marketDataMinPrice", 0) or 0))
    explicit_max = max(0, int(source.get("marketDataMaxPrice", 0) or 0))
    if explicit_min and explicit_max >= explicit_min:
        return snap_market_price(explicit_min), snap_market_price(explicit_max)

    rating = max(0, int(source.get(
        "rating", source.get("Rating", source.get("value", 0))) or 0))
    rare = bool(int(source.get(
        "rareflag", source.get("Rare", source.get("weightrare", 0))) or 0))
    inventory_type = _object_token(source.get("inventoryType"))
    catalog_type = _object_token(source.get(
        "catalogType", source.get("_type")))
    if rating >= 75:
        minimum, maximum = (600 if rare else 300), 20_000
    elif rating >= 65:
        minimum, maximum = (300 if rare else 200), 10_000
    else:
        minimum, maximum = (200 if rare else 150), 5_000
    if inventory_type in {"manager", "staff"} or catalog_type in {
            "manager", "headcoach", "fitnesscoach", "gkcoach", "physio"}:
        maximum = max(maximum, 30_000)
    elif inventory_type in {"kit", "badge", "stadium"} or catalog_type in {
            "kithome", "kitaway", "badge", "stadium"}:
        maximum = max(maximum, 15_000)
    return snap_market_price(minimum), snap_market_price(maximum)


def _object_fair_market_value(source: Mapping[str, Any]) -> int:
    minimum, maximum = market_object_price_limits(source)
    rating = max(0, int(source.get(
        "rating", source.get("Rating", source.get("value", 0))) or 0))
    rare = bool(int(source.get(
        "rareflag", source.get("Rare", source.get("weightrare", 0))) or 0))
    if rating >= 75:
        baseline = 900
    elif rating >= 65:
        baseline = 450
    else:
        baseline = 250
    if rare:
        baseline *= 1.5
    resource_id = int(source.get(
        "resourceId", source.get("definitionId", source.get("_resourceId", 0)))
        or 0)
    catalog_type = _object_token(source.get(
        "catalogType", source.get("_type")))
    digest = hashlib.sha256(
        ("fut19-object-value:%s:%d" %
         (catalog_type, resource_id)).encode("ascii")).digest()
    baseline *= 0.90 + (int.from_bytes(digest[:2], "big") % 2100) / 10_000.0
    return min(maximum, max(minimum, snap_market_price(baseline)))


def market_price_limits(source: Any) -> tuple[int, int]:
    """Return item-specific limits for the local market."""
    if _is_object_market_source(source):
        return market_object_price_limits(source)
    if isinstance(source, Mapping):
        rid = int(source.get("resourceId", source.get("definitionId",
                  source.get("assetId", 0))) or 0)
        raw = native_player_fields(rid, source)
    else:
        rid = int(source or 0)
        raw = native_player_fields(rid)
    rating = int(raw.get("rating", 0) or 0)
    revision = str(raw.get("cardRevision") or card_revision(rid))
    source_minimum=max(150,int(raw.get("marketDataMinPrice",150) or 150))
    # Build the old limits first and then revalue the complete range.  Scaling
    # only the estimate would leave low-card floor thresholds (5k/10k/30k)
    # unchanged even though their actual purchase price had risen.
    estimated=_base_estimated_market_value(raw)
    if revision != "Normal":
        minimum=max(source_minimum,10_000,
                    snap_market_price(estimated*0.50))
        maximum=min(15_000_000,max(30_000,
                    snap_market_price(estimated*1.80)))
    elif rating >= 87:
        minimum=max(source_minimum,snap_market_price(estimated*0.25))
        maximum=max(75_000,snap_market_price(estimated*2.50))
    elif rating >= 84:
        minimum=max(source_minimum,snap_market_price(estimated*0.20))
        maximum=max(30_000,snap_market_price(estimated*3.00))
    elif rating >= 80:
        minimum=source_minimum
        maximum=max(10_000,snap_market_price(estimated*3.00))
    elif rating >= 75:
        minimum=source_minimum
        maximum=max(5_000,snap_market_price(estimated*3.00))
    else:
        minimum=source_minimum
        maximum=max(5_000,snap_market_price(estimated*3.00))
    multiplier=_market_revaluation_multiplier(raw)
    if multiplier != 1.0:
        minimum=snap_market_price(minimum*multiplier)
        maximum=snap_market_price(maximum*multiplier)
    return minimum, max(minimum, maximum)


def fair_market_value(source: dict[str, Any]) -> int:
    """Return a differentiated local value, not historical historical_reference pricing."""
    if _is_object_market_source(source):
        return _object_fair_market_value(source)
    minimum, maximum = market_price_limits(source)
    value=_estimated_market_value(source)
    return min(maximum, max(minimum, snap_market_price(value)))


def _quality_matches(search: PlayerSearch, fields: dict[str, Any]) -> bool:
    quality = search.quality
    if not quality or quality in {"any", "all"}:
        return True
    revision = str(fields.get("cardRevision") or "Normal")
    rating = int(fields.get("rating", 0) or 0)
    if quality in {"special", "spec"}:
        return revision != "Normal"
    if revision != "Normal":
        return False
    if quality in {"gold", "3"}:
        return rating >= 75
    if quality in {"silver", "2"}:
        return 65 <= rating <= 74
    if quality in {"bronze", "1"}:
        return rating <= 64
    return True


def _price_for_card(fields: dict[str, Any]) -> tuple[int, int]:
    rid = int(fields.get("resourceId", fields.get("assetId", 0)) or 0)
    digest = hashlib.sha256(("fut19-price:" + str(rid)).encode("ascii")).digest()
    # Stable range 0.88 .. 1.1299, independent of search filters/pagination.
    multiplier = 0.88 + (int.from_bytes(digest[:2], "big") % 2500) / 10_000.0
    minimum, maximum = market_price_limits(fields)
    buy_now = min(maximum, max(minimum,
                  snap_market_price(fair_market_value(fields) * multiplier)))
    starting = min(buy_now, max(minimum, snap_market_price(buy_now * 0.9)))
    return starting, buy_now


def market_epoch(timestamp: Any = None) -> int:
    """Return the bounded local supply epoch used for deterministic refresh."""
    value=time.time() if timestamp is None else float(timestamp)
    return max(0,int(value)//MARKET_EPOCH_SECONDS)


def _listing_filter_hash(kind: str, resource_id: Any = 0) -> str:
    """Return the stable scope hash stored with every actionable snapshot."""
    descriptor={"kind":str(kind),"resourceId":max(0,int(resource_id or 0))}
    return hashlib.sha256(json.dumps(
        descriptor,sort_keys=True,separators=(",", ":")
    ).encode("ascii")).hexdigest()


def _listing_signature(snapshot: Mapping[str, Any]) -> str:
    """Fingerprint all economy-relevant fields in a local AI listing.

    The HTTP client only sends a trade ID, but the actionable row travels
    through a process cache before SQLite materialisation.  Sealing that row
    makes accidental mutation, stale test fixtures and future routes that
    accept client-shaped auction objects fail closed instead of changing the
    price or player granted by the deterministic market.
    """
    payload={key:snapshot.get(key) for key in _AI_SNAPSHOT_KEYS}
    payload["fields"]=dict(snapshot.get("fields") or {})
    encoded=json.dumps(payload,sort_keys=True,separators=(",", ":"),
                       ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(b"localfut19-ai-listing-v1\0"+encoded).hexdigest()


def seal_player_listing(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached, versioned and integrity-sealed listing snapshot."""
    row=json.loads(json.dumps(dict(snapshot)))
    row["snapshotVersion"]=AI_LISTING_SNAPSHOT_VERSION
    row["snapshotSignature"]=_listing_signature(row)
    return row


def verify_player_listing_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Validate a generated listing before it can become a durable auction."""
    if not isinstance(snapshot,Mapping):
        return False
    try:
        if int(snapshot.get("snapshotVersion",0) or 0) != \
                AI_LISTING_SNAPSHOT_VERSION:
            return False
        if int(snapshot.get("tradeId",0) or 0) <= 0:
            return False
        if int(snapshot.get("resourceId",0) or 0) <= 0:
            return False
        if int(snapshot.get("itemId",0) or 0) != int(snapshot["tradeId"]):
            return False
        if int(snapshot.get("startingBid",0) or 0) <= 0 or \
                int(snapshot.get("buyNowPrice",0) or 0) < \
                int(snapshot.get("startingBid",0) or 0):
            return False
        signature=str(snapshot.get("snapshotSignature","") or "")
        return bool(signature) and signature == _listing_signature(snapshot)
    except (KeyError,TypeError,ValueError):
        return False


def exact_player_listing(resource_id: Any, index: Any,
                         timestamp: Any = None) -> dict[str, Any]:
    """Build one actionable Compare Price row for an exact sourced card."""
    rid=market_exact_resource_id(resource_id)
    absolute_index=max(0,int(index))
    fields=native_player_fields(rid)
    minimum,maximum=market_price_limits(fields)
    baseline=fair_market_value(fields)
    multiplier=0.88+(absolute_index%12)*0.035
    buy_now=min(maximum,max(minimum,snap_market_price(baseline*multiplier)))
    starting=min(buy_now,max(minimum,snap_market_price(buy_now*0.9)))
    epoch=market_epoch(timestamp)
    identity=hashlib.sha256(
        ("fut19-exact-trade:%d:%d:%d" %
         (epoch,rid,absolute_index)).encode("ascii")
    ).digest()
    trade_id=900_000_000_000+int.from_bytes(identity[:6],"big")%90_000_000_000
    snapshot={
        "tradeId":trade_id,"itemId":trade_id,"resourceId":rid,
        "fields":fields,"startingBid":starting,"currentBid":0,
        "buyNowPrice":buy_now,
        "expires":300+(absolute_index%12)*275,
        "absoluteIndex":absolute_index,"marketEpoch":epoch,
        "filterHash":_listing_filter_hash("exact",rid),
        "listingKind":"exact",
    }
    return remember_player_listing(snapshot)


@lru_cache(maxsize=1)
def _market_cards() -> tuple[tuple[dict[str, Any], int, int], ...]:
    """Materialize immutable catalogue projections once per server process."""
    cards = []
    seen_resources: set[int] = set()
    for row in card_version_rows():
        fields = native_player_fields(int(row["resourceId"]), row)
        rid = int(fields.get("resourceId", 0) or 0)
        if rid <= 0 or rid in seen_resources:
            continue
        if not market_card_is_eligible(fields):
            continue
        seen_resources.add(rid)
        starting, buy_now = _price_for_card(fields)
        cards.append((fields, starting, buy_now))
    return tuple(cards)


def _object_market_family(fields: Mapping[str, Any]) -> tuple[str, str]:
    catalog_type = _object_token(fields.get("catalogType"))
    definition_type = str(fields.get("definitionItemType", "") or "")
    if catalog_type == "consumable":
        if definition_type == "ContractPlayer":
            return "development", "contract"
        if definition_type in {"FitnessPlayer", "FitnessTeam"}:
            return "development", "fitness"
        if definition_type.startswith("Health"):
            return "development", "healing"
        if definition_type.startswith("TrainingGk"):
            return "training", "gktraining"
        if definition_type.startswith("TrainingPlayerPos"):
            return "training", "position"
        if definition_type.startswith("TrainingPlayer"):
            return "training", "playertraining"
        if definition_type.startswith("TrainingPlaystyle"):
            return "training", "playstyle"
        return "consumable", ""
    if catalog_type in {"kithome", "kitaway"}:
        return "clubinfo", "kit"
    if catalog_type in {"badge", "stadium"}:
        return "clubinfo", catalog_type
    if catalog_type in {
            "manager", "headcoach", "fitnesscoach", "gkcoach", "physio"}:
        return "staff", catalog_type
    return "", ""


def _price_for_object(fields: Mapping[str, Any]) -> tuple[int, int]:
    resource_id = int(fields.get("resourceId", 0) or 0)
    catalog_type = _object_token(fields.get("catalogType"))
    digest = hashlib.sha256(
        ("fut19-object-price:%s:%d" %
         (catalog_type, resource_id)).encode("ascii")).digest()
    minimum, maximum = market_object_price_limits(fields)
    multiplier = 0.90 + (int.from_bytes(digest[:2], "big") % 2200) / 10_000.0
    buy_now = min(maximum, max(minimum, snap_market_price(
        _object_fair_market_value(fields) * multiplier)))
    starting = min(buy_now, max(minimum, snap_market_price(buy_now * 0.9)))
    return starting, buy_now


@lru_cache(maxsize=1)
def _market_objects() -> tuple[tuple[dict[str, Any], int, int], ...]:
    """Build the small allow-listed object supply from packaged sources."""
    catalog = object_catalog()
    grant_rows = list(default_grant()["items"])
    by_resource = {
        int(spec.get("resourceId", 0) or 0): spec
        for spec in grant_rows if int(spec.get("resourceId", 0) or 0) > 0
    }
    selected = []
    for resource_id in _OBJECT_MARKET_RESOURCE_IDS:
        spec = by_resource.get(resource_id)
        if spec is None or str(resource_id) not in catalog:
            raise ValueError(
                "object market identity %d is absent from packaged sources" %
                resource_id)
        selected.append(spec)
    for team_id, catalog_type in _OBJECT_MARKET_KITS:
        spec = next((row for row in grant_rows
                     if _object_token(row.get("type")) == catalog_type.replace("_", "")
                     and int(row.get("teamId", 0) or 0) == team_id), None)
        if spec is None:
            raise ValueError(
                "object market kit %s/%d is absent from default_grant" %
                (catalog_type, team_id))
        selected.append(spec)

    objects = []
    for spec in selected:
        fields = build_grant_item(spec, catalog)
        fields.update({
            "id": 0,
            "itemId": 0,
            "timestamp": 0,
            "pile": 5,
            "untradeable": False,
            "tradeable": True,
            "discardValue": 20,
        })
        minimum, maximum = market_object_price_limits(fields)
        fields["marketDataMinPrice"] = minimum
        fields["marketDataMaxPrice"] = maximum
        family, category = _object_market_family(fields)
        if not family or not category:
            raise ValueError("object market identity has no native search family")
        starting, buy_now = _price_for_object(fields)
        objects.append((fields, starting, buy_now))
    return tuple(objects)


def _object_quality_matches(search: ObjectSearch,
                            fields: Mapping[str, Any]) -> bool:
    quality = search.quality
    if not quality or quality in {"any", "all"}:
        return True
    rating = int(fields.get("rating", 0) or 0)
    if quality in {"special", "spec"}:
        return False
    if quality in {"gold", "3"}:
        return rating >= 75
    if quality in {"silver", "2"}:
        return 65 <= rating <= 74
    if quality in {"bronze", "1"}:
        return rating <= 64
    return True


def _object_matches(search: ObjectSearch, fields: Mapping[str, Any],
                    starting: int, buy_now: int) -> bool:
    family, category = _object_market_family(fields)
    if search.item_type and search.item_type not in {family, "consumable"}:
        return False
    if search.item_type == "consumable" and family not in {
            "development", "training", "consumable"}:
        return False
    if search.category and search.category != category:
        return False
    if search.resource_id and int(fields.get("resourceId", 0) or 0) != \
            search.resource_id:
        return False
    if search.nation and int(fields.get(
            "nation", fields.get("nationId", 0)) or 0) != search.nation:
        return False
    if search.league and int(fields.get("leagueId", 0) or 0) != search.league:
        return False
    if search.club and int(fields.get(
            "teamid", fields.get("teamId", 0)) or 0) != search.club:
        return False
    if not _object_quality_matches(search, fields):
        return False
    if search.rare >= 0 and int(bool(int(fields.get("rareflag", 0) or 0))) != \
            search.rare:
        return False
    if search.min_bid and starting < search.min_bid:
        return False
    if search.max_bid and starting > search.max_bid:
        return False
    if search.min_buy_now and buy_now < search.min_buy_now:
        return False
    if search.max_buy_now and buy_now > search.max_buy_now:
        return False
    return True


def _matches(search: PlayerSearch, fields: dict[str, Any],
             starting: int, buy_now: int) -> bool:
    rid = int(fields.get("resourceId", 0) or 0)
    asset = int(fields.get("assetId", base_asset_id(rid)) or 0)
    if search.resource_id and rid != search.resource_id:
        return False
    if search.asset_id and asset != search.asset_id:
        return False
    if search.nation and int(fields.get("nation", 0) or 0) != search.nation:
        return False
    if search.league and int(fields.get("leagueId", 0) or 0) != search.league:
        return False
    if search.club and int(fields.get("teamid", fields.get("teamId", 0)) or 0) != search.club:
        return False
    if search.position and str(fields.get("preferredPosition", "")).upper() != search.position:
        return False
    if not _quality_matches(search, fields):
        return False
    if search.rare >= 0 and int(bool(int(fields.get("rareflag", 0) or 0))) != search.rare:
        return False
    if search.min_bid and starting < search.min_bid:
        return False
    if search.max_bid and starting > search.max_bid:
        return False
    if search.min_buy_now and buy_now < search.min_buy_now:
        return False
    if search.max_buy_now and buy_now > search.max_buy_now:
        return False
    return True


def search_player_listings(query: Mapping[str, Any], market_epoch_value: Any = None,
                           unavailable_trade_ids: Any = None) -> dict[str, Any]:
    """Generate a deterministic page of read-only player listing snapshots."""
    search = normalize_player_search(query)
    stable_json = json.dumps(search.stable_filters(), sort_keys=True,
                             separators=(",", ":"))
    filter_hash = hashlib.sha256(stable_json.encode("utf-8")).hexdigest()
    epoch=(market_epoch() if market_epoch_value is None
           else max(0,int(market_epoch_value)))
    supply_hash=hashlib.sha256(
        (filter_hash+":"+str(epoch)).encode("ascii")).hexdigest()
    if search.item_type not in {"player","players"}:
        return {"listings":[],"start":search.start,"num":search.num,
                "totalResults":0,"endOfList":True,"filterHash":filter_hash,
                "marketEpoch":epoch}
    candidates: list[tuple[bytes, dict[str, Any], int, int]] = []
    for fields, starting, buy_now in _market_cards():
        rid = int(fields["resourceId"])
        if not _matches(search, fields, starting, buy_now):
            continue
        order = hashlib.sha256((supply_hash + ":" + str(rid)).encode("ascii")).digest()
        candidates.append((order, fields, starting, buy_now))
    candidates.sort(key=lambda entry: (entry[0], int(entry[1]["resourceId"])))

    unavailable={int(value) for value in (unavailable_trade_ids or ())}
    available=[]
    used_trade_ids: set[int] = set()
    for absolute_index,(_,fields,starting,buy_now) in enumerate(candidates):
        rid = int(fields["resourceId"])
        identity = hashlib.sha256(
            ("fut19-trade:" + supply_hash + ":" + str(rid)).encode("ascii")
        ).digest()
        trade_id = 800_000_000_000 + int.from_bytes(identity[:6], "big") % 100_000_000_000
        while trade_id in used_trade_ids:
            trade_id += 1
        used_trade_ids.add(trade_id)
        if trade_id in unavailable:
            continue
        available.append({
            "tradeId": trade_id,
            "itemId": trade_id,
            "resourceId": rid,
            "fields": fields,
            "startingBid": starting,
            "currentBid": 0,
            "buyNowPrice": buy_now,
            "expires": 300 + int.from_bytes(identity[6:8], "big") % 3_301,
            "absoluteIndex": absolute_index,
            "marketEpoch":epoch,
            "filterHash":filter_hash,
            "listingKind":"search",
        })
    listings=[remember_player_listing(listing) for listing in
              available[search.start:search.start+search.num]]
    total = len(available)
    return {
        "listings": listings,
        "start": search.start,
        "num": search.num,
        "totalResults": total,
        "endOfList": search.start + len(listings) >= total,
        "filterHash": filter_hash,"marketEpoch":epoch,
    }


def search_object_listings(query: Mapping[str, Any], market_epoch_value: Any = None,
                           unavailable_trade_ids: Any = None) -> dict[str, Any]:
    """Generate one deterministic page from the bounded object catalogue."""
    search = normalize_object_search(query)
    stable_json = json.dumps(search.stable_filters(), sort_keys=True,
                             separators=(",", ":"))
    filter_hash = hashlib.sha256(stable_json.encode("utf-8")).hexdigest()
    epoch = (market_epoch() if market_epoch_value is None
             else max(0, int(market_epoch_value)))
    supply_hash = hashlib.sha256(
        ("object:" + filter_hash + ":" + str(epoch)).encode("ascii")).hexdigest()
    supported_types = {"", "development", "training", "clubinfo", "staff",
                       "consumable"}
    if search.item_type not in supported_types:
        return {"listings": [], "start": search.start, "num": search.num,
                "totalResults": 0, "endOfList": True,
                "filterHash": filter_hash, "marketEpoch": epoch}

    candidates = []
    for fields, starting, buy_now in _market_objects():
        if not _object_matches(search, fields, starting, buy_now):
            continue
        identity = "%s:%d" % (
            _object_token(fields.get("catalogType")),
            int(fields.get("resourceId", 0) or 0))
        order = hashlib.sha256(
            (supply_hash + ":" + identity).encode("ascii")).digest()
        candidates.append((order, identity, fields, starting, buy_now))
    candidates.sort(key=lambda entry: (entry[0], entry[1]))

    unavailable = {int(value) for value in (unavailable_trade_ids or ())}
    available = []
    used_trade_ids: set[int] = set()
    for absolute_index, (_, identity, fields, starting, buy_now) in enumerate(
            candidates):
        digest = hashlib.sha256(
            ("fut19-object-trade:" + supply_hash + ":" + identity
             ).encode("ascii")).digest()
        trade_id = (700_000_000_000 +
                    int.from_bytes(digest[:6], "big") % 100_000_000_000)
        while trade_id in used_trade_ids:
            trade_id += 1
        used_trade_ids.add(trade_id)
        if trade_id in unavailable:
            continue
        available.append({
            "tradeId": trade_id,
            "itemId": trade_id,
            "resourceId": int(fields["resourceId"]),
            "fields": dict(fields),
            "startingBid": starting,
            "currentBid": 0,
            "buyNowPrice": buy_now,
            "expires": 300 + int.from_bytes(digest[6:8], "big") % 3_301,
            "absoluteIndex": absolute_index,
            "marketEpoch": epoch,
            "filterHash": filter_hash,
            "listingKind": "object-search",
        })
    listings = [remember_player_listing(listing) for listing in
                available[search.start:search.start + search.num]]
    total = len(available)
    return {
        "listings": listings,
        "start": search.start,
        "num": search.num,
        "totalResults": total,
        "endOfList": search.start + len(listings) >= total,
        "filterHash": filter_hash,
        "marketEpoch": epoch,
    }


def remember_player_listing(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Keep a bounded snapshot so a later native bid route can resolve it."""
    row=json.loads(json.dumps(dict(snapshot)))
    trade_id=int(row.get("tradeId",0) or 0)
    if trade_id <= 0:
        raise ValueError("AI listing requires a positive tradeId")
    if row.get("snapshotSignature"):
        if not verify_player_listing_snapshot(row):
            raise ValueError("AI listing snapshot integrity check failed")
    else:
        row=seal_player_listing(row)
    _AI_LISTING_CACHE[trade_id]=row
    _AI_LISTING_CACHE.move_to_end(trade_id)
    while len(_AI_LISTING_CACHE) > _AI_LISTING_CACHE_LIMIT:
        _AI_LISTING_CACHE.popitem(last=False)
    return json.loads(json.dumps(row))


def resolve_player_listing(trade_id: Any) -> dict[str, Any] | None:
    row=_AI_LISTING_CACHE.get(int(trade_id or 0))
    if row is None:
        return None
    _AI_LISTING_CACHE.move_to_end(int(trade_id))
    return json.loads(json.dumps(row))


def deterministic_user_sale(fields: Mapping[str, Any], buy_now: Any,
                            duration: Any, trade_id: Any,
                            created: Any) -> dict[str, Any]:
    """Choose and persist one restart-independent bot-sale outcome.

    Prices at or below fair value always attract a buyer. Moderately expensive
    listings have a deterministic decreasing chance; listings above 140% of
    fair value remain unsold. The result is a local simulation, not historical
    FIFA 19 market evidence.
    """
    price=max(150,int(buy_now or 0))
    seconds=max(60,int(duration or 0))
    fair=max(150,fair_market_value(dict(fields)))
    ratio=float(price)/float(fair)
    digest=hashlib.sha256(
        ("fut19-buyer:%s:%s:%s" % (int(trade_id),price,seconds)).encode("ascii")
    ).digest()
    roll=int.from_bytes(digest[:2],"big")/65535.0
    if ratio <= 1.0:
        chance=1.0
    elif ratio >= 1.4:
        chance=0.0
    else:
        chance=max(0.0,1.0-(ratio-1.0)/0.4)
    if roll >= chance:
        return {"saleAt":None,"saleDelay":None,"fairValue":fair,
                "priceRatio":round(ratio,6)}
    if ratio <= 1.0:
        # A listing at or below the server's fair value is the local buyer's
        # strongest signal. Waiting up to 30 minutes made a guaranteed sale
        # indistinguishable from a broken bot during normal play.
        delay=2+(int.from_bytes(digest[2:6],"big") % 7)
    else:
        upper=max(60,min(seconds-1,1800))
        delay=60+(int.from_bytes(digest[2:6],"big") % max(1,upper-59))
    return {"saleAt":int(created)+delay,"saleDelay":delay,
            "fairValue":fair,"priceRatio":round(ratio,6)}


def deterministic_bid_outcome(trade_id: Any, amount: Any, now: Any,
                              expires_at: Any) -> dict[str, Any]:
    """Schedule one durable local AI response to a user's normal bid."""
    digest=hashlib.sha256(
        ("fut19-bid:%s:%s" % (int(trade_id),int(amount))).encode("ascii")
    ).digest()
    outcome="WON" if digest[0] < 96 else "OUTBID"
    delay=60+int.from_bytes(digest[1:3],"big")%241
    outcome_at=min(int(expires_at),int(now)+delay)
    return {"outcome":outcome,"outcomeAt":outcome_at}

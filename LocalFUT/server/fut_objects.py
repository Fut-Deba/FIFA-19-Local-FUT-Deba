#!/usr/bin/env python3
"""Static non-player FUT catalogue, grant expansion, and native item DTOs."""
import collections
import json
import os
import struct
import time


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
OBJECT_CATALOG_PATH = os.path.join(DATA_DIR, "object_catalog.json")
DEFAULT_GRANT_PATH = os.path.join(DATA_DIR, "default_grant.json")
DEFAULT_GRANT_NAMESPACE = "default-object-v1"
RTG_GRANT_NAMESPACE = "rtg-object-v1"

CATALOG_TYPES = {
    "manager", "headcoach", "fitnesscoach", "physio", "gkcoach",
    "badge", "stadium", "ball", "consumable",
}
GRANT_TYPES = CATALOG_TYPES | {"kit_home", "kit_away"}
STAFF_TYPES = {"headcoach", "fitnesscoach", "physio", "gkcoach"}

_CATALOG = None
_DEFAULT_GRANT = None
_MANAGER_CONTEXT = None
_TEAM_TO_LEAGUE = None
_BADGE_TEAM_LEAGUE = None


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _field(source, *names, default=None):
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return default


def _pile_number(value):
    if isinstance(value, str):
        return {"trade": 5, "transfer": 5, "purchased": 6, "club": 7,
                "inbox": 8, "gift": 9}.get(value.lower(), 7)
    return _int(value, 7)


def object_catalog():
    global _CATALOG
    if _CATALOG is None:
        with open(OBJECT_CATALOG_PATH, encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("object_catalog.json must contain an object")
        _CATALOG = payload
    return _CATALOG


def default_grant():
    global _DEFAULT_GRANT
    if _DEFAULT_GRANT is None:
        with open(DEFAULT_GRANT_PATH, encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("default_grant.json must contain an items array")
        _DEFAULT_GRANT = payload
    return _DEFAULT_GRANT


def object_definition(resource_id):
    return object_catalog().get(str(_int(resource_id)))


def _t3_table_reader(data, directory, table_name):
    """Return the calibrated record reader for one Frostbite t3db table."""
    table_offset = directory[table_name]
    ordered_offsets = sorted(set(directory.values()))
    table_index = ordered_offsets.index(table_offset)
    table_end = (ordered_offsets[table_index + 1]
                 if table_index + 1 < len(ordered_offsets) else len(data))
    field_count = struct.unpack_from("<I", data, table_offset + 0x1c)[0]
    record_start = table_offset + 0x30 + field_count * 16
    stride_bytes = struct.unpack_from("<I", data, table_offset + 0x08)[0]
    stride_bits = stride_bytes * 8
    record_count = ((table_end - record_start) * 8) // stride_bits

    def read(record, bit_offset, width):
        bit_position = record * stride_bits + bit_offset
        value = 0
        for index in range(width):
            absolute = bit_position + index
            value |= ((data[record_start + (absolute >> 3)] >>
                       (absolute & 7)) & 1) << index
        return value

    return read, record_count


def manager_context_map():
    """Map FIFA 19 manager asset IDs to their real team and league.

    Both relationships come from the installed Frostbite database:
    manager.managerid -> manager.teamid -> leagueteamlinks.leagueid.  The
    packed fields use a range-low value of one, hence the explicit +1.
    """
    global _MANAGER_CONTEXT
    if _MANAGER_CONTEXT is not None:
        return _MANAGER_CONTEXT
    path = os.path.join(DATA_DIR, "fifa_ng_db.DB")
    with open(path, "rb") as stream:
        data = stream.read()
    if data[:2] != b"DB":
        raise ValueError("fifa_ng_db.DB is not a Frostbite t3db file")
    datastart = 0x18 + 157 * 8
    directory = {}
    for index in range(157):
        offset = 0x18 + index * 8
        name = data[offset:offset + 4].decode("latin1")
        relative = struct.unpack_from("<I", data, offset + 4)[0]
        directory[name] = datastart + relative

    league_reader, league_rows = _t3_table_reader(
        data, directory, "qdZF")
    team_to_league = {}
    for row in range(league_rows):
        team_id = league_reader(row, 68, 18) + 1
        league_id = league_reader(row, 8, 12) + 1
        team_to_league[team_id] = league_id

    global _TEAM_TO_LEAGUE
    _TEAM_TO_LEAGUE = dict(team_to_league)

    manager_reader, manager_rows = _t3_table_reader(
        data, directory, "Knen")
    result = {}
    for row in range(manager_rows):
        manager_id = manager_reader(row, 240, 14) + 1
        team_id = manager_reader(row, 288, 18) + 1
        league_id = team_to_league.get(team_id, 0)
        if manager_id and team_id and league_id:
            result[1000000 + manager_id] = {
                "teamid": team_id, "leagueId": league_id,
            }
    _MANAGER_CONTEXT = result
    return _MANAGER_CONTEXT


def team_to_league_map():
    """Return the sourced team -> league mapping used by every club object."""
    if _TEAM_TO_LEAGUE is None:
        manager_context_map()
    return _TEAM_TO_LEAGUE or {}


def badge_team_league_map():
    """Return the league each badge in the catalogue declares for its team.

    The badge catalogue and the sourced team table disagree for 16 of the 100
    clubs granted by default - the catalogue knows competition leagues like
    3006 and 332 that the table flattens to 76. A kit and the badge of the
    same club have to answer a league filter identically, so the kit follows
    the badge and falls back to the table only where no badge exists.
    """
    global _BADGE_TEAM_LEAGUE
    if _BADGE_TEAM_LEAGUE is not None:
        return _BADGE_TEAM_LEAGUE
    mapping = {}
    for spec in object_catalog().values():
        if not isinstance(spec, dict):
            continue
        team = _int(_field(spec, "teamid", "teamId", "_teamId", "ClubId"))
        league = _int(_field(spec, "leagueId", "LeagueId"))
        if team and league:
            mapping.setdefault(team, league)
    _BADGE_TEAM_LEAGUE = mapping
    return _BADGE_TEAM_LEAGUE


def manager_context(resource_id):
    """Return source-backed manager metadata, never a fabricated fallback."""
    return dict(manager_context_map().get(_int(resource_id), {}))


def validate_object_data(catalog=None, grant=None):
    """Return every catalogue/grant integrity error without changing state."""
    catalog = object_catalog() if catalog is None else catalog
    grant = default_grant() if grant is None else grant
    errors = []
    if not isinstance(catalog, dict):
        return ["catalog is not an object"]
    if not isinstance(grant, dict) or not isinstance(grant.get("items"), list):
        return ["grant does not contain an items array"]

    for key, definition in catalog.items():
        if not isinstance(definition, dict):
            errors.append("catalog %s is not an object" % key)
            continue
        resource_id = _int(definition.get("_resourceId"), -1)
        if str(resource_id) != str(key):
            errors.append("catalog key/resource mismatch: %s" % key)
        if definition.get("_type") not in CATALOG_TYPES:
            errors.append("catalog %s has unsupported type %r" %
                          (key, definition.get("_type")))

    for index, spec in enumerate(grant["items"]):
        if not isinstance(spec, dict):
            errors.append("grant row %d is not an object" % index)
            continue
        grant_type = str(spec.get("type", "")).lower()
        quantity = _int(spec.get("qty", 1), 0)
        if grant_type not in GRANT_TYPES:
            errors.append("grant row %d has unsupported type %r" %
                          (index, grant_type))
        if quantity <= 0:
            errors.append("grant row %d has invalid qty" % index)
        if grant_type in ("kit_home", "kit_away"):
            if _int(spec.get("teamId"), 0) <= 0:
                errors.append("grant row %d has invalid teamId" % index)
            expected_category = 2 if grant_type == "kit_home" else 3
            if _int(spec.get("category"), expected_category) != expected_category:
                errors.append("grant row %d has invalid kit category" % index)
            continue
        resource_id = _int(spec.get("resourceId"), 0)
        definition = catalog.get(str(resource_id))
        if definition is None:
            errors.append("grant row %d references missing resource %d" %
                          (index, resource_id))
        elif definition.get("_type") != grant_type:
            errors.append("grant row %d type %s does not match catalog %s" %
                          (index, grant_type, definition.get("_type")))
    return errors


def default_grant_quantity():
    return sum(_int(row.get("qty", 1), 0) for row in default_grant()["items"])


def default_grant_counts():
    counts = collections.Counter()
    for row in default_grant()["items"]:
        counts[str(row.get("type", "")).lower()] += _int(row.get("qty", 1), 0)
    return dict(counts)


def rtg_grant():
    """Return the complete source-backed non-player catalogue for RTG.

    RTG restricts the economy and pack probabilities, not access to the local
    offline object catalogue.  Reuse the validated normal grant while the RTG
    namespace keeps its additive migration ledger independent.
    """
    return {"items":[dict(row) for row in default_grant()["items"]]}


def rtg_grant_quantity():
    return sum(_int(row.get("qty",1),0) for row in rtg_grant()["items"])


def stored_item_kind(item):
    return str(item.get("inventoryType", "player") or "player").lower()


def _common_item_dto(source, item_type, subtype, asset_id=None):
    resource_id = _int(_field(source, "resourceId", "_resourceId"))
    rating = _int(_field(source, "rating", "Rating", "value", "Value"), 75)
    rare = _int(_field(source, "rareflag", "Rare"), 0)
    # The retail PC parser chooses the concrete object reader as soon as it
    # sees itemType. Keep it before the static asset identity as well as the
    # subtype discriminator used by the card factory.
    untradeable=bool(source.get("untradeable", True))
    dto = {
        "id": _int(_field(source, "id", "itemId")),
        "itemId": _int(_field(source, "id", "itemId")),
        "timestamp": _int(source.get("timestamp"), int(time.time())),
        "itemType": str(item_type),
        "formation": str(source.get("formation", "f442")),
        "untradeable":untradeable,
        "tradeable":not untradeable,
        "assetId": _int(resource_id if asset_id is None else asset_id),
        "rating": max(0, min(99, rating)),
        "resourceId": resource_id,
        # The EA App build resolves every revealed item through its immutable
        # definition identity. Player DTOs already carry this field; object
        # DTOs must do the same or mixed-pack parsing can stall after the HTTP
        # response while the client tries to instantiate the object card.
        "definitionId": resource_id,
        "owners": _int(source.get("owners"), 1),
        # Non-player objects are deliberately low-value economy utilities in
        # Local FUT.  Old generated packs used rating*8 (typically 600 coins),
        # which made bulk consumable/manager discards an unintended coin farm.
        # Untradeable objects remain zero; every tradeable object is worth 20.
        "discardValue":0 if untradeable else 20,
        "itemState": str(source.get("itemState", "free")),
        "lastSalePrice": _int(source.get("lastSalePrice"), 0),
        "marketDataMinPrice": _int(source.get("marketDataMinPrice"), 0),
        "marketDataMaxPrice": _int(source.get("marketDataMaxPrice"), 0),
        "rareflag": rare,
        "pile": _pile_number(source.get("pile", 7)),
        "resourceGameYear": 2019,
        "stackCount": _int(source.get("stackCount"), 1),
        # Inventory is persisted per item rather than as mutable stacks. Keep
        # the aggregate marker consistent with the item's tradeability even
        # when an older database row contains the former default value of one.
        "untradeableCount": 1 if untradeable else 0,
        "statsList": [],
        "lifetimeStats": [],
        "attributeList": [],
    }
    if subtype is not None:
        dto["cardsubtypeid"] = _int(subtype)
    return dto


def manager_item_dto(source):
    resource_id = _int(_field(source, "resourceId", "_resourceId",
                              "assetId", "AssetId"))
    context = manager_context(resource_id)
    dto = _common_item_dto(
        source, "manager", 4,
        _field(source, "assetId", "AssetId", "resourceId"))
    nation = _int(_field(source, "nation", "nationId", "NationId"))
    contract = _int(_field(source, "contract", "contracts", default=30), 30)
    team_id = _int(context.get("teamid"),
                   _field(source, "teamid", "teamId", default=0))
    league_id = _int(context.get("leagueId"),
                     _field(source, "leagueId", "LeagueId", default=0))
    dto.update({
        "firstName": str(_field(source, "firstName", "FirstName", default="")),
        "lastName": str(_field(source, "lastName", "LastName", default="")),
        "nation": nation,
        "nationId": nation,
        "formationId": _int(_field(source, "formationId", "FormationId")),
        "negotiation": _int(_field(source, "negotiation", "Negotiation")),
        "talkRating": _int(_field(source, "talkRating", "TalkRating")),
        "value": _int(_field(source, "value", "Value", "rating", "Rating"), 75),
        "weightrare": _int(_field(source, "weightrare", "Weight", "Rare")),
        "contract": contract,
        "contracts": contract,
        "teamid": team_id,
        "teamId": team_id,
        "leagueId": league_id,
    })
    return dto


_STAFF_META = {
    "headcoach": ("headCoach", 5),
    "gkcoach": ("gkCoach", 6),
    "physio": ("physio", 7),
    "fitnesscoach": ("fitnessCoach", 8),
}


def staff_item_dto(source):
    catalog_type = str(source.get("catalogType", source.get("_type", ""))).lower()
    item_type, subtype = _STAFF_META[catalog_type]
    dto = _common_item_dto(source, item_type, subtype)
    dto.update({
        "firstName": str(_field(source, "firstName", "FirstName", default="")),
        "lastName": str(_field(source, "lastName", "LastName", default="")),
        "amount": _int(_field(source, "amount", "Amount")),
        "attr": _int(_field(source, "attr", "Attr"), -1),
        "pos": _int(_field(source, "pos", "Pos"), -1),
        "posBonus": _int(_field(source, "posBonus", "PosBonus")),
    })
    return dto


def kit_item_dto(source):
    category = _int(source.get("category"), 2)
    team_id = _int(_field(source, "teamid", "teamId"))
    home = category == 2
    resource_id = (6300000 if home else 6400000) + team_id
    enriched = dict(source)
    enriched["resourceId"] = resource_id
    dto = _common_item_dto(enriched, "kit", 9, 14 if home else 15)
    dto.update({
        "teamid": team_id,
        # Badges carry their league and kits did not, so filtering the club by
        # kit league matched nothing at all. The mapping is the same sourced
        # table both already depend on.
        "leagueId": _int(
            _field(source, "leagueId", "LeagueId"),
            badge_team_league_map().get(
                team_id, team_to_league_map().get(team_id, 0))),
        "category": 2 if home else 3,
        "name": str(source.get("name", "TeamName_Abbr15_%d" % team_id)),
        "year": 0,
    })
    return dto


def badge_item_dto(source):
    team_id = _int(_field(source, "teamid", "teamId", "_teamId", "ClubId"))
    resource_id = _int(_field(source, "resourceId", "_resourceId"), 6000000 + team_id)
    enriched = dict(source)
    enriched["resourceId"] = resource_id
    dto = _common_item_dto(enriched, "custom", 11, team_id)
    dto.update({
        "teamid": team_id,
        "leagueId": _int(_field(source, "leagueId", "LeagueId")),
        "nation": _int(_field(source, "nation", "NationId")),
        "category": _int(_field(source, "category", "Category"), 1),
        "cardassetid": 39,
        "value": _int(_field(source, "value", "Rating"), 75),
        "weightrare": _int(_field(source, "weightrare", "Rare")),
        "name": str(source.get("name", "TeamName_Abbr15_%d" % team_id)),
        "description": str(source.get("description", "TeamName_Abbr15_%d" % team_id)),
        "header": "Badge",
        "biodescription": str(source.get("biodescription", "TeamName_Abbr15_%d" % team_id)),
        "chantsCount": _int(source.get("chantsCount"), 0),
    })
    return dto


def stadium_item_dto(source):
    stadium_id = _int(_field(source, "stadiumid", "stadiumId", "StadiumId", "AssetId"))
    dto = _common_item_dto(source, "stadium", 10, stadium_id)
    dto.update({
        "stadiumid": stadium_id,
        "stadiumId": stadium_id,
        "category": 4,
        "name": str(_field(source, "name", "Name", default="")),
        "capacity": _int(_field(source, "capacity", "Cap")),
        "boost": _int(_field(source, "boost", "Boost")),
        "myStadium": bool(source.get("myStadium", False)),
    })
    return dto


def ball_item_dto(source):
    dto = _common_item_dto(
        # Ball is a concrete object type in the retail parser.  Supplying the
        # generic card subtype makes FIFA try the card factory and silently
        # discard the row from Club Items search.
        source, "ball", None, _field(source, "assetId", "AssetId", default=0))
    dto.update({
        "name": str(_field(source, "name", "Name", "Desc", default="")),
        "description": str(_field(source, "description", "Desc", default="")),
        "manufacturer": str(_field(source, "manufacturer", "Manufacturer", default="")),
        "ballRestricted": bool(source.get("ballRestricted", False)),
    })
    return dto


_FORMATION_NAMES = (
    "3412", "3421", "343", "352", "41212", "4231", "4222", "4312",
    "4321", "433", "4411", "442", "451", "5212", "5221", "532",
)
_POSITION_NAMES = (
    "LWB_LB", "LB_LWB", "RWB_RB", "RB_RWB", "LM_LW", "RM_RW",
    "LW_LM", "RW_RM", "LW_LF", "RW_RF", "LF_LW", "RF_RW", "CM_CAM",
    "CAM_CM", "CDM_CM", "CM_CDM", "CAM_CF", "CF_CAM", "CF_ST", "ST_CF",
)
_GK_TRAINING = {
    "Diving": 51, "Handling": 52, "Kicking": 53, "Speed": 54,
    "Position": 55, "Reflex": 56, "All": 57,
}
_PLAYER_TRAINING = {
    "Pace": 61, "Shooting": 62, "Passing": 63, "Dribbling": 64,
    "Heading": 65, "Defend": 66, "All": 67,
}
_HEALING = {
    "HealthHead": 211, "HealthShoulder": 212, "HealthArm": 213,
    "HealthBack": 214, "HealthHip": 215, "HealthLeg": 216,
    "HealthFoot": 217, "HealthAll": 218,
}


def _consumable_meta(source):
    definition_type = str(source.get("definitionItemType", source.get("ItemType", "")))
    resource_id = _int(_field(source, "resourceId", "_resourceId"))
    if definition_type == "ContractPlayer":
        return "development", 201, 7
    if definition_type == "ContractStaff":
        return "development", 202, 8
    if definition_type in _HEALING:
        return "development", _HEALING[definition_type], 9
    if definition_type == "FitnessPlayer":
        return "development", 219, 10
    if definition_type == "FitnessTeam":
        return "development", 220, 10
    if definition_type.startswith("TrainingGk"):
        return "training", _GK_TRAINING.get(definition_type[10:], 0), 3
    if definition_type.startswith("TrainingPlayerPos"):
        name = definition_type[17:]
        return "training", 91 + _POSITION_NAMES.index(name), 34
    if definition_type.startswith("TrainingPlayer"):
        return "training", _PLAYER_TRAINING.get(definition_type[14:], 0), 1
    if definition_type.startswith("TrainingManagerFormation"):
        name = definition_type[24:]
        return "training", 121 + _FORMATION_NAMES.index(name), 2
    if definition_type.startswith("TrainingPlaystyle"):
        subtype = _int(definition_type[17:])
        return "training", subtype, 51 if subtype >= 269 else 50
    if definition_type == "TrainingLeagueModifier":
        # FIFA's modifier definitions are ordered from subtype 300 onward.
        return "training", 300 + max(0, resource_id - 5003119), 32
    if definition_type == "MiscDraftToken":
        return "development", 236, 0
    raise ValueError("unsupported granted consumable type %r" % definition_type)


def _consumable_member(definition_type, subtype):
    """Return the native union member used by FIFA's consumable parser.

    Consumable definitions share the same outer DTO.  The retail client does
    not infer their concrete class from cardassetid/cardsubtypeid alone: one
    typed member must also be present in the definition object.
    """
    if definition_type == "ContractPlayer":
        return "consumablesContractPlayer"
    if definition_type == "ContractStaff":
        return "consumablesContractManager"
    if definition_type == "FitnessPlayer":
        return "consumablesFitnessPlayer"
    if definition_type == "FitnessTeam":
        return "consumablesFitnessTeam"
    if definition_type in _HEALING:
        return "consumablesHealing"
    if definition_type.startswith("TrainingPlayerPos"):
        return "consumablesPosition"
    if definition_type.startswith("TrainingGk"):
        return "consumablesTrainingGk"
    if definition_type.startswith("TrainingPlayer"):
        return "consumablesTrainingPlayer"
    if definition_type.startswith("TrainingManagerFormation"):
        return "consumablesFormationManager"
    if definition_type.startswith("TrainingPlaystyle"):
        return ("consumablesTrainingGkPlayStyle" if int(subtype) >= 269
                else "consumablesTrainingPlayerPlayStyle")
    if definition_type == "TrainingLeagueModifier":
        return "consumablesTrainingManagerLeagueModifier"
    if definition_type == "MiscDraftToken":
        return "consumables"
    raise ValueError("unsupported granted consumable member %r" % definition_type)


def _consumable_member_value(definition_type, amount):
    """Return a valid value for the concrete consumable discriminator.

    Contracts, position changes, play styles and manager modifiers carry their
    effect in other definition fields. Their source ``Amount`` can therefore
    legitimately be zero or absent even though the concrete union member must
    be non-zero for the native item factory to select that consumable class.
    Numeric fitness, healing and training boosts retain their source amount.
    """
    marker_only=(
        definition_type in {"ContractPlayer","ContractStaff",
                            "TrainingLeagueModifier","MiscDraftToken"} or
        definition_type.startswith("TrainingPlayerPos") or
        definition_type.startswith("TrainingManagerFormation") or
        definition_type.startswith("TrainingPlaystyle")
    )
    return 1 if marker_only else max(1,_int(amount))


def consumable_item_dto(source):
    item_type, subtype, card_asset_id = _consumable_meta(source)
    definition_type = str(source.get(
        "definitionItemType", source.get("ItemType", "")))
    amount = _int(_field(source, "amount", "Amount"))
    dto = _common_item_dto(source, item_type, subtype, card_asset_id)
    dto.update({
        "cardassetid": card_asset_id,
        "amount": amount,
        "bronze": _int(_field(source, "bronze", "Bronze")),
        "silver": _int(_field(source, "silver", "Silver")),
        "gold": _int(_field(source, "gold", "Gold")),
        "weightrare": _int(_field(source, "weightrare", "Rare")),
        "name": str(_field(source, "name", "Desc", default="")),
        "description": str(_field(source, "description", "Desc", default="")),
    })
    # This discriminator is consumed by createConsumable(). Some definitions
    # use a zero/absent Amount because their effect lives in the subtype or a
    # separate field; zero leaves the concrete native class unselected.
    dto[_consumable_member(definition_type, subtype)] = (
        _consumable_member_value(definition_type,amount))
    if definition_type == "TrainingLeagueModifier":
        dto["leagueId"] = dto["amount"]
    return dto


_DTO_BY_KIND = {
    "manager": manager_item_dto,
    "staff": staff_item_dto,
    "kit": kit_item_dto,
    "badge": badge_item_dto,
    "stadium": stadium_item_dto,
    "ball": ball_item_dto,
    "consumable": consumable_item_dto,
}


def native_object_item(source):
    kind = stored_item_kind(source)
    if kind not in _DTO_BY_KIND:
        raise ValueError("unsupported stored object kind %r" % kind)
    return _DTO_BY_KIND[kind](source)


def build_grant_item(spec, catalog=None):
    """Resolve one grant row into the normalized SQLite item payload."""
    catalog = object_catalog() if catalog is None else catalog
    grant_type = str(spec.get("type", "")).lower()
    if grant_type in ("kit_home", "kit_away"):
        team_id = _int(spec.get("teamId"))
        source = {
            "inventoryType": "kit", "catalogType": grant_type,
            "teamid": team_id,
            "category": 2 if grant_type == "kit_home" else 3,
            "rating": 75, "rareflag": 0, "untradeable": True,
            "pile": "club",
        }
        dto = kit_item_dto(source)
        dto.update({"inventoryType": "kit", "catalogType": grant_type})
        return dto

    resource_id = _int(spec.get("resourceId"))
    definition = catalog[str(resource_id)]
    kind = ("staff" if grant_type in STAFF_TYPES else grant_type)
    source = dict(definition)
    source.update({
        "inventoryType": kind,
        "catalogType": grant_type,
        "resourceId": resource_id,
        "rating": _int(_field(definition, "Rating", "Value"), 75),
        "rareflag": _int(definition.get("Rare"), 0),
        "untradeable": True,
        "pile": "club",
    })
    if grant_type == "consumable":
        source["definitionItemType"] = str(definition.get("ItemType", ""))
    dto = _DTO_BY_KIND[kind](source)
    dto.update({"inventoryType": kind, "catalogType": grant_type})
    if grant_type == "consumable":
        dto["definitionItemType"] = source["definitionItemType"]
    return dto


def expanded_default_grant(catalog=None, grant=None):
    """Yield stable per-instance rows; quantities are never stored as stacks."""
    catalog = object_catalog() if catalog is None else catalog
    grant = default_grant() if grant is None else grant
    errors = validate_object_data(catalog, grant)
    if errors:
        raise ValueError("invalid object grant: " + "; ".join(errors[:10]))
    occurrences = collections.Counter()
    for spec in grant["items"]:
        grant_type = str(spec["type"]).lower()
        if grant_type in ("kit_home", "kit_away"):
            identity = "%d:%d" % (_int(spec["teamId"]), _int(spec["category"]))
        else:
            identity = str(_int(spec["resourceId"]))
        occurrence_key = (grant_type, identity)
        base = build_grant_item(spec, catalog)
        for unused in range(_int(spec.get("qty", 1))):
            ordinal = occurrences[occurrence_key]
            occurrences[occurrence_key] += 1
            grant_key = "%s:%s:%s:%d" % (
                DEFAULT_GRANT_NAMESPACE, grant_type, identity, ordinal)
            yield {
                "grantKey": grant_key,
                "itemKind": stored_item_kind(base),
                "resourceId": _int(base["resourceId"]),
                "item": dict(base),
            }


def expanded_rtg_grant(catalog=None):
    """Yield every RTG object as untradeable and worth zero on discard."""
    catalog = object_catalog() if catalog is None else catalog
    grant = rtg_grant()
    errors = validate_object_data(catalog,grant)
    if errors:
        raise ValueError("invalid RTG object grant: " + "; ".join(errors[:10]))
    occurrences = collections.Counter()
    for spec in grant["items"]:
        grant_type = str(spec["type"]).lower()
        if grant_type in ("kit_home","kit_away"):
            identity = "%d:%d" % (_int(spec["teamId"]),_int(spec["category"]))
        else:
            identity = str(_int(spec["resourceId"]))
        occurrence_key = (grant_type,identity)
        base = build_grant_item(spec,catalog)
        base.update({"untradeable":True,"tradeable":False,
                     "discardValue":0,
                     "untradeableCount":max(1,_int(
                         base.get("untradeableCount"),1))})
        for unused in range(_int(spec.get("qty",1))):
            ordinal = occurrences[occurrence_key]
            occurrences[occurrence_key] += 1
            yield {
                "grantKey":"%s:%s:%s:%d" % (
                    RTG_GRANT_NAMESPACE,grant_type,identity,ordinal),
                "itemKind":stored_item_kind(base),
                "resourceId":_int(base["resourceId"]),
                "item":dict(base),
            }

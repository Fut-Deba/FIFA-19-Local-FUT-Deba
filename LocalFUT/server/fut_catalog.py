#!/usr/bin/env python3
"""FIFA 19 FUT player catalogue backed by packaged static metadata."""

from __future__ import annotations

import json
import os
import threading
import unicodedata
from typing import Any


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PLAYERS_PATH = os.path.join(DATA_DIR, "players.json")
PLAYERS_META_PATH = os.path.join(DATA_DIR, "players_meta.json")
# Local player metadata and card-version attributes used by the runtime.
PLAYERS_DB_PATH = os.path.join(DATA_DIR, "players_db.json")
CARD_VERSIONS_PATH = os.path.join(DATA_DIR, "card_versions.json")
# Independently reviewed additions remain separate from the generated catalogue.
# The generated file wins if a later rebuild contains the same resource ID, so
# an overlay can be retired without a data migration.
CARD_VERSION_OVERLAY_PATHS = (
    os.path.join(DATA_DIR, "carniball_versions.json"),
    os.path.join(DATA_DIR, "ucl_moments_versions.json"),
)
# Reviewed corrections for dynamic cards whose final upgrade is not represented
# in card_versions.json.
CARD_VERSION_CORRECTIONS_PATH = os.path.join(
    DATA_DIR, "fut19_card_version_corrections.json")
SBC_REWARD_CARDS_PATH = os.path.join(
    DATA_DIR, "fut19_sbc_reward_cards_verified.json")
RESOURCE_VERSION_FACTOR = 1 << 24
# The supported November 2018 client masks version-encoded FUT resource IDs to
# 24 bits before resolving the player's launch-database identity.  PIM rows
# arrived later as standalone 246xxx definitions, so they cannot resolve in
# that database.  Version 31 is outside FIFA 19's sourced 0..18 card range and
# gives each PIM a collision-free, reversible wire identity while preserving
# the verified base Icon in the lower 24 bits.
PIM_COMPAT_VERSION_INDEX = 31
# FIFA 19's retail ItemSubType enum defines PLAYER as 2.  This value describes
# the object family and is intentionally identical for Normal, TOTW and every
# promo revision; card design and promo identity come from rareflag.
PLAYER_CARD_SUBTYPE = 2

_lock = threading.Lock()
_catalog: dict[int, dict[str, Any]] | None = None
_players_db: dict[str, Any] | None = None
_card_versions: dict[str, Any] | None = None
_league_name2id: dict[str, int] | None = None
_prime_icon_identity_by_resource: dict[int, dict[str, Any]] | None = None

ICON_LEAGUE_ID = 2118
ICON_REVISIONS = frozenset(("Icon", "Prime Icon Moments"))

# Rarity/card-design IDs read from futitemraritytunables.json. The native
# renderer selects the card face from rareflag; using 1 for every version made
# TOTY/Icon/TOTW resource IDs render as ordinary gold cards.
CARD_REVISION_RARITY = {
    "Icon": 12, "IF": 3, "SIF": 3, "TIF": 3, "TOTY": 5,
    "OTW": 21, "OTW SBC": 21, "Halloween": 22, "Halloween SBC": 22,
    "FUTmas SBC": 32, "UCL LIVE": 50, "TOTGS": 70,
    "UCL Moments": 69,
    "CL TOTT SBC": 69, "SBC": 24, "Flashback SBC": 51,
    "Swap Deals": 52, "Swap Deals SBC": 52, "Swap Deal Reward": 63,
    "Swap Deal Reward SBC": 63, "UEL LIVE": 46,
    "UEL LIVE SBC": 44, "Europa TOTGS": 68, "Europa Base": 78,
    "TOTY Nominee": 64, "TOTY Nominee Loan": 64, "TOTY Nominee SBC": 64,
    "PL POTM": 43, "Bundes POTM": 42, "CL": 48,
    "FUT Future Stars": 71, "SBC Future Stars": 71,
    "SBC - Future Stars": 71, "Weekly Objective Future Stars": 83,
    "Weekly Obj- FFS": 83, "Weekly Obj- FS": 83, "Award Winner": 28,
    "TOTS": 66, "FUT Birthday": 30, "Carniball": 72,
    # SBC and weekly-objective variants share one promo design. Their distinct
    # revisions remain necessary because pack pools include only pack cards.
    "Carniball SBC": 72, "Carniball Obj": 72,
    # Rarity 84 is defined by the packaged tuning data as PRIME_ICON_MOMENTS.
    # Its color arrays share the Legend family while retaining a distinct
    # palette. Without this mapping the items fall back to a normal Gold face.
    "Prime Icon Moments": 84,
    # Rarity 85 is defined by the same tuning data as FUT_19_HEADLINERS.
    "Headliners": 85, "Headliners SBC": 85,
}


def base_asset_id(resource_id: int) -> int:
    """Return the base player id encoded in a FUT resource/definition id."""
    value = int(resource_id)
    return value % RESOURCE_VERSION_FACTOR if value >= RESOURCE_VERSION_FACTOR else value


def _load_catalog() -> dict[int, dict[str, Any]]:
    global _catalog
    if _catalog is not None:
        return _catalog
    with _lock:
        if _catalog is not None:
            return _catalog
        with open(PLAYERS_PATH, "r", encoding="utf-8-sig") as handle:
            names = json.load(handle)
        with open(PLAYERS_META_PATH, "r", encoding="utf-8-sig") as handle:
            meta = json.load(handle)

        rows: dict[int, dict[str, Any]] = {}
        for group in ("Players", "LegendsPlayers"):
            for source in names.get(group, []):
                asset_id = int(source["id"])
                row = dict(source)
                row["assetId"] = asset_id
                row["meta"] = dict(meta.get(str(asset_id), {}))
                rows[asset_id] = row
        _catalog = rows
        return rows


def player(resource_id: int) -> dict[str, Any] | None:
    row = _load_catalog().get(base_asset_id(resource_id))
    return dict(row) if row else None


def catalogue_size() -> int:
    return len(_load_catalog())


def display_name(row: dict[str, Any]) -> str:
    return str(row.get("c") or " ".join(x for x in (row.get("f"), row.get("l")) if x)).strip()


# Italian starter squad for first-time FUT onboarding. Every resource ID is in
# the final FIFA 19 static catalogue. The starting eleven form a 4-4-2 with six
# common golds and five silvers; substitutes/reserves are split 6+6. The full
# range is 65-77, avoiding high-rated grants before the loan-player step.
STARTER_PLAYERS: tuple[dict[str, Any], ...] = (
    # Starting XI: six golds (75-77), five silvers (65-74).
    {"resourceId": 228413, "preferredPosition": "GK", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 228881, "preferredPosition": "RB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 237383, "preferredPosition": "CB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 229582, "preferredPosition": "CB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 202884, "preferredPosition": "LB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 233556, "preferredPosition": "RM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 222077, "preferredPosition": "CM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 225439, "preferredPosition": "CM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 229857, "preferredPosition": "LM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 237715, "preferredPosition": "ST", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 215689, "preferredPosition": "ST", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    # Substitutes and reserves: six golds, six silvers.
    {"resourceId": 205659, "preferredPosition": "GK", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 223329, "preferredPosition": "RB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 226108, "preferredPosition": "CB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 243237, "preferredPosition": "LB", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 211361, "preferredPosition": "CM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 212096, "preferredPosition": "CAM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 219995, "preferredPosition": "RW", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 201389, "preferredPosition": "LM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 236610, "preferredPosition": "ST", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 202848, "preferredPosition": "ST", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 228882, "preferredPosition": "CM", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
    {"resourceId": 203668, "preferredPosition": "ST", "nation": 27, "rareflag": 0, "cardsubtypeid": 0},
)


# Extra untradeable players granted once to a new local club. They remain in
# My Club and are deliberately not referenced by the active Starter Italy
# squad. The fixed, source-backed definitions make the grant reproducible and
# keep its exact Bronze/Silver composition stable across restarts and releases.
INITIAL_BONUS_PLAYERS: tuple[dict[str, Any], ...] = tuple(
    {"resourceId": resource_id, "quality": "bronze"}
    for resource_id in (
        11793, 138083, 11430, 53827, 48722, 11811, 134792, 189813,
        2702, 53855, 167919, 111503, 50531, 138427, 2335, 11800,
        49939, 13883, 102593, 10899,
    )
) + tuple(
    {"resourceId": resource_id, "quality": "silver"}
    for resource_id in (
        2147, 102056, 3484, 7647, 34079, 164,
        16, 3665, 54051, 51159, 19975, 163925,
    )
)


# Onboarding loan pool. The client does not simply show five global attackers:
# it downloads the pool and samples up to five players matching the general
# position of the selected starter card. At least five candidates are therefore
# required for every selectable position, or the picker can remain empty.
#
# Every entry is a real base card in the final FIFA 19 database. Seven loan
# matches are applied only to the granted copy.
LOAN_PLAYER_CANDIDATES: tuple[int, ...] = (
    # GK
    193080, 200389, 192119, 167495, 192448,
    # RB
    212622, 204963, 199564, 188377, 203551,
    # CB
    155862, 182493, 178603, 138956, 164240,
    # LB
    176676, 189332, 191043, 197445, 164169,
    # RM
    9014, 204970, 193082, 20775, 178088,
    # CM
    177003, 182521, 171877, 168651, 41,
    # LM
    190483, 181458, 193747, 188350, 184267,
    # ST
    20801, 176580, 188545, 202126, 179813,
    # CAM (present among starter reserves)
    192985, 211110, 197781, 168542, 198710,
    # RW (present among starter reserves)
    158023, 173731, 231747, 204485, 202652,
)


def _load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _sbc_reward_card_versions() -> dict[str, dict[str, Any]]:
    """Adapt the source-verified SBC reward cards to the card-version schema."""
    payload=_load_json(SBC_REWARD_CARDS_PATH)
    rows={}
    revision_aliases={
        "MLS POTM":"Award Winner",
        "Premium SBC":"SBC",
        "FUT Future Stars SBC":"SBC - Future Stars",
        "FUT Birthday SBC":"FUT Birthday",
        "TOTS Moments SBC":"TOTS",
        "TOTS SBC":"TOTS",
    }
    for source in payload.get("cards",[]) if isinstance(payload,dict) else []:
        resource_id=int(source.get("resourceId",0) or 0)
        if resource_id <= 0:
            continue
        revision=str(source.get("revision","") or "")
        if resource_id == 246516:
            revision="Prime Icon Moments"
        elif resource_id == 67292258:
            revision="FUT Birthday"
        else:
            revision=revision_aliases.get(revision,revision or "SBC")
        row={
            "assetId":int(source.get("assetId",resource_id) or resource_id),
            "versionIdx":(resource_id // RESOURCE_VERSION_FACTOR
                          if resource_id >= RESOURCE_VERSION_FACTOR else 0),
            "name":str(source.get("name","") or ""),
            "rating":int(source.get("rating",0) or 0),
            "pos":str(source.get("position","") or ""),
            "rev":revision,
            "pac":int(source.get("pac",0) or 0),
            "sho":int(source.get("sho",0) or 0),
            "pas":int(source.get("pas",0) or 0),
            "dri":int(source.get("dri",0) or 0),
            "def":int(source.get("def",0) or 0),
            "phy":int(source.get("phy",0) or 0),
            "sm":int(source.get("sm",0) or 0),
            "wf":int(source.get("wf",0) or 0),
            "club":int(source.get("club",0) or 0),
            "nation":int(source.get("nation",0) or 0),
            "league":source.get("league",0),
            "source":str(source.get("url","") or ""),
            "verifiedAt":"2026-08-31",
        }
        rows[str(resource_id)]=row
    return rows


def _players_db_map() -> dict[str, Any]:
    global _players_db
    if _players_db is None:
        with _lock:
            if _players_db is None:
                _players_db = _load_json(PLAYERS_DB_PATH)
    return _players_db


def _card_versions_map() -> dict[str, Any]:
    global _card_versions
    if _card_versions is None:
        with _lock:
            if _card_versions is None:
                merged = _load_json(CARD_VERSIONS_PATH)
                for path in CARD_VERSION_OVERLAY_PATHS:
                    for resource_id, row in _load_json(path).items():
                        merged.setdefault(str(resource_id), row)
                for resource_id, row in _sbc_reward_card_versions().items():
                    merged[str(resource_id)] = row
                for resource_id, row in _load_json(
                        CARD_VERSION_CORRECTIONS_PATH).items():
                    merged[str(resource_id)] = row
                _card_versions = merged
    return _card_versions


def card_revision(resource_id: int) -> str:
    """Return the card-version name from the extracted database."""
    row = _card_versions_map().get(str(int(resource_id)), {})
    return str(row.get("rev") or "Normal")


def _normalized_icon_name(value: Any) -> str:
    """Return a stable comparison key for an Icon's source-backed name."""
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(character for character in normalized
                   if character.isalnum())


def _prime_icon_identity_map() -> dict[int, dict[str, Any]]:
    """Verify every Prime Icon Moments row against its canonical base Icon.

    Prime Icon Moments resource IDs in the extracted third-party catalogue are
    standalone FIFA 19 card definitions rather than FUT's usual version-encoded
    IDs. They must remain the item ``resourceId``/``definitionId``. The native
    DTO still needs a known player ``assetId`` for name and portrait lookup, so
    it is linked to the uniquely matched highest-rated regular Icon. Keeping
    those two identities separate avoids both anonymous PIM cards and the old
    bug which serialized the regular Icon as the actual item definition.
    """
    global _prime_icon_identity_by_resource
    if _prime_icon_identity_by_resource is not None:
        return _prime_icon_identity_by_resource

    cards = _card_versions_map()
    regular_by_name: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for resource_id, source in cards.items():
        if str(source.get("rev") or "Normal") != "Icon":
            continue
        name_key = _normalized_icon_name(source.get("name"))
        if name_key:
            regular_by_name.setdefault(name_key, []).append(
                (int(resource_id), source))

    resolved: dict[int, dict[str, Any]] = {}
    failures: list[str] = []
    for resource_id, source in cards.items():
        if str(source.get("rev") or "Normal") != "Prime Icon Moments":
            continue
        pim_resource_id = int(resource_id)
        name_key = _normalized_icon_name(source.get("name"))
        candidates = regular_by_name.get(name_key, [])
        if not name_key or not candidates:
            failures.append(f"{pim_resource_id}:{source.get('name')!r}")
            continue
        highest_rating=max(int(candidate.get("rating",0) or 0)
                           for _,candidate in candidates)
        canonical_candidates=[
            item for item in candidates
            if int(item[1].get("rating",0) or 0)==highest_rating
        ]
        canonical_asset_ids={
            int(candidate.get("assetId",candidate_resource_id)
                or candidate_resource_id)
            for candidate_resource_id,candidate in canonical_candidates
        }
        if len(canonical_asset_ids)!=1:
            failures.append(f"{pim_resource_id}:{source.get('name')!r}")
            continue
        canonical_resource_id,canonical=min(
            canonical_candidates,key=lambda item:int(item[0]))
        pim_asset_id=int(source.get("assetId",pim_resource_id)
                         or pim_resource_id)
        if pim_asset_id != pim_resource_id:
            failures.append(f"{pim_resource_id}:{source.get('name')!r}")
            continue
        resolved[pim_resource_id] = {
            "assetId": canonical_asset_ids.pop(),
            "resourceId": pim_resource_id,
            "name": str(source.get("name") or canonical.get("name") or ""),
            "baseIconResourceId": canonical_resource_id,
            "sourceCardAssetId": pim_asset_id,
        }

    if failures:
        raise ValueError("Unresolved Prime Icon Moments identities: " +
                         ", ".join(failures))
    if len(resolved) != 44:
        raise ValueError("Expected 44 Prime Icon Moments identities, got "
                         f"{len(resolved)}")

    with _lock:
        if _prime_icon_identity_by_resource is None:
            _prime_icon_identity_by_resource = resolved
        return _prime_icon_identity_by_resource


def player_presentation_resource_id(resource_id: int) -> int:
    """Return the source-backed regular-Icon alias for a PIM definition.

    FIFA 19's late Prime Icon Moments rows are independent definitions added
    after this November 2018 client's launch player table.  Its UI derives
    ``assetID`` by masking the full player ID, which makes a standalone PIM ID
    resolve to an unrelated base identity.  The runtime compatibility layer
    publishes this uniquely matched regular Prime Icon only as the UI asset
    alias; the wire ``resourceId``/``definitionId`` remain the exact PIM ID so
    rarity 84 and p<PimDefinitionId>.dds still select the Moments shell and
    portrait.
    """
    value = int(resource_id)
    if card_revision(value) != "Prime Icon Moments":
        return value
    identity = _prime_icon_identity_map().get(value)
    if identity is None:
        raise ValueError(f"Missing Prime Icon Moments identity: {value}")
    return int(identity["baseIconResourceId"])


def player_wire_resource_id(resource_id: int) -> int:
    """Return the native-client resource ID for a sourced player definition.

    Ordinary FIFA 19 definitions remain byte-for-byte exact.  A Prime Icon
    Moments definition uses a private compatibility version whose low 24 bits
    are the independently verified launch Icon identity.  This lets the old
    client resolve the real name/profile while keeping the exact PIM definition
    separate for persistence, rarity 84, attributes and portrait remapping.
    """
    value = int(resource_id)
    if card_revision(value) != "Prime Icon Moments":
        return value
    identity = _prime_icon_identity_map().get(value)
    if identity is None:
        raise ValueError(f"Missing Prime Icon Moments identity: {value}")
    asset_id = int(identity["assetId"])
    return PIM_COMPAT_VERSION_INDEX * RESOURCE_VERSION_FACTOR + asset_id


def client_can_name(asset_id: int, revision: str = "Normal") -> bool:
    """Return whether FIFA 19 can resolve this player's own identity.

    The item DTO carries the club, nation, rating, position and rarity that
    the card draws, but not the player. Name, known-as, date of birth, height,
    weak foot and skill moves are resolved by the client from its own shipped
    player database, keyed by the asset id. An asset that database does not
    contain renders with another player's identity: the owner's TOTS Filip
    Kostic (asset 208574, definition 100871870) draws the Eintracht badge, the
    Serbian flag, 93 and LWB from us and is labelled `ALLSOP`.

    `players_db.json` is the players table of the installed `fifa_ng_db.DB`,
    so membership of it is exactly that test. On the transfer-market page the
    owner captured - Bundesliga, club 1824 - five special cards are in it and
    render, and the one card that is not is the one that renders wrong.

    Icons and Prime Icon Moments are exempt: they are absent from that table
    by construction, carry their identity through the FUT icon path, and have
    always rendered their own names.
    """
    if str(revision or "Normal") in ICON_REVISIONS:
        return True
    return str(int(asset_id)) in _players_db_map()


def unnameable_card_versions() -> list[dict[str, Any]]:
    """Return every catalogue card this client would label as someone else."""
    rows = []
    for resource_id, source in _card_versions_map().items():
        revision = str(source.get("rev") or "Normal")
        asset_id = int(source.get("assetId", 0) or 0)
        if client_can_name(asset_id, revision):
            continue
        row = dict(source)
        row["resourceId"] = int(resource_id)
        row["revision"] = revision
        rows.append(row)
    rows.sort(key=lambda row: (-int(row.get("rating", 0) or 0),
                               int(row["resourceId"])))
    return rows


def card_version_rows(revisions: str | set[str] | tuple[str, ...] | None = None,
                      min_rating: int = 0,
                      max_rating: int = 99) -> list[dict[str, Any]]:
    """Return exact, source-backed FUT card versions in a stable order.

    Offline opponent and Draft squads must use real definition IDs from the
    extracted FIFA 19 catalogue.  Exposing a read-only projection here avoids
    duplicating the private JSON loader in the HTTP layer and, importantly,
    prevents either mode from fabricating resource IDs.
    """
    if revisions is None:
        accepted = None
    elif isinstance(revisions, str):
        accepted = {revisions}
    else:
        accepted = {str(value) for value in revisions}
    lower = max(0, int(min_rating))
    upper = max(lower, int(max_rating))
    rows = []
    for resource_id, source in _card_versions_map().items():
        revision = str(source.get("rev") or "Normal")
        rating = int(source.get("rating", 0) or 0)
        if accepted is not None and revision not in accepted:
            continue
        if rating < lower or rating > upper:
            continue
        if not client_can_name(int(source.get("assetId", 0) or 0), revision):
            # Never offer a card the client would label as another player.
            # This is the single projection Draft, packs, the market, Squad
            # Battles and the offline opponents all draw from.
            continue
        row = dict(source)
        row["resourceId"] = int(resource_id)
        row["revision"] = revision
        if revision == "Prime Icon Moments":
            identity = _prime_icon_identity_map().get(int(resource_id))
            if identity is None:
                raise ValueError(f"Missing Prime Icon Moments identity: {resource_id}")
            row["assetId"] = int(identity["assetId"])
            row["name"] = str(identity["name"])
        if revision in ICON_REVISIONS:
            row["leagueId"] = ICON_LEAGUE_ID
            icon_team_id = int(source.get("club", 0) or 0)
            if icon_team_id:
                row["teamid"] = icon_team_id
                row["teamId"] = icon_team_id
        rows.append(row)
    rows.sort(key=lambda row:(-int(row.get("rating",0) or 0),
                              int(row["resourceId"])))
    return rows


def card_rarity_id(resource_id: int, default: int = 1) -> int:
    return int(CARD_REVISION_RARITY.get(card_revision(resource_id), default))


def card_discard_value(rating: int, revision: str = "Normal") -> int:
    """Return the FIFA 19 coin quick-sell value for a player card.

    FUT 19 applies rating-based multipliers to special revisions.  Local FUT
    keeps a 10,000-coin floor for IF and other event cards so no special item
    is valued below the project requirement.
    """
    value=max(0,int(rating or 0))
    normalized_revision=str(revision or "Normal").strip()
    if normalized_revision == "Icon":
        return value * 1200
    if normalized_revision == "TOTY":
        return value * 800
    if normalized_revision == "TOTS":
        return value * 240
    if normalized_revision not in ("", "Normal"):
        return max(10000,value * 122)
    return value * 8


# A league name has to be carried by this many players before its display id
# is trusted. Below it the join is noise rather than a league.
_LEAGUE_MIN_VOTES = 5


def _league_map() -> dict[str, int]:
    """Map card-version league names to display league IDs.

    Built by joining card_versions league names with internal players_db
    league IDs (+1 enters the display-ID space).
    """
    global _league_name2id
    if _league_name2id is not None:
        return _league_name2id
    # Load outside the lock: _lock is not reentrant and both loaders acquire it
    # themselves, which would deadlock on the first call.
    pdb = _players_db_map()
    cards = _card_versions_map()
    with _lock:
        if _league_name2id is not None:
            return _league_name2id
        votes: dict[str, dict[int, int]] = {}
        for rid, cv in cards.items():
            if cv.get("rev") not in ("Normal", ""):
                continue
            name = cv.get("league")
            row = pdb.get(str(cv.get("assetId")))
            if not name or not row or not row.get("league"):
                continue
            disp = int(row["league"]) + 1  # internal ID -> display ID
            votes.setdefault(name, {}).setdefault(disp, 0)
            votes[name][disp] += 1
        # Taking each name's best id independently let two leagues win the
        # same one: a league with few players could outvote nobody yet still
        # claim an id already earned by a much larger league. Live on
        # 2026-09-03 that put Russian players in an Allsvenskan search - both
        # resolved to 56 - and there were four such collisions in all.
        #
        # Resolve strongest evidence first and give each display id to one
        # league only; a league whose best id is taken falls to its next best.
        claims = sorted(
            ((count, name, display)
             for name, options in votes.items()
             for display, count in options.items()),
            key=lambda claim: (-claim[0], claim[1], claim[2]))
        resolved: dict[str, int] = {}
        taken: dict[int, str] = {}
        for _count, name, display in claims:
            if name in resolved or display in taken:
                continue
            resolved[name] = display
            taken[display] = name
        # A claim resting on one or two players is not evidence, it is a
        # mis-joined row: "League of Russia" had a single vote for 56 against
        # Allsvenskan's 346, and "South African FL" a single vote for 68
        # against Sueper Lig's 391. Assigning such a league any id at all is
        # what put foreign players in another league's search results, so
        # leave it unmapped instead - a league nobody can filter by is far
        # less confusing than a league full of the wrong players.
        confident = {}
        for name, display in resolved.items():
            options = votes[name]
            support = options[display]
            if support >= _LEAGUE_MIN_VOTES and                     support * 2 >= sum(options.values()):
                confident[name] = display
        _league_name2id = confident
        return _league_name2id


def _enrich_from_data(result: dict[str, Any], resource_id: int, asset_id: int) -> None:
    """Fill position, league, nation, club, rating, and extracted attributes.

    Prefer card_versions.json (exact version and display IDs), then fall back
    to players_db.json (base player). Preserve non-default values.
    """
    cv = _card_versions_map().get(str(resource_id))
    pdb = _players_db_map().get(str(asset_id))
    if not cv and not pdb:
        return

    if not result.get("preferredPosition"):
        pos = (cv or {}).get("pos") or (pdb or {}).get("pos")
        if pos:
            result["preferredPosition"] = pos

    if not result.get("nation"):
        nation = (cv or {}).get("nation") or (pdb or {}).get("nation")
        if nation:
            result["nation"] = int(nation)

    if not result.get("teamid") and not result.get("teamId"):
        club = (cv or {}).get("club") or (pdb or {}).get("club")
        if club:
            result["teamid"] = result["teamId"] = int(club)

    if not result.get("leagueId"):
        league = 0
        if cv and cv.get("league"):
            league = _league_map().get(cv["league"], 0)
        if not league and pdb and pdb.get("league"):
            league = int(pdb["league"]) + 1
        if league:
            result["leagueId"] = int(league)

    # card_versions.json is keyed by the exact resourceId.  A special card
    # must therefore override the base-player values supplied by players.json;
    # keeping the base rating/attributes makes IF and other revisions display
    # as their normal card and can also make the native item validator reject
    # the object when it is moved from the unassigned pile.
    if cv and cv.get("rating"):
        result["rating"] = int(cv["rating"])
        result["rareflag"] = 1 if int(cv["rating"]) >= 75 else 0
        result["discardValue"] = card_discard_value(
            int(cv["rating"]), str(cv.get("rev") or "Normal"))

    if cv:
        revision = str(cv.get("rev") or "Normal")
        if revision != "Normal":
            result["rareflag"] = CARD_REVISION_RARITY.get(revision, 1)
        result["cardRevision"] = revision

    if cv:
        stats = [cv.get("pac"), cv.get("sho"), cv.get("pas"),
                 cv.get("dri"), cv.get("def"), cv.get("phy")]
        if any(stats):
            result["attributeList"] = [{"index": i, "value": int(v or 0)}
                                       for i, v in enumerate(stats)]

    # The native payload uses these scalar fields in addition to the six FUT
    # attributes. Source metadata stores them as sm/wf; retain safe defaults.
    if cv:
        if cv.get("sm") is not None:
            result["skillmoves"] = int(cv["sm"] or 0)
        if cv.get("wf") is not None:
            result["weakfootabilitytypecode"] = int(cv["wf"] or 0)
        if str(cv.get("rev") or "Normal") in ICON_REVISIONS:
            result["leagueId"] = ICON_LEAGUE_ID


def native_player_fields(resource_id: int, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    resource_id = int(resource_id)
    revision = card_revision(resource_id)
    pim_identity = None
    if revision == "Prime Icon Moments":
        pim_identity = _prime_icon_identity_map().get(resource_id)
        if pim_identity is None:
            raise ValueError(f"Missing Prime Icon Moments identity: {resource_id}")
        asset_id = int(pim_identity["assetId"])
    else:
        asset_id = base_asset_id(resource_id)
    entry = player(asset_id) or {}
    rating = int(entry.get("r", 0) or 0)
    result: dict[str, Any] = {
        "resourceId": resource_id,
        "assetId": asset_id,
        "definitionId": resource_id,
        "resourceGameYear": 2019,
        "rating": rating,
        "rareflag": 1 if rating >= 75 else 0,
        "itemState": "free",
        "itemType": "player",
        "cardsubtypeid": PLAYER_CARD_SUBTYPE,
        "formation": "f433",
        "preferredPosition": "",
        "leagueId": 0,
        "nation": 0,
        "teamid": 0,
        "teamId": 0,
        "contract": 7,
        "contracts": 7,
        "fitness": 99,
        "morale": 50,
        "injuryType": "none",
        "injuryGames": 0,
        "suspension": 0,
        "training": 0,
        "trainingId": 0,
        "trainingResourceId": 0,
        "playStyle": 250,
        "owners": 1,
        "untradeable": False,
        "tradeable": True,
        "discardValue": card_discard_value(rating),
        "lastSalePrice": 0,
        "loyaltyBonus": 1,
        "marketDataMinPrice": 150,
        "marketDataMaxPrice": 15_000_000,
        "attributeList": [],
        "statsList": [],
        "lifetimeStats": [],
        "skillmoves": 0,
        "weakfootabilitytypecode": 0,
        "attackingworkrate": 0,
        "defensiveworkrate": 0,
        "trait1": 0,
        "trait2": 0,
        "preferredfoot": 1,
        "posMods": [],
        "name": display_name(entry) if entry else "",
    }
    if overrides:
        result.update(overrides)
    # Enrich after overrides: SQLite items can arrive with empty position,
    # nation, league, and club values. Fill only default fields so persisted
    # items gain catalogue data without losing saved state.
    _enrich_from_data(result, resource_id, asset_id)
    if pim_identity is not None:
        pim_row = _card_versions_map().get(str(resource_id), {})
        result["assetId"] = asset_id
        result["resourceId"] = resource_id
        result["definitionId"] = resource_id
        result["name"] = str(pim_identity["name"])
        result["preferredPosition"] = str(pim_row.get("pos") or "")
    if revision in ICON_REVISIONS:
        result["leagueId"] = ICON_LEAGUE_ID
        icon_source = _card_versions_map().get(str(resource_id), {})
        icon_team_id = int(icon_source.get("club", 0) or 0)
        if icon_team_id:
            # Icon/PIM club identity belongs to the exact source-backed card
            # revision.  Do this after saved overrides so a stale ordinary
            # player team cannot turn a PIM item into the wrong identity.
            result["teamid"] = icon_team_id
            result["teamId"] = icon_team_id
    # Pack generation and the starter catalogue explicitly distinguish common
    # from rare normal cards. card_versions.json does not carry that normal-card
    # rarity bit, so retain the caller's value for Normal revisions only. Exact
    # special revisions still own their rarity, rating and boosted attributes.
    if (overrides and "rareflag" in overrides and
            revision == "Normal"):
        result["rareflag"] = int(overrides["rareflag"] or 0)
    # Reward-only presentation metadata can apply an EA card shell to a sourced
    # performance definition.  Squad Battles red picks use this to retain the
    # exact IF/SIF/TIF stats and identity while rendering the official rarity
    # 18 CHAMPION_REWARD design on every subsequent load from SQLite.
    if overrides and int(overrides.get("presentationRareflag",0) or 0)>0:
        result["presentationRareflag"]=int(overrides["presentationRareflag"])
        result["rareflag"]=int(overrides["presentationRareflag"])
        result["presentationRevision"]=str(overrides.get(
            "presentationRevision","FUT Champions") or "FUT Champions")
        result["cardRevision"]=result["presentationRevision"]
    result["teamId"] = int(result.get("teamid", result.get("teamId", 0)) or 0)
    result["teamid"] = result["teamId"]
    result["tradeable"] = not bool(result.get("untradeable", False))
    if not result["tradeable"]:
        # FUT 19 never assigns a quick-sell value to an untradeable item.
        # Normalising this at the catalogue boundary also repairs persisted
        # reward/pack DTOs that were written before the flag was enforced.
        result["discardValue"] = 0
    return result


def validate_starter_catalogue() -> list[int]:
    return [int(row["resourceId"]) for row in STARTER_PLAYERS if player(int(row["resourceId"])) is None]


def validate_initial_bonus_catalogue() -> list[int]:
    """Return bonus definitions that are missing or in the wrong quality."""
    invalid = []
    for row in INITIAL_BONUS_PLAYERS:
        resource_id = int(row["resourceId"])
        fields = native_player_fields(resource_id)
        rating = int(fields.get("rating", 0) or 0)
        expected = str(row["quality"])
        actual = "bronze" if rating < 65 else "silver" if rating < 75 else "gold"
        if not player(resource_id) or card_revision(resource_id) != "Normal" or actual != expected:
            invalid.append(resource_id)
    return invalid

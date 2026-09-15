"""Deterministic Single Player Seasons catalogue and progression rules.

The structure follows the retail FIFA 19 single-player Seasons tables: a
Season is ten matches long, every division offers its own competitions, and
the title/promotion/holding thresholds and rewards belong to the pair
(division, competition). Division 10 offers the difficulty tiers and Division
1 the leagues shown in the retail carousel.
"""

from __future__ import annotations

import base64
import binascii
import math
import time

from fut_sbc import sbc_preview_reward_dto


# Retail single-player Seasons are ten matches: the authentic thresholds (12
# points for the title in Division 10, 28 in Division 1, 30 for Ultimate
# League) only fit ten results, and the client has always displayed ten
# remaining games.
MATCH_COUNT = 10

# The per-Season client blob FIFA sends on every fresh enrollment: length 16,
# version 0x106, zero matches, zero points, no player records.
EMPTY_SEASON_DATA = "EAAAAAYBAAAAAAAAAAAAAAAAAAA="

# One id base per carousel slot. A Season id stays `base + division`, so every
# id published before this table is still resolvable.
_SLOT_BASES = (19_000, 29_000, 39_000, 49_000, 59_000, 69_000, 79_000)

# Reward packs named by the retail tables, mapped to the pack definitions this
# server actually ships. Where retail names a pack we do not define (gift,
# contract and jumbo bronze/silver variants), the closest shipped pack of the
# same tier is used.
_BRONZE = 100
_PREMIUM_BRONZE = 101
_BRONZE_PLAYERS = 104
_SILVER = 200
_PREMIUM_SILVER = 201
_GOLD = 300
_PREMIUM_GOLD = 301
_JUMBO_PREMIUM_GOLD = 303
_CONSUMABLES = 504

# (name, stars, titlePoints, promotionPoints, titleCoins, titlePack,
#  promotionCoins, holdingCoins) in carousel slot order.
_DIVISION_COMPETITIONS = {
    10: (
        ("Beginner", 0.5, 12, 9, 2500, 0, 2000, 300),
        ("Amateur", 0.5, 12, 9, 2600, 0, 2100, 300),
        ("Semi-Pro", 1.5, 12, 9, 1100, _BRONZE, 2000, 300),
        ("Professional", 1.5, 12, 9, 3000, 0, 2400, 300),
        ("World Class", 2.5, 12, 9, 3700, 0, 3000, 300),
        ("Legendary", 3.5, 12, 9, 4100, 0, 3300, 300),
        ("Ultimate", 5.0, 12, 9, 4600, 0, 3700, 300),
    ),
    9: (
        ("World Tour", 1.0, 17, 14, 1300, 0, 1000, 800),
        ("Chinese Super League", 1.0, 14, 11, 1500, 0, 1200, 900),
        ("Liga Dimayor", 1.0, 14, 11, 1400, _PREMIUM_BRONZE, 1400, 1100),
        ("Ultimate League", 0.5, 14, 11, 1900, 0, 1500, 1200),
    ),
    8: (
        ("World Tour", 0.5, 16, 13, 1700, _BRONZE, 1000, 800),
        ("EFL League Two", 0.5, 16, 13, 1500, 0, 1200, 900),
        ("A-League", 1.0, 16, 13, 1800, 0, 1400, 1100),
        ("Ultimate League", 2.0, 16, 13, 2400, _PREMIUM_BRONZE, 1800, 1400),
    ),
    7: (
        ("World Tour", 1.0, 23, 20, 2300, _CONSUMABLES, 1900, 1400),
        ("Allsvenskan", 1.0, 18, 15, 2200, _BRONZE, 2100, 1500),
        ("Liga 123", 1.5, 18, 15, 2300, _SILVER, 1800, 1300),
        ("Scotland Premiership", 1.0, 18, 15, 3300, 0, 2600, 1900),
        ("Ultimate League", 1.5, 18, 15, 3150, _BRONZE, 3000, 2200),
    ),
    6: (
        ("World Tour", 1.0, 23, 20, 2500, _SILVER, 2000, 1400),
        ("Football League 1", 1.0, 20, 17, 2700, _BRONZE, 1900, 1300),
        ("Polska Liga", 1.0, 20, 17, 2400, _SILVER, 1900, 1400),
        ("Superliga", 1.5, 20, 17, 3800, 0, 2700, 1900),
        ("Ultimate League", 2.5, 20, 17, 2000, _SILVER, 3200, 2300),
    ),
    5: (
        ("World Tour", 1.0, 25, 22, 1800, _CONSUMABLES, 1900, 1200),
        ("Raiffeisen SL", 1.0, 22, 19, 2500, _CONSUMABLES, 2200, 1400),
        ("Ligue 2", 1.0, 22, 19, 2700, _SILVER, 2000, 1500),
        ("Tippeligaen", 1.0, 22, 19, 4700, 0, 2700, 2400),
        ("Ultimate League", 2.5, 22, 19, 1550, _PREMIUM_SILVER, 3900, 2600),
    ),
    4: (
        ("World Tour", 1.5, 27, 24, 2000, _CONSUMABLES, 2900, 1700),
        ("A. Bundesliga", 1.5, 24, 21, 2800, _GOLD, 2200, 1300),
        ("Calcio B", 1.5, 24, 21, 2800, _GOLD, 2200, 1300),
        ("Ultimate League", 3.0, 24, 21, 2300, _PREMIUM_SILVER, 4400, 2700),
    ),
    3: (
        ("World Tour", 2.0, 27, 24, 2100, _CONSUMABLES, 2600, 1500),
        ("MLS", 2.0, 26, 23, 2300, _CONSUMABLES, 2500, 1600),
        ("2. Bundesliga", 2.0, 26, 23, 3300, _CONSUMABLES, 3100, 1900),
        ("League Championship", 2.0, 26, 23, 7300, 0, 5500, 3400),
        ("Ultimate League", 3.0, 24, 21, 2300, _PREMIUM_SILVER, 4700, 3000),
    ),
    2: (
        ("World Tour", 2.5, 28, 26, 2900, _CONSUMABLES, 3200, 1900),
        ("Eredivisie", 2.5, 26, 23, 3600, _BRONZE_PLAYERS, 4000, 2400),
        ("Liga Nos", 2.5, 26, 23, 4100, _PREMIUM_BRONZE, 4200, 2600),
        ("Ultimate League", 4.5, 26, 23, 4000, _GOLD, 6700, 4100),
    ),
    1: (
        ("World Tour", 3.0, 28, 28, 3500, _SILVER, 0, 2600),
        ("Bundesliga", 3.0, 28, 28, 2000, _PREMIUM_GOLD, 0, 4400),
        ("Superliga Argentina", 3.0, 28, 28, 2000, _PREMIUM_GOLD, 0, 4300),
        ("Serie A TIM", 3.0, 28, 28, 2000, _PREMIUM_GOLD, 0, 4300),
        ("Premier League", 3.5, 28, 28, 2000, _PREMIUM_GOLD, 0, 4500),
        ("International", 4.5, 28, 28, 2000, _PREMIUM_GOLD, 0, 4900),
        ("Ultimate League", 5.0, 30, 30, 0, _JUMBO_PREMIUM_GOLD, 0, 6200),
    ),
}

# Retail shows the relegation-safety line per division; Division 1 is the
# confirmed one at 15 points, and the ladder below it keeps one point per
# division.
_MAINTENANCE_POINTS = {division: 15 - (division - 1)
                       for division in range(1, 11)}

# Opponent clubs come from the local catalogue, so a competition uses its own
# league pool when this server ships one and the mixed pool otherwise.
_MIXED_CLUBS = (3, 4, 89, 106, 1790, 2, 12, 15, 1792, 1960,
                7, 22, 47, 461, 481, 10, 21, 73, 241, 243)
# `fut_sqbt.star_rating` puts exactly these six clubs in the five-star band
# of the extracted catalogue: Bayern, Real Madrid, Barcelona, Juventus,
# Manchester City and Paris Saint-Germain.  The Ultimate competitions field
# only them, so ten fixtures repeat opponents; that is what "five-star teams
# only" means with six eligible clubs.
_FIVE_STAR_CLUBS = (21, 243, 241, 45, 10, 73)
_LEAGUE_CLUBS = {
    "Ultimate": _FIVE_STAR_CLUBS,
    "Ultimate League": _FIVE_STAR_CLUBS,
    "Bundesliga": (21, 22, 32, 34, 23, 10029, 112172, 36, 175, 38,
                   166, 1824, 25, 169, 100409),
    "Superliga Argentina": (1877, 1876, 101085, 1013, 110580, 101084, 110396,
                            110406, 112670, 110581, 111706, 111716, 111715,
                            111714, 111713),
    "Serie A TIM": (45, 48, 52, 46, 47, 110374, 189, 1837, 111974, 192,
                    1842, 112791, 55, 50, 1746),
    "Premier League": (10, 11, 18, 9, 1, 5, 7, 19, 95, 110,
                       1795, 1796, 17, 1799, 1808),
}


def _split(season_id):
    """Return ``(base, division)`` for a published Season id."""
    division = int(season_id) % 100
    return int(season_id) - division, division


def _row(season_id):
    """Return the competition row a Season id belongs to."""
    base, division = _split(season_id)
    rows = _DIVISION_COMPETITIONS.get(division)
    if rows is None or base not in _SLOT_BASES:
        raise ValueError("unknown offline competition")
    slot = _SLOT_BASES.index(base)
    if slot >= len(rows):
        raise ValueError("unknown offline competition")
    return rows[slot]


def _competition(season_id=None):
    """Return ``(base, name, clubs, matchCount)`` for one Season id."""
    if season_id is None:
        return _SLOT_BASES[0], _DIVISION_COMPETITIONS[10][0][0], \
            _MIXED_CLUBS, MATCH_COUNT
    base, _division = _split(season_id)
    name = _row(season_id)[0]
    return base, name, _clubs(name), MATCH_COUNT


def _clubs(name):
    return _LEAGUE_CLUBS.get(str(name), _MIXED_CLUBS)


def _season_id(division, competition=None):
    base = (_SLOT_BASES[0] if competition is None
            else _split(competition)[0])
    return base + int(division)


def _slots(division):
    return _DIVISION_COMPETITIONS[int(division)]


def _award_mapping(value, pack_id, promotes=False):
    awards = []
    if int(value) > 0:
        awards.append({"type": "coin", "value": int(value), "count": 1})
    if int(pack_id) > 0:
        awards.append({"type": "pack", "value": int(pack_id),
                       "halId": int(pack_id), "count": 1})
    if promotes:
        awards.append(sbc_preview_reward_dto({
            "type": "playerPick", "value": 0, "count": 2,
            "optionCount": 4, "label": "FUT Champions Player Pick",
            "description": "Choose one untradeable FUT Champions TOTW player.",
            "untradeable": True,
        }))
    return {"timesWon": 0, "awards": awards}


def _rules(division, season_id=None):
    """Return ``(title, promotion, maintenance, rewards)`` for one Season."""
    row = (_slots(division)[0] if season_id is None else _row(season_id))
    promotion = None if int(division) == 1 else int(row[3])
    rewards = (int(row[4]), int(row[6]), int(row[7]), 0)
    return int(row[2]), promotion, _MAINTENANCE_POINTS[int(division)], rewards


def _packs(season_id):
    """Return the pack published with each finish band."""
    row = _row(season_id)
    return int(row[5]), 0, 0, 0


def _prizes(division, season_id=None):
    title, promotion, maintenance, rewards = _rules(division, season_id)
    packs = _packs(season_id if season_id is not None
                   else _season_id(division))
    rows = [
        {"prizeLevel": "CHAMPIONSHIP", "thresholdPoint": title,
         "awardMappings": [_award_mapping(
             rewards[0], packs[0], promotes=int(division) > 1)]},
    ]
    if promotion is not None:
        rows.append({
            "prizeLevel": "PROMOTION", "thresholdPoint": promotion,
            "awardMappings": [_award_mapping(
                rewards[1], packs[1], promotes=True)],
        })
    rows.append({
        "prizeLevel": "MAINTENANCE", "thresholdPoint": maintenance,
        "awardMappings": [_award_mapping(rewards[2], packs[2])],
    })
    rows.append({
        "prizeLevel": "RELEGATION", "thresholdPoint": 0,
        "awardMappings": [_award_mapping(rewards[3], packs[3])],
    })
    return rows


def _difficulty(stars):
    """Map the carousel star rating to the client's six difficulty levels."""
    return max(1, min(6, int(math.ceil(float(stars)))))


def _matches(division, season_id):
    row = _row(season_id)
    clubs = _clubs(row[0])
    base, _division = _split(season_id)
    offset = (_SLOT_BASES.index(base) * 3 + (10 - int(division))) % len(clubs)
    difficulty = _difficulty(row[1])
    return [{
        "roundId": round_id,
        "teamId": clubs[(offset + round_id) % len(clubs)],
        "difficulty": difficulty,
        "rewardMult": 1,
        "coins": 0,
    } for round_id in range(MATCH_COUNT)]


def season_catalog(now=None, divisions=None):
    """Return the closed ``FutSeasonListServerResponse`` wire shape."""
    end = int(time.time() if now is None else now) + 10 * 365 * 24 * 60 * 60
    selected = tuple(range(10, 0, -1) if divisions is None else
                     (int(value) for value in divisions))
    if any(division not in _DIVISION_COMPETITIONS for division in selected):
        raise ValueError("division must be between 1 and 10")
    rows = []
    for division in selected:
        for slot, _row_data in enumerate(_slots(division)):
            season_id = _SLOT_BASES[slot] + division
            rows.append({
                "id": season_id,
                "divisionId": division,
                "type": "OFFLINE",
                # CardsDLL+0x28c164 maps this proved member to the event-card
                # asset used by +0x1c8a76.
                "trophyResourceId": season_id,
                "endDateTime": end,
                "numMatches": MATCH_COUNT,
                "matchLengthMin": 6,
                "matches": _matches(division, season_id),
                "prizeSet": _prizes(division, season_id),
            })
    return {"seasons": rows}


# `CardsDLL+0x1c81fe` adds 0x91 to the parsed `tournamentType` and formats
# `TOURNY_LOC_%d` for 0x91-0x94, `SEASON_LOC_%d` for 0x95-0x96 and no name at
# all outside that band, with `tournamentId` as the `%d`.  Four is the lowest
# value that selects the Season key.
SEASON_ASSET_TYPE = 4


# Do not add members to this document.  Only `tournamentId` and
# `tournamentType` are safe; three live sessions measured every other
# attempt by counting the asset documents the client asks for, and any
# `locString` costs the screen:
#
# | Session      | `locString`                   | Documents | Screen |
# | `...162124`  | absent                        | 7         | drew   |
# | `...164032`  | scalar, keys 18-23 characters | 4         | froze  |
# | `...165306`  | scalar, keys 13-18 characters | 4         | froze  |
# | `...172001`  | one-element collection        | 4         | froze  |
#
# Neither the key length nor the JSON type is the variable, so the append
# loop at `CardsDLL+0x1c8120` is not driven by the shapes this server can
# express.  `assetName` and `silName` reach the same buffer family and stay
# withheld with it.
#
# The consequence is that the card renders `*SEASON_LOC_<id>`: it resolves
# that key in the game's own localization database, which the FUT locstring
# overlay this server serves does not reach, and our Season ids are
# invented - RC140 recorded that the original local content archive answers 404 for
# `19010.json`.  Naming these cards needs the retail Season ids or the
# Frostbite localization bundles behind `Data/Win32/loc/en.toc`, and FIFA19
# itself is packed (`.xcode`, `.xpdata`), so neither is reachable from here.


def season_asset(season_id):
    """Return the card asset document for one Season id, or ``None``.

    The carousel resolves every row of `season/list` through
    `fut/items/pc/<trophyResourceId>.json`.  Answering 404 left
    `tournamentId` and `tournamentType` unset, so the card formatted no
    localization key and rendered the placeholder trophy without a name.
    Only those two members decide the name; `assetName`, `silName` and
    `locString` select trophy artwork this server does not mirror, so they
    stay out and the placeholder art remains.
    """
    try:
        _row(int(season_id))
    except (ValueError, KeyError):
        return None
    return {
        "tournamentId": int(season_id),
        "tournamentType": SEASON_ASSET_TYPE,
    }


def season_localizations():
    """Names consumed by the native Season and Tournament tile formatters."""
    return tuple((form % (_SLOT_BASES[slot] + division), row[0])
                 for division, rows in _DIVISION_COMPETITIONS.items()
                 for slot, row in enumerate(rows)
                 for form in ("SEASON_LOC_%d", "TOURNY_LOC_%d"))


def competition_stars(season_id):
    """Return the carousel difficulty rating of one competition."""
    return float(_row(season_id)[1])


def new_season(division=10, season_id=None):
    division = int(division)
    if division not in _DIVISION_COMPETITIONS:
        raise ValueError("division must be between 1 and 10")
    season_id = (_season_id(division) if season_id is None else int(season_id))
    if season_id != _season_id(division, season_id):
        raise ValueError("season does not belong to division")
    _row(season_id)
    return {
        "active": True,
        "complete": False,
        "divisionId": division,
        "seasonId": season_id,
        "round": 0,
        "userPoints": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "results": [],
        "operationKeys": [],
        "dataVersion": 3,
        "entered": False,
        "rewardClaimed": False,
    }


def outcome(division, points, season_id=None):
    title, promotion, maintenance, rewards = _rules(division, season_id)
    if int(points) >= title:
        return "CHAMPIONSHIP", rewards[0]
    if promotion is not None and int(points) >= promotion:
        return "PROMOTION", rewards[1]
    if int(points) >= maintenance:
        return "MAINTENANCE", rewards[2]
    return "RELEGATION", rewards[3]


def record_result(state, result, operation_key):
    """Apply one result once and return ``(new_state, created_now)``."""
    value = dict(state)
    key = str(operation_key or "")
    if not key:
        raise ValueError("operation_key is required")
    if key in value.get("operationKeys", []):
        return value, False
    if value.get("complete") or int(value.get("round", 0)) >= MATCH_COUNT:
        raise ValueError("season is complete")
    normalized = str(result or "").upper()
    if normalized == "LOSE":
        normalized = "LOSS"
    if normalized not in ("WIN", "DRAW", "LOSS"):
        raise ValueError("result must be WIN, DRAW or LOSS")
    value.setdefault("results", []).append(normalized)
    value.setdefault("operationKeys", []).append(key)
    value[normalized.lower() + ("es" if normalized == "LOSS" else "s")] = (
        int(value.get(normalized.lower() +
                      ("es" if normalized == "LOSS" else "s"), 0)) + 1)
    value["round"] = int(value.get("round", 0)) + 1
    value["userPoints"] = int(value.get("userPoints", 0)) + {
        "WIN": 3, "DRAW": 1, "LOSS": 0,
    }[normalized]
    value["dataVersion"] = int(value.get("dataVersion", 3) or 3)
    if value["round"] == MATCH_COUNT:
        level, coins = outcome(value["divisionId"], value["userPoints"],
                               value["seasonId"])
        division = int(value["divisionId"])
        next_division = division
        if level in ("CHAMPIONSHIP", "PROMOTION") and division > 1:
            next_division -= 1
        elif level == "RELEGATION" and division < 10:
            next_division += 1
        value.update({"complete": True, "seasonEndResult": level,
                      "rewardCoins": coins, "nextDivision": next_division})
    return value, True


def season_user_payload(state):
    """Return the closed ``FutSeasonLoadDataServerResponse`` wire shape."""
    value = dict(state)
    if not value.get("entered"):
        return {}
    payload = {
        "dataVersion": int(value.get("dataVersion", 1)),
        "divisionId": int(value["divisionId"]),
        # The native parser stores wire round minus one. Send the next match
        # number while persistence continues to store completed matches.
        "round": int(value.get("round", 0)) + 1,
        "seasonId": int(value["seasonId"]),
        "userPoints": int(value.get("userPoints", 0)),
    }
    if isinstance(value.get("userData"), str) and value["userData"]:
        payload["data"] = value["userData"]
    return payload


def season_history_payload(state, totals=None):
    """Return the mapped offline history record required after enrollment."""
    value = dict(state)
    totals = dict(totals or {})
    return {
        "bestPointsSeasonId": int(totals.get(
            "bestPointsSeasonId", value["seasonId"])),
        "bestPointsSeasonValue": max(
            int(totals.get("bestPointsSeasonValue", 0)),
            int(value.get("userPoints", 0))),
        # The hub renders these two counts under the opposite labels.  Live
        # 2026-09-12 the account held titles 0, completed 2 and relegations 2
        # and the SEASON HISTORY panel showed "Titles Won 2, Seasons
        # Completed 0", so the trophy does not read `seasonTitlesWon`.  Of
        # the members that carried 2, `seasonCompleted` is the only one whose
        # label pairs with the zero shown opposite it.  Publish each count
        # under the member its label reads; if a later session still shows
        # a title for a relegated Season, the trophy reads
        # `seasonRelegations` instead and this pair goes back as it was.
        "seasonCompleted": int(totals.get("titles", 0)),
        "seasonGamesDraw": int(totals.get("draws", 0)),
        "seasonGamesLost": int(totals.get("losses", 0)),
        "seasonGamesWon": int(totals.get("wins", 0)),
        "seasonPromotions": int(totals.get("promotions", 0)),
        "seasonRelegations": int(totals.get("relegations", 0)),
        "seasonTitlesWon": int(totals.get("completed", 0)),
        "seasonConcededGoals": int(totals.get("goalsAgainst", 0)),
        "seasonsScoredGoals": int(totals.get("goalsFor", 0)),
        "type": "offline",
    }


def season_hub_payload(state):
    """Return the native Hub offline-season record after enrollment."""
    value = dict(state)
    progress = value.get("progressData")
    if not value.get("entered") or not isinstance(progress, str) or not progress:
        return None
    version = int(value.get("progressDataVersion", 3) or 3)
    try:
        decoded = base64.b64decode(progress, validate=True)
    except (ValueError, binascii.Error):
        return None
    if (version != 3 or len(decoded) != 7 or
            int.from_bytes(decoded[:4], "little") != 3):
        return None
    return {
        "divisionId": int(value["divisionId"]),
        "gamesPlayed": int(value.get("round", 0)),
        "points": int(value.get("userPoints", 0)),
        "progressdata": progress,
        "progressDataVersion": version,
        "totalGames": MATCH_COUNT,
    }


def claim_reward(state):
    """Mark a completed reward claimed, idempotently."""
    value = dict(state)
    if not value.get("complete"):
        raise ValueError("season is not complete")
    level = str(value["seasonEndResult"])
    reward_index = {
        "CHAMPIONSHIP": 0,
        "PROMOTION": 1,
        "MAINTENANCE": 2,
        "RELEGATION": 3,
    }[level]
    pack_id = _packs(value["seasonId"])[reward_index]
    receipt = {
        "seasonId": int(value["seasonId"]),
        "divisionId": int(value["divisionId"]),
        "seasonEndResult": level,
        "coins": int(value["rewardCoins"]),
        "packId": int(pack_id),
        "nextDivision": int(value["nextDivision"]),
    }
    if value.get("rewardClaimed"):
        return value, receipt, False
    value["rewardClaimed"] = True
    return value, receipt, True


def next_season(state):
    if not state.get("complete") or not state.get("rewardClaimed"):
        raise ValueError("completed reward must be claimed first")
    next_division = int(state["nextDivision"])
    base = _split(int(state["seasonId"]))[0]
    slot = _SLOT_BASES.index(base)
    # A competition slot can be absent from the next division, so the closest
    # remaining slot keeps the player in the same carousel position.
    slot = min(slot, len(_slots(next_division)) - 1)
    value = new_season(next_division, _SLOT_BASES[slot] + next_division)
    # FIFA re-enters directly after Advance and sends no new enrollment PUT,
    # so the enrollment carries over. The client blob holds this Season's
    # player records and FIFA appends to whatever it is given, so the new
    # Season gets the empty container every enrollment sends. The progress
    # blob encodes the division thresholds and is kept only when the division
    # is unchanged.
    keys = ["entered", "dataVersion"]
    if next_division == int(state["divisionId"]):
        keys += ["progressData", "progressDataVersion"]
    value.update({key: state[key] for key in keys if key in state})
    if value.get("entered"):
        value["userData"] = EMPTY_SEASON_DATA
    return value

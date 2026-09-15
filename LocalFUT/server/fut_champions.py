#!/usr/bin/env python3
"""Deterministic domain model for the local twenty-match FUT Champions run."""

from __future__ import annotations

import random
from collections import Counter
from functools import lru_cache
from typing import Any

from fut_catalog import card_version_rows
from fut_objects import default_grant, object_definition


MATCH_COUNT = 20
LEADERBOARD_SIZE = 100
NUMBER_OF_WINS_TIER_TYPE = "NUMBER_OF_WINS_TYPE"

DIFFICULTIES = (
    {"id": 1, "name": "BEGINNER", "coinMultiplier": 0.50,
     "playerPickOptions": 2, "bonusPackId": 0},
    {"id": 2, "name": "AMATEUR", "coinMultiplier": 0.65,
     "playerPickOptions": 2, "bonusPackId": 300},
    {"id": 3, "name": "SEMI_PRO", "coinMultiplier": 0.80,
     "playerPickOptions": 3, "bonusPackId": 301},
    {"id": 4, "name": "PROFESSIONAL", "coinMultiplier": 1.00,
     "playerPickOptions": 3, "bonusPackId": 303},
    {"id": 5, "name": "WORLD_CLASS", "coinMultiplier": 1.25,
     "playerPickOptions": 4, "bonusPackId": 305},
    {"id": 6, "name": "LEGENDARY", "coinMultiplier": 1.50,
     "playerPickOptions": 4, "bonusPackId": 403},
    {"id": 7, "name": "ULTIMATE", "coinMultiplier": 1.75,
     "playerPickOptions": 5, "bonusPackId": 401},
)

# The custom ladder is explicit because this competition has twenty matches,
# not retail's thirty.  A formula hidden in a DTO would make balance changes
# silently move a reachable rank by a whole win.
RANK_SPECS = (
    {"tierLevel": 1, "name": "ELITE 1", "minWins": 18,
     "coins": 40000, "packs": ((402, 2), (404, 1)), "pickCount": 4},
    {"tierLevel": 2, "name": "ELITE 2", "minWins": 17,
     "coins": 32000, "packs": ((402, 1), (404, 1)), "pickCount": 4},
    {"tierLevel": 3, "name": "ELITE 3", "minWins": 16,
     "coins": 25000, "packs": ((404, 1), (403, 1)), "pickCount": 3},
    {"tierLevel": 4, "name": "GOLD 1", "minWins": 14,
     "coins": 20000, "packs": ((404, 1), (401, 1)), "pickCount": 2},
    {"tierLevel": 5, "name": "GOLD 2", "minWins": 13,
     "coins": 15000, "packs": ((403, 1), (401, 1)), "pickCount": 2},
    {"tierLevel": 6, "name": "GOLD 3", "minWins": 11,
     "coins": 10000, "packs": ((401, 1), (305, 1)), "pickCount": 2},
    {"tierLevel": 7, "name": "SILVER 1", "minWins": 9,
     "coins": 7000, "packs": ((401, 1),), "pickCount": 1},
    {"tierLevel": 8, "name": "SILVER 2", "minWins": 7,
     "coins": 5000, "packs": ((305, 1),), "pickCount": 0},
    {"tierLevel": 9, "name": "SILVER 3", "minWins": 5,
     "coins": 3000, "packs": ((303, 1),), "pickCount": 0},
    {"tierLevel": 10, "name": "BRONZE 1", "minWins": 4,
     "coins": 2000, "packs": ((301, 1),), "pickCount": 0},
    {"tierLevel": 11, "name": "BRONZE 2", "minWins": 2,
     "coins": 1000, "packs": ((300, 1),), "pickCount": 0},
    {"tierLevel": 12, "name": "BRONZE 3", "minWins": 1,
     "coins": 0, "packs": ((701, 1),), "pickCount": 0},
)

_UNRANKED_SPEC = {
    "tierLevel": 0, "name": "UNRANKED", "minWins": 0,
    "coins": 0, "packs": (), "pickCount": 0,
}

_PACK_VALUES = {
    300: 5000, 301: 7500, 303: 15000, 305: 25000,
    401: 50000, 402: 100000, 403: 35000, 404: 55000,
    701: 0,
}

_OPPONENT_NAMES = (
    "Bomboclat XI", "Going Toulouse", "Boca Seniors", "Boom Xhakalaka",
    "Tea & Busquets", "Salt and Pepe", "BetterCallSaul", "Bad Kompany",
    "Unreal Madrid", "Pogback FC", "Hakuna Matata", "Tekkerslovakia",
    "Pique Blinders", "Balotellitubbies", "Game of Stones", "Final Bosses",
    "Invincible Icons", "Ultimate Selection", "GoalOfDuty",
    "FUT Deba All-Stars",
)

_EXTRA_LEADERBOARD_NAMES = (
    "Expected Goals", "Ctrl Alt De Ligt", "Net Six and Chill",
    "No Kane No Gain", "Kroos Control", "Lord of the Ings",
    "Obi Wan Iwobi", "Alisson Wonderland", "Who Ate Depays",
    "Rice Rice Baby", "Enter Shaqiri", "Moves Like Agger", "Gueye Pride",
    "Son of a Pitch", "De Bruyne Ultimatum", "Finding Timo",
    "Chicken Tikka Mo", "Game of Throw Ins", "Goal Diggers", "VARcelona",
    "Inter Row Z", "AC Me Rollin", "Real Sosobad", "Paris Ganja Man",
    "Bayern Bru", "Dynamo Chicken", "Borussia Teeth", "Atletico Mince",
    "Queens Park Raisins", "Wolverham Sandwich", "Nottingham Fret",
    "Sheffield Sundae", "Leeds by Example", "Norfolk Enchants",
    "Camavinga Boys", "Haaland Oates", "Messi Business",
    "Risk It for Busquets", "Silence of Lahms", "Boom Boom Saka",
    "Saka Potatoes", "Mount Rushmore", "Partey Animals",
    "Onana Whats My Name", "Lallanas in Pyjamas", "Tea and Tactics",
    "Park the Bus", "Route One Rebels", "Nutmeg Merchants",
    "Crossbar Cowboys", "Offside Society", "Sunday League Kings",
    "Clean Sheet Cheats", "Five Star Weak Foot", "Red Card Royalty",
    "Yellow Submarine", "Extra Time Lords", "Penalty Pending",
    "Top Bins Only", "Near Post Ninjas", "False Nine Lives",
    "Pressing Matters", "Low Block Legends", "High Line Heroes",
    "Counter Culture", "Two Footed Poets", "Header Specialists",
    "Own Goal United", "Expected Banter", "Midfield Crisis",
    "Bench Warmers FC", "Transfer Listed", "Loan Rangers",
    "Free Agent Army", "Academy Dropouts", "Manager Out",
    "Pack Luck Pending", "Rare Gold Enjoyers", "Bronze Bench Mafia",
    "Dead Ball Society",
)

_LEADERBOARD_NAMES = _OPPONENT_NAMES + _EXTRA_LEADERBOARD_NAMES

_FORMATIONS = (
    "f442", "f433", "f4231", "f41212", "f352", "f3412", "f3421",
    "f4141", "f4222", "f4312", "f4321", "f4411", "f451", "f5212",
    "f5221", "f532", "f442", "f433", "f4231", "f41212",
)

_FORMATION_POSITIONS = {
    "f442": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "ST", "ST"),
    "f433": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "RW", "ST", "LW"),
    "f4231": ("GK", "RB", "CB", "CB", "LB", "CDM", "CDM", "CAM", "CAM", "CAM", "ST"),
    "f41212": ("GK", "RB", "CB", "CB", "LB", "CDM", "RM", "LM", "CAM", "ST", "ST"),
    "f352": ("GK", "CB", "CB", "CB", "CM", "CM", "RM", "LM", "CAM", "ST", "ST"),
    "f3412": ("GK", "CB", "CB", "CB", "RM", "CM", "CM", "LM", "CAM", "ST", "ST"),
    "f3421": ("GK", "CB", "CB", "CB", "RM", "CM", "CM", "LM", "RW", "LW", "ST"),
    "f4141": ("GK", "RB", "CB", "CB", "LB", "CDM", "RM", "CM", "CM", "LM", "ST"),
    "f4222": ("GK", "RB", "CB", "CB", "LB", "CDM", "CDM", "CAM", "CAM", "ST", "ST"),
    "f4312": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "CAM", "ST", "ST"),
    "f4321": ("GK", "RB", "CB", "CB", "LB", "CM", "CM", "CM", "CF", "CF", "ST"),
    "f4411": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "CF", "ST"),
    "f451": ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "CM", "LM", "ST"),
    "f5212": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "CAM", "ST", "ST"),
    "f5221": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "RW", "LW", "ST"),
    "f532": ("GK", "RWB", "CB", "CB", "CB", "LWB", "CM", "CM", "CM", "ST", "ST"),
}

_RESERVE_POSITIONS = (
    "GK", "CB", "LB", "RB", "CDM", "CM", "CAM", "RM", "LM", "RW", "LW", "ST",
)

# The catalogue contains 237 TOTS cards but only 57 IF, 25 SIF and 4 TIF
# cards in the supported 85-96 band.  Sampling that catalogue as one pool
# therefore made almost every opponent look like a TOTS squad.  These quotas
# express the intended opponent variety independently of catalogue size: the
# five-team cycle averages exactly four Team of the Week cards per full squad.
_TOTW_REVISIONS = frozenset({"IF", "SIF", "TIF"})
_TOTW_COUNTS = (2, 3, 4, 5, 6) * 4
_RARE_GOLD_COUNTS = (6, 5, 5, 4, 4) * 4
_TOTS_COUNTS = (1,) * 10 + (2,) * 10

_POSITION_GROUPS = {
    "GK": {"GK"},
    "RB": {"RB", "RWB"}, "RWB": {"RWB", "RB", "RM"},
    "LB": {"LB", "LWB"}, "LWB": {"LWB", "LB", "LM"},
    "CB": {"CB", "RB", "LB"},
    "CDM": {"CDM", "CM", "CB"}, "CM": {"CM", "CDM", "CAM"},
    "CAM": {"CAM", "CM", "CF"},
    "RM": {"RM", "RW", "CM"}, "LM": {"LM", "LW", "CM"},
    "RW": {"RW", "RM", "RF"}, "LW": {"LW", "LM", "LF"},
    "CF": {"CF", "CAM", "ST"}, "ST": {"ST", "CF"},
}


def difficulty_spec(difficulty: int) -> dict[str, Any]:
    value = int(difficulty)
    if value not in range(1, 8):
        raise ValueError("FUT Champions difficulty must be between 1 and 7")
    return dict(DIFFICULTIES[value - 1])


def tier_for_wins(wins: int) -> dict[str, Any]:
    value = max(0, min(MATCH_COUNT, int(wins)))
    if value == 0:
        # Bronze 3 begins at one win; treating a loss-only run as that tier
        # would grant a rank and a difficulty bonus it did not earn.
        return dict(_UNRANKED_SPEC)
    return dict(next(row for row in RANK_SPECS
                     if value >= int(row["minWins"])))


def _identity_team_ids() -> list[int]:
    grant = default_grant()["items"]
    home = {int(row["teamId"]) for row in grant
            if row.get("type") == "kit_home"}
    away = {int(row["teamId"]) for row in grant
            if row.get("type") == "kit_away"}
    badges = set()
    for row in grant:
        if row.get("type") != "badge":
            continue
        definition = object_definition(row["resourceId"]) or {}
        team_id = int(definition.get(
            "teamid", definition.get("_teamId", 0)) or 0)
        if team_id:
            badges.add(team_id)
    result = sorted(home & away & badges)
    if len(result) < MATCH_COUNT:
        raise ValueError("not enough source-backed badge and kit identities")
    return result


def _manager_resource_ids() -> list[int]:
    result = sorted({int(row["resourceId"])
                     for row in default_grant()["items"]
                     if row.get("type") == "manager"})
    if len(result) < MATCH_COUNT:
        raise ValueError("not enough source-backed FUT managers")
    return result


def _player_pool() -> list[dict[str, Any]]:
    return [dict(row) for row in card_version_rows(None, 85, 96)]


def _card_class(row: dict[str, Any]) -> str:
    revision = str(row.get("revision") or "Normal")
    if revision == "Normal":
        return "rare_gold"
    if revision in _TOTW_REVISIONS:
        return "totw"
    if revision == "TOTS":
        return "tots"
    return "other"


def _pick_row(pool: list[dict[str, Any]], rng: random.Random, target: int,
              team_assets: set[int], global_assets: set[int],
              position: str | None = None,
              revision: str | None = None) -> dict[str, Any]:
    candidates = [row for row in pool
                  if int(row.get("assetId", 0) or 0) not in team_assets and
                  (revision is None or str(row.get("revision")) == revision)]
    if position is not None:
        accepted = _POSITION_GROUPS.get(position, {position})
        positioned = [row for row in candidates
                      if str(row.get("pos", "")).upper() in accepted]
        if positioned:
            candidates = positioned
    if not candidates:
        raise ValueError("card pool cannot fill the requested roster")
    unused = [row for row in candidates
              if int(row.get("assetId", 0) or 0) not in global_assets]
    candidates = unused or candidates
    candidates.sort(key=lambda row: (
        abs(int(row.get("rating", 0) or 0) - int(target)),
        int(row["resourceId"])))
    # Rating controls the strength band; the bounded seeded choice keeps two
    # events from becoming identical without allowing a low card into a late
    # high-strength squad.
    window = candidates[:min(10, len(candidates))]
    return window[rng.randrange(len(window))]


@lru_cache(maxsize=64)
def _champion_opponents_cached(event_id: int) -> tuple[dict[str, Any], ...]:
    event_id = int(event_id)
    pool = _player_pool()
    class_pools = {
        name: [row for row in pool if _card_class(row) == name]
        for name in ("rare_gold", "totw", "tots", "other")
    }
    revisions = sorted({str(row["revision"]) for row in pool
                        if str(row["revision"]) != "Normal"})
    forced_by_team = [[] for _ in range(MATCH_COUNT)]
    for index, revision in enumerate(revisions):
        forced_by_team[index % MATCH_COUNT].append(revision)

    identity_rng = random.Random(event_id * 1_000_003 + 41)
    team_ids = identity_rng.sample(_identity_team_ids(), MATCH_COUNT)
    manager_ids = identity_rng.sample(_manager_resource_ids(), MATCH_COUNT)
    global_assets: set[int] = set()
    opponents = []
    for index in range(MATCH_COUNT):
        formation = _FORMATIONS[index]
        target = 85 + round(index * 11 / (MATCH_COUNT - 1))
        rng = random.Random(event_id * 1_000_003 + (index + 1) * 97_409)
        team_assets: set[int] = set()
        forced = []
        for revision in forced_by_team[index]:
            row = _pick_row(pool, rng, target, team_assets, global_assets,
                            revision=revision)
            forced.append(row)
            team_assets.add(int(row["assetId"]))
            global_assets.add(int(row["assetId"]))

        totw_count = _TOTW_COUNTS[index]
        rare_gold_count = _RARE_GOLD_COUNTS[index]
        tots_count = _TOTS_COUNTS[index]
        starter_counts = {
            "rare_gold": min(3, rare_gold_count),
            "totw": min(3, max(1, round(totw_count / 2))),
            "tots": 0 if index < 10 else 1,
        }
        starter_classes = [
            name for name, count in starter_counts.items()
            for _ in range(count)
        ]
        starter_classes.extend(
            ["other"] * (11 - len(starter_classes)))
        rng.shuffle(starter_classes)

        starters = []
        for position, card_class in zip(
                _FORMATION_POSITIONS[formation], starter_classes):
            row = _pick_row(class_pools[card_class], rng, target,
                            team_assets, global_assets,
                            position=position)
            starters.append(row)
            team_assets.add(int(row["assetId"]))
            global_assets.add(int(row["assetId"]))

        forced_counts = Counter(_card_class(row) for row in forced)
        reserve_classes = []
        for card_class, wanted in (
                ("rare_gold", rare_gold_count),
                ("totw", totw_count),
                ("tots", tots_count)):
            remaining = (wanted - starter_counts[card_class] -
                         forced_counts[card_class])
            reserve_classes.extend([card_class] * max(0, remaining))
        reserve_classes.extend(
            ["other"] * (12 - len(forced) - len(reserve_classes)))
        rng.shuffle(reserve_classes)

        reserves = list(forced)
        for position, card_class in zip(
                _RESERVE_POSITIONS[len(reserves):], reserve_classes):
            row = _pick_row(class_pools[card_class], rng, target,
                            team_assets, global_assets,
                            position=position)
            reserves.append(row)
            team_assets.add(int(row["assetId"]))
            global_assets.add(int(row["assetId"]))
        rows = starters + reserves
        if len(rows) != 23:
            raise ValueError("FUT Champions opponent is not a 23-card squad")
        opponents.append({
            "matchNumber": index + 1,
            "opponentId": 9_100_000 + (event_id % 10_000) * 100 + index + 1,
            "name": _OPPONENT_NAMES[index],
            "formation": formation,
            "identityTeamId": int(team_ids[index]),
            "managerResourceId": int(manager_ids[index]),
            "targetRating": target,
            "rating": round(sum(int(row["rating"]) for row in starters) / 11),
            "starterPositions": tuple(_FORMATION_POSITIONS[formation]),
            "resourceIds": tuple(int(row["resourceId"]) for row in rows),
            "revisions": tuple(str(row["revision"]) for row in rows),
        })
    return tuple(opponents)


def champion_opponents(event_id: int) -> list[dict[str, Any]]:
    """Return isolated copies of the event's twenty immutable squad specs."""
    return [{**row, "starterPositions": list(row["starterPositions"]),
             "resourceIds": list(row["resourceIds"]),
             "revisions": list(row["revisions"])}
            for row in _champion_opponents_cached(int(event_id))]


@lru_cache(maxsize=64)
def _leaderboard_bots_cached(event_id: int) -> tuple[dict[str, Any], ...]:
    rows = []
    for index in range(LEADERBOARD_SIZE):
        rng = random.Random(int(event_id) * 1_000_003 + (index + 1) * 65_537)
        ability = 1.0 - index / max(1, LEADERBOARD_SIZE - 1)
        wins = max(8, min(MATCH_COUNT, round(10 + ability * 9 +
                                             rng.uniform(-1.5, 1.5))))
        remaining = MATCH_COUNT - wins
        draws = min(remaining, rng.randrange(0, min(3, remaining) + 1))
        losses = remaining - draws
        goal_difference = max(-10, round(wins * (0.8 + ability * 1.1) -
                                          losses * 0.7 + rng.uniform(-4, 4)))
        # Reusing the opponent identities makes the simulated competition feel
        # coherent; the remaining authored names avoid exposing placeholder
        # accounts such as LocalChampions001 in the public Top 100.
        name = _LEADERBOARD_NAMES[index]
        rows.append({
            "personaId": 8_900_000_000 + index,
            "persona": name,
            "clubName": name,
            "matchesPlayed": MATCH_COUNT,
            "wins": wins, "draws": draws, "losses": losses,
            "goalDifference": goal_difference,
            "value": wins * 1000 + goal_difference,
            "isUser": False,
        })
    rows.sort(key=lambda row: (-int(row["wins"]),
                               -int(row["goalDifference"]),
                               str(row["persona"])))
    return tuple(rows)


def leaderboard_bots(event_id: int) -> list[dict[str, Any]]:
    """Publish final bot records immediately so a zero-game user is unranked."""
    return [dict(row) for row in _leaderboard_bots_cached(int(event_id))]


def user_rank(event_id: int, wins: int, draws: int, losses: int,
              goal_difference: int = 0) -> int:
    user = {"wins": max(0, min(MATCH_COUNT, int(wins))),
            "goalDifference": int(goal_difference), "persona": ""}
    rows = leaderboard_bots(int(event_id)) + [user]
    rows.sort(key=lambda row: (-int(row["wins"]),
                               -int(row["goalDifference"]),
                               0 if row is user else 1,
                               str(row.get("persona", ""))))
    position = rows.index(user) + 1
    return position if position <= LEADERBOARD_SIZE else 0


def reward_bundle(wins: int, difficulty: int,
                  top_100_rank: int = 0) -> dict[str, Any]:
    rank = tier_for_wins(wins)
    if 1 <= int(top_100_rank or 0) <= LEADERBOARD_SIZE:
        rank = {"tierLevel": 0, "name": "TOP 100", "minWins": 0,
                "coins": 50000, "packs": ((402, 2), (404, 1)),
                "pickCount": 5}
    difficulty_row = difficulty_spec(difficulty)
    coins = int(round((int(rank["coins"]) *
                       float(difficulty_row["coinMultiplier"])) / 250.0) * 250)
    packs = Counter()
    for pack_id, count in rank["packs"]:
        packs[int(pack_id)] += max(1, int(count))
    bonus_pack = (int(difficulty_row["bonusPackId"])
                  if int(rank["tierLevel"]) != 0 else 0)
    if bonus_pack:
        packs[bonus_pack] += 1
    pack_rows = tuple({"packId": pack_id, "count": count}
                      for pack_id, count in sorted(packs.items()))
    pick_count = int(rank["pickCount"])
    option_count = int(difficulty_row["playerPickOptions"])
    estimated = coins + sum(_PACK_VALUES[pack_id] * count
                            for pack_id, count in packs.items())
    estimated += pick_count * (5000 + option_count * 2000)
    return {
        "tierLevel": int(rank["tierLevel"]),
        "tierName": str(rank["name"]),
        "minWins": int(rank["minWins"]),
        "difficulty": int(difficulty_row["id"]),
        "difficultyName": str(difficulty_row["name"]),
        "coins": coins,
        "packs": list(pack_rows),
        "playerPickCount": pick_count,
        "playerPickOptions": option_count,
        "estimatedValue": int(estimated),
    }


def champion_prize_tier_dto(*, awards: list[dict[str, Any]], tier_start: int,
                            tier_end: int, tier_level: int,
                            tier_type: str = NUMBER_OF_WINS_TIER_TYPE,
                            ) -> dict[str, Any]:
    """Shape one event tier without inventing an unobserved reward envelope."""
    # The cumulative dispatch at CardsDLL+0x279d0a..+0x279d30 reads these five
    # members.  Keeping Award values opaque lets the HTTP layer reuse its
    # already-proved Award serializer instead of defining a second schema here.
    return {
        "awards": [dict(row) for row in awards],
        "tierEnd": int(tier_end),
        "tierLevel": int(tier_level),
        "tierStart": int(tier_start),
        "tierType": str(tier_type),
    }


def champion_registration_dto(*, competition_region: str) -> dict[str, Any]:
    """Shape the one member read by the registration parser at RVA 0x263050."""
    return {"competitionRegion": str(competition_region)}


def champion_awarded_prize_dto(*, awards: list[dict[str, Any]], event_id: int,
                               rank: int, tier_level: int) -> dict[str, Any]:
    """Shape one granted event decoded at RVA 0x258755..0x25887b."""
    return {
        "awards": [dict(row) for row in awards],
        "eventId": int(event_id),
        "rank": int(rank),
        "tierLevel": int(tier_level),
    }


def champion_prize_error_dto(*, error_type: str,
                             event_id: int) -> dict[str, Any]:
    """Shape one failed event decoded at RVA 0x258598..0x2585d9."""
    return {"errorType": str(error_type), "eventId": int(event_id)}


def champion_prize_grant_dto(
        *, awarded_prizes: list[dict[str, Any]] | None = None,
        prizes_in_error: list[dict[str, Any]] | None = None,
        dynamic_objectives_updates: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
    """Return the closed Champions prize response parsed at RVA 0x258350.

    This is deliberately separate from the similarly named Squad Battles
    response: the Champions root dispatch accepts only member ids 0x4a,
    0x125 and 0x2f3. Reusing the six-member Squad Battles envelope would hide
    the event identity inside fields this response never reads.
    """
    return {
        "awardedPrizes": [dict(row) for row in (awarded_prizes or [])],
        "dynamicObjectivesUpdates": dict(dynamic_objectives_updates or {}),
        "prizesInError": [dict(row) for row in (prizes_in_error or [])],
    }


def champion_event_dto(*, event_id: int, current_time: int, start_time: int,
                       end_time: int, localized_name: str,
                       max_matches: int = MATCH_COUNT,
                       min_matches_to_rank: int = 0,
                       eligibilities: list[dict[str, Any]] | None = None,
                       eligibility_operation: str = "AND",
                       match_params: dict[str, Any] | None = None,
                       prize_tiers: list[dict[str, Any]] | None = None,
                       qualifier_tournaments: list[dict[str, Any]] | None = None,
                       ) -> dict[str, Any]:
    """Return only members decoded by the event parser at RVA 0x278e80."""
    return {
        "currentTime": int(current_time),
        "eligibilities": [dict(row) for row in (eligibilities or [])],
        "elgOperation": str(eligibility_operation),
        "endTime": int(end_time),
        "id": int(event_id),
        "localizedName": str(localized_name),
        "matchParamsKeyValues": dict(match_params or {}),
        "maxMatches": int(max_matches),
        "minMatchesToRank": int(min_matches_to_rank),
        "prizeTiers": [dict(row) for row in (prize_tiers or [])],
        "qualifierTournaments": [dict(row)
                                 for row in (qualifier_tournaments or [])],
        "startTime": int(start_time),
    }


def champion_user_stat_dto(*, event_id: int, encrypted_nucleus_id: str,
                           encrypted_persona_id: str,
                           expected_tier_level: int, games_played: int,
                           games_remaining: int, rank: int, score: int,
                           tier_level: int) -> dict[str, Any]:
    """Shape one element consumed by the stats parser at RVA 0x281270."""
    return {
        "championEventId": int(event_id),
        "encryptedNucleusId": str(encrypted_nucleus_id),
        "encryptedPersonaId": str(encrypted_persona_id),
        "expectedTierLevel": int(expected_tier_level),
        "gamesPlayed": int(games_played),
        "gamesRemaining": int(games_remaining),
        "rank": int(rank),
        "score": int(score),
        "tierLevel": int(tier_level),
    }


def champion_user_stats_dto(
        rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The RVA 0x26b1f0 response reader iterates the JSON root as an array."""
    return [dict(row) for row in (rows or [])]


def champion_leaderboard_entry_dto(*, badge: int, club_name: str, est: int,
                                   inset_url: str, persona: str, rank: int,
                                   remaining_matches: int,
                                   score: list[dict[str, Any]] | None = None,
                                   tiebreak: list[dict[str, Any]] | None = None,
                                   ) -> dict[str, Any]:
    """Shape one Top-X record decoded at RVA 0x259816..0x259b84."""
    def components(values: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [{"icon": str(row.get("icon", "")),
                 "value": int(row.get("value", 0) or 0)}
                for row in (values or [])]

    return {
        "badge": int(badge),
        "clubName": str(club_name),
        "est": int(est),
        "insetUrl": str(inset_url),
        "persona": str(persona),
        "rank": int(rank),
        "remainingMatches": int(remaining_matches),
        "score": components(score),
        "tiebreak": components(tiebreak),
    }


def champion_leaderboard_dto(
        entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Top-X RVA 0x259320 dispatches only member 0x150, ``entries``."""
    return {"entries": [dict(row) for row in (entries or [])]}


def champion_friend_stat_dto(*, games_played: int, persona: str, rank: int,
                             remaining_matches: int, score: int,
                             tier_level: int) -> dict[str, Any]:
    """Shape one record consumed at RVA 0x25a8d6..0x25a99e."""
    return {
        "gamesPlayed": int(games_played),
        "persona": str(persona),
        "rank": int(rank),
        "remainingMatches": int(remaining_matches),
        "score": int(score),
        "tierLevel": int(tier_level),
    }


def champion_friends_stats_dto(
        rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Friends RVA 0x25a600 requires outer member 0x3c5, ``stats``."""
    return {"stats": [dict(row) for row in (rows or [])]}


def champion_hub_dto(*, competition_country_code: str,
                     active_events: list[dict[str, Any]] | None = None,
                     previous_events: list[dict[str, Any]] | None = None,
                     qualified_league_ids: list[int] | None = None,
                     unclaimed_events: list[dict[str, Any]] | None = None,
                     user_stats: list[dict[str, Any]] | None = None,
                     user_registration: dict[str, Any] | None = None,
                     game_mode_restriction: int = 0,
                     min_win_form_value: int = 0) -> dict[str, Any]:
    """Return the intersection of both registered Champions Hub readers."""
    # RVA 0x2a9080 consumes competitionCountryCode.  The richer reader at RVA
    # 0x2b39d0 consumes the remaining members below.  Its outer 0xbd/0xbe
    # dispatch at +0x2b472f writes the top-level country/region projection;
    # action 0x10 later uses that country as the registration boundary.
    return {
        "competitionCountryCode": str(competition_country_code),
        "activeChampionLeagues": [dict(row) for row in (active_events or [])],
        "gameModeRestriction": int(game_mode_restriction),
        "minWinFormValue": int(min_win_form_value),
        "previousChampionEvents": [dict(row)
                                   for row in (previous_events or [])],
        "qualifiedChampionLeagueIds": [int(value)
                                       for value in (qualified_league_ids or [])],
        "unclaimedPrizesChampionEvents": [dict(row)
                                           for row in (unclaimed_events or [])],
        "userRegistration": dict(user_registration or {}),
        "userStats": [dict(row) for row in (user_stats or [])],
    }

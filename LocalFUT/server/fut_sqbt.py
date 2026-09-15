"""Squad Battles selection logic: featured squad, opponent pools, scoring.

The HTTP layer owns the retail DTO shapes; this module owns the decisions that
fill them, so both can be tested without a running server:

* the featured squad, a full Prime Icon Moments XI,
* the four opponent slots, each a pool of five real FIFA 19 clubs chosen by
  star rating,
* the points a finished match is worth, by difficulty and opponent,
* the leaderboard the hub ranks the user against.

Everything is derived from the extracted catalogue and is deterministic for a
given event: the same week always produces the same pools, the same match
always produces the same points.  Nothing here invents a resource ID.
"""
from __future__ import annotations

import csv
import io
import os
import random
import re
from functools import lru_cache
from typing import Any

from fut_catalog import card_version_rows

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PLAYER_CATALOG_CSV = os.path.join(
    DATA_DIR, "catalog", "fut19_players.csv")

# ---------------------------------------------------------------- featured --

FEATURED_SQUAD_NAME = "Icon team"
FEATURED_SQUAD_FORMATION = "f433"
EVENT_DURATION_SECONDS = 3 * 24 * 60 * 60
FUT_CHAMPIONS_RARITY_ID = 18  # EA futitemraritytunables: CHAMPION_REWARD
FUT_CHAMPIONS_PICK_OPTIONS = 4

# The Prime Icon Moments XI, in the retail PC f433 slot order
# (GK, RB, CB, CB, LB, CM, CM, CM, RW, ST, LW). FIFA 19 ships no Prime Icon at
# RB, so that slot takes Zanetti, who played there but is listed at CDM. Every ID below is a
# `Prime Icon Moments` row of the extracted catalogue, checked by
# `featured_squad_resource_ids`.
FEATURED_SQUAD_XI = (
    246526,  # Lev Yashin, GK, 95
    246492,  # Javier Zanetti, CDM, 93
    246535,  # Paolo Maldini, CB, 95
    246490,  # Fabio Cannavaro, CB, 93
    246533,  # Roberto Carlos, LB, 92
    246534,  # Lothar Matthaeus, CM, 94
    246514,  # Diego Maradona, CAM, 98
    246525,  # Pele, CAM, 99
    246524,  # George Best, RW, 94
    246497,  # Ronaldo, ST, 97
    246472,  # Ronaldinho, LW, 95
)

# Bench and reserves, again in `_SQUAD_POSITIONS` order for slots 11 to 22.
FEATURED_SQUAD_BENCH = (
    246532,  # Peter Schmeichel, GK, 93
    246515,  # Bobby Moore, CB, 93
    246528,  # Carles Puyol, CB, 93
    246479,  # Marcel Desailly, CB, 92
    246506,  # Emmanuel Petit, CDM, 91
    246531,  # Patrick Vieira, CM, 92
    246493,  # Roberto Baggio, CAM, 94
    246503,  # Luis Figo, RW, 93
    246527,  # Alessandro Del Piero, LW, 93
    246516,  # Johan Cruyff, CF, 95
    246489,  # Thierry Henry, ST, 94
    246519,  # Marco van Basten, ST, 94
)


def featured_squad_resource_ids() -> list[int]:
    """Return the ICON TEAM roster, refusing anything that is not a Prime Icon.

    A wrong ID here would reach the client as an anonymous card, so the check
    is part of the accessor rather than a separate validation step.
    """
    prime = {int(row["resourceId"]) for row in
             card_version_rows("Prime Icon Moments")}
    roster = list(FEATURED_SQUAD_XI) + list(FEATURED_SQUAD_BENCH)
    missing = [value for value in roster if value not in prime]
    if missing:
        raise ValueError("featured squad has non Prime Icon rows: %r" % missing)
    if len(set(roster)) != len(roster):
        raise ValueError("featured squad repeats a card")
    return roster


# ------------------------------------------------------------------ clubs ---

_club_rows: dict[int, list[dict[str, Any]]] | None = None
_club_names: dict[int, str] | None = None
_club_ratings: dict[int, int] | None = None

# The XI a club is rated on, and the positions each slot accepts.  This mirrors
# `_SQUAD_POSITIONS` in the HTTP layer so a club's advertised star rating and
# the squad the match actually loads come from the same players.
_RATING_XI = ("GK", "RB", "CB", "CB", "LB", "RM", "CM", "CM", "LM", "ST", "ST")
_BROAD_POSITIONS = {
    "GK": {"GK"},
    "RB": {"RB", "RWB"},
    "LB": {"LB", "LWB"},
    "CB": {"CB", "RB", "LB"},
    "CM": {"CM", "CDM", "CAM"},
    "RM": {"RM", "RW", "CM"},
    "LM": {"LM", "LW", "CM"},
    "ST": {"ST", "CF", "LW", "RW"},
}


def _load_clubs() -> dict[int, list[dict[str, Any]]]:
    global _club_rows
    if _club_rows is not None:
        return _club_rows
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in card_version_rows("Normal"):
        club = int(row.get("teamid", row.get("teamId", row.get("club", 0))) or 0)
        if club > 0:
            grouped.setdefault(club, []).append(row)
    _club_rows = grouped
    return grouped


def _load_club_names() -> dict[int, str]:
    """Map club IDs to display names from the packaged player catalogue."""
    global _club_names
    if _club_names is not None:
        return _club_names
    names: dict[int, str] = {}
    try:
        with io.open(PLAYER_CATALOG_CSV, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                match = re.search(r"/clubs/(\d+)\.png", row.get("ClubPic") or "")
                name = (row.get("Club") or "").strip()
                if match and name:
                    names.setdefault(int(match.group(1)), name)
    except OSError:
        names = {}
    _club_names = names
    return names


def club_name(club_id: int) -> str:
    return _load_club_names().get(int(club_id), "") or "Club %d" % int(club_id)


def club_rating(club_id: int) -> int:
    """Return the club's best-XI average rating, 0 when it cannot field one."""
    global _club_ratings
    if _club_ratings is None:
        _club_ratings = {}
    club_id = int(club_id)
    if club_id in _club_ratings:
        return _club_ratings[club_id]
    rows = _load_clubs().get(club_id, [])
    used: set[int] = set()
    ratings: list[int] = []
    for slot in _RATING_XI:
        accepted = _BROAD_POSITIONS.get(slot, {slot})
        best = None
        for row in rows:
            asset = int(row.get("assetId", 0) or 0)
            if asset in used or str(row.get("pos", "")).upper() not in accepted:
                continue
            if best is None or int(row.get("rating", 0) or 0) > int(
                    best.get("rating", 0) or 0):
                best = row
        if best is None:
            continue
        used.add(int(best.get("assetId", 0) or 0))
        ratings.append(int(best.get("rating", 0) or 0))
    value = round(sum(ratings) / len(ratings)) if len(ratings) == 11 else 0
    _club_ratings[club_id] = value
    return value


# FIFA grades a team in half stars.  The thresholds below are calibrated on the
# extracted catalogue so the five-star band holds the clubs FIFA 19 itself
# rates five stars (Bayern, Juventus, Manchester City, Barcelona, Real Madrid,
# Paris Saint-Germain) rather than on a guessed curve.
_STAR_THRESHOLDS = ((85, 5.0), (83, 4.5), (80, 4.0), (78, 3.5),
                    (75, 3.0), (73, 2.5), (70, 2.0))


def star_rating(club_id: int) -> float:
    rating = club_rating(club_id)
    if rating <= 0:
        return 0.0
    for threshold, stars in _STAR_THRESHOLDS:
        if rating >= threshold:
            return stars
    return 1.5


# --------------------------------------------------------------- opponents --

# One entry per Opponent Select slot: the star band the slot draws from, and
# how many clubs its pool holds.  The bands are the ones requested for the
# mode: an easy slot capped at three stars, then two overlapping middle bands,
# then a five-star slot.
SLOT_RULES = (
    {"slot": 1, "minStars": 0.0, "maxStars": 3.0, "poolSize": 5},
    {"slot": 2, "minStars": 3.0, "maxStars": 4.0, "poolSize": 5},
    {"slot": 3, "minStars": 4.0, "maxStars": 5.0, "poolSize": 5},
    {"slot": 4, "minStars": 5.0, "maxStars": 5.0, "poolSize": 5},
)

# Each slot owns a five-club pool.  A weekly competition can therefore expose
# five non-repeating sets of four opponents (20 point-bearing matches) before
# the local fixture list is exhausted.
MAX_ROTATIONS = max(int(rule["poolSize"]) for rule in SLOT_RULES)
MAX_POINT_MATCHES = len(SLOT_RULES) * MAX_ROTATIONS

# How far down the eligible list a pool may reach.  Without this the weakest
# band would fill with clubs nobody recognises; with it every slot still
# rotates, but between clubs at the top of its own band.
_POOL_CANDIDATE_DEPTH = 14


def _eligible_clubs(rule: dict[str, Any]) -> list[tuple[int, int]]:
    """Return `(rating, club_id)` for every club inside the slot's star band."""
    eligible = []
    for club_id in _load_clubs():
        stars = star_rating(club_id)
        if stars <= 0:
            continue
        if stars < float(rule["minStars"]) or stars > float(rule["maxStars"]):
            continue
        eligible.append((club_rating(club_id), int(club_id)))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    return eligible


def event_pools(event_id: int) -> list[list[dict[str, Any]]]:
    """Return every slot's pool for this event, with no club used twice.

    The star bands overlap on purpose, so the pools are filled hardest slot
    first and each one excludes the clubs already taken.  Without that, the
    five-star clubs eligible for both of the top two slots could show up as
    two different opponents in the same rotation.
    """
    taken: set[int] = set()
    pools: list[list[dict[str, Any]]] = [[] for _ in SLOT_RULES]
    for slot_index in reversed(range(len(SLOT_RULES))):
        rule = SLOT_RULES[slot_index]
        eligible = [item for item in _eligible_clubs(rule)
                    if item[1] not in taken]
        if not eligible:
            continue
        size = int(rule["poolSize"])
        depth = max(size, min(_POOL_CANDIDATE_DEPTH, len(eligible)))
        window = eligible[:depth]
        rng = random.Random(int(event_id) * 100 + slot_index)
        chosen = window if len(window) <= size else rng.sample(window, size)
        chosen.sort(key=lambda item: (-item[0], item[1]))
        taken.update(club_id for _, club_id in chosen)
        pools[slot_index] = [
            {"clubId": club_id, "name": club_name(club_id),
             "rating": rating, "stars": star_rating(club_id),
             "slot": int(rule["slot"])}
            for rating, club_id in chosen]
    return pools


def slot_pool(event_id: int, slot_index: int) -> list[dict[str, Any]]:
    """Return the five clubs a slot can present for this event."""
    return event_pools(event_id)[int(slot_index)]


def slot_opponent(event_id: int, slot_index: int,
                  rotation: int = 0) -> dict[str, Any] | None:
    """Return the club a slot shows after `rotation` refreshes."""
    pool = slot_pool(event_id, slot_index)
    if not pool:
        return None
    return pool[int(rotation) % len(pool)]


def opponents(event_id: int, rotation: int = 0) -> list[dict[str, Any]]:
    return [entry for entry in
            (slot_opponent(event_id, index, rotation)
             for index in range(len(SLOT_RULES)))
            if entry is not None]


# ----------------------------------------------------------------- scoring --

# FIFA 19 offers seven difficulties, Beginner through Ultimate, and Squad
# Battles pays by the one the match was played on.  These two tables are the
# `difficultyBasedWinPoints` and `difficultyBasedLossPoints` arrays the
# featured-squad response carries, so the points the UI advertises and the
# points a result actually books are the same numbers.
DIFFICULTY_LEVELS = 7
WIN_POINTS = (300, 400, 500, 650, 800, 1000, 1200)
LOSS_POINTS = (60, 80, 100, 130, 160, 200, 240)
DRAW_FACTOR = 0.45
# A goal swing is worth a fixed amount, capped so a rout cannot outweigh the
# difficulty the match was won on.
GOAL_POINTS = 25
GOAL_BONUS_CAP = 5
GOAL_PENALTY_CAP = 3
MATCH_POINTS_CAP = 2000


def difficulty_index(difficulty: Any) -> int:
    """Clamp any client value to a 0-based difficulty index."""
    try:
        value = int(difficulty)
    except (TypeError, ValueError):
        value = 1
    # The client counts difficulties from one; accept a 0-based value too.
    if value >= DIFFICULTY_LEVELS:
        value = DIFFICULTY_LEVELS
    if value <= 0:
        value = 1
    return value - 1


def opponent_multiplier(stars: float) -> float:
    """Return the opponent-strength factor applied to a result.

    A three-star opponent is the reference point, and each half star moves the
    reward by four per cent, so beating the five-star slot is worth more than
    beating the one the mode hands out first.
    """
    try:
        value = float(stars)
    except (TypeError, ValueError):
        value = 3.0
    if value <= 0:
        value = 3.0
    return round(1.0 + (value - 3.0) * 0.08, 4)


def match_points(difficulty: Any, result: str, goals_for: int = 0,
                 goals_against: int = 0, stars: float = 3.0) -> int:
    """Return the Squad Battles points a finished match is worth."""
    index = difficulty_index(difficulty)
    outcome = str(result or "").upper()
    if outcome == "WIN":
        base = WIN_POINTS[index]
    elif outcome == "DRAW":
        base = int(round(WIN_POINTS[index] * DRAW_FACTOR))
    else:
        base = LOSS_POINTS[index]
    difference = int(goals_for or 0) - int(goals_against or 0)
    difference = max(-GOAL_PENALTY_CAP, min(GOAL_BONUS_CAP, difference))
    total = base * opponent_multiplier(stars) + difference * GOAL_POINTS
    # A match never books a negative score: the difficulty floor is the reward
    # for finishing it at all.
    return min(MATCH_POINTS_CAP,
               max(int(round(LOSS_POINTS[index] * 0.5)), int(round(total))))


# ------------------------------------------------------------- leaderboard --

# Deterministic opponents for the Top 100 board.  They exist so rank, tier and
# the prize bands mean something offline; the handles are generated, and no
# entry claims to be a real player.
_BOT_PREFIXES = ("Nord", "Vero", "Alta", "Rapi", "Iron", "Onyx", "Vast",
                 "Kilo", "Zeta", "Umbra", "Halo", "Nova", "Ember", "Delta",
                 "Solar", "Prima", "Vento", "Argo", "Lyra", "Corvo")
_BOT_SUFFIXES = ("Wolves", "Rangers", "United", "Athletic", "Legion",
                 "Dynamo", "Rovers", "Sporting", "Kickers", "Vipers",
                 "Falcons", "Titans", "Comets", "Foxes", "Stallions")

LEADERBOARD_SIZE = 100
def _bot_name(index: int) -> str:
    prefix = _BOT_PREFIXES[index % len(_BOT_PREFIXES)]
    suffix = _BOT_SUFFIXES[(index // len(_BOT_PREFIXES)) % len(_BOT_SUFFIXES)]
    return "%s%s%02d" % (prefix, suffix, index + 1)


def leaderboard_bots(event_id: int, size: int = LEADERBOARD_SIZE,
                     rounds_played: int = 0) -> list[dict[str, Any]]:
    """Simulate the deterministic final local leaderboard for this event.

    The board used to advance only when the user played.  At zero user matches
    that left all 100 opponents on zero points, so the client truthfully
    rendered the user as Rank 1 and the Top 100 reward even though the score
    tier was Bronze 3.  Publishing the already-settled final bot season makes
    the target visible from the beginning and keeps it stable for the whole
    three-day competition.  ``rounds_played`` remains accepted for callers
    from older builds, but it no longer changes the simulated opponents.
    """
    count = max(1, int(size))
    entries = []
    for index in range(count):
        # A mixed activity total avoids presenting 100 identical bot records
        # while the settled board still gives the user a fixed target.
        declared_rounds = 14 + ((int(event_id) + index) % 2)
        # Keeping the established score simulation prevents the presentation
        # correction from lowering the target before the explicit rank cut.
        scoring_rounds = MAX_POINT_MATCHES
        position = index / max(1, count - 1)
        ability = 1.0 - position
        score = wins = draws = losses = 0
        for round_index in range(scoring_rounds):
            rng = random.Random(
                int(event_id) * 1_000_003 + (index + 1) * 97_409 +
                (round_index + 1) * 65_537)
            difficulty = max(1, min(7, int(round(
                2.5 + ability * 4.5 + rng.uniform(-0.65, 0.65)))))
            stars = max(1.0, min(5.0, round(
                (1.5 + rng.randrange(8) * 0.5) * 2.0) / 2.0))
            roll = rng.random()
            win_chance = 0.40 + ability * 0.47
            draw_chance = 0.08 + (1.0 - ability) * 0.08
            if roll < win_chance:
                result = "WIN"
                if round_index < declared_rounds:
                    wins += 1
                goals_for = rng.randint(1, 3 + int(ability * 3))
                goals_against = rng.randint(0, max(0, goals_for - 1))
            elif roll < win_chance + draw_chance:
                result = "DRAW"
                if round_index < declared_rounds:
                    draws += 1
                goals_for = goals_against = rng.randint(0, 3)
            else:
                result = "LOSS"
                if round_index < declared_rounds:
                    losses += 1
                goals_for = rng.randint(0, 2)
                goals_against = rng.randint(goals_for + 1,
                                            min(6, goals_for + 4))
            score += match_points(difficulty, result, goals_for,
                                  goals_against, stars)
        name = _bot_name(index)
        entries.append({"personaId": 8_800_000_000 + index,
                        "persona": name, "clubName": name,
                        "value": score,
                        "matchesPlayed": declared_rounds, "wins": wins,
                        "draws": draws, "losses": losses,
                        "isUser": False})
    entries.sort(key=lambda entry: -int(entry["value"]))
    return entries


def leaderboard(event_id: int, user_name: str, user_score: int,
                size: int = LEADERBOARD_SIZE,
                rounds_played: int = 0, user_wins: int = 0,
                user_draws: int = 0, user_losses: int = 0) -> list[dict[str, Any]]:
    """Return the board with the user placed on it by score."""
    rows = [dict(entry) for entry in leaderboard_bots(
        event_id, size, rounds_played)]
    score = max(0, int(user_score or 0))
    # The native screen calls this list Top 100.  Keeping an unqualified user
    # in its last rows made that screen contradict the authoritative reward
    # tier, so the user enters the list only after reaching its published floor.
    if score >= _prize_thresholds(int(event_id))[0]:
        rows.append({"persona": str(user_name or "You"),
                     "value": score,
                     "matchesPlayed":max(0,int(rounds_played or 0)),
                     "wins":max(0,int(user_wins or 0)),
                     "draws":max(0,int(user_draws or 0)),
                     "losses":max(0,int(user_losses or 0)),
                     "isUser": True})
    rows.sort(key=lambda entry: (-int(entry["value"]),
                                 0 if entry.get("isUser") else 1,
                                 str(entry["persona"])))
    return rows[:max(1, int(size))]


def user_rank(event_id: int, user_score: int,
              size: int = LEADERBOARD_SIZE,
              rounds_played: int = 0) -> int:
    """Return a 1-based Top-100 position, or zero below its point floor."""
    score = max(0, int(user_score or 0))
    if score < _prize_thresholds(int(event_id))[0]:
        # The hub view model at CardsDLL+0x959c0 tests rank > 0. A positive
        # value is rendered literally through USER_TIER_LABEL at +0x959f1;
        # zero takes the +0x95a0a branch which maps userTierLevel to Bronze 3.
        return 0
    ahead = sum(1 for entry in leaderboard_bots(
        event_id, size, rounds_played)
                if int(entry["value"]) > score)
    return ahead + 1


# --------------------------------------------------------------- rewards --

# FIFA 19 displays the tiers in this exact numeric order.  The awards are the
# FIFA 19-shaped, progression-safe rewards.  The original weekly values were
# too rich for a three-day local competition, so every tier is capped at three
# ordinary gold-oriented packs and modest coins.  FUT Champions picks are an
# additional untradeable reward and draw only from sourced TOTW definitions.
PRIZE_TIER_SPECS = (
    {"tierLevel": 0, "name": "TOP 100", "percent": 0,
     "coins": 15000, "packs": ((401, 1), (403, 1), (305, 1)), "pickCount": 5},
    {"tierLevel": 1, "name": "ELITE 1", "percent": 3,
     "coins": 12500, "packs": ((401, 1), (403, 1), (305, 1)), "pickCount": 4},
    {"tierLevel": 2, "name": "ELITE 2", "percent": 3,
     "coins": 10000, "packs": ((403, 1), (305, 1), (303, 1)), "pickCount": 4},
    {"tierLevel": 3, "name": "ELITE 3", "percent": 4,
     "coins": 8000, "packs": ((305, 1), (303, 1), (301, 1)), "pickCount": 4},
    {"tierLevel": 4, "name": "GOLD 1", "percent": 10,
     "coins": 6000, "packs": ((305, 1), (303, 1), (301, 1)), "pickCount": 3},
    {"tierLevel": 5, "name": "GOLD 2", "percent": 10,
     "coins": 5000, "packs": ((305, 1), (301, 1), (300, 1)), "pickCount": 3},
    {"tierLevel": 6, "name": "GOLD 3", "percent": 10,
     "coins": 4000, "packs": ((303, 1), (301, 1)), "pickCount": 2},
    {"tierLevel": 7, "name": "SILVER 1", "percent": 10,
     "coins": 3000, "packs": ((301, 1), (300, 1)), "pickCount": 1},
    {"tierLevel": 8, "name": "SILVER 2", "percent": 10,
     "coins": 2000, "packs": ((300, 2),), "pickCount": 0},
    {"tierLevel": 9, "name": "SILVER 3", "percent": 10,
     "coins": 1000, "packs": ((300, 1),), "pickCount": 0},
    {"tierLevel": 10, "name": "BRONZE 1", "percent": 10,
     "coins": 500, "packs": ((300, 1),), "pickCount": 0},
    {"tierLevel": 11, "name": "BRONZE 2", "percent": 10,
     "coins": 0, "packs": ((300, 1),), "pickCount": 0},
    {"tierLevel": 12, "name": "BRONZE 3", "percent": 10,
     "coins": 0, "packs": ((701, 1),), "pickCount": 0},
)


@lru_cache(maxsize=256)
def fut_champions_pick_spec(event_id: int, tier_level: int, pick_index: int,
                            option_count: int = FUT_CHAMPIONS_PICK_OPTIONS,
                            min_rating: int = 0
                            ) -> dict[str, Any]:
    """Return one deterministic red pick from every sourced FIFA 19 TOTW.

    FUT Champions rewards use the attributes and exact resource identity of
    their IF/SIF/TIF counterpart.  Rarity 18 is the EA-supplied red
    CHAMPION_REWARD presentation; keeping it as persisted presentation metadata
    avoids fabricating a second player-definition database.
    """
    option_count=max(2,int(option_count))
    floor=max(0,int(min_rating))
    pool=[dict(row) for row in card_version_rows(
        ("IF","SIF","TIF"),min_rating=floor)]
    if len({int(row["resourceId"]) % (1 << 24) for row in pool}) < option_count:
        raise ValueError("not enough sourced TOTW players for FUT Champions pick")
    rng=random.Random(int(event_id)*1_000_003+int(tier_level)*10_007+
                      int(pick_index)*101)
    rng.shuffle(pool)
    chosen=[]; used_assets=set()
    for row in pool:
        resource_id=int(row["resourceId"])
        asset_id=resource_id % (1 << 24)
        if asset_id in used_assets:
            continue
        used_assets.add(asset_id); chosen.append(row)
        if len(chosen)==option_count:
            break
    options=[]
    for row in chosen:
        options.append({"resourceId":int(row["resourceId"]),"extra":{
            "rating":int(row.get("rating",0) or 0),
            "presentationRareflag":FUT_CHAMPIONS_RARITY_ID,
            "presentationRevision":"FUT Champions",
            "cardRevision":"FUT Champions",
            "untradeable":True,"tradeable":False,"discardValue":0,
            "itemState":"free",
            "acquisitionSource":"SQBT_FUT_CHAMPIONS_PICK"}})
    return {"minRating":min(int(row.get("rating",0) or 0) for row in chosen),
            "optionCount":option_count,"quality":"SPECIAL",
            "name":"FUT Champions Player Pick",
            "description":"Choose one untradeable FUT Champions TOTW player.",
            "options":options}


@lru_cache(maxsize=32)
def _prize_thresholds(event_id: int) -> tuple[int, ...]:
    """Return stable full-competition thresholds for Top 100 through Bronze 3.

    The local Top 100 is deterministic.  Using its settled bot table
    gives meaningful thresholds from the first fixture while keeping them
    stable across refreshes and restarts.  Elite occupies 3/3/4 percent, then
    Gold, Silver and Bronze use ten-percent bands.  Bronze 3 deliberately has
    a zero floor so any completed fixture earns a weekly placement.
    """
    scores=[int(row["value"]) for row in leaderboard_bots(
        int(event_id), LEADERBOARD_SIZE, MAX_POINT_MATCHES)]
    cumulative=(3, 6, 10, 20, 30, 40, 50, 60, 70, 80, 90)
    historical=[scores[min(len(scores), boundary)-1]
                for boundary in cumulative]
    # Top 100 needs its own floor above Elite 1; Bronze 3 needs zero so a
    # completed fixture can always earn a weekly placement.
    former=[historical[0]]+[int(round(value*0.8)) for value in historical]+[0]
    # Integer three-fifths rounding keeps the requested 40% reduction exact at
    # whole-point boundaries without binary floating-point drift.
    return tuple((value*3+2)//5 for value in former)


def prize_tiers(event_id: int) -> list[dict[str, Any]]:
    """Build Top 100 and all twelve native-order rank tiers."""
    thresholds=_prize_thresholds(int(event_id))
    tiers=[]
    for index,(spec,minimum) in enumerate(zip(PRIZE_TIER_SPECS,thresholds)):
        maximum=(2_147_483_647 if index == 0 else thresholds[index-1]-1)
        awards=[]
        coins=max(0,int(spec["coins"]))
        if coins:
            awards.append({"type":"coin","value":coins,"count":1})
        awards.extend({"type":"pack","value":int(pack_id),
                       "count":max(1,int(count))}
                      for pack_id,count in spec["packs"])
        pick_count=max(0,int(spec.get("pickCount",0) or 0))
        if pick_count:
            awards.append({"type":"playerPick","value":0,
                           "count":pick_count,"minRating":0,
                           "optionCount":FUT_CHAMPIONS_PICK_OPTIONS,
                           "isUntradeable":True,
                           "rewardPool":"FUT_CHAMPIONS_TOTW"})
        tiers.append({**spec,"minScore":int(minimum),
                      "maxScore":max(int(minimum),int(maximum)),
                      "awards":awards})
    return tiers


def prize_tier(event_id: int, user_score: int) -> dict[str, Any]:
    """Resolve a score to Top 100 or one of the twelve FIFA 19 rank tiers."""
    score=max(0,int(user_score or 0))
    for tier in prize_tiers(int(event_id)):
        if score >= int(tier["minScore"]):
            return tier
    return prize_tiers(int(event_id))[-1]


def tier_level(event_id: int, user_score: int) -> int:
    return int(prize_tier(event_id,user_score)["tierLevel"])

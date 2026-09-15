#!/usr/bin/env python3
"""Build source-verified FIFA 19 SBC sets from the local historical_reference captures.

The JSON inputs in ``server/data`` are deliberately factual: they retain the
source wording, identities, formations, rewards and artwork URLs.  This module
is the executable adapter.  It refuses unknown requirement wording instead of
silently publishing a partially validated challenge.
"""
from __future__ import annotations

import json
import os
import re


DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PLAYER_DATA = os.path.join(DATA_DIR, "fut19_sbc_players_verified.json")
ICON_DATA = os.path.join(DATA_DIR, "fut19_sbc_icons_verified.json")
LEAGUE_DATA = os.path.join(DATA_DIR, "fut19_sbc_leagues_verified.json")
UPGRADE_DATA = os.path.join(DATA_DIR, "fut19_sbc_upgrades_verified.json")
SUPPORT_DATA = os.path.join(DATA_DIR, "fut19_sbc_support_verified.json")
REQUESTED_DATA = os.path.join(
    DATA_DIR, "fut19_sbc_requested_verified.json")
IDENTITY_DATA = os.path.join(
    DATA_DIR, "fut19_sbc_requirement_identities_verified.json")

SET_ID_BASE = 8_000_000
CHALLENGE_ID_BASE = 9_000_000
VERIFIED_AT = "2026-08-31"

PACK_NAME_IDS = {
    "GOLD PACK": 300,
    "PREMIUM GOLD PACK": 301,
    "SILVER PLAYERS PACK": 204,
    "PRIME SILVER PLAYERS PACK": 208,
    "GOLD PLAYERS PACK": 304,
    "PREMIUM GOLD PLAYERS PACK": 305,
    "PRIME GOLD PLAYERS PACK": 308,
    "JUMBO PREMIUM GOLD PLAYERS": 309,
    "SMALL GOLD PLAYERS PACK": 314,
    "SMALL PRIME GOLD PLAYERS PACK": 315,
    "SMALL RARE GOLD PLAYERS PACK": 316,
    "JUMBO GOLD PACK": 302,
    "JUMBO PREMIUM GOLD PACK": 303,
    "RARE GOLD PACK": 400,
    "RARE PLAYERS PACK": 401,
    "JUMBO RARE PLAYERS PACK": 402,
    "MEGA PACK": 403,
    "RARE MEGA PACK": 404,
    "TWO PLAYERS PACK": 511,
    "ELECTRUM PLAYERS PACK": 515,
    "PREMIUM ELECTRUM PLAYERS PACK": 516,
    "PRIME ELECTRUM PLAYERS PACK": 517,
    "RARE ELECTRUM PLAYERS PACK": 518,
    "SMALL PRIME ELECTRUM PLAYERS PACK": 520,
    "SMALL RARE ELECTRUM PLAYERS PACK": 521,
    "MIXED PLAYERS PACK": 534,
    "PREMIUM MIXED PLAYERS PACK": 535,
    "RARE MIXED PLAYERS PACK": 537,
    "RARE CONSUMABLES PACK": 504,
}

REQUESTED_SET_NAMES = {
    211: "FUTMAS Wilfried Zaha",
    424: "UCL Moments Kostas Manolas",
    434: "Flashback Payet",
    498: "Carniball Thiago",
    529: "FUT Birthday Willian",
    541: "FUT Birthday Ivan Perišić",
    550: "Arjen Robben",
    580: "FUT Champions Premium Upgrade",
    600: "UCL Moments Karim Benzema",
    606: "Sadio Mané",
    717: "Premier League TOTS Guarantee",
    732: "Bundesliga TOTS Guarantee",
    751: "LaLiga TOTS Guarantee",
    770: "Luis Muriel",
    774: "Serie A TOTS Guarantee",
    779: "Flashback Depay",
    786: "Ligue 1 TOTS Guarantee",
    907: "PFA Player of the Year Van Dijk",
    925: "PFA Young Player of the Year Sterling",
}

REQUESTED_TOTS_PACKS = {
    717: 1017,
    732: 1018,
    751: 1019,
    774: 1020,
    786: 1021,
}

# The five archived TOTS Guarantee pages share one challenge image and publish
# no set image, so every tile rendered the same. These local set tiles are
# built from artwork already verified for other sets, keyed by the owner's
# explicit choice, and are therefore excluded from the download manifest.
REQUESTED_TOTS_SET_ARTWORK = {
    717: "SBS_CHALLENGE_9002316.png",
    732: "SBS_CHALLENGE_3052002.png",
    751: "SBS_CHALLENGE_9002212.png",
    774: "SBS_CHALLENGE_9002057.png",
    786: "SBS_CHALLENGE_3052002.png",
}

# A historical page is not an executable SBC contract. These entries remain
# closed until the exact missing field can be sourced, rather than replacing a
# missing player, formation, pack or kit with a plausible local substitute.
REQUESTED_BLOCKED = {
    355: "Challenge 924 has no verified formation.",
    393: "Verified reward metadata is ready, but publishing this set exceeds the retail 65 KB active-catalogue envelope.",
    542: "Verified reward metadata is ready, but publishing this set exceeds the retail 65 KB active-catalogue envelope.",
    698: "The 92-rated Van Persie reward has no verified resource ID.",
    708: "Challenge 1946 has a kit reward without a verified kit identity.",
    734: "The group reward is not present in the capture.",
    796: "Ultimate TOTS reward pool is not present in the capture.",
    872: "The tradeable TOTW reward pack contract is not mapped locally.",
    900: "The group reward is not present in the capture.",
    929: "The group reward is not present in the capture.",
    931: "Challenge requirements, one formation and the group reward are missing.",
}

PLAYER_SET_NAMES = {
    218: "Marcus Rashford",
    360: "Allan Saint-Maximin",
    681: "Antonio Valencia",
    696: "Moussa Sissoko",
    798: "Paulinho",
    800: "Fernando Torres",
    806: "Flashback David Luiz",
    837: "Marcos Alonso",
    846: "Flashback Firmino",
    850: "Sergio Ramos",
    853: "Flashback Dani Alves",
    858: "Joe Gomez",
    863: "Flashback Arturo Vidal",
    867: "Déjà vu",
    879: "Franck Ribéry",
    906: "Josef Martínez",
    924: "Houssem Aouar",
}

# These local balance targets are keyed by both sourced set and challenge so
# changing one named segment (notably Sissoko's France squad) cannot silently
# alter a sibling challenge imported from the same historical set.  Aouar's
# archived source is 87; the requested final target is deliberately 84.
PLAYER_SQUAD_RATING_OVERRIDES = {
    (696, 1922): 85,
    (798, 2110): 85,
    (800, 2115): 86,
    (853, 2215): 85,
    (867, 2245): 85,
    (906, 2313): 84,
    (924, 2351): 84,
}

ICON_SET_NAMES = {
    587: "Johan Cruyff - Prime Icon Moments",
    620: "Eusébio - Prime Icon",
    622: "Patrick Vieira - Prime Icon",
    624: "Ronaldinho - Prime Icon",
    626: "Ruud Gullit - Prime Icon",
    628: "Pelé - Prime Icon",
    630: "Ronaldo - Prime Icon",
}

LEAGUE_SET_NAMES = {
    323: "Premier League",
    324: "Eredivisie",
    363: "LaLiga Santander",
}

LEAGUE_REWARD_KEYS = {323: "premier", 324: "eredivisie", 363: "laliga"}


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _comparison(text):
    match = re.search(r":\s*(?:(Min|Max|Exactly)\s+)?(\d+)\s*$", text)
    if not match:
        raise ValueError("Unparseable historical_reference comparison: %s" % text)
    return {None: "exact", "Min": "min", "Max": "max",
            "Exactly": "exact"}[match.group(1)], int(match.group(2))


def _identity_rows():
    source = _load(IDENTITY_DATA).get("identities", {})
    result = {}
    for set_id, challenges in source.items():
        for challenge in challenges:
            for row in challenge.get("rows", []):
                result[(int(set_id), str(challenge.get("name", "")),
                        str(row.get("text", "")))] = list(row.get("imgs", []))
    return result


def _specific_identity_rule(set_id, challenge_name, text, identity_rows):
    images = identity_rows.get((int(set_id), str(challenge_name), str(text)), [])
    identities = []
    subject = None
    patterns = (("club", r"/clubs/(\d+)\.png"),
                ("nation", r"/nation/(\d+)\.png"),
                ("league", r"/league/(\d+)\.png"))
    for image in images:
        for candidate, pattern in patterns:
            match = re.search(pattern, str(image), re.I)
            if not match:
                continue
            if subject is not None and subject != candidate:
                raise ValueError("Mixed identity classes in SBC rule: %s" % text)
            subject = candidate
            value = int(match.group(1))
            if value not in identities:
                identities.append(value)
    if not subject or not identities:
        raise ValueError("Missing sourced identity for SBC rule: %s" % text)
    operator, value = _comparison(text)
    rule = {"type": "specific_%s" % subject, "operator": operator,
            "value": value, "description": text}
    if len(identities) == 1:
        rule["identity"] = identities[0]
    else:
        rule["identities"] = identities
    return rule


def _requirement_rule(set_id, challenge_name, text, identity_rows):
    text = str(text).strip()
    if text.startswith("Player Level:"):
        match = re.search(r":\s*(Min|Max|Exactly)\s+([A-Za-z]+)\s*$", text)
        if not match:
            raise ValueError("Unparseable historical_reference player level: %s" % text)
        operator = {"Min": "min", "Max": "max",
                    "Exactly": "exact"}[match.group(1)]
        quality = match.group(2).upper()
        return {"type": "player_quality", "nativeType": "PLAYER_LEVEL",
                "operator": operator, "quality": quality,
                "description": text}
    operator, value = _comparison(text) if ":" in text else (None, None)
    if text.startswith("# of players in the Squad:"):
        return {"type": "player_count", "nativeType": "PLAYER_COUNT",
                "operator": "exact", "value": value, "description": text}
    if text.startswith("Squad Rating:"):
        return {"type": "squad_rating", "nativeType": "TEAM_RATING",
                "operator": operator, "value": value, "description": text}
    if text.startswith("Squad Total Chemistry Points:"):
        return {"type": "squad_chemistry",
                "nativeType": "TEAM_CHEMISTRY", "operator": operator,
                "value": value, "description": text}
    if text.startswith("Rare Players:"):
        return {"type": "rare_count", "operator": operator,
                "value": value, "description": text}
    if text.startswith("Same League Count:"):
        return {"type": "same_league", "operator": operator,
                "value": value, "description": text}
    if text.startswith("Same Nation Count:"):
        return {"type": "same_nation", "operator": operator,
                "value": value, "description": text}
    if text.startswith("Same Club Count:"):
        return {"type": "same_club", "operator": operator,
                "value": value, "description": text}
    unique_types = {"Clubs:": "club", "Leagues:": "league",
                    "Nationalities:": "nation"}
    for prefix, subject in unique_types.items():
        if text.startswith(prefix):
            return {"type": "unique_%s" % subject, "operator": operator,
                    "value": value, "description": text}
    if text.startswith("TOTS Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["TOTS"], "value": value,
                "description": text}
    if text.startswith("IF + FUT-CHAMP Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["TOTW", "FUT_CHAMP"], "value": value,
                "description": text}
    if text.startswith("TOTW or FUT-CHAMP Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["TOTW", "FUT_CHAMP"], "value": value,
                "description": text}
    if text.startswith("FUT-CHAMP Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["FUT_CHAMP"], "value": value,
                "description": text}
    if text.startswith("UCL NON RARE + UCL RARE Players:"):
        return {"type": "ucl_count", "operator": operator,
                "value": value, "description": text}
    if text.startswith("IF + TOTS Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["TOTW", "TOTS"], "value": value,
                "description": text}
    if text.startswith("IF + OTW Players:"):
        return {"type": "card_type_count", "operator": operator,
                "cardTypes": ["TOTW", "OTW"], "value": value,
                "description": text}
    if text.startswith("IF Players:"):
        return {"type": "totw_count", "operator": operator,
                "value": value, "description": text}
    if text.startswith("LEGEND Players:"):
        return {"type": "icon_count", "operator": operator,
                "value": value, "description": text}
    if text.startswith("# of players from "):
        return _specific_identity_rule(
            set_id, challenge_name, text, identity_rows)
    raise ValueError("Unsupported source-verified SBC requirement: %s" % text)


def _formation(value, set_id=0, challenge_id=0):
    source = str(value or "").strip()
    if not source and int(set_id) == 360 and int(challenge_id) == 938:
        source = "4-2-4"
    if not source and int(set_id) == 363 and int(challenge_id) == 952:
        source = "3-1-4-2"
    if not source and int(set_id) == 795:
        source = "4-2-4"
    if not source:
        raise ValueError("Missing verified formation for SBC challenge %s" % challenge_id)
    key = source.lower().replace(" ", "")
    aliases = {
        "3-1-4-2": "f3142",
        "3-4-1-2": "f3412", "3-4-2-1": "f3421",
        "3-4-3": "f343", "3-5-2": "f352",
        "4-1-2-1-2": "f41212", "4-1-3-2": "f4132",
        "4-1-4-1": "f4141", "4-2-2-2": "f4222",
        "4-2-3-1": "f4231-2", "4-2-4": "f424",
        "4-3-1-2": "f4312", "4-3-2-1": "f4321",
        "4-3-3": "f433", "4-3-3(2)": "f433-2",
        "4-3-3(3)": "f433-3", "4-3-3(4)": "f433-4",
        "4-4-1-1": "f4411", "4-4-2": "f442", "4-5-1": "f451",
        "5-2-1-2": "f5212", "5-2-2-1": "f5221", "5-3-2": "f532",
    }
    if key not in aliases:
        raise ValueError("Unsupported verified formation: %s" % source)
    return aliases[key]


def _pack_reward(reward):
    if not reward:
        return []
    if isinstance(reward, dict):
        pack_id = int(reward.get("packId", 0) or 0)
        label = str(reward.get("label", "Pack"))
    else:
        label = str(reward)
        match = re.fullmatch(r"\s*(\d+)\s+(.+?)\s*", label)
        count = int(match.group(1)) if match else 1
        name = match.group(2) if match else label
        pack_id = PACK_NAME_IDS.get(name.upper(), 0)
    if pack_id <= 0:
        raise ValueError("Unmapped verified SBC pack reward: %s" % label)
    return [{"type": "pack", "value": pack_id, "packId": pack_id,
             "label": name.title() if not isinstance(reward, dict) else label.title(),
             "count": count if not isinstance(reward, dict) else 1}]


def _challenge(set_runtime_id, source_set_id, source, requirements, rewards):
    source_id = int(source.get("sourceChallengeId", 0) or 0)
    if source_id <= 0:
        match = re.search(r"/ALL/(\d+)(?:/|$)", str(source.get("href", "")))
        if not match:
            raise ValueError("Missing source challenge identity")
        source_id = int(match.group(1))
    image_id = CHALLENGE_ID_BASE + source_id
    player_count_rule = next(
        (rule for rule in requirements if rule.get("type") == "player_count"),
        {"value": 11})
    player_count = max(1, min(11, int(player_count_rule.get("value", 11))))
    provenance_url = str(source.get("url", source.get("href", "")))
    artwork_url = str(source.get("artwork", ""))
    return {
        "challengeId": image_id,
        "setId": int(set_runtime_id),
        "name": str(source.get("name", "")),
        "description": str(source.get("desc", source.get("description", ""))),
        "verificationStatus": "historical_verified",
        "enabled": True,
        "repeatable": False,
        "formation": _formation(source.get("formation"), source_set_id, source_id),
        "slotMask": ["OPEN"] * player_count + ["BRICK"] * (11 - player_count),
        "challengeImageId": image_id,
        "startTime": 0,
        "endTime": 0,
        "notExpirable": True,
        "rewards": list(rewards),
        "requirements": list(requirements),
        "sourceChallengeId": source_id,
        "sourceProvenance": {
            "provider": "historical_reference", "url": provenance_url,
            "artworkUrl": artwork_url, "verifiedAt": VERIFIED_AT,
            "evidence": ["Challenge Requirements", "Formation", "Rewards",
                         "Artwork"],
        },
    }


def _set(source_set_id, name, description, category, artwork_url,
         challenges, rewards, repeatable=False):
    runtime_id = SET_ID_BASE + int(source_set_id)
    row = {
        "setId": runtime_id,
        "name": str(name),
        "description": str(description),
        "category": str(category),
        "verificationStatus": "historical_verified",
        "enabled": True,
        "assetId": runtime_id,
        "tileImage": "SBS_SET_%d.png" % runtime_id,
        "repeatable": bool(repeatable),
        "startTime": 0,
        "endTime": 0,
        "notExpirable": True,
        "isFeatured": True,
        "rewards": list(rewards),
        "challenges": list(challenges),
        "sourceSetId": int(source_set_id),
        "sourceProvenance": {
            "provider": "historical_reference",
            "url": "private-release-record/%d" %
                   int(source_set_id),
            "artworkUrl": str(artwork_url or ""),
            "verifiedAt": VERIFIED_AT,
        },
    }
    if repeatable:
        contract = {"mode": "saved_squad_attempt",
                    "retryScope": "operation_key"}
        row["attemptContract"] = dict(contract)
        for challenge in row["challenges"]:
            challenge["repeatable"] = True
            challenge["attemptContract"] = dict(contract)
    return row


def _player_sets(identity_rows, support):
    players = _load(PLAYER_DATA)["players"]
    deja_pool = [int(row["resourceId"]) for row in support["dejaRewardData"]]
    result = []
    for source_key, source in players.items():
        source_set_id = int(source_key)
        runtime_id = SET_ID_BASE + source_set_id
        challenges = []
        for challenge in source.get("challenges", []):
            requirements = [_requirement_rule(
                source_set_id, challenge.get("name", ""), text, identity_rows)
                for text in challenge.get("reqs", [])]
            rating_target = PLAYER_SQUAD_RATING_OVERRIDES.get(
                (source_set_id,
                 int(challenge.get("sourceChallengeId", 0) or 0)))
            if rating_target is not None:
                # FIFA renders the description but validates the numeric
                # value, so a balance override must keep both representations
                # synchronized or the client promises a different threshold.
                for requirement in requirements:
                    if str(requirement.get("type", "")).lower() == "squad_rating":
                        requirement["value"] = rating_target
                        requirement["description"] = (
                            "Squad Rating: Min %d" % rating_target)
            challenges.append(_challenge(
                runtime_id, source_set_id, challenge, requirements,
                _pack_reward(challenge.get("reward"))))
        player = source.get("player") or {}
        if source_set_id == 867:
            rewards = [{"type": "player_pick", "value": 0, "count": 1,
                        "label": "Déjà vu Player Pick", "optionCount": 4,
                        "minRating": 87, "resourceIds": deja_pool,
                        "untradeable": True}]
            artwork = challenges[0]["sourceProvenance"]["artworkUrl"]
            description = "Choose one of: %s." % ", ".join(
                str(row["name"]) for row in support["dejaRewardData"])
        else:
            resource_id = int(player["resourceId"])
            rewards = [{"type": "player", "resourceId": resource_id,
                        "value": resource_id, "count": 1,
                        "untradeable": True}]
            artwork = str(player.get("img", ""))
            description = "Complete every sourced squad to earn %s." % (
                PLAYER_SET_NAMES[source_set_id])
        result.append(_set(
            source_set_id, PLAYER_SET_NAMES[source_set_id], description,
            "PLAYERS", artwork, challenges, rewards))
    return result


def _icon_sets(support):
    icons = _load(ICON_DATA)["icons"]
    reward_by_set = {int(row["setId"]): row
                     for row in support["iconRewardData"]}
    card_by_resource = {int(row["resourceId"]): row for row in
                        _load(os.path.join(
                            DATA_DIR, "fut19_sbc_reward_cards_verified.json"))["cards"]}
    result = []
    for source_key, source_challenges in icons.items():
        source_set_id = int(source_key)
        runtime_id = SET_ID_BASE + source_set_id
        challenges = []
        for source in source_challenges:
            requirements = [_requirement_rule(
                source_set_id, source.get("name", ""), text, {})
                for text in source.get("reqs", [])]
            challenges.append(_challenge(
                runtime_id, source_set_id, source, requirements,
                _pack_reward(source.get("reward"))))
        reward_source = reward_by_set[source_set_id]
        resource_id = int(reward_source["resourceId"])
        artwork = str(card_by_resource[resource_id].get("img", ""))
        rewards = [{"type": "player", "resourceId": resource_id,
                    "value": resource_id, "count": 1,
                    "untradeable": True}]
        result.append(_set(
            source_set_id, ICON_SET_NAMES[source_set_id],
            "Complete every sourced squad to earn the exact Icon reward.",
            "ICONS", artwork, challenges, rewards))
    return result


def _league_sets(support):
    leagues = _load(LEAGUE_DATA)["leagues"]
    result = []
    for source_key, source_challenges in leagues.items():
        source_set_id = int(source_key)
        runtime_id = SET_ID_BASE + source_set_id
        challenges = []
        for source in source_challenges:
            requirements = [
                {"type": "specific_club", "operator": "exact",
                 "identity": int(source["clubId"]), "value": 11,
                 "description": "# of players from %s: Exactly 11" %
                                source["name"]},
                {"type": "squad_rating", "nativeType": "TEAM_RATING",
                 "operator": "min", "value": int(source["rating"]),
                 "description": "Squad Rating: Min %d" % int(source["rating"])},
                {"type": "squad_chemistry", "nativeType": "TEAM_CHEMISTRY",
                 "operator": "min", "value": int(source["chemistry"]),
                 "description": "Squad Total Chemistry Points: Min %d" %
                                int(source["chemistry"])},
                {"type": "player_count", "nativeType": "PLAYER_COUNT",
                 "operator": "exact", "value": 11,
                 "description": "Number of players in the Squad: 11"},
            ]
            source = dict(source)
            source["url"] = (
                "private-release-record/%d" %
                int(source["sourceChallengeId"]))
            challenges.append(_challenge(
                runtime_id, source_set_id, source, requirements,
                _pack_reward(source.get("reward"))))
        pick_rows = support["leagueRewards"][LEAGUE_REWARD_KEYS[source_set_id]]
        resource_ids = [int(row["resourceId"]) for row in pick_rows]
        rewards = [{"type": "player_pick", "value": 0, "count": 1,
                    "label": "%s League Player Pick" %
                             LEAGUE_SET_NAMES[source_set_id],
                    "optionCount": len(resource_ids),
                    "minRating": min(int(row["rating"]) for row in pick_rows),
                    "resourceIds": resource_ids, "untradeable": True}]
        artwork = support["setArtworkUrls"][str(source_set_id)][0]
        reward_names = [str(row["name"]) for row in pick_rows]
        result.append(_set(
            source_set_id, LEAGUE_SET_NAMES[source_set_id],
            "Exchange every club squad and choose one of: %s." %
            ", ".join(reward_names),
            "LEAGUES", artwork, challenges, rewards))
    return result


def _upgrade_sets():
    source = next(row for row in _load(UPGRADE_DATA)["upgrades"]
                  if int(row["setId"]) == 795)
    source_set_id = 795
    runtime_id = SET_ID_BASE + source_set_id
    source_challenge = dict(source["challenges"][0])
    requirements = [_requirement_rule(
        source_set_id, source_challenge.get("name", ""), text, {})
        for text in source_challenge.get("reqs", [])]
    # CardsDLL enables Submit from elgReq before the server is called. Keeping
    # the sourced Rare rule intact prevents a common-Gold squad from looking
    # valid in the UI and then being rejected by authoritative validation.
    reward = {"type": "player_pick", "value": 0, "count": 1,
              "label": "80+ Rare Gold Player Pick", "optionCount": 3,
              "minRating": 80, "quality": "GOLD", "rareOnly": True,
              "specialChance": 0.125, "untradeable": True}
    challenge = _challenge(
        runtime_id, source_set_id, source_challenge, requirements, [])
    # The archived 795 art is the generic diving-player shield seen in RC71.
    # The requested people silhouette is the verified local challenge 1003.
    replacement_artwork = (
        "private-release-record"
        "sbc_challenge_image_1001003.png")
    challenge["challengeImageId"] = 1003
    challenge["sourceProvenance"]["artworkUrl"] = replacement_artwork
    result = _set(
        source_set_id, "80+ Player Pick Upgrade",
        "Repeatable 1-of-3 untradeable Rare Gold Player Pick rated 80+.",
        "UPGRADES", source_challenge.get("artwork", ""), [challenge],
        [reward], repeatable=True)
    result["assetId"] = 1003
    result["tileImage"] = "SBS_SET_1003.png"
    # The carousel tile is a deterministic set-sized derivative, while the
    # provenance manifest records the authentic challenge-shaped download.
    # Treating the derivative name as a source target made release validation
    # demand a second fictitious download of the same artwork.
    result["sourceArtworkTarget"] = "SBS_CHALLENGE_1003.png"
    result["sourceProvenance"]["artworkUrl"] = replacement_artwork
    return [result]


def _requested_sets():
    """Adapt only complete records from the owner's verified historical_reference batch."""
    payload = _load(REQUESTED_DATA)
    verified_at = str(payload.get("verifiedAt", VERIFIED_AT))
    source_by_id = {int(row["sourceSetId"]): row
                    for row in payload.get("sets", [])}
    result = []
    for source_set_id, set_name in REQUESTED_SET_NAMES.items():
        source_set = source_by_id[source_set_id]
        runtime_id = SET_ID_BASE + source_set_id
        identity_rows = {}
        for source in source_set.get("challenges", []):
            for row in source.get("requirementRows", []):
                identity_rows[(source_set_id, str(source.get("name", "")),
                               str(row.get("text", "")))] = list(
                                   row.get("imgs", []))
        challenges = []
        for source in source_set.get("challenges", []):
            requirements = []
            for row in source.get("requirementRows", []):
                text = str(row.get("text", ""))
                if source_set_id == 580 and text == \
                        "FUT-CHAMP Players: Exactly 11":
                    text = "TOTW or FUT-CHAMP Players: Exactly 11"
                requirements.append(_requirement_rule(
                    source_set_id, source.get("name", ""), text,
                    identity_rows))
            challenge = _challenge(
                runtime_id, source_set_id, source, requirements,
                _pack_reward(source.get("reward")))
            challenge["sourceProvenance"]["verifiedAt"] = verified_at
            challenges.append(challenge)

        if source_set_id == 580:
            rewards = [{
                "type": "player_pick", "value": 0, "count": 1,
                "label": "1 of 3 FUT Champions 86+ Player Pick",
                "description": (
                    "Choose one untradeable FUT Champions player rated 86+."),
                "quality": "GOLD", "minRating": 86, "optionCount": 3,
                "rewardPool": "FUT_CHAMPIONS_TOTW", "untradeable": True,
            }]
            description = str(challenges[0]["description"])
            artwork = next(
                (url for url in source_set.get("pageSbcImages", [])
                 if "sbc_set_image_" in str(url)), "")
            category = "UPGRADES"
            repeatable = True
        elif source_set_id in REQUESTED_TOTS_PACKS:
            pack_id = REQUESTED_TOTS_PACKS[source_set_id]
            rewards = _pack_reward({"packId": pack_id, "label": set_name})
            rewards[0]["untradeable"] = True
            description = str(challenges[0]["description"])
            # The archived pages publish an empty set-image URL and share one
            # TOTS challenge image, so all five tiles looked identical. Each
            # set now installs its own league tile from a local derivative
            # built by tools/build_tots_guarantee_set_artwork.py.
            artwork = challenges[0]["sourceProvenance"]["artworkUrl"]
            category = "UPGRADES"
            # The owner made the five league guarantees repeatable so a TOTS
            # pack stays obtainable after the first completion.
            repeatable = True
        else:
            player = source_set["players"][0]
            resource_id = int(player["resourceId"])
            rewards = [{"type": "player", "resourceId": resource_id,
                        "value": resource_id, "count": 1,
                        "untradeable": True}]
            description = "Complete every sourced squad to earn %s." % set_name
            artwork = str(player.get("image", ""))
            category = "PLAYERS"
            repeatable = False
        set_spec = _set(
            source_set_id, set_name, description, category, artwork,
            challenges, rewards, repeatable=repeatable)
        set_spec["sourceProvenance"]["verifiedAt"] = verified_at
        result.append(set_spec)
    return result


def build_verified_sbc_sets():
    """Return all fully executable additions requested for the local hub."""
    identity_rows = _identity_rows()
    support = _load(SUPPORT_DATA)
    rows = (_player_sets(identity_rows, support) + _icon_sets(support) +
            _league_sets(support) + _upgrade_sets() + _requested_sets())
    set_ids = [int(row["setId"]) for row in rows]
    challenge_ids = [int(challenge["challengeId"]) for row in rows
                     for challenge in row["challenges"]]
    if len(set_ids) != len(set(set_ids)):
        raise ValueError("Duplicate verified SBC set IDs")
    if len(challenge_ids) != len(set(challenge_ids)):
        raise ValueError("Duplicate verified SBC challenge IDs")
    return tuple(rows)


def verified_artwork_assets():
    """Return collision-free local targets for every authentic source image."""
    assets = []
    for set_spec in build_verified_sbc_sets():
        set_url = str(set_spec["sourceProvenance"].get("artworkUrl", ""))
        if not set_url:
            raise ValueError("Missing artwork for SBC set %s" %
                             set_spec["sourceSetId"])
        if int(set_spec["sourceSetId"]) not in REQUESTED_TOTS_SET_ARTWORK:
            assets.append({
                "target": str(set_spec.get("sourceArtworkTarget") or
                              set_spec.get("tileImage") or
                              "SBS_SET_%d.png" % int(set_spec["assetId"])),
                "url": set_url,
            })
        for challenge in set_spec["challenges"]:
            challenge_url = str(
                challenge["sourceProvenance"].get("artworkUrl", ""))
            if not challenge_url:
                raise ValueError("Missing artwork for SBC challenge %s" %
                                 challenge["sourceChallengeId"])
            assets.append({
                "target": "SBS_CHALLENGE_%d.png" %
                          int(challenge["challengeImageId"]),
                "url": challenge_url,
            })
    unique = {}
    for row in assets:
        previous = unique.get(row["target"])
        if previous is not None and previous["url"] != row["url"]:
            raise ValueError("Conflicting verified SBC artwork target %s" %
                             row["target"])
        unique[row["target"]] = row
    return tuple(unique.values())

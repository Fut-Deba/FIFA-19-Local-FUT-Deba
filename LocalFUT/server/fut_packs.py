#!/usr/bin/env python3
"""Local FIFA 19 FUT pack catalogue and deterministic pack generator."""

from __future__ import annotations

import json
import os
import random

from fut_catalog import card_revision, card_version_rows, native_player_fields
from fut_objects import object_catalog


DATA_DIR=os.path.join(os.path.dirname(os.path.abspath(__file__)),"data")
with open(os.path.join(DATA_DIR,"pack_defs.json"),"r",encoding="utf-8") as handle:
    PACKS={int(key):value for key,value in json.load(handle).items()}
with open(os.path.join(DATA_DIR,"pack_weights.json"),"r",encoding="utf-8") as handle:
    SPECIAL_WEIGHTS=json.load(handle)

_normal_pools = None
_special_pool = None
_object_pack_pools = None
PACK_REVEAL_OBJECT_CATEGORIES = ("consumable",)


_OBJECT_CATEGORY_WEIGHTS = {
    "consumable": 55,
    "staff": 10,
    "manager": 10,
    "badge": 7,
    "kit": 8,
    "stadium": 5,
    "ball": 5,
}
_STAFF_OBJECT_TYPES = {"headcoach", "fitnesscoach", "physio", "gkcoach"}

# These revisions were distributed through packs in FUT 19. Reward-only cards
# (SBC, objectives, Swap Deals and loans) must never enter a purchased pack.
PACKABLE_SPECIAL_REVISIONS = {
    "Icon", "IF", "SIF", "TIF", "TOTY", "TOTS", "OTW", "Halloween",
    "UCL LIVE", "TOTGS", "UEL LIVE", "Europa TOTGS", "CL",
    "FUT Future Stars", "TOTY Nominee", "Carniball", "FUTmas",
    "FUT Birthday", "Headliners", "Prime Icon Moments",
}

# The headline promotions. Each one is the selling point of its own pack, so
# outside that pack they must stay rare. Measured on 2026-09-03, they were
# instead 82% of every special found in a promo pack: buying a Carniball pack
# most often produced an OTW or a TOTGS and no Carniball at all.
PROMO_REVISIONS = {
    "Icon", "Prime Icon Moments", "TOTY", "TOTS", "OTW", "Carniball",
    "FUTmas", "FUT Birthday", "Headliners", "Halloween",
    "FUT Future Stars", "TOTY Nominee",
}
# Share of the non-featured special slots of a promo pack that may be drawn
# from another headline promotion. The rest come from the ordinary specials
# that exist all season (IF, SIF, TOTGS, UCL LIVE...).
DEFAULT_OTHER_PROMO_CHANCE = 0.08

# FUT 19 Store PackTier values recovered from the official client.  Seasonal
# FX ids (30..37) are valid only for the opening animation.  The Icon pack is
# the sole local promotion with a verified dedicated Store tier: LEGENDS=8.
STORE_ASSET_BY_PACK = {406: 8, 4008: 8, 414: 8}

# Complete historical per-card odds are not available. Both account modes use
# the same project-authored pack configuration: RTG progression comes from its
# closed economy, not from an artificial reduction in pack quality.
RTG_SPECIAL_CHANCE_MULTIPLIER = 1.0
RTG_FEATURED_CHANCE_MULTIPLIER = 1.0


def _profile_probability(value, probability_profile, multiplier):
    chance=max(0.0,min(1.0,float(value or 0)))
    if str(probability_profile or "default").strip().lower() == "rtg":
        chance*=float(multiplier)
    return max(0.0,min(1.0,chance))


def pack_item_rareflag(resource_id, guaranteed_rare=False):
    """Return the native rarity flag without downgrading elite base cards.

    The source catalogue does not contain the base-card common/rare bit. Pack
    slots therefore remain the fallback for ordinary 75-79 cards, while every
    80+ base card is treated as rare. Special revisions keep their exact FUT
    design through ``native_player_fields``.
    """
    fields=native_player_fields(int(resource_id))
    if card_revision(int(resource_id)) != "Normal":
        return int(fields.get("rareflag",1) or 1)
    return 1 if guaranteed_rare or int(fields.get("rating",0) or 0) >= 80 else 0


def _pools():
    global _normal_pools, _special_pool
    if _normal_pools is not None:
        return _normal_pools, _special_pool
    normal = {"bronze": [], "silver": [], "gold": []}
    special = []
    for row in card_version_rows():
        rid = int(row["resourceId"])
        rating = int(row.get("rating", 0) or 0)
        revision = str(row.get("rev") or "Normal")
        if revision == "Normal":
            tier = "bronze" if rating < 65 else ("silver" if rating < 75 else "gold")
            normal[tier].append((rid, rating))
        elif revision in PACKABLE_SPECIAL_REVISIONS:
            special.append((rid, rating, revision))
    _normal_pools, _special_pool = normal, special
    return normal, special


def _pack_tier(rating):
    value=int(rating or 0)
    return "bronze" if value < 65 else ("silver" if value < 75 else "gold")


def _packable_consumable(definition):
    item_type=str(definition.get("ItemType","") or "")
    if item_type == "ContractStaff":
        # Manager contracts remain available in the complete club catalogue,
        # but are excluded from random pack reveals. The native pack-opening
        # item factory does not reliably instantiate this concrete member.
        return False
    if item_type == "TrainingLeagueModifier":
        # The source table also contains internal FUT leagues used by
        # IceBreaker/custom-kit/Legends content. They share the outer type,
        # but were never packable manager-league cards and render as an empty
        # black silhouette in the retail unassigned pile. Real FIFA 19 league
        # modifiers in this table end at league 2076 (3. Liga).
        try:
            return 0 < int(definition.get("Amount",0) or 0) <= 2076
        except (TypeError,ValueError):
            return False
    return bool(
        item_type in {"ContractPlayer","FitnessPlayer",
                      "FitnessTeam"} or
        item_type.startswith("Health") or
        item_type.startswith("TrainingGk") or
        item_type.startswith("TrainingPlayer") or
        item_type.startswith("TrainingPlaystyle"))


def _object_pools():
    """Return source-backed non-player pack pools by tier/rarity/category."""
    global _object_pack_pools
    if _object_pack_pools is not None:
        return _object_pack_pools
    pools={tier:{rare:{} for rare in (0,1)}
           for tier in ("bronze","silver","gold")}
    for definition in object_catalog().values():
        catalog_type=str(definition.get("_type","") or "").lower()
        if catalog_type == "consumable" and not _packable_consumable(definition):
            # Coin, pack, crowd and empty database rows are not card items the
            # FUT 19 consumable parser can place in the unassigned pile.
            continue
        if catalog_type not in {
                "manager","headcoach","fitnesscoach","physio","gkcoach",
                "badge","stadium","consumable"}:
            continue
        # The FIFA 19 unassigned-pile renderer has no safe generic card
        # subtype for balls.  A real BallName_* object is therefore displayed
        # as the empty black FUT 19 card seen in Premium Gold Pack opening 26.
        # Balls remain available in My Club/default grants; only random pack
        # generation excludes them.
        rating=int(definition.get("Rating",definition.get("Value",0)) or 0)
        tier=_pack_tier(rating)
        rare=1 if int(definition.get("Rare",0) or 0) else 0
        resource_id=int(definition.get("_resourceId",0) or 0)
        if not resource_id:
            continue
        category=("staff" if catalog_type in _STAFF_OBJECT_TYPES
                  else catalog_type)
        candidate={
            "resourceId":resource_id,
            "rating":rating,
            "rareflag":rare,
            "category":category,
            "objectSpec":{"type":catalog_type,"resourceId":resource_id},
        }
        pools[tier][rare].setdefault(category,[]).append(candidate)
        if catalog_type == "badge":
            # FIFA's source database exposes both kit textures through the
            # same real team identity used by the badge definition.
            team_id=int(definition.get("_teamId",definition.get("ClubId",0)) or 0)
            if team_id:
                for home,grant_type in ((True,"kit_home"),(False,"kit_away")):
                    kit_resource=(6300000 if home else 6400000)+team_id
                    pools[tier][rare].setdefault("kit",[]).append({
                        "resourceId":kit_resource,
                        "rating":rating,
                        "rareflag":rare,
                        "category":"kit",
                        "objectSpec":{"type":grant_type,"teamId":team_id,
                                      "category":2 if home else 3},
                    })
    _object_pack_pools=pools
    return pools


def _choose_pack_object(tier,rare,rng,used_objects,allowed_categories=None):
    pools=_object_pools()[str(tier)][1 if rare else 0]
    allowed={str(name).strip().lower() for name in (
        allowed_categories or PACK_REVEAL_OBJECT_CATEGORIES) if str(name).strip()}
    categories=[name for name,rows in pools.items()
                if rows and str(name).lower() in allowed]
    if not categories:
        raise ValueError("no %s %s object pack pool for %s" % (
            "rare" if rare else "common",tier,
            ",".join(sorted(allowed)) or "<none>"))
    weights=[float(_OBJECT_CATEGORY_WEIGHTS.get(name,1))
             for name in categories]
    chosen=None
    for unused in range(200):
        category=rng.choices(categories,weights=weights,k=1)[0]
        candidate=rng.choice(pools[category])
        identity=(str(candidate["objectSpec"].get("type","")),
                  int(candidate["resourceId"]))
        if identity not in used_objects:
            chosen=dict(candidate)
            used_objects.add(identity)
            break
    if chosen is None:
        candidates=[dict(row) for category in categories
                    for row in pools[category]
                    if (str(row["objectSpec"].get("type","")),
                        int(row["resourceId"])) not in used_objects]
        if not candidates:
            candidates=[dict(row) for category in categories
                        for row in pools[category]]
        chosen=rng.choice(candidates)
        used_objects.add((str(chosen["objectSpec"].get("type","")),
                          int(chosen["resourceId"])))
    return chosen


def guaranteed_revision_names(pack):
    """Return the revisions a pack guarantees, single or multi-promo."""
    names = [str(name) for name in (pack or {}).get("guaranteedRevisions", [])
             if str(name)]
    single = str((pack or {}).get("guaranteedRevision", "") or "")
    if single and single not in names:
        names.append(single)
    return names


def pack_definition(pack_id):
    row = PACKS.get(int(pack_id))
    if row and row.get("requiredRevision") not in (None,""):
        if str(row["requiredRevision"]) not in available_revisions():
            return None
    if row and row.get("guaranteedRevisions"):
        # A multi-promo guarantee stays advertised while at least one of its
        # revisions has source-backed cards. Advertising a pack whose whole
        # pool is absent would sell an empty guarantee.
        available = set(available_revisions())
        if not available.intersection(guaranteed_revision_names(row)):
            return None
    return dict(row, id=int(pack_id)) if row else None


def store_purchases(credits=0, unopened=None):
    result = []
    counts={}
    for row in unopened or []:
        pid=int(row.get("packId",0) or 0)
        if pid:
            counts[pid]=counts.get(pid,0)+1
    ordered_packs=sorted(
        PACKS.items(),
        key=lambda pair:(int(pair[1].get("storePriority",pair[0]) or pair[0]),
                         int(pair[0])))
    for pack_id, pack in ordered_packs:
        if pack_definition(pack_id) is None:
            # Keep event definitions ready for the complete late-season data
            # import, but never advertise a pack whose exact card revisions are
            # absent. Mapping base cards onto a special design is not valid.
            continue
        if bool(pack.get("hiddenFromStore",False)) and not counts.get(pack_id,0):
            # Reward-only definitions remain openable from My Packs without
            # creating an extra paid Store offer.
            continue
        if counts.get(pack_id,0):
            # The client keys Store offers by pack id. Advertising the paid
            # offer beside an unopened reward with the same id makes My Packs
            # resolve the paid offer and display the reward as Sold Out.
            continue
        promo=bool(pack.get("promo",False))
        tier=str(pack.get("tier","gold"))
        category_id=int(pack.get("categoryId",3) or 3)
        tier_asset_id={"bronze":1,"silver":2,"gold":3}[tier]
        # FIFA builds the Store navbar from the exact native display-group
        # token.  `special` is group/asset 4; mapping promotions onto Bronze
        # changes both the tab label and the pack-opening presentation.
        group="special" if promo else tier
        group_asset_id=4 if promo else tier_asset_id
        group_name="Promo Packs" if promo else tier.title()+" Packs"
        native_category_id=4 if promo else category_id
        # packAssetId selects the pack-opening seasonal FX.  StorePackView uses
        # PackTier identities instead; feeding FX ids such as 30/37 to assetId
        # makes it render the grey missing-image shell.
        native_asset_id=(STORE_ASSET_BY_PACK.get(pack_id,4)
                         if promo else tier_asset_id)
        opening_asset_id=int(pack.get("packAssetId",native_asset_id)
                             or native_asset_id)
        store_priority=int(pack.get("storePriority",pack_id) or pack_id)
        offer={
            "id": pack_id, "productId": str(pack_id), "packType": pack_id,
            "packId":pack_id,"purchasePackType":"CARDPACK",
            "assetId": native_asset_id,
            "packAssetId":opening_asset_id,
            "displayGroupAssetId":group_asset_id,
            "displayGroupUseDefaultImage":True,"useDefaultImage":True,
            "actionType":"CREATEPACK",
            "displayGroup":{"priority":group_asset_id,"value":group},
            "currencies": [{"name": "COINS", "funds": pack["price"],
                            "finalFunds": pack["price"]}],
            "state": "active", "saleType": "NONE", "quantity": pack["items"],
            "visible":True,"isPromo":promo,"category":group,
            "categoryId":native_category_id,"group":group,"groupName":group_name,
            "priority":store_priority,"sortPriority":store_priority,
            "purchaseCount":0,"purchaseLimit":999999,"unopened":False,
            "isPurchaseable":int(credits) >= int(pack["price"]),
            "start": 0, "end": 2147483647,
            "packContentInfo": {
                "itemQuantity": pack["items"], "rareQuantity": pack["rares"],
                "bronzeQuantity": pack["items"] if tier == "bronze" else 0,
                "silverQuantity": pack["items"] if tier == "silver" else 0,
                "goldQuantity": pack["items"] if tier == "gold" else 0},
            "name": pack["name"], "price": pack["price"],
        }
        if promo:
            offer["dealType"]="PROMO"
        result.append(offer)
    for pack_id,count in counts.items():
        pack=PACKS.get(pack_id)
        if not pack: continue
        tier=str(pack.get("tier","gold"))
        tier_asset_id={"bronze":1,"silver":2,"gold":3}[tier]
        result.insert(0,{
            "id":pack_id,"productId":str(pack_id),"packType":pack_id,
            "packId":pack_id,"purchasePackType":"CARDPACK",
            "assetId":tier_asset_id,
            "packAssetId":int(pack.get("packAssetId",tier_asset_id) or tier_asset_id),
            # My Packs is its own native navbar group. categoryId remains 0
            # and assetId remains the artwork tier; gold/3 here kept the
            # notification alive but hid the My Packs tab.
            "displayGroupAssetId":5,
            "displayGroupUseDefaultImage":True,"useDefaultImage":True,
            "displayGroup":{"priority":5,"value":"mypacks"},
            # CREATEPACK is the Store action consumed when the tile is
            # selected. PACK_CREATE_UNOPENED_PACK is a native post-purchase
            # event name: exposing it as the offer action makes CardsDLL stop
            # before POST /purchased/items, which is exactly what the retail
            # trace shows for owned Draft rewards.
            "currencies":[],"actionType":"CREATEPACK",
            # An owned reward is not a limited Store sale.  The retail client
            # renders every QUANTITY offer through the commercial limit path;
            # attaching purchaseCount/purchaseLimit therefore produced the
            # observed LIMITED ribbon and blocked the pack as Sold Out before
            # it could issue the open request.  The unopened-pack count lives
            # in userMassInfo/credits and the persisted rows, while quantity
            # retains its normal Store meaning (items inside this pack).
            "state":"active","saleType":"NONE","quantity":pack["items"],
            "unopened":True,
            "isPurchaseable":count > 0,
            "isUnopened":True,"IS_UNOPENED":True,
            "isMyPacksCategory":True,"IS_MYPACKS_CATEGORY":True,
            "visible":True,"isPromo":False,"category":"mypacks",
            "categoryId":0,"group":"mypacks","groupName":"My Packs",
            "start":0,"end":2147483647,
            "packContentInfo":{"itemQuantity":pack["items"],
                "rareQuantity":pack["rares"],
                "bronzeQuantity":pack["items"] if pack.get("tier")=="bronze" else 0,
                "silverQuantity":pack["items"] if pack.get("tier")=="silver" else 0,
                "goldQuantity":pack["items"] if pack.get("tier","gold")=="gold" else 0},
            "name":pack["name"],"price":0})
    return result


def generate_pack(pack_id, seed, probability_profile="default",
                  include_objects=True):
    pack = pack_definition(pack_id)
    if not pack:
        raise ValueError("unknown pack: %s" % pack_id)
    normal_pools, special = _pools()
    item_count=int(pack["items"])
    player_slots=(int(pack.get("playerSlots",item_count) or 0)
                  if include_objects else item_count)
    if not 0 <= player_slots <= item_count:
        raise ValueError("pack playerSlots must be within its item count")
    object_categories=tuple(str(name).strip().lower() for name in
        (pack.get("objectCategories") or PACK_REVEAL_OBJECT_CATEGORIES)
        if str(name).strip())
    tier=str(pack.get("tier","gold"))
    tier_sequence=list(pack.get("tierSequence",[]) or [])
    if tier_sequence and len(tier_sequence) != item_count:
        raise ValueError("pack tierSequence must match its item count")
    special_by_revision={}
    for row in special:
        special_by_revision.setdefault(row[2],[]).append(row)
    featured_revisions={str(x) for x in pack.get("featuredRevisions",[])}
    excluded_revisions={str(x) for x in pack.get("excludedRevisions",[])}
    guaranteed_names=guaranteed_revision_names(pack)
    for guaranteed_revision in guaranteed_names:
        if guaranteed_revision in excluded_revisions:
            raise ValueError("guaranteed pack revision cannot be excluded")
    featured_pool=[row for row in special
                   if row[2] in featured_revisions and
                   row[2] not in excluded_revisions]
    # A multi-promo guarantee draws uniformly across the pooled cards of every
    # named revision, so each eligible player is equally likely.
    guaranteed_pool=[row for name in guaranteed_names
                     for row in special_by_revision.get(name,[])]
    guaranteed_league_id=int(pack.get("guaranteedLeagueId",0) or 0)
    if guaranteed_league_id:
        # EA assigns one pack id to each domestic TOTS guarantee. Filtering
        # only by the shared TOTS rarity would let, for example, a Serie A
        # card escape from the Premier League reward.
        guaranteed_pool=[row for row in guaranteed_pool
                         if int(native_player_fields(row[0]).get(
                             "leagueId",0) or 0)==guaranteed_league_id]
    guaranteed_slots=max(0,min(player_slots,int(
        pack.get("guaranteedRevisionSlots",0) or 0)))
    if guaranteed_slots and not guaranteed_pool:
        raise ValueError("guaranteed pack revision has no eligible players")
    revision_names=[name for name in special_by_revision
                    if name not in featured_revisions and
                    name not in excluded_revisions]

    def _weights_for(names):
        return [float(SPECIAL_WEIGHTS.get(
            name,SPECIAL_WEIGHTS.get("default",1))) for name in names]

    revision_weights=_weights_for(revision_names)
    # A pack that advertises a promotion stratifies its remaining specials:
    # other headline promos become rare, ordinary specials carry the rest.
    # A pack with no featured revision keeps its established draw sequence.
    promo_names=[name for name in revision_names if name in PROMO_REVISIONS]
    plain_names=[name for name in revision_names
                 if name not in PROMO_REVISIONS]
    stratify=bool(featured_revisions and promo_names and plain_names)
    other_promo_chance=max(0.0,min(1.0,float(pack.get(
        "otherPromoChance",DEFAULT_OTHER_PROMO_CHANCE) or 0.0)))
    promo_weights=_weights_for(promo_names)
    plain_weights=_weights_for(plain_names)
    rng = random.Random(int(seed))
    slot_kinds=(['player']*player_slots+
                ['object']*(item_count-player_slots))
    if player_slots != item_count:
        rng.shuffle(slot_kinds)
        rare_slots=set(rng.sample(
            range(item_count),min(int(pack["rares"]),item_count)))
    else:
        # Preserve the established player-only generator sequence and native
        # contract: the leading slots are the guaranteed Rare slots.
        rare_slots=set(range(min(int(pack["rares"]),item_count)))
    player_positions=[index for index,kind in enumerate(slot_kinds)
                      if kind == 'player']
    rare_player_positions=[index for index in player_positions
                           if index in rare_slots]
    chosen, used_assets, used_objects = [], set(), set()
    special_slots = 0
    featured_slot=None
    raw_featured_chance=float(pack.get("featuredChance",0) or 0)
    # A 100% featured pack is an advertised guarantee expressed by the source
    # definition in a different field; retain it just like guaranteedSlots.
    featured_chance=(raw_featured_chance if raw_featured_chance>=1.0 else
        _profile_probability(raw_featured_chance,probability_profile,
                             RTG_FEATURED_CHANCE_MULTIPLIER))
    special_chance=_profile_probability(
        pack.get("specialChance",0),probability_profile,
        RTG_SPECIAL_CHANCE_MULTIPLIER)
    if (featured_pool and featured_chance>0 and
            rng.random()<featured_chance):
        eligible=rare_player_positions or player_positions
        if eligible:
            featured_slot=rng.choice(eligible)
    player_ordinal=0
    for index in range(item_count):
        slot_tier=str(tier_sequence[index] if tier_sequence else tier)
        guaranteed_rare=index in rare_slots
        untradeable=bool(pack.get("untradeable",False))
        if slot_kinds[index] == 'object':
            selected=_choose_pack_object(
                slot_tier,guaranteed_rare,rng,used_objects,
                object_categories)
            extra={
                "untradeable":untradeable,
                "tradeable":not untradeable,
                "itemState":"free",
                "packId":int(pack_id),
                "rareflag":int(selected["rareflag"]),
                "rating":int(selected["rating"]),
            }
            extra["discardValue"]=0 if untradeable else 20
            chosen.append({
                "resourceId":int(selected["resourceId"]),
                "objectSpec":dict(selected["objectSpec"]),
                "extra":extra,
            })
            continue
        normal=normal_pools[slot_tier]
        if not guaranteed_rare:
            common_pool=[row for row in normal
                         if pack_item_rareflag(int(row[0])) == 0]
            if common_pool:
                normal=common_pool
        if player_ordinal < int(pack.get("loanSlots",0) or 0):
            minimum=int(pack.get("loanMinRating",1) or 1)
            maximum=int(pack.get("loanMaxRating",99) or 99)
            loan_pool=[row for row in normal
                       if minimum <= int(row[1]) <= maximum]
            if loan_pool:
                normal=loan_pool
        wants_special = guaranteed_rare and rng.random() < special_chance
        if player_ordinal < guaranteed_slots:
            pool=guaranteed_pool
        elif index == featured_slot:
            pool=featured_pool
        elif wants_special and revision_names:
            names,weights=revision_names,revision_weights
            if stratify:
                if rng.random()<other_promo_chance:
                    names,weights=promo_names,promo_weights
                else:
                    names,weights=plain_names,plain_weights
            revision=rng.choices(names,weights=weights,k=1)[0]
            pool=special_by_revision[revision]
        else:
            pool=normal
        for _ in range(100):
            row = rng.choice(pool)
            rid = int(row[0]); asset = rid % (1 << 24)
            if asset not in used_assets:
                break
        if asset in used_assets:
            unused=[candidate for candidate in pool
                    if int(candidate[0]) % (1 << 24) not in used_assets]
            if unused:
                row=rng.choice(unused)
                rid=int(row[0]); asset=rid % (1 << 24)
        used_assets.add(asset)
        revision = card_revision(rid)
        if revision != "Normal":
            special_slots += 1
        extra={
            "untradeable": untradeable,
            "tradeable": not untradeable,"itemState": "free",
            "packId": int(pack_id), "cardRevision": revision,
            "rareflag": pack_item_rareflag(
                rid, guaranteed_rare=guaranteed_rare)}
        if untradeable:
            extra["discardValue"]=0
        if player_ordinal < int(pack.get("loanSlots",0) or 0):
            loan_games=max(1,int(pack.get("loanGames",7) or 7))
            extra.update({"isLoan":True,"loans":loan_games,
                          "loanGamesRemaining":loan_games,
                          "untradeable":True,"tradeable":False,
                          "discardValue":0})
        chosen.append({"resourceId": rid, "extra":extra})
        player_ordinal+=1
    return chosen, special_slots


def generate_player_pick_bundle(pack_id, seed, probability_profile="default"):
    """Generate options for a dedicated bundle without creating club items."""
    pack = pack_definition(pack_id)
    if not pack or not pack.get("playerPickBundle"):
        raise ValueError("not a Player Pick bundle: %s" % pack_id)
    config = pack["playerPickBundle"]
    pick_count=max(1,int(config.get("pickCount",24)))
    option_count=max(2,int(config.get("optionCount",4)))
    min_rating=max(1,int(config.get("minRating",81)))
    # The Store bundle expresses the chance for the complete Pick, not for
    # each visible option.  Three independent 25% option rolls would make the
    # effective chance of seeing a special 57.8125%.
    raw_special_chance=max(0.0,min(1.0,float(config.get(
        "specialPickChance",config.get("specialOptionChance",0.25)))))
    special_chance=(raw_special_chance if raw_special_chance>=1.0 else
        _profile_probability(raw_special_chance,probability_profile,
                             RTG_SPECIAL_CHANCE_MULTIPLIER))
    candidate_revisions={str(value) for value in
                         config.get("candidateRevisions",[]) if str(value)}
    unique_players=bool(config.get("uniquePlayers",False))
    normal_pools,special=_pools()
    normal=[(int(rid),int(rating),"Normal")
            for rid,rating in normal_pools["gold"] if int(rating)>=min_rating
            and (not candidate_revisions or "Normal" in candidate_revisions)]
    special=[(int(rid),int(rating),str(revision))
             for rid,rating,revision in special if int(rating)>=min_rating
             and (not candidate_revisions or
                  str(revision) in candidate_revisions)]
    candidates=special+normal
    if len(candidates)<option_count:
        raise ValueError("not enough eligible Player Pick candidates")
    candidate_names={}
    if unique_players:
        for row in card_version_rows(
                revisions=candidate_revisions or None,min_rating=min_rating):
            candidate_names[int(row["resourceId"])]=str(
                row.get("name") or "").strip().casefold()
    special_by_revision={}
    for row in special:
        special_by_revision.setdefault(row[2],[]).append(row)
    revision_names=sorted(special_by_revision)
    revision_weights=[float(SPECIAL_WEIGHTS.get(
        name,SPECIAL_WEIGHTS.get("default",1))) for name in revision_names]
    rng=random.Random(int(seed))
    untradeable=bool(pack.get("untradeable",False))
    picks=[]
    for _pick_index in range(pick_count):
        chosen=[]; used_assets=set(); used_players=set()
        pick_wants_special=bool(revision_names) and rng.random()<special_chance
        special_selected=False
        attempts=0
        while len(chosen)<option_count and attempts<len(candidates)*12:
            attempts+=1
            wants_special=pick_wants_special and not special_selected
            if wants_special:
                revision=rng.choices(
                    revision_names,weights=revision_weights,k=1)[0]
                pool=special_by_revision[revision]
            else:
                pool=normal or candidates
            row=rng.choice(pool)
            asset=row[0] % (1 << 24)
            player_key=candidate_names.get(row[0]) or str(asset)
            if asset in used_assets or (unique_players and
                                        player_key in used_players):
                continue
            chosen.append(row); used_assets.add(asset)
            used_players.add(player_key)
            if row[2] != "Normal":
                special_selected=True
        if len(chosen)<option_count:
            # Normal 81+ players complete a standard Pick without silently
            # adding another special.  Guaranteed revision-only Picks (Icon)
            # have no normal pool and therefore retain all-special options.
            for row in (normal or candidates):
                asset=row[0] % (1 << 24)
                player_key=candidate_names.get(row[0]) or str(asset)
                if asset in used_assets or (unique_players and
                                            player_key in used_players):
                    continue
                chosen.append(row); used_assets.add(asset)
                used_players.add(player_key)
                if len(chosen)==option_count: break
        if len(chosen)<option_count:
            raise ValueError("could not create unique Player Pick options")
        rng.shuffle(chosen)
        options=[]
        for rid,rating,revision in chosen:
            option_extra={
                "rating":rating,"cardRevision":revision,
                "rareflag":pack_item_rareflag(rid,guaranteed_rare=True),
                "untradeable":untradeable,"tradeable":not untradeable,
                "acquisitionSource":"PLAYER_PICK_REWARD",
                "itemState":"free","packId":int(pack_id)}
            if untradeable:
                option_extra["discardValue"]=0
            options.append({"resourceId":rid,"extra":option_extra})
        picks.append({"minRating":min_rating,"optionCount":option_count,
                      "options":options})
    guarantee_special=bool(config.get("guaranteeSpecial",False))
    guarantee_rating=max(0,int(config.get("guaranteeMinRating",0)))
    all_options=[row for pick in picks for row in pick["options"]]
    needs_special=guarantee_special and not any(
        row["extra"]["cardRevision"] != "Normal" for row in all_options)
    needs_rating=guarantee_rating and not any(
        row["extra"]["rating"] >= guarantee_rating for row in all_options)
    if needs_special or needs_rating:
        targets=[(pick,index) for pick in picks
                 for index in range(len(pick["options"]))]
        rng.shuffle(targets)
        for pick,index in targets:
            used={row["resourceId"] % (1 << 24)
                  for offset,row in enumerate(pick["options"])
                  if offset != index}
            eligible=[row for row in (special if needs_special else candidates)
                      if (not guarantee_rating or row[1] >= guarantee_rating)
                      and row[0] % (1 << 24) not in used]
            if not eligible:
                continue
            rid,rating,revision=rng.choice(eligible)
            pick["options"][index]["resourceId"]=rid
            pick["options"][index]["extra"].update({
                "rating":rating,"cardRevision":revision,
                "rareflag":pack_item_rareflag(rid,guaranteed_rare=True)})
            break
        else:
            raise ValueError("not enough eligible guaranteed Pick candidates")
    return picks


def generate_player_pick_options(min_rating=83, option_count=3, seed=0,
                                 pack_id=0, special_chance=0.25,
                                 position=None, quality=None,
                                 resource_ids=None, rare_only=False,
                                 max_rating=99):
    """Build one deterministic, untradeable Player Pick.

    This is deliberately independent from a Store pack definition so rewards
    can grant picks without charging coins or creating temporary club items.
    Candidate asset IDs are unique inside the pick and all ratings meet the
    advertised floor.
    """
    min_rating=max(1,int(min_rating)); max_rating=min(99,int(max_rating))
    option_count=max(2,int(option_count))
    if max_rating < min_rating:
        raise ValueError("Player Pick maximum rating is below its minimum")
    exact_ids=[]
    for value in resource_ids or []:
        resource_id=int(value)
        if resource_id > 0 and resource_id not in exact_ids:
            exact_ids.append(resource_id)
    if exact_ids:
        exact=[]
        for resource_id in exact_ids:
            fields=native_player_fields(resource_id)
            rating=int(fields.get("rating",0) or 0)
            if min_rating <= rating <= max_rating:
                exact.append((resource_id,rating,card_revision(resource_id)))
        normal=[row for row in exact if row[2] == "Normal"]
        special=[row for row in exact if row[2] != "Normal"]
    else:
        normal_pools,special_pool=_pools()
        normal=[(int(rid),int(rating),"Normal")
                for rid,rating in normal_pools["gold"]
                if min_rating <= int(rating) <= max_rating]
        special=[(int(rid),int(rating),str(revision))
                 for rid,rating,revision in special_pool
                 if min_rating <= int(rating) <= max_rating]
    requested_position=str(position or "").upper().strip()
    requested_quality=str(quality or "").upper().strip()

    def eligible(row):
        resource_id,rating,_revision=row
        if requested_quality == "GOLD" and int(rating) < 75:
            return False
        if requested_quality == "SILVER" and not 65 <= int(rating) <= 74:
            return False
        if requested_quality == "BRONZE" and int(rating) >= 65:
            return False
        if requested_position:
            fields=native_player_fields(int(resource_id))
            if str(fields.get("preferredPosition","")).upper() != requested_position:
                return False
        if rare_only and not pack_item_rareflag(
                int(resource_id),guaranteed_rare=False):
            return False
        return True

    normal=[row for row in normal if eligible(row)]
    special=[row for row in special if eligible(row)]
    candidates=special+normal
    if len({rid % (1 << 24) for rid,_,_ in candidates}) < option_count:
        raise ValueError("not enough eligible Player Pick candidates")
    rng=random.Random(int(seed)); chosen=[]; used_assets=set()
    if exact_ids and len(candidates) == option_count:
        chosen=list(candidates)
        used_assets={row[0] % (1 << 24) for row in chosen}
    attempts=0
    while len(chosen)<option_count and attempts<len(candidates)*12:
        attempts+=1
        pool=special if special and rng.random()<float(special_chance) else normal
        if not pool: pool=candidates
        row=rng.choice(pool); asset=row[0] % (1 << 24)
        if asset in used_assets: continue
        chosen.append(row); used_assets.add(asset)
    if len(chosen)<option_count:
        for row in candidates:
            asset=row[0] % (1 << 24)
            if asset in used_assets: continue
            chosen.append(row); used_assets.add(asset)
            if len(chosen)==option_count: break
    options=[]
    for rid,rating,revision in chosen:
        options.append({"resourceId":rid,"extra":{
            "rating":rating,"cardRevision":revision,
            "rareflag":pack_item_rareflag(rid,guaranteed_rare=True),
            "untradeable":True,"tradeable":False,"discardValue":0,
            "itemState":"free","packId":int(pack_id)}})
    result={"minRating":min_rating,"maxRating":max_rating,
            "optionCount":option_count,
            "options":options}
    if requested_position:
        result["position"]=requested_position
    if requested_quality:
        result["quality"]=requested_quality
    if exact_ids:
        result["resourceIds"]=[int(row[0]) for row in candidates]
    if rare_only:
        result["rareOnly"]=True
    return result


def available_revisions():
    _, special = _pools()
    return sorted({row[2] for row in special})

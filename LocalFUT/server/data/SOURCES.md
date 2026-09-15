# Local data provenance

This repository does not distribute extracted game databases, mirrored EA CDN
assets, card art, or third-party catalogue dumps. Those files remain local and
are excluded by `.gitignore`.

## EA static player catalogues

The local development copy was validated against the original FIFA 19 static
player-name and metadata catalogues. Together they contain 15,892 base
player/Icon records. The versioned source keeps only parsers and tests; users
must reconstruct permitted local inputs from their own legitimate installation
or an independently lawful source.

## Store and pack definitions

`ea_pack_catalog.json` is a text-only index reconstructed from EA's FIFA 19
`storepackdescriptions.en_us.xml` dated 2019-08-22. The local development copy
also retains five explicitly documented historical project extensions at
406..410; those rows are not used as EA provenance. Prices, local odds and
availability remain project-owned values in `pack_defs.json`.

`pack_defs.json` records the local offer IDs used by the offline backend:

| Definition ID | Local offer |
|---:|---|
| 305 | Premium Gold Players Pack |
| 401 | Rare Players Pack |
| 402 | Jumbo Rare Players Pack |
| 403 | Mega Pack (official FIFA 19 identity) |
| 404 | Rare Mega Pack (official FIFA 19 identity) |
| 405 | Ultimate Pack (official FIFA 19 identity) |
| 406 | Single Icon Pack |
| 407 | Ones to Watch Jumbo Rare Players Pack |
| 408 | Future Stars Jumbo Rare Players Pack |
| 410 | Carniball Jumbo Rare Players Pack |
| 4002 | Native Player Pick Pack 3 identity used for the local 20x 81+ trial |
| 9403 | Local TOTY Jumbo Rare Players Pack experiment |
| 9404 | Local FUT Birthday Jumbo Rare Players Pack experiment |
| 9405 | Local TOTS Jumbo Rare Players Pack experiment |

The official EA localization proves 403 as a 30-item Gold/18-Rare Mega Pack,
404 as a 30-item all-Rare Gold Rare Mega Pack, and 405 as a 30-player all-Rare
Gold Ultimate Pack. Earlier project builds incorrectly reused those IDs for
campaign experiments; the experiments now use the collision-free local range
9403..9405.

The Carniball offer is backed by the 14 cards distributed in packs. Its narrow
`carniball_versions.json` overlay records exact resource/base IDs, positions,
ratings and face attributes cross-checked against the archived FIFA 19
FifaRosters and FUTBIN pages. It deliberately excludes the eight SBC and four
Weekly Objective Carniball rewards. Native rarity ID 72 and the corresponding
background are verified against the local EA `futitemraritytunables.json`.
The official EA Store localization supplies pack 410's identity. The generated
`card_versions.json` remains untouched and takes precedence if a future rebuild
contains the same IDs.

The five late FIFA 19 UCL Moments reward cards are kept in the narrow
`ucl_moments_versions.json` overlay for the same reason. Their exact FUT
resource IDs, base identities, positions, ratings, face attributes, clubs,
nations, leagues, skill moves and weak foot values are cross-checked against
the individual archived FIFA 19 WeFUT records. Native rarity ID 69 is the
`CHAMPIONS_LEAGUE_SBC` design in the local EA rarity tunables. These cards are
SBC rewards and are deliberately absent from `PACKABLE_SPECIAL_REVISIONS`, so
adding them to the searchable catalogue does not place them in purchased pack
pools. Their card shell is WeFUT's original FIFA 19 `gold69.png` asset observed
on those same records. `tools/import_fut19_card_background.py` crops only its
transparent margin, then resamples the authentic pixels onto the expanded and
collapsed geometry of FIFA 19's native UCL shell before DXT5 conversion. The
source and converted runtime assets remain private and excluded from public
distribution.

FUTmas remains a gated development entry. Existing `FUTmas SBC` rows remain
excluded from purchased packs because they are reward cards, not pack content.

`pack_weights.json` contains project-authored local simulation weights. These
are not represented as official historical EA pack probabilities. Carniball
uses the same local family weight as Future Stars; this is simulation balance,
not a claim about EA's historical odds.

The local Player Pick presentation uses two independently sourced identities:
pack definition 4002 is `PLAYER PICK PACK 3` in the Store localization, while
resource 5004045 in the reconstructed non-player catalogue is the native
`PlayerPickItemDesc3` object with amount/definition value 3002. The local
quantity, minimum rating, candidate generation, price, and runtime English
bundle description are project-authored behavior rather than historical EA
pack contents. The current local bundle uses 20 Picks and one 25% special roll
for each complete Pick. A successful roll places at most one special among the
three options; rolling 25% independently for every option would instead make
the effective chance of seeing a special 57.8125%. Special families are then
selected with the local simulation weights in `pack_weights.json`, avoiding
catalogue-size bias toward promotions with many version rows.

The archived official FUT 19 Companion bundles dated 2019-06-27 provide the
runtime discriminator and request contract: a Pick Item is `itemType=misc`
with `cardsubtypeid=237` (`PLAYER_PICK_ITEM`). Redemption uses
`POST /ut/game/fifa19/item/{instanceId}`, interrupted selections are recovered
from `GET /ut/game/fifa19/playerpicks/pending`, and the chosen player is
confirmed through `POST /ut/game/fifa19/playerpicks/item/{resourceId}/select`.
Only these protocol facts are reused; the archived JavaScript is research input
and is not distributed with this repository.

The retail PC session provides an additional direct observation: redeeming a
Pick Item from the unassigned pile invokes CardsDLL's `ApplyCardNontargeted`
delegate as `POST /ut/game/fifa19/item/nontargeted?itemId=<instanceId>`. This
console entry point shares the same persisted redeem/pending/select state; it
does not define a second Player Pick representation.

Store `assetId` uses the client's PackTier identities, independently from the
30..37 seasonal opening-effect family. `LEGENDS=8` is used for the local Icon
offer. No dedicated FUT Birthday, TOTY, or Future Stars Store tier has been
verified, so those offers retain the valid generic promo tier rather than an
invented mapping.

## Prime Icon Moments player-head images

The private runtime contains 44 local transparent PNG references, one for each
source-backed FIFA 19 Prime Icon Moments definition (`246472..246541`, with the
exact sparse identities supplied by the card-version catalogue). Every source
is 220x256 RGBA. The files do not retain trustworthy remote-origin metadata, so
this document deliberately does not attribute them to an unverified site or
represent them as EA CDN downloads.

`tools/import_fut19_playerhead.py` converts each PNG deterministically to the
retail player-head layout: a standard 124-byte DDS header, DXT5/BC3 blocks, and
the verified eight-byte EA texture trailer. The resulting 44 private runtime
DDS files are exactly 56,456 bytes each and are served only as
`/fut/playerheads/g4/single/p<PrimeMomentsDefinitionId>.dds`. Automated tests
check the complete set, not only Moore and Puyol. These PNG/DDS assets remain
excluded from public distribution under the rules below.

## Transfer Market player-head images

`tools/sync_fut19_playerheads.py` fills missing private runtime portraits only
from FUTBIN's original FIFA 19 player CDN. It first requests the exact dynamic
cut-out `p<resourceId>.png`; where that historical file is unavailable, it may
use only `<assetId>.png`, the original portrait for that same player. Every URL,
source SHA-256 and converted DDS SHA-256 is retained in the private
`research/fut19_market_playerhead_sources.json` manifest. No generated image or
different-player fallback is permitted. Conversion uses the same deterministic
DXT5 container described above. Exact 485x567 dynamic artwork retains its
original canvas; a 160/180px same-player base portrait is centred without
upscaling on the compact 220x256 canvas. This prevents both delayed/misplaced
dynamic artwork and oversized fallback faces. All mirrored assets remain
excluded from public distribution.

## Offline competitive-mode contracts

The supported retail CardsDLL is used only as a local, read-only
interoperability reference. Its response parser establishes the Draft route
suffixes and JSON member names, including the native state sequence from
`FORMATION_DRAFT` through `READY_FOR_REWARDS`. No executable, disassembly, or
proprietary binary data is distributed by this repository.

An archived official FUT 19 Companion bundle establishes the Squad Battles hub
members `sqbtEventId`, `rank`, `score`, `userTierLevel`, `isPrizeAvailable`,
and `prizeTiers`. The archive remains private research input.

The supported retail DLL also exposes both Draft service bases
`ut/%s/squad/mode` and `ut/%s/draft/mode`, plus the Squad Battles prize suffix
`/user/prize`. The local compatibility layer therefore treats the two Draft
state URLs as aliases and accepts the retail GET as well as the Companion-style
POST for the idempotent prize claim.

The same binary names the feature switches `enableDraftMode`,
`enableOfflineDraftMode`, and `enableSinglePlayerDraftMode`, and pairs
`RS4:FutGetDraftAwardServerResponse` with `/%d/draft`. Those identifiers are
used verbatim; the local handler grants the award only after the persisted
session reaches `READY_FOR_REWARDS`.

Read-only inspection of `RS4:FutGetDraftCurrentStateServerResponse` further
establishes the complete current-state member set and primitive conversions.
The embedded `entranceCriteria` object contains integer `COINS`, `POINTS`, and
`DRAFT_TOKEN` members; the retail Draft entry prices are therefore represented
locally as 15000, 300, and 1. The remaining parsed members are
`gameModeRestriction`, `gamesWonCurrentMatch`, `roundsInfo`, `squadState`,
`stateParam1`, and `stateParam2`. Project-only diagnostic fields may coexist in
the JSON but are not treated as evidence of a retail protocol member.

The same read-only constructor initializes `stateParam2` to signed `-1` before
JSON decoding. The local no-session response preserves that native invalid
sentinel as the numeric string `"-1"`; zero is reserved for a real Draft slot.

`RS4:FutPurchaseDraftModeServerResponse` is handled by the code block that also
references `/purchase/mode/%d/draft`. Its three root members are read in the
order `DRAFT_TOKEN`, `POINTS`, and `COINS`. The nearby `%I64d` conversion path,
together with the client's verified string-backed OSDK size fields, supports
serializing the three remaining balances as decimal JSON strings. This is kept
separate from the numeric entry prices nested in the current-state response.

`RS4:FutGetTowChallengeServerResponse` uses a custom positional decoder at
CardsDLL RVA `0x259290`; it is not the generic `entries/key/value` client-data
DTO. The decoder stores its first scalar as a byte-order selector, and its
nested record path at RVA `0x2594b0` treats value `3` as native PC ordering.
The next scalar is the cached-record count. A zero-record response parses but
does not initialize the data provider that renders the Single Player TOTW
formation, so it is not a complete tile response.

The complete PC stream contains the outer signed 64-bit cache key, a nested
record count, each available squad name packed into little-endian words, then
byte, byte and three unsigned-short values per squad. Read-only inspection of
the tile consumer shows that the first byte is its completed flag. The
featured-squad callback stores these squads under outer key
`0x7fffffffffffffff`; therefore the local response emits one owner record with
all saved source-backed challenge names. The selected squad ID is published
through the four adjacent Hub members `sqbtEventId`, `sqbtOpponentSquadId`,
`featuredSquadId` and `sqbtMatchDifficulty`, and every listed squad ID is
retrievable through the retail public-squad route. The nearby
`PENDING_TOTW_ASSET_ID` and `DOWNLOADED_TOTW_ASSET_ID` strings belong to the
player-head asset view models and are not members of this response contract.

`totw_squads.json` is the saved offline source record for FIFA 19 FUTBIN Team
of the Week pages and the corresponding official weekly pages. Its selected XI
resource IDs are independently checked against `card_versions.json`; TOTW 7
and TOTW 8 use only exact IF/SIF catalogue rows. No runtime scraping, generated
player, invented resource ID, or generated artwork participates in these
challenge squads.

The following behavior is explicitly project-authored for offline play rather
than claimed as historical EA live-service data: Draft candidate selection and
reward amounts, the weekly TOTW selection, Squad Battles opponent rotations,
local scoring thresholds, rank bands, refresh timing, and prizes. Player
identities always come from exact local FIFA 19 catalogue rows; temporary Draft
and CPU-squad items are never granted to the user's persistent club.

The additional Squad Battles red Player Picks retain the exact sourced
IF/SIF/TIF identity and statistics. Their presentation uses rarity ID 18,
`CHAMPION_REWARD`, read from EA's bundled
`futitemraritytunables.json`; no synthetic player definition is introduced.

## Non-player FUT catalogues

FIFA 19 does not expose one complete `items.json`. Non-player definitions are
split across individual CDN-style resource families and Frostbite database
tables. Read-only development probes confirmed representative resource-ID
families for managers, badges, kits, stadiums, balls, and consumables, but those
records must not be enabled until the native DTO and matching asset are both
validated.

Representative local research examples include:

| Type | Representative resource ID | Meaning |
|---|---:|---|
| manager | 1000448 | Manager definition 448 |
| badge | 6000001 | Club badge definition for club ID 1 |
| kit | build-dependent | Must be enumerated from validated team-kit links |
| stadium | build-dependent | Must be paired with the native stadium DTO |
| ball | build-dependent | Must be paired with the native ball DTO |
| consumable | family-dependent | Contract, healing, chemistry, league, or position item |

The Frostbite data sources required for a complete local catalogue include
manager, stadium/team-stadium link, team-kit, team, competition-badge, nation,
league, and consumable-related tables. Database identity is not enough by
itself: native presentation also requires the matching local texture/model and
the correct FUT item contract.

For each locally reconstructed input, retain a private provenance record with
source, date, byte size, and SHA-256. Runtime behavior must not depend on a live
external network service.

## FIFA 19 Squad Building Challenges

`research/fut19_sbc_catalog_index.tsv` is the saved, normalized FUTBIN FIFA 19
index: 921 sets (4 Basic, 3 Advanced, 5 Upgrades, 18 Leagues, 47 Icons and 844
Live). The normalized audit catalogue enables the seven Basic/Advanced sets
and their 25 challenges. The runtime supplements those with 40 separately
verified groups, for a beta registry of 47 active sets and 261 challenges:
four Upgrades, 24 Player SBCs, four League SBCs and eight Icon SBCs. Index-only
records remain disabled until their child challenges, formations/slot masks,
requirements, rewards and authentic artwork have source evidence; they are
never reconstructed from a name or solution price. Throwback FUT Birthday is
retained as verified research but not published because the archived page has
empty set/challenge artwork URLs. FUT Swap Deals are not exposed.

`research/fut19_sbc_artwork_sources.json` and
`research/fut19_player_sbc_artwork_sources.json` record the exact FUTBIN source
URL and SHA-256 for 96 authentic set/challenge/player PNGs. The private files
under `fut_content/fut/sbc/gen4/source_png` are installed only after their
hashes match. Set art is aspect-preserved and centred on the native transparent
485x567 canvas. FUTBIN's original 561x515 challenge sprites are downsampled as
complete transparent images to FIFA 19's native 280x258 canvas, without
cropping, so they remain centred and cannot overlap neighbouring carousel
tiles. There is no generated or unrelated fallback: a missing historical asset
is served as 404. These images remain local assets and are excluded by the
distribution rules below.

`fut19_card_version_corrections.json` contains the one verified live-upgrade
correction currently required by an active player SBC: FUTmas Lucas Torreira
resource `50555607`, whose final FUTBIN player page records 86 OVR and
82/73/83/85/86/79 face stats. It overlays the stale 82-rated one-time card
extraction without mutating or regenerating the original catalogue.

## Transfer-list state contract

Read-only inspection of the supported installed retail CardsDLL exposes the
native trade-state enum values `active`, `inactive`, `expired`, and `closed` in
one contiguous descriptor table. The local wire contract uses `inactive` for
an inventory item moved to the Transfer List before any auction exists,
`expired` for an ended unsold auction, and `closed` only for a completed sale.
An unlisted item has no auction identity (`tradeId=0`); its persistent item id
remains in `itemData.id`. No executable or disassembly is distributed.

## Distribution rules

A public source release may include:

- parser and validator source code;
- project-authored schemas, tests, and reconstruction documentation;
- hashes and provenance references that do not redistribute protected content.

A public source or binary release must exclude:

- FIFA executables, DLLs, CAS/TOC/SB archives, and extracted databases;
- mirrored CDN files, card art, textures, models, audio, and BIG/DDS assets;
- FUTBIN or other third-party data dumps;
- local saves, logs, certificates, keys, and machine-specific caches.

See [LEGAL_NOTICE.txt](../../../LEGAL_NOTICE.txt), the
[test checklist](../../../ADVANCED/TEST_CHECKLIST.md), and the
[public-release checklist](../../../docs/PUBLIC_RELEASE_CHECKLIST.md).

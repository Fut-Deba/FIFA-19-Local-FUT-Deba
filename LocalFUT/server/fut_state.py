#!/usr/bin/env python3
# =====================================================================
#  FIFA 19 Local FUT - user-state persistence (SQLite)
#  Stored in LOCALFUT19_DATA_ROOT when set, otherwise under
#  %LOCALAPPDATA%\FIFA19LocalFUT\fut19-local.sqlite3.
#  Adapted from the FIFA 15 Local FUT State model.
#
#  Handles coins, club identity, item piles (club/squad/trade/watchlist),
#  squads, unopened packs, auctions, offline matches and seasons, and SBC state.
# =====================================================================
import sqlite3, threading, json, os, time, hashlib

from fut_catalog import (INITIAL_BONUS_PLAYERS, STARTER_PLAYERS,
                         native_player_fields, validate_initial_bonus_catalogue,
                         validate_starter_catalogue)
from fut_objects import (badge_item_dto, ball_item_dto, consumable_item_dto,
                         build_grant_item,
                         default_grant_quantity, expanded_default_grant,
                         expanded_rtg_grant, rtg_grant_quantity,
                         kit_item_dto, manager_item_dto, object_definition,
                         native_object_item, staff_item_dto, stadium_item_dto,
                         stored_item_kind)
from fut_sbc import (SbcValidationError, extract_item_ids, fut_squad_rating,
                     submission_chemistry, validate_challenge_submission)
from fut_sqbt import (fut_champions_pick_spec,
                      prize_tier as sqbt_prize_tier,
                      tier_level as sqbt_tier_level,
                      user_rank as sqbt_user_rank)
from fut_champions import (MATCH_COUNT as CHAMPION_MATCH_COUNT,
                           leaderboard_bots as champion_leaderboard_bots,
                           reward_bundle as champion_reward_bundle)
from fut_seasons import (MATCH_COUNT as SEASON_MATCH_COUNT,
                         claim_reward as claim_season_reward,
                         new_season as new_offline_season,
                         next_season as next_offline_season,
                         record_result as record_offline_season_result)

def now_s(): return int(time.time())
def sha1(s): return hashlib.sha1(str(s).encode("utf-8")).hexdigest()

MARKET_TRADE_PILE_CAPACITY = 100
MARKET_WATCHLIST_CAPACITY = 100
MARKET_DURATIONS = frozenset((3600,10800,21600,43200,86400,259200))
MAX_MATCH_COIN_REWARD = 1000
ACCOUNT_MODE_NORMAL = "NORMAL"
ACCOUNT_MODE_RTG = "RTG"


def normalize_account_mode(value=None):
    explicit=value is not None
    raw=str(value if explicit else os.environ.get(
        "LOCALFUT19_PROFILE","") or "").strip().upper()
    rtg_flag=("" if explicit else str(
        os.environ.get("LOCALFUT19_RTG_MODE","") or "").strip().lower())
    if raw == ACCOUNT_MODE_RTG or rtg_flag in {"1","true","yes","on"}:
        return ACCOUNT_MODE_RTG
    return ACCOUNT_MODE_NORMAL

def data_root():
    """Directory holding the local account databases."""
    configured=str(os.environ.get("LOCALFUT19_DATA_ROOT","") or "").strip()
    base=(os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
          if configured else
          os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "FIFA19LocalFUT"))
    os.makedirs(base, exist_ok=True)
    return base


def account_db_filename(account_mode=None):
    return ("fut19-rtg.sqlite3"
            if normalize_account_mode(account_mode)==ACCOUNT_MODE_RTG
            else "fut19-local.sqlite3")


def resolve_active_db_path(account_mode=None):
    """Locate the database the launcher actually opens for this account.

    The launcher runs the server with LOCALFUT19_DATA_ROOT pointing at a
    per-build profile directory, so a tool started from its own console -
    RESET_CLUB.cmd, for example - would otherwise open the database in the
    data root and report success after changing a file the game never reads.
    """
    mode=normalize_account_mode(account_mode)
    filename=account_db_filename(mode)
    if str(os.environ.get("LOCALFUT19_DATA_ROOT","") or "").strip():
        return os.path.join(data_root(), filename)
    base=data_root()
    profiles=os.path.join(base,"profiles")
    candidates=[]
    if os.path.isdir(profiles):
        for name in sorted(os.listdir(profiles)):
            is_rtg=name.lower().endswith("-rtg")
            if is_rtg != (mode==ACCOUNT_MODE_RTG):
                continue
            path=os.path.join(profiles,name,filename)
            if os.path.isfile(path):
                candidates.append(path)
    if candidates:
        # Several builds can leave a profile behind; the live one is the
        # database that was written most recently.
        return max(candidates,key=os.path.getmtime)
    return os.path.join(base, filename)


def default_db_path(account_mode=None):
    configured_root=str(os.environ.get("LOCALFUT19_DATA_ROOT","") or "").strip()
    base=(os.path.abspath(os.path.expandvars(os.path.expanduser(configured_root)))
          if configured_root else
          os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "FIFA19LocalFUT"))
    os.makedirs(base, exist_ok=True)
    filename=("fut19-rtg.sqlite3" if normalize_account_mode(account_mode)==ACCOUNT_MODE_RTG
              else "fut19-local.sqlite3")
    return os.path.join(base, filename)

def _submitted_squad_chemistry(squad):
    """Return the total chemistry FIFA itself reported for this squad.

    The SBC requirement panel the player sees is ticked by the client's own
    chemistry, and the client sends that number with the squad it saves. When
    the server recomputed it instead, the two could disagree and the player
    was told a requirement was unmet while every row on screen was green: on
    2026-09-08 at 00:07 the client saved `"chemistry":70` against a `Min 70`
    rule and the submit was still refused, which the client shows as the
    generic FUT communication error.

    The client is authoritative for what it displays, so the displayed value
    is what the rule is evaluated against. Anything outside 0..100, or absent,
    falls back to the server-side calculation, and the slot, mask and
    individual-chemistry checks are unchanged either way.
    """
    if not isinstance(squad, dict):
        return None
    raw = squad.get("chemistry")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


class FutState:
    def __init__(self, path=None, credits=0, club_name="FUT Deba Local", club_abbr="LF9",
                 persona_name="FUT Deba Local", account_mode=None):
        requested_mode=normalize_account_mode(account_mode)
        self.path = path or default_db_path(requested_mode)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._defaults = dict(credits=credits, club_name=club_name, club_abbr=club_abbr,
                              persona_name=persona_name,
                              account_mode=requested_mode)
        self._init()

    def _init(self):
        with self.lock:
            self.conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(
                    id INTEGER PRIMARY KEY, resource_id INTEGER NOT NULL,
                    pile TEXT NOT NULL DEFAULT 'club',
                    item_kind TEXT NOT NULL DEFAULT 'player', data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inventory_grants(
                    grant_key TEXT PRIMARY KEY, item_id INTEGER NOT NULL,
                    created INTEGER NOT NULL, data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS squads(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS unopened_packs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, pack_id INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS auctions(
                    trade_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL DEFAULT 0,
                    resource_id INTEGER NOT NULL, seller_is_user INTEGER NOT NULL DEFAULT 1,
                    starting_bid INTEGER NOT NULL DEFAULT 0, buy_now INTEGER NOT NULL DEFAULT 0,
                    current_bid INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL DEFAULT 0,
                    expires INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'active',
                    data TEXT NOT NULL DEFAULT '{}', item_payload TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS market_targets(
                    trade_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'WATCHED',
                    reserved_credits INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS market_operations(
                    operation_key TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    trade_id INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL,
                    response TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS matches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL,
                    mode TEXT, result TEXT, home_goals INTEGER, away_goals INTEGER,
                    reward_coins INTEGER, data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS totw_results(
                    operation_key TEXT PRIMARY KEY,
                    challenge_id INTEGER NOT NULL,
                    difficulty INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    response TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS sbc(
                    set_id INTEGER, challenge_id INTEGER, status TEXT NOT NULL DEFAULT 'NONE',
                    data TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(set_id, challenge_id));
                CREATE TABLE IF NOT EXISTS sbc_submissions(
                    operation_key TEXT PRIMARY KEY,
                    set_id INTEGER NOT NULL, challenge_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created INTEGER NOT NULL,
                    consumed_item_ids TEXT NOT NULL DEFAULT '[]',
                    rewards TEXT NOT NULL DEFAULT '[]',
                    response TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS sbc_set_rewards(
                    set_id INTEGER PRIMARY KEY, created INTEGER NOT NULL,
                    response TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS sbc_set_attempt_rewards(
                    set_id INTEGER NOT NULL, cycle INTEGER NOT NULL,
                    created INTEGER NOT NULL, response TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(set_id,cycle));
                CREATE TABLE IF NOT EXISTS pack_openings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER NOT NULL,
                    pack_id INTEGER NOT NULL, price INTEGER NOT NULL,
                    currency TEXT NOT NULL, seed INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS player_picks(
                    id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'PENDING',
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS draft_sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL, state TEXT NOT NULL,
                    formation TEXT NOT NULL DEFAULT '',
                    captain_resource_id INTEGER NOT NULL DEFAULT 0,
                    difficulty INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    entry_currency TEXT NOT NULL,
                    entry_cost INTEGER NOT NULL DEFAULT 0,
                    rng_seed INTEGER NOT NULL,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    reward_claimed INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS draft_picks(
                    session_id INTEGER NOT NULL,
                    choice_type TEXT NOT NULL, slot INTEGER NOT NULL,
                    choices_json TEXT NOT NULL,
                    selected_value TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id,choice_type,slot));
                CREATE TABLE IF NOT EXISTS draft_matches(
                    session_id INTEGER NOT NULL, round INTEGER NOT NULL,
                    result TEXT NOT NULL, home_goals INTEGER NOT NULL,
                    away_goals INTEGER NOT NULL, created INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id,round));
                CREATE TABLE IF NOT EXISTS sqbt_events(
                    id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                    score INTEGER NOT NULL DEFAULT 0,
                    rank INTEGER NOT NULL DEFAULT 1,
                    tier INTEGER NOT NULL DEFAULT 1,
                    reward_claimed INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS sqbt_opponents(
                    event_id INTEGER NOT NULL, opponent_id INTEGER NOT NULL,
                    rotation INTEGER NOT NULL DEFAULT 0,
                    played INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(event_id,opponent_id));
                CREATE TABLE IF NOT EXISTS sqbt_matches(
                    event_id INTEGER NOT NULL, opponent_id INTEGER NOT NULL,
                    result TEXT NOT NULL, difficulty INTEGER NOT NULL,
                    points INTEGER NOT NULL, created INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(event_id,opponent_id));
                CREATE TABLE IF NOT EXISTS champion_sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL UNIQUE,
                    event_key TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PICK_DIFFICULTY',
                    difficulty INTEGER NOT NULL DEFAULT 0,
                    wins INTEGER NOT NULL DEFAULT 0,
                    draws INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    rng_seed INTEGER NOT NULL,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    expires INTEGER NOT NULL,
                    reward_claimed INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS champion_opponents(
                    session_id INTEGER NOT NULL,
                    opponent_id INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL,
                    played INTEGER NOT NULL DEFAULT 0,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id,opponent_id),
                    UNIQUE(session_id,ordinal));
                CREATE TABLE IF NOT EXISTS champion_matches(
                    session_id INTEGER NOT NULL,
                    opponent_id INTEGER NOT NULL,
                    gid TEXT NOT NULL,
                    result TEXT NOT NULL,
                    difficulty INTEGER NOT NULL,
                    home_goals INTEGER NOT NULL,
                    away_goals INTEGER NOT NULL,
                    created INTEGER NOT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(session_id,opponent_id),
                    UNIQUE(session_id,gid));
            """)
            # CREATE TABLE IF NOT EXISTS does not add columns to an existing
            # profile. Every pre-catalogue row is a player, so this additive
            # migration is both deterministic and non-destructive.
            item_columns={row["name"] for row in
                          self.conn.execute("PRAGMA table_info(items)").fetchall()}
            if "item_kind" not in item_columns:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN item_kind TEXT NOT NULL "
                    "DEFAULT 'player'")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_pile_kind "
                "ON items(pile,item_kind,id)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auctions_owner_state "
                "ON auctions(seller_is_user,state,expires,trade_id)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_targets_status "
                "ON market_targets(status,updated,trade_id)")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sbc_submissions_challenge "
                "ON sbc_submissions(set_id,challenge_id,attempt)")
            self.conn.commit()
            self._set_default("credits", int(self._defaults["credits"]))
            self._set_default("club_name", self._defaults["club_name"])
            self._set_default("club_abbr", self._defaults["club_abbr"])
            self._set_default("persona_name", self._defaults["persona_name"])
            # Persist the mode in the profile itself. Reopening the RTG file
            # through another launcher therefore cannot lift its restrictions.
            self._set_default("account_mode", self._defaults["account_mode"])
            self._set_default("next_item_id", 100000000000)
            self._set_default("next_trade_id", 650000000000)
            self._set_default("established", now_s())
            self._set_default("sid", "LOCAL19-" + sha1(time.time())[:20].upper())
            self._set_default("fifa_points", 0)
            self._set_default("season_wins", 0)
            self._set_default("season_draws", 0)
            self._set_default("season_losses", 0)
            self._set_default("season_division", 10)
            self._set_default("offline_season_v1", new_offline_season(
                int(self.get("season_division",10) or 10)))
            self._set_default("offline_season_totals_v1", {
                "wins":0,"draws":0,"losses":0,"goalsFor":0,
                "goalsAgainst":0,"completed":0,"titles":0,
                "promotions":0,"relegations":0,
                "bestPointsSeasonId":19010,"bestPointsSeasonValue":0,
            })
            # The standard sandbox account keeps the convenience grant.  RTG
            # starts from a genuinely empty economy and must earn Draft entry
            # through gameplay instead of inheriting ten free tokens.
            self._set_default("draft_tokens",0 if self.is_rtg_mode() else 10)
            self._migrate_legacy_draft_token_grant()
            self._migrate_rtg_draft_token_grant()
            self._migrate_invalid_legacy_sbc_reward()
            self.migrate_legacy_sqbt_quits()

    def _migrate_legacy_draft_token_grant(self):
        """Replace the old 100-token gift with the profile's intended grant.

        The marker makes this a one-time balance migration.  It never caps
        tokens subsequently earned by the user and does not touch any other
        account row.
        """
        marker="draft_token_grant_v2"
        if self.get(marker) is not None:
            return False
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='draft_tokens'").fetchone()
                balance=max(0,int(json.loads(row["value"]))) if row else 0
                initial_grant=0 if self.is_rtg_mode() else 10
                migrated=min(balance,initial_grant)
                if migrated != balance:
                    self.conn.execute(
                        "UPDATE kv SET value=? WHERE key='draft_tokens'",
                        (json.dumps(migrated),))
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?)",
                    (marker,json.dumps({"initialGrant":initial_grant,
                                        "previous":balance})))
                self.conn.commit()
                return migrated != balance
            except Exception:
                self.conn.rollback()
                raise

    def _migrate_rtg_draft_token_grant(self):
        """Remove the former ten-token convenience gift from existing RTGs.

        Subtracting rather than forcing the balance to zero preserves tokens
        above the original grant.  The marker makes this a one-time migration,
        so tokens earned after the update are never removed on restart.
        """
        if not self.is_rtg_mode():
            return False
        marker="rtg_draft_token_grant_removed_v1"
        if self.get(marker) is not None:
            return False
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='draft_tokens'").fetchone()
                balance=max(0,int(json.loads(row["value"]))) if row else 0
                removed=min(balance,10)
                migrated=balance-removed
                if migrated != balance:
                    self.conn.execute(
                        "UPDATE kv SET value=? WHERE key='draft_tokens'",
                        (json.dumps(migrated),))
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?)",
                    (marker,json.dumps({"removed":removed,
                                        "previous":balance})))
                self.conn.commit()
                return removed > 0
            except Exception:
                self.conn.rollback()
                raise

    def _migrate_invalid_legacy_sbc_reward(self):
        """Remove the one impossible reward produced by the early SBC stub.

        An obsolete build marked challenge 191001 complete with an empty squad
        and granted pack 305.  The pack cannot be opened by the corrected SBC
        contract and also keeps the Store's My Packs tab alive.  Match every
        legacy field before deleting anything so legitimate unassigned items,
        unopened packs, and completed SBCs remain untouched.
        """
        row=self.conn.execute(
            "SELECT status,data FROM sbc WHERE set_id=? AND challenge_id=?",
            (1,191001)).fetchone()
        if row is None or row["status"] != "COMPLETED":
            return False
        try:
            if json.loads(row["data"]) != {"squad":{}}:
                return False
        except (TypeError,ValueError):
            return False

        reward_ids=[]
        for reward in self.conn.execute(
                "SELECT id,data FROM unopened_packs WHERE pack_id=?",(305,)).fetchall():
            try:
                payload=json.loads(reward["data"])
            except (TypeError,ValueError):
                continue
            if (payload.get("source") == "sbc" and
                    int(payload.get("challengeId",0) or 0) == 191001):
                reward_ids.append(int(reward["id"]))
        if not reward_ids:
            return False

        placeholders=",".join("?" for _ in reward_ids)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "DELETE FROM unopened_packs WHERE id IN (%s)" % placeholders,
                reward_ids)
            self.conn.execute(
                "DELETE FROM sbc WHERE set_id=? AND challenge_id=?",(1,191001))
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    # ---------- kv ----------
    def _set_default(self, key, value):
        row = self.conn.execute("SELECT 1 FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))
            self.conn.commit()
    def get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
    def set(self, key, value):
        with self.lock:
            self.conn.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
            self.conn.commit()

    def reset_objective_progress(self):
        """Reset only Objective counters/claims and unchosen Objective picks.

        Club items, selected rewards, credits, squads, matches and every other
        mode remain untouched.  A Pick already selected into Unassigned is a
        real claimed item and is deliberately preserved.
        """
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                objective_keys=[str(row["key"]) for row in self.conn.execute(
                    "SELECT key FROM kv WHERE key='objective_claims' OR "
                    "key='objective_claims_v2' OR key='objective_windows_v1' OR "
                    "key LIKE 'objective\\_%' ESCAPE '\\'").fetchall()]
                if objective_keys:
                    placeholders=",".join("?" for _ in objective_keys)
                    self.conn.execute(
                        "DELETE FROM kv WHERE key IN (%s)" % placeholders,
                        objective_keys)
                pending_pick_ids=[]
                for row in self.conn.execute(
                        "SELECT id,status,data FROM player_picks WHERE "
                        "status IN ('PENDING','REDEEMED')").fetchall():
                    try:
                        source_key=str(json.loads(row["data"]).get(
                            "sourceKey","") or "")
                    except (TypeError,ValueError):
                        continue
                    if source_key.startswith("objective:"):
                        pending_pick_ids.append(int(row["id"]))
                if pending_pick_ids:
                    placeholders=",".join("?" for _ in pending_pick_ids)
                    self.conn.execute(
                        "DELETE FROM player_picks WHERE id IN (%s)" %
                        placeholders,pending_pick_ids)
                self.conn.commit()
                return {"objectiveKeysRemoved":len(objective_keys),
                        "pendingObjectivePicksRemoved":len(pending_pick_ids)}
            except Exception:
                self.conn.rollback()
                raise

    # ---------- coins ----------
    def credits(self): return int(self.get("credits", 0) or 0)
    def add_credits(self, amount, external=False):
        if external and not self.external_coin_adjustment_allowed():
            raise PermissionError(
                "RTG mode blocks external coin additions and balance edits")
        with self.lock:
            c = self.credits() + int(amount)
            if c < 0: c = 0
            self.set("credits", c)
            return c
    def set_credits(self, amount, external=False):
        if external and not self.external_coin_adjustment_allowed():
            raise PermissionError(
                "RTG mode blocks external coin additions and balance edits")
        with self.lock:
            self.set("credits", max(0, int(amount))); return self.credits()

    def account_mode(self):
        return normalize_account_mode(self.get(
            "account_mode",self._defaults.get("account_mode",ACCOUNT_MODE_NORMAL)))

    def is_rtg_mode(self):
        return self.account_mode() == ACCOUNT_MODE_RTG

    def pack_probability_profile(self):
        # Retain the profile label for diagnostics while both modes use the
        # same configured probabilities. RTG restrictions belong to economy
        # entry points and never weaken pack contents.
        return "rtg" if self.is_rtg_mode() else "default"

    def external_coin_adjustment_allowed(self):
        """RTG earns coins only through normal in-game transactions."""
        return not self.is_rtg_mode()

    # ---------- club identity ----------
    def club_name(self): return str(self.get("club_name", "FUT Deba Local"))
    def club_abbr(self): return str(self.get("club_abbr", "LF9"))
    def persona_name(self): return str(self.get("persona_name", "FUT Deba Local"))
    def update_profile(self, payload):
        with self.lock:
            # RTG has a fixed public identity as part of its profile contract.
            # Native onboarding may still post the user's previous normal-club
            # name; accepting it would make the separate RTG save appear to be
            # the unrestricted account after the first screen transition.
            if self.is_rtg_mode():
                self.set("club_name","FUT Deba RTG")
                self.set("club_abbr","RTG")
                self.set("persona_name","FUT Deba RTG")
                return {"clubName":self.club_name(),
                        "clubAbbr":self.club_abbr(),
                        "personaName":self.persona_name()}
            if isinstance(payload, dict):
                if payload.get("clubName"): self.set("club_name", str(payload["clubName"]))
                if payload.get("clubAbbr"): self.set("club_abbr", str(payload["clubAbbr"]))
            return {"clubName": self.club_name(), "clubAbbr": self.club_abbr(),
                    "personaName": self.persona_name()}

    # ---------- cards / items ----------
    def _next_item_id(self):
        with self.lock:
            i = int(self.get("next_item_id", 100000000000) or 100000000000)
            self.set("next_item_id", i + 1)
            return i
    def add_item(self, resource_id, pile="club", extra=None):
        with self.lock:
            iid = self._next_item_id()
            item = native_player_fields(int(resource_id), extra)
            item.update({"id": iid, "itemId": iid, "pile": pile, "timestamp": now_s()})
            self.conn.execute(
                "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                "VALUES(?,?,?,?,?)",
                (iid, int(resource_id), pile, "player", json.dumps(item)))
            self.conn.commit()
            return item
    def items_in_pile(self, pile=None, item_kind=None):
        clauses=[]; params=[]
        if pile is not None:
            clauses.append("pile=?"); params.append(str(pile))
        if item_kind is not None:
            clauses.append("item_kind=?"); params.append(str(item_kind))
        where=(" WHERE "+" AND ".join(clauses)) if clauses else ""
        rows=self.conn.execute(
            "SELECT data FROM items%s ORDER BY id" % where,params).fetchall()
        return [json.loads(r["data"]) for r in rows]
    def inventory_counts(self, pile="club"):
        rows=self.conn.execute(
            "SELECT item_kind,COUNT(*) AS count FROM items WHERE pile=? "
            "GROUP BY item_kind ORDER BY item_kind",(str(pile),)).fetchall()
        return {str(row["item_kind"]):int(row["count"]) for row in rows}
    def item(self, item_id):
        row = self.conn.execute("SELECT data FROM items WHERE id=?", (int(item_id),)).fetchone()
        return json.loads(row["data"]) if row else None
    def move_item(self, item_id, pile):
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT pile,data FROM items WHERE id=?",(int(item_id),)
                ).fetchone()
                if not row:
                    self.conn.rollback(); return None
                source_pile=str(row["pile"]); destination=str(pile)
                d = json.loads(row["data"]); d["pile"] = destination
                self.conn.execute(
                    "UPDATE items SET pile=?, data=? WHERE id=?",
                    (destination,json.dumps(d),int(item_id)))
                if source_pile == "purchased" and destination == "club":
                    # A won card belongs to Transfer Targets only while it is
                    # still unassigned. Closing the target in the same
                    # transaction prevents the native client from reloading
                    # the completed purchase as an Expired listing.
                    self._clear_stale_won_market_targets_tx(
                        now_s(),item_id=int(item_id))
                self.conn.commit(); return d
            except Exception:
                self.conn.rollback(); raise
    def apply_consumable_resource(self, resource_id, updates_by_item_id):
        """Atomically update targets and consume one matching club instance."""
        updates_by_item_id={int(key):dict(value) for key,value in
                            dict(updates_by_item_id or {}).items()}
        if not updates_by_item_id:
            return None
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                consumable=self.conn.execute(
                    "SELECT id,data FROM items WHERE pile='club' AND "
                    "item_kind='consumable' AND resource_id=? ORDER BY id LIMIT 1",
                    (int(resource_id),)).fetchone()
                if not consumable:
                    self.conn.rollback(); return None
                updated=[]
                for item_id,changes in updates_by_item_id.items():
                    row=self.conn.execute(
                        "SELECT data FROM items WHERE id=?",(item_id,)).fetchone()
                    if not row:
                        self.conn.rollback(); return None
                    data=json.loads(row["data"]); data.update(changes)
                    self.conn.execute("UPDATE items SET data=? WHERE id=?",
                                      (json.dumps(data),item_id))
                    updated.append(data)
                self.conn.execute("DELETE FROM items WHERE id=?",
                                  (int(consumable["id"]),))
                self.conn.commit()
                return {"items":updated,"consumable":json.loads(consumable["data"])}
            except Exception:
                self.conn.rollback(); raise
    def delete_item(self, item_id):
        with self.lock:
            self.conn.execute("DELETE FROM items WHERE id=?", (int(item_id),)); self.conn.commit()

    def migrate_manager_metadata(self):
        """Backfill source-derived manager team/league and base contracts.

        This is an additive, idempotent migration.  Existing contract values
        are preserved, including a legitimate zero after the contracts have
        been consumed; only legacy rows where both contract keys are absent
        receive the FIFA base value of 30.
        """
        updated=0
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                rows=self.conn.execute(
                    "SELECT id,data FROM items WHERE item_kind='manager'"
                ).fetchall()
                for row in rows:
                    item=json.loads(row["data"])
                    native=manager_item_dto(item)
                    changed=False
                    for key in ("teamid","teamId","leagueId"):
                        value=int(native.get(key,0) or 0)
                        if value and int(item.get(key,0) or 0) != value:
                            item[key]=value; changed=True
                    if "contract" not in item and "contracts" not in item:
                        item["contract"]=30; item["contracts"]=30; changed=True
                    elif "contract" not in item:
                        item["contract"]=int(item.get("contracts",0) or 0); changed=True
                    elif "contracts" not in item:
                        item["contracts"]=int(item.get("contract",0) or 0); changed=True
                    if changed:
                        self.conn.execute("UPDATE items SET data=? WHERE id=?",
                                          (json.dumps(item),int(row["id"])))
                        updated+=1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return updated

    def activate_club_item(self, item_id, slot=None):
        """Persist one active badge, ball, stadium, or kit slot atomically."""
        active_by_kind={
            "badge":"activeBadge", "ball":"activeBall",
            "stadium":"activeStadium",
        }
        with self.lock:
            row=self.conn.execute(
                "SELECT id,pile,item_kind,data FROM items WHERE id=?",
                (int(item_id),)).fetchone()
            if not row or str(row["pile"]) != "club":
                return None
            kind=str(row["item_kind"])
            if kind == "kit":
                try: slot_number=int(slot)
                except (TypeError,ValueError): slot_number=0
                # FIFA 19's KitActivationSlot enum is HOME=101, AWAY=102.
                active_state=("activeAwayKit" if slot_number == 102
                              else "activeHomeKit")
            else:
                active_state=active_by_kind.get(kind)
            if active_state is None:
                return None
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for candidate in self.conn.execute(
                        "SELECT id,data FROM items WHERE pile='club' AND item_kind=?",
                        (kind,)).fetchall():
                    data=json.loads(candidate["data"])
                    if data.get("itemState") == active_state:
                        data["itemState"]="free"
                        self.conn.execute("UPDATE items SET data=? WHERE id=?",
                                          (json.dumps(data),int(candidate["id"])))
                selected=json.loads(row["data"])
                selected["itemState"]=active_state
                selected["pile"]="club"
                self.conn.execute("UPDATE items SET pile='club',data=? WHERE id=?",
                                  (json.dumps(selected),int(row["id"])))
                self.conn.commit()
                return selected
            except Exception:
                self.conn.rollback()
                raise

    def ensure_active_club_items(self):
        """Give migrated accounts the mandatory owned stadium/ball actives."""
        activated={}
        for kind,state in (("stadium","activeStadium"),("ball","activeBall")):
            rows=self.items_in_pile("club",kind)
            current=next((item for item in rows
                          if item.get("itemState") == state),None)
            if current:
                activated[kind]=int(current["id"]); continue
            if rows:
                selected=self.activate_club_item(int(rows[0]["id"]))
                if selected:
                    activated[kind]=int(selected["id"])
        return activated

    def ensure_starter_squad_manager(self):
        """Assign one owned manager to the initial active starter squad."""
        with self.lock:
            row=self.conn.execute(
                "SELECT id,name,data FROM squads "
                "WHERE active=1 AND id<700000 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None or str(row["name"]) != "Starter Italy":
                return {"managerId":0,"updated":False}
            data=json.loads(row["data"] or "{}")
            raw=data.get("manager",[])
            if isinstance(raw,dict):
                raw=[raw]
            manager_id=0
            if isinstance(raw,list) and raw:
                candidate=raw[0]
                if isinstance(candidate,dict):
                    candidate=candidate.get("id",candidate.get("itemId",0))
                try: manager_id=int(candidate or 0)
                except (TypeError,ValueError): manager_id=0
            if not manager_id:
                try: manager_id=int(data.get("coachId",0) or 0)
                except (TypeError,ValueError): manager_id=0
            owned=(self.conn.execute(
                "SELECT id FROM items WHERE id=? AND pile='club' "
                "AND item_kind='manager'",(manager_id,)
            ).fetchone() if manager_id else None)
            if owned is None:
                owned=self.conn.execute(
                    "SELECT id FROM items WHERE pile='club' "
                    "AND item_kind='manager' ORDER BY id LIMIT 1"
                ).fetchone()
                if owned is None:
                    return {"managerId":0,"updated":False}
                manager_id=int(owned["id"])
            normalized=[{"id":manager_id,"dream":False}]
            if data.get("manager") == normalized and int(
                    data.get("coachId",0) or 0) == manager_id:
                return {"managerId":manager_id,"updated":False}
            data["manager"]=normalized
            data["coachId"]=manager_id
            self.conn.execute("UPDATE squads SET data=? WHERE id=?",
                              (json.dumps(data),int(row["id"])))
            self.conn.commit()
            return {"managerId":manager_id,"updated":True}

    def initial_bonus_player_grant_status(self):
        """Report the durable one-time Bronze/Silver player grant."""
        row=self.conn.execute(
            "SELECT value FROM kv WHERE key='initial_bonus_player_grant_v1'"
        ).fetchone()
        marker=json.loads(row["value"]) if row else {}
        item_ids=[int(value) for value in marker.get("itemIds",[])]
        live=0
        if item_ids:
            placeholders=",".join("?" for _ in item_ids)
            live=int(self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE id IN (%s)" % placeholders,
                item_ids).fetchone()[0])
        return {
            "expected":len(INITIAL_BONUS_PLAYERS),
            "recorded":len(item_ids),
            "live":live,
            "bronze":sum(row["quality"]=="bronze"
                         for row in INITIAL_BONUS_PLAYERS),
            "silver":sum(row["quality"]=="silver"
                         for row in INITIAL_BONUS_PLAYERS),
        }

    def ensure_initial_bonus_player_grant(self):
        """Grant 20 Bronze and 12 Silver club players exactly once.

        The marker survives later sales, SBC submissions, or discards, so a
        restart never refills consumed players. All items and the marker are
        committed atomically, and no squad row is modified.
        """
        invalid=validate_initial_bonus_catalogue()
        if invalid:
            raise RuntimeError(
                "initial bonus player definitions are invalid: %s" % invalid)
        timestamp=now_s()
        inserted=0
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                marker=self.conn.execute(
                    "SELECT value FROM kv WHERE key="
                    "'initial_bonus_player_grant_v1'"
                ).fetchone()
                if marker is None:
                    stored_next=int(self.get(
                        "next_item_id",100000000000) or 100000000000)
                    highest=int(self.conn.execute(
                        "SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0])
                    next_id=max(stored_next,highest+1,100000000000)
                    item_ids=[]
                    for index,spec in enumerate(INITIAL_BONUS_PLAYERS):
                        resource_id=int(spec["resourceId"])
                        grant_key="initial-bonus-player-v1:%02d" % index
                        item=native_player_fields(resource_id,{
                            "untradeable":True,"grantKey":grant_key})
                        item.update({"id":next_id,"itemId":next_id,
                                     "pile":"club","timestamp":timestamp})
                        self.conn.execute(
                            "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                            "VALUES(?,?,?,?,?)",
                            (next_id,resource_id,"club","player",
                             json.dumps(item)))
                        item_ids.append(next_id)
                        next_id+=1; inserted+=1
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(next_id),))
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES(?,?)",
                        ("initial_bonus_player_grant_v1",json.dumps({
                            "itemIds":item_ids,"created":timestamp,
                            "bronze":20,"silver":12})))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        status=self.initial_bonus_player_grant_status()
        status["inserted"]=inserted
        return status

    def default_object_grant_status(self):
        expected=(rtg_grant_quantity() if self.is_rtg_mode()
                  else default_grant_quantity())
        recorded=int(self.conn.execute(
            "SELECT COUNT(*) FROM inventory_grants").fetchone()[0])
        live=int(self.conn.execute(
            "SELECT COUNT(*) FROM inventory_grants g "
            "JOIN items i ON i.id=g.item_id").fetchone()[0])
        return {"expected":expected,"recorded":recorded,"live":live}

    def ensure_default_object_grant(self):
        """Migrate the configured non-player inventory exactly once per copy.

        The ledger deliberately survives an item's later deletion/consumption:
        a restart must not refill spent consumables. The entire initial grant
        and its ledger are committed atomically, preserving existing players,
        squads, balance, onboarding choices, and every other account table.
        """
        expanded=list(expanded_rtg_grant() if self.is_rtg_mode()
                      else expanded_default_grant())
        timestamp=now_s()
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing={str(row["grant_key"]) for row in self.conn.execute(
                    "SELECT grant_key FROM inventory_grants").fetchall()}
                stored_next=int(self.get("next_item_id",100000000000) or 100000000000)
                highest=int(self.conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM items").fetchone()[0])
                next_id=max(stored_next,highest+1,100000000000)
                inserted=0
                for grant in expanded:
                    grant_key=str(grant["grantKey"])
                    if grant_key in existing:
                        continue
                    item=dict(grant["item"])
                    item.update({"id":next_id,"itemId":next_id,"pile":"club",
                                 "timestamp":timestamp,"grantKey":grant_key})
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (next_id,int(grant["resourceId"]),"club",
                         str(grant["itemKind"]),json.dumps(item)))
                    self.conn.execute(
                        "INSERT INTO inventory_grants(grant_key,item_id,created,data) "
                        "VALUES(?,?,?,?)",
                        (grant_key,next_id,timestamp,json.dumps({
                            "itemKind":grant["itemKind"],
                            "resourceId":int(grant["resourceId"])})))
                    existing.add(grant_key)
                    next_id+=1; inserted+=1
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id),))
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('default_object_grant',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps({"expected":len(expanded),"updated":timestamp}),))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        status=self.default_object_grant_status()
        status["inserted"]=inserted
        return status

    # ---------- squads ----------
    @staticmethod
    def _is_concept_squad_data(data):
        """Recognise every native spelling used by a Concept Squad.

        Concept players are virtual references (``dream=true``), not owned
        inventory.  Keep legacy rows recoverable in SQLite, but never expose
        or activate them now that FUT Deba Local deliberately disables the
        mode.
        """
        if not isinstance(data,dict):
            return False
        squad_type=str(data.get("squadType",data.get("type","")) or "").upper()
        if "CONCEPT" in squad_type:
            return True
        if any(bool(data.get(key,False)) for key in
               ("concept","isConcept","conceptSquad","dreamSquad")):
            return True
        for entry in list(data.get("players",[]) or [])+list(
                data.get("manager",[]) or []):
            if not isinstance(entry,dict):
                continue
            item=entry.get("itemData",entry.get("item",{}))
            if bool(entry.get("dream",False)) or (
                    isinstance(item,dict) and bool(item.get("dream",False))):
                return True
        return False

    def is_concept_squad(self,squad_id):
        row=self.conn.execute("SELECT data FROM squads WHERE id=?",
                              (int(squad_id),)).fetchone()
        if row is None:
            return False
        try:
            return self._is_concept_squad_data(json.loads(row["data"] or "{}"))
        except (TypeError,ValueError,json.JSONDecodeError):
            return False

    def squads(self):
        rows = self.conn.execute("SELECT id,name,active,data FROM squads ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            # Draft squads live in draft_sessions/draft_picks. Older builds
            # accidentally persisted their 700xxx wire DTO in the regular
            # squads table as well, which exposed it in My Squads and made it
            # consume a normal squad slot. Keep those rows recoverable in
            # SQLite, but never publish or count them as account squads.
            if (int(r["id"]) >= 700000 or
                    str(d.get("squadType", "")).upper() == "DRAFT_SQUAD" or
                    self._is_concept_squad_data(d)):
                continue
            d["id"] = r["id"]; d["squadName"] = r["name"]
            d["active"] = bool(r["active"]); out.append(d)
        return out
    def active_squad(self):
        rows = self.squads()
        for s in rows:
            if s.get("active"): return s
        return rows[0] if rows else {"id": 0, "squadName": "Squad", "players": []}
    def save_squad(self, payload):
        with self.lock:
            if self._is_concept_squad_data(payload):
                raise ValueError(
                    "Concept Squads are disabled in FUT Deba Local.")
            sid = int(payload.get("id", 0) or 0)
            if (sid >= 700000 or
                    str(payload.get("squadType", "")).upper() == "DRAFT_SQUAD"):
                raise ValueError("Draft squads cannot be stored as regular squads")
            name = str(payload.get("squadName", payload.get("name", "Squad")))
            data = json.dumps(payload)
            if sid and self.conn.execute("SELECT 1 FROM squads WHERE id=?", (sid,)).fetchone():
                self.conn.execute("UPDATE squads SET name=?, data=? WHERE id=?", (name, data, sid))
            else:
                if not sid:
                    # Draft wire IDs occupy the separate 700xxx namespace.
                    # A legacy Draft row in this table must not make the next
                    # regular squad become 700002 and disappear from My Squads.
                    sid = self.conn.execute(
                        "SELECT COALESCE(MAX(id),0)+1 FROM squads WHERE id<700000"
                    ).fetchone()[0]
                self.conn.execute("INSERT INTO squads(id,name,active,data) VALUES(?,?,?,?)",
                                  (sid, name, 0, data))
            self.conn.commit()
            payload["id"] = sid
            return payload

    def _is_draft_row(self, row_id, data):
        return (int(row_id) >= 700000 or
                str(data.get("squadType", "")).upper() == "DRAFT_SQUAD")

    def squad_count(self):
        return len(self.squads())

    def set_active_squad(self, squad_id):
        # The client shows exactly one active squad. Clearing the flag on every
        # other regular row here means the invariant holds whichever route sets
        # it, instead of relying on each caller to remember.
        sid = int(squad_id)
        with self.lock:
            row = self.conn.execute("SELECT id,data FROM squads WHERE id=?",
                                    (sid,)).fetchone()
            if row is None:
                return None
            row_data=json.loads(row["data"])
            if self._is_concept_squad_data(row_data):
                raise ValueError(
                    "Concept Squads are disabled in FUT Deba Local.")
            if self._is_draft_row(row["id"], row_data):
                raise ValueError("A Draft squad cannot become the active squad")
            self.conn.execute("UPDATE squads SET active=0 WHERE id<700000")
            self.conn.execute("UPDATE squads SET active=1 WHERE id=?", (sid,))
            self.conn.commit()
        return self.active_squad()

    def delete_squad(self, squad_id):
        # The last squad is not deletable: the client always expects one to
        # exist. Deleting the active one promotes the first survivor so the
        # account never ends up without a selected squad.
        sid = int(squad_id)
        with self.lock:
            row = self.conn.execute("SELECT id,active,data FROM squads WHERE id=?",
                                    (sid,)).fetchone()
            if row is None:
                return False
            row_data=json.loads(row["data"])
            if self._is_concept_squad_data(row_data):
                raise ValueError(
                    "Concept Squads are disabled in FUT Deba Local.")
            if self._is_draft_row(row["id"], row_data):
                raise ValueError("Draft squads are not deletable from My Squads")
            remaining = self.conn.execute(
                "SELECT COUNT(*) FROM squads WHERE id<700000").fetchone()[0]
            if remaining <= 1:
                raise ValueError("The last squad cannot be deleted")
            was_active = bool(row["active"])
            self.conn.execute("DELETE FROM squads WHERE id=?", (sid,))
            if was_active:
                nxt = self.conn.execute(
                    "SELECT id FROM squads WHERE id<700000 ORDER BY id LIMIT 1"
                ).fetchone()
                if nxt is not None:
                    self.conn.execute("UPDATE squads SET active=1 WHERE id=?",
                                      (nxt["id"],))
            self.conn.commit()
        return True

    def copy_squad(self, source_id=None, name=None, max_squads=10):
        # In FUT 19 a new squad is always a copy of an existing one: the client
        # calls FutSquadCopy, never a create-from-nothing. Returning None at the
        # limit lets the caller answer with the native error instead of raising.
        rows = self.squads()
        if len(rows) >= int(max_squads):
            return None
        source = None
        if source_id is not None:
            raw=self.conn.execute("SELECT data FROM squads WHERE id=?",
                                  (int(source_id),)).fetchone()
            if raw is not None and self._is_concept_squad_data(
                    json.loads(raw["data"] or "{}")):
                raise ValueError(
                    "Concept Squads are disabled in FUT Deba Local.")
            source = next((x for x in rows
                           if int(x.get("id", 0) or 0) == int(source_id)), None)
        if source is None:
            source = self.active_squad() if rows else {}
        payload = json.loads(json.dumps(source)) if source else {}
        payload.pop("id", None)
        payload["active"] = False
        payload["squadName"] = str(name or ("Squad %d" % (len(rows) + 1)))
        return self.save_squad(payload)

    # ---------- packs ----------
    def add_unopened_pack(self, pack_id, data=None):
        with self.lock:
            self.conn.execute("INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                              (int(pack_id), json.dumps(data or {})))
            self.conn.commit()
    def unopened_packs(self):
        rows = self.conn.execute("SELECT id,pack_id,data FROM unopened_packs ORDER BY id").fetchall()
        return [{"id": r["id"], "packId": r["pack_id"], **json.loads(r["data"])} for r in rows]
    def reward_unopened_packs(self):
        """Return only provenance-backed rewards advertised in My Packs.

        Legacy rows without a known source are retained for forensic safety,
        but no longer create a Store notification or a free pack surface.
        """
        return [pack for pack in self.unopened_packs()
                if str(pack.get("source","")).lower() in
                   ("objective","sbc","sbc_set","draft","sqbt",
                    "champions","season")]
    def take_unopened_pack(self, pack_id):
        with self.lock:
            row=self.conn.execute(
                "SELECT id,pack_id,data FROM unopened_packs WHERE pack_id=? ORDER BY id LIMIT 1",
                (int(pack_id),)).fetchone()
            if not row:
                return None
            self.conn.execute("DELETE FROM unopened_packs WHERE id=?",(row["id"],))
            self.conn.commit()
            return {"id":row["id"],"packId":row["pack_id"],**json.loads(row["data"])}
    def take_reward_pack(self, pack_id):
        with self.lock:
            rows=self.conn.execute(
                "SELECT id,pack_id,data FROM unopened_packs WHERE pack_id=? ORDER BY id",
                (int(pack_id),)).fetchall()
            for row in rows:
                data=json.loads(row["data"])
                if str(data.get("source","")).lower() not in \
                        ("objective","sbc","sbc_set","draft","sqbt",
                         "champions","season"):
                    continue
                self.conn.execute("DELETE FROM unopened_packs WHERE id=?",
                                  (int(row["id"]),))
                self.conn.commit()
                return {"id":row["id"],"packId":row["pack_id"],**data}
            return None
    def reward_unopened_pack(self, pack_id):
        """Return the oldest eligible reward without consuming it."""
        for pack in self.reward_unopened_packs():
            if int(pack.get("packId",0) or 0) == int(pack_id):
                return pack
        return None

    @staticmethod
    def _reward_source(data):
        # Group SBC and Champions prizes use their mode-level provenance.
        # Excluding either source strands a successfully granted pack outside
        # My Packs and prevents the atomic reward-open path from consuming it.
        return str(data.get("source","")).lower() in (
            "objective","sbc","sbc_set","draft","sqbt","champions",
            "season")

    def _reward_row_in_transaction(self, reward_pack_id, pack_id):
        if reward_pack_id is None:
            return None
        row=self.conn.execute(
            "SELECT id,pack_id,data FROM unopened_packs WHERE id=?",
            (int(reward_pack_id),)).fetchone()
        if row is None or int(row["pack_id"]) != int(pack_id):
            return None
        data=json.loads(row["data"])
        if not self._reward_source(data):
            return None
        return row

    def purchase_pack(self, pack_id, price, item_specs, seed, currency="COINS",
                      reward_pack_id=None):
        """Charge and save the complete pack in one transaction.

        A process interruption cannot leave a charge without items (or the
        reverse). Generated items enter the purchased/unassigned pile.
        """
        with self.lock:
            price=max(0,int(price))
            base_items=[]
            for spec in item_specs:
                object_spec=spec.get("objectSpec")
                if isinstance(object_spec,dict):
                    item=build_grant_item(object_spec)
                    item.update(spec.get("extra") or {})
                    rid=int(item["resourceId"])
                    item_kind=stored_item_kind(item)
                else:
                    rid=int(spec["resourceId"])
                    item=native_player_fields(rid,spec.get("extra") or {})
                    item_kind="player"
                base_items.append((rid,item_kind,item))
            try:
                # Acquire the SQLite lock before reading balance and next ID.
                # ADD_COINS.cmd can run beside the server without a concurrent
                # purchase losing either update.
                self.conn.execute("BEGIN IMMEDIATE")
                reward_row=self._reward_row_in_transaction(
                    reward_pack_id,pack_id)
                if reward_pack_id is not None and reward_row is None:
                    self.conn.rollback()
                    return None
                if reward_row is not None:
                    price=0
                    reward_data=json.loads(reward_row["data"])
                    if bool(reward_data.get("untradeable",False)):
                        for _,_,item in base_items:
                            item["untradeable"]=True
                            item["tradeable"]=False
                            item["discardValue"]=0
                current=self.credits()
                if current < price:
                    self.conn.rollback()
                    return None
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                prepared=[]
                for offset,(rid,item_kind,item) in enumerate(base_items):
                    iid=next_id+offset
                    item.update({"id":iid,"itemId":iid,"pile":"purchased",
                                 "timestamp":now_s()})
                    prepared.append((iid,rid,item_kind,item))
                new_credits=current-price
                self.conn.execute("INSERT INTO kv(key,value) VALUES('credits',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(new_credits),))
                self.conn.execute("INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id+len(prepared)),))
                for iid,rid,item_kind,item in prepared:
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (iid,rid,"purchased",item_kind,json.dumps(item)))
                cur=self.conn.execute(
                    "INSERT INTO pack_openings(created,pack_id,price,currency,seed,data) VALUES(?,?,?,?,?,?)",
                    (now_s(),int(pack_id),price,str(currency),int(seed),
                     json.dumps({"itemIds":[x[0] for x in prepared],
                                 "rewardPackId":(int(reward_row["id"])
                                     if reward_row is not None else None)})))
                if reward_row is not None:
                    self.conn.execute("DELETE FROM unopened_packs WHERE id=?",
                                      (int(reward_row["id"]),))
                self.conn.commit()
                return {"openingId":cur.lastrowid,"credits":new_credits,
                        "items":[x[3] for x in prepared],
                        "rewardPackId":(int(reward_row["id"])
                            if reward_row is not None else None)}
            except Exception:
                self.conn.rollback()
                raise

    def purchase_player_pick_bundle(self, pack_id, price, pick_specs, seed,
                                    currency="COINS",reward_pack_id=None):
        """Persist unopened picks atomically; options are not club items yet."""
        with self.lock:
            price=max(0,int(price)); prepared=[]
            for pick in pick_specs:
                options=[]
                for spec in pick.get("options",[]):
                    rid=int(spec["resourceId"])
                    options.append((rid,native_player_fields(
                        rid,spec.get("extra") or {})))
                if len(options)<2:
                    raise ValueError("a Player Pick requires at least two options")
                prepared.append((pick,options))
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                reward_row=self._reward_row_in_transaction(
                    reward_pack_id,pack_id)
                if reward_pack_id is not None and reward_row is None:
                    self.conn.rollback(); return None
                if reward_row is not None:
                    price=0
                    reward_data=json.loads(reward_row["data"])
                    if bool(reward_data.get("untradeable",False)):
                        for _,options in prepared:
                            for _,item in options:
                                item["untradeable"]=True
                                item["tradeable"]=False
                                item["discardValue"]=0
                current=self.credits()
                if current<price:
                    self.conn.rollback(); return None
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                stored=[]; cursor=next_id
                for pick,options in prepared:
                    pick_id=cursor; cursor+=1; native_options=[]
                    for rid,item in options:
                        iid=cursor; cursor+=1
                        item.update({"id":iid,"itemId":iid,"pile":"playerpick",
                                     "timestamp":now_s()})
                        native_options.append(item)
                    payload={"id":pick_id,"itemId":pick_id,"timestamp":now_s(),
                             "playerPickId":pick_id,"packId":int(pack_id),
                             "minRating":int(pick.get("minRating",81)),
                             "optionCount":len(native_options),
                             "options":native_options}
                    self.conn.execute(
                        "INSERT INTO player_picks(id,status,data) VALUES(?,?,?)",
                        (pick_id,"PENDING",json.dumps(payload)))
                    stored.append(payload)
                new_credits=current-price
                self.conn.execute("INSERT INTO kv(key,value) VALUES('credits',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(new_credits),))
                self.conn.execute("INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(cursor),))
                cur=self.conn.execute(
                    "INSERT INTO pack_openings(created,pack_id,price,currency,seed,data) VALUES(?,?,?,?,?,?)",
                    (now_s(),int(pack_id),price,str(currency),int(seed),
                     json.dumps({"playerPickIds":[x["id"] for x in stored],
                                 "rewardPackId":(int(reward_row["id"])
                                     if reward_row is not None else None)})))
                if reward_row is not None:
                    self.conn.execute("DELETE FROM unopened_packs WHERE id=?",
                                      (int(reward_row["id"]),))
                self.conn.commit()
                return {"openingId":cur.lastrowid,"credits":new_credits,
                        "playerPicks":stored,
                        "rewardPackId":(int(reward_row["id"])
                            if reward_row is not None else None)}
            except Exception:
                self.conn.rollback(); raise

    def pending_player_picks(self):
        rows=self.conn.execute(
            "SELECT data FROM player_picks WHERE status='PENDING' ORDER BY id").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def active_player_pick(self):
        """Return the one redeemed pick awaiting a native selection."""
        row=self.conn.execute(
            "SELECT data FROM player_picks WHERE status='REDEEMED' "
            "ORDER BY id LIMIT 1").fetchone()
        return json.loads(row["data"]) if row else None

    def redeem_player_pick(self, pick_id):
        """Move one unopened Pick Item into FIFA's temporary choice state.

        FIFA permits only one active selection at a time.  Existing rows from
        the first bundle implementation remain ``PENDING`` and therefore stay
        visible in the unassigned pile until the user explicitly redeems one.
        """
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                active=self.conn.execute(
                    "SELECT data FROM player_picks WHERE status='REDEEMED' "
                    "ORDER BY id LIMIT 1").fetchone()
                if active:
                    self.conn.commit()
                    return json.loads(active["data"])
                row=self.conn.execute(
                    "SELECT data FROM player_picks WHERE id=? AND status='PENDING'",
                    (int(pick_id),)).fetchone()
                if not row:
                    self.conn.rollback(); return None
                payload=json.loads(row["data"])
                self.conn.execute(
                    "UPDATE player_picks SET status='REDEEMED' WHERE id=?",
                    (int(pick_id),))
                self.conn.commit(); return payload
            except Exception:
                self.conn.rollback(); raise

    def _grant_player_pick_tx(self,source_key,pick_spec,pack_id=0,
                              deduplicate=True):
        """Grant one Player Pick inside the caller's open transaction."""
        source_key=str(source_key)
        if deduplicate:
            rows=self.conn.execute(
                "SELECT data FROM player_picks ORDER BY id").fetchall()
            for row in rows:
                existing=json.loads(row["data"])
                if str(existing.get("sourceKey",'')) == source_key:
                    return existing
        options=[]
        for spec in pick_spec.get("options",[]):
            rid=int(spec["resourceId"])
            options.append((rid,native_player_fields(
                rid,spec.get("extra") or {})))
        if len(options)<2:
            raise ValueError("a Player Pick requires at least two options")
        cursor=int(self.get("next_item_id",100000000000) or 100000000000)
        pick_id=cursor; cursor+=1; native_options=[]
        for rid,item in options:
            iid=cursor; cursor+=1
            item.update({"id":iid,"itemId":iid,"pile":"playerpick",
                         "timestamp":now_s()})
            native_options.append(item)
        payload={"id":pick_id,"itemId":pick_id,"timestamp":now_s(),
                 "playerPickId":pick_id,"packId":int(pack_id),
                 "sourceKey":source_key,
                 "minRating":int(pick_spec.get("minRating",83)),
                 "optionCount":len(native_options),
                 "options":native_options}
        for key in ("position","quality","name","description",
                    "rewardSource"):
            if key in pick_spec:
                payload[key]=pick_spec[key]
        self.conn.execute(
            "INSERT INTO player_picks(id,status,data) VALUES(?,?,?)",
            (pick_id,"PENDING",json.dumps(payload)))
        self.conn.execute("INSERT INTO kv(key,value) VALUES('next_item_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(cursor),))
        return payload

    def grant_player_pick(self, source_key, pick_spec, pack_id=0):
        """Grant one pick once for a stable reward source.

        Keeping the source key in the persisted payload makes objective claims
        crash-safe: retrying a response can return the original pick instead
        of manufacturing duplicate options.
        """
        source_key=str(source_key)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                payload=self._grant_player_pick_tx(source_key,pick_spec,pack_id)
                self.conn.commit(); return payload
            except Exception:
                self.conn.rollback(); raise

    def select_player_pick(self, pick_id, selected_item_id,
                           require_redeemed=False, by_resource_id=False):
        """Move exactly the selected candidate into the purchased pile."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                allowed_status="status='REDEEMED'" if require_redeemed else \
                               "status IN ('PENDING','REDEEMED')"
                row=self.conn.execute(
                    "SELECT data FROM player_picks WHERE id=? AND "+allowed_status,
                    (int(pick_id),)).fetchone()
                if not row:
                    self.conn.rollback(); return None
                payload=json.loads(row["data"]); selected=None
                for option in payload.get("options",[]):
                    option_key=(option.get("resourceId",0) if by_resource_id
                                else option.get("id",0))
                    if int(option_key or 0)==int(selected_item_id):
                        selected=dict(option); break
                if selected is None:
                    self.conn.rollback(); return None
                selected["pile"]="purchased"; selected["itemState"]="free"
                self.conn.execute(
                    "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                    "VALUES(?,?,?,?,?)",
                    (int(selected["id"]),int(selected["resourceId"]),"purchased",
                     "player",json.dumps(selected)))
                payload["selectedItemId"]=int(selected["id"])
                payload["selectedItem"]=selected
                self.conn.execute(
                    "UPDATE player_picks SET status='SELECTED',data=? WHERE id=?",
                    (json.dumps(payload),int(pick_id)))
                self.conn.commit(); return selected
            except Exception:
                self.conn.rollback(); raise

    def pack_openings(self):
        rows=self.conn.execute("SELECT * FROM pack_openings ORDER BY id").fetchall()
        return [{**dict(r),**json.loads(r["data"])} for r in rows]

    # ---------- persistent transfer market ----------
    @staticmethod
    def _market_auction_dict(row, timestamp=None):
        if row is None:
            return None
        now=now_s() if timestamp is None else int(timestamp)
        raw=dict(row)
        data=json.loads(raw.get("data") or "{}")
        item=json.loads(raw.get("item_payload") or "{}")
        state=str(raw.get("state") or "active").lower()
        expires=(max(0,int(raw.get("expires",0))-now)
                 if state=="active" else -1)
        return {
            "tradeId":int(raw["trade_id"]),"itemId":int(raw.get("item_id",0)),
            "resourceId":int(raw.get("resource_id",0)),
            "sellerIsUser":bool(raw.get("seller_is_user",0)),
            "startingBid":int(raw.get("starting_bid",0)),
            "buyNowPrice":int(raw.get("buy_now",0)),
            "currentBid":int(raw.get("current_bid",0)),
            "created":int(raw.get("created",0)),
            "expiresAt":int(raw.get("expires",0)),"expires":expires,
            "tradeState":state,"state":state,"itemData":item,
            **data,
        }

    @staticmethod
    def _market_credits_tx(conn):
        row=conn.execute("SELECT value FROM kv WHERE key='credits'").fetchone()
        return max(0,int(json.loads(row["value"]))) if row else 0

    @staticmethod
    def _market_set_credits_tx(conn, value):
        normalized=max(0,int(value))
        conn.execute("INSERT INTO kv(key,value) VALUES('credits',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (json.dumps(normalized),))
        return normalized

    @staticmethod
    def _market_increment_tx(conn, key, amount=1):
        row=conn.execute("SELECT value FROM kv WHERE key=?",(str(key),)).fetchone()
        current=int(json.loads(row["value"])) if row else 0
        updated=current+int(amount)
        conn.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (str(key),json.dumps(updated)))
        return updated

    @staticmethod
    def _market_receipt_tx(conn, operation_key):
        row=conn.execute(
            "SELECT response FROM market_operations WHERE operation_key=?",
            (str(operation_key),)).fetchone()
        return json.loads(row["response"]) if row else None

    @staticmethod
    def _store_market_receipt_tx(conn, operation_key, kind, trade_id, response,
                                 timestamp):
        conn.execute(
            "INSERT INTO market_operations(operation_key,kind,trade_id,created,response) "
            "VALUES(?,?,?,?,?)",
            (str(operation_key),str(kind),int(trade_id),int(timestamp),
             json.dumps(response)))

    @staticmethod
    def _market_item_from_row(row):
        item=json.loads(row["data"])
        item["id"]=int(row["id"]); item["itemId"]=int(row["id"])
        item["resourceId"]=int(row["resource_id"])
        item["pile"]=str(row["pile"])
        item["itemKind"]=str(row["item_kind"])
        return item

    def _grant_market_item_tx(self, payload, trade_id, timestamp,
                              acquisition="BUY_NOW"):
        source=dict(payload or {})
        rid=int(source.get("resourceId",source.get("definitionId",0)) or 0)
        if rid <= 0:
            raise ValueError("market listing has no sourced item identity")
        kind=stored_item_kind(source)
        supported={"player","manager","staff","kit","badge","stadium",
                   "consumable"}
        if kind not in supported:
            raise ValueError("market listing has unsupported item kind %r" % kind)
        if kind == "player":
            item=native_player_fields(rid,source)
        else:
            # The AI supply is built from the same source-backed definitions
            # as My Club. Re-run the concrete serializer before persistence so
            # a bought object cannot lose the union member which selected its
            # native card class in the search result.
            prepared=dict(source)
            prepared.update({"resourceId":rid,"inventoryType":kind,
                             "untradeable":False,"tradeable":True,
                             "pile":"purchased"})
            item=native_object_item(prepared)
            item.update({"inventoryType":kind,
                         "catalogType":str(source.get("catalogType",kind)),
                         "untradeable":False,"tradeable":True})
            if kind == "consumable":
                # This private definition tag is needed when the persisted row
                # is serialized again by /purchased/items and /item; the wire
                # DTO alone only retains its concrete consumables* member.
                item["definitionItemType"]=str(
                    source.get("definitionItemType", ""))
        next_row=self.conn.execute(
            "SELECT value FROM kv WHERE key='next_item_id'").fetchone()
        item_id=(int(json.loads(next_row["value"])) if next_row
                 else 100000000000)
        item.update({"id":item_id,"itemId":item_id,"pile":"purchased",
                     "timestamp":int(timestamp),"marketTradeId":int(trade_id),
                     "acquiredAt":int(timestamp),"acquiredFrom":"LOCAL_MARKET",
                     "marketAcquisition":str(acquisition)})
        self.conn.execute(
            "INSERT INTO items(id,resource_id,pile,item_kind,data) VALUES(?,?,?,?,?)",
            (item_id,rid,"purchased",kind,json.dumps(item)))
        self.conn.execute("INSERT INTO kv(key,value) VALUES('next_item_id',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(item_id+1),))
        return item

    def market_operation(self, operation_key):
        row=self.conn.execute(
            "SELECT response FROM market_operations WHERE operation_key=?",
            (str(operation_key),)).fetchone()
        return json.loads(row["response"]) if row else None

    def _active_starting_player_ids_tx(self):
        """Resolve owned starter instance IDs from the persisted active squad."""
        row=self.conn.execute(
            "SELECT id,data FROM squads WHERE id<700000 "
            "ORDER BY active DESC,id LIMIT 1").fetchone()
        if row is None:
            return 0,[]
        squad=json.loads(row["data"] or "{}")
        result=[]
        for order,slot in enumerate(squad.get("players",[]) or []):
            if not isinstance(slot,dict):
                continue
            try:
                index=int(slot.get("index",order))
            except (TypeError,ValueError):
                continue
            if index not in range(11):
                continue
            ids=extract_item_ids({"players":[slot]})
            if ids and ids[0] not in result:
                result.append(ids[0])
        return int(row["id"]),result

    def _record_market_player_debuts_tx(self, mode, match_key, timestamp):
        """Advance debut Objectives once per exact market-acquired item.

        The caller owns the SQLite transaction.  Keeping the match insert,
        Objective counter and per-item receipt together prevents both a lost
        debut after a crash and duplicate progress after a request retry.
        """
        normalized_mode=str(mode or "OFFLINE").upper()
        if normalized_mode not in ("OFFLINE","SQBT","SQUAD_BATTLES","TOTW"):
            return []
        squad_id,item_ids=self._active_starting_player_ids_tx()
        recorded=[]
        for item_id in item_ids:
            row=self.conn.execute(
                "SELECT pile,item_kind,resource_id,data FROM items WHERE id=?",
                (int(item_id),)).fetchone()
            if (row is None or str(row["pile"]) != "club" or
                    str(row["item_kind"]) != "player"):
                continue
            item=json.loads(row["data"] or "{}")
            try: trade_id=int(item.get("marketTradeId",0) or 0)
            except (TypeError,ValueError): trade_id=0
            if (trade_id <= 0 or
                    str(item.get("acquiredFrom","")).upper() != "LOCAL_MARKET"):
                continue
            operation_key="market-player-debut:%d" % int(item_id)
            if self._market_receipt_tx(self.conn,operation_key) is not None:
                continue
            receipt={"operationKey":operation_key,"matchKey":str(match_key),
                     "mode":normalized_mode,"squadId":squad_id,
                     "itemId":int(item_id),"tradeId":trade_id,
                     "resourceId":int(row["resource_id"]),
                     "recordedAt":int(timestamp)}
            self._store_market_receipt_tx(
                self.conn,operation_key,"MARKET_PLAYER_DEBUT",trade_id,
                receipt,timestamp)
            recorded.append(receipt)
        if recorded:
            self._market_increment_tx(
                self.conn,"objective_player_debuts",len(recorded))
        return recorded

    def record_market_player_debuts(self, mode, match_key, timestamp=None):
        """Idempotent recovery entry point for an already persisted match."""
        at=now_s() if timestamp is None else int(timestamp)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                recorded=self._record_market_player_debuts_tx(
                    mode,match_key,at)
                self.conn.commit(); return recorded
            except Exception:
                self.conn.rollback(); raise

    def market_auctions(self, seller_is_user=None, timestamp=None):
        sql="SELECT * FROM auctions WHERE state!='cleared'"
        args=[]
        if seller_is_user is not None:
            sql+=" AND seller_is_user=?"; args.append(int(bool(seller_is_user)))
        sql+=" ORDER BY trade_id"
        rows=self.conn.execute(sql,args).fetchall()
        return [self._market_auction_dict(row,timestamp) for row in rows]

    def market_auction(self, trade_id, timestamp=None):
        row=self.conn.execute("SELECT * FROM auctions WHERE trade_id=?",
                              (int(trade_id),)).fetchone()
        return self._market_auction_dict(row,timestamp)

    def market_ai_snapshot(self, trade_id, timestamp=None):
        auction=self.market_auction(trade_id,timestamp)
        if not auction or auction["sellerIsUser"]:
            return None
        metadata={key:auction.get(key) for key in
                  ("marketEpoch","filterHash","snapshotVersion",
                   "snapshotSignature","absoluteIndex","listingKind")
                  if auction.get(key) is not None}
        return {"tradeId":auction["tradeId"],"itemId":auction["itemId"],
                "resourceId":auction["resourceId"],"fields":auction["itemData"],
                "startingBid":auction["startingBid"],
                "currentBid":auction["currentBid"],
                "buyNowPrice":auction["buyNowPrice"],
                "expires":auction["expires"],**metadata}

    def market_unavailable_trade_ids(self):
        rows=self.conn.execute(
            "SELECT trade_id FROM auctions WHERE seller_is_user=0 AND state!='active'"
        ).fetchall()
        return {int(row["trade_id"]) for row in rows}

    def _market_item_used_by_squad_tx(self,item_id):
        """Return true when a regular squad still references an owned item."""
        wanted=int(item_id)
        for row in self.conn.execute("SELECT data FROM squads").fetchall():
            try:
                squad=json.loads(row["data"] or "{}")
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
            if wanted in extract_item_ids(squad):
                return True
        return False

    def list_market_item(self, item_id, starting_bid, buy_now, duration,
                         sale=None, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        item_id=int(item_id); starting=int(starting_bid)
        buy=int(buy_now); seconds=int(duration)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing=self.conn.execute(
                    "SELECT * FROM auctions WHERE item_id=? AND seller_is_user=1 "
                    "AND state='active' ORDER BY trade_id DESC LIMIT 1",
                    (item_id,)).fetchone()
                if existing is not None:
                    self.conn.commit()
                    return self._market_auction_dict(existing,now)
                if self.conn.execute(
                    "SELECT COUNT(*) AS n FROM auctions WHERE seller_is_user=1 "
                    "AND state IN ('active','sold','expired')").fetchone()["n"] >= \
                        MARKET_TRADE_PILE_CAPACITY:
                    self.conn.rollback(); return None
                row=self.conn.execute("SELECT * FROM items WHERE id=?",
                                      (item_id,)).fetchone()
                supported={"player","manager","staff","kit","badge",
                           "stadium","consumable"}
                if row is None or str(row["item_kind"]) not in supported or \
                        str(row["pile"]) not in ("club","trade"):
                    self.conn.rollback(); return None
                item=self._market_item_from_row(row)
                if bool(item.get("untradeable",False)) or \
                        int(item.get("loans",0) or 0)>0:
                    self.conn.rollback(); return None
                if (str(row["item_kind"])=="player" and
                        self._market_item_used_by_squad_tx(item_id)):
                    self.conn.rollback(); return None
                from fut_market import market_price_limits, snap_market_price
                minimum,maximum=market_price_limits(item)
                if (seconds not in MARKET_DURATIONS or
                        starting < minimum or starting > maximum or
                        buy < starting or buy > maximum or
                        snap_market_price(starting) != starting or
                        snap_market_price(buy) != buy):
                    self.conn.rollback(); return None
                next_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='next_trade_id'").fetchone()
                trade_id=(int(json.loads(next_row["value"])) if next_row
                          else 650000000000)
                self.conn.execute("INSERT INTO kv(key,value) VALUES('next_trade_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(trade_id+1),))
                item["pile"]="trade"
                self.conn.execute("UPDATE items SET pile='trade',data=? WHERE id=?",
                                  (json.dumps(item),item_id))
                if sale is None:
                    from fut_market import deterministic_user_sale
                    sale=deterministic_user_sale(
                        item,buy,seconds,trade_id,now)
                market_data={"offers":0,"bidState":"none",
                             "sellerName":self.persona_name(),
                             "sellerId":1,"tradeOwner":True,**dict(sale or {})}
                self.conn.execute(
                    "INSERT INTO auctions(trade_id,item_id,resource_id,seller_is_user,"
                    "starting_bid,buy_now,current_bid,created,expires,state,data,item_payload) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (trade_id,item_id,int(row["resource_id"]),1,starting,buy,0,
                     now,now+seconds,"active",json.dumps(market_data),json.dumps(item)))
                self._market_increment_tx(self.conn,"objective_transfer_listings",1)
                result=self._market_auction_dict(self.conn.execute(
                    "SELECT * FROM auctions WHERE trade_id=?",(trade_id,)).fetchone(),now)
                self._store_market_receipt_tx(
                    self.conn,"market-list:%d"%trade_id,"LIST",trade_id,
                    {"tradeId":trade_id,"itemId":item_id},now)
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def _ensure_ai_auction_tx(self, snapshot, timestamp):
        trade_id=int(snapshot.get("tradeId",0) or 0)
        row=self.conn.execute("SELECT * FROM auctions WHERE trade_id=?",
                              (trade_id,)).fetchone()
        if row is not None:
            return row
        from fut_market import verify_player_listing_snapshot
        if not verify_player_listing_snapshot(snapshot):
            return None
        fields=dict(snapshot.get("fields") or {})
        rid=int(snapshot.get("resourceId",fields.get("resourceId",0)) or 0)
        if trade_id <= 0 or rid <= 0:
            return None
        item_id=int(snapshot.get("itemId",trade_id) or trade_id)
        remaining=max(1,int(snapshot.get("expires",300) or 300))
        data={"offers":0,"bidState":"none","sellerName":"Local Market",
              "sellerId":0,"tradeOwner":False,"source":"DETERMINISTIC_AI"}
        for key in ("marketEpoch","filterHash","snapshotVersion",
                    "snapshotSignature","absoluteIndex","listingKind"):
            if key in snapshot:
                data[key]=snapshot[key]
        self.conn.execute(
            "INSERT INTO auctions(trade_id,item_id,resource_id,seller_is_user,"
            "starting_bid,buy_now,current_bid,created,expires,state,data,item_payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id,item_id,rid,0,int(snapshot.get("startingBid",150) or 150),
             int(snapshot.get("buyNowPrice",150) or 150),
             int(snapshot.get("currentBid",0) or 0),int(timestamp),
             int(timestamp)+remaining,"active",json.dumps(data),json.dumps(fields)))
        return self.conn.execute("SELECT * FROM auctions WHERE trade_id=?",
                                 (trade_id,)).fetchone()

    def watch_market_listing(self, snapshot, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(snapshot.get("tradeId",0) or 0)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                target=self.conn.execute(
                    "SELECT * FROM market_targets WHERE trade_id=?",
                    (trade_id,)).fetchone()
                if target is None and self.conn.execute(
                        "SELECT COUNT(*) AS n FROM market_targets "
                        "WHERE status!='CLEARED'").fetchone()["n"] >= \
                        MARKET_WATCHLIST_CAPACITY:
                    self.conn.rollback(); return None
                auction=self._ensure_ai_auction_tx(snapshot,now)
                if auction is None or str(auction["state"])!="active":
                    self.conn.rollback(); return None
                if (target is not None and str(target["status"])=="HIGHEST" and
                        int(target["reserved_credits"])>0):
                    # Native clients can repeat the watch action while their
                    # bid is highest. Never erase the reservation or silently
                    # turn that bid back into a plain watchlist row.
                    result={"tradeId":trade_id,"status":"HIGHEST",
                            "watched":True,"bidState":"highest",
                            "reservedCredits":int(target["reserved_credits"])}
                    self.conn.commit(); return result
                self.conn.execute(
                    "INSERT INTO market_targets(trade_id,status,reserved_credits,created,updated,data) "
                    "VALUES(?, 'WATCHED',0,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
                    "status='WATCHED',reserved_credits=0,updated=excluded.updated,"
                    "data=excluded.data",
                    (trade_id,now,now,json.dumps({"watched":True})))
                result={"tradeId":trade_id,"status":"WATCHED","watched":True}
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    @staticmethod
    def _minimum_market_bid(current, starting):
        value=max(int(current or 0),int(starting or 0))
        if int(current or 0)<=0:
            return value
        if value < 1_000: step=50
        elif value < 10_000: step=100
        elif value < 50_000: step=250
        elif value < 100_000: step=500
        else: step=1_000
        return value+step

    def bid_market_listing(self, snapshot, amount, timestamp=None):
        from fut_market import deterministic_bid_outcome
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(snapshot.get("tradeId",0) or 0); bid=max(0,int(amount))
        operation="market-bid:%d:%d"%(trade_id,bid)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt=self._market_receipt_tx(self.conn,operation)
                if receipt is not None:
                    self.conn.commit(); return receipt
                auction=self._ensure_ai_auction_tx(snapshot,now)
                if auction is None or str(auction["state"])!="active" or \
                        int(auction["expires"])<=now:
                    self.conn.rollback(); return None
                minimum=self._minimum_market_bid(
                    auction["current_bid"],auction["starting_bid"])
                if bid < minimum or bid >= int(auction["buy_now"]):
                    self.conn.rollback(); return None
                target=self.conn.execute(
                    "SELECT * FROM market_targets WHERE trade_id=?",
                    (trade_id,)).fetchone()
                reserved=int(target["reserved_credits"]) if target else 0
                delta=max(0,bid-reserved)
                credits=self._market_credits_tx(self.conn)
                if credits < delta:
                    self.conn.rollback(); return None
                self._market_set_credits_tx(self.conn,credits-delta)
                outcome=deterministic_bid_outcome(
                    trade_id,bid,now,int(auction["expires"]))
                data={"watched":True,"bidAmount":bid,**outcome}
                created=int(target["created"]) if target else now
                self.conn.execute(
                    "INSERT INTO market_targets(trade_id,status,reserved_credits,created,updated,data) "
                    "VALUES(?,'HIGHEST',?,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
                    "status='HIGHEST',reserved_credits=excluded.reserved_credits,"
                    "updated=excluded.updated,data=excluded.data",
                    (trade_id,bid,created,now,json.dumps(data)))
                auction_data=json.loads(auction["data"] or "{}")
                auction_data.update({"offers":int(auction_data.get("offers",0))+1,
                                     "bidState":"highest","watched":True})
                self.conn.execute(
                    "UPDATE auctions SET current_bid=?,data=? WHERE trade_id=?",
                    (bid,json.dumps(auction_data),trade_id))
                result={"tradeId":trade_id,"status":"HIGHEST",
                        "bidState":"highest","currentBid":bid,
                        "reservedCredits":bid,"credits":credits-delta}
                self._store_market_receipt_tx(
                    self.conn,operation,"BID",trade_id,result,now)
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def buy_now_market_listing(self, snapshot, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(snapshot.get("tradeId",0) or 0)
        operation="market-buy-now:%d"%trade_id
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt=self._market_receipt_tx(self.conn,operation)
                if receipt is not None:
                    self.conn.commit(); return receipt
                auction=self._ensure_ai_auction_tx(snapshot,now)
                if auction is None or bool(auction["seller_is_user"]) or \
                        str(auction["state"])!="active" or int(auction["expires"])<=now:
                    self.conn.rollback(); return None
                target=self.conn.execute(
                    "SELECT * FROM market_targets WHERE trade_id=?",
                    (trade_id,)).fetchone()
                reserved=int(target["reserved_credits"]) if target else 0
                price=int(auction["buy_now"]); due=max(0,price-reserved)
                credits=self._market_credits_tx(self.conn)
                if credits < due:
                    self.conn.rollback(); return None
                new_credits=self._market_set_credits_tx(self.conn,credits-due)
                item=self._grant_market_item_tx(
                    json.loads(auction["item_payload"] or "{}"),trade_id,now,
                    "BUY_NOW")
                target_data=json.loads(target["data"] or "{}") if target else {}
                target_data.update({"watched":True,"bidAmount":price,
                                    "itemId":int(item["id"]),"wonAt":now})
                created=int(target["created"]) if target else now
                self.conn.execute(
                    "INSERT INTO market_targets(trade_id,status,reserved_credits,created,updated,data) "
                    "VALUES(?,'WON',0,?,?,?) ON CONFLICT(trade_id) DO UPDATE SET "
                    "status='WON',reserved_credits=0,updated=excluded.updated,data=excluded.data",
                    (trade_id,created,now,json.dumps(target_data)))
                auction_data=json.loads(auction["data"] or "{}")
                auction_data.update({"bidState":"highest","watched":True,
                                     "buyerIsUser":True,"soldAt":now,"offers":1})
                self.conn.execute(
                    "UPDATE auctions SET state='sold',current_bid=?,data=? WHERE trade_id=?",
                    (price,json.dumps(auction_data),trade_id))
                # The objective is explicitly player-scoped. Buying a badge,
                # kit, manager or consumable must not satisfy it merely because
                # the auction lifecycle is shared.
                if stored_item_kind(item)=="player":
                    self._market_increment_tx(
                        self.conn,"objective_players_bought",1)
                result={"tradeId":trade_id,"status":"WON","bidState":"highest",
                        "currentBid":price,"buyNowPrice":price,
                        "item":item,"itemId":int(item["id"]),"credits":new_credits}
                self._store_market_receipt_tx(
                    self.conn,operation,"BUY_NOW",trade_id,result,now)
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def market_targets(self, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self._clear_stale_won_market_targets_tx(now)
                rows=self.conn.execute(
                    "SELECT * FROM market_targets WHERE status!='CLEARED' "
                    "ORDER BY trade_id").fetchall()
                result=[]
                for target in rows:
                    auction=self.market_auction(target["trade_id"],now)
                    if not auction: continue
                    data=json.loads(target["data"] or "{}")
                    auction.update({"targetStatus":str(target["status"]),
                                    "reservedCredits":int(target["reserved_credits"]),
                                    "watched":True,**data})
                    if str(target["status"])=="HIGHEST":
                        auction["bidState"]="highest"
                    elif str(target["status"])=="OUTBID":
                        auction["bidState"]="outbid"
                    elif str(target["status"])=="WON":
                        auction["bidState"]="highest"
                    result.append(auction)
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def _clear_stale_won_market_targets_tx(self, timestamp, item_id=None):
        """Clear won targets whose purchased item has already been handled."""
        rows=self.conn.execute(
            "SELECT trade_id,data FROM market_targets WHERE status='WON' "
            "ORDER BY trade_id").fetchall()
        cleared=[]
        wanted=None if item_id is None else int(item_id)
        for target in rows:
            try:
                data=json.loads(target["data"] or "{}")
                linked_item_id=int(data.get("itemId",0) or 0)
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
            if linked_item_id <= 0 or (wanted is not None and
                                      linked_item_id != wanted):
                continue
            item=self.conn.execute(
                "SELECT pile FROM items WHERE id=?",(linked_item_id,)).fetchone()
            if item is not None and str(item["pile"]) == "purchased":
                continue
            self.conn.execute(
                "UPDATE market_targets SET status='CLEARED',updated=? "
                "WHERE trade_id=? AND status='WON'",
                (int(timestamp),int(target["trade_id"])))
            cleared.append(int(target["trade_id"]))
        return cleared

    def settle_market(self, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        changed=[]
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                user_rows=self.conn.execute(
                    "SELECT * FROM auctions WHERE seller_is_user=1 AND state='active' "
                    "ORDER BY trade_id").fetchall()
                for row in user_rows:
                    trade_id=int(row["trade_id"])
                    data=json.loads(row["data"] or "{}")
                    # Repair listings created by the older buyer scheduler.
                    # Guaranteed below-fair sales used to carry delays as long
                    # as 30 minutes; recompute their deterministic outcome and
                    # persist the earlier timestamp before settlement.
                    try:
                        from fut_market import deterministic_user_sale
                        item_payload=json.loads(row["item_payload"] or "{}")
                        duration=max(60,int(row["expires"])-int(row["created"]))
                        repaired=deterministic_user_sale(
                            item_payload,int(row["buy_now"]),duration,
                            trade_id,int(row["created"]))
                        repaired_at=repaired.get("saleAt")
                        old_at=data.get("saleAt")
                        if (float(repaired.get("priceRatio",2.0))<=1.0 and
                                repaired_at is not None and
                                (old_at is None or int(old_at)>int(repaired_at))):
                            data.update(repaired)
                            self.conn.execute(
                                "UPDATE auctions SET data=? WHERE trade_id=?",
                                (json.dumps(data),trade_id))
                    except (TypeError,ValueError,json.JSONDecodeError):
                        # Malformed legacy metadata still follows the ordinary
                        # expiry path; never make repair block the trade pile.
                        pass
                    sale_at=data.get("saleAt")
                    if sale_at is not None and int(sale_at)<=now:
                        operation="market-seller-credit:%d"%trade_id
                        if self._market_receipt_tx(self.conn,operation) is None:
                            credits=self._market_credits_tx(self.conn)
                            credits=self._market_set_credits_tx(
                                self.conn,credits+int(row["buy_now"]))
                            self.conn.execute("DELETE FROM items WHERE id=?",
                                              (int(row["item_id"]),))
                            data.update({"soldAt":now,"bidState":"highest",
                                         "offers":1,"buyerName":"Market AI"})
                            self.conn.execute(
                                "UPDATE auctions SET state='sold',current_bid=?,data=? "
                                "WHERE trade_id=?",
                                (int(row["buy_now"]),json.dumps(data),trade_id))
                            receipt={"tradeId":trade_id,"state":"sold",
                                     "credited":int(row["buy_now"]),"credits":credits}
                            self._store_market_receipt_tx(
                                self.conn,operation,"SELLER_CREDIT",trade_id,receipt,now)
                            self._market_increment_tx(
                                self.conn,"objective_market_sales",1)
                            changed.append(receipt)
                    elif int(row["expires"])<=now:
                        operation="market-expire:%d:%d"%(
                            trade_id,int(row["expires"]))
                        if self._market_receipt_tx(self.conn,operation) is None:
                            item_row=self.conn.execute(
                                "SELECT * FROM items WHERE id=?",(int(row["item_id"]),)).fetchone()
                            if item_row is not None:
                                item=self._market_item_from_row(item_row); item["pile"]="club"
                                self.conn.execute(
                                    "UPDATE items SET pile='club',data=? WHERE id=?",
                                    (json.dumps(item),int(row["item_id"])))
                            data.update({"expiredAt":now,"bidState":"none"})
                            self.conn.execute(
                                "UPDATE auctions SET state='expired',data=? WHERE trade_id=?",
                                (json.dumps(data),trade_id))
                            receipt={"tradeId":trade_id,"state":"expired",
                                     "itemId":int(row["item_id"])}
                            self._store_market_receipt_tx(
                                self.conn,operation,"EXPIRE",trade_id,receipt,now)
                            changed.append(receipt)

                targets=self.conn.execute(
                    "SELECT * FROM market_targets WHERE status IN ('WATCHED','HIGHEST') "
                    "ORDER BY trade_id").fetchall()
                for target in targets:
                    trade_id=int(target["trade_id"])
                    auction=self.conn.execute(
                        "SELECT * FROM auctions WHERE trade_id=?",(trade_id,)).fetchone()
                    reserved=int(target["reserved_credits"])
                    target_data=json.loads(target["data"] or "{}")
                    if auction is None:
                        if reserved:
                            self._market_set_credits_tx(
                                self.conn,self._market_credits_tx(self.conn)+reserved)
                        self.conn.execute(
                            "UPDATE market_targets SET status='EXPIRED',reserved_credits=0,"
                            "updated=? WHERE trade_id=?",(now,trade_id))
                        continue
                    expired=int(auction["expires"])<=now
                    if str(target["status"])=="WATCHED" and expired:
                        self.conn.execute(
                            "UPDATE auctions SET state='expired' WHERE trade_id=?",
                            (trade_id,))
                        self.conn.execute(
                            "UPDATE market_targets SET status='EXPIRED',updated=? WHERE trade_id=?",
                            (now,trade_id))
                        changed.append({"tradeId":trade_id,"state":"expired"})
                        continue
                    if str(target["status"])!="HIGHEST":
                        continue
                    outcome_at=int(target_data.get("outcomeAt",auction["expires"]) or
                                   auction["expires"])
                    if not expired and outcome_at>now:
                        continue
                    outcome="WON" if expired else str(target_data.get("outcome","OUTBID"))
                    operation="market-bid-settle:%d:%d"%(trade_id,reserved)
                    if self._market_receipt_tx(self.conn,operation) is not None:
                        continue
                    if outcome=="OUTBID":
                        credits=self._market_set_credits_tx(
                            self.conn,self._market_credits_tx(self.conn)+reserved)
                        target_data.update({"outbidAt":now})
                        self.conn.execute(
                            "UPDATE market_targets SET status='OUTBID',reserved_credits=0,"
                            "updated=?,data=? WHERE trade_id=?",
                            (now,json.dumps(target_data),trade_id))
                        auction_data=json.loads(auction["data"] or "{}")
                        auction_data.update({"bidState":"outbid","watched":True})
                        self.conn.execute(
                            "UPDATE auctions SET current_bid=?,data=? WHERE trade_id=?",
                            (self._minimum_market_bid(reserved,reserved),
                             json.dumps(auction_data),trade_id))
                        receipt={"tradeId":trade_id,"state":"outbid",
                                 "refunded":reserved,"credits":credits}
                    else:
                        item=self._grant_market_item_tx(
                            json.loads(auction["item_payload"] or "{}"),trade_id,now,
                            "BID_WIN")
                        target_data.update({"wonAt":now,"itemId":int(item["id"])})
                        self.conn.execute(
                            "UPDATE market_targets SET status='WON',reserved_credits=0,"
                            "updated=?,data=? WHERE trade_id=?",
                            (now,json.dumps(target_data),trade_id))
                        auction_data=json.loads(auction["data"] or "{}")
                        auction_data.update({"bidState":"highest","watched":True,
                                             "buyerIsUser":True,"soldAt":now})
                        self.conn.execute(
                            "UPDATE auctions SET state='sold',data=? WHERE trade_id=?",
                            (json.dumps(auction_data),trade_id))
                        if stored_item_kind(item)=="player":
                            self._market_increment_tx(
                                self.conn,"objective_players_bought",1)
                        receipt={"tradeId":trade_id,"state":"won",
                                 "itemId":int(item["id"]),"item":item,
                                 "credits":self._market_credits_tx(self.conn)}
                    self._store_market_receipt_tx(
                        self.conn,operation,"BID_SETTLE",trade_id,receipt,now)
                    changed.append(receipt)
                self.conn.commit(); return changed
            except Exception:
                self.conn.rollback(); raise

    def clear_sold_market_trades(self, timestamp=None):
        """Atomically remove every completed user sale from Transfer List."""
        now=now_s() if timestamp is None else int(timestamp)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                rows=self.conn.execute(
                    "SELECT trade_id FROM auctions WHERE seller_is_user=1 "
                    "AND state='sold' ORDER BY trade_id").fetchall()
                trade_ids=[int(row["trade_id"]) for row in rows]
                if trade_ids:
                    self.conn.execute(
                        "UPDATE auctions SET state='cleared' WHERE "
                        "seller_is_user=1 AND state='sold'")
                self.conn.commit()
                return {"cleared":len(trade_ids),"tradeIds":trade_ids,
                        "timestamp":now}
            except Exception:
                self.conn.rollback(); raise

    def clear_market_trade(self, trade_id, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(trade_id)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                auction=self.conn.execute("SELECT * FROM auctions WHERE trade_id=?",
                                          (trade_id,)).fetchone()
                target=self.conn.execute("SELECT * FROM market_targets WHERE trade_id=?",
                                         (trade_id,)).fetchone()
                changed=False
                if target is not None and str(target["status"]) in \
                        ("WATCHED","OUTBID","WON","EXPIRED") and \
                        int(target["reserved_credits"])==0:
                    self.conn.execute(
                        "UPDATE market_targets SET status='CLEARED',updated=? "
                        "WHERE trade_id=?",(now,trade_id))
                    changed=True
                elif target is not None and str(target["status"])=="HIGHEST":
                    self.conn.rollback(); return None
                if auction is not None and bool(auction["seller_is_user"]):
                    if str(auction["state"]) not in ("sold","expired","cleared"):
                        self.conn.rollback(); return None
                    if str(auction["state"])!="cleared":
                        self.conn.execute(
                            "UPDATE auctions SET state='cleared' WHERE trade_id=?",
                            (trade_id,))
                    changed=True
                elif target is not None and str(target["status"])=="CLEARED":
                    # Clearing is naturally idempotent.  Avoid a permanent
                    # trade-id-only receipt: the same deterministic listing
                    # may be watched again and must then be clearable again.
                    changed=True
                elif auction is None and target is None:
                    self.conn.rollback(); return None
                if not changed:
                    self.conn.rollback(); return None
                result={"tradeId":trade_id,"cleared":True}
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def unwatch_market_trade(self, trade_id, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(trade_id)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                target=self.conn.execute("SELECT * FROM market_targets WHERE trade_id=?",
                                         (trade_id,)).fetchone()
                if target is None:
                    self.conn.rollback(); return None
                if str(target["status"])=="HIGHEST" and \
                        int(target["reserved_credits"])>0:
                    self.conn.rollback(); return None
                self.conn.execute(
                    "UPDATE market_targets SET status='CLEARED',updated=? WHERE trade_id=?",
                    (now,trade_id))
                result={"tradeId":trade_id,"watched":False}
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def relist_market_trade(self, trade_id, timestamp=None):
        now=now_s() if timestamp is None else int(timestamp)
        trade_id=int(trade_id)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute("SELECT * FROM auctions WHERE trade_id=?",
                                      (trade_id,)).fetchone()
                if row is None or not bool(row["seller_is_user"]) or \
                        str(row["state"])!="expired":
                    self.conn.rollback(); return None
                operation="market-relist:%d:%d"%(trade_id,int(row["expires"]))
                receipt=self._market_receipt_tx(self.conn,operation)
                if receipt is not None:
                    self.conn.commit(); return receipt
                item_row=self.conn.execute("SELECT * FROM items WHERE id=?",
                                           (int(row["item_id"]),)).fetchone()
                if item_row is None or str(item_row["pile"])!="club":
                    self.conn.rollback(); return None
                item=self._market_item_from_row(item_row); item["pile"]="trade"
                self.conn.execute("UPDATE items SET pile='trade',data=? WHERE id=?",
                                  (json.dumps(item),int(row["item_id"])))
                duration=max(60,int(row["expires"])-int(row["created"]))
                data=json.loads(row["data"] or "{}")
                delay=data.get("saleDelay")
                data.update({"offers":0,"bidState":"none","relistedAt":now,
                             "saleAt":now+int(delay) if delay is not None else None})
                self.conn.execute(
                    "UPDATE auctions SET state='active',current_bid=0,created=?,expires=?,"
                    "data=?,item_payload=? WHERE trade_id=?",
                    (now,now+duration,json.dumps(data),json.dumps(item),trade_id))
                self._market_increment_tx(self.conn,"objective_transfer_listings",1)
                result={"tradeId":trade_id,"relisted":True,
                        "expires":duration,"itemId":int(row["item_id"])}
                self._store_market_receipt_tx(
                    self.conn,operation,"RELIST",trade_id,result,now)
                self.conn.commit(); return result
            except Exception:
                self.conn.rollback(); raise

    def market_counts(self):
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                # Repair old databases even when the Hub count is requested
                # before the Transfer Targets screen itself is opened.
                self._clear_stale_won_market_targets_tx(now_s())
                listed=self.conn.execute(
                    "SELECT COUNT(*) AS n FROM auctions WHERE seller_is_user=1 "
                    "AND state!='cleared'"
                ).fetchone()["n"]
                unlisted=self.conn.execute(
                    "SELECT COUNT(*) AS n FROM items i WHERE i.pile='trade' AND NOT "
                    "EXISTS (SELECT 1 FROM auctions a WHERE a.seller_is_user=1 AND "
                    "a.state!='cleared' AND a.item_id=i.id)"
                ).fetchone()["n"]
                active=self.conn.execute(
                    "SELECT COUNT(*) AS n FROM auctions WHERE state='active'"
                ).fetchone()["n"]
                watch=self.conn.execute(
                    "SELECT COUNT(*) AS n FROM market_targets WHERE status!='CLEARED'"
                ).fetchone()["n"]
                self.conn.commit()
                return {"tradePile":int(listed)+int(unlisted),
                        "watchList":int(watch),"active":int(active)}
            except Exception:
                self.conn.rollback(); raise

    # ---------- offline Draft ----------
    @staticmethod
    def _draft_dict(row):
        if row is None:
            return None
        result=dict(row)
        result["data"]=json.loads(result.get("data") or "{}")
        result.update(result["data"])
        return result

    @staticmethod
    def _normalize_draft_swap_player_def_ids(values):
        if not isinstance(values,(list,tuple)):
            return None
        normalized=[]
        for value in values:
            try:
                item=int(value or 0)
            except (TypeError,ValueError):
                return None
            if item<0:
                return None
            normalized.append(item)
        return normalized if len(normalized)==23 else None

    def draft_tokens(self):
        return max(0,int(self.get("draft_tokens",0) or 0))

    def add_draft_tokens(self, amount, external=False):
        """Atomically add Draft Tokens without touching club state."""
        if external and not self.external_coin_adjustment_allowed():
            raise PermissionError(
                "RTG mode blocks external Draft Token additions")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='draft_tokens'").fetchone()
                current=max(0,int(json.loads(row["value"]))) if row else 0
                balance=max(0,current+int(amount))
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('draft_tokens',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(balance),))
                self.conn.commit()
                return balance
            except Exception:
                self.conn.rollback()
                raise

    def draft_session(self, session_id):
        row=self.conn.execute(
            "SELECT * FROM draft_sessions WHERE id=?",(int(session_id),)).fetchone()
        return self._draft_dict(row)

    def current_draft(self, mode="SINGLE_PLAYER"):
        row=self.conn.execute(
            "SELECT * FROM draft_sessions WHERE mode=? AND NOT "
            "(state='COMPLETED_DRAFT' AND reward_claimed=1) "
            "ORDER BY id DESC LIMIT 1",(str(mode).upper(),)).fetchone()
        return self._draft_dict(row)

    def discard_active_draft(self, mode="SINGLE_PLAYER"):
        """Delete the unfinished Draft of one mode and refund its entry.

        A Draft cannot be left from inside the game without playing it out, so
        retesting the entry flow otherwise costs a full four-match run. A
        session whose reward has already been claimed is history and is left
        alone. Returns the discarded session, or None when there was none.
        """
        normalized=str(mode or "SINGLE_PLAYER").upper()
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE mode=? AND NOT "
                    "(state='COMPLETED_DRAFT' AND reward_claimed=1) "
                    "ORDER BY id DESC LIMIT 1",(normalized,)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                session_id=int(row["id"])
                discarded=self._draft_dict(row)
                self.conn.execute("DELETE FROM draft_picks WHERE session_id=?",
                                  (session_id,))
                self.conn.execute("DELETE FROM draft_matches WHERE session_id=?",
                                  (session_id,))
                self.conn.execute("DELETE FROM draft_sessions WHERE id=?",
                                  (session_id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
        currency=str(discarded.get("entry_currency","") or "").upper()
        cost=max(0,int(discarded.get("entry_cost",0) or 0))
        if currency=="COINS" and cost:
            self.set_credits(self.credits()+cost)
        elif currency=="DRAFT_TOKEN":
            self.add_draft_tokens(1)
        # An active-match owner still pointing at the removed session would
        # book the next unrelated result against it.
        for key in ("active_draft_match","active_offline_match_owner"):
            owner=self.get(key,{})
            if (isinstance(owner,dict) and
                    int(owner.get("sessionId",0) or 0)==session_id):
                self.set(key,{})
        return discarded

    def latest_draft(self, mode="SINGLE_PLAYER"):
        """Return the newest session, including a completed claimed Draft.

        Retail route token ``1`` identifies Single Player mode rather than a
        persisted session id.  Award/stats retries can arrive after the claim
        has removed that session from ``current_draft``; they must still bind
        to the same durable receipt instead of falling through to session 1.
        """
        row=self.conn.execute(
            "SELECT * FROM draft_sessions WHERE mode=? "
            "ORDER BY id DESC LIMIT 1",(str(mode).upper(),)).fetchone()
        return self._draft_dict(row)

    def draft_history(self, mode="SINGLE_PLAYER"):
        """Return durable all-time Draft totals for the native history panel.

        A Draft entry exists as soon as its session is created.  Match results
        are authoritative in ``draft_matches``; summing that table avoids
        losing completed sessions after their rewards have been claimed while
        also excluding the empty legacy session counters created by old
        builds.  A championship is one session with four recorded wins.
        """
        normalized_mode=str(mode or "SINGLE_PLAYER").upper()
        row=self.conn.execute(
            "SELECT COUNT(*) AS entries,"
            "COALESCE(SUM(CASE WHEN wins>=4 THEN 1 ELSE 0 END),0) AS champions "
            "FROM draft_sessions WHERE mode=?",(normalized_mode,)).fetchone()
        match_rows=self.conn.execute(
            "SELECT m.result,m.home_goals,m.away_goals,m.data "
            "FROM draft_matches m JOIN draft_sessions s ON s.id=m.session_id "
            "WHERE s.mode=? ORDER BY m.created,m.session_id,m.round",
            (normalized_mode,)).fetchall()
        wins=losses=goals_scored=goals_conceded=0
        pass_accuracy_total=possession_total=best_builder_score=0
        stats_matches=0
        for match in match_rows:
            if str(match["result"]).upper()=="WIN": wins+=1
            else: losses+=1
            goals_scored+=int(match["home_goals"] or 0)
            goals_conceded+=int(match["away_goals"] or 0)
            try: data=json.loads(match["data"] or "{}")
            except (TypeError,ValueError,json.JSONDecodeError): data={}
            has_stats=False
            if data.get("passAccuracy") is not None:
                pass_accuracy_total+=max(0,min(100,int(
                    data.get("passAccuracy",0) or 0)))
                has_stats=True
            if data.get("possession") is not None:
                possession_total+=max(0,min(100,int(
                    data.get("possession",0) or 0)))
                has_stats=True
            if has_stats: stats_matches+=1
            best_builder_score=max(best_builder_score,max(0,int(
                data.get("builderScore",0) or 0)))
        return {
            "draftsCompleted":int(row["entries"] or 0),
            "draftChampion":int(row["champions"] or 0),
            "wins":wins,"losses":losses,
            "goalsScored":goals_scored,"concededGoals":goals_conceded,
            "passAccuracyTotal":pass_accuracy_total,
            "possessionTotal":possession_total,
            "bestBuilderScore":best_builder_score,
            "statsMatches":stats_matches,
        }

    def start_draft(self, mode="SINGLE_PLAYER", currency="DRAFT_TOKEN",
                    coin_cost=15000, rng_seed=0):
        """Atomically charge one entry and create at most one live session."""
        normalized_mode=str(mode or "SINGLE_PLAYER").upper()
        normalized_currency=str(currency or "DRAFT_TOKEN").upper()
        if normalized_currency not in ("DRAFT_TOKEN","COINS"):
            normalized_currency="DRAFT_TOKEN"
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE mode=? AND NOT "
                    "(state='COMPLETED_DRAFT' AND reward_claimed=1) "
                    "ORDER BY id DESC LIMIT 1",(normalized_mode,)).fetchone()
                if existing is not None:
                    self.conn.commit()
                    result=self._draft_dict(existing); result["createdNow"]=False
                    return result
                if normalized_currency == "DRAFT_TOKEN":
                    row=self.conn.execute(
                        "SELECT value FROM kv WHERE key='draft_tokens'").fetchone()
                    balance=int(json.loads(row["value"])) if row else 0
                    if balance < 1:
                        self.conn.rollback(); return None
                    new_balance=balance-1; entry_cost=1
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES('draft_tokens',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(new_balance),))
                else:
                    row=self.conn.execute(
                        "SELECT value FROM kv WHERE key='credits'").fetchone()
                    balance=int(json.loads(row["value"])) if row else 0
                    entry_cost=max(0,int(coin_cost))
                    if balance < entry_cost:
                        self.conn.rollback(); return None
                    new_balance=balance-entry_cost
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES('credits',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(new_balance),))
                timestamp=now_s()
                cur=self.conn.execute(
                    "INSERT INTO draft_sessions(mode,state,entry_currency,"
                    "entry_cost,rng_seed,created,updated,data) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (normalized_mode,"PICK_DIFFICULTY",normalized_currency,entry_cost,
                     int(rng_seed),timestamp,timestamp,json.dumps({
                         "maxWins":4,"draftChampion":False,
                         "difficultySelectionVersion":2})))
                session_id=int(cur.lastrowid)
                self.conn.commit()
                result=self.draft_session(session_id); result["createdNow"]=True
                return result
            except Exception:
                self.conn.rollback(); raise

    def ensure_draft_pick(self, session_id, choice_type, slot, choices,
                          data=None):
        """Persist generated choices once so restarts cannot reroll them."""
        normalized=str(choice_type).upper()
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO draft_picks(session_id,choice_type,slot,"
                "choices_json,data) VALUES(?,?,?,?,?)",
                (int(session_id),normalized,int(slot),json.dumps(list(choices)),
                 json.dumps(data or {})))
            self.conn.commit()
        row=self.conn.execute(
            "SELECT * FROM draft_picks WHERE session_id=? AND choice_type=? "
            "AND slot=?",(int(session_id),normalized,int(slot))).fetchone()
        if row is None: return None
        return {**dict(row),"choices":json.loads(row["choices_json"]),
                "data":json.loads(row["data"] or "{}")}

    def replace_unselected_draft_pick(self, session_id, choice_type, slot,
                                      choices, data=None):
        """Replace a position-agnostic row without rerolling a selection.

        Draft player rows created by older builds were position-agnostic.  A
        source-backed role row may replace them only while unselected; once a
        user has chosen, the persisted five-card roll is immutable.
        """
        normalized=str(choice_type).upper()
        with self.lock:
            self.conn.execute(
                "UPDATE draft_picks SET choices_json=?,data=? WHERE "
                "session_id=? AND choice_type=? AND slot=? AND selected_value=''",
                (json.dumps(list(choices)),json.dumps(data or {}),
                 int(session_id),normalized,int(slot)))
            self.conn.commit()
        row=self.conn.execute(
            "SELECT * FROM draft_picks WHERE session_id=? AND choice_type=? "
            "AND slot=?",(int(session_id),normalized,int(slot))).fetchone()
        if row is None: return None
        return {**dict(row),"choices":json.loads(row["choices_json"]),
                "data":json.loads(row["data"] or "{}")}

    def draft_picks(self, session_id):
        rows=self.conn.execute(
            "SELECT * FROM draft_picks WHERE session_id=? "
            "ORDER BY choice_type,slot",(int(session_id),)).fetchall()
        return [{**dict(row),"choices":json.loads(row["choices_json"]),
                 "data":json.loads(row["data"] or "{}")} for row in rows]

    @staticmethod
    def _draft_choice_values(choice):
        if not isinstance(choice,dict):
            return {str(choice)}
        keys=("resourceId","definitionId","assetId","id","itemId",
              "formation","formationId","difficulty","value","choiceIndex")
        return {str(choice[key]) for key in keys
                if key in choice and choice[key] not in (None,"")}

    @staticmethod
    def _draft_progress_state(counts):
        """Return the retail client's next native Draft state."""
        if int(counts.get("PICK_DIFFICULTY",0)) < 1:
            return "PICK_DIFFICULTY"
        if int(counts.get("FORMATION_DRAFT",0)) < 1:
            return "FORMATION_DRAFT"
        if int(counts.get("CAPTAIN_DRAFT",0)) < 1:
            return "CAPTAIN_DRAFT"
        if int(counts.get("PLAYER_DRAFT",0)) < 22:
            return "PLAYER_DRAFT"
        if int(counts.get("MANAGER_DRAFT",0)) < 1:
            return "MANAGER_DRAFT"
        return "READY_FOR_MATCH"

    def reconcile_draft_state(self, session_id):
        """Safely migrate old DRAFTING rows and resume partial drafts."""
        with self.lock:
            row=self.conn.execute(
                "SELECT * FROM draft_sessions WHERE id=?",
                (int(session_id),)).fetchone()
            if row is None:
                return None
            if str(row["state"]) in ("READY_FOR_REWARDS","COMPLETED_DRAFT"):
                return self._draft_dict(row)
            selected_rows=self.conn.execute(
                "SELECT choice_type,COUNT(*) AS count FROM draft_picks "
                "WHERE session_id=? AND selected_value<>'' GROUP BY choice_type",
                (int(session_id),)).fetchall()
            counts={str(value["choice_type"]):int(value["count"])
                    for value in selected_rows}
            data=json.loads(row["data"] or "{}")
            legacy_complete=(
                int(data.get("difficultySelectionVersion",0) or 0)<2 and
                int(counts.get("FORMATION_DRAFT",0))>=1 and
                int(counts.get("CAPTAIN_DRAFT",0))>=1 and
                int(counts.get("PLAYER_DRAFT",0))>=22 and
                int(counts.get("MANAGER_DRAFT",0))>=1)
            if legacy_complete:
                # Before difficulty became a real seven-level Draft step the
                # offline match launcher always used Ultimate.  Preserve a
                # fully built legacy squad as READY_FOR_MATCH instead of
                # reopening onboarding and consuming its first formation PUT
                # as a fake difficulty selection.
                choices=[{"index":value-1,"choiceIndex":value-1,
                          "difficulty":value,"value":value,
                          "difficultyName":name,
                          "rewardMultiplier":multiplier}
                         for value,name,multiplier in (
                    (1,"BEGINNER",0.5),(2,"AMATEUR",0.65),
                    (3,"SEMIPRO",0.8),(4,"PROFESSIONAL",1.0),
                    (5,"WORLDCLASS",1.25),(6,"LEGENDARY",1.5),
                    (7,"ULTIMATE",1.75))]
                self.conn.execute(
                    "INSERT INTO draft_picks(session_id,choice_type,slot,"
                    "choices_json,selected_value,data) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(session_id,choice_type,slot) DO UPDATE SET "
                    "choices_json=excluded.choices_json,selected_value='7',"
                    "data=excluded.data",
                    (int(session_id),"PICK_DIFFICULTY",0,json.dumps(choices),
                     "7",json.dumps({"generationVersion":2,
                                      "legacyPreserved":True})))
                data["difficultySelectionVersion"]=2
                data["legacyDifficultyMigrated"]="ULTIMATE"
                self.conn.execute(
                    "UPDATE draft_sessions SET state='READY_FOR_MATCH',"
                    "difficulty=7,data=?,updated=? WHERE id=?",
                    (json.dumps(data),now_s(),int(session_id)))
                self.conn.commit()
                return self.draft_session(session_id)
            state=self._draft_progress_state(counts)
            if state != str(row["state"]):
                self.conn.execute(
                    "UPDATE draft_sessions SET state=?,updated=? WHERE id=?",
                    (state,now_s(),int(session_id)))
                self.conn.commit()
            return self.draft_session(session_id)

    def save_draft_squad_layout(self, session_id, payload):
        """Persist the user's Draft slot switches without creating a My Squad."""
        players=[]
        for raw in payload.get("players",[]):
            if not isinstance(raw,dict):
                continue
            item=raw.get("itemData") or {}
            try:
                index=int(raw.get("index",-1)); item_id=int(item.get("id",0) or 0)
                kit_number=int(raw.get("kitNumber",0) or 0)
            except (TypeError,ValueError):
                continue
            if 0<=index<=22 and item_id:
                players.append({"index":index,"id":item_id,
                                "kitNumber":kit_number})
        players.sort(key=lambda value:value["index"])
        if len(players)!=23 or len({row["index"] for row in players})!=23:
            return None
        with self.lock:
            row=self.conn.execute(
                "SELECT data FROM draft_sessions WHERE id=?",
                (int(session_id),)).fetchone()
            if row is None:
                return None
            data=json.loads(row["data"] or "{}")
            data["squadLayout"]=players
            self.conn.execute(
                "UPDATE draft_sessions SET data=?,updated=? WHERE id=?",
                (json.dumps(data),now_s(),int(session_id)))
            self.conn.commit()
        return self.draft_session(session_id)

    def choose_draft_pick(self, session_id, choice_type, slot, selected_value,
                          position_id=None, swap_player_def_ids=None):
        normalized=str(choice_type).upper(); selected=str(selected_value)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM draft_picks WHERE session_id=? AND "
                    "choice_type=? AND slot=?",
                    (int(session_id),normalized,int(slot))).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                choices=json.loads(row["choices_json"])
                chosen=next((choice for choice in choices
                    if selected in self._draft_choice_values(choice)),None)
                if chosen is None:
                    self.conn.rollback(); return None
                previous=str(row["selected_value"] or "")
                if previous and previous != selected:
                    self.conn.rollback(); return None
                self.conn.execute(
                    "UPDATE draft_picks SET selected_value=? WHERE session_id=? "
                    "AND choice_type=? AND slot=?",
                    (selected,int(session_id),normalized,int(slot)))
                updates=[]; params=[]
                if normalized == "FORMATION_DRAFT":
                    formation=(chosen.get("formation",selected)
                               if isinstance(chosen,dict) else selected)
                    updates.append("formation=?"); params.append(str(formation))
                elif normalized == "CAPTAIN_DRAFT":
                    resource=(chosen.get("resourceId",selected)
                              if isinstance(chosen,dict) else selected)
                    updates.append("captain_resource_id=?"); params.append(int(resource))
                elif normalized == "PICK_DIFFICULTY":
                    difficulty=(chosen.get("difficulty",chosen.get("value",selected))
                                if isinstance(chosen,dict) else selected)
                    updates.append("difficulty=?"); params.append(int(difficulty))
                selected_rows=self.conn.execute(
                    "SELECT choice_type,COUNT(*) AS count FROM draft_picks "
                    "WHERE session_id=? AND selected_value<>'' GROUP BY choice_type",
                    (int(session_id),)).fetchall()
                counts={str(value["choice_type"]):int(value["count"])
                        for value in selected_rows}
                current=self.conn.execute(
                    "SELECT state,data FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                session_data=None
                if normalized == "CAPTAIN_DRAFT" and position_id is not None:
                    session_data=(json.loads(current["data"] or "{}")
                                  if current is not None else {})
                    session_data["captainPositionId"]=int(position_id)
                partial_layout=self._normalize_draft_swap_player_def_ids(
                    swap_player_def_ids)
                if partial_layout is not None:
                    if session_data is None:
                        session_data=(json.loads(current["data"] or "{}")
                                      if current is not None else {})
                    session_data["swapPlayerDefIds"]=partial_layout
                if session_data is not None:
                    updates.append("data=?"); params.append(json.dumps(session_data))
                if current is not None and str(current["state"]) not in (
                        "READY_FOR_REWARDS","COMPLETED_DRAFT"):
                    updates.append("state=?")
                    params.append(self._draft_progress_state(counts))
                updates.append("updated=?"); params.append(now_s())
                params.append(int(session_id))
                self.conn.execute(
                    "UPDATE draft_sessions SET %s WHERE id=?" % ",".join(updates),
                    params)
                self.conn.commit()
                return chosen
            except Exception:
                self.conn.rollback(); raise

    def autocomplete_draft(self, session_id):
        rows=self.draft_picks(session_id)
        for row in rows:
            if row.get("selected_value") or not row["choices"]:
                continue
            choice=row["choices"][0]
            values=self._draft_choice_values(choice)
            preferred=(choice.get("resourceId",choice.get("formation",
                       choice.get("difficulty",choice.get("id"))))
                       if isinstance(choice,dict) else choice)
            selected=str(preferred if preferred not in (None,"") else sorted(values)[0])
            self.choose_draft_pick(session_id,row["choice_type"],row["slot"],selected)
        return self.draft_session(session_id)

    def draft_selected(self, session_id):
        result=[]
        for row in self.draft_picks(session_id):
            selected=str(row.get("selected_value") or "")
            if not selected: continue
            choice=next((value for value in row["choices"]
                if selected in self._draft_choice_values(value)),None)
            result.append({"choiceType":row["choice_type"],"slot":row["slot"],
                           "selectedValue":selected,"choice":choice})
        return result

    def draft_matches(self, session_id):
        rows=self.conn.execute(
            "SELECT round,result,home_goals,away_goals,created,data "
            "FROM draft_matches WHERE session_id=? ORDER BY round",
            (int(session_id),)).fetchall()
        return [{**dict(row),"data":json.loads(row["data"] or "{}")} for row in rows]

    def record_draft_match(self, session_id, result, home=0, away=0,
                           history_stats=None):
        normalized=str(result or "WIN").upper()
        if normalized == "LOSE": normalized="LOSS"
        if normalized not in ("WIN","LOSS","DRAW"): normalized="LOSS"
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                session=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if session is None or session["state"] != "READY_FOR_MATCH":
                    self.conn.rollback(); return None
                round_no=int(self.conn.execute(
                    "SELECT COUNT(*) FROM draft_matches WHERE session_id=?",
                    (int(session_id),)).fetchone()[0])+1
                if round_no>4:
                    self.conn.rollback(); return self._draft_dict(session)
                self.conn.execute(
                    "INSERT INTO draft_matches(session_id,round,result,home_goals,"
                    "away_goals,created,data) VALUES(?,?,?,?,?,?,?)",
                    (int(session_id),round_no,normalized,int(home),int(away),
                     now_s(),json.dumps(dict(history_stats or {}))))
                wins=int(session["wins"])+(1 if normalized=="WIN" else 0)
                losses=int(session["losses"])+(1 if normalized!="WIN" else 0)
                terminal=normalized!="WIN" or round_no>=4
                state="READY_FOR_REWARDS" if terminal else "READY_FOR_MATCH"
                self.conn.execute(
                    "UPDATE draft_sessions SET state=?,wins=?,losses=?,updated=? "
                    "WHERE id=?",(state,wins,losses,now_s(),int(session_id)))
                self._season_record_increment_tx(self.conn,normalized)
                self.conn.commit()
                return self.draft_session(session_id)
            except Exception:
                self.conn.rollback(); raise

    def complete_draft_match(self, active_key, gid, session_id, result,
                             response, home=0, away=0, reward=0,
                             history_stats=None):
        """Close one gid-backed Draft match and persist its response atomically.

        Retail may retry ``/match/end`` after a transport interruption.  The
        match row, Draft state transition, Objective increment and immutable
        native response therefore share one SQLite transaction and one receipt
        in ``active_key``.  Legacy rows completed by an older build are adopted
        without recording a second result.
        """
        normalized=str(result or "LOSS").upper()
        if normalized == "LOSE": normalized="LOSS"
        if normalized not in ("WIN","LOSS","DRAW"): normalized="LOSS"
        response=dict(response or {})
        reward=min(MAX_MATCH_COIN_REWARD,max(0,int(reward or 0)))
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                active_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key=?",(str(active_key),)
                ).fetchone()
                active=(json.loads(active_row["value"])
                        if active_row is not None else {})
                if (not isinstance(active,dict) or
                        int(active.get("gid",0) or 0) != int(gid) or
                        int(active.get("sessionId",0) or 0) != int(session_id)):
                    self.conn.rollback(); return None

                stored_response=active.get("response")
                if bool(active.get("completed",False)):
                    if not isinstance(stored_response,dict) or not stored_response:
                        active["response"]=response
                        self.conn.execute(
                            "UPDATE kv SET value=? WHERE key=?",
                            (json.dumps(active),str(active_key)))
                        stored_response=response
                    self.conn.commit()
                    return {"createdNow":False,"response":stored_response,
                            "session":self.draft_session(session_id)}

                session=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if session is None:
                    self.conn.rollback(); return None

                created_now=False
                if session["state"] == "READY_FOR_MATCH":
                    round_no=int(self.conn.execute(
                        "SELECT COUNT(*) FROM draft_matches WHERE session_id=?",
                        (int(session_id),)).fetchone()[0])+1
                    if round_no>4:
                        self.conn.rollback(); return None
                    self.conn.execute(
                        "INSERT INTO draft_matches(session_id,round,result,"
                        "home_goals,away_goals,created,data) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (int(session_id),round_no,normalized,int(home),int(away),
                         now_s(),json.dumps({
                             **dict(history_stats or {}),
                             "rewardCoins":reward,
                             "endReason":response.get(
                                 "endReason",normalized)})))
                    wins=int(session["wins"])+(1 if normalized=="WIN" else 0)
                    losses=int(session["losses"])+(1 if normalized!="WIN" else 0)
                    terminal=normalized!="WIN" or round_no>=4
                    state="READY_FOR_REWARDS" if terminal else "READY_FOR_MATCH"
                    self.conn.execute(
                        "UPDATE draft_sessions SET state=?,wins=?,losses=?,"
                        "updated=? WHERE id=?",
                        (state,wins,losses,now_s(),int(session_id)))
                    created_now=True
                else:
                    # An older build could commit the match before marking the
                    # gid receipt complete. Adopt only an existing round; never
                    # invent a result for an unrelated Draft state.
                    match_count=int(self.conn.execute(
                        "SELECT COUNT(*) FROM draft_matches WHERE session_id=?",
                        (int(session_id),)).fetchone()[0])
                    if match_count < 1:
                        self.conn.rollback(); return None

                if created_now:
                    self._season_record_increment_tx(self.conn,normalized)
                    objective_row=self.conn.execute(
                        "SELECT value FROM kv WHERE key='objective_matches_played'"
                    ).fetchone()
                    objective=(int(json.loads(objective_row["value"]))
                               if objective_row is not None else 0)+1
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES"
                        "('objective_matches_played',?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        (json.dumps(objective),))

                applied_reward=reward if created_now else 0
                credits=self._market_credits_tx(self.conn)
                if applied_reward:
                    credits+=applied_reward
                    self._market_set_credits_tx(self.conn,credits)
                response.update({
                    "allCoins":credits,"credits":credits,"coins":credits,
                    "totalCredits":credits,"funds":credits,
                    "finalFunds":credits,"sessionCoinsBankBalance":credits,
                    "matchCoins":applied_reward,
                    "seasonCoins":applied_reward,
                    "rewardCoins":applied_reward,
                    "totalCoins":applied_reward,
                    "completionAward":applied_reward,
                })
                game_mode_award=(response.get("gameModeAward")
                    if isinstance(response.get("gameModeAward"),dict) else {})
                game_mode_award=dict(game_mode_award)
                game_mode_award.update({"bidTokens":int(game_mode_award.get(
                    "bidTokens",0) or 0),"coins":applied_reward})
                response["gameModeAward"]=game_mode_award

                active.update({"completed":True,"completedAt":now_s(),
                               "result":normalized,"homeGoals":int(home),
                               "awayGoals":int(away),
                               "rewardCoins":applied_reward,
                               "response":response})
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value",
                    (str(active_key),json.dumps(active)))
                self.conn.commit()
                return {"createdNow":created_now,"response":response,
                        "session":self.draft_session(session_id)}
            except Exception:
                self.conn.rollback(); raise

    @staticmethod
    def draft_reward_plan(wins, difficulty):
        """Return the original local FIFA 19 Draft reward progression.

        The later experimental table mixed difficulty-scaled multi-pack
        bundles and Player Picks into Draft prizes.  Restore the simple
        preconfigured 0..4-win ladder: one classic pack plus coins, with no
        project-authored Player Pick reward.
        """
        wins=max(0,min(4,int(wins))); difficulty=max(1,min(7,int(difficulty)))
        coins,pack_id={
            0:(1000,301),
            1:(3000,301),
            2:(7500,303),
            3:(15000,305),
            4:(30000,402),
        }[wins]
        return {"tier":wins,"coins":coins,"packId":pack_id,
                "packIds":[pack_id],"pickCount":0,"minRating":0,
                "optionCount":3,"difficulty":difficulty,"wins":wins}

    def _ensure_draft_reward_picks(self, session_id, plan):
        if not int(plan.get("pickCount",0) or 0):
            return []
        from fut_packs import generate_player_pick_options
        picks=[]
        for index in range(int(plan["pickCount"])):
            seed=(int(session_id)*1000003+int(plan["difficulty"])*10007+
                  int(plan["wins"])*101+index)
            spec=generate_player_pick_options(
                min_rating=int(plan["minRating"]),
                option_count=int(plan["optionCount"]),seed=seed,
                special_chance=0.25)
            picks.append(self.grant_player_pick(
                "draft:%d:pick:%d" % (int(session_id),index),spec))
        return picks

    @staticmethod
    def _draft_reward_receipt(session_id, plan, picks):
        """Build the durable receipt returned by every claim retry."""
        coins=int(plan.get("coins",0) or 0)
        pack_ids=[int(value) for value in
                  (plan.get("packIds") or [plan.get("packId",0)])
                  if int(value or 0)>0]
        pack_counts={}
        for pack_id in pack_ids:
            pack_counts[pack_id]=pack_counts.get(pack_id,0)+1
        awards=[{"type":"coin","value":coins,"count":1}]
        awards.extend({"type":"pack","value":pack_id,"count":count}
                      for pack_id,count in pack_counts.items())
        if picks:
            awards.append({"type":"playerPick","value":0,
                           "count":len(picks),
                           "minRating":int(plan.get("minRating",0) or 0),
                           "optionCount":int(plan.get("optionCount",0) or 0)})
        return {"receiptId":"draft:%d" % int(session_id),
                "awards":awards,"coins":coins,
                "packId":pack_ids[0] if pack_ids else 0,
                "packIds":pack_ids,"playerPicks":picks,
                "rewardPlan":plan,"alreadyClaimed":False}

    def _persist_draft_reward_receipt(self, session_id, receipt):
        """Store one immutable receipt, returning the winner of a retry race."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT data FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return receipt
                data=json.loads(row["data"] or "{}")
                existing=data.get("rewardReceipt")
                if isinstance(existing,dict):
                    self.conn.commit(); return existing
                data["rewardReceipt"]=receipt
                self.conn.execute(
                    "UPDATE draft_sessions SET data=?,updated=? WHERE id=?",
                    (json.dumps(data),now_s(),int(session_id)))
                self.conn.commit(); return receipt
            except Exception:
                self.conn.rollback(); raise

    def claim_draft_reward(self, session_id):
        plan=None
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                if int(row["reward_claimed"]):
                    data=json.loads(row["data"] or "{}")
                    receipt=data.get("rewardReceipt")
                    plan=data.get("rewardPlan")
                    self.conn.commit()
                    if isinstance(receipt,dict):
                        return receipt
                    if not plan:
                        receipt={"receiptId":"draft:%d" % int(session_id),
                                 "awards":[],"coins":0,"packId":0,
                                 "packIds":[],"playerPicks":[],
                                 "rewardPlan":None,"alreadyClaimed":False}
                        return self._persist_draft_reward_receipt(
                            session_id,receipt)
                    picks=self._ensure_draft_reward_picks(session_id,plan)
                    receipt=self._draft_reward_receipt(session_id,plan,picks)
                    return self._persist_draft_reward_receipt(
                        session_id,receipt)
                if row["state"]!="READY_FOR_REWARDS":
                    self.conn.rollback(); return None
                if int(row["difficulty"] or 0) not in range(1,8):
                    self.conn.rollback(); return None
                plan=self.draft_reward_plan(row["wins"],row["difficulty"])
                coins=int(plan["coins"])
                pack_ids=[int(value) for value in
                          (plan.get("packIds") or [plan["packId"]])]
                credit_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='credits'").fetchone()
                credits=int(json.loads(credit_row["value"])) if credit_row else 0
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('credits',?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value",(json.dumps(credits+coins),))
                for bundle_index,pack_id in enumerate(pack_ids):
                    self.conn.execute(
                        "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                        (pack_id,json.dumps({"source":"draft",
                                            "draftId":int(session_id),
                                            "difficulty":int(plan["difficulty"]),
                                            "wins":int(plan["wins"]),
                                            "bundleIndex":bundle_index})))
                data=json.loads(row["data"] or "{}")
                data["rewardPlan"]=plan
                self.conn.execute(
                    "UPDATE draft_sessions SET state='COMPLETED_DRAFT',"
                    "reward_claimed=1,data=?,updated=? WHERE id=?",
                    (json.dumps(data),now_s(),int(session_id)))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
        picks=self._ensure_draft_reward_picks(session_id,plan)
        receipt=self._draft_reward_receipt(session_id,plan,picks)
        return self._persist_draft_reward_receipt(session_id,receipt)

    def abandon_draft(self, session_id):
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM draft_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                if (row["state"]=="COMPLETED_DRAFT" or
                        int(row["reward_claimed"] or 0)):
                    self.conn.commit(); return self._draft_dict(row)
                difficulty=int(row["difficulty"] or 0)
                if difficulty not in range(1,8):
                    difficulty=1
                if (row["state"]!="READY_FOR_REWARDS" or
                        int(row["difficulty"] or 0)!=difficulty):
                    data=json.loads(row["data"] or "{}")
                    data["retired"]=True
                    data["retiredAt"]=now_s()
                    self.conn.execute(
                        "UPDATE draft_sessions SET state='READY_FOR_REWARDS',"
                        "difficulty=?,data=?,updated=? WHERE id=?",
                        (difficulty,json.dumps(data),now_s(),int(session_id)))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
        return self.draft_session(session_id)

    # ---------- Squad Battles ----------
    def migrate_legacy_sqbt_quits(self):
        """Repair withdrawals settled by the pre-zero-points implementation.

        Early local builds stored a QUIT as an ordinary LOSS, so the opponent
        row and club loss were correct but the match retained loss points (the
        reported live case was 140).  The durable active receipt is the only
        legacy row that still carries ``endReason``; newer rows also persist it
        in match data.  Repair only those proven withdrawals, keep the loss,
        and recompute the event score/rank/tier from its match ledger.
        """
        quit_reasons={"QUIT","DNF","DISCONNECT","FORFEIT","NO_CONTEST"}
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                active_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='active_sqbt_match'"
                ).fetchone()
                active=(json.loads(active_row["value"])
                        if active_row is not None else {})
                active=active if isinstance(active,dict) else {}
                active_wire=(active.get("wireResponse")
                             if isinstance(active.get("wireResponse"),dict)
                             else {})
                active_reason=str(active_wire.get(
                    "endReason",active.get("endReason","")) or "").upper()
                active_key=None
                if active_reason in quit_reasons:
                    active_key=(int(active.get("eventId",0) or 0),
                                int(active.get("opponentId",0) or 0))

                rows=self.conn.execute(
                    "SELECT event_id,opponent_id,points,data FROM sqbt_matches"
                ).fetchall()
                changed=0; deducted_coins=0; changed_events=set()
                for row in rows:
                    data=json.loads(row["data"] or "{}")
                    key=(int(row["event_id"]),int(row["opponent_id"]))
                    reason=str(data.get("endReason","") or "").upper()
                    if key != active_key and reason not in quit_reasons:
                        continue
                    old_points=max(0,int(row["points"] or 0))
                    old_coins=max(0,int(data.get("rewardCoins",0) or 0))
                    if old_points or old_coins or reason not in quit_reasons:
                        changed+=1
                    deducted_coins+=old_coins
                    data.update({"rewardCoins":0,"endReason":(
                        active_reason if key==active_key else reason) or "QUIT",
                        "retired":True,"settlementVersion":2})
                    self.conn.execute(
                        "UPDATE sqbt_matches SET points=0,data=? "
                        "WHERE event_id=? AND opponent_id=?",
                        (json.dumps(data),key[0],key[1]))
                    changed_events.add(key[0])

                if deducted_coins:
                    credits=max(0,self._market_credits_tx(self.conn)-deducted_coins)
                    self._market_set_credits_tx(self.conn,credits)
                else:
                    credits=self._market_credits_tx(self.conn)

                for event_id in changed_events:
                    score=int(self.conn.execute(
                        "SELECT COALESCE(SUM(points),0) FROM sqbt_matches "
                        "WHERE event_id=?",(event_id,)).fetchone()[0])
                    match_count=int(self.conn.execute(
                        "SELECT COUNT(*) FROM sqbt_matches WHERE event_id=?",
                        (event_id,)).fetchone()[0])
                    rank=sqbt_user_rank(event_id,score,
                                        rounds_played=match_count)
                    tier=sqbt_tier_level(event_id,score)
                    self.conn.execute(
                        "UPDATE sqbt_events SET score=?,rank=?,tier=? WHERE id=?",
                        (score,rank,tier,event_id))

                if active_key is not None:
                    score_fields=("finalScore","goalsScore","matchResultScore",
                                  "skillRatingScore","teamRatingScore")
                    score_doc=(active_wire.get("squadBattlesScore")
                               if isinstance(active_wire.get(
                                   "squadBattlesScore"),dict) else {})
                    for field in score_fields:
                        score_doc[field]=0
                    score_doc["matchDifficultyScoreModifier"]=0.0
                    active_wire["squadBattlesScore"]=score_doc
                    for field in ("matchCoins","seasonCoins","rewardCoins",
                                  "totalCoins","completionAward","skillAward"):
                        active_wire[field]=0
                    game_mode_award=(active_wire.get("gameModeAward")
                                     if isinstance(active_wire.get(
                                         "gameModeAward"),dict) else {})
                    game_mode_award["coins"]=0
                    active_wire["gameModeAward"]=game_mode_award
                    for field in ("allCoins","credits","coins","totalCredits",
                                  "funds","finalFunds",
                                  "sessionCoinsBankBalance"):
                        active_wire[field]=credits
                    active.update({"wireResponse":active_wire,
                                   "rewardCoins":0,"points":0,
                                   "endReason":active_reason or "QUIT",
                                   "settlementVersion":2})
                    self.conn.execute(
                        "UPDATE kv SET value=? WHERE key='active_sqbt_match'",
                        (json.dumps(active),))
                self.conn.commit()
                return {"matches":changed,"events":len(changed_events),
                        "deductedCoins":deducted_coins}
            except Exception:
                self.conn.rollback(); raise

    def ensure_sqbt_event(self, event_id, event_key, expires, opponents):
        """Create the current local event and its source-backed opponents once."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                timestamp=now_s()
                self.conn.execute(
                    "INSERT OR IGNORE INTO sqbt_events(id,event_key,created,expires,data) "
                    "VALUES(?,?,?,?,?)",
                    (int(event_id),str(event_key),timestamp,int(expires),"{}"))
                for opponent in opponents:
                    self.conn.execute(
                        "INSERT INTO sqbt_opponents(event_id,opponent_id,"
                        "rotation,data) VALUES(?,?,?,?) ON CONFLICT(event_id,"
                        "opponent_id) DO UPDATE SET rotation=excluded.rotation,"
                        "data=excluded.data",
                        (int(event_id),int(opponent["opponentId"]),
                         int(opponent.get("rotation",0)),json.dumps(opponent)))
                score_row=self.conn.execute(
                    "SELECT score,rank,tier FROM sqbt_events WHERE id=?",
                    (int(event_id),)).fetchone()
                if score_row is not None:
                    match_count=int(self.conn.execute(
                        "SELECT COUNT(*) FROM sqbt_matches WHERE event_id=?",
                        (int(event_id),)).fetchone()[0])
                    rank=sqbt_user_rank(
                        int(event_id),int(score_row["score"]),
                        rounds_played=match_count)
                    tier=sqbt_tier_level(
                        int(event_id),int(score_row["score"]))
                    if (int(score_row["rank"])!=rank or
                            int(score_row["tier"])!=tier):
                        self.conn.execute(
                            "UPDATE sqbt_events SET rank=?,tier=? WHERE id=?",
                            (rank,tier,int(event_id)))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
        return self.sqbt_event(event_id)

    def sqbt_event(self, event_id=None):
        if event_id is None:
            row=self.conn.execute(
                "SELECT * FROM sqbt_events ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row=self.conn.execute(
                "SELECT * FROM sqbt_events WHERE id=?",(int(event_id),)).fetchone()
        if row is None: return None
        result=dict(row); result["data"]=json.loads(row["data"] or "{}")
        result["opponents"]=self.sqbt_opponents(int(row["id"]))
        return result

    def claimable_sqbt_event(self, reference_time=None):
        """Return the newest finished competition whose prize is claimable.

        Squad Battles rewards belong to the competition that just ended, not
        to the current in-progress event. An expired zero-match competition
        receives the published Bronze 3 reward.
        """
        cutoff=now_s() if reference_time is None else int(reference_time)
        row=self.conn.execute(
            "SELECT event.* FROM sqbt_events AS event WHERE "
            "event.expires<=? "
            "ORDER BY event.expires DESC,event.id DESC "
            "LIMIT 1",(cutoff,)).fetchone()
        if row is None or int(row["reward_claimed"]):
            return None
        return self.sqbt_event(int(row["id"]))

    def sqbt_opponents(self, event_id, rotation=None):
        if rotation is None:
            rows=self.conn.execute(
                "SELECT * FROM sqbt_opponents WHERE event_id=? AND rotation=(SELECT "
                "COALESCE(MAX(rotation),0) FROM sqbt_opponents WHERE event_id=?) "
                "ORDER BY opponent_id",(int(event_id),int(event_id))).fetchall()
        else:
            rows=self.conn.execute(
                "SELECT * FROM sqbt_opponents WHERE event_id=? AND rotation=? "
                "ORDER BY opponent_id",(int(event_id),int(rotation))).fetchall()
        match_rows=self.conn.execute(
            "SELECT * FROM sqbt_matches WHERE event_id=?",
            (int(event_id),)).fetchall()
        matches={int(row["opponent_id"]):dict(row) for row in match_rows}
        merged=[]
        for row in rows:
            columns=dict(row)
            definition=json.loads(columns.pop("data",None) or "{}")
            # The stored definition is a snapshot taken when the rotation was
            # created, so its "played" is always False.  Let the row's mutable
            # columns win, otherwise a recorded match never marks its opponent
            # as played on the hub.  The raw definition string itself is a
            # duplicate of the merged members and never crosses the wire.
            entry={**columns,**definition}
            entry["played"]=bool(columns.get("played",0))
            entry["rotation"]=int(columns.get("rotation",0) or 0)
            match=matches.get(int(columns.get("opponent_id",0) or 0))
            if match is not None:
                match_data=json.loads(match.get("data") or "{}")
                entry.update({"result":str(match.get("result","") or ""),
                              "difficulty":int(match.get("difficulty",0) or 0),
                              "pointsWon":int(match.get("points",0) or 0),
                              "userScore":int(match_data.get("homeGoals",0) or 0),
                              "oppScore":int(match_data.get("awayGoals",0) or 0)})
            merged.append(entry)
        return merged

    def sqbt_match_stats(self, event_id):
        """Return durable weekly W/D/L and points statistics."""
        rows=self.conn.execute(
            "SELECT result,points FROM sqbt_matches WHERE event_id=?",
            (int(event_id),)).fetchall()
        wins=sum(1 for row in rows if str(row["result"]).upper()=="WIN")
        draws=sum(1 for row in rows if str(row["result"]).upper()=="DRAW")
        losses=len(rows)-wins-draws
        return {"matchesPlayed":len(rows),"wins":wins,"draws":draws,
                "losses":losses,
                "points":sum(int(row["points"] or 0) for row in rows)}

    def record_sqbt_match(self, event_id, opponent_id, result, difficulty,
                          points, home=0, away=0, reward_coins=0,
                          end_reason=""):
        """Settle one Squad Battles fixture atomically and exactly once.

        The event score, the general club record and the match-coin balance
        are one economy operation.  Retail may retry ``/match/end`` after the
        HTTP response was lost, so the opponent primary key is also the
        durable idempotency key: a retry returns the original award without
        incrementing score, record or credits again.
        """
        normalized=str(result or "WIN").upper()
        if normalized == "LOSE": normalized="LOSS"
        reason=str(end_reason or normalized).upper()
        if reason in ("QUIT","DNF","DISCONNECT","FORFEIT","NO_CONTEST"):
            normalized="LOSS"
            points=0
            reward_coins=0
        reward_coins=min(MAX_MATCH_COIN_REWARD,max(
            0,int(reward_coins or 0)))
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                timestamp=now_s()
                opponent=self.conn.execute(
                    "SELECT played FROM sqbt_opponents WHERE event_id=? AND "
                    "opponent_id=?",
                    (int(event_id),int(opponent_id))).fetchone()
                if opponent is None:
                    self.conn.rollback(); return None
                existing=self.conn.execute(
                    "SELECT * FROM sqbt_matches WHERE event_id=? AND opponent_id=?",
                    (int(event_id),int(opponent_id))).fetchone()
                if existing is not None:
                    existing_data=json.loads(existing["data"] or "{}")
                    event=self.conn.execute(
                        "SELECT score,rank,tier FROM sqbt_events WHERE id=?",
                        (int(event_id),)).fetchone()
                    credits=self._market_credits_tx(self.conn)
                    self.conn.commit()
                    return {
                        "eventId":int(event_id),
                        "opponentId":int(opponent_id),
                        "result":str(existing["result"]),
                        "difficulty":int(existing["difficulty"]),
                        "points":int(existing["points"]),
                        "score":int(event["score"] if event else 0),
                        "rank":int(event["rank"] if event else 0),
                        "tier":int(event["tier"] if event else 0),
                        "homeGoals":int(existing_data.get("homeGoals",0) or 0),
                        "awayGoals":int(existing_data.get("awayGoals",0) or 0),
                        "rewardCoins":int(existing_data.get(
                            "rewardCoins",0) or 0),
                        "credits":credits,
                        "createdNow":False,
                    }
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+reward_coins)
                self.conn.execute(
                    "INSERT INTO sqbt_matches(event_id,opponent_id,result,difficulty,"
                    "points,created,data) VALUES(?,?,?,?,?,?,?)",
                    (int(event_id),int(opponent_id),normalized,int(difficulty),
                     max(0,int(points)),timestamp,json.dumps({
                         "homeGoals":int(home),"awayGoals":int(away),
                         "rewardCoins":reward_coins,
                         "endReason":reason,
                         "matchKey":"sqbt:%d:%d" %
                                    (int(event_id),int(opponent_id))})))
                self.conn.execute(
                    "UPDATE sqbt_opponents SET played=1 WHERE event_id=? AND opponent_id=?",
                    (int(event_id),int(opponent_id)))
                self._season_record_increment_tx(self.conn,normalized)
                row=self.conn.execute(
                    "SELECT score FROM sqbt_events WHERE id=?",(int(event_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                score=int(row["score"])+max(0,int(points))
                tier=sqbt_tier_level(int(event_id),score)
                match_count=int(self.conn.execute(
                    "SELECT COUNT(*) FROM sqbt_matches WHERE event_id=?",
                    (int(event_id),)).fetchone()[0])
                rank=sqbt_user_rank(
                    int(event_id),score,rounds_played=match_count)
                self.conn.execute(
                    "UPDATE sqbt_events SET score=?,rank=?,tier=? WHERE id=?",
                    (score,rank,tier,int(event_id)))
                debuts=self._record_market_player_debuts_tx(
                    "SQBT","sqbt:%d:%d" %
                           (int(event_id),int(opponent_id)),timestamp)
                self.conn.commit()
                return {"eventId":int(event_id),"opponentId":int(opponent_id),
                        "result":normalized,"points":max(0,int(points)),
                        "difficulty":int(difficulty),
                        "homeGoals":int(home),"awayGoals":int(away),
                        "rewardCoins":reward_coins,"credits":credits,
                        "score":score,"rank":rank,"tier":tier,
                        "createdNow":True,
                        "marketPlayerDebuts":debuts}
            except Exception:
                self.conn.rollback(); raise

    def claim_sqbt_reward(self, event_id, reference_time=None):
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM sqbt_events WHERE id=?",(int(event_id),)).fetchone()
                matches=int(self.conn.execute(
                    "SELECT COUNT(*) FROM sqbt_matches WHERE event_id=?",
                    (int(event_id),)).fetchone()[0])
                cutoff=now_s() if reference_time is None else int(reference_time)
                if row is None or int(row["expires"])>cutoff:
                    self.conn.rollback(); return None
                if int(row["reward_claimed"]):
                    self.conn.rollback(); return {"awards":[],"alreadyClaimed":True}
                tier_spec=sqbt_prize_tier(int(event_id),int(row["score"]))
                tier=int(tier_spec["tierLevel"])
                score=max(0,int(row["score"]))
                rank=sqbt_user_rank(int(event_id),score,
                                    rounds_played=matches)
                awards=[dict(award) for award in tier_spec["awards"]]
                coins=sum(int(award["value"])*int(award.get("count",1))
                          for award in awards if award["type"]=="coin")
                credit_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='credits'").fetchone()
                credits=int(json.loads(credit_row["value"])) if credit_row else 0
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('credits',?) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value",(json.dumps(credits+coins),))
                for award in awards:
                    if award["type"] != "pack":
                        continue
                    for _ in range(max(1,int(award.get("count",1)))):
                        self.conn.execute(
                            "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                            (int(award["value"]),json.dumps({
                                "source":"sqbt","eventId":int(event_id),
                                "tierLevel":tier})))
                player_picks=[]
                for award in awards:
                    if str(award.get("type","")).lower() != "playerpick":
                        continue
                    for pick_index in range(max(0,int(award.get(
                            "count",0) or 0))):
                        spec=fut_champions_pick_spec(
                            int(event_id),tier,pick_index,
                            int(award.get("optionCount",4) or 4))
                        player_picks.append(self._grant_player_pick_tx(
                            "sqbt:%d:tier:%d:fut-champions:%d" %
                            (int(event_id),tier,pick_index),spec))
                self.conn.execute(
                    "UPDATE sqbt_events SET reward_claimed=1,tier=?,rank=? WHERE id=?",
                    (tier,rank,int(event_id)))
                self.conn.commit()
                return {"awards":awards,"playerPicks":player_picks,
                        "alreadyClaimed":False,
                        "tierLevel":tier,"tierName":str(tier_spec["name"]),
                        "rank":rank,"score":score}
            except Exception:
                self.conn.rollback(); raise

    # ---------- FUT Champions offline ----------
    def _champion_session_dict(self, row):
        if row is None:
            return None
        result=dict(row)
        result["data"]=json.loads(row["data"] or "{}")
        opponent_rows=self.conn.execute(
            "SELECT * FROM champion_opponents WHERE session_id=? "
            "ORDER BY ordinal",(int(row["id"]),)).fetchall()
        opponents=[]
        for opponent in opponent_rows:
            definition=json.loads(opponent["data"] or "{}")
            definition["played"]=bool(opponent["played"])
            definition["ordinal"]=int(opponent["ordinal"])
            opponents.append(definition)
        match_rows=self.conn.execute(
            "SELECT * FROM champion_matches WHERE session_id=? "
            "ORDER BY created,opponent_id",(int(row["id"]),)).fetchall()
        matches=[]
        for match in match_rows:
            value=dict(match)
            value["data"]=json.loads(match["data"] or "{}")
            matches.append(value)
        result["opponents"]=opponents
        result["matches"]=matches
        return result

    def champion_session(self, session_id=None, event_id=None):
        if session_id is not None:
            row=self.conn.execute(
                "SELECT * FROM champion_sessions WHERE id=?",
                (int(session_id),)).fetchone()
        elif event_id is not None:
            row=self.conn.execute(
                "SELECT * FROM champion_sessions WHERE event_id=?",
                (int(event_id),)).fetchone()
        else:
            row=self.conn.execute(
                "SELECT * FROM champion_sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._champion_session_dict(row)

    def current_champion_session(self):
        row=self.conn.execute(
            "SELECT * FROM champion_sessions WHERE NOT "
            "(state='COMPLETED' AND reward_claimed=1) "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return self._champion_session_dict(row)

    def start_champion_session(self, event_id, event_key, expires, opponents,
                               rng_seed=0, event_attempt=0):
        """Persist one immutable 20-opponent competition, once per event.

        Opponent recipes are snapshots because a catalogue update during a
        multi-day run must not change the next squad after a launcher restart.
        """
        definitions=[dict(value) for value in opponents]
        ordinals=[int(value.get("matchNumber",0) or 0)
                  for value in definitions]
        opponent_ids=[int(value.get("opponentId",0) or 0)
                      for value in definitions]
        if (len(definitions)!=CHAMPION_MATCH_COUNT or
                sorted(ordinals)!=list(range(1,CHAMPION_MATCH_COUNT+1)) or
                len(set(opponent_ids))!=CHAMPION_MATCH_COUNT or
                any(value<=0 for value in opponent_ids)):
            raise ValueError("FUT Champions requires 20 numbered opponents")
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                existing=self.conn.execute(
                    "SELECT * FROM champion_sessions WHERE event_id=?",
                    (int(event_id),)).fetchone()
                if existing is not None:
                    self.conn.commit()
                    result=self.champion_session(int(existing["id"]))
                    result["createdNow"]=False
                    return result
                timestamp=now_s()
                payload={"generationVersion":1,
                         "eventAttempt":max(0,int(event_attempt)),
                         "botLeaderboard":champion_leaderboard_bots(
                             int(event_id))}
                cursor=self.conn.execute(
                    "INSERT INTO champion_sessions(event_id,event_key,state,"
                    "difficulty,rng_seed,created,updated,expires,data) "
                    "VALUES(?,?,'PICK_DIFFICULTY',0,?,?,?,?,?)",
                    (int(event_id),str(event_key),int(rng_seed),timestamp,
                     timestamp,int(expires),json.dumps(payload)))
                session_id=int(cursor.lastrowid)
                for definition in definitions:
                    ordinal=int(definition["matchNumber"])
                    opponent_id=int(definition["opponentId"])
                    stored=dict(definition)
                    stored["ordinal"]=ordinal
                    stored["played"]=False
                    self.conn.execute(
                        "INSERT INTO champion_opponents(session_id,opponent_id,"
                        "ordinal,played,data) VALUES(?,?,?,?,?)",
                        (session_id,opponent_id,ordinal,0,json.dumps(stored)))
                self.conn.commit()
                result=self.champion_session(session_id)
                result["createdNow"]=True
                return result
            except Exception:
                self.conn.rollback(); raise

    def register_champion_session(self, session_id, country_code, region):
        """Persist the explicit native registration without rebuilding a run."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT data FROM champion_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                data=json.loads(row["data"] or "{}")
                registration={
                    "competitionCountryCode":str(country_code),
                    "competitionRegion":str(region),
                }
                created_now=data.get("registration")!=registration
                if created_now:
                    # Opening the RC80 Hub created an otherwise untouched run.
                    # Storing registration separately lets that legacy row wait
                    # for the real POST instead of silently enrolling the user.
                    data["registration"]=registration
                    data["registeredAt"]=now_s()
                    self.conn.execute(
                        "UPDATE champion_sessions SET data=?,updated=? WHERE id=?",
                        (json.dumps(data),now_s(),int(session_id)))
                self.conn.commit()
                result=self.champion_session(int(session_id))
                result["registeredNow"]=created_now
                return result
            except Exception:
                self.conn.rollback(); raise

    def lock_champion_difficulty(self, session_id, difficulty):
        """Accept the first difficulty and only exact retries afterwards."""
        value=int(difficulty)
        if value not in range(1,8):
            return None
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT difficulty,state FROM champion_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                current=int(row["difficulty"] or 0)
                if current not in (0,value):
                    self.conn.rollback(); return None
                if current==0:
                    self.conn.execute(
                        "UPDATE champion_sessions SET difficulty=?,"
                        "state='READY_FOR_MATCH',updated=? WHERE id=?",
                        (value,now_s(),int(session_id)))
                self.conn.commit()
                return self.champion_session(int(session_id))
            except Exception:
                self.conn.rollback(); raise

    def register_champion_offline_session(self, session_id, difficulty):
        """Atomically enroll an offline run and lock its one difficulty."""
        value=int(difficulty)
        if value not in range(1,8):
            return None
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT difficulty,state,data FROM champion_sessions "
                    "WHERE id=?",(int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                current=int(row["difficulty"] or 0)
                if (current not in (0,value) or
                        str(row["state"]) not in (
                            "PICK_DIFFICULTY","READY_FOR_MATCH")):
                    self.conn.rollback(); return None
                data=json.loads(row["data"] or "{}")
                registration=dict(data.get("registration",{}) or {})
                registered_now=(not bool(registration.get("offline",False)) or
                                current==0)
                # RC94 proved that retaining a stock country sends the next
                # entry through regional and marketing UI.  Offline enrollment
                # owns no country at all, so replace that object rather than
                # disguising it with a different nation or region.
                data["registration"]={"offline":True}
                if registered_now:
                    data["registeredAt"]=now_s()
                self.conn.execute(
                    "UPDATE champion_sessions SET difficulty=?,"
                    "state='READY_FOR_MATCH',data=?,updated=? WHERE id=?",
                    (value,json.dumps(data),now_s(),int(session_id)))
                self.conn.commit()
                result=self.champion_session(int(session_id))
                result["registeredNow"]=registered_now
                return result
            except Exception:
                self.conn.rollback(); raise

    @staticmethod
    def _champion_rank_from_snapshot(data,wins,goal_difference):
        bots=list(data.get("botLeaderboard",[]) or [])
        user={"wins":int(wins),"goalDifference":int(goal_difference),
              "persona":"","isUser":True}
        rows=bots+[user]
        rows.sort(key=lambda value:(-int(value.get("wins",0) or 0),
                                    -int(value.get("goalDifference",0) or 0),
                                    0 if value is user else 1,
                                    str(value.get("persona",''))))
        position=rows.index(user)+1
        return position if position<=100 else 0

    def record_champion_match(self,session_id,opponent_id,gid,result,
                              home_goals,away_goals,reward_coins=0,
                              end_reason=""):
        """Settle one of the twenty fixtures with gid/opponent idempotency."""
        normalized=str(result or "LOSS").upper()
        if normalized=="LOSE": normalized="LOSS"
        if normalized not in ("WIN","DRAW","LOSS"): normalized="LOSS"
        reason=str(end_reason or normalized).upper()
        if reason in ("QUIT","DNF","DISCONNECT","FORFEIT","NO_CONTEST"):
            normalized="LOSS"; reward_coins=0
        reward_coins=min(MAX_MATCH_COIN_REWARD,max(0,int(reward_coins or 0)))
        operation=str(gid or "").strip()
        if not operation:
            return None
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                session=self.conn.execute(
                    "SELECT * FROM champion_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if (session is None or int(session["difficulty"] or 0)<=0 or
                        str(session["state"]) not in (
                            "READY_FOR_MATCH","READY_FOR_REWARDS")):
                    self.conn.rollback(); return None
                existing=self.conn.execute(
                    "SELECT * FROM champion_matches WHERE session_id=? AND "
                    "(gid=? OR opponent_id=?) ORDER BY created LIMIT 1",
                    (int(session_id),operation,int(opponent_id))).fetchone()
                if existing is not None:
                    data=json.loads(existing["data"] or "{}")
                    receipt=dict(data.get("receipt",{}) or {})
                    receipt["createdNow"]=False
                    self.conn.commit(); return receipt
                count=int(self.conn.execute(
                    "SELECT COUNT(*) FROM champion_matches WHERE session_id=?",
                    (int(session_id),)).fetchone()[0])
                opponent=self.conn.execute(
                    "SELECT played FROM champion_opponents WHERE session_id=? "
                    "AND opponent_id=?",
                    (int(session_id),int(opponent_id))).fetchone()
                if (count>=CHAMPION_MATCH_COUNT or opponent is None or
                        int(opponent["played"] or 0)):
                    self.conn.rollback(); return None
                difficulty=int(session["difficulty"])
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+reward_coins)
                timestamp=now_s()
                wins=int(session["wins"])+(1 if normalized=="WIN" else 0)
                draws=int(session["draws"])+(1 if normalized=="DRAW" else 0)
                losses=int(session["losses"])+(1 if normalized=="LOSS" else 0)
                match_count=count+1
                state=("READY_FOR_REWARDS" if
                       match_count==CHAMPION_MATCH_COUNT else "READY_FOR_MATCH")
                receipt={"sessionId":int(session_id),
                         "eventId":int(session["event_id"]),
                         "opponentId":int(opponent_id),"gid":operation,
                         "result":normalized,"difficulty":difficulty,
                         "homeGoals":int(home_goals),
                         "awayGoals":int(away_goals),
                         "rewardCoins":reward_coins,"credits":credits,
                         "wins":wins,"draws":draws,"losses":losses,
                         "matchesPlayed":match_count,"state":state,
                         "createdNow":True}
                self.conn.execute(
                    "INSERT INTO champion_matches(session_id,opponent_id,gid,"
                    "result,difficulty,home_goals,away_goals,created,data) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(session_id),int(opponent_id),operation,normalized,
                     difficulty,int(home_goals),int(away_goals),timestamp,
                     json.dumps({"endReason":reason,"rewardCoins":reward_coins,
                                 "receipt":receipt})))
                self.conn.execute(
                    "UPDATE champion_opponents SET played=1 WHERE session_id=? "
                    "AND opponent_id=?",(int(session_id),int(opponent_id)))
                self.conn.execute(
                    "UPDATE champion_sessions SET state=?,wins=?,draws=?,"
                    "losses=?,updated=? WHERE id=?",
                    (state,wins,draws,losses,timestamp,int(session_id)))
                self._season_record_increment_tx(self.conn,normalized)
                self.conn.commit()
                return receipt
            except sqlite3.IntegrityError:
                self.conn.rollback()
                row=self.conn.execute(
                    "SELECT data FROM champion_matches WHERE session_id=? AND "
                    "(gid=? OR opponent_id=?) ORDER BY created LIMIT 1",
                    (int(session_id),operation,int(opponent_id))).fetchone()
                if row is None:
                    raise
                receipt=dict(json.loads(row["data"] or "{}").get(
                    "receipt",{}) or {})
                receipt["createdNow"]=False
                return receipt
            except Exception:
                self.conn.rollback(); raise

    def claim_champion_reward(self,session_id):
        """Grant the completed run once and replay its immutable receipt."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT * FROM champion_sessions WHERE id=?",
                    (int(session_id),)).fetchone()
                if row is None:
                    self.conn.rollback(); return None
                data=json.loads(row["data"] or "{}")
                if int(row["reward_claimed"] or 0):
                    receipt=dict(data.get("rewardReceipt",{}) or {})
                    receipt["alreadyClaimed"]=True
                    self.conn.commit(); return receipt
                matches=int(self.conn.execute(
                    "SELECT COUNT(*) FROM champion_matches WHERE session_id=?",
                    (int(session_id),)).fetchone()[0])
                if matches!=CHAMPION_MATCH_COUNT:
                    self.conn.rollback(); return None
                goal_difference=int(self.conn.execute(
                    "SELECT COALESCE(SUM(home_goals-away_goals),0) FROM "
                    "champion_matches WHERE session_id=?",
                    (int(session_id),)).fetchone()[0])
                rank=self._champion_rank_from_snapshot(
                    data,int(row["wins"]),goal_difference)
                bundle=champion_reward_bundle(
                    int(row["wins"]),int(row["difficulty"]),rank)
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+
                    int(bundle["coins"]))
                for pack in bundle["packs"]:
                    for _ in range(max(1,int(pack["count"]))):
                        self.conn.execute(
                            "INSERT INTO unopened_packs(pack_id,data) "
                            "VALUES(?,?)",(int(pack["packId"]),json.dumps({
                                "source":"champions",
                                "sessionId":int(session_id),
                                "eventId":int(row["event_id"]),
                                "tierLevel":int(bundle["tierLevel"])})))
                player_picks=[]
                for pick_index in range(int(bundle["playerPickCount"])):
                    spec=fut_champions_pick_spec(
                        int(row["event_id"]),int(bundle["tierLevel"]),
                        pick_index,int(bundle["playerPickOptions"]))
                    player_picks.append(self._grant_player_pick_tx(
                        "champions:%d:tier:%d:pick:%d" %
                        (int(session_id),int(bundle["tierLevel"]),pick_index),
                        spec))
                receipt={**bundle,"sessionId":int(session_id),
                         "eventId":int(row["event_id"]),"rank":rank,
                         "wins":int(row["wins"]),"draws":int(row["draws"]),
                         "losses":int(row["losses"]),"credits":credits,
                         "playerPicks":player_picks,"alreadyClaimed":False}
                data["rewardReceipt"]=receipt
                # A claimed event must remain immutable because the client can
                # retry its prize POST later.  Advance a separate local event
                # identity so replay never overwrites that receipt or its
                # twenty match rows.
                cursor={"eventKey":str(row["event_key"]),
                        "attempt":max(0,int(data.get(
                            "eventAttempt",0) or 0))+1}
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("champion_event_cursor",json.dumps(cursor)))
                self.conn.execute(
                    "UPDATE champion_sessions SET state='COMPLETED',"
                    "reward_claimed=1,updated=?,data=? WHERE id=?",
                    (now_s(),json.dumps(data),int(session_id)))
                self.conn.commit()
                return receipt
            except Exception:
                self.conn.rollback(); raise

    def _season_record_increment_tx(self, conn, result):
        """Book one finished match into the club record, inside the caller's
        transaction.

        The header record used to count only the offline-season matches routed
        through `record_match` and the TOTW results, so a Draft run or a Squad
        Battles week left it reading 0-0-0 no matter how many matches were
        actually played.  Every mode that closes a match now books its result
        here, exactly once, in the same transaction that stores the match.
        """
        normalized=str(result or "LOSS").upper()
        if normalized == "LOSE": normalized="LOSS"
        key=("season_wins" if normalized == "WIN" else
             "season_draws" if normalized == "DRAW" else "season_losses")
        self._market_increment_tx(conn,key,1)

    def offline_season(self):
        value=self.get("offline_season_v1")
        if isinstance(value,dict) and int(value.get("divisionId",0)) in range(1,11):
            return value
        value=new_offline_season(int(self.get("season_division",10) or 10))
        self.set("offline_season_v1",value)
        return value

    def offline_season_totals(self):
        value=self.get("offline_season_totals_v1",{})
        return value if isinstance(value,dict) else {}

    def record_offline_season_match(self,result,operation_key,home=0,away=0,
                                    reward=0):
        """Persist one fifteen-game Season result and its match coins once."""
        reward=min(MAX_MATCH_COIN_REWARD,max(0,int(reward or 0)))
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='offline_season_v1'"
                ).fetchone()
                state=(json.loads(row["value"]) if row else
                       new_offline_season(10))
                updated,created=record_offline_season_result(
                    state,result,operation_key)
                if not created:
                    self.conn.commit()
                    return {"createdNow":False,"season":state,
                            "credits":self._market_credits_tx(self.conn)}
                home=max(0,int(home or 0)); away=max(0,int(away or 0))
                updated["goalsFor"]=int(state.get("goalsFor",0))+home
                updated["goalsAgainst"]=int(state.get("goalsAgainst",0))+away
                if updated.get("complete"):
                    updated["active"]=False
                timestamp=now_s()
                source_key="season-match:%s" % str(operation_key)
                # Owner decision 2026-09-11: an ordinary Season match grants
                # coins only; Player Picks stay with promotion rewards.
                promotion_picks=[]
                if (updated.get("complete") and
                        int(updated.get("nextDivision",10)) <
                        int(updated.get("divisionId",10))):
                    event_id=int(sha1(source_key)[:8],16)
                    for index in range(2):
                        promotion_picks.append(self._grant_player_pick_tx(
                            "%s:promotion:%d" % (source_key,index),
                            fut_champions_pick_spec(
                                event_id,int(updated["divisionId"]),index)))
                cursor=self.conn.execute(
                    "INSERT INTO matches(created,mode,result,home_goals,"
                    "away_goals,reward_coins,data) VALUES(?,?,?,?,?,?,?)",
                    (timestamp,"SEASON",str(result).upper(),home,away,reward,
                     json.dumps({"operationKey":str(operation_key),
                                 "seasonId":int(state["seasonId"]),
                                 "round":int(updated["round"])})))
                self._season_record_increment_tx(self.conn,result)
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+reward)
                totals=self.offline_season_totals()
                normalized="LOSS" if str(result).upper()=="LOSE" else str(result).upper()
                totals[normalized.lower()+(
                    "es" if normalized=="LOSS" else "s")]=int(totals.get(
                        normalized.lower()+(
                            "es" if normalized=="LOSS" else "s"),0))+1
                totals["goalsFor"]=int(totals.get("goalsFor",0))+home
                totals["goalsAgainst"]=int(totals.get("goalsAgainst",0))+away
                if int(updated["userPoints"])>int(totals.get(
                        "bestPointsSeasonValue",0)):
                    totals["bestPointsSeasonValue"]=int(updated["userPoints"])
                    totals["bestPointsSeasonId"]=int(updated["seasonId"])
                for key in ("objective_matches_played",
                            "objective_seasons_matches"):
                    self._market_increment_tx(self.conn,key,1)
                if normalized=="WIN":
                    self._market_increment_tx(
                        self.conn,"objective_seasons_wins",1)
                for key,value in (("offline_season_v1",updated),
                                  ("offline_season_totals_v1",totals)):
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        (key,json.dumps(value)))
                self.conn.commit()
                return {"createdNow":True,"matchId":int(cursor.lastrowid),
                        "season":updated,"credits":credits,
                        "rewardCoins":reward,
                        "promotionPlayerPicks":promotion_picks}
            except Exception:
                self.conn.rollback(); raise

    def claim_offline_season_reward(self):
        """Grant the terminal division reward once."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='offline_season_v1'"
                ).fetchone()
                state=(json.loads(row["value"]) if row else
                       new_offline_season(10))
                updated,receipt,created=claim_season_reward(state)
                credits=self._market_credits_tx(self.conn)
                if created:
                    credits=self._market_set_credits_tx(
                        self.conn,credits+int(receipt["coins"]))
                    # Retail pays a pack only with some finish bands.
                    if int(receipt["packId"]) > 0:
                        pack=self.conn.execute(
                            "INSERT INTO unopened_packs(pack_id,data) "
                            "VALUES(?,?)",
                            (int(receipt["packId"]),json.dumps({
                                "source":"season",
                                "seasonId":int(receipt["seasonId"]),
                                "seasonEndResult":receipt["seasonEndResult"],
                            })))
                        receipt["unopenedPackId"]=int(pack.lastrowid)
                    updated["rewardReceipt"]=dict(receipt)
                    totals=self.offline_season_totals()
                    totals["completed"]=int(totals.get("completed",0))+1
                    key={"CHAMPIONSHIP":"titles","PROMOTION":"promotions",
                         "RELEGATION":"relegations"}.get(
                             receipt["seasonEndResult"])
                    if key:
                        totals[key]=int(totals.get(key,0))+1
                    self.conn.execute(
                        "UPDATE kv SET value=? WHERE key='offline_season_totals_v1'",
                        (json.dumps(totals),))
                    self.conn.execute(
                        "UPDATE kv SET value=? WHERE key='offline_season_v1'",
                        (json.dumps(updated),))
                self.conn.commit()
                return {**receipt,"credits":credits,"createdNow":created}
            except Exception:
                self.conn.rollback(); raise

    def start_next_offline_season(self):
        """Reset a claimed Season at its promoted/held/relegated division."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='offline_season_v1'"
                ).fetchone()
                state=(json.loads(row["value"]) if row else
                       new_offline_season(10))
                updated=next_offline_season(state)
                for key,value in (("offline_season_v1",updated),
                                  ("season_division",updated["divisionId"])):
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        (key,json.dumps(value)))
                self.conn.commit(); return updated
            except Exception:
                self.conn.rollback(); raise

    def forfeit_offline_season(self):
        """Abandon an unfinished Season and re-open the carousel.

        Retail states the terms on the confirmation itself: forfeiting loses
        all progress made in the current Season and lets a new one start in
        the same division.  It is not a completion, so the history totals and
        the all-time record keep the results already played and gain neither
        a completion nor a relegation.
        """
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='offline_season_v1'"
                ).fetchone()
                state=(json.loads(row["value"]) if row else
                       new_offline_season(10))
                if state.get("complete"):
                    self.conn.rollback(); return None
                division=int(state.get("divisionId",10) or 10)
                updated=new_offline_season(division)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('offline_season_v1',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(updated),))
                self.conn.commit()
                return updated
            except Exception:
                self.conn.rollback(); raise

    def prepare_offline_season_final_test(self):
        """Move an entered test Season to one result before completion."""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='offline_season_v1'"
                ).fetchone()
                state=(json.loads(row["value"]) if row else
                       new_offline_season(10))
                if not state.get("entered"):
                    raise ValueError(
                        "enter an offline Season before preparing the test")
                if state.get("complete"):
                    raise ValueError("the offline Season is already complete")
                final_round=SEASON_MATCH_COUNT-1
                current=max(0,int(state.get("round",0) or 0))
                if current > final_round:
                    raise ValueError(
                        "the offline Season has no playable result left")
                missing=final_round-current
                state.setdefault("results",[]).extend(["LOSS"]*missing)
                state.setdefault("operationKeys",[]).extend(
                    "season-final-test:%d:%d" %
                    (int(state["seasonId"]),number)
                    for number in range(current,final_round))
                state["losses"]=int(state.get("losses",0) or 0)+missing
                state["round"]=final_round
                state["active"]=True
                state["complete"]=False
                state["rewardClaimed"]=False
                state["testPrepared"]=True
                for key in ("seasonEndResult","rewardCoins","nextDivision",
                            "rewardReceipt"):
                    state.pop(key,None)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('offline_season_v1',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(state),))
                self.conn.commit()
                return state
            except Exception:
                self.conn.rollback(); raise

    # ---------- partite offline ----------
    def record_match(self, mode, result, home, away, reward):
        reward=min(MAX_MATCH_COIN_REWARD,max(0,int(reward or 0)))
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                timestamp=now_s()
                normalized=str(result or "LOSS").upper()
                if normalized == "LOSE": normalized="LOSS"
                cursor=self.conn.execute(
                    "INSERT INTO matches(created,mode,result,home_goals,"
                    "away_goals,reward_coins,data) VALUES(?,?,?,?,?,?,?)",
                    (timestamp,str(mode),normalized,int(home),int(away),
                     reward,"{}"))
                match_id=int(cursor.lastrowid)
                match_key="offline:%d" % match_id
                if normalized == "WIN":
                    self._market_increment_tx(self.conn,"season_wins",1)
                elif normalized == "DRAW":
                    self._market_increment_tx(self.conn,"season_draws",1)
                elif normalized == "LOSS":
                    self._market_increment_tx(self.conn,"season_losses",1)
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+reward)
                debuts=self._record_market_player_debuts_tx(
                    mode,match_key,timestamp)
                self.conn.execute(
                    "UPDATE matches SET data=? WHERE id=?",
                    (json.dumps({"matchKey":match_key,
                                 "marketPlayerDebutItemIds":[
                                     row["itemId"] for row in debuts]}),
                     match_id))
                self.conn.commit()
                return {"matchId":match_id,"matchKey":match_key,
                        "credits":credits,"marketPlayerDebuts":debuts}
            except Exception:
                self.conn.rollback(); raise

    def complete_totw_challenge(self,challenge_id,difficulty,result,reward,
                                operation_key,home=0,away=0):
        """Persist one TOTW result and its economy effects exactly once."""
        challenge_id=int(challenge_id); difficulty=int(difficulty)
        reward=min(MAX_MATCH_COIN_REWARD,max(0,int(reward)))
        operation_key=str(operation_key or "").strip()
        normalized=str(result or "LOSS").upper()
        if normalized == "LOSE": normalized="LOSS"
        if challenge_id <= 0 or difficulty not in range(1,7):
            raise ValueError("invalid TOTW challenge or difficulty")
        if normalized not in ("WIN","DRAW","LOSS"):
            raise ValueError("invalid TOTW result")
        if not operation_key:
            raise ValueError("a stable TOTW operation key is required")
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                receipt=self.conn.execute(
                    "SELECT challenge_id,difficulty,result,response FROM "
                    "totw_results WHERE operation_key=?",
                    (operation_key,)).fetchone()
                if receipt is not None:
                    if (int(receipt["challenge_id"]) != challenge_id or
                            int(receipt["difficulty"]) != difficulty or
                            str(receipt["result"]) != normalized):
                        raise ValueError(
                            "TOTW operation key was reused for another result")
                    self.conn.commit(); return json.loads(receipt["response"])
                timestamp=now_s()
                cursor=self.conn.execute(
                    "INSERT INTO matches(created,mode,result,home_goals,"
                    "away_goals,reward_coins,data) VALUES(?,?,?,?,?,?,?)",
                    (timestamp,"TOTW",normalized,int(home),int(away),reward,"{}"))
                match_id=int(cursor.lastrowid)
                match_key="totw:%s" % operation_key
                if normalized == "WIN":
                    self._market_increment_tx(self.conn,"season_wins",1)
                elif normalized == "DRAW":
                    self._market_increment_tx(self.conn,"season_draws",1)
                else:
                    self._market_increment_tx(self.conn,"season_losses",1)
                credits=self._market_set_credits_tx(
                    self.conn,self._market_credits_tx(self.conn)+reward)
                debuts=self._record_market_player_debuts_tx(
                    "TOTW",match_key,timestamp)
                completed_row=self.conn.execute(
                    "SELECT value FROM kv WHERE key='totw_completed_challenges'"
                ).fetchone()
                completed={int(value) for value in
                           (json.loads(completed_row["value"])
                            if completed_row else [])}
                already_completed=challenge_id in completed
                if normalized == "WIN":
                    completed.add(challenge_id)
                    ordered=sorted(completed)
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        ("totw_completed_challenges",json.dumps(ordered)))
                    self.conn.execute(
                        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value",
                        ("totw_completed_count",json.dumps(len(ordered))))
                objective=self._market_increment_tx(
                    self.conn,"objective_totw_matches",1)
                response={"success":True,"challengeId":challenge_id,
                          "difficulty":difficulty,"result":normalized,
                          "completed":normalized == "WIN",
                          "alreadyCompleted":already_completed,
                          "matchId":match_id,"matchKey":match_key,
                          "rewards":{"coins":reward},"credits":credits,
                          "objectiveProgress":objective,
                          "marketPlayerDebuts":debuts,
                          "operationKey":operation_key}
                self.conn.execute(
                    "UPDATE matches SET data=? WHERE id=?",
                    (json.dumps({"matchKey":match_key,
                                 "challengeId":challenge_id,
                                 "difficulty":difficulty,
                                 "operationKey":operation_key,
                                 "marketPlayerDebutItemIds":[
                                     row["itemId"] for row in debuts]}),match_id))
                self.conn.execute(
                    "INSERT INTO totw_results(operation_key,challenge_id,"
                    "difficulty,result,created,response) VALUES(?,?,?,?,?,?)",
                    (operation_key,challenge_id,difficulty,normalized,timestamp,
                     json.dumps(response)))
                self.conn.commit(); return response
            except Exception:
                self.conn.rollback(); raise

    def record(self):
        return {"wins": int(self.get("season_wins",0)), "draws": int(self.get("season_draws",0)),
                "losses": int(self.get("season_losses",0))}

    def player_quality_progress(self):
        """Return durable high-water counts for owned Bronze/Silver/Gold cards.

        Milestones must not become incomplete after a legitimate sale, SBC or
        discard. The current inventory is therefore sampled into a persistent
        high-water mark while the live counts remain available for diagnostics.
        """
        counts={"bronze":0,"silver":0,"gold":0}
        rows=self.conn.execute(
            "SELECT data FROM items WHERE item_kind='player'"
        ).fetchall()
        for row in rows:
            try:
                rating=int(json.loads(row["data"] or "{}").get("rating",0) or 0)
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
            quality="bronze" if rating < 65 else ("silver" if rating < 75 else "gold")
            counts[quality]+=1
        stored=self.get("career_quality_highwater_v1",{}) or {}
        highwater={key:max(int(stored.get(key,0) or 0),value)
                   for key,value in counts.items()}
        if any(int(stored.get(key,0) or 0) != value
               for key,value in highwater.items()):
            self.set("career_quality_highwater_v1",highwater)
        return {"current":counts,"highwater":highwater}

    def objective_progress(self, counter):
        """Resolve one Objective counter from authoritative persistent state."""
        key=str(counter or "")
        record=self.record()
        derived={
            "career_matches_played":sum(record.values()),
            "career_matches_won":record["wins"],
            "career_sbc_completed":int(self.conn.execute(
                "SELECT COUNT(*) FROM sbc_submissions").fetchone()[0]),
            "career_market_sales":int(self.conn.execute(
                "SELECT COUNT(*) FROM auctions WHERE seller_is_user=1 "
                "AND state IN ('sold','cleared')").fetchone()[0]),
            "career_sqbt_matches":int(self.conn.execute(
                "SELECT COUNT(*) FROM sqbt_matches").fetchone()[0]),
            "career_draft_matches":int(self.conn.execute(
                "SELECT COUNT(*) FROM draft_matches").fetchone()[0]),
        }
        if key.startswith("career_players_"):
            quality=key.rsplit("_",1)[-1]
            return int(self.player_quality_progress()["highwater"].get(quality,0))
        if key in derived:
            return int(derived[key])
        return int(self.get(key,0) or 0)

    def career_history(self):
        """Return the persistent all-mode career summary for this account."""
        record=self.record()
        modes={str(row["mode"] or "OFFLINE").upper():int(row["count"])
               for row in self.conn.execute(
                   "SELECT mode,COUNT(*) AS count FROM matches GROUP BY mode"
               ).fetchall()}
        draft_matches=int(self.conn.execute(
            "SELECT COUNT(*) FROM draft_matches").fetchone()[0])
        sqbt_matches=int(self.conn.execute(
            "SELECT COUNT(*) FROM sqbt_matches").fetchone()[0])
        if draft_matches:
            modes["DRAFT"]=draft_matches
        if sqbt_matches:
            modes["SQUAD_BATTLES"]=sqbt_matches
        draft_row=self.conn.execute(
            "SELECT COALESCE(MAX(wins),0) AS best_wins,"
            "COALESCE(SUM(CASE WHEN wins>=4 THEN 1 ELSE 0 END),0) AS champions "
            "FROM draft_sessions").fetchone()
        sqbt_row=self.conn.execute(
            "SELECT COALESCE(MAX(tier),0) AS best_tier,"
            "COALESCE(MIN(CASE WHEN score>0 THEN rank END),0) AS best_rank,"
            "COALESCE(MAX(score),0) AS best_score FROM sqbt_events"
        ).fetchone()
        club_value=0
        from fut_market import fair_market_value
        for row in self.conn.execute(
                "SELECT data FROM items WHERE item_kind='player'").fetchall():
            try:
                club_value+=int(fair_market_value(json.loads(row["data"] or "{}")))
            except (TypeError,ValueError,json.JSONDecodeError):
                continue
        champions=int(draft_row["champions"] or 0)
        trophies=([{"type":"DRAFT_CHAMPION","name":"Draft Champion",
                    "count":champions}] if champions else [])
        quality=self.player_quality_progress()
        return {
            "record":record,
            "matchesPlayed":sum(record.values()),
            "matchesByMode":modes,
            "trophyCount":champions,
            "trophies":trophies,
            "bestPlacement":{
                "draftWins":int(draft_row["best_wins"] or 0),
                "squadBattlesTier":int(sqbt_row["best_tier"] or 0),
                "squadBattlesRank":int(sqbt_row["best_rank"] or 0),
                "squadBattlesScore":int(sqbt_row["best_score"] or 0),
            },
            "clubValue":max(0,int(club_value)),
            "playerQuality":quality,
            "packOpenings":int(self.conn.execute(
                "SELECT COUNT(*) FROM pack_openings").fetchone()[0]),
            "sbcCompleted":int(self.conn.execute(
                "SELECT COUNT(*) FROM sbc_submissions").fetchone()[0]),
            "marketSales":int(self.conn.execute(
                "SELECT COUNT(*) FROM auctions WHERE seller_is_user=1 "
                "AND state IN ('sold','cleared')").fetchone()[0]),
        }

    # ---------- SBC ----------
    def reset_sbc_progress(self, set_id=None):
        """Forget the progress of one SBC set, or of every set.

        A completed challenge cannot be reopened, so a set that has been
        finished once can never be tested again without this. Rewards already
        granted are left in the club: only the progress is cleared.
        """
        with self.lock:
            if set_id is None:
                cleared=self.conn.execute(
                    "SELECT COUNT(*) FROM sbc").fetchone()[0]
                self.conn.execute("DELETE FROM sbc")
                self.conn.execute("DELETE FROM sbc_submissions")
                self.conn.execute("DELETE FROM sbc_set_rewards")
                self.conn.execute("DELETE FROM sbc_set_attempt_rewards")
            else:
                target=int(set_id)
                cleared=self.conn.execute(
                    "SELECT COUNT(*) FROM sbc WHERE set_id=?",
                    (target,)).fetchone()[0]
                for statement in (
                        "DELETE FROM sbc WHERE set_id=?",
                        "DELETE FROM sbc_submissions WHERE set_id=?",
                        "DELETE FROM sbc_set_rewards WHERE set_id=?",
                        "DELETE FROM sbc_set_attempt_rewards WHERE set_id=?"):
                    self.conn.execute(statement,(target,))
            self.conn.commit()
        return int(cleared)

    def sbc_status(self):
        rows = self.conn.execute("SELECT set_id,challenge_id,status,data FROM sbc").fetchall()
        return [{"setId": r["set_id"], "challengeId": r["challenge_id"], "status": r["status"],
                 **json.loads(r["data"])} for r in rows]

    def sbc_set_reward_receipt(self,set_id,cycle=None):
        """Recover the durable final-set reward response without regranting it."""
        set_id=int(set_id)
        if cycle is None:
            row=self.conn.execute(
                "SELECT cycle,created,response FROM sbc_set_attempt_rewards "
                "WHERE set_id=? ORDER BY cycle DESC LIMIT 1",(set_id,)).fetchone()
            if row is None:
                row=self.conn.execute(
                    "SELECT 1 AS cycle,created,response FROM sbc_set_rewards "
                    "WHERE set_id=?",(set_id,)).fetchone()
        else:
            row=self.conn.execute(
                "SELECT cycle,created,response FROM sbc_set_attempt_rewards "
                "WHERE set_id=? AND cycle=?",(set_id,int(cycle))).fetchone()
        if row is None:
            return None
        return {"setId":set_id,"cycle":int(row["cycle"]),
                "created":int(row["created"]),
                **json.loads(row["response"] or "{}")}

    def set_sbc(self, set_id, challenge_id, status, data=None):
        with self.lock:
            self.conn.execute("INSERT INTO sbc(set_id,challenge_id,status,data) VALUES(?,?,?,?) "
                "ON CONFLICT(set_id,challenge_id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (int(set_id), int(challenge_id), status, json.dumps(data or {})))
            self.conn.commit()

    def mark_sbc_reward_claimed(self,set_id,challenge_id=None,attempt=None):
        """Acknowledge a completed SBC reward without erasing its history."""
        set_id=int(set_id)
        challenge=(int(challenge_id) if challenge_id is not None else None)
        expected_attempt=(int(attempt) if attempt is not None else None)
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                if challenge is None:
                    rows=self.conn.execute(
                        "SELECT challenge_id,status,data FROM sbc WHERE set_id=?",
                        (set_id,)).fetchall()
                else:
                    rows=self.conn.execute(
                        "SELECT challenge_id,status,data FROM sbc WHERE set_id=? "
                        "AND challenge_id=?",(set_id,challenge)).fetchall()
                changed=0; claimed_at=now_s()
                for row in rows:
                    if str(row["status"]).upper() != "COMPLETED":
                        continue
                    data=json.loads(row["data"] or "{}")
                    current_attempt=int(data.get("currentAttempt",0) or 0)
                    if (expected_attempt is not None and current_attempt and
                            current_attempt != expected_attempt):
                        continue
                    data["claimedAt"]=claimed_at
                    self.conn.execute(
                        "UPDATE sbc SET status='CLAIMED',data=? WHERE set_id=? "
                        "AND challenge_id=?",
                        (json.dumps(data),set_id,int(row["challenge_id"])))
                    changed+=1
                self.conn.commit()
                return changed
            except Exception:
                self.conn.rollback(); raise

    @staticmethod
    def _sbc_clean_consumed_refs(payload, consumed_ids):
        """Remove consumed owned-instance references while preserving slots."""
        consumed_ids={int(value) for value in consumed_ids}
        data=json.loads(json.dumps(payload or {}))

        def clean(value):
            if isinstance(value,list):
                return [clean(entry) for entry in value]
            if not isinstance(value,dict):
                return value
            # Inspect the original row before recursively cleaning itemData;
            # otherwise the nested owned item becomes an empty dict and its ID
            # is no longer available to clear the surrounding positional row.
            item_data=value.get("itemData")
            candidate=0
            if isinstance(item_data,dict):
                candidate=int(item_data.get("id",0) or 0)
            if not candidate:
                candidate=int(value.get("itemId",value.get("id",0)) or 0)
            if candidate in consumed_ids:
                # Native squad lists are positional.  Keep visual metadata and
                # turn the row into an empty slot instead of collapsing it.
                return {key:value[key] for key in
                        ("index","kitNumber","position","slot") if key in value}
            result={key:clean(entry) for key,entry in value.items()}
            if int(result.get("captain",0) or 0) in consumed_ids:
                result["captain"]=0
            return result

        return clean(data)

    @staticmethod
    def _sbc_counter_tx(conn,key,amount=1):
        row=conn.execute("SELECT value FROM kv WHERE key=?",(str(key),)).fetchone()
        try:
            current=int(json.loads(row["value"])) if row else 0
        except (TypeError,ValueError,json.JSONDecodeError):
            current=0
        updated=max(0,current+int(amount))
        conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value",(str(key),json.dumps(updated)))
        return updated

    def save_sbc_squad(self,set_id,challenge_id,squad,repeatable=False,
                       assignment_item_ids=None):
        """Persist an incomplete SBC squad without consuming any item."""
        set_id=int(set_id); challenge_id=int(challenge_id)
        compact={"players":list((squad or {}).get("players",[]) or [])}
        for key in ("id","squadName","formation","manager","coachId",
                    "chemistry","rating"):
            if key in (squad or {}):
                compact[key]=(squad or {})[key]
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row=self.conn.execute(
                    "SELECT status,data FROM sbc WHERE set_id=? AND challenge_id=?",
                    (set_id,challenge_id)).fetchone()
                previous=json.loads(row["data"]) if row else {}
                previous_status=str(row["status"]) if row else "NOT_STARTED"
                if row and previous_status == "COMPLETED" and not repeatable:
                    # Completed ordinary challenges are immutable. Repeatable
                    # challenges, however, receive their reward atomically during
                    # submit and may immediately start the next isolated attempt;
                    # an unopened pack/pick remains independently unassigned.
                    self.conn.commit()
                    return {"status":previous_status,**previous}
                if row and previous_status == "CLAIMED" and not repeatable:
                    self.conn.commit()
                    return {"status":previous_status,**previous}

                # A won card returned by Player Search can sit in Work Area
                # (index > 10). The workspace normalizer correctly excludes it
                # from the SBC XI, but the raw payload is still the user's
                # explicit assignment decision and must clear Claim New Items.
                item_ids=sorted(set(extract_item_ids(compact)) | {
                    int(value) for value in (assignment_item_ids or [])
                    if int(value or 0)>0})
                if item_ids:
                    placeholders=",".join("?" for _ in item_ids)
                    purchased=self.conn.execute(
                        "SELECT id,data FROM items WHERE item_kind='player' "
                        "AND pile='purchased' AND id IN (%s)" % placeholders,
                        item_ids).fetchall()
                    assigned_at=now_s()
                    for purchased_row in purchased:
                        item_id=int(purchased_row["id"])
                        item=json.loads(purchased_row["data"])
                        item["pile"]="club"
                        self.conn.execute(
                            "UPDATE items SET pile='club',data=? WHERE id=?",
                            (json.dumps(item),item_id))
                        # Selecting a won market card in an SBC is the user's
                        # assignment decision. Closing its target in this same
                        # transaction prevents the next screen from reloading
                        # the already-handled purchase as an expired listing.
                        self._clear_stale_won_market_targets_tx(
                            assigned_at,item_id=item_id)

                completion_count=int(previous.get("completionCount",0) or 0)
                current_attempt=int(previous.get("currentAttempt",0) or 0)
                if (not current_attempt or
                        previous_status in {"CLAIMED","COMPLETED"}):
                    current_attempt=completion_count+1
                data={"squad":compact,"updatedAt":now_s(),
                      "completionCount":completion_count,
                      "currentAttempt":current_attempt}
                self.conn.execute(
                    "INSERT INTO sbc(set_id,challenge_id,status,data) VALUES(?,?,?,?) "
                    "ON CONFLICT(set_id,challenge_id) DO UPDATE SET "
                    "status=excluded.status,data=excluded.data",
                    (set_id,challenge_id,"IN_PROGRESS",json.dumps(data)))
                self.conn.commit()
                return {"status":"IN_PROGRESS",**data}
            except Exception:
                self.conn.rollback()
                raise

    def _sbc_manager_tx(self,squad):
        """Resolve an optional SBC manager from owned state, never client data."""
        raw=(squad or {}).get("manager")
        if isinstance(raw,list):
            raw=raw[0] if raw else None
        if isinstance(raw,dict) and isinstance(raw.get("itemData"),dict):
            raw=raw["itemData"]
        manager_id=0
        if isinstance(raw,dict):
            manager_id=int(raw.get("id",raw.get("itemId",0)) or 0)
        elif raw is not None:
            try: manager_id=int(raw or 0)
            except (TypeError,ValueError): manager_id=0
        if not manager_id:
            manager_id=int((squad or {}).get("coachId",0) or 0)
        if not manager_id:
            return None
        row=self.conn.execute(
            "SELECT pile,item_kind,data FROM items WHERE id=?",(manager_id,)
        ).fetchone()
        if (not row or str(row["pile"]) != "club" or
                str(row["item_kind"]) != "manager"):
            raise SbcValidationError(
                "The selected SBC manager must be owned in My Club.")
        return manager_item_dto(json.loads(row["data"]))

    def preview_sbc_submission(self,challenge_spec,squad):
        """Evaluate saved SBC work against the same owned state as submit."""
        squad=squad if isinstance(squad,dict) else {}
        item_ids=extract_item_ids(squad)
        if not item_ids or len(item_ids) != len(set(item_ids)):
            return {"valid":False,"chemistry":0,"rating":0}
        with self.lock:
            placeholders=",".join("?" for _ in item_ids)
            rows=self.conn.execute(
                "SELECT id,pile,item_kind,data FROM items WHERE id IN (%s)" %
                placeholders,item_ids).fetchall()
            by_id={int(row["id"]):row for row in rows}
            if len(by_id) != len(item_ids):
                return {"valid":False,"chemistry":0,"rating":0}
            items=[]
            for item_id in item_ids:
                row=by_id[item_id]
                item=json.loads(row["data"])
                if (str(row["pile"]) != "club" or
                        str(row["item_kind"]) != "player" or
                        bool(item.get("isLoan",False)) or
                        int(item.get("loans",0) or 0)>0):
                    return {"valid":False,"chemistry":0,"rating":0}
                items.append(item)
            rating=fut_squad_rating(items)
            chemistry=0
            try:
                manager=self._sbc_manager_tx(squad)
                chemistry_result=submission_chemistry(
                    challenge_spec,squad,items,manager=manager)
                chemistry=int(chemistry_result.get("total",0) or 0)
                validate_challenge_submission(
                    challenge_spec,squad,items,chemistry=chemistry,
                    manager=manager)
                valid=True
            except SbcValidationError:
                # RC101 let a chemistry-62 David Luiz-derived squad reach
                # submit because its saved workspace advertised valid/100.
                # The preview must fail closed on the same validation boundary.
                valid=False
            return {"valid":valid,"chemistry":chemistry,"rating":rating}

    @staticmethod
    def _sbc_reward_award(reward):
        kind=str(reward.get("type","")).lower()
        value=int(reward.get("value",reward.get("packId",0)) or 0)
        count=max(1,int(reward.get("count",reward.get("quantity",1)) or 1))
        return {"type":kind,"value":value,"count":count}

    def _grant_sbc_rewards_tx(self,rewards,source):
        """Grant supported rewards through the caller's open transaction."""
        awards=[]; unopened_ids=[]; granted_item_ids=[]; player_picks=[]
        for reward_index,reward in enumerate(rewards or []):
            award=self._sbc_reward_award(reward)
            kind=award["type"]
            if kind == "pack":
                if award["value"] <= 0:
                    raise ValueError("SBC pack reward has no verified pack ID")
                for _ in range(award["count"]):
                    pack_source=dict(source)
                    pack_source["untradeable"]=bool(
                        reward.get("untradeable",False))
                    cursor=self.conn.execute(
                        "INSERT INTO unopened_packs(pack_id,data) VALUES(?,?)",
                        (award["value"],json.dumps(pack_source)))
                    unopened_ids.append(int(cursor.lastrowid))
            elif kind == "coin":
                self._sbc_counter_tx(self.conn,"credits",
                                     award["value"]*award["count"])
            elif kind == "consumable":
                pool=sorted({int(value) for value in
                             reward.get("resourcePool",[]) if int(value)>0})
                if not pool:
                    raise ValueError("SBC consumable reward has no verified pool")
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                for offset in range(award["count"]):
                    selector=("%s:%d:%d" % (
                        source.get("operationKey",""),reward_index,offset))
                    choice=int(hashlib.sha256(selector.encode("utf-8")).hexdigest(),16)
                    resource_id=pool[choice % len(pool)]
                    definition=object_definition(resource_id)
                    if not definition or definition.get("_type") != "consumable":
                        raise ValueError("SBC consumable pool references an unknown item")
                    item=consumable_item_dto({**definition,
                        "untradeable":bool(reward.get("untradeable",True)),
                        "inventoryType":"consumable","pile":"club"})
                    item_id=next_id+offset
                    item.update({"id":item_id,"itemId":item_id,"pile":"club",
                                 "timestamp":now_s(),"inventoryType":"consumable",
                                 "tradeable":not bool(item.get("untradeable",True))})
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (item_id,resource_id,"club","consumable",json.dumps(item)))
                    granted_item_ids.append(item_id)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id+award["count"]),))
            elif kind in ("player_pick","playerpick"):
                from fut_packs import generate_player_pick_options
                pick_ids=[]
                for offset in range(award["count"]):
                    source_key="%s:reward:%d:%d" % (
                        source.get("operationKey",""),reward_index,offset)
                    seed=int(hashlib.sha256(source_key.encode("utf-8")).hexdigest(),16)
                    if str(reward.get("rewardPool","")).upper() == \
                            "FUT_CHAMPIONS_TOTW":
                        # FUT Champions items reuse authentic TOTW identities
                        # but need EA rarity 18; the shared Champions helper is
                        # the only source-backed path that preserves both.
                        from fut_sqbt import fut_champions_pick_spec
                        pick_spec=fut_champions_pick_spec(
                            seed,reward_index,offset,
                            option_count=int(reward.get("optionCount",3) or 3),
                            min_rating=int(reward.get("minRating",75) or 75))
                    else:
                        pick_spec=generate_player_pick_options(
                            min_rating=int(reward.get("minRating",75) or 75),
                            option_count=int(reward.get("optionCount",3) or 3),
                            seed=seed,pack_id=0,
                            special_chance=float(reward.get("specialChance",0) or 0),
                            position=reward.get("position"),
                            quality=reward.get("quality"),
                            resource_ids=reward.get("resourceIds"),
                            rare_only=bool(reward.get("rareOnly",False)))
                    pick_spec.update({
                        "name":str(reward.get("label","Player Pick")),
                        "description":str(reward.get("description","")),
                        "rewardSource":dict(source)})
                    # SBC submission receipts are the idempotency boundary.
                    # RESET_SBC preserves earned picks while deleting those
                    # receipts and restarting attempt numbering, so sourceKey
                    # alone cannot identify a new post-reset completion.
                    pick=self._grant_player_pick_tx(
                        source_key,pick_spec,deduplicate=False)
                    player_picks.append(pick)
                    pick_ids.append(int(pick["playerPickId"]))
                award={"type":"playerPick","value":pick_ids[0],
                       "count":len(pick_ids),"playerPickId":pick_ids[0],
                       "playerPickIds":pick_ids,
                       "minRating":int(reward.get("minRating",75) or 75),
                       "optionCount":int(reward.get("optionCount",3) or 3)}
            elif kind == "player":
                resource_id=int(reward.get("resourceId",award["value"]) or 0)
                loan_games=max(0,int(reward.get("loan",0) or 0))
                if resource_id <= 0:
                    raise ValueError("SBC player reward has no resource ID")
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                for offset in range(award["count"]):
                    item=native_player_fields(resource_id,{
                        "untradeable":bool(reward.get("untradeable",True)),
                        "loan":bool(loan_games),"loans":loan_games,
                        "itemState":"free","pile":"purchased"})
                    item_id=next_id+offset
                    item.update({"id":item_id,"itemId":item_id,
                                 "timestamp":now_s(),"pile":"purchased"})
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (item_id,resource_id,"purchased","player",json.dumps(item)))
                    granted_item_ids.append(item_id)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id+award["count"]),))
                award.update({"resourceId":resource_id,"value":resource_id,
                              "loan":loan_games,
                              "untradeable":bool(reward.get("untradeable",True)),
                              # The native submit receipt needs the granted
                              # inventory identity, not a display-only item
                              # whose id equals the definition.  This lets the
                              # completion screen enter Unassigned immediately.
                              "itemData":item})
            elif kind == "club_item":
                team_id=int(reward.get("teamId",47) or 47)
                category=int(reward.get("category",2) or 2)
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                for offset in range(award["count"]):
                    item=kit_item_dto({"teamid":team_id,"category":category,
                        "rating":75,"rareflag":1,"untradeable":True,
                        "pile":"club","inventoryType":"kit"})
                    item_id=next_id+offset
                    item.update({"id":item_id,"itemId":item_id,"pile":"club",
                                 "timestamp":now_s(),"inventoryType":"kit",
                                 "tradeable":False})
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (item_id,int(item["resourceId"]),"club","kit",json.dumps(item)))
                    granted_item_ids.append(item_id)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id+award["count"]),))
                award={"type":"clubItem","value":int(item["resourceId"]),
                       "resourceId":int(item["resourceId"]),
                       "count":award["count"],"untradeable":True}
            elif kind in ("item","object"):
                resource_id=int(reward.get("resourceId",award["value"]) or 0)
                definition=object_definition(resource_id)
                if not definition:
                    raise ValueError("SBC item reward references an unknown object")
                definition_type=str(definition.get("_type",""))
                dto_by_type={
                    "manager":manager_item_dto,"badge":badge_item_dto,
                    "stadium":stadium_item_dto,"ball":ball_item_dto,
                    "consumable":consumable_item_dto,
                    "headcoach":staff_item_dto,"gkcoach":staff_item_dto,
                    "fitnesscoach":staff_item_dto,"physio":staff_item_dto,
                }
                dto_factory=dto_by_type.get(definition_type)
                if dto_factory is None:
                    raise ValueError("SBC item reward has no native object DTO")
                item_kind=("staff" if definition_type in {
                    "headcoach","gkcoach","fitnesscoach","physio"}
                    else definition_type)
                next_id=int(self.get("next_item_id",100000000000) or 100000000000)
                untradeable=bool(reward.get("untradeable",True))
                for offset in range(award["count"]):
                    item=dto_factory({**definition,"untradeable":untradeable,
                                      "pile":"club"})
                    item_id=next_id+offset
                    item.update({"id":item_id,"itemId":item_id,"pile":"club",
                                 "timestamp":now_s(),"inventoryType":item_kind,
                                 "tradeable":not untradeable,
                                 "untradeable":untradeable})
                    self.conn.execute(
                        "INSERT INTO items(id,resource_id,pile,item_kind,data) "
                        "VALUES(?,?,?,?,?)",
                        (item_id,resource_id,"club",item_kind,json.dumps(item)))
                    granted_item_ids.append(item_id)
                self.conn.execute(
                    "INSERT INTO kv(key,value) VALUES('next_item_id',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(next_id+award["count"]),))
                award={"type":"item","value":resource_id,
                       "resourceId":resource_id,"count":award["count"],
                       "untradeable":untradeable}
            else:
                raise ValueError("unsupported atomic SBC reward type: %s" % kind)
            complete=dict(award)
            if kind == "pack" and "untradeable" in reward:
                complete["untradeable"]=bool(reward["untradeable"])
            if kind == "consumable":
                complete["untradeable"]=bool(reward.get("untradeable",True))
                complete["resourcePool"]=[int(value) for value in
                                           reward.get("resourcePool",[])]
            awards.append(complete)
        return awards,unopened_ids,granted_item_ids,player_picks

    def submit_sbc(self,set_spec,challenge_spec,squad=None,operation_key=None,
                   fail_after=None):
        """Validate, consume, progress, reward and receipt one SBC atomically."""
        set_id=int(set_spec.get("setId",0) or 0)
        challenge_id=int(challenge_spec.get("challengeId",0) or 0)
        if not set_id or not challenge_id or int(challenge_spec.get("setId",set_id)) != set_id:
            raise ValueError("invalid SBC set/challenge identity")
        repeatable=bool(challenge_spec.get("repeatable",False))
        if repeatable:
            contract=challenge_spec.get("attemptContract")
            if (not isinstance(contract,dict) or
                    str(contract.get("mode","")) != "saved_squad_attempt" or
                    str(contract.get("retryScope","")) != "operation_key"):
                raise ValueError(
                    "repeatable SBC attempts require a verified attempt contract")
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                progress=self.conn.execute(
                    "SELECT status,data FROM sbc WHERE set_id=? AND challenge_id=?",
                    (set_id,challenge_id)).fetchone()
                saved=json.loads(progress["data"]) if progress else {}
                progress_status=str(progress["status"]) if progress else "NOT_STARTED"
                completion_count=int(saved.get("completionCount",0) or 0)
                direct_ids=extract_item_ids(squad or {})
                attempt=int(saved.get("currentAttempt",0) or 0)
                if not attempt:
                    attempt=completion_count+1
                if (repeatable and
                        progress_status in {"COMPLETED","CLAIMED"}):
                    if progress_status == "COMPLETED" or not direct_ids:
                        previous=self.conn.execute(
                            "SELECT response FROM sbc_submissions WHERE set_id=? AND "
                            "challenge_id=? ORDER BY attempt DESC LIMIT 1",
                            (set_id,challenge_id)).fetchone()
                        if previous:
                            self.conn.commit()
                            return json.loads(previous["response"])
                    attempt=completion_count+1
                if not repeatable:
                    attempt=1
                operation_key=str(operation_key or "sbc:%d:%d:%d" % (
                    set_id,challenge_id,attempt))
                receipt=self.conn.execute(
                    "SELECT set_id,challenge_id,response FROM sbc_submissions "
                    "WHERE operation_key=?",
                    (operation_key,)).fetchone()
                if receipt:
                    if (int(receipt["set_id"]) != set_id or
                            int(receipt["challenge_id"]) != challenge_id):
                        raise ValueError("SBC operation key belongs to another challenge")
                    self.conn.commit()
                    return json.loads(receipt["response"])
                previous=self.conn.execute(
                    "SELECT response FROM sbc_submissions WHERE set_id=? AND "
                    "challenge_id=? AND attempt=? ORDER BY created LIMIT 1",
                    (set_id,challenge_id,attempt)).fetchone()
                if previous:
                    self.conn.commit()
                    return json.loads(previous["response"])
                submitted=squad if extract_item_ids(squad or {}) else saved.get("squad",{})
                item_ids=extract_item_ids(submitted)
                if not item_ids:
                    raise SbcValidationError("No owned players were submitted.")
                if len(item_ids) != len(set(item_ids)):
                    raise SbcValidationError(
                        "The submitted squad contains a duplicate item instance.")
                placeholders=",".join("?" for _ in item_ids)
                rows=self.conn.execute(
                    "SELECT id,pile,item_kind,data FROM items WHERE id IN (%s)" % placeholders,
                    item_ids).fetchall()
                by_id={int(row["id"]):row for row in rows}
                if len(by_id) != len(item_ids):
                    raise SbcValidationError(
                        "Every submitted player must be owned in My Club.")
                items=[]
                for item_id in item_ids:
                    row=by_id[item_id]
                    item=json.loads(row["data"])
                    if str(row["pile"]) != "club" or str(row["item_kind"]) != "player":
                        raise SbcValidationError(
                            "Every submitted player must be owned in My Club.")
                    if bool(item.get("isLoan",False)) or int(item.get("loans",0) or 0)>0:
                        raise SbcValidationError("Loan players cannot be submitted to an SBC.")
                    items.append(item)
                manager=self._sbc_manager_tx(submitted)
                results=validate_challenge_submission(
                    challenge_spec,submitted,items,
                    chemistry=_submitted_squad_chemistry(submitted),
                    manager=manager)
                if fail_after == "validation":
                    raise RuntimeError("injected SBC failure after validation")

                source={"source":"sbc","setId":set_id,
                        "challengeId":challenge_id,"attempt":attempt,
                        "operationKey":operation_key}
                awards,unopened_ids,granted_item_ids,player_picks=\
                    self._grant_sbc_rewards_tx(
                    challenge_spec.get("rewards",[]),source)
                if fail_after == "rewards":
                    raise RuntimeError("injected SBC failure after rewards")

                self.conn.execute("DELETE FROM items WHERE id IN (%s)" % placeholders,
                                  item_ids)
                consumed=set(item_ids)
                for row in self.conn.execute("SELECT id,data FROM squads").fetchall():
                    payload=json.loads(row["data"])
                    cleaned=self._sbc_clean_consumed_refs(payload,consumed)
                    if cleaned != payload:
                        self.conn.execute("UPDATE squads SET data=? WHERE id=?",
                                          (json.dumps(cleaned),int(row["id"])))
                for row in self.conn.execute(
                        "SELECT set_id,challenge_id,data FROM sbc").fetchall():
                    payload=json.loads(row["data"])
                    cleaned=self._sbc_clean_consumed_refs(payload,consumed)
                    if cleaned != payload:
                        self.conn.execute(
                            "UPDATE sbc SET data=? WHERE set_id=? AND challenge_id=?",
                            (json.dumps(cleaned),int(row["set_id"]),
                             int(row["challenge_id"])))

                updated_completion_count=completion_count+1
                completed_data={"squad":self._sbc_clean_consumed_refs(submitted,consumed),
                                "completionCount":updated_completion_count,
                                "currentAttempt":attempt,"completedAt":now_s(),
                                "operationKey":operation_key,
                                "validation":results}
                set_challenges=list(set_spec.get("challenges",[]) or [])
                repeatable_group_cycle=(
                    repeatable and bool(set_spec.get("repeatable",False)) and
                    len(set_challenges)>1 and
                    bool(list(set_spec.get("rewards",[]) or [])))
                # A one-challenge repeatable must reopen immediately after
                # submit; that same-session flow is live-confirmed. In a
                # multi-challenge repeatable group, reopening child one before
                # child two is submitted loses the native 1/2 completion state
                # and threw the tester out of FUT. The final set Award is the
                # only boundary that may reset the complete group cycle.
                visible_status=("COMPLETED" if repeatable_group_cycle else
                                ("CLAIMED" if repeatable else "COMPLETED"))
                self.conn.execute(
                    "INSERT INTO sbc(set_id,challenge_id,status,data) VALUES(?,?,?,?) "
                    "ON CONFLICT(set_id,challenge_id) DO UPDATE SET "
                    "status=excluded.status,data=excluded.data",
                    (set_id,challenge_id,visible_status,
                     json.dumps(completed_data)))

                group_awards=[]; group_pack_ids=[]; group_item_ids=[]
                group_player_picks=[]
                challenge_ids=[int(row.get("challengeId",0) or 0)
                               for row in set_spec.get("challenges",[])]
                if challenge_ids and all(challenge_ids):
                    marks=",".join("?" for _ in challenge_ids)
                    progress_rows=self.conn.execute(
                        "SELECT challenge_id,status,data FROM sbc WHERE set_id=? "
                        "AND challenge_id IN (%s)" % marks,
                        [set_id]+challenge_ids).fetchall()
                    progress_by_id={int(row["challenge_id"]):row
                                    for row in progress_rows}
                    child_counts=[]
                    for child_id in challenge_ids:
                        child=progress_by_id.get(child_id)
                        child_data=json.loads(child["data"]) if child else {}
                        child_counts.append(int(child_data.get(
                            "completionCount",0) or 0))
                    group_cycle=(min(child_counts) if child_counts and
                                 all(value>0 for value in child_counts) else 0)
                    if not bool(set_spec.get("repeatable",False)) and group_cycle:
                        group_cycle=1
                    existing_group=None
                    if group_cycle:
                        existing_group=self.conn.execute(
                            "SELECT response FROM sbc_set_attempt_rewards WHERE "
                            "set_id=? AND cycle=?",(set_id,group_cycle)).fetchone()
                        if not bool(set_spec.get("repeatable",False)):
                            existing_group=(existing_group or self.conn.execute(
                                "SELECT response FROM sbc_set_rewards WHERE set_id=?",
                                (set_id,)).fetchone())
                    if group_cycle and not existing_group:
                        (group_awards,group_pack_ids,group_item_ids,
                         group_player_picks)=self._grant_sbc_rewards_tx(
                            set_spec.get("rewards",[]),{
                                "source":"sbc_set","setId":set_id,
                                "challengeId":challenge_id,
                                "cycle":group_cycle,
                                "operationKey":operation_key})
                        group_response={"awards":group_awards,
                                        "unopenedPackIds":group_pack_ids,
                                        "playerPicks":group_player_picks,
                                        "cycle":group_cycle}
                        self.conn.execute(
                            "INSERT INTO sbc_set_attempt_rewards(set_id,cycle,created,"
                            "response) VALUES(?,?,?,?)",
                            (set_id,group_cycle,now_s(),json.dumps(group_response)))
                        if not bool(set_spec.get("repeatable",False)):
                            self.conn.execute(
                                "INSERT OR IGNORE INTO sbc_set_rewards(set_id,created,"
                                "response) VALUES(?,?,?)",
                                (set_id,now_s(),json.dumps(group_response)))

                self._sbc_counter_tx(self.conn,"objective_sbc_completed",1)
                response={"challengeId":challenge_id,"setId":set_id,
                          "status":"COMPLETED","completed":True,
                          "attempt":attempt,
                          "completionCount":updated_completion_count,
                          "alreadyCompleted":False,"awards":awards,
                          "groupAwards":group_awards,
                          "consumedItemIds":item_ids,
                          "unopenedPackIds":unopened_ids+group_pack_ids,
                          "grantedItemIds":granted_item_ids+group_item_ids,
                          "playerPicks":player_picks+group_player_picks,
                          "credits":int(self.get("credits",0) or 0),
                          "validation":results,
                          "operationKey":operation_key}
                self.conn.execute(
                    "INSERT INTO sbc_submissions(operation_key,set_id,challenge_id,"
                    "attempt,created,consumed_item_ids,rewards,response) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (operation_key,set_id,challenge_id,attempt,now_s(),json.dumps(item_ids),
                     json.dumps(awards+group_awards),json.dumps(response)))
                if fail_after == "receipt":
                    raise RuntimeError("injected SBC failure after receipt")
                self.conn.commit()
                return response
            except Exception:
                self.conn.rollback()
                raise

    # ---------- backup ----------
    def backup(self):
        base = self.path + ".backup-" + str(now_s())
        dst=base
        suffix=1
        while os.path.exists(dst):
            dst=base+"-"+str(suffix)
            suffix+=1
        try:
            with self.lock:
                target = sqlite3.connect(dst)
                try:
                    self.conn.backup(target)
                finally:
                    target.close()
            return dst
        except Exception: return None

    def reset_account(self):
        """Reset the complete local FUT profile after creating a backup.

        Unlike the club-only reset, this also clears onboarding, balance,
        inventory, objectives, packs, matches, SBC state, and session identity.
        """
        preserved_mode=self.account_mode()
        self._defaults["account_mode"]=preserved_mode
        if preserved_mode == ACCOUNT_MODE_RTG:
            self._defaults.update({
                "credits":0,"club_name":"FUT Deba RTG","club_abbr":"RTG",
                "persona_name":"FUT Deba RTG",
            })
        backup_path=self.backup()
        if not backup_path:
            raise RuntimeError("profile backup failed; reset cancelled")
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for table in ("items","squads","unopened_packs","auctions",
                              "matches","totw_results","sbc","pack_openings","player_picks",
                              "inventory_grants","draft_sessions","draft_picks",
                              "draft_matches","sqbt_events","sqbt_opponents",
                              "sqbt_matches","champion_sessions",
                              "champion_opponents","champion_matches",
                              "market_targets",
                              "market_operations","sbc_submissions",
                              "sbc_set_rewards","sbc_set_attempt_rewards"):
                    self.conn.execute("DELETE FROM %s" % table)
                self.conn.execute("DELETE FROM kv")
                self.conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name IN "
                    "('unopened_packs','matches','pack_openings','draft_sessions',"
                    "'champion_sessions')")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            self._init()
        return backup_path

    def close(self):
        with self.lock:
            self.conn.close()


# Starter club: use only resource IDs found in the static local catalogue.
def seed_starter_club(state, n=23):
    # Non-player grants may already exist on a migrated account. Only player
    # rows participate in legacy starter-seed detection.
    existing = state.items_in_pile(item_kind="player")
    placeholder_ids = set(range(100000, 100023))
    old_psg_ids = {
        1179, 146530, 207865, 164240, 205069, 199556, 210008, 183898,
        231747, 179813, 190871, 193105, 188943, 202371, 225850, 201510,
        226229, 202166, 226226, 232411, 163711, 183569, 241496,
    }
    # The first Italian starter seed used 241096, the only item without complete
    # catalogue attributes. Match that exact seed so customized clubs are never
    # overwritten.
    old_italia_ids = {
        228413, 228881, 237383, 229582, 202884, 233556, 222077, 225439,
        229857, 237715, 215689, 205659, 223329, 226108, 243237, 211361,
        212096, 219995, 201389, 236610, 202848, 241096, 203668,
    }
    existing_resource_ids = {int(x.get("resourceId", 0) or 0) for x in existing}
    if existing:
        # Migrate only exact legacy development seeds. Never touch a club whose
        # items were added or modified by the user.
        if len(existing) == 23 and existing_resource_ids in (
                placeholder_ids, old_psg_ids, old_italia_ids):
            state.backup()
            with state.lock:
                state.conn.execute("DELETE FROM items")
                state.conn.execute("DELETE FROM squads")
                state.conn.commit()
        else:
            return

    missing = validate_starter_catalogue()
    if missing:
        raise RuntimeError("starter resource IDs missing from local catalogue: %s" % missing)

    players = []
    for spec in STARTER_PLAYERS[:max(0, min(int(n), len(STARTER_PLAYERS)))]:
        rid = int(spec["resourceId"])
        extra = dict(spec)
        extra.pop("resourceId", None)
        extra["untradeable"] = True
        item = state.add_item(rid, pile="club", extra=extra)
        players.append(item["id"])

    squad = {"id": 1, "squadName": "Starter Italy", "formation": "f442", "players": [
        {"itemData": {"id": pid}, "index": idx, "kitNumber": idx + 1}
        for idx, pid in enumerate(players[:23])],
        "chemistry": 100, "rating": 73, "starRating": 3,
        "squadType": "REGULAR_SQUAD", "newSquad": False}
    saved = state.save_squad(squad)
    state.conn.execute("UPDATE squads SET active=1 WHERE id=?", (saved["id"],))
    state.conn.commit()


if __name__ == "__main__":
    st = FutState()
    seed_starter_club(st)
    print("DB:", st.path)
    print("coins:", st.credits())
    print("club:", st.club_name(), st.club_abbr())
    print("club items:", len(st.items_in_pile("club")))
    print("squads:", [s["squadName"] for s in st.squads()])

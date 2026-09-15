#!/usr/bin/env python3
# Local FIFA 19 FUT account utility: balances, club reset, and account info.
# Usage: python fut_tools.py <command> [value]
#   info                 show local account state
#   addcoins <n>         add n coins
#   setcoins <n>         set the coin balance to n
#   adddrafttokens <n>   add n local Draft Tokens
#   reset                reset club/squads with a backup; preserve onboarding
#   resetaccount         reset the complete account and reopen FUT onboarding
#   resetsbc [setId]     forget SBC progress so a finished set can be replayed
#   prepareseasonfinal   back up and leave one offline Season match to play
import sys, os, json
from urllib.request import Request, urlopen
from urllib.error import URLError
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fut_seasons import MATCH_COUNT as SEASON_MATCH_COUNT
from fut_state import FutState, seed_starter_club, resolve_active_db_path


def live_profile():
    try:
        with urlopen("http://127.0.0.1:8199/localfut19/profile",timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError,ValueError,URLError):
        return None


def live_balance_adjustment(operation, amount):
    """Apply an external balance edit to the profile served on loopback."""
    profile=live_profile()
    if not isinstance(profile,dict):
        return None
    payload=json.dumps({"operation":str(operation),"amount":int(amount)}).encode(
        "utf-8")
    request=Request(
        "http://127.0.0.1:8199/localfut19/tools/balance",
        data=payload,headers={"Content-Type":"application/json"},method="POST")
    try:
        with urlopen(request,timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError,ValueError,URLError):
        return {"success":False,"error":"active server rejected the adjustment"}


def print_live_account(profile):
    """Print the account currently served to FIFA instead of a legacy DB."""
    print("Active profile:",profile.get("profileMode","NORMAL"))
    print("Account DB  :",profile.get("database","active server"))
    print("Coins       :",int(profile.get("credits",0) or 0))
    print("Draft Tokens:",int(profile.get("draftTokens",0) or 0))
    print("Club        :",profile.get("clubName",""),
          "("+str(profile.get("clubAbbr","") or "")+")")
    print("Club items  :",int(profile.get("clubItems",0) or 0))
    print("Inventory   :",profile.get("inventory",{}))
    print("Object grant:",profile.get("objectGrant",{}))
    print("Bonus players:",profile.get("bonusPlayers",{}))
    print("Squads      :",profile.get("squads",[]))
    print("Record      :",profile.get("record",{}))


def external_coin_adjustment_allowed(state):
    if not state.external_coin_adjustment_allowed():
        print("ERROR: RTG mode blocks external coin additions and balance edits.")
        print("Earn coins through matches, rewards, SBCs, discards, or the market.")
        return False
    active=live_profile()
    if (isinstance(active,dict) and
            str(active.get("profileMode","")).upper()=="RTG"):
        print("ERROR: FUT Deba RTG is currently active; ADD_COINS is disabled.")
        return False
    return True

def verify_live_server(expected):
    """Confirm that the active server immediately sees the SQLite balance."""
    try:
        with urlopen("http://127.0.0.1:8199/ut/game/fifa19/user/credits",timeout=2) as response:
            payload=json.loads(response.read().decode("utf-8"))
        visible=int(payload.get("credits",-1))
        if visible != int(expected):
            print("WARNING: the active server sees %d coins; expected %d." %
                  (visible,int(expected)))
            return False
        print("Active FUT server synchronized: %d coins." % visible)
        print("Change screens or reopen the Store to refresh the in-game header.")
        return True
    except (OSError,ValueError,URLError):
        print("FUT server is not active; the balance will load at next startup.")
        return False

def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "info"
    active=live_profile()
    if cmd=="info" and isinstance(active,dict) and "credits" in active:
        print_live_account(active)
        return 0
    if cmd in ("addcoins","setcoins","adddrafttokens"):
        amount=int(sys.argv[2])
        live=(live_balance_adjustment(cmd,amount)
              if isinstance(active,dict) else None)
        if live is not None:
            if not bool(live.get("success")):
                print("ERROR:",live.get(
                    "error","the active account rejected the coin adjustment"))
                return 2
            if cmd=="adddrafttokens":
                balance=int(live.get("draftTokens",0) or 0)
                print("Added %d Draft Tokens. New balance: %d" %
                      (amount,balance))
                print("Reopen the Draft menu to refresh the displayed balance.")
                return 0
            credits=int(live.get("credits",0) or 0)
            if cmd=="addcoins":
                print("Added %d coins. New balance: %d" % (amount,credits))
            else:
                print("Coin balance set to: %d" % credits)
            print("Active FUT server synchronized: %d coins." % credits)
            print("Change screens or reopen the Store to refresh the in-game header.")
            return 0
    # The launcher runs the server against a per-build profile directory. A
    # tool started from its own console inherits none of that, so opening the
    # data-root database would report success after resetting a file the game
    # never reads.
    database=resolve_active_db_path()
    if cmd in ("reset","resetaccount","reset-account","resetsbc","reset-sbc",
               "prepareseasonfinal","prepare-season-final"):
        if live_profile() is not None:
            print("ERROR: LocalFUT19 is running. Close FIFA 19 and let the")
            print("launcher window finish before resetting; otherwise the")
            print("running server writes its own state back over the reset.")
            return 2
        if not os.path.isfile(database):
            print("ERROR: no local account was found to reset.")
            print("Expected: %s" % database)
            print("Start the game once so the account is created.")
            return 2
    print("Account DB  :", database)
    st = FutState(path=database)
    if cmd not in ("reset","resetaccount","reset-account","resetsbc",
                   "reset-sbc","prepareseasonfinal","prepare-season-final"):
        seed_starter_club(st)
        st.ensure_initial_bonus_player_grant()
        st.ensure_default_object_grant()
    if cmd == "info":
        print("Coins       :", st.credits())
        print("Draft Tokens:", st.draft_tokens())
        print("Club        :", st.club_name(), "(" + st.club_abbr() + ")")
        print("Club items  :", len(st.items_in_pile("club")))
        print("Inventory   :", st.inventory_counts("club"))
        print("Object grant:", st.default_object_grant_status())
        print("Bonus players:", st.initial_bonus_player_grant_status())
        print("Squads      :", [s["squadName"] for s in st.squads()])
        print("Record      :", st.record())
    elif cmd == "addcoins":
        if not external_coin_adjustment_allowed(st):
            st.close()
            return 2
        n = int(sys.argv[2]); c = st.add_credits(n,external=True)
        print("Added %d coins. New balance: %d" % (n, c))
        verify_live_server(c)
    elif cmd == "setcoins":
        if not external_coin_adjustment_allowed(st):
            st.close()
            return 2
        n = int(sys.argv[2]); c = st.set_credits(n,external=True)
        print("Coin balance set to: %d" % c)
        verify_live_server(c)
    elif cmd in ("adddrafttokens", "add-draft-tokens"):
        n = int(sys.argv[2])
        if n < 1:
            raise ValueError("Draft Tokens to add must be at least 1")
        try:
            balance = st.add_draft_tokens(n,external=True)
        except PermissionError:
            print("ERROR: RTG mode blocks external Draft Token additions.")
            print("Earn Draft entry through normal in-game rewards.")
            st.close()
            return 2
        print("Added %d Draft Tokens. New balance: %d" % (n,balance))
        print("Reopen the Draft menu to refresh the displayed balance.")
    elif cmd == "reset":
        b = st.backup(); print("Backup:", b)
        with st.lock:
            st.conn.execute("DELETE FROM items")
            st.conn.execute("DELETE FROM inventory_grants")
            st.conn.execute("DELETE FROM squads")
            # The starter grants are gated by a marker that deliberately
            # survives sales, discards and SBC submissions, so a restart never
            # refills a club the player spent. A reset is the one case where
            # that marker has to go too: leaving it left the club with the
            # items deleted and the grant still recorded as delivered, so the
            # 20 Bronze and 12 Silver starter players never came back and the
            # starter SBCs - which ask for a Bronze player - became
            # impossible. Observed on 2026-09-03: 23 players, none Bronze,
            # with the ledger reporting 32 granted and 0 live.
            for marker in ("initial_bonus_player_grant_v1",
                           "default_object_grant"):
                st.conn.execute("DELETE FROM kv WHERE key=?", (marker,))
            st.conn.commit()
        seed_starter_club(st)
        bonus = st.ensure_initial_bonus_player_grant()
        objects = st.ensure_default_object_grant()
        counts = st.inventory_counts("club")
        print("Club reset. Items:", len(st.items_in_pile("club")))
        print("Starter players restored:", counts.get("player", 0),
              "(bonus grant inserted %s)" % bonus)
        print("Club objects restored:", objects)
    elif cmd in ("resetaccount", "reset-account"):
        b = st.reset_account(); print("Backup:", b)
        seed_starter_club(st)
        st.ensure_initial_bonus_player_grant()
        st.ensure_default_object_grant()
        print("Local FUT account reset.")
        print("Coins        :", st.credits())
        print("Draft Tokens :", st.draft_tokens())
        print("Starter items:", len(st.items_in_pile("club")))
        print("Onboarding will restart the next time you enter Ultimate Team.")
    elif cmd in ("resetsbc", "reset-sbc"):
        # A completed challenge cannot be reopened from inside the game, so a
        # set finished once could never be tested again.
        target = None
        if len(sys.argv) > 2:
            raw_target=str(sys.argv[2]).strip()
            if raw_target.lower() not in ("", "empty", "all"):
                try:
                    target=int(raw_target)
                except ValueError:
                    # RESET_SBC.cmd explicitly labels the field
                    # "empty = all". A tester entered that literal and the
                    # uncaught int("empty") traceback made a safe reset look
                    # like a database failure.
                    print("ERROR: set id must be a number, empty, or all.")
                    st.close()
                    return 2
        backup = st.backup(); print("Backup:", backup)
        cleared = st.reset_sbc_progress(target)
        scope = "set %d" % target if target is not None else "every set"
        print("SBC progress cleared for %s (%d challenge rows)." %
              (scope, cleared))
        print("Rewards already in your club are untouched.")
        print("Re-enter Ultimate Team to see the set open again.")
    elif cmd in ("resetdraft", "reset-draft"):
        # A Draft cannot be left from inside the game without playing all four
        # matches, so retesting the entry flow otherwise costs a full run.
        # The entry is refunded so the balance stays honest.
        backup = st.backup(); print("Backup:", backup)
        discarded = st.discard_active_draft("SINGLE_PLAYER")
        if discarded is None:
            print("No unfinished Single Player Draft to discard.")
        else:
            print("Discarded Draft %s (%s)." % (discarded.get("id"),
                                                discarded.get("state")))
            print("Entry refunded :", discarded.get("entry_currency"))
        print("Draft Tokens   :", st.draft_tokens())
        print("Coins          :", st.credits())
        print("Re-enter Ultimate Team to start a new Draft.")
    elif cmd in ("prepareseasonfinal", "prepare-season-final"):
        backup=st.backup(); print("Backup:",backup)
        prepared=st.prepare_offline_season_final_test()
        print("Offline Season prepared with one match remaining.")
        print("Season       :",prepared["seasonId"])
        print("Division     :",prepared["divisionId"])
        print("Stored round :",prepared["round"],"of",SEASON_MATCH_COUNT)
        print("No synthetic match reward was granted.")
    else:
        print("unknown command:", cmd)
    # The command scripts run in a short-lived process, but tests and embedded
    # callers reuse it. Closing explicitly keeps the selected account database
    # movable immediately after the command completes on Windows.
    st.close()

if __name__ == "__main__":
    raise SystemExit(main() or 0)

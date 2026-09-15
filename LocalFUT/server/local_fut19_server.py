#!/usr/bin/env python3
# =====================================================================
#  FIFA 19 LOCAL FUT - unified offline server
#  Starts in one process:
#    - TLS redirector on 42230
#    - Blaze server on 10051 (login)
#    - FUT HTTP server on 8199 (Ultimate Team API)
#    - Frida runtime interoperability hook for the FIFA19.exe certificate check
#      (Certificate handler RVA 0x31b1890 -> required success value 0x15)
#
#  Usage: run as Administrator, start FIFA 19, and enter Ultimate Team.
#  Press Ctrl+C to stop.
# =====================================================================
import socket, ssl, threading, datetime, os, sys, time, json, warnings, secrets, random, itertools, math
from urllib.parse import urlsplit, parse_qs

from fut_compat import (PC_1_0_0_0_PROFILE, fingerprint_summary,
                        inspect_process_build, runtime_adapter_profile)

BASE = os.path.dirname(os.path.abspath(__file__))
_configured_data_root=str(os.environ.get("LOCALFUT19_DATA_ROOT","") or "").strip()
DATA_ROOT=(os.path.abspath(os.path.expandvars(os.path.expanduser(
    _configured_data_root))) if _configured_data_root else
    os.path.join(os.environ.get("LOCALAPPDATA",os.path.expanduser("~")),
                 "FIFA19LocalFUT"))
CERTDIR = os.path.join(DATA_ROOT, "certs")
CERT = os.path.join(CERTDIR, "redirector-cert.pem")
KEY = os.path.join(CERTDIR, "redirector-key.pem")

REDIRECT_PORT = 42230
BLAZE_PORT = 10051
FUT_PORT = 8199
LOCAL_BIND_HOST = "127.0.0.1"
BLAZE_MAX_CONNECTIONS = 8
LOCAL_UTAS_SID = "localfut19-utas-session"
SPONSORED_EVENTS_COMPONENT = 2076
SPONSORED_EVENTS_GET_EVENTS_URL = 3
NATIVE_PROFILE = PC_1_0_0_0_PROFILE
CERT_HANDLER_RVA = NATIVE_PROFILE.certificate_handler_rva
CERT_SUCCESS = NATIVE_PROFILE.certificate_success_value


def require_runtime_adapter_profile(expected_server_mode=None):
    """Resolve the launcher's exact build choice before serving traffic."""
    try:
        profile=runtime_adapter_profile(os.environ.get(
            "LOCALFUT19_BUILD_PROFILE"))
    except ValueError as exc:
        raise RuntimeError("explicit LocalFUT19 build profile required: %s" %
                           exc) from exc
    if (expected_server_mode is not None and
            profile.server_mode != str(expected_server_mode)):
        raise RuntimeError(
            "build profile %s requires %s server mode, not %s" % (
                profile.profile_id,profile.server_mode,expected_server_mode))
    return profile

PERSONA_ID = 1000019
PROFILE_MODE=("RTG" if (str(os.environ.get("LOCALFUT19_PROFILE","")).upper()=="RTG" or
                        str(os.environ.get("LOCALFUT19_RTG_MODE","")).lower() in
                        {"1","true","yes","on"}) else "NORMAL")
PERSONA_NAME = "FUT Deba RTG" if PROFILE_MODE=="RTG" else "FUT Deba Local"
CLUB_ID = 1
CLUB_NAME = "FUT Deba RTG" if PROFILE_MODE=="RTG" else "FUT Deba Local"
CLUB_ABBR = "RTG" if PROFILE_MODE=="RTG" else "LF9"
# Coins are never granted at startup. Use ADD_COINS.cmd when requested;
# existing profiles always preserve their balance.
CREDITS = 0
MAX_SQUADS = 10
ACTIVE_DRAFT_MATCH_KEY = "active_draft_match"
ACTIVE_TOTW_MATCH_KEY = "active_totw_match"
ACTIVE_SQBT_MATCH_KEY = "active_sqbt_match"
ACTIVE_CHAMPION_MATCH_KEY = "active_champion_match"
ACTIVE_SEASON_MATCH_KEY = "active_season_match"
# Durable correlation token written by the most recent accepted CreateMatch.
# Retail's later PUT /match and /match/end requests identify a run only by gid,
# so stale pending records from another offline mode must never win by lookup
# order.
ACTIVE_OFFLINE_MATCH_OWNER_KEY = "active_offline_match_owner"
# Base points published to the hub and applied when a match is recorded.
SQBT_MATCH_RESULT_WIN = 1000
SQBT_MATCH_RESULT_DRAW = 400
SQBT_MATCH_RESULT_LOSS = 200
MAX_MATCH_COIN_REWARD = 1000
SELECTED_SQBT_OPPONENT_KEY = "selected_sqbt_opponent"
# Keep the unfinished offline Champions implementation recoverable without
# exposing its stock online entry flow in the first dual-build beta.
EXPERIMENTAL_CHAMPIONS_ENABLED=(str(os.environ.get(
    "LOCALFUT19_ENABLE_EXPERIMENTAL_CHAMPIONS","") or "").strip().lower()
    in {"1","true","yes","on"})

LOGFILE = os.path.abspath(os.environ.get(
    "LOCALFUT19_NETWORK_LOG",os.path.join(DATA_ROOT,"server.log")))
_loglock = threading.Lock()
# The retail SBC auction return calls a packed FIFA19.exe receiver which
# disconnects after a successful Purchased Items parse. Track only listings
# opened from an SBC workspace so their one-player Buy Now can bypass that
# broken receiver without changing normal Transfer Market purchases.
# ponytail: one local account/server; key this by persona if multiplayer is added.
_SBC_MARKET_CONTEXT={"state":None,"until":0.0,"trades":{}}
_SEASON_CONTEXT={"state":None,"until":0.0}
# The Draft AWAY accessors are global and also run in TOTW's Offline Select.
# Keep the last explicitly identified mode in memory so a stale persisted
# Draft round can never replace another mode's opponent.
_OFFLINE_SELECT_CONTEXT={"state":None,"mode":None}

def reset_server_log():
    """Start each successful server session with a fresh diagnostic log."""
    try:
        os.makedirs(os.path.dirname(LOGFILE),exist_ok=True)
        with open(LOGFILE, "w", encoding="utf-8"):
            pass
    except Exception as exc:
        print("WARNING: could not reset server.log: %r" % exc, flush=True)

def log(tag, msg):
    with _loglock:
        line = "%s [%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), tag, msg)
        print(line, flush=True)
        try:
            with open(LOGFILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
def now_s(): return int(time.time())

def occupied_service_ports(ports=None):
    """Return local listeners that already exist before threads start.

    Earlier revisions bound each service inside a daemon thread. An
    address-in-use error stopped only that thread while main still announced
    success, allowing FIFA to reach an older process instead of the new code.
    """
    if ports is None:
        ports=(REDIRECT_PORT,BLAZE_PORT,FUT_PORT)
    occupied=[]
    for port in ports:
        probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            if probe.connect_ex(("127.0.0.1",int(port))) == 0:
                occupied.append(int(port))
        finally:
            probe.close()
    return occupied

# =====================================================================
#  Self-signed redirector certificate
# =====================================================================
def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    os.makedirs(CERTDIR, exist_ok=True)
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime as dt
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "spring18.gosredirector.ea.com")])
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key())
         .serial_number(1).not_valid_before(dt.datetime(2015,1,1)).not_valid_after(dt.datetime(2035,1,1))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName("spring18.gosredirector.ea.com")]), False)
         .sign(k, hashes.SHA256()))
    open(KEY,"wb").write(k.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    open(CERT,"wb").write(c.public_bytes(serialization.Encoding.PEM))
    log("cert","redirector certificate generated")

# =====================================================================
#  Blaze TDF / Fire2
# =====================================================================
_VARINT,_STRING,_BLOB,_GROUP,_LIST,_MAP,_OBJECT_TYPE=0,1,2,3,4,5,8
def _tag(tag, vt):
    raw = tag.encode("ascii") if isinstance(tag,str) else bytes(tag)
    out=[0,0,0,vt&0xFF]
    if len(raw)>0: out[0]|=(raw[0]&0x40)<<1; out[0]|=(raw[0]&0x10)<<2; out[0]|=(raw[0]&0x0F)<<2
    if len(raw)>1: out[0]|=(raw[1]&0x40)>>5; out[0]|=(raw[1]&0x10)>>4; out[1]|=(raw[1]&0x0F)<<4
    if len(raw)>2: out[1]|=(raw[2]&0x40)>>3; out[1]|=(raw[2]&0x10)>>2; out[1]|=(raw[2]&0x0C)>>2; out[2]|=(raw[2]&0x03)<<6
    if len(raw)>3: out[2]|=(raw[3]&0x40)>>1; out[2]|=raw[3]&0x1F
    return bytes(out)
def _varint(v):
    v=int(v)
    if v<0: v&=(1<<64)-1
    if v<0x40: return bytes([v])
    out=bytearray([(v&0x3F)|0x80]); v>>=6
    while v>=0x80: out.append((v&0x7F)|0x80); v>>=7
    out.append(v&0x7F); return bytes(out)
def _sval(s):
    raw=str(s).encode("utf-8"); return _varint(len(raw)+1)+raw+b"\x00"
def f_int(t,v): return _tag(t,_VARINT)+_varint(v)
def f_str(t,v): return _tag(t,_STRING)+_sval(v)
def f_grp(t,b): return _tag(t,_GROUP)+b+b"\x00"
def f_objtype(t,c,e): return _tag(t,_OBJECT_TYPE)+_varint(c)+_varint(e)
def f_objid(t,c,e,i): return _tag(t,9)+_varint(c)+_varint(e)+_varint(i)
def f_list_int(t,vals):
    o=bytearray(_tag(t,_LIST)); o.append(_VARINT); o+=_varint(len(vals))
    for v in vals: o+=_varint(v)
    return bytes(o)
def f_map_ss(t,vals):
    o=bytearray(_tag(t,_MAP)); o+=bytes((_STRING,_STRING)); o+=_varint(len(vals))
    for k,v in vals: o+=_sval(k); o+=_sval(v)
    return bytes(o)
def f_map_si(t,vals):
    o=bytearray(_tag(t,_MAP)); o+=bytes((_STRING,_VARINT)); o+=_varint(len(vals))
    for k,v in vals: o+=_sval(k); o+=_varint(v)
    return bytes(o)
def f_map_sg(t,vals):
    o=bytearray(_tag(t,_MAP)); o+=bytes((_STRING,_GROUP)); o+=_varint(len(vals))
    for k,b in vals: o+=_sval(k); o+=b; o.append(0)
    return bytes(o)
def f_list_group(t,vals):
    o=bytearray(_tag(t,_LIST)); o.append(_GROUP); o+=_varint(len(vals))
    for value in vals: o+=bytes(value)+b"\x00"
    return bytes(o)
def get_str(raw,tag):
    m=_tag(tag,_STRING); p=raw.find(m)
    if p<0: return None
    p+=4
    if p>=len(raw): return None
    first=raw[p]; p+=1; n=first&0x3F; sh=6
    if first&0x80:
        while True:
            b=raw[p]; p+=1; n|=(b&0x7F)<<sh
            if not (b&0x80): break
            sh+=7
    if n<=0 or p+n>len(raw): return None
    v=raw[p:p+n]
    if v.endswith(b"\x00"): v=v[:-1]
    return v.decode("utf-8","replace")

def get_str_all(raw,tag):
    """Every string carrying this tag, in order. get_str stops at the first."""
    marker=_tag(tag,_STRING); out=[]; p=raw.find(marker)
    while p>=0:
        q=p+4
        if q>=len(raw): break
        first=raw[q]; q+=1; n=first&0x3F; sh=6
        if first&0x80:
            while q<len(raw):
                b=raw[q]; q+=1; n|=(b&0x7F)<<sh
                if not (b&0x80): break
                sh+=7
        if n<=0 or q+n>len(raw): break
        v=raw[q:q+n]
        if v.endswith(b"\x00"): v=v[:-1]
        out.append(v.decode("utf-8","replace"))
        p=raw.find(marker,q+n)
    return out


def get_int_last(raw,tag):
    """Decode the last matching TDF varint (nested reports repeat GRID)."""
    marker=_tag(tag,_VARINT); p=raw.rfind(marker)
    if p<0: return None
    p+=len(marker); value=0; shift=0
    while p<len(raw):
        byte=raw[p]; p+=1
        if shift==0:
            value=byte&0x3F; shift=6
        else:
            value|=(byte&0x7F)<<shift; shift+=7
        if not (byte&0x80): return value
    return None

def get_int_all(raw,tag):
    """Decode every matching Blaze TDF integer without parsing its container.

    GameReporting embeds the finished score in a deeply nested, game-specific
    report.  The member itself is still the ordinary TDF ``SCOR`` integer, so
    locating that typed member is safer than trying to reinterpret the closed
    report schema used by a different FIFA build.
    """
    marker=_tag(tag,_VARINT); out=[]; start=0
    while True:
        p=raw.find(marker,start)
        if p<0: break
        p+=len(marker); value=0; shift=0
        while p<len(raw):
            byte=raw[p]; p+=1
            if shift==0:
                value=byte&0x3F; shift=6
            else:
                value|=(byte&0x7F)<<shift; shift+=7
            if not (byte&0x80):
                out.append(value); break
        start=max(p,start+1)
    return out

def _game_report_score_values(payload):
    """Return plausible team scores from SubmitOfflineGameReport.

    FIFA's HTTP DestroyMatch request identifies the result and the players but
    omits the final score.  Blaze GameReporting immediately precedes it and
    carries the two ``SCOR`` values.  Keep duplicates because some reports
    publish the same value in both their common and FIFA-specific records; the
    resolver below deliberately normalizes those repetitions.
    """
    values=[int(value) for value in get_int_all(bytes(payload or b""),"SCOR")
            if 0 <= int(value) <= 50]
    return values

def _capture_offline_game_report(payload):
    """Attach a Blaze score snapshot to the currently correlated FUT match."""
    values=_game_report_score_values(payload)
    owner=STATE.get(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{})
    owner=owner if isinstance(owner,dict) else {}
    mode=str(owner.get("mode","") or "").upper()
    active_key={"DRAFT":ACTIVE_DRAFT_MATCH_KEY,
                "SQBT":ACTIVE_SQBT_MATCH_KEY,
                "TOTW":ACTIVE_TOTW_MATCH_KEY,
                "CHAMPIONS":ACTIVE_CHAMPION_MATCH_KEY}.get(mode)
    if not active_key:
        return values
    active=STATE.get(active_key,{})
    if not isinstance(active,dict) or bool(active.get("completed",False)):
        return values
    if values:
        active=dict(active)
        active["gameReportScores"]=values
        active["gameReportCapturedAt"]=now_s()
        STATE.set(active_key,active)
        log("match","GameReporting score captured owner=%s values=%s" %
            (mode,",".join(str(value) for value in values)))
    if len(set(values))<2:
        # A Draft win played out 6-1 was stored 6-0 on 2026-09-07 20:53, and
        # one played out 3-2 was stored 3-0 at 23:38. `match/end` is not the
        # source: its `events` array carries only this club's goals - three
        # `goal` entries for a 3-2 - and it has no away member at all. This
        # report is the only place the opponent score can be, and the single
        # `SCOR` integer it yields is zero, so the tag is either different or
        # nested in a container this flat scan cannot read.
        log("match","GameReporting score unusable owner=%s values=%s bytes=%d" %
            (mode,",".join(str(value) for value in values),
             len(payload or b"")))
        _log_unscored_game_report(mode,payload)
        _log_game_report_tag_sweep(payload)
    return values


def _decode_blaze_tag(first,second,third):
    """Return the four-character tag `_tag` encoded into these three bytes.

    `_tag` keeps bit 6, bit 4 and the low nibble of each character and drops
    bit 5, which is lossless over `0x30-0x5F` - the digits and capitals every
    Blaze tag is built from - so the inverse is exact for a real tag and
    rejects anything else.
    """
    parts=[
        (((first>>7)&1)<<6)|(((first>>6)&1)<<4)|((first>>2)&0x0F),
        (((first>>1)&1)<<6)|((first&1)<<4)|((second>>4)&0x0F),
        (((second>>3)&1)<<6)|(((second>>2)&1)<<4)|((second&0x03)<<2)|
        ((third>>6)&0x03),
        (((third>>5)&1)<<6)|(third&0x1F),
    ]
    characters=[]
    for value in parts:
        # Bit 5 is the one the encoding drops. Capitals carry it clear and
        # digits and the pad space carry it set, so each position has exactly
        # one legal reading.
        if 0x41<=value<=0x5A or value==0x5F:
            characters.append(chr(value))
        elif 0x10<=value<=0x19:
            characters.append(chr(value|0x20))
        elif value==0x00:
            characters.append(" ")
        else:
            return None
    tag="".join(characters)
    if not tag.strip():
        return None
    if _tag(tag,_VARINT)!=bytes((first,second,third,_VARINT)):
        return None
    return tag


def _log_game_report_tag_sweep(payload):
    """Log every plausible small integer TDF in an unusable game report.

    Blaze packs a four-character tag into three bytes plus a type byte. Rather
    than guess which tag holds the opponent score, list every tag whose value
    could be one - `0..50` - so the next completed match names it.
    """
    if _UNSCORED_GAME_REPORT_DUMPS[0]>_UNSCORED_GAME_REPORT_LIMIT:
        return
    raw=bytes(payload or b"")
    if not raw:
        return
    found=[]
    for offset in range(0,max(0,len(raw)-4)):
        first,second,third,kind=raw[offset:offset+4]
        if kind!=_VARINT:
            continue
        tag=_decode_blaze_tag(first,second,third)
        if tag is None:
            continue
        cursor=offset+4; value=0; shift=0
        while cursor<len(raw):
            byte=raw[cursor]; cursor+=1
            if shift==0: value=byte&0x3F; shift=6
            else: value|=(byte&0x7F)<<shift; shift+=7
            if not (byte&0x80): break
        else:
            continue
        if 0<=value<=50:
            found.append("%s@%d=%d" % (tag.strip(),offset,value))
        if len(found)>=64:
            break
    log("match","GameReporting tag sweep %s" % (" ".join(found) or "<none>"))


_UNSCORED_GAME_REPORT_DUMPS=[0]
_UNSCORED_GAME_REPORT_LIMIT=2
_UNSCORED_GAME_REPORT_BYTES=2048


def _log_unscored_game_report(mode,payload):
    """Record a bounded hex copy of a GameReporting report with no score."""
    if _UNSCORED_GAME_REPORT_DUMPS[0]>=_UNSCORED_GAME_REPORT_LIMIT:
        return
    raw=bytes(payload or b"")
    if not raw:
        return
    _UNSCORED_GAME_REPORT_DUMPS[0]+=1
    log("match","GameReporting unscored report owner=%s dump=%d/%d "
        "bytes=%d hex=%s" %
        (mode,_UNSCORED_GAME_REPORT_DUMPS[0],_UNSCORED_GAME_REPORT_LIMIT,
         len(raw),raw[:_UNSCORED_GAME_REPORT_BYTES].hex()))

def blaze_preauth():
    conf=f_map_ss("CONF",[("connIdleTimeout","120s"),("defaultRequestTimeout","80s"),
        ("pingPeriod","20s"),("voipHeadsetUpdateRate","1000"),("xlspConnectionIdleTimeout","300")])
    bwps=bytearray(); bwps+=f_str("PSA","127.0.0.1"); bwps+=f_int("PSP",17502); bwps+=f_str("SNA","qos")
    lat=bytearray(); lat+=f_str("PSA","127.0.0.1"); lat+=f_int("PSP",17502); lat+=f_str("SNA","ea-sjc")
    qoss=bytearray(); qoss+=f_grp("BWPS",bytes(bwps)); qoss+=f_int("LNP",10)
    qoss+=f_map_sg("LTPS",[("ea-sjc",bytes(lat))]); qoss+=f_int("SVID",1161889797); qoss+=f_int("TIME",5000)
    b=bytearray()
    b+=f_str("ASRC","303107"); b+=f_list_int(
        "CIDS",[1,9,35,SPONSORED_EVENTS_COMPONENT,30722]); b+=f_grp("CONF",conf)
    b+=f_int("EEFA",1); b+=f_str("ESRC","303107"); b+=f_str("INST","fifa-2019-pc")
    b+=f_int("MAID",89); b+=f_int("MINR",0); b+=f_str("NASP","cem_ea_id"); b+=f_str("PILD","")
    b+=f_str("PLAT","pc"); b+=f_grp("QOSS",bytes(qoss)); b+=f_str("RSRC","303107")
    b+=f_str("SVER","Blaze 15.1.1.7.2")
    return bytes(b)
def login_session():
    pdtl=bytearray(); pdtl+=f_str("DSNM",PERSONA_NAME); pdtl+=f_int("PID",PERSONA_ID); pdtl+=f_int("PLAT",4)
    sess=bytearray()
    sess+=f_int("1CON",0); sess+=f_int("BUID",PERSONA_ID); sess+=f_int("FRST",0)
    sess+=f_str("KEY","localfut19-blaze"); sess+=f_int("LLOG",now_s())
    sess+=f_str("MAIL","localfut19@localhost"); sess+=f_grp("PDTL",bytes(pdtl)); sess+=f_int("UID",PERSONA_ID)
    b=bytearray(); b+=f_int("ANON",0); b+=f_int("NTOS",0); b+=f_grp("SESS",bytes(sess)); b+=f_int("SPAM",1); b+=f_int("UNDR",0)
    return bytes(b)
def _osdk_config(cfid):
    fut=FUT_PORT
    roster_url="http://127.0.0.1:%d/roster"%fut
    if cfid == "OSDK_ROSTER":
        items=[("FUT/ROSTERUPDATE_URL",roster_url),("ROSTERUPDATE_URL",roster_url)]
    elif cfid == "OSDK_CORE":
        items=[("AUTH_TYPE","NUCLEUS"),("CONTENT_URL","http://127.0.0.1:%d/content"%fut),
            ("FIFA_POW_CONTENT_SERVER_URL","http://127.0.0.1:%d/"%fut),
            ("FIFA_POW_NUCLEUS_PROXY_URL","http://127.0.0.1:%d/"%fut),
            ("FIFA_POW_URL","http://127.0.0.1:%d/"%fut),("FUT_RS4_BASE_URL","http://127.0.0.1:%d/"%fut),
            ("FUTDYNAMICMESSAGES_URL_BASE","http://127.0.0.1:%d"%fut),("NUCLEUS_LOGIN_ENABLED","1"),
            ("ORIGIN_LOGIN_ENABLED","1"),("OSDK_AUTH_REQUIRED","1"),
            ("OSDK_EASW_AUTH_URL","http://127.0.0.1:%d"%fut),("OSDK_EASW_REQ_URL","http://127.0.0.1:%d"%fut),
            ("FUT/ROSTERUPDATE_URL",roster_url),("ROSTERUPDATE_URL",roster_url),("USE_TOKEN_AUTH","1")]
    else:
        items=[]
    b=bytearray(); b+=f_map_ss("CONF",items); b+=f_str("ID",cfid); b+=f_str("ITYP",""); b+=f_str("TOKN","")
    return bytes(b)
def blaze_telemetry():
    b=bytearray()
    b+=f_str("ADRS","127.0.0.1"); b+=f_int("ANON",0); b+=f_str("DISA","1"); b+=f_str("FILT","")
    b+=f_int("LOC",1701729619); b+=f_str("NOOK",""); b+=f_int("PORT",9988); b+=f_int("SDLY",0)
    b+=f_str("SESS",""); b+=f_str("SKEY",""); b+=f_int("SPCT",0); b+=f_str("STIM","")
    return bytes(b)
def blaze_postauth():
    pss=bytearray(); pss+=f_int("PORT",0); pss+=f_int("RPRT",0); pss+=f_int("TIID",0)
    tick=bytearray(); tick+=f_str("ADRS","127.0.0.1"); tick+=f_int("PORT",8999); tick+=f_str("SKEY","")
    urop=bytearray(); urop+=f_int("TMOP",0); urop+=f_int("UID",PERSONA_ID)
    b=bytearray(); b+=f_grp("PSS",bytes(pss)); b+=f_grp("TELE",blaze_telemetry()); b+=f_grp("TICK",bytes(tick)); b+=f_grp("UROP",bytes(urop))
    return bytes(b)
def blaze_skill_game_leaderboard_group():
    """Minimal valid Stats.GetLeaderboardGroup response for warm-up drills."""
    b=bytearray(); b+=f_int("ASCD",0); b+=f_str("BNAM","SkillGame17")
    b+=f_str("DESC",""); b+=f_objtype("ETYP",30722,1)
    b+=f_map_sg("KSUM",[]); b+=f_int("LBSZ",0)
    b+=f_list_group("LIST",[]); b+=f_str("META","")
    b+=f_str("NAME","SkillGame17"); b+=f_str("SNAM","score")
    return bytes(b)
def blaze_empty_leaderboard_values():
    """Well-formed empty Stats.LeaderboardStatValues response."""
    return f_list_group("LDLS",[])

def blaze_skill_game_stat_group(name="SkillGameStats"):
    """Typed Stats.StatGroupResponse used while the match warm-up loads."""
    safe_name=str(name or "SkillGameStats")
    b=bytearray(); b+=f_str("CNAM",safe_name); b+=f_str("DESC","")
    b+=f_objtype("ETYP",30722,1); b+=f_map_si("KSUM",[])
    b+=f_str("META",""); b+=f_str("NAME",safe_name)
    b+=f_list_group("STAT",[])
    return bytes(b)

def blaze_empty_key_scopes():
    """Well-formed empty Stats.KeyScopes response."""
    return f_map_sg("KSIT",[])

def blaze_user_data_response():
    """Typed UserSessions.LookupUsersByPersonaNames response for local self."""
    user=bytearray()
    user+=f_int("AID",PERSONA_ID); user+=f_int("ALOC",1701729619)
    user+=_tag("EXBB",_BLOB)+_varint(0); user+=f_int("EXID",0)
    user+=f_int("ID",PERSONA_ID); user+=f_str("NAME",PERSONA_NAME)
    data=bytearray(); data+=f_grp("EDAT",_ext()); data+=f_int("FLGS",3)
    data+=f_grp("USER",bytes(user))
    return f_list_group("ULST",[bytes(data)])

def blaze_game_reporting_result_notification(game_reporting_id=0):
    """Terminal-success Blaze::GameReporting::ResultNotification DTO."""
    safe_id=max(0,int(game_reporting_id or 0))
    return (f_int("EROR",0)+f_int("FNL",1)+f_int("GHID",safe_id)+
            f_int("GRID",safe_id))
def _ext():
    q=bytearray(); q+=f_int("DBPS",0); q+=f_int("NATT",0); q+=f_int("UBPS",0)
    o=bytearray(); o+=f_int("HWFG",0); o+=f_grp("QDAT",bytes(q)); o+=f_int("UATT",0); return bytes(o)
def notifs():
    ua=bytearray()
    ua+=f_int("AID",PERSONA_ID); ua+=f_int("ALOC",1701729619); ua+=_tag("EXBB",2)+_varint(0)
    ua+=f_int("EXID",0); ua+=f_int("ID",PERSONA_ID); ua+=f_str("NAME",PERSONA_NAME)
    ua+=f_str("NASP","cem_ea_id"); ua+=f_int("ORIG",PERSONA_ID); ua+=f_int("PIDI",0)
    n2=bytearray(); n2+=f_grp("DATA",_ext()); n2+=f_grp("USER",bytes(ua))
    ls=bytearray()
    ls+=f_int("BUID",PERSONA_ID); ls+=f_objid("CGID",30722,2,PERSONA_ID); ls+=f_str("DSNM",PERSONA_NAME)
    ls+=f_str("KEY","localfut19-blaze"); ls+=f_int("LAST",now_s()); ls+=f_str("MAIL","localfut19@localhost")
    ls+=f_str("NASP","cem_ea_id"); ls+=f_int("PID",PERSONA_ID); ls+=f_int("PLAT",0)
    ls+=f_int("UID",PERSONA_ID); ls+=f_int("USTP",0); ls+=f_int("XREF",0)
    ed=bytearray(); ed+=f_grp("DATA",_ext()); ed+=f_int("USID",PERSONA_ID)
    uu=bytearray(); uu+=f_int("FLGS",3); uu+=f_int("ID",PERSONA_ID)
    return [(30722,2,bytes(n2)),(30722,8,bytes(ls)),(30722,1,bytes(ed)),(30722,5,bytes(uu))]

# FIFA 19/Blaze 15 uses Authentication.Login = 10 (0x0A). Keep variants seen
# in older generations, but only these explicit IDs may establish a session
# and publish UserSessions notifications.
AUTH_LOGIN_ROUTES={(1,10),(1,40),(1,50),(1,60),(1,152)}
# These commands are state-changing/unsubscribe calls whose response contract
# is genuinely void.  Keep them distinct from unknown routes: logging a valid
# zero-byte acknowledgement as ``EMPTY`` obscures the missing-DTO failures that
# can otherwise leave FIFA stuck on "Loading Ultimate Team" after a match.
VOID_ACK_ROUTES={(1,70),(9,22),(9,23),(9,28),(10,2),
                 (30722,8),(30722,20)}
ASYNC_ACK_ROUTES={(28,2)}

def blaze_response(comp,cmd,payload):
    if (comp,cmd)==(9,7): return blaze_preauth(),False
    if (comp,cmd)==(9,2): return f_int("STIM",now_s()),False
    if (comp,cmd)==(9,8): return blaze_postauth(),False
    if (comp,cmd)==(9,5): return blaze_telemetry(),False
    if (comp,cmd)==(9,12): return f_map_ss("SMAP",[("FirstTimeFlag","0")]),False
    if (comp,cmd)==(9,1):
        cfid=get_str(payload,"CFID") or ""
        return _osdk_config(cfid),False
    # FIFA enters SkillGame17 while an offline FUT match loads. Returning an
    # empty Fire2 body makes the leaderboard decoder retry forever and leaves
    # the warm-up without its continue command. These two Stats DTOs are valid
    # empty data, not successful responses with a missing object.
    if (comp,cmd)==(7,10):
        return blaze_skill_game_leaderboard_group(),False
    if (comp,cmd)==(7,13):
        return blaze_empty_leaderboard_values(),False
    if (comp,cmd)==(7,4):
        return blaze_skill_game_stat_group(get_str(payload,"NAME")),False
    if (comp,cmd)==(7,15):
        return blaze_empty_key_scopes(),False
    # These are valid empty/default DTOs requested both at bootstrap and when
    # FIFA returns from an offline match.  A zero-byte success leaves the
    # corresponding decoder object null; after a QUIT the frontend then waits
    # forever on "Loading Ultimate Team".  The member names and types below
    # are the Blaze 2 response contracts matched against the captured request
    # tags (not fabricated gameplay data).
    if (comp,cmd)==(7,3):
        return f_list_group("GRPS",[]),False
    if (comp,cmd)==(15,2):
        return f_int("MCNT",0),False
    if (comp,cmd)==(25,6):
        return f_list_group("LMAP",[]),False
    if (comp,cmd)==(11,1600):
        return f_list_group("CIST",[]),False
    if (comp,cmd)==(11,2600):
        return (f_list_group("AWST",[])+f_int("CLDS",0)+
                f_int("INAC",0)+f_int("MXEV",0)+f_int("MXGM",0)+
                f_int("MXIN",0)+f_int("MXME",0)+f_int("MXNE",0)+
                f_int("MXRV",0)+f_int("PUHR",0)+
                f_list_group("REST",[])+f_int("SOVR",0)+
                f_int("STRT",0)),False
    if (comp,cmd)==(30722,50):
        return blaze_user_data_response(),False
    # SponsoredEvents.GetEventsURL builds a URLResponse whose only required
    # TDF member is URL. FIFA requests it during bootstrap and while onboarding
    # opens the loan step. A successful response without URL leaves the client
    # without a destination and fails before GET /loan/players.
    if (comp,cmd)==(SPONSORED_EVENTS_COMPONENT,
                    SPONSORED_EVENTS_GET_EVENTS_URL):
        return f_str("URL","http://127.0.0.1:%d/sponsored-events"%FUT_PORT),False
    # Authentication command IDs are not interchangeable. In particular k70
    # (0x46) is Logout: the old catch-all incorrectly replied with LoginResponse
    # and emitted four UserSessions notifications on every logout request.
    # Util.filterForProfanity = 20. FUT runs every name the player types through
    # it, squad names included, and blocks on the answer: it is the first thing
    # that happens when a squad is created or renamed, before any HTTP call. The
    # client reads the returned TLST without checking it, so the empty body left
    # its result object null and killed the process with an access violation at
    # FIFA19.exe+0xd4c5611 (mov rax,[r9+0x30] / mov r9,[rax] with rax=0). That is
    # also why creating a squad produced no request at all: the flow never got
    # past the name. Offline there is nothing to censor, so each text comes back
    # unchanged with DIRT=0, the "clean" answer.
    if (comp,cmd)==(9,20):
        texts=get_str_all(payload,"UTXT")
        return f_list_group("TLST",[f_int("DIRT",0)+f_str("UTXT",t)
                                    for t in texts]),False
    if (comp,cmd) in AUTH_LOGIN_ROUTES: return login_session(),True
    if (comp,cmd) in VOID_ACK_ROUTES: return b"",False
    return b"",False

def fire2_build(comp,cmd,msgnum,mtype,payload=b""):
    out=bytearray()
    out+=len(payload).to_bytes(4,"big"); out+=(0).to_bytes(2,"big")
    out+=int(comp).to_bytes(2,"big"); out+=int(cmd).to_bytes(2,"big")
    out+=(int(msgnum)&0xFFFFFF).to_bytes(3,"big"); out+=bytes([(mtype&7)<<5,0,0]); out+=payload
    return bytes(out)
def fire2_parse(buf):
    if len(buf)<16: return None
    ps=int.from_bytes(buf[0:4],"big"); ms=int.from_bytes(buf[4:6],"big"); total=16+ms+ps
    if len(buf)<total: return None
    raw=bytes(buf[:total]); del buf[:total]
    comp=int.from_bytes(raw[6:8],"big"); cmd=int.from_bytes(raw[8:10],"big")
    msgnum=int.from_bytes(raw[10:13],"big"); mtype=raw[13]>>5; payload=raw[16+ms:]
    return comp,cmd,msgnum,mtype,payload

def blaze_conn(c,a):
    log("blaze","connected %s:%s" % a[:2]); buf=bytearray()
    route_log_counts={}
    traced_empty_routes=set()
    try:
        c.settimeout(300)
        while True:
            try:
                d=c.recv(8192)
            except socket.timeout:
                # CPU matches can legitimately produce no Blaze traffic for
                # five minutes. Closing the socket here strands the client
                # mid-match, so keep waiting until FIFA closes it explicitly.
                continue
            if not d: break
            buf+=d
            while True:
                pk=fire2_parse(buf)
                if pk is None: break
                comp,cmd,msgnum,mtype,payload=pk
                if mtype==4:
                    log("blaze","PING c%d/k%d m%d" % (comp,cmd,msgnum))
                    c.sendall(fire2_build(comp,cmd,msgnum,5,b"")); continue
                if mtype!=0: continue
                resp,notify=blaze_response(comp,cmd,payload)
                route=(comp,cmd)
                route_log_counts[route]=route_log_counts.get(route,0)+1
                detail=""
                if (comp,cmd)==(9,1):
                    detail=" CFID=%s" % (get_str(payload,"CFID") or "")
                    if get_str(payload,"CFID")=="OSDK_CORE": detail+=" AUTH=ONLINE"
                elif (comp,cmd) in AUTH_LOGIN_ROUTES:
                    detail=" AUTH_ROUTE=LOGIN TOKEN_PRESENT=%s" % bool(get_str(payload,"AUTH"))
                suffix=(" ASYNC_ACK" if route in ASYNC_ACK_ROUTES else
                        (" ACK" if route in VOID_ACK_ROUTES else
                         (" EMPTY" if not resp else "")))
                # CensusData c10/k5 is polled rapidly while the frontend stays
                # open. Responses continue; only repetitive logging is capped.
                noisy_limit=8 if route==(10,5) else None
                if noisy_limit is None or route_log_counts[route]<=noisy_limit:
                    log("blaze","REQ c%d/k%d m%d p%d%s -> %d%s" %
                        (comp,cmd,msgnum,len(payload),detail,len(resp),suffix))
                elif route_log_counts[route]==noisy_limit+1:
                    log("blaze","REPETITIVE c10/k5 REQ: additional lines suppressed; responses continue")
                # Record only bounded metadata for unknown FIFA-specific
                # commands. Raw Blaze payloads may contain session or identity
                # material and must never be persisted in diagnostics.
                if (not resp and route not in traced_empty_routes and
                        route not in VOID_ACK_ROUTES and
                        route not in ASYNC_ACK_ROUTES):
                    traced_empty_routes.add(route)
                    log("blaze","TRACE first empty response c%d/k%d payloadBytes=%d" %
                        (comp,cmd,len(payload)))
                c.sendall(fire2_build(comp,cmd,msgnum,1,resp))
                # SubmitOfflineGameReport is completed asynchronously.  An
                # empty RPC success alone leaves FIFA waiting in the warm-up;
                # notification 114 is the terminal result consumed by the
                # match-loading controller.
                if route==(28,2):
                    reporting_id=get_int_last(payload,"GRID") or 0
                    _capture_offline_game_report(payload)
                    time.sleep(0.020)
                    result=blaze_game_reporting_result_notification(reporting_id)
                    c.sendall(fire2_build(28,114,0,2,result))
                    log("blaze","GameReporting ResultNotification sent GRID=%d" %
                        reporting_id)
                if notify:
                    for nc,nk,npl in notifs():
                        c.sendall(fire2_build(nc,nk,0,2,npl))
                    log("blaze","login notifications sent: %d" % len(notifs()))
    except Exception as e:
        # FIFA can open one early Blaze probe, send only PreAuth, and close the
        # socket before its account/profile bootstrap is ready. The identical
        # PreAuth response succeeds on the automatic retry, so WinError 10053
        # in this exact one-route sequence is client lifecycle, not a local
        # server failure.
        if (getattr(e,"winerror",None)==10053 and
                sum(route_log_counts.values())==1 and
                route_log_counts.get((9,7))==1):
            log("blaze","initial PreAuth probe closed by client; waiting for automatic retry")
        else:
            log("blaze","connection error: %r" % e)
    finally:
        try: c.close()
        except: pass
        log("blaze","disconnected")

# =====================================================================
#  FUT HTTP (Ultimate Team) with persistent user state
# =====================================================================
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re as _re
try:
    from fut_state import FutState, seed_starter_club
    from fut_catalog import (catalogue_size, native_player_fields,
                             player_presentation_resource_id,
                             player_wire_resource_id,
                             LOAN_PLAYER_CANDIDATES, card_revision,
                             card_version_rows)
    from fut_market import (market_object_price_limits, market_price_limits,
                            search_object_listings, search_player_listings,
                            snap_market_price, resolve_player_listing,
                            exact_player_listing, market_exact_resource_id)
    from fut_packs import (pack_definition, store_purchases, generate_pack,
                           generate_player_pick_bundle,
                           generate_player_pick_options)
    from fut_sbc import (DEFAULT_CATALOG_PATH, SbcCatalogue,
                         SbcValidationError, challenge_dto, set_dto,
                         evaluate_requirements, native_reward_dto,
                         validate_challenge_submission)
    from fut_draft_config import load_draft_settings
    from fut_seasons import (new_season, season_asset, season_catalog,
                             season_history_payload, season_hub_payload,
                             season_localizations, season_user_payload)
    from fut_champions import (
        MATCH_COUNT as CHAMPION_MATCH_COUNT,
        NUMBER_OF_WINS_TIER_TYPE,
        RANK_SPECS as CHAMPION_RANK_SPECS,
        champion_event_dto,
        champion_friend_stat_dto,
        champion_friends_stats_dto,
        champion_hub_dto,
        champion_leaderboard_dto,
        champion_leaderboard_entry_dto,
        champion_opponents,
        champion_awarded_prize_dto,
        champion_prize_grant_dto,
        champion_prize_tier_dto,
        champion_registration_dto,
        champion_user_stat_dto,
        champion_user_stats_dto,
        leaderboard_bots as champion_leaderboard_bots,
        reward_bundle as champion_reward_bundle,
        tier_for_wins as champion_tier_for_wins,
        user_rank as champion_user_rank,
    )
    from fut_objects import (native_object_item, stored_item_kind,
                             object_definition, object_catalog,
                             manager_item_dto, stadium_item_dto,
                             ball_item_dto)
    from fut_sqbt import (LOSS_POINTS as SQBT_LOSS_POINTS,
                         EVENT_DURATION_SECONDS as SQBT_EVENT_DURATION_SECONDS,
                         FEATURED_SQUAD_FORMATION,
                         FEATURED_SQUAD_NAME,
                         FUT_CHAMPIONS_RARITY_ID,
                         MAX_POINT_MATCHES as SQBT_MAX_POINT_MATCHES,
                         MAX_ROTATIONS as SQBT_MAX_ROTATIONS,
                         WIN_POINTS as SQBT_WIN_POINTS,
                         featured_squad_resource_ids,
                         leaderboard as sqbt_leaderboard,
                         match_points as sqbt_match_points,
                         opponents as sqbt_catalogue_opponents,
                         prize_tiers as sqbt_prize_tiers,
                         tier_level as sqbt_tier_level,
                         user_rank as sqbt_user_rank)
except ImportError:
    sys.path.insert(0, BASE)
    from fut_state import FutState, seed_starter_club
    from fut_catalog import (catalogue_size, native_player_fields,
                             player_presentation_resource_id,
                             player_wire_resource_id,
                             LOAN_PLAYER_CANDIDATES, card_revision,
                             card_version_rows)
    from fut_market import (market_object_price_limits, market_price_limits,
                            search_object_listings, search_player_listings,
                            snap_market_price, resolve_player_listing,
                            exact_player_listing, market_exact_resource_id)
    from fut_packs import (pack_definition, store_purchases, generate_pack,
                           generate_player_pick_bundle,
                           generate_player_pick_options)
    from fut_sbc import (DEFAULT_CATALOG_PATH, SbcCatalogue,
                         SbcValidationError, challenge_dto, set_dto,
                         evaluate_requirements, native_reward_dto,
                         validate_challenge_submission)
    from fut_draft_config import load_draft_settings
    from fut_seasons import (new_season, season_asset, season_catalog,
                             season_history_payload, season_hub_payload,
                             season_localizations, season_user_payload)
    from fut_champions import (
        MATCH_COUNT as CHAMPION_MATCH_COUNT,
        NUMBER_OF_WINS_TIER_TYPE,
        RANK_SPECS as CHAMPION_RANK_SPECS,
        champion_event_dto,
        champion_friend_stat_dto,
        champion_friends_stats_dto,
        champion_hub_dto,
        champion_leaderboard_dto,
        champion_leaderboard_entry_dto,
        champion_opponents,
        champion_awarded_prize_dto,
        champion_prize_grant_dto,
        champion_prize_tier_dto,
        champion_registration_dto,
        champion_user_stat_dto,
        champion_user_stats_dto,
        leaderboard_bots as champion_leaderboard_bots,
        reward_bundle as champion_reward_bundle,
        tier_for_wins as champion_tier_for_wins,
        user_rank as champion_user_rank,
    )
    from fut_objects import (native_object_item, stored_item_kind,
                             object_definition, object_catalog,
                             manager_item_dto, stadium_item_dto,
                             ball_item_dto)
    from fut_sqbt import (LOSS_POINTS as SQBT_LOSS_POINTS,
                         EVENT_DURATION_SECONDS as SQBT_EVENT_DURATION_SECONDS,
                         FEATURED_SQUAD_FORMATION,
                         FEATURED_SQUAD_NAME,
                         FUT_CHAMPIONS_RARITY_ID,
                         MAX_POINT_MATCHES as SQBT_MAX_POINT_MATCHES,
                         MAX_ROTATIONS as SQBT_MAX_ROTATIONS,
                         WIN_POINTS as SQBT_WIN_POINTS,
                         featured_squad_resource_ids,
                         leaderboard as sqbt_leaderboard,
                         match_points as sqbt_match_points,
                         opponents as sqbt_catalogue_opponents,
                         prize_tiers as sqbt_prize_tiers,
                         tier_level as sqbt_tier_level,
                         user_rank as sqbt_user_rank)

_PIM_EXACT_TO_WIRE={
    int(row["resourceId"]):int(player_wire_resource_id(
        int(row["resourceId"])))
    for row in card_version_rows(revisions="Prime Icon Moments")
}
_PIM_WIRE_TO_EXACT={wire:exact for exact,wire in _PIM_EXACT_TO_WIRE.items()}
if (len(_PIM_EXACT_TO_WIRE)!=44 or len(_PIM_WIRE_TO_EXACT)!=44 or
        set(_PIM_EXACT_TO_WIRE) & set(_PIM_WIRE_TO_EXACT)):
    raise RuntimeError("Prime Icon Moments wire compatibility map is invalid")

SBC_CATALOGUE = SbcCatalogue.load(DEFAULT_CATALOG_PATH)

STATE = FutState(credits=CREDITS, club_name=CLUB_NAME, club_abbr=CLUB_ABBR,
                 persona_name=PERSONA_NAME,account_mode=PROFILE_MODE)
try:
    seed_starter_club(STATE)
    bonus_players=STATE.ensure_initial_bonus_player_grant()
    object_grant=STATE.ensure_default_object_grant()
    manager_metadata_updated=STATE.migrate_manager_metadata()
    starter_manager=STATE.ensure_starter_squad_manager()
    active_defaults=STATE.ensure_active_club_items()
    settled_market=STATE.settle_market()
    counts=STATE.inventory_counts("club")
    log("fut","state loaded: %d coins, %d players, %d objects, %d local catalogue players, club '%s'" %
        (STATE.credits(),counts.get("player",0),
         sum(value for key,value in counts.items() if key != "player"),
         catalogue_size(),STATE.club_name()))
    club_record=STATE.record()
    log("fut","club record: %d-%d-%d (win-draw-loss)" %
        (club_record["wins"],club_record["draws"],club_record["losses"]))
    log("fut","default object grant: %d inserted, %d/%d recorded (%d live)" %
        (object_grant["inserted"],object_grant["recorded"],
         object_grant["expected"],object_grant["live"]))
    log("fut","initial bonus players: %d inserted, %d/%d recorded (%d live; "
        "%d Bronze, %d Silver)" % (
        bonus_players["inserted"],bonus_players["recorded"],
        bonus_players["expected"],bonus_players["live"],
        bonus_players["bronze"],bonus_players["silver"]))
    log("fut","object metadata: %d managers migrated; active defaults=%s" %
        (manager_metadata_updated,",".join(sorted(active_defaults)) or "preserved"))
    log("fut","starter manager: %d (%s)" % (
        starter_manager["managerId"],
        "assigned" if starter_manager["updated"] else "preserved"))
    if settled_market:
        log("market","startup settlement transitions=%d" % len(settled_market))
except Exception as e:
    log("fut","state bootstrap failed: %r" % e)

def J(o): return json.dumps(o,separators=(",",":")).encode("utf-8")

def _credits_payload(credits=None):
    """Return the balance shape accepted by native FIFA 19 FUT parsers.

    Store refresh does not always read the same bootstrap member. Publishing
    only ``credits`` kept SQLite correct but reset the client header to zero.
    Draft navigation also refreshes this endpoint after ``userMassInfo``; all
    three native currencies therefore have to be repeated here or the Draft
    Token counter is replaced with the parser's zero default.
    """
    value=STATE.credits() if credits is None else max(0,int(credits))
    fifa_points=max(0,int(STATE.get("fifa_points",0) or 0))
    draft_tokens=STATE.draft_tokens()
    return {"coins":value,"credits":value,"totalCredits":value,
            "funds":value,"finalFunds":value,
            "currencies":[
                {"name":"COINS","funds":value,"finalFunds":value},
                {"name":"POINTS","funds":fifa_points,
                 "finalFunds":fifa_points},
                {"name":"DRAFT_TOKEN","funds":draft_tokens,
                 "finalFunds":draft_tokens}],
            "unopenedPacks":{"preOrderPacks":len(STATE.reward_unopened_packs()),
                              "recoveredPacks":0}}


def _generate_pack_for_profile(pack_id,seed):
    if STATE.is_rtg_mode():
        return generate_pack(pack_id,seed,probability_profile="rtg")
    return generate_pack(pack_id,seed)


def _native_create_pack_response(item_list,pack_id,duplicate_item_ids=None,
                                 dynamic_objective_updates=None,**extensions):
    """Return the closed FIFA 19 ``FutCreatePackServerResponse`` contract.

    Static inspection of both supported CardsDLL builds shows five recognized
    root members. ``numberItems`` and ``purchasedPackId`` are not aliases for
    the project's diagnostic ``openingId``/``packId`` fields.

    Objective progress is persisted before this response, but the create-pack
    parser must receive an empty delta collection. Full objective records
    belong to the dedicated Dynamic Objectives endpoint; placing those records
    in this narrower native update collection prevents the pack-opening state
    machine from reaching its animation transition.

    ``dynamicObjectivesUpdates`` must be a JSON **object**, never an array.
    The retail member table resolves the five recognized ids to
    ``duplicateItemIdList`` (289), ``dynamicObjectivesUpdates`` (293),
    ``itemList`` (480), ``numberItems`` (613) and ``purchasedPackId`` (774).
    Id 293 dispatches to the deserializer at CardsDLL+0x27dc70, whose member
    loop at +0x27e0d0 runs ``skip value, read token, repeat until token 10``,
    and token 10 is the object-close. An array therefore never terminates that
    loop: the reader at +0x1996b0 returns zero at end of input without
    advancing, so the parser spins forever and the client freezes with the
    request already answered. The 2026-09-02 live capture proved this
    end to end: the stalled thread stayed inside the response parser for the
    whole observation window with its innermost frame at +0x27e0dd, the
    instruction immediately after that loop's token read.
    """
    items=list(item_list or [])
    response={
        "itemList":items,
        "duplicateItemIdList":list(duplicate_item_ids or []),
        "dynamicObjectivesUpdates":dict(dynamic_objective_updates or {}),
        "numberItems":len(items),
        "purchasedPackId":int(pack_id),
    }
    # Retain project-facing metadata for diagnostics and companion tooling.
    # The native closed DTO safely skips unknown members after consuming the
    # five fields above.
    response.update(extensions)
    return response


def _generate_pick_bundle_for_profile(pack_id,seed):
    if STATE.is_rtg_mode():
        return generate_player_pick_bundle(
            pack_id,seed,probability_profile="rtg")
    return generate_player_pick_bundle(pack_id,seed)

def _roll_over_completed_season(trigger):
    """Claim a completed offline Season once and start the next one.

    RC157 live: after Advance FIFA never sent the mapped reset request; it read
    the next-division catalogue and then rejected Seasons while round 15 was
    still published. Both paths therefore share this idempotent rollover.
    """
    with STATE.lock:
        if not STATE.offline_season().get("complete"):
            return None
        receipt=STATE.claim_offline_season_reward()
        following=STATE.start_next_offline_season()
        STATE.set(ACTIVE_SEASON_MATCH_KEY,None)
        STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,None)
    log("seasons","rollover trigger=%s season=%d result=%s rewardCreated=%s "
        "nextSeason=%d division=%d entered=%s" % (
            trigger,int(receipt["seasonId"]),receipt["seasonEndResult"],
            bool(receipt["createdNow"]),int(following["seasonId"]),
            int(following["divisionId"]),bool(following.get("entered"))))
    return following


def _native_destroy_match_response(end_reason="QUIT", credits=None,reward=0):
    """Build the typed FutDestroyMatchServerResponse parsed by CardsDLL.

    Read-only inspection of the retail decoder at CardsDLL+0x211790 confirms
    these root and nested members. ``boostConis`` intentionally preserves the
    misspelling in the native parser. The detailed object is persisted as the
    idempotent settlement receipt; the wire response below publishes only its
    parser-safe members.
    """
    balance=STATE.credits() if credits is None else max(0,int(credits))
    reward=max(0,int(reward or 0))
    reason=str(end_reason or "").upper()
    if reason in ("DISCONNECT","FORFEIT"):
        reason="QUIT"
    if reason not in ("WIN","LOSS","DRAW","DNF","QUIT","NO_CONTEST",
                      "DNF_WIN","DNF_DRAW","DNF_LOSS"):
        reason="NO_CONTEST"
    return {
        "allCoins":balance,
        "boostConis":0,
        "boostCountLeft":0,
        "dynamicObjectivesUpdates":[],
        "endReason":reason,
        "gameModeAward":{"bidTokens":0,"coins":reward},
        "matchCoins":reward,
        "matchCoinMultipliers":[],
        "matchCoinPartials":[],
        "qualifiedChampionEventId":0,
        "spectatorModeShouldUpload":False,
        "spectatorModeMatchScore":0,
        "rivalsScore":{"rivalPoints":0,"skillPoints":0,"trusted":True},
        "seasonCoins":reward,
        "squadBattlesScore":{
            "finalScore":0,
            "goalsScore":0,
            "matchDifficultyScoreModifier":0.0,
            "matchResultScore":0,
            "skillRatingScore":0,
            "teamRatingScore":0,
        },
        "tournamentCoins":0,
    }

def _native_destroy_match_wire_response(result="LOSS", end_reason="QUIT",
                                        receipt=None):
    """Return the smallest parser-safe native match completion decision.

    Static mapping of the retail member table proves that ``seasonEndResult``
    is *not* the match win/loss result.  Its enum accepts season outcomes such
    as CHAMPIONSHIP and RELEGATION; sending LOSS therefore decoded to the
    constructor default zero and provided no post-match signal.  The previous
    complete response also entered array/nested-object branches and could
    stall inside CardsDLL+0x211790.  Keep rewards and the actual Draft result
    in the durable receipt and expose only the verified scalar ``endReason``
    decision on the wire.
    """
    # The retail continuation first compares the response member at +0xb4
    # against the reason saved from the request at flowState+0x1988.  A QUIT
    # request therefore *must* receive QUIT; replying LOSS takes the explicit
    # "result under review" mismatch branch before Draft completion can run.
    # The native QUIT branch reaches the common teardown directly.  Do not
    # substitute a LOSS result in the transient branch register: the live
    # 2026-08-28 trace proved that the forced provider notification does not
    # dismiss the FUT loading overlay.
    normalized_reason=str(end_reason or "NO_CONTEST").upper()
    if normalized_reason in ("DISCONNECT","FORFEIT"):
        normalized_reason="QUIT"
    if normalized_reason not in (
            "WIN","LOSS","DRAW","DNF","QUIT","NO_CONTEST",
            "DNF_WIN","DNF_DRAW","DNF_LOSS"):
        normalized_reason="NO_CONTEST"
    source=receipt if isinstance(receipt,dict) else {}
    if not source:
        return {"endReason":normalized_reason}
    balance=max(0,int(source.get("credits",source.get(
        "finalFunds",source.get("allCoins",STATE.credits()))) or 0))
    reward=min(MAX_MATCH_COIN_REWARD,max(
        0,int(source.get("matchCoins",0) or 0)))
    game_mode_award=(source.get("gameModeAward")
                     if isinstance(source.get("gameModeAward"),dict) else {})
    return {
        "endReason":normalized_reason,
        # allCoins is the aggregate *match award*, not the account balance.
        # Publishing a large club balance here overflows the retail result
        # animation and produces a negative "Match Coins Awarded" value.
        "allCoins":reward,"credits":balance,"coins":balance,
        "totalCredits":balance,"funds":balance,"finalFunds":balance,
        "sessionCoinsBankBalance":balance,
        "matchCoins":reward,"seasonCoins":reward,
        "rewardCoins":reward,"totalCoins":reward,
        "completionAward":reward,
        "gameModeAward":{"bidTokens":int(game_mode_award.get(
            "bidTokens",0) or 0),"coins":reward},
    }


def _totw_destroy_match_response(result, end_reason, reward, credits=None):
    """Publish the durable TOTW award to the native post-match economy view.

    Unlike challenge completion, the difficulty award is a participation
    payment: every completed TOTW fixture receives it, while only a win marks
    the selected challenge as completed.  Keep this response scalar apart
    from the one proven ``gameModeAward`` record; Draft's unsafe array members
    deliberately remain absent.
    """
    reason=str(end_reason or result or "NO_CONTEST").upper()
    if reason in ("DISCONNECT","FORFEIT"):
        reason="QUIT"
    if reason not in ("WIN","LOSS","DRAW","DNF","QUIT","NO_CONTEST",
                      "DNF_WIN","DNF_DRAW","DNF_LOSS"):
        reason="NO_CONTEST"
    reward=min(MAX_MATCH_COIN_REWARD,max(0,int(reward or 0)))
    balance=max(0,int(STATE.credits() if credits is None else credits))
    return {
        "endReason":reason,
        "allCoins":reward,"credits":balance,"coins":balance,
        "totalCredits":balance,"funds":balance,"finalFunds":balance,
        "sessionCoinsBankBalance":balance,
        "matchCoins":reward,"seasonCoins":reward,
        "rewardCoins":reward,"totalCoins":reward,
        "completionAward":reward,
        "gameModeAward":{"bidTokens":0,"coins":reward},
    }

def _offline_division():
    """Return the division every user reload has to republish.

    FIFA reads ``userMassInfo`` when FUT starts and ``user`` when it returns
    from a match.  Only the first published the offline division, so after a
    match the native member kept its default and the Seasons carousel asked
    the catalogue for division 11, which cannot exist; the screen then failed
    with ``unable to retrieve your Seasons data``.  Ten guarded sessions show
    the same order without exception: ``userMassInfo`` then divisionList=10,
    ``user`` then divisionList=11, and the one session that never loaded
    ``user`` never asked for 11.
    """
    return int(STATE.offline_season()["divisionId"])

def _native_user():
    record=STATE.record()
    return {**_credits_payload(),"personaId":PERSONA_ID,"personaName":STATE.persona_name(),
        "divisionOffline":_offline_division(),"divisionOnline":10,
        # This server has no online mode, so every division dialect the
        # client knows carries the offline one.
        "offlineDivision":_offline_division(),
        "currentDivision":_offline_division(),
        "clubName":STATE.club_name(),"clubAbbr":STATE.club_abbr(),"fifaPoints":int(STATE.get("fifa_points",0)),
        "trophies":[],"reliability":{"actionable":False},
        # The retail header reads the root won/draw/lost members. Preserve
        # the project aliases too, and publish both record dialects so every
        # FIFA 19 view observes the same durable total.
        "won":record["wins"],"wins":record["wins"],
        "draw":record["draws"],"draws":record["draws"],
        "lost":record["losses"],"loss":record["losses"],
        "losses":record["losses"],
        "gamesWon":record["wins"],"gamesDrawn":record["draws"],
        "gamesDraw":record["draws"],"gamesLost":record["losses"],
        "gamesPlayed":record["wins"]+record["draws"]+record["losses"],
        "record":{"won":record["wins"],"wins":record["wins"],
                  "draw":record["draws"],"draws":record["draws"],
                  "lost":record["losses"],"loss":record["losses"],
                  "losses":record["losses"]}}

def _native_account_info():
    last_access=int(STATE.get("last_access_time",now_s()))
    established=int(STATE.get("established",last_access))
    active_badge=next((item for item in _selected_onboarding_items()
                       if item.get("itemState") == "activeBadge"),None)
    badge_id=int((active_badge or {}).get("teamid",CLUB_ID) or CLUB_ID)
    club={"year":"2019","assetId":CLUB_ID,"teamId":CLUB_ID,
        "lastAccessTime":last_access,"platform":"pc","clubName":STATE.club_name(),
        "clubAbbr":STATE.club_abbr(),"established":established,
        "divisionOnline":10,"badgeId":badge_id,
        "squadListSize":MAX_SQUADS,"maxSquads":MAX_SQUADS,
        "maximumSquads":MAX_SQUADS,"squadSlots":MAX_SQUADS,
        "skuAccessList":{"FFA19PCC":last_access}}
    return {"userAccountInfo":{"personas":[{"personaId":PERSONA_ID,
        "personaName":STATE.persona_name(),"returningUser":1,"trial":False,
        "userState":None,"squadListSize":MAX_SQUADS,
        "maxSquads":MAX_SQUADS,"maximumSquads":MAX_SQUADS,
        "squadSlots":MAX_SQUADS,"userClubList":[club]}]}}

def _market_price_limits(source):
    return market_price_limits(source)

def _snap_market_price(value):
    return snap_market_price(value)

def _local_compare_price_payload(path):
    """Build deterministic local auctions for the native market screen.

    A two-byte or empty transfer-market response leaves the FIFA 19 console UI
    waiting indefinitely. The request's native ``type`` selects either the
    player catalogue or the deliberately bounded non-player catalogue.
    """
    raw_query=parse_qs(urlsplit(path).query)
    query={str(key).lower():values for key,values in raw_query.items()}
    # Compare Price sends an exact card identity in definitionId/defId (and
    # some companion builds use resourceId).  The retail player-name picker
    # instead sends the base player identity in maskedDefId.  Treating that
    # value as an exact definition bypassed the general search engine before
    # rare=SP, quality, club, league, position and price filters could run; a
    # Benzema + Special request therefore materialised twelve copies of his
    # Normal 84 card.  maskedDefId is deliberately left to
    # normalize_player_search(), which applies every filter to every sourced
    # version of that player.
    raw=(query.get("definitionid") or query.get("defid") or
         query.get("resourceid") or [0])[0]
    try: definition_id=int(raw or 0)
    except (TypeError,ValueError): definition_id=0
    definition_id=market_exact_resource_id(definition_id)
    try: start=max(0,int((query.get("start") or [0])[0] or 0))
    except (TypeError,ValueError): start=0
    try: requested=max(1,int((query.get("num") or [12])[0] or 12))
    except (TypeError,ValueError): requested=12
    requested=min(requested,21)

    response={"auctionInfo":[],"credits":STATE.credits(),"currencies":[],
              "bidTokens":{},"start":start,"num":requested}
    market_type=str((query.get("type") or query.get("itemtype") or
                     ["player"])[0] or "player").strip().lower()
    if market_type not in ("player","players"):
        generated=search_object_listings(
            raw_query,
            unavailable_trade_ids=STATE.market_unavailable_trade_ids())
        for listing in generated["listings"]:
            item=_native_item({**listing["fields"],
                               "id":listing["itemId"],"pile":5,
                               "timestamp":1_700_000_000})
            response["auctionInfo"].append({
                "tradeId":listing["tradeId"],"itemData":item,
                "tradeState":"active","bidState":"none",
                "buyNowPrice":listing["buyNowPrice"],"currentBid":0,
                "startingBid":listing["startingBid"],
                "expires":listing["expires"],"offers":0,
                "sellerEstablished":2019,"sellerId":0,
                "sellerName":"Local Market","tradeOwner":False,
                "confidenceValue":100})
        response.update({"start":generated["start"],"num":generated["num"],
                         "totalResults":generated["totalResults"],
                         "endOfList":generated["endOfList"]})
        return response
    if definition_id <= 0:
        unavailable=STATE.market_unavailable_trade_ids()
        generated=search_player_listings(
            raw_query,unavailable_trade_ids=unavailable)
        for listing in generated["listings"]:
            item=_native_player_item({**listing["fields"],
                                      "id":listing["itemId"],"pile":5,
                                      "timestamp":1_700_000_000})
            response["auctionInfo"].append({
                "tradeId":listing["tradeId"],"itemData":item,
                "tradeState":"active","bidState":"none",
                "buyNowPrice":listing["buyNowPrice"],"currentBid":0,
                "startingBid":listing["startingBid"],
                "expires":listing["expires"],"offers":0,
                "sellerEstablished":2019,"sellerId":0,
                "sellerName":"Local Market","tradeOwner":False,
                "confidenceValue":100})
        response.update({"start":generated["start"],"num":generated["num"],
                         "totalResults":generated["totalResults"],
                         "endOfList":generated["endOfList"]})
        return response

    rows=[]
    unavailable=STATE.market_unavailable_trade_ids()
    index=start
    # Skip already settled rows from this supply epoch and keep the requested
    # page full.  The next 15-minute epoch receives fresh deterministic IDs.
    while len(rows)<requested and index<start+requested+100:
        listing=exact_player_listing(definition_id,index)
        index+=1
        if int(listing["tradeId"]) in unavailable:
            continue
        fields=listing["fields"]
        buy_now=listing["buyNowPrice"]
        starting=listing["startingBid"]
        synthetic_id=listing["tradeId"]
        item=_native_player_item({**fields,"id":synthetic_id,
                                  "resourceId":definition_id,"pile":5,
                                  "timestamp":1_700_000_000})
        rows.append({"tradeId":synthetic_id,"itemData":item,
                     "tradeState":"active","bidState":"none",
                     "buyNowPrice":buy_now,"currentBid":0,
                     "startingBid":starting,"expires":listing["expires"],
                     "offers":0,"sellerEstablished":2019,
                     "sellerId":0,"sellerName":"Local Market",
                     "tradeOwner":False,"confidenceValue":100})
    response["auctionInfo"]=rows
    return response

def _native_player_item(source):
    source_rid=int(source.get("resourceId",source.get("assetId",0)) or 0)
    source_definition=int(source.get("definitionId",source_rid) or source_rid)
    # Older persisted PIM choices can carry the former Prime presentation ID
    # in resourceId while retaining the exact Moments definition.  The exact
    # definition remains authoritative and no state rewrite is required.
    rid=(source_definition
         if card_revision(source_definition)=="Prime Icon Moments"
         else source_rid)
    raw=native_player_fields(rid,source)
    wire_resource_id=int(player_wire_resource_id(rid))
    wire_rareflag=int(raw.get("rareflag",0) or 0)
    iid=int(source.get("id",source.get("itemId",0)) or 0)
    attributes=[0,0,0,0,0,0]
    for entry in raw.get("attributeList",[]):
        try:
            index=int(entry.get("index",-1)); value=int(entry.get("value",0) or 0)
        except (AttributeError,TypeError,ValueError):
            continue
        if 0 <= index < len(attributes):
            attributes[index]=value

    # Native DTO consumed by the console frontend. The previous object contained
    # Web App aliases (attributeList, itemId, teamId and pile="club"): its JSON
    # was valid, but FIFA stalled immediately after userMassInfo while building
    # the squad cards.
    raw_pile=raw.get("pile",7)
    if isinstance(raw_pile,str):
        pile={"trade":5,"transfer":5,"purchased":6,"club":7,
              "inbox":8,"gift":9}.get(raw_pile.lower(),7)
    else:
        try: pile=int(raw_pile)
        except (TypeError,ValueError): pile=7
    minimum_price,maximum_price=_market_price_limits(raw)
    return {
        "id":iid,"timestamp":int(raw.get("timestamp",now_s()) or now_s()),
        "formation":raw.get("formation","f442"),
        "untradeable":bool(raw.get("untradeable",False)),
        # PIM definitions were added after the launch player table.  assetId is
        # the independently verified regular-Icon identity used for the name
        # and detailed player data.
        "assetId":int(raw.get("assetId",0) or 0),
        "rating":int(raw.get("rating",0) or 0),"itemType":"player",
        # definitionId remains the exact source-backed PIM.  resourceId uses a
        # collision-free version encoding whose low 24 bits are the verified
        # launch Icon identity; the old client can therefore resolve the real
        # name/profile while its unique portrait request is remapped to the
        # exact Moments DDS.  Ordinary definitions remain unchanged.
        "resourceId":wire_resource_id,"definitionId":rid,
        "owners":int(raw.get("owners",1) or 0),
        "discardValue":int(raw.get("discardValue",0) or 0),
        "itemState":raw.get("itemState","free"),
        "cardsubtypeid":int(raw.get("cardsubtypeid",0) or 0),
        "lastSalePrice":int(raw.get("lastSalePrice",0) or 0),
        "marketDataMinPrice":minimum_price,
        "marketDataMaxPrice":maximum_price,
        "fitness":int(raw.get("fitness",99) or 0),
        "injuryType":raw.get("injuryType","none"),
        "injuryGames":int(raw.get("injuryGames",0) or 0),
        "preferredPosition":raw.get("preferredPosition",""),
        "training":int(raw.get("training",0) or 0),
        "contract":int(raw.get("contract",raw.get("contracts",7)) or 0),
        "teamid":int(raw.get("teamid",raw.get("teamId",0)) or 0),
        "rareflag":wire_rareflag,
        "playStyle":int(raw.get("playStyle",250) or 0),
        "leagueId":int(raw.get("leagueId",0) or 0),
        "assists":int(raw.get("assists",0) or 0),
        "lifetimeAssists":int(raw.get("lifetimeAssists",0) or 0),
        "loyaltyBonus":int(raw.get("loyaltyBonus",0) or 0),
        # Normal owned players use zero.  Serializing the internal sentinel -1
        # makes the native consumable screen classify them as loan players.
        "loans":max(0,int(raw.get("loans",0) if raw.get("loans",0) is not None else 0)),
        "pile":pile,"nation":int(raw.get("nation",0) or 0),
        # FIFA 19 uses two readers for the same item during bootstrap: the
        # console DTO accepts attributeArray, while the legacy card renderer
        # fills PAC/SHO/PAS/DRI/DEF/PHY from attributeList. Keeping both avoids
        # zero-stat cards without restoring unsafe aliases from the old payload
        # (string pile, itemId and teamId).
        "resourceGameYear":2019,"attributeArray":attributes,
        "attributeList":[{"index":i,"value":value}
                         for i,value in enumerate(attributes)],
        "statsArray":[0,0,0,0,0],"lifetimeStatsArray":[0,0,0,0,0],
        "skillmoves":int(raw.get("skillmoves",0) or 0),
        "weakfootabilitytypecode":int(raw.get("weakfootabilitytypecode",0) or 0),
        "attackingworkrate":int(raw.get("attackingworkrate",0) or 0),
        "defensiveworkrate":int(raw.get("defensiveworkrate",0) or 0),
        "trait1":int(raw.get("trait1",0) or 0),
        "trait2":int(raw.get("trait2",0) or 0),
        "preferredfoot":int(raw.get("preferredfoot",1) or 0),
    }

def _native_item(source):
    """Dispatch a persisted inventory row to its type-specific native DTO."""
    if stored_item_kind(source) == "player":
        return _native_player_item(source)
    item=native_object_item(source)
    # The onboarding fallback repairs an active cosmetic only while rendering
    # an owned Club row.  Applying it to a market or Purchased Items copy of
    # the same definition falsely moves that auction to pile 7 and can make a
    # newly bought badge/kit disappear from the assignment flow.
    if (stored_item_kind(source) in ("kit","badge") and
            int(item.get("pile",7) or 7)==7):
        kind=stored_item_kind(source)
        persisted_states={
            str(candidate.get("itemState",""))
            for candidate in STATE.items_in_pile("club",kind)
        }
        selected={int(x.get("resourceId",0) or 0):x
                  for x in _selected_onboarding_fallback_items()
                  if str(x.get("itemState","")) not in persisted_states}
        active=selected.get(int(item.get("resourceId",0) or 0))
        if active:
            item["itemState"]=active.get("itemState",item.get("itemState","free"))
            item["pile"]=7
    return item


def _native_market_auction(source):
    """Serialize one persisted or synthetic auction by its real item kind."""
    row=dict(source or {})
    trade_id=int(row.get("tradeId",0) or 0)
    item_source=dict(row.get("itemData") or {})
    item_id=int(row.get("itemId",item_source.get("id",trade_id)) or trade_id)
    if bool(row.get("sellerIsUser",False)):
        persisted=STATE.item(item_id)
        if persisted is not None:
            item_source=persisted
    item_source.update({"id":item_id,"itemId":item_id,"pile":"trade"})
    item=_native_item(item_source)
    state=str(row.get("tradeState",row.get("state","active"))).lower()
    target=str(row.get("targetStatus","")).upper()
    if target in ("WON","OUTBID","EXPIRED"):
        state="sold" if target=="WON" else "expired" if target=="EXPIRED" else state
    # CardsDLL's own trade-state enum is active/inactive/expired/closed.
    # ``closed`` is a completed sale (the UI says a buyer was found), whereas
    # an inventory item moved to the Transfer List but never auctioned is
    # ``inactive`` and an unsold ended auction is ``expired``.
    native_state={"active":"active","inactive":"inactive",
                  "unlisted":"inactive","expired":"expired"}.get(
                      state,"closed")
    bid_state=str(row.get("bidState","none") or "none").lower()
    return {
        "tradeId":trade_id,"itemData":item,"tradeState":native_state,
        "bidState":bid_state,"buyNowPrice":int(row.get("buyNowPrice",0) or 0),
        "currentBid":int(row.get("currentBid",0) or 0),
        "startingBid":int(row.get("startingBid",0) or 0),
        "expires":int(row.get("expires",-1) if row.get("expires") is not None else -1),
        "offers":int(row.get("offers",0) or 0),
        "watched":bool(row.get("watched",False)),
        "confidenceValue":int(row.get("confidenceValue",100) or 100),
        "timestamp":int(row.get("created",now_s()) or now_s()),
        "sellerEstablished":2019,
        "sellerId":int(row.get("sellerId",1 if row.get("sellerIsUser") else 0) or 0),
        "sellerName":str(row.get("sellerName") or
                         (STATE.persona_name() if row.get("sellerIsUser")
                          else "Local Market")),
        "tradeOwner":bool(row.get("sellerIsUser",False)),
    }


def _market_collection(rows, **extra):
    auctions=[_native_market_auction(row) for row in rows]
    return {"auctionInfo":auctions,"bidTokens":{},
            "totalResults":len(auctions),**_credits_payload(),**extra}


PLAYER_PICK_RESOURCE_ID=5004045
PLAYER_PICK_DEFINITION_ID=3002
PLAYER_PICK_ITEM_SUBTYPE=237


def _native_player_pick_item(pick):
    """Serialize a persisted choice as FIFA 19's official Pick Item #3."""
    pick_id=int(pick.get("playerPickId",pick.get("id",0)) or 0)
    pack=pack_definition(int(pick.get("packId",0) or 0)) or {}
    config=pack.get("playerPickBundle") or {}
    pick_name=str(pick.get("name") or config.get("pickName") or "Player Pick")
    pick_description=str(pick.get("description") or config.get("pickDescription") or
        "Choose 1 of %d players rated %d or higher." % (
            int(pick.get("optionCount",len(pick.get("options",[]))) or 0),
            int(pick.get("minRating",81) or 81)))
    return {
        "id":pick_id,"itemId":pick_id,"timestamp":int(pick.get("timestamp",0) or 0),
        "formation":"f442","untradeable":True,
        "assetId":PLAYER_PICK_RESOURCE_ID,"definitionId":PLAYER_PICK_RESOURCE_ID,
        "resourceId":PLAYER_PICK_RESOURCE_ID,"rating":99,"rareflag":1,
        "itemType":"misc","itemState":"free",
        "cardsubtypeid":PLAYER_PICK_ITEM_SUBTYPE,
        "owners":1,"discardValue":0,"lastSalePrice":0,"pile":6,
        "resourceGameYear":2019,"amount":PLAYER_PICK_DEFINITION_ID,
        "value":PLAYER_PICK_DEFINITION_ID,"playerPickId":pick_id,
        "playerPickDefinitionId":PLAYER_PICK_DEFINITION_ID,
        "name":pick_name,"description":pick_description,
        "packId":int(pick.get("packId",0) or 0),
        "optionCount":int(pick.get("optionCount",len(pick.get("options",[]))) or 0),
    }


def _pending_player_pick_items():
    return [_native_player_pick_item(pick) for pick in STATE.pending_player_picks()]


def _pending_player_pick(pick_id):
    return next((pick for pick in STATE.pending_player_picks()
                 if int(pick.get("playerPickId",pick.get("id",0)) or 0)==int(pick_id)),None)


def _acknowledge_repeatable_sbc_reward(reward):
    """Acknowledge rewards left COMPLETED by an older persisted profile."""
    reward=reward if isinstance(reward,dict) else {}
    source=reward.get("rewardSource")
    source=source if isinstance(source,dict) else reward
    try:
        set_id=int(source.get("setId",0) or 0)
        challenge_id=int(source.get("challengeId",0) or 0)
        attempt=int(source.get("attempt",0) or 0)
    except (TypeError,ValueError):
        set_id=challenge_id=attempt=0
    if not set_id or not challenge_id:
        match=_re.match(r"^sbc:(\d+):(\d+):(\d+)(?::|$)",
                        str(reward.get("sourceKey","") or ""))
        if match:
            set_id,challenge_id,attempt=(int(value) for value in match.groups())
    resolved=_sbc_challenge_spec(challenge_id) if challenge_id else None
    if (not resolved or int(resolved[0].get("setId",0) or 0)!=set_id or
            not bool(resolved[1].get("repeatable",False))):
        return 0
    group_reward=str(source.get("source","")).lower()=="sbc_set"
    set_spec=resolved[0]
    if (not group_reward and
            len(list(set_spec.get("challenges",[]) or []))>1 and
            list(set_spec.get("rewards",[]) or [])):
        # Child packs can be used before the last challenge is submitted. The
        # set-level reward, not a child pack, is the only safe boundary between
        # two attempts of a repeatable multi-challenge group.
        return 0
    return STATE.mark_sbc_reward_claimed(
        set_id,None if group_reward else challenge_id,attempt or None)


def _active_player_pick_payload():
    return _native_player_pick_payload(STATE.active_player_pick())


def _native_player_pick_payload(pick):
    if not pick:
        return None
    native=dict(pick)
    native["options"]=[_native_player_item({**option,"pile":"purchased"})
                       for option in pick.get("options",[])]
    native["optionCount"]=len(native["options"])
    return native


def _apply_consumable(resource_id, payload):
    """Validate, apply and consume one FIFA 19 consumable definition."""
    definition=object_definition(resource_id) or {}
    definition_type=str(definition.get("ItemType", ""))
    amount=int(definition.get("Amount",0) or 0)
    targets=[]
    for row in payload.get("apply",[]) if isinstance(payload,dict) else []:
        try: target_id=int(row.get("id",0) or 0)
        except (AttributeError,TypeError,ValueError): continue
        target=STATE.item(target_id)
        if target: targets.append((target_id,target))
    if not definition or not targets:
        return None,461

    changes={}
    for target_id,target in targets:
        kind=stored_item_kind(target)
        update={}
        if definition_type == "ContractPlayer" and kind == "player":
            if int(target.get("loans",0) or 0)>0:
                return None,478
            rating=int(target.get("rating",0) or 0)
            tier_key="Bronze" if rating<65 else ("Silver" if rating<75 else "Gold")
            increment=int(definition.get(tier_key,amount) or amount)
            update["contract"]=min(99,int(target.get("contract",7) or 0)+increment)
            update["contracts"]=update["contract"]
        elif definition_type == "ContractStaff" and kind == "manager":
            rating=int(target.get("rating",target.get("value",0)) or 0)
            tier_key="Bronze" if rating<65 else ("Silver" if rating<75 else "Gold")
            increment=int(definition.get(tier_key,amount) or amount)
            update["contract"]=min(99,int(target.get("contract",30) or 0)+increment)
            update["contracts"]=update["contract"]
        elif definition_type in ("FitnessPlayer","FitnessTeam") and kind == "player":
            update["fitness"]=min(99,int(target.get("fitness",0) or 0)+amount)
        elif definition_type.startswith("Health") and kind == "player":
            remaining=max(0,int(target.get("injuryGames",0) or 0)-amount)
            update.update({"injuryGames":remaining,
                           "injuryType":target.get("injuryType","none") if remaining else "none"})
        elif definition_type.startswith("TrainingPlayerPos") and kind == "player":
            position=definition_type[len("TrainingPlayerPos"):].split("_")[-1]
            update["preferredPosition"]=position
        elif definition_type.startswith("TrainingPlaystyle") and kind == "player":
            update["playStyle"]=int(definition_type[len("TrainingPlaystyle"):])
        elif (definition_type.startswith("TrainingPlayer") or
              definition_type.startswith("TrainingGk")) and kind == "player":
            update["training"]=amount
        elif definition_type == "TrainingLeagueModifier" and kind == "manager":
            update["leagueId"]=amount
        else:
            return None,461
        changes[target_id]=update
    applied=STATE.apply_consumable_resource(resource_id,changes)
    return applied,(0 if applied else 461)

def _player_duplicate_key(item):
    """Return the exact inventory identity used by duplicate handling."""
    # PIM cards retain their exact sourced definitionId but are serialized
    # with a private version-encoded resourceId for this launch client.
    # Comparing resourceId therefore misses an identical PIM already in the
    # club. definitionId is stable for ordinary players and PIM alike.
    definition_id=int(item.get(
        "definitionId",item.get("resourceId",0)) or 0)
    revision=str(item.get("presentationRevision",item.get(
        "cardRevision","")) or "").strip().lower()
    presentation_rareflag=int(item.get(
        "presentationRareflag",item.get("rareflag",0)) or 0)
    # A FUT Champions reward intentionally reuses the sourced IF/SIF/TIF
    # definition so it keeps the exact player attributes.  The retail
    # inventory nevertheless treats the red rarity as a separate card:
    # owning the black TOTW must not make the red reward a duplicate, while a
    # second copy of that same red reward still must be linked.
    presentation=(FUT_CHAMPIONS_RARITY_ID if (
        presentation_rareflag == FUT_CHAMPIONS_RARITY_ID or
        revision == "fut champions") else 0)
    return definition_id,presentation


def _duplicate_item_links(items):
    """Return the native unassigned-to-club duplicate relationships."""

    club_by_definition={_player_duplicate_key(x):int(x.get("id",0) or 0)
                        for x in STATE.items_in_pile("club","player")}
    unassigned_by_definition={}
    for existing in STATE.items_in_pile("purchased","player"):
        key=_player_duplicate_key(existing)
        item_id=int(existing.get("id",0) or 0)
        if key and item_id:
            unassigned_by_definition.setdefault(key,item_id)
    links=[]
    for item in items:
        if str(item.get("itemType","player")).lower() != "player":
            continue
        item_id=int(item.get("id",0) or 0)
        key=_player_duplicate_key(item)
        existing=(club_by_definition.get(key) or
                  unassigned_by_definition.get(key))
        if existing == item_id:
            existing=next((int(candidate.get("id",0) or 0)
                for candidate in STATE.items_in_pile("purchased","player")
                if _player_duplicate_key(candidate)==key and
                   int(candidate.get("id",0) or 0)!=item_id),None)
        if existing and existing != item_id:
            links.append({"itemId":item_id,
                          "duplicateItemId":existing})
    return links

# The native FIFA 19 client does not use the generic Web App response for the
# first kit selection: RS4:FutGetOnboardingKitsResponse explicitly expects
# homeItemDataList/awayItemDataList. These team IDs come from the installed
# FIFA 19 Frostbite database, not invented IDs or FIFA 20 assets.
# Live validation on 2026-08-20 also checked each rendered item: 484, 40 and 16
# produced blank kits and unresolved localization keys in this build. Milan
# (47), Juventus (45) and Lazio (46) are confirmed by the starter-squad assets.
ONBOARDING_TEAM_IDS = (1, 480, 47, 144, 88, 45, 46, 10, 8)

def _onboarding_item_id(kind, team_id):
    # onboardingClientData stores these values in the console profile. Keeping
    # them below INT32_MAX prevents truncation in legacy screen readers.
    bases={"home":2100000000,"away":2110000000,"badge":2120000000}
    return bases[kind]+int(team_id)

def _native_kit_item(team_id, category, active=False):
    """Console DTO for a real kit embedded in the FIFA 19 database."""
    team_id=int(team_id)
    home=int(category)==2
    kind="home" if home else "away"
    return {
        "id":_onboarding_item_id(kind,team_id),"timestamp":now_s(),
        "formation":"f442","untradeable":True,
        # 14/15 are the home/away kit-card assets used by the native renderer;
        # teamid+category select the team texture from the local game database.
        "assetId":14 if home else 15,"rating":75,"itemType":"kit",
        "resourceId":(6300000 if home else 6400000)+team_id,
        "owners":1,"discardValue":0,
        "itemState":("activeHomeKit" if home else "activeAwayKit")
                    if active else "free",
        "cardsubtypeid":9,"lastSalePrice":0,
        "statsList":[],"lifetimeStats":[],"attributeList":[],
        "teamid":team_id,"rareflag":0,"leagueId":0,"pile":7 if active else 6,
        "category":2 if home else 3,"name":"TeamName_Abbr15_%d"%team_id,
        "year":0,"resourceGameYear":2019,
    }

def _native_badge_item(team_id, active=False):
    """Console DTO for a badge; FUT classifies badges as custom items."""
    team_id=int(team_id)
    return {
        "id":_onboarding_item_id("badge",team_id),"timestamp":now_s(),
        "formation":"f442","untradeable":True,"assetId":team_id,
        "rating":75,"itemType":"custom","resourceId":6000000+team_id,
        "owners":1,"discardValue":0,
        "itemState":"activeBadge" if active else "free",
        "cardsubtypeid":11,"lastSalePrice":0,
        "statsList":[],"lifetimeStats":[],"attributeList":[],
        "teamid":team_id,"rareflag":0,"leagueId":0,"pile":7 if active else 6,
        "cardassetid":39,"value":75,"category":1,
        "name":"TeamName_Abbr15_%d"%team_id,"weightrare":0,
        "description":"TeamName_Abbr15_%d"%team_id,"header":"Badge",
        "biodescription":"TeamName_Abbr15_%d"%team_id,"chantsCount":0,
        "resourceGameYear":2019,
    }

def _native_onboarding_kits():
    return {
        "homeItemDataList":[_native_kit_item(t,2) for t in ONBOARDING_TEAM_IDS],
        "awayItemDataList":[_native_kit_item(t,3) for t in ONBOARDING_TEAM_IDS],
    }

def _native_onboarding_badges():
    return {"badgeItemDataList":[_native_badge_item(t) for t in ONBOARDING_TEAM_IDS]}

def _onboarding_candidate(item_id, kind=None):
    try: item_id=int(item_id)
    except (TypeError,ValueError): return None
    for team_id in ONBOARDING_TEAM_IDS:
        candidates={
            "home":_native_kit_item(team_id,2),
            "away":_native_kit_item(team_id,3),
            "badge":_native_badge_item(team_id),
        }
        if kind in candidates:
            candidates={kind:candidates[kind]}
        for candidate in candidates.values():
            accepted={candidate["id"],candidate["resourceId"]}
            if candidate["itemType"]=="custom":
                accepted.add(candidate["assetId"])
                accepted.add(candidate["teamid"])
            if item_id in accepted:
                return candidate
    return None

def _selected_onboarding_fallback_items():
    selected=[]
    for key,kind in (("onboarding_home_kit_id","home"),
                     ("onboarding_away_kit_id","away"),
                     ("onboarding_badge_id","badge")):
        item=_onboarding_candidate(STATE.get(key,0),kind)
        if item:
            item=dict(item); item["pile"]=7
            item["itemState"]={"home":"activeHomeKit","away":"activeAwayKit",
                               "badge":"activeBadge"}[kind]
            selected.append(item)
    return selected

def _selected_onboarding_items():
    """Return every persisted club active plus safe onboarding fallbacks."""
    active=[]
    active_states=set()
    valid_states={"activeHomeKit","activeAwayKit","activeBadge",
                  "activeStadium","activeBall"}
    for kind in ("kit","badge","stadium","ball"):
        for source in STATE.items_in_pile("club",kind):
            state=str(source.get("itemState",""))
            if state in valid_states and state not in active_states:
                active.append(native_object_item(source))
                active_states.add(state)
    for fallback in _selected_onboarding_fallback_items():
        state=str(fallback.get("itemState",""))
        if state not in active_states:
            active.append(fallback); active_states.add(state)
    return active

def _query_first(query,*names,default=""):
    for name in names:
        values=query.get(str(name).lower())
        if values:
            value=str(values[0]).strip()
            if value:
                return value
    return default

def _query_integer(query,*names,default=0,minimum=None,maximum=None):
    try: value=int(_query_first(query,*names,default=str(default)) or default)
    except (TypeError,ValueError): value=int(default)
    if minimum is not None: value=max(int(minimum),value)
    if maximum is not None: value=min(int(maximum),value)
    return value

def _normalized_search_name(value):
    return _re.sub(r"[^a-z0-9]","",str(value or "").lower())

def _object_category_matches(item,category):
    category=_normalized_search_name(category)
    if not category or category in ("any","all"):
        return True
    inventory_type=stored_item_kind(item)
    catalog_type=_normalized_search_name(item.get("catalogType",""))
    item_type=_normalized_search_name(item.get("itemType",""))
    subtype=int(item.get("cardsubtypeid",0) or 0)
    aliases={
        "manager":inventory_type=="manager",
        "headcoach":catalog_type=="headcoach",
        "gkcoach":catalog_type=="gkcoach",
        "fitnesscoach":catalog_type=="fitnesscoach",
        "physio":catalog_type=="physio",
        "kit":inventory_type=="kit",
        "badge":inventory_type=="badge",
        "custom":inventory_type=="badge",
        "stadium":inventory_type=="stadium",
        "ball":inventory_type=="ball",
        "contract":subtype in (201,202),
        "fitness":subtype in (219,220),
        "healing":211<=subtype<=218,
        "playertraining":61<=subtype<=67,
        "gktraining":51<=subtype<=57,
        "position":91<=subtype<=110,
        "managerformation":121<=subtype<=136,
        "playstyle":250<=subtype<=273,
        "managerleaguemodifier":300<=subtype<=399,
        "draft":subtype==236,
        "drafttoken":subtype==236,
    }
    return bool(aliases.get(category,
        catalog_type==category or item_type==category))


def _supported_club_object(item):
    """Hide source rows that the FIFA 19 card factory cannot render safely."""
    kind=stored_item_kind(item)
    if kind == "ball":
        # The backend retains the active match ball, but this PC build has no
        # validated Club Items card contract for BallName_* definitions. An
        # untyped row renders blank; a generic subtype routes to the wrong card
        # factory. Do not advertise an unusable selector until that contract is
        # established from the native client.
        return False
    if kind != "consumable":
        return True
    definition_type=str(item.get("definitionItemType","") or "")
    if definition_type.startswith("TrainingManagerFormation"):
        # Manager formation cards were retired before FUT 19. Their legacy
        # source rows render as an undefined common Gold card in this client.
        return False
    if definition_type == "TrainingLeagueModifier":
        # Internal/custom leagues share the public definition type but render
        # as an empty black silhouette. FIFA 19's real modifiers stop at 2076.
        try:
            return 0 < int(item.get("amount",0) or 0) <= 2076
        except (TypeError,ValueError):
            return False
    return True


def _club_object_items(wanted,query):
    """Return filtered, deterministically ordered non-player My Club DTOs."""
    wanted=_normalized_search_name(wanted)
    wanted={"stadia":"stadium","stadiums":"stadium",
            "balls":"ball",
            # In the retail Club Items search, "Any" is sent as
            # ``type=equippables``.  It means equippable club cosmetics, not
            # consumable cards.
            "equippable":"clubitems","equippables":"clubitems",
            # The consumables delegate uses the plural URL suffix.
            "contracts":"contract"}.get(wanted,wanted)
    category=_query_first(query,"category","subtype","itemtype")
    category_names={
        "contract","fitness","healing","playertraining","gktraining",
        "position","managerformation","playstyle","managerleaguemodifier",
        "draft","drafttoken",
    }
    if wanted in category_names:
        category=wanted; wanted="consumable"

    stored=[]
    if wanted == "manager":
        stored=STATE.items_in_pile("club","manager")
    elif wanted in ("staff","coaches"):
        stored=(STATE.items_in_pile("club","manager")+
                STATE.items_in_pile("club","staff"))
    elif wanted in ("headcoach","gkcoach","fitnesscoach","physio"):
        stored=STATE.items_in_pile("club","staff")
        category=wanted
    elif wanted == "kit":
        stored=STATE.items_in_pile("club","kit")
    elif wanted in ("badge","custom"):
        stored=STATE.items_in_pile("club","badge")
    elif wanted == "stadium":
        stored=STATE.items_in_pile("club","stadium")
    elif wanted == "ball":
        stored=STATE.items_in_pile("club","ball")
    elif wanted in ("clubinfo","clubitem","clubitems"):
        for kind in ("kit","badge","stadium","ball"):
            stored.extend(STATE.items_in_pile("club",kind))
    elif wanted in ("development","training"):
        stored=STATE.items_in_pile("club","consumable")
        stored=[item for item in stored
                if _normalized_search_name(item.get("itemType")) == wanted or
                (wanted=="development" and
                 _normalized_search_name(item.get("itemType")) in
                 ("contract","health"))]
    elif wanted in ("consumable","consumables"):
        stored=STATE.items_in_pile("club","consumable")
    else:
        return []

    stored=[item for item in stored if _supported_club_object(item)]
    if category:
        stored=[item for item in stored if _object_category_matches(item,category)]
    items=[_native_item(item) for item in stored]

    # Onboarding selections predate the catalogue migration and use fixed
    # client-safe IDs. Keep an active choice visible if its team was not among
    # the 100 default kit/badge grants, without manufacturing a DB duplicate.
    if wanted in ("kit","badge","custom","clubinfo","clubitem","clubitems"):
        owned={int(item.get("resourceId",0) or 0) for item in items}
        for selected in _selected_onboarding_items():
            if ((wanted=="kit" and selected.get("itemType")!="kit") or
                (wanted in ("badge","custom") and selected.get("itemType")!="custom")):
                continue
            if int(selected.get("resourceId",0) or 0) not in owned:
                items.append(selected)

    def numeric_filter(names,field):
        raw=_query_first(query,*names)
        if not raw or raw.lower() in ("any","-1"):
            return
        try: expected=int(raw)
        except ValueError: return
        items[:]=[item for item in items if int(item.get(field,0) or 0)==expected]

    numeric_filter(("defid","definitionid","maskeddefid","resourceid"),"resourceId")
    numeric_filter(("assetid",),"assetId")
    numeric_filter(("nationality","nation","nationid"),"nation")
    numeric_filter(("league","leagueid"),"leagueId")
    numeric_filter(("club","clubid","teamid"),"teamid")

    quality=_normalized_search_name(_query_first(query,"level","quality"))
    if quality and quality not in ("any","0"):
        if quality in ("bronze","1"):
            items[:]=[item for item in items if int(item.get("rating",0) or 0)<65]
        elif quality in ("silver","2"):
            items[:]=[item for item in items
                      if 65<=int(item.get("rating",0) or 0)<75]
        elif quality in ("gold","3"):
            items[:]=[item for item in items if int(item.get("rating",0) or 0)>=75]
        elif quality in ("special","sp"):
            items[:]=[item for item in items if int(item.get("rareflag",0) or 0)>1]
    rare=_normalized_search_name(_query_first(query,"rare"))
    if rare in ("1","true","yes","rare"):
        items[:]=[item for item in items if int(item.get("rareflag",0) or 0)>0]
    elif rare in ("0","false","no","common"):
        items[:]=[item for item in items if int(item.get("rareflag",0) or 0)==0]

    items.sort(key=lambda item:(-int(item.get("rating",0) or 0),
                                str(item.get("lastName",item.get("name",""))).casefold(),
                                int(item.get("resourceId",0) or 0),
                                int(item.get("id",0) or 0)))
    return items

def _club_page_payload(items,query):
    start=_query_integer(query,"start","offset",default=0,minimum=0)
    count=_query_integer(query,"count","num",default=50,minimum=1,maximum=100)
    page=items[start:start+count]
    return {"itemData":page,"totalItemCount":len(items),
            "itemCount":len(page),"start":start,"count":count}

def _annotate_consumable_counts(items):
    """Publish how many of each consumable the club owns.

    The dedicated /club/consumables endpoint stacks rows and carries the total
    in `count`. The generic /club route returns one row per instance with
    `stackCount` instead - and `stackCount` is not a key this client knows, so
    the screen that applies a consumable to a player had nothing to read and
    showed 0 for every one of them. `count` and `untradeableCount` are both
    real client keys (222 and 1100).
    """
    totals={}
    owned={}
    for item in items:
        resource_id=int(item.get("resourceId",0) or 0)
        if resource_id<=0:
            continue
        stack=max(1,int(item.get("stackCount",1) or 1))
        totals[resource_id]=totals.get(resource_id,0)+stack
        if bool(item.get("untradeable",False)):
            owned[resource_id]=owned.get(resource_id,0)+stack
    for item in items:
        resource_id=int(item.get("resourceId",0) or 0)
        if resource_id<=0:
            continue
        item["count"]=totals.get(resource_id,1)
        item["untradeableCount"]=owned.get(resource_id,0)
    return items


def _club_consumables_payload(wanted,query):
    """Return the native stacked consumable envelope, one row per resource.

    ClubConsumableSearchDelegate does not consume ordinary ItemData rows.  A
    row with ``count`` is dispatched to createConsumable(), which expects the
    definition under ``item`` plus aggregate count/untradeableCount fields.
    The SQLite inventory deliberately remains per-instance so consuming one
    card and restarting cannot refill the stack.
    """
    # The retail ClubConsumableSearchDelegate always calls the historical
    # `/club/consumables/development` URL once, then separates development and
    # training locally.  Treating that suffix as a server filter loaded only
    # contracts/fitness/healing and left Chemistry, Positioning and Training
    # tabs at zero.
    if _normalized_search_name(wanted) in ("development","training"):
        wanted="consumable"
    items=_club_object_items(wanted,query)
    grouped={}
    for item in items:
        resource_id=int(item.get("resourceId",0) or 0)
        if resource_id <= 0:
            continue
        entry=grouped.get(resource_id)
        stack=max(1,int(item.get("stackCount",1) or 1))
        raw_untradeable=item.get("untradeableCount")
        if raw_untradeable is None:
            untradeable=stack if bool(item.get("untradeable",False)) else 0
        else:
            untradeable=max(0,int(raw_untradeable or 0))
        if entry is None:
            inner=dict(item)
            inner.pop("stackCount",None)
            inner.pop("untradeableCount",None)
            entry={"count":0,"discardValue":int(
                       item.get("discardValue",0) or 0),
                   "item":inner,"resourceId":resource_id,
                   "untradeableCount":0}
            grouped[resource_id]=entry
        entry["count"]+=stack
        entry["untradeableCount"]+=min(stack,untradeable)
    rows=sorted(grouped.values(),key=lambda entry:(
        -int(entry["item"].get("rating",0) or 0),
        str(entry["item"].get("name","" )).casefold(),
        int(entry["resourceId"])))
    # These endpoints are non-paginated in the retail delegate, even if a
    # generic caller happens to append count/start.  Returning a slice makes
    # the repository mark itself complete and permanently hides definitions.
    return {"itemData":rows,"totalItemCount":len(rows),
            "itemCount":len(rows),"start":0,"count":len(rows)}

def _club_staff_stats_payload():
    """Build the exact typed staff bonuses consumed by FIFA's stats parser."""
    player_types=("pace","shooting","passing","dribbling","defending","heading")
    gk_types=("gkDiving","gkHandling","gkKicking","gkReflexes",
              "gkOneOnOne","gkPositioning")
    physio_types=("physioArm","physioBack","physioFoot","physioHead",
                  "physioHip","physioLeg","physioShoudler")
    totals={name:0 for name in player_types+gk_types+physio_types+
            ("fitness","managerTalk","physioAll","contract")}
    managers=STATE.items_in_pile("club","manager")
    for manager in managers:
        totals["contract"]+=int(manager.get("negotiation",0) or 0)
        totals["managerTalk"]+=int(manager.get("talkRating",0) or 0)
    for item in STATE.items_in_pile("club","staff"):
        kind=_normalized_search_name(item.get("catalogType",""))
        amount=max(0,int(item.get("amount",0) or 0))
        try: attribute=int(item.get("attr",-1))
        except (TypeError,ValueError): attribute=-1
        if kind == "headcoach" and 0 <= attribute < len(player_types):
            totals[player_types[attribute]]+=amount
        elif kind == "gkcoach" and 0 <= attribute < len(gk_types):
            totals[gk_types[attribute]]+=amount
        elif kind == "physio" and 0 <= attribute < len(physio_types):
            totals[physio_types[attribute]]+=amount
        elif kind == "fitnesscoach":
            totals["fitness"]+=amount
    bonus=[{"type":name,"value":min(50,value)}
           for name,value in totals.items()]
    return {"bonus":bonus,"totalItemCount":len(managers)+len(
        STATE.items_in_pile("club","staff")),"itemCount":len(managers)+len(
        STATE.items_in_pile("club","staff"))}

def _club_consumable_stats_payload():
    """Count the club's consumables under the names this client reads.

    The tab headers of the APPLY CONSUMABLE screen all read 0 while the cards
    below them showed the right quantity. The counts were published as
    `contracts`, `fitness`, `playStyle` and so on, and none of those is a key
    the client knows: its interned table names every one of them with a
    `consumables` prefix - `consumablesContract` (200), `consumablesFitness`
    (202), `consumablesHealing` (209), `consumablesPosition` (212),
    `consumablesTraining` (201) and the finer buckets beside them.
    """
    counts={"contracts":0,"fitness":0,"healing":0,
            "managerLeagueModifier":0,"playStyle":0,"position":0,
            "training":0}
    consumables=_club_object_items("consumable",{})
    for item in consumables:
        subtype=int(item.get("cardsubtypeid",0) or 0)
        stack=max(1,int(item.get("stackCount",1) or 1))
        if subtype in (201,202): counts["contracts"]+=stack
        elif subtype in (219,220): counts["fitness"]+=stack
        elif 211 <= subtype <= 218: counts["healing"]+=stack
        elif 300 <= subtype <= 399: counts["managerLeagueModifier"]+=stack
        elif 250 <= subtype <= 273: counts["playStyle"]+=stack
        elif 91 <= subtype <= 110: counts["position"]+=stack
        elif 51 <= subtype <= 67: counts["training"]+=stack
    counts["totalItemCount"]=len(consumables)
    counts["rareCount"]=sum(int(item.get("rareflag",0) or 0)>0
                            for item in consumables)
    # The names the screen actually reads. The legacy spellings stay: an
    # unread member is skipped safely, and other callers already use them.
    counts.update({
        "consumablesContract":counts["contracts"],
        "consumablesContractPlayer":counts["contracts"],
        "consumablesContractManager":0,
        "consumablesFitness":counts["fitness"],
        "consumablesFitnessPlayer":counts["fitness"],
        "consumablesFitnessTeam":0,
        "consumablesHealing":counts["healing"],
        "consumablesPosition":counts["position"],
        "consumablesTraining":counts["training"]+counts["playStyle"]+
                              counts["managerLeagueModifier"],
        "consumablesTrainingPlayer":counts["training"],
        "consumablesTrainingPlayerPlayStyle":counts["playStyle"],
        "consumablesTrainingGk":0,
        "consumablesTrainingGkPlayStyle":0,
        "consumablesTrainingManager":counts["managerLeagueModifier"],
        "consumablesTrainingManagerLeagueModifier":
            counts["managerLeagueModifier"],
        "consumablesFormationManager":0,
    })
    return counts

def _club_year_stats_payload():
    """Advertise the real 2019 inventory buckets used by club search."""
    counts=STATE.inventory_counts("club")
    counts["ball"]=0
    counts["consumable"]=sum(
        _supported_club_object(item)
        for item in STATE.items_in_pile("club","consumable"))
    ordered=("player","manager","staff","consumable","kit","badge",
             "stadium","ball")
    type_counts=[{"type":kind,"year":2019,"count":int(counts.get(kind,0))}
                 for kind in ordered]
    players=STATE.items_in_pile("club","player")
    position_counts=[]
    for position in sorted({str(item.get("preferredPosition",""))
                            for item in players if item.get("preferredPosition")}):
        position_counts.append({"position":position,"year":2019,
            "count":sum(str(item.get("preferredPosition","")) == position
                        for item in players)})
    total=sum(int(counts.get(kind,0)) for kind in ordered)
    return {"yearCount":[{"year":2019,"count":total}],
            "itemTypeCount":type_counts,"typeCount":type_counts,
            "positionCount":position_counts,
            "rareCount":sum(int(item.get("rareflag",0) or 0)>0 and
                            _supported_club_object(item)
                            for kind in ordered
                            for item in STATE.items_in_pile("club",kind)),
            "totalItemCount":total,"itemCount":total}

def _onboarding_client_entries():
    """Persistent onboarding state consumed by the FUT console bootstrap."""
    has_completed_selection=all(int(STATE.get(key,0) or 0) for key in (
        "onboarding_loan_resource_id","onboarding_home_kit_id",
        "onboarding_away_kit_id","onboarding_badge_id"))
    # Migration for a live session that completed every step before
    # PUT /clientdata/onboarding was persisted correctly.
    stage=int(STATE.get("onboarding_stage",2 if has_completed_selection else 0) or 0)
    return [
        {"key":0,"value":stage},
        {"key":1,"value":int(STATE.get("onboarding_home_kit_id",0) or 0)},
        {"key":2,"value":int(STATE.get("onboarding_away_kit_id",0) or 0)},
        {"key":3,"value":int(STATE.get("onboarding_badge_id",0) or 0)},
        {"key":4,"value":int(STATE.get("onboarding_aux_state",0) or 0)},
    ]

def _native_squad_summary(sq):
    return {"id":int(sq.get("id",0) or 0),"personaId":PERSONA_ID,
        "squadName":sq.get("squadName","Squad"),"formation":sq.get("formation","f433"),
        "active":bool(sq.get("active",False)),"changed":False,
        "chemistry":int(sq.get("chemistry",100) or 0),"rating":int(sq.get("rating",0) or 0),
        "starRating":int(sq.get("starRating",5) or 0),"valid":True,
        "newSquad":False}

def _native_squad_list_payload():
    squads = STATE.squads()
    active = STATE.active_squad()
    # squadListSize stays the capacity, never the live count: the client reads it
    # in the same settings block as TRADE_PILE_SIZE and WATCH_LIST_SIZE, which are
    # both capacities, so reporting the count there caps the account at one squad.
    return {"squad":[_native_squad_summary(s) for s in squads],
            "activeSquadId":active.get("id",0),
            "maxSquads":MAX_SQUADS,"maximumSquads":MAX_SQUADS,
            "squadSlots":MAX_SQUADS,"squadLimit":MAX_SQUADS,
            "squadListSize":MAX_SQUADS}

def _native_squad_json(sq):
    item_by_id={int(x.get("id",0) or 0):x
                for x in STATE.items_in_pile(item_kind="player")}
    positions={}
    for p in sq.get("players",[]):
        idv=p.get("itemData",{}).get("id") or p.get("itemId") or p.get("id")
        try: iid=int(idv); idx=int(p.get("index",len(positions)))
        except (TypeError,ValueError): continue
        if iid in item_by_id:
            item_data=_native_player_item(item_by_id[iid])
            positions[idx]={"index":idx,"kitNumber":int(p.get("kitNumber",idx+1) or 0),
                "loyaltyBonus":int(item_data.get("loyaltyBonus",0) or 0),
                "itemData":item_data}
    players=[positions.get(idx,{"index":idx,"kitNumber":0}) for idx in range(23)]
    populated=[x for x in players if isinstance(x.get("itemData"),dict)]
    captain=int(sq.get("captain",populated[0]["itemData"]["id"] if populated else 0) or 0)
    ratings=[int(x["itemData"].get("rating",0) or 0) for x in populated]
    rating=round(sum(ratings[:11])/max(1,len(ratings[:11]))) if ratings else 0
    manager_id=0
    raw_manager=sq.get("manager",[])
    if isinstance(raw_manager,dict):
        raw_manager=[raw_manager]
    if isinstance(raw_manager,list) and raw_manager:
        candidate=raw_manager[0]
        if isinstance(candidate,dict):
            candidate=candidate.get("id",candidate.get("itemId",0))
        try: manager_id=int(candidate or 0)
        except (TypeError,ValueError): manager_id=0
    manager_source=next((item for item in STATE.items_in_pile("club","manager")
                         if int(item.get("id",0) or 0) == manager_id),None)
    if manager_source is None:
        manager_id=0; manager=[]
    else:
        manager=[_native_item(manager_source)]
    return {"id":int(sq.get("id",0) or 0),"personaId":PERSONA_ID,"valid":True,
            "squadName":sq.get("squadName","Squad"),"formation":sq.get("formation","f433"),
            "players":players,"captain":captain,"coachId":manager_id,"chemistry":100,
            "rating":rating,"changed":False,"starRating":rating,
            "newSquad":False,"squadType":"REGULAR_SQUAD",
            "custom":"[0,0,0,0,0,0,0,0,0,0,0]","dreamSquad":None,
            "manager":manager,"actives":_selected_onboarding_items(),"club":[],
            "kicktakers":[],"tactics":[]}


def _native_controlled_match_squad(squad_id):
    """Return the owned HOME squad used by the offline match controller.

    Squad-detail and CreateMatch are two different native consumers.  The
    latter needs all five active presentation items, a manager and native
    1..5 ``starRating`` semantics.  Build a transient copy only: no generated
    opponent item can enter My Squads and no persistent owned item is changed.
    """
    try:
        wanted=int(squad_id)
    except (TypeError,ValueError):
        return {}
    stored=next((row for row in STATE.squads()
                 if int(row.get("id",row.get("squadId",0)) or 0)==wanted),None)
    if stored is None:
        return {}
    squad=_native_squad_json(stored)
    populated=[row.get("itemData") for row in squad.get("players",[])[:11]
               if isinstance(row.get("itemData"),dict)]
    if not populated:
        return {}
    selected=list(squad.get("actives",[]) or [])
    team_id=next((int(item.get("teamid",0) or 0) for item in selected
                  if str(item.get("itemState","")) in (
                      "activeHomeKit","activeAwayKit","activeBadge") and
                  int(item.get("teamid",0) or 0)>0),int(CLUB_ID))
    metadata=_cpu_match_metadata(wanted,PERSONA_ID+wanted,team_id=team_id)
    instance_base=860000000000+wanted*10
    timestamp_base=1_710_000_000+(wanted%1_000_000)*10
    actives=_complete_match_actives(
        selected,team_id,instance_base,timestamp_base)
    manager=list(squad.get("manager",[]) or [])
    if not manager:
        manager=list(metadata.get("manager",[]) or [])
    coach_id=int(squad.get("coachId",0) or 0)
    if not coach_id and manager:
        coach_id=int(manager[0].get("id",manager[0].get("itemId",0)) or 0)
    ids=[int(item.get("id",item.get("itemId",0)) or 0)
         for item in populated if int(item.get(
             "id",item.get("itemId",0)) or 0)>0]
    captain=int(squad.get("captain",0) or 0)
    if captain not in ids:
        captain=ids[0]
    kick_ids=ids[:5]
    while kick_ids and len(kick_ids)<5:
        kick_ids.append(kick_ids[-1])
    rating=max(1,min(99,int(squad.get("rating",0) or 0)))
    squad.update({
        "personaId":PERSONA_ID,"teamId":team_id,"badgeId":team_id,
        "active":True,"valid":True,"changed":False,"newSquad":False,
        "dreamSquad":False,"squadType":"REGULAR_SQUAD",
        "starRating":_native_star_rating(rating),
        "captain":captain,"coachId":coach_id,"manager":manager,
        "actives":actives,"club":actives,
        "kicktakers":[{"index":index,"id":item_id,"dream":False}
                       for index,item_id in enumerate(kick_ids)],
    })
    return squad


# ---------------------------------------------------------------------
# Source-backed offline competitive squads
# ---------------------------------------------------------------------
_SQUAD_POSITIONS=("GK","RB","CB","CB","LB","RM","CM","CM","LM",
                  "ST","ST","GK","CB","LB","RB","CDM","CM","CAM",
                  "RW","LW","CF","ST","ST")

# Source-backed FIFA 19 clubs, split by the four Offline Draft rounds.  The
# five choices in every tier were checked against the local Normal-card
# catalogue (at least 23 distinct players per club).  Difficulty still drives
# the match AI; the round now drives squad strength, as it did in retail Draft.
_DRAFT_OPPONENT_POOLS={
    1:((3,"Blackburn Rovers"),(4,"Bolton Wanderers"),
       (89,"Charlton Athletic"),(106,"Sunderland"),(1790,"Portsmouth")),
    2:((2,"Aston Villa"),(12,"Middlesbrough"),
       (15,"Queens Park Rangers"),(1792,"Norwich City"),(1960,"Swansea City")),
    3:((7,"Everton"),(22,"Borussia Dortmund"),(47,"AC Milan"),
       (461,"Valencia CF"),(481,"Sevilla FC")),
    4:((10,"Manchester City"),(21,"FC Bayern Munchen"),
       (73,"Paris Saint-Germain"),(241,"FC Barcelona"),(243,"Real Madrid")),
}


def _source_backed_roster(revisions=None, min_rating=0, max_rating=99,
                          seed=0, count=23, excluded_assets=None):
    """Build a balanced roster exclusively from exact catalogue definitions."""
    rows=card_version_rows(revisions,min_rating,max_rating)
    if not rows:
        return []
    rng=random.Random(int(seed)); selected=[]
    used_assets={int(value) for value in (excluded_assets or ())
                 if int(value or 0)>0}
    broad={
        "RB":{"RB","RWB"}, "LB":{"LB","LWB"},
        "CB":{"CB","RB","LB"}, "CDM":{"CDM","CM","CB"},
        "CM":{"CM","CDM","CAM"}, "CAM":{"CAM","CM","CF"},
        "RM":{"RM","RW","CM"}, "LM":{"LM","LW","CM"},
        "RW":{"RW","RM","RF"}, "LW":{"LW","LM","LF"},
        "CF":{"CF","CAM","ST"}, "ST":{"ST","CF"}, "GK":{"GK"},
    }
    for slot,position in enumerate(_SQUAD_POSITIONS[:int(count)]):
        accepted=broad.get(position,{position})
        candidates=[row for row in rows
                    if str(row.get("pos","")).upper() in accepted and
                       int(row.get("assetId",0) or 0) not in used_assets]
        if not candidates:
            candidates=[row for row in rows
                        if int(row.get("assetId",0) or 0) not in used_assets]
        if not candidates:
            break
        # Keep rating quality deterministic while varying squads within each
        # tier. A bounded offset never promotes a card outside the requested
        # rating interval.
        window=candidates[:max(1,min(12,len(candidates)))]
        chosen=window[rng.randrange(len(window))]
        selected.append(int(chosen["resourceId"]))
        used_assets.add(int(chosen.get("assetId",0) or 0))
    return selected


def _source_backed_team_roster(team_id, seed=0, count=23):
    """Return a balanced, real-card roster belonging only to ``team_id``."""
    team_id=int(team_id); rng=random.Random(int(seed)); selected=[]
    rows=[]; seen=set()
    for row in card_version_rows("Normal"):
        club_id=int(row.get("teamid",row.get("teamId",row.get("club",0))) or 0)
        asset_id=int(row.get("assetId",0) or 0)
        if club_id != team_id or asset_id <= 0 or asset_id in seen:
            continue
        seen.add(asset_id); rows.append(row)
    broad={
        "RB":{"RB","RWB"}, "LB":{"LB","LWB"},
        "CB":{"CB","RB","LB"}, "CDM":{"CDM","CM","CB"},
        "CM":{"CM","CDM","CAM"}, "CAM":{"CAM","CM","CF"},
        "RM":{"RM","RW","CM"}, "LM":{"LM","LW","CM"},
        "RW":{"RW","RM","RF"}, "LW":{"LW","LM","LF"},
        "CF":{"CF","CAM","ST"}, "ST":{"ST","CF"}, "GK":{"GK"},
    }
    used_assets=set()
    for position in _SQUAD_POSITIONS[:int(count)]:
        accepted=broad.get(position,{position})
        candidates=[row for row in rows
                    if str(row.get("pos","")).upper() in accepted and
                       int(row.get("assetId",0) or 0) not in used_assets]
        if not candidates:
            candidates=[row for row in rows
                        if int(row.get("assetId",0) or 0) not in used_assets]
        if not candidates:
            break
        # Keep the club's strongest spine while allowing genuine rotation
        # between separate Draft sessions.
        window=candidates[:max(1,min(3,len(candidates)))]
        chosen=window[rng.randrange(len(window))]
        selected.append(int(chosen["resourceId"]))
        used_assets.add(int(chosen.get("assetId",0) or 0))
    return selected


def _native_star_rating(rating):
    """Convert a 0..99 squad rating to the native 1..5 star count.

    ``starRating`` is member 954 and the CreateMatch squad parser reads it at
    `CardsDLL+0x27419b`, next to `squadType` at `+0x274192`. Publishing the
    squad rating there sent values such as 69 into a five-star field.
    """
    return max(1,min(5,round((max(0,int(rating or 0))-55)/7)))


def _native_cpu_squad(squad_id, name, resource_ids, formation="f442",
                      squad_type="DREAM_SQUAD", badge_id=0):
    players=[]; ratings=[]
    for index,resource_id in enumerate(list(resource_ids)[:23]):
        item_id=820000000000+int(squad_id)*100+index
        # CreateMatch may be requested repeatedly while the kit/warm-up
        # controllers initialise. A wall-clock fallback made the otherwise
        # identical opponent DTO change whenever those retries crossed a
        # second boundary. Keep its temporary item timestamps deterministic
        # for the same squad/round just like its item instance IDs.
        item_timestamp=1_700_000_000+(int(squad_id)%1_000_000)+index
        item=native_player_fields(int(resource_id),{
            "id":item_id,"itemId":item_id,"pile":7,"itemState":"free",
            "untradeable":True,"tradeable":False,"formation":formation,
            "contract":99,"contracts":99,"fitness":99,
            "timestamp":item_timestamp,
        })
        native=_native_player_item(item); ratings.append(int(native.get("rating",0)))
        players.append({"index":index,"kitNumber":index+1,
                        "loyaltyBonus":1,"itemData":native})
    while len(players)<23:
        players.append({"index":len(players),"kitNumber":0})
    first_id=(players[0].get("itemData") or {}).get("id",0)
    rating=round(sum(ratings[:11])/max(1,len(ratings[:11]))) if ratings else 0
    return {"id":int(squad_id),"squadId":int(squad_id),"personaId":0,
            "valid":bool(ratings),"squadName":str(name),"name":str(name),
            "formation":str(formation),"players":players,"captain":first_id,
            "coachId":0,"chemistry":100,"rating":rating,
            "starRating":_native_star_rating(rating),
            "changed":False,"newSquad":False,"squadType":str(squad_type),
            "custom":"[0,0,0,0,0,0,0,0,0,0,0]","dreamSquad":True,
            "manager":[],"actives":[],"club":[],"kicktakers":[],"tactics":[],
            "badgeId":int(badge_id or 0)}


MATCH_SAFE_STADIUM_ID=28       # Stamford Bridge, verified FIFA 19 catalog row.
MATCH_SAFE_BALL_ASSET_ID=1    # EA SPORTS White / Black, base-game catalog row.


def _match_catalog_object(kind, preferred_id):
    """Return a deterministic valid base-game object definition."""
    rows=sorted((dict(row) for row in object_catalog().values()
                 if row.get("_type")==kind),
                key=lambda row:int(row.get("_resourceId",0) or 0))
    if kind=="stadium":
        identifier=lambda row:int(row.get("StadiumId",0) or 0)
    elif kind=="ball":
        identifier=lambda row:int(row.get("AssetId",0) or 0)
    else:
        raise ValueError("unsupported match object kind %r" % kind)
    valid=[row for row in rows
           if int(row.get("_resourceId",0) or 0)>0 and identifier(row)>0]
    selected=next((row for row in valid
                   if identifier(row)==int(preferred_id)),None)
    if selected is None and valid:
        selected=valid[0]
    if selected is None:
        raise ValueError("missing source-backed %s match object" % kind)
    return selected


def _valid_match_active(item, state):
    if not isinstance(item,dict) or item.get("itemState")!=state:
        return False
    if (int(item.get("id",item.get("itemId",0)) or 0)<=0 or
            int(item.get("resourceId",0) or 0)<=0 or
            int(item.get("assetId",0) or 0)<=0):
        return False
    if state in ("activeHomeKit","activeAwayKit","activeBadge"):
        return int(item.get("teamid",0) or 0)>0
    if state=="activeStadium":
        return (int(item.get("stadiumid",item.get("stadiumId",0)) or 0)>0
                and int(item.get("category",0) or 0)==4
                and int(item.get("cardsubtypeid",0) or 0)==10)
    if state=="activeBall":
        return (str(item.get("itemType","")).lower()=="ball" and
                int(item.get("assetId",0) or 0)>0)
    return False


def _complete_match_actives(items, team_id, instance_base, timestamp_base):
    """Return all five rendering identities without mutating club state."""
    states=("activeHomeKit","activeAwayKit","activeBadge",
            "activeStadium","activeBall")
    active={}
    for source in items or ():
        item=dict(source) if isinstance(source,dict) else None
        state=str((item or {}).get("itemState", ""))
        if (state in states and state not in active and
                _valid_match_active(item,state)):
            active[state]=item

    fallbacks={
        "activeHomeKit":_native_kit_item(team_id,2,True),
        "activeAwayKit":_native_kit_item(team_id,3,True),
        "activeBadge":_native_badge_item(team_id,True),
        "activeStadium":stadium_item_dto(_match_catalog_object(
            "stadium",MATCH_SAFE_STADIUM_ID)),
        "activeBall":ball_item_dto(_match_catalog_object(
            "ball",MATCH_SAFE_BALL_ASSET_ID)),
    }
    for offset,state in enumerate(states):
        if state in active:
            continue
        item=fallbacks[state]
        item.update({"id":int(instance_base)+offset,
                     "itemId":int(instance_base)+offset,
                     "timestamp":int(timestamp_base)+offset,
                     "pile":7,"itemState":state,
                     "untradeable":True,"tradeable":False})
        if not _valid_match_active(item,state):
            raise ValueError("invalid fallback match active %s" % state)
        active[state]=item
    return [active[state] for state in states]


def _cpu_match_metadata(squad_id, seed, team_id=None,
                        manager_resource_id=None):
    """Return source-backed identity items required by the match loader.

    CreateMatch's embedded squad is the CPU opponent.  It must therefore not
    borrow the user's active account objects, but an empty ``manager`` and
    ``actives`` collection leaves FIFA in a standalone, untimed Skill Game.
    The definitions below come from the installed Frostbite/object catalog;
    only their temporary item-instance IDs belong to this offline match.
    """
    squad_id=int(squad_id); seed=int(seed)
    local_team_ids={int(item.get("teamid",0) or 0)
                    for item in _selected_onboarding_items()
                    if str(item.get("itemState","")) in {
                        "activeHomeKit","activeAwayKit"}}
    available_teams=[team_id for team_id in ONBOARDING_TEAM_IDS
                     if int(team_id) not in local_team_ids]
    if not available_teams:
        available_teams=list(ONBOARDING_TEAM_IDS)
    if team_id is None:
        team_id=int(available_teams[seed % len(available_teams)])
    else:
        team_id=int(team_id)
    instance_base=850000000000+squad_id*10
    timestamp_base=1_700_000_000+(squad_id%1_000_000)*10

    home=_native_kit_item(team_id,2,True)
    away=_native_kit_item(team_id,3,True)
    for offset,item in enumerate((home,away)):
        item["id"]=instance_base+offset
        item["itemId"]=instance_base+offset
        item["timestamp"]=timestamp_base+offset

    actives=_complete_match_actives(
        [home,away],team_id,instance_base,timestamp_base)

    manager_sources=sorted(
        (row for row in object_catalog().values()
         if row.get("_type")=="manager"),
        key=lambda row:int(row.get("_resourceId",0) or 0))
    manager=[]; coach_id=0
    if manager_sources:
        if manager_resource_id is None:
            manager_source=manager_sources[seed % len(manager_sources)]
        else:
            wanted_manager=int(manager_resource_id)
            manager_source=next((row for row in manager_sources
                if int(row.get("_resourceId",0) or 0)==wanted_manager),None)
            if manager_source is None:
                raise ValueError("missing source-backed manager %d" %
                                 wanted_manager)
        item=manager_item_dto(manager_source)
        coach_id=instance_base+5
        item.update({"id":coach_id,"itemId":coach_id,"pile":7,
                     "timestamp":timestamp_base+5,
                     "itemState":"free","untradeable":True,
                     "tradeable":False,"contract":30,"contracts":30})
        manager=[item]
    return {"teamId":team_id,"badgeId":team_id,"actives":actives,
            "manager":manager,"coachId":coach_id}


def _champion_cpu_squad(snapshot):
    source=dict(snapshot or {})
    resources=[int(value) for value in source.get("resourceIds",())]
    if len(resources)!=23 or any(value<=0 for value in resources):
        raise ValueError("FUT Champions CPU squad requires 23 resource IDs")
    opponent_id=int(source.get("opponentId",0) or 0)
    team_id=int(source.get("identityTeamId",0) or 0)
    manager_resource_id=int(source.get("managerResourceId",0) or 0)
    formation=str(source.get("formation","") or "")
    if opponent_id<=0 or team_id<=0 or manager_resource_id<=0 or not formation:
        raise ValueError("incomplete FUT Champions CPU identity")

    name=str(source.get("name","") or "FUT Champions XI")
    squad=_native_cpu_squad(opponent_id,name,resources,formation,
                            "DREAM_SQUAD",badge_id=team_id)
    metadata=_cpu_match_metadata(
        opponent_id,opponent_id*31+7,team_id=team_id,
        manager_resource_id=manager_resource_id)
    # CPU snapshots must not inherit the local user's presentation objects;
    # their persisted identity chooses all three kit/badge assets and manager.
    squad.update({"personaId":0,"dreamSquad":True,
                  "changed":True,"newSquad":True,"active":True,
                  "valid":True,"teamId":metadata["teamId"],
                  "clubId":metadata["teamId"],
                  "opponentTeamId":metadata["teamId"],
                  "badgeId":metadata["badgeId"],
                  "opponentBadgeId":metadata["badgeId"],
                  "badgeAssetId":metadata["badgeId"],
                  "badgeResourceId":6000000+metadata["badgeId"],
                  "coachId":metadata["coachId"],
                  "manager":metadata["manager"],
                  "actives":metadata["actives"],
                  "club":metadata["actives"],
                  "clubName":name})
    return squad


CHAMPION_EVENT_DURATION_SECONDS=3*24*60*60
CHAMPION_EVENT_EPOCH=1546300800  # 2019-01-01 00:00 UTC
CHAMPION_EVENT_ATTEMPT_STRIDE=1_000_000
CHAMPION_COMPETITION_COUNTRY_CODE="IT"
# CardsDLL's closed region table names Europe/Africa ``EUR``; returning the
# intuitive two-letter form leaves registration outside every client enum row.
CHAMPION_COMPETITION_REGION="EUR"


def _champion_event_context(reference_time=None):
    """Return one signed-32-bit identity and a three-day Unix-second window."""
    current=now_s() if reference_time is None else int(reference_time)
    cycle=max(0,(current-CHAMPION_EVENT_EPOCH)//
              CHAMPION_EVENT_DURATION_SECONDS)
    start=CHAMPION_EVENT_EPOCH+cycle*CHAMPION_EVENT_DURATION_SECONDS
    expires=start+CHAMPION_EVENT_DURATION_SECONDS
    start_date=datetime.datetime.fromtimestamp(
        start,datetime.timezone.utc).strftime("%Y-%m-%d")
    end_date=datetime.datetime.fromtimestamp(
        expires,datetime.timezone.utc).strftime("%Y-%m-%d")
    event_key="%s/%s" % (start_date,end_date)
    cursor=STATE.get("champion_event_cursor",{})
    attempt=(max(0,int(cursor.get("attempt",0) or 0))
             if isinstance(cursor,dict) and
             str(cursor.get("eventKey",''))==event_key else 0)
    while True:
        event_id=(3_000_000+int(cycle)+
                  attempt*CHAMPION_EVENT_ATTEMPT_STRIDE)
        if event_id>2_147_483_647:
            raise RuntimeError("FUT Champions event identity exhausted")
        previous=STATE.champion_session(event_id=event_id)
        if not (previous is not None and
                str(previous.get("state",''))=="COMPLETED" and
                int(previous.get("reward_claimed",0) or 0)):
            break
        # Profiles which claimed before the replay cursor existed must still
        # leave the populated-country branch without mutating state on a GET.
        attempt+=1
    return {"eventId":event_id,"start":int(start),
            "expires":int(expires),"eventKey":event_key,
            "eventAttempt":attempt}


def _champion_session_snapshot():
    context=_champion_event_context()
    event_id=int(context["eventId"])
    session=STATE.champion_session(event_id=event_id)
    return session,context


def _champion_tier_awards(wins,difficulty,rank=0):
    # The durable claim computes Top 100 from the session's frozen bot table.
    # Reusing only wins here advertised Elite rewards even after the larger
    # rank-based bundle had already been granted to the account.
    bundle=champion_reward_bundle(int(wins),int(difficulty),int(rank or 0))
    source=[]
    if int(bundle["coins"])>0:
        source.append({"type":"coin","value":int(bundle["coins"]),
                       "count":1})
    for pack in bundle["packs"]:
        pack_id=int(pack["packId"])
        source.append({"type":"pack","value":pack_id,"halId":pack_id,
                       "count":int(pack["count"]),"untradeable":False})
    if int(bundle["playerPickCount"])>0:
        source.append({
            "type":"playerPick","value":0,
            "count":int(bundle["playerPickCount"]),
            "optionCount":int(bundle["playerPickOptions"]),
            "label":"FUT Champions Player Pick",
            "description":"Choose one untradeable FUT Champions TOTW player.",
            "untradeable":True,
        })
    return [native_reward_dto(row) for row in source]


def _champion_runtime_payload():
    """Build the five observed read-only Champions responses from one snapshot.

    The event parser converts its nanosecond platform clock to seconds at
    CardsDLL+0x2790ed..+0x279107 before subtracting JSON ``currentTime``.
    Publishing milliseconds here would therefore shift every event window by
    roughly 55,000 years and prevent the native hub from classifying it.
    """
    session,context=_champion_session_snapshot()
    event_id=int(context["eventId"])
    registration=(dict((session.get("data",{}) or {}).get(
        "registration",{}) or {}) if session is not None else {})
    registered=bool(registration)
    wire_registration={key:registration[key] for key in (
        "competitionCountryCode","competitionRegion") if key in registration}
    matches=list(session.get("matches",[]) or []) if registered else []
    played=len(matches)
    wins=int(session.get("wins",0) or 0) if registered else 0
    draws=int(session.get("draws",0) or 0) if registered else 0
    losses=int(session.get("losses",0) or 0) if registered else 0
    goal_difference=sum(int(row.get("home_goals",0) or 0)-
                        int(row.get("away_goals",0) or 0)
                        for row in matches)
    rank=(champion_user_rank(event_id,wins,draws,losses,goal_difference)
          if played else 0)
    tier=champion_tier_for_wins(wins)
    difficulty=(int(session.get("difficulty",0) or 0)
                if registered else 0) or 4

    prize_tiers=[]
    for index,spec in enumerate(CHAMPION_RANK_SPECS):
        upper=(CHAMPION_MATCH_COUNT if index==0 else
               int(CHAMPION_RANK_SPECS[index-1]["minWins"])-1)
        prize_tiers.append(champion_prize_tier_dto(
            awards=_champion_tier_awards(int(spec["minWins"]),difficulty),
            tier_start=int(spec["minWins"]),tier_end=upper,
            tier_level=int(spec["tierLevel"]),
            tier_type=NUMBER_OF_WINS_TIER_TYPE))

    current=now_s()
    event=champion_event_dto(
        event_id=event_id,current_time=current,
        start_time=int(context["start"]),end_time=int(context["expires"]),
        localized_name="FUT Deba Champions",max_matches=CHAMPION_MATCH_COUNT,
        min_matches_to_rank=1,prize_tiers=prize_tiers)
    stat=champion_user_stat_dto(
        event_id=event_id,encrypted_nucleus_id=str(PERSONA_ID),
        encrypted_persona_id=str(PERSONA_ID),
        expected_tier_level=int(tier["tierLevel"]),games_played=played,
        games_remaining=max(0,CHAMPION_MATCH_COUNT-played),rank=rank,
        score=wins,tier_level=int(tier["tierLevel"]))
    unclaimed=([event] if registered and
               str(session.get("state",""))=="READY_FOR_REWARDS" else [])
    hub=champion_hub_dto(
        # Action 0x10 selects GotoRegistration only for a qualified event whose
        # country is still empty.  RC82-85 could not exercise that branch
        # because /champion/user/stats still used the array root later proved
        # wrong at RVA 0x2531e0; keep enrollment exclusive to the explicit POST.
        competition_country_code=(str(registration.get(
            "competitionCountryCode","") or
            CHAMPION_COMPETITION_COUNTRY_CODE) if registered and not
            bool(registration.get("offline",False)) else ""),
        active_events=[event],
        qualified_league_ids=[event_id],
        unclaimed_events=unclaimed,user_stats=([stat] if registered else []),
        # The offline registration marker is durable server state, not a
        # CardsDLL member.  Publishing only the two stock fields keeps the
        # country-less custom run invisible to the retired regional flow.
        user_registration=wire_registration if registered else {},
        game_mode_restriction=0,min_win_form_value=0)
    return {"session":session,"context":context,"event":event,
            "stat":stat,"hub":hub,"rank":rank,"registered":registered,
            "goalDifference":goal_difference}


def _champion_leaderboard_wire(snapshot):
    session=dict(snapshot["session"] or {})
    event_id=int(snapshot["event"]["id"])
    bots=list((session.get("data",{}) or {}).get("botLeaderboard",[]) or [])
    if not bots:
        bots=champion_leaderboard_bots(event_id)
    opponents=(list(session.get("opponents",[]) or []) or
               champion_opponents(event_id))
    team_ids=[int(row.get("identityTeamId",0) or 0) for row in opponents]
    rows=[dict(row) for row in bots]
    stat=dict(snapshot["stat"])
    if int(stat["gamesPlayed"])>0 and int(stat["rank"])>0:
        rows.append({
            "persona":STATE.persona_name(),"clubName":STATE.club_name(),
            "wins":int(stat["score"]),
            "goalDifference":int(snapshot["goalDifference"]),
            "matchesPlayed":int(stat["gamesPlayed"]),"isUser":True,
        })
    rows.sort(key=lambda row:(-int(row.get("wins",0) or 0),
                              -int(row.get("goalDifference",0) or 0),
                              0 if row.get("isUser") else 1,
                              str(row.get("persona",""))))
    entries=[]
    for position,row in enumerate(rows[:100],1):
        played=int(row.get("matchesPlayed",CHAMPION_MATCH_COUNT) or 0)
        entries.append(champion_leaderboard_entry_dto(
            badge=team_ids[(position-1)%len(team_ids)] if team_ids else 0,
            club_name=str(row.get("clubName",row.get("persona",""))),
            est=2019,inset_url="",persona=str(row.get("persona","")),
            rank=position,
            remaining_matches=max(0,CHAMPION_MATCH_COUNT-played),
            score=[{"icon":"","value":int(row.get("wins",0) or 0)}],
            tiebreak=[{"icon":"","value":int(
                row.get("goalDifference",0) or 0)}]))
    return champion_leaderboard_dto(entries)


_DRAFT_CHOICE_TYPES={
    "formation":"FORMATION_DRAFT", "captain":"CAPTAIN_DRAFT",
    "player":"PLAYER_DRAFT", "manager":"MANAGER_DRAFT",
    "difficulty":"PICK_DIFFICULTY",
}


def _draft_route_mode(value):
    """Translate FIFA's numeric Draft *mode* segment.

    Retail PC sends ``mode/1`` for Single Player and ``mode/0`` for Online.
    The number is not a persisted ``draft_sessions.id``. Treating it as one
    made every later choice request reopen historical session 1 after a new
    session had been purchased.
    """
    try:
        native_mode=int(value)
    except (TypeError,ValueError):
        return ""
    if native_mode==1:
        return "SINGLE_PLAYER"
    # The local project implements CPU Draft only.  Fail closed for the native
    # Online token so the menu cannot create a session or debit any currency.
    if native_mode==0:
        return ""
    return ""


def _draft_session_for_route(value):
    """Resolve a retail mode token, retaining legacy id aliases above 1."""
    mode=_draft_route_mode(value)
    if mode:
        current=STATE.current_draft(mode)
        if current:
            return current
        # A successful award claim removes the session from current_draft,
        # but CardsDLL may immediately retry GET stats or POST grant/award.
        # Native mode token 1 must remain attached to the newest durable
        # session; treating it as database id 1 can replay an older Draft.
        return STATE.latest_draft(mode)
    try:
        return STATE.draft_session(int(value))
    except (TypeError,ValueError):
        return None

# Starting-XI position order used by FIFA 19's native Draft controller.  These
# labels are the FUT-collapsed forms of the Frostbite formations table
# (RCB/LCB -> CB, RDM/LDM -> CDM, RAM/LAM -> CAM, RS/LS -> ST).
_DRAFT_FORMATION_POSITIONS={
    # The retail client numbers each horizontal line from right to left.
    "f442":   ("GK","RB","CB","CB","LB","RM","CM","CM","LM","ST","ST"),
    # The three forwards are one horizontal line, so right to left is
    # RW, ST, LW. Listing LW at index 9 crossed the two requests: the ST
    # tile returned wingers and the LW tile returned strikers, reported live
    # on 2026-09-03. Formations whose wide forwards sit on their own line
    # behind a lone striker - f3421 and f5221 - keep RW, LW, ST.
    "f433":   ("GK","RB","CB","CB","LB","CM","CM","CM","RW","ST","LW"),
    "f4231":  ("GK","RB","CB","CB","LB","CDM","CDM","CAM","CAM","CAM","ST"),
    "f41212": ("GK","RB","CB","CB","LB","CDM","RM","LM","CAM","ST","ST"),
    # Live FIFA 19 traces prove that the visual RM tile is native position 6,
    # while the first central-midfield tile is native position 4. Keeping RM at
    # index 4 crossed the two requests (RM returned CMs and CM returned RMs).
    "f352":   ("GK","CB","CB","CB","CM","CM","RM","LM","CAM","ST","ST"),
    "f3412":  ("GK","CB","CB","CB","RM","CM","CM","LM","CAM","ST","ST"),
    "f3421":  ("GK","CB","CB","CB","RM","CM","CM","LM","RW","LW","ST"),
    "f4141":  ("GK","RB","CB","CB","LB","CDM","RM","CM","CM","LM","ST"),
    "f4222":  ("GK","RB","CB","CB","LB","CDM","CDM","CAM","CAM","ST","ST"),
    "f4312":  ("GK","RB","CB","CB","LB","CM","CM","CM","CAM","ST","ST"),
    "f4321":  ("GK","RB","CB","CB","LB","CM","CM","CM","CF","CF","ST"),
    "f4411":  ("GK","RB","CB","CB","LB","RM","CM","CM","LM","CF","ST"),
    "f451":   ("GK","RB","CB","CB","LB","RM","CM","CM","CM","LM","ST"),
    "f5212":  ("GK","RWB","CB","CB","CB","LWB","CM","CM","CAM","ST","ST"),
    "f5221":  ("GK","RWB","CB","CB","CB","LWB","CM","CM","RW","LW","ST"),
    "f532":   ("GK","RWB","CB","CB","CB","LWB","CM","CM","CM","ST","ST"),
}

_DRAFT_DEFENDER_POSITIONS=("LB","LWB","CB","RB","RWB")
_DRAFT_MIDFIELDER_POSITIONS=("CDM","CM","CAM","LM","RM")
_DRAFT_ATTACKER_POSITIONS=("LW","RW","CF","ST")
_DRAFT_ICON_REVISIONS=frozenset(("Icon","Prime Icon Moments"))
_DRAFT_FEATURED_REVISIONS=frozenset(("Icon","Prime Icon Moments","TOTY"))
_DRAFT_ICON_REVISIONS=frozenset(("Icon","Prime Icon Moments"))


def _draft_card_class(row):
    """Classify one Draft choice as gold, special or Icon."""
    revision=_draft_revision(row)
    if revision in _DRAFT_ICON_REVISIONS:
        return "icon"
    return "normal" if revision=="Normal" else "special"
_DRAFT_CARD_ROWS_BY_RESOURCE=None


def _draft_card_row(resource_id):
    """Return the stable source row used when a persisted roll is re-read.

    Native player DTO names are presentation strings and can be shorter than
    the source catalogue name (for example Ronaldo's 96 Icon is rendered as
    just ``Ronaldo``).  Mixing those two names made the same exact Icon look
    like two identities after a roll had been persisted, so it could reappear
    in a later Draft row.  Key both paths from the same source record.
    """
    global _DRAFT_CARD_ROWS_BY_RESOURCE
    if _DRAFT_CARD_ROWS_BY_RESOURCE is None:
        _DRAFT_CARD_ROWS_BY_RESOURCE={
            int(row.get("resourceId",0) or 0):row
            for row in card_version_rows(None,75,99)
            if int(row.get("resourceId",0) or 0)>0
        }
    return _DRAFT_CARD_ROWS_BY_RESOURCE.get(int(resource_id or 0))


def _draft_player_identity(row):
    """Collapse every version of a footballer to one Draft identity.

    Ordinary promo cards share a sourced assetId. Baby/Mid/Prime Icons do not,
    and Prime Icon Moments uses a linked late definition, so Icon families are
    identified by their canonical player name instead.
    """
    row=row if isinstance(row,dict) else {}
    revision=str(row.get("revision",row.get("rev",
                 row.get("cardRevision","Normal"))) or "Normal")
    if revision in _DRAFT_ICON_REVISIONS:
        # The source catalogue spells the same footballer more than one way:
        # resource 28130 is "Ronaldo de Assis Moreira" while 246472, 238395
        # and 238706 are "Ronaldinho". Comparing the raw spellings let a
        # 94 Icon and a 95 Prime Icon Moments of the same man share one choice
        # row, seen live on 2026-09-03. The resolved display name collapses
        # all four, so resolve first and fall back to the row only when the
        # catalogue cannot answer.
        resolved=""
        try:
            resolved=str(native_player_fields(
                int(row.get("resourceId",0) or 0),{}).get("name") or "")
        except (TypeError,ValueError):
            resolved=""
        name=resolved or str(row.get("name") or " ".join(
            value for value in (
                str(row.get("firstName","") or "").strip(),
                str(row.get("lastName","") or "").strip()) if value))
        name=" ".join(name.casefold().split())
        if name:
            return ("icon",name)
    return ("asset",int(row.get("assetId",0) or 0))


def _draft_choice_identity(choice):
    choice=choice if isinstance(choice,dict) else {}
    item=choice.get("itemData") or {}
    definition=int(choice.get("definitionId",item.get(
        "definitionId",choice.get("resourceId",item.get("resourceId",0)))) or 0)
    source=_draft_card_row(definition)
    return _draft_player_identity(
        source if isinstance(source,dict) else
        native_player_fields(definition,item))


def _draft_revision(row):
    return str(row.get("revision",row.get("rev",
               row.get("cardRevision","Normal"))) or "Normal")


# The 5% Icon class share is a per-row probability, so it bounds the average
# and not the run: a complete Draft still offered six or more, and the owner
# asked for a hard ceiling. A session budget makes the promise exact.
DRAFT_ICON_LIMIT=5


def _draft_offered_icon_count(session_id, exclude_slot=None):
    """Count Icons already offered by this Draft's materialised rows.

    ``_ensure_draft_choices`` writes eager placeholder player rows which
    ``_draft_position_choices`` later replaces, so only a row the player can
    actually have seen - the captain roll, a materialised roll or one already
    selected - may consume the budget.
    """
    total=0
    for row in STATE.draft_picks(int(session_id)):
        kind=str(row.get("choice_type") or "")
        if kind not in ("PLAYER_DRAFT","CAPTAIN_DRAFT"):
            continue
        if (kind=="PLAYER_DRAFT" and exclude_slot is not None and
                int(row.get("slot",-1))==int(exclude_slot)):
            continue
        metadata=row.get("data") or {}
        if (kind=="PLAYER_DRAFT" and not row.get("selected_value") and
                int(metadata.get("generationVersion",0) or 0)<5):
            continue
        for choice in row.get("choices") or []:
            source=_draft_card_row(int(choice.get("resourceId",0) or 0))
            if source is not None and _draft_card_class(source)=="icon":
                total+=1
    return total


def _draft_icon_budget(session_id, exclude_slot=None):
    """Return how many more Icons this Draft may still offer."""
    return max(0,DRAFT_ICON_LIMIT-_draft_offered_icon_count(
        session_id,exclude_slot))


def _draft_select_player_rows(pool, used_identities, rng, settings, count=5,
                              icon_budget=None):
    """Select a deterministic quality-aware row without duplicate players."""
    count=max(1,int(count)); minimum=int(settings.get("minRating",75) or 75)
    eligible=[row for row in pool
              if int(row.get("rating",0) or 0)>=minimum]
    if len(eligible)<count:
        eligible=list(pool)
    selected=[]

    def is_special(row):
        return _draft_revision(row)!="Normal"

    def is_high(row):
        return (is_special(row) and int(row.get("rating",0) or 0)>=
                int(settings.get("highRating",88) or 88))

    def is_featured(row):
        return _draft_revision(row) in _DRAFT_FEATURED_REVISIONS

    def weight(row):
        rating=int(row.get("rating",0) or 0)
        value=1.0+max(0,rating-minimum)*0.18
        if is_special(row): value*=3.0
        if is_high(row): value*=2.5
        if is_featured(row): value*=3.0
        return value

    # Filtering inside `ordered` applies the budget to every path that can
    # reach an Icon - the featured/high/special quotas, the class-share loop
    # and the unrestricted final fill - rather than only to the share loop.
    remaining_icons=[None if icon_budget is None else max(0,int(icon_budget))]

    def ordered(predicate):
        candidates=[]
        selected_identities={_draft_player_identity(value)
                             for value in selected}
        for row in eligible:
            identity=_draft_player_identity(row)
            if (not identity[1] or identity in used_identities or
                    identity in selected_identities or not predicate(row)):
                continue
            if (remaining_icons[0]==0 and _draft_card_class(row)=="icon"):
                continue
            key=rng.random() ** (1.0/max(0.01,weight(row)))
            candidates.append((key,row))
        candidates.sort(key=lambda value:value[0],reverse=True)
        return [row for _key,row in candidates]

    def take(row):
        selected.append(row)
        used_identities.add(_draft_player_identity(row))
        if (remaining_icons[0] is not None and
                _draft_card_class(row)=="icon"):
            remaining_icons[0]=max(0,remaining_icons[0]-1)

    def add_until(predicate,target):
        target=max(0,min(count,int(target or 0)))
        while (sum(1 for row in selected if predicate(row))<target and
               len(selected)<count):
            candidates=ordered(predicate)
            if not candidates: break
            take(candidates[0])

    add_until(is_featured,settings.get("featuredChoices",0))
    add_until(is_high,settings.get("highChoices",0))
    add_until(is_special,settings.get("specialChoices",0))

    # Remaining slots are filled by class share when the preset defines one.
    # Weighting every card by rating alone inverted the catalogue: measured on
    # 2026-09-03 it produced 36.8% Icons against 10.4% gold, from a pool that
    # is 65% gold. Choosing the class first and the card second makes the mix
    # an explicit, testable setting instead of an emergent side effect.
    shares={name:max(0.0,float(value or 0.0)) for name,value in
            (settings.get("classShares") or {}).items()}
    if shares and sum(shares.values())>0:
        default_bias=max(0.0,float(settings.get("ratingBias",1.0) or 1.0))
        # A single rating bias made every class skew to the top of its own
        # range, so the specials on offer were almost all 92+ TOTS and the
        # ordinary Team of the Week cards that dominate a real Draft barely
        # appeared. A lower bias inside a class flattens it towards its own
        # spread without changing how often the class itself comes up.
        per_class={name:max(0.0,float(value or 0.0)) for name,value in
                   (settings.get("classRatingBias") or {}).items()}

        def rated(row,name):
            bias=per_class.get(name,default_bias)
            return (1.0+max(0,int(row.get("rating",0) or 0)-minimum))**bias

        while len(selected)<count:
            available={}
            for name in shares:
                available[name]=ordered(
                    lambda row,name=name:_draft_card_class(row)==name)
            weights=[(name,shares[name]) for name in shares
                     if shares[name]>0 and available[name]]
            if not weights:
                break
            threshold=rng.random()*sum(share for _name,share in weights)
            chosen=weights[-1][0]
            for name,share in weights:
                threshold-=share
                if threshold<=0:
                    chosen=name
                    break
            candidates=available[chosen]
            keyed=sorted(
                ((rng.random()**(1.0/max(0.01,rated(row,chosen))),row)
                 for row in candidates),
                key=lambda value:value[0],reverse=True)
            take(keyed[0][1])

    add_until(lambda _row:True,count)
    return selected[:count]


def _draft_position_pool(formation, position):
    """Return the native role pool for one starter/bench/reserve slot."""
    if position<11:
        roles=_DRAFT_FORMATION_POSITIONS.get(
            formation,_DRAFT_FORMATION_POSITIONS["f442"])
        return (roles[position],)
    # FIFA's seven substitute rolls: goalkeeper, three defenders, a
    # midfielder, a midfielder/attacker hybrid and an attacker.  The final
    # five reserve rolls are intentionally unrestricted.
    if position==11:
        return ("GK",)
    if 12<=position<=14:
        return _DRAFT_DEFENDER_POSITIONS
    if position==15:
        return _DRAFT_MIDFIELDER_POSITIONS
    if position==16:
        return _DRAFT_MIDFIELDER_POSITIONS+_DRAFT_ATTACKER_POSITIONS
    if position==17:
        return _DRAFT_ATTACKER_POSITIONS
    return ()


def _draft_player_row_slot(session, position_id):
    """Map FIFA's 0..22 squad position onto the 22 persisted player rows.

    The captain already occupies one native position.  Rows 1..22 therefore
    form a compact, restart-safe index with that position removed.
    """
    position=max(0,min(22,int(position_id)))
    captain=session.get("captainPositionId")
    if captain is None:
        return max(1,position)
    captain=max(0,min(22,int(captain)))
    if position == captain:
        return None
    return position+1 if position<captain else position


def _draft_position_choices(session, position_id):
    """Materialise one deterministic, role-correct, non-repeating Draft row."""
    _ensure_draft_choices(session)
    position=max(0,min(22,int(position_id)))
    row_slot=_draft_player_row_slot(session,position)
    if row_slot is None:
        return None,None
    rows=STATE.draft_picks(int(session["id"]))
    current=next((row for row in rows if row["choice_type"]=="PLAYER_DRAFT" and
                  int(row["slot"])==int(row_slot)),None)
    formation=str(session.get("formation") or "f442").lower()
    role_pool=_draft_position_pool(formation,position)
    pool_key="|".join(role_pool)
    target=role_pool[0] if len(role_pool)==1 else ""
    metadata=(current or {}).get("data") or {}
    settings=load_draft_settings()
    # A row which has already been selected belongs to the active Draft and
    # must never be regenerated after the user changes the global preset.
    if current and current.get("selected_value"):
        return current,row_slot
    if (current and int(metadata.get("generationVersion",0) or 0)>=5 and
            int(metadata.get("positionId",-1))==position and
            str(metadata.get("positionPool") or "")==pool_key and
            str(metadata.get("qualityPreset") or "")==settings["name"]):
        return current,row_slot

    # A footballer may appear only once across every five-card roll in this
    # Draft. Exclude both base and promo versions through the common assetId.
    used_identities=set()
    for row in rows:
        if (row["choice_type"]=="PLAYER_DRAFT" and
                int(row["slot"])==int(row_slot)):
            continue
        metadata=row.get("data") or {}
        if (row["choice_type"]=="PLAYER_DRAFT" and
                not row.get("selected_value") and
                int(metadata.get("generationVersion",0) or 0)<2):
            # _ensure_draft_choices creates legacy placeholder rows eagerly.
            # They have never been shown to the player and must not consume
            # the uniqueness budget of role-correct rows materialised later.
            continue
        for choice in row.get("choices",[]):
            if not isinstance(choice,dict): continue
            item=choice.get("itemData") or {}
            identity=_draft_choice_identity(choice)
            if identity[1]: used_identities.add(identity)
    pool=card_version_rows(None,75,99)
    if role_pool:
        pool=[row for row in pool if str(row.get("pos") or "") in role_pool]
    rng=random.Random(int(session.get("rng_seed",0) or 0)+position*7919+211)
    selected_rows=_draft_select_player_rows(
        pool,used_identities,rng,settings,count=5,
        icon_budget=_draft_icon_budget(int(session["id"]),row_slot))
    choices=[]
    for index,row in enumerate(selected_rows):
        choices.append(_draft_choice_item(
            row["resourceId"],830000000000+int(session["id"])*1000+
            int(row_slot)*10+index,index))
    if len(choices)<5:
        return current,row_slot
    replaced=STATE.replace_unselected_draft_pick(
        int(session["id"]),"PLAYER_DRAFT",int(row_slot),choices,{
            "generationVersion":5,"positionId":position,
            "preferredPosition":target,"positionPool":pool_key,
            "qualityPreset":settings["name"]})
    return replaced,row_slot


def _draft_choice_item(resource_id, item_id, choice_index):
    native=_native_draft_player_item(native_player_fields(int(resource_id),{
        "id":int(item_id),"itemId":int(item_id),"pile":7,
        "itemState":"free","untradeable":True,"tradeable":False,
        "contract":99,"contracts":99,"fitness":99,
    }))
    return {"index":int(choice_index),"choiceIndex":int(choice_index),
            "resourceId":int(resource_id),
            "definitionId":int(resource_id),"itemData":native}


def _native_draft_player_item(source):
    """Preserve the general native identity contract inside Draft.

    PIM cards need three distinct values: the private version-31 ``resourceId``
    keeps the Moments card recognizable after native rarity classification,
    ``definitionId`` keeps the exact late definition, and ``assetId`` supplies
    the verified launch Icon profile. Replacing both visible IDs with the base
    Icon made Draft lose the collision-free signal used by artwork and link
    compatibility, especially after rarity 84 was temporarily exposed as 12.
    """
    return _native_player_item(source)


def _draft_difficulty_choices():
    return [{"index":value-1,"choiceIndex":value-1,
             "difficulty":value,"value":value,"difficultyName":name,
             "rewardMultiplier":multiplier}
            for value,name,multiplier in (
        (1,"BEGINNER",0.5),(2,"AMATEUR",0.65),(3,"SEMIPRO",0.8),
        (4,"PROFESSIONAL",1.0),(5,"WORLDCLASS",1.25),
            (6,"LEGENDARY",1.5),(7,"ULTIMATE",1.75))]


_DRAFT_MANAGER_LEAGUE_DESCRIPTIONS=frozenset({
    "England Premier League (1)",
    "France Ligue 1 (1)",
    "Germany 1. Bundesliga (1)",
    "Italy Serie A (1)",
    "Spain Primera Division (1)",
})


def _draft_manager_league_ids():
    """Resolve the five major-league IDs from the local object catalogue."""
    league_ids=set()
    for row in object_catalog().values():
        if str(row.get("ItemType") or "")!="TrainingLeagueModifier":
            continue
        if str(row.get("Desc") or "") not in _DRAFT_MANAGER_LEAGUE_DESCRIPTIONS:
            continue
        try:
            league_id=int(row.get("Amount",0) or 0)
        except (TypeError,ValueError):
            continue
        if league_id>0:
            league_ids.add(league_id)
    return frozenset(league_ids)


def _draft_manager_choices(session_id, seed):
    """Five restart-stable managers restricted to the five major leagues."""
    allowed_leagues=_draft_manager_league_ids()
    sources=[manager_item_dto(row) for row in object_catalog().values()
             if row.get("_type")=="manager"]
    rng=random.Random(int(seed)+104729)
    by_league={}
    for source in sources:
        league=int(source.get("leagueId",0) or 0)
        if league in allowed_leagues:
            by_league.setdefault(league,[]).append(source)
    leagues=list(by_league); rng.shuffle(leagues)
    for rows in by_league.values(): rng.shuffle(rows)

    selected=[]
    for league in leagues:
        if len(selected)>=5: break
        selected.append(by_league[league].pop())
    remainder=[]
    for rows in by_league.values(): remainder.extend(rows)
    rng.shuffle(remainder)
    for source in remainder:
        if len(selected)>=5: break
        selected.append(source)

    choices=[]
    for index,source in enumerate(selected):
        item=dict(source); item_id=840000000000+int(session_id)*10+index
        item.update({"id":item_id,"itemId":item_id,"pile":7,
                     "untradeable":True,"tradeable":False})
        choices.append({"index":index,"choiceIndex":index,
            "resourceId":int(item.get("resourceId",0) or 0),
            "definitionId":int(item.get("resourceId",0) or 0),
            "itemData":item})
    return choices


def _ensure_draft_choices(session):
    """Generate all choices once; FutState makes this restart/reroll safe."""
    if not session:
        return []
    session_id=int(session["id"]); seed=int(session.get("rng_seed",0) or 0)
    existing=STATE.draft_picks(session_id)
    existing_keys={(str(row["choice_type"]),int(row["slot"])) for row in existing}
    required={("FORMATION_DRAFT",0),("CAPTAIN_DRAFT",0),
              ("MANAGER_DRAFT",0),("PICK_DIFFICULTY",0)} | {
              ("PLAYER_DRAFT",slot) for slot in range(1,23)}
    if required <= existing_keys:
        manager=next((row for row in existing
                      if row["choice_type"]=="MANAGER_DRAFT"),None)
        if (manager and not manager.get("selected_value") and
                int((manager.get("data") or {}).get(
                    "generationVersion",0) or 0)<2):
            STATE.replace_unselected_draft_pick(
                session_id,"MANAGER_DRAFT",0,
                _draft_manager_choices(session_id,seed),
                {"generationVersion":2})
            return STATE.draft_picks(session_id)
        difficulty=next((row for row in existing
                         if row["choice_type"]=="PICK_DIFFICULTY"),None)
        if (difficulty and not difficulty.get("selected_value") and
                int((difficulty.get("data") or {}).get(
                    "generationVersion",0) or 0)<2):
            STATE.replace_unselected_draft_pick(
                session_id,"PICK_DIFFICULTY",0,
                _draft_difficulty_choices(),{"generationVersion":2})
            return STATE.draft_picks(session_id)
        return existing
    settings=load_draft_settings()
    formation_rng=random.Random(seed+5)
    formation_pool=tuple(_DRAFT_FORMATION_POSITIONS)
    if not settings.get("allowWingbackFormations",True):
        formation_pool=tuple(
            name for name,positions in _DRAFT_FORMATION_POSITIONS.items()
            if "LWB" not in positions and "RWB" not in positions)
    formations=tuple(formation_rng.sample(
        formation_pool,5))
    STATE.ensure_draft_pick(session_id,"FORMATION_DRAFT",0,[
        {"index":index,"choiceIndex":index,"formation":formation,
         "value":formation}
        for index,formation in enumerate(formations)],{
            "generationVersion":2,"qualityPreset":settings["name"]})

    captain_pool=card_version_rows({"Icon","TOTY","TOTS","FUT Birthday",
                                    "FUT Future Stars","UCL LIVE"},
                                   int(settings.get("captainMinRating",86)),99)
    rng=random.Random(seed+17); captain_identities=set()
    # The captain pool is deliberately all-special, so without a share of the
    # session budget one roll could spend the whole Icon allowance.
    captain_rows=_draft_select_player_rows(
        captain_pool,captain_identities,rng,settings,count=5,
        icon_budget=max(1,DRAFT_ICON_LIMIT//3))
    STATE.ensure_draft_pick(session_id,"CAPTAIN_DRAFT",0,[
        _draft_choice_item(row["resourceId"],
            830000000000+session_id*1000+index,index)
        for index,row in enumerate(captain_rows[:5])],{
            "generationVersion":2,"qualityPreset":settings["name"]})

    player_rows=card_version_rows(None,75,99)
    captain_identities={_draft_player_identity(row)
                        for row in captain_rows[:5]}
    # The retail Draft never offers the same footballer twice in one run.
    # Choices are materialised up front for restart safety, therefore keep a
    # session-wide asset set rather than only de-duplicating each five-card row.
    used_player_identities=set(captain_identities)
    for slot in range(1,23):
        slot_rng=random.Random(seed+slot*7919)
        pool=player_rows[:]
        selected_rows=_draft_select_player_rows(
            pool,used_player_identities,slot_rng,settings,count=5,
            icon_budget=_draft_icon_budget(session_id,slot))
        choices=[_draft_choice_item(
                row["resourceId"],830000000000+session_id*1000+slot*10+index,
                index)
            for index,row in enumerate(selected_rows)]
        STATE.ensure_draft_pick(session_id,"PLAYER_DRAFT",slot,choices,{
            "generationVersion":1,"qualityPreset":settings["name"]})

    STATE.ensure_draft_pick(session_id,"MANAGER_DRAFT",0,
                            _draft_manager_choices(session_id,seed),
                            {"generationVersion":2})
    STATE.ensure_draft_pick(session_id,"PICK_DIFFICULTY",0,
                            _draft_difficulty_choices(),
                            {"generationVersion":2})
    return STATE.draft_picks(session_id)


def _draft_native_squad(session):
    session_id=int(session["id"])
    formation=str(session.get("formation") or "f442")
    captain_position=session.get("captainPositionId")
    try:
        captain_position=int(captain_position)
    except (TypeError,ValueError):
        captain_position=None
    if captain_position is not None and not 0<=captain_position<=22:
        captain_position=None

    # A Draft pick row is persisted in a compact database slot, but its
    # ``data.positionId`` is the native 0..22 squad slot chosen by FIFA.  Do
    # not compact selected cards into players[0], players[1], ...: that moved
    # strikers into goal/defence while the squad was still being built and did
    # the same to substitutes and reserves after a restart.
    positioned=[]
    manager=None
    for row in STATE.draft_picks(session_id):
        selected=str(row.get("selected_value") or "")
        if not selected:
            continue
        choice=next((value for value in row.get("choices",[])
                     if selected in STATE._draft_choice_values(value)),None)
        choice_type=str(row.get("choice_type") or "")
        if choice_type=="MANAGER_DRAFT" and isinstance(choice,dict):
            manager=choice.get("itemData")
            continue
        if choice_type not in ("CAPTAIN_DRAFT","PLAYER_DRAFT") or not isinstance(choice,dict):
            continue
        try:
            resource=int(choice.get("resourceId",0) or 0)
        except (TypeError,ValueError):
            resource=0
        if not resource:
            continue
        if choice_type=="CAPTAIN_DRAFT":
            position=captain_position
        else:
            metadata=row.get("data") or {}
            try:
                position=int(metadata.get("positionId"))
            except (TypeError,ValueError):
                # Safe migration for rows selected by older builds.  Player
                # rows are the native positions with the captain removed.
                compact=int(row.get("slot",0) or 0)
                if captain_position is None:
                    position=max(0,compact-1)
                elif compact<=captain_position:
                    position=compact-1
                else:
                    position=compact
        if position is None or not 0<=int(position)<=22:
            continue
        positioned.append({"position":int(position),"choice":choice,
                           "captain":choice_type=="CAPTAIN_DRAFT"})

    def entry_ids(entry):
        choice=entry.get("choice") or {}
        source=choice.get("itemData") or {}
        ids=set()
        identity_values=(choice.get("resourceId"),choice.get("definitionId"),
                         source.get("resourceId"),
                         source.get("definitionId"),source.get("assetId"))
        for value in identity_values+(source.get("id"),source.get("itemId")):
            try:
                item=int(value or 0)
            except (TypeError,ValueError):
                item=0
            if item:
                ids.add(item)
        # PIM cards keep their exact Moments definition in persistence. Accept
        # both the current version-encoded wire ID and the former Prime
        # presentation identity so layouts saved by either compatibility build
        # resume without touching SQLite.
        for value in identity_values:
            try:
                presentation=int(player_presentation_resource_id(
                    int(value or 0)) or 0)
                wire=int(player_wire_resource_id(int(value or 0)) or 0)
            except (TypeError,ValueError):
                presentation=0
                wire=0
            if presentation:
                ids.add(presentation)
            if wire:
                ids.add(wire)
        return ids

    # A full 23-card layout is written only after FIFA explicitly saves user
    # switches.  It is authoritative then; while construction is incomplete,
    # native pick positions above remain authoritative and empty slots stay
    # empty.  Stable choice item ids make switched cards match across restarts.
    layout=session.get("squadLayout") or []
    if (len(layout)==23 and
            len({int(value.get("index",-1)) for value in layout
                 if isinstance(value,dict)})==23):
        remaining=list(positioned); reordered=[]
        for slot in sorted(layout,key=lambda value:int(value.get("index",99))):
            try:
                wanted=int(slot.get("id",0) or 0)
                target=int(slot.get("index",-1))
            except (AttributeError,TypeError,ValueError):
                continue
            match=next((entry for entry in remaining
                        if wanted in entry_ids(entry)),None)
            if match is not None and 0<=target<=22:
                remaining.remove(match)
                match=dict(match); match["position"]=target
                reordered.append(match)
        positioned=reordered+remaining
    else:
        partial=session.get("swapPlayerDefIds") or []
        try:
            partial=[int(value or 0) for value in partial]
        except (TypeError,ValueError):
            partial=[]
        if len(partial)==23 and positioned:
            # Retail sends the cumulative slot identities while cards are
            # picked, but the just-selected/final card can still be zero.  The
            # old all-or-nothing check discarded every preceding user switch
            # in that case, so choosing the manager and reopening Draft reset
            # the entire squad.  Resolve every explicit identity first, then
            # place omitted selected cards into the still-free native slots.
            # A non-zero identity that cannot be resolved remains a hard
            # failure: silently accepting it could duplicate or lose a card.
            remaining=list(positioned); reordered={}; valid=True
            for target,wanted_id in enumerate(partial):
                if not wanted_id:
                    continue
                match=next((entry for entry in remaining
                            if wanted_id in entry_ids(entry)),None)
                if match is None:
                    valid=False
                    break
                remaining.remove(match)
                match=dict(match); match["position"]=target
                reordered[target]=match
            if valid:
                pending=[]
                for entry in remaining:
                    original=int(entry.get("position",-1))
                    if 0<=original<=22 and original not in reordered:
                        match=dict(entry); match["position"]=original
                        reordered[original]=match
                    else:
                        pending.append(entry)
                free=[target for target in range(23)
                      if target not in reordered]
                if len(pending)<=len(free):
                    for target,entry in zip(free,pending):
                        match=dict(entry); match["position"]=target
                        reordered[target]=match
                    if len(reordered)==len(positioned):
                        positioned=[reordered[target]
                                    for target in sorted(reordered)]

    squad_id=700000+session_id
    squad=_native_cpu_squad(squad_id,"My Offline Draft",[],formation,
                            "DRAFT_SQUAD")
    ratings=[]; captain_id=0; occupied=set()
    for entry in positioned:
        position=int(entry["position"])
        if position in occupied:
            continue
        occupied.add(position)
        choice=entry["choice"]; source=choice.get("itemData") or {}
        resource=int(choice.get("resourceId",source.get("resourceId",0)) or 0)
        definition=int(choice.get("definitionId",
                       source.get("definitionId",resource)) or resource)
        # Older saved PIM picks may carry the launch Prime Icon as their outer
        # resourceId while retaining the exact Moments definitionId. Rebuild
        # from the exact definition so resume does not silently downgrade the
        # card to rarity 12 before the Draft-only presentation bridge runs.
        if card_revision(definition)=="Prime Icon Moments":
            resource=definition
        stable_id=int(source.get("id",source.get("itemId",0)) or
                      (830000000000+session_id*1000+position))
        item=native_player_fields(resource,{
            "id":stable_id,"itemId":stable_id,"pile":7,
            "itemState":"free","untradeable":True,"tradeable":False,
            "formation":formation,"contract":99,"contracts":99,
            "fitness":99,
        })
        native=_native_draft_player_item(item)
        squad["players"][position]={"index":position,
            "kitNumber":int(next((value.get("kitNumber",position+1)
                for value in layout if isinstance(value,dict) and
                int(value.get("index",-1))==position),position+1) or position+1),
            "loyaltyBonus":1,"itemData":native}
        if position<11:
            ratings.append(int(native.get("rating",0) or 0))
        if entry["captain"]:
            captain_id=stable_id
    squad.update({"valid":bool(occupied),"captain":captain_id,
                  "chemistry":0,
                  "rating":round(sum(ratings)/len(ratings)) if ratings else 0,
                  "starRating":round(sum(ratings)/len(ratings)) if ratings else 0,
                  "newSquad":not bool(occupied)})
    # Before the first card is chosen FIFA has already cached the account's
    # active My Squads row from /squad/list.  The early Draft state must
    # therefore publish an explicit empty dream squad to replace that cache.
    # _native_cpu_squad deliberately uses chemistry 100 for populated CPU
    # teams; that default is invalid for an empty Draft and renders stale
    # green/yellow links in the formation preview.
    if not occupied:
        squad.update({"valid":False,"captain":0,"chemistry":0,"rating":0,
                      "starRating":0,"newSquad":True})
    # Publish all 23 native slots, including the empty ones. RC107 filtered
    # them to occupied rows only, on the theory that a row without itemData
    # NULLs the client's sentinel and makes the DRAFT_SQUAD finaliser at
    # CardsDLL+0x2748F0 write IS_DRAFT_PLAYER to NULL+0xB6. That filter is the
    # first build in which paying a Draft token and entering raises the generic
    # FUT communication popup; RC42 through RC106 published the full 23 rows
    # and the flow worked, as the 2026-09-03 capture shows
    # (`"players":[{"index":0,"kitNumber":0},...]` at PICK_DIFFICULTY). The
    # Polish crash the filter targeted is independently corrected by the `PRO`
    # difficulty alias, which left that client in a broken state first. If a
    # locale-specific crash ever returns here, capture a native trace rather
    # than emptying this array again.
    # Draft selections are temporary, server-owned cards, but the squad itself
    # is still the local user's match side.  POST /match returns only the CPU
    # opponent, so there is no later "match context" that can supply the local
    # persona, kits, ball or stadium.  Omitting them produced a fully running
    # match (HUD/minimap/input) with a black 3D scene.  Mirror the proven regular
    # squad DTO and keep every object source-backed by the account catalogue.
    squad["personaId"]=PERSONA_ID
    selected_actives=_selected_onboarding_items()
    local_team_id=next((int(item.get("teamid",0) or 0)
                        for item in selected_actives
                        if item.get("itemState")=="activeHomeKit" and
                        int(item.get("teamid",0) or 0)>0),
                       ONBOARDING_TEAM_IDS[0])
    squad["actives"]=_complete_match_actives(
        selected_actives,local_team_id,
        860000000000+session_id*10,
        1_710_000_000+session_id*10)
    if isinstance(manager,dict):
        manager=dict(manager)
        squad["manager"]=[manager]
        squad["coachId"]=int(manager.get("id",manager.get("itemId",0)) or 0)
    return squad


def _draft_opponent_identity(session, round_no=None):
    """Return the one deterministic CPU identity shared by UI and match flow.

    The current-state response and ``POST /match`` must never select the round
    independently.  When the state omitted this identity, FIFA retained its
    built-in Manchester City fallback even though CreateMatch returned a
    different source-backed club.  Keeping the selection in this small helper
    also lets ``/squad/<opponentId>`` resolve the exact same CPU squad if the
    retail client asks for it while building Team Comparison.
    """
    session_id=int(session["id"])
    if round_no is None:
        round_no=len(STATE.draft_matches(session_id))+1
    round_no=max(1,min(4,int(round_no)))
    difficulty=max(1,min(7,int(session.get("difficulty",1) or 1)))
    seed=(int(session.get("rng_seed",0) or 0)+session_id*1009+
          difficulty*101+round_no*17)
    # Keep an explicit no-repeat guard for sessions created by earlier builds,
    # whose round pools could overlap. New pools are disjoint already, but a
    # persisted run must not inherit the same club in two consecutive rounds.
    played_team_ids=set()
    for match in STATE.draft_matches(session_id):
        data=match.get("data",{}) if isinstance(match.get("data",{}),dict) else {}
        try:
            played=int(data.get("opponentTeamId",data.get("teamId",0)) or 0)
        except (TypeError,ValueError):
            played=0
        if played>0:
            played_team_ids.add(played)
    pool=list(_DRAFT_OPPONENT_POOLS[round_no])
    available=[row for row in pool if int(row[0]) not in played_team_ids]
    team_id,team_name=random.Random(seed).choice(available or pool)
    return {"round":round_no,"difficulty":difficulty,"seed":seed,
            "teamId":int(team_id),"teamName":str(team_name),
            "opponentId":760000+session_id*10+round_no}


def _draft_opponent_from_squad_id(squad_id):
    """Resolve a public Draft CPU squad id back to its session and round."""
    try: encoded=int(squad_id)-760000
    except (TypeError,ValueError): return None
    round_no=encoded%10
    session_id=encoded//10
    if session_id<=0 or round_no not in range(1,5):
        return None
    session=STATE.draft_session(session_id)
    if session is None:
        return None
    return _draft_match_squad(session,round_no=round_no)


def _draft_match_squad(session, round_no=None):
    """Build the CPU opponent consumed by FutCreateMatchServerResponse.

    ``POST /match`` already identifies the local dream squad through
    ``squadId``.  The response member named ``squad`` is the *opponent*, not a
    round-trip of that local squad.  Returning the player's Draft here made
    FIFA display those same cards under the CPU club in Match Preview and left
    the match loader waiting forever in the warm-up drill.

    The opponent is stable for one session/round, distinct from the local
    persona and assembled only from one source-backed club roster.  Each round
    selects one of five progressively stronger clubs; match difficulty remains
    an independent AI setting selected by the user.
    """
    identity=_draft_opponent_identity(session,round_no)
    round_no=int(identity["round"])
    seed=int(identity["seed"])
    team_id=int(identity["teamId"])
    team_name=str(identity["teamName"])
    formation="f442"
    resources=_source_backed_team_roster(team_id,seed=seed,count=23)
    if len(resources) != 23:
        raise ValueError("incomplete Draft opponent club %d: %d players" %
                         (team_id,len(resources)))
    opponent_id=int(identity["opponentId"])
    # CreateMatch's opponent is owned by the local CPU persona, not by a user
    # club.  Live testing proved that REGULAR_SQUAD is accepted by the wire
    # parser but ignored by the offline participant provider: the response
    # contained Dortmund while Match Preview kept the cached Arsenal.  The
    # native offline modes that do refresh this provider (TOTW and Squad
    # Battles) publish a DREAM_SQUAD.  Keep the per-round squad id/newSquad
    # correlation so each round replaces the previous CPU cache entry.
    opponent=_native_cpu_squad(opponent_id,team_name,resources,
                               formation,"DREAM_SQUAD")
    opponent.update({"personaId":0,"dreamSquad":True,
                     "changed":True,"newSquad":True})
    metadata=_cpu_match_metadata(opponent_id,seed,team_id=team_id)
    opponent.update({"teamId":metadata["teamId"],
                     "badgeId":metadata["badgeId"],
                     "coachId":metadata["coachId"],
                     "manager":metadata["manager"],
                     "actives":metadata["actives"]})
    return opponent


def _draft_state_payload(mode="SINGLE_PLAYER", session=None):
    normalized=str(mode or "SINGLE_PLAYER").upper()
    online_disabled=normalized!="SINGLE_PLAYER"
    if online_disabled:
        session=None
    elif session is None:
        session=STATE.current_draft(normalized)
    if not session:
        native_state="INVALID"
        return {"draftState":native_state,"state":native_state,
                "squadState":native_state,"stateParam1":"INVALID",
                # CardsDLL initialises the numeric state parameter to -1.
                # Zero is a real Draft slot, so preserve the native sentinel
                # for the empty state inside the required one-element array.
                "stateParam2":"-1","gamesWonCurrentMatch":0,
                "roundsInfo":[],"gameModeRestriction":1 if online_disabled else 0,
                # CardsDLL's retail response parser has three mandatory
                # entrance slots (COINS, POINTS and DRAFT_TOKEN).  Leaving
                # POINTS absent initializes it to zero, which is not a valid
                # retail Draft entry price for the tollbooth.
                "entranceCriteria":{"COINS":15000,"POINTS":300,
                                    "DRAFT_TOKEN":1},
                "draftId":0,
                "mode":normalized,"draftChampion":False,
                "draftToken":STATE.draft_tokens(),
                "draftTokens":STATE.draft_tokens(),
                "draftSummary":{"gamesWon":0,"winsRemaining":4,
                                "maxWins":4,"losses":0},
                "entryFee":{"coins":15000,"draftToken":1}}
    _ensure_draft_choices(session)
    session=STATE.reconcile_draft_state(int(session["id"]))
    wins=int(session.get("wins",0) or 0); losses=int(session.get("losses",0) or 0)
    native_state=str(session["state"])
    picks=STATE.draft_picks(int(session["id"]))
    selected={(str(row["choice_type"]),int(row["slot"]))
              for row in picks if str(row.get("selected_value") or "")}
    if native_state == "PICK_DIFFICULTY":
        # `DIFFICULTY` is not one of the client's 1,164 interned names, and
        # the Draft state parser at `CardsDLL+0x255350..+0x255c8f` resolves
        # only MANAGER, PLAYER, INVALID and the four `*_DRAFT` literals.
        # Sending it raised the generic FUT server error on the first entry
        # after paying, while every request answered HTTP 200. No slot exists
        # during the difficulty choice, so the numeric parameter keeps the
        # same -1 sentinel the empty state already documents.
        state_param_1="INVALID"; state_param_2="-1"
    elif native_state == "PLAYER_DRAFT":
        state_param_1="PLAYER"
        state_param_2=str(next((slot for slot in range(1,23)
                                if ("PLAYER_DRAFT",slot) not in selected),1))
    elif native_state == "MANAGER_DRAFT":
        state_param_1="MANAGER"; state_param_2="0"
    else:
        state_param_1="INVALID"; state_param_2="0"
    matches=STATE.draft_matches(int(session["id"]))
    # These seven tokens are not interchangeable spellings: the `roundsInfo`
    # element decoder `CardsDLL+0x255104..+0x255340` reads `difficulty` as a
    # string, interns it through `+0x2b6440`, and switches on the resulting
    # token id at `+0x255213..+0x25527b`.  It accepts exactly 98 `BEGINNER`,
    # 28 `AMATEUR`, 884 `SEMIPRO`, 757 `PRO`, 1157 `WORLDCLASS` and
    # 504 `LEGENDARY`; **every** other token, `PROFESSIONAL` (760) included,
    # falls through to `mov dword ptr [rbx+8], 6` at `+0x255268`, which is
    # Ultimate.  `ULTIMATE` is deliberately absent because 6 is already the
    # default the entry is memcpy-initialised with at `+0x2555d9`.
    difficulty_names={1:"BEGINNER",2:"AMATEUR",3:"SEMIPRO",
                      4:"PRO",5:"WORLDCLASS",6:"LEGENDARY",
                      7:"ULTIMATE"}
    difficulty_name=difficulty_names.get(
        int(session.get("difficulty",0) or 0),"PRO")
    # gamesWonCurrentMatch drives the five progress nodes.  The CardsDLL
    # Draft-state decoder treats roundsInfo as the history of rounds already
    # played; its element parser accepts only the six members below.  Publishing
    # the upcoming opponent here produced a fake empty round and the extra
    # opponentId member was silently ignored, leaving Team Comparison with an
    # all-undefined AWAY card.  The current CPU belongs exclusively to the
    # FutCreateMatchServerResponse returned by POST /match.
    rounds_info=[]
    for match in matches:
        match_data=(match.get("data",{})
                    if isinstance(match.get("data",{}),dict) else {})
        rounds_info.append({
            "round":int(match.get("round",len(rounds_info)+1) or
                        len(rounds_info)+1),
            "difficulty":difficulty_name,
            "score":int(match.get("home_goals",0) or 0),
            "opponentScore":int(match.get("away_goals",0) or 0),
            "penaltyScore":int(match_data.get(
                "penaltyScore",match_data.get("homePenaltyScore",0)) or 0),
            "opponentPenaltyScore":int(match_data.get(
                "opponentPenaltyScore",match_data.get(
                    "awayPenaltyScore",0)) or 0),
        })
    summary={"draftId":int(session["id"]),"gamesWon":wins,
             "wins":wins,"losses":losses,"maxWins":4,
             "winsRemaining":max(0,4-wins)}
    return {"draftState":native_state,"state":native_state,
            "squadState":native_state,"stateParam1":state_param_1,
            "stateParam2":state_param_2,"gamesWonCurrentMatch":wins,
            "roundsInfo":rounds_info,"gameModeRestriction":0,
            "entranceCriteria":{"COINS":15000,"POINTS":300,
                                "DRAFT_TOKEN":1},
            "draftId":int(session["id"]),"id":int(session["id"]),
            "mode":str(session["mode"]),"draftChampion":wins>=4,
            "draftToken":STATE.draft_tokens(),"draftTokens":STATE.draft_tokens(),
            "formation":str(session.get("formation") or ""),
            "formationid":str(session.get("formation") or ""),
            "captain":int(session.get("captain_resource_id",0) or 0),
            "captainid":int(session.get("captain_resource_id",0) or 0),
            "difficulty":int(session.get("difficulty",0) or 0),
            "draftSummary":summary,"summary":summary,
            "squad":_draft_native_squad(session),
            "entryFee":{"coins":15000,"draftToken":1}}


def _draft_history_payload(mode="SINGLE_PLAYER"):
    """Expose the durable aggregate using the native Draft field names."""
    history=STATE.draft_history(mode)
    wins=int(history["wins"]); losses=int(history["losses"])
    # FutDraftStatsViewModel does not consume the project aliases wins/losses.
    # Its native DraftSummary DTO reads gamesWon/gamesLost plus the aggregate
    # match-stat members below.  Keeping the aliases alongside them is useful
    # for local tools, but the retail names are what populate "My Draft Record".
    summary={"gamesWon":wins,"gamesLost":losses,
             "gamesPlayed":wins+losses,
             "goalsScored":int(history.get("goalsScored",0) or 0),
             "concededGoals":int(history.get("concededGoals",0) or 0),
             "passAccuracyTotal":int(history.get(
                 "passAccuracyTotal",0) or 0),
             "possessionTotal":int(history.get(
                 "possessionTotal",0) or 0),
             "bestBuilderScore":int(history.get(
                 "bestBuilderScore",0) or 0),
             "wins":wins,"losses":losses}
    return {"draftsCompleted":int(history["draftsCompleted"]),
            "draftChampion":int(history["draftChampion"]),
            "draftSummary":summary,
            # These aliases are retained for project tools; the retail
            # viewmodel uses the three members above.
            "wins":wins,"losses":losses,
            "fourInARow":int(history["draftChampion"]),
            "draftEntries":int(history["draftsCompleted"])}


def _draft_match_history_stats(payload,session=None,active_match=None):
    """Extract only observed Draft-history values from one match report."""
    payload=payload if isinstance(payload,dict) else {}
    own=payload.get("myMatchStats",{})
    own=own if isinstance(own,dict) else {}

    def optional(names):
        for source in (own,payload):
            for name in names:
                if name not in source:
                    continue
                try: return int(float(source[name]))
                except (TypeError,ValueError): continue
        return None

    result={}
    passing=optional(("passingPercentage","passAccuracy","passing"))
    possession=optional(("possessionPercentage","possession"))
    if passing is not None: result["passAccuracy"]=max(0,min(100,passing))
    if possession is not None: result["possession"]=max(0,min(100,possession))
    builder=optional(("bestBuilderScore","builderScore","teamScore"))
    if builder is None and isinstance(session,dict):
        squad=_draft_native_squad(session)
        rating=max(0,int(squad.get("rating",0) or 0))
        chemistry=max(0,int(squad.get("chemistry",0) or 0))
        if rating or chemistry:
            # FUT Draft's Best Team Score is the built squad rating plus
            # chemistry, not a fabricated match statistic.
            builder=min(200,rating+chemistry)
    if builder is not None: result["builderScore"]=max(0,int(builder))
    identity=(active_match if isinstance(active_match,dict) else {})
    if isinstance(session,dict) and not int(identity.get("teamId",0) or 0):
        identity=_draft_opponent_identity(session)
    try: opponent_team_id=int(identity.get("teamId",0) or 0)
    except (TypeError,ValueError): opponent_team_id=0
    if opponent_team_id>0:
        result.update({
            "opponentTeamId":opponent_team_id,
            "opponentId":int(identity.get("opponentId",0) or 0),
            "opponentName":str(identity.get("opponentName",
                                identity.get("teamName","")) or ""),
        })
    return result


def _draft_purchase_result(mode="SINGLE_PLAYER"):
    """Return the Draft state the client's own decoder expects after paying.

    ``COINS``, ``DRAFT_TOKEN`` and ``POINTS`` are member ids 0xad, 0x111 and
    0x2d8 in this build's name table, and exactly one function in the whole
    4,507,968-byte EA App CardsDLL dispatches them: ``+0x255350..+0x255c8f``.
    That same function dispatches ``entranceCriteria`` (0x14f),
    ``gameModeRestriction`` (0x18f), ``gamesWonCurrentMatch`` (0x194),
    ``roundsInfo`` (0x349), ``squad`` (0x394), ``squadState`` (0x3ab),
    ``stateParam1`` (0x3c8) and ``stateParam2`` (0x3c9), and it reaches the
    three currency ids only inside ``entranceCriteria``. There is no separate
    purchase decoder: the purchase reply and the current-state reply are read
    by the same closed DTO, so a bare balance map arrives at a decoder looking
    for a Draft state and carrying nothing it can place.

    That matches the live shape of the defect exactly. On 2026-09-07 the same
    3,765-byte ``PICK_DIFFICULTY`` state was served twice, byte for byte
    identical (19:09:44 and 19:12:09,
    ``eaapp-full-server-guarded-20260907-190819.network.log``): the pass that
    followed the purchase raised the communication popup and returned to the
    hub one second later, and the pass that did not follow a purchase opened
    the difficulty screen and played on. The state payload therefore cannot be
    the cause, and the purchase reply is the only other thing on that path.
    18:44:18-18:44:29 in the 18:38 session is the same sequence.

    Already eliminated live, do not retry any of them: the ``stateParam1``
    literal (RC111), the object root here, which **froze** FIFA outright with
    42 bytes answered and no further request for 70 seconds (RC112,
    ``...-20260907-171319.network.log``), the bare balance map under the array
    root (RC113), and restoring the 23 native player rows (RC114). The array
    root is kept because it is the wrapping the object root disproved.

    Do not re-derive this from the vtable at RVA ``0x3814e8``. Its slot 1 is
    ``CardsDLL+0x2531e0``, which is recorded elsewhere as a single-object
    entry, and reasoning from that alone produced exactly the freeze above:
    the object at ``+0x252f80`` carries a 30000 ms timeout, so it is the
    request, and its response decoding is not the same path FUT Champions
    Stats takes.

    The three balance members are kept alongside the state, at the root. They
    are what refreshes the coin and token counters the moment the entry is
    paid: RC116 answered with the state alone and the owner watched 15,000
    coins stay on screen although the debit had already happened (the database
    shows `entry_currency=COINS, entry_cost=15000` and credits 87,508 ->
    73,297). An unknown scalar member at this level is safe - the squad object
    this same decoder reads already carries `squadId`, `valid`, `name`,
    `coachId`, `rating`, `newSquad` and `dreamSquad`, none of which it
    dispatches - and `entranceCriteria` stays the price, not the balance.
    """
    # The header on the Draft screens is not refreshed by this reply at all.
    # RC119 added `coins`/`credits`/`totalCredits`/`funds`/`finalFunds` here
    # and the 2026-09-08 00:08 run still showed the pre-purchase balance, and
    # the log explains why: between the purchase and the difficulty screen the
    # client issues exactly one request, the Draft state, and nothing that
    # reads a balance. FIFA re-renders that header on the way back to the hub.
    # Do not add a sixth member here on a guess.
    response = _draft_wire_state(_draft_state_payload(mode))
    response.update({"COINS": int(STATE.credits()),
                     "POINTS": max(0, int(STATE.get("fifa_points", 0) or 0)),
                     "DRAFT_TOKEN": int(STATE.draft_tokens())})
    return response


def _draft_wire_state(payload):
    """Return only members decoded by the retail current-state response.

    The richer payload is useful to the local persistence/controller code, but
    the native RS4 parser is a closed DTO rather than an open JSON document.
    Nested project-only objects must not cross this wire boundary.
    """
    keys=("entranceCriteria","gameModeRestriction","gamesWonCurrentMatch",
          "roundsInfo","squadState","stateParam1","stateParam2")
    response={key:payload[key] for key in keys}
    if (str(payload.get("squadState") or "") in
            ("PICK_DIFFICULTY","FORMATION_DRAFT","CAPTAIN_DRAFT",
             "PLAYER_DRAFT","MANAGER_DRAFT","READY_FOR_MATCH",
             "READY_FOR_REWARDS") and isinstance(payload.get("squad"),dict)):
        response["squad"]=payload["squad"]
    return response


# `FutGetDraftAwardServerResponse` is allocated at `CardsDLL+0x24f650`, keeps
# its vtable at `0x37c0f0` and parses at `+0x24f8d0`.  Resolving that parser's
# member switch against the RS4 name table at `+0x34c0c0` gives a closed DTO of
# four members, each with the reader its handler calls:
#
#   `type`   0x42f  int, handler `+0x24fb37` -> [rsp+0x40]
#   `value`  0x458  int, handler `+0x24fb16` -> [rsp+0x44]
#   `halId`  0x1ab  int, handler `+0x24fc52` -> [rsp+0x48]
#   `item`   0x1d6  item objects, handler `+0x24fb58`
#
# The response is an **array**: when an object ends, `+0x24fc80` pushes a
# 0x50-byte element carrying exactly those fields into a vector and loops back
# for the next one.  The parser's own defaults are `type` 3 and `value` 0
# (`+0x24f9b1`), which is why the two-byte `{}` reply of 2026-08-28 committed
# the reward but left the Rewards screen with nothing to show, and why the
# later empty body failed outright: a zero-length response never reaches the
# parser and the client reports a communication error instead.
#
# `halId` is left out: its meaning is not decoded and the parser defaults it to
# zero.  `item` is left out as well, because the Draft ladder awards coins and
# a pack, and neither carries an item DTO.
_DRAFT_AWARD_TYPE_IDS = {"coin":0,"coins":0,"pack":1,"player":2,"item":2}
_DRAFT_AWARD_TYPE_UNKNOWN = 3


def _draft_award_wire(receipt):
    """Return the award array `FutGetDraftAwardServerResponse` decodes."""
    source=receipt if isinstance(receipt,dict) else {}
    awards=[]
    for raw in source.get("awards",[]):
        if not isinstance(raw,dict):
            continue
        kind=str(raw.get("type","") or "").lower()
        awards.append({
            "type":int(_DRAFT_AWARD_TYPE_IDS.get(kind,_DRAFT_AWARD_TYPE_UNKNOWN)),
            "value":int(raw.get("value",0) or 0)})
    return awards


def _draft_reward_wire(receipt,session_id=0):
    """Translate the durable Draft receipt into the retail award DTO.

    The native award/stats screens dereference the award container even when
    there is no reward.  Always expose the complete (possibly empty) shape;
    project-only receipt details stay behind this wire boundary.
    """
    source=receipt if isinstance(receipt,dict) else {}
    awards=[]
    for raw in source.get("awards",[]):
        if not isinstance(raw,dict):
            continue
        award={"awardType":str(raw.get("type","") or ""),
               "awardValue":int(raw.get("value",0) or 0),
               "awardCount":int(raw.get("count",0) or 0)}
        for key in ("minRating","optionCount"):
            if key in raw:
                award[key]=int(raw.get(key,0) or 0)
        awards.append(award)
    picks=[dict(value) for value in source.get("playerPicks",[])
           if isinstance(value,dict)]
    award_set_id=int(session_id or 0)
    mappings=[dict(value) for value in awards]
    award_set={"awardSetId":award_set_id,"awards":awards,
               "awardMappings":mappings,"awardItemData":picks}
    return {"awards":awards,"awardType":"DRAFT",
            "awardValue":int(source.get("coins",0) or 0),
            "awardCount":sum(max(0,value["awardCount"]) for value in awards),
            "awardSet":award_set,"awardSetId":award_set_id,
            "awardMappings":mappings,"awardItemData":picks,
            "playerPicks":picks,
            "receiptId":str(source.get("receiptId") or
                            ("draft:%d" % award_set_id))}


def _draft_choice_payload(session, choice_type, slot, position_id=None):
    normalized=str(choice_type).upper(); _ensure_draft_choices(session)
    row=next((value for value in STATE.draft_picks(int(session["id"]))
              if value["choice_type"]==normalized and
                 int(value["slot"])==int(slot)),None)
    choices=(row or {}).get("choices",[])
    wire_position=int(slot if position_id is None else position_id)
    return {"draftId":int(session["id"]),"choiceType":normalized,
            "slot":int(slot),"choiceIndex":int(slot),
            # CardsDLL sends positionId (capital I) in the native PUT body.
            # Retain the historic lower-case spelling for compatibility with
            # older local callers while exposing the retail member verbatim.
            "positionId":wire_position,"positionid":wire_position,
            "tier":0,"choices":choices,
            "itemData":[value.get("itemData") for value in choices
                        if isinstance(value,dict) and value.get("itemData")],
            "selectedValue":(row or {}).get("selected_value","")}


def _current_mode_week():
    iso=datetime.datetime.now(datetime.timezone.utc).isocalendar()
    return int(iso.year),int(iso.week)


# The Team of the Week screen is a challenge list, not a single opponent.  The
# member names below are the ones CardsDLL actually knows: they come from the
# client's own sorted table of JSON field names, so "challenges",
# "challengeId", "difficultyName", "grantedChallengeAwards" and the rest are
# spelled the way the decoder expects.  The response type is
# RS4:FutGetTowChallengeServerResponse, paired in the binary with the URL
# suffix "/totw".
#
# The six difficulties and their coin awards match the retail ladder.  The
# squads come from server/data/totw_squads.json, which holds the real starting
# elevens with resource IDs taken from the official FIFA 19 TOTW pages, so the
# opponent is a genuine Team of the Week rather than a random pick of in-forms.
TOTW_DIFFICULTIES = (
    (1, "Beginner",     50),
    (2, "Amateur",     150),
    (3, "Semi-Pro",    250),
    (4, "Professional",400),
    (5, "World Class", 600),
    (6, "Legendary",   750),
)

# FIFA 19 has no static club row for a weekly FUT squad. Use the installed FUT
# presentation pool instead of choosing an arbitrary real club from the generic
# CPU fallback: the local catalogue contains badge 6112658 and the Frostbite
# team-kit table contains three team-112658 entries. The visible opponent name
# remains the inline ``TOTW 6`` / ``TW6`` supplied by GetClubInfo; this ID only
# supplies its badge and kit assets.
# Keep weekly TOTW presentation separate from the Squad Battles Icon Team.
# Both previously used Icons team 112658, so the native team adapter could
# reuse the most recently decoded ``TOTW 8`` name for the Featured Squad in
# Match Preview. Arsenal (team 1) is part of the locally verified onboarding
# presentation pool and supplies a complete badge/home/away-kit trio; every
# sourced TOTW week still shares the same presentation, as the historical
# challenge screen expects, while 112658 remains exclusive to the Icon Team.
TOTW_PRESENTATION_TEAM_ID = 1
ICON_PRESENTATION_TEAM_ID = 112658
# historical_reference/official weekly pages do not publish a FUT manager item. Reuse the
# already validated TOTW 6 manager choice for every added week, as well as its
# source-backed Icons-team badge and kits, instead of inventing weekly items.
TOTW_PRESENTATION_SEED = 500006

# historical_reference reports how many players sit in each line, not a formation code, so the
# shape is mapped onto the closest real FIFA 19 formation and falls back to the
# neutral 4-4-2 when a week does not match one.
_TOTW_SHAPES = {(4,4,2):"f442",(4,3,3):"f433",(3,5,2):"f352",(4,2,4):"f424",
                (4,5,1):"f451",(5,3,2):"f532",(3,4,3):"f343",(4,1,5):"f4141"}

# Native squad indices are formation slots, not an arbitrary player list.  The
# archived TOTW source preserves the eleven cards but not their on-pitch order;
# passing that order through put the first card (TOTW 6 Lewandowski) at index 0,
# which the 3-5-2 renderer correctly interpreted as goalkeeper.  Keep this map
# local to TOTW so Draft's independently verified ordering remains untouched.
_TOTW_FORMATION_POSITIONS = {
    "f442":   ("GK","RB","CB","CB","LB","RM","CM","CM","LM","ST","ST"),
    # Same correction as the Draft map. The live evidence is from Draft; the
    # geometry is a property of the client formation, not of the mode.
    "f433":   ("GK","RB","CB","CB","LB","CM","CM","CM","RW","ST","LW"),
    "f352":   ("GK","CB","CB","CB","CM","CM","RM","LM","CAM","ST","ST"),
    "f424":   ("GK","RB","CB","CB","LB","CM","CM","RW","LW","ST","ST"),
    "f451":   ("GK","RB","CB","CB","LB","RM","CM","CM","CM","LM","ST"),
    "f532":   ("GK","RWB","CB","CB","CB","LWB","CM","CM","CM","ST","ST"),
    "f343":   ("GK","CB","CB","CB","RM","CM","CM","LM","RW","LW","ST"),
    "f4141":  ("GK","RB","CB","CB","LB","CDM","RM","CM","CM","LM","ST"),
}

_TOTW_POSITION_COMPATIBILITY = {
    "GK":  ("GK",),
    "CB":  ("CB","RB","LB","RWB","LWB","CDM"),
    "RB":  ("RB","RWB","CB"),
    "LB":  ("LB","LWB","CB"),
    "RWB": ("RWB","RB","RM","CB"),
    "LWB": ("LWB","LB","LM","CB"),
    "CDM": ("CDM","CM","CB","CAM"),
    "CM":  ("CM","CDM","CAM","RM","LM"),
    "CAM": ("CAM","CF","CM","ST","RM","LM"),
    "RM":  ("RM","RW","RWB","CM","CAM"),
    "LM":  ("LM","LW","LWB","CM","CAM"),
    "RW":  ("RW","RF","RM","ST","CF"),
    "LW":  ("LW","LF","LM","ST","CF"),
    "ST":  ("ST","CF","RF","LF","RW","LW"),
}

_TOTW_CACHE=[]
def _totw_weeks():
    """The curated Team of the Week squads, read once and kept in memory."""
    if not _TOTW_CACHE:
        path=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data","totw_squads.json")
        try:
            with open(path,"r",encoding="utf-8") as handle:
                data=json.load(handle)
        except (OSError,ValueError) as exc:
            log("fut","totw_squads.json not usable: %s" % exc)
            data={}
        _TOTW_CACHE.extend(w for w in data.get("weeks",[]) if w.get("players"))
    return _TOTW_CACHE

def _totw_formation(week):
    shape=week.get("shape") or {}
    key=(int(shape.get("DEF",4) or 4),int(shape.get("MID",4) or 4),
         int(shape.get("ATT",2) or 2))
    return _TOTW_SHAPES.get(key,"f442")


def _totw_ordered_resources(week, formation):
    """Assign the sourced starting eleven to native formation indices."""
    players=list(week.get("players") or [])[:11]
    slots=_TOTW_FORMATION_POSITIONS.get(str(formation),
                                        _TOTW_FORMATION_POSITIONS["f442"])
    if len(players)!=len(slots):
        return [int(row["resourceId"]) for row in players]

    # Exhaustive dynamic assignment is only 11 * 2^11 states and avoids a
    # greedy winger/wing-back choice displacing a later exact match.  Exact
    # positions dominate; compatible alternatives retain their declared
    # preference order. Goalkeeper mismatch is effectively forbidden.
    scores=[]
    for slot in slots:
        compatible=_TOTW_POSITION_COMPATIBILITY.get(slot,(slot,))
        row=[]
        for player in players:
            position=str(player.get("position","")).upper()
            if position==slot:
                score=1000
            elif position in compatible:
                score=700-compatible.index(position)*40
            elif slot=="GK" or position=="GK":
                score=-10000
            else:
                score=0
            row.append(score)
        scores.append(row)

    states={0:(0,())}
    for slot_index in range(len(slots)):
        next_states={}
        for mask,(total,assignment) in states.items():
            for player_index in range(len(players)):
                bit=1<<player_index
                if mask & bit:
                    continue
                candidate=(total+scores[slot_index][player_index],
                           assignment+(player_index,))
                current=next_states.get(mask|bit)
                if current is None or candidate[0]>current[0]:
                    next_states[mask|bit]=candidate
        states=next_states
    assignment=states[(1<<len(players))-1][1]
    return [int(players[index]["resourceId"]) for index in assignment]

def _totw_seconds_until_rotation():
    """Seconds left in the current week, which is when the cover squad turns."""
    now=datetime.datetime.now(datetime.timezone.utc)
    start=now-datetime.timedelta(days=now.weekday(),hours=now.hour,
                                 minutes=now.minute,seconds=now.second,
                                 microseconds=now.microsecond)
    return max(0,int((start+datetime.timedelta(days=7)-now).total_seconds()))


def _totw_featured_squad():
    """Return the currently selected source-backed TOTW challenge squad."""
    challenge=_totw_challenge()
    if not challenge:
        return 0,{}
    return int(challenge.get("squadId",0) or 0),challenge.get("squad") or {}


def _totw_challenge(selector=None):
    """Resolve a challenge by week, squad id, or the durable selection."""
    payload=_totw_payload()
    if selector is None:
        wanted=int(payload.get("selectedChallengeId",0) or 0)
    else:
        try:
            wanted=int(selector or 0)
        except (TypeError,ValueError):
            return None
        if wanted>=500000:
            wanted-=500000
    return next((row for row in payload.get("challenges",[])
                 if int(row.get("challengeId",0) or 0)==wanted),None)


def _totw_public_squad_dto(squad_id, squad, squad_name=None):
    """Complete the selected public squad for RetrieveSquad.

    The full squad decoder consumes this flat root.  Challenge Select exposes
    two named aliases for each sourced squad; keep the selected alias name in
    the detailed response so the summary and RetrieveSquad caches agree.
    """
    squad_id=int(squad_id)
    source=json.loads(json.dumps(squad)) if isinstance(squad,dict) else {}
    if squad_name is not None:
        source["squadName"]=str(squad_name)
    metadata=_cpu_match_metadata(
        squad_id,TOTW_PRESENTATION_SEED,team_id=TOTW_PRESENTATION_TEAM_ID)
    return {**source,**metadata,
            "personaId":0x7fffffffffffffff,
            "squadType":"REGULAR_SQUAD","dreamSquad":False}


def _totw_match_squad():
    """Build the complete CPU opponent cached before TOTW CreateMatch.

    Offline Select has already materialized the public AWAY participant as
    owner ``0x7fffffffffffffff`` plus squad ``500006``.  CreateMatch itself
    returns the controlled HOME squad; this detailed CPU copy remains the AWAY
    participant selected by the preceding public-squad request.  It uses the
    match-only CPU identity (persona zero and dream-squad semantics): reusing
    the public sentinel owner would make the match decoder treat its badge as a
    user-side update and leave the CPU MatchData context incomplete.

    Keep the sourced roster, but use the retail CPU squad type accepted by the
    nested CreateMatch decoder.  ``TOTW_SQUAD`` is a public-content label and
    is not one of the native squad-type enum's three values; Draft and Squad
    Battles both use ``DREAM_SQUAD`` for an offline CPU participant.  Add the
    manager, badge, both kits, stadium and ball required by the
    MatchData/Press-Start chain. Neither persisted squad is mutated.
    """
    featured_id,featured=_totw_featured_squad()
    opponent=json.loads(json.dumps(featured)) if isinstance(featured,dict) else {}
    metadata=_cpu_match_metadata(
        featured_id,TOTW_PRESENTATION_SEED,
        team_id=TOTW_PRESENTATION_TEAM_ID)
    opponent.update(metadata)
    opponent.update({"personaId":0,"squadType":"DREAM_SQUAD",
                     "dreamSquad":True})
    return opponent


def _totw_string_words(value):
    """Pack the compact TOTW decoder's native-PC string representation."""
    encoded=str(value or "").encode("utf-8")
    words=[]
    for offset in range(0,len(encoded),4):
        chunk=encoded[offset:offset+4].ljust(4,b"\x00")
        words.append(int.from_bytes(chunk,"little",signed=False))
    return encoded,words


def _totw_clientdata():
    """Return every source-backed week in the proven native PC record."""
    # RS4:FutGetTowChallengeServerResponse is a positional decoder, unlike the
    # ordinary ClientData response used by onboarding and pile sizes.  Static
    # inspection of CardsDLL+0x259290 shows that the first scalar is stored as
    # the wire byte-order selector; the nested record decoder at +0x2594b0
    # treats value 3 as the native PC ordering.  Each outer key identifies a
    # cached featured squad; its nested string key is that squad's exact name.
    # The featured-squad callback assigns the sentinel outer key below.  The
    # first byte of the eight-byte value is the COMPLETED flag read by the tile.
    # A zero-record header parses safely but leaves the tile without a matching
    # formation, which is why the retail screen remained on its spinner.
    payload=_totw_payload()
    def records(values):
        # The scalar reader at CardsDLL+0x259a10 enumerates array objects and
        # extracts member ``value`` (ID 0x458); anonymous JSON numbers never
        # advance it. ``key`` preserves and tests the positional wire order.
        return [{"key":index,"value":value}
                for index,value in enumerate(values)]
    challenges=list(payload.get("challenges",[]) or [])
    if not challenges:
        return {"data":records([3,0])}
    difficulty=max(1,min(len(TOTW_DIFFICULTIES),int(
        payload.get("difficulty",STATE.get("totw_difficulty",3)) or 3)))
    values=[3,1,              # native PC word order; one public owner
            0x7fffffff,-1,   # featured-squad cache identity
            len(challenges)]
    for challenge in challenges:
        squad=challenge.get("squad") or {}
        name=str(squad.get("squadName",challenge.get("name","")) or "")
        encoded,words=_totw_string_words(name)
        values.extend([len(encoded),*words,
                       # Native challenge state: COMPLETED, DIFFICULTY,
                       # POINTS_AWARDED, HOME_SCORE and AWAY_SCORE.
                       int(bool(challenge.get("completed",False))),
                       difficulty,0,0,0])
    return {"data":records(values)}


def _totw_payload():
    difficulty=int(STATE.get("totw_difficulty",3) or 3)
    difficulty=max(1,min(len(TOTW_DIFFICULTIES),difficulty))
    name,award=next((n,a) for d,n,a in TOTW_DIFFICULTIES if d==difficulty)
    legacy_completed=int(STATE.get("totw_completed_count",0) or 0)
    completed_ids={int(value) for value in
                   (STATE.get("totw_completed_challenges",[]) or [])}
    weeks=_totw_weeks()
    challenges=[]
    for week_index,week in enumerate(weeks):
        number=int(week.get("week",len(challenges)+1))
        squad_id=500000+number
        # Only the starting eleven is sourced: the official pages show card art
        # for the eleven, so their resource IDs are real, while the bench has no
        # artwork and therefore no recoverable ID.  The remaining slots are
        # filled with other in-forms, seeded by week so a challenge always
        # fields the same side.
        formation=_totw_formation(week)
        resources=_totw_ordered_resources(week,formation)
        for rid in _source_backed_roster({"IF","SIF","TIF"},75,99,seed=number):
            if len(resources)>=23: break
            if rid not in resources: resources.append(rid)
        squad=_native_cpu_squad(
            squad_id,week.get("name","TOTW %d"%number),
            resources,formation,"TOTW_SQUAD")
        challenges.append({
            "challengeId":number,"challengeImageId":number,
            "name":week.get("name","TOTW %d"%number),
            "releaseDate":week.get("releaseDate",""),
            "averageRating":week.get("averageRating",0),
            "difficulty":difficulty,"difficultyName":name,
            "matchDifficulty":difficulty,
            "squadId":squad_id,"squad":squad,"opponent":squad,
            "completed":(number in completed_ids or
                         (not completed_ids and week_index<legacy_completed)),
            "grantedChallengeAwards":[
                {"type":"coins","value":award,"count":1}],
        })
    completed=sum(bool(row["completed"]) for row in challenges)
    selected_id=int(STATE.get("totw_challenge_id",0) or 0)
    selected=next((row for row in challenges
                   if int(row["challengeId"]) == selected_id),None)
    if selected is None:
        # With no saved choice, feature the newest source-backed week. This is
        # the offline equivalent of the current live TOTW advertised by the
        # retail Single Player tile.
        selected=challenges[-1] if challenges else None
    return {"totwEnabled":True,"enabled":True,
            "challenges":challenges,
            "challengesCount":len(challenges),
            "challengesCompletedCount":min(completed,len(challenges)),
            "difficulty":difficulty,"difficultyName":name,
            "matchDifficulty":difficulty,
            "difficultyModifiers":[
                {"difficulty":d,"difficultyName":n,
                 "winDifficultyModifiers":a,"lossDifficultyModifiers":0,
                 "grantedChallengeAwards":[{"type":"coins","value":a,"count":1}]}
                for d,n,a in TOTW_DIFFICULTIES],
            # The screen still reads the legacy single-squad members, so the
            # currently selected week is repeated under the old names.
            "selectedChallengeId":int(selected["challengeId"]) if selected else 0,
            "totwId":selected["squadId"] if selected else 0,
            "squadId":selected["squadId"] if selected else 0,
            "squad":selected["squad"] if selected else {},
            "totw":selected["squad"] if selected else {},
            "opponent":selected["squad"] if selected else {},
            "awards":[{"type":"coins","value":award,"count":1}]}


def _sqbt_opponent_definitions(event_id, rotation=0):
    result=[]
    for index,club in enumerate(sqbt_catalogue_opponents(
            int(event_id),int(rotation))):
        opponent_id=int(event_id)*100+int(rotation)*10+index+1
        team_id=int(club.get("clubId",0) or 0)
        name=str(club.get("name","") or "Squad Battles XI")
        resources=_source_backed_team_roster(
            team_id,seed=opponent_id,count=23)
        # The selected pools are made only from clubs that can field a valid
        # catalogue XI.  Keep a source-backed safety net for a damaged import,
        # while never inventing a player/resource ID.
        if len(resources)<23:
            resources+=_source_backed_roster(
                "Normal",60,99,seed=opponent_id+97,
                count=23-len(resources),
                excluded_assets={int(native_player_fields(value).get(
                    "assetId",0) or 0) for value in resources})
        squad=_native_cpu_squad(opponent_id,name,resources,"f442",
                                "SQUAD_BATTLE_SQUAD",team_id)
        opponent_persona=1_100_000_000+(opponent_id%100_000_000)
        squad.update({"personaId":opponent_persona,"teamId":team_id,
                      "badgeId":team_id,"dreamSquad":False})
        result.append({"opponentId":opponent_id,"sqbtOppid":opponent_id,
            "sqbtOpponentSquadId":opponent_id,"rotation":int(rotation),
            "name":name,"opponentRating":int(squad["rating"]),
            "rating":int(squad["rating"]),"chemistry":100,
            "stars":float(club.get("stars",0) or 0),
            "slot":int(club.get("slot",index+1) or index+1),
            "opponentScore":0,"opponentTeamId":team_id,
            "opponentBadgeId":team_id,"clubId":team_id,
            "personaId":opponent_persona,"played":False,
            "definitionVersion":2,"source":"fut19-club-pool",
            "squad":squad})
    return result


def _sqbt_featured_definition(event_id):
    """Return the weekly, source-backed Prime Icon Moments Featured Squad."""
    event_id=int(event_id)
    feature_id=event_id*1000+99
    resources=featured_squad_resource_ids()
    squad=_native_cpu_squad(
        feature_id,FEATURED_SQUAD_NAME,resources,
        FEATURED_SQUAD_FORMATION,"FEATURED_SQUAD",
        ICON_PRESENTATION_TEAM_ID)
    # The formation slot is authoritative, but FIFA 19 also renders the
    # player's catalogue preferredPosition in the Featured Squad preview.
    # Publish both wide/central attackers explicitly so Ronaldinho is shown
    # at LW and Ronaldo at ST instead of inheriting a mismatched card value.
    squad["players"][9]["itemData"]["preferredPosition"]="ST"
    squad["players"][10]["itemData"]["preferredPosition"]="LW"
    persona_id=1_200_000_000+(feature_id%100_000_000)
    # 112658 is EA's real FIFA 19 Icons team.  Its badge (resource 6112658)
    # and three kit rows exist in the installed catalog, so the Featured club
    # no longer falls back to Leeds/TOTW or to an empty shield in preview.
    squad.update({"personaId":persona_id,"dreamSquad":True,
                  "teamId":ICON_PRESENTATION_TEAM_ID,
                  "clubId":ICON_PRESENTATION_TEAM_ID,
                  "opponentTeamId":ICON_PRESENTATION_TEAM_ID,
                  "badgeId":ICON_PRESENTATION_TEAM_ID,
                  "opponentBadgeId":ICON_PRESENTATION_TEAM_ID,
                  "badgeAssetId":ICON_PRESENTATION_TEAM_ID,
                  "badgeResourceId":6000000+ICON_PRESENTATION_TEAM_ID,
                  "starRating":5})
    return {
        "opponentId":feature_id,"sqbtOppid":feature_id,
        "sqbtOpponentSquadId":feature_id,"featuredSquadId":feature_id,
        # Rotation -1 keeps the Featured Squad durable and playable without
        # adding it to the four rotating Opponent Select slots.
        "rotation":-1,"name":FEATURED_SQUAD_NAME,
        "opponentRating":int(squad["rating"]),"rating":int(squad["rating"]),
        "chemistry":100,"stars":5.0,"slot":0,"opponentScore":0,
        "opponentTeamId":ICON_PRESENTATION_TEAM_ID,
        "opponentBadgeId":ICON_PRESENTATION_TEAM_ID,
        "badgeAssetId":ICON_PRESENTATION_TEAM_ID,
        "badgeResourceId":6000000+ICON_PRESENTATION_TEAM_ID,
        "clubId":ICON_PRESENTATION_TEAM_ID,
        "personaId":persona_id,"played":False,"definitionVersion":4,
        "source":"fut19-featured-prime-icon-moments","squad":squad,
    }


def _sqbt_featured_opponent(event=None):
    event=event or _ensure_sqbt_event()
    event_id=int(event["id"])
    stored=STATE.sqbt_opponents(event_id,-1)
    if (stored and int(stored[0].get("definitionVersion",0) or 0)>=4 and
            int(stored[0].get("clubId",0) or 0)==
            ICON_PRESENTATION_TEAM_ID):
        return stored[0]
    definition=_sqbt_featured_definition(event_id)
    STATE.ensure_sqbt_event(event_id,str(event.get("event_key",event_id)),
                            int(event["expires"]),[definition])
    stored=STATE.sqbt_opponents(event_id,-1)
    return stored[0] if stored else definition


def _sqbt_featured_squad(event=None):
    opponent=_sqbt_featured_opponent(event)
    return int(opponent["opponentId"]),_sqbt_match_squad(opponent)


def _sqbt_match_squad(opponent):
    """Return the Squad Battles CPU opponent embedded by CreateMatch.

    `_sqbt_opponent_definitions` stores a presentation-only squad: it has no
    `manager` and no `actives`, and `_cpu_match_metadata` documents that an
    empty pair leaves FIFA in a standalone, untimed Skill Game.  The Draft
    opponent at `_draft_match_squad` is the live-verified shape, so build the
    same one here from the stored event opponent.  The match-only identity is
    persona zero: a synthetic non-zero persona makes the native match session
    treat the CPU as a remote peer, disabling pause and waiting forever for
    that peer at the half-time synchronization boundary.
    """
    if not isinstance(opponent, dict) or not opponent.get("squad"):
        return {}
    squad = json.loads(json.dumps(opponent["squad"]))
    opponent_id = int(opponent.get("opponentId", 0) or 0)
    team_id=int(opponent.get("clubId",opponent.get(
        "opponentTeamId",0)) or 0)
    metadata = _cpu_match_metadata(opponent_id, opponent_id * 31 + 7,
                                   team_id or None)
    squad.update({"teamId": metadata["teamId"],
                  "clubId":metadata["teamId"],
                  "opponentTeamId":metadata["teamId"],
                  "badgeId": metadata["badgeId"],
                  "opponentBadgeId":metadata["badgeId"],
                  "badgeAssetId":metadata["badgeId"],
                  "badgeResourceId":6000000+metadata["badgeId"],
                  "coachId": metadata["coachId"],
                  "personaId":0,
                  "manager": metadata["manager"],
                  "actives": metadata["actives"],
                  "club":metadata["actives"],
                  # Native squadType is a closed REGULAR/DREAM/DRAFT enum.
                  # FEATURED_SQUAD and SQUAD_BATTLE_SQUAD are content labels,
                  # not parser values; they left the AWAY preview undefined.
                  "squadType":"DREAM_SQUAD","dreamSquad":True,
                  "active":True,"valid":True,
                  "starRating":_native_star_rating(
                      squad.get("rating",75) or 75),
                  "clubName":str(opponent.get("name","") or
                                 squad.get("squadName","") or
                                 "Squad Battles XI")})
    return squad


def _sqbt_selected_opponent():
    """Return the explicitly selected, still-playable current opponent."""
    event = _ensure_sqbt_event()
    opponents = event.get("opponents", []) or []
    try:
        wanted = int(STATE.get(SELECTED_SQBT_OPPONENT_KEY, 0) or 0)
    except (TypeError, ValueError):
        wanted = 0
    if wanted<=0:
        return None
    selected = next((value for value in opponents
                     if int(value.get("opponentId", 0) or 0) == wanted and
                     not bool(value.get("played"))), None)
    if selected is None:
        featured=_sqbt_featured_opponent(event)
        if (int(featured.get("opponentId",0) or 0)==wanted and
                not bool(featured.get("played"))):
            selected=featured
    if selected is None:
        STATE.set(SELECTED_SQBT_OPPONENT_KEY,0)
    return selected


_SQBT_OPPONENT_FIELDS=("sqbtOpponentSquadId","sqbtOppid","opponentId",
                       "selectedOpponentId","opponentSquadId",
                       "featuredSquadId")
_SQBT_DIFFICULTY_FIELDS=("sqbtMatchDifficulty","difficulty","difficultyId",
                         "gameDifficulty","skillLevel")


def _sqbt_request_integer(payload,names,default=0):
    payload=payload if isinstance(payload,dict) else {}
    for name in names:
        if name not in payload:
            continue
        try:
            return int(payload.get(name,default) or default)
        except (TypeError,ValueError):
            return int(default)
    return int(default)


def _sqbt_select_from_payload(payload,event=None):
    """Persist the retail opponent/difficulty fields from a request."""
    event=event or _ensure_sqbt_event()
    wanted=_sqbt_request_integer(payload,_SQBT_OPPONENT_FIELDS,0)
    if wanted<=0:
        return None
    opponent=next((value for value in event.get("opponents",[])
                   if int(value.get("opponentId",0) or 0)==wanted and
                   not bool(value.get("played"))),None)
    if opponent is None:
        featured=_sqbt_featured_opponent(event)
        if (int(featured.get("opponentId",0) or 0)==wanted and
                not bool(featured.get("played"))):
            opponent=featured
    if opponent is None:
        return None
    difficulty=max(1,min(7,_sqbt_request_integer(
        payload,_SQBT_DIFFICULTY_FIELDS,
        STATE.get("sqbt_difficulty",3) or 3)))
    STATE.set(SELECTED_SQBT_OPPONENT_KEY,wanted)
    STATE.set("sqbt_difficulty",difficulty)
    return opponent


def _sqbt_selection_response(opponent,event=None):
    event=event or _ensure_sqbt_event()
    difficulty=max(1,min(7,int(STATE.get("sqbt_difficulty",3) or 3)))
    return {"sqbtEventId":int(event["id"]),
            "selectedOpponentId":int(opponent.get("opponentId",0) or 0),
            "sqbtOppid":int(opponent.get("opponentId",0) or 0),
            "sqbtOpponentSquadId":int(opponent.get("opponentId",0) or 0),
            "sqbtMatchDifficulty":difficulty,"difficulty":difficulty,
            "opponent":_sqbt_opponent_wire(opponent)}


def _sqbt_result_stat(payload,names,opponent=False,default=0):
    payload=payload if isinstance(payload,dict) else {}
    stats=payload.get("opponentMatchStats" if opponent else "myMatchStats",{})
    stats=stats if isinstance(stats,dict) else {}
    for name in names:
        source=stats if name in stats else payload
        if name not in source:
            continue
        try:
            return int(float(source.get(name,default) or default))
        except (TypeError,ValueError):
            continue
    return int(default)


def _sqbt_result_scores(payload):
    """Recover the score from every retail match-end representation.

    The live FIFA 19 request reports player goals in ``items`` even when the
    compact root omits homeGoals/awayGoals.  Using those stats prevents a 4-1
    result from being persisted as 0-0, which also affected Battle Points and
    the opponent history row.
    """
    payload=payload if isinstance(payload,dict) else {}
    home_names=("homeGoals","home","goals","goalsScored")
    away_names=("awayGoals","away","goalsAgainst","opponentGoals")
    has_home=(any(name in payload for name in home_names) or
              isinstance(payload.get("myMatchStats"),dict) and
              any(name in payload["myMatchStats"] for name in
                  ("goals","goalsScored")))
    has_away=(any(name in payload for name in away_names) or
              isinstance(payload.get("opponentMatchStats"),dict) and
              any(name in payload["opponentMatchStats"] for name in
                  ("goals","goalsScored")))
    home=_sqbt_result_stat(payload,home_names,False,0)
    away=_sqbt_result_stat(payload,away_names,True,0)
    if not has_home:
        home=sum(max(0,int(item.get("goals",0) or 0))
                 for item in payload.get("items",[])
                 if isinstance(item,dict))
    return max(0,home),max(0,away),has_away


def _completed_match_scores(payload,result,active=None):
    """Resolve a coherent HOME/AWAY score for every completed local mode.

    The HTTP request sometimes carries explicit goals and sometimes only item
    goal stats.  A retail Draft completion carries neither: its real score is
    read from the immediately preceding Blaze GameReporting packet and stored
    on the active match receipt.  The final one-goal fallback is used only
    when that packet genuinely omitted ``SCOR``; it preserves the proven match
    outcome instead of persisting the impossible WIN 0-0 seen in the UI.
    """
    home,away,has_away=_sqbt_result_scores(payload)
    normalized=str(result or "DRAW").upper()
    if normalized == "LOSE": normalized="LOSS"
    explicit_home=any(name in (payload or {}) for name in
                      ("homeGoals","home","goals","goalsScored"))
    item_home=any(int(item.get("goals",0) or 0)>0
                  for item in (payload or {}).get("items",[])
                  if isinstance(item,dict))
    if has_away and (explicit_home or item_home or home or away):
        return home,away,"http"

    active=active if isinstance(active,dict) else {}
    raw=[]
    for value in active.get("gameReportScores",[]) or []:
        try: value=int(value)
        except (TypeError,ValueError): continue
        if 0 <= value <= 50: raw.append(value)
    distinct=[]
    for value in raw:
        if value not in distinct: distinct.append(value)
    if len(distinct)>=2:
        high=max(distinct); low=min(distinct)
        if normalized=="WIN": return high,low,"blaze"
        if normalized=="LOSS": return low,high,"blaze"
        return distinct[0],distinct[1],"blaze"
    if raw and len(raw)>=2 and normalized=="DRAW":
        return raw[0],raw[0],"blaze"

    if home>0:
        if normalized=="WIN": return home,max(0,home-1),"item-fallback"
        if normalized=="LOSS": return home,home+1,"item-fallback"
        return home,home,"item-fallback"
    if normalized=="WIN": return 1,0,"result-fallback"
    if normalized=="LOSS": return 0,1,"result-fallback"
    return 0,0,"result-fallback"


def _sqbt_match_coin_breakdown(payload,result,end_reason="",score_override=None):
    """Calculate the deterministic FIFA 19 offline match-coin award."""
    payload=payload if isinstance(payload,dict) else {}
    normalized=str(result or "LOSS").upper()
    reason=str(end_reason or normalized).upper()
    home,away,has_away=_sqbt_result_scores(payload)
    if score_override is not None:
        home=max(0,int(score_override[0])); away=max(0,int(score_override[1]))
        has_away=True
    completed=(normalized in ("WIN","DRAW","LOSS") and
               reason not in ("QUIT","DNF","DISCONNECT","FORFEIT",
                              "NO_CONTEST"))
    if not completed:
        return {"matchCoins":0,"skillReward":0,"completionAward":0,
                "goals":home,"goalsAgainst":away,"parts":{},
                "completed":False}

    shots=max(0,_sqbt_result_stat(
        payload,("shotsOnTarget","shotsontarget"),False,0))
    tackles=max(0,_sqbt_result_stat(
        payload,("successfulTackles","tacklesWon","tackles"),False,0))
    corners=max(0,_sqbt_result_stat(
        payload,("corners","cornerKicks"),False,0))
    passing=max(0,min(100,_sqbt_result_stat(
        payload,("passingPercentage","passAccuracy","passing"),False,0)))
    possession=max(0,min(100,_sqbt_result_stat(
        payload,("possessionPercentage","possession"),False,0)))
    fouls=max(0,_sqbt_result_stat(payload,("fouls",),False,0))
    yellows=max(0,_sqbt_result_stat(
        payload,("yellowCards","yellow"),False,0))
    reds=max(0,_sqbt_result_stat(payload,("redCards","red"),False,0))
    offsides=max(0,_sqbt_result_stat(payload,("offsides",),False,0))
    motm=1 if _sqbt_result_stat(
        payload,("manOfTheMatch","motm"),False,0) else 0
    parts={
        "goals":min(home,5)*40,
        "shotsOnTarget":min(shots,10)*5,
        "successfulTackles":min(tackles,20),
        "corners":min(corners,10)*5,
        "cleanSheet":75 if has_away and away==0 else 0,
        "passAccuracy":min(passing,80),
        "possession":min(possession,80),
        "manOfTheMatch":15 if motm else 0,
        "goalsConceded":-min(away,4)*20,
        "fouls":-min(fouls,4)*5,
        "cards":-min(yellows+reds,8)*10,
        "offsides":-min(offsides,15),
    }
    skill=sum(parts.values())
    try:
        seconds=max(0,int(payload.get("secondsPlayed",0) or 0))
    except (TypeError,ValueError):
        seconds=0
    minutes=(min(90.0,seconds/60.0) if seconds>180 else
             min(90.0,float(seconds)))
    if minutes<=0:
        minutes=90.0
    completion=int(round(325.0*minutes/90.0))
    return {"matchCoins":min(MAX_MATCH_COIN_REWARD,max(0,skill+completion)),
            "skillReward":skill,"completionAward":completion,
            "goals":home,"goalsAgainst":away,"parts":parts,
            "completed":True}


def _sqbt_destroy_match_response(result,end_reason,recorded,breakdown,
                                  difficulty):
    """Native reward DTO used by the Squad Battles post-match screen.

    Keep the response scalar except for the one proven Squad Battles score
    object.  This avoids the unsafe Draft array branches while still exposing
    the balance, match coins and Battle Points that FIFA's result view reads.
    """
    result=str(result or "LOSS").upper()
    reason=str(end_reason or result).upper()
    if reason in ("DISCONNECT","FORFEIT"):
        reason="QUIT"
    if reason not in ("WIN","LOSS","DRAW","DNF","QUIT","NO_CONTEST",
                      "DNF_WIN","DNF_DRAW","DNF_LOSS"):
        reason="NO_CONTEST"
    coins=min(MAX_MATCH_COIN_REWARD,max(
        0,int(recorded.get("rewardCoins",0) or 0)))
    balance=max(0,int(recorded.get("credits",STATE.credits()) or 0))
    completed=bool(breakdown.get("completed",False))
    points=(min(2000,max(0,int(recorded.get("points",0) or 0)))
            if completed else 0)
    difficulty=max(1,min(7,int(difficulty or 1)))
    if result=="WIN":
        result_base=SQBT_MATCH_RESULT_WIN
        difficulty_score=SQBT_WIN_POINTS[difficulty-1]
    elif result=="DRAW":
        result_base=SQBT_MATCH_RESULT_DRAW
        difficulty_score=round(SQBT_WIN_POINTS[difficulty-1]*0.45)
    else:
        result_base=SQBT_MATCH_RESULT_LOSS
        difficulty_score=SQBT_LOSS_POINTS[difficulty-1]
    modifier=(float(difficulty_score)/float(result_base)
              if completed else 0.0)
    record=STATE.record()
    return {
        "endReason":reason,
        "allCoins":coins,"credits":balance,"coins":balance,
        "totalCredits":balance,"funds":balance,"finalFunds":balance,
        "sessionCoinsBankBalance":balance,
        "matchCoins":coins,"seasonCoins":coins,
        "rewardCoins":coins,"totalCoins":coins,
        "completionAward":int(breakdown.get("completionAward",0) or 0),
        "skillAward":int(breakdown.get("skillReward",0) or 0),
        "boostConis":0,"boostCountLeft":0,
        "gameModeAward":{"bidTokens":0,"coins":coins},
        "squadBattlesScore":{
            "finalScore":points,
            "goalsScore":max(0,int(breakdown.get("goals",0) or 0))*40
                         if completed else 0,
            "matchDifficultyScoreModifier":modifier,
            # FIFA applies this base through the dimensionless difficulty
            # modifier above. Repeating finalScore here made retail combine the
            # total twice and render values such as 126960 after a 140-point
            # fixture.
            "matchResultScore":result_base if completed else 0,
            "skillRatingScore":0,
            "teamRatingScore":0,
        },
        "record":{"won":record["wins"],"draw":record["draws"],
                  "loss":record["losses"]},
        "gamesWon":record["wins"],"gamesDraw":record["draws"],
        "gamesLost":record["losses"],
        "gamesPlayed":record["wins"]+record["draws"]+record["losses"],
        "won":1 if result=="WIN" else 0,
        "draw":1 if result=="DRAW" else 0,
        "loss":1 if result=="LOSS" else 0,
        "dnfModifier":1.0,
    }


SQBT_EVENT_EPOCH=1546300800  # 2019-01-01 00:00 UTC


def _sqbt_event_context(reference_time=None):
    """Return the stable three-day Squad Battles competition window."""
    current=now_s() if reference_time is None else int(reference_time)
    cycle=max(0,(current-SQBT_EVENT_EPOCH)//SQBT_EVENT_DURATION_SECONDS)
    start=SQBT_EVENT_EPOCH+cycle*SQBT_EVENT_DURATION_SECONDS
    expires=start+SQBT_EVENT_DURATION_SECONDS
    start_date=datetime.datetime.fromtimestamp(
        start,datetime.timezone.utc).strftime("%Y-%m-%d")
    end_date=datetime.datetime.fromtimestamp(
        expires,datetime.timezone.utc).strftime("%Y-%m-%d")
    # Opponent and Featured Squad identities are derived as event*100 and
    # event*1000+99. Keep every derived identity within the native signed
    # 32-bit range consumed by the FIFA 19 hub and featured-squad controllers.
    # The former 30,000,000 base produced feature ids above 30 billion; the
    # client rejected that context before issuing /featuredsquad requests.
    return {"eventId":2_000_000+int(cycle),"start":int(start),
            "expires":int(expires),"eventKey":"%s/%s" %
            (start_date,end_date)}


def _ensure_sqbt_event(rotation=None):
    context=_sqbt_event_context(); event_id=int(context["eventId"])
    expires=int(context["expires"]); event_key=str(context["eventKey"])
    existing=STATE.sqbt_event(event_id)
    if (rotation is None and existing and
            len(existing.get("opponents",[]))==4 and
            all(int(value.get("definitionVersion",0) or 0)>=2
                for value in existing.get("opponents",[]))):
        if not STATE.sqbt_opponents(event_id,-1):
            STATE.ensure_sqbt_event(
                event_id,event_key,expires,
                [_sqbt_featured_definition(event_id)])
        return existing
    if rotation is None:
        rotation=max([int(value.get("rotation",0) or 0)
                      for value in (existing or {}).get("opponents",[])] or [0])
    opponents=_sqbt_opponent_definitions(event_id,int(rotation))
    if not STATE.sqbt_opponents(event_id,-1):
        opponents.append(_sqbt_featured_definition(event_id))
    return STATE.ensure_sqbt_event(event_id,event_key,
                                   expires,opponents)


def _sqbt_hub_payload():
    # Repair the one legacy settlement shape that awarded loss points for a
    # withdrawal before publishing the score or the retry receipt.
    STATE.migrate_legacy_sqbt_quits()
    event=_ensure_sqbt_event(); score=int(event.get("score",0) or 0)
    opponents=event.get("opponents",[])
    match_stats=STATE.sqbt_match_stats(int(event["id"]))
    matches_played=int(match_stats.get("matchesPlayed",0) or 0)
    # Score is the authoritative competition value.  Re-resolve the displayed
    # placement from it on every hub read so profiles created by older builds
    # (or points restored from a durable match table) cannot retain a stale
    # Bronze/not-ranked tier beside their real weekly points.
    rank=sqbt_user_rank(int(event["id"]),score,
                        rounds_played=matches_played)
    tier=sqbt_tier_level(int(event["id"]),score)
    claimable=STATE.claimable_sqbt_event()
    prize_tiers=sqbt_prize_tiers(int(event["id"]))
    event_view={"id":int(event["id"]),"eventId":int(event["id"]),
                "sqbtEventId":int(event["id"]),
                "endTime":int(event["expires"]),
                "endTimeStamp":int(event["expires"]),
                "expiryTime":int(event["expires"]),
                "rank":rank,"score":score,
                "userTierLevel":tier}
    return {"_startTime":int(event["expires"])-SQBT_EVENT_DURATION_SECONDS,
            "_endTime":int(event["expires"]),
            "sqbtEventId":int(event["id"]),"rank":rank,
            "score":score,"userScore":score,"userRank":rank,
            "userTierLevel":tier,
            "sqbtEvent":event_view,
            "isPrizeAvailable":claimable is not None,
            "claimableEventId":int((claimable or {}).get("id",0) or 0),
            "matchesPlayed":matches_played,
            "matchesRemaining":max(0,SQBT_MAX_POINT_MATCHES-matches_played),
            "maxMatches":SQBT_MAX_POINT_MATCHES,
            "wins":int(match_stats.get("wins",0) or 0),
            "draws":int(match_stats.get("draws",0) or 0),
            "losses":int(match_stats.get("losses",0) or 0),
            "prizeTiers":prize_tiers,"sqbtOppSquadList":opponents,
            "sqbtOppSquads":opponents,"opponents":opponents,
            "nextSquadRefreshTimeStamp":_sqbt_next_refresh({
                "opponents":opponents,"expires":int(event["expires"])}),
            "nextLBRefreshTimeStamp":int(event["expires"]),
            "nextFeatureSquadTimeStamp":int(event["expires"]),
            "squadBattleEnabled":True,"squadBattleCouchPlayEnabled":True}

def _sqbt_hub_wire(payload):
    """Return exactly the members `FutGetSquadBattleHubServerResponse` decodes.

    The parser is `CardsDLL+0x250010`, reached through the response allocated at
    `+0x24ff90` (size 0x28). Decoding its field-id switch
    against the RS4 name table at `+0x34c0c0` gives the complete closed DTO:

    * `sqbtEventId`, `score`, `rank`, `userTierLevel`, `gameModeRestriction`
    * `currentTime` -> `[response+0x50]`, `startTime` -> `+0x40`,
      `endTime` -> `+0x48`, all read as 64-bit integers at `+0x24c996`,
      `+0x24ba78` and `+0x24ba63`
    * `matchResultWin` and `matchResultLoss`, clamped integers
    * `difficultyModifiers`, `winDifficultyModifiers` and
      `lossDifficultyModifiers`, arrays the parser caps at seven single-byte
      entries (`cmp rdi, 7` at `+0x24c920`), one per difficulty
    * `prizeTiers`, an array of objects carrying `tierLevel`, `tierStart`,
      `tierStartValue`, `tierEndValue` and `tierType`

    FIFA 18's proven retail contract additionally nests the lightweight
    opponent list under `sqbtOppSquadList`. FIFA 19 does not issue a separate
    refresh request on first entry, so omitting that compatible member leaves
    Opponent Select disabled. Only descriptors belong here; the 23-player body
    stays behind `/sqbt/user/opponentsquad/{id}`. `tierType` is a string in
    the retail parser, not a numeric enum. The hub view model compares it with
    the literal `LB_RANKING_TYPE` at CardsDLL+0x967de: that path renders the
    fixed leaderboard band instead of passing tierLevel=0 through the normal
    1..12 rank-name switch, where it becomes `FUT_CHAMP_NOT_RANKED`. The
    expanded `awards` rows populate the reward preview panel for every rank.
    """
    event_end=int(payload.get("_endTime",0) or 0)
    event_start=int(payload.get("_startTime",0) or 0)
    # CardsDLL parses all three modifier arrays as floating-point values
    # (`+0x2503bd..+0x2503d8` and `+0x25118a..+0x2511ac`).  Sending percentages
    # such as 100 made the client multiply a plausible 1,593-point projection
    # by 100 and display 159,300.  These are ratios against the 1,000-point win
    # base, not whole percentages.
    modifiers=[value/SQBT_MATCH_RESULT_WIN
               for value in SQBT_WIN_POINTS]
    tiers=[]
    percentage_cursor=0
    for tier in payload.get("prizeTiers",[]):
        start=int(tier.get("minScore",0) or 0)
        end=int(tier.get("maxScore",0) or 0)
        tier_level=int(tier.get("tierLevel",0) or 0)
        # tierStart/tierEnd are rank/percentage boundaries, not point values.
        # Omitting tierEnd left most percentage bands invalid in the retail
        # rank carousel, so tiers and their reward panels disappeared even
        # though tierStartValue/tierEndValue contained the correct point floor.
        if tier_level == 0:
            tier_start=1
            tier_end=100
            tier_type="LB_RANKING_TYPE"  # fixed Top 100 leaderboard band
        else:
            tier_start=percentage_cursor
            percentage_cursor+=max(0,int(tier.get("percent",0) or 0))
            tier_end=percentage_cursor
            # Any other string takes the native percentage-tier path. Keep it
            # empty rather than inventing an unsupported numeric/string enum.
            tier_type=""
        # Squad Battles uses the same native Award record decoded elsewhere
        # by CardsDLL.  Keeping only numeric type/value pairs made the right
        # hand reward panel empty because pack HAL ids, quantities and the
        # untradeable/player-pick metadata never reached the client.
        wire_awards=[native_reward_dto(award)
                     for award in tier.get("awards",[]) or []
                     if isinstance(award,dict)]
        tiers.append({"tierLevel":int(tier.get("tierLevel",0) or 0),
                      "tierStart":tier_start,"tierEnd":tier_end,
                      "tierStartValue":start,
                      "tierEndValue":end,"tierType":tier_type,
                      "awards":wire_awards})
    opponents={
        "nextSquadRefreshTimeStamp":_sqbt_next_refresh({
            "opponents":payload.get("opponents",[]),
            "expires":event_end,
        }),
        "sqbtOppSquads":[_sqbt_opponent_wire(value)
                          for value in payload.get("opponents",[])],
    }
    return {"sqbtEventId":int(payload.get("sqbtEventId",0) or 0),
            "score":int(payload.get("score",0) or 0),
            "rank":int(payload.get("rank",0) or 0),
            "userTierLevel":int(payload.get("userTierLevel",0) or 0),
            # FIFA 19 does not issue the refresh request on first entry when
            # the compatible opponent list is already embedded below. Carry
            # the reward-day trigger on the initial hub response too.
            "isPrizeAvailable":bool(payload.get("isPrizeAvailable",False)),
            "gameModeRestriction":0,
            "currentTime":now_s(),
            "startTime":event_start,
            "endTime":event_end,
            "matchResultWin":SQBT_MATCH_RESULT_WIN,
            "matchResultLoss":SQBT_MATCH_RESULT_LOSS,
            "difficultyModifiers":modifiers,
            "winDifficultyModifiers":modifiers,
            "lossDifficultyModifiers":modifiers,
            "prizeTiers":tiers,
            "sqbtOppSquadList":opponents}


def _sqbt_opponent_wire(opponent):
    """Return the lightweight opponent descriptor used by refresh/list DTOs."""
    opponent=opponent if isinstance(opponent,dict) else {}
    opponent_id=int(opponent.get("opponentId",0) or 0)
    team_id=int(opponent.get("clubId",opponent.get(
        "opponentTeamId",0)) or 0)
    name=str(opponent.get("name","") or "Squad Battles XI")
    words=[word for word in _re.split(r"[^A-Za-z0-9]+",name.upper()) if word]
    abbreviation=("".join(word[:1] for word in words)[:3] or
                  name[:3].upper() or "CPU")
    played=bool(opponent.get("played"))
    points=(int(opponent.get("pointsWon",0) or 0) if played else -1)
    user_score=(int(opponent.get("userScore",0) or 0) if played else -1)
    opp_score=(int(opponent.get("oppScore",0) or 0) if played else -1)
    rating=int(opponent.get("opponentRating",opponent.get("rating",0)) or 0)
    return {"id":opponent_id,"squadId":opponent_id,
            "opponentId":opponent_id,"sqbtOppid":opponent_id,
            "sqbtOpponentSquadId":opponent_id,
            "name":name,"clubName":name,"clubAbbr":abbreviation,
            "opponentRating":rating,"rating":rating,
            "chemistry":int(opponent.get("chemistry",100) or 0),
            "stars":float(opponent.get("stars",0) or 0),
            "rotation":int(opponent.get("rotation",0) or 0),
            "slot":int(opponent.get("slot",0) or 0),
            "opponentTeamId":team_id,"opponentBadgeId":team_id,
            "badgeAssetId":team_id,"personaId":int(opponent.get(
                "personaId",0) or 0),"played":played,"isPlayed":played,
            "available":not played,"pointsWon":points,
            "score":user_score,"oppScore":opp_score,
            "result":str(opponent.get("result","") or "")}


def _sqbt_next_refresh(event):
    opponents=list(event.get("opponents",[]) or [])
    rotation=max([int(value.get("rotation",0) or 0)
                  for value in opponents] or [0])
    all_played=bool(opponents) and all(bool(value.get("played"))
                                      for value in opponents)
    if all_played and rotation<SQBT_MAX_ROTATIONS-1:
        # The view model schedules refresh only after currentTime has strictly
        # crossed this value. Publishing the same second could leave a fresh
        # four-opponent set behind the normal 15-minute countdown.
        return now_s()-1
    if rotation>=SQBT_MAX_ROTATIONS-1:
        return int(event["expires"])
    return min(int(event["expires"]),now_s()+900)


def _sqbt_refresh_wire(event):
    """Return exactly the members `FutSquadBattleRefreshServerResponse` reads.

    Allocated at `CardsDLL+0x24aa2f` (size 0x60, vtable `0x37b898`) and parsed
    at `+0x24aaf0`.  Its field switch decodes `sqbtOppSquads`,
    `nextSquadRefreshTimeStamp`, `nextLBRefreshTimeStamp`, `isPrizeAvailable`
    and `gameModeRestriction`.  This is the response that carries the four
    opponents: the hub DTO has no member for them, so the previous
    `sqbtOppSquadList`/`opponents` aliases were never read by anything.
    """
    claimable=STATE.claimable_sqbt_event()
    return {"sqbtOppSquads":[_sqbt_opponent_wire(value)
                              for value in event.get("opponents",[])],
            "nextSquadRefreshTimeStamp":_sqbt_next_refresh(event),
            "nextLBRefreshTimeStamp":int(event["expires"]),
            "isPrizeAvailable":claimable is not None,
            "gameModeRestriction":0}


def _sqbt_wire_hub(payload):
    """Strip the embedded opponent squads from a project-internal payload.

    The routes that are not retail DTOs still return the richer local shape,
    but never the four complete 23-player squads: a single hub response reached
    316,651 bytes live.  Retail delivers one squad through
    `/sqbt/user/opponentsquad/{id}`.
    """
    entries=[_sqbt_opponent_wire(opponent)
             for opponent in payload.get("opponents",[])]
    wire=dict(payload)
    wire["sqbtOppSquadList"]=entries
    wire["sqbtOppSquads"]=entries
    wire["opponents"]=entries
    return wire


def _native_settings_configs():
    return [
        {"type":"tradingEnabled","value":1},
        {"type":"storeEnabled","value":1},
        {"type":"cardPackStoreEnabled","value":1},
        {"type":"enablePlayerPicks","value":1},
        {"type":"enableGrantLoaner","value":1},
        {"type":"loanPlayerPurchaseFeatureEnable","value":1},
        {"type":"enableDynamicObjectives","value":1},
        {"type":"enableDailyDynamicObjectives","value":1},
        {"type":"enableWeeklyDynamicObjectives","value":1},
        {"type":"enableSquadBuildingSetsFeature","value":1},
        {"type":"conceptSquadsEnabled","value":0},
        {"type":"enableConceptSquads","value":0},
        {"type":"conceptSquadEnabled","value":0},
        {"type":"FUT_ENABLE_DYNAMIC_PORTRAIT_HDD_CACHING","value":1},
        # enableDraftMode is the retail Online Draft switch.  Keep the two
        # explicit Single Player switches enabled and reject mode/0 server-side.
        {"type":"enableDraftMode","value":0},
        {"type":"enableOfflineDraftMode","value":1},
        {"type":"enableSinglePlayerDraftMode","value":1},
        {"type":"draftEnabled","value":0},
        {"type":"singlePlayerDraftEnabled","value":1},
        {"type":"squadBattleEnabled","value":1},
        {"type":"squadBattlesEnabled","value":1},
        {"type":"enableSquadBattles","value":1},
        {"type":"squadBattleFeatureEnabled","value":1},
        {"type":"singlePlayerSquadBattleEnabled","value":1},
        {"type":"squadBattleRefreshEnabled","value":1},
        {"type":"squadBattleCouchPlayEnabled","value":1},
        {"type":"teamOfTheWeekEnabled","value":1},
        {"type":"totwEnabled","value":1},
        # These native OSDK capacity values are string-backed.  Supplying an
        # integer makes the PC client fall back to its default of one squad
        # before it ever sends POST /squad.
        {"type":"SQUAD_LIST_SIZE","value":str(MAX_SQUADS)},
        {"type":"TRADE_PILE_SIZE","value":"100"},
        {"type":"WATCH_LIST_SIZE","value":"50"},
        {"type":"squadSlots","value":MAX_SQUADS},
        {"type":"maxSquads","value":MAX_SQUADS},
        {"type":"maximumSquads","value":MAX_SQUADS},
        {"type":"transferListSize","value":100},
        {"type":"transferTargetListSize","value":50},
    ]


def _concept_squads_disabled_payload():
    return {"success":False,"errorCode":461,
            "reason":"conceptSquadsDisabled",
            "message":"Concept Squads are disabled in FUT Deba Local."}

def _native_club_users(include_totw_owner=False):
    """Return the native club users and optional public TOTW owner.

    The EA App client builds the Match Preview owner lookup from the standalone
    ``clubUser`` response. Publishing only the local persona meant it later
    loaded the sentinel-owned TOTW squad without ever resolving that owner's
    club record, leaving the entire AWAY panel undefined. The aggregated FUT
    bootstrap keeps its normal local-only contract; only TOTW navigation asks
    for the public owner alias as well.
    """
    users=[{
        "persona":STATE.persona_name(),
        "personaId":PERSONA_ID,
        "public":True,
    }]
    if include_totw_owner:
        challenge=_totw_challenge()
        squad=(challenge or {}).get("squad") or {}
        users.append({
            "persona":str(squad.get("squadName") or "Team of the Week"),
            "personaId":0x7fffffffffffffff,
            "public":True,
        })
    return {"user":users}


def _native_club_info_users(persona_ids=None):
    """Return the FutGetClubInfoServerResponse for requested users.

    CardsDLL's retail decoder at RVA 0x21c570 requires a root ``user`` array.
    The element parser at RVA 0x21c230 accepts the scalar members below and
    member ``squadList`` (0x39f).  Its nested decoder at RVA 0x2696e0 requires
    ``squad`` (0x38a) as an array of summaries.  Provider 0x758a builds the TOTW
    Challenge Select rows from that cache; omitting it leaves the list spinning
    and the active challenge without a name or numeric rating.
    """
    requested={int(value) for value in (persona_ids or ())
               if str(value).strip().lstrip("-").isdigit()}
    featured_persona=0x7fffffffffffffff
    if requested and not ({PERSONA_ID,featured_persona} & requested):
        return {"user":[]}
    totw_payload=_totw_payload()
    challenges=list(totw_payload.get("challenges",[]) or [])
    selected_id=int(totw_payload.get("selectedChallengeId",0) or 0)
    selected=next((row for row in challenges
                   if int(row.get("challengeId",0) or 0)==selected_id),None)
    if selected is None:
        return {"user":[]}
    featured_id=int(selected.get("squadId",0) or 0)
    featured=selected.get("squad") or {}

    def challenge_summary(challenge):
        squad=challenge.get("squad") or {}
        return {
            "chemistry":int(squad.get("chemistry",0) or 0),
            "formation":str(squad.get("formation") or "f442"),
            "id":int(challenge.get("squadId",0) or 0),
            "rating":int(squad.get("rating",0) or 0),
            "squadName":str(challenge.get("name") or "Team of the Week"),
            # The retail summary enum only knows REGULAR/DREAM/DRAFT.  These
            # are public opponents and must not become concept squads.
            "squadType":"REGULAR_SQUAD",
        }
    featured_summaries=[challenge_summary(row) for row in challenges]
    metadata=_cpu_match_metadata(
        featured_id,TOTW_PRESENTATION_SEED,
        team_id=TOTW_PRESENTATION_TEAM_ID)
    presentation={str(item.get("itemState","")):item
                  for item in metadata.get("actives",[])
                  if isinstance(item,dict)}

    def club_item_ref(state,category_id):
        """Compact GetClubInfo badge/kit reference decoded at RVA 0x21c0a0."""
        item=presentation[state]
        return {
            "categoryId":int(item.get("category",category_id) or category_id),
            "resourceId":int(item.get("resourceId",0) or 0),
            "teamId":int(item.get("teamid",metadata.get("teamId",0)) or 0),
            "year":int(item.get("resourceGameYear",2019) or 2019),
        }

    challenge_id=max(0,int(featured_id)-500000)
    featured_abbr=("TW%d"%challenge_id)[:3] if challenge_id else "TOT"
    featured_identity={
        "awaykit":club_item_ref("activeAwayKit",3),
        "badge":club_item_ref("activeBadge",1),
        "clubAbbr":featured_abbr,
        "clubName":str(featured.get("squadName") or "Team of the Week"),
        "established":"0",
        "homekit":club_item_ref("activeHomeKit",2),
        "numberOfMatchesPlayed":0,
        "squadList":{"squad":featured_summaries},
        "teamId":int(metadata.get("teamId",0) or 0),
        "won":0,
    }
    # The local identity is needed by the bootstrap request, but a challenge
    # row owned by it is reloaded as /user/1000019 and stalls before CreateMatch.
    # The successful retained session reloaded the selected row through the
    # public sentinel, which also matches the compact record's owner.
    local_user={**featured_identity,"personaId":PERSONA_ID,
                "squadList":{"squad":[]}}
    if (_OFFLINE_SELECT_CONTEXT.get("state") is STATE and
            _OFFLINE_SELECT_CONTEXT.get("mode")=="SQBT"):
        local_user.update(clubName=STATE.club_name(),
                          clubAbbr=STATE.club_abbr())
    featured_user={**featured_identity,"personaId":featured_persona}
    # The retail request names the local persona because it starts from the
    # user's selected challenge list.  Offline Select then stores the chosen
    # opponent as the public sentinel + squad 500006.  Publishing only the
    # local GetClubInfo row leaves that public owner without a team/presentation
    # record: Match Preview falls back to TEAMNAME_ABBR15_0 and the gameplay
    # loader dereferences a null team object.  Keep the requested local row and
    # include the exact public owner it references.
    users=[]
    if not requested or PERSONA_ID in requested:
        users.append(local_user)
    if (not requested or PERSONA_ID in requested or
            featured_persona in requested):
        users.append(featured_user)
    return {"user":users}


def _native_user_mass_info():
    """Aggregated response loaded by the console client when entering FUT."""
    squad = _native_squad_json(STATE.active_squad())
    featured_id, featured_squad = _totw_featured_squad()
    featured_squad = _totw_public_squad_dto(featured_id,featured_squad)
    account_squads=STATE.squads()
    summaries=[]
    for stored_squad in account_squads:
        native=_native_squad_json(stored_squad)
        summary = {k:native.get(k) for k in (
            "id","valid","personaId","formation","rating","chemistry","manager",
            "dreamSquad","changed","squadName","starRating","captain","kicktakers",
            "actives","newSquad","squadType","custom","tactics")}
        summary["players"] = []
        summaries.append(summary)
    record = STATE.record()
    draft_history=_draft_history_payload("SINGLE_PLAYER")
    selected_loan=int(STATE.get("onboarding_loan_resource_id",0) or 0)
    # Offline PC compatibility: the console tutorial pick stops before sending
    # loan/players. Grant the same reward type once (a real, untradeable,
    # seven-match card) and persist it so kit/badge onboarding can continue.
    if not selected_loan:
        automatic=_grant_onboarding_loan(158023)  # Lionel Messi base card
        if automatic:
            STATE.move_item(int(automatic["id"]),"club")
            selected_loan=158023
    active_cosmetics=_selected_onboarding_items()
    onboarding_entries=_onboarding_client_entries()
    credits=STATE.credits()
    fifa_points=int(STATE.get("fifa_points",0) or 0)
    purchased=(_pending_player_pick_items()+
               [_native_item(x) for x in STATE.items_in_pile("purchased")])
    unopened=len(STATE.reward_unopened_packs())
    return {
        "errors":{}, "settings":{"configs":_native_settings_configs()},
        # CardsDLL selects the active-squad cache from these two root members.
        # Without personaId the root squad is written into the featured cache
        # used by the Single Player TOTW tile.  hub.squad is the only decoded
        # member that sets IS_TOTW_VALID and intentionally carries the same
        # sentinel identity as the compact /clientdata/totw record.
        "personaId":PERSONA_ID,"squadType":"REGULAR_SQUAD",
        # The squad cap does not come from any of the settings above.  CardsDLL
        # keeps it in a field that is written in exactly one place: the
        # /userMassInfo decoder, and only after a type check on this member.
        # Without pileSizeClientData the check fails, the field keeps its
        # built-in default and the client answers FUT_MAX_NUM_SQUAD_REACHED
        # locally, which is why creating a squad produced no request at all.
        # The decoder stores three consecutive ints in this order, so the
        # entries are listed in that order too.
        "pileSizeClientData":{"entries":[
            {"key":2,"value":100},          # trade pile
            {"key":4,"value":50},           # watch list
            {"key":6,"value":MAX_SQUADS},   # squad list
        ]},
        "userInfo":{
            "personaId":PERSONA_ID,"personaName":STATE.persona_name(),
            "clubName":STATE.club_name(),"clubAbbr":STATE.club_abbr(),
            "won":record["wins"],"wins":record["wins"],
            "draw":record["draws"],"draws":record["draws"],
            "lost":record["losses"],"loss":record["losses"],
            "losses":record["losses"],
            "gamesWon":record["wins"],"gamesDrawn":record["draws"],
            "gamesDraw":record["draws"],"gamesLost":record["losses"],
            "gamesPlayed":record["wins"]+record["draws"]+record["losses"],
            "record":{"won":record["wins"],"wins":record["wins"],
                      "draw":record["draws"],"draws":record["draws"],
                      "lost":record["losses"],"loss":record["losses"],
                      "losses":record["losses"]},
            "credits":credits,"bidTokens":{},"currencies":[
                {"name":"COINS","funds":credits,"finalFunds":credits},
                {"name":"POINTS","funds":fifa_points,"finalFunds":fifa_points},
                {"name":"DRAFT_TOKEN","funds":STATE.draft_tokens(),
                 "finalFunds":STATE.draft_tokens()},
            ],"trophies":0,
            "actives":active_cosmetics,
            "established":str(STATE.get("established",now_s())),
            "divisionOffline":_offline_division(),"divisionOnline":10,
            "offlineDivision":_offline_division(),
            "currentDivision":_offline_division(),
            "squadList":{"squad":summaries,"activeSquadId":squad["id"],
                         "maxSquads":MAX_SQUADS,"maximumSquads":MAX_SQUADS,
                         "squadSlots":MAX_SQUADS,"squadListSize":MAX_SQUADS},
            "maxSquads":MAX_SQUADS,"maximumSquads":MAX_SQUADS,
            "squadSlots":MAX_SQUADS,"squadLimit":MAX_SQUADS,
            "squadListSize":MAX_SQUADS,
            "unopenedPacks":{"preOrderPacks":unopened,"recoveredPacks":0},
            "purchased":bool(purchased),"unassignedPileSize":len(purchased),
            "reliability":{"reliability":100,"startedMatches":0,
                           "finishedMatches":0,"matchUnfinishedTime":0},
            "feature":{"trade":2,"rivals":0},
            "seasonTicket":False,"accountCreatedPlatformName":"PC",
            "fifaPointsFromLastYear":0,"fifaPointsTransferredStatus":0,
            "sessionCoinsBankBalance":credits,
            **draft_history,
        },
        "purchasedItems":{"itemData":purchased,
                          "duplicateItemIdList":_duplicate_item_links(purchased)},
        # The client treats the first entry as the definition/resource ID of
        # the granted loan; persistence prevents reopening this step.
        "loanPlayerClientData":{"entries":
            ([{"key":0,"value":selected_loan}] if selected_loan else [])},
        "squad":squad,"hub":{"squad":featured_squad},
        "clubUser":_native_club_users(),
        "activeMessages":{"activeMessage":[]},
        # Zero means onboarding is incomplete: FIFA must continue with loan,
        # kit and badge selection after displaying the starter squad.
        "onboardingClientData":{"entries":onboarding_entries},
        "isHighTierReturningUser":False,
        # This flag refers to a redeemed pick whose options await a choice,
        # not to unopened Pick Items still sitting in the unassigned pile.
        "isPlayerPicksTemporaryStorageNotEmpty":bool(STATE.active_player_pick()),
    }

def _native_loan_candidates():
    rows=[]
    for index,rid in enumerate(LOAN_PLAYER_CANDIDATES):
        item=native_player_fields(rid,{"untradeable":True,"loan":True,
                                      "loans":7,"contract":7,"contracts":7,
                                      "pile":"purchased"})
        item["id"] = item["itemId"] = 900000000000 + index
        # Native contract: GET loan/players ->
        # {"loans":[{"itemData": <player>}, ...]}. Returning <player>
        # directly makes the tutorial parser fail before the grant.
        rows.append({"itemData":_native_player_item(item)})
    return rows

def _find_state_item(item_id):
    try: wanted=int(item_id)
    except (TypeError,ValueError): return None
    for item in STATE.items_in_pile():
        if int(item.get("id",0) or 0)==wanted:
            return item
    return None

def _grant_onboarding_loan(resource_id):
    """Grant one persistent copy of the selected loan item (idempotent)."""
    rid=int(resource_id)
    if rid not in LOAN_PLAYER_CANDIDATES:
        return None
    existing_id=int(STATE.get("onboarding_loan_item_id",0) or 0)
    existing=_find_state_item(existing_id) if existing_id else None
    if existing is not None:
        return _native_player_item(existing)
    item=STATE.add_item(rid,pile="purchased",extra={
        "untradeable":True,"loan":True,"loans":7,
        "contract":7,"contracts":7,
    })
    STATE.set("onboarding_loan_resource_id",rid)
    STATE.set("onboarding_loan_item_id",int(item["id"]))
    return _native_player_item(item)


def _starter_objective(objective_id,group_id,name,description,counter,target=1):
    return {"id":int(objective_id),"groupId":int(group_id),"type":"STARTER",
            "name":str(name),"description":str(description),
            "counter":str(counter),"target":int(target),
            "reward":{"type":"playerPick","value":0,
                      "label":"1 of 3 78+ Player Pick",
                      "description":"Choose one of three untradeable Gold players rated 78 to 86.",
                      "minRating":78,"maxRating":86,"optionCount":3,
                      "quality":"GOLD","specialChance":0.125,
                      "untradeable":True}}


def _objective_player_pick():
    """Return the common visible, claimable objective reward contract."""
    return {"type":"playerPick","value":0,
            "label":"1 of 3 78+ Player Pick",
            "description":"Choose one of three untradeable Gold players rated 78 to 86.",
            "minRating":78,"maxRating":86,"optionCount":3,
            "quality":"GOLD","specialChance":0.125,
            "untradeable":True}


def _chapter_objective(objective_id,name,description,counter,target,reward,
                       sequence):
    """Build one permanent objective in the first local progression chapter."""
    return {"id":int(objective_id),"groupId":19600,"type":"STARTER",
            "name":str(name),"description":str(description),
            "counter":str(counter),"target":int(target),
            "reward":dict(reward),"chapterId":1,
            "chapterSequence":int(sequence)}


# FUT 19 shipped 21 Starter Objectives in five named groups. Three local
# compatibility objectives cover flows exposed by this restoration. A sixth,
# explicitly local group contains the first permanent progression chapter for
# both Normal and RTG accounts.
OBJECTIVE_DEFS = (
    _starter_objective(19101,19100,"Create Your Squad",
        "Create your first FUT squad.","objective_squads_created"),
    _starter_objective(19102,19100,"Play a Match",
        "Complete a FUT match.","objective_matches_played"),
    _starter_objective(19103,19100,"Buy a Player",
        "Buy a player for your club.","objective_players_bought"),
    _starter_objective(19104,19100,"New Player Debut",
        "Play a match with a newly acquired player.","objective_player_debuts"),

    _starter_objective(19201,19200,"Name Your Club",
        "Complete the initial club setup.","onboarding_stage",4),
    _starter_objective(19202,19200,"In Formation",
        "Save a change to your squad formation.","objective_squad_saves"),
    _starter_objective(19203,19200,"Getting Fit",
        "Apply a fitness item.","objective_fitness_applied"),
    _starter_objective(19204,19200,"Play a Single Player Match",
        "Complete an offline FUT match.","objective_matches_played"),
    _starter_objective(19205,19200,"Take Your Positions",
        "Apply a position modifier.","objective_position_changes"),

    _starter_objective(19301,19300,"Improving Chemistry",
        "Improve your squad chemistry.","objective_chemistry_actions"),
    _starter_objective(19302,19300,"Contract Extension",
        "Apply a contract item.","objective_contracts_applied"),
    _starter_objective(19303,19300,"TOTW Challenge",
        "Play the Team of the Week challenge.","objective_totw_matches"),
    _starter_objective(19304,19300,"Buy a Contract",
        "Buy a contract item.","objective_contracts_bought"),

    _starter_objective(19401,19400,"Green Link",
        "Create a green chemistry link.","objective_green_links"),
    _starter_objective(19402,19400,"New Signings",
        "Add a newly purchased player to your club.","objective_players_bought"),
    _starter_objective(19403,19400,"FUT Seasons",
        "Play a FUT Seasons match.","objective_seasons_matches"),
    _starter_objective(19404,19400,"Single Player Seasons Win",
        "Win a Single Player Seasons match.","objective_seasons_wins"),

    _starter_objective(19501,19500,"List a Player on the Transfer Market",
        "List a player on the Transfer Market.","objective_transfer_listings"),
    _starter_objective(19502,19500,"Building Chemistry",
        "Improve squad chemistry.","objective_chemistry_actions"),
    _starter_objective(19503,19500,"First Exchange",
        "Complete a Squad Building Challenge.","objective_sbc_completed"),
    _starter_objective(19504,19500,"Squad Battles",
        "Play a Squad Battles match.","objective_squad_battles_matches"),
    _starter_objective(19505,19500,"Open Your First Pack",
        "Open a pack in the local Store.","objective_packs_opened"),
    _starter_objective(19506,19500,"Manage Your Squad",
        "Save a change to the active squad.","objective_squad_saves"),
    _starter_objective(19507,19500,"Organize Your Club",
        "Send a new item to My Club.","objective_items_to_club"),

    _chapter_objective(19601,"First Match",
        "Complete your first FUT match.","career_matches_played",1,
        {"type":"pack","value":100,"label":"Bronze Pack"},1),
    _chapter_objective(19602,"First Win",
        "Win your first FUT match.","career_matches_won",1,
        {"type":"pack","value":101,"label":"Premium Bronze Pack"},2),
    _chapter_objective(19603,"First SBC",
        "Complete your first Squad Building Challenge.",
        "career_sbc_completed",1,
        {"type":"pack","value":200,"label":"Silver Pack"},3),
    _chapter_objective(19604,"First Market Sale",
        "Sell your first player on the Transfer Market.",
        "career_market_sales",1,
        {"type":"pack","value":200,"label":"Silver Pack"},4),
    _chapter_objective(19605,"Bronze Milestone",
        "Reach a high-water mark of 30 owned Bronze players.",
        "career_players_bronze",30,
        {"type":"pack","value":101,"label":"Premium Bronze Pack"},5),
    _chapter_objective(19606,"Silver Milestone",
        "Reach a high-water mark of 30 owned Silver players.",
        "career_players_silver",30,
        {"type":"pack","value":201,"label":"Premium Silver Pack"},6),
    _chapter_objective(19607,"Gold Milestone",
        "Reach a high-water mark of 20 owned Gold players.",
        "career_players_gold",20,
        {"type":"pack","value":301,"label":"Premium Gold Pack"},7),
    _chapter_objective(19608,"Squad Battles Milestone",
        "Complete five Squad Battles matches.","career_sqbt_matches",5,
        {"type":"pack","value":304,"label":"Gold Players Pack"},8),
    _chapter_objective(19609,"Draft Milestone",
        "Complete four FUT Draft matches.","career_draft_matches",4,
        {"type":"pack","value":305,
         "label":"Premium Gold Players Pack"},9),

    {"id":19001,"type":"DAILY","name":"Open a Pack",
     "description":"Open a pack in the local Store.","counter":"objective_packs_opened",
     "target":1,"reward":_objective_player_pick()},
    {"id":19004,"type":"DAILY","name":"Squad Management",
     "description":"Save a change to the active squad.","counter":"objective_squad_saves",
     "target":1,"reward":_objective_player_pick()},
    {"id":19005,"type":"DAILY","name":"Play Offline",
     "description":"Complete an offline FUT match.","counter":"objective_matches_played",
     "target":1,"reward":_objective_player_pick()},
    {"id":19002,"type":"WEEKLY","name":"Organize Your Club",
     "description":"Send three new items to My Club.","counter":"objective_items_to_club",
     "target":3,"reward":_objective_player_pick()},
    {"id":19003,"type":"WEEKLY","name":"First Offline Match",
     "description":"Complete an offline FUT match.","counter":"objective_matches_played",
     "target":1,"reward":_objective_player_pick()},
    {"id":19006,"type":"WEEKLY","name":"Open Three Packs",
     "description":"Open three packs in the local Store.","counter":"objective_packs_opened",
     "target":3,"reward":_objective_player_pick()},
)

def _objective_window(kind):
    current=now_s()
    utc=datetime.datetime.fromtimestamp(current,datetime.timezone.utc)
    if kind == "DAILY":
        start=utc.replace(hour=0,minute=0,second=0,microsecond=0)
        end=start+datetime.timedelta(days=1)
    elif kind == "WEEKLY":
        start=(utc-datetime.timedelta(days=utc.weekday())).replace(
            hour=0,minute=0,second=0,microsecond=0)
        end=start+datetime.timedelta(days=7)
    else:
        start=datetime.datetime.fromtimestamp(
            int(STATE.get("established",current) or current),datetime.timezone.utc)
        end=start+datetime.timedelta(days=3650)
    return int(start.timestamp()),int(end.timestamp())

OBJECTIVE_CLAIM_KEY="objective_claims_v2"
OBJECTIVE_WINDOW_KEY="objective_windows_v1"

def _objective_claim_window(defn):
    """Return the window start this objective was claimed in, or None.

    Claims used to be a permanent id list, so a DAILY objective stayed
    REDEEMED for ever and never returned the next day. A claim now belongs to
    the window it was made in. A legacy list is migrated into each
    objective's current window, which keeps starter objectives claimed and
    lets DAILY and WEEKLY rows come back at the next rollover.
    """
    claims=STATE.get(OBJECTIVE_CLAIM_KEY,None)
    if not isinstance(claims,dict):
        claims={}
    legacy=[int(x) for x in (STATE.get("objective_claims",[]) or [])]
    if legacy:
        for value in legacy:
            if str(value) in claims:
                continue
            kind=next((row["type"] for row in OBJECTIVE_DEFS
                       if int(row["id"])==int(value)),"STARTER")
            claims[str(value)]=int(_objective_window(kind)[0])
        # Consume the old list so a repeating claim migrates once and can then
        # expire with its window.
        STATE.set("objective_claims",None)
        STATE.set(OBJECTIVE_CLAIM_KEY,claims)
    elif STATE.get(OBJECTIVE_CLAIM_KEY,None) is None:
        STATE.set(OBJECTIVE_CLAIM_KEY,claims)
    claimed=claims.get(str(int(defn["id"])))
    return None if claimed is None else int(claimed)

def _objective_progress_in_window(defn,start):
    """Return only the progress made inside the current window.

    The counters are cumulative career totals, so a DAILY objective opened
    yesterday started today already complete. Each repeating objective keeps
    the counter value it had when its window opened and reports the delta.
    """
    raw=int(STATE.objective_progress(defn["counter"]))
    if str(defn.get("type","")) not in ("DAILY","WEEKLY"):
        return raw
    windows=STATE.get(OBJECTIVE_WINDOW_KEY,{})
    if not isinstance(windows,dict):
        windows={}
    key=str(int(defn["id"]))
    record=windows.get(key)
    if not isinstance(record,dict):
        # First sight of this objective: everything counted so far belongs to
        # the window that is open, so a pack opened before the screen was
        # ever visited still completes today's objective.
        windows[key]={"start":int(start),"base":0}
        STATE.set(OBJECTIVE_WINDOW_KEY,windows)
        return raw
    if int(record.get("start",0) or 0)!=int(start):
        windows[key]={"start":int(start),"base":raw}
        STATE.set(OBJECTIVE_WINDOW_KEY,windows)
        return 0
    return max(0,raw-int(record.get("base",0) or 0))

def _native_award(reward):
    # Use the same verified Award projection as SBC and Squad Battles so the
    # Player Pick icon and its untradeable metadata reach the native panel.
    native=native_reward_dto({**reward,"count":1})
    # Dynamic Objectives and the reward-details drawer use different Award
    # readers in this retail build. Publish both verified spellings so a Pick
    # Item renders as a Player Pick instead of a zero-rated Gold placeholder.
    row={**native,
         "type":str(reward.get("type",native.get("awardType","")) or ""),
         "value":int(reward.get("value",native.get("awardValue",0)) or 0),
         "count":int(native.get("awardCount",1) or 1),
         "isUntradeable":bool(native.get("untradeable",False))}
    if str(reward.get("type","")).lower() in {"playerpick","player_pick"}:
        row["playerPickDefinitionId"]=PLAYER_PICK_DEFINITION_ID
        row["optionCount"]=int(reward.get("optionCount",3) or 3)
    return row

def _objective_row(defn):
    start_time,expiry_time=_objective_window(defn["type"])
    progress=_objective_progress_in_window(defn,start_time)
    claimed_at=_objective_claim_window(defn)
    state="REDEEMED" if claimed_at==start_time else (
        "COMPLETED" if progress >= defn["target"] else "IN_PROGRESS")
    group_id=int(defn.get("groupId",defn["id"]))
    row={"objectiveId":defn["id"],"groupId":group_id,
            "objectiveGroupId":group_id,
            "name":defn["name"],"header":defn["name"],
            "description":defn["description"],"shortDescription":defn["description"],
            "imageBase":"","gameArea":"FUT","takeMeThereLink":"",
            "difficulty":1,"type":defn["type"],"requiresReset":False,
            "isWeb":True,"startTime":start_time,"lastUpdateTime":now_s(),
            "expiryTime":expiry_time,"slot":defn["id"]-19000,
            "state":state,"currentProgress":min(progress,defn["target"]),
            "multiplier":defn["target"],"awards":[_native_award(defn["reward"])]}
    if defn.get("chapterId") is not None:
        row.update({"chapterId":int(defn["chapterId"]),
                    "chapterSequence":int(defn.get("chapterSequence",0) or 0)})
    return row


def _objective_updates_for_counter(counter,compact=False):
    """Return the native objective deltas affected by one committed action."""
    key=str(counter or "")
    rows=[_objective_row(defn) for defn in OBJECTIVE_DEFS
          if str(defn.get("counter","")) == key]
    if not compact:
        return rows
    # /sbs/sets is already close to CardsDLL's proven safe catalogue size.
    # The global Objectives cache needs only identity, progress and state to
    # invalidate the matching row after a submit; full awards stay on the
    # canonical Dynamic Objectives route and on pack responses.
    keys=("objectiveId","groupId","objectiveGroupId","state",
          "currentProgress","multiplier","lastUpdateTime")
    return [{member:row[member] for member in keys if member in row}
            for row in rows]

def _native_dynamic_objectives():
    rows=[_objective_row(x) for x in OBJECTIVE_DEFS]
    group_meta={
        19100:("Welcome to FUT","Create your squad and begin your FUT journey."),
        19200:("The Club","Learn the essentials of managing your club."),
        19300:("FUT Basics","Learn contracts, chemistry and FUT challenges."),
        19400:("Chemistry is King","Build links and take your squad into Seasons."),
        19500:("Ways to Play","Explore FUT modes and local restoration features."),
        19600:("Local Progression: Chapter 1",
               "Build your club through matches, SBCs, the market, Draft and Squad Battles."),
    }
    starter_groups=[]
    for group_id,(name,description) in group_meta.items():
        starter=[x for x in rows
                 if x["type"]=="STARTER" and x["groupId"]==group_id]
        starter_done=sum(x["state"] in ("COMPLETED","REDEEMED") for x in starter)
        starter_redeemed=sum(x["state"] == "REDEEMED" for x in starter)
        starter_groups.append({
            "id":group_id,"groupId":group_id,"objectiveGroupId":group_id,
            "name":name,"groupName":name,"header":name,"description":description,
            # A group-level award made CardsDLL render a permanently claimable
            # icon. Child objectives own the rewards, so groups remain empty.
            # Completion belongs to the rewarded child rows.  Marking the
            # non-claimable parent REDEEMED while completed children were
            # still waiting to be claimed made the group look already paid.
            "state":"REDEEMED" if starter_redeemed == len(starter)
                    else "IN_PROGRESS",
            "currentProgress":starter_done,"multiplier":len(starter),
            "objectives":starter,"objectivesForCurrentUser":starter,
            "allObjectivesForCurrentGameSpaceId":starter,
            "awards":[],"isClaimable":False,"canClaim":False,
            "startTime":min(x["startTime"] for x in starter),
            "expiryTime":max(x["expiryTime"] for x in starter),
        })
    return {"coinsAutoClaimed":0,"itemsAutoClaimed":0,"packsAutoClaimed":0,
            "dailyRewardsAutoClaimed":False,"weeklyRewardsAutoClaimed":False,
            # The starter set is published under "groups".  CardsDLL keeps a
            # sorted table of every JSON member name it understands, and
            # "starterObjectives" is not in it, while "groups", "groupId" and
            # "groupName" are: the whole block was being dropped in silence,
            # which is why only the three daily and three weekly objectives
            # reached the screen.  The old key is kept alongside because it
            # costs nothing and older callers still read it.
            "groups":starter_groups,
            "starterObjectives":starter_groups,
            # The groups arrive (the screen shows five dots under CHANGE
            # GROUPS) but render empty with an undefined title, while the
            # daily list works with the very same objective fields.  The UI
            # tracks ACTIVE_GROUP_ID and VISIBLE_GROUP_ID, so it picks a group
            # and then looks its objectives up in a flat list rather than
            # inside the group.  Every row already carries its groupId.
            "objectives":[x for x in rows if x["type"]=="STARTER"],
            "objectivesForCurrentUser":[x for x in rows if x["type"]=="STARTER"],
            "dailyObjectives":[x for x in rows if x["type"]=="DAILY"],
            "weeklyObjectives":[x for x in rows if x["type"]=="WEEKLY"]}

def _claim_objective(objective_id):
    definition=next((x for x in OBJECTIVE_DEFS if x["id"]==int(objective_id)),None)
    if not definition:
        return None
    row=_objective_row(definition)
    if row["state"] not in ("COMPLETED","REDEEMED"):
        return {"objectiveId":definition["id"],"awards":[]}
    if row["state"] == "REDEEMED":
        return {"objectiveId":definition["id"],"awards":[]}
    reward_spec=definition["reward"]
    reward=_native_award(reward_spec)
    reward_kind=str(reward_spec.get("type","") or "")
    if reward_kind == "coin":
        STATE.add_credits(int(reward_spec.get("value",0) or 0))
    elif reward_kind == "pack":
        STATE.add_unopened_pack(int(reward_spec.get("value",0) or 0),
                                {"source":"objective"})
    elif reward_kind == "playerPick":
        pick_spec=generate_player_pick_options(
            min_rating=int(reward_spec.get("minRating",78)),
            max_rating=int(reward_spec.get("maxRating",86)),
            option_count=int(reward_spec.get("optionCount",3)),
            seed=(int(definition["id"])*1000003+int(STATE.get("established",0) or 0)),
            quality=definition["reward"].get("quality"),
            special_chance=float(reward_spec.get("specialChance",0.0) or 0.0))
        pick_spec.update({
            "name":str(definition["reward"].get(
                "label","1 of 3 78+ Player Pick")),
            "description":str(definition["reward"].get(
                "description","Choose one of three untradeable Gold players rated 78 or higher.")),
        })
        pick=STATE.grant_player_pick(
            "objective:%d" % int(definition["id"]),pick_spec)
    start_time,_expiry=_objective_window(definition["type"])
    claims=STATE.get(OBJECTIVE_CLAIM_KEY,None)
    if not isinstance(claims,dict):
        _objective_claim_window(definition)
        claims=STATE.get(OBJECTIVE_CLAIM_KEY,{}) or {}
    claims[str(int(definition["id"]))]=int(start_time)
    STATE.set(OBJECTIVE_CLAIM_KEY,claims)
    response={"objectiveId":definition["id"],"awards":[reward],**_credits_payload()}
    if reward_kind == "playerPick": response["playerPicks"]=[pick]
    return response


LOCAL_SBC_SET_ID=1
LOCAL_SBC_CHALLENGE_ID=191001

_LOCAL_SBC_ATTEMPT_CONTRACT={
    "mode":"saved_squad_attempt","retryScope":"operation_key"}


def _local_sbc_challenge(challenge_id,set_id,name,description,image_id,
                         rewards,requirements,repeatable=False,
                         formation="f442"):
    count_rule=next((rule for rule in requirements
                     if str(rule.get("type","")).lower()=="player_count"),{})
    player_count=max(1,min(11,int(count_rule.get("value",11) or 11)))
    row={
        "challengeId":int(challenge_id),"setId":int(set_id),"name":str(name),
        "description":str(description),
        "verificationStatus":"local_implemented","enabled":True,
        "repeatable":bool(repeatable),"formation":str(formation),
        "slotMask":["OPEN"]*player_count+["BRICK"]*(11-player_count),
        "challengeImageId":int(image_id),"startTime":0,"endTime":0,
        "notExpirable":True,"rewards":list(rewards),
        "requirements":list(requirements),
    }
    if repeatable:
        row["attemptContract"]=dict(_LOCAL_SBC_ATTEMPT_CONTRACT)
    return row


def _local_sbc_set(set_id,name,description,category,image_id,challenges,
                   rewards=None,repeatable=False,featured=False):
    row={
        "setId":int(set_id),"name":str(name),"description":str(description),
        "category":str(category),"verificationStatus":"local_implemented",
        "enabled":True,"assetId":int(image_id),
        "tileImage":"SBS_SET_%d.png" % int(image_id),
        "repeatable":bool(repeatable),"startTime":0,"endTime":0,
        "notExpirable":True,"isFeatured":bool(featured),
        "rewards":list(rewards or []),"challenges":list(challenges),
    }
    if repeatable:
        row["attemptContract"]=dict(_LOCAL_SBC_ATTEMPT_CONTRACT)
    return row


def _sbc_player_count(value=11):
    return {"type":"player_count","nativeType":"PLAYER_COUNT",
            "operator":"exact","value":int(value),
            "description":"Number of players in the Squad: %d" % int(value)}


def _sbc_rating(value):
    return {"type":"squad_rating","nativeType":"TEAM_RATING",
            "operator":"min","value":int(value),
            "description":"Squad Rating: Min %d" % int(value)}


def _sbc_chemistry(value):
    return {"type":"squad_chemistry","nativeType":"TEAM_CHEMISTRY",
            "operator":"min","value":int(value),
            "description":"Squad Total Chemistry Points: Min %d" % int(value)}


def _sbc_specific(subject,identity,count=1,label="",operator="min"):
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":"specific_%s" % str(subject),
            "operator":str(operator).lower(),
            "identity":int(identity),"value":int(count),
            "description":"# of players from %s: %s %d" % (
                str(label or subject).strip(),comparison,int(count))}


def _sbc_specific_any(subject,identities,count=1,label="",operator="min"):
    """Require players matching any identity from one sourced historical_reference row.

    FIFA 19 SBCs occasionally combine two nations in a single requirement
    (for example "Gabon + Cameroon").  Keeping that as one rule is important:
    two independent rules would incorrectly require at least one player from
    each nation instead of one player from either nation.
    """
    cleaned=[]
    for identity in identities:
        value=int(identity)
        if value > 0 and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise ValueError("combined SBC requirement needs an identity")
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":"specific_%s" % str(subject),
            "operator":str(operator).lower(),"identities":cleaned,
            "value":int(count),
            "description":"# of players from %s: %s %d" % (
                str(label or subject).strip(),comparison,int(count))}


def _sbc_unique(subject,count,operator="min",label=""):
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":"unique_%s" % str(subject),
            "operator":str(operator).lower(),"value":int(count),
            "description":"%s: %s %d" % (
                str(label or (str(subject).title()+"s")),comparison,int(count))}


def _sbc_same(subject,count,operator="max",label=""):
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":"same_%s" % str(subject),
            "operator":str(operator).lower(),"value":int(count),
            "description":"%s: %s %d" % (
                str(label or ("Same %s Count" % str(subject).title())),
                comparison,int(count))}


def _sbc_quality(quality="GOLD"):
    return {"type":"player_quality","nativeType":"PLAYER_LEVEL",
            "operator":"exact","quality":str(quality).upper(),
            "description":"Player Level: Exactly %s" % str(quality).title()}


def _sbc_quality_count(quality,count,operator="min"):
    kind={"min":"minimum_quality_count",
          "max":"maximum_quality_count"}.get(
              str(operator).lower(),"quality_count")
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":kind,"operator":str(operator).lower(),
            "quality":str(quality).upper(),"value":int(count),
            "description":"%s Players: %s %d" %
                          (str(quality).title(),comparison,int(count))}


def _sbc_rare(count,operator="min"):
    comparison={"min":"Min","max":"Max","exact":"Exactly"}.get(
        str(operator).lower(),str(operator).title())
    return {"type":"rare_count","operator":str(operator).lower(),
            "value":int(count),
            "description":"Rare Players: %s %d" % (comparison,int(count))}


def _sbc_totw(count=1):
    return {"type":"totw_count","operator":"min","value":int(count),
            "description":"IF Players: Min %d" % int(count)}


def _sbc_swap_deals(count=1,phase=4):
    return {"type":"swap_deals_count","operator":"min",
            "value":int(count),"cardType":"SWAP DEALS",
            "description":"SWAP DEALS %d Players: Min %d" %
                          (int(phase),int(count))}

LOCAL_SBC_CHALLENGE=_local_sbc_challenge(
    LOCAL_SBC_CHALLENGE_ID,LOCAL_SBC_SET_ID,"Starter Exchange",
    "Exchange a full squad for a Premium Gold Players Pack.",1,
    [{"type":"pack","value":305,"count":1}],
    [_sbc_player_count(),_sbc_rating(65)])
LOCAL_SBC_SET=_local_sbc_set(
    LOCAL_SBC_SET_ID,"Starter Exchange",
    "Complete a local starter-squad exchange with a persistent reward.",
    "BASIC",1,[LOCAL_SBC_CHALLENGE])

LOCAL_GOLD_SBC_CHALLENGE=_local_sbc_challenge(
    191002,2,"Gold Upgrade",
    "Exchange eleven Gold players for two Rare Gold Players.",1001,
    [],
    [_sbc_player_count(),
     {"type":"player_quality","nativeType":"PLAYER_LEVEL",
      "operator":"exact","quality":"GOLD",
      "description":"Player Level: Exactly Gold"},
     _sbc_chemistry(40)],repeatable=True,formation="f41212")
LOCAL_GOLD_SBC_SET=_local_sbc_set(
    2,"Gold Upgrade",
    "Repeatable Gold upgrade with two untradeable Rare Gold Players.",
    "UPGRADES",208,[LOCAL_GOLD_SBC_CHALLENGE],
    rewards=[{"type":"pack","value":512,"count":1,"untradeable":True}],
    repeatable=True)
LOCAL_GOLD_SBC_CHALLENGE.update({
    "verificationStatus":"historical_verified","sourceChallengeId":20,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})
LOCAL_GOLD_SBC_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":8,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})

LOCAL_ELITE_SBC_CHALLENGE=_local_sbc_challenge(
    191003,3,"Elite Exchange",
    "Exchange an 82-rated squad for a Mega Pack.",3,
    [{"type":"pack","value":403,"count":1}],
    [_sbc_player_count(),_sbc_rating(82)])
LOCAL_ELITE_SBC_SET=_local_sbc_set(
    3,"Elite Exchange",
    "A local high-rated exchange with an idempotent pack reward.",
    "ADVANCED",3,[LOCAL_ELITE_SBC_CHALLENGE])

LOCAL_BRONZE_UPGRADE_CHALLENGE=_local_sbc_challenge(
    191004,4,"Bronze Upgrade",
    "Exchange eleven Bronze players for two Silver Players.",1001,
    [],
    [_sbc_player_count(),
     {"type":"player_quality","operator":"exact","quality":"BRONZE",
      "description":"Player Level: Exactly Bronze"},
     _sbc_chemistry(40)],repeatable=True,formation="f41212")
LOCAL_BRONZE_UPGRADE_SET=_local_sbc_set(
    4,"Bronze Upgrade","Repeatable Bronze-to-Silver player upgrade.",
    "UPGRADES",206,[LOCAL_BRONZE_UPGRADE_CHALLENGE],
    rewards=[{"type":"pack","value":509,"count":1,"untradeable":True}],
    repeatable=True)
LOCAL_BRONZE_UPGRADE_CHALLENGE.update({
    "verificationStatus":"historical_verified","sourceChallengeId":18,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})
LOCAL_BRONZE_UPGRADE_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":6,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})

LOCAL_SILVER_UPGRADE_CHALLENGE=_local_sbc_challenge(
    191005,5,"Silver Upgrade",
    "Exchange eleven Silver players for three Common Gold Players.",1001,
    [],
    [_sbc_player_count(),
     {"type":"player_quality","operator":"exact","quality":"SILVER",
      "description":"Player Level: Exactly Silver"},
     _sbc_chemistry(40)],repeatable=True,formation="f41212")
LOCAL_SILVER_UPGRADE_SET=_local_sbc_set(
    5,"Silver Upgrade","Repeatable Silver-to-Gold player upgrade.",
    "UPGRADES",207,[LOCAL_SILVER_UPGRADE_CHALLENGE],
    rewards=[{"type":"pack","value":510,"count":1,"untradeable":True}],
    repeatable=True)
LOCAL_SILVER_UPGRADE_CHALLENGE.update({
    "verificationStatus":"historical_verified","sourceChallengeId":19,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})
LOCAL_SILVER_UPGRADE_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":7,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "verifiedAt":"2026-08-30"}})


def _player_sbc_set(set_id,name,description,image_id,resource_id,
                    source_set_id,source_url,player_url,player_artwork_url,
                    segments):
    challenges=[]
    for offset,segment in enumerate(segments,1):
        challenge=_local_sbc_challenge(
            set_id*1000+offset,set_id,segment["name"],segment["description"],
            segment["imageId"],segment.get("rewards",[]),
            segment["requirements"],formation=segment["formation"])
        challenge.update({
            "verificationStatus":"historical_verified",
            "sourceChallengeId":int(segment["sourceChallengeId"]),
            "sourceProvenance":{
                "provider":"historical_reference","url":segment["url"],
                "artworkUrl":segment["artworkUrl"],
                "verifiedAt":"2026-08-30",
                "evidence":["Challenge Requirements","Formation","Rewards"],
            },
        })
        challenges.append(challenge)
    row=_local_sbc_set(
        set_id,name,description,"PLAYERS",image_id,challenges,
        rewards=[{"type":"player","resourceId":int(resource_id),
                  "value":int(resource_id),"count":1,"untradeable":True}],
        featured=True)
    row.update({
        "verificationStatus":"historical_verified",
        "sourceSetId":int(source_set_id),
        "sourceProvenance":{
            "provider":"historical_reference","url":str(source_url),
            "playerUrl":str(player_url),
            "playerArtworkUrl":str(player_artwork_url),
            "verifiedAt":"2026-08-30",
        },
    })
    return row


def _sbc_pack(pack_id,label):
    return {"type":"pack","value":int(pack_id),"packId":int(pack_id),
            "label":str(label),"count":1}


def _sbc_kit(team_id,label):
    # historical_reference's historical pages expose the reward only as KIT, without a
    # recoverable object/resource id.  A related club home kit is the smallest
    # executable local mapping which preserves that second reward.
    return {"type":"club_item","teamId":int(team_id),"category":2,
            "label":str(label),"count":1,"untradeable":True}


LOCAL_LUCAS_POTM_SET=_player_sbc_set(
    20,"Lucas - Premier League POTM",
    "Earn the 86-rated Premier League Player of the Month Lucas.",201,50532597,
    13,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Premier League",
      "description":"Exchange a squad of Premier League players",
      "sourceChallengeId":36,
      "url":"private-release-record",
      "imageId":3014001,
      "artworkUrl":"private-release-record",
      "formation":"f4231",
      "rewards":[_sbc_pack(303,"Jumbo Premium Gold Pack")],
      "requirements":[
          _sbc_specific("league",13,11,"Premier League",operator="exact"),
          _sbc_unique("nation",3,operator="max",label="Nationalities"),
          _sbc_rare(3),_sbc_rating(80),_sbc_chemistry(90),
          _sbc_player_count()]},
     {"name":"Tottenham Hotspur",
      "description":"Exchange a squad of players featuring Tottenham Hotspur",
      "sourceChallengeId":37,
      "url":"private-release-record",
      "imageId":3014002,
      "artworkUrl":"private-release-record",
      "formation":"f4141","rewards":[_sbc_pack(400,"Rare Gold Pack")],
      "requirements":[_sbc_specific("club",18,2,"Tottenham Hotspur"),
          _sbc_specific("league",13,4,"Premier League"),_sbc_rating(81),
          _sbc_chemistry(90),_sbc_player_count()]},
     {"name":"Brazil","description":"Exchange a squad of players featuring Brazil",
      "sourceChallengeId":38,
      "url":"private-release-record",
      "imageId":3014003,
      "artworkUrl":"private-release-record",
      "formation":"f442","rewards":[_sbc_pack(304,"Gold Players Pack")],
      "requirements":[_sbc_specific("nation",54,3,"Brazil"),
          _sbc_same("league",4),_sbc_rating(82),_sbc_chemistry(85),
          _sbc_player_count()]},
     {"name":"Player of the Month",
      "description":"Exchange a squad built around the first POTM winner, Lucas Moura",
      "sourceChallengeId":39,
      "url":"private-release-record",
      "imageId":3014004,
      "artworkUrl":"private-release-record",
      "formation":"f4231",
      "rewards":[_sbc_pack(301,"Premium Gold Pack"),
                 _sbc_kit(18,"Lucas POTM Kit")],
      "requirements":[_sbc_same("league",3),_sbc_same("nation",4),
          _sbc_quality("GOLD"),_sbc_chemistry(90),_sbc_player_count(10)]}])

LOCAL_ZLATAN_FLASHBACK_SET=_player_sbc_set(
    21,"Zlatan Ibrahimovic - Flashback",
    "Earn the 92-rated Flashback Zlatan Ibrahimovic.",202,50372884,
    312,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Blågult","description":"Exchange a Squad featuring players from Sweden",
      "sourceChallengeId":784,
      "url":"private-release-record",
      "imageId":3060031,
      "artworkUrl":"private-release-record",
      "formation":"f41212","rewards":[_sbc_pack(401,"Rare Players Pack")],
      "requirements":[_sbc_specific("nation",46,1,"Sweden"),
          _sbc_same("league",4),_sbc_totw(2),_sbc_rating(84),
          _sbc_chemistry(80),_sbc_player_count()]},
     {"name":"Les Parisiens",
      "description":"Exchange a Squad featuring players from Paris Saint-Germain",
      "sourceChallengeId":785,
      "url":"private-release-record",
      "imageId":3046030,
      "artworkUrl":"private-release-record",
      "formation":"f433","rewards":[_sbc_pack(404,"Rare Mega Pack")],
      "requirements":[_sbc_specific("club",73,1,"Paris Saint-Germain"),
          _sbc_same("nation",4),_sbc_rating(85),_sbc_chemistry(85),
          _sbc_player_count()]}])

LOCAL_REUS_POTM_SET=_player_sbc_set(
    22,"Marco Reus - Bundesliga POTM",
    "Earn the 90-rated Bundesliga Player of the Month Marco Reus.",203,84074430,
    50,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Marco Reus",
      "description":"Exchange a squad built around the September Bundesliga POTM winner, Marco Reus",
      "sourceChallengeId":189,
      "url":"private-release-record",
      "imageId":3052001,
      "artworkUrl":"private-release-record",
      "formation":"f4141","rewards":[_sbc_pack(400,"Rare Gold Pack")],
      "requirements":[_sbc_same("league",3),_sbc_same("nation",4),
          _sbc_totw(),_sbc_rating(83),_sbc_chemistry(80),
          _sbc_player_count(10)]},
     {"name":"Bundesliga",
      "description":"Exchange a squad featuring Bundesliga players",
      "sourceChallengeId":190,
      "url":"private-release-record",
      "imageId":3052002,
      "artworkUrl":"private-release-record",
      "formation":"f442","rewards":[_sbc_pack(305,"Premium Gold Players Pack")],
      "requirements":[_sbc_specific("league",19,9,"Bundesliga"),
          _sbc_totw(),_sbc_rating(85),_sbc_chemistry(85),
          _sbc_player_count()]},
     {"name":"Borussia Dortmund",
      "description":"Exchange a squad of players featuring Reus's current club",
      "sourceChallengeId":191,
      "url":"private-release-record",
      "imageId":3052003,
      "artworkUrl":"private-release-record",
      "formation":"f451",
      "rewards":[_sbc_pack(516,"Premium Electrum Players Pack")],
      "requirements":[_sbc_specific("club",22,9,"Borussia Dortmund"),
          _sbc_totw(2),_sbc_rating(82),_sbc_chemistry(85),
          _sbc_player_count()]},
     {"name":"Player Of the Month",
      "description":"Exchange an 86+ rated squad to celebrate Marco Reus's POTM Award",
      "sourceChallengeId":192,
      "url":"private-release-record",
      "imageId":3052004,
      "artworkUrl":"private-release-record",
      "formation":"f41212",
      "rewards":[_sbc_pack(308,"Prime Gold Players Pack"),
                 _sbc_kit(22,"Marco Reus POTM Kit")],
      "requirements":[_sbc_specific("nation",21,1,"Germany"),
          _sbc_specific("league",19,1,"Bundesliga"),
          _sbc_specific("club",22,1,"Borussia Dortmund"),
          _sbc_rating(86),_sbc_chemistry(70),_sbc_player_count()]}])

LOCAL_DE_JONG_FUTURE_STARS_SET=_player_sbc_set(
    23,"Frenkie de Jong - Future Stars",
    "Earn the 90-rated Future Stars Frenkie de Jong.",204,67337566,
    343,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Frenkie De Jong",
      "description":"Earn a FUT Future Stars Frenkie De Jong",
      "sourceChallengeId":897,
      "url":"private-release-record",
      "imageId":3092002,
      "artworkUrl":"private-release-record",
      "formation":"f442","rewards":[],
      "requirements":[_sbc_totw(),_sbc_specific("club",245,2,"Ajax"),
          _sbc_specific("league",10,4,"Eredivisie",operator="max"),
          _sbc_rating(85),_sbc_chemistry(80),_sbc_player_count()]}])

LOCAL_HAZARD_POTM_SET=_player_sbc_set(
    24,"Eden Hazard - Premier League POTM",
    "Earn the 93-rated Premier League Player of the Month Eden Hazard.",
    205,67292141,
    45,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Belgium","description":"Exchange a squad of Belgium players",
      "sourceChallengeId":169,
      "url":"private-release-record",
      "imageId":3040001,
      "artworkUrl":"private-release-record",
      "formation":"f3421","rewards":[_sbc_pack(403,"Mega Pack")],
      "requirements":[_sbc_specific("nation",7,9,"Belgium",operator="exact"),
          _sbc_totw(),_sbc_rating(82),_sbc_chemistry(95),
          _sbc_player_count()]},
     {"name":"Premier League",
      "description":"Exchange a squad of players featuring Premier League",
      "sourceChallengeId":170,
      "url":"private-release-record",
      "imageId":3040002,
      "artworkUrl":"private-release-record",
      "formation":"f451","rewards":[_sbc_pack(404,"Rare Mega Pack")],
      "requirements":[
          _sbc_specific("league",13,11,"Premier League",operator="exact"),
          _sbc_unique("nation",8,label="Nationalities"),_sbc_totw(2),
          _sbc_rating(84),_sbc_chemistry(90),_sbc_player_count()]},
     {"name":"Chelsea",
      "description":"Exchange a squad of players featuring Hazard's current Club",
      "sourceChallengeId":171,
      "url":"private-release-record",
      "imageId":3040003,
      "artworkUrl":"private-release-record",
      "formation":"f433","rewards":[_sbc_pack(401,"Rare Players Pack")],
      "requirements":[_sbc_specific("club",5,11,"Chelsea",operator="exact"),
          _sbc_rating(83),_sbc_chemistry(90),_sbc_player_count()]},
     {"name":"One-Man Show",
      "description":"Exchange a squad of 11 Gold Rare Players",
      "sourceChallengeId":172,
      "url":"private-release-record",
      "imageId":3040004,
      "artworkUrl":"private-release-record",
      "formation":"f442",
      "rewards":[_sbc_pack(309,"Jumbo Premium Gold Players Pack")],
      "requirements":[_sbc_unique("league",3,label="Leagues"),
          _sbc_unique("nation",3,label="Nationalities"),_sbc_quality("GOLD"),
          _sbc_rare(11,operator="exact"),_sbc_rating(86),
          _sbc_chemistry(90),_sbc_player_count()]},
     {"name":"7 Goals",
      "description":"Exchange a squad of players featuring Clubs that Hazard scored against in September",
      "sourceChallengeId":173,
      "url":"private-release-record",
      "imageId":3040005,
      "artworkUrl":"private-release-record",
      "formation":"f442","rewards":[_sbc_pack(401,"Rare Players Pack")],
      "requirements":[_sbc_specific("club",1943,2,"Bournemouth"),
          _sbc_specific("club",1961,2,"Cardiff City"),
          _sbc_specific("club",9,2,"Liverpool"),_sbc_rating(84),
          _sbc_chemistry(85),_sbc_player_count()]},
     {"name":"Finisher","description":"Exchange a squad featuring high profile players",
      "sourceChallengeId":174,
      "url":"private-release-record",
      "imageId":3040006,
      "artworkUrl":"private-release-record",
      "formation":"f3421","rewards":[_sbc_pack(402,"Jumbo Rare Players Pack")],
      "requirements":[_sbc_totw(4),_sbc_rating(86),_sbc_chemistry(50),
          _sbc_player_count()]},
     {"name":"Player Of the Month",
      "description":"Exchange a squad built around the September POTM winner, Eden Hazard",
      "sourceChallengeId":175,
      "url":"private-release-record",
      "imageId":3040007,
      "artworkUrl":"private-release-record",
      "formation":"f433","rewards":[_sbc_pack(403,"Mega Pack"),
                                      _sbc_kit(5,"Eden Hazard POTM Kit")],
      "requirements":[_sbc_same("league",3),_sbc_same("nation",4),
          _sbc_totw(3),_sbc_quality("GOLD"),_sbc_chemistry(90),
          _sbc_player_count(10)]}])

LOCAL_AUBAMEYANG_POTM_SET=_player_sbc_set(
    25,"Pierre-Emerick Aubameyang - Premier League POTM",
    "Earn the 89-rated Premier League Player of the Month Aubameyang.",
    209,84074647,
    102,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Pierre-Emerick Aubameyang",
      "description":"Exchange the POTM winner, Pierre-Emerick Aubameyang",
      "sourceChallengeId":367,
      "url":"private-release-record",
      "imageId":3065001,
      "artworkUrl":"private-release-record",
      "formation":"f4231","rewards":[_sbc_pack(401,"Rare Players Pack")],
      "requirements":[
          _sbc_specific("nation",115,1,"Gabon",operator="exact"),
          _sbc_specific("club",1,1,"Arsenal",operator="exact"),
          _sbc_player_count(1)]},
     {"name":"Africa Unite",
      "description":"Exchange a squad featuring players from Gabon, Cameroon, Congo and Nigeria",
      "sourceChallengeId":368,
      "url":"private-release-record",
      "imageId":3065002,
      "artworkUrl":"private-release-record",
      "formation":"f41212","rewards":[_sbc_pack(400,"Rare Gold Pack")],
      "requirements":[
          _sbc_specific_any("nation",(115,103),1,"Gabon + Cameroon"),
          _sbc_specific_any("nation",(107,133),1,"Congo + Nigeria"),
          _sbc_totw(2),_sbc_rating(82),_sbc_chemistry(80),
          _sbc_player_count()]},
     {"name":"Gunners",
      "description":"Exchange a squad featuring Arsenal and Premier League players",
      "sourceChallengeId":369,
      "url":"private-release-record",
      "imageId":3065003,
      "artworkUrl":"private-release-record",
      "formation":"f4231","rewards":[_sbc_pack(403,"Mega Pack")],
      "requirements":[_sbc_specific("club",1,3,"Arsenal"),
          _sbc_specific("league",13,4,"Premier League"),_sbc_totw(),
          _sbc_rating(85),_sbc_chemistry(80),_sbc_player_count()]},
     {"name":"Fast Plays",
      "description":"Exchange a squad featuring clubs Aubameyang scored against in October",
      "sourceChallengeId":370,
      "url":"private-release-record",
      "imageId":3065004,
      "artworkUrl":"private-release-record",
      "formation":"f442",
      "rewards":[_sbc_pack(305,"Premium Gold Players Pack"),
                 _sbc_kit(1,"Aubameyang POTM Kit")],
     "requirements":[_sbc_specific("club",144,1,"Fulham"),
          _sbc_specific("club",95,1,"Leicester City"),
          _sbc_specific("club",1799,1,"Crystal Palace"),_sbc_totw(2),
          _sbc_rating(84),_sbc_chemistry(80),_sbc_player_count()]}])

LOCAL_TORREIRA_FUTMAS_SET=_player_sbc_set(
    26,"Lucas Torreira - FUTmas",
    "Earn the final 86-rated FUTmas Lucas Torreira.",210,50555607,
    206,"private-release-record",
    "private-release-record",
    "private-release-record",
    [{"name":"Lucas Torreira",
      "description":"Exchange a squad to earn a special FUTmas Lucas Torreira!",
      "sourceChallengeId":588,
      "url":"private-release-record",
      "imageId":3100001,
      "artworkUrl":"private-release-record",
      "formation":"f451","rewards":[],
      "requirements":[_sbc_specific("club",1,1,"Arsenal"),
          _sbc_specific("nation",60,1,"Uruguay"),_sbc_same("league",6),
          _sbc_rating(83),_sbc_chemistry(85),_sbc_player_count()]}])

_EFL_CHAMPIONSHIP_ROWS=(
    (55,"Aston Villa",2,72,3018001,302,"Jumbo Gold Pack","aston-villa"),
    (56,"Birmingham City",88,68,3018002,314,"Small Gold Players Pack","birmingham-city"),
    (57,"Blackburn Rovers",3,67,3018003,205,"Premium Silver Players Pack","blackburn-rovers"),
    (58,"Bolton Wanderers",4,67,3018004,539,"Small Prime Mixed Players Pack","bolton-wanderers"),
    (59,"Brentford",1925,69,3018005,300,"Gold Pack","brentford"),
    (60,"Bristol City",1919,69,3018006,208,"Prime Silver Players Pack","bristol-city"),
    (61,"Derby County",91,71,3018007,516,"Premium Electrum Players Pack","derby-county"),
    (62,"Hull City",1952,68,3018008,300,"Gold Pack","hull-city"),
    (63,"Ipswich Town",94,68,3018009,300,"Gold Pack","ipswich-town"),
    (64,"Leeds United",8,71,3018010,302,"Jumbo Gold Pack","leeds-united"),
    (65,"Middlesbrough",12,70,3018011,400,"Rare Gold Pack","middlesbrough"),
    (66,"Millwall",97,68,3018012,534,"Mixed Players Pack","millwall"),
    (67,"Norwich City",1792,70,3018013,515,"Electrum Players Pack","norwich-city"),
    (68,"Nottingham Forest",14,70,3018014,302,"Jumbo Gold Pack","nottingham-forest"),
    (69,"Preston North End",1801,69,3018015,520,"Small Prime Electrum Players Pack","preston-north-end"),
    (70,"Queens Park Rangers",15,69,3018016,534,"Mixed Players Pack","queens-park-rangers"),
    (71,"Reading",1793,69,3018017,301,"Premium Gold Pack","reading"),
    (72,"Rotherham United",1797,65,3018018,211,"Small Prime Silver Players Pack","rotherham-united"),
    (73,"Sheffield United",1794,69,3018019,300,"Gold Pack","sheffield-united"),
    (74,"Sheffield Wednesday",1807,70,3018020,400,"Rare Gold Pack","sheffield-wednesday"),
    (75,"Stoke City",1806,74,3018021,303,"Jumbo Premium Gold Pack","stoke-city"),
    (76,"Swansea City",1960,71,3018022,314,"Small Gold Players Pack","swansea-city"),
    (77,"West Bromwich Albion",109,72,3018023,516,"Premium Electrum Players Pack","west-bromwich-albion"),
    (78,"Wigan Athletic",1917,67,3018024,208,"Prime Silver Players Pack","wigan-athletic"),
)
LOCAL_EFL_CHAMPIONSHIP_CHALLENGES=[]
for _offset,(_source_id,_club_name,_club_id,_rating,_image_id,
             _pack_id,_pack_name,_slug) in enumerate(_EFL_CHAMPIONSHIP_ROWS,1):
    _challenge=_local_sbc_challenge(
        193000+_offset,30,_club_name,
        "Exchange a squad of %s players" % _club_name,
        _image_id,[_sbc_pack(_pack_id,_pack_name)],
        [_sbc_specific("club",_club_id,11,_club_name,operator="exact"),
         _sbc_rating(_rating),_sbc_chemistry(95),_sbc_player_count()],
        formation="f442")
    _challenge.update({
        "verificationStatus":"historical_verified",
        "sourceChallengeId":_source_id,
        "sourceProvenance":{"provider":"historical_reference",
            "url":"private-release-record/%d/%s" %
                  (_source_id,_slug),
            "artworkUrl":"private-release-record/%d" % _image_id,
            "archive":"private-release-record",
            "verifiedAt":"2026-08-30",
            "evidence":["Challenge Requirements","Rewards","Artwork"]}})
    LOCAL_EFL_CHAMPIONSHIP_CHALLENGES.append(_challenge)
LOCAL_EFL_CHAMPIONSHIP_SET=_local_sbc_set(
    30,"EFL Championship",
    "Exchange all 24 EFL Championship club squads and choose one of: "
    "Curtis Davies, Fernando Forestieri, Leroy Fer.",
    "LEAGUES",3018000,LOCAL_EFL_CHAMPIONSHIP_CHALLENGES,
    rewards=[{"type":"player_pick","value":0,"count":1,
              "label":"EFL Championship League Player Pick",
              "optionCount":3,"minRating":83,"quality":"GOLD",
              "resourceIds":[50495409,50510276,50517999],
              "untradeable":True}],featured=True)
LOCAL_EFL_CHAMPIONSHIP_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":22,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "artworkUrl":"private-release-record",
        "archive":"private-release-record","verifiedAt":"2026-08-30"}})

LOCAL_ICON_UPGRADE_CHALLENGES=[
    _local_sbc_challenge(194001,40,"84-Rated Squad","Submit an 84-rated squad.",
        3046005,[{"type":"pack","value":511,"count":1}],
        [_sbc_totw(),_sbc_rating(84),_sbc_chemistry(65),
         _sbc_player_count()]),
    _local_sbc_challenge(194002,40,"85-Rated Squad","Submit an 85-rated squad.",
        3046006,[{"type":"pack","value":511,"count":1}],
        [_sbc_totw(),_sbc_rating(85),_sbc_chemistry(65),
         _sbc_player_count()]),
    _local_sbc_challenge(194003,40,"86-Rated Squad","Submit an 86-rated squad.",
        3046007,[{"type":"pack","value":511,"count":1}],
        [_sbc_totw(),_sbc_rating(86),_sbc_chemistry(65),
         _sbc_player_count()]),
]
for _source_id,_challenge in zip(
        (1071,1072,1073),LOCAL_ICON_UPGRADE_CHALLENGES):
    _image_id=int(_challenge["challengeImageId"])
    _slug=_challenge["name"].lower().replace(" ","-")
    _challenge.update({
        "verificationStatus":"historical_verified",
        "sourceChallengeId":_source_id,
        "sourceProvenance":{"provider":"historical_reference",
            "url":"private-release-record/%d/%s" %
                  (_source_id,_slug),
            "artworkUrl":("private-release-record"
                "sbc_challenge_image_%d-43ad271f-24af.png" % _image_id),
            "archive":"private-release-record",
            "verifiedAt":"2026-08-31",
            "evidence":["Challenge Requirements","Formation","Rewards",
                        "Artwork"]}})
LOCAL_ICON_UPGRADE_SET=_local_sbc_set(
    40,"Base Icon Upgrade","Complete all squads to earn one Base Icon.",
    "ICONS",3046005,LOCAL_ICON_UPGRADE_CHALLENGES,
    rewards=[{"type":"pack","value":1008,"count":1,"untradeable":True}],
    featured=True)
LOCAL_ICON_UPGRADE_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":411,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "verifiedAt":"2026-08-30",
        "evidence":["Challenge Requirements","Rewards","Artwork"]}})

_THROWBACK_FUT_BIRTHDAY_ROWS=(
    (2341,"FUT 09",403,"Mega Pack",[
        _sbc_unique("nation",3,label="Nationalities"),_sbc_quality("GOLD"),
        _sbc_rare(5),_sbc_chemistry(90),_sbc_player_count(10)]),
    (2342,"FUT 10",515,"Electrum Players Pack",[
        _sbc_same("nation",3),_sbc_unique("league",3,label="Leagues"),
        _sbc_rating(71),_sbc_chemistry(85),_sbc_player_count(10)]),
    (2343,"FUT 11",517,"Prime Electrum Players Pack",[
        _sbc_same("club",6),_sbc_quality_count("GOLD",6),_sbc_rare(3),
        _sbc_chemistry(95),_sbc_player_count(10)]),
    (2344,"FUT 12",303,"Jumbo Premium Gold Pack",[
        _sbc_specific("nation",54,2,"Brazil"),
        _sbc_quality_count("SILVER",3),_sbc_quality_count("GOLD",4),
        _sbc_chemistry(95),_sbc_player_count(10)]),
    (2345,"FUT 13",308,"Prime Gold Players Pack",[
        _sbc_same("league",4),_sbc_rare(4),_sbc_rating(80),
        _sbc_chemistry(80),_sbc_player_count(10)]),
)
LOCAL_THROWBACK_FUT_BIRTHDAY_CHALLENGES=[]
for _offset,(_source_id,_name,_pack_id,_pack_name,_requirements) in enumerate(
        _THROWBACK_FUT_BIRTHDAY_ROWS,1):
    _slug=_name.lower().replace(" ","-")
    _challenge=_local_sbc_challenge(
        195000+_offset,50,_name,
        "Celebrate FUT Birthday and build a squad around this timeless player from %s" % _name,
        922000+_offset,[_sbc_pack(_pack_id,_pack_name)],_requirements,
        formation="f442")
    _challenge.update({
        "verificationStatus":"historical_verified",
        "sourceChallengeId":_source_id,
        "sourceProvenance":{"provider":"historical_reference",
            "url":"private-release-record/%d/%s" %
                  (_source_id,_slug),
            "archive":"private-release-record",
            "verifiedAt":"2026-08-30",
            "evidence":["Challenge Requirements","Rewards"]}})
    LOCAL_THROWBACK_FUT_BIRTHDAY_CHALLENGES.append(_challenge)
LOCAL_THROWBACK_FUT_BIRTHDAY_SET=_local_sbc_set(
    50,"Throwback FUT Birthday",
    "Complete the five sourced FUT 09 through FUT 13 birthday squads.",
    "LIVE",922,LOCAL_THROWBACK_FUT_BIRTHDAY_CHALLENGES,featured=True)
LOCAL_THROWBACK_FUT_BIRTHDAY_SET.update({
    "verificationStatus":"historical_verified","sourceSetId":922,
    "sourceProvenance":{"provider":"historical_reference",
        "url":"private-release-record",
        "archive":"private-release-record","verifiedAt":"2026-08-30",
        "artworkStatus":"unavailable-in-archive"}})

try:
    from fut_sbc_verified import build_verified_sbc_sets
except ImportError:
    from .fut_sbc_verified import build_verified_sbc_sets

LOCAL_VERIFIED_REQUESTED_SBC_SETS=build_verified_sbc_sets()

CUSTOM_SBC_PARTY_BAG_SET_ID=8_100_001
CUSTOM_SBC_TOTS_PICK_SET_ID=8_100_002
CUSTOM_SBC_PICK_BUNDLE_SET_ID=8_100_003

_CUSTOM_IF_OR_TOTS_REQUIREMENT={
    "type":"card_type_count","operator":"min","value":1,
    "cardTypes":["TOTW","TOTS"],
    "description":"IF + TOTS Players: Min 1"}

CUSTOM_SBC_PARTY_BAG_CHALLENGE=_local_sbc_challenge(
    9_100_001,CUSTOM_SBC_PARTY_BAG_SET_ID,"FUT Deba Party Bag",
    "Exchange an 84-rated squad for an untradeable FUT Deba Party Bag.",
    9_100_001,[],
    [dict(_CUSTOM_IF_OR_TOTS_REQUIREMENT),_sbc_rating(84),
     _sbc_chemistry(50),_sbc_player_count()],
    repeatable=True,formation="f442")
CUSTOM_SBC_PARTY_BAG_SET=_local_sbc_set(
    CUSTOM_SBC_PARTY_BAG_SET_ID,"FUT Deba Party Bag",
    "Exchange a squad for one guaranteed untradeable Carniball, FUT Birthday or Headliners player.",
    "CUSTOM",CUSTOM_SBC_PARTY_BAG_SET_ID,
    [CUSTOM_SBC_PARTY_BAG_CHALLENGE],
    # RC75 exposed the live cache boundary: a challenge-level 9408 Award
    # returned straight to the SBC menu, while the set-level 80+ Pick entered
    # the reward flow in the same session. Party Bag is the completed group's
    # advertised main reward, so it must travel in grantedSetAwards as well.
    rewards=[{"type":"pack","value":9408,"packId":9408,"count":1,
              "label":"FUT Deba Party Bag","untradeable":True}],
    repeatable=True,featured=True)

_CUSTOM_DAVID_LUIZ_SOURCE=next(
    row for row in LOCAL_VERIFIED_REQUESTED_SBC_SETS
    if int(row.get("sourceSetId",0) or 0)==806)
CUSTOM_SBC_TOTS_PICK_CHALLENGES=[]
for _offset,_source in enumerate(_CUSTOM_DAVID_LUIZ_SOURCE["challenges"],1):
    # The requested shortcut is a real copy of the two sourced David Luiz
    # squads: only local identity and repeatability change, so requirements,
    # formations, child rewards and artwork cannot silently drift.
    _challenge=json.loads(json.dumps(_source))
    _challenge.update({
        "challengeId":9_100_010+_offset,
        "setId":CUSTOM_SBC_TOTS_PICK_SET_ID,
        "verificationStatus":"local_implemented","repeatable":True,
        "attemptContract":dict(_LOCAL_SBC_ATTEMPT_CONTRACT),
    })
    CUSTOM_SBC_TOTS_PICK_CHALLENGES.append(_challenge)

_CUSTOM_TOTS_88_ROWS=card_version_rows("TOTS",88,99)
_CUSTOM_TOTS_88_IDS=[int(row["resourceId"]) for row in _CUSTOM_TOTS_88_ROWS]
CUSTOM_SBC_TOTS_PICK_SET=_local_sbc_set(
    CUSTOM_SBC_TOTS_PICK_SET_ID,"1 of 3 Guaranteed TOTS Player Pick 88+",
    "Complete both squads to choose one of three untradeable TOTS players rated 88 or higher.",
    "CUSTOM",103,
    CUSTOM_SBC_TOTS_PICK_CHALLENGES,
    rewards=[{"type":"player_pick","value":0,"count":1,
              "label":"1 of 3 Guaranteed TOTS Player Pick 88+",
              "description":"Choose one of three untradeable TOTS players rated 88 or higher.",
              "optionCount":3,"minRating":88,"quality":"GOLD",
              "resourceIds":_CUSTOM_TOTS_88_IDS,
              "untradeable":True}],
    repeatable=True,featured=True)

CUSTOM_SBC_PICK_BUNDLE_CHALLENGE=_local_sbc_challenge(
    9_100_003,CUSTOM_SBC_PICK_BUNDLE_SET_ID,"Player Pick Bundle",
    "Exchange a balanced squad for twelve untradeable 1 of 4 78+ Player Picks.",
    1_003,[],
    [dict(_CUSTOM_IF_OR_TOTS_REQUIREMENT),_sbc_rating(84),
     _sbc_chemistry(50),_sbc_player_count()],
    repeatable=True,formation="f424")
CUSTOM_SBC_PICK_BUNDLE_SET=_local_sbc_set(
    CUSTOM_SBC_PICK_BUNDLE_SET_ID,"Player Pick Bundle",
    "Complete the squad to earn 12 untradeable 1 of 4 Player Picks rated 78+.",
    "CUSTOM",1_003,[CUSTOM_SBC_PICK_BUNDLE_CHALLENGE],
    rewards=[{"type":"pack","value":9409,"packId":9409,"count":1,
              "label":"Player Pick Bundle","untradeable":True}],
    repeatable=True,featured=True)
# The verified challenge source is installed on a native 485x567 set canvas;
# serving its 280x258 challenge texture here made the carousel art tiny.
CUSTOM_SBC_PICK_BUNDLE_SET["tileImage"]="SBS_SET_1003.png"

LOCAL_SBC_SETS=(
    # Starter Exchange and Elite Exchange were project-authored compatibility
    # placeholders, not historical FIFA 19/historical_reference sets.  Keeping them in the
    # hub produced the duplicate tutorial tile and forced invented rectangular
    # artwork into the retail menu.  The route registry now exposes only
    # source-backed historical groups plus the explicitly sourced local modes.
    LOCAL_LUCAS_POTM_SET,LOCAL_ZLATAN_FLASHBACK_SET,LOCAL_REUS_POTM_SET,
    LOCAL_DE_JONG_FUTURE_STARS_SET,LOCAL_HAZARD_POTM_SET,
    LOCAL_AUBAMEYANG_POTM_SET,LOCAL_TORREIRA_FUTMAS_SET,
    LOCAL_EFL_CHAMPIONSHIP_SET,LOCAL_ICON_UPGRADE_SET,
    # historical_reference's archived Throwback FUT Birthday page verifies the five rules
    # and pack rewards, but its original set/challenge image URLs are empty.
    # Publishing the group anyway makes the retail client request six assets
    # which can only return 404.  Keep the sourced specification above for
    # research and re-enable it only after authentic artwork is recovered;
    # an active beta catalogue must never advertise an incomplete tile.
    CUSTOM_SBC_PARTY_BAG_SET,CUSTOM_SBC_TOTS_PICK_SET,
    CUSTOM_SBC_PICK_BUNDLE_SET,
    )+LOCAL_VERIFIED_REQUESTED_SBC_SETS+(
    # A category renders its sets in registry order. The owner wants the three
    # generic tier upgrades at the end of UPGRADES so the sourced league and
    # Player Pick groups are seen first, so they are registered last.
    LOCAL_GOLD_SBC_SET,LOCAL_BRONZE_UPGRADE_SET,LOCAL_SILVER_UPGRADE_SET,
    )


SBC_CATEGORY_ORDER=("BASIC","ADVANCED","PLAYERS","UPGRADES","LEAGUES",
                    "ICONS","LIVE","CUSTOM")
SBC_CATEGORY_IDS={name:(index+1)*10
                  for index,name in enumerate(SBC_CATEGORY_ORDER)}


def _sbc_runtime_sets():
    # The catalogue file holds 921 sets of which 914 define no challenge, but
    # active_sets() already excludes those, so every set reaching this point
    # has content. tests/test_sbc_menu_has_no_empty_sets.py holds that line.
    sets=list(LOCAL_SBC_SETS)+list(SBC_CATALOGUE.active_sets())
    set_ids=[int(row.get("setId",0) or 0) for row in sets]
    challenge_ids=[int(challenge.get("challengeId",0) or 0)
                   for row in sets for challenge in row.get("challenges",[])]
    if (0 in set_ids or 0 in challenge_ids or
            len(set_ids) != len(set(set_ids)) or
            len(challenge_ids) != len(set(challenge_ids))):
        raise RuntimeError("active SBC registry contains an identity collision")
    return sets


def _sbc_set_spec(set_id):
    requested=int(set_id)
    return next((row for row in _sbc_runtime_sets()
                 if int(row.get("setId",0) or 0) == requested),None)


def _sbc_challenge_spec(challenge_id):
    requested=int(challenge_id)
    for set_spec in _sbc_runtime_sets():
        challenge=next((row for row in set_spec.get("challenges",[])
                        if int(row.get("challengeId",0) or 0) == requested),None)
        if challenge is not None:
            return set_spec,challenge
    return None


def _sbc_visible_progress_status(challenge_spec,saved):
    """Hide legacy full-squad echoes from partial SBC presentation state."""
    saved=saved if isinstance(saved,dict) else {}
    status=str(saved.get("status","NOT_STARTED"))
    if (bool(challenge_spec.get("repeatable",False)) and
            status.upper() == "CLAIMED"):
        # The submit Award remains independently claimable. Keeping the tile
        # COMPLETED suppressed both that same-session Award transition and the
        # next attempt in the live client, so repeatables reopen immediately.
        return "NOT_STARTED"
    count_rule=next((rule for rule in challenge_spec.get("requirements",[])
                     if isinstance(rule,dict) and
                     str(rule.get("type","")).lower()=="player_count"),{})
    required=max(1,min(11,int(count_rule.get("value",11) or 11)))
    squad=saved.get("squad",{}) if isinstance(saved.get("squad",{}),dict) else {}
    saved_count=sum(_sbc_candidate_item_id(row)>0
                    for row in squad.get("players",[]) or [])
    if required<11 and saved_count>required:
        # Older builds persisted the active 23-card squad-builder cache for a
        # partial challenge. The workspace already renders that cache empty;
        # publishing IN_PROGRESS in the challenge index nevertheless makes
        # CardsDLL disable submission before it sends PUT /squad.
        return "NOT_STARTED"
    return status


def _native_sbc_challenge(challenge_spec=LOCAL_SBC_CHALLENGE):
    set_id=int(challenge_spec.get("setId",0) or 0)
    challenge_id=int(challenge_spec.get("challengeId",0) or 0)
    saved=next((x for x in STATE.sbc_status()
                if int(x.get("setId",0)) == set_id and
                int(x.get("challengeId",0)) == challenge_id),None)
    status=_sbc_visible_progress_status(challenge_spec,saved)
    dto=challenge_dto(challenge_spec,status)
    dto["timesCompleted"]=int((saved or {}).get(
        "completionCount",0) or 0)
    dto["lastCompleteTime"]=int((saved or {}).get("completedAt",0) or 0)
    return dto


_SBC_SET_INDEX_KEYS=(
    # Parser-safe set-index contract proven by the retail SBC menu.  Do not
    # copy zero-valued favourite/interval bookkeeping into this feed: with the
    # complete verified catalogue those aliases are repeated in both the ALL
    # preview and the category envelopes and push CardsDLL over its safe
    # initial-payload size.  Challenge rules/rewards remain on
    # /setId/<id>/challenges and completion responses.
    "setId","categoryId","name","description","priority",
    "challengesCount","challengesCompletedCount","hidden",
    "endTime","repeatable","timesCompleted",
    "tutorial",
    "setImageId","rewardPreviewImageId",
    "awards",
)

_SBC_INDEX_ITEM_KEYS=(
    # Reward -> Item.createItem only needs the stable native identity and card
    # presentation fields here. Mutable gameplay fields (contracts, fitness,
    # morale and work rates) belong to the actual grant. The six compact FUT
    # face attributes are required in this preview, otherwise the card shows
    # PAC/SHO/PAS/DRI/DEF/PHY as zero even though its detail stats are valid.
    "id","assetId","definitionId","resourceId",
    "resourceGameYear","itemType","cardsubtypeid","rating","rareflag",
    "preferredPosition","teamid","leagueId","nation",
    "untradeable","loans","attributeArray","attributeList",
    "name","description","amount","value","playerPickDefinitionId",
    "optionCount",
)

# The native PC client was designed for a small simultaneously-active ALL
# feed.  Every category still embeds its complete catalogue; the root list is
# only the balanced ALL-tab preview. One tile from each active native
# category is enough to seed the client-owned ALL tab, avoids duplicating
# extra full set records, and leaves room for the sourced Group Rewards while
# remaining below the CardsDLL crash envelope.
SBC_ALL_PREVIEW_LIMIT=len(SBC_CATEGORY_ORDER)


def _native_sbc_index_award(award):
    """Return the parser-safe Award preview used only by /sbs/sets."""
    if not isinstance(award,dict):
        return None
    # set_dto() returns the legacy SBS preview spelling (type/value/count).
    # Submission receipts and Squad Battles use a different native Award DTO;
    # converting this row to that contract makes the catalogue load while all
    # of its visual reward slots remain empty in the retail FIFA 19 client.
    row=dict(award)
    award_type=str(row.get("type",row.get("awardType","")) or "").lower()
    if award_type=="item":
        item=row.get("itemData")
        if not isinstance(item,dict):
            return None
        definition_id=int(item.get("definitionId",item.get(
            "resourceId",0)) or 0)
        # Prime Icon Moments were released after the launch player table used
        # by this FIFA 19 build.  The exact FUT definition remains the reward
        # value/grant identity, but the card factory needs the private wire ID
        # whose low 24 bits point at the verified launch Icon profile.  Without
        # this conversion Cruyff resolves to the unrelated base-table player
        # Ryan Allsop and the preview face attributes remain zero.
        if card_revision(definition_id)=="Prime Icon Moments":
            item=_native_player_item(item)
        pick_item=(int(item.get("resourceId",0) or 0)==
                   PLAYER_PICK_RESOURCE_ID)
        if pick_item:
            # The set index duplicates every record in its category and the
            # balanced ALL preview. Keep the Pick Item compact, but retain all
            # fields read by Item.createItem: the earlier identity-only row
            # produced a blank Gold-looking object instead of EA's white
            # Player Pick card in the Deja vu and League SBC tiles.
            pick_keys=("id","assetId","definitionId","resourceId",
                       "resourceGameYear","itemType","cardsubtypeid",
                       "rating","rareflag","itemState","owners",
                       "discardValue","pile","untradeable","amount",
                       "value","playerPickDefinitionId","optionCount",
                       "name","description")
            compact={key:item[key] for key in pick_keys if key in item}
        else:
            compact={key:item[key] for key in _SBC_INDEX_ITEM_KEYS if key in item}
        if int(compact.get("resourceId",0) or 0)<=0:
            return None
        # The retail reward factory requires loans to distinguish permanent
        # item rewards from loan grants, including a zero value.
        if int(compact.get("resourceId",0) or 0)!=PLAYER_PICK_RESOURCE_ID:
            compact.setdefault("loans",0)
        row["itemData"]=compact
        if pick_item:
            # This Award parser ignores loan/resourceId aliases and defaults a
            # missing zero halId. Removing them from every duplicated Pick
            # preview keeps the full catalogue below CardsDLL's proven 65 KB
            # initial-payload ceiling without removing any parsed member.
            for key in ("halId","loan","resourceId"):
                row.pop(key,None)
    return row


def _native_sbc_set_index_summary(native):
    """Strip internal/runtime metadata from one initial set-list record."""
    row={key:native[key] for key in _SBC_SET_INDEX_KEYS if key in native}
    if bool(row.get("repeatable",False)):
        # CardsDLL stores this field independently from the zero completed-
        # challenge count (parser branches +0x295e7a and +0x2961f4). RC75
        # proved that carrying historical attempts here leaves the refreshed
        # repeatable tile green even though its next workspace is available.
        # Receipts and SQLite retain the real completion count.
        row["timesCompleted"]=0
    row["awards"]=[value for value in (
        _native_sbc_index_award(award) for award in native.get("awards",[]))
        if value is not None]
    return row


def _native_sbc_all_preview(grouped,limit=SBC_ALL_PREVIEW_LIMIT):
    """Return a small category-balanced root list for the synthetic ALL tab."""
    limit=max(0,int(limit))
    if not limit:
        return []
    queues={name:list(grouped.get(name,[])) for name in SBC_CATEGORY_ORDER}
    preview=[]
    # First expose one tile from every populated section so ALL never looks as
    # if an entire Players/Icons/Leagues section is missing.  Fill the remaining
    # budget round-robin, preserving the catalogue priority within each tab.
    while len(preview)<limit and any(queues.values()):
        progressed=False
        for name in SBC_CATEGORY_ORDER:
            rows=queues.get(name,[])
            if not rows:
                continue
            preview.append(rows.pop(0))
            progressed=True
            if len(preview)>=limit:
                break
        if not progressed:
            break
    return preview


def _native_sbc_sets():
    statuses={(int(row.get("setId",0)),int(row.get("challengeId",0))):row
              for row in STATE.sbc_status()}
    grouped={}
    recent_completed=[]
    for priority,set_spec in enumerate(_sbc_runtime_sets(),1):
        set_id=int(set_spec.get("setId",0) or 0)
        challenge_statuses={
            int(challenge.get("challengeId",0) or 0):statuses.get(
                (set_id,int(challenge.get("challengeId",0) or 0)),
                {"status":"NOT_STARTED","completionCount":0})
            for challenge in set_spec.get("challenges",[])}
        native=set_dto(set_spec,challenge_statuses,priority)
        category=str(native.get("category","BASIC") or "BASIC")
        native["categoryId"]=SBC_CATEGORY_IDS.get(category,90)
        summary=_native_sbc_set_index_summary(native)
        grouped.setdefault(category,[]).append(summary)
        last_completed=int(native.get("lastCompletedTime",0) or 0)
        if (bool(native.get("isCompleted",False)) and last_completed>0 and
                (bool(native.get("repeatable",False)) or
                 now_s()-last_completed<=300)):
            recent_completed.append((last_completed,summary))
    audit_categories=dict((SBC_CATALOGUE.payload.get("catalogueAudit",{}) or {})
                          .get("categories",{}) or {})
    ordered=list(SBC_CATEGORY_ORDER)+sorted(
        set(grouped).union(audit_categories).difference(SBC_CATEGORY_ORDER))
    categories=[]
    for name in ordered:
        category_id=SBC_CATEGORY_IDS.get(name,90)
        active_sets=list(grouped.get(name,[]))
        # CardsDLL renders a published category even when its set array is
        # empty, leaving a selectable blank panel (the RC70 LIVE symptom).
        if not active_sets:
            continue
        categories.append({"categoryId":category_id,"id":category_id,
                           "name":name,"priority":category_id,
                           # FIFA 19 resolves a category from its embedded set
                           # array; setIds alone leave the entire tab blank.
                           # The client creates its global ALL tab from the
                           # root set list, so each category owns only its own
                           # rows and no synthetic All category is published.
                           "sets":active_sets,
                           "setIds":[row["setId"] for row in active_sets],
                           "displayable":True,"isAll":False,
                           "isFavourite":False,"type":0})
    visible_sets=[row for name in ordered for row in grouped.get(name,[])]
    preview_sets=[dict(row) for row in _native_sbc_all_preview(grouped)]
    # The completion controller refreshes /sbs/sets immediately after submit.
    # The root list is intentionally capped for CardsDLL safety, so ensure the
    # just-completed set is present even when it was not one of the balanced
    # category representatives. Repeatable rows stay here until claimed;
    # ordinary completions receive a short transition window.
    if recent_completed:
        recent=max(recent_completed,key=lambda pair:pair[0])[1]
        if int(recent["setId"]) not in {
                int(row.get("setId",0) or 0) for row in preview_sets}:
            if preview_sets:
                preview_sets[-1]=dict(recent)
            else:
                preview_sets=[dict(recent)]
    visible_ids=[row["setId"] for row in visible_sets]
    # Do not add an explicit All category. The retail UI always prepends its
    # own ALL tab from this root set list; publishing another one is exactly
    # what produced the two adjacent ALL/All tabs in the live screenshot.
    return {"categories":categories,"sets":preview_sets,
            "count":len(preview_sets),"catalogCount":len(visible_sets),
            "allSetIds":visible_ids,"endOfList":True,"timestamp":now_s(),
            # FIFA refreshes this route immediately after an SBC submit. The
            # compact delta invalidates its cached Objectives state without
            # requiring the user to leave and re-enter FUT.
            "dynamicObjectivesUpdates":_objective_updates_for_counter(
                "objective_sbc_completed",compact=True)}


def _is_sbc_brick_item_id(value):
    """True for the placeholder cards published in locked pitch slots.

    The client returns the whole squad on save and on submit, bricks
    included. They are not cards the player placed: counting them would fail
    every size rule, and consuming them would try to delete items that do not
    exist in the club.
    """
    try:
        return int(value or 0)>=SBC_BRICK_ITEM_BASE_ID
    except (TypeError,ValueError):
        return False


def _sbc_candidate_item_id(row):
    if not isinstance(row,dict):
        return 0
    item=row.get("itemData",row.get("item",row))
    if not isinstance(item,dict):
        return 0
    try:
        item_id=int(item.get("id",item.get("itemId",0)) or 0)
    except (TypeError,ValueError):
        return 0
    return 0 if _is_sbc_brick_item_id(item_id) else item_id


def _normalize_sbc_candidate(challenge_spec,candidate):
    """Keep exactly the cards the player placed in the native SBC builder.

    ``slotMask`` is not a member name FIFA 19 knows. Read-only inspection of
    ``CardsDLL_Win64_retail.dll`` shows ``slot`` and ``slotIndex`` in the
    sorted key table at +0x351200 and no mask member, so the client never
    receives the challenge's OPEN/BRICK mask. It renders its own formation,
    lets the player use any pitch slot, and returns the cards at whatever
    native indices were used.

    The previous implementation assumed the cards would arrive at the
    catalogue's OPEN indices. When they did not it either inferred a placement
    by brute force, which consumed cards the player had never selected, or
    gave up and returned the raw 23-slot squad, which reached submit with
    bench rows and reported an unrelated slot-mapping error. The 2026-09-03
    live session reproduced both: "The Second Step" consumed the wrong cards
    and "The Third Step" could not be completed at all.

    The player's placement is authoritative. Keep the populated pitch rows as
    sent, drop the bench and reserve rows, which are never part of an SBC
    squad, and let the challenge requirements decide whether the submission is
    valid. An oversized squad now fails on its own player-count rule with a
    message the player can act on.
    """
    if not isinstance(candidate,dict):
        return candidate
    rows=list(candidate.get("players",[]) or [])
    if not rows:
        return candidate
    kept=[]
    dropped=0
    for row in rows:
        if not isinstance(row,dict):
            continue
        if _sbc_candidate_item_id(row)<=0:
            continue
        if int(row.get("sourceSlot",row.get("cardId",0)) or 0)>0:
            kept.append(row)
            continue
        native_index=-1
        for key in ("index","slot"):
            if key not in row:
                continue
            try: native_index=int(row.get(key,-1))
            except (TypeError,ValueError): native_index=-1
            if native_index>=0:
                break
        if 0 <= native_index <= 10:
            kept.append(row)
        else:
            dropped+=1
    if not kept:
        return candidate
    if dropped:
        log("sbc","challenge=%s dropped %d bench/reserve rows from the "
            "submitted squad" % (challenge_spec.get("challengeId"),dropped))
    normalized=json.loads(json.dumps(candidate))
    normalized["players"]=[json.loads(json.dumps(row)) for row in kept]
    return normalized


def _sbc_raw_assignment_item_ids(candidate):
    """Keep owned IDs from every populated row before pitch normalization."""
    if not isinstance(candidate,dict):
        return []
    return [_sbc_candidate_item_id(row)
            for row in candidate.get("players",[]) or []
            if _sbc_candidate_item_id(row)>0]


_SBC_SUBMIT_WIRE_KEYS={
    "challengeId","credits","grantedChallengeAwards","grantedSetAwards",
    "preOrderPacks","recoveredPacks","setId",
}


def _native_sbc_submit_wire(receipt,challenge_id,set_id):
    """Project an internal receipt onto FIFA 19's closed submit DTO.

    The retail ``FutSBCSubmitChallengeServerResponse`` reads exactly these
    seven members.  Completion status is durable and is refreshed through the
    normal set/challenge endpoints; sending nested project-only status and set
    objects here left the retail workspace stuck on its stale IN_PROGRESS
    snapshot even though the transaction and reward had already completed.
    """
    receipt=receipt if isinstance(receipt,dict) else {}

    def submit_award(award):
        """Return an Award containing the console-native granted item DTO.

        FutState persists an inventory-friendly player row whose pile is the
        string ``purchased``. That is valid in SQLite, but the Award reader
        expects the same numeric item DTO returned by GET /purchased/items.
        Sending the storage row made the reward durable yet invisible until
        the next FUT bootstrap rebuilt it.
        """
        source=dict(award or {})
        item=source.get("itemData")
        if (isinstance(item,dict) and
                int(item.get("id",item.get("itemId",0)) or 0)>0):
            source["itemData"]=_native_item(item)
        native=native_reward_dto(source)
        # FutSBCSubmitChallengeServerResponse passes both granted arrays to the
        # legacy Award parser at CardsDLL+0x2788b0. Its cumulative type dispatch
        # recognizes only bidToken/coin/item/pack, so project-level names such
        # as player and playerPick must use the normalized native Award type.
        # The concrete subtype-237 Pick identity remains inside itemData.
        native.update({
            "type":str(native.get("awardType",source.get("type","")) or ""),
            "value":int(native.get("awardValue",source.get("value",0)) or 0),
            "count":int(native.get("awardCount",source.get("count",1)) or 1),
            "isUntradeable":bool(native.get("untradeable",False)),
        })
        if str(source.get("type","")).lower() in {"playerpick","player_pick"}:
            native["playerPickDefinitionId"]=PLAYER_PICK_DEFINITION_ID
            native["optionCount"]=int(source.get("optionCount",3) or 3)
        return native

    challenge_awards=[submit_award(award) for award in
                      receipt.get("awards",[]) or []
                      if isinstance(award,dict)]
    set_awards=[submit_award(award) for award in
                receipt.get("groupAwards",[]) or []
                if isinstance(award,dict)]
    return {
        "challengeId":int(challenge_id),
        "credits":int(STATE.credits()),
        "grantedChallengeAwards":challenge_awards,
        "grantedSetAwards":set_awards,
        "preOrderPacks":len(STATE.reward_unopened_packs()),
        "recoveredPacks":0,
        "setId":int(set_id),
    }


# A locked SBC slot is expressed by the item that occupies it, not by a flag
# on the empty row. CardsDLL reads IS_CUSTOM_BRICK_PLAYER from item offset
# +0xb7, beside IS_DRAFT_PLAYER at +0xb6, and the routine that recognises the
# "BRICK" string at +0x248472 is parsing a position string off an item.
# futitemraritytunables.json, which we serve, defines rarity 29 as
# CUSTOM_BRICK and rarity 2 as LOCK.
#
# Publishing the marker on an empty row - what RC44 did - put it on an object
# the client does not read. These ids sit far above every club item so a brick
# can never be confused with, or consumed as, a real card.
SBC_BRICK_ITEM_BASE_ID=990000000000
# Rarity 29 (CUSTOM_BRICK) was tried live on 2026-09-03 and the client drew a
# normal player card: rating substituted to 50, "NOT FOUND" club badge, an
# expired contract. Rarity 2 is named LOCK in the same tunables, and the
# retail screenshots of these three challenges show a padlock, so LOCK is the
# closer match to the plain "BRICK" position we publish (the "CUSTOM_BRICK"
# position maps to 29, which is a different challenge type).
#
# Both were then tried live and both are wrong: 29 draws a player card with a
# substituted 50 rating, a "NOT FOUND" badge and an expired contract, and 2
# draws a neutral shield indistinguishable from an empty slot. Neither is the
# padlock the retail screenshots show, so the lock presentation is not chosen
# by the item rarity and guessing further values only costs live test cycles.
#
# Publishing bricks is therefore off by default: an empty slot is no worse
# than a neutral shield and strictly better than a broken card. The mechanism
# stays reachable for the next candidate without a rebuild - set
# LOCALFUT19_SBC_BRICK_RARITY to a positive rarity to publish them again.
try:
    SBC_BRICK_RARITY=int(os.environ.get("LOCALFUT19_SBC_BRICK_RARITY","2"))
except (TypeError,ValueError):
    SBC_BRICK_RARITY=2
# Kept only so an explicit setting cannot silently resurrect the mistake.
_raw_custom_lock=os.environ.get("LOCALFUT19_SBC_CUSTOM_LOCK","")
# Reinstated on 2026-09-04. Turning bricks off was wrong: with the locked
# slots occupied the pitch really did offer only the challenge's own number of
# places, and the client's "Number of players in the Squad" row read that
# number instead of 11. Rarity 2 (LOCK) draws a neutral shield rather than
# rarity 29's player card with a substituted 50 rating, so it is the one to
# keep while the padlock art is still unidentified.
try:
    SBC_CUSTOM_LOCK_VALUE=(int(_raw_custom_lock)
                           if str(_raw_custom_lock).strip() else None)
except (TypeError,ValueError):
    SBC_CUSTOM_LOCK_VALUE=1


def _native_sbc_brick_item(challenge_id,index):
    """Build the placeholder card that occupies a locked pitch slot."""
    item=native_player_fields(0,{})
    item.update({
        "id":SBC_BRICK_ITEM_BASE_ID+int(challenge_id)*100+int(index),
        "itemId":SBC_BRICK_ITEM_BASE_ID+int(challenge_id)*100+int(index),
        "resourceId":0,"definitionId":0,"assetId":0,
        "itemType":"player","cardsubtypeid":0,
        "position":"BRICK","preferredPosition":"BRICK",
        "rareflag":SBC_BRICK_RARITY,"rating":0,
        # The client substituted 50 for a zero rating and drew an expired
        # contract, so a brick must not look like a playable card at all.
        "cardType":"lock","playStyle":0,
        "untradeable":True,"tradeable":False,"discardValue":0,
        "pile":7,"contract":0,"fitness":0,"loyaltyBonus":0,
        "marketDataMinPrice":0,"marketDataMaxPrice":0,
        "attributeArray":[0,0,0,0,0,0],
        "attributeList":[{"index":i,"value":0} for i in range(6)],
        "statsArray":[0,0,0,0,0],"lifetimeStatsArray":[0,0,0,0,0],
        "teamid":0,"leagueId":0,"nation":0,"skillmoves":0,
        "weakfootabilitytypecode":0,"preferredfoot":1,
        "timestamp":now_s(),"resourceGameYear":2019,
    })
    return item


def _native_sbc_workspace(set_spec,challenge_spec):
    """Return an explicit SBC dream squad, never the cached active squad."""
    set_id=int(set_spec.get("setId",0) or 0)
    challenge_id=int(challenge_spec.get("challengeId",0) or 0)
    saved=next((row for row in STATE.sbc_status()
                if int(row.get("setId",0)) == set_id and
                int(row.get("challengeId",0)) == challenge_id),{})
    repeatable_finished=(bool(challenge_spec.get("repeatable",False)) and
        str(saved.get("status","NOT_STARTED")).upper() == "CLAIMED")
    squad=({
        "id":challenge_id,"squadName":challenge_spec.get("name","SBC"),
        "formation":challenge_spec.get("formation","f442"),"players":[]}
        if repeatable_finished else saved.get("squad",{
            "id":challenge_id,"squadName":challenge_spec.get("name","SBC"),
            "formation":challenge_spec.get("formation","f442"),"players":[]}))
    workspace_status=_sbc_visible_progress_status(challenge_spec,saved)
    # A saved placement within the challenge size is the player's own work and
    # is returned unchanged, so reopening never loses a selection.
    #
    # A saved squad with more pitch cards than the challenge accepts can never
    # be submitted. FIFA never receives the slot mask, so it reopens the
    # builder pre-filled with exactly those cards and the player is stuck on a
    # permanent "# of players in the Squad" rejection, which is what blocked
    # "The Third Step" on 2026-09-03 at 01:20:35 with eleven cards saved for a
    # four-card challenge. Start such a workspace empty instead. Nothing is
    # chosen on the player's behalf: trimming to a subset would repeat the
    # inference mistake removed in RC37.
    count_rule=next((rule for rule in challenge_spec.get("requirements",[])
                     if isinstance(rule,dict) and
                     str(rule.get("type","")).lower()=="player_count"),{})
    required=max(1,min(11,int(count_rule.get("value",11) or 11)))
    saved_count=sum(_sbc_candidate_item_id(row)>0
                    for row in squad.get("players",[]) or [])
    if saved_count>required:
        log("sbc","challenge=%s reopened empty: %d saved cards exceed the "
            "%d the challenge accepts" % (
                challenge_id,saved_count,required))
        squad={"id":challenge_id,
               "squadName":challenge_spec.get("name","SBC"),
               "formation":challenge_spec.get("formation","f442"),
               "players":[]}
        workspace_status="NOT_STARTED"
    native=_native_squad_json(squad)
    validation=STATE.preview_sbc_submission(challenge_spec,squad)
    native.update({"valid":bool(validation["valid"]),
                   "chemistry":int(validation["chemistry"]),
                   "rating":int(validation["rating"]),
                   "starRating":int(validation["rating"])})
    # The StartChallenge response has a dedicated per-slot contract; putting
    # BRICK on a squad row or inventing an item only changes card presentation.
    # Its parser dispatches playerRequirements (member 717) at
    # CardsDLL+0x24937e, then index/playerType/elgReq at +0x2495b7. "BRICK" is
    # decoded to code 2 at +0x2496cc and stored in the indexed slot table at
    # +0x2498c1. The validator later accepts an empty slot carrying code 2 for
    # BRICK_CHALLENGE at +0x234612..+0x234621. Without this array the client
    # treats all eleven formation slots as open and displays 11 in its own
    # "Number of players in the Squad" row.
    mask=[str(value).upper() for value in challenge_spec.get("slotMask",[]) or []]
    player_requirements=[
        {"index":index,"playerType":"BRICK","elgReq":[]}
        for index,state in enumerate(mask[:11]) if state == "BRICK"]
    if player_requirements:
        log("sbc","challenge=%s published %d BRICK player requirements" %
            (challenge_id,len(player_requirements)))
    populated=any(isinstance(row.get("itemData"),dict)
                  for row in native.get("players",[]))
    native.update({"id":challenge_id,"squadType":"DREAM_SQUAD",
                   "dreamSquad":True,"actives":[],
                   "slotMask":list(challenge_spec.get("slotMask",[]) or [])})
    native.update({"squadId":challenge_id,"challengeId":challenge_id,
                   "setId":set_id})
    if not populated:
        native["newSquad"]=True
    # Some FIFA 19 controllers decode a squad response directly, while the SBC
    # screen also accepts the documented nested member.  Publishing both views
    # keeps the identities identical and prevents a fallback to activeSquadId.
    return {**native,"challengeId":challenge_id,"setId":set_id,
            "status":workspace_status,
            "playerRequirements":player_requirements,"squad":native}


def _native_sbc_set_rewards(set_spec,cycle=None):
    """Project a stored set-reward receipt without granting it a second time."""
    set_id=int(set_spec.get("setId",0) or 0)
    receipt=STATE.sbc_set_reward_receipt(set_id,cycle)
    awards=list((receipt or {}).get("awards",[]) or [])
    status="COMPLETED" if receipt else "NOT_STARTED"
    return {"setId":set_id,"completedSetId":set_id,
            "status":status,"setCompleted":bool(receipt),
            "awards":awards,"rewards":awards,
            "setAwards":awards,"groupAwards":awards,
            "grantedSetAwards":awards,"grantedAwards":awards,
            "unopenedPackIds":list((receipt or {}).get(
                "unopenedPackIds",[]) or []),
            "playerPicks":list((receipt or {}).get("playerPicks",[]) or []),
            "cycle":int((receipt or {}).get("cycle",cycle or 0) or 0),
            "completedAt":int((receipt or {}).get("created",0) or 0),
            **_credits_payload()}


ROSTER_XML=b'''<?xml version="1.0" encoding="UTF-8"?>
<squad>
  <squadInfoSet>
    <squadInfo platform="pc64">
      <dbSchemaCRC>0000000000000000000000000000000000000000</dbSchemaCRC>
      <dbMajor>0</dbMajor><dbMinor>0</dbMinor>
      <dbMajorCRC>0</dbMajorCRC><dbMinorCRC>0</dbMinorCRC>
      <dbMajorLoc></dbMajorLoc><dbMinorLoc></dbMinorLoc>
      <dbFUTVer>0</dbFUTVer><dbFUTCRC>0</dbFUTCRC><dbFUTLoc></dbFUTLoc>
    </squadInfo>
  </squadInfoSet>
</squad>'''

# Local mirror of FUT static content stored in the still-active local content archive
# (fifa19.content.easports.com). It serves the real files required by the
# client (futitemraritytunables.json, packopening*.json, fut2dheads.big, ...)
# instead of {}/404 responses that caused endless retries and hub freezes.
FUT_CONTENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "fut_content")

def _valid_playerhead_file(path):
    """Validate compact base/PIM and original large dynamic DXT5 canvases."""
    try:
        with open(path,"rb") as handle:
            header=handle.read(128)
        if len(header)!=128 or header[:4]!=b"DDS " or header[84:88]!=b"DXT5":
            return False
        height=int.from_bytes(header[12:16],"little")
        width=int.from_bytes(header[16:20],"little")
        known_canvas=((width,height)==(220,256) or
                      (400 <= width <= 512 and 500 <= height <= 600 and
                       abs((width / height) - (220 / 256)) <= 0.02))
        encoded_bytes=((width+3)//4)*((height+3)//4)*16
        return (known_canvas and
                os.path.getsize(path)==128+encoded_bytes+8)
    except OSError:
        return False

STATIC_CONTENT_TYPES = {
    ".big": "application/octet-stream",
    ".bin": "application/octet-stream",
    ".csv": "text/csv; charset=utf-8",
    ".dds": "image/vnd-ms.dds",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".tga": "application/octet-stream",
    ".xml": "application/xml; charset=utf-8",
}

SQUAD_BATTLE_ART_PREFIX = "/fut/squadbattle/"

def _static_content_type(low):
    """Return the CDN static-content MIME type, or None for API routes."""
    if not low.startswith(("/fut/", "/contentfifa/")):
        return None
    if low.startswith(SQUAD_BATTLE_ART_PREFIX):
        # `bannerImageName` from FutGetSquadBattleFeaturedServerResponse is a
        # bare name, so the client requests this artwork without an extension.
        # Without this the path fell through to the JSON branch and answered
        # 200 with two bytes, which the client would decode as an image.  The
        # access violation reading 0x398 seen in the same runs came from
        # Origin::OriginSDK::GrantAchievement, not from this reply.
        return STATIC_CONTENT_TYPES[".png"]
    return STATIC_CONTENT_TYPES.get(os.path.splitext(low)[1])


def _season_card_asset(low):
    """Return the Season card asset document for `fut/items/pc/<id>.json`."""
    match=_re.fullmatch(r"/fut/items/pc/(\d+)\.json",str(low))
    if not match:
        return None
    document=season_asset(int(match.group(1)))
    if document is None:
        return None
    log("seasons","card asset %d type=%d" % (
        int(document["tournamentId"]),int(document["tournamentType"])))
    return J(document)


def _playerhead_request_id(low):
    match=_re.fullmatch(r"/fut/playerheads/g4/single/p(\d+)\.dds",str(low))
    return int(match.group(1)) if match else 0


def _resolved_playerhead_path(requested_playerhead,root):
    """Resolve one player-head URL without request-order or session state.

    Transfer Search loads several portrait URLs concurrently.  A former
    compatibility path remembered the most recently requested special card
    for each base player; consequently the bytes returned by a base URL could
    change according to which HTTP worker won a race.  CardsDLL also reuses a
    carousel texture when one request is missing, so a single 404 could leave
    a preceding player's face on a later card.

    Exact sourced DDS files always win.  A genuinely absent definition may
    borrow only another sourced revision of the *same* base player, selected
    deterministically (same promo family first, then rating and resource ID).
    The same URL therefore always produces the same bytes and can be cached
    safely.  It can never resolve to another player's portrait.
    """
    requested_playerhead=int(requested_playerhead or 0)
    exact_id=int(_PIM_WIRE_TO_EXACT.get(
        requested_playerhead,requested_playerhead))
    exact_path=os.path.join(
        root,"fut","playerheads","g4","single","p%d.dds" % exact_id)
    if _valid_playerhead_file(exact_path):
        return exact_path

    base_asset=exact_id % (1 << 24)
    requested_revision=str(card_revision(exact_id) or "")
    alternatives=sorted(
        (row for row in card_version_rows()
         if int(row.get("assetId",0) or 0)==base_asset and
            int(row.get("resourceId",0) or 0)!=exact_id),
        key=lambda row:(
            0 if str(row.get("revision","") or "")==requested_revision else 1,
            -int(row.get("rating",0) or 0),
            int(row.get("resourceId",0) or 0)))
    for row in alternatives:
        alternative=os.path.join(
            root,"fut","playerheads","g4","single",
            "p%d.dds" % int(row.get("resourceId",0) or 0))
        if _valid_playerhead_file(alternative):
            return alternative
    return None


def _squad_battle_artwork(low):
    """Resolve the Squad Battles banner name to local valid PNG art."""
    if not low.startswith(SQUAD_BATTLE_ART_PREFIX):
        return None
    artwork_dir=os.path.join(FUT_CONTENT_DIR,"fut","sbc","gen4","tile")
    if not os.path.isdir(artwork_dir):
        return None
    available={name.lower():name for name in os.listdir(artwork_dir)}
    requested=os.path.basename(low)
    selected=(available.get(requested) or
              available.get(requested+".png") or
              available.get("gamehub_sbs.png"))
    if selected is None:
        return None
    with open(os.path.join(artwork_dir,selected),"rb") as handle:
        return handle.read()


def _sbc_tile_artwork(low):
    """Resolve every retail/legacy SBC image route to a valid local PNG.

    FIFA 19 does not consume ``tileImageUrl`` consistently.  Its native UI
    commonly synthesizes ``sets/images/sbc_set_image_<assetId>.png`` (and the
    matching challenge route) from the numeric image id. It also requests the
    complete fixed-choice Reward Details composition as
    ``sbc_pickpack_image_<assetId>.png``; resolving that name by number to the
    ordinary set tile leaves the candidate-card area blank. Supporting only the
    convenient ``tile`` alias caused a tight 404 retry loop and left every tile
    on its loading/locked presentation.
    """
    tile_prefix="/fut/sbc/gen4/tile/"
    set_prefixes=("/fut/sbc/gen4/sets/images/",
                  "/fut/sbc/gen4/set/images/")
    challenge_prefixes=("/fut/sbc/gen4/challenges/images/",
                        "/fut/sbc/gen4/challenge/images/")
    is_tile=low.startswith(tile_prefix)
    is_set=low.startswith(set_prefixes)
    is_challenge=low.startswith(challenge_prefixes)
    if not (is_tile or is_set or is_challenge) or not low.endswith(".png"):
        return None
    artwork_dir=os.path.join(FUT_CONTENT_DIR,"fut","sbc","gen4","tile")
    requested=os.path.basename(low)
    available={name.lower():name for name in os.listdir(artwork_dir)} \
              if os.path.isdir(artwork_dir) else {}
    selected=available.get(requested)
    if selected is None:
        # Older controllers synthesize their own filename from assetId. Match
        # the numeric identity even when their prefix differs from ours.
        numeric=_re.findall(r"\d+",requested)
        if numeric:
            suffix=numeric[-1]
            # The directory in the URL is a hint, not a guarantee: FIFA asks
            # for "challenges/images/sbc_set_image_101.png", where 101 is a
            # *set* asset id, and pinning the lookup to the challenge tiles
            # left that request unanswered. Try the hinted kind first, then
            # the other one, before giving up.
            challenge_name=("sbs_challenge_%s.png"%suffix).lower()
            set_name=("sbs_set_%s.png"%suffix).lower()
            order=((challenge_name,set_name) if is_challenge
                   else (set_name,challenge_name))
            for name in order:
                selected=available.get(name)
                if selected is not None:
                    break
    if selected is None:
        return None
    with open(os.path.join(artwork_dir,selected),"rb") as handle:
        return handle.read()

def _fut_mirror(low):
    """Return mirrored FUT static-file bytes, or None when absent."""
    # FIFA requests this late-content localization database before the regular
    # FUT leaderboard messages.  Reuse the verified custom_messages document
    # and inject the official rarity-84 label on the path the client actually
    # consumes during bootstrap.  The November 2018 launch mirror has no
    # standalone dynamicLocDB file because PIM shipped in February 2019.
    dynamic_loc_sources={
        "/contentfifa/loc/gen4/dynamiclocdb-eng_us.xml":
            os.path.join(FUT_CONTENT_DIR,"fut","loc","PC",
                         "leaderboards.ENG_US.xml"),
        "/contentfifa/loc/gen4/dynamiclocdb-ita_it.xml":
            os.path.join(FUT_CONTENT_DIR,"fut","loc","PC",
                         "leaderboards.ITA_IT.xml"),
    }
    dynamic_source=dynamic_loc_sources.get(low)
    if (dynamic_source is None and _re.fullmatch(
            r"/contentfifa/loc/gen4/dynamiclocdb-[a-z]{3}_[a-z]{2}\.xml",
            low)):
        # A Polish client requested its locale-specific document and received
        # 404.  The message ids are language-independent, so reuse the complete
        # English document when a localized mirror is absent and still inject
        # the LocalFUT-only rows below.
        dynamic_source=os.path.join(
            FUT_CONTENT_DIR,"fut","loc","PC","leaderboards.ENG_US.xml")
    if dynamic_source is not None and os.path.isfile(dynamic_source):
        with open(dynamic_source,"rb") as handle:
            return _inject_local_fut_text(handle.read())
    rel = low.lstrip("/")
    if not rel:
        return None
    # A late standalone PIM ID cannot resolve against this client's launch
    # player table.  Its native DTO therefore uses a private version-encoded
    # resource ID. Keep the HTTP identity unique; the deterministic resolver
    # below maps it to the verified exact PIM player head.
    playerhead_match=_re.fullmatch(
        r"(fut/playerheads/g4/single/p)(\d+)(\.dds)",rel)
    requested_playerhead=(int(playerhead_match.group(2))
                          if playerhead_match else 0)
    full = os.path.normpath(os.path.join(FUT_CONTENT_DIR, rel))
    root = os.path.normpath(FUT_CONTENT_DIR)
    if not (full == root or full.startswith(root + os.sep)):
        return None  # Path-traversal guard.
    if playerhead_match:
        full=_resolved_playerhead_path(requested_playerhead,root)
    localized_pack_fallback=False
    localized_fut_fallback=False
    if full is not None and not os.path.isfile(full):
        if _re.fullmatch(
                r"/fut/packs/loc/storepackdescriptions\.[a-z]{2}_[a-z]{2}\.xml",
                low):
            full=os.path.join(
                FUT_CONTENT_DIR,"fut","packs","loc",
                "storepackdescriptions.en_us.xml")
            localized_pack_fallback=True
        elif _re.fullmatch(
                r"/fut/loc/pc/leaderboards\.[a-z]{3}_[a-z]{2}\.xml",low):
            full=os.path.join(
                FUT_CONTENT_DIR,"fut","loc","PC","leaderboards.ENG_US.xml")
            localized_fut_fallback=True
    if ((not playerhead_match and os.path.isfile(full)) or
            (playerhead_match and full is not None and
             _valid_playerhead_file(full))):
        with open(full, "rb") as handle:
            data=handle.read()
        if (localized_pack_fallback or
                low.endswith(("/storepackdescriptions.en_us.xml",
                              "/storepackdescriptions.it_it.xml"))):
            data=_inject_local_player_pick_text(data)
        if (localized_fut_fallback or
                low.endswith(("/leaderboards.eng_us.xml",
                              "/leaderboards.ita_it.xml"))):
            data=_inject_local_fut_text(data)
        return data
    return None


# Display label for every card rarity our packs can produce. The launch
# localization only names the rarities that existed in September 2018, so the
# later promotions reach the reveal board as a generic "SPECIAL ITEM". The
# labels are the promotion names EA used, taken from the rarity identities in
# futitemraritytunables.json.
_LOCAL_RARITY_LABELS={
    3:"TEAM OF THE WEEK",
    5:"TEAM OF THE YEAR",
    12:"ICON",
    21:"ONES TO WATCH",
    22:"ULTIMATE SCREAM",
    24:"SBC REWARD",
    28:"AWARD WINNER",
    30:"FUT BIRTHDAY",
    32:"FUTMAS",
    42:"PLAYER OF THE MONTH",
    43:"PLAYER OF THE MONTH",
    44:"UEL SBC",
    46:"UEL LIVE",
    48:"UEFA CHAMPIONS LEAGUE",
    50:"UCL LIVE",
    51:"FLASHBACK",
    52:"FUT SWAP DEALS",
    63:"SWAP DEALS REWARD",
    64:"TOTY NOMINEE",
    66:"TEAM OF THE SEASON",
    68:"UEL TEAM OF THE TOURNAMENT",
    69:"UCL SBC",
    70:"UCL TEAM OF THE GROUP STAGE",
    71:"FUTURE STARS",
    72:"CARNIBALL",
    78:"UEFA EUROPA LEAGUE",
    83:"FUTURE STARS NOMINEE",
    84:"PRIME ICON MOMENTS",
    85:"HEADLINERS",
}
# Rarity 84 is presented to this client under the supported Icon
# classification, so its short forms deliberately keep the Icon label.
_LOCAL_RARITY_SHORT_OVERRIDES={84:"ICON"}
# The SBC eligibility formatter dispatches PLAYER_RARITY (key 18) through the
# jump-table entry at CardsDLL+0x22c1d7 and formats ``Player_Rarity_%d`` from
# the literal at +0x3776b0. Without that exact key, TOTS rarity 66 falls back
# to "Special Item Players" even though the eligibility value is correct.
# The other three forms remain for the separate card and pack presentations.
_RARITY_KEY_FORMS=("Player_Rarity_%d","ITEM_RARITY_%d",
                   "dut_item_rarity_name_%d","LOC_ITEM_RARITY_%d")


def _local_rarity_localization():
    """Build the (key, label) rows published for every packable rarity."""
    rows=[]
    for rarity in sorted(_LOCAL_RARITY_LABELS):
        label=_LOCAL_RARITY_LABELS[rarity]
        short=_LOCAL_RARITY_SHORT_OVERRIDES.get(rarity,label)
        for form in _RARITY_KEY_FORMS:
            rows.append((form % rarity,label if form in {
                "Player_Rarity_%d","ITEM_RARITY_%d"} else short))
    return tuple(rows)


def _inject_local_fut_text(data):
    """Add late official FUT strings absent from the launch localization."""
    text=data.decode("utf-8-sig")
    missing=[]
    # The Season names are added first. Live 2026-09-12 the carousel drew
    # `*SEASON_LOC_19010`, FIFA's missing-string marker, although the served
    # document carried the key: it sat at entry 215 of 315, behind 116
    # rarity rows. If the native parser bounds the message set, the last
    # block appended is the one that falls off, so the block the screen
    # needs goes first.
    for key,value in season_localizations()+_local_rarity_localization():
        pattern=(r'(<locstring\s+id="%s"[^>]*>)(.*?)(</locstring>)' %
                 _re.escape(key))
        text,count=_re.subn(
            pattern,
            lambda match:match.group(1)+value+match.group(3),
            text,flags=_re.DOTALL)
        if count == 0:
            missing.append((key,value))
    if missing:
        if "  </message_set>" not in text:
            raise ValueError("invalid FUT localization document")
        # This document is CRLF and the appended rows were LF, the only
        # structural difference between an original entry and ours.
        newline=("\r\n" if "\r\n" in text else "\n")
        rows="".join(
            ('    <message>%(nl)s'
             '      <locstring id="%(key)s">%(value)s</locstring>%(nl)s'
             '    </message>%(nl)s') %
            {"nl":newline,"key":key,"value":value}
            for key,value in missing)
        text=text.replace("  </message_set>",rows+"  </message_set>",1)
    return text.encode("utf-8")


def _english_store_pack_units():
    """Return every Store pack unit of the English description document."""
    path=os.path.join(FUT_CONTENT_DIR,"fut","packs","loc",
                      "storepackdescriptions.en_us.xml")
    with open(path,encoding="utf-8") as handle:
        return _re.findall(
            r'<trans-unit resname="FUT_STORE_PACK_[^"]+">.*?</trans-unit>',
            handle.read(),flags=_re.DOTALL)


def _inject_local_player_pick_text(data):
    """Overlay local Store offers while preserving every official EA string."""
    replacements={
        "FUT_STORE_PACK_4002_DESC":(
            "Contains 20 Player Picks. Each pick offers 1 of 3 players rated "
            "81 or higher, including eligible special campaign items. All "
            "selected players are untradeable."),
        "FUT_STORE_PACK_4002_DESC_MOBILE":(
            "20 individual 1 of 3 81+ untradeable Player Picks."),
        "FUT_STORE_PACK_4002_NAME":"20X 81+ PLAYER PICK BUNDLE",
        "FUT_STORE_PACK_4002_NAME_MOBILE":"20X 81+ PLAYER PICK BUNDLE",
        "FUT_STORE_PACK_4008_DESC":(
            "Includes 1 of 3 Icons (Baby, Mid, or Prime version)."),
        "FUT_STORE_PACK_4008_DESC_MOBILE":(
            "Choose 1 of 3 Baby, Mid, or Prime Icons."),
        "FUT_STORE_PACK_4008_NAME":"1 OF 3 ICON PLAYER PICK",
        "FUT_STORE_PACK_4008_NAME_MOBILE":"1 OF 3 ICON PLAYER PICK",
    }
    text=data.decode("utf-8")
    for key,value in replacements.items():
        pattern=(r'(<trans-unit\s+resname="%s">\s*<source>)(.*?)(</source>)' %
                 _re.escape(key))
        text,count=_re.subn(pattern,lambda match:match.group(1)+value+match.group(3),
                            text,count=1,flags=_re.DOTALL)
        if count != 1:
            raise ValueError("missing Store localization key %s" % key)
    # Local-only offers use collision-free IDs and are appended at runtime.
    # In particular, the campaign experiments formerly occupied official
    # FIFA 19 IDs 403/404/405 (Mega/Rare Mega/Ultimate Pack).
    local_offers={
        406:("SINGLE ICON PACK",
             "Contains 1 guaranteed untradeable Icon player.",
             "1 guaranteed untradeable Icon player."),
        414:("PRIME ICON MOMENTS PACK",
             "Contains 1 guaranteed untradeable Prime Icon Moments player.",
             "1 guaranteed untradeable Prime Icon Moments player."),
        9403:("TOTY JUMBO RARE PLAYERS PACK",
              "Includes 24 Rare Gold players with an increased chance of a Team of the Year player.",
              "24 Rare Gold Players, TOTY Chance"),
        # These two guaranteed their promotion until 2026-09-03, which made
        # them strictly the best buy of the Jumbo promo group. Every Jumbo
        # pack now advertises the same chance.
        9404:("FUT BIRTHDAY JUMBO RARE PLAYERS PACK",
              "Includes 24 Rare Gold players with an increased chance of a FUT Birthday player.",
              "24 Rare Gold Players, FUT Birthday Chance"),
        9405:("TOTS JUMBO RARE PLAYERS PACK",
              "Includes 24 Rare Gold players with an increased chance of a Team of the Season player.",
              "24 Rare Gold Players, TOTS Chance"),
        9406:("TEAM OF THE YEAR PLAYER PACK",
              "Contains 1 guaranteed untradeable Team of the Year player.",
              "1 untradeable TOTY player guaranteed"),
        9407:("TEAM OF THE SEASON PLAYER PACK",
              "Contains 1 guaranteed untradeable Team of the Season player.",
              "1 untradeable TOTS player guaranteed"),
        9408:("PARTY BAG PLAYER PACK",
              "Contains 1 guaranteed untradeable special player from Carniball, "
              "FUT Birthday, Headliners or Ultimate Scream.",
              "1 untradeable special player guaranteed"),
        9409:("PLAYER PICK BUNDLE",
              "Contains 12 Player Picks. Each pick offers 1 of 4 players rated "
              "78 or higher. All selected players are untradeable.",
              "12 individual 1 of 4 78+ untradeable Player Picks."),
    }
    local_rows=[]
    # A localized document trails the English one. Live v1 2026-09-12 in
    # Italian, three Promo packs drew without a name: packs 407-410 (Ones to
    # Watch, Future Stars, FUTMAS and Carniball Jumbo Rare Players) exist only
    # in `storepackdescriptions.en_us.xml`, and a pack with no unit renders
    # blank. Carry every missing unit over in English instead.
    for unit in _english_store_pack_units():
        resname=_re.search(r'resname="([^"]+)"',unit).group(1)
        if 'resname="%s"' % resname not in text:
            local_rows.append("      %s\n" % unit)
    for pack_id,(name,description,mobile_description) in local_offers.items():
        if ('resname="FUT_STORE_PACK_%d_NAME"' % pack_id in
                text+"".join(local_rows)):
            continue
        local_rows.extend((
            '      <trans-unit resname="FUT_STORE_PACK_%d_DESC">\n'
            '        <source>%s</source>\n'
            '      </trans-unit>\n' % (pack_id,description),
            '      <trans-unit resname="FUT_STORE_PACK_%d_DESC_MOBILE">\n'
            '        <source>%s</source>\n'
            '      </trans-unit>\n' % (pack_id,mobile_description),
            '      <trans-unit resname="FUT_STORE_PACK_%d_NAME">\n'
            '        <source>%s</source>\n'
            '      </trans-unit>\n' % (pack_id,name),
            '      <trans-unit resname="FUT_STORE_PACK_%d_NAME_MOBILE">\n'
            '        <source>%s</source>\n'
            '      </trans-unit>\n' % (pack_id,name),
        ))
    if local_rows:
        if "    </body>" not in text:
            raise ValueError("invalid Store localization document")
        text=text.replace("    </body>","".join(local_rows)+"    </body>",1)
    return text.encode("utf-8")

def fut_route(method, path, body):
    low = path.lower().split("?")[0]
    if method == "GET" and low == "/localfut19/health":
        return J({"ready":True})
    sbc_artwork=_sbc_tile_artwork(low)
    if sbc_artwork is not None:
        return sbc_artwork
    squad_battle_artwork=_squad_battle_artwork(low)
    if squad_battle_artwork is not None:
        return squad_battle_artwork
    season_card=_season_card_asset(low)
    if season_card is not None:
        return season_card
    # Serve real FUT static content from the CDN mirror before empty fallbacks.
    mirror = _fut_mirror(low)
    if mirror is not None:
        return mirror
    # A missing static asset must return a real 404. Returning the `{}` JSON
    # fallback (200, two bytes) makes the client decode it as DDS/BIG/XML and
    # can leave the hub in an inconsistent loading state.
    if _static_content_type(low) is not None:
        return b""
    try: payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception: payload = {}

    if (not EXPERIMENTAL_CHAMPIONS_ENABLED and
            (low.startswith("/ut/game/fifa19/champion/") or
             (low == "/ut/game/fifa19/match" and method == "POST" and
              payload.get("championId") not in (None,"")))):
        # HTTP 480 is the native unavailable-match boundary already proved by
        # Online Single Match.  Return before any Champions state lookup or
        # mutation so pausing the mode cannot enroll an account or book a GID.
        log("champions","temporarily unavailable for first beta")
        return b"",480

    # Local destination advertised by SponsoredEvents.GetEventsURL. Onboarding
    # does not need its content; the endpoint only needs to be valid and finish
    # immediately without contacting external EA services.
    if low == "/sponsored-events": return b""

    # --- POW / Origin in-game store ---
    # These resources are JSON lists. Returning `{}` makes the frontend treat
    # the page as unavailable and start an endless retry loop (catalogue plus
    # OriginGetProfile plus CensusData). An empty array correctly represents a
    # local store with no offers.
    if low == "/pow/store/game/fifa19/catalog/list": return J([])
    if _re.match(r"^/pow/store/game/fifa19/catalog/\d+/item/list$", low):
        return J([])
    if low == "/pow/inventory/item/list": return J([])
    if low == "/pow/user/friends": return J([])

    # UTAS authentication. The frontend derives both the base URL for later
    # requests and X-UT-SID from this response. The old `{}` fallback returned
    # HTTP 200 but left the session ID and hostname empty: enough to show
    # already-fetched bootstrap data, but not to open services such as
    # `loan/players` from the onboarding controller.
    if low == "/ut/auth":
        if method == "POST":
            return J({"protocol":"http",
                      "ipPort":"127.0.0.1:%d" % FUT_PORT,
                      "sid":LOCAL_UTAS_SID,
                      "nucleusPersonaId":PERSONA_ID})
        return J({})

    # --- account / user ---
    if low == "/ut/game/fifa19/user/accountinfo":
        return J(_native_account_info())
    if low == "/ut/game/fifa19/user/pidinfo" and method == "GET":
        # RC95 proved the stock view uses this country only for its regional
        # leaderboard and online privacy flow. The offline entry guard now
        # replaces that view before its action handler, so exposing a country
        # would retain data that the local competition neither needs nor owns.
        return J({})
    if low == "/ut/game/fifa19/usermassinfo":
        return J(_native_user_mass_info())
    if low == "/ut/game/fifa19/user": return J(_native_user())
    if low == "/ut/game/fifa19/userdata":
        return J({"onlineELORating":0,"onlineRatedUser":False,"accountResetCount":0})
    if low == "/localfut19/draft/opponent":
        # Local-only. The Draft AWAY guard reads the club the current round
        # must show: the client's own Offline Select controller carries owner
        # 0 and squad 0 and its participant provider returns NULL, so it draws
        # a built-in club unless the guard replaces the id.
        active=STATE.get(ACTIVE_DRAFT_MATCH_KEY,{})
        active=active if isinstance(active,dict) else {}
        live=(_OFFLINE_SELECT_CONTEXT.get("state") is STATE and
              _OFFLINE_SELECT_CONTEXT.get("mode")=="DRAFT" and
              bool(active) and not bool(active.get("completed",False)))
        return J({"teamId":int(active.get("teamId",0) or 0) if live else 0,
                  "round":int(active.get("round",0) or 0) if live else 0,
                  "name":str(active.get("opponentName","") or "") if live
                         else ""})
    if low == "/localfut19/profile":
        with STATE.lock:
            career=STATE.career_history()
            return J({"profileMode":STATE.account_mode(),
                      "clubName":STATE.club_name(),
                      "clubAbbr":STATE.club_abbr(),
                      "credits":STATE.credits(),
                      "draftTokens":STATE.draft_tokens(),
                      "clubItems":len(STATE.items_in_pile("club")),
                      "inventory":STATE.inventory_counts("club"),
                      "objectGrant":STATE.default_object_grant_status(),
                      "bonusPlayers":STATE.initial_bonus_player_grant_status(),
                      "squads":[row["squadName"] for row in STATE.squads()],
                      "record":STATE.record(),
                      "career":career,
                      "externalCoinAdjustmentAllowed":
                          STATE.external_coin_adjustment_allowed(),
                      "packProbabilityProfile":STATE.pack_probability_profile(),
                      "database":os.path.basename(STATE.path)})
    if low == "/localfut19/career":
        return J(STATE.career_history())
    if low == "/localfut19/tools/balance" and method == "POST":
        operation=str(payload.get("operation","") or "").strip().lower()
        try: amount=int(payload.get("amount",0))
        except (TypeError,ValueError): amount=0
        if operation not in ("addcoins","setcoins","adddrafttokens"):
            return J({"success":False,"error":"unsupported balance operation"})
        if operation=="addcoins" and amount<=0:
            return J({"success":False,"error":"coin amount must be positive"})
        if operation=="setcoins" and amount<0:
            return J({"success":False,"error":"coin balance cannot be negative"})
        if operation=="adddrafttokens" and amount<=0:
            return J({"success":False,
                      "error":"Draft Token amount must be positive"})
        if not STATE.external_coin_adjustment_allowed():
            return J({"success":False,
                      "error":"RTG mode blocks external balance adjustments"})
        try:
            if operation=="adddrafttokens":
                return J({"success":True,"operation":operation,
                          "draftTokens":STATE.add_draft_tokens(amount)})
            credits=(STATE.add_credits(amount,external=True)
                     if operation=="addcoins" else
                     STATE.set_credits(amount,external=True))
        except PermissionError:
            return J({"success":False,
                      "error":"RTG mode blocks external balance adjustments"})
        return J({"success":True,"operation":operation,
                  **_credits_payload(credits)})
    if low == "/ut/game/fifa19/user/credits": return J(_credits_payload())
    # Keep this exact route ahead of the broad ``/club`` item-search prefix.
    # TOTW navigation requests /clubUser; treating that path as /club returned
    # the complete player inventory instead of FutGetClubUsersServerResponse.
    # Its native decoder requires user[].persona/personaId/public.  An empty
    # list parses, but leaves the transition without a club owner to select.
    if low == "/ut/game/fifa19/clubuser":
        return J(_native_club_users(include_totw_owner=True))
    # Selecting the TOTW tile next resolves that owner through GetClubInfo.
    # The generic two-byte fallback parsed as an object with no ``user`` array;
    # the subsequent provider dereferenced an uninitialised element and the
    # native client attempted to execute an address in heap memory.
    if low == "/ut/game/fifa19/user/list" and method == "GET":
        query=parse_qs(urlsplit(path).query)
        raw_ids=[]
        for value in query.get("personaIdList",query.get("personaidlist",[])):
            raw_ids.extend(part for part in str(value).split(",") if part)
        return J(_native_club_info_users(raw_ids))
    if low == "/ut/game/fifa19/user/club" and method in ("PUT","POST"):
        prof = STATE.update_profile(payload)
        return J({"clubId":CLUB_ID,"clubName":prof["clubName"],"clubAbbr":prof["clubAbbr"],
                  "personaId":PERSONA_ID,"personaName":prof["personaName"],"credits":STATE.credits()})

    # --- clientdata ---
    if low.startswith("/ut/game/fifa19/clientdata/pilesize"):
        return J({"entries":[{"key":2,"value":100},{"key":4,"value":100}]})
    if low.startswith("/ut/game/fifa19/clientdata/userhubdata"):
        return J({"entries":[{"key":0,"value":13},{"key":1,"value":12}]})
    if low == "/ut/game/fifa19/clientdata/onboarding":
        if method in ("PUT","POST"):
            for entry in payload.get("entries",[]):
                try: key=int(entry.get("key")); value=int(entry.get("value"))
                except (AttributeError,TypeError,ValueError): continue
                if key == 0: STATE.set("onboarding_stage",value)
                elif key == 4: STATE.set("onboarding_aux_state",value)
        return J({"entries":_onboarding_client_entries()})
    if low == "/ut/game/fifa19/clientdata/totw":
        return J(_totw_clientdata())
    if low.startswith("/ut/game/fifa19/clientdata/"): return J({"entries":[]})
    if low == "/ut/game/fifa19/settings":
        return J({"configs":_native_settings_configs()})
    if low == "/ut/game/fifa19/eventfeed": return J({"event":[],"timestamp":now_s()})
    if low == "/ut/game/fifa19/user/dynamicobjectives":
        return J(_native_dynamic_objectives())
    objective_claim=_re.match(
        r"^/ut/game/fifa19/user/dynamicobjectives/claim/(\d+)$",low)
    if objective_claim and method == "POST":
        return J(_claim_objective(int(objective_claim.group(1))) or {"awards":[]})
    if low == "/ut/game/fifa19/user/dynamicobjectives/claim/all" and method == "POST":
        awards=[]; player_picks=[]
        for definition in OBJECTIVE_DEFS:
            claimed=_claim_objective(definition["id"])
            if claimed:
                awards.extend(claimed.get("awards",[]))
                player_picks.extend(claimed.get("playerPicks",[]))
        return J({"awards":awards,"playerPicks":player_picks,
                  "credits":STATE.credits()})

    # --- Offline Draft (native CardsDLL routes) ---
    if low in (
        "/ut/game/fifa19/squad/mode/draft/state",
        "/ut/game/fifa19/draft/mode/draft/state",
    ) and method == "GET":
        query=parse_qs(urlsplit(path).query)
        mode=str((query.get("mode") or ["SINGLE_PLAYER"])[0]).upper()
        response=_draft_state_payload(mode)
        # FutGetDraftCurrentStateServerResponse consumes an array containing
        # one state object. Its retail parser closes the object on token 0x0A
        # and then explicitly requires the enclosing array-close token 0x0D.
        # A root object reaches EOF instead and redispatches its final member.
        return J([_draft_wire_state(response)])

    draft_purchase=_re.match(
        r"^/ut/game/fifa19/(?:(?:squad|draft)/mode/)?purchase/mode/(\d+)/draft$",
        low)
    if draft_purchase and method == "POST":
        raw_currency=str(payload.get("currency",payload.get(
            "paymentType","DRAFT_TOKEN"))).upper()
        use_coins=payload.get("useCoins",payload.get("useCredits",False))
        try: use_coins=bool(int(use_coins))
        except (TypeError,ValueError): use_coins=bool(use_coins)
        currency="COINS" if use_coins or "COIN" in raw_currency else "DRAFT_TOKEN"
        # Retail uses mode/1 for Single Player and mode/0 for Online.  The
        # path is authoritative; native requests do not include ``mode`` in
        # the purchase body.
        native_mode=int(draft_purchase.group(1))
        if native_mode==1:
            mode="SINGLE_PLAYER"
        elif native_mode==0:
            # Online Draft is deliberately unavailable in Local FUT 19.  Do
            # not create a session and, critically, do not debit any balance.
            return J([_draft_purchase_result("ONLINE")])
        else:
            mode=str(payload.get("mode","SINGLE_PLAYER")).upper()
        session=STATE.start_draft(mode,currency,15000,secrets.randbits(62))
        if session is None:
            return J([_draft_purchase_result(mode)])
        # The array root is live-proved: the object root freezes FIFA.  The
        # body is the current Draft state, because the only function in this
        # build that decodes COINS, DRAFT_TOKEN and POINTS is the Draft-state
        # decoder itself; see `_draft_purchase_result`.
        return J([_draft_purchase_result(mode)])

    draft_detail=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft$",low)
    if draft_detail:
        route_token=int(draft_detail.group(1))
        session=_draft_session_for_route(route_token)
        session_id=int(session["id"]) if session else 0
        if method == "GET":
            # The retail GetDraftAward call uses this exact GET route once the
            # session reaches READY_FOR_REWARDS.  Before then, keep returning
            # the resumable state for compatibility with local callers.
            if session and session.get("state")=="READY_FOR_REWARDS":
                claimed=STATE.claim_draft_reward(session_id)
                if claimed is None:
                    return J({"success":False,"errorCode":461,"awards":[]})
                return J({"success":True,
                          **_draft_reward_wire(claimed,session_id),
                          **_credits_payload()})
            return J(_draft_state_payload(
                (session or {}).get("mode",_draft_route_mode(route_token) or
                                    "SINGLE_PLAYER"),session))
        if method == "DELETE":
            abandoned=(STATE.abandon_draft(session_id) if session_id else None)
            if abandoned is None:
                return J({"success":False,"errorCode":461})
            # The retirement controller, like GetDraftAward, advances from the
            # successful HTTP operation and does not consume a result DTO. A
            # project-authored JSON receipt committed READY_FOR_REWARDS but
            # left retail indefinitely on Loading Ultimate Team. Keep the
            # durable state server-side and return the native empty success.
            return b""

    draft_choices=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft/choices/"
        r"(player|captain|formation|difficulty|manager)$",low)
    if draft_choices and method in ("GET","PUT"):
        session=_draft_session_for_route(draft_choices.group(1))
        if not session: return J({"choices":[],"errorCode":461})
        choice_type=_DRAFT_CHOICE_TYPES[draft_choices.group(2)]
        query=parse_qs(urlsplit(path).query)
        body_slot=payload.get("positionId",payload.get("positionid",
                  payload.get("slot",payload.get("choiceIndex",0))))
        try: slot=int((query.get("slot") or query.get("choiceIndex") or
                       [body_slot])[0])
        except (TypeError,ValueError): slot=0
        if choice_type == "PLAYER_DRAFT":
            row,row_slot=_draft_position_choices(session,slot)
            if row is None or row_slot is None:
                return J({"choices":[],"errorCode":461})
            return J(_draft_choice_payload(
                session,choice_type,row_slot,position_id=slot))
        return J(_draft_choice_payload(session,choice_type,slot))

    draft_choose=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft/choose$",low)
    if draft_choose and method in ("POST","PUT"):
        session=_draft_session_for_route(draft_choose.group(1))
        if not session: return J({"success":False,"errorCode":461})
        _ensure_draft_choices(session)
        raw_type=str(payload.get("choiceType",payload.get("type",""))).upper()
        aliases={**_DRAFT_CHOICE_TYPES,
                 **{value:value for value in _DRAFT_CHOICE_TYPES.values()}}
        choice_type=aliases.get(raw_type.lower(),aliases.get(raw_type,""))
        rows=STATE.draft_picks(int(session["id"]))
        if not choice_type:
            # Retail PUT /draft/choose sends only choiceIndex and positionId.
            # The active persisted state is therefore the authoritative type;
            # falling back to the first pending row keeps legacy POST callers
            # and partially migrated sessions resumable.
            active_state=str(session.get("state") or "").upper()
            if (active_state=="PICK_DIFFICULTY" and
                    not any(key in payload for key in
                            ("difficulty","difficultyName","value"))):
                # Retail chooses difficulty through its dedicated endpoint.
                # A formation PUT has the same bare choiceIndex/positionId
                # shape, so never consume it as an accidental difficulty.
                return J({"success":False,"errorCode":461,"choices":[]})
            if active_state in _DRAFT_CHOICE_TYPES.values():
                choice_type=active_state
            else:
                order=("PICK_DIFFICULTY","FORMATION_DRAFT","CAPTAIN_DRAFT",
                       "PLAYER_DRAFT","MANAGER_DRAFT")
                choice_type=next((kind for kind in order if any(
                    row["choice_type"]==kind and not row.get("selected_value")
                    for row in rows)),"PICK_DIFFICULTY")
        native_position=payload.get("positionId",payload.get("positionid",0))
        try: native_position=int(native_position or 0)
        except (TypeError,ValueError): native_position=0
        try: slot=int(payload.get("slot",payload.get("round",0)) or 0)
        except (TypeError,ValueError): slot=0
        if choice_type == "PLAYER_DRAFT":
            row,slot=_draft_position_choices(session,native_position)
            if row is None or slot is None:
                return J({"success":False,"errorCode":461,"choices":[]})
            rows=STATE.draft_picks(int(session["id"]))
        row=next((value for value in rows if value["choice_type"]==choice_type and
                  int(value["slot"])==slot),None)
        selected=payload.get("resourceId",payload.get("itemId",payload.get(
            "formation",payload.get("difficulty",payload.get("selectedValue")))))
        if selected in (None,"") and row:
            try: index=int(payload.get("choiceIndex",payload.get("selectedIndex",0)))
            except (TypeError,ValueError): index=0
            choices=row.get("choices",[])
            if 0<=index<len(choices):
                choice=choices[index]
                selected=(choice.get("resourceId",choice.get("formation",
                          choice.get("difficulty",choice.get("id",index))))
                          if isinstance(choice,dict) else choice)
        chosen=STATE.choose_draft_pick(
            int(session["id"]),choice_type,slot,
            selected if selected is not None else "",
            position_id=(native_position if choice_type=="CAPTAIN_DRAFT" else None),
            swap_player_def_ids=payload.get("swapPlayerDefIds",
                                            payload.get("swapplayerdefids")))
        if chosen is None:
            return J({"success":False,"errorCode":461,"choices":[]})
        if method == "PUT":
            # FutPickDraftChoiceServerResponse accepts the retail empty object
            # and the controller advances from its local choice. Persistence
            # above is the only server-side effect required on this path.
            return J({})
        response=_draft_state_payload(session["mode"],STATE.draft_session(int(session["id"])))
        response.update({"success":True,"choice":chosen,"selectedChoice":chosen})
        return J(response)

    if low in ("/ut/game/fifa19/squad/mode/draft/choose/difficulty",
               "/ut/game/fifa19/draft/mode/draft/choose/difficulty") and method in ("POST","PUT"):
        session=STATE.current_draft("SINGLE_PLAYER")
        if not session: return J({"success":False,"errorCode":461})
        _ensure_draft_choices(session)
        difficulty_names={"BEGINNER":1,"AMATEUR":2,"SEMIPRO":3,
                          "SEMI_PRO":3,"PRO":4,"PROFESSIONAL":4,"WORLDCLASS":5,
                          "WORLD_CLASS":5,"LEGENDARY":6,"ULTIMATE":7}
        raw_name=str(payload.get("difficultyName","") or "").upper()
        value=(difficulty_names.get(raw_name) if raw_name else
               payload.get("difficulty",payload.get("value")))
        try: value=int(value)
        except (TypeError,ValueError):
            return J({"success":False,"errorCode":461})
        if value not in range(1,8):
            return J({"success":False,"errorCode":461})
        chosen=STATE.choose_draft_pick(int(session["id"]),"PICK_DIFFICULTY",0,value)
        # Answer with the Draft state, exactly like the purchase route: this
        # controller has one decoder, `CardsDLL+0x255350`, and a two-byte `{}`
        # gives it nothing to commit. Match Preview kept showing `Ultimate`
        # after WORLDCLASS was chosen and reported `matchDifficulty: 6` at
        # match end, so the client never took the choice.
        if method=="PUT":
            if not chosen:
                return J({"success":False,"errorCode":461})
            return J([_draft_wire_state(_draft_state_payload("SINGLE_PLAYER"))])
        return J({"success":bool(chosen),"difficulty":int(value),
                  "draftState":STATE.draft_session(int(session["id"]))["state"]})

    draft_autocomplete=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft/autocomplete$",low)
    if draft_autocomplete and method in ("POST","PUT"):
        session=_draft_session_for_route(draft_autocomplete.group(1))
        if not session: return J({"success":False,"errorCode":461})
        _ensure_draft_choices(session)
        session=STATE.reconcile_draft_state(int(session["id"]))
        if str(session.get("state") or "")=="PLAYER_DRAFT":
            try: captain_position=int(session.get("captainPositionId"))
            except (TypeError,ValueError): captain_position=-1
            if not 0<=captain_position<=22:
                return J({"success":False,"errorCode":461})
            # Complete My Squad arrives immediately after the captain. The
            # eager persisted rows are position-agnostic placeholders; turn
            # each remaining native slot into the same role-correct row that
            # an interactive pick would have requested before selecting it.
            for position in range(23):
                if position==captain_position:
                    continue
                row,_row_slot=_draft_position_choices(session,position)
                if not row or len(row.get("choices",[]))<1:
                    return J({"success":False,"errorCode":461})
        completed=STATE.autocomplete_draft(int(session["id"]))
        if not completed or str(completed.get("state") or "")!="READY_FOR_MATCH":
            return J({"success":False,"errorCode":461})
        if method=="PUT":
            # Live CardsDLL identifies this as PickAutoChoice. Its mandatory
            # body accepted the two-byte object before the missing route sent
            # the client back to Hub; persistence above is the omitted effect.
            return J({})
        response=_draft_state_payload(completed["mode"],completed)
        response["success"]=True
        return J(response)

    draft_stats=_re.match(
        r"^/ut/game/fifa19/(?:squad/)?draft/mode/(\d+)/stats$",low)
    if draft_stats and method == "GET":
        route_token=int(draft_stats.group(1))
        mode=_draft_route_mode(route_token)
        # mode/0 is the disabled Online Draft namespace.  Do not leak the
        # Single Player aggregate into its invalid response: callers that
        # probe the hidden route must see an empty Online history.
        history=_draft_history_payload(
            "ONLINE" if route_token==0 else mode or "SINGLE_PLAYER")
        session=_draft_session_for_route(route_token)
        if not session:
            return J({"id":route_token,"draftId":0,
                      "mode":mode or "ONLINE",
                      "draftState":"INVALID","gamesWon":0,"wins":0,
                      "losses":0,"rewardClaimed":False,
                      **history,
                      **_draft_reward_wire({},0)})
        session_id=int(session["id"])
        wins=int(session.get("wins",0) or 0)
        losses=int(session.get("losses",0) or 0)
        return J({"id":route_token,"draftId":session_id,
                  "mode":str(session.get("mode") or "SINGLE_PLAYER"),
                  "draftState":str(session.get("state") or "INVALID"),
                  "gamesWon":wins,"wins":wins,"losses":losses,
                  "rewardClaimed":bool(session.get("reward_claimed")),
                  **history,
                  **_draft_reward_wire(session.get("rewardReceipt"),
                                       session_id)})

    draft_award=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft/grant/award$",low)
    if draft_award and method in ("POST","GET"):
        session=_draft_session_for_route(draft_award.group(1))
        claimed=(STATE.claim_draft_reward(int(session["id"]))
                 if session else None)
        if claimed is None:
            return J({"success":False,"errorCode":461,"awards":[]})
        return J(_draft_award_wire(claimed))
    if low in ("/ut/game/fifa19/squad/mode/grant/award",
               "/ut/game/fifa19/draft/mode/grant/award") and method in ("POST","GET"):
        session=(STATE.current_draft("SINGLE_PLAYER") or
                 STATE.latest_draft("SINGLE_PLAYER"))
        if not session: return J({"success":False,"errorCode":461,"awards":[]})
        claimed=STATE.claim_draft_reward(int(session["id"]))
        if claimed is None:
            return J({"success":False,"errorCode":461,"awards":[]})
        return b""

    # --- Team of the Week challenge ---
    if low == "/ut/game/fifa19/squad/mode/totw":
        _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="TOTW")
        if method == "GET": return J(_totw_payload())
        if method in ("POST","PUT"):
            current=int(STATE.get("totw_difficulty",3) or 3)
            try: difficulty=int(payload.get("difficulty",current) or current)
            except (TypeError,ValueError): difficulty=current
            difficulty=max(1,min(len(TOTW_DIFFICULTIES),difficulty))
            raw_challenge=payload.get("challengeId",payload.get("totwId",0))
            try: challenge_id=int(raw_challenge or 0)
            except (TypeError,ValueError): challenge_id=0
            if challenge_id >= 500000: challenge_id-=500000
            valid_ids={int(row.get("challengeId",0) or 0)
                       for row in _totw_payload().get("challenges",[])}
            if raw_challenge not in (None,"",0,"0") and challenge_id not in valid_ids:
                return J({"success":False,"errorCode":461})
            if "result" in payload:
                selected=challenge_id or int(
                    STATE.get("totw_challenge_id",0) or 0)
                if selected not in valid_ids:
                    return J({"success":False,"errorCode":461})
                operation_key=(payload.get("operationKey") or
                               payload.get("requestId") or payload.get("gid"))
                if operation_key in (None,""):
                    return J({"success":False,"errorCode":461,
                              "message":"TOTW completion requires a stable request id"})
                normalized=str(payload.get("result","")).upper()
                award=next(a for d,_n,a in TOTW_DIFFICULTIES if d==difficulty)
                reward=award
                try:
                    response=STATE.complete_totw_challenge(
                        selected,difficulty,normalized,reward,operation_key,
                        payload.get("home",payload.get("homeGoals",0)),
                        payload.get("away",payload.get("awayGoals",0)))
                except ValueError as exc:
                    return J({"success":False,"errorCode":461,
                              "message":str(exc)})
                # Publish the selection only after the idempotent completion
                # transaction succeeds. A rejected operation-key collision
                # must not silently switch the TOTW shown by the hub tile.
                STATE.set("totw_difficulty",difficulty)
                STATE.set("totw_challenge_id",selected)
                return J(response)
            STATE.set("totw_difficulty",difficulty)
            if challenge_id:
                STATE.set("totw_challenge_id",challenge_id)
            response=_totw_payload(); response["success"]=True
            return J(response)

    # --- FUT Champions (captured and statically closed entry sequence) ---
    # The RC74 trace contains the five read requests at network-log lines 364
    # and 367-370. Registration and prize use the POST executor at RVA
    # 0x2a4330; the request-object field previously treated as a method enum is
    # unrelated to the HTTP verb. Match creation and settlement remain
    # disconnected until their ownership contract is independently proved.
    if low == "/ut/game/fifa19/champion/user/hub" and method == "GET":
        snapshot=_champion_runtime_payload()
        log("champions","Hub event=%d registered=%s played=%d state=%s" % (
            int(snapshot["event"]["id"]),
            bool(snapshot["registered"]),
            int(snapshot["stat"]["gamesPlayed"]),
            str((snapshot["session"] or {}).get("state","AVAILABLE"))))
        return J(snapshot["hub"])
    if (low == "/ut/game/fifa19/champion/user/offline-entry" and
            method == "GET"):
        snapshot=_champion_runtime_payload()
        session=snapshot["session"]
        registration=(dict((session.get("data",{}) or {}).get(
            "registration",{}) or {}) if session is not None else {})
        # The process-side selector needs to resume an already locked run after
        # a launcher restart.  Return only the local state it owns; country and
        # the complete session snapshot never cross this control boundary.
        return J({
            "eventId":int(snapshot["event"]["id"]),
            "state":str(session.get("state","AVAILABLE")
                        if session is not None else "AVAILABLE"),
            "difficulty":int(session.get("difficulty",0) or 0)
                         if session is not None else 0,
            "offlineRegistered":bool(registration.get("offline",False)),
        })
    if (low == "/ut/game/fifa19/champion/user/registration" and
            method == "POST"):
        # CardsDLL+0x262d50 builds this suffix, +0x262d70 serializes the
        # NATION_ISO value under competitionCountryCode and the caller invokes
        # the POST executor at +0xf9e4b. The response parser at RVA 0x263050
        # consumes only competitionRegion.
        offline_mode=payload.get("offlineMode") is True
        offline_difficulty=0
        if offline_mode:
            try: offline_difficulty=int(payload.get("difficulty",0) or 0)
            except (TypeError,ValueError): offline_difficulty=0
            if offline_difficulty not in range(1,8):
                # The selector has no default. Reject before creating the
                # opponent snapshot so closing or corrupting the modal cannot
                # look like enrollment in subsequent Hub reads.
                log("champions","Offline registration rejected difficulty=%s" %
                    payload.get("difficulty"))
                return J({"success":False,"errorCode":461})
        country=str(payload.get("competitionCountryCode","") or "").upper()
        context=_champion_event_context()
        event_id=int(context["eventId"])
        session=STATE.champion_session(event_id=event_id)
        if session is None:
            session=STATE.start_champion_session(
                event_id,str(context["eventKey"]),int(context["expires"]),
                champion_opponents(event_id),rng_seed=event_id*97+19,
                event_attempt=int(context["eventAttempt"]))
        if offline_mode:
            session=STATE.register_champion_offline_session(
                int(session["id"]),offline_difficulty)
            if session is None:
                log("champions","Offline registration rejected difficulty=%s" %
                    payload.get("difficulty"))
                return J({"success":False,"errorCode":461})
            log("champions","Offline registration event=%d difficulty=%d" % (
                event_id,int(session["difficulty"])))
            return J({"success":True,"offlineCompetition":True,
                      "eventId":event_id,
                      "difficulty":int(session["difficulty"]),
                      "state":str(session["state"])})
        session=STATE.register_champion_session(
            int(session["id"]),country or CHAMPION_COMPETITION_COUNTRY_CODE,
            CHAMPION_COMPETITION_REGION)
        log("champions","Registration country=%s region=%s" % (
            country or "<empty>",CHAMPION_COMPETITION_REGION))
        return J(champion_registration_dto(
            competition_region=CHAMPION_COMPETITION_REGION))
    if low == "/ut/game/fifa19/champion/user/prize" and method == "POST":
        # The request builder at CardsDLL+0x257da0 serializes exactly
        # {idList:[eventId,...]}, and its caller invokes the POST executor at
        # +0xbeefb. Claim
        # only those durable events: treating a hub refresh as an implicit
        # claim would make rewards disappear before this response can present
        # them through FutGrantChampionsPrizeServerResponse (RVA 0x258350).
        requested=[]
        for value in payload.get("idList",[]) if isinstance(
                payload.get("idList"),list) else []:
            try: event_id=int(value)
            except (TypeError,ValueError): continue
            if event_id not in requested:
                requested.append(event_id)
        awarded=[]
        for event_id in requested:
            session=STATE.champion_session(event_id=event_id)
            if session is None:
                log("champions","Prize ignored unknown event=%d" % event_id)
                continue
            receipt=STATE.claim_champion_reward(int(session["id"]))
            if receipt is None:
                log("champions","Prize not ready event=%d state=%s" % (
                    event_id,str(session.get("state",""))))
                continue
            awarded.append(champion_awarded_prize_dto(
                awards=_champion_tier_awards(
                    int(receipt["wins"]),int(receipt["difficulty"]),
                    int(receipt["rank"])),
                event_id=event_id,rank=int(receipt["rank"]),
                tier_level=int(receipt["tierLevel"])))
            log("champions","Prize event=%d tier=%d rank=%d retry=%s" % (
                event_id,int(receipt["tierLevel"]),int(receipt["rank"]),
                bool(receipt.get("alreadyClaimed",False))))
        return J(champion_prize_grant_dto(awarded_prizes=awarded))
    if low == "/ut/game/fifa19/champion/user/stats" and method == "GET":
        # The live RC88 freeze occurred after this response because the current
        # user parser at +0x2531e0 decodes one object directly at +0x281270;
        # feeding it the StatsList array leaves that decoder waiting for an
        # object-close token. Enrollment remains exclusive to registration.
        return J(_champion_runtime_payload()["stat"])
    if low == "/ut/game/fifa19/champion/stats/list" and method == "GET":
        # Unlike current-user Stats, FutGetChampionsStatsListResponse loops over
        # an array at +0x26b369..+0x26b4d8.
        return J(champion_user_stats_dto([
            _champion_runtime_payload()["stat"]]))
    if low == "/ut/game/fifa19/champion/leaderboard" and method == "GET":
        # This offline competition owns no global or regional service. The
        # completion thunk at CardsDLL+0xc43c6 sends any non-zero transport
        # error to +0xc36e0, which builds the stock FUT_LB_NOTAVAILABLE popup;
        # a successful empty board would instead leave the unusable tab open.
        log("champions","blocked online leaderboard tab")
        return b"",480
    if (low == "/ut/game/fifa19/champion/type/week/period/curr/friends/stats"
            and method == "GET"):
        log("champions","blocked online friends leaderboard tab")
        return b"",480

    # --- Squad Battles ---
    if low == "/ut/game/fifa19/sqbt/user/hub" and method == "GET":
        return J(_sqbt_hub_wire(_sqbt_hub_payload()))
    if (method == "GET" and low.startswith("/ut/game/fifa19/sqbt/") and
            any(token in low for token in
                ("leaderboard","top100","top/100","standing","rank"))):
        # FutGetSquadBattleLeaderboardServerResponse, allocated at
        # CardsDLL+0x248370 (vtable 0x37b618) and parsed at +0x248400.  Its
        # FIFA 19 row decoder accepts badge/clubName/est/insetUrl/persona/rank
        # plus nested score and tiebreak objects.  Both nested values use the
        # exact {icon,value} decoder.  The Top 100 screen labels tiebreak as
        # Games Played, so use the durable fixture count there; a QUIT then
        # advances the visible count while correctly leaving score at zero.
        hub=_sqbt_hub_payload()
        rows=sqbt_leaderboard(int(hub["sqbtEventId"]),
                              STATE.persona_name(),int(hub["score"]),100,
                              rounds_played=int(hub.get(
                                  "matchesPlayed",0) or 0),
                              user_wins=int(hub.get("wins",0) or 0),
                              user_draws=int(hub.get("draws",0) or 0),
                              user_losses=int(hub.get("losses",0) or 0))
        entries=[]
        for position,row in enumerate(rows,1):
            persona=str(row.get("persona","") or "CPU")
            entries.append({
                "badge":0,
                "clubName":persona,
                "est":2026,
                "insetUrl":"",
                "persona":persona,
                "rank":position,
                "score":{"icon":"",
                         "value":int(row.get("value",0) or 0)},
                "tiebreak":{"icon":"","value":int(row.get(
                    "matchesPlayed",0) or 0)},
            })
        log("sqbt","Top 100 event=%d rows=%d userRank=%d score=%d" % (
            int(hub["sqbtEventId"]),len(entries),int(hub["rank"]),
            int(hub["score"])))
        return J({"entries":entries})
    if low == "/ut/game/fifa19/sqbt/user/prize" and method in ("GET", "POST"):
        current_event=_ensure_sqbt_event()
        event=STATE.claimable_sqbt_event()
        claimed=(STATE.claim_sqbt_reward(int(event["id"]))
                 if event is not None else None)
        # FutGetSquadBattlePrizeServerResponse, allocated at CardsDLL+0x24b7b0
        # (size 0x80) and parsed at +0x24b8c0. It decodes `awardedPrizes`,
        # whose rows carry `awards`, `eventId`, `rank` and `tierLevel`, plus
        # `prizesInError` and the response error state. `errorCode` and the
        # credit members are project-only and never reach the parser.
        hub=_sqbt_hub_payload()
        if claimed is None:
            return J({"awardedPrizes":[],"prizesInError":[],"errorType":0,
                      "eventId":int(current_event["id"]),
                      "rank":int(hub["rank"]),
                      "tierLevel":int(hub["userTierLevel"])})
        prize_awards=[]
        for award in claimed.get("awards",[]) or []:
            if not isinstance(award,dict):
                continue
            kind=str(award.get("type","") or "").lower()
            # The aggregate tier specification is the source for persistence,
            # but the claim scene needs one concrete visual award per pack and
            # per persisted Pick Item. A count=4 Player Pick with id zero
            # rendered neither the red picks nor their correct quantity.
            if kind in {"playerpick","player_pick"}:
                continue
            copies=(max(1,int(award.get("count",1) or 1))
                    if kind=="pack" else 1)
            for _ in range(copies):
                prize_awards.append(native_reward_dto(
                    {**award,"count":1 if kind=="pack" else
                     max(1,int(award.get("count",1) or 1))}))
        for pick in claimed.get("playerPicks",[]) or []:
            if not isinstance(pick,dict):
                continue
            prize_awards.append(native_reward_dto({
                "type":"playerPick","value":0,"count":1,
                "playerPickId":int(pick.get(
                    "playerPickId",pick.get("id",0)) or 0),
                "optionCount":int(pick.get("optionCount",len(
                    pick.get("options",[]) or [])) or 0),
                "label":str(pick.get("name","FUT Champions Player Pick")),
                "description":str(pick.get("description","") or
                    "Choose one untradeable FUT Champions TOTW player."),
                "untradeable":True,
            }))
        return J({"awardedPrizes":[{
                      "awards":prize_awards,
                      "eventId":int(event["id"]),
                      "rank":int(claimed.get("rank",0) or 0),
                      "tierLevel":int(claimed.get("tierLevel",0) or 0)}]
                  if prize_awards else [],"prizesInError":[],
                  "errorType":0,"eventId":int(event["id"]),
                  # The native scene maps tierLevel to its badge and rank
                  # label. These values must describe the finished event,
                  # never the newly-created competition returned by the hub.
                  "rank":int(claimed.get("rank",0) or 0),
                  "tierLevel":int(claimed.get("tierLevel",0) or 0)})
    if (low == "/ut/game/fifa19/sqbt/user/opponents/refresh" and
            method in ("GET","POST","PUT")):
        event=_ensure_sqbt_event()
        current_rotation=max([int(value.get("rotation",0) or 0)
                              for value in event.get("opponents",[])] or [0])
        pending=STATE.get(ACTIVE_SQBT_MATCH_KEY,{})
        match_in_progress=(isinstance(pending,dict) and
                           not bool(pending.get("completed",False)) and
                           int(pending.get("opponentId",0) or 0)>0)
        if match_in_progress or current_rotation>=SQBT_MAX_ROTATIONS-1:
            refreshed=event
        else:
            refreshed=_ensure_sqbt_event(current_rotation+1)
            STATE.set(SELECTED_SQBT_OPPONENT_KEY,0)
        return J(_sqbt_refresh_wire(refreshed))
    if (method == "GET" and low in {
            "/ut/game/fifa19/sqbt/user/opponents",
            "/ut/game/fifa19/sqbt/user/opponents/list",
            "/ut/game/fifa19/sqbt/user/opponentsquad",
            "/ut/game/fifa19/sqbt/user/selection"}):
        # Compatibility aliases used by the FIFA 18 controller and late
        # Companion builds. They return the same lightweight retail list as a
        # refresh without rotating the current four opponents.
        return J(_sqbt_refresh_wire(_ensure_sqbt_event()))
    selection_paths={
        "/ut/game/fifa19/sqbt/user/opponent",
        "/ut/game/fifa19/sqbt/user/opponents/select",
        "/ut/game/fifa19/sqbt/user/select",
        "/ut/game/fifa19/sqbt/user/selection",
        "/ut/game/fifa19/sqbt/user/start",
        "/ut/game/fifa19/sqbt/user/play",
    }
    sqbt_selection_request=(
        low in selection_paths or
        (low=="/ut/game/fifa19/sqbt/user/match" and
         not any(name in payload for name in
                 ("result","endReason","home","homeGoals","away",
                  "awayGoals"))))
    if method in ("POST","PUT") and sqbt_selection_request:
        _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SQBT")
        event=_ensure_sqbt_event()
        opponent=_sqbt_select_from_payload(payload,event)
        if opponent is None:
            return J({"success":False,"errorCode":461,
                      "reason":"INVALID_OR_PLAYED_OPPONENT"})
        return J({"success":True,**_sqbt_selection_response(opponent,event)})
    sqbt_opponent=_re.match(
        r"^/ut/game/fifa19/sqbt/user/opponentsquad/(\d+)$",low)
    if sqbt_opponent and method == "GET":
        _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SQBT")
        hub=_sqbt_hub_payload(); wanted=int(sqbt_opponent.group(1))
        opponent=next((value for value in hub["opponents"]
                       if int(value.get("opponentId",0))==wanted),None)
        if opponent is None:
            featured_opponent=_sqbt_featured_opponent(_ensure_sqbt_event())
            if int(featured_opponent.get("opponentId",0) or 0)==wanted:
                opponent=featured_opponent
        if not opponent: return J({"errorCode":461,"squad":{}})
        # Retail fetches the chosen opponent's squad immediately before the
        # match.  Latch it so CreateMatch can embed that exact CPU club without
        # having to guess the mode from the request body.
        if not bool(opponent.get("played")):
            STATE.set(SELECTED_SQBT_OPPONENT_KEY,wanted)
        else:
            STATE.set(SELECTED_SQBT_OPPONENT_KEY,0)
        squad=_sqbt_match_squad(opponent)
        # FutGetSquadBattleOpponentSquadServerResponse is a standard FUT squad
        # at the response root. The previous wrapper put the 23 players under a
        # nested squad member; FIFA still read the top-level name/rating but
        # found no top-level players, which exactly produced 23 grey
        # placeholders and aborted opponent selection back to the hub. FIFA
        # 18's retail-proven route has the same direct-root contract.
        log("sqbt","opponent squad id=%d players=%d manager=%d actives=%d" % (
            wanted,sum(isinstance(row.get("itemData"),dict)
                       for row in squad.get("players",[])),
            len(squad.get("manager",[])),len(squad.get("actives",[]))))
        return J(squad)
    if low == "/ut/game/fifa19/featuredsquad/user/stats" and method == "GET":
        # FutGetSquadBattleFeaturedServerResponse, allocated at
        # CardsDLL+0x249710 (size 0x28, vtable 0x37b730) and parsed at
        # +0x249790.  Decoding its field-id switch against the RS4 name table
        # gives a closed DTO of five members: `squadName` and
        # `bannerImageName` are strings (`+0x249a46` and `+0x249c4f` read into
        # `[response+0x1e0]` and `+0x1b8`), `endTimeStamp` is a 64-bit integer
        # (`+0x249a72` into `+0xe0`), and `difficultyBasedWinPoints` and
        # `difficultyBasedLossPoints` are arrays the parser caps at seven
        # dwords (`cmp rdi, 7` and `add rbx, 4` at `+0x249aa8`), one per
        # difficulty.  The previous reply carried none of them, so both strings
        # stayed empty.  The access violation reading 0x398 that followed this
        # reply was NOT caused by it: it is Origin::OriginSDK::GrantAchievement
        # at FIFA19.exe+0x1755e54, guarded in FRIDA_JS.
        event=_ensure_sqbt_event()
        feature_id,feature_squad=_sqbt_featured_squad(event)
        feature_opponent=_sqbt_featured_opponent(event)
        played=bool(feature_opponent.get("played"))
        return J({"featureConsumerId":"sqbt",
                  "featureId":feature_id,"featuredSquadId":feature_id,
                  "squadId":feature_id,"opponentId":feature_id,
                  "teamId":ICON_PRESENTATION_TEAM_ID,
                  "clubId":ICON_PRESENTATION_TEAM_ID,
                  "opponentTeamId":ICON_PRESENTATION_TEAM_ID,
                  "badgeId":ICON_PRESENTATION_TEAM_ID,
                  "opponentBadgeId":ICON_PRESENTATION_TEAM_ID,
                  "badgeAssetId":ICON_PRESENTATION_TEAM_ID,
                  "badgeResourceId":6000000+ICON_PRESENTATION_TEAM_ID,
                  "squadName":str((feature_squad or {}).get(
                      "squadName","") or FEATURED_SQUAD_NAME),
                  # A display string: the hub tile asset the client already
                  # downloads from /fut/sbc/gen4/tile/GameHub_SBS.png.
                  "bannerImageName":"GameHub_SBS",
                  "endTimeStamp":int(event["expires"]),
                  "rating":int((feature_squad or {}).get("rating",0) or 0),
                  "chemistry":int((feature_squad or {}).get(
                      "chemistry",100) or 0),
                  "played":played,"isPlayed":played,
                  "active":not played,"isActive":not played,
                  "difficultyBasedWinPoints":list(SQBT_WIN_POINTS),
                  "difficultyBasedLossPoints":list(SQBT_LOSS_POINTS)})
    featured=_re.match(r"^/ut/game/fifa19/featuredsquad/(\d+)$",low)
    if featured and method == "GET":
        feature_id=int(featured.group(1))
        sqbt_feature_id,sqbt_feature_squad=_sqbt_featured_squad()
        totw_challenge=_totw_challenge(feature_id)
        if feature_id == sqbt_feature_id:
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SQBT")
            squad=dict(sqbt_feature_squad)
            feature_opponent=_sqbt_featured_opponent()
            if not bool(feature_opponent.get("played")):
                STATE.set(SELECTED_SQBT_OPPONENT_KEY,feature_id)
        elif (totw_challenge and
              feature_id==int(totw_challenge.get("squadId",0) or 0)):
            # Challenge Select can move directly from one archived week to
            # another without posting /squad/mode/totw again.  The detailed
            # featured-squad request is therefore the authoritative native
            # selection signal consumed by the following CreateMatch.
            STATE.set("totw_challenge_id",int(
                totw_challenge.get("challengeId",0) or 0))
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="TOTW")
            squad=dict(totw_challenge.get("squad") or {})
            squad["squadType"]="FEATURED_SQUAD"
        else:
            resources=_source_backed_roster({"TOTS","TOTY","Icon"},85,99,
                                            seed=feature_id)
            squad=_native_cpu_squad(feature_id,"FIFA 19 Ultimate XI",resources,
                                    "f433","FEATURED_SQUAD")
        # FIFA 18 consumes the squad at the root while FIFA 19's local
        # compatibility paths already use `squad`/`featureSquad`. Publish the
        # same object in all three views so loading the Featured Squad always
        # primes the subsequent CreateMatch handoff.
        squad.update({"teamId":int(squad.get(
                          "teamId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "clubId":int(squad.get(
                          "clubId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "opponentTeamId":int(squad.get(
                          "opponentTeamId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "badgeId":int(squad.get(
                          "badgeId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "opponentBadgeId":int(squad.get(
                          "opponentBadgeId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "badgeAssetId":int(squad.get(
                          "badgeAssetId",ICON_PRESENTATION_TEAM_ID) or
                          ICON_PRESENTATION_TEAM_ID),
                      "badgeResourceId":6000000+ICON_PRESENTATION_TEAM_ID})
        return J({**squad,"featuredSquadId":feature_id,
                  "featureConsumerId":"sqbt",
                  "squad":squad,"featureSquad":squad})

    # Native FIFA 19 Player Pick flow, recovered from the official June 2019
    # Companion bundle.  Unopened subtype-237 misc items live in pile 6.  Only
    # after POST /item/{id} do their choices enter temporary storage.
    if low == "/ut/game/fifa19/playerpicks/pending" and method == "GET":
        active=_active_player_pick_payload()
        choices=active.get("options",[]) if active else []
        return J({"itemData":choices,
                  "duplicateItemIdList":_duplicate_item_links(choices)})
    native_player_pick_selection=_re.match(
        r"^/ut/game/fifa19/playerpicks/item/(\d+)/select$",low)
    if native_player_pick_selection and method == "POST":
        active=STATE.active_player_pick()
        if not active:
            return J({"success":False,"errorCode":461})
        selected_resource_id=int(native_player_pick_selection.group(1))
        selected_item_id=next((int(option.get("id",0) or 0)
            for option in active.get("options",[])
            if selected_resource_id in (
                int(option.get("resourceId",0) or 0),
                int(_native_player_item(option).get("resourceId",0) or 0))),0)
        chosen=STATE.select_player_pick(
            int(active.get("playerPickId",active.get("id",0)) or 0),
            selected_item_id,require_redeemed=True)
        if chosen is None:
            return J({"success":False,"errorCode":461})
        _acknowledge_repeatable_sbc_reward(active)
        # The native client wraps this top-level item in an array itself.
        return J(_native_player_item(chosen))

    # Compatibility aliases retained for objectives/tests from older local
    # builds. They expose unopened definitions, but the console uses the three
    # native routes above plus POST /item/{id} below.
    if low in ("/ut/game/fifa19/playerpick",
               "/ut/game/fifa19/playerpicks",
               "/ut/game/fifa19/user/playerpicks",
               "/ut/game/fifa19/purchased/playerpick",
               "/ut/game/fifa19/purchased/playerpicks") and method == "GET":
        picks=[_native_player_pick_payload(pick)
               for pick in STATE.pending_player_picks()]
        return J({"playerPicks":picks,"itemData":picks,"count":len(picks)})
    player_pick_selection=_re.match(
        r"^/ut/game/fifa19/(?:user/|purchased/)?playerpicks?/(\d+)(?:/select)?$",
        low)
    if player_pick_selection and method == "GET":
        requested=int(player_pick_selection.group(1))
        picks=STATE.pending_player_picks()
        pick=next((row for row in picks
                   if int(row.get("playerPickId",0) or 0)==requested),None)
        if pick is None and requested in (PLAYER_PICK_RESOURCE_ID,
                                          PLAYER_PICK_DEFINITION_ID):
            pick=next(iter(picks),None)
        pick=_native_player_pick_payload(pick)
        return J({"playerPicks":[pick] if pick else [],
                  "itemData":[pick] if pick else [],"count":1 if pick else 0})
    if player_pick_selection and method in ("POST","PUT"):
        requested=int(player_pick_selection.group(1))
        if requested in (PLAYER_PICK_RESOURCE_ID,PLAYER_PICK_DEFINITION_ID):
            first=next(iter(STATE.pending_player_picks()),None)
            if first:
                requested=int(first.get("playerPickId",first.get("id",0)) or 0)
        selected=(payload.get("selectedItemId",payload.get("itemId",payload.get("id",0)))
                  if isinstance(payload,dict) else 0)
        if isinstance(payload,dict) and isinstance(payload.get("selectedItem"),dict):
            selected=payload["selectedItem"].get(
                "itemId",payload["selectedItem"].get("id",selected))
        pick_before=next((row for row in STATE.pending_player_picks()
                          if int(row.get("playerPickId",row.get("id",0)) or 0)
                          ==requested),None)
        chosen=STATE.select_player_pick(
            requested,int(selected or 0))
        if chosen is None:
            return J({"success":False,"errorCode":461,"itemData":[]})
        _acknowledge_repeatable_sbc_reward(pick_before or {})
        return J({"success":True,"itemData":[chosen],"selectedItem":chosen})

    # --- First FUT launch: kits and badge ---
    # GET /onboarding/kits used to be absorbed by the `{}` fallback: FIFA made
    # nine itemless tiles and CardsDLL dereferenced a null pointer on selection.
    # Collection names come from the native DLL contract, not the Web App.
    if low == "/ut/game/fifa19/onboarding/kits":
        if method == "GET":
            return J(_native_onboarding_kits())
        if method in ("POST","PUT"):
            home_id=payload.get("homeKitId",payload.get("home_kit_id",0))
            away_id=payload.get("awayKitId",payload.get("away_kit_id",0))
            home=_onboarding_candidate(home_id,"home")
            away=_onboarding_candidate(away_id,"away")
            if not home or not away:
                return J({"success":False,"errorCode":461})
            STATE.set("onboarding_home_kit_id",home["id"])
            STATE.set("onboarding_away_kit_id",away["id"])
            return J({"homeKitId":home["id"],"awayKitId":away["id"]})
    if low in ("/ut/game/fifa19/onboarding/badges",
               "/ut/game/fifa19/onboarding/badge") and method == "GET":
        return J(_native_onboarding_badges())
    badge_match=_re.match(r"^/ut/game/fifa19/onboarding/badge(?:/(\d+))?$",low)
    if badge_match and method in ("POST","PUT"):
        badge_id=badge_match.group(1) or payload.get("badgeId",payload.get("badge_id",0))
        badge=_onboarding_candidate(badge_id,"badge")
        if not badge:
            return J({"success":False,"errorCode":461})
        STATE.set("onboarding_badge_id",badge["id"])
        return J({"badgeId":badge["id"]})
    if low == "/ut/game/fifa19/onboarding/badges" and method in ("POST","PUT"):
        badge_id=payload.get("badgeId",payload.get("badge_id",0))
        badge=_onboarding_candidate(badge_id,"badge")
        if not badge:
            return J({"success":False,"errorCode":461})
        STATE.set("onboarding_badge_id",badge["id"])
        return J({"badgeId":badge["id"]})

    # --- Squads ---
    if low in ("/ut/game/fifa19/squad/list","/ut/game/fifa19/squad") and method == "GET":
        return J(_native_squad_list_payload())
    public_squad = _re.match(
        r"^/ut/game/fifa19/squad/(\d+)/user/(-?\d+)$", low)
    if public_squad and method == "GET":
        requested_squad=int(public_squad.group(1))
        requested_persona=int(public_squad.group(2))
        draft_opponent=_draft_opponent_from_squad_id(requested_squad)
        if (draft_opponent is not None and requested_persona in
                (0,PERSONA_ID,0x7fffffffffffffff)):
            return J(draft_opponent)
        challenge=_totw_challenge(requested_squad)
        featured_persona=0x7fffffffffffffff
        if (challenge and requested_squad==int(
                challenge.get("squadId",0) or 0) and
                requested_persona in (PERSONA_ID,featured_persona)):
            # GetSquadInfo passes this same root through the full squad decoder.
            # Both supported URL forms identify the same public featured squad.
            # Both URL spellings feed the public featured-squad cache.  The
            # decoded root must therefore retain its sentinel owner; persona 0
            # is rejected by the retail RetrieveSquad boundary before Offline
            # Select can publish the opponent.  Keep the independently proven
            # regular/non-concept flags while isolating that owner correction.
            # Match preview also consumes the opponent's presentation objects,
            # so publish source-backed kits/badge/manager without borrowing or
            # changing any active item from the user's club.
            # This is the other native path used after selecting an archived
            # challenge.  Persist it before returning the validated public
            # squad so Match Preview and CreateMatch use the same week.
            STATE.set("totw_challenge_id",int(
                challenge.get("challengeId",0) or 0))
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="TOTW")
            return J(_totw_public_squad_dto(
                requested_squad,challenge.get("squad") or {},
                challenge.get("name")))
        return J({})
    active_user = _re.match(
        r"^/ut/game/fifa19/squad/active/user/(-?\d+)$", low)
    if active_user and method == "GET":
        requested_persona=int(active_user.group(1))
        if requested_persona==PERSONA_ID:
            # FutRetrieveSquadServerResponse first reads personaId/squadType
            # from the response root, then passes the same root reader to the
            # native squad decoder. A nested ``squad`` object is ignored and
            # leaves the selected active-squad cache empty.
            return J(_native_squad_json(STATE.active_squad()))
        return J({"personaId":requested_persona,"squad":{}})
    if low == "/ut/game/fifa19/squad/active" and method == "GET":
        return J(_native_squad_json(STATE.active_squad()))

    # CardsDLL exposes FutSquadCopy/Delete/Rename/SetData alongside the list and
    # save calls.  Without these routes the request fell through to the generic
    # "{} with 200" branch, so a copy appeared to succeed while doing nothing and
    # the client mapped the empty result onto FUT_MAX_NUM_SQUAD_REACHED, which is
    # a copy outcome and not a squad count.  The binary does spell the copy URL
    # out after all: CardsDLL pairs the suffix "/clone" with the response type
    # RS4:FutSquadCopyServerResponse, exactly as it pairs "/list" with
    # RS4:FutSquadListServerResponse.  /copy was a guess and is kept only so an
    # older client spelling still works.  Any unmatched squad route is still
    # logged loudly.
    m = _re.match(r"^/ut/delete/game/fifa19/squad/(\d+)$", low)
    if m or (method == "DELETE" and
             _re.match(r"^/ut/game/fifa19/squad/(\d+)$", low)):
        sid = int((m or _re.match(r"^/ut/game/fifa19/squad/(\d+)$", low)).group(1))
        try:
            removed = STATE.delete_squad(sid)
        except ValueError as exc:
            if "Concept Squads" in str(exc):
                return J(_concept_squads_disabled_payload())
            return J({"errorCode":461,"reason":str(exc)})
        if not removed:
            return J({"errorCode":404,"reason":"squadNotFound"})
        return J(_native_squad_list_payload())

    m = _re.match(r"^/ut/game/fifa19/squad/(?:(\d+)/)?(?:clone|copy)$", low)
    if m and method in ("POST","PUT"):
        if STATE._is_concept_squad_data(payload):
            return J(_concept_squads_disabled_payload())
        source = m.group(1) or payload.get("sourceSquadId") or payload.get("squadId")
        try:
            created = STATE.copy_squad(
                source,payload.get("squadName"),MAX_SQUADS)
        except ValueError as exc:
            if "Concept Squads" in str(exc):
                return J(_concept_squads_disabled_payload())
            return J({"success":False,"errorCode":461,
                      "reason":"invalidSquad","message":str(exc)})
        if created is None:
            return J({"errorCode":460,"maxSquads":MAX_SQUADS,
                      "reason":"maxSquadsReached"})
        STATE.set("objective_squads_created",
                  int(STATE.get("objective_squads_created",0) or 0)+1)
        return J(_native_squad_json(created))

    m = _re.match(r"^/ut/game/fifa19/squad/(\d+)/(?:active|setactive|select)$", low)
    if m and method in ("POST","PUT"):
        try:
            active = STATE.set_active_squad(int(m.group(1)))
        except ValueError as exc:
            if "Concept Squads" in str(exc):
                return J(_concept_squads_disabled_payload())
            return J({"errorCode":461,"reason":str(exc)})
        if active is None:
            return J({"errorCode":404,"reason":"squadNotFound"})
        return J(_native_squad_list_payload())

    m = _re.match(r"^/ut/game/fifa19/squad/(\d+)/(?:rename|name)$", low)
    if m and method in ("POST","PUT"):
        sid = int(m.group(1))
        for s in STATE.squads():
            if s.get("id") == sid:
                s["squadName"] = str(payload.get("squadName",
                                                 payload.get("name", s.get("squadName","Squad"))))
                return J(_native_squad_json(STATE.save_squad(s)))
        return J({"errorCode":404,"reason":"squadNotFound"})

    m = _re.match(r"^/ut/game/fifa19/squad/(\d+)$", low)
    if m:
        sid = int(m.group(1))
        if method == "GET":
            draft_opponent=_draft_opponent_from_squad_id(sid)
            if draft_opponent is not None:
                return J(draft_opponent)
            draft_session_id=sid-700000
            draft_session=(STATE.draft_session(draft_session_id)
                           if draft_session_id>0 else None)
            if (draft_session and not
                    (str(draft_session.get("state",""))=="COMPLETED_DRAFT" and
                     int(draft_session.get("reward_claimed",0) or 0)==1)):
                return J(_draft_native_squad(draft_session))
            if STATE.is_concept_squad(sid):
                return J(_concept_squads_disabled_payload())
            for s in STATE.squads():
                if s.get("id")==sid: return J(_native_squad_json(s))
            return J(_native_squad_json({"id":sid,"players":[]}))
        if method in ("PUT","POST"):
            draft_session_id=sid-700000
            draft_session=(STATE.draft_session(draft_session_id)
                           if draft_session_id>0 else None)
            if (draft_session and str(payload.get("squadType","")).upper()==
                    "DRAFT_SQUAD"):
                saved=STATE.save_draft_squad_layout(draft_session_id,payload)
                return J(_draft_native_squad(saved or draft_session))
            if (sid>=700000 or
                    str(payload.get("squadType","")).upper()=="DRAFT_SQUAD"):
                return J({"errorCode":461,"reason":"invalidSquadType"})
            payload["id"]=sid
            try:
                saved=STATE.save_squad(payload)
            except ValueError as exc:
                if "Concept Squads" in str(exc):
                    return J(_concept_squads_disabled_payload())
                return J({"success":False,"errorCode":461,
                          "reason":"invalidSquad","message":str(exc)})
            # The live My Squads selection saves the opened squad with PUT but
            # omits `active`. Persist the route target as the selection itself;
            # otherwise relogging returns the only row still marked active,
            # normally Starter Italy.
            try:
                saved=STATE.set_active_squad(sid) or saved
            except ValueError:
                pass
            STATE.set("objective_squad_saves",
                      int(STATE.get("objective_squad_saves",0) or 0)+1)
            return J(_native_squad_json(saved))
    if low == "/ut/game/fifa19/squad" and method == "POST":
        if len(STATE.squads()) >= MAX_SQUADS:
            return J({"errorCode":460,"maxSquads":MAX_SQUADS})
        # "Start from Scratch" posts the squad the player was looking at, id and
        # all: a live capture shows {"id": 1, "squadName": "prova 2 team",
        # "players": []} arriving here while squad 1 was the only one in the
        # club.  Honouring that id renamed and emptied the existing squad
        # instead of adding a second one.  The HTTP method is what separates
        # the two operations, exactly as the client uses them: POST /squad
        # creates, PUT /squad/{id} updates.
        payload = dict(payload)
        payload.pop("id", None); payload.pop("squadId", None)
        try:
            saved = STATE.save_squad(payload)
        except ValueError as exc:
            if "Concept Squads" in str(exc):
                return J(_concept_squads_disabled_payload())
            return J({"success":False,"errorCode":461,
                      "reason":"invalidSquad","message":str(exc)})
        # A live Start from Scratch capture enters the newly allocated squad
        # immediately, but sends neither an `active` member nor a later
        # set-active request. Persist that observed selection here; otherwise
        # the next active-squad lookup still returns the older Starter Italy.
        active=STATE.set_active_squad(saved["id"])
        STATE.set("objective_squads_created",
                  int(STATE.get("objective_squads_created",0) or 0)+1)
        return J(_native_squad_json(active or saved))

    # --- Club (owned items) ---
    if low == "/ut/game/fifa19/club/stats/staff":
        return J(_club_staff_stats_payload())
    if low.startswith("/ut/game/fifa19/club/stats/consumables"):
        return J(_club_consumable_stats_payload())
    if low == "/ut/game/fifa19/club/stats/year":
        return J(_club_year_stats_payload())
    if low.startswith("/ut/game/fifa19/club/stats"):
        return J(_club_year_stats_payload())
    if low.startswith("/ut/game/fifa19/club/consumables"):
        raw_query=parse_qs(urlsplit(path).query)
        query={str(key).lower():value for key,value in raw_query.items()}
        suffix=low[len("/ut/game/fifa19/club/consumables"):].strip("/")
        wanted=suffix or _query_first(query,"type",default="consumable")
        return J(_club_consumables_payload(wanted,query))
    if low.startswith("/ut/game/fifa19/club"):
        raw_query=parse_qs(urlsplit(path).query)
        query={str(key).lower():value for key,value in raw_query.items()}
        wanted=str((query.get("type") or ["player"])[0]).lower()
        player_types={"player","playergk","nogk","gk","rb","cb","lb","rwb","lwb",
                      "cdm","cm","cam","rm","lm","rw","lw","cf","st",
                      "goalkeeper","defender","midfielder","forward","attacker"}
        if wanted in player_types:
            items=[_native_player_item(x)
                   for x in STATE.items_in_pile("club","player")]
            if wanted == "playergk" or wanted == "gk":
                items=[x for x in items if x.get("preferredPosition")=="GK"]
            elif wanted == "nogk":
                items=[x for x in items if x.get("preferredPosition")!="GK"]
            elif wanted not in ("player","playergk","nogk"):
                broad_positions={
                    "goalkeeper":{"GK"},
                    "defender":{"RB","RWB","CB","LB","LWB"},
                    "midfielder":{"CDM","CM","CAM","RM","LM"},
                    "forward":{"RW","LW","CF","ST"},
                    "attacker":{"RW","LW","CF","ST"},
                }
                accepted=broad_positions.get(wanted,{wanted.upper()})
                items=[x for x in items if str(x.get("preferredPosition","")) in accepted]

            def query_value(*names):
                for name in names:
                    values=query.get(name.lower())
                    if values and str(values[0]).strip() not in ("","0","any","-1"):
                        return str(values[0]).strip()
                return ""

            # FIFA 19 uses both position and zone.  Zone is the broad
            # goalkeeper/defender/midfielder/forward selector shown in the
            # advanced search UI.
            position=query_value("position")
            zone=query_value("zone")
            if position:
                normalized=position.upper()
                zone_map={"GK":{"GK"},"DEF":{"RB","RWB","CB","LB","LWB"},
                          "MID":{"CDM","CM","CAM","RM","LM"},
                          "ATT":{"RW","LW","CF","ST"}}
                accepted=zone_map.get(normalized,{normalized})
                items=[x for x in items if str(x.get("preferredPosition","")) in accepted]
            if zone:
                zone_map={"0":{"GK"},"1":{"RB","RWB","CB","LB","LWB"},
                          "2":{"CDM","CM","CAM","RM","LM"},
                          "3":{"RW","LW","CF","ST"},
                          "GK":{"GK"},"DEF":{"RB","RWB","CB","LB","LWB"},
                          "MID":{"CDM","CM","CAM","RM","LM"},
                          "ATT":{"RW","LW","CF","ST"}}
                accepted=zone_map.get(zone.upper())
                if accepted:
                    items=[x for x in items if str(x.get("preferredPosition","")) in accepted]

            numeric_filters=(
                (("nationality","nation","nationid"),"nation"),
                (("league","leagueid"),"leagueId"),
                (("club","clubid","teamid"),"teamid"),
                (("defid","assetid"),"assetId"),
            )
            for aliases,field in numeric_filters:
                raw=query_value(*aliases)
                if raw:
                    try: expected=int(raw)
                    except ValueError: continue
                    items=[x for x in items if int(x.get(field,0) or 0)==expected]

            quality=str((query.get("level") or query.get("quality") or
                         [""])[0]).strip().lower()
            if quality and quality not in {"any","-1"}:
                if quality in ("bronze","0"):
                    items=[x for x in items
                           if int(x.get("rating",0) or 0)<65 and
                           card_revision(int(x.get("resourceId",0) or 0))=="Normal"]
                elif quality in ("silver","1"):
                    items=[x for x in items
                           if 65<=int(x.get("rating",0) or 0)<75 and
                           card_revision(int(x.get("resourceId",0) or 0))=="Normal"]
                elif quality in ("gold","2"):
                    items=[x for x in items
                           if int(x.get("rating",0) or 0)>=75 and
                           card_revision(int(x.get("resourceId",0) or 0))=="Normal"]
                elif quality in ("special","3"):
                    items=[x for x in items
                           if card_revision(int(x.get("resourceId",0) or 0))!="Normal"]
            rare_filter=str((query.get("rare") or [""])[0]).strip().lower()
            if rare_filter and rare_filter not in {"any","-1"}:
                if rare_filter in ("sp","special"):
                    items=[x for x in items
                           if card_revision(int(x.get("resourceId",0) or 0))!="Normal"]
                elif rare_filter in ("1","true","yes","rare"):
                    items=[x for x in items if int(x.get("rareflag",0) or 0)>0]
                elif rare_filter in ("0","false","no","common"):
                    items=[x for x in items if int(x.get("rareflag",0) or 0)==0]

            # The advanced Club search sends these independently from the
            # normal rating direction.  Preserve zero/false here because they
            # are meaningful values for the tradeability selector.
            untradeable_raw=str((query.get("untradeable") or
                                 query.get("isuntradeable") or [""])[0]).strip().lower()
            if untradeable_raw in {"1","true","yes","only","untradeable"}:
                items=[x for x in items if bool(x.get("untradeable",False))]
            elif untradeable_raw in {"0","false","no","tradeable","tradable"}:
                items=[x for x in items if not bool(x.get("untradeable",False))]

            name_key=lambda x:str(
                x.get("lastName",x.get("name",""))).casefold()
            tie_key=lambda x:(name_key(x),
                              int(x.get("resourceId",0) or 0),
                              int(x.get("id",0) or 0))
            acquired=str((query.get("acquireddate") or [""])[0]).strip().lower()
            sell_value=str((query.get("sellvalue") or [""])[0]).strip().lower()
            rating_sort=str((query.get("sort") or ["desc"])[0]).strip().lower()
            ascending={"asc","ascending","lowtohigh","low_to_high","1"}
            descending={"desc","descending","hightolow","high_to_low","-1"}
            if acquired in ascending | descending:
                reverse=acquired in descending
                items.sort(key=lambda x:(int(x.get("timestamp",0) or 0),
                                         int(x.get("rating",0) or 0),
                                         *tie_key(x)),reverse=reverse)
            elif sell_value in ascending | descending:
                reverse=sell_value in descending
                items.sort(key=lambda x:(int(x.get("discardValue",0) or 0),
                                         int(x.get("rating",0) or 0),
                                         *tie_key(x)),reverse=reverse)
            else:
                reverse=rating_sort not in ascending
                items.sort(key=lambda x:(int(x.get("rating",0) or 0),
                                         *tie_key(x)),reverse=reverse)
        else:
            items=_club_object_items(wanted,query)
            if stored_item_kind({"itemType":wanted}) == "consumable" or                     wanted in ("consumable","consumables"):
                _annotate_consumable_counts(items)
        return J(_club_page_payload(items,query))
    if low == "/ut/game/fifa19/purchased/items" and method == "GET":
        items=(_pending_player_pick_items()+
               [_native_item(x) for x in STATE.items_in_pile("purchased")])
        return J({"itemData":items,
                  "duplicateItemIdList":_duplicate_item_links(items)})

    # --- hub / record ---
    if low == "/ut/game/fifa19/hub":
        STATE.settle_market()
        r = STATE.record()
        # FutGetHubDataServerResponse owns four adjacent Squad Battles scalars.
        # The featured ID alone is insufficient: the retail Single Player hub
        # does not load /featuredsquad/{id} unless the event/opponent context is
        # also valid.  That featured response populates the native squad cache
        # subsequently consumed by FUT_TOTW_TILE_DATA_DP.
        sqbt=_sqbt_hub_payload()
        opponents=sqbt.get("opponents",[])
        opponent_id=int((opponents[0] if opponents else {}).get(
            "sqbtOpponentSquadId",0) or 0)
        featured,_featured_squad=_sqbt_featured_squad()
        remaining=max(0,int(sqbt.get("_endTime",0) or 0)-now_s())
        market_counts=STATE.market_counts()
        payload={"credits":STATE.credits(),"won":r["wins"],"draw":r["draws"],
                 "lost":r["losses"],
                 **_draft_history_payload("SINGLE_PLAYER"),
                 "tradePileSize":market_counts["tradePile"],
                 "watchListSize":market_counts["watchList"],
                 "activeAuctions":market_counts["active"],
                 "sqbtEventId":int(sqbt.get("sqbtEventId",0) or 0),
                 "sqbtOpponentSquadId":opponent_id,
                 "featuredSquadId":featured,
                 "sqbtMatchDifficulty":int(STATE.get(
                     "sqbt_difficulty",1) or 1),
                 "secondsUntilEnd":remaining,"untilEndSeconds":remaining,
                 "timeUntilEnd":remaining,
                 "nextFeatureSquadTimeStamp":now_s()+remaining}
        offline_season=season_hub_payload(STATE.offline_season())
        if offline_season is not None:
            payload["offlineSeason"]=offline_season
        return J(payload)

    # --- Transfer market / trade pile / watch list ---
    # The retail PC client calls this operation ``offer``; companion clients
    # and older route fixtures call the same auction operation ``bid``.
    trade_bid=_re.match(r"^/ut/game/fifa19/trade/(\d+)/(?:bid|offer)$",low)
    trade_watch=_re.match(r"^/ut/game/fifa19/trade/(\d+)/watch$",low)
    watch_entry=_re.match(r"^/ut/game/fifa19/watchlist/(\d+)$",low)
    clear_trade=_re.match(r"^/ut/game/fifa19/trade/(\d+)$",low)

    if (low == "/ut/delete/game/fifa19/trade/sold" and
            method in ("GET","POST","DELETE")):
        STATE.settle_market()
        cleared=STATE.clear_sold_market_trades()
        log("market","clear sold count=%d" % int(cleared["cleared"]))
        # FutDeleteSoldItems has a fieldless response in this client. Keep the
        # two-byte body observed live while performing the durable mutation.
        return J({})

    if low == "/ut/game/fifa19/auctionhouse" and method in ("POST","PUT"):
        item_doc=payload.get("itemData",{}) if isinstance(payload,dict) else {}
        if isinstance(item_doc,list): item_doc=item_doc[0] if item_doc else {}
        try:
            item_id=int((item_doc or {}).get("id",payload.get("itemId",
                        payload.get("id",0))) or 0)
            starting=int(payload.get("startingBid",payload.get("startingbid",150)) or 150)
            buy_now=int(payload.get("buyNowPrice",payload.get("buyNow",
                        payload.get("buynow",starting))) or starting)
            duration=int(payload.get("duration",payload.get("expires",3600)) or 3600)
        except (TypeError,ValueError):
            item_id=0; starting=buy_now=duration=0
        item=STATE.item(item_id) if item_id else None
        if not item:
            return J({"success":False,"errorCode":461,"auctionInfo":[],
                      **_credits_payload()})
        listed=STATE.list_market_item(item_id,starting,buy_now,duration)
        log("market","list itemId=%d start=%d buyNow=%d duration=%d result=%s" %
            (item_id,starting,buy_now,duration,
             listed.get("tradeId") if listed else "rejected"))
        if not listed:
            return J({"success":False,"errorCode":470,"auctionInfo":[],
                      **_credits_payload()})
        return J({"success":True,"auctionInfo":[_native_market_auction(listed)],
                  **_credits_payload()})

    if low in ("/ut/game/fifa19/auctionhouse/relist",
               "/ut/game/fifa19/tradepile/relist") and method in ("POST","PUT"):
        try: requested=int(payload.get("tradeId",0) or 0)
        except (TypeError,ValueError): requested=0
        candidates=([requested] if requested else
            [row["tradeId"] for row in STATE.market_auctions(True)
             if row["state"]=="expired"])
        relisted=[]
        for trade_id in candidates:
            result=STATE.relist_market_trade(trade_id)
            if result: relisted.append(result)
        log("market","relist requested=%s completed=%d" %
            (requested or "all",len(relisted)))
        return J({"success":bool(relisted),"relisted":relisted,
                  "auctionInfo":[_native_market_auction(STATE.market_auction(
                      row["tradeId"])) for row in relisted],**_credits_payload()})

    if trade_bid and method in ("POST","PUT"):
        trade_id=int(trade_bid.group(1))
        snapshot=(resolve_player_listing(trade_id) or
                  STATE.market_ai_snapshot(trade_id))
        if snapshot is None:
            return J({"success":False,"errorCode":461,"auctionInfo":[],
                      **_credits_payload()})
        try:
            amount=int(payload.get("bid",payload.get("bidAmount",
                       payload.get("currentBid",0))) or 0)
        except (TypeError,ValueError): amount=0
        buy_now=int(snapshot.get("buyNowPrice",0) or 0)
        is_buy_now=bool(payload.get("buyNow",payload.get("isBuyNow",False))) or \
                   (buy_now>0 and amount>=buy_now)
        context_now=time.monotonic()
        contextual_sbc_buy=(is_buy_now and
            _SBC_MARKET_CONTEXT.get("state") is STATE and
            float(_SBC_MARKET_CONTEXT.get("until",0.0))>=context_now and
            float(_SBC_MARKET_CONTEXT.get("trades",{}).get(
                trade_id,0.0))>=context_now)
        result=(STATE.buy_now_market_listing(snapshot) if is_buy_now else
                STATE.bid_market_listing(snapshot,amount))
        log("market","%s tradeId=%d amount=%d result=%s" %
            ("buy-now" if is_buy_now else "bid",trade_id,amount,
             result.get("status") if result else "rejected"))
        if not result:
            return J({"success":False,"errorCode":470,"auctionInfo":[],
                      **_credits_payload()})
        if contextual_sbc_buy and result.get("item"):
            _SBC_MARKET_CONTEXT["trades"].pop(trade_id,None)
            _SBC_MARKET_CONTEXT["until"]=0.0
            moved=STATE.move_item(int(result["item"]["id"]),"club")
            if moved:
                result=dict(result); result["item"]=moved
                log("market","SBC Buy Now assigned directly to club itemId=%d" %
                    int(moved["id"]))
        # Idempotent retries must describe the item's current pile, not the
        # receipt snapshot captured before the contextual move.
        if result.get("itemId"):
            current_item=STATE.item(int(result["itemId"]))
            if current_item:
                result=dict(result); result["item"]=current_item
        STATE.settle_market()
        auction=STATE.market_auction(trade_id)
        response={"success":True,"auctionInfo":[_native_market_auction(auction)],
                  **_credits_payload(result.get("credits",STATE.credits()))}
        if result.get("item"):
            response["itemData"]=[_native_item(result["item"])]
        return J(response)

    if ((trade_watch and method in ("POST","PUT")) or
            (low == "/ut/game/fifa19/watchlist" and method in ("POST","PUT"))):
        try:
            trade_id=(int(trade_watch.group(1)) if trade_watch else
                      int(payload.get("tradeId",0) or 0))
        except (TypeError,ValueError): trade_id=0
        snapshot=(resolve_player_listing(trade_id) or
                  STATE.market_ai_snapshot(trade_id))
        watched=STATE.watch_market_listing(snapshot) if snapshot else None
        log("market","watch tradeId=%d result=%s" %
            (trade_id,"ok" if watched else "rejected"))
        return J({"success":bool(watched),"errorCode":0 if watched else 461,
                  "tradeId":trade_id,"watched":bool(watched),**_credits_payload()})

    if watch_entry and method == "DELETE":
        trade_id=int(watch_entry.group(1))
        result=STATE.unwatch_market_trade(trade_id)
        return J({"success":bool(result),"errorCode":0 if result else 470,
                  "tradeId":trade_id,"watched":False,**_credits_payload()})

    if clear_trade and method == "DELETE":
        trade_id=int(clear_trade.group(1))
        STATE.settle_market()
        result=STATE.clear_market_trade(trade_id)
        log("market","clear tradeId=%d result=%s" %
            (trade_id,"ok" if result else "rejected"))
        return J({"success":bool(result),"errorCode":0 if result else 470,
                  "tradeId":trade_id,**_credits_payload()})

    if low.startswith("/ut/game/fifa19/watchlist") and method == "GET":
        STATE.settle_market()
        return J(_market_collection(STATE.market_targets()))

    if low == "/ut/game/fifa19/trade/status" and method == "GET":
        STATE.settle_market()
        query=parse_qs(urlsplit(path).query)
        raw_ids=[]
        for value in query.get("tradeIds",query.get("tradeids",[])):
            raw_ids.extend(str(value).split(","))
        requested=[]
        for value in raw_ids:
            try: trade_id=int(value)
            except (TypeError,ValueError): continue
            if trade_id>0 and trade_id not in requested:
                requested.append(trade_id)
        auctions=[]
        for trade_id in requested:
            auction=STATE.market_auction(trade_id)
            if auction is not None and str(auction.get("state",""))!="cleared":
                auctions.append(auction)
        return J(_market_collection(auctions,
                                    requestedTradeIds=requested))

    if low.startswith("/ut/game/fifa19/tradepile") and method == "GET":
        STATE.settle_market()
        persisted=STATE.market_auctions(True)
        listed_item_ids={int(row["itemId"]) for row in persisted}
        # Preserve pre-market trade-pile items. They remain clearable/movable
        # inventory and are never silently converted into active auctions.
        for item in STATE.items_in_pile("trade"):
            if int(item["id"]) in listed_item_ids: continue
            persisted.append({"tradeId":0,"itemId":int(item["id"]),
                "resourceId":int(item.get("resourceId",0) or 0),
                "itemData":item,"sellerIsUser":True,"tradeState":"inactive",
                "bidState":"none","expires":-1,"startingBid":0,
                "currentBid":0,"buyNowPrice":0,"offers":0})
        return J(_market_collection(persisted))

    if low.startswith("/ut/game/fifa19/transfermarket") and method == "GET":
        market_payload=_local_compare_price_payload(path)
        context_now=time.monotonic()
        context_trades=_SBC_MARKET_CONTEXT.get("trades",{})
        for trade_id,deadline in tuple(context_trades.items()):
            if float(deadline)<context_now: context_trades.pop(trade_id,None)
        if (_SBC_MARKET_CONTEXT.get("state") is STATE and
                float(_SBC_MARKET_CONTEXT.get("until",0.0))>=context_now):
            deadline=context_now+180.0
            for auction in market_payload.get("auctionInfo",[]):
                context_trades[int(auction.get("tradeId",0) or 0)]=deadline
        return J(market_payload)

    price_limits_match=_re.match(
        r"^/ut/game/fifa19/marketdata/pricelimits$",low)
    if price_limits_match and method == "GET":
        query=parse_qs(urlsplit(path).query)
        raw=(query.get("defId") or query.get("defid") or
             query.get("assetId") or query.get("assetid") or [0])[0]
        try: requested_definition_id=int(raw or 0)
        except (TypeError,ValueError): requested_definition_id=0
        definition=object_definition(requested_definition_id)
        is_kit=6_300_000 <= requested_definition_id < 6_500_000
        if definition is not None or is_kit:
            fields=dict(definition or {"Rating":75,"Rare":0})
            fields.update({"resourceId":requested_definition_id,
                           "inventoryType":("kit" if is_kit else
                               "staff" if str(fields.get("_type","")) in
                               {"headcoach","fitnesscoach","physio","gkcoach"}
                               else str(fields.get("_type","")))})
            minimum,maximum=market_object_price_limits(fields)
        else:
            definition_id=market_exact_resource_id(requested_definition_id)
            fields=native_player_fields(definition_id)
            minimum,maximum=_market_price_limits(fields)
        # The native market-data delegate iterates the response itself.  For
        # a defId query each row is identified by `defId`; wrapping this row in
        # itemData/priceLimits leaves the in-game Compare Price dialog waiting.
        return J([{"defId":requested_definition_id,
                   "minPrice":minimum,"maxPrice":maximum}])
    if low in ("/ut/game/fifa19/store/purchasegroup/cardpack",
               "/ut/game/fifa19/store/purchasegroup/all") and method == "GET":
        unopened=STATE.reward_unopened_packs()
        purchases=store_purchases(STATE.credits(),unopened)
        if unopened:
            counts={}
            for row in unopened:
                pid=int(row.get("packId",0) or 0)
                counts[pid]=counts.get(pid,0)+1
            log("store","My Packs offers="+json.dumps(
                counts,sort_keys=True,separators=(",",":")))
        categories=[
            {"id":1,"categoryId":1,"name":"Bronze Packs","groupName":"Bronze Packs",
             "value":"bronze","displayGroup":"bronze","sortPriority":1,"visible":True},
            {"id":2,"categoryId":2,"name":"Silver Packs","groupName":"Silver Packs",
             "value":"silver","displayGroup":"silver","sortPriority":2,"visible":True},
            {"id":3,"categoryId":3,"name":"Gold Packs","groupName":"Gold Packs",
             "value":"gold","displayGroup":"gold","sortPriority":3,"visible":True},
            {"id":4,"categoryId":4,"name":"Promo Packs","groupName":"Promo Packs",
             "value":"special","displayGroup":"special","sortPriority":4,"visible":True}]
        if unopened:
            # `mypacks`, `IS_MYPACKS_CATEGORY` and
            # `PACK_CREATE_UNOPENED_PACK` are native CardsDLL identities. A
            # QUANTITY offer without this matching category is counted in the
            # FUT hub notification but filtered out of the Store carousel.
            categories.insert(0,{
                "id":0,"categoryId":0,"name":"My Packs",
                "groupName":"My Packs","value":"mypacks",
                "displayGroup":"mypacks","sortPriority":0,"visible":True,
                "isMyPacksCategory":True,"IS_MYPACKS_CATEGORY":True})
        return J({"purchase":purchases,"categoryInfo":categories,
                  "categories":categories,"timestamp":now_s(),**_credits_payload()})
    if low.startswith("/ut/game/fifa19/store"): return J({"purchase":[]})
    # The client filters this pool by position and applies slice(0,5), keeping
    # the visible pick at five while preserving coverage for every position.
    if low == "/ut/game/fifa19/loan/players" and method == "GET":
        return J({"loans":_native_loan_candidates()})
    loan_match=_re.match(r"^/ut/game/fifa19/loan/player/(\d+)$",low)
    if loan_match and method == "PUT":
        granted=_grant_onboarding_loan(int(loan_match.group(1)))
        return J(granted or {})

    # Before a bulk move the native client resolves every unassigned item via
    # GET /item?idList=... .  Returning the generic two-byte `{}` fallback here
    # makes an otherwise valid pack fail with the communication-error popup.
    if low == "/ut/game/fifa19/item" and method == "GET":
        query=parse_qs(urlsplit(path).query)
        requested=[]
        for chunk in query.get("idList",query.get("idlist",[])):
            for raw in str(chunk).split(","):
                try: requested.append(int(raw))
                except (TypeError,ValueError): pass
        items=[]
        for item_id in requested:
            item=STATE.item(item_id)
            if item:
                items.append(_native_item(item))
            else:
                pick=_pending_player_pick(item_id)
                if pick: items.append(_native_player_pick_item(pick))
        return J({"itemData":items,
                  "duplicateItemIdList":_duplicate_item_links(items)})

    item_resource=_re.match(r"^/ut/game/fifa19/item/resource/(\d+)$",low)
    if item_resource and method == "POST":
        applied,error=_apply_consumable(int(item_resource.group(1)),payload)
        if applied is None:
            return J({"success":False,"errorCode":error,"itemData":[]})
        native=[_native_item(item) for item in applied["items"]]
        return J({"success":True,"errorCode":0,"itemData":native,
                  "items":native,"resourceId":int(item_resource.group(1))})

    # The PC game does not use the Companion endpoint POST /item/{id} when a
    # subtype-237 Pick Item is redeemed from the unassigned pile. CardsDLL's
    # ApplyCardNontargeted delegate posts the instance id as a query parameter.
    # Both entry points deliberately share the same transactional state flow.
    if low == "/ut/game/fifa19/item/nontargeted" and method == "POST":
        query=parse_qs(urlsplit(path).query)
        try:
            pick_id=int(next(iter(query.get("itemId",query.get("itemid",[])))))
        except (StopIteration,TypeError,ValueError):
            return J({"success":False,"errorCode":461,"itemData":[]})
        pick=_pending_player_pick(pick_id)
        active=STATE.active_player_pick()
        if pick is None and active is None:
            return J({"success":False,"errorCode":461,"itemData":[]})
        redeemed=STATE.redeem_player_pick(pick_id)
        if redeemed is None:
            return J({"success":False,"errorCode":461,"itemData":[]})
        _acknowledge_repeatable_sbc_reward(redeemed)
        choices=_native_player_pick_payload(redeemed)["options"]
        return J({"itemData":choices,
                  "duplicateItemIdList":_duplicate_item_links(choices),
                  "dynamicObjectivesUpdates":{},"success":True})

    one_item=_re.match(r"^/ut/game/fifa19/item/(\d+)$",low)
    if one_item and method == "POST":
        pick_id=int(one_item.group(1))
        pick=_pending_player_pick(pick_id)
        if pick is None and STATE.active_player_pick() is None:
            return J({"success":False,"errorCode":461,"itemData":[]})
        redeemed=STATE.redeem_player_pick(pick_id)
        if redeemed is None:
            return J({"success":False,"errorCode":461,"itemData":[]})
        _acknowledge_repeatable_sbc_reward(redeemed)
        choices=_native_player_pick_payload(redeemed)["options"]
        return J({"itemData":choices,
                  "duplicateItemIdList":_duplicate_item_links(choices),
                  "dynamicObjectivesUpdates":{},"success":True})
    if one_item and method == "PUT":
        updated=STATE.activate_club_item(
            int(one_item.group(1)),payload.get("activateSlotNumber"))
        if not updated:
            return J({"errorCode":461})
        # ActivateClubItemDelegate declares RESPONSE.EMPTY.  Sending the
        # generic two-byte JSON object leaves the native state transition in
        # an inconsistent path even though the HTTP status is 200.
        return b""
    if one_item and method == "GET":
        item=STATE.item(int(one_item.group(1)))
        if not item:
            pick=_pending_player_pick(int(one_item.group(1)))
            if pick:
                native=_native_player_pick_item(pick)
                return J({"itemData":[native],"duplicateItemIdList":[]})
        if not item:
            return J({"itemData":[],"duplicateItemIdList":[],"errorCode":461})
        native=_native_item(item)
        # The native CardsDLL reader uses the same collection contract as
        # GET /item?idList=... .  A previous compatibility payload duplicated
        # the card at the top level and nested a bare object under itemData;
        # CardsDLL dereferenced that object as an array after pack opening.
        return J({"itemData":[native],
                  "duplicateItemIdList":_duplicate_item_links([native])})

    # After the grant the client explicitly moves the card from pile 6
    # (purchased) to pile 7 (club) through PUT /item. The response must include
    # one result per element; `{}` is treated as an error.
    if low == "/ut/game/fifa19/item" and method == "PUT":
        results=[]
        for move in payload.get("itemData",[]):
            try: iid=int(move.get("id",0) or 0)
            except (TypeError,ValueError): iid=0
            raw_destination=move.get("pile","club")
            destination={5:"trade",6:"purchased",7:"club",
                         "5":"trade","6":"purchased","7":"club"}.get(
                             raw_destination,str(raw_destination or "club").lower())
            moved=None; move_error=0
            source=STATE.item(iid) if iid else None
            duplicate=None
            if (source and destination == "club" and
                    stored_item_kind(source) == "player"):
                duplicate=next((x for x in STATE.items_in_pile("club","player")
                    if _player_duplicate_key(x)==_player_duplicate_key(source)
                    and int(x.get("id",0) or 0)!=iid),None)
                swap_id=int(move.get("swap",0) or 0)
                if duplicate:
                    # A duplicate may only replace the club copy when the
                    # client explicitly identifies that copy as `swap`.
                    # Inferring the swap merely moved the old card back to the
                    # unassigned pile, producing an endless item on every FUT
                    # login even after the user had confirmed the batch.
                    duplicate_id=int(duplicate.get("id",0) or 0)
                    if swap_id != duplicate_id:
                        move_error=472
                    else:
                        STATE.move_item(duplicate_id,"purchased")
                        moved=STATE.move_item(iid,destination)
                else:
                    moved=STATE.move_item(iid,destination)
            elif source:
                moved=STATE.move_item(iid,destination)
            if moved and destination == "club":
                STATE.set("objective_items_to_club",
                          int(STATE.get("objective_items_to_club",0) or 0)+1)
            result={"id":iid,"success":bool(moved),
                    "errorCode":0 if moved else (move_error or 461)}
            if source and destination == "club" and duplicate:
                result["duplicateItemId"]=int(duplicate.get("id",0) or 0)
            results.append(result)
            log("items","move itemId=%d destination=%s success=%s errorCode=%d" %
                (iid,destination,bool(moved),int(result["errorCode"])))
        return J({"itemData":results})

    delete_ids=[]
    one_delete=_re.match(r"^/ut/game/fifa19/item/(\d+)$",low)
    if one_delete and method == "DELETE":
        delete_ids=[int(one_delete.group(1))]
    elif low == "/ut/delete/game/fifa19/item" and method == "POST":
        raw_ids=payload.get("itemId",[])
        if not isinstance(raw_ids,list): raw_ids=[raw_ids]
        for value in raw_ids:
            try: delete_ids.append(int(value))
            except (TypeError,ValueError): pass
    if delete_ids:
        discarded=[]; total=0
        for iid in delete_ids:
            item=STATE.item(iid)
            if not item: continue
            if stored_item_kind(item)=="player":
                value=int(item.get("discardValue",0) or 0)
            else:
                # Normalize legacy pack objects too: older persisted rows may
                # still carry the former rating*8 value even though every new
                # DTO now advertises the corrected 20-coin object discard.
                value=0 if bool(item.get("untradeable",True)) else 20
            total+=value
            STATE.delete_item(iid); discarded.append({"id":iid})
        if total: STATE.add_credits(total)
        return J({"discardCredits":total,"items":discarded,**_credits_payload()})

    # --- Packs ---
    if low == "/ut/game/fifa19/purchased/items" and method == "POST":
        try: pack_id=int(payload.get("packId",0) or 0)
        except (TypeError,ValueError): pack_id=0
        pack=pack_definition(pack_id)
        if not pack:
            return J({"code":"461","itemList":[],"credits":STATE.credits()})
        # Reward packs are opened with useCredits=0 (and, depending on the
        # screen, with or without usePreOrder).  A matching unopened grant is
        # still mandatory, so this cannot create arbitrary free packs.
        currency=payload.get("currency","COINS")
        reward_pack=STATE.reward_unopened_pack(pack_id)
        has_reward=reward_pack is not None
        use_credits=payload.get("useCredits",1)
        try: use_credits=bool(int(use_credits))
        except (TypeError,ValueError): use_credits=bool(use_credits)
        reward_requested=(bool(payload.get("usePreOrder",False)) or
                          "useCredits" in payload and not use_credits)
        # While this pack id is visible in My Packs its paid Store offer is
        # deliberately hidden, so the only valid action is opening the oldest
        # persisted reward.  This also supports retail bodies which omit both
        # usePreOrder and useCredits.
        free=has_reward
        if reward_requested and not has_reward:
            log("packs","rejected stale reward open packId=%d body=%s" % (
                pack_id,json.dumps(payload,sort_keys=True,separators=(",",":"))))
            return J({"code":"461","itemList":[],"credits":STATE.credits()})
        reward_pack_id=(int(reward_pack["id"]) if free else None)
        log("packs","open request packId=%d rewardPackId=%s free=%s body=%s" % (
            pack_id,reward_pack_id,free,
            json.dumps(payload,sort_keys=True,separators=(",",":"))))
        price=0 if free else int(pack["price"])
        seed=secrets.randbits(62)
        if pack.get("playerPickBundle"):
            pick_specs=_generate_pick_bundle_for_profile(pack_id,seed)
            opened=STATE.purchase_player_pick_bundle(
                pack_id,price,pick_specs,seed,
                "REWARD" if free else str(currency),reward_pack_id)
            if opened is None:
                return J({"code":"470","itemList":[],"playerPicks":[],
                          "credits":STATE.credits()})
            if free:
                _acknowledge_repeatable_sbc_reward(reward_pack)
            STATE.set("objective_packs_opened",
                      int(STATE.get("objective_packs_opened",0) or 0)+1)
            pick_items=[_native_player_pick_item(row)
                        for row in opened["playerPicks"]]
            return J(_native_create_pack_response(
                pick_items,pack_id,
                playerPicks=opened["playerPicks"],
                specialItemCount=0,packId=pack_id,
                openingId=opened["openingId"],
                **_credits_payload(opened["credits"])))
        specs,special_count=_generate_pack_for_profile(pack_id,seed)
        opened=STATE.purchase_pack(pack_id,price,specs,seed,
                                   "REWARD" if free else str(currency),
                                   reward_pack_id)
        if opened is None:
            return J({"code":"470","itemList":[],"credits":STATE.credits()})
        if free:
            _acknowledge_repeatable_sbc_reward(reward_pack)
        STATE.set("objective_packs_opened",int(STATE.get("objective_packs_opened",0) or 0)+1)
        opened_items=[_native_item(x) for x in opened["items"]]
        item_type_counts={}
        for item in opened_items:
            item_type=str(item.get("itemType","unknown") or "unknown")
            item_type_counts[item_type]=item_type_counts.get(item_type,0)+1
        log("packs","response packId=%d openingId=%d numberItems=%d "
            "itemTypes=%s" % (
                pack_id,int(opened["openingId"]),len(opened_items),
                ",".join("%s:%d" % pair for pair in
                         sorted(item_type_counts.items()))))
        # Native reveal order: campaign/special items before base cards, then
        # highest OVR. Non-player items follow the player reveal sequence.
        # No artificial ranking is introduced between player promotions.
        opened_items.sort(key=lambda item:(
            (2 if str(item.get("itemType","player")).lower() != "player"
             else (1 if card_revision(int(item.get(
                 "definitionId",item.get("resourceId",0)) or 0)) == "Normal"
                   else 0)),
            -int(item.get("rating",0) or 0),
            -int(item.get("rareflag",0) or 0),
            int(item.get("resourceId",0) or 0)))
        return J(_native_create_pack_response(
            opened_items,pack_id,
            duplicate_item_ids=_duplicate_item_links(opened_items),
            specialItemCount=special_count,packId=pack_id,
            openingId=opened["openingId"],
            **_credits_payload(opened["credits"])))
    if low.startswith("/ut/game/fifa19/purchase"): return J({"itemData":[],"credits":STATE.credits()})

    # --- Squad Building Challenges ---
    if low == "/ut/game/fifa19/sbs/sets" and method == "GET":
        return J(_native_sbc_sets())
    sbc_rewards=_re.match(
        r"^/ut/game/fifa19/sbs/(?:setid|set|sets)/(\d+)/(?:rewards|awards)$",
        low)
    if sbc_rewards and method in ("GET","POST"):
        set_id=int(sbc_rewards.group(1))
        set_spec=_sbc_set_spec(set_id)
        if set_spec is None:
            return J({"errorCode":404,"reason":"unknownSbcSet",
                      "setId":set_id,"awards":[]})
        raw_cycle=(parse_qs(urlsplit(path).query).get("cycle") or [None])[0]
        try: cycle=int(raw_cycle) if raw_cycle is not None else None
        except (TypeError,ValueError): cycle=None
        return J(_native_sbc_set_rewards(set_spec,cycle))
    if (low in ("/ut/game/fifa19/sbs/rewards",
                "/ut/game/fifa19/sbs/sets/rewards",
                "/ut/game/fifa19/sbs/sets/awards") and
            method in ("GET","POST")):
        query=parse_qs(urlsplit(path).query)
        try: set_id=int((query.get("setId") or query.get("setid") or
                        query.get("id") or [0])[0] or 0)
        except (TypeError,ValueError): set_id=0
        set_spec=_sbc_set_spec(set_id)
        if set_spec is None:
            return J({"errorCode":404,"reason":"unknownSbcSet",
                      "setId":set_id,"awards":[]})
        return J(_native_sbc_set_rewards(set_spec))
    sbc_set=_re.match(
        r"^/ut/game/fifa19/sbs/(?:setid|set|sets)/(\d+)/challenges$",low)
    if sbc_set and method == "GET":
        set_id=int(sbc_set.group(1))
        set_spec=_sbc_set_spec(set_id)
        if set_spec is None:
            log("sbc","unknown setId=%d route=%s" % (set_id,path))
            return J({"errorCode":404,"reason":"unknownSbcSet",
                      "setId":set_id,"challenges":[]})
        challenges=[_native_sbc_challenge(challenge)
                    for challenge in set_spec.get("challenges",[])]
        return J({"setId":set_id,"challenges":challenges})
    sbc_squad=_re.match(
        r"^/ut/game/fifa19/sbs/(?:challenge|challenges)/(\d+)/squad$",low)
    if sbc_squad:
        challenge_id=int(sbc_squad.group(1))
        resolved=_sbc_challenge_spec(challenge_id)
        if resolved is None:
            log("sbc","unknown challengeId=%d workspace route=%s" %
                (challenge_id,path))
            return J({"errorCode":461})
        set_spec,challenge_spec=resolved
        set_id=int(set_spec.get("setId",0) or 0)
        if method == "GET":
            _SBC_MARKET_CONTEXT.update(
                state=STATE,until=time.monotonic()+180.0,trades={})
            return J(_native_sbc_workspace(set_spec,challenge_spec))
        if method in ("POST","PUT"):
            raw_squad=payload.get("squad",payload)
            assignment_item_ids=_sbc_raw_assignment_item_ids(raw_squad)
            squad=_normalize_sbc_candidate(challenge_spec,raw_squad)
            saved_progress=STATE.save_sbc_squad(
                set_id,challenge_id,squad,
                repeatable=bool(challenge_spec.get("repeatable",False)),
                assignment_item_ids=assignment_item_ids)
            workspace=_native_sbc_workspace(set_spec,challenge_spec)
            workspace.update({"status":saved_progress["status"],
                              "saved":True,"updatedAt":saved_progress.get(
                                  "updatedAt",0)})
            return J(workspace)
    sbc_action=_re.match(
        r"^/ut/game/fifa19/sbs/(?:challenge|challenges)/(\d+)"
        r"(?:/(submit|complete))?$",low)
    if sbc_action and method == "GET" and not sbc_action.group(2):
        challenge_id=int(sbc_action.group(1))
        resolved=_sbc_challenge_spec(challenge_id)
        if resolved is None:
            log("sbc","unknown challengeId=%d detail route=%s" %
                (challenge_id,path))
            return J({"errorCode":404,"reason":"unknownSbcChallenge",
                      "challengeId":challenge_id})
        _SBC_MARKET_CONTEXT.update(
            state=STATE,until=time.monotonic()+180.0,trades={})
        return J(_native_sbc_challenge(resolved[1]))
    if sbc_action and method in ("POST","PUT"):
        challenge_id=int(sbc_action.group(1))
        action=str(sbc_action.group(2) or "")
        resolved=_sbc_challenge_spec(challenge_id)
        if resolved is None:
            log("sbc","unknown challengeId=%d submit route=%s" %
                (challenge_id,path))
            return J({"errorCode":461,"awards":[]})
        set_spec,challenge_spec=resolved
        set_id=int(set_spec.get("setId",0) or 0)
        saved=next((row for row in STATE.sbc_status()
                    if int(row.get("setId",0)) == set_id and
                    int(row.get("challengeId",0)) == challenge_id),None)
        candidate=(payload.get("squad",payload)
                   if isinstance(payload,dict) else {})
        has_players=(isinstance(candidate,dict) and
                     isinstance(candidate.get("players"),list))
        # Verified FUT open/save/submit sequence. Every SBC exchange captured
        # on the supported EA App build uses exactly this split:
        #   POST /challenge/<id>            -> open the workspace
        #   PUT  /challenge/<id>/squad      -> save
        #   empty PUT /challenge/<id>       -> submit the saved workspace
        # Not one captured submit used POST. Treating an empty POST as a
        # submit once a squad was saved made simply opening a challenge that
        # already had progress fire a submission of the stale squad, which is
        # what rejected "The Third Step" with an unrelated requirement message
        # on 2026-09-03 at 00:58:23. Opening always reopens the workspace, and
        # the workspace restores the saved squad, so no progress is lost.
        # Explicit /submit and /complete aliases always submit.
        if not action and method == "POST" and not payload:
            _SBC_MARKET_CONTEXT.update(
                state=STATE,until=time.monotonic()+180.0,trades={})
            return J(_native_sbc_workspace(set_spec,challenge_spec))
        if not action and method == "POST" and has_players:
            assignment_item_ids=_sbc_raw_assignment_item_ids(candidate)
            candidate=_normalize_sbc_candidate(challenge_spec,candidate)
            saved_progress=STATE.save_sbc_squad(
                set_id,challenge_id,candidate,
                repeatable=bool(challenge_spec.get("repeatable",False)),
                assignment_item_ids=assignment_item_ids)
            workspace=_native_sbc_workspace(set_spec,challenge_spec)
            workspace.update({"status":saved_progress["status"],
                              "saved":True,"updatedAt":saved_progress.get(
                                  "updatedAt",0)})
            return J(workspace)
        if not action and method == "PUT" and has_players:
            assignment_item_ids=_sbc_raw_assignment_item_ids(candidate)
            candidate=_normalize_sbc_candidate(challenge_spec,candidate)
            saved_progress=STATE.save_sbc_squad(
                set_id,challenge_id,candidate,
                repeatable=bool(challenge_spec.get("repeatable",False)),
                assignment_item_ids=assignment_item_ids)
            workspace=_native_sbc_workspace(set_spec,challenge_spec)
            workspace.update({"status":saved_progress["status"],
                              "saved":True,"updatedAt":saved_progress.get(
                                  "updatedAt",0)})
            return J(workspace)
        submitted=(_normalize_sbc_candidate(challenge_spec,candidate)
                   if has_players else None)
        try:
            requested_operation=(payload.get("operationKey",
                payload.get("requestId",payload.get("idempotencyKey")))
                if isinstance(payload,dict) else None)
            response=STATE.submit_sbc(
                set_spec,challenge_spec,submitted,
                operation_key=requested_operation)
        except (SbcValidationError,ValueError) as exc:
            # Name the rule and both numbers: a rejection the player
            # cannot see on screen is what the generic FUT
            # communication error looks like from the front end.
            unmet=" ".join(
                "%s actual=%s target=%s" % (row.get("type"),
                                            row.get("actual"),
                                            row.get("value",
                                                    row.get("target")))
                for row in getattr(exc,"results",[]) or []
                if isinstance(row,dict) and not row.get("satisfied"))
            log("sbc","submit rejected challenge=%d set=%d reason=%s %s" %
                (challenge_id,set_id,str(exc),unmet))
            return (J({"challengeId":challenge_id,"setId":set_id,
                       "status":"IN_PROGRESS","awards":[],"rewards":[],
                       "success":False,"errorCode":461,
                       "reason":str(exc),"debug":str(exc),
                       "message":str(exc)}),461)
        response=dict(response)
        latest_statuses={
            int(row.get("challengeId",0)):row
            for row in STATE.sbc_status()
            if int(row.get("setId",0))==set_id}
        completed_set=set_dto(set_spec,latest_statuses)
        completed_set["categoryId"]=SBC_CATEGORY_IDS.get(
            str(completed_set.get("category","BASIC")),90)
        completed_set=_native_sbc_set_index_summary(completed_set)
        # A repeatable set reports zero completed challenges so its tile
        # returns to Available immediately, which is correct for the index but
        # wrong for this receipt: the submit that just finished the last
        # challenge did complete the set, and its group award has already been
        # granted. Reporting setCompleted=False here made the client skip the
        # reward presentation for a set whose only reward is set-level, which
        # is why the 80+ Player Pick never offered its redeem screen on
        # 2026-09-03 at 10:16:40 (completed=0/1, groupAwards=1).
        challenge_ids=[int(row.get("challengeId",row.get("id",0)) or 0)
                       for row in set_spec.get("challenges",[]) or []]
        set_completed=bool(challenge_ids) and all(
            str((latest_statuses.get(cid) or {}).get(
                "status","NOT_STARTED")).upper() in {"COMPLETED","CLAIMED"}
            for cid in challenge_ids)
        completed_set["status"]=("COMPLETED" if set_completed
                                 else "IN_PROGRESS")
        challenge_awards=list(response.get("awards",[]) or [])
        group_awards=list(response.get("groupAwards",[]) or [])
        if set_completed:
            completed_set["awards"]=group_awards
        response.update({
            "success":True,"completed":True,"challengeCompleted":True,
            "status":"COMPLETED","completedSetId":set_id,
            "setCompleted":set_completed,"setComplete":set_completed,
            "allChallengesCompleted":set_completed,
            "setStatus":"COMPLETED" if set_completed else "IN_PROGRESS",
            "challengesCompletedCount":int(completed_set.get(
                "challengesCompletedCount",0) or 0),
            "challengesCount":int(completed_set.get(
                "challengesCount",0) or 0),
            "rewards":challenge_awards,
            "challengeAwards":challenge_awards,
            "challengeRewards":challenge_awards,
            "setAwards":group_awards,"groupRewards":group_awards,
            "grantedChallengeAwards":challenge_awards,
            "grantedSetAwards":group_awards,
            "grantedAwards":challenge_awards+group_awards,
            "challenge":_native_sbc_challenge(challenge_spec),
            "challengeData":_native_sbc_challenge(challenge_spec),
            "set":completed_set,"completedSet":completed_set,
            "sbcSet":completed_set,"setData":completed_set,
            "sets":[completed_set] if set_completed else [],
            "completedSets":[completed_set] if set_completed else [],
            "setsCompleted":[set_id] if set_completed else [],
        })
        log("sbc","submit challenge=%d set=%d status=COMPLETED setCompleted=%s "
            "completed=%d/%d awards=%d groupAwards=%d" % (
                challenge_id,set_id,set_completed,
                int(completed_set.get("challengesCompletedCount",0) or 0),
                int(completed_set.get("challengesCount",0) or 0),
                len(challenge_awards),len(group_awards)))
        return J(_native_sbc_submit_wire(response,challenge_id,set_id))
    if low.startswith(("/ut/game/fifa19/sbc","/ut/game/fifa19/sbs")):
        log("sbc","UNHANDLED SBC route %s %s body=%s" %
            (method,path,json.dumps(payload)[:400]))
        return J({"errorCode":404,"reason":"unknownSbcRoute",
                  "method":method,"path":low})

    # --- Single Player Seasons ---
    if low == "/ut/game/fifa19/season/list" and method == "GET":
        _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SEASON")
        _roll_over_completed_season("catalogue")
        query={key.lower():values for key,values in
               parse_qs(urlsplit(path).query).items()}
        # CardsDLL+0x28b3d4 writes each value as "%d," of 11 - client division.
        requested_divisions=[value for item in query.get("divisionlist",())
                             for value in str(item).split(",") if value]
        divisions=tuple(int(value) for value in requested_divisions
                        if value.isdigit() and int(value) in range(1,11))
        _SEASON_CONTEXT.update(state=STATE,until=time.monotonic()+300.0)
        if requested_divisions and not divisions:
            # After every match retail asks for client division 0 (wire 11),
            # which does not exist. Answering with Division 10 rows made every
            # post-match return fail; a nonexistent division has no seasons.
            # An empty `seasons` list is still a catalogue, and the client
            # replaces the one it loaded on entry with nothing. Omitting the
            # member leaves that catalogue in place, which is what the screen
            # needs after a match.
            log("seasons","catalogue divisionList=%s has no division; "
                "no catalogue member" % ",".join(requested_divisions))
            return J({})
        seasons=season_catalog(divisions=divisions or None)
        log("seasons","catalogue rows=%d divisions=%s" % (
            len(seasons["seasons"]),divisions or "all"))
        return J(seasons)
    if low == "/ut/game/fifa19/season/user" and method == "GET":
        current=STATE.offline_season()
        _SEASON_CONTEXT.update(state=STATE,until=time.monotonic()+300.0)
        log("seasons","user division=%d round=%d points=%d" % (
            int(current["divisionId"]),int(current["round"]),
            int(current["userPoints"])))
        return J(season_user_payload(current))
    season_user_update=_re.match(
        r"^/ut/game/fifa19/season/(\d+)/division/(\d+)/user$",low)
    if season_user_update and method == "PUT":
        current=dict(STATE.offline_season())
        season_id=int(season_user_update.group(1))
        division_id=int(season_user_update.group(2))
        competition=None
        if division_id in range(1,11):
            competition=next((row for row in season_catalog(
                divisions=(division_id,))["seasons"]
                if int(row["id"])==season_id),None)
        if competition is None:
            return J({"errorCode":461,"state":"INVALID"})
        if (int(current.get("seasonId",0) or 0)!=season_id or
                int(current.get("divisionId",0) or 0)!=division_id):
            current=new_season(division_id,season_id)
            STATE.set(ACTIVE_SEASON_MATCH_KEY,None)
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,None)
        elif bool(current.get("complete",False)):
            return J({"errorCode":461,"state":"INVALID"})
        current["entered"]=True
        current["dataVersion"]=int(payload.get("dataVersion",3) or 3)
        if isinstance(payload.get("data"),str) and payload["data"]:
            current["userData"]=payload["data"]
        if isinstance(payload.get("progressData"),str) and payload["progressData"]:
            current["progressData"]=payload["progressData"]
        current["progressDataVersion"]=int(payload.get(
            "progressDataVersion",3) or 3)
        STATE.set("offline_season_v1",current)
        _SEASON_CONTEXT.update(state=STATE,until=time.monotonic()+300.0)
        log("seasons","entered season=%d division=%d clientRound=%s" % (
            season_id,division_id,payload.get("round",0)))
        # The post-match update used to answer {}. The client copies a
        # parsed Season record into its competition object
        # (CardsDLL+0x284e30 writes +0x38c from that record), and after a
        # match the division it re-sends drops to 0, so season/list then
        # asks for the impossible divisionList=11. Replay the record.
        return J(season_user_payload(current))
    if low == "/ut/game/fifa19/season/user/history" and method == "GET":
        history=season_history_payload(
            STATE.offline_season(),STATE.offline_season_totals())
        log("seasons","history type=offline record=%d-%d-%d completed=%d "
            "titles=%d promotions=%d relegations=%d" % (
                history["seasonGamesWon"],history["seasonGamesDraw"],
                history["seasonGamesLost"],history["seasonCompleted"],
                history["seasonTitlesWon"],history["seasonPromotions"],
                history["seasonRelegations"]))
        return J(history)
    # CardsDLL registers the authentic terminal endpoint as
    # ``ut/%s/season/%%s/reset``. Both Season update/quit responses are empty
    # DTOs, so the state change stays server-side and the reply remains {}.
    season_reset=_re.match(
        r"^/ut/game/fifa19/season/(?:offline|\d+(?:/division/\d+)?)/reset$",
        low)
    if season_reset and method in ("POST","PUT","DELETE"):
        if _roll_over_completed_season("reset") is None:
            # An unfinished Season on this route is Forfeit Season. The
            # client sends a zeroed blob and then reloads the catalogue; it
            # only offers the carousel again when the account holds no
            # enrolled Season, so the enrollment has to be dropped here.
            with STATE.lock:
                STATE.set(ACTIVE_SEASON_MATCH_KEY,None)
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,None)
                abandoned=int(STATE.offline_season()["seasonId"])
                forfeited=STATE.forfeit_offline_season()
            if forfeited is None:
                log("seasons","forfeit ignored: no unfinished Season")
            else:
                log("seasons","forfeit season=%d division=%d; carousel "
                    "re-opened" % (abandoned,int(forfeited["divisionId"])))
        return J({})

    # --- Offline matches ---
    draft_match=_re.match(
        r"^/ut/game/fifa19/(?:squad|draft)/mode/(\d+)/draft/match$",low)
    if draft_match and method in ("POST","PUT"):
        session=_draft_session_for_route(draft_match.group(1))
        updated=(STATE.record_draft_match(
            int(session["id"]),payload.get("result","WIN"),
            payload.get("home",payload.get("homeGoals",0)),
            payload.get("away",payload.get("awayGoals",0)),
            _draft_match_history_stats(payload,session)) if session else None)
        if not updated: return J({"success":False,"errorCode":461})
        STATE.set("objective_matches_played",
                  int(STATE.get("objective_matches_played",0) or 0)+1)
        return J({"success":True,**_draft_state_payload(updated["mode"],updated)})
    if low == "/ut/game/fifa19/sqbt/user/match" and method in ("POST","PUT"):
        event=_ensure_sqbt_event()
        opponent_id=_sqbt_request_integer(payload,_SQBT_OPPONENT_FIELDS,0)
        opponent=next((value for value in event["opponents"]
                       if int(value.get("opponentId",0))==opponent_id),None)
        if opponent is None:
            featured=_sqbt_featured_opponent(event)
            if int(featured.get("opponentId",0) or 0)==opponent_id:
                opponent=featured
        if not opponent: return J({"success":False,"errorCode":461})
        difficulty=max(1,min(7,_sqbt_request_integer(
            payload,_SQBT_DIFFICULTY_FIELDS,3)))
        result=str(payload.get("result","WIN")).upper()
        if result=="LOSE": result="LOSS"
        home,away,_has_away=_sqbt_result_scores(payload)
        coin_breakdown=_sqbt_match_coin_breakdown(
            payload,result,payload.get("endReason",result))
        points=sqbt_match_points(difficulty,result,home,away,
                                 opponent.get("stars",3.0)) \
               if coin_breakdown["completed"] else 0
        recorded=STATE.record_sqbt_match(
            int(event["id"]),opponent_id,result,difficulty,points,
            home,away,coin_breakdown["matchCoins"],
            payload.get("endReason",result))
        if not recorded: return J({"success":False,"errorCode":461})
        if recorded.get("createdNow"):
            STATE.set("objective_squad_battles_matches",
                      int(STATE.get("objective_squad_battles_matches",0) or 0)+1)
        if recorded.get("createdNow") and result=="WIN":
            STATE.set("objective_squad_battles_wins",
                      int(STATE.get("objective_squad_battles_wins",0) or 0)+1)
        STATE.set(SELECTED_SQBT_OPPONENT_KEY,0)
        return J({"success":True,"matchResult":recorded,
                  "sqbtEvent":_sqbt_wire_hub(_sqbt_hub_payload())})
    if low == "/ut/game/fifa19/ready" and method in ("POST","PUT"):
        # CardsDLL exposes MatchReady separately from CreateMatch.  Preserve
        # the observed body-optional acknowledgement if retail invokes this
        # route; it has not yet been observed in the failing live flow and is
        # not assumed here to release the pre-match gate.  The request contains
        # match/persona/item identifiers, so validate the persisted offline run.
        session=STATE.current_draft("SINGLE_PLAYER")
        if (session is None or session.get("state")!="READY_FOR_MATCH" or
                int(session.get("difficulty",0) or 0) not in range(1,8)):
            return J({"errorCode":461,"state":"INVALID"})
        log("draft","MatchReady acknowledged draft=%d items=%d" % (
            int(session["id"]),len(payload.get("items",[]))
            if isinstance(payload.get("items"),list) else 0))
        return b""
    if low == "/ut/game/fifa19/match" and method == "PUT":
        # Retail assigns the game id only after CreateMatch.  Its later
        # /match/end body contains this gid, but no mode or draftId, so persist
        # the association while the Single Player Draft is ready to play.
        try: gid=int(payload.get("gid",0) or 0)
        except (TypeError,ValueError): gid=0
        owner=STATE.get(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{})
        owner=owner if isinstance(owner,dict) else {}
        owner_mode=str(owner.get("mode","") or "").upper()
        if gid>0 and owner_mode in (
                "DRAFT","TOTW","SQBT","CHAMPIONS","SEASON"):
            owner_specs={
                "DRAFT":(ACTIVE_DRAFT_MATCH_KEY,"sessionId","draft"),
                "TOTW":(ACTIVE_TOTW_MATCH_KEY,"challengeId","totw"),
                "SQBT":(ACTIVE_SQBT_MATCH_KEY,"opponentId","sqbt"),
                "CHAMPIONS":(ACTIVE_CHAMPION_MATCH_KEY,"sessionId","champions"),
                "SEASON":(ACTIVE_SEASON_MATCH_KEY,"seasonId","seasons"),
            }
            active_key,identity_key,log_tag=owner_specs[owner_mode]
            pending=STATE.get(active_key,{})
            expected_id=int(owner.get(identity_key,0) or 0)
            pending_id=(int(pending.get(identity_key,0) or 0)
                        if isinstance(pending,dict) else 0)
            pending_gid=(int(pending.get("gid",0) or 0)
                         if isinstance(pending,dict) else 0)
            if (expected_id>0 and pending_id==expected_id and
                    pending_gid in (0,gid) and
                    not bool(pending.get("completed",False))):
                associated=dict(pending)
                associated.update({"gid":gid,"startedAt":now_s(),
                                   "completed":False})
                STATE.set(active_key,associated)
                owner=dict(owner)
                owner.update({"gid":gid,"startedAt":associated["startedAt"],
                              "completed":False})
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner)
                log(log_tag,"associated gid=%d with %s=%d owner=%s" % (
                    gid,identity_key,expected_id,owner_mode))
            else:
                log("match","gid=%d not associated: explicit owner=%s "
                    "%s=%d pending=%d pendingGid=%d" % (
                        gid,owner_mode,identity_key,expected_id,pending_id,
                        pending_gid))
            # An explicit owner is authoritative. Do not let a stale record in
            # another mode capture this gid through the legacy fallback.
            return J({})
        session=STATE.current_draft("SINGLE_PLAYER") if gid>0 else None
        existing=STATE.get(ACTIVE_DRAFT_MATCH_KEY,{})
        pending_draft=(
            session is not None and session.get("state")=="READY_FOR_MATCH" and
            isinstance(existing,dict) and
            int(existing.get("sessionId",0) or 0)==int(session["id"]) and
            int(existing.get("gid",0) or 0) in (0,gid) and
            not bool(existing.get("completed",False)))
        if pending_draft:
            associated=dict(existing)
            associated.update({"gid":gid,"sessionId":int(session["id"]),
                               "startedAt":now_s(),"completed":False})
            STATE.set(ACTIVE_DRAFT_MATCH_KEY,associated)
            log("draft","associated gid=%d with draft=%d" %
                (gid,int(session["id"])))
            return J({})
        pending=STATE.get(ACTIVE_TOTW_MATCH_KEY,{}) if gid>0 else {}
        if (isinstance(pending,dict) and
                int(pending.get("challengeId",0) or 0)>0 and
                not bool(pending.get("completed",False))):
            pending=dict(pending)
            pending.update({"gid":gid,"startedAt":now_s()})
            STATE.set(ACTIVE_TOTW_MATCH_KEY,pending)
            log("totw","associated gid=%d with challenge=%d difficulty=%d" % (
                gid,int(pending["challengeId"]),
                int(pending.get("difficulty",3) or 3)))
            return J({})
        pending_sqbt=STATE.get(ACTIVE_SQBT_MATCH_KEY,{}) if gid>0 else {}
        if (isinstance(pending_sqbt,dict) and
                int(pending_sqbt.get("opponentId",0) or 0)>0 and
                not bool(pending_sqbt.get("completed",False))):
            pending_sqbt=dict(pending_sqbt)
            pending_sqbt.update({"gid":gid,"startedAt":now_s()})
            STATE.set(ACTIVE_SQBT_MATCH_KEY,pending_sqbt)
            log("sqbt","associated gid=%d with opponent=%d difficulty=%d" % (
                gid,int(pending_sqbt["opponentId"]),
                int(pending_sqbt.get("difficulty",3) or 3)))
            return J({})
        if session is not None and session.get("state")=="READY_FOR_MATCH":
            same_completed=(isinstance(existing,dict) and
                            int(existing.get("gid",0) or 0)==gid and
                            bool(existing.get("completed",False)))
            if not same_completed:
                STATE.set(ACTIVE_DRAFT_MATCH_KEY,{
                    "gid":gid,"sessionId":int(session["id"]),
                    "startedAt":now_s(),"completed":False})
                log("draft","associated gid=%d with draft=%d" %
                    (gid,int(session["id"])))
        return J({})
    if low == "/ut/game/fifa19/match" and method == "POST":
        # CreateMatch consumes the CPU opponent plus the match start time.
        # MatchReady has a separate /ready binding.  Keep the resources distinct
        # while the passive trace identifies which client-side gate is reached;
        # do not infer the native transition from an unobserved HTTP request.
        try: draft_id=int(payload.get("squadId",0) or 0)-700000
        except (TypeError,ValueError): draft_id=0
        session=STATE.draft_session(draft_id) if draft_id>0 else None
        if session is not None:
            if (session.get("state")!="READY_FOR_MATCH" or
                    int(session.get("difficulty",0) or 0) not in range(1,8)):
                return J({"errorCode":461,"state":"INVALID"})
            local_squad=_draft_native_squad(session)
            identity=_draft_opponent_identity(session)
            opponent=_draft_match_squad(session,int(identity["round"]))
            existing=STATE.get(ACTIVE_DRAFT_MATCH_KEY,{})
            same_pending=(isinstance(existing,dict) and
                          int(existing.get("sessionId",0) or 0)==
                          int(session["id"]) and
                          not bool(existing.get("completed",False)))
            pending=dict(existing) if same_pending else {
                "gid":0,"sessionId":int(session["id"]),
                "createdAt":now_s(),"completed":False}
            pending.update({
                "round":int(identity["round"]),
                "opponentId":int(identity["opponentId"]),
                "teamId":int(identity["teamId"]),
                "opponentName":str(identity["teamName"]),
            })
            STATE.set(ACTIVE_DRAFT_MATCH_KEY,pending)
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="DRAFT")
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{
                "mode":"DRAFT","gid":0,"sessionId":int(session["id"]),
                "opponentId":int(opponent.get("id",0) or 0),
                "createdAt":now_s(),"completed":False})
            log("draft","CreateMatch local id=%d persona=%d manager=%d "
                "actives=%s; opponent id=%d persona=%d manager=%d "
                "actives=%s" % (
                    int(local_squad.get("id",0) or 0),
                    int(local_squad.get("personaId",0) or 0),
                    len(local_squad.get("manager",[])),
                    ",".join(str(item.get("itemState",""))
                             for item in local_squad.get("actives",[])),
                    int(opponent.get("id",0) or 0),
                    int(opponent.get("personaId",0) or 0),
                    len(opponent.get("manager",[])),
                    ",".join(str(item.get("itemState",""))
                             for item in opponent.get("actives",[]))))
            return J({"squad":opponent,"startDateTime":now_s()})
        if draft_id>0:
            return J({"errorCode":461,"state":"INVALID"})
        raw_champion_id=payload.get("championId")
        champion_id=0
        if raw_champion_id not in (None,""):
            try: champion_id=int(raw_champion_id or 0)
            except (TypeError,ValueError): champion_id=0
            session=(STATE.champion_session(event_id=champion_id)
                     if champion_id>0 else None)
            if (session is not None and
                    session.get("state")=="PICK_DIFFICULTY" and
                    int(session.get("difficulty",0) or 0)==0):
                # RC94 proved that a JSON INVALID response is too late: the
                # eligible-squad view still enters retired online Search
                # Opponent.  The client's HTTP-480 CreateMatchDisabledErr
                # branch is the established pre-match stop until the custom
                # competition has an explicit, persisted difficulty choice.
                log("champions","blocked CreateMatch before difficulty "
                    "event=%d squadId=%s" % (
                        champion_id,payload.get("squadId",0)))
                return b"",480
            if (session is None or session.get("state")!="READY_FOR_MATCH" or
                    int(session.get("difficulty",0) or 0) not in range(1,8)):
                return J({"errorCode":461,"state":"INVALID"})
            squad_id=int(payload.get("squadId",0) or 0)
            if not _native_controlled_match_squad(squad_id):
                return J({"errorCode":461,"state":"INVALID"})
            opponent_snapshot=next((row for row in session.get("opponents",[])
                                    if not bool(row.get("played",False))),None)
            if opponent_snapshot is None:
                return J({"errorCode":461,"state":"INVALID"})
            opponent=_champion_cpu_squad(opponent_snapshot)
            existing=STATE.get(ACTIVE_CHAMPION_MATCH_KEY,{})
            same_pending=(isinstance(existing,dict) and
                          int(existing.get("sessionId",0) or 0)==
                          int(session["id"]) and
                          int(existing.get("opponentId",0) or 0)==
                          int(opponent_snapshot["opponentId"]) and
                          not bool(existing.get("completed",False)))
            pending=dict(existing) if same_pending else {
                "gid":0,"createdAt":now_s(),"completed":False}
            pending.update({
                "sessionId":int(session["id"]),"eventId":champion_id,
                "opponentId":int(opponent_snapshot["opponentId"]),
                "matchNumber":int(opponent_snapshot["ordinal"]),
                "difficulty":int(session["difficulty"]),
                "squadId":squad_id,
            })
            STATE.set(ACTIVE_CHAMPION_MATCH_KEY,pending)
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{
                "mode":"CHAMPIONS","gid":int(pending.get("gid",0) or 0),
                "sessionId":int(session["id"]),"eventId":champion_id,
                "opponentId":int(opponent_snapshot["opponentId"]),
                "createdAt":int(pending.get("createdAt",now_s())),
                "completed":False,
            })
            log("champions","CreateMatch event=%d match=%d opponent=%d "
                "difficulty=%d squadId=%d" % (
                    champion_id,int(opponent_snapshot["ordinal"]),
                    int(opponent_snapshot["opponentId"]),
                    int(session["difficulty"]),squad_id))
            # Mode 1012 serializes type ONLINE just like the retired single-
            # match flow. championId is the native discriminator, so this branch
            # must run before the generic HTTP-480 Online Single Match guard.
            return J({"squad":opponent,"startDateTime":now_s()})
        raw_season_id=payload.get("seasonId")
        if raw_season_id not in (None,""):
            try:
                season_id=int(raw_season_id or 0)
                division_id=int(payload.get("divisionId",0) or 0)
            except (TypeError,ValueError):
                season_id=division_id=0
            current=STATE.offline_season()
            if (bool(current.get("complete",False)) or
                    int(current.get("seasonId",0) or 0)!=season_id or
                    int(current.get("divisionId",0) or 0)!=division_id):
                return J({"errorCode":461,"state":"INVALID"})
            controlled=_native_controlled_match_squad(
                int(payload.get("squadId",0) or 0))
            if not controlled:
                return J({"errorCode":461,"state":"INVALID"})
            competition=next((row for row in season_catalog(
                                  divisions=(division_id,))["seasons"]
                              if int(row["id"])==season_id),None)
            round_id=int(current.get("round",0) or 0)
            if competition is None or round_id not in range(
                    len(competition["matches"])):
                return J({"errorCode":461,"state":"INVALID"})
            opponent=competition["matches"][round_id]
            pending={
                "gid":0,"seasonId":season_id,"divisionId":division_id,
                "round":round_id,"teamId":int(opponent["teamId"]),
                "difficulty":int(opponent["difficulty"]),
                "squadId":int(payload.get("squadId",0) or 0),
                "createdAt":now_s(),"completed":False,
            }
            STATE.set(ACTIVE_SEASON_MATCH_KEY,pending)
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SEASON")
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{
                "mode":"SEASON","gid":0,"seasonId":season_id,
                "divisionId":division_id,"round":round_id,
                "createdAt":pending["createdAt"],"completed":False,
            })
            log("seasons","CreateMatch season=%d division=%d round=%d "
                "opponentTeam=%d difficulty=%d squadId=%d" % (
                    season_id,division_id,round_id,
                    int(opponent["teamId"]),int(opponent["difficulty"]),
                    int(payload.get("squadId",0) or 0)))
            return J({"squad":controlled,"startDateTime":now_s()})
        match_type=str(payload.get("type","") or "").upper()
        if match_type=="ONLINE":
            # The retired Online Single Match flow otherwise accepts the
            # owned squad here and stalls in Blaze matchmaking. HTTP 480 is
            # the client's dedicated CreateMatchDisabledErr branch at
            # CardsDLL+0x46b60..+0x46b97, so it returns to FUT with the native
            # unavailable-mode fallback instead of entering matchmaking.
            log("match","blocked retired online CreateMatch squadId=%s" %
                payload.get("squadId",0))
            return b"",480
        # The request squadId identifies the controlled HOME and the response
        # root must preserve that same owned squad. Offline Select has already
        # cached the selected CPU opponent as AWAY; returning the CPU squad at
        # the root would make its temporary item IDs the user-controlled XI.
        squad_id=int(payload.get("squadId",0) or 0)
        game_mode=str(payload.get("gameMode","") or "").upper()
        if game_mode=="TOTW_CHALLENGE":
            controlled=_native_controlled_match_squad(squad_id)
            if not controlled:
                return J({"errorCode":461,"state":"INVALID"})
            opponent=_totw_match_squad()
            log("totw","CreateMatch CPU opponent id=%d name=%s persona=%d "
                "rating=%d chemistry=%d manager=%d actives=%s; "
                "controlled HOME remains id=%d name=%s" % (
                    int(opponent.get("id",0) or 0),
                    opponent.get("squadName",""),
                    int(opponent.get("personaId",0) or 0),
                    int(opponent.get("rating",0) or 0),
                    int(opponent.get("chemistry",0) or 0),
                    len(opponent.get("manager",[])),
                    ",".join(str(item.get("itemState",""))
                             for item in opponent.get("actives",[])),
                    int(controlled.get("id",0) or 0),
                    controlled.get("squadName","")))
            selected=int(STATE.get("totw_challenge_id",0) or 0)
            if selected<=0:
                selected=max((int(row.get("challengeId",0) or 0)
                              for row in _totw_payload().get(
                                  "challenges",[])),default=0)
            difficulty=max(1,min(len(TOTW_DIFFICULTIES),int(
                STATE.get("totw_difficulty",3) or 3)))
            STATE.set(ACTIVE_TOTW_MATCH_KEY,{
                "gid":0,"challengeId":selected,"difficulty":difficulty,
                "squadId":squad_id,"createdAt":now_s(),"completed":False})
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="TOTW")
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{
                "mode":"TOTW","gid":0,"challengeId":selected,
                "createdAt":now_s(),"completed":False})
            # Offline Select already cached the public TOTW as AWAY.  The
            # CreateMatch root is the controlled HOME, matching the proven
            # FUT 18 handoff and preventing TOTW 6 from replacing My Squad.
            return J({"squad":controlled,
                      "opponentTeamId":int(opponent.get("teamId",0) or 0),
                      "startDateTime":now_s()})
        # Squad Battles reaches CreateMatch with the user's own squadId and no
        # distinguishing member, exactly like an ordinary offline match.  The
        # selected opponent latched by /sqbt/user/opponentsquad is what makes
        # the mode identifiable without inventing a wire field.  Returning the
        # user's own squad here is the failure `_draft_match_squad` documents:
        # the CPU club shows the player's cards and the loader never releases.
        requested_sqbt_id=_sqbt_request_integer(
            payload,_SQBT_OPPONENT_FIELDS,0)
        if requested_sqbt_id>0:
            sqbt_opponent=_sqbt_select_from_payload(payload)
            if sqbt_opponent is None:
                return J({"errorCode":461,"state":"INVALID",
                          "reason":"INVALID_OR_PLAYED_SQBT_OPPONENT"})
        else:
            sqbt_opponent=_sqbt_selected_opponent()
        sqbt_squad=_sqbt_match_squad(sqbt_opponent)
        if sqbt_squad:
            event=_ensure_sqbt_event()
            opponent_id=int(sqbt_opponent.get("opponentId",0) or 0)
            controlled=_native_controlled_match_squad(squad_id)
            if not controlled:
                return J({"errorCode":461,"state":"INVALID",
                          "reason":"INVALID_CONTROLLED_SQUAD"})
            difficulty=max(1,min(7,int(STATE.get("sqbt_difficulty",3) or 3)))
            existing=STATE.get(ACTIVE_SQBT_MATCH_KEY,{})
            same_pending=(isinstance(existing,dict) and
                          int(existing.get("opponentId",0) or 0)==opponent_id and
                          not bool(existing.get("completed",False)))
            if not same_pending:
                STATE.set(ACTIVE_SQBT_MATCH_KEY,{
                    "gid":0,"eventId":int(event["id"]),
                    "opponentId":opponent_id,"difficulty":difficulty,
                    "squadId":squad_id,"createdAt":now_s(),"completed":False})
            _OFFLINE_SELECT_CONTEXT.update(state=STATE,mode="SQBT")
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{
                "mode":"SQBT","gid":0,"opponentId":opponent_id,
                "createdAt":now_s(),"completed":False})
            log("sqbt","CreateMatch HOME id=%d name=%s ownedItems=%d; "
                "AWAY opponent id=%d name=%s rating=%d manager=%d "
                "actives=%s team=%d" % (
                    int(controlled.get("id",0) or 0),
                    controlled.get("squadName",""),
                    sum(isinstance(row.get("itemData"),dict) for row in
                        controlled.get("players",[])[:11]),
                    opponent_id,sqbt_squad.get("squadName",""),
                    int(sqbt_squad.get("rating",0) or 0),
                    len(sqbt_squad.get("manager",[])),
                    ",".join(str(item.get("itemState",""))
                             for item in sqbt_squad.get("actives",[])),
                    int(sqbt_squad.get("teamId",0) or 0)))
            # /opponentsquad or /featuredsquad has already populated AWAY.
            # Returning it here as root made its temporary ICON item IDs the
            # controlled XI (confirmed by the live PUT /match item list).
            return J({"squad":controlled,
                      "opponentTeamId":int(sqbt_squad.get("teamId",0) or 0),
                      "startDateTime":now_s()})
        # Keep ordinary offline callers compatible while still satisfying the
        # parser's mandatory two-member contract.
        if (_SEASON_CONTEXT.get("state") is STATE and
                float(_SEASON_CONTEXT.get("until",0.0))>=time.monotonic()):
            log("seasons","CreateMatch candidate body=%s" % json.dumps(
                {key:value for key,value in payload.items() if key!="items"},
                sort_keys=True)[:1200])
        squad=next((row for row in STATE.squads()
                    if int(row.get("id",row.get("squadId",0)) or 0)==squad_id),
                   None)
        return J({"squad":squad or {},"startDateTime":now_s()})
    if low == "/ut/game/fifa19/match/end" and method in ("POST","PUT"):
        try: gid=int(payload.get("gid",0) or 0)
        except (TypeError,ValueError): gid=0
        # The generic request trace stops at 512 bytes, which lands inside the
        # `items` array, so the members that decide the score were never
        # visible. A Draft win played out 6-1 was stored 6-0 on 2026-09-07
        # with `scoreSource=http`, meaning an away member was present and
        # zero. Log every member except `items` so that member can be named.
        log("match","match/end fields gid=%d %s" % (gid,json.dumps(
            {key:value for key,value in (payload or {}).items()
             if key!="items"},sort_keys=True)[:1200]))
        offline_owner=STATE.get(ACTIVE_OFFLINE_MATCH_OWNER_KEY,{})
        offline_owner=(offline_owner if isinstance(offline_owner,dict) else {})
        owner_mode=str(offline_owner.get("mode","") or "").upper()
        owner_gid=int(offline_owner.get("gid",0) or 0)
        def owner_allows(mode):
            return (not owner_mode or
                    (owner_mode==mode and owner_gid in (0,gid)))
        active_match=STATE.get(ACTIVE_DRAFT_MATCH_KEY,{})
        if (owner_allows("DRAFT") and gid>0 and
                isinstance(active_match,dict) and
                int(active_match.get("gid",0) or 0)==gid):
            # FutDestroyMatchServerRequest identifies retail Draft matches by
            # gid alone. A repeated request returns the exact durable response
            # receipt without recording a second result.
            try: draft_id=int(active_match.get("sessionId",0) or 0)
            except (TypeError,ValueError): draft_id=0
            session=STATE.draft_session(draft_id) if draft_id>0 else None
            end_reason=str(payload.get("endReason","") or "").upper()
            explicit_result=str(payload.get("result","") or "").upper()
            if end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT"):
                result="LOSS"
            elif end_reason in ("WIN","LOSS","DRAW"):
                result=end_reason
            elif explicit_result in ("WIN","LOSS","LOSE","DRAW"):
                result=explicit_result
            else:
                # Do not invent a result for an as-yet unobserved retail end
                # shape.  Keep the association live so a valid retry can close
                # it, while returning the native empty response safely.
                log("draft","match/end missing result gid=%d reason=%s" %
                    (gid,end_reason or "<empty>"))
                return J({})
            home,away,score_source=_completed_match_scores(
                payload,result,active_match)
            coin_breakdown=_sqbt_match_coin_breakdown(
                payload,result,end_reason,score_override=(home,away))
            reward=int(coin_breakdown["matchCoins"])
            response=_native_destroy_match_response(
                end_reason,reward=reward)
            completed=(STATE.complete_draft_match(
                ACTIVE_DRAFT_MATCH_KEY,gid,draft_id,result,response,
                home,away,reward,
                _draft_match_history_stats(payload,session,active_match))
                if session else None)
            if completed is None:
                log("draft","match/end rejected gid=%d draft=%d" %
                    (gid,draft_id))
                return J({})
            if owner_mode=="DRAFT":
                owner_receipt=dict(offline_owner)
                owner_receipt.update({"gid":gid,"completed":True,
                                      "result":result})
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner_receipt)
            log("draft","%s gid=%d draft=%d result=%s score=%d-%d "
                "scoreSource=%s coins=%d response=%s" % (
                "recorded" if completed.get("createdNow") else "retried",
                gid,draft_id,result,home,away,score_source,
                int(completed["response"].get("matchCoins",0) or 0),
                ",".join(sorted(completed["response"].keys()))))
            # Keep the detailed response as the durable transactional receipt.
            # On the wire publish only the two independently decoded scalar
            # enums: an empty object returns from the parser but makes retail
            # treat the result as unvalidated, whereas the old rich DTO entered
            # unsafe array/nested-object decoder branches.
            return J(_native_destroy_match_wire_response(
                result,end_reason,completed["response"]))
        active_champion=STATE.get(ACTIVE_CHAMPION_MATCH_KEY,{})
        if (owner_allows("CHAMPIONS") and gid>0 and
                isinstance(active_champion,dict) and
                int(active_champion.get("gid",0) or 0)==gid):
            if bool(active_champion.get("completed",False)):
                retry_response=active_champion.get("wireResponse")
                if isinstance(retry_response,dict):
                    log("champions","match/end retry gid=%d opponent=%d" % (
                        gid,int(active_champion.get("opponentId",0) or 0)))
                    return J(retry_response)
                return J(_native_destroy_match_wire_response(
                    active_champion.get("result","LOSS"),
                    payload.get("endReason","NO_CONTEST")))
            end_reason=str(payload.get("endReason","") or "").upper()
            explicit_result=str(payload.get("result","") or "").upper()
            if end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT"):
                result="LOSS"
            elif end_reason in ("WIN","LOSS","DRAW"):
                result=end_reason
            elif explicit_result in ("WIN","LOSS","LOSE","DRAW"):
                result="LOSS" if explicit_result=="LOSE" else explicit_result
            else:
                log("champions","match/end missing result gid=%d reason=%s" %
                    (gid,end_reason or "<empty>"))
                return J({})
            session_id=int(active_champion.get("sessionId",0) or 0)
            opponent_id=int(active_champion.get("opponentId",0) or 0)
            home,away,score_source=_completed_match_scores(
                payload,result,active_champion)
            coin_breakdown=_sqbt_match_coin_breakdown(
                payload,result,end_reason,score_override=(home,away))
            recorded=STATE.record_champion_match(
                session_id,opponent_id,gid,result,home,away,
                coin_breakdown["matchCoins"],end_reason)
            if recorded is None:
                log("champions","match/end rejected gid=%d session=%d "
                    "opponent=%d" % (gid,session_id,opponent_id))
                return J({})
            detailed=_native_destroy_match_response(
                end_reason,credits=recorded.get("credits"),
                reward=recorded.get("rewardCoins",0))
            wire_response=_native_destroy_match_wire_response(
                result,end_reason,detailed)
            active_champion=dict(active_champion)
            active_champion.update({
                "completed":True,"result":result,
                "operationKey":"champions-match:%d" % gid,
                "wireResponse":wire_response,"response":detailed,
                "rewardCoins":int(recorded.get("rewardCoins",0) or 0),
            })
            STATE.set(ACTIVE_CHAMPION_MATCH_KEY,active_champion)
            if owner_mode=="CHAMPIONS":
                owner_receipt=dict(offline_owner)
                owner_receipt.update({"gid":gid,"completed":True,
                                      "result":result})
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner_receipt)
            if recorded.get("createdNow"):
                STATE.set("objective_matches_played",int(STATE.get(
                    "objective_matches_played",0) or 0)+1)
            log("champions","%s gid=%d session=%d opponent=%d result=%s "
                "score=%d-%d scoreSource=%s coins=%d played=%d state=%s" % (
                    "recorded" if recorded.get("createdNow",True) else
                    "retried",gid,session_id,opponent_id,result,home,away,
                    score_source,int(recorded.get("rewardCoins",0) or 0),
                    int(recorded.get("matchesPlayed",0) or 0),
                    str(recorded.get("state","") or "")))
            return J(wire_response)
        active_season=STATE.get(ACTIVE_SEASON_MATCH_KEY,{})
        if (owner_allows("SEASON") and gid>0 and
                isinstance(active_season,dict) and
                int(active_season.get("gid",0) or 0)==gid):
            if bool(active_season.get("completed",False)):
                retry_response=active_season.get("wireResponse")
                return J(retry_response if isinstance(retry_response,dict)
                         else {"endReason":str(active_season.get(
                             "endReason","NO_CONTEST"))})
            end_reason=str(payload.get("endReason","") or "").upper()
            explicit_result=str(payload.get("result","") or "").upper()
            if end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT"):
                result="LOSS"
            elif end_reason in ("WIN","LOSS","DRAW"):
                result=end_reason
            elif explicit_result in ("WIN","LOSS","LOSE","DRAW"):
                result="LOSS" if explicit_result=="LOSE" else explicit_result
            else:
                log("seasons","match/end missing result gid=%d reason=%s" %
                    (gid,end_reason or "<empty>"))
                return J({})
            home,away,score_source=_completed_match_scores(
                payload,result,active_season)
            coin_breakdown=_sqbt_match_coin_breakdown(
                payload,result,end_reason,score_override=(home,away))
            recorded=STATE.record_offline_season_match(
                result,"season-match:%d" % gid,home,away,
                coin_breakdown["matchCoins"])
            detailed=_native_destroy_match_response(
                end_reason,recorded.get("credits"),
                recorded.get("rewardCoins",0))
            wire_response=_native_destroy_match_wire_response(
                result,end_reason,detailed)
            current=recorded["season"]
            terminal_receipt=None
            if current.get("complete"):
                terminal_receipt=STATE.claim_offline_season_reward()
                current=STATE.offline_season()
                wire_response["seasonEndResult"]=current["seasonEndResult"]
                wire_response["seasonCoins"]=int(terminal_receipt["coins"])
            active_season=dict(active_season)
            active_season.update({
                "completed":True,"result":result,"endReason":end_reason,
                "wireResponse":wire_response,
            })
            if terminal_receipt is not None:
                active_season["terminalReceipt"]=terminal_receipt
            STATE.set(ACTIVE_SEASON_MATCH_KEY,active_season)
            owner_receipt=dict(offline_owner)
            owner_receipt.update({"gid":gid,"completed":True,
                                  "result":result})
            STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner_receipt)
            log("seasons","%s gid=%d season=%d round=%d result=%s "
                "score=%d-%d scoreSource=%s points=%d coins=%d" % (
                    "recorded" if recorded.get("createdNow") else "retried",
                    gid,int(current["seasonId"]),int(current["round"]),result,
                    home,away,score_source,int(current["userPoints"]),
                    int(recorded.get("rewardCoins",0) or 0)))
            return J(wire_response)
        STATE.migrate_legacy_sqbt_quits()
        active_sqbt=STATE.get(ACTIVE_SQBT_MATCH_KEY,{})
        if (owner_allows("SQBT") and gid>0 and
                isinstance(active_sqbt,dict) and
                int(active_sqbt.get("gid",0) or 0)==gid):
            # FutDestroyMatchServerRequest carries only gid, endReason and the
            # item list; it has no mode member, so the pending association made
            # at CreateMatch/PUT is the only way to resolve a Squad Battles
            # result.  The mode-tagged fallback below stays for local tools.
            if bool(active_sqbt.get("completed",False)):
                retry_response=active_sqbt.get("wireResponse")
                if isinstance(retry_response,dict):
                    log("sqbt","match/end retry gid=%d reused coins=%d "
                        "points=%d" % (
                            gid,int(retry_response.get("matchCoins",0) or 0),
                            int((retry_response.get("squadBattlesScore") or
                                 {}).get("finalScore",0) or 0)))
                    return J(retry_response)
                return J(_native_destroy_match_wire_response(
                    active_sqbt.get("result","LOSS"),
                    payload.get("endReason","NO_CONTEST")))
            end_reason=str(payload.get("endReason","") or "").upper()
            explicit_result=str(payload.get("result","") or "").upper()
            if end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT"):
                result="LOSS"
            elif end_reason in ("WIN","LOSS","DRAW"):
                result=end_reason
            elif explicit_result in ("WIN","LOSS","LOSE","DRAW"):
                result="LOSS" if explicit_result=="LOSE" else explicit_result
            else:
                log("sqbt","match/end missing result gid=%d reason=%s" %
                    (gid,end_reason or "<empty>"))
                return J({})
            event_id=int(active_sqbt.get("eventId",0) or 0)
            opponent_id=int(active_sqbt.get("opponentId",0) or 0)
            difficulty=max(1,min(7,int(active_sqbt.get("difficulty",3) or 3)))
            event=_ensure_sqbt_event()
            opponent=next((value for value in event.get("opponents",[])
                           if int(value.get("opponentId",0) or 0)==opponent_id),{})
            if not opponent:
                featured=_sqbt_featured_opponent(event)
                if int(featured.get("opponentId",0) or 0)==opponent_id:
                    opponent=featured
            home,away,_has_away=_sqbt_result_scores(payload)
            if (end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT") and
                    not _has_away and away<=home):
                # FutDestroyMatch omits the scoreboard on a withdrawal. A
                # forfeit is nevertheless a played loss; persist the minimum
                # truthful losing score instead of displaying 0-0.
                away=home+1
            coin_breakdown=_sqbt_match_coin_breakdown(
                payload,result,end_reason)
            points=sqbt_match_points(difficulty,result,home,away,
                                     opponent.get("stars",3.0)) \
                   if coin_breakdown["completed"] else 0
            recorded=STATE.record_sqbt_match(
                event_id,opponent_id,result,difficulty,points,
                home,away,coin_breakdown["matchCoins"],end_reason)
            if not recorded:
                log("sqbt","match/end rejected gid=%d opponent=%d" %
                    (gid,opponent_id))
                return J({})
            if recorded.get("createdNow"):
                STATE.set("objective_squad_battles_matches",
                          int(STATE.get(
                              "objective_squad_battles_matches",0) or 0)+1)
                if result=="WIN":
                    STATE.set("objective_squad_battles_wins",
                              int(STATE.get(
                                  "objective_squad_battles_wins",0) or 0)+1)
                STATE.set("objective_matches_played",
                          int(STATE.get("objective_matches_played",0) or 0)+1)
            wire_response=_sqbt_destroy_match_response(
                result,end_reason,recorded,coin_breakdown,difficulty)
            active_sqbt=dict(active_sqbt)
            active_sqbt.update({"completed":True,"result":result,
                                "operationKey":"sqbt-match:%d" % gid,
                                "wireResponse":wire_response,
                                "rewardCoins":int(recorded.get(
                                    "rewardCoins",0) or 0),
                                "points":int(recorded.get("points",0) or 0)})
            STATE.set(ACTIVE_SQBT_MATCH_KEY,active_sqbt)
            if owner_mode=="SQBT":
                owner_receipt=dict(offline_owner)
                owner_receipt.update({"gid":gid,"completed":True,
                                      "result":result})
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner_receipt)
            STATE.set(SELECTED_SQBT_OPPONENT_KEY,0)
            log("sqbt","%s gid=%d opponent=%d result=%s score=%d-%d "
                "points=%d coins=%d balance=%d" % (
                "recorded" if recorded.get("createdNow",True) else "retried",
                gid,opponent_id,result,home,away,points,
                int(recorded.get("rewardCoins",0) or 0),
                int(recorded.get("credits",STATE.credits()) or 0)))
            return J(wire_response)
        active_totw=STATE.get(ACTIVE_TOTW_MATCH_KEY,{})
        if (owner_allows("TOTW") and gid>0 and
                isinstance(active_totw,dict) and
                int(active_totw.get("gid",0) or 0)==gid):
            end_reason=str(payload.get("endReason","") or "").upper()
            explicit_result=str(payload.get("result","") or "").upper()
            if end_reason in ("QUIT","DNF","DISCONNECT","FORFEIT"):
                result="LOSS"
            elif end_reason in ("WIN","LOSS","DRAW"):
                result=end_reason
            elif explicit_result in ("WIN","LOSS","LOSE","DRAW"):
                result=explicit_result
            else:
                log("totw","match/end missing result gid=%d reason=%s" %
                    (gid,end_reason or "<empty>"))
                return J({})
            challenge_id=int(active_totw.get("challengeId",0) or 0)
            difficulty=max(1,min(len(TOTW_DIFFICULTIES),int(
                active_totw.get("difficulty",3) or 3)))
            award=next(a for d,_n,a in TOTW_DIFFICULTIES if d==difficulty)
            reward=award
            try:
                completed=STATE.complete_totw_challenge(
                    challenge_id,difficulty,result,reward,
                    "totw-match:%d" % gid,
                    payload.get("home",payload.get("homeGoals",0)),
                    payload.get("away",payload.get("awayGoals",0)))
            except ValueError as exc:
                log("totw","match/end rejected gid=%d: %s" % (gid,exc))
                return J({})
            active_totw=dict(active_totw)
            active_totw.update({"completed":True,"result":result,
                                "operationKey":"totw-match:%d" % gid})
            STATE.set(ACTIVE_TOTW_MATCH_KEY,active_totw)
            if owner_mode=="TOTW":
                owner_receipt=dict(offline_owner)
                owner_receipt.update({"gid":gid,"completed":True,
                                      "result":result})
                STATE.set(ACTIVE_OFFLINE_MATCH_OWNER_KEY,owner_receipt)
            log("totw","%s gid=%d challenge=%d result=%s reward=%d" % (
                "recorded" if completed.get("createdNow",True) else "retried",
                gid,challenge_id,result,reward))
            return J(_totw_destroy_match_response(
                result,end_reason,reward,completed.get("credits")))
        # A present owner is authoritative even when a malformed or stale gid
        # is received. Mode-tagged fallbacks are retained only for legacy/local
        # callers that predate CreateMatch correlation.
        if owner_mode:
            log("match","match/end ignored gid=%d explicit owner=%s ownerGid=%d" %
                (gid,owner_mode,owner_gid))
            return J({})
        raw_mode=str(payload.get("mode",payload.get("gameMode","OFFLINE"))).upper()
        if "DRAFT" in raw_mode or payload.get("draftId"):
            try: draft_id=int(payload.get("draftId",0) or 0)
            except (TypeError,ValueError): draft_id=0
            session=STATE.draft_session(draft_id) if draft_id else \
                    STATE.current_draft("SINGLE_PLAYER")
            if not session: return J({"success":False,"errorCode":461})
            updated=STATE.record_draft_match(
                int(session["id"]),payload.get("result","WIN"),
                payload.get("home",payload.get("homeGoals",0)),
                payload.get("away",payload.get("awayGoals",0)),
                _draft_match_history_stats(payload,session))
            if not updated: return J({"success":False,"errorCode":461})
            STATE.set("objective_matches_played",
                      int(STATE.get("objective_matches_played",0) or 0)+1)
            return J({"success":True,"rewards":{"coins":0},
                      **_draft_state_payload(updated["mode"],updated)})
        if "SQBT" in raw_mode or "SQUAD_BATTLE" in raw_mode:
            event=_ensure_sqbt_event()
            opponent_id=_sqbt_request_integer(payload,_SQBT_OPPONENT_FIELDS,0)
            if not opponent_id and event["opponents"]:
                opponent_id=int(event["opponents"][0]["opponentId"])
            if not any(int(value.get("opponentId",0) or 0)==opponent_id
                       for value in event["opponents"]):
                return J({"success":False,"errorCode":461})
            difficulty=max(1,min(7,_sqbt_request_integer(
                payload,_SQBT_DIFFICULTY_FIELDS,3)))
            result=str(payload.get("result","WIN")).upper()
            if result=="LOSE": result="LOSS"
            opponent=next((value for value in event.get("opponents",[])
                           if int(value.get("opponentId",0) or 0)==opponent_id),{})
            home,away,_has_away=_sqbt_result_scores(payload)
            coin_breakdown=_sqbt_match_coin_breakdown(
                payload,result,payload.get("endReason",result))
            points=sqbt_match_points(difficulty,result,home,away,
                                     opponent.get("stars",3.0)) \
                   if coin_breakdown["completed"] else 0
            recorded=STATE.record_sqbt_match(
                int(event["id"]),opponent_id,result,difficulty,points,
                home,away,coin_breakdown["matchCoins"],
                payload.get("endReason",result))
            if not recorded: return J({"success":False,"errorCode":461})
            if recorded.get("createdNow"):
                STATE.set("objective_squad_battles_matches",
                          int(STATE.get("objective_squad_battles_matches",0) or 0)+1)
                if result=="WIN":
                    STATE.set("objective_squad_battles_wins",
                              int(STATE.get("objective_squad_battles_wins",0) or 0)+1)
            return J({"success":True,"matchResult":recorded,
                      "sqbtEvent":_sqbt_wire_hub(_sqbt_hub_payload())})
        if "TOTW" in raw_mode:
            result=str(payload.get("result","WIN") or "WIN").upper()
            challenge_id=int(payload.get("challengeId",0) or
                             STATE.get("totw_challenge_id",0) or 0)
            difficulty=max(1,min(len(TOTW_DIFFICULTIES),int(
                payload.get("difficulty",STATE.get("totw_difficulty",3)) or 3)))
            operation_key=(payload.get("operationKey") or
                           payload.get("requestId") or
                           ("totw-match:%d" % gid if gid>0 else None))
            if challenge_id<=0 or operation_key in (None,""):
                return J({"success":False,"errorCode":461})
            award=next(a for d,_n,a in TOTW_DIFFICULTIES if d==difficulty)
            reward=award
            try:
                completed=STATE.complete_totw_challenge(
                    challenge_id,difficulty,result,reward,operation_key,
                    payload.get("home",payload.get("homeGoals",0)),
                    payload.get("away",payload.get("awayGoals",0)))
            except ValueError as exc:
                return J({"success":False,"errorCode":461,
                          "message":str(exc)})
            return J(completed)
        reward = 400
        STATE.record_match("OFFLINE", str(payload.get("result","WIN")).upper(),
                           payload.get("home",0), payload.get("away",0), reward)
        STATE.set("objective_matches_played",
                  int(STATE.get("objective_matches_played",0) or 0)+1)
        return J({"rewards":{"coins":reward},**_credits_payload()})

    # Diagnostic fallback: normally unused because Blaze configuration no
    # longer advertises a nonexistent squad update.
    if low=="/roster": return ROSTER_XML
    if low.startswith("/contentfifa") or low.endswith((".csv",".xml",".big")): return b""
    if low.startswith("/ut/game/fifa19"):
        # An unmatched squad call is what made a copy look successful while doing
        # nothing, so name it in the log instead of letting it vanish into {}.
        if "squad" in low and method != "GET":
            log("fut", "UNHANDLED squad route %s %s body=%s"
                % (method, path, json.dumps(payload)[:200]))
        if "season" in low:
            log("seasons","UNHANDLED route %s %s body=%s" % (
                method,path,json.dumps(payload,sort_keys=True)[:1200]))
        return J({})
    return J({})


def _v1_3865658_route_adapter(method,path,body):
    """Dispatch the v1 route surface through the shared domain core."""
    return fut_route(method,path,body)


def _eaapp_4052077_route_adapter(method,path,body):
    """Dispatch the EA App route surface through the shared domain core."""
    return fut_route(method,path,body)


def _v1_3865658_dto_adapter(response):
    """Retain the v1 wire result until a parser fixture proves divergence."""
    return response


def _eaapp_4052077_dto_adapter(response):
    """Retain the EA wire result until a parser fixture proves divergence."""
    return response


# Route and DTO identities must select executable code, even while a statically
# verified contract is shared.  Keeping separate wrappers prevents a future
# build-specific serializer from silently affecting the other CardsDLL.
FUT_ROUTE_ADAPTERS={
    "fut19-v1-3865658-routes":_v1_3865658_route_adapter,
    "fut19-eaapp-4052077-routes":_eaapp_4052077_route_adapter,
}
FUT_DTO_ADAPTERS={
    "cardsdll-v1-3865658-dtos":_v1_3865658_dto_adapter,
    "cardsdll-eaapp-4052077-dtos":_eaapp_4052077_dto_adapter,
}
_ACTIVE_RUNTIME_DISPATCH=None


def configure_runtime_adapter(expected_server_mode=None):
    """Bind the exact route and DTO implementations before opening a port."""
    global _ACTIVE_RUNTIME_DISPATCH
    profile=require_runtime_adapter_profile(expected_server_mode)
    expected_namespace=(profile.data_namespace+
                        ("-rtg" if PROFILE_MODE=="RTG" else ""))
    actual_namespace=os.path.basename(os.path.normpath(DATA_ROOT))
    if os.path.normcase(actual_namespace)!=os.path.normcase(expected_namespace):
        raise RuntimeError(
            "build profile %s requires data namespace %s, not %s" % (
                profile.profile_id,expected_namespace,
                actual_namespace or "<empty>"))
    route_adapter=FUT_ROUTE_ADAPTERS.get(profile.route_adapter_id)
    if not callable(route_adapter):
        raise RuntimeError("no route adapter registered for %s" %
                           profile.route_adapter_id)
    dto_adapter=FUT_DTO_ADAPTERS.get(profile.dto_adapter_id)
    if not callable(dto_adapter):
        raise RuntimeError("no DTO adapter registered for %s" %
                           profile.dto_adapter_id)
    _ACTIVE_RUNTIME_DISPATCH=(profile,route_adapter,dto_adapter)
    return profile


def dispatch_runtime_fut_route(method,path,body):
    """Serve one request only through the previously selected build pair."""
    dispatch=_ACTIVE_RUNTIME_DISPATCH
    if dispatch is None:
        raise RuntimeError("LocalFUT19 runtime adapters are not configured")
    _profile,route_adapter,dto_adapter=dispatch
    return dto_adapter(route_adapter(method,path,body))

_HTTP_LOG_LOCK=threading.Lock()
_HTTP_LOG_COUNTS={}
_HTTP_BODY_TRACED=set()
_SENSITIVE_HTTP_BODY_ROUTES={"/pow/auth","/ut/auth"}

def _http_request_body_preview(low,body):
    """Return a diagnostic preview without persisting auth/session material."""
    if low in _SENSITIVE_HTTP_BODY_ROUTES:
        return "<redacted-sensitive-request bodyBytes=%d>" % len(body)
    try:
        preview=body[:512].decode("utf-8","replace")
    except Exception:
        return "<binary-request bodyBytes=%d>" % len(body)
    return preview.replace("\r"," ").replace("\n"," ")

_PLAYERHEAD_DELIVERY_LOCK=threading.Lock()
_PLAYERHEAD_NEXT_DELIVERY=[0.0]
# A loopback server can finish dozens of BC3 textures in the same render tick,
# unlike the original CDN.  FIFA 19's small transfer-carousel upload queue then
# cancels later pipelined requests and falls back to the base Gold face.  A
# 12 ms completion cadence keeps HTTP concurrent while feeding the native GPU
# texture queue at a rate it can retain (24 portraits still arrive in <0.3 s).
_PLAYERHEAD_DELIVERY_INTERVAL=0.012

class FUTHandler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"
    def log_message(self,*a): pass
    def _h(self):
        n=int(self.headers.get("Content-Length","0") or "0")
        body=self.rfile.read(n) if n else b""
        low=self.path.lower().split("?",1)[0]
        status=200; content_type="application/json; charset=utf-8"
        route_method="GET" if self.command=="HEAD" else self.command
        try: resp=dispatch_runtime_fut_route(route_method,self.path,body)
        except Exception as e: log("fut","error %s: %r"%(self.path,e)); resp=b"{}"; status=500
        # A route may answer with (body, status). Returning 200 for a refused
        # SBC submission made the client close the builder and go back to the
        # set with nothing said, so the player saw the challenge simply not
        # happen; live on 2026-09-04 with "Player Level: Exactly Bronze".
        if isinstance(resp,tuple):
            resp,status=resp[0],int(resp[1])
        static_content_type=_static_content_type(low)
        if low=="/sponsored-events":
            status=204; content_type="text/plain; charset=utf-8"
        elif low=="/roster": content_type="application/xml; charset=utf-8"
        elif (low in ("/ut/game/fifa19/clientdata/totw",
                      "/ut/game/fifa19/squad/mode/draft/state",
                      "/ut/game/fifa19/draft/mode/draft/state",
                      "/ut/game/fifa19/match",
                      "/ut/game/fifa19/ready") or
              _re.match(
                  r"^/ut/game/fifa19/(?:(?:squad|draft)/mode/)?purchase/mode/\d+/draft$",
                  low) or
              _re.match(
                  r"^/ut/game/fifa19/(?:squad|draft)/mode/\d+/draft/grant/award$",
                  low) or
              _re.match(
                  r"^/ut/game/fifa19/(?:squad|draft)/mode/\d+/draft$",low) or
              _re.match(
                  r"^/ut/game/fifa19/(?:squad|draft)/mode/\d+/draft/choices/"
                  r"(?:player|captain|formation|difficulty|manager)$",low)):
            # The retail RS4 response dispatcher classifies these payloads
            # before invoking the current-state or purchase response decoder.
            # Keep the historic FUT MIME token exact: appending a charset can
            # make a mandatory body arrive at the dispatcher with size zero
            # even though ProtoHttp received every byte from the socket.
            content_type="application/json"
        elif static_content_type is not None:
            if resp:  # Real content served by the CDN mirror.
                status=200
                content_type=static_content_type
            else:
                status=404; content_type="text/plain; charset=utf-8"
        elif low.endswith(".json") and resp:
            content_type="application/json; charset=utf-8"
        elif low.startswith("/contentfifa") or low.endswith((".csv",".xml",".big")):
            if resp:
                status=200
                content_type=STATIC_CONTENT_TYPES.get(
                    os.path.splitext(low)[1], "application/octet-stream")
            else:
                status=404; content_type="text/plain; charset=utf-8"
        log_key=(self.command,low)
        with _HTTP_LOG_LOCK:
            log_count=_HTTP_LOG_COUNTS.get(log_key,0)+1
            _HTTP_LOG_COUNTS[log_key]=log_count
            trace_body=bool(body) and log_key not in _HTTP_BODY_TRACED and (
                low.startswith("/pow/") or low.startswith("/ut/game/fifa19/"))
            if trace_body: _HTTP_BODY_TRACED.add(log_key)
        noisy_catalog=_re.match(
            r"^/pow/store/game/fifa19/catalog/\d+/item/list$",low) is not None
        if not noisy_catalog or log_count<=8:
            log("fut","%s %s -> %d %d" % (self.command,self.path,status,len(resp)))
        elif log_count==9:
            log("fut","REPETITIVE POW CATALOG GET: additional lines suppressed; responses continue")
        if trace_body:
            log("fut","TRACE request body before %s %s: %s" %
                (self.command,low,_http_request_body_preview(low,body)))
        self.send_response(status); self.send_header("Content-Type",content_type)
        # The Web frontend reads `sid` from /ut/auth JSON, while the native PC
        # client stores the session from this response header. Without it,
        # later calls send an empty `X-UT-SID:` and the onboarding controller
        # does not open `loan/players`.
        if self.command == "POST" and low == "/ut/auth":
            self.send_header("X-UT-SID", LOCAL_UTAS_SID)
        # Static FUT assets are content-addressed by their stable resource ID.
        # Let the renderer retain card portraits/artwork instead of forcing a
        # revalidation after its short in-memory cache expires. API responses
        # remain strictly non-cacheable because they expose mutable user state.
        if static_content_type is not None and status == 200:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length",str(len(resp))); self.send_header("Connection","keep-alive")
        self.end_headers()
        try:
            written=0
            if self.command!="HEAD":
                if _playerhead_request_id(low) is not None:
                    with _PLAYERHEAD_DELIVERY_LOCK:
                        delay=_PLAYERHEAD_NEXT_DELIVERY[0]-time.monotonic()
                        if delay>0:
                            time.sleep(delay)
                        written=self.wfile.write(resp)
                        self.wfile.flush()
                        _PLAYERHEAD_NEXT_DELIVERY[0]=(
                            time.monotonic()+_PLAYERHEAD_DELIVERY_INTERVAL)
                else:
                    written=self.wfile.write(resp)
                    self.wfile.flush()
            if (low in ("/ut/game/fifa19/clientdata/totw",
                        "/ut/game/fifa19/squad/mode/draft/state",
                        "/ut/game/fifa19/draft/mode/draft/state",
                        "/ut/game/fifa19/match",
                        "/ut/game/fifa19/ready") or
                "/purchase/mode/" in low or "/draft/choices/" in low):
                tag="totw" if low=="/ut/game/fifa19/clientdata/totw" else "draft"
                log(tag,"HTTP body flushed bytes=%s payload=%s" %
                    (written,resp.decode("utf-8","replace")[:1200]))
        except Exception as error:
            if (low in ("/ut/game/fifa19/clientdata/totw",
                        "/ut/game/fifa19/squad/mode/draft/state",
                        "/ut/game/fifa19/draft/mode/draft/state",
                        "/ut/game/fifa19/match",
                        "/ut/game/fifa19/ready") or
                "/purchase/mode/" in low or "/draft/choices/" in low):
                tag="totw" if low=="/ut/game/fifa19/clientdata/totw" else "draft"
                log(tag,"HTTP response write failed: %r" % error)
    do_GET=_h; do_HEAD=_h; do_POST=_h; do_PUT=_h; do_DELETE=_h

class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress tracebacks when FIFA abruptly closes a keep-alive socket."""
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # ConnectionAbortedError is what Windows raises as WinError 10053, and
        # FIFA produces it constantly: it pipelines whole batches of requests on
        # one keep-alive socket and drops it as soon as it has what it needs,
        # usually right after a 100 KB /club page or a run of player heads.  It
        # was missing from this tuple, so those printed a full traceback and
        # buried the real errors.
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            log("fut", "HTTP connection closed by client %s:%s" % client_address)
            return
        super().handle_error(request, client_address)

# =====================================================================
#  Redirector TLS
# =====================================================================
def redir_xml():
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<serverinstanceinfo>\n'
        '    <address member="0"><valu>\n'
        '        <hostname>127.0.0.1</hostname><ip>2130706433</ip><port>%d</port>\n'
        '    </valu></address>\n    <secure>0</secure>\n</serverinstanceinfo>' % BLAZE_PORT).encode()
def redir_conn(conn, ctx):
    try: tls=ctx.wrap_socket(conn, server_side=True)
    except Exception:
        try: conn.close()
        except: pass
        return
    try:
        tls.recv(4096)
        xml=redir_xml()
        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"%len(xml)+xml)
        log("redir","redirector request served -> Blaze %d" % BLAZE_PORT)
    except Exception: pass
    finally:
        try: tls.close()
        except: pass

# =====================================================================
#  Server startup (threads)
# =====================================================================
def serve_tcp(port, handler):
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind((LOCAL_BIND_HOST,port)); s.listen(32)
    while True:
        c,a=s.accept(); threading.Thread(target=handler,args=(c,a),daemon=True).start()

def _accept_bounded_client(listener, slots):
    """Reserve capacity before accepting so reconnect storms stay bounded."""
    slots.acquire()
    try:
        return listener.accept()
    except BaseException:
        slots.release()
        raise

def _run_bounded_connection(handler, conn, address, slots):
    try:
        handler(conn,address)
    finally:
        slots.release()

def start_redirector():
    ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(CERT,KEY)
    ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version=ssl.TLSVersion.TLSv1
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception: pass
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind((LOCAL_BIND_HOST,REDIRECT_PORT)); s.listen(32)
    log("redir","TLS listening on %d" % REDIRECT_PORT)
    while True:
        c,a=s.accept(); threading.Thread(target=redir_conn,args=(c,ctx),daemon=True).start()
def start_blaze():
    # Emit the message only after a successful bind.
    s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    s.bind((LOCAL_BIND_HOST,BLAZE_PORT)); s.listen(32)
    log("blaze","listening on %d" % BLAZE_PORT)
    slots=threading.BoundedSemaphore(BLAZE_MAX_CONNECTIONS)
    while True:
        c,a=_accept_bounded_client(s,slots)
        worker=threading.Thread(target=_run_bounded_connection,
            args=(blaze_conn,c,a,slots),daemon=True)
        try:
            worker.start()
        except BaseException:
            try: c.close()
            finally: slots.release()
            raise
def start_fut():
    server=QuietThreadingHTTPServer((LOCAL_BIND_HOST,FUT_PORT), FUTHandler)
    log("fut","HTTP listening on %d" % FUT_PORT)
    server.serve_forever()

# =====================================================================
#  Frida hook - attach to FIFA19.exe and neutralize certificate pinning
# =====================================================================
_PIM_ICON_ALIASES={}
_PIM_ICON_BASE_ALIASES={}
for _pim_exact,_pim_wire in _PIM_EXACT_TO_WIRE.items():
    _pim_alias=int(player_presentation_resource_id(_pim_exact))
    # Accept both the new legacy-client wire identity and the exact definition
    # so already-decoded/cached cards fail safely into the same narrow hooks.
    _PIM_ICON_ALIASES[str(_pim_wire)]=_pim_alias
    _PIM_ICON_ALIASES[str(_pim_exact)]=_pim_alias
    _existing_base_alias=_PIM_ICON_BASE_ALIASES.get(str(_pim_alias))
    if _existing_base_alias not in (None,_pim_alias):
        raise RuntimeError("Prime Icon Moments base alias collision")
    _PIM_ICON_BASE_ALIASES[str(_pim_alias)]=_pim_alias
if len(_PIM_ICON_ALIASES)!=88:
    raise RuntimeError("Prime Icon Moments compatibility requires 88 aliases")
if len(_PIM_ICON_BASE_ALIASES)!=44:
    raise RuntimeError("Prime Icon Moments compatibility requires 44 base aliases")
_PIM_ICON_ALIASES_JSON=json.dumps(
    _PIM_ICON_ALIASES,sort_keys=True,separators=(",",":"))
_PIM_ICON_BASE_ALIASES_JSON=json.dumps(
    _PIM_ICON_BASE_ALIASES,sort_keys=True,separators=(",",":"))

FRIDA_JS = """
'use strict';
const base = Process.enumerateModules()[0].base;
Interceptor.attach(base.add(%d), {
  onLeave(r){ if (r.toInt32() !== %d) r.replace(ptr(%d)); }
});

// ItsAMe_Origin.dll implements OriginCheckOnline as `xor eax,eax; ret`: it
// returns ORIGIN_SUCCESS without setting the bool* passed by FIFA. The call
// site at FIFA19.exe+0x1886549 loads RCX from [rdi+0x940], calls the bridge and
// ignores EAX, so the online state must be written to the output buffer.
let originHooksInstalled = false;
let originRetryTimer = null;

function installOriginHooks() {
  if (originHooksInstalled) return true;
  const originModule = Process.findModuleByName('ItsAMe_Origin.dll');
  if (originModule === null) return false;
  // Prevent duplicate installation if the timer fires while FIFA slots are
  // being enumerated and replaced.
  originHooksInstalled = true;
  const fifaModule = Process.enumerateModules()[0];
  const fifaEnd = fifaModule.base.add(fifaModule.size);
  const originKeepAlive = [];
  const wrapperByName = {};
  // The OriginGetProfile callback used by FIFA receives an integer event and a
  // pointer to its value. The legacy Origin wrapper reports success without
  // completing the request, so keep a `true` payload for one asynchronous
  // online-profile notification.
  const originProfileOnlinePayload = Memory.alloc(4);
  originProfileOnlinePayload.writeS32(1);
  let originProfileOnlineEventSent = false;
  // The legacy OriginQueryEntitlements wrapper reports success but does not
  // create the enumeration or invoke the completion handler. FIFA passes the
  // handle pointer through the stack slot observed as args[12]. Keep a stable
  // opaque handle and complete the query with zero results.
  const originEmptyEnumerationHandle = Memory.alloc(0x20);
  originEmptyEnumerationHandle.writeU64(0);
  const originEmptyFriendsHandle = Memory.alloc(0x20);
  originEmptyFriendsHandle.writeU64(0);
  const originEmptyBlockedUsersHandle = Memory.alloc(0x20);
  originEmptyBlockedUsersHandle.writeU64(0);
  const originEmptyWalletBalance = Memory.alloc(8);
  originEmptyWalletBalance.writeU64(0);
  // FIFA reuses the OSDK_UNDERAGE_ERROR popup when Origin provides no text for
  // an error that is not the underage code. Keep a stable string to repair
  // only that missing value without changing a real 0xA2000012 error.
  const originEligibilityFallbackText = Memory.allocUtf8String('Login Success');
  // After login FIFA builds the Identity connect/token form. The wrapper may
  // leave application credentials null, while form encoding always expects
  // valid C strings.
  const originLocalClientId = Memory.allocUtf8String('fifa19-local-client');
  const originLocalClientSecret = Memory.allocUtf8String('localfut19-client-secret');
  const originLocalRedirectUri = Memory.allocUtf8String(
    'http://127.0.0.1:8199/identity/callback');
  const originLocalAuthCodeValue = 'localfut19-auth-code';
  const originLocalAuthCode = Memory.allocUtf8String(originLocalAuthCodeValue);
  const originEntitlementCallbackAddress = fifaModule.base.add(0x4470850);
  let originEntitlementCallback = null;
  let originEntitlementCompletionCount = 0;

  function summarizeOriginArg(value) {
    let summary = value.toString();
    try {
      const range = Process.findRangeByAddress(value);
      if (range !== null) summary += '/' + range.protection;
      const module = Process.findModuleByAddress(value);
      if (module !== null) summary += '/' + module.name + '+' + value.sub(module.base);
    } catch (_) {}
    return summary;
  }

  function previewOriginArg(value) {
    let summary = summarizeOriginArg(value);
    try {
      const range = Process.findRangeByAddress(value);
      if (range !== null && range.protection.indexOf('r') !== -1) {
        const raw = value.readByteArray(16);
        const bytes = Array.from(new Uint8Array(raw)).map(function (item) {
          return ('0' + item.toString(16)).slice(-2);
        }).join('');
        summary += '/bytes=' + bytes;
      }
    } catch (_) {}
    return summary;
  }

  function makeOriginWrapper(name, target) {
    const argTypes = ['pointer','pointer','pointer','pointer','pointer','pointer'];
    const original = new NativeFunction(target, 'int', argTypes);
    let callCount = 0;
    const wrapper = new NativeCallback(function (a0,a1,a2,a3,a4,a5) {
      callCount++;
      const rawArgs = callCount <= 20
        ? [a0,a1,a2,a3,a4,a5].map(summarizeOriginArg)
        : [];
      const returnAddress = this.returnAddress ? this.returnAddress.toString() : null;
      const result = original(a0,a1,a2,a3,a4,a5);
      let output = a0;
      let forcedOnline = false;
      let previousValue = null;
      let profileNormalized = false;
      let underageBefore = null;
      let personaId = null;
      let profileEventScheduled = false;
      if (name === 'OriginCheckOnline' && !a0.isNull()) {
        try {
          const range = Process.findRangeByAddress(a0);
          if (range !== null && range.protection.indexOf('w') !== -1) {
            previousValue = a0.readU8();
            a0.writeU8(1);
            forcedOnline = true;
          }
        } catch (_) {}
      } else if (name === 'OriginGetProfileSync' && !a3.isNull()) {
        // The legacy Origin wrapper uses an older OriginProfileT: Country,
        // GeoCountry, CommerceCountry and Currency are shifted by +8. FIFA 19
        // reads IsUnderAge at +0x29 and SubscriberLevel at +0x2c. Realign the
        // pointers and clear the actual flags without modifying strings.
        output = a3;
        try {
          const flags = a3.add(0x28);
          const range = Process.findRangeByAddress(flags);
          if (range !== null && range.protection.indexOf('w') !== -1) {
            const country = a3.add(0x28).readPointer();
            const geoCountry = a3.add(0x38).readPointer();
            const commerceCountry = a3.add(0x40).readPointer();
            const commerceCurrency = a3.add(0x48).readPointer();
            underageBefore = a3.add(0x29).readU8();
            personaId = a3.add(8).readU64().toString();
            a3.add(0x20).writePointer(country);
            flags.writeU64(0);
            a3.add(0x30).writePointer(geoCountry);
            a3.add(0x38).writePointer(commerceCountry);
            a3.add(0x40).writePointer(commerceCurrency);
            a3.add(0x48).writeU64(0);
            profileNormalized = true;
          }
        } catch (_) {}
      } else if (name === 'OriginGetProfile' && !a1.isNull() &&
                 !originProfileOnlineEventSent) {
        // FIFA's second argument is the dispatcher observed at
        // FIFA19.exe+0x1404c50. Its event-2 branch reads a DWORD from R8 and,
        // when it equals 1, sets online state with error 0. The compatibility
        // layer does not invoke it, so complete outside the wrapper to avoid
        // re-entering the SDK while the call is active.
        try {
          const callbackRange = Process.findRangeByAddress(a1);
          if (callbackRange !== null && callbackRange.protection.indexOf('x') !== -1) {
            const profileCallback = new NativeFunction(a1, 'int',
              ['int','int','pointer','int']);
            originKeepAlive.push(profileCallback);
            originProfileOnlineEventSent = true;
            profileEventScheduled = true;
            setImmediate(function () {
              try {
                const callbackResult = profileCallback(
                  2, 0, originProfileOnlinePayload, 0);
                send({ event: 'origin-profile-callback', status: 'sent',
                  eventType: 2, value: 1, result: callbackResult.toString() });
              } catch (error) {
                originProfileOnlineEventSent = false;
                send({ event: 'origin-profile-callback', status: 'error',
                  eventType: 2, value: 1, error: String(error) });
              }
            });
          }
        } catch (error) {
          send({ event: 'origin-profile-callback', status: 'schedule-error',
            eventType: 2, value: 1, error: String(error) });
        }
      }
      // Do not capture or symbolize backtraces here: doing so blocked FIFA's
      // thread for tens of seconds and timed out Blaze. Serialization also
      // stops after 20 samples because the frontend may call OriginGetProfile
      // thousands of times; the wrapper must not flood Python or the log file.
      if (callCount <= 20) {
        send({ event: 'origin-call', name: name, callCount: callCount,
          output: output.toString(), previousValue: previousValue,
          forcedOnline: forcedOnline, returnAddress: returnAddress,
          profileNormalized: profileNormalized, underageBefore: underageBefore,
          personaId: personaId, profileEventScheduled: profileEventScheduled,
          args: rawArgs, result: result.toString() });
      } else if (callCount === 21) {
        send({ event: 'origin-call', name: name, callCount: callCount,
          status: 'further-calls-suppressed', output: output.toString(),
          previousValue: previousValue, forcedOnline: forcedOnline,
          returnAddress: returnAddress, profileNormalized: profileNormalized,
          underageBefore: underageBefore, personaId: personaId,
          profileEventScheduled: profileEventScheduled, args: [],
          result: result.toString() });
      }
      return result;
    }, 'int', argTypes);
    originKeepAlive.push(original, wrapper);
    wrapperByName[name] = wrapper;
    return wrapper;
  }

  function replaceFifaSlots(target, wrapper) {
    let count = 0;
    const pattern = target.toMatchPattern();
    Process.enumerateRanges({ protection: 'rw-', coalesce: true }).forEach(function (range) {
      if (range.base.compare(fifaModule.base) < 0 || range.base.compare(fifaEnd) >= 0) return;
      Memory.scanSync(range.base, range.size, pattern).forEach(function (match) {
        try {
          if (match.address.readPointer().equals(target)) {
            match.address.writePointer(wrapper);
            count++;
          }
        } catch (_) {}
      });
    });
    return count;
  }

  // Trace Origin functions whose signatures remain unknown without calling
  // them through an over-declared NativeFunction. The FIFA slot enters an x64
  // trampoline that jumps to the original function, preserving registers,
  // stack and return value. Interceptor observes the trampoline, not legacy stubs.
  // troppo corte (alcune sono soltanto `ret`).
  function makeOriginPassthroughTracer(name, target) {
    const trampoline = Memory.alloc(Process.pageSize);
    Memory.protect(trampoline, Process.pageSize, 'rwx');
    const writer = new X86Writer(trampoline, { pc: trampoline });
    writer.putMovRegAddress('rax', target);
    writer.putJmpReg('rax');
    writer.flush();
    let callCount = 0;
    const listener = Interceptor.attach(trampoline, {
      onEnter(args) {
        callCount++;
        this.originPassiveCallCount = callCount;
        this.originPassiveReturnAddress = this.returnAddress
          ? this.returnAddress.toString() : null;
        // Limit noise and cost on FIFA's thread. Trace only the first 20 calls.
        // The first 20 are sufficient to reconstruct order, arguments and call sites.
        if (callCount <= 20) {
          this.originPassiveArgs = [];
          const argLimit = name === 'OriginQueryEntitlements' ? 16 : 6;
          for (let index = 0; index < argLimit; index++) {
            this.originPassiveArgs.push(
              name === 'OriginQueryEntitlements'
                ? previewOriginArg(args[index])
                : summarizeOriginArg(args[index]));
          }
        } else {
          this.originPassiveArgs = null;
        }
      },
      onLeave(retval) {
        const current = this.originPassiveCallCount;
        if (current <= 20) {
          send({ event: 'origin-passive-call', name: name,
            callCount: current, returnAddress: this.originPassiveReturnAddress,
            args: this.originPassiveArgs, result: retval.toString() });
        } else if (current === 21) {
          send({ event: 'origin-passive-call', name: name,
            callCount: current, status: 'further-calls-suppressed',
            returnAddress: this.originPassiveReturnAddress,
            args: [], result: retval.toString() });
        }
      }
    });
    originKeepAlive.push(trampoline, listener);
    wrapperByName[name] = trampoline;
    return trampoline;
  }

  function scheduleOriginEmptyEntitlementCompletion(context, source, callCount) {
    try {
      if (originEntitlementCallback === null) {
        const callbackRange = Process.findRangeByAddress(
          originEntitlementCallbackAddress);
        if (callbackRange === null || callbackRange.protection.indexOf('x') === -1)
          throw new Error('dispatcher entitlement non eseguibile');
        // void *context, OriginHandleT, totalCount, availableCount, error.
        originEntitlementCallback = new NativeFunction(
          originEntitlementCallbackAddress, 'int',
          ['pointer','pointer','uint32','uint32','int']);
        originKeepAlive.push(originEntitlementCallback);
      }
      setImmediate(function () {
        try {
          const callbackResult = originEntitlementCallback(
            context, originEmptyEnumerationHandle, 0, 0, 0);
          originEntitlementCompletionCount++;
          send({ event: 'origin-entitlements-callback', status: 'sent',
            source: source, callCount: callCount,
            completionCount: originEntitlementCompletionCount,
            handle: originEmptyEnumerationHandle.toString(), total: 0,
            available: 0, errorCode: 0,
            result: callbackResult.toString() });
        } catch (error) {
          send({ event: 'origin-entitlements-callback', status: 'error',
            source: source, callCount: callCount,
            handle: originEmptyEnumerationHandle.toString(),
            error: String(error) });
        }
      });
      return true;
    } catch (error) {
      send({ event: 'origin-entitlements-callback', status: 'schedule-error',
        source: source, callCount: callCount,
        handle: originEmptyEnumerationHandle.toString(),
        error: String(error) });
      return false;
    }
  }

  function makeOriginQueryEntitlementsWrapper(target) {
    // The observed signature uses four registers and nine stack slots. Declaring
    // all thirteen arguments prevents Frida from losing the args[12] output
    // pointer. The original stub is not called because it is only
    // `xor eax,eax; ret` and has no useful side effect.
    const argTypes = new Array(13).fill('pointer');
    let callCount = 0;
    const wrapper = new NativeCallback(function (
      a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12) {
      callCount++;
      const rawArgs = [a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12]
        .map(previewOriginArg);
      const returnAddress = this.returnAddress
        ? this.returnAddress.toString() : null;
      let handleWritten = false;
      let completionScheduled = false;
      try {
        if (!a12.isNull()) {
          const handleRange = Process.findRangeByAddress(a12);
          if (handleRange !== null && handleRange.protection.indexOf('w') !== -1) {
            a12.writePointer(originEmptyEnumerationHandle);
            handleWritten = true;
          }
        }
        completionScheduled = scheduleOriginEmptyEntitlementCompletion(
          a0, 'OriginQueryEntitlements', callCount);
      } catch (error) {
        send({ event: 'origin-entitlements-callback', status: 'schedule-error',
          callCount: callCount, handle: originEmptyEnumerationHandle.toString(),
          error: String(error) });
      }
      send({ event: 'origin-entitlements-query', callCount: callCount,
        returnAddress: returnAddress, args: rawArgs,
        handle: originEmptyEnumerationHandle.toString(),
        handleWritten: handleWritten,
        completionScheduled: completionScheduled, result: '0x0' });
      return 0;
    }, 'int', argTypes);
    originKeepAlive.push(wrapper);
    wrapperByName.OriginQueryEntitlements = wrapper;
    return wrapper;
  }

  function installInternalEmptyEntitlementsWrapper() {
    // The second entitlement group bypasses the DLL slot and calls FIFA's
    // embedded SDK implementation directly. In the legacy wrapper build,
    // [SDK+0x4c0] is null and the original crashes while reading 0x98. The
    // 0x4471c3b call-site signature places the output handle in the caller's
    // +0x58 stack slot, which is args[11] at NativeCallback entry.
    const target = fifaModule.base.add(0x175ace0);
    if (target.readU8() !== 0x40 || target.add(1).readU8() !== 0x55)
      throw new Error('unexpected internal entitlement-query signature');
    const argTypes = new Array(12).fill('pointer');
    let callCount = 0;
    const wrapper = new NativeCallback(function (
      a0,a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11) {
      callCount++;
      let handleWritten = false;
      try {
        if (!a11.isNull()) {
          const outputRange = Process.findRangeByAddress(a11);
          if (outputRange !== null && outputRange.protection.indexOf('w') !== -1) {
            a11.writePointer(originEmptyEnumerationHandle);
            handleWritten = true;
          }
        }
      } catch (_) {}
      const completionScheduled = scheduleOriginEmptyEntitlementCompletion(
        a0, 'FIFA19.exe+0x175ace0', callCount);
      send({ event: 'origin-internal-entitlements-query', callCount: callCount,
        returnAddress: this.returnAddress ? this.returnAddress.toString() : null,
        sdk: a0.toString(), user: a1.toString(), filters: a2.toString(),
        filterCount: a3.toString(), output: a11.toString(),
        handle: originEmptyEnumerationHandle.toString(),
        handleWritten: handleWritten,
        completionScheduled: completionScheduled, result: '0x0' });
      return 0;
    }, 'int', argTypes);
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(wrapper);
    return true;
  }

  function installInternalWalletBalanceWrapper() {
    // The call-site string "OriginGetWalletBalance entered" confirms the
    // function name. The legacy wrapper enters the same uninitialized SDK
    // backend and crashes at [SDK+0x4c0]+0x98. FIFA's fixed callback receives
    // context, a balance pointer and an error code.
    const target = fifaModule.base.add(0x175a690);
    if (target.readU8() !== 0x48 || target.add(1).readU8() !== 0x8b ||
        target.add(2).readU8() !== 0xc4)
      throw new Error('unexpected internal OriginGetWalletBalance signature');
    const callbackAddress = fifaModule.base.add(0x445f920);
    const callbackRange = Process.findRangeByAddress(callbackAddress);
    if (callbackRange === null || callbackRange.protection.indexOf('x') === -1)
      throw new Error('FIFA wallet callback is not executable');
    const walletCallback = new NativeFunction(callbackAddress, 'int',
      ['pointer','pointer','pointer','int']);
    let callCount = 0;
    let completionCount = 0;
    const wrapper = new NativeCallback(function (
      sdk, user, currency, unused, context) {
      callCount++;
      const currentCall = callCount;
      const returnAddress = this.returnAddress
        ? this.returnAddress.toString() : null;
      let currencyValue = currency.toString();
      try {
        if (!currency.isNull()) currencyValue = currency.readCString();
      } catch (_) {}
      let completionScheduled = false;
      try {
        completionScheduled = true;
        setImmediate(function () {
          try {
            const callbackResult = walletCallback(
              context, originEmptyWalletBalance, ptr(0), 0);
            completionCount++;
            send({ event: 'origin-wallet-callback', status: 'sent',
              callCount: currentCall, completionCount: completionCount,
              context: context.toString(), balance: '0', errorCode: 0,
              result: callbackResult.toString() });
          } catch (error) {
            send({ event: 'origin-wallet-callback', status: 'error',
              callCount: currentCall, context: context.toString(),
              error: String(error) });
          }
        });
      } catch (error) {
        send({ event: 'origin-wallet-callback', status: 'schedule-error',
          callCount: currentCall, context: context.toString(),
          error: String(error) });
      }
      send({ event: 'origin-wallet-query', callCount: currentCall,
        returnAddress: returnAddress, sdk: sdk.toString(), user: user.toString(),
        currency: currencyValue, context: context.toString(),
        completionScheduled: completionScheduled, result: '0x0' });
      return 0;
    }, 'int', new Array(5).fill('pointer'));
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(walletCallback, wrapper);
    return true;
  }

  function installInternalEmptyFriendsWrapper() {
    // The FIFA19.exe+0x17664cb crash belongs to the function identified by the
    // caller-wrapper string "OriginQueryFriends entered". Its implementation
    // reads [SDK+0x4c0]+0x18, but the compatibility backend is null. Emulate an
    // empty friends enumeration with the callback contract observed for entitlements:
    // context, handle, totalCount, availableCount, error.
    const target = fifaModule.base.add(0x17663f0);
    if (target.readU8() !== 0x40 || target.add(1).readU8() !== 0x55 ||
        target.add(2).readU8() !== 0x56 || target.add(3).readU8() !== 0x57)
      throw new Error('unexpected internal OriginQueryFriends signature');
    let callCount = 0;
    let completionCount = 0;
    const wrapper = new NativeCallback(function (sdk, user, callback, context) {
      callCount++;
      const currentCall = callCount;
      const returnAddress = this.returnAddress
        ? this.returnAddress.toString() : null;
      let completionScheduled = false;
      let callbackStatus = callback.isNull() ? 'missing' : 'pending';
      try {
        if (!callback.isNull()) {
          const callbackRange = Process.findRangeByAddress(callback);
          if (callbackRange === null || callbackRange.protection.indexOf('x') === -1)
            throw new Error('friends callback is not executable');
          const friendsCallback = new NativeFunction(callback, 'int',
            ['pointer','pointer','uint32','uint32','int']);
          originKeepAlive.push(friendsCallback);
          completionScheduled = true;
          setImmediate(function () {
            try {
              const callbackResult = friendsCallback(
                context, originEmptyFriendsHandle, 0, 0, 0);
              completionCount++;
              send({ event: 'origin-friends-callback', status: 'sent',
                callCount: currentCall, completionCount: completionCount,
                context: context.toString(),
                handle: originEmptyFriendsHandle.toString(), total: 0,
                available: 0, errorCode: 0,
                result: callbackResult.toString() });
            } catch (error) {
              send({ event: 'origin-friends-callback', status: 'error',
                callCount: currentCall, context: context.toString(),
                handle: originEmptyFriendsHandle.toString(),
                error: String(error) });
            }
          });
        }
      } catch (error) {
        callbackStatus = 'schedule-error';
        send({ event: 'origin-friends-callback', status: 'schedule-error',
          callCount: currentCall, context: context.toString(),
          handle: originEmptyFriendsHandle.toString(), error: String(error) });
      }
      send({ event: 'origin-friends-query', callCount: currentCall,
        returnAddress: returnAddress, sdk: sdk.toString(), user: user.toString(),
        callback: callback.toString(), context: context.toString(),
        callbackStatus: callbackStatus,
        handle: originEmptyFriendsHandle.toString(),
        completionScheduled: completionScheduled, result: '0x0' });
      return 0;
    }, 'int', new Array(4).fill('pointer'));
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(wrapper);
    return true;
  }

  function installInternalEmptyBlockedUsersWrapper() {
    // FIFA's caller wrapper contains "OriginQueryBlockedUsers entered" and
    // calls +0x1757630 directly. This routine also dereferences the null
    // [SDK+0x4c0] backend, this time at +0x300. Complete FIFA's fixed callback
    // with zero blocked users without entering the incomplete legacy code.
    const target = fifaModule.base.add(0x1757630);
    if (target.readU8() !== 0x48 || target.add(1).readU8() !== 0x8b ||
        target.add(2).readU8() !== 0xc4 || target.add(3).readU8() !== 0x56)
      throw new Error('unexpected internal OriginQueryBlockedUsers signature');
    const callbackAddress = fifaModule.base.add(0x1887da0);
    const callbackRange = Process.findRangeByAddress(callbackAddress);
    if (callbackRange === null || callbackRange.protection.indexOf('x') === -1)
      throw new Error('FIFA blocked-users callback is not executable');
    const blockedUsersCallback = new NativeFunction(callbackAddress, 'int',
      ['pointer','pointer','uint32','uint32','int']);
    let callCount = 0;
    let completionCount = 0;
    const wrapper = new NativeCallback(function (sdk, user, unused, context) {
      callCount++;
      const currentCall = callCount;
      const returnAddress = this.returnAddress
        ? this.returnAddress.toString() : null;
      let completionScheduled = false;
      try {
        completionScheduled = true;
        setImmediate(function () {
          try {
            const callbackResult = blockedUsersCallback(
              context, originEmptyBlockedUsersHandle, 0, 0, 0);
            completionCount++;
            send({ event: 'origin-blocked-users-callback', status: 'sent',
              callCount: currentCall, completionCount: completionCount,
              context: context.toString(),
              handle: originEmptyBlockedUsersHandle.toString(), total: 0,
              available: 0, errorCode: 0,
              result: callbackResult.toString() });
          } catch (error) {
            send({ event: 'origin-blocked-users-callback', status: 'error',
              callCount: currentCall, context: context.toString(),
              handle: originEmptyBlockedUsersHandle.toString(),
              error: String(error) });
          }
        });
      } catch (error) {
        send({ event: 'origin-blocked-users-callback', status: 'schedule-error',
          callCount: currentCall, context: context.toString(),
          handle: originEmptyBlockedUsersHandle.toString(),
          error: String(error) });
      }
      send({ event: 'origin-blocked-users-query', callCount: currentCall,
        returnAddress: returnAddress, sdk: sdk.toString(), user: user.toString(),
        unused: unused.toString(), context: context.toString(),
        handle: originEmptyBlockedUsersHandle.toString(),
        completionScheduled: completionScheduled, result: '0x0' });
      return 0;
    }, 'int', new Array(4).fill('pointer'));
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(blockedUsersCallback, wrapper);
    return true;
  }

  function installInternalAuthCodeSyncWrapper() {
    // FIFA call site +0xc437c4a and the SDK wrapper contain the string
    // "OriginRequestAuthCodeSync entered". The original +0x17438f0 builds the
    // request from [SDK+0x4c0]+0x120 and crashes at +0x1743993 because the
    // legacy-wrapper backend is null. The caller passes char** and uint64* in
    // stack arguments 5 and 6, then immediately copies the code and length.
    const target = fifaModule.base.add(0x17438f0);
    if (target.readU8() !== 0x4c || target.add(1).readU8() !== 0x89 ||
        target.add(2).readU8() !== 0x4c || target.add(3).readU8() !== 0x24)
      throw new Error('unexpected internal OriginRequestAuthCodeSync signature');
    let callCount = 0;
    const wrapper = new NativeCallback(function (
      sdk, user, request, ignoredFourth, outputCode, outputLength) {
      callCount++;
      let codeWritten = false;
      let lengthWritten = false;
      try {
        if (!outputCode.isNull()) {
          outputCode.writePointer(originLocalAuthCode);
          codeWritten = true;
        }
      } catch (_) {}
      try {
        if (!outputLength.isNull()) {
          outputLength.writeU64(originLocalAuthCodeValue.length);
          lengthWritten = true;
        }
      } catch (_) {}
      const result = codeWritten && lengthWritten ? 0 : 0xa2000004;
      send({ event: 'origin-auth-code-sync', callCount: callCount,
        returnAddress: this.returnAddress ? this.returnAddress.toString() : null,
        requestNull: request.isNull(),
        codeWritten: codeWritten, lengthWritten: lengthWritten,
        length: originLocalAuthCodeValue.length,
        result: '0x' + result.toString(16) });
      return result;
    }, 'int', new Array(6).fill('pointer'));
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(wrapper);
    return true;
  }

  function installInternalGrantAchievementGuard() {
    // Origin::OriginSDK::GrantAchievement, FIFA19.exe+0x1755d00, named by its
    // own error strings ("Origin::OriginSDK::GrantAchievement" at +0x5128900,
    // originsdkachievements.cpp at +0x51288a0). FIFA's AchievementService
    // reaches it with a direct call from +0x1ac4c6b; entering the Squad Battles
    // hub is one of the triggers. The original loads the achievement string
    // from [SDK+0x4c0]+0x380 at +0x1755e46 and faults at +0x1755e54
    // (`cmp qword [rdi + 0x18], 0x10`, read of 0x398) because the legacy
    // wrapper backend is null, exactly like OriginRequestAuthCodeSync at
    // +0x1743993 and OriginGetWalletBalance at [SDK+0x4c0]+0x98. The call site
    // ignores the return value (`call` then `jmp` straight to the epilogue) and
    // registers no completion, so reporting success without building the
    // request object is the entire contract.
    const target = fifaModule.base.add(0x1755d00);
    if (target.readU8() !== 0x48 || target.add(1).readU8() !== 0x8b ||
        target.add(2).readU8() !== 0xc4 || target.add(3).readU8() !== 0x57)
      throw new Error('unexpected internal GrantAchievement signature');
    let callCount = 0;
    const wrapper = new NativeCallback(function (sdk, user, achievement, value) {
      callCount++;
      let backend = null;
      try {
        backend = sdk.add(0x4c0).readPointer().toString();
      } catch (_) {}
      let achievementValue = achievement.toString();
      try {
        if (!achievement.isNull()) achievementValue = achievement.readCString();
      } catch (_) {}
      send({ event: 'origin-achievement-grant', callCount: callCount,
        returnAddress: this.returnAddress ? this.returnAddress.toString() : null,
        sdk: sdk.toString(), user: user.toString(),
        achievement: achievementValue, value: value.toString(),
        backend: backend, result: '0x0' });
      return 0;
    }, 'int', new Array(4).fill('pointer'));
    Interceptor.replace(target, wrapper);
    originKeepAlive.push(wrapper);
    return true;
  }

  // Every Origin::OriginSDK entry point that dereferences the null
  // [SDK+0x4c0] backend and that FIFA reaches with a direct call. `guarded`
  // marks the ones this script replaces; `detoured` reports whether the entry
  // point already starts with a jump, which is how ItsAMe_Origin.dll covers the
  // APIs it implements. An entry that is neither guarded nor detoured is a
  // guaranteed access violation the first time FIFA reaches it, so the census
  // names the next crash before it happens instead of after.
  const originNullSdkLandmines = [
    { name: 'RequestAuthCodeSync', rva: 0x17438f0, guarded: true },
    { name: 'GetSettingSync', rva: 0x1743d80, guarded: false },
    { name: 'GetGameInfoSync', rva: 0x17441a0, guarded: false },
    { name: 'QueryUserIdSync', rva: 0x17467b0, guarded: false },
    { name: 'AreChunksInstalledSync', rva: 0x17510b0, guarded: false },
    { name: 'GrantAchievement', rva: 0x1755d00, guarded: true },
    { name: 'QueryBlockedUsers', rva: 0x1757630, guarded: true },
    { name: 'GetWalletBalance', rva: 0x175a690, guarded: true },
    { name: 'ShowCodeRedemptionUISync', rva: 0x175aa80, guarded: false },
    { name: 'SendXml', rva: 0x175ace0, guarded: true },
    { name: 'Checkout', rva: 0x175b290, guarded: false },
    { name: 'ShowCheckoutUISync', rva: 0x175b740, guarded: false },
    { name: 'QueryEntitlements', rva: 0x175bad0, guarded: false },
    { name: 'ExtendTrial', rva: 0x1762be0, guarded: false },
    { name: 'ShowIGOWindowSync', rva: 0x1764480, guarded: false },
    { name: 'QueryFriends', rva: 0x17663f0, guarded: true },
    { name: 'GetPresence', rva: 0x1767c10, guarded: false },
    { name: 'GetProfileSync', rva: 0x1769df0, guarded: false }
  ];

  function reportNullSdkLandmines() {
    const entries = [];
    originNullSdkLandmines.forEach(function (landmine) {
      const address = fifaModule.base.add(landmine.rva);
      let head = null;
      let detoured = null;
      try {
        head = address.readU8();
        detoured = head === 0xe9 || head === 0xeb ||
          (head === 0xff && address.add(1).readU8() === 0x25);
      } catch (_) {}
      entries.push({ name: landmine.name,
        rva: '0x' + landmine.rva.toString(16),
        guarded: landmine.guarded, detoured: detoured,
        head: head === null ? null : '0x' + head.toString(16) });
    });
    send({ event: 'origin-null-sdk-landmines', entries: entries });
    return entries.length;
  }

  function installEligibilityDecisionGuard() {
    // This FIFA branch selects OSDK_UNDERAGE_ERROR. At +0x1568d78 it compares
    // EAX with 0xA2000012 and then checks the string pointer in [rsp+0x40]. If
    // the error is not underage but the string is absent, fill only the missing
    // value while preserving FIFA's original flow and comparisons.
    const decision = fifaModule.base.add(0x1568d78);
    if (decision.readU8() !== 0x3d || decision.add(1).readU32() !== 0xa2000012)
      throw new Error('unexpected eligibility-decision signature');
    const underageBranch = fifaModule.base.add(0x1568da5);
    if (underageBranch.readU8() !== 0x48 || underageBranch.add(1).readU8() !== 0x8d)
      throw new Error('unexpected OSDK_UNDERAGE_ERROR branch signature');
    let decisionCount = 0;
    let underageCount = 0;
    const decisionListener = Interceptor.attach(decision, {
      onEnter() {
        decisionCount++;
        const sdkErrorValue = this.context.rax.toUInt32();
        const textSlot = this.context.rsp.add(0x40);
        let textPointer = ptr(0);
        let textValue = null;
        let textReadable = true;
        let fallbackApplied = false;
        let state = null;
        let mappedError = null;
        try {
          textPointer = textSlot.readPointer();
          if (!textPointer.isNull()) textValue = textPointer.readCString();
        } catch (error) {
          textReadable = false;
          textValue = '<read error: ' + String(error) + '>';
        }
        if (sdkErrorValue !== 0xa2000012 && textReadable &&
            (textPointer.isNull() || textValue === '')) {
          try {
            textSlot.writePointer(originEligibilityFallbackText);
            fallbackApplied = true;
            send({ event: 'origin-eligibility-fallback',
              callCount: decisionCount,
              sdkError: '0x' + sdkErrorValue.toString(16),
              originalTextPointer: textPointer.toString(),
              replacementTextPointer: originEligibilityFallbackText.toString(),
              replacementText: 'Login Success' });
          } catch (error) {
            send({ event: 'origin-eligibility-fallback-error',
              callCount: decisionCount,
              sdkError: '0x' + sdkErrorValue.toString(16),
              error: String(error) });
          }
        }
        try {
          if (!this.context.rbx.isNull()) {
            state = this.context.rbx.add(0x51c).readS32();
            mappedError = this.context.rbx.add(0x54c).readU32();
          }
        } catch (_) {}
        send({ event: 'origin-eligibility-decision',
          callCount: decisionCount,
          sdkError: '0x' + sdkErrorValue.toString(16),
          textPointer: textPointer.toString(), textValue: textValue,
          fallbackApplied: fallbackApplied,
          state: state, mappedError: mappedError === null ? null :
            '0x' + mappedError.toString(16),
          rbx: this.context.rbx.toString(), r15: this.context.r15.toString() });
      }
    });
    const underageListener = Interceptor.attach(underageBranch, {
      onEnter() {
        underageCount++;
        send({ event: 'origin-underage-branch', callCount: underageCount,
          sdkError: '0x' + this.context.rax.toUInt32().toString(16),
          rbx: this.context.rbx.toString() });
      }
    });
    originKeepAlive.push(decisionListener, underageListener);
    return true;
  }

  function installIdentityCredentialsGuard() {
    // FIFA obtains client_id in RSI from method +0x90, client_secret in R14
    // from method +0x88 and redirect_uri in R15 from method +0x98. The first
    // builder starts at +0x15691fe; intercept here to validate all three fields
    // before +0x156fd94 calls strlen.
    const useSite = fifaModule.base.add(0x15691fe);
    if (useSite.readU8() !== 0x4c || useSite.add(1).readU8() !== 0x8b ||
        useSite.add(2).readU8() !== 0xc6)
      throw new Error('unexpected Identity credential-use signature');
    let callCount = 0;
    const listener = Interceptor.attach(useSite, {
      onEnter() {
        callCount++;
        const originalClientId = this.context.rsi;
        const originalClientSecret = this.context.r14;
        const originalRedirectUri = this.context.r15;
        let clientIdFallbackApplied = false;
        let clientSecretFallbackApplied = false;
        let redirectUriFallbackApplied = false;
        if (originalClientId.isNull()) {
          this.context.rsi = originLocalClientId;
          clientIdFallbackApplied = true;
        }
        if (originalClientSecret.isNull()) {
          this.context.r14 = originLocalClientSecret;
          clientSecretFallbackApplied = true;
        }
        if (originalRedirectUri.isNull()) {
          this.context.r15 = originLocalRedirectUri;
          redirectUriFallbackApplied = true;
        }
        send({ event: 'origin-identity-credentials', callCount: callCount,
          clientIdWasMissing: originalClientId.isNull(),
          clientSecretWasMissing: originalClientSecret.isNull(),
          redirectUriWasMissing: originalRedirectUri.isNull(),
          clientIdFallbackApplied: clientIdFallbackApplied,
          clientSecretFallbackApplied: clientSecretFallbackApplied,
          redirectUriFallbackApplied: redirectUriFallbackApplied });
      }
    });
    originKeepAlive.push(listener);
    return true;
  }

  function installEmptyEnumerationReadWrapper() {
    // FIFA calls this static thunk after the callback even when item count and
    // buffer size are both zero. The original enters the full SDK, whose
    // legacy-wrapper enumeration registry is null. For an empty response the
    // correct contract is itemsRead=0 and ORIGIN_SUCCESS without reading a handle.
    const thunk = fifaModule.base.add(0x1740640);
    if (thunk.readU8() !== 0xe9)
      throw new Error('unexpected OriginReadEnumerationSync thunk signature');
    const argTypes = new Array(6).fill('pointer');
    let callCount = 0;
    const wrapper = new NativeCallback(function (handle, buffer, bufferSize,
                                                   ignoredFourth, startIndex, itemsRead) {
      callCount++;
      let itemsReadWritten = false;
      try {
        if (!itemsRead.isNull()) {
          // FIFA jobs use fiber stacks that Process.findRangeByAddress does not
          // always classify as `rw-`, even when the pointer is valid and read
          // immediately after return. Attempt the write inside this catch;
          // leaving it uninitialized makes random memory look like an item count.
          itemsRead.writeU64(0);
          itemsReadWritten = true;
        }
      } catch (_) {}
      send({ event: 'origin-empty-enumeration-read', callCount: callCount,
        handle: handle.toString(), buffer: buffer.toString(),
        bufferSize: bufferSize.toString(),
        ignoredFourth: ignoredFourth.toString(),
        startIndex: startIndex.toString(),
        itemsRead: itemsRead.toString(), itemsReadWritten: itemsReadWritten,
        returnAddress: this.returnAddress ? this.returnAddress.toString() : null,
        result: '0x0' });
      return 0;
    }, 'int', argTypes);
    Interceptor.replace(thunk, wrapper);
    originKeepAlive.push(wrapper);
    return true;
  }

  ['OriginCheckOnline', 'OriginGoOnline', 'OriginGetProfileSync',
    'OriginGetProfile', 'OriginCheckPermission'].forEach(function (name) {
    try {
      const target = originModule.findExportByName(name);
      if (target === null) {
        send({ event: 'origin-hook', status: 'export-missing', module: name });
        return;
      }
      const wrapper = makeOriginWrapper(name, target);
      const slots = replaceFifaSlots(target, wrapper);
      send({ event: 'origin-hook', status: 'slot-wrapper-ready:' + slots, module: name });
    } catch (error) {
      send({ event: 'origin-hook', status: 'slot-wrapper-error:' + String(error), module: name });
    }
  });

  ['OriginGetDefaultUser',
    'OriginRegisterEventCallback', 'OriginUnregisterEventCallback',
    'OriginGetGameInfoSync', 'OriginGetSettingSync',
    'OriginReadEnumerationSync'].forEach(function (name) {
    try {
      const target = originModule.findExportByName(name);
      if (target === null) {
        send({ event: 'origin-passive-hook', status: 'export-missing', module: name });
        return;
      }
      const trampoline = makeOriginPassthroughTracer(name, target);
      const slots = replaceFifaSlots(target, trampoline);
      send({ event: 'origin-passive-hook',
        status: 'passthrough-ready:' + slots, module: name });
    } catch (error) {
      send({ event: 'origin-passive-hook',
        status: 'passthrough-error:' + String(error), module: name });
    }
  });

  // Install empty completion only when the downstream synchronous read can
  // also finish without entering the null legacy SDK registry.
  let emptyEnumerationReadReady = false;
  try {
    emptyEnumerationReadReady = installEmptyEnumerationReadWrapper();
    send({ event: 'origin-hook', status: 'empty-read-wrapper-ready',
      module: 'OriginReadEnumerationSync@FIFA19.exe+0x1740640' });
  } catch (error) {
    send({ event: 'origin-hook', status: 'empty-read-wrapper-error:' + String(error),
      module: 'OriginReadEnumerationSync@FIFA19.exe+0x1740640' });
  }

  let internalEmptyEntitlementsReady = false;
  if (emptyEnumerationReadReady) {
    try {
      internalEmptyEntitlementsReady = installInternalEmptyEntitlementsWrapper();
      send({ event: 'origin-hook', status: 'internal-empty-wrapper-ready',
        module: 'OriginQueryEntitlements@FIFA19.exe+0x175ace0' });
    } catch (error) {
      send({ event: 'origin-hook',
        status: 'internal-empty-wrapper-error:' + String(error),
        module: 'OriginQueryEntitlements@FIFA19.exe+0x175ace0' });
    }
  }

  let internalWalletBalanceReady = false;
  if (emptyEnumerationReadReady) {
    try {
      internalWalletBalanceReady = installInternalWalletBalanceWrapper();
      send({ event: 'origin-hook', status: 'internal-wallet-wrapper-ready',
        module: 'OriginGetWalletBalance@FIFA19.exe+0x175a690' });
    } catch (error) {
      send({ event: 'origin-hook',
        status: 'internal-wallet-wrapper-error:' + String(error),
        module: 'OriginGetWalletBalance@FIFA19.exe+0x175a690' });
    }
  }

  let internalEmptyFriendsReady = false;
  if (emptyEnumerationReadReady) {
    try {
      internalEmptyFriendsReady = installInternalEmptyFriendsWrapper();
      send({ event: 'origin-hook', status: 'internal-friends-wrapper-ready',
        module: 'OriginQueryFriends@FIFA19.exe+0x17663f0' });
    } catch (error) {
      send({ event: 'origin-hook',
        status: 'internal-friends-wrapper-error:' + String(error),
        module: 'OriginQueryFriends@FIFA19.exe+0x17663f0' });
    }
  }

  let internalEmptyBlockedUsersReady = false;
  if (emptyEnumerationReadReady) {
    try {
      internalEmptyBlockedUsersReady = installInternalEmptyBlockedUsersWrapper();
      send({ event: 'origin-hook', status: 'internal-blocked-users-wrapper-ready',
        module: 'OriginQueryBlockedUsers@FIFA19.exe+0x1757630' });
    } catch (error) {
      send({ event: 'origin-hook',
        status: 'internal-blocked-users-wrapper-error:' + String(error),
        module: 'OriginQueryBlockedUsers@FIFA19.exe+0x1757630' });
    }
  }

  let internalAuthCodeSyncReady = false;
  try {
    internalAuthCodeSyncReady = installInternalAuthCodeSyncWrapper();
    send({ event: 'origin-hook', status: 'internal-auth-code-wrapper-ready',
      module: 'OriginRequestAuthCodeSync@FIFA19.exe+0x17438f0' });
  } catch (error) {
    send({ event: 'origin-hook',
      status: 'internal-auth-code-wrapper-error:' + String(error),
      module: 'OriginRequestAuthCodeSync@FIFA19.exe+0x17438f0' });
  }

  try {
    installInternalGrantAchievementGuard();
    send({ event: 'origin-hook', status: 'achievement-grant-guard-ready',
      module: 'GrantAchievement@FIFA19.exe+0x1755d00' });
  } catch (error) {
    send({ event: 'origin-hook',
      status: 'achievement-grant-guard-error:' + String(error),
      module: 'GrantAchievement@FIFA19.exe+0x1755d00' });
  }

  try {
    installEligibilityDecisionGuard();
    send({ event: 'origin-hook', status: 'eligibility-missing-text-guard-ready',
      module: 'OSDK_UNDERAGE_ERROR@FIFA19.exe+0x1568d78' });
  } catch (error) {
    send({ event: 'origin-hook', status: 'eligibility-missing-text-guard-error:' + String(error),
      module: 'OSDK_UNDERAGE_ERROR@FIFA19.exe+0x1568d78' });
  }

  try {
    installIdentityCredentialsGuard();
    send({ event: 'origin-hook', status: 'identity-credentials-guard-ready',
      module: 'connect/token@FIFA19.exe+0x15691fe' });
  } catch (error) {
    send({ event: 'origin-hook', status: 'identity-credentials-guard-error:' + String(error),
      module: 'connect/token@FIFA19.exe+0x15691fe' });
  }

  try {
    reportNullSdkLandmines();
  } catch (error) {
    send({ event: 'origin-hook',
      status: 'null-sdk-landmine-census-error:' + String(error),
      module: 'Origin::OriginSDK' });
  }

  try {
    const target = originModule.findExportByName('OriginQueryEntitlements');
    if (target === null) {
      send({ event: 'origin-passive-hook', status: 'export-missing',
        module: 'OriginQueryEntitlements' });
    } else if (emptyEnumerationReadReady && internalEmptyEntitlementsReady &&
               internalWalletBalanceReady && internalEmptyFriendsReady) {
      const wrapper = makeOriginQueryEntitlementsWrapper(target);
      const slots = replaceFifaSlots(target, wrapper);
      send({ event: 'origin-hook',
        status: 'empty-enumeration-wrapper-ready-with-read-bypass:' + slots,
        module: 'OriginQueryEntitlements' });
    } else {
      const trampoline = makeOriginPassthroughTracer(
        'OriginQueryEntitlements', target);
      const slots = replaceFifaSlots(target, trampoline);
      send({ event: 'origin-passive-hook',
        status: 'passthrough-restored-after-dependent-hook-failure:' + slots,
        module: 'OriginQueryEntitlements' });
    }
  } catch (error) {
    send({ event: 'origin-passive-hook',
      status: 'passthrough-restore-error:' + String(error),
      module: 'OriginQueryEntitlements' });
  }

  // Also cover GetProcAddress resolutions performed after attachment.
  try {
    const getProcAddress = Process.getModuleByName('kernel32.dll').getExportByName('GetProcAddress');
    Interceptor.attach(getProcAddress, {
      onEnter(args) {
        this.originResolvedName = null;
        try {
          const name = args[1].readCString();
          if (wrapperByName[name] !== undefined) this.originResolvedName = name;
        } catch (_) {}
      },
      onLeave(retval) {
        if (this.originResolvedName !== null) {
          retval.replace(wrapperByName[this.originResolvedName]);
          send({ event: 'origin-hook', status: 'getproc-wrapper', module: this.originResolvedName });
        }
      }
    });
  } catch (error) {
    send({ event: 'origin-hook', status: 'getproc-hook-error:' + String(error) });
  }
  return true;
}

// Depending on startup timing, Frida may attach before the loader maps
// ItsAMe_Origin.dll. Retry without blocking FIFA's thread and install the same
// wrappers as soon as the module becomes visible.
if (!installOriginHooks()) {
  send({ event: 'origin-hook', status: 'module-waiting' });
  originRetryTimer = setInterval(function () {
    try {
      if (installOriginHooks()) {
        clearInterval(originRetryTimer);
        originRetryTimer = null;
        send({ event: 'origin-hook', status: 'module-loaded-late' });
      }
    } catch (error) {
      if (originRetryTimer !== null) clearInterval(originRetryTimer);
      originRetryTimer = null;
      send({ event: 'origin-hook', status: 'module-late-error:' + String(error) });
    }
  }, 50);
}

// Passive diagnostics for the native-client path after choosing the player to
// replace. The Web App uses /loan/players, but FIFA 19 PC did not send that
// request. Record DNS, new connections and readable HTTP lines mentioning
// UT/loan/onboarding without changing arguments or outcomes.
function installPassiveNetworkTrace() {
  const destinationCounts = {};
  const dnsCounts = {};
  let readableSendCount = 0;
  let readableReceiveCount = 0;

  function exportAddress(module, name) {
    try { return module.getExportByName(name); } catch (_) { return null; }
  }

  function decodeSockaddr(address) {
    if (address.isNull()) return null;
    try {
      const family = address.readU8() | (address.add(1).readU8() << 8);
      const port = (address.add(2).readU8() << 8) | address.add(3).readU8();
      if (family === 2) {
        const octets = [];
        for (let index = 0; index < 4; index++)
          octets.push(address.add(4 + index).readU8());
        return octets.join('.') + ':' + port;
      }
      if (family === 23) {
        const groups = [];
        for (let index = 0; index < 16; index += 2) {
          const value = (address.add(8 + index).readU8() << 8) |
            address.add(9 + index).readU8();
          groups.push(value.toString(16));
        }
        return '[' + groups.join(':') + ']:' + port;
      }
      return 'family=' + family + ',port=' + port;
    } catch (error) {
      return 'sockaddr-error:' + String(error);
    }
  }

  function attachConnect(address, name) {
    if (address === null) return false;
    Interceptor.attach(address, {
      onEnter(args) {
        this.destination = decodeSockaddr(args[1]);
        this.caller = this.returnAddress ? this.returnAddress.toString() : null;
      },
      onLeave(result) {
        if (this.destination === null) return;
        const count = (destinationCounts[this.destination] || 0) + 1;
        destinationCounts[this.destination] = count;
        // Local HTTP connections are numerous: keep the initial requests and
        // periodic samples, while always reporting a new destination.
        if (count <= 12 || count %% 50 === 0) {
          send({ event: 'network-connect', api: name,
            destination: this.destination, count: count,
            result: result.toInt32(), caller: this.caller });
        }
      }
    });
    return true;
  }

  function attachDns(address, name, wide) {
    if (address === null) return false;
    Interceptor.attach(address, {
      onEnter(args) {
        let host = null;
        try {
          if (!args[0].isNull())
            host = wide ? args[0].readUtf16String() : args[0].readUtf8String();
        } catch (_) {}
        if (!host) return;
        const count = (dnsCounts[host] || 0) + 1;
        dnsCounts[host] = count;
        if (count <= 5 || count %% 25 === 0)
          send({ event: 'network-dns', api: name, host: host, count: count });
      }
    });
    return true;
  }

  try {
    const ws2 = Process.findModuleByName('ws2_32.dll');
    if (ws2 === null) {
      send({ event: 'network-hook', status: 'ws2_32-missing' });
      return false;
    }
    const attached = [];
    const seenAddresses = {};
    [['connect', 'connect'], ['WSAConnect', 'WSAConnect']].forEach(function (item) {
      const address = exportAddress(ws2, item[0]);
      const key = address === null ? null : address.toString();
      if (key !== null && !seenAddresses[key]) {
        seenAddresses[key] = true;
        if (attachConnect(address, item[1])) attached.push(item[1]);
      }
    });
    if (attachDns(exportAddress(ws2, 'getaddrinfo'), 'getaddrinfo', false))
      attached.push('getaddrinfo');
    if (attachDns(exportAddress(ws2, 'GetAddrInfoW'), 'GetAddrInfoW', true))
      attached.push('GetAddrInfoW');

    const sendAddress = exportAddress(ws2, 'send');
    if (sendAddress !== null) {
      Interceptor.attach(sendAddress, {
        onEnter(args) {
          const size = args[2].toInt32();
          if (size <= 0 || size > 1024 * 1024 || args[1].isNull()) return;
          try {
            const previewSize = Math.min(size, 2048);
            const raw = args[1].readByteArray(previewSize);
            const bytes = new Uint8Array(raw);
            let printable = '';
            for (let index = 0; index < bytes.length; index++) {
              const value = bytes[index];
              printable += (value === 9 || value === 10 || value === 13 ||
                (value >= 32 && value <= 126)) ? String.fromCharCode(value) : '.';
            }
            const lower = printable.toLowerCase();
            if (lower.indexOf('/ut/') === -1 && lower.indexOf('loan') === -1 &&
                lower.indexOf('onboarding') === -1 &&
                lower.indexOf('get /') === -1 && lower.indexOf('post /') === -1 &&
                lower.indexOf('put /') === -1) return;
            readableSendCount++;
            if (readableSendCount <= 80) {
              send({ event: 'network-send', count: readableSendCount, size: size,
                preview: printable.slice(0, 500) });
            }
          } catch (_) {}
        }
      });
      attached.push('send-filter');
    }
    const recvAddress = exportAddress(ws2, 'recv');
    if (recvAddress !== null) {
      Interceptor.attach(recvAddress, {
        onEnter(args) {
          this.receiveBuffer = args[1];
          this.receiveCapacity = args[2].toInt32();
        },
        onLeave(result) {
          const size = result.toInt32();
          if (size <= 0 || size > 1024 * 1024 ||
              this.receiveBuffer === undefined || this.receiveBuffer.isNull()) return;
          try {
            const previewSize = Math.min(size, 4096);
            const raw = this.receiveBuffer.readByteArray(previewSize);
            const bytes = new Uint8Array(raw);
            let printable = '';
            for (let index = 0; index < bytes.length; index++) {
              const value = bytes[index];
              printable += (value === 9 || value === 10 || value === 13 ||
                (value >= 32 && value <= 126)) ? String.fromCharCode(value) : '.';
            }
            const lower = printable.toLowerCase();
            if (lower.indexOf('draftstate') === -1 &&
                lower.indexOf('squadstate') === -1 &&
                lower.indexOf('draft_token') === -1 &&
                lower.indexOf('leaderboard') === -1) return;
            readableReceiveCount++;
            if (readableReceiveCount <= 20) {
              send({ event: 'network-receive', count: readableReceiveCount,
                size: size, preview: printable.slice(0, 1500) });
            }
          } catch (_) {}
        }
      });
      attached.push('recv-filter');
    }
    send({ event: 'network-hook', status: 'passive-ready', apis: attached });
    return true;
  } catch (error) {
    send({ event: 'network-hook', status: 'passive-error:' + String(error) });
    return false;
  }
}

installPassiveNetworkTrace();

// The setting is published by both /settings and userMassInfo, but this RS4
// CardsDLL reads it at two different native sites.  Live market paging showed
// that one of those reads can still retain the constructor default after the
// first texture-cache churn: special portraits then fall back to a base/gold
// head or to a recycled carousel texture.  Force the already-authorized value
// 1 only at the two verified setting reads.  No card identity, DDS payload or
// executable byte is changed; mismatched builds reject the hook by signature.
let dynamicPortraitHddCachingInstalled = false;
let dynamicPortraitHddCachingRejected = false;
let dynamicPortraitHddCachingRetryTimer = null;
const dynamicPortraitHddCachingTraceCounts = {};

function installDynamicPortraitHddCaching() {
  if (dynamicPortraitHddCachingInstalled ||
      dynamicPortraitHddCachingRejected) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  const sites = [
    { name: 'portrait-cache-create', rva: 0x1eb26c },
    { name: 'portrait-cache-store', rva: 0x26a9be }
  ];
  const valid = sites.every(function (site) {
    const address = cards.base.add(site.rva);
    return address.readU8() === 0x83 &&
      address.add(1).readU8() === 0xf8 &&
      address.add(2).readU8() === 0x01;
  });
  if (!valid) {
    dynamicPortraitHddCachingRejected = true;
    send({ event: 'dynamic-portrait-cache', status: 'signature-rejected',
      module: cards.name });
    return true;
  }
  const listeners = [];
  try {
    sites.forEach(function (site) {
      listeners.push(Interceptor.attach(cards.base.add(site.rva), {
        onEnter() {
          const before = this.context.rax.toInt32();
          this.context.rax = ptr(1);
          const count = dynamicPortraitHddCachingTraceCounts[site.name] || 0;
          if (count < 4) {
            dynamicPortraitHddCachingTraceCounts[site.name] = count + 1;
            send({ event: 'dynamic-portrait-cache', status: 'forced-enabled',
              site: site.name, count: count + 1, before: before, after: 1 });
          }
        }
      }));
    });
  } catch (error) {
    listeners.forEach(function (listener) {
      try { listener.detach(); } catch (_) {}
    });
    dynamicPortraitHddCachingRejected = true;
    send({ event: 'dynamic-portrait-cache', status: 'install-rejected',
      module: cards.name, error: String(error) });
    return true;
  }
  dynamicPortraitHddCachingInstalled = true;
  send({ event: 'dynamic-portrait-cache', status: 'ready',
    module: cards.name, sites: sites.length });
  return true;
}

if (!installDynamicPortraitHddCaching()) {
  dynamicPortraitHddCachingRetryTimer = setInterval(function () {
    try {
      if (installDynamicPortraitHddCaching()) {
        clearInterval(dynamicPortraitHddCachingRetryTimer);
        dynamicPortraitHddCachingRetryTimer = null;
      }
    } catch (error) {
      clearInterval(dynamicPortraitHddCachingRetryTimer);
      dynamicPortraitHddCachingRetryTimer = null;
      send({ event: 'dynamic-portrait-cache', status: 'late-error',
        error: String(error) });
    }
  }, 100);
}

// FIFA 19 shipped this CardsDLL in November 2018, before rarity 84 Prime Icon
// Moments was released.  The late EA rarity data and exact player-heads are
// available, but this old client needs a version-encoded compatibility ID to
// resolve each launch Icon identity and still hard-codes LEGEND rarity 12 in
// profile/chemistry checks. Extend only those verified checks for the 44
// source-backed PIM definitions. The executable is never modified on disk and
// every hook is rejected if its instruction bytes do not match this build.
const primeIconMomentsAliases = __PIM_ICON_ALIASES__;
const primeIconMomentsBaseAliases = __PIM_ICON_BASE_ALIASES__;
let primeIconMomentsCompatibilityInstalled = false;
let primeIconMomentsCompatibilityRejected = false;
let primeIconMomentsCompatibilityRetryTimer = null;
const pimCompatibilityTraceCounts = {};
let pimRarityProviderTraceCount = 0;
const pimAggregateRarityRestores = {};

function restorePimAggregateRarities(threadId) {
  const key = String(threadId);
  const pending = pimAggregateRarityRestores[key] || [];
  delete pimAggregateRarityRestores[key];
  pending.forEach(function (entry) {
    try { entry.address.writeS32(entry.rarity); } catch (_) {}
  });
  return pending.length;
}

function pimInstructionBytesMatch(address, expected) {
  try {
    for (let index = 0; index < expected.length; index++) {
      if (address.add(index).readU8() !== expected[index]) return false;
    }
    return true;
  } catch (_) {
    return false;
  }
}

function pimAliasForCard(card) {
  const snapshot = pimCardSnapshot(card);
  return snapshot === null ? 0 : snapshot.acceptedAlias;
}

function pimCardSnapshot(card) {
  try {
    if (card === null || card.isNull()) return null;
    const playerId = card.add(0x18).readU32();
    const rarity = card.add(0x58).readS32();
    const direct = primeIconMomentsAliases[String(playerId)];
    const base = primeIconMomentsBaseAliases[String(playerId)];
    const candidate = direct !== undefined ? Number(direct) :
      (base !== undefined ? Number(base) : 0);
    // CardsDLL may retain the private version-31 wire ID or normalize it to
    // the launch Icon ID before the five Icon-only checks.  The private ID is
    // collision-free; a normalized/base or exact ID additionally requires the
    // sourced Moments rarity so ordinary Prime Icons are never changed. Do not
    // use card+0x54 as an overall discriminator: live tracing proved that slot
    // contains another enum in this card representation.
    const privateWire = direct !== undefined &&
      Math.floor(playerId / 0x1000000) === 31;
    const acceptedAlias = candidate !== 0 &&
      (privateWire || rarity === 84) ? candidate : 0;
    return { card: card.toString(), playerId: playerId, rarity: rarity,
      aliasCandidate: candidate, privateWire: privateWire,
      acceptedAlias: acceptedAlias };
  } catch (_) {
    return null;
  }
}

function tracePimCompatibility(hook, card, alias, applied, extra) {
  const snapshot = pimCardSnapshot(card);
  // Some UI records retain rarity at +0x58 but do not expose the player ID in
  // the chemistry record's +0x18 slot. Rarity 84 is the sourced FIFA 19 PIM
  // rarity, so keep those UI-only records traceable even when no portrait
  // alias can be recovered from this particular native representation.
  if (snapshot === null ||
      (snapshot.acceptedAlias === 0 && snapshot.rarity !== 84)) return;
  const count = pimCompatibilityTraceCounts[hook] || 0;
  if (count >= 4) return;
  pimCompatibilityTraceCounts[hook] = count + 1;
  send({ event: 'pim-compatibility-hit', hook: hook,
    count: count + 1, card: snapshot.card, playerId: snapshot.playerId,
    rarity: snapshot.rarity,
    alias: alias, applied: applied,
    aliasCandidate: snapshot.aliasCandidate, privateWire: snapshot.privateWire,
    extra: extra || null });
}

function pimClassificationForCard(card) {
  const snapshot = pimCardSnapshot(card);
  return snapshot !== null &&
    (snapshot.acceptedAlias !== 0 || snapshot.rarity === 84);
}

function pimVisualLinkOperandValid(card) {
  try {
    return card !== null && !card.isNull() &&
      card.add(0x18).readU32() !== 0;
  } catch (_) {
    return false;
  }
}

function installPrimeIconMomentsCompatibility() {
  if (primeIconMomentsCompatibilityInstalled ||
      primeIconMomentsCompatibilityRejected) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  const fifa = Process.enumerateModules()[0];
  const playerBioIconLabel = fifa.base.add(0x4134dd4);
  const playerBioIconProfile = fifa.base.add(0x2b38a3a);
  const playerBioIconRecord = fifa.base.add(0x23554de);
  const assetIdPublish = cards.base.add(0x89d8b);
  const iconRarityPublish = cards.base.add(0x89ebd);
  const iconProfilePublish = cards.base.add(0x89efa);
  const iconPlayerRecord = cards.base.add(0x17d871);
  const iconCategoryPublish = cards.base.add(0x1e876);
  const rarityDataInput = cards.base.add(0x2c876a);
  const rarityDataPublish = cards.base.add(0x2c876d);
  const iconSquadFlag = cards.base.add(0x2c9ff5);
  const aggregateChemistry = cards.base.add(0x2f6a01);
  const aggregateChemistryContinue = cards.base.add(0x2f6aaa);
  const visualPairCalculator = cards.base.add(0x2fb5b0);
  const visualPairLink = cards.base.add(0x2fb61f);
  const signatures = {
    playerBioIconLabel: pimInstructionBytesMatch(
      playerBioIconLabel,
      [0x83,0x78,0x58,0x0c,0x0f,0x84,0xa8,0x00,0x00,0x00]),
    playerBioIconProfile: pimInstructionBytesMatch(
      playerBioIconProfile,
      [0x83,0x7b,0x58,0x0c,0x74,0x07,0x32,0xc0,0xe9,0x51,0x05,0x00,0x00]),
    playerBioIconRecord: pimInstructionBytesMatch(
      playerBioIconRecord,
      [0x83,0x7b,0x58,0x0c,0x75,0x04,0x44,0x8b,0x7b,0x5c]),
    assetIdPublish: pimInstructionBytesMatch(
      assetIdPublish, [0x4c,0x8d,0x05,0xfe,0x1f,0x2d,0x00]),
    iconRarityPublish: pimInstructionBytesMatch(
      iconRarityPublish, [0x4c,0x8d,0x05,0x54,0x1f,0x2d,0x00]),
    iconProfilePublish: pimInstructionBytesMatch(
      iconProfilePublish, [0x83,0x78,0x58,0x0c,0x75,0x2e]),
    iconPlayerRecord: pimInstructionBytesMatch(
      iconPlayerRecord, [0x83,0x7a,0x58,0x0c,0x0f,0x94,0xc0]),
    iconCategoryPublish: pimInstructionBytesMatch(
      iconCategoryPublish, [0x83,0x79,0x58,0x0c,0x75,0x07,
                            0xb9,0x74,0x00,0x00,0x00]),
    rarityDataInput: pimInstructionBytesMatch(
      rarityDataInput, [0x8b,0x4f,0x1c,0xff,0xd2,0x48,0x8b,0xd8]),
    iconSquadFlag: pimInstructionBytesMatch(
      iconSquadFlag, [0x83,0x7a,0x58,0x0c,0x74,0x04]),
    aggregateChemistry: pimInstructionBytesMatch(
      aggregateChemistry, [0x83,0x7a,0x58,0x0c,0x74,0x6c,
                           0x83,0x79,0x58,0x0c,0x74,0x66]),
    aggregateChemistryContinue: pimInstructionBytesMatch(
      aggregateChemistryContinue,
      [0x48,0x8d,0x3d,0x47,0xc4,0x05,0x00]),
    visualPairCalculator: pimInstructionBytesMatch(
      visualPairCalculator, [0x4c,0x8b,0x4a,0x18,0x33,0xc9,
                             0x4d,0x85,0xc9,0x74,0x08]),
    visualPairLink: pimInstructionBytesMatch(
      visualPairLink, [0x41,0x83,0x79,0x58,0x0c,0x74,0x62,
                       0x83,0x7a,0x58,0x0c,0x74,0x5c])
  };
  if (Object.keys(signatures).some(function (key) { return !signatures[key]; })) {
    primeIconMomentsCompatibilityRejected = true;
    send({ event: 'pim-compatibility', status: 'signature-rejected',
      module: cards.name, signatures: signatures });
    return true;
  }

  const installedListeners = [];
  try {
  // Player Bio is assembled by FIFA19.exe rather than the CardsDLL paths used
  // by squad chemistry. This formatter sends every non-launch rarity to the
  // generic SPECIAL ITEM branch. Route only sourced rarity-84/aliased PIM
  // records through its native rarity-12 Icon label branch.
  installedListeners.push(Interceptor.attach(playerBioIconLabel, {
    onEnter() {
      const card = this.context.rax;
      const alias = pimAliasForCard(card);
      const pim = pimClassificationForCard(card);
      tracePimCompatibility('player-bio-icon-label', card, alias, pim, null);
      if (pim) this.context.pc = fifa.base.add(0x4134e86);
    }
  }));
  // The Player Bio Icon assembler returns false immediately when rarity is not
  // 12. Continue at its verified Icon block for PIM so ICON PROFILE is built
  // from the already-correct player identity and detailed attributes.
  installedListeners.push(Interceptor.attach(playerBioIconProfile, {
    onEnter() {
      const card = this.context.rbx;
      const alias = pimAliasForCard(card);
      const pim = pimClassificationForCard(card);
      tracePimCompatibility('player-bio-icon-profile', card, alias, pim, null);
      if (pim) this.context.pc = fifa.base.add(0x2b38a47);
    }
  }));
  // Preserve the Icon-specific version record used by adjacent Player Bio UI
  // state without mutating the stored PIM rarity or its Moments shell.
  installedListeners.push(Interceptor.attach(playerBioIconRecord, {
    onEnter() {
      const card = this.context.rbx;
      const alias = pimAliasForCard(card);
      const pim = pimClassificationForCard(card);
      tracePimCompatibility('player-bio-icon-record', card, alias, pim, null);
      if (pim) this.context.pc = fifa.base.add(0x23554e4);
    }
  }));
  // The UI publishes playerID from card+0x18, then derives assetID by masking
  // the same value to 24 bits. The compatibility ID already masks to the
  // verified Icon; keep this guarded override for cached exact PIM records.
  installedListeners.push(Interceptor.attach(assetIdPublish, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rax);
      tracePimCompatibility('asset-id-publish', this.context.rax, alias,
        alias !== 0, { outputBefore: this.context.r9.toString() });
      if (alias !== 0) this.context.r9 = ptr(alias);
    }
  }));
  // Card art has already been selected from the real rarity 84 at this point.
  // The player-identity view model separately publishes `rareFlag`; this old
  // November client only enables the Icon label/profile for launch rarity 12.
  // Present that one UI field as Icon without mutating the card, so the exact
  // Moments shell and player head remain rarity-84 assets.
  installedListeners.push(Interceptor.attach(iconRarityPublish, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rax);
      const pim = pimClassificationForCard(this.context.rax);
      tracePimCompatibility('icon-rarity-publish', this.context.rax, alias,
        pim, { outputBefore: this.context.r9.toString() });
      if (pim) this.context.r9 = ptr(12);
    }
  }));
  // Rarity 12 publishes the Icon-only specificAge field consumed by the Icon
  // profile view.  Let a verified rarity-84 PIM take that same narrow branch.
  installedListeners.push(Interceptor.attach(iconProfilePublish, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rax);
      const pim = pimClassificationForCard(this.context.rax);
      tracePimCompatibility('icon-profile', this.context.rax, alias,
        pim, null);
      if (pim)
        this.context.pc = cards.base.add(0x89f00);
    }
  }));
  // This constructor stores `rarity == 12` as the player record's is-Icon bit.
  installedListeners.push(Interceptor.attach(iconPlayerRecord, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rdx);
      const pim = pimClassificationForCard(this.context.rdx);
      tracePimCompatibility('icon-player-record', this.context.rdx, alias,
        pim, null);
      if (pim) {
        this.context.rax = ptr(1);
        this.context.pc = cards.base.add(0x17d878);
      }
    }
  }));
  // The squad UI maps rarity 12 to category code 0x74 (Icon) and every
  // rarity >= 3 to 0x75 (generic Special Item). This is the live consumer
  // responsible for the label reported by the retail client. Route only a
  // verified PIM through the native Icon category branch; its stored rarity
  // and Moments art remain untouched.
  installedListeners.push(Interceptor.attach(iconCategoryPublish, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rcx);
      const pim = pimClassificationForCard(this.context.rcx);
      tracePimCompatibility('icon-category', this.context.rcx, alias,
        pim, null);
      if (pim)
        this.context.pc = cards.base.add(0x1e87c);
    }
  }));
  // The item-rarity data provider publishes `rareFlag` independently of the
  // card view model. This is the provider actually consumed by the squad UI.
  // Keep rarity 84 for its Moments art data, but expose its classification
  // value as launch Icon rarity 12 to this November client.
  installedListeners.push(Interceptor.attach(rarityDataInput, {
    onEnter() {
      let rarity = 0;
      try { rarity = this.context.rdi.add(0x1c).readU32(); } catch (_) {
        return;
      }
      if (rarity !== 84) return;
      if (pimRarityProviderTraceCount < 4) {
        pimRarityProviderTraceCount++;
        send({ event: 'pim-rarity-provider-hit',
          count: pimRarityProviderTraceCount, input: 84, output: 12 });
      }
      // The hook runs immediately before `mov ecx,[rdi+0x1c]`. Skip only
      // that load and continue at the verified indirect provider call with
      // the launch-Icon classification in RCX. Attaching to the two-byte
      // `call rdx` itself is not supported by Frida on this build.
      this.context.rcx = ptr(12);
      this.context.pc = rarityDataPublish;
    }
  }));
  // Preserve the squad-level Icon flag for PIM without changing other special
  // rarities or the general rarity category table.
  installedListeners.push(Interceptor.attach(iconSquadFlag, {
    onEnter() {
      const alias = pimAliasForCard(this.context.rdx);
      const pim = pimClassificationForCard(this.context.rdx);
      tracePimCompatibility('icon-squad-flag', this.context.rdx, alias,
        pim, null);
      if (pim)
        this.context.pc = cards.base.add(0x2c9fff);
    }
  }));
  // This calculator contributes the line component of individual chemistry.
  // Its rarity-12 path starts at 0x2f6a73, but redirecting PC from an
  // instruction listener did not change the retail result: a live PIM
  // Ronaldinho still received the ordinary-card 3 line points. Present the
  // sourced PIM operands as rarity 12 only while the native comparison and
  // identity reads execute, then restore rarity 84 at their common
  // continuation. This reuses the complete native Icon arithmetic without
  // changing the DTO, card art, database item or any non-PIM special card.
  installedListeners.push(Interceptor.attach(aggregateChemistry, {
    onEnter() {
      const threadId = Process.getCurrentThreadId();
      restorePimAggregateRarities(threadId);
      const leftAlias = pimAliasForCard(this.context.rdx);
      const rightAlias = pimAliasForCard(this.context.rcx);
      tracePimCompatibility('chemistry-left', this.context.rdx, leftAlias,
        leftAlias !== 0, null);
      tracePimCompatibility('chemistry-right', this.context.rcx, rightAlias,
        rightAlias !== 0, null);
      const key = String(threadId);
      const pending = [];
      const seen = {};
      // Publish the restore bucket before the first write. If a later operand
      // is unreadable, the common continuation can still restore every write
      // that already succeeded.
      pimAggregateRarityRestores[key] = pending;
      [[this.context.rdx, leftAlias], [this.context.rcx, rightAlias]]
        .forEach(function (entry) {
          try {
            const card = entry[0];
            const alias = entry[1];
            if (alias === 0 || card.isNull()) return;
            const addressKey = card.toString();
            if (seen[addressKey]) return;
            seen[addressKey] = true;
            const rarityAddress = card.add(0x58);
            const rarity = rarityAddress.readS32();
            if (rarity === 12) return;
            rarityAddress.writeS32(12);
            pending.push({ address: rarityAddress, rarity: rarity });
          } catch (_) {}
        });
      if (pending.length === 0)
        delete pimAggregateRarityRestores[key];
    }
  }));
  installedListeners.push(Interceptor.attach(aggregateChemistryContinue, {
    onEnter() {
      restorePimAggregateRarities(Process.getCurrentThreadId());
    }
  }));
  // Guard the pair calculator's final result as well as its native Icon
  // branch. A normal Icon always has at least a medium link to a valid player;
  // preserve a stronger same-nation/same-team result and upgrade only weak PIM
  // results. Reading both wrapper operands at function entry is stable on this
  // build and avoids relying on transient registers at the inner branch.
  installedListeners.push(Interceptor.attach(visualPairCalculator, {
    onEnter(args) {
      this.pimPair = false;
      this.pimCard = null;
      this.pimAlias = 0;
      this.pimMinimum = 2;
      try {
        const leftCard = args[1].add(0x18).readPointer();
        const rightCard = args[2].add(0x18).readPointer();
        // The Draft builder represents unfilled positions with allocated
        // placeholder records.  They can reach this visual calculator with a
        // non-null card pointer but playerId=0.  Never apply the PIM minimum
        // link until both ends identify real players.
        if (!pimVisualLinkOperandValid(leftCard) ||
            !pimVisualLinkOperandValid(rightCard)) return;
        const leftAlias = pimAliasForCard(leftCard);
        const rightAlias = pimAliasForCard(rightCard);
        if (leftAlias !== 0 || rightAlias !== 0) {
          this.pimPair = true;
          this.pimCard = leftAlias !== 0 ? leftCard : rightCard;
          this.pimAlias = leftAlias !== 0 ? leftAlias : rightAlias;
          const leftRarity = leftCard.add(0x58).readS32();
          const rightRarity = rightCard.add(0x58).readS32();
          const leftNation = leftCard.add(0x94).readU32();
          const rightNation = rightCard.add(0x94).readU32();
          const leftIcon = leftAlias !== 0 || leftRarity === 12;
          const rightIcon = rightAlias !== 0 || rightRarity === 12;
          // The native Icon branch returns a strong link for the same nation
          // and for two Icon-family cards. Mirror those two boundaries in the
          // wrapper result guard in case a caller bypasses visualPairLink.
          if ((leftIcon && rightIcon) ||
              (leftNation !== 0 && leftNation === rightNation))
            this.pimMinimum = 3;
        }
      } catch (_) {}
    },
    onLeave(result) {
      if (!this.pimPair || this.pimCard === null) return;
      const before = result.toInt32();
      if (before < this.pimMinimum) result.replace(ptr(this.pimMinimum));
      tracePimCompatibility('visual-link-result', this.pimCard,
        this.pimAlias, true,
        { before: before, minimum: this.pimMinimum,
          after: Math.max(before, this.pimMinimum) });
    }
  }));
  // The squad-screen pair calculator returns the visible weak/medium/strong
  // link. Its rarity-12 branch is the native universal Icon path. Extend that
  // exact branch only when either operand is one of the verified PIM cards.
  installedListeners.push(Interceptor.attach(visualPairLink, {
    onEnter() {
      const leftValid = pimVisualLinkOperandValid(this.context.r9);
      const rightValid = pimVisualLinkOperandValid(this.context.rdx);
      const leftAlias = pimAliasForCard(this.context.r9);
      const rightAlias = pimAliasForCard(this.context.rdx);
      tracePimCompatibility('visual-link-left', this.context.r9, leftAlias,
        leftAlias !== 0, null);
      tracePimCompatibility('visual-link-right', this.context.rdx, rightAlias,
        rightAlias !== 0, null);
      if (leftValid && rightValid &&
          (leftAlias !== 0 || rightAlias !== 0))
        this.context.pc = cards.base.add(0x2fb688);
    }
  }));
  } catch (error) {
    installedListeners.forEach(function (listener) {
      try { listener.detach(); } catch (_) {}
    });
    primeIconMomentsCompatibilityRejected = true;
    send({ event: 'pim-compatibility', status: 'install-rejected',
      module: cards.name, error: String(error) });
    return true;
  }
  primeIconMomentsCompatibilityInstalled = true;
  send({ event: 'pim-compatibility', status: 'ready', module: cards.name,
    definitionCount: 44,
    aliasCount: Object.keys(primeIconMomentsAliases).length });
  return true;
}

if (!installPrimeIconMomentsCompatibility()) {
  primeIconMomentsCompatibilityRetryTimer = setInterval(function () {
    try {
      if (installPrimeIconMomentsCompatibility()) {
        clearInterval(primeIconMomentsCompatibilityRetryTimer);
        primeIconMomentsCompatibilityRetryTimer = null;
      }
    } catch (error) {
      clearInterval(primeIconMomentsCompatibilityRetryTimer);
      primeIconMomentsCompatibilityRetryTimer = null;
      send({ event: 'pim-compatibility', status: 'late-error',
        error: String(error) });
    }
  }, 100);
}

// Offline Select mode 1008 builds the post-Side-Select AWAY participant at
// CardsDLL+0x486e0. Its cache search accepts only 0xf0-byte GetClubInfo records
// whose internal byte at +0x30 is nonzero. The JSON decoder at +0x21c230 fills
// every public TOTW identity field but has no member that writes this internal
// classification byte, so the native loop skips the complete sentinel record
// and never calls the participant publisher at +0x49930. That missing
// participant causes the absent AWAY label, duplicate HOME kit side,
// TEAMNAME_ABBR15_0 fallback and null-team crash.
//
// Mark only the exact, already-decoded selected TOTW record immediately before
// the native search. Challenge Select can address the public sentinel owner or
// its local-persona alias and can select any sourced week. Identity,
// presentation and the matching 0x40-byte summary are all checked at fixed
// offsets proven by the installed retail decoder. HOME, Draft and every other
// Offline Select mode fail closed.
let totwAwayParticipantCompatibilityInstalled = false;
let totwAwayParticipantCompatibilityRejected = false;
let totwAwayParticipantCompatibilityRetryTimer = null;
let totwAwayParticipantCompatibilityCount = 0;
let offlineSelectSearchReports = 0;

function installTotwAwayParticipantCompatibility() {
  if (totwAwayParticipantCompatibilityInstalled ||
      totwAwayParticipantCompatibilityRejected) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  const builder = cards.base.add(0x486e0);
  const cacheSearch = cards.base.add(0x4883e);

  function bytesMatch(address, expected) {
    try {
      const bytes = new Uint8Array(address.readByteArray(expected.length));
      for (let index = 0; index < expected.length; index++)
        if (bytes[index] !== expected[index]) return false;
      return true;
    } catch (_) { return false; }
  }
  function readable(address, size) {
    try {
      if (address === null || address.isNull()) return false;
      const first = Process.findRangeByAddress(address);
      const last = Process.findRangeByAddress(address.add(Math.max(0,size - 1)));
      return first !== null && last !== null &&
        first.protection.indexOf('r') !== -1 &&
        last.protection.indexOf('r') !== -1;
    } catch (_) { return false; }
  }
  function writable(address) {
    try {
      const range = Process.findRangeByAddress(address);
      return range !== null && range.protection.indexOf('w') !== -1;
    } catch (_) { return false; }
  }
  function inlineBytesEqual(address, expected) {
    if (!readable(address, expected.length + 1)) return false;
    try {
      for (let index = 0; index < expected.length; index++)
        if (address.add(index).readU8() !== expected[index]) return false;
      return address.add(expected.length).readU8() === 0;
    } catch (_) { return false; }
  }

  // Validate the whole owner/classification lookup, not only the hook byte.
  const builderSignatureOk = bytesMatch(builder, [
    0x40,0x55,0x56,0x57,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x50
  ]);
  const searchSignatureOk = bytesMatch(cacheSearch, [
    0x48,0x8d,0x97,0x20,0x71,0x00,0x00,0x4c,0x8b,0x42,0x30,
    0x4c,0x3b,0x42,0x38,0x74,0x43,0x49,0x8b,0x08,0x48,0x8b,
    0xc1,0x48,0xc1,0xe8,0x20,0x41,0x80,0x78,0x30,0x00,0x74,
    0x10,0x39,0x83,0x34,0x01,0x00,0x00,0x75,0x08,0x39,0x8b,
    0x38,0x01,0x00,0x00,0x74,0x09,0x49,0x81,0xc0,0xf0,0x00,
    0x00,0x00,0xeb,0xd0,0x89,0x4c,0x24,0x28,0x89,0x44,0x24,
    0x20,0x45,0x33,0xc9,0x48,0x8d,0x94,0x24,0x90,0x00,0x00,
    0x00,0xe8,0x9f,0x10,0x00,0x00
  ]);
  if (!builderSignatureOk || !searchSignatureOk) {
    totwAwayParticipantCompatibilityRejected = true;
    send({ event: 'totw-away-participant-compatibility',
      status: 'signature-mismatch', module: cards.name,
      builderSignatureOk: builderSignatureOk,
      searchSignatureOk: searchSignatureOk });
    return true;
  }

  function inlineText(address, maximum) {
    try {
      if (!readable(address, maximum)) return null;
      let text = '';
      for (let index = 0; index < maximum; index++) {
        const value = address.add(index).readU8();
        if (value === 0) break;
        if (value < 0x20 || value > 0x7e) return null;
        text += String.fromCharCode(value);
      }
      return text;
    } catch (_) { return null; }
  }

  function reportOfflineSelectSearch(mode, controller, service) {
    if (offlineSelectSearchReports >= 6) return;
    offlineSelectSearchReports++;
    const report = { event: 'offline-select-away-search', mode: mode,
      ownerHigh: null, ownerLow: null, selectedSquadId: null,
      records: null, entries: [] };
    try {
      report.ownerHigh = controller.add(0x134).readU32();
      report.ownerLow = controller.add(0x138).readU32();
      report.selectedSquadId = controller.add(0x13c).readU32();
      if (!readable(service, 0x7160)) { report.records = -2; send(report); return; }
      const vector = service.add(0x7120);
      const begin = vector.add(0x30).readPointer();
      const end = vector.add(0x38).readPointer();
      if (begin.isNull() || end.isNull() || end.compare(begin) < 0) {
        send(report); return;
      }
      const byteCount = end.sub(begin).toInt32();
      if (byteCount < 0 || byteCount %% 0xf0 !== 0 ||
          byteCount > 0xf0 * 64 || !readable(begin, byteCount)) {
        report.records = -1; send(report); return;
      }
      report.records = byteCount / 0xf0;
      for (let offset = 0; offset < byteCount && report.entries.length < 8;
           offset += 0xf0) {
        const record = begin.add(offset);
        report.entries.push({
          ownerLow: record.readU32(), ownerHigh: record.add(4).readU32(),
          classified: record.add(0x30).readU8(),
          name: inlineText(record.add(0x38), 24),
          abbreviation: inlineText(record.add(0x60), 8),
          badgeAssetId: record.add(0x8c).readU32(),
          teamId: record.add(0x94).readU32() });
      }
    } catch (error) {
      report.error = String(error);
    }
    send(report);
  }

  try {
    Interceptor.attach(cacheSearch, {
      onEnter() {
        const controller = this.context.rbx;
        const service = this.context.rdi;
        try {
          if (!readable(controller, 0x140)) return;
          const offlineSelectMode = controller.add(0x130).readS32();
          // The service readability check belongs to the TOTW marking path.
          // Keeping it ahead of the mode read hid Draft entirely on RC118.
          if (offlineSelectMode === 1008 && !readable(service, 0x7160))
            return;
          if (offlineSelectMode !== 1008) {
            // Every Offline Select mode reaches this one cache search, and
            // the AWAY club it loads is whichever 0xf0-byte GetClubInfo
            // record it accepts here. A Draft round shows Manchester City and
            // then Arsenal whatever the server publishes, so record what this
            // search actually sees for the non-TOTW modes: the mode id, the
            // owner it is looking for, and the identity of every cached
            // record. Read-only and bounded; nothing else changes.
            reportOfflineSelectSearch(offlineSelectMode, controller, service);
            return;
          }
          // The controller stores the 64-bit owner as high/low at 134/138,
          // while each GetClubInfo record stores the native integer low/high
          // at 00/04.  Comparing both pairs in the same order made the public
          // 0x7fffffffffffffff sentinel impossible to match.
          const controllerOwnerHigh = controller.add(0x134).readU32();
          const controllerOwnerLow = controller.add(0x138).readU32();
          const ownerIsPublic = controllerOwnerHigh === 0x7fffffff &&
            controllerOwnerLow === 0xffffffff;
          const ownerIsLocal = controllerOwnerHigh === 0 &&
            controllerOwnerLow === 1000019;
          const selectedSquadId = controller.add(0x13c).readU32();
          const selectedWeek = selectedSquadId - 500000;
          if ((!ownerIsPublic && !ownerIsLocal) || selectedWeek < 1 ||
              selectedWeek > 99) return;
          const expectedName = Array.from('TOTW ' + selectedWeek).map(
            function (value) { return value.charCodeAt(0); });
          const expectedAbbr = Array.from(('TW' + selectedWeek).slice(0, 3)).map(
            function (value) { return value.charCodeAt(0); });

          const vector = service.add(0x7120);
          const begin = vector.add(0x30).readPointer();
          const end = vector.add(0x38).readPointer();
          if (begin.isNull() || end.isNull() || end.compare(begin) < 0) return;
          const byteCount = end.sub(begin).toInt32();
          if (byteCount <= 0 || byteCount %% 0xf0 !== 0 ||
              byteCount > 0xf0 * 64 || !readable(begin, byteCount)) return;

          const publicMatches = [];
          const localMatches = [];
          for (let offset = 0; offset < byteCount; offset += 0xf0) {
            const record = begin.add(offset);
            const recordOwnerLow = record.readU32();
            const recordOwnerHigh = record.add(4).readU32();
            const recordIsPublic = recordOwnerHigh === 0x7fffffff &&
              recordOwnerLow === 0xffffffff;
            const recordIsLocal = recordOwnerHigh === 0 &&
              recordOwnerLow === 1000019;
            if (!recordIsPublic && !recordIsLocal) continue;
            const summariesBegin = record.add(0xb8).readPointer();
            const summariesEnd = record.add(0xc0).readPointer();
            if (summariesBegin.isNull() || summariesEnd.isNull() ||
                summariesEnd.compare(summariesBegin) < 0)
              continue;
            const summariesBytes = summariesEnd.sub(summariesBegin).toInt32();
            if (summariesBytes <= 0 || summariesBytes %% 0x40 !== 0 ||
                summariesBytes > 0x40 * 64 ||
                !readable(summariesBegin, summariesBytes)) continue;
            let selectedSummary = null;
            for (let summaryOffset = 0; summaryOffset < summariesBytes;
                 summaryOffset += 0x40) {
              const summary = summariesBegin.add(summaryOffset);
              const rating = summary.add(0x30).readU32();
              if (summary.readU32() === selectedSquadId &&
                  rating >= 70 && rating <= 99 &&
                  summary.add(0x34).readU32() === 100) {
                selectedSummary = summary;
                break;
              }
            }
            if (selectedSummary === null) continue;
            const identityMatches =
              record.add(0x94).readU32() === __TOTW_TEAM_ID__ &&
              record.add(0x8c).readU32() === __TOTW_BADGE_ASSET_ID__ &&
              record.add(0x98).readU32() === __TOTW_HOME_KIT_ASSET_ID__ &&
              record.add(0xa8).readU32() === __TOTW_AWAY_KIT_ASSET_ID__ &&
              inlineBytesEqual(record.add(0x38), expectedName) &&
              inlineBytesEqual(record.add(0x60), expectedAbbr);
            const match = { record: record, summary: selectedSummary,
              identityMatches: identityMatches };
            (recordIsPublic ? publicMatches : localMatches).push(match);
          }
          // /user/list deliberately exposes both the local-persona alias and
          // the public sentinel.  Offline Select addresses the public record;
          // retain the local alias only as a compatibility fallback.
          let selected = null;
          if (publicMatches.length === 1) selected = publicMatches[0];
          else if (publicMatches.length === 0 && localMatches.length === 1)
            selected = localMatches[0];
          if (selected === null) {
            if (totwAwayParticipantCompatibilityCount < 4) {
              totwAwayParticipantCompatibilityCount++;
              send({ event: 'totw-away-participant-compatibility',
                status: 'record-mismatch',
                publicMatches: publicMatches.length,
                localMatches: localMatches.length,
                records: byteCount / 0xf0 });
            }
            return;
          }
          const target = selected.record.add(0x30);
          const previous = target.readU8();
          if (previous === 0) {
            if (!writable(target)) return;
            target.writeU8(1);
          }
          if (totwAwayParticipantCompatibilityCount < 8) {
            totwAwayParticipantCompatibilityCount++;
            send({ event: 'totw-away-participant-compatibility',
              status: previous === 0 ? 'applied' : 'already-classified',
              controller: controller.toString(), service: service.toString(),
              record: selected.record.toString(),
              summary: selected.summary.toString(),
              selectedSquadId: selectedSquadId, previous: previous,
              current: target.readU8(), records: byteCount / 0xf0,
              identityMatches: selected.identityMatches });
          }
        } catch (error) {
          if (totwAwayParticipantCompatibilityCount < 8) {
            totwAwayParticipantCompatibilityCount++;
            send({ event: 'totw-away-participant-compatibility',
              status: 'record-error', error: String(error) });
          }
        }
      }
    });
  } catch (error) {
    totwAwayParticipantCompatibilityRejected = true;
    send({ event: 'totw-away-participant-compatibility',
      status: 'install-error', module: cards.name, error: String(error) });
    return true;
  }
  totwAwayParticipantCompatibilityInstalled = true;
  send({ event: 'totw-away-participant-compatibility', status: 'ready',
    module: cards.name, builderOffset: '0x486e0',
    cacheSearchOffset: '0x4883e' });
  return true;
}

if (!installTotwAwayParticipantCompatibility()) {
  totwAwayParticipantCompatibilityRetryTimer = setInterval(function () {
    try {
      if (installTotwAwayParticipantCompatibility()) {
        clearInterval(totwAwayParticipantCompatibilityRetryTimer);
        totwAwayParticipantCompatibilityRetryTimer = null;
      }
    } catch (error) {
      clearInterval(totwAwayParticipantCompatibilityRetryTimer);
      totwAwayParticipantCompatibilityRetryTimer = null;
      send({ event: 'totw-away-participant-compatibility',
        status: 'late-error', error: String(error) });
    }
  }, 100);
}

// The retail warm-up state machine moves directly from state 2 to state 7.
// CardsDLL+0x17c910 performs that transition for frontend event 0x0c when the
// payload dword is zero. In offline TOTW the accepted CreateMatch reaches the
// final event-0x37 readiness gate without the preceding 0x0c notification, so
// the gate keeps observing state 2 and never publishes CanShowPressStart.
//
// Recover only that proven missing edge. The CreateMatch cache lookup arms a
// short-lived, one-shot token for mode 1008 and squad 500006. Event 0x37 then
// invokes the real retail handler synchronously on the same FIFA thread before
// its state-7 comparison. There is no polling, direct state write, provider
// construction or Draft path involved.
let totwWarmupStopCompatibilityInstalled = false;
let totwWarmupStopCompatibilityRejected = false;
let totwWarmupStopCompatibilityRetryTimer = null;
let totwWarmupStopGeneration = 0;
let totwWarmupStopArmedGeneration = 0;
let totwWarmupStopAttemptedGeneration = 0;
let totwWarmupStopArmedAtMs = 0;
let totwWarmupStopControllerText = null;
let totwWarmupStopLastAvailable = -1;
let totwWarmupStopLogCount = 0;
const totwWarmupStopMaxAgeMs = 120000;
// The 2026-08-29 retail run proved that forcing STOP makes the state machine
// and HUD advance while Frostbite's 3D MatchData bootstrap is still absent.
// Keep the proven transition code for reference, but fail closed until a
// passive trace observes the real 0x7568/0x7569 readiness chain.
const totwWarmupStopForcedTransitionEnabled = false;

function installTotwWarmupStopCompatibility() {
  if (totwWarmupStopCompatibilityInstalled ||
      totwWarmupStopCompatibilityRejected) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  if (!totwWarmupStopForcedTransitionEnabled) {
    totwWarmupStopCompatibilityInstalled = true;
    send({event: 'totw-warmup-stop', status: 'disabled-black-scene',
      module: cards.name,
      reason: 'awaiting-real-matchdata-and-kit-readiness'});
    return true;
  }
  const createMatchLookupResult = cards.base.add(0x4701a);
  const futFeStopDecision = cards.base.add(0x17c910);
  const event37Handler = cards.base.add(0x182240);

  function bytesMatch(address, expected) {
    try {
      const bytes = new Uint8Array(address.readByteArray(expected.length));
      for (let index = 0; index < expected.length; index++)
        if (bytes[index] !== expected[index]) return false;
      return true;
    } catch (_) { return false; }
  }
  function readable(address, size) {
    try {
      if (address === null || address.isNull()) return false;
      const first = Process.findRangeByAddress(address);
      const last = Process.findRangeByAddress(
        address.add(Math.max(0, size - 1)));
      return first !== null && last !== null &&
        first.protection.indexOf('r') !== -1 &&
        last.protection.indexOf('r') !== -1;
    } catch (_) { return false; }
  }
  function emitTotwWarmupStop(status, details) {
    if (totwWarmupStopLogCount >= 24) return;
    totwWarmupStopLogCount++;
    send(Object.assign({event: 'totw-warmup-stop', status: status,
      generation: totwWarmupStopGeneration}, details || {}));
  }

  const lookupSignatureOk = bytesMatch(createMatchLookupResult,
    [0x84,0xc0,0x0f,0x84,0xa3,0x00,0x00,0x00,0x48,0x8b,0x05]);
  const stopSignatureOk = bytesMatch(futFeStopDecision,
    [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,
     0x48,0x8d,0x6c,0x24,0xd9,0x48,0x81,0xec,0xb0,0x00,0x00,0x00]);
  const event37SignatureOk = bytesMatch(event37Handler,
    [0x40,0x57,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x50,0x48,0xc7,
     0x44,0x24,0x40,0xfe,0xff,0xff,0xff]);
  if (!lookupSignatureOk || !stopSignatureOk || !event37SignatureOk) {
    totwWarmupStopCompatibilityRejected = true;
    emitTotwWarmupStop('signature-mismatch', {
      module: cards.name, lookupSignatureOk: lookupSignatureOk,
      stopSignatureOk: stopSignatureOk,
      event37SignatureOk: event37SignatureOk});
    return true;
  }

  const listeners = [];
  try {
    const zeroPayload = Memory.alloc(4);
    zeroPayload.writeS32(0);
    const completeWarmupStop = new NativeFunction(futFeStopDecision, 'void',
      ['pointer','uint32','pointer'],
      {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'});

    listeners.push(Interceptor.attach(createMatchLookupResult, {
      onEnter() {
        const controller = this.context.rbx;
        if (!readable(controller, 0x140)) return;
        const mode = controller.add(0x130).readS32();
        const selectedSquadId = controller.add(0x13c).readU32();
        if (mode !== 1008 || selectedSquadId !== 500006) return;
        const available = this.context.rax.toInt32() & 0xff;
        const controllerText = controller.toString();
        if (available === 0) {
          if (totwWarmupStopControllerText !== controllerText ||
              totwWarmupStopLastAvailable !== 0) {
            totwWarmupStopGeneration++;
            totwWarmupStopArmedGeneration = 0;
            totwWarmupStopAttemptedGeneration = 0;
            totwWarmupStopArmedAtMs = 0;
          }
          totwWarmupStopControllerText = controllerText;
          totwWarmupStopLastAvailable = 0;
          emitTotwWarmupStop('create-match-pending', {
            controller: controllerText, available: available,
            mode: mode, selectedSquadId: selectedSquadId});
          return;
        }
        if (available !== 1) return;
        if (totwWarmupStopGeneration === 0 ||
            totwWarmupStopControllerText !== controllerText)
          totwWarmupStopGeneration++;
        totwWarmupStopControllerText = controllerText;
        totwWarmupStopLastAvailable = 1;
        totwWarmupStopArmedGeneration = totwWarmupStopGeneration;
        totwWarmupStopArmedAtMs = Date.now();
        emitTotwWarmupStop('armed', {
          controller: controllerText, available: available,
          mode: mode, selectedSquadId: selectedSquadId});
      }
    }));

    listeners.push(Interceptor.attach(event37Handler, {
      onEnter(args) {
        const eventId = args[1].toInt32() & 0xffff;
        const object = args[0];
        if (eventId !== 0x37 || !readable(object, 0xf04)) return;
        const state = object.add(0xf00).readS32();
        const gameModeId = object.add(0x18).readS32();
        const ageMs = Date.now() - totwWarmupStopArmedAtMs;
        if (state !== 2 || gameModeId !== 27 ||
            totwWarmupStopArmedGeneration !== totwWarmupStopGeneration ||
            totwWarmupStopArmedGeneration === 0 ||
            totwWarmupStopAttemptedGeneration === totwWarmupStopGeneration ||
            ageMs < 0 || ageMs > totwWarmupStopMaxAgeMs) {
          emitTotwWarmupStop('event37-skipped', {
            object: object.toString(), eventId: eventId, state: state,
            gameModeId: gameModeId, ageMs: ageMs,
            armedGeneration: totwWarmupStopArmedGeneration,
            attemptedGeneration: totwWarmupStopAttemptedGeneration});
          return;
        }
        const generation = totwWarmupStopGeneration;
        totwWarmupStopAttemptedGeneration = generation;
        emitTotwWarmupStop('enter', {object: object.toString(),
          eventId: eventId, state: state, gameModeId: gameModeId,
          ageMs: ageMs});
        try {
          completeWarmupStop(object, 0x0c, zeroPayload);
          const stateAfter = readable(object, 0xf04) ?
            object.add(0xf00).readS32() : null;
          emitTotwWarmupStop('leave', {object: object.toString(),
            eventId: eventId, stateBefore: state, stateAfter: stateAfter,
            transitioned: stateAfter === 7});
        } catch (error) {
          emitTotwWarmupStop('error', {object: object.toString(),
            eventId: eventId, error: String(error)});
        }
      }
    }));
  } catch (error) {
    listeners.forEach(function (listener) {
      try { listener.detach(); } catch (_) {}
    });
    totwWarmupStopCompatibilityRejected = true;
    emitTotwWarmupStop('install-error', {
      module: cards.name, error: String(error)});
    return true;
  }
  totwWarmupStopCompatibilityInstalled = true;
  emitTotwWarmupStop('ready', {module: cards.name,
    lookupOffset: '0x4701a', stopOffset: '0x17c910',
    event37Offset: '0x182240'});
  return true;
}

if (!installTotwWarmupStopCompatibility()) {
  totwWarmupStopCompatibilityRetryTimer = setInterval(function () {
    try {
      if (installTotwWarmupStopCompatibility()) {
        clearInterval(totwWarmupStopCompatibilityRetryTimer);
        totwWarmupStopCompatibilityRetryTimer = null;
      }
    } catch (error) {
      clearInterval(totwWarmupStopCompatibilityRetryTimer);
      totwWarmupStopCompatibilityRetryTimer = null;
      send({event: 'totw-warmup-stop', status: 'late-error',
        error: String(error)});
    }
  }, 100);
}

// Passive trace for the retail Single Player Draft hand-off. The HTTP log
// proves that the response was sent, but not that CardsDLL accepted it. These
// hooks only observe construction and decoding of the exact response class;
// they never replace a return value or write into FIFA memory.
let draftTraceInstalled = false;
let draftTraceRetryTimer = null;
// Decoder/transport diagnostics are available as separate bounded Draft
// probes. Do not keep their shared dispatcher hook or accurate backtraces
// attached during gameplay; they are observation-only and not server logic.
const embeddedDraftDecoderTraceEnabled = false;
let draftMilestonesSeen = {};
let purchaseDecoderDepth = 0;
let purchaseMilestonesSeen = {};
let createMatchParserHooks = {};
// Retain only the address of the most recently decoded native DestroyMatch
// response.  The post-match continuation probe uses it to prove whether R14
// is the same DTO that carried endReason=QUIT; it never keeps or dereferences
// the object after the guarded observation window on its own.
let lastDecodedDestroyMatchText = null;
let lastDecodedDestroyMatchAtMs = 0;

function readPointerSlots(address, byteCount) {
  const slots = [];
  if (address === null || address.isNull()) return slots;
  const count = Math.max(0, Math.min(32, Math.floor(byteCount / Process.pointerSize)));
  for (let index = 0; index < count; index++) {
    try {
      slots.push(address.add(index * Process.pointerSize).readPointer().toString());
    } catch (_) {
      slots.push('<unreadable>');
    }
  }
  return slots;
}

function installCreateMatchParserTrace(cards, parserAddress, responseVtable) {
  const key = parserAddress.toString();
  if (createMatchParserHooks[key]) return;
  const range = Process.findRangeByAddress(parserAddress);
  if (range === null || range.protection.indexOf('x') < 0) {
    send({ event: 'draft-trace', status: 'create-match-parser-rejected',
      parser: key, vtable: responseVtable.toString(),
      reason: range === null ? 'unmapped' : range.protection });
    return;
  }
  createMatchParserHooks[key] = true;
  Interceptor.attach(parserAddress, {
    onEnter(args) {
      this.createMatchResponseMatched = false;
      this.createMatchResponse = args[0];
      this.createMatchReader = args[1];
      try {
        this.createMatchResponseMatched = !args[0].isNull() &&
          args[0].readPointer().equals(responseVtable);
      } catch (_) {}
      if (!this.createMatchResponseMatched) return;
      send({ event: 'draft-trace', status: 'create-match-parser-entered',
        response: args[0].toString(), reader: args[1].toString(),
        parser: parserAddress.toString(),
        objectSlots: readPointerSlots(args[0], 0x80) });
    },
    onLeave(result) {
      if (!this.createMatchResponseMatched) return;
      const resultRaw = result.toInt32();
      const resultByte = resultRaw & 0xff;
      // The parser hook is authoritative when it was installed before this
      // call. Transport discovery can race the very first parser invocation,
      // so accept its already-created candidate instead of creating another
      // generation here.
      if (resultByte === 1) {
        if (warmupRecoveryPendingGeneration !== warmupRecoveryGeneration)
          beginWarmupCreateMatchCandidate('parser-decoded');
        acceptWarmupCreateMatchCandidate('parser-decoded');
        if (warmupCreateMatchProviderRecoveryTrigger !== null)
          warmupCreateMatchProviderRecoveryTrigger('parser-decoded');
      }
      send({ event: 'draft-trace', status: 'create-match-decoded',
        response: this.createMatchResponse.toString(),
        reader: this.createMatchReader.toString(),
        parser: parserAddress.toString(), result: resultRaw,
        resultByte: resultRaw & 0xff,
        objectSlots: readPointerSlots(this.createMatchResponse, 0x80) });
    }
  });
  send({ event: 'draft-trace', status: 'create-match-parser-hooked',
    parser: parserAddress.toString(), vtable: responseVtable.toString(),
    moduleOffset: parserAddress.sub(cards.base).toString() });
}

function readDecodedDraftState(address, baseOffset) {
  try {
    if (address === null || address.isNull()) return { address: '0x0' };
    const state = address.add(baseOffset || 0);
    const roundsBegin = state.add(0x78).readPointer();
    const roundsEnd = state.add(0x80).readPointer();
    let roundsBytes = null;
    if (!roundsBegin.isNull() && !roundsEnd.isNull()) {
      try { roundsBytes = roundsEnd.sub(roundsBegin).toInt32(); } catch (_) {}
    } else if (roundsBegin.isNull() && roundsEnd.isNull()) {
      roundsBytes = 0;
    }
    return {
      address: state.toString(),
      parseSuccess: state.add(0x50).readU8(),
      squadState: state.add(0x58).readS32(),
      stateParam1: state.add(0x5c).readS32(),
      gamesWonCurrentMatch: state.add(0x60).readS32(),
      stateParam2: state.add(0x64).readS32(),
      coins: state.add(0x68).readS32(),
      points: state.add(0x6c).readS32(),
      draftToken: state.add(0x70).readS32(),
      roundsBegin: roundsBegin.toString(),
      roundsEnd: roundsEnd.toString(),
      roundsBytes: roundsBytes
    };
  } catch (error) {
    return { address: address ? address.toString() : null,
      error: String(error) };
  }
}

function readDecodedDestroyMatchState(address) {
  try {
    if (address === null || address.isNull()) return { address: '0x0' };
    return {
      address: address.toString(),
      vtable: address.readPointer().toString(),
      scalar28: address.add(0x28).readS32(),
      scalar2c: address.add(0x2c).readS32(),
      scalar30: address.add(0x30).readS32(),
      flag34: address.add(0x34).readU8(),
      scalar38: address.add(0x38).readS32(),
      scalar3c: address.add(0x3c).readS32(),
      seasonEndResultValue: address.add(0x40).readS32(),
      scalar44: address.add(0x44).readS32(),
      scalar48: address.add(0x48).readS32(),
      scalar4c: address.add(0x4c).readS32(),
      scalarB0: address.add(0xb0).readS32(),
      endReasonValue: address.add(0xb4).readS32(),
      flagB8: address.add(0xb8).readU8(),
      scalarBC: address.add(0xbc).readS32(),
      scalarC0: address.add(0xc0).readS32(),
      scalarC4: address.add(0xc4).readS32(),
      scalarC8: address.add(0xc8).readS32(),
      scalarCC: address.add(0xcc).readS32(),
      scalarD0: address.add(0xd0).readS32(),
      floatD4: address.add(0xd4).readFloat(),
      scalarD8: address.add(0xd8).readS32(),
      scalarDC: address.add(0xdc).readS32(),
      scalarE0: address.add(0xe0).readS32()
    };
  } catch (error) {
    return { address: address ? address.toString() : null,
      error: String(error) };
  }
}

function installPassiveDraftTrace() {
  if (draftTraceInstalled) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  const destructor = cards.base.add(0x250020);
  const completion = cards.base.add(0x250530);
  const constructor = cards.base.add(0x2506c0);
  const parser = cards.base.add(0x250af0);
  // FutPurchaseDraftModeServerResponse is constructed at 0x251480 and its
  // virtual response parser is at 0x251af0. 0x2516c0 is the request
  // serializer (it runs before HTTP), not the response decoder.
  const purchaseConstructor = cards.base.add(0x251480);
  const purchaseParser = cards.base.add(0x251af0);
  // `/user/credits` is requested on the same hub transition and is known to
  // decode successfully. Trace its response class as a control sample so a
  // Draft-specific schema rejection can be distinguished from a transport
  // failure affecting every REST response.
  const referenceConstructor = cards.base.add(0x20af00);
  const referenceParser = cards.base.add(0x20af90);
  // RS4:FutGetDraftAwardServerResponse is a bodyless 0x28-byte response.
  // Its constructor clears bodyRequired at +0x24 and its virtual parser is
  // the three-byte `return true` stub. Verify that stub statically, but never
  // attach an Interceptor to it: Frida cannot relocate such a short function
  // and aborting here would prevent the later CreateMatch transport trace
  // from being installed.
  const awardConstructor = cards.base.add(0x24f270);
  const awardParser = cards.base.add(0xa1a0);
  const awardVtable = cards.base.add(0x352f10);
  // RS4:FutDestroyMatchServerResponse.  Empty JSON decodes successfully but
  // leaves every continuation field at its constructor default; trace the
  // parser-backed response used by the post-QUIT return path.
  const destroyMatchConstructor = cards.base.add(0x1e95a0);
  const destroyMatchParser = cards.base.add(0x211790);
  const destroyMatchVtable = cards.base.add(0x375af8);
  const transportDispatch = cards.base.add(0x29f9e6);
  // Instruction-boundary milestones inside the same retail Draft decoder.
  // The enclosing parser signature is the version guard for these offsets.
  // They are observation-only and let a single live reproduction identify
  // the exact DTO member whose retail handler does not return.
  const parserMilestones = [
    [0x250b84, 'service-check-entered'],
    [0x250b90, 'service-check-returned'],
    [0x250c52, 'json-members-entered'],
    [0x250d57, 'roundsInfo-entered'],
    [0x250f10, 'gamesWonCurrentMatch-entered'],
    [0x250f2b, 'gameModeRestriction-entered'],
    [0x250f52, 'entranceCriteria-entered'],
    [0x25107a, 'squad-entered'],
    [0x251170, 'stateParam2-entered'],
    [0x25119b, 'stateParam1-entered'],
    [0x2511ef, 'squadState-entered'],
    [0x2512aa, 'members-complete'],
    [0x2512ef, 'parse-success'],
    [0x251392, 'decoder-cleanup']
  ];
  const purchaseParserMilestones = [
    [0x251c30, 'COINS-entered'],
    [0x251c70, 'POINTS-entered'],
    [0x251cb0, 'DRAFT_TOKEN-entered']
  ];
  try {
    const destructorSignatureOk = destructor.readU8() === 0x40 &&
      destructor.add(1).readU8() === 0x57;
    const completionSignatureOk = completion.readU8() === 0x48 &&
      completion.add(1).readU8() === 0x8b && completion.add(2).readU8() === 0xc4;
    const constructorSignatureOk = constructor.readU8() === 0x48 &&
      constructor.add(1).readU8() === 0x89 && constructor.add(2).readU8() === 0x4c;
    const parserSignatureOk = parser.readU8() === 0x40 &&
      parser.add(1).readU8() === 0x55 && parser.add(2).readU8() === 0x56;
    const purchaseConstructorSignatureOk =
      purchaseConstructor.readU8() === 0x48 &&
      purchaseConstructor.add(1).readU8() === 0x83 &&
      purchaseConstructor.add(2).readU8() === 0xec;
    const purchaseParserSignatureOk = purchaseParser.readU8() === 0x48 &&
      purchaseParser.add(1).readU8() === 0x8b &&
      purchaseParser.add(2).readU8() === 0xc4;
    const referenceConstructorSignatureOk =
      referenceConstructor.readU8() === 0x48 &&
      referenceConstructor.add(1).readU8() === 0x83;
    const referenceParserSignatureOk = referenceParser.readU8() === 0x48 &&
      referenceParser.add(1).readU8() === 0x8b &&
      referenceParser.add(2).readU8() === 0xc4;
    const awardConstructorSignatureOk =
      awardConstructor.readU8() === 0x48 &&
      awardConstructor.add(1).readU8() === 0x83 &&
      awardConstructor.add(2).readU8() === 0xec &&
      awardConstructor.add(3).readU8() === 0x28;
    const awardParserSignatureOk = awardParser.readU8() === 0xb0 &&
      awardParser.add(1).readU8() === 0x01 &&
      awardParser.add(2).readU8() === 0xc3;
    const destroyMatchConstructorSignatureOk =
      destroyMatchConstructor.readU8() === 0x48 &&
      destroyMatchConstructor.add(1).readU8() === 0x89 &&
      destroyMatchConstructor.add(2).readU8() === 0x4c;
    const destroyMatchParserSignatureOk = destroyMatchParser.readU8() === 0x48 &&
      destroyMatchParser.add(1).readU8() === 0x8b &&
      destroyMatchParser.add(2).readU8() === 0xc4;
    const transportDispatchSignatureOk = transportDispatch.readU8() === 0x48 &&
      transportDispatch.add(1).readU8() === 0x8b &&
      transportDispatch.add(2).readU8() === 0xc8;
    if (!destructorSignatureOk || !completionSignatureOk ||
        !constructorSignatureOk || !parserSignatureOk ||
        !purchaseConstructorSignatureOk || !purchaseParserSignatureOk ||
        !referenceConstructorSignatureOk || !referenceParserSignatureOk ||
        !awardConstructorSignatureOk || !awardParserSignatureOk ||
        !destroyMatchConstructorSignatureOk ||
        !destroyMatchParserSignatureOk ||
        !transportDispatchSignatureOk) {
      send({ event: 'draft-trace', status: 'signature-mismatch',
        module: cards.name, destructorSignatureOk: destructorSignatureOk,
        completionSignatureOk: completionSignatureOk,
        constructorSignatureOk: constructorSignatureOk,
        parserSignatureOk: parserSignatureOk,
        purchaseConstructorSignatureOk: purchaseConstructorSignatureOk,
        purchaseParserSignatureOk: purchaseParserSignatureOk,
        referenceConstructorSignatureOk: referenceConstructorSignatureOk,
        referenceParserSignatureOk: referenceParserSignatureOk,
        awardConstructorSignatureOk: awardConstructorSignatureOk,
        awardParserSignatureOk: awardParserSignatureOk,
        destroyMatchConstructorSignatureOk: destroyMatchConstructorSignatureOk,
        destroyMatchParserSignatureOk: destroyMatchParserSignatureOk,
        transportDispatchSignatureOk: transportDispatchSignatureOk });
      draftTraceInstalled = true;
      return true;
    }
    Interceptor.attach(constructor, {
      onEnter() {
        this.caller = this.returnAddress;
        try {
          this.backtrace = Thread.backtrace(this.context, Backtracer.ACCURATE)
            .slice(0, 10).map(DebugSymbol.fromAddress).map(String);
        } catch (_) { this.backtrace = []; }
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'constructed',
          response: result.toString(), caller: this.caller.toString(),
          backtrace: this.backtrace,
          state: readDecodedDraftState(result, 0) });
      }
    });
    Interceptor.attach(completion, {
      onEnter(args) {
        send({ event: 'draft-trace', status: 'completion-entered',
          object: args[0].toString(), caller: this.returnAddress.toString() });
      }
    });
    Interceptor.attach(parser, {
      onEnter(args) {
        this.draftStateObject = args[0];
        this.draftJsonReader = args[1];
        draftMilestonesSeen = {};
        send({ event: 'draft-trace', status: 'decoder-entered',
          response: args[0].toString(), reader: args[1].toString() });
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'decoded',
          result: result.toInt32(), reader: this.draftJsonReader.toString(),
          state: readDecodedDraftState(this.draftStateObject, 0) });
      }
    });
    parserMilestones.forEach(function (milestone) {
      Interceptor.attach(cards.base.add(milestone[0]), {
        onEnter() {
          if (draftMilestonesSeen[milestone[1]]) return;
          draftMilestonesSeen[milestone[1]] = true;
          send({ event: 'draft-trace', status: 'decoder-milestone',
            milestone: milestone[1], offset: '0x' + milestone[0].toString(16) });
        }
      });
    });
    Interceptor.attach(destructor, {
      onEnter(args) {
        send({ event: 'draft-trace', status: 'destroyed',
          response: args[0].toString(), caller: this.returnAddress.toString(),
          state: readDecodedDraftState(args[0], 0) });
      }
    });
    Interceptor.attach(purchaseConstructor, {
      onEnter() {
        this.caller = this.returnAddress;
      },
      onLeave(result) {
        let vtable = null;
        try { vtable = result.readPointer().toString(); } catch (_) {}
        send({ event: 'draft-trace', status: 'purchase-constructed',
          response: result.toString(), caller: this.caller.toString(),
          vtable: vtable });
      }
    });
    Interceptor.attach(purchaseParser, {
      onEnter(args) {
        purchaseDecoderDepth++;
        purchaseMilestonesSeen = {};
        this.purchaseResponse = args[0];
        this.purchaseReader = args[1];
        send({ event: 'draft-trace', status: 'purchase-decoder-entered',
          response: args[0].toString(), reader: args[1].toString() });
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'purchase-decoded',
          response: this.purchaseResponse.toString(),
          reader: this.purchaseReader.toString(), result: result.toInt32() });
        purchaseDecoderDepth = Math.max(0, purchaseDecoderDepth - 1);
      }
    });
    purchaseParserMilestones.forEach(function (milestone) {
      Interceptor.attach(cards.base.add(milestone[0]), {
        onEnter() {
          if (purchaseDecoderDepth <= 0 ||
              purchaseMilestonesSeen[milestone[1]]) return;
          purchaseMilestonesSeen[milestone[1]] = true;
          send({ event: 'draft-trace', status: 'purchase-decoder-milestone',
            milestone: milestone[1], offset: '0x' + milestone[0].toString(16) });
        }
      });
    });
    Interceptor.attach(referenceConstructor, {
      onEnter() {
        this.caller = this.returnAddress;
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'reference-constructed',
          response: result.toString(), caller: this.caller.toString() });
      }
    });
    Interceptor.attach(referenceParser, {
      onEnter(args) {
        this.referenceResponse = args[0];
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'reference-decoded',
          response: this.referenceResponse.toString(),
          result: result.toInt32() });
      }
    });
    Interceptor.attach(awardConstructor, {
      onLeave(result) {
        let vtableMatches = false;
        let bodyRequired = null;
        try {
          vtableMatches = result.readPointer().equals(awardVtable);
          bodyRequired = result.add(0x24).readU8();
        } catch (_) {}
        send({ event: 'draft-trace', status: 'award-constructed',
          response: result.toString(), vtableMatches: vtableMatches,
          bodyRequired: bodyRequired });
      }
    });
    send({ event: 'draft-trace', status: 'award-parser-static',
      parser: awardParser.toString(), signatureOk: awardParserSignatureOk,
      result: 1 });
    Interceptor.attach(destroyMatchConstructor, {
      onEnter(args) {
        this.destroyMatchResponse = args[0];
        this.destroyMatchCaller = this.returnAddress;
        try {
          this.destroyMatchBacktrace = Thread.backtrace(
            this.context, Backtracer.ACCURATE).slice(0, 10)
            .map(DebugSymbol.fromAddress).map(String);
        } catch (_) { this.destroyMatchBacktrace = []; }
      },
      onLeave(result) {
        send({ event: 'draft-trace', status: 'destroy-match-constructed',
          response: result.toString(),
          caller: this.destroyMatchCaller.toString(),
          backtrace: this.destroyMatchBacktrace,
          state: readDecodedDestroyMatchState(result) });
      }
    });
    Interceptor.attach(destroyMatchParser, {
      onEnter(args) {
        this.destroyMatchResponse = args[0];
        this.destroyMatchReader = args[1];
        this.destroyMatchCaller = this.returnAddress;
        try {
          this.destroyMatchBacktrace = Thread.backtrace(
            this.context, Backtracer.ACCURATE).slice(0, 10)
            .map(DebugSymbol.fromAddress).map(String);
        } catch (_) { this.destroyMatchBacktrace = []; }
        let vtableMatches = false;
        try {
          vtableMatches = args[0].readPointer().equals(destroyMatchVtable);
        } catch (_) {}
        send({ event: 'draft-trace', status: 'destroy-match-decoder-entered',
          response: args[0].toString(), reader: args[1].toString(),
          caller: this.destroyMatchCaller.toString(),
          backtrace: this.destroyMatchBacktrace,
          vtableMatches: vtableMatches,
          state: readDecodedDestroyMatchState(args[0]) });
      },
      onLeave(result) {
        lastDecodedDestroyMatchText = this.destroyMatchResponse.toString();
        lastDecodedDestroyMatchAtMs = Date.now();
        send({ event: 'draft-trace', status: 'destroy-match-decoded',
          response: this.destroyMatchResponse.toString(),
          reader: this.destroyMatchReader.toString(),
          caller: this.destroyMatchCaller.toString(),
          backtrace: this.destroyMatchBacktrace,
          result: result.toInt32(),
          state: readDecodedDestroyMatchState(this.destroyMatchResponse) });
      }
    });
    Interceptor.attach(transportDispatch, {
      onEnter() {
        try {
          const response = this.context.rax;
          if (response.isNull()) return;
          const responseVtable = response.readPointer();
          const bodyAddress = this.context.r15;
          const bodyLength = this.context.r14.toInt32();
          let bodyPreview = '';
          if (!bodyAddress.isNull() && bodyLength > 0 && bodyLength <= 1048576) {
            bodyPreview = bodyAddress.readUtf8String(Math.min(bodyLength, 4096));
          }
          // CreateMatch is a closed response class whose vtable was not known
          // when the Draft-state trace was written. Detect only its distinctive
          // top-level JSON envelope, then observe the exact virtual parser that
          // this retail build is about to call. No result or object is changed.
          // GET /squad/list starts with {"squad":[...]}; only the CreateMatch
          // DTO has a single squad object. Do not hook a list parser and later
          // mistake its successful return for CreateMatch acceptance.
          const createMatchEnvelope =
            bodyPreview.indexOf('{"squad": {') === 0 ||
            bodyPreview.indexOf('{"squad":{') === 0;
          if (createMatchEnvelope) {
            const createMatchParser = responseVtable.add(8).readPointer();
            const httpStatus = this.context.r12.toInt32();
            const bodyRequired = response.add(0x24).readU8();
            // The first encounter discovers and attaches the virtual parser
            // while the same dispatch is already in flight, so onEnter can be
            // missed. Record only an exact successful CreateMatch envelope as
            // pending; the parser result or downstream FUT mode controller
            // must still confirm it before active recovery is authorized.
            if (httpStatus === 200 && bodyRequired === 1 && bodyLength > 0)
              beginWarmupCreateMatchCandidate('transport');
            installCreateMatchParserTrace(cards, createMatchParser,
                                          responseVtable);
            send({ event: 'draft-trace', status: 'create-match-transport',
              response: response.toString(), vtable: responseVtable.toString(),
              parser: createMatchParser.toString(),
              body: bodyAddress.toString(), bodyLength: bodyLength,
              httpStatus: httpStatus,
              bodyRequired: bodyRequired,
              preview: bodyPreview.slice(0, 160) });
          }
          const draftVtable = cards.base.add(0x37c2d0);
          const purchaseVtable = cards.base.add(0x37c2f8);
          const referenceVtable = cards.base.add(0x375e98);
          if (!responseVtable.equals(draftVtable) &&
              !responseVtable.equals(purchaseVtable) &&
              !responseVtable.equals(referenceVtable) &&
              !responseVtable.equals(awardVtable) &&
              !responseVtable.equals(destroyMatchVtable)) return;
          send({ event: 'draft-trace', status: 'transport-dispatch',
            kind: responseVtable.equals(draftVtable) ? 'draft' :
              (responseVtable.equals(purchaseVtable) ? 'draft-purchase' :
               (responseVtable.equals(awardVtable) ? 'draft-award' :
                (responseVtable.equals(destroyMatchVtable) ?
                 'destroy-match' : 'credits'))),
            response: response.toString(), body: bodyAddress.toString(),
            bodyLength: bodyLength,
            httpStatus: this.context.r12.toInt32(),
            bodyRequired: response.add(0x24).readU8() });
        } catch (error) {
          send({ event: 'draft-trace', status: 'transport-trace-error',
            error: String(error) });
        }
      }
    });
    draftTraceInstalled = true;
    send({ event: 'draft-trace', status: 'passive-ready', module: cards.name,
      constructor: constructor.toString(), parser: parser.toString(),
      purchaseConstructor: purchaseConstructor.toString(),
      purchaseParser: purchaseParser.toString(),
      awardConstructor: awardConstructor.toString(),
      awardParser: awardParser.toString(),
      destroyMatchConstructor: destroyMatchConstructor.toString(),
      destroyMatchParser: destroyMatchParser.toString() });
    return true;
  } catch (error) {
    send({ event: 'draft-trace', status: 'install-error', module: cards.name,
      error: String(error) });
    return false;
  }
}

if (embeddedDraftDecoderTraceEnabled && !installPassiveDraftTrace()) {
  send({ event: 'draft-trace', status: 'module-waiting' });
  draftTraceRetryTimer = setInterval(function () {
    try {
      if (installPassiveDraftTrace()) {
        clearInterval(draftTraceRetryTimer);
        draftTraceRetryTimer = null;
      }
    } catch (error) {
      clearInterval(draftTraceRetryTimer);
      draftTraceRetryTimer = null;
      send({ event: 'draft-trace', status: 'module-late-error',
        error: String(error) });
    }
  }, 1000);
}

// Trace for the native FUT pre-match gate in the verified retail CardsDLL
// build. Every address has an instruction-byte guard before any Interceptor or
// NativeFunction is installed. Active recovery is fail-closed and replays only
// the game's verified STOP handler after an accepted current CreateMatch. It
// never patches code or writes game object state directly. The native offline
// team-selection viewmodel is also observed so an accepted CreateMatch can be
// delivered through its own FUT_CREATE_MATCH_DP handler when the normal
// provider notification is lost. That handler publishes MATCH_CREATED and is
// the verified bridge from team/kit selection into gameplay.
let warmupGateTraceInstalled = false;
let warmupGateTraceRetryTimer = null;
// The former embedded warm-up recovery invoked CardsDLL providers/factories
// from Frida's thread and polled FIFA memory throughout asset loading.  Live
// Draft evidence showed that it could only create a payload-less MatchData
// controller/HUD and caused severe stutter.  Keep the implementation available
// for static comparison, but never install it in the server process.  Draft
// lifecycle diagnosis now lives in bounded, passive tools/live_draft_* probes.
const embeddedDraftWarmupTraceEnabled = false;
let warmupGateTraceSequence = 0;
let warmupRecoveryGeneration = 0;
let warmupRecoveryPendingGeneration = 0;
let warmupRecoveryPendingAtMs = 0;
let warmupRecoveryAcceptedGeneration = 0;
let warmupRecoveryAcceptedAtMs = 0;
let warmupRecoveryAttemptedGeneration = 0;
let warmupRecoveryInFlight = false;
let warmupModeObjectText = null;
let warmupModeObjectGeneration = 0;
let warmupStatePollTimer = null;
// A router branch that merely forwards provider 0x7569 with R8=0 is not a
// usable MatchData publication.  Keep that observation separate so it cannot
// suppress the real builder recovery.
let warmupMatchDataRouteObservedGeneration = 0;
let warmupMatchDataPublishedGeneration = 0;
let warmupMatchDataWaitingGeneration = 0;
let warmupMatchDataBuildAttemptedGeneration = 0;
let warmupMatchDataBuildInFlight = false;
let warmupMatchDataRouterText = null;
let warmupMatchDataRouterVtableText = null;
let warmupMatchDataRouterStableObservations = 0;
let warmupMatchDataRouterAttemptedGeneration = 0;
let warmupMatchDataRouterAttemptedAtMs = 0;
let warmupMatchDataRouterWaitingGeneration = 0;
let warmupMatchDataRouterRecoveryInFlight = false;
let warmupFutMatchViewModelGeneration = 0;
let warmupKitLoadCompleteGeneration = 0;
let warmupMqTileDataGeneration = 0;
let warmupStopWaitingGeneration = 0;
let warmupEvent37ObservedGeneration = 0;
let warmupStateTwoObservedAtMs = 0;
let warmupMatchDataCandidateSequence = 0;
const warmupMatchDataCandidates = {};
let warmupCreateMatchControllerText = null;
let warmupCreateMatchProviderObservedGeneration = 0;
let warmupCreateMatchProviderAttemptedGeneration = 0;
let warmupCreateMatchProviderWaitingGeneration = 0;
let warmupCreateMatchProviderRecoveryInFlight = false;
let warmupCreateMatchProviderRecoveryTrigger = null;
let warmupOfflineSelectControllerText = null;
let warmupOfflineSelectObservedGeneration = 0;
let warmupOfflineSelectAttemptedGeneration = 0;
let warmupOfflineSelectWaitingGeneration = 0;
let warmupOfflineSelectRecoveryInFlight = false;
let warmupOfflineSelectRecoveryAttemptCount = 0;
let warmupFactoryArg0Text = null;
let warmupFactoryArg1Text = null;
let warmupFactoryAllocatorText = null;
let warmupFactoryArg0VtableText = null;
let warmupFactoryArg1VtableText = null;
let warmupFactoryAllocatorVtableText = null;
let warmupFactoryContextGeneration = 0;
let warmupMatchBootstrapFactoryAttemptedGeneration = 0;
let warmupMatchBootstrapFactoryInFlight = false;
let warmupRecoveredMatchDataControllerText = null;
let warmupRecoveredFutMatchViewModelText = null;
let warmupMatchDayQueryInstalled = false;
let warmupMatchDayServiceCaptured = false;
const warmupMatchDayMethodHooks = {};
const warmupRecoveryMaxAgeMs = 120000;
// CreateMatch parsing is the authoritative acceptance gate, but it is not
// proof that MatchData exists. Never advance the state-7/Press Start chain
// until provider 0x7569 has published a non-null payload for this generation.
// The genuine router is called only after it has been observed repeatedly
// with a stable CardsDLL vtable. Source event 1012 is a verified null-payload
// route to FUT_MATCH_DATA_DP (0x7569). Give its downstream controller/viewmodel
// and kit chain a bounded observation window while STOP remains blocked.
const warmupMatchDataRouterRecoveryDelayMs = 750;
const warmupMatchDataRouterObservationMs = 2000;
const warmupMatchDataRouterUnavailableFallbackMs = 8000;
// Give the game's normal FUT_CREATE_MATCH_DP delivery enough time to run.
// The live trace showed it arriving roughly 460 ms after parser acceptance;
// the old 50 ms fallback therefore created MATCH_CREATED twice.
const warmupOfflineSelectRecoveryDelayMs = 750;
// The natural 0x7564 call and every forced retry returned null in the same
// generation. Keep one guarded diagnostic recovery only; repeated native
// calls add main-thread work without publishing MATCH_CREATED.
const warmupOfflineSelectRecoveryMaxAttempts = 1;
// AssetLoadingStart (0x32) is followed by the one-shot native 0x37 readiness
// event several seconds later. Live timing placed state 2 at 01:14:27 and
// 0x37 at 01:14:33. Keep a five-second state-2 bootstrap window so stadium,
// lighting, audio and gameplay context can finish, while reaching state 7
// before the one-shot readiness handler checks it.
const warmupStateTwoBootstrapDelayMs = 5000;
// A 100 ms cadence still observes state 2 well before the five-second gate
// while avoiding forty Frida memory-read passes per second during asset load.
const warmupStatePollIntervalMs = 100;
// Do not invoke the native STOP handler re-entrantly from event 0x37.  The
// live trace proved that the outer handler continued immediately afterwards
// and replaced state 7 before the UI could render its Press Start frame.
// One frame-sized delay lets the current native dispatch unwind first.
const warmupRecoveryDelayMs = 50;

function emitWarmupGate(stage, details) {
  const payload = {
    event: 'warmup-gate',
    sequence: ++warmupGateTraceSequence,
    timestampMs: Date.now(),
    stage: stage
  };
  if (details) {
    Object.keys(details).forEach(function (key) {
      payload[key] = details[key];
    });
  }
  send(payload);
}

function beginWarmupCreateMatchCandidate(source) {
  warmupRecoveryGeneration++;
  warmupRecoveryPendingGeneration = warmupRecoveryGeneration;
  warmupRecoveryPendingAtMs = Date.now();
  warmupRecoveryAcceptedGeneration = 0;
  warmupRecoveryAcceptedAtMs = 0;
  warmupRecoveryAttemptedGeneration = 0;
  warmupMatchDataRouteObservedGeneration = 0;
  warmupMatchDataPublishedGeneration = 0;
  warmupMatchDataWaitingGeneration = 0;
  warmupMatchDataBuildAttemptedGeneration = 0;
  warmupMatchDataRouterAttemptedGeneration = 0;
  warmupMatchDataRouterAttemptedAtMs = 0;
  warmupMatchDataRouterWaitingGeneration = 0;
  warmupMatchDataRouterRecoveryInFlight = false;
  warmupFutMatchViewModelGeneration = 0;
  warmupKitLoadCompleteGeneration = 0;
  warmupMqTileDataGeneration = 0;
  warmupStopWaitingGeneration = 0;
  warmupEvent37ObservedGeneration = 0;
  warmupStateTwoObservedAtMs = 0;
  warmupCreateMatchProviderObservedGeneration = 0;
  warmupCreateMatchProviderAttemptedGeneration = 0;
  warmupCreateMatchProviderWaitingGeneration = 0;
  warmupCreateMatchProviderRecoveryInFlight = false;
  warmupOfflineSelectObservedGeneration = 0;
  warmupOfflineSelectAttemptedGeneration = 0;
  warmupOfflineSelectWaitingGeneration = 0;
  warmupOfflineSelectRecoveryInFlight = false;
  warmupOfflineSelectRecoveryAttemptCount = 0;
  warmupFactoryArg0Text = null;
  warmupFactoryArg1Text = null;
  warmupFactoryAllocatorText = null;
  warmupFactoryArg0VtableText = null;
  warmupFactoryArg1VtableText = null;
  warmupFactoryAllocatorVtableText = null;
  warmupFactoryContextGeneration = 0;
  warmupMatchBootstrapFactoryAttemptedGeneration = 0;
  warmupMatchBootstrapFactoryInFlight = false;
  warmupRecoveredMatchDataControllerText = null;
  warmupRecoveredFutMatchViewModelText = null;
  // MatchData controller lifetime spans multiple CreateMatch requests. Keep
  // constructor-captured candidates across generations; selection revalidates
  // both vtables, context and native guards, while the verified destructor
  // removes released objects. Clearing this map here discarded the only live
  // controller immediately before recovery needed it.
  warmupModeObjectText = null;
  warmupModeObjectGeneration = 0;
  emitWarmupGate('create-match-response-candidate', {
    generation: warmupRecoveryGeneration,
    candidateGeneration: warmupRecoveryPendingGeneration,
    candidateAtMs: warmupRecoveryPendingAtMs,
    source: source
  });
}

function acceptWarmupCreateMatchCandidate(source) {
  if (warmupRecoveryAcceptedGeneration === warmupRecoveryGeneration &&
      warmupRecoveryGeneration > 0) return true;
  const now = Date.now();
  const candidateAgeMs = now - warmupRecoveryPendingAtMs;
  if (warmupRecoveryGeneration <= 0 ||
      warmupRecoveryPendingGeneration !== warmupRecoveryGeneration ||
      warmupRecoveryPendingAtMs <= 0 || candidateAgeMs < 0 ||
      candidateAgeMs > warmupRecoveryMaxAgeMs) return false;
  warmupRecoveryAcceptedGeneration = warmupRecoveryGeneration;
  warmupRecoveryAcceptedAtMs = now;
  emitWarmupGate('create-match-response-accepted', {
    generation: warmupRecoveryGeneration,
    acceptedGeneration: warmupRecoveryAcceptedGeneration,
    acceptedAtMs: warmupRecoveryAcceptedAtMs,
    candidateAgeMs: candidateAgeMs,
    source: source
  });
  return true;
}

function warmupBytesMatch(address, expected) {
  try {
    for (let index = 0; index < expected.length; index++) {
      if (address.add(index).readU8() !== expected[index]) return false;
    }
    return true;
  } catch (_) {
    return false;
  }
}

function readWarmupS32(object, offset, label) {
  try {
    const address = object.add(offset);
    const range = Process.findRangeByAddress(address);
    if (range === null || range.protection.indexOf('r') < 0 ||
        address.add(4).compare(range.base.add(range.size)) > 0) {
      return { value: null, error: label + ':unreadable' };
    }
    return { value: address.readS32(), error: null };
  } catch (error) {
    return { value: null, error: label + ':' + String(error) };
  }
}

function readWarmupU8(object, offset, label) {
  try {
    const address = object.add(offset);
    const range = Process.findRangeByAddress(address);
    if (range === null || range.protection.indexOf('r') < 0 ||
        address.add(1).compare(range.base.add(range.size)) > 0) {
      return { value: null, error: label + ':unreadable' };
    }
    return { value: address.readU8(), error: null };
  } catch (error) {
    return { value: null, error: label + ':' + String(error) };
  }
}

function readWarmupPointer(object, offset, label, allowNull) {
  try {
    const address = object.add(offset);
    const range = Process.findRangeByAddress(address);
    if (range === null || range.protection.indexOf('r') < 0 ||
        address.add(Process.pointerSize).compare(
          range.base.add(range.size)) > 0) {
      return { value: null, error: label + ':unreadable' };
    }
    const value = address.readPointer();
    if (value.isNull() && !allowNull)
      return { value: null, error: label + ':null' };
    return { value: value, error: null };
  } catch (error) {
    return { value: null, error: label + ':' + String(error) };
  }
}

function describeWarmupCode(address) {
  const details = { address: address.toString(), module: null,
    moduleOffset: null, symbol: null };
  try {
    const module = Process.findModuleByAddress(address);
    if (module !== null) {
      details.module = module.name;
      details.moduleOffset = address.sub(module.base).toString();
    }
    details.symbol = String(DebugSymbol.fromAddress(address));
  } catch (error) {
    details.error = String(error);
  }
  return details;
}

function installWarmupMatchDayMethodTrace(service, vtable, offset, label) {
  let method;
  try {
    method = vtable.add(offset).readPointer();
  } catch (error) {
    emitWarmupGate('match-day-method-read-error', {
      object: service.toString(), vtable: vtable.toString(),
      methodOffset: offset, methodLabel: label, error: String(error)
    });
    return;
  }
  const key = method.toString();
  if (warmupMatchDayMethodHooks[key]) return;
  const range = Process.findRangeByAddress(method);
  if (range === null || range.protection.indexOf('x') < 0) {
    emitWarmupGate('match-day-method-rejected', {
      object: service.toString(), vtable: vtable.toString(),
      methodOffset: offset, methodLabel: label,
      method: describeWarmupCode(method),
      reason: range === null ? 'unmapped' : range.protection
    });
    return;
  }
  warmupMatchDayMethodHooks[key] = true;
  Interceptor.attach(method, {
    onEnter(args) {
      this.matchDayObject = args[0];
      emitWarmupGate('match-day-method-enter', {
        object: args[0].toString(), methodOffset: offset,
        methodLabel: label, method: describeWarmupCode(method),
        argument1: args[1].toString(),
        objectSlots: readPointerSlots(args[0], 0x100),
        caller: this.returnAddress.toString()
      });
    },
    onLeave(result) {
      emitWarmupGate('match-day-method-leave', {
        object: this.matchDayObject.toString(), methodOffset: offset,
        methodLabel: label, method: describeWarmupCode(method),
        result: result.toString()
      });
    }
  });
  emitWarmupGate('match-day-method-hooked', {
    object: service.toString(), vtable: vtable.toString(),
    methodOffset: offset, methodLabel: label,
    method: describeWarmupCode(method)
  });
}

function installWarmupMatchDayTrace(fifa) {
  if (warmupMatchDayQueryInstalled) return true;
  const queryInterface = fifa.base.add(0xc321ee0);
  if (!warmupBytesMatch(queryInterface,
      [0x81,0xfa,0xf4,0xbf,0xbd,0x0e,0x74,0x0c,0x31,0xc0,
       0x81,0xfa,0x6e,0x51,0x3f,0xee])) {
    emitWarmupGate('match-day-query-signature-mismatch', {
      query: describeWarmupCode(queryInterface)
    });
    return false;
  }
  Interceptor.attach(queryInterface, {
    onEnter(args) {
      this.matchDayTag = args[1].toInt32() >>> 0;
      this.matchDayService = args[0];
      this.matchDayCapture = this.matchDayTag === 0x0ebdbff4 &&
        !warmupMatchDayServiceCaptured;
      if (!this.matchDayCapture) return;
      warmupMatchDayServiceCaptured = true;
      try {
        const vtable = args[0].readPointer();
        emitWarmupGate('match-day-service-captured', {
          object: args[0].toString(), tag: this.matchDayTag,
          vtable: vtable.toString(),
          query: describeWarmupCode(queryInterface),
          method28: describeWarmupCode(vtable.add(0x28).readPointer()),
          method88: describeWarmupCode(vtable.add(0x88).readPointer()),
          method98: describeWarmupCode(vtable.add(0x98).readPointer()),
          objectSlots: readPointerSlots(args[0], 0x100),
          vtableSlots: readPointerSlots(vtable, 0xa0)
        });
        installWarmupMatchDayMethodTrace(args[0], vtable, 0x28,
          'set-start-date-time');
        installWarmupMatchDayMethodTrace(args[0], vtable, 0x88,
          'publish-match-day');
        installWarmupMatchDayMethodTrace(args[0], vtable, 0x98,
          'set-match-date-time');
      } catch (error) {
        emitWarmupGate('match-day-service-capture-error', {
          object: args[0].toString(), tag: this.matchDayTag,
          error: String(error)
        });
      }
    },
    onLeave(result) {
      if (!this.matchDayCapture) return;
      emitWarmupGate('match-day-query-return', {
        object: this.matchDayService.toString(), result: result.toString()
      });
    }
  });
  warmupMatchDayQueryInstalled = true;
  emitWarmupGate('match-day-query-ready', {
    module: fifa.name, query: describeWarmupCode(queryInterface)
  });
  return true;
}

function readWarmupModeState(object) {
  if (object === null || object.isNull()) {
    return { object: '0x0', state: null, gameModeId: null,
      readErrors: ['object:null'] };
  }
  const state = readWarmupS32(object, 0xf00, 'state');
  const gameModeId = readWarmupS32(object, 0x18, 'gameModeId');
  const readErrors = [];
  if (state.error !== null) readErrors.push(state.error);
  if (gameModeId.error !== null) readErrors.push(gameModeId.error);
  return { object: object.toString(), state: state.value,
    gameModeId: gameModeId.value, readErrors: readErrors };
}

function rememberWarmupMatchDataController(object, source) {
  if (object === null || object.isNull()) return;
  try {
    const range = Process.findRangeByAddress(object);
    if (range === null || range.protection.indexOf('r') < 0) return;
    const key = object.toString();
    warmupMatchDataCandidates[key] = {
      object: key, source: source,
      sequence: ++warmupMatchDataCandidateSequence,
      seenAtMs: Date.now()
    };
    emitWarmupGate('match-data-controller-captured', {
      object: key, source: source,
      candidateSequence: warmupMatchDataCandidateSequence
    });
  } catch (error) {
    emitWarmupGate('match-data-controller-capture-error', {
      object: object.toString(), source: source, error: String(error)
    });
  }
}

function selectWarmupMatchDataController(cards) {
  const valid = [];
  const rejected = [];
  const primaryVtable = cards.base.add(0x35a7f0);
  const secondaryVtable = cards.base.add(0x35a7d8);
  Object.keys(warmupMatchDataCandidates).forEach(function (key) {
    const candidate = warmupMatchDataCandidates[key];
    const object = ptr(key);
    const primary = readWarmupPointer(object, 0, 'primaryVtable');
    const secondary = readWarmupPointer(object, 0x128, 'secondaryVtable');
    const ready = readWarmupS32(object, 0xd40, 'matchDataReady');
    const parameter = readWarmupS32(object, 0xd3c, 'matchDataParam');
    const context = readWarmupPointer(
      object, 0xc58, 'matchDataContext', true);
    if (primary.error !== null || secondary.error !== null ||
        ready.error !== null || parameter.error !== null ||
        context.error !== null) {
      delete warmupMatchDataCandidates[key];
      rejected.push({ object: key, source: candidate.source,
        reason: 'unreadable-or-released', primaryError: primary.error,
        secondaryError: secondary.error, readyError: ready.error,
        parameterError: parameter.error, contextError: context.error });
      return;
    }
    if (!primary.value.equals(primaryVtable) ||
        !secondary.value.equals(secondaryVtable)) {
      delete warmupMatchDataCandidates[key];
      rejected.push({ object: key, source: candidate.source,
        reason: 'controller-vtable-mismatch',
        primaryVtable: primary.value.toString(),
        secondaryVtable: secondary.value.toString() });
      return;
    }
    if (context.value.isNull()) {
      rejected.push({ object: key, source: candidate.source,
        reason: 'context-not-assigned' });
      return;
    }
    const contextState = readWarmupS32(
      context.value, 0x15c, 'matchDataContextState');
    const contextActive = readWarmupU8(
      context.value, 0x165, 'matchDataContextActive');
    if (contextState.error !== null || contextActive.error !== null) {
      rejected.push({ object: key, source: candidate.source,
        reason: 'context-unreadable', context: context.value.toString(),
        contextStateError: contextState.error,
        contextActiveError: contextActive.error });
      return;
    }
    if (ready.value !== 0 || parameter.value === -1) {
      rejected.push({ object: key, source: candidate.source,
        reason: 'native-guard-failed', ready: ready.value,
        parameter: parameter.value });
      return;
    }
    if (contextState.value !== 2 || contextActive.value === 0) {
      rejected.push({ object: key, source: candidate.source,
        reason: 'context-not-ready', context: context.value.toString(),
        contextState: contextState.value,
        contextActive: contextActive.value });
      return;
    }
    valid.push({ object: key, source: candidate.source,
      sequence: candidate.sequence, ready: ready.value,
      parameter: parameter.value, context: context.value.toString(),
      contextState: contextState.value, contextActive: contextActive.value });
  });
  if (valid.length !== 1) {
    return { controller: null,
      reason: valid.length === 0 ? 'match-data-controller-missing' :
        'match-data-controller-ambiguous',
      valid: valid, rejected: rejected };
  }
  return { controller: ptr(valid[0].object), reason: null,
    valid: valid, rejected: rejected };
}

function addWarmupCallSite(details, invocation, cards) {
  try {
    const caller = invocation.returnAddress;
    details.caller = caller.toString();
    details.callerOffset = caller.sub(cards.base).toString();
    details.callerSymbol = String(DebugSymbol.fromAddress(caller));
  } catch (error) {
    details.callSiteError = String(error);
  }
}

function installPassiveWarmupGateTrace() {
  if (warmupGateTraceInstalled) return true;
  const cards = Process.findModuleByName('CardsDLL_Win64_retail.dll');
  if (cards === null) return false;
  const fifa = Process.findModuleByName('FIFA19.exe');
  if (fifa !== null) installWarmupMatchDayTrace(fifa);

  const createMatchProviderHandler = cards.base.add(0x53320);
  const createMatchDpAccepted = cards.base.add(0x536ed);
  const createMatchParser = cards.base.add(0x20e0e0);
  const createMatchResponseVtable = cards.base.add(0x3763e0);
  const responseRequestVtable = cards.base.add(0x376320);
  const responseCompletionDispatch = cards.base.add(0x20d4e0);
  // The parser result is consumed by this response-routing routine. These
  // three probes are diagnostic only: they expose the live dispatch objects
  // and the previously unseen provider 0x7535 without modifying registers or
  // invoking an unknown virtual method.
  const createMatchPostParser = cards.base.add(0x29fa33);
  const createMatchResponseDispatch = cards.base.add(0x29fb9c);
  const createMatchProviderPublish7535 = cards.base.add(0x29fc44);
  // `futofflineselectteamviewmodel` is registered at +0x2e0f2 with factory
  // +0x49f90. Its primary +0x356980 vtable dispatches data-provider updates
  // through +0x46e50. Provider 0x7564 reaches +0x4700b and publishes the
  // literal MATCH_CREATED; this is the authoritative offline match bridge.
  const offlineSelectFactory = cards.base.add(0x49f90);
  const offlineSelectConstructor = cards.base.add(0x45f00);
  const offlineSelectDestructor = cards.base.add(0x460f0);
  const offlineSelectProviderHandler = cards.base.add(0x46e50);
  const offlineSelectCreateMatchBranch = cards.base.add(0x4700b);
  // Allocator-backed controller factories.  Their return values are the
  // authoritative fully constructed objects; tracing them also covers builds
  // where the deleting-destructor prologue differs or construction happens
  // through the registered factory rather than a direct controller call.
  const createMatchControllerFactory = cards.base.add(0x54f00);
  const matchDataControllerFactory = cards.base.add(0x76e70);
  // Constructor/destructor of the concrete FUT MatchData controller. Static
  // vtable references prove that +0x6d230 installs the primary +0x35a7f0
  // table (handler +0x709f0) and the secondary +0x35a7d8 table at +0x128
  // (handler +0x76630). Capture the completed object instead of guessing a
  // controller from unrelated provider callbacks or scanning the heap.
  const matchDataControllerConstructor = cards.base.add(0x6d230);
  const matchDataControllerDestructor = cards.base.add(0x6e050);
  const matchDataBuilder = cards.base.add(0x6fc80);
  const matchDataPublish = cards.base.add(0x6ff0b);
  const matchDataProviderHandler = cards.base.add(0x709f0);
  const matchDataProviderSubHandler = cards.base.add(0x76630);
  const futEventDispatcher = cards.base.add(0x179dd0);
  const futFeStopDecision = cards.base.add(0x17c910);
  const futFeStopPayloadZeroGate = cards.base.add(0x17c9d3);
  const futFeStopStateTwoGate = cards.base.add(0x17c9dc);
  const stateSevenRequest = cards.base.add(0x17cb61);
  const stateRequestMethod = cards.base.add(0x17d430);
  const futFeToBe = cards.base.add(0x17dc80);
  const event37Handler = cards.base.add(0x182240);
  const event37StateSevenGate = cards.base.add(0x1822b7);
  const canShowPressStartPublish = cards.base.add(0x18237f);
  const matchReadyMethod = cards.base.add(0x135c50);
  // Genuine MatchData routes discovered from the retail provider tables.
  // These passive probes identify which native bootstrap stage is missing
  // when gameplay starts with a black scene.
  const matchDataRouter = cards.base.add(0x15b5f0);
  const matchDataRoute7569 = cards.base.add(0x15b730);
  const matchDataProviderDispatch = cards.base.add(0x15b738);
  const futMatchViewModelFactory = cards.base.add(0x12d3f0);
  const provider7568Publish = cards.base.add(0x1264f8);
  // 0x756a is FUT_MQ_TILE_DATA_DP. These handlers belong to MQ tile data and
  // are kept separate from the real kit-complete provider 0x7568.
  const mqTileDataProviderHandler = cards.base.add(0x12d310);
  // Do not interpret +0x108ee0/+0x10905f as kit readiness either: the latter
  // explicitly requests 0x756a and its vtables resolve to the Rivals reward
  // viewmodel family.
  const mqTileDataConsumer = cards.base.add(0x120c40);
  const mqTileDataConsumerSub = cards.base.add(0x121210);
  // The DestroyMatch parser can succeed while the native frontend remains on
  // "Loading Ultimate Team...". These instruction-boundary probes expose the
  // exact state gate, the pre-provider status and the common continuation.
  // Static inspection proves that the status at [rsp+0x30] predates provider
  // 0x7583 and that native QUIT reaches the common continuation directly.
  const postDestroyStateGate = cards.base.add(0xd7c48);
  const postDestroyQuitRecoveryGate = cards.base.add(0xd7c4f);
  const postDestroyProviderResultGate = cards.base.add(0xd7d16);
  const postDestroyContinuation = cards.base.add(0xd7e6e);

  const signatureChecks = {
    createMatchProviderHandler: warmupBytesMatch(createMatchProviderHandler,
      [0x48,0x8b,0xc4,0x56,0x57,0x41,0x56,0x48,0x81,0xec,0xe0,0x00,0x00,0x00]),
    createMatchDpAccepted: warmupBytesMatch(createMatchDpAccepted,
      [0xc6,0x40,0x28,0x00,0x48,0x8b,0x0d,0x58,0x64,0x3d,0x00]),
    createMatchParser: warmupBytesMatch(createMatchParser,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0x6c,0x24,0x80]),
    responseCompletionDispatch: warmupBytesMatch(responseCompletionDispatch,
      [0x48,0x89,0x5c,0x24,0x08,0x57,0x48,0x83,0xec,0x20]),
    createMatchPostParser: warmupBytesMatch(createMatchPostParser,
      [0x84,0xc0,0x75,0x0b]),
    createMatchResponseDispatch: warmupBytesMatch(createMatchResponseDispatch,
      [0xff,0x90,0xb0,0x00,0x00,0x00]),
    createMatchProviderPublish7535: warmupBytesMatch(createMatchProviderPublish7535,
      [0xba,0x35,0x75,0x00,0x00,0x48,0x8b,0xce]),
    offlineSelectFactory: warmupBytesMatch(offlineSelectFactory,
      [0x4c,0x89,0x44,0x24,0x18,0x57,0x48,0x83,0xec,0x40,0x48,0xc7,
       0x44,0x24,0x20,0xfe,0xff,0xff,0xff]),
    offlineSelectConstructor: warmupBytesMatch(offlineSelectConstructor,
      [0x48,0x89,0x4c,0x24,0x08,0x56,0x57,0x41,0x56,0x48,0x83,0xec,
       0x30]),
    offlineSelectDestructor: warmupBytesMatch(offlineSelectDestructor,
      [0x40,0x57,0x48,0x83,0xec,0x30,0x48,0xc7,0x44,0x24,0x20,0xfe,
       0xff,0xff,0xff]),
    offlineSelectProviderHandler: warmupBytesMatch(
      offlineSelectProviderHandler,
      [0x40,0x57,0x48,0x83,0xec,0x30,0x48,0xc7,0x44,0x24,0x20,0xfe,
       0xff,0xff,0xff]),
    offlineSelectCreateMatchBranch: warmupBytesMatch(
      offlineSelectCreateMatchBranch,
      [0x48,0x8b,0x4b,0x18,0x48,0x8b,0x01,0xba,0x48,0x75,0x00,0x00,
       0xff,0x50,0x58]),
    createMatchControllerFactory: warmupBytesMatch(
      createMatchControllerFactory,
      [0x4c,0x89,0x44,0x24,0x18,0x56,0x57,0x41,0x56,0x48,0x83,0xec,
       0x40,0x48,0xc7,0x44,0x24,0x28,0xfe,0xff,0xff,0xff]),
    matchDataControllerFactory: warmupBytesMatch(
      matchDataControllerFactory,
      [0x4c,0x89,0x44,0x24,0x18,0x57,0x48,0x83,0xec,0x40,0x48,0xc7,
       0x44,0x24,0x20,0xfe,0xff,0xff,0xff]),
    matchDataControllerConstructor: warmupBytesMatch(
      matchDataControllerConstructor,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,
       0x48,0x8d,0xac,0x24,0x60,0xff,0xff,0xff]),
    matchDataControllerDestructor: warmupBytesMatch(
      matchDataControllerDestructor,
      [0x48,0x89,0x5c,0x24,0x08,0x57,0x48,0x83,0xec,0x20,0x8b,0xda,
       0x48,0x8b,0xf9]),
    matchDataBuilder: warmupBytesMatch(matchDataBuilder,
      [0x48,0x89,0x4c,0x24,0x08,0x55,0x53,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57]),
    matchDataPublish: warmupBytesMatch(matchDataPublish,
      [0xba,0x69,0x75,0x00,0x00,0x41,0xff,0x51,0x38]),
    matchDataProviderHandler: warmupBytesMatch(matchDataProviderHandler,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0xb0,0xfa,0xff,0xff,0x48,0x81,0xec,0x50]),
    matchDataProviderSubHandler: warmupBytesMatch(matchDataProviderSubHandler,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x40]),
    futEventDispatcher: warmupBytesMatch(futEventDispatcher,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0x50,0xe8,0xff,0xff]),
    futFeStopDecision: warmupBytesMatch(futFeStopDecision,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0x6c,0x24,0xd9,0x48,0x81,0xec,0xb0,0x00,0x00,0x00]),
    futFeStopPayloadZeroGate: warmupBytesMatch(futFeStopPayloadZeroGate,
      [0x83,0x3b,0x00,0x0f,0x85,0xb3,0x02,0x00,0x00]),
    futFeStopStateTwoGate: warmupBytesMatch(futFeStopStateTwoGate,
      [0x41,0x83,0xbd,0x00,0x0f,0x00,0x00,0x02,0x0f,0x85,0x76,0x02,0x00,0x00]),
    stateSevenRequest: warmupBytesMatch(stateSevenRequest,
      [0xba,0x07,0x00,0x00,0x00,0x49,0x8b,0xcd,0xff,0x50,0x28]),
    stateRequestMethod: warmupBytesMatch(stateRequestMethod,
      [0x48,0x89,0x5c,0x24,0x08,0x57,0x48,0x83,0xec,0x20,0x8d,0x42,0xf3,0x8b,0xfa,0x48,0x8b,0xd9,0x83,0xf8,0x01,0x77,0x16,0x80]),
    futFeToBe: warmupBytesMatch(futFeToBe,
      [0x40,0x55,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0xc0,0xec,0xff,0xff]),
    event37Handler: warmupBytesMatch(event37Handler,
      [0x40,0x57,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x50,0x48,0xc7,0x44,0x24,0x40,0xfe,0xff,0xff,0xff]),
    event37StateSevenGate: warmupBytesMatch(event37StateSevenGate,
      [0x41,0x83,0xbe,0x00,0x0f,0x00,0x00,0x07,0x75,0x38]),
    canShowPressStartPublish: warmupBytesMatch(canShowPressStartPublish,
      [0x48,0x8d,0x15,0xb2,0xc2,0x1e,0x00,0x48,0x8d,0x0d,0x6f,0xc6,0x2b,0x00,0x41,0xff,0xd0]),
    matchReadyMethod: warmupBytesMatch(matchReadyMethod,
      [0x40,0x53,0x48,0x83,0xec,0x20,0x48,0x8b,0x0d,0xf3,0x4c,0x2f,0x00,0xba,0x01,0x00,0x00,0x00]),
    matchDataRouter: warmupBytesMatch(matchDataRouter,
      [0x40,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,
       0x48,0x8b,0xec,0x48,0x81,0xec,0x80,0x00,0x00,0x00]),
    matchDataRoute7569: warmupBytesMatch(matchDataRoute7569,
      [0xba,0x69,0x75,0x00,0x00,0x45,0x33,0xc0]),
    matchDataProviderDispatch: warmupBytesMatch(matchDataProviderDispatch,
      [0x48,0x8b,0x03,0x48,0x8b,0xcb,0xff,0x50,0x20]),
    futMatchViewModelFactory: warmupBytesMatch(futMatchViewModelFactory,
      [0x4c,0x89,0x44,0x24,0x18,0x57,0x48,0x83,0xec,0x40]),
    provider7568Publish: warmupBytesMatch(provider7568Publish,
      [0xba,0x68,0x75,0x00,0x00]),
    mqTileDataProviderHandler: warmupBytesMatch(
      mqTileDataProviderHandler,
      [0x40,0x57,0x48,0x83,0xec,0x30,0x48,0xc7,0x44,0x24,0x20,0xfe,
       0xff,0xff,0xff]),
    mqTileDataConsumer: warmupBytesMatch(mqTileDataConsumer,
      [0x40,0x55,0x56,0x57,0x41,0x56,0x41,0x57,0x48,0x83,0xec,0x60]),
    mqTileDataConsumerSub: warmupBytesMatch(mqTileDataConsumerSub,
      [0x40,0x57,0x48,0x83,0xec,0x70]),
    postDestroyStateGate: warmupBytesMatch(postDestroyStateGate,
      [0x41,0x8b,0x8d,0x88,0x19,0x00,0x00]),
    postDestroyQuitRecoveryGate: warmupBytesMatch(
      postDestroyQuitRecoveryGate,
      [0x8d,0x41,0xff,0x83,0xf8,0x02,0x76,0x09,0x83,0xf9,0x07]),
    postDestroyProviderResultGate: warmupBytesMatch(
      postDestroyProviderResultGate,
      [0x83,0x7c,0x24,0x30,0x02,0x0f,0x85,0xb8,0x00,0x00,0x00]),
    postDestroyContinuation: warmupBytesMatch(postDestroyContinuation,
      [0x41,0x8b,0x86,0xb0,0x00,0x00,0x00])
  };
  // These two probes only improve lifetime bookkeeping.  Provider/event
  // hooks below also discover live controllers, so a version mismatch here
  // must not disable the complete warm-up gate.
  const optionalSignatureNames = [
    'responseCompletionDispatch',
    'createMatchPostParser',
    'createMatchResponseDispatch',
    'createMatchProviderPublish7535',
    'createMatchControllerFactory',
    'matchDataControllerFactory',
    'offlineSelectFactory',
    'offlineSelectConstructor',
    'offlineSelectDestructor',
    'offlineSelectProviderHandler',
    'offlineSelectCreateMatchBranch',
    'matchDataRouter',
    'matchDataRoute7569',
    'matchDataProviderDispatch',
    'futMatchViewModelFactory',
    'mqTileDataProviderHandler',
    'mqTileDataConsumer',
    'mqTileDataConsumerSub',
    'postDestroyStateGate',
    'postDestroyQuitRecoveryGate',
    'postDestroyProviderResultGate',
    'postDestroyContinuation'
  ];
  const failedChecks = [];
  const optionalFailedChecks = [];
  Object.keys(signatureChecks).forEach(function (name) {
    if (signatureChecks[name]) return;
    if (optionalSignatureNames.indexOf(name) !== -1) {
      optionalFailedChecks.push(name);
    } else {
      failedChecks.push(name);
    }
  });
  if (failedChecks.length > 0) {
    warmupGateTraceInstalled = true;
    emitWarmupGate('signature-mismatch', {
      module: cards.name, failedChecks: failedChecks
    });
    return true;
  }
  if (optionalFailedChecks.length > 0) {
    emitWarmupGate('optional-signature-mismatch', {
      module: cards.name,
      failedChecks: optionalFailedChecks
    });
  }
  const canTraceResponseCompletionDispatch =
    signatureChecks.responseCompletionDispatch;
  const canTraceCreateMatchPostParser =
    signatureChecks.createMatchPostParser;
  const canTraceCreateMatchResponseDispatch =
    signatureChecks.createMatchResponseDispatch;
  const canTraceCreateMatchProviderPublish7535 =
    signatureChecks.createMatchProviderPublish7535;
  const canRecoverOfflineSelect =
    signatureChecks.offlineSelectFactory &&
    signatureChecks.offlineSelectConstructor &&
    signatureChecks.offlineSelectDestructor &&
    signatureChecks.offlineSelectProviderHandler &&
    signatureChecks.offlineSelectCreateMatchBranch;

  // Transport discovery happens inside the dispatch that immediately invokes
  // this virtual parser, which is too late for the first CreateMatch. Its RVA,
  // vtable slot and prologue are verified above for the supported retail DLL,
  // so attach before the request and let the actual result byte authorize the
  // provider recovery without waiting for a later frontend event.
  installCreateMatchParserTrace(cards, createMatchParser,
                                createMatchResponseVtable);

  try {
    // +0x17c910 is `void handler(object, eventId, payload)` on Win64. The
    // persistent four-byte allocation models the natural event 0x0c payload
    // whose first dword must be zero; no pointer into FIFA-owned memory is
    // mutated by the recovery.
    const warmupStopZeroPayload = Memory.alloc(4);
    warmupStopZeroPayload.writeS32(0);
    const completeWarmupStop = new NativeFunction(futFeStopDecision, 'void',
      ['pointer','uint32','pointer'],
      {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'});
    const rebuildWarmupMatchData = new NativeFunction(matchDataBuilder, 'void',
      ['pointer'],
      {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'});
    const completeCreateMatchProvider = new NativeFunction(
      createMatchProviderHandler, 'void', ['pointer','uint32'],
      {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'});
    const completeOfflineSelectCreateMatch = canRecoverOfflineSelect ?
      new NativeFunction(offlineSelectProviderHandler, 'pointer',
        ['pointer','uint32'],
        {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'}) :
      null;
    const replayMatchDataRouter = signatureChecks.matchDataRouter ?
      new NativeFunction(matchDataRouter, 'void',
        ['pointer','int32','pointer'],
        {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'}) :
      null;
    const createRecoveredMatchDataController =
      signatureChecks.matchDataControllerFactory ?
        new NativeFunction(matchDataControllerFactory, 'pointer',
          ['pointer','pointer','pointer'],
          {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'}) :
        null;
    const createRecoveredFutMatchViewModel =
      signatureChecks.futMatchViewModelFactory ?
        new NativeFunction(futMatchViewModelFactory, 'pointer',
          ['pointer','pointer','pointer'],
          {abi: 'win64', scheduling: 'cooperative', exceptions: 'steal'}) :
        null;

    function rememberWarmupOfflineSelectController(object, source) {
      if (!canRecoverOfflineSelect || object === null || object.isNull())
        return false;
      try {
        const range = Process.findRangeByAddress(object);
        if (range === null || range.protection.indexOf('r') < 0) return false;
        const primaryVtable = object.readPointer();
        const secondaryVtable = object.add(0x128).readPointer();
        if (!primaryVtable.equals(cards.base.add(0x356980)) ||
            !secondaryVtable.equals(cards.base.add(0x356950))) return false;
        const changed = warmupOfflineSelectControllerText !==
          object.toString();
        warmupOfflineSelectControllerText = object.toString();
        if (changed) {
          emitWarmupGate('offline-select-controller-captured', {
            object: warmupOfflineSelectControllerText, source: source,
            primaryVtable: primaryVtable.toString(),
            secondaryVtable: secondaryVtable.toString(),
            mode: object.add(0x130).readS32()
          });
        }
        if (warmupCreateMatchProviderRecoveryTrigger !== null)
          warmupCreateMatchProviderRecoveryTrigger('offline-select-captured');
        return true;
      } catch (error) {
        emitWarmupGate('offline-select-controller-capture-error', {
          object: object.toString(), source: source, error: String(error)
        });
        return false;
      }
    }

    function attemptWarmupOfflineSelectRecovery(source) {
      if (!canRecoverOfflineSelect ||
          completeOfflineSelectCreateMatch === null) return false;
      const generation = warmupRecoveryGeneration;
      const ageMs = Date.now() - warmupRecoveryAcceptedAtMs;
      if (generation <= 0 ||
          warmupRecoveryAcceptedGeneration !== generation ||
          warmupRecoveryAcceptedAtMs <= 0 || ageMs < 0 ||
          ageMs > warmupRecoveryMaxAgeMs ||
          warmupOfflineSelectObservedGeneration === generation ||
          warmupOfflineSelectAttemptedGeneration === generation ||
          warmupOfflineSelectRecoveryAttemptCount >=
            warmupOfflineSelectRecoveryMaxAttempts ||
          warmupOfflineSelectRecoveryInFlight) return false;
      if (warmupOfflineSelectControllerText === null) {
        if (warmupOfflineSelectWaitingGeneration !== generation) {
          warmupOfflineSelectWaitingGeneration = generation;
          emitWarmupGate('offline-select-recovery-waiting', {
            generation: generation, acceptedAgeMs: ageMs, source: source,
            reason: 'controller-missing'
          });
        }
        return false;
      }

      const recoveryGeneration = generation;
      const controllerText = warmupOfflineSelectControllerText;
      warmupOfflineSelectAttemptedGeneration = generation;
      warmupOfflineSelectRecoveryInFlight = true;
      warmupOfflineSelectRecoveryAttemptCount++;
      const recoveryAttempt = warmupOfflineSelectRecoveryAttemptCount;
      setTimeout(function () {
        try {
          const controller = ptr(controllerText);
          const delayedAgeMs = Date.now() - warmupRecoveryAcceptedAtMs;
          const range = Process.findRangeByAddress(controller);
          const primaryVtable = range === null ? null :
            controller.readPointer();
          const secondaryVtable = range === null ? null :
            controller.add(0x128).readPointer();
          if (warmupRecoveryGeneration !== recoveryGeneration ||
              warmupRecoveryAcceptedGeneration !== recoveryGeneration ||
              warmupOfflineSelectObservedGeneration === recoveryGeneration ||
              delayedAgeMs < 0 || delayedAgeMs > warmupRecoveryMaxAgeMs ||
              range === null || range.protection.indexOf('r') < 0 ||
              primaryVtable === null || secondaryVtable === null ||
              !primaryVtable.equals(cards.base.add(0x356980)) ||
              !secondaryVtable.equals(cards.base.add(0x356950))) {
            emitWarmupGate('offline-select-recovery-skipped', {
              generation: recoveryGeneration, acceptedAgeMs: delayedAgeMs,
              source: source, object: controllerText,
              reason: 'delayed-guard-failed'
            });
            return;
          }
          emitWarmupGate('offline-select-recovery-enter', {
            generation: recoveryGeneration, acceptedAgeMs: delayedAgeMs,
            source: source, object: controllerText, providerId: 0x7564,
            attempt: recoveryAttempt
          });
          const result = completeOfflineSelectCreateMatch(
            controller, 0x7564);
          const succeeded = !result.isNull();
          if (succeeded) {
            warmupOfflineSelectObservedGeneration = recoveryGeneration;
          } else if (warmupRecoveryGeneration === recoveryGeneration &&
                     warmupRecoveryAcceptedGeneration === recoveryGeneration &&
                     recoveryAttempt < warmupOfflineSelectRecoveryMaxAttempts) {
            // Provider 0x7548 is asynchronous. A null 0x7564 result proves
            // MATCH_CREATED was not published, so allow the bounded poll to
            // schedule another attempt instead of treating entry as success.
            warmupOfflineSelectAttemptedGeneration = 0;
          }
          emitWarmupGate('offline-select-recovery-leave', {
            generation: recoveryGeneration, source: source,
            object: controllerText, providerId: 0x7564,
            result: result.toString(), succeeded: succeeded,
            attempt: recoveryAttempt,
            observed: warmupOfflineSelectObservedGeneration ===
              recoveryGeneration
          });
          if (!succeeded &&
              recoveryAttempt >= warmupOfflineSelectRecoveryMaxAttempts) {
            emitWarmupGate('offline-select-recovery-exhausted', {
              generation: recoveryGeneration, source: source,
              object: controllerText, providerId: 0x7564,
              attempts: recoveryAttempt
            });
          }
        } catch (error) {
          emitWarmupGate('offline-select-recovery-error', {
            generation: recoveryGeneration, source: source,
            object: controllerText, error: String(error)
          });
        } finally {
          warmupOfflineSelectRecoveryInFlight = false;
        }
      }, warmupOfflineSelectRecoveryDelayMs);
      return true;
    }

    warmupCreateMatchProviderRecoveryTrigger =
      attemptWarmupOfflineSelectRecovery;

    if (canRecoverOfflineSelect) {
      Interceptor.attach(offlineSelectConstructor, {
        onEnter(args) { this.offlineSelectObject = args[0]; },
        onLeave() {
          rememberWarmupOfflineSelectController(
            this.offlineSelectObject, 'constructor');
        }
      });
      Interceptor.attach(offlineSelectFactory, {
        onEnter(args) {
          this.factoryCall = captureWarmupFactoryCall(this, args, cards);
        },
        onLeave(result) {
          emitWarmupGate('offline-select-factory-call', {
            generation: warmupRecoveryGeneration,
            result: result.toString(),
            arg0: this.factoryCall.arg0,
            arg1: this.factoryCall.arg1,
            allocator: this.factoryCall.allocator,
            caller: this.factoryCall.caller,
            callerOffset: this.factoryCall.callerOffset,
            callerSymbol: this.factoryCall.callerSymbol,
            backtrace: this.factoryCall.backtrace,
            callSiteError: this.factoryCall.callSiteError
          });
          if (!result.isNull() &&
              warmupRecoveryGeneration > 0 &&
              warmupRecoveryAcceptedGeneration === warmupRecoveryGeneration &&
              this.factoryCall.arg0.address !== null &&
              this.factoryCall.arg1.address !== null &&
              this.factoryCall.allocator.address !== null &&
              this.factoryCall.arg0.vtable !== null &&
              this.factoryCall.arg1.vtable !== null &&
              this.factoryCall.allocator.vtable !== null) {
            warmupFactoryArg0Text = this.factoryCall.arg0.address;
            warmupFactoryArg1Text = this.factoryCall.arg1.address;
            warmupFactoryAllocatorText = this.factoryCall.allocator.address;
            warmupFactoryArg0VtableText = this.factoryCall.arg0.vtable;
            warmupFactoryArg1VtableText = this.factoryCall.arg1.vtable;
            warmupFactoryAllocatorVtableText =
              this.factoryCall.allocator.vtable;
            warmupFactoryContextGeneration = warmupRecoveryGeneration;
            emitWarmupGate('match-bootstrap-factory-context-captured', {
              generation: warmupRecoveryGeneration,
              arg0: this.factoryCall.arg0,
              arg1: this.factoryCall.arg1,
              allocator: this.factoryCall.allocator,
              source: 'offline-select-factory'
            });
          }
          if (!result.isNull())
            rememberWarmupOfflineSelectController(result, 'factory');
        }
      });
      Interceptor.attach(offlineSelectDestructor, {
        onEnter(args) {
          if (warmupOfflineSelectControllerText === args[0].toString()) {
            emitWarmupGate('offline-select-controller-released', {
              object: warmupOfflineSelectControllerText
            });
            warmupOfflineSelectControllerText = null;
          }
        }
      });
      Interceptor.attach(offlineSelectProviderHandler, {
        onEnter(args) {
          const providerId = args[1].toInt32() & 0xffff;
          this.offlineSelectProviderId = null;
          if (providerId !== 0x755f && providerId !== 0x7564) return;
          const captured = rememberWarmupOfflineSelectController(
            args[0], 'provider-' + providerId.toString(16));
          if (!captured) return;
          this.offlineSelectProviderId = providerId;
          this.offlineSelectProviderObject = args[0].toString();
          this.offlineSelectProviderGeneration = warmupRecoveryGeneration;
          emitWarmupGate('offline-select-provider', {
            object: args[0].toString(), providerId: providerId,
            generation: warmupRecoveryGeneration,
            caller: this.returnAddress.toString()
          });
        },
        onLeave(result) {
          if (this.offlineSelectProviderId === null) return;
          const generation = this.offlineSelectProviderGeneration;
          const succeeded = !result.isNull();
          if (this.offlineSelectProviderId === 0x7564 && succeeded &&
              generation > 0 &&
              warmupRecoveryGeneration === generation &&
              warmupRecoveryAcceptedGeneration === generation) {
            warmupOfflineSelectObservedGeneration = generation;
          }
          emitWarmupGate('offline-select-provider-return', {
            object: this.offlineSelectProviderObject,
            providerId: this.offlineSelectProviderId,
            generation: generation, result: result.toString(),
            succeeded: succeeded,
            observed: warmupOfflineSelectObservedGeneration === generation
          });
        }
      });
      Interceptor.attach(offlineSelectCreateMatchBranch, {
        onEnter() {
          emitWarmupGate('offline-select-match-created-branch', {
            object: this.context.rbx.toString(), providerId: 0x7564,
            generation: warmupRecoveryGeneration,
            caller: this.returnAddress.toString()
          });
        }
      });
      emitWarmupGate('offline-select-hook-ready', {
        factoryOffset: '0x49f90', constructorOffset: '0x45f00',
        providerHandlerOffset: '0x46e50',
        createMatchBranchOffset: '0x4700b'
      });
    }

    if (signatureChecks.matchDataRouter) {
      Interceptor.attach(matchDataRouter, {
        onEnter(args) {
          if (warmupRecoveryAcceptedGeneration !== warmupRecoveryGeneration)
            return;
          let routerVtable = null;
          let routerValidated = false;
          try {
            const routerRange = Process.findRangeByAddress(args[0]);
            routerVtable = args[0].readPointer();
            const vtableModule = Process.findModuleByAddress(routerVtable);
            const firstMethod = routerVtable.readPointer();
            const firstMethodRange = Process.findRangeByAddress(firstMethod);
            routerValidated = routerRange !== null &&
              routerRange.protection.indexOf('r') >= 0 &&
              vtableModule !== null && vtableModule.base.equals(cards.base) &&
              firstMethodRange !== null &&
              firstMethodRange.protection.indexOf('x') >= 0;
            if (routerValidated && !warmupMatchDataRouterRecoveryInFlight) {
              const routerText = args[0].toString();
              const vtableText = routerVtable.toString();
              if (warmupMatchDataRouterText === routerText &&
                  warmupMatchDataRouterVtableText === vtableText) {
                warmupMatchDataRouterStableObservations = Math.min(
                  1000, warmupMatchDataRouterStableObservations + 1);
              } else {
                warmupMatchDataRouterText = routerText;
                warmupMatchDataRouterVtableText = vtableText;
                warmupMatchDataRouterStableObservations = 1;
              }
            }
          } catch (_) { routerValidated = false; }
          emitWarmupGate('match-data-router-event', {
            generation: warmupRecoveryGeneration,
            router: args[0].toString(), sourceEventId: args[1].toInt32(),
            payload: args[2].toString(), caller: this.returnAddress.toString(),
            routerVtable: routerVtable === null ? null : routerVtable.toString(),
            routerValidated: routerValidated,
            stableObservations: warmupMatchDataRouterStableObservations,
            recovery: warmupMatchDataRouterRecoveryInFlight
          });
        }
      });
    }
    if (signatureChecks.matchDataRoute7569) {
      Interceptor.attach(matchDataRoute7569, {
        onEnter() {
          warmupMatchDataRouteObservedGeneration = warmupRecoveryGeneration;
          emitWarmupGate('match-data-route-7569', {
            generation: warmupRecoveryGeneration,
            sourceEventId: this.context.rdi.toInt32(),
            router: this.context.r15.toString(),
            provider: this.context.rbx.toString(), payload: '0x0',
            payloadReady: false
          });
        }
      });
    }
    if (signatureChecks.matchDataProviderDispatch) {
      Interceptor.attach(matchDataProviderDispatch, {
        onEnter() {
          const providerId = this.context.rdx.toInt32() & 0xffff;
          if (providerId !== 0x7569) return;
          const payload = this.context.r8;
          if (!payload.isNull())
            warmupMatchDataPublishedGeneration = warmupRecoveryGeneration;
          emitWarmupGate('match-data-provider-dispatch', {
            generation: warmupRecoveryGeneration, providerId: providerId,
            provider: this.context.rbx.toString(),
            payload: payload.toString(), payloadReady: !payload.isNull()
          });
        }
      });
    }
    if (signatureChecks.futMatchViewModelFactory) {
      Interceptor.attach(futMatchViewModelFactory, {
        onEnter(args) {
          this.factoryCall = captureWarmupFactoryCall(this, args, cards);
        },
        onLeave(result) {
          if (!result.isNull())
            warmupFutMatchViewModelGeneration = warmupRecoveryGeneration;
          emitWarmupGate('fut-match-view-model-created', {
            generation: warmupRecoveryGeneration,
            object: result.toString(), success: !result.isNull(),
            arg0: this.factoryCall.arg0,
            arg1: this.factoryCall.arg1,
            allocator: this.factoryCall.allocator,
            caller: this.factoryCall.caller,
            callerOffset: this.factoryCall.callerOffset,
            callerSymbol: this.factoryCall.callerSymbol,
            backtrace: this.factoryCall.backtrace,
            callSiteError: this.factoryCall.callSiteError
          });
        }
      });
    }
    if (signatureChecks.provider7568Publish) {
      Interceptor.attach(provider7568Publish, {
        onEnter() {
          warmupKitLoadCompleteGeneration = warmupRecoveryGeneration;
          emitWarmupGate('kit-load-complete-publish', {
            generation: warmupRecoveryGeneration, providerId: 0x7568,
            provider: this.context.rcx.toString(),
            payload: this.context.r8.toString(),
            payloadReady: !this.context.r8.isNull()
          });
        }
      });
    }
    if (signatureChecks.mqTileDataProviderHandler) {
      Interceptor.attach(mqTileDataProviderHandler, {
        onEnter(args) {
          const providerId = args[1].toInt32() & 0xffff;
          if (providerId !== 0x756a) return;
          warmupMqTileDataGeneration = warmupRecoveryGeneration;
          emitWarmupGate('mq-tile-data-handler', {
            generation: warmupRecoveryGeneration,
            object: args[0].toString(), providerId: providerId,
            caller: this.returnAddress.toString()
          });
        }
      });
    }
    if (signatureChecks.mqTileDataConsumer) {
      Interceptor.attach(mqTileDataConsumer, {
        onEnter(args) {
          const providerId = args[1].toInt32() & 0xffff;
          if (providerId !== 0x756a) return;
          emitWarmupGate('mq-tile-data-consumer', {
            generation: warmupRecoveryGeneration,
            object: args[0].toString(), providerId: providerId
          });
        }
      });
    }
    if (signatureChecks.mqTileDataConsumerSub) {
      Interceptor.attach(mqTileDataConsumerSub, {
        onEnter(args) {
          const providerId = args[1].toInt32() & 0xffff;
          if (providerId !== 0x756a) return;
          emitWarmupGate('mq-tile-data-consumer-sub', {
            generation: warmupRecoveryGeneration,
            object: args[0].toString(), providerId: providerId,
            argument: args[2].toInt32()
          });
        }
      });
    }

    Interceptor.attach(matchDataControllerConstructor, {
      onEnter(args) {
        this.matchDataController = args[0];
      },
      onLeave() {
        try {
          rememberWarmupMatchDataController(
            this.matchDataController, 'verified-constructor');
          emitWarmupGate('match-data-controller-constructed', {
            object: this.matchDataController.toString(),
            primaryVtable: describeWarmupCode(
              this.matchDataController.readPointer()),
            secondaryVtable: describeWarmupCode(
              this.matchDataController.add(0x128).readPointer())
          });
        } catch (error) {
          emitWarmupGate('match-data-controller-capture-error', {
            object: this.matchDataController.toString(),
            source: 'verified-constructor', error: String(error)
          });
        }
      }
    });
    Interceptor.attach(matchDataControllerDestructor, {
      onEnter(args) {
        const key = args[0].toString();
        if (warmupMatchDataCandidates[key] !== undefined) {
          delete warmupMatchDataCandidates[key];
          emitWarmupGate('match-data-controller-released', {
            object: key, source: 'verified-destructor'
          });
        }
      }
    });
    if (signatureChecks.createMatchControllerFactory) {
      Interceptor.attach(createMatchControllerFactory, {
        onLeave(result) {
          if (result.isNull()) return;
          const secondary = result.add(0x128);
          const captured = rememberWarmupCreateMatchController(
            secondary, 'verified-factory');
          emitWarmupGate('create-match-controller-factory-return', {
            object: result.toString(), secondary: secondary.toString(),
            captured: captured
          });
        }
      });
    }
    if (signatureChecks.matchDataControllerFactory) {
      Interceptor.attach(matchDataControllerFactory, {
        onEnter(args) {
          this.factoryCall = captureWarmupFactoryCall(this, args, cards);
        },
        onLeave(result) {
          emitWarmupGate('match-data-controller-factory-return', {
            generation: warmupRecoveryGeneration,
            object: result.toString(), success: !result.isNull(),
            arg0: this.factoryCall.arg0,
            arg1: this.factoryCall.arg1,
            allocator: this.factoryCall.allocator,
            caller: this.factoryCall.caller,
            callerOffset: this.factoryCall.callerOffset,
            callerSymbol: this.factoryCall.callerSymbol,
            backtrace: this.factoryCall.backtrace,
            callSiteError: this.factoryCall.callSiteError
          });
          if (!result.isNull())
            rememberWarmupMatchDataController(result, 'verified-factory');
        }
      });
    }

    function captureWarmupFactoryCall(invocation, args, cardsModule) {
      const details = {
        // The retail factories all preserve RCX/RDX and use R8 as the
        // allocator interface.  Keep the first two roles neutral until a
        // successful native call proves which is parent and which is context.
        arg0: describeWarmupPointer(args[0], 0x40),
        arg1: describeWarmupPointer(args[1], 0x40),
        allocator: describeWarmupPointer(args[2], 0x40)
      };
      addWarmupCallSite(details, invocation, cardsModule);
      return details;
    }

    function describeWarmupPointer(address, byteLength) {
      const description = {
        address: address === null ? null : address.toString(),
        vtable: null,
        slots: []
      };
      if (address === null || address.isNull()) return description;
      try {
        const range = Process.findRangeByAddress(address);
        if (range === null || range.protection.indexOf('r') < 0) {
          description.error = range === null ? 'unmapped' : range.protection;
          return description;
        }
        description.vtable = address.readPointer().toString();
        description.slots = readPointerSlots(address,
          byteLength === undefined ? 0x40 : byteLength);
      } catch (error) {
        description.error = String(error);
      }
      return description;
    }

    function validateWarmupFactoryObject(addressText, vtableText, label) {
      const result = { address: addressText, vtable: vtableText,
        valid: false, error: null };
      if (addressText === null || vtableText === null) {
        result.error = label + ':missing';
        return result;
      }
      try {
        const address = ptr(addressText);
        const range = Process.findRangeByAddress(address);
        if (range === null || range.protection.indexOf('r') < 0) {
          result.error = label + ':unreadable';
          return result;
        }
        const currentVtable = address.readPointer();
        if (currentVtable.toString() !== vtableText) {
          result.error = label + ':vtable-changed';
          result.currentVtable = currentVtable.toString();
          return result;
        }
        const vtableRange = Process.findRangeByAddress(currentVtable);
        const firstMethod = currentVtable.readPointer();
        const methodRange = Process.findRangeByAddress(firstMethod);
        if (vtableRange === null || vtableRange.protection.indexOf('r') < 0 ||
            methodRange === null || methodRange.protection.indexOf('x') < 0) {
          result.error = label + ':interface-invalid';
          return result;
        }
        result.valid = true;
        result.firstMethod = firstMethod.toString();
      } catch (error) {
        result.error = label + ':' + String(error);
      }
      return result;
    }

    function attemptWarmupMatchBootstrapFactoryRecovery(object, source) {
      const generation = warmupRecoveryGeneration;
      if (createRecoveredMatchDataController === null ||
          createRecoveredFutMatchViewModel === null || generation <= 0 ||
          warmupRecoveryAcceptedGeneration !== generation ||
          warmupFactoryContextGeneration !== generation ||
          warmupMatchBootstrapFactoryAttemptedGeneration === generation ||
          warmupMatchBootstrapFactoryInFlight ||
          (warmupFutMatchViewModelGeneration === generation &&
           selectWarmupMatchDataController(cards).controller !== null))
        return false;
      const mode = readWarmupModeState(object);
      if (mode.state !== 2 || mode.gameModeId !== 17 ||
          mode.readErrors.length !== 0) return false;
      const arg0Check = validateWarmupFactoryObject(
        warmupFactoryArg0Text, warmupFactoryArg0VtableText, 'arg0');
      const arg1Check = validateWarmupFactoryObject(
        warmupFactoryArg1Text, warmupFactoryArg1VtableText, 'arg1');
      const allocatorCheck = validateWarmupFactoryObject(
        warmupFactoryAllocatorText, warmupFactoryAllocatorVtableText,
        'allocator');
      if (!arg0Check.valid || !arg1Check.valid || !allocatorCheck.valid) {
        warmupMatchBootstrapFactoryAttemptedGeneration = generation;
        emitWarmupGate('match-bootstrap-factory-recovery-skipped', {
          generation: generation, source: source, object: object.toString(),
          arg0: arg0Check, arg1: arg1Check, allocator: allocatorCheck,
          reason: 'factory-context-lifetime-guard-failed'
        });
        return false;
      }

      warmupMatchBootstrapFactoryAttemptedGeneration = generation;
      warmupMatchBootstrapFactoryInFlight = true;
      const modeObjectText = object.toString();
      emitWarmupGate('match-bootstrap-factory-recovery-scheduled', {
        generation: generation, source: source, object: modeObjectText,
        arg0: arg0Check, arg1: arg1Check, allocator: allocatorCheck,
        delayMs: warmupRecoveryDelayMs
      });
      setTimeout(function () {
        try {
          const delayedMode = readWarmupModeState(ptr(modeObjectText));
          const delayedArg0 = validateWarmupFactoryObject(
            warmupFactoryArg0Text, warmupFactoryArg0VtableText, 'arg0');
          const delayedArg1 = validateWarmupFactoryObject(
            warmupFactoryArg1Text, warmupFactoryArg1VtableText, 'arg1');
          const delayedAllocator = validateWarmupFactoryObject(
            warmupFactoryAllocatorText,
            warmupFactoryAllocatorVtableText, 'allocator');
          if (warmupRecoveryGeneration !== generation ||
              warmupRecoveryAcceptedGeneration !== generation ||
              warmupFactoryContextGeneration !== generation ||
              delayedMode.state !== 2 || delayedMode.gameModeId !== 17 ||
              delayedMode.readErrors.length !== 0 ||
              !delayedArg0.valid || !delayedArg1.valid ||
              !delayedAllocator.valid) {
            emitWarmupGate('match-bootstrap-factory-recovery-skipped', {
              generation: generation, source: source,
              object: modeObjectText, arg0: delayedArg0,
              arg1: delayedArg1, allocator: delayedAllocator,
              reason: 'delayed-factory-guard-failed'
            });
            return;
          }
          emitWarmupGate('match-bootstrap-factory-recovery-enter', {
            generation: generation, source: source, object: modeObjectText
          });
          const arg0 = ptr(warmupFactoryArg0Text);
          const arg1 = ptr(warmupFactoryArg1Text);
          const allocator = ptr(warmupFactoryAllocatorText);
          const controller = createRecoveredMatchDataController(
            arg0, arg1, allocator);
          if (!controller.isNull()) {
            warmupRecoveredMatchDataControllerText = controller.toString();
            rememberWarmupMatchDataController(
              controller, 'factory-recovery');
          }
          const viewModel = createRecoveredFutMatchViewModel(
            arg0, arg1, allocator);
          if (!viewModel.isNull()) {
            warmupRecoveredFutMatchViewModelText = viewModel.toString();
            warmupFutMatchViewModelGeneration = generation;
          }
          emitWarmupGate('match-bootstrap-factory-recovery-leave', {
            generation: generation, source: source,
            controller: controller.toString(),
            object: viewModel.toString(),
            success: !controller.isNull() && !viewModel.isNull()
          });
        } catch (error) {
          emitWarmupGate('match-bootstrap-factory-recovery-error', {
            generation: generation, source: source,
            object: modeObjectText, error: String(error)
          });
        } finally {
          warmupMatchBootstrapFactoryInFlight = false;
        }
      }, warmupRecoveryDelayMs);
      return true;
    }

    function describeWarmupCodePointer(address) {
      const description = {
        address: address === null ? null : address.toString(),
        module: null, moduleOffset: null, symbol: null
      };
      if (address === null || address.isNull()) return description;
      try {
        const module = Process.findModuleByAddress(address);
        if (module !== null) {
          description.module = module.name;
          description.moduleOffset = address.sub(module.base).toString();
        }
        description.symbol = String(DebugSymbol.fromAddress(address));
      } catch (error) {
        description.error = String(error);
      }
      return description;
    }

    function readWarmupStackPointer(frame, offset) {
      try {
        return frame.sub(offset).readPointer();
      } catch (_) {
        return ptr(0);
      }
    }

    function createMatchRoutingResponse(invocation) {
      const response = readWarmupStackPointer(invocation.context.rbp, 0x39);
      if (response.isNull()) return null;
      try {
        const range = Process.findRangeByAddress(response);
        if (range === null || range.protection.indexOf('r') < 0 ||
            !response.readPointer().equals(createMatchResponseVtable))
          return null;
        return response;
      } catch (_) {
        return null;
      }
    }

    function emitCreateMatchRouting(stage, invocation, resultByte, response) {
      const context = invocation.context;
      const eventLocal = context.rbp.sub(0x19);
      const rbx = describeWarmupPointer(context.rbx);
      const rdi = describeWarmupPointer(context.rdi);
      const rsi = describeWarmupPointer(context.rsi);
      emitWarmupGate(stage, {
        resultByte: resultByte,
        response: response.toString(),
        responseSlots: readPointerSlots(response, 0x80),
        eventLocal: eventLocal.toString(),
        eventSlots: readPointerSlots(eventLocal, 0x30),
        rbx: rbx.address, rbxVtable: rbx.vtable, rbxSlots: rbx.slots,
        rdi: rdi.address, rdiVtable: rdi.vtable, rdiSlots: rdi.slots,
        rsi: rsi.address, rsiVtable: rsi.vtable, rsiSlots: rsi.slots,
        caller: invocation.returnAddress.toString()
      });
    }

    function attachWarmupRoutingProbe(address, callbacks, name) {
      try {
        Interceptor.attach(address, callbacks);
        return true;
      } catch (error) {
        // Routing probes are observational. A Frida relocation limitation at
        // one instruction must never abort the verified warm-up recovery hooks
        // installed below.
        emitWarmupGate('routing-probe-install-error', {
          source: name, object: address.toString(), error: String(error)
        });
        return false;
      }
    }

    function describePostDestroyContext(invocation, includePreProviderStatus) {
      const r13 = invocation.context.r13;
      const r14 = invocation.context.r14;
      const flowState = readWarmupS32(r13, 0x1988,
        'postDestroyFlowState');
      const details = {
        generation: warmupRecoveryGeneration,
        r13: r13.toString(),
        r14: r14.toString(),
        flowState: flowState.value,
        flowStateError: flowState.error,
        r13Slots: readPointerSlots(r13, 0x40),
        r14Slots: readPointerSlots(r14, 0x40),
        destroyMatch: readDecodedDestroyMatchState(r14),
        lastDestroyMatch: lastDecodedDestroyMatchText,
        destroyMatchMatchesLast: lastDecodedDestroyMatchText !== null &&
          r14.toString() === lastDecodedDestroyMatchText,
        warmupMode: warmupModeObjectText === null ? null :
          readWarmupModeState(ptr(warmupModeObjectText))
      };
      if (includePreProviderStatus) {
        try {
          details.preProviderStatus =
            invocation.context.rsp.add(0x30).readS32();
        } catch (error) {
          details.preProviderStatusError = String(error);
        }
      }
      addWarmupCallSite(details, invocation, cards);
      return details;
    }

    if (signatureChecks.postDestroyStateGate) {
      attachWarmupRoutingProbe(postDestroyStateGate, {
        onEnter() {
          emitWarmupGate('post-destroy-state-gate',
            describePostDestroyContext(this, false));
        }
      }, 'post-destroy-state-gate');
    }
    if (signatureChecks.postDestroyQuitRecoveryGate) {
      attachWarmupRoutingProbe(postDestroyQuitRecoveryGate, {
        onEnter() {
          const now = Date.now();
          const r13 = this.context.r13;
          const r14 = this.context.r14;
          const flowState = readWarmupS32(
            r13, 0x1988, 'postDestroyQuitFlowState');
          const responseReason = readWarmupS32(
            r14, 0xb4, 'postDestroyQuitResponseReason');
          const mode = warmupModeObjectText === null ? null :
            readWarmupModeState(ptr(warmupModeObjectText));
          const decodedAgeMs = lastDecodedDestroyMatchAtMs <= 0 ? -1 :
            now - lastDecodedDestroyMatchAtMs;
          const authorized = warmupRecoveryGeneration > 0 &&
            warmupRecoveryAcceptedGeneration === warmupRecoveryGeneration &&
            mode !== null && mode.gameModeId === 17 &&
            mode.readErrors.length === 0 && flowState.error === null &&
            responseReason.error === null && flowState.value === 5 &&
            responseReason.value === 5 &&
            this.context.rcx.toInt32() === 5 &&
            lastDecodedDestroyMatchText !== null &&
            r14.toString() === lastDecodedDestroyMatchText &&
            decodedAgeMs >= 0 && decodedAgeMs <= 10000;
          if (!authorized) {
            emitWarmupGate('post-destroy-quit-recovery-skipped', {
              generation: warmupRecoveryGeneration,
              flowState: flowState.value,
              responseReason: responseReason.value,
              destroyMatch: r14.toString(),
              lastDestroyMatch: lastDecodedDestroyMatchText,
              decodedAgeMs: decodedAgeMs,
              warmupMode: mode,
              reason: 'strict-guard-failed'
            });
            return;
          }
          // +0xd7c4f computes (RCX - 1). Values outside 1..3 and 7 jump to
          // +0xd7e6e, so QUIT=5 already selects the native common teardown.
          // Observation only: the earlier LOSS substitution was disproved by
          // the 2026-08-28 live trace and must not alter this register.
          emitWarmupGate('post-destroy-quit-native-path', {
            generation: warmupRecoveryGeneration,
            flowState: flowState.value,
            responseReason: responseReason.value,
            destroyMatch: r14.toString(), decodedAgeMs: decodedAgeMs,
            nativeTarget: 'post-destroy-continuation', warmupMode: mode
          });
        }
      }, 'post-destroy-quit-recovery');
    }
    if (signatureChecks.postDestroyProviderResultGate) {
      attachWarmupRoutingProbe(postDestroyProviderResultGate, {
        onEnter() {
          emitWarmupGate('post-destroy-pre-provider-status-gate',
            describePostDestroyContext(this, true));
        }
      }, 'post-destroy-pre-provider-status-gate');
    }
    if (signatureChecks.postDestroyContinuation) {
      attachWarmupRoutingProbe(postDestroyContinuation, {
        onEnter() {
          emitWarmupGate('post-destroy-continuation',
            describePostDestroyContext(this, false));
        }
      }, 'post-destroy-continuation');
    }

    if (canTraceCreateMatchPostParser) {
      attachWarmupRoutingProbe(createMatchPostParser, {
        onEnter() {
          const response = createMatchRoutingResponse(this);
          if (response === null) return;
          emitCreateMatchRouting('create-match-post-parser', this,
            this.context.rax.toInt32() & 0xff, response);
        }
      }, 'post-parser');
    }
    if (canTraceCreateMatchResponseDispatch) {
      attachWarmupRoutingProbe(createMatchResponseDispatch, {
        onEnter() {
          const response = createMatchRoutingResponse(this);
          if (response === null) return;
          emitCreateMatchRouting('create-match-response-dispatch', this,
            null, response);
        }
      }, 'response-dispatch');
    }
    if (canTraceCreateMatchProviderPublish7535) {
      attachWarmupRoutingProbe(createMatchProviderPublish7535, {
        onEnter() {
          const response = createMatchRoutingResponse(this);
          if (response === null) return;
          const provider = describeWarmupPointer(this.context.rsi);
          let callback = null;
          try {
            callback = this.context.rax.add(0x20).readPointer().toString();
          } catch (_) {}
          emitWarmupGate('create-match-provider-7535', {
            // Hooked at the preceding `mov edx, 0x7535`, so RDX has not been
            // assigned yet; the verified instruction immediate is definitive.
            providerId: 0x7535,
            object: provider.address,
            providerVtable: provider.vtable,
            providerSlots: provider.slots,
            providerCallback: callback,
            data: this.context.r8.toString(),
            caller: this.returnAddress.toString()
          });
        }
      }, 'provider-7535');
    }
    if (canTraceResponseCompletionDispatch) {
      attachWarmupRoutingProbe(responseCompletionDispatch, {
        onEnter(args) {
          const request = args[0];
          const response = args[1];
          try {
            if (request.isNull() || response.isNull() ||
                !request.readPointer().equals(responseRequestVtable) ||
                !response.readPointer().equals(createMatchResponseVtable)) return;
            const callback90 = request.add(0x90).readPointer();
            const callbackA0 = request.add(0xa0).readPointer();
            emitWarmupGate('create-match-completion-dispatch', {
              request: request.toString(),
              requestSlots: readPointerSlots(request, 0xc0),
              response: response.toString(),
              responseSlots: readPointerSlots(response, 0x30),
              field90: callback90.toString(),
              field98: request.add(0x98).readPointer().toString(),
              fieldA0: callbackA0.toString(),
              fieldA8: request.add(0xa8).readPointer().toString(),
              fieldB0: request.add(0xb0).readPointer().toString(),
              fieldB8: request.add(0xb8).readPointer().toString(),
              callback90: describeWarmupCodePointer(callback90),
              callbackA0: describeWarmupCodePointer(callbackA0),
              caller: this.returnAddress.toString()
            });
          } catch (error) {
            emitWarmupGate('create-match-completion-dispatch-error', {
              request: request.toString(), response: response.toString(),
              error: String(error)
            });
          }
        }
      }, 'completion-dispatch');
    }

    function rememberWarmupCreateMatchController(object, source) {
      if (object === null || object.isNull()) return false;
      try {
        const range = Process.findRangeByAddress(object);
        if (range === null || range.protection.indexOf('r') < 0) return false;
        const vtable = object.readPointer();
        if (!vtable.equals(cards.base.add(0x3576d0))) return false;
        const changed = warmupCreateMatchControllerText !== object.toString();
        warmupCreateMatchControllerText = object.toString();
        if (changed) {
          emitWarmupGate('create-match-controller-captured', {
            object: warmupCreateMatchControllerText, source: source,
            vtable: vtable.toString()
          });
        }
        if (warmupCreateMatchProviderRecoveryTrigger !== null)
          warmupCreateMatchProviderRecoveryTrigger('controller-captured');
        return true;
      } catch (error) {
        emitWarmupGate('create-match-controller-capture-error', {
          object: object.toString(), source: source, error: String(error)
        });
        return false;
      }
    }

    function attemptWarmupCreateMatchProviderRecovery(source) {
      const generation = warmupRecoveryGeneration;
      const ageMs = Date.now() - warmupRecoveryAcceptedAtMs;
      if (generation <= 0 ||
          warmupRecoveryAcceptedGeneration !== generation ||
          warmupRecoveryAcceptedAtMs <= 0 || ageMs < 0 ||
          ageMs > warmupRecoveryMaxAgeMs ||
          warmupCreateMatchProviderObservedGeneration === generation ||
          warmupCreateMatchProviderAttemptedGeneration === generation ||
          warmupCreateMatchProviderRecoveryInFlight) return false;
      if (warmupCreateMatchControllerText === null) {
        if (warmupCreateMatchProviderWaitingGeneration !== generation) {
          warmupCreateMatchProviderWaitingGeneration = generation;
          emitWarmupGate('create-match-provider-recovery-waiting', {
            generation: generation, acceptedAgeMs: ageMs, source: source,
            reason: 'controller-missing'
          });
        }
        return false;
      }

      const recoveryGeneration = generation;
      const controllerText = warmupCreateMatchControllerText;
      warmupCreateMatchProviderAttemptedGeneration = generation;
      warmupCreateMatchProviderRecoveryInFlight = true;
      setTimeout(function () {
        try {
          const controller = ptr(controllerText);
          const delayedAgeMs = Date.now() - warmupRecoveryAcceptedAtMs;
          const range = Process.findRangeByAddress(controller);
          const vtable = range === null ? null : controller.readPointer();
          if (warmupRecoveryGeneration !== recoveryGeneration ||
              warmupRecoveryAcceptedGeneration !== recoveryGeneration ||
              warmupCreateMatchProviderObservedGeneration === recoveryGeneration ||
              delayedAgeMs < 0 || delayedAgeMs > warmupRecoveryMaxAgeMs ||
              range === null || range.protection.indexOf('r') < 0 ||
              vtable === null || !vtable.equals(cards.base.add(0x3576d0))) {
            emitWarmupGate('create-match-provider-recovery-skipped', {
              generation: recoveryGeneration, acceptedAgeMs: delayedAgeMs,
              source: source, object: controllerText,
              reason: 'delayed-guard-failed'
            });
            return;
          }
          emitWarmupGate('create-match-provider-recovery-enter', {
            generation: recoveryGeneration, acceptedAgeMs: delayedAgeMs,
            source: source, object: controllerText, providerId: 0x7564
          });
          completeCreateMatchProvider(controller, 0x7564);
          emitWarmupGate('create-match-provider-recovery-leave', {
            generation: recoveryGeneration, source: source,
            object: controllerText, providerId: 0x7564,
            observed: warmupCreateMatchProviderObservedGeneration ===
              recoveryGeneration
          });
        } catch (error) {
          emitWarmupGate('create-match-provider-recovery-error', {
            generation: recoveryGeneration, source: source,
            object: controllerText, error: String(error)
          });
        } finally {
          warmupCreateMatchProviderRecoveryInFlight = false;
        }
      }, warmupRecoveryDelayMs);
      return true;
    }

    // The +0x53320 handler above belongs to futwatchlistviewmodel. Keep its
    // trace for comparison, but never replay CreateMatch through that unrelated
    // screen. The active trigger remains the offline-select handler installed
    // above.

    function attemptWarmupMatchDataRecovery(source) {
      const generation = warmupRecoveryGeneration;
      const ageMs = Date.now() - warmupRecoveryAcceptedAtMs;
      if (generation <= 0 ||
          warmupRecoveryAcceptedGeneration !== generation ||
          warmupRecoveryAcceptedAtMs <= 0 || ageMs < 0 ||
          ageMs > warmupRecoveryMaxAgeMs ||
          warmupMatchDataPublishedGeneration === generation ||
          warmupMatchDataBuildAttemptedGeneration === generation ||
          warmupMatchDataBuildInFlight) return false;
      const selection = selectWarmupMatchDataController(cards);
      if (selection.controller === null) {
        // Never scan every writable process range from this timer. The retail
        // 09:54 trace stopped immediately after candidate acceptance because
        // that synchronous scan monopolised the injected runtime and froze the
        // kit-selection screen. Constructor/provider hooks are already active
        // before CreateMatch and are the authoritative controller source.
        if (warmupMatchDataWaitingGeneration !== generation) {
          warmupMatchDataWaitingGeneration = generation;
          emitWarmupGate('match-data-recovery-waiting', {
            generation: generation, source: source,
            reason: selection.reason, valid: selection.valid,
            rejected: selection.rejected
          });
        }
        return false;
      }

      warmupMatchDataBuildAttemptedGeneration = generation;
      warmupMatchDataBuildInFlight = true;
      emitWarmupGate('match-data-recovery-enter', {
        generation: generation, acceptedAgeMs: ageMs, source: source,
        object: selection.controller.toString(), valid: selection.valid
      });
      try {
        rebuildWarmupMatchData(selection.controller);
        emitWarmupGate('match-data-recovery-leave', {
          generation: generation, source: source,
          object: selection.controller.toString(),
          published: warmupMatchDataPublishedGeneration === generation
        });
      } catch (error) {
        emitWarmupGate('match-data-recovery-error', {
          generation: generation, source: source,
          object: selection.controller.toString(), error: String(error)
        });
      } finally {
        warmupMatchDataBuildInFlight = false;
      }
      return true;
    }

    function attemptWarmupMatchDataRouterRecovery(object, source) {
      const generation = warmupRecoveryGeneration;
      const now = Date.now();
      const acceptedAgeMs = now - warmupRecoveryAcceptedAtMs;
      const stateTwoAgeMs = warmupStateTwoObservedAtMs <= 0 ? -1 :
        now - warmupStateTwoObservedAtMs;
      if (replayMatchDataRouter === null || generation <= 0 ||
          warmupRecoveryAcceptedGeneration !== generation ||
          warmupRecoveryAcceptedAtMs <= 0 || acceptedAgeMs < 0 ||
          acceptedAgeMs > warmupRecoveryMaxAgeMs ||
          warmupMatchDataPublishedGeneration === generation ||
          warmupMatchDataRouterAttemptedGeneration === generation ||
          warmupMatchDataRouterRecoveryInFlight || stateTwoAgeMs < 0 ||
          stateTwoAgeMs < warmupMatchDataRouterRecoveryDelayMs) return false;
      const mode = readWarmupModeState(object);
      if (mode.state !== 2 || mode.gameModeId !== 17 ||
          mode.readErrors.length !== 0) return false;
      if (warmupMatchDataRouterText === null ||
          warmupMatchDataRouterVtableText === null ||
          warmupMatchDataRouterStableObservations < 2) {
        if (warmupMatchDataRouterWaitingGeneration !== generation) {
          warmupMatchDataRouterWaitingGeneration = generation;
          emitWarmupGate('match-data-router-recovery-waiting', {
            generation: generation, source: source,
            stateTwoAgeMs: stateTwoAgeMs,
            reason: warmupMatchDataRouterText === null ?
              'router-missing' : 'router-not-stable',
            stableObservations: warmupMatchDataRouterStableObservations
          });
        }
        return false;
      }

      const router = ptr(warmupMatchDataRouterText);
      try {
        const routerRange = Process.findRangeByAddress(router);
        const routerVtable = routerRange === null ? null :
          router.readPointer();
        const vtableModule = routerVtable === null ? null :
          Process.findModuleByAddress(routerVtable);
        const firstMethod = routerVtable === null ? null :
          routerVtable.readPointer();
        const firstMethodRange = firstMethod === null ? null :
          Process.findRangeByAddress(firstMethod);
        if (routerRange === null ||
            routerRange.protection.indexOf('r') < 0 ||
            routerVtable === null ||
            routerVtable.toString() !== warmupMatchDataRouterVtableText ||
            vtableModule === null || !vtableModule.base.equals(cards.base) ||
            firstMethodRange === null ||
            firstMethodRange.protection.indexOf('x') < 0) {
          emitWarmupGate('match-data-router-recovery-skipped', {
            generation: generation, source: source,
            router: warmupMatchDataRouterText,
            reason: 'router-lifetime-guard-failed'
          });
          return false;
        }
      } catch (error) {
        emitWarmupGate('match-data-router-recovery-skipped', {
          generation: generation, source: source,
          router: warmupMatchDataRouterText,
          reason: 'router-validation-error', error: String(error)
        });
        return false;
      }

      warmupMatchDataRouterAttemptedGeneration = generation;
      warmupMatchDataRouterAttemptedAtMs = now;
      warmupMatchDataRouterRecoveryInFlight = true;
      emitWarmupGate('match-data-router-recovery-enter', {
        generation: generation, source: source,
        router: warmupMatchDataRouterText, sourceEventId: 1012,
        payload: '0x0', stateTwoAgeMs: stateTwoAgeMs,
        stableObservations: warmupMatchDataRouterStableObservations
      });
      try {
        replayMatchDataRouter(router, 1012, ptr(0));
        emitWarmupGate('match-data-router-recovery-leave', {
          generation: generation, source: source,
          router: warmupMatchDataRouterText, sourceEventId: 1012,
          matchDataPublished:
            warmupMatchDataPublishedGeneration === generation,
          viewModelCreated:
            warmupFutMatchViewModelGeneration === generation,
          kitLoadComplete:
            warmupKitLoadCompleteGeneration === generation,
          mqTileDataObserved:
            warmupMqTileDataGeneration === generation
        });
      } catch (error) {
        emitWarmupGate('match-data-router-recovery-error', {
          generation: generation, source: source,
          router: warmupMatchDataRouterText, sourceEventId: 1012,
          error: String(error)
        });
      } finally {
        warmupMatchDataRouterRecoveryInFlight = false;
      }
      return true;
    }

    function scheduleWarmupStopRecovery(object, source) {
      const now = Date.now();
      const before = readWarmupModeState(object);
      const acceptedAgeMs = now - warmupRecoveryAcceptedAtMs;
      const matchDataPublished = warmupMatchDataPublishedGeneration ===
        warmupRecoveryGeneration;
      const routerRecoveryRequired = replayMatchDataRouter !== null &&
        !matchDataPublished &&
        (warmupStateTwoObservedAtMs <= 0 ||
         now - warmupStateTwoObservedAtMs <
           warmupMatchDataRouterUnavailableFallbackMs ||
         warmupMatchDataRouterAttemptedGeneration === warmupRecoveryGeneration);
      const routerRecoveryAttempted =
        warmupMatchDataRouterAttemptedGeneration === warmupRecoveryGeneration;
      const routerObservationAgeMs = routerRecoveryAttempted ?
        now - warmupMatchDataRouterAttemptedAtMs : -1;
      let skipReason = null;
      if (before.state !== 2 || before.gameModeId !== 17 ||
          before.readErrors.length !== 0) skipReason = 'native-state-not-ready';
      else if (warmupRecoveryGeneration <= 0) skipReason = 'no-generation';
      else if (warmupRecoveryAcceptedGeneration !== warmupRecoveryGeneration)
        skipReason = 'create-match-not-accepted';
      else if (warmupRecoveryAcceptedAtMs <= 0 || acceptedAgeMs < 0 ||
               acceptedAgeMs > warmupRecoveryMaxAgeMs)
        skipReason = 'create-match-not-recent';
      else if (routerRecoveryRequired && !routerRecoveryAttempted)
        skipReason = 'match-data-router-not-attempted';
      else if (routerRecoveryRequired &&
               routerObservationAgeMs < warmupMatchDataRouterObservationMs)
        skipReason = 'match-data-router-observing';
      else if (!matchDataPublished)
        skipReason = 'match-data-not-published';
      else if (warmupRecoveryAttemptedGeneration === warmupRecoveryGeneration)
        skipReason = 'generation-already-attempted';
      else if (warmupRecoveryInFlight) skipReason = 'recovery-in-flight';
      if (skipReason !== null) {
        // A 25 ms poll used to emit thousands of identical lines while
        // waiting for MatchData. Log that bounded wait once per generation.
        if (before.state === 2) {
          before.generation = warmupRecoveryGeneration;
          before.acceptedGeneration = warmupRecoveryAcceptedGeneration;
          before.attemptedGeneration = warmupRecoveryAttemptedGeneration;
          before.acceptedAgeMs = acceptedAgeMs;
          before.routerRecoveryAttempted = routerRecoveryAttempted;
          before.routerObservationAgeMs = routerObservationAgeMs;
          before.source = source;
          before.reason = skipReason;
          if (skipReason === 'match-data-not-published') {
            if (warmupStopWaitingGeneration !==
                warmupRecoveryGeneration) {
              warmupStopWaitingGeneration = warmupRecoveryGeneration;
              emitWarmupGate('state-two-stop-recovery-waiting', before);
            }
          } else {
            emitWarmupGate('state-two-stop-recovery-skipped', before);
          }
        }
        return false;
      }

      const recoveryGeneration = warmupRecoveryGeneration;
      const recoveryObjectText = object.toString();
      warmupRecoveryInFlight = true;
      before.generation = recoveryGeneration;
      before.acceptedAgeMs = acceptedAgeMs;
      before.delayMs = warmupRecoveryDelayMs;
      before.matchDataPublished = matchDataPublished;
      before.routerRecoveryAttempted = routerRecoveryAttempted;
      before.routerObservationAgeMs = routerObservationAgeMs;
      before.source = source;
      emitWarmupGate('state-two-stop-recovery-scheduled', before);
      setTimeout(function () {
        const recoveryObject = ptr(recoveryObjectText);
        try {
          const delayedAgeMs = Date.now() - warmupRecoveryAcceptedAtMs;
          const delayed = readWarmupModeState(recoveryObject);
          const delayedMatchDataPublished =
            warmupMatchDataPublishedGeneration === recoveryGeneration;
          const delayedRouterRecoveryRequired = replayMatchDataRouter !== null &&
            !delayedMatchDataPublished &&
            (warmupStateTwoObservedAtMs <= 0 ||
             Date.now() - warmupStateTwoObservedAtMs <
               warmupMatchDataRouterUnavailableFallbackMs ||
             warmupMatchDataRouterAttemptedGeneration === recoveryGeneration);
          const delayedRouterRecoveryAttempted =
            warmupMatchDataRouterAttemptedGeneration === recoveryGeneration;
          const delayedRouterObservationAgeMs =
            delayedRouterRecoveryAttempted ?
              Date.now() - warmupMatchDataRouterAttemptedAtMs : -1;
          delayed.generation = recoveryGeneration;
          delayed.acceptedAgeMs = delayedAgeMs;
          delayed.matchDataPublished = delayedMatchDataPublished;
          delayed.routerRecoveryAttempted = delayedRouterRecoveryAttempted;
          delayed.routerObservationAgeMs = delayedRouterObservationAgeMs;
          delayed.source = source;
          if (warmupRecoveryGeneration !== recoveryGeneration ||
              warmupRecoveryAcceptedGeneration !== recoveryGeneration ||
              !delayedMatchDataPublished ||
              (delayedRouterRecoveryRequired &&
               (!delayedRouterRecoveryAttempted ||
                delayedRouterObservationAgeMs <
                  warmupMatchDataRouterObservationMs)) ||
              delayedAgeMs < 0 || delayedAgeMs > warmupRecoveryMaxAgeMs ||
              delayed.state !== 2 || delayed.gameModeId !== 17 ||
              delayed.readErrors.length !== 0) {
            delayed.reason = 'delayed-guard-failed';
            emitWarmupGate('state-two-stop-recovery-skipped', delayed);
            return;
          }
          warmupRecoveryAttemptedGeneration = recoveryGeneration;
          emitWarmupGate('state-two-stop-recovery-enter', delayed);
          completeWarmupStop(recoveryObject, 0x0c, warmupStopZeroPayload);
          const after = readWarmupModeState(recoveryObject);
          after.generation = recoveryGeneration;
          after.source = source;
          after.stateSevenObserved = after.state === 7;
          emitWarmupGate('state-two-stop-recovery-leave', after);
        } catch (error) {
          emitWarmupGate('state-two-stop-recovery-error', {
            object: recoveryObjectText, generation: recoveryGeneration,
            source: source, error: String(error)
          });
        } finally {
          warmupRecoveryInFlight = false;
        }
      }, warmupRecoveryDelayMs);
      return true;
    }

    // Live evidence shows AssetLoadingStart at state 0, state 2 roughly one
    // second later, and the one-shot 0x37 readiness handler another six
    // seconds after that. Advancing as soon as state 2 appeared exposed a
    // working HUD over an uninitialised black scene. Wait through most of that
    // native bootstrap interval, but enter state 7 before 0x37 checks it.
    warmupStatePollTimer = setInterval(function () {
      attemptWarmupOfflineSelectRecovery('recovery-poll');
      if (warmupModeObjectText === null ||
          warmupModeObjectGeneration !== warmupRecoveryGeneration ||
          warmupRecoveryAcceptedGeneration !== warmupRecoveryGeneration ||
          warmupRecoveryAttemptedGeneration === warmupRecoveryGeneration ||
          warmupRecoveryInFlight) return;
      const modeObject = ptr(warmupModeObjectText);
      const polled = readWarmupModeState(modeObject);
      if (polled.state !== 2 || polled.gameModeId !== 17 ||
          polled.readErrors.length !== 0) {
        warmupStateTwoObservedAtMs = 0;
        return;
      }
      const now = Date.now();
      if (warmupStateTwoObservedAtMs <= 0) {
        warmupStateTwoObservedAtMs = now;
        emitWarmupGate('state-two-bootstrap-waiting', {
          generation: warmupRecoveryGeneration,
          object: warmupModeObjectText,
          delayMs: warmupStateTwoBootstrapDelayMs
        });
        attemptWarmupMatchBootstrapFactoryRecovery(
          modeObject, 'state-2-entered');
        return;
      }
      attemptWarmupMatchBootstrapFactoryRecovery(modeObject, 'state-2-poll');
      attemptWarmupMatchDataRecovery('state-2-poll');
      attemptWarmupMatchDataRouterRecovery(modeObject, 'state-2-poll');
      if (now - warmupStateTwoObservedAtMs <
          warmupStateTwoBootstrapDelayMs) return;
      scheduleWarmupStopRecovery(modeObject, 'state-2-bootstrap-grace');
    }, warmupStatePollIntervalMs);
    Interceptor.attach(matchDataProviderHandler, {
      onEnter(args) {
        rememberWarmupMatchDataController(args[0], 'provider-handler');
      }
    });
    Interceptor.attach(matchDataProviderSubHandler, {
      onEnter(args) {
        try {
          rememberWarmupMatchDataController(args[0].sub(0x128),
            'provider-sub-handler');
        } catch (error) {
          emitWarmupGate('match-data-controller-capture-error', {
            object: args[0].toString(), source: 'provider-sub-handler',
            error: String(error)
          });
        }
      }
    });
    Interceptor.attach(createMatchProviderHandler, {
      onEnter(args) {
        const providerId = args[1].toInt32() & 0xffff;
        const watchedProviderIds = [0x754f,0x755a,0x7562,0x7563,0x7564];
        if (watchedProviderIds.indexOf(providerId) >= 0)
          rememberWarmupCreateMatchController(
            args[0], 'provider-' + providerId.toString(16));
        if (providerId !== 0x7564) return;
        warmupCreateMatchProviderObservedGeneration =
          warmupRecoveryGeneration;
        emitWarmupGate('create-match-provider', {
          providerId: providerId, object: args[0].toString(),
          generation: warmupRecoveryGeneration,
          caller: this.returnAddress.toString()
        });
      }
    });
    Interceptor.attach(createMatchDpAccepted, {
      onEnter() {
        // Diagnostic only: this callback does not prove that the CreateMatch
        // response DTO decoded successfully. Authorization is set exclusively
        // by installCreateMatchParserTrace() when resultByte === 1.
        emitWarmupGate('create-match-dp-accepted', {
          generation: warmupRecoveryGeneration,
          acceptedGeneration: warmupRecoveryAcceptedGeneration,
          acceptedAtMs: warmupRecoveryAcceptedAtMs,
          caller: this.returnAddress.toString()
        });
      }
    });
    Interceptor.attach(matchDataBuilder, {
      onEnter(args) {
        rememberWarmupMatchDataController(args[0], 'builder');
        emitWarmupGate('match-data-build', {
          object: args[0].toString(), caller: this.returnAddress.toString()
        });
      }
    });
    Interceptor.attach(matchDataPublish, {
      onEnter() {
        const payload = this.context.r8;
        if (!payload.isNull())
          warmupMatchDataPublishedGeneration = warmupRecoveryGeneration;
        emitWarmupGate('match-data-publish', {
          providerId: 0x7569, caller: this.returnAddress.toString(),
          generation: warmupRecoveryGeneration,
          payload: payload.toString(), payloadReady: !payload.isNull()
        });
      }
    });
    Interceptor.attach(futEventDispatcher, {
      onEnter(args) {
        const argumentEventId = args[1].toInt32() & 0xffff;
        const registerEventId = this.context.rdx.toInt32() & 0xffff;
        const watched = [0x0c, 0x23, 0x32, 0x37];
        if (watched.indexOf(argumentEventId) < 0 &&
            watched.indexOf(registerEventId) < 0) return;
        const details = readWarmupModeState(args[0]);
        details.argumentEventId = argumentEventId;
        details.registerEventId = registerEventId;
        details.data = args[2].toString();
        addWarmupCallSite(details, this, cards);
        emitWarmupGate('dispatcher-event', details);
        if (details.gameModeId === 17 && details.readErrors.length === 0 &&
            details.state >= 0 && details.state <= 2 &&
            warmupRecoveryAcceptedGeneration !== warmupRecoveryGeneration &&
            warmupRecoveryPendingGeneration === warmupRecoveryGeneration) {
          if (acceptWarmupCreateMatchCandidate('native-controller') &&
              warmupCreateMatchProviderRecoveryTrigger !== null)
            warmupCreateMatchProviderRecoveryTrigger('native-controller');
        }
        if (details.gameModeId === 17 && details.readErrors.length === 0 &&
            warmupRecoveryGeneration > 0 &&
            warmupRecoveryAcceptedGeneration === warmupRecoveryGeneration) {
          warmupModeObjectText = args[0].toString();
          warmupModeObjectGeneration = warmupRecoveryGeneration;
        }
      }
    });
    Interceptor.attach(event37Handler, {
      onEnter(args) {
        const argumentEventId = args[1].toInt32() & 0xffff;
        const details = readWarmupModeState(args[0]);
        details.argumentEventId = argumentEventId;
        details.data = args[2].toString();
        addWarmupCallSite(details, this, cards);
        emitWarmupGate('event37-handler-enter', details);

        if (argumentEventId === 0x37 &&
            warmupRecoveryAcceptedGeneration === warmupRecoveryGeneration)
          warmupEvent37ObservedGeneration = warmupRecoveryGeneration;

        // 0x37 is a one-shot readiness gate. STOP must already have completed
        // when its state-7 comparison executes, so never schedule an
        // asynchronous STOP from inside this handler.
        if (argumentEventId === 0x37 && details.state !== 7)
          emitWarmupGate('event37-before-state-seven', details);
      }
    });
    Interceptor.attach(futFeStopDecision, {
      onEnter(args) {
        const argumentEventId = args[1].toInt32() & 0xffff;
        const registerEventId = this.context.rdx.toInt32() & 0xffff;
        this.warmupStopDecision = (argumentEventId === 0x0c ||
          registerEventId === 0x0c);
        if (!this.warmupStopDecision) return;
        this.warmupStopObject = args[0];
        const details = readWarmupModeState(args[0]);
        const payload0 = readWarmupS32(args[2], 0, 'payload0');
        details.argumentEventId = argumentEventId;
        details.registerEventId = registerEventId;
        details.payload = args[2].toString();
        details.payload0 = payload0.value;
        if (payload0.error !== null) details.readErrors.push(payload0.error);
        addWarmupCallSite(details, this, cards);
        emitWarmupGate('fut-fe-stop-decision-enter', details);
      },
      onLeave(result) {
        if (!this.warmupStopDecision) return;
        const details = readWarmupModeState(this.warmupStopObject);
        details.result = result.toString();
        emitWarmupGate('fut-fe-stop-decision-leave', details);
      }
    });
    Interceptor.attach(stateSevenRequest, {
      onEnter() {
        const details = readWarmupModeState(this.context.r13);
        details.requestedState = 7;
        addWarmupCallSite(details, this, cards);
        emitWarmupGate('state-seven-request', details);
      }
    });
    Interceptor.attach(stateRequestMethod, {
      onEnter(args) {
        const requestedState = args[1].toInt32();
        this.warmupStateSeven = requestedState === 7;
        if (!this.warmupStateSeven) return;
        this.warmupStateObject = args[0];
        const details = readWarmupModeState(args[0]);
        details.requestedState = requestedState;
        addWarmupCallSite(details, this, cards);
        emitWarmupGate('state-seven-callback-enter', details);
      },
      onLeave(result) {
        if (!this.warmupStateSeven) return;
        const details = readWarmupModeState(this.warmupStateObject);
        details.requestedState = 7;
        details.result = result.toString();
        emitWarmupGate('state-seven-callback-leave', details);
      }
    });
    Interceptor.attach(futFeToBe, {
      onEnter(args) {
        const details = readWarmupModeState(args[0]);
        details.caller = this.returnAddress.toString();
        emitWarmupGate('fut-fe-to-be', details);
      }
    });
    Interceptor.attach(canShowPressStartPublish, {
      onEnter() {
        emitWarmupGate('can-show-press-start-publish', {
          caller: this.returnAddress.toString()
        });
      }
    });
    Interceptor.attach(matchReadyMethod, {
      onEnter(args) {
        emitWarmupGate('match-ready-method', {
          rawRcx: args[0].toString(), caller: this.returnAddress.toString()
        });
      }
    });
    warmupGateTraceInstalled = true;
    emitWarmupGate('passive-ready', { module: cards.name });
    return true;
  } catch (error) {
    emitWarmupGate('install-error', {
      module: cards.name, error: String(error)
    });
    return false;
  }
}

if (embeddedDraftWarmupTraceEnabled && !installPassiveWarmupGateTrace()) {
  emitWarmupGate('module-waiting');
  warmupGateTraceRetryTimer = setInterval(function () {
    try {
      if (installPassiveWarmupGateTrace()) {
        clearInterval(warmupGateTraceRetryTimer);
        warmupGateTraceRetryTimer = null;
      }
    } catch (error) {
      clearInterval(warmupGateTraceRetryTimer);
      warmupGateTraceRetryTimer = null;
      emitWarmupGate('module-late-error', { error: String(error) });
    }
  }, 1000);
}

// The post-Side-Select participant compatibility is installed above at the
// mode-1008 cache search. CreateMatch remains a complete HOME clone; it cannot
// repair or replace a participant that must already exist in Team Comparison.

// Polling for CardsDLL once per second can miss early provider registrations.
// Frida's module observer runs synchronously when the module is added, before
// the application can use it, so the verified provider and completion probes
// above are installed in time. The existing timers remain a compatibility
// fallback for runtimes without the observer API.
let cardsModuleObserver = null;
if (typeof Process.attachModuleObserver === 'function') {
  cardsModuleObserver = Process.attachModuleObserver({
    onAdded(module) {
      if (module.name.toLowerCase() !==
          'cardsdll_win64_retail.dll') return;
      try {
        const portraitCacheReady = installDynamicPortraitHddCaching();
        const pimReady = installPrimeIconMomentsCompatibility();
        const totwAwayReady = installTotwAwayParticipantCompatibility();
        const totwWarmupReady = installTotwWarmupStopCompatibility();
        const draftReady = embeddedDraftDecoderTraceEnabled &&
          installPassiveDraftTrace();
        const warmupReady = embeddedDraftWarmupTraceEnabled &&
          installPassiveWarmupGateTrace();
        if (pimReady && primeIconMomentsCompatibilityRetryTimer !== null) {
          clearInterval(primeIconMomentsCompatibilityRetryTimer);
          primeIconMomentsCompatibilityRetryTimer = null;
        }
        if (portraitCacheReady &&
            dynamicPortraitHddCachingRetryTimer !== null) {
          clearInterval(dynamicPortraitHddCachingRetryTimer);
          dynamicPortraitHddCachingRetryTimer = null;
        }
        if (totwAwayReady &&
            totwAwayParticipantCompatibilityRetryTimer !== null) {
          clearInterval(totwAwayParticipantCompatibilityRetryTimer);
          totwAwayParticipantCompatibilityRetryTimer = null;
        }
        if (totwWarmupReady &&
            totwWarmupStopCompatibilityRetryTimer !== null) {
          clearInterval(totwWarmupStopCompatibilityRetryTimer);
          totwWarmupStopCompatibilityRetryTimer = null;
        }
        if (draftReady && draftTraceRetryTimer !== null) {
          clearInterval(draftTraceRetryTimer);
          draftTraceRetryTimer = null;
        }
        if (warmupReady && warmupGateTraceRetryTimer !== null) {
          clearInterval(warmupGateTraceRetryTimer);
          warmupGateTraceRetryTimer = null;
        }
        send({ event: 'draft-trace', status: 'module-observer-ready',
          module: module.name, pimReady: pimReady,
          portraitCacheReady: portraitCacheReady,
          totwAwayReady: totwAwayReady,
          totwWarmupReady: totwWarmupReady,
          draftReady: draftReady, warmupReady: warmupReady });
      } catch (error) {
        send({ event: 'draft-trace', status: 'module-observer-error',
          module: module.name, error: String(error) });
      }
    }
  });
}

// Match Day reads disabledregion.json in the background. When the retired FIFA
// Live service returns no document, this build passes a null string to the
// FNV-1a routine at +0xd13c936 (`movzx eax, byte [rbx]`). The case was
// reproduced twice with the same PC, read 0x0 and RBX=0. Treat only that null
// pointer as an empty string: EDX retains the FNV-1a offset basis and execution
// resumes at the next block. Every other exception remains passive and logged.
const fifaLiveNullHashCrash = base.add(0xd13c936);
const fifaLiveNullHashResume = base.add(0xd13c963);
const fifaLiveNullHashSignatureOk =
  fifaLiveNullHashCrash.readU8() === 0x0f &&
  fifaLiveNullHashCrash.add(1).readU8() === 0xb6 &&
  fifaLiveNullHashCrash.add(2).readU8() === 0x03;

// Record the native exception preceding termination. Suppress only the FIFA
// Live null-hash case above; no other crash is masked.
Process.setExceptionHandler(function (details) {
  if (fifaLiveNullHashSignatureOk && details.type === 'access-violation' &&
      details.context && details.context.pc.equals(fifaLiveNullHashCrash) &&
      details.context.rbx.isNull() && details.memory &&
      details.memory.operation === 'read' && details.memory.address.isNull()) {
    details.context.rax = ptr(0);
    details.context.rdx = ptr('0x811c9dc5');
    details.context.pc = fifaLiveNullHashResume;
    send({ event: 'fifa-live-null-hash-guard',
      crash: fifaLiveNullHashCrash.toString(),
      resume: fifaLiveNullHashResume.toString() });
    return true;
  }
  try {
    let moduleName = null;
    let moduleOffset = null;
    const module = Process.findModuleByAddress(details.address);
    if (module !== null) {
      moduleName = module.name;
      moduleOffset = details.address.sub(module.base).toString();
    }
    let backtrace = [];
    try {
      backtrace = Thread.backtrace(details.context, Backtracer.ACCURATE)
        .slice(0, 16).map(DebugSymbol.fromAddress).map(String);
    } catch (_) {}
    send({
      event: 'native-exception',
      exceptionType: details.type,
      address: details.address ? details.address.toString() : null,
      module: moduleName,
      moduleOffset: moduleOffset,
      operation: details.memory ? details.memory.operation : null,
      memoryAddress: details.memory && details.memory.address
        ? details.memory.address.toString() : null,
      pc: details.context && details.context.pc
        ? details.context.pc.toString() : null,
      sp: details.context && details.context.sp
        ? details.context.sp.toString() : null,
      registers: (function () {
        const result = {};
        const names = ['rax','rbx','rcx','rdx','rsi','rdi','rbp','rsp',
          'r8','r9','r10','r11','r12','r13','r14','r15','rip'];
        names.forEach(function (name) {
          if (details.context && details.context[name] !== undefined)
            result[name] = details.context[name].toString();
        });
        return result;
      })(),
      backtrace: backtrace
    });
  } catch (error) {
    send({ event: 'exception-handler-error', error: String(error) });
  }
  return false;
});
""" % (CERT_HANDLER_RVA, CERT_SUCCESS, CERT_SUCCESS)
FRIDA_JS=FRIDA_JS.replace("__PIM_ICON_ALIASES__",_PIM_ICON_ALIASES_JSON)
FRIDA_JS=FRIDA_JS.replace("__TOTW_TEAM_ID__",str(TOTW_PRESENTATION_TEAM_ID))
FRIDA_JS=FRIDA_JS.replace("__TOTW_BADGE_ASSET_ID__",str(
    _native_badge_item(TOTW_PRESENTATION_TEAM_ID,True)["assetId"]))
FRIDA_JS=FRIDA_JS.replace("__TOTW_HOME_KIT_ASSET_ID__",str(
    _native_kit_item(TOTW_PRESENTATION_TEAM_ID,2,True)["assetId"]))
FRIDA_JS=FRIDA_JS.replace("__TOTW_AWAY_KIT_ASSET_ID__",str(
    _native_kit_item(TOTW_PRESENTATION_TEAM_ID,3,True)["assetId"]))
FRIDA_JS=FRIDA_JS.replace(
    "__PIM_ICON_BASE_ALIASES__",_PIM_ICON_BASE_ALIASES_JSON)
# Python decodes ``\0`` inside the embedded triple-quoted source to U+0000,
# even when it appears only in a JavaScript comment. Frida rejects any script
# containing that character before parsing it. Preserve the intended textual
# notation and keep the generated agent valid without changing hook behavior.
def _frida_script_source(source):
    """Return source that is safe to pass across Frida's C string boundary."""
    cleaned=str(source).replace("\x00",r"\0")
    if "\x00" in cleaned:
        raise ValueError("Frida agent still contains an embedded NUL")
    return cleaned


FRIDA_JS=_frida_script_source(FRIDA_JS)
FRIDA_AGENTS_BY_NATIVE_ADAPTER = {
    NATIVE_PROFILE.native_adapter_id: FRIDA_JS,
}


def _native_exception_classification(payload):
    if (str(payload.get("module","")).lower()=="kernelbase.dll" and
            str(payload.get("moduleOffset","")).lower()=="0xc41ca"):
        return "hook","HANDLED STARTUP DRIVER EXCEPTION"
    return "crash","NATIVE EXCEPTION"

def frida_loop():
    try: import frida
    except ImportError:
        log("hook","!! frida non installato: esegui INSTALL_PREREQUISITES.cmd"); return
    # Validate and freeze every registered agent before opening a process
    # session. A deterministic source error must never create a partial
    # attachment that is then retried every three seconds.
    agent_sources={adapter:_frida_script_source(source)
                   for adapter,source in FRIDA_AGENTS_BY_NATIVE_ADAPTER.items()}
    device = frida.get_local_device()
    sessions={}
    build_inspections={}

    def make_script_message_handler(attached_pid):
        def on_message(message, data):
            if message.get("type") == "send":
                payload = message.get("payload")
                if isinstance(payload, dict) and payload.get("event") == "native-exception":
                    tag,label=_native_exception_classification(payload)
                    log(tag, "%s FIFA19.exe PID %d: %s at %s (%s+%s) mem=%s %s pc=%s sp=%s" % (label,
                        attached_pid, payload.get("exceptionType"), payload.get("address"),
                        payload.get("module"), payload.get("moduleOffset"),
                        payload.get("operation"), payload.get("memoryAddress"),
                        payload.get("pc"), payload.get("sp")))
                    for frame in (payload.get("backtrace") or [])[:16]:
                        log("crash", "  %s" % str(frame)[:500])
                    registers = payload.get("registers") or {}
                    if registers:
                        log("crash", "  REGISTERS %s" % " ".join(
                            "%s=%s" % (name, registers[name])
                            for name in ("rax","rbx","rcx","rdx","rsi","rdi","rbp","rsp",
                                         "r8","r9","r10","r11","r12","r13","r14","r15","rip")
                            if name in registers))
                elif isinstance(payload, dict) and payload.get("event") == "exception-handler-error":
                    log("hook", "exception logger error: %s" % payload.get("error"))
                elif isinstance(payload, dict) and payload.get("event") == "fifa-live-null-hash-guard":
                    log("live", "null-string guard applied: %s -> %s" %
                        (payload.get("crash"), payload.get("resume")))
                elif (isinstance(payload, dict) and
                      payload.get("event") ==
                      "totw-away-participant-compatibility"):
                    log("totw", "AWAY participant compatibility %s squad=%s record=%s summary=%s previous=%s current=%s matches=%s records=%s error=%s" %
                        (payload.get("status"),payload.get("selectedSquadId"),
                         payload.get("record"),payload.get("summary"),
                         payload.get("previous"),payload.get("current"),
                         payload.get("matches"),payload.get("records"),
                         payload.get("error")))
                elif (isinstance(payload, dict) and
                      payload.get("event") in ("draft-opponent-accessors",
                                               "draft-opponent-team")):
                    log("draft", "AWAY accessor %s %s round=%s team=%s "
                        "from=%s to=%s squad=%s error=%s" %
                        (payload.get("status"), payload.get("accessor", ""),
                         payload.get("round"), payload.get("teamId"),
                         payload.get("from"), payload.get("to"),
                         payload.get("squadId"), payload.get("error")))
                elif (isinstance(payload, dict) and
                      payload.get("event") == "offline-select-away-search"):
                    # The AWAY club every offline mode loads is decided by
                    # this one cache search. A Draft round shows Manchester
                    # City and then Arsenal whatever the server publishes, so
                    # record the mode, the owner it matches and every cached
                    # record's identity instead of guessing another payload.
                    log("match", "offline-select AWAY search mode=%s owner=%s/%s "
                        "squad=%s records=%s error=%s" %
                        (payload.get("mode"), payload.get("ownerHigh"),
                         payload.get("ownerLow"),
                         payload.get("selectedSquadId"),
                         payload.get("records"), payload.get("error")))
                    for entry in (payload.get("entries") or [])[:8]:
                        log("match", "  record owner=%s/%s classified=%s "
                            "team=%s badge=%s name=%r abbr=%r" %
                            (entry.get("ownerHigh"), entry.get("ownerLow"),
                             entry.get("classified"), entry.get("teamId"),
                             entry.get("badgeAssetId"), entry.get("name"),
                             entry.get("abbreviation")))
                elif (isinstance(payload, dict) and
                      payload.get("event") == "totw-warmup-stop"):
                    log("totw", "warm-up STOP %s generation=%s state=%s->%s mode=%s event=%s ageMs=%s transitioned=%s error=%s" %
                        (payload.get("status"),payload.get("generation"),
                         payload.get("state",payload.get("stateBefore")),
                         payload.get("stateAfter"),payload.get("gameModeId"),
                         payload.get("eventId"),payload.get("ageMs"),
                         payload.get("transitioned"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "network-hook":
                    log("net", "network trace: %s%s" %
                        (payload.get("status"), " API=" + ",".join(payload.get("apis") or [])
                         if payload.get("apis") else ""))
                elif isinstance(payload, dict) and payload.get("event") == "network-connect":
                    log("net", "CONNECTION #%s via %s -> %s result=%s caller=%s" %
                        (payload.get("count"),payload.get("api"),payload.get("destination"),
                         payload.get("result"),payload.get("caller")))
                elif isinstance(payload, dict) and payload.get("event") == "network-dns":
                    log("net", "DNS #%s via %s host=%r" %
                        (payload.get("count"),payload.get("api"),payload.get("host")))
                elif isinstance(payload, dict) and payload.get("event") == "network-send":
                    log("net", "READABLE SEND #%s bytes=%s: %s" %
                        (payload.get("count"),payload.get("size"),
                         str(payload.get("preview") or "").replace("\\r"," ").replace("\\n"," ")[:500]))
                elif isinstance(payload, dict) and payload.get("event") == "network-receive":
                    log("net", "READABLE RECEIVE #%s bytes=%s: %s" %
                        (payload.get("count"),payload.get("size"),
                         str(payload.get("preview") or "").replace("\\r"," ").replace("\\n"," ")[:1500]))
                elif isinstance(payload, dict) and payload.get("event") == "draft-trace":
                    status=payload.get("status")
                    if status == "decoded":
                        log("draft", "TRACE decoded return=%s reader=%s state=%s" %
                            (payload.get("result"),payload.get("reader"),
                             repr(payload.get("state"))))
                    elif status == "decoder-entered":
                        log("draft", "TRACE decoder entered response=%s reader=%s" %
                            (payload.get("response"),payload.get("reader")))
                    elif status == "decoder-milestone":
                        log("draft", "TRACE decoder milestone %s at %s" %
                            (payload.get("milestone"),payload.get("offset")))
                    elif status in ("purchase-decoder-entered",
                                    "purchase-decoded"):
                        log("draft", "TRACE %s response=%s reader=%s return=%s" %
                            (status,payload.get("response"),payload.get("reader"),
                             payload.get("result")))
                    elif status == "purchase-decoder-milestone":
                        log("draft", "TRACE purchase decoder milestone %s at %s" %
                            (payload.get("milestone"),payload.get("offset")))
                    elif status == "purchase-constructed":
                        log("draft", "TRACE purchase response constructed at %s caller=%s vtable=%s" %
                            (payload.get("response"),payload.get("caller"),
                             payload.get("vtable")))
                    elif status == "constructed":
                        log("draft", "TRACE response constructed at %s caller=%s state=%s" %
                            (payload.get("response"),payload.get("caller"),
                             repr(payload.get("state"))))
                        for frame in (payload.get("backtrace") or [])[:10]:
                            log("draft", "  %s" % str(frame)[:500])
                    elif status in ("completion-entered","destroyed"):
                        log("draft", "TRACE %s object=%s caller=%s state=%s" %
                            (status,payload.get("object") or payload.get("response"),
                             payload.get("caller"),repr(payload.get("state"))))
                    elif status in ("reference-constructed","reference-decoded"):
                        log("draft", "TRACE %s response=%s caller=%s result=%s" %
                            (status,payload.get("response"),payload.get("caller"),
                             payload.get("result")))
                    elif status in ("destroy-match-constructed",
                                    "destroy-match-decoder-entered",
                                    "destroy-match-decoded"):
                        log("draft", "TRACE %s response=%s reader=%s caller=%s return=%s vtableMatches=%s state=%s" %
                            (status,payload.get("response"),payload.get("reader"),
                             payload.get("caller"),payload.get("result"),
                             payload.get("vtableMatches"),
                             repr(payload.get("state"))))
                        for frame in (payload.get("backtrace") or [])[:10]:
                            log("draft", "  %s" % str(frame)[:500])
                    elif status == "transport-dispatch":
                        log("draft", "TRACE transport %s response=%s status=%s bodyLength=%s required=%s body=%s" %
                            (payload.get("kind"),payload.get("response"),
                             payload.get("httpStatus"),payload.get("bodyLength"),
                             payload.get("bodyRequired"),payload.get("body")))
                    elif status == "create-match-transport":
                        log("draft", "TRACE CreateMatch transport response=%s vtable=%s parser=%s status=%s bodyLength=%s required=%s body=%s preview=%s" %
                            (payload.get("response"),payload.get("vtable"),
                             payload.get("parser"),payload.get("httpStatus"),
                             payload.get("bodyLength"),payload.get("bodyRequired"),
                             payload.get("body"),payload.get("preview")))
                    elif status in ("create-match-parser-hooked",
                                    "create-match-parser-rejected"):
                        log("draft", "TRACE CreateMatch %s parser=%s vtable=%s offset=%s reason=%s" %
                            (status,payload.get("parser"),payload.get("vtable"),
                             payload.get("moduleOffset"),payload.get("reason")))
                    elif status in ("create-match-parser-entered",
                                    "create-match-decoded"):
                        log("draft", "TRACE CreateMatch %s response=%s reader=%s parser=%s return=%s returnByte=%s slots=%s" %
                            (status,payload.get("response"),payload.get("reader"),
                             payload.get("parser"),payload.get("result"),
                             payload.get("resultByte"),
                             repr(payload.get("objectSlots"))))
                    else:
                        log("draft", "TRACE %s module=%s constructor=%s parser=%s error=%s" %
                            (status,payload.get("module"),payload.get("constructor"),
                             payload.get("parser"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "warmup-gate":
                    fields=[]
                    for name in ("eventId","argumentEventId","registerEventId",
                                 "providerId","sourceEventId","object","rawRcx",
                                 "router","routerVtable","routerValidated",
                                 "stableObservations","recovery",
                                 "provider","controller","success","argument","state",
                                 "gameModeId","data","payload","payload0",
                                 "resultByte","request","requestSlots",
                                 "response","responseSlots",
                                 "field90","field98","fieldA0","fieldA8",
                                 "fieldB0","fieldB8","callback90","callbackA0",
                                 "eventLocal","eventSlots",
                                 "rbx","rbxVtable","rbxSlots",
                                 "rdi","rdiVtable","rdiSlots",
                                 "rsi","rsiVtable","rsiSlots",
                                 "providerVtable","providerSlots",
                                 "providerCallback",
                                 "arg0","arg1","allocator",
                                 "r13","r14","r13Slots","r14Slots",
                                 "flowState","flowStateError",
                                 "providerResult","providerResultError",
                                 "responseReason","decodedAgeMs",
                                 "transientResult","payloadReady",
                                 "destroyMatch","lastDestroyMatch",
                                 "destroyMatchMatchesLast","warmupMode",
                                 "tag","query","vtable","methodOffset",
                                 "methodLabel","method","method28",
                                 "method88","method98","argument1",
                                 "primaryVtable","secondaryVtable",
                                 "objectSlots","vtableSlots",
                                 "requestedState","result","caller","callerOffset",
                                 "callerSymbol","backtrace","module","failedChecks",
                                 "generation","acceptedGeneration",
                                 "candidateGeneration","candidateAtMs",
                                 "candidateAgeMs","source",
                                 "attemptedGeneration","acceptedAtMs",
                                 "acceptedAgeMs","stateSevenObserved","reason",
                                 "stateTwoAgeMs","routerRecoveryAttempted",
                                 "routerObservationAgeMs","matchDataPublished",
                                 "viewModelCreated","kitLoadComplete",
                                 "readErrors","callSiteError","error"):
                        value=payload.get(name)
                        if value is not None and value != []:
                            fields.append("%s=%s" % (name,repr(value)))
                    log("warmup", "GATE #%s t=%s %s%s" % (
                        payload.get("sequence"),payload.get("timestampMs"),
                        payload.get("stage"),
                        " " + " ".join(fields) if fields else ""))
                elif isinstance(payload, dict) and payload.get("event") == "origin-hook":
                    log("origin", "Origin trace: %s%s" %
                        (payload.get("status"), " (%s)" % payload.get("module") if payload.get("module") else ""))
                elif isinstance(payload, dict) and payload.get("event") == "origin-call":
                    log("origin", "CALL %s #%s output=%s previous_value=%s forced_online=%s profile_normalized=%s underage_before=%s personaId=%s profile_event_scheduled=%s return=%s caller=%s args=%s" %
                        (payload.get("name"),payload.get("callCount"),
                         payload.get("output"),payload.get("previousValue"),
                         payload.get("forcedOnline"),payload.get("profileNormalized"),
                         payload.get("underageBefore"),payload.get("personaId"),
                         payload.get("profileEventScheduled"),
                         payload.get("result"),
                         payload.get("returnAddress"),
                         ",".join(str(value) for value in (payload.get("args") or []))))
                elif isinstance(payload, dict) and payload.get("event") == "origin-passive-hook":
                    log("origin", "passive Origin trace: %s%s" %
                        (payload.get("status"), " (%s)" % payload.get("module") if payload.get("module") else ""))
                elif isinstance(payload, dict) and payload.get("event") == "origin-passive-call":
                    log("origin", "PASSIVE %s #%s status=%s return=%s caller=%s args=%s" %
                        (payload.get("name"), payload.get("callCount"),
                         payload.get("status") or "observed", payload.get("result"),
                         payload.get("returnAddress"),
                         ",".join(str(value) for value in (payload.get("args") or []))))
                elif isinstance(payload, dict) and payload.get("event") == "origin-profile-callback":
                    if payload.get("status") == "sent":
                        log("origin", "PROFILE CALLBACK sent: event=%s value=%s return=%s" %
                            (payload.get("eventType"),payload.get("value"),payload.get("result")))
                    else:
                        log("origin", "PROFILE CALLBACK failed: status=%s error=%s" %
                            (payload.get("status"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-entitlements-query":
                    log("origin", "LOCAL ENTITLEMENTS QUERY #%s handle=%s written=%s callback_scheduled=%s return=%s caller=%s args=%s" %
                        (payload.get("callCount"),payload.get("handle"),
                         payload.get("handleWritten"),payload.get("completionScheduled"),
                         payload.get("result"),payload.get("returnAddress"),
                         ",".join(str(value) for value in (payload.get("args") or []))))
                elif isinstance(payload, dict) and payload.get("event") == "origin-internal-entitlements-query":
                    log("origin", "INTERNAL ENTITLEMENTS QUERY #%s sdk=%s user=%s filters=%s count=%s output=%s handle=%s written=%s callback_scheduled=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("sdk"),payload.get("user"),
                         payload.get("filters"),payload.get("filterCount"),
                         payload.get("output"),payload.get("handle"),
                         payload.get("handleWritten"),payload.get("completionScheduled"),
                         payload.get("result"),payload.get("returnAddress")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-achievement-grant":
                    log("origin", "ACHIEVEMENT GRANT #%s sdk=%s user=%s achievement=%s value=%s backend=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("sdk"),payload.get("user"),
                         payload.get("achievement"),payload.get("value"),
                         payload.get("backend"),payload.get("result"),
                         payload.get("returnAddress")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-null-sdk-landmines":
                    entries=payload.get("entries") or []
                    covered=[e for e in entries if e.get("guarded") or e.get("detoured")]
                    open_entries=[e for e in entries
                                  if not e.get("guarded") and not e.get("detoured")]
                    log("origin", "null-SDK landmines: %s/%s covered%s" %
                        (len(covered),len(entries),
                         "; open " + ",".join("%s@+%s" % (e.get("name"),e.get("rva"))
                                              for e in open_entries)
                         if open_entries else ""))
                elif isinstance(payload, dict) and payload.get("event") == "origin-wallet-query":
                    log("origin", "LOCAL WALLET QUERY #%s sdk=%s user=%s currency=%s context=%s callback_scheduled=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("sdk"),payload.get("user"),
                         payload.get("currency"),payload.get("context"),
                         payload.get("completionScheduled"),payload.get("result"),
                         payload.get("returnAddress")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-wallet-callback":
                    if payload.get("status") == "sent":
                        log("origin", "EMPTY WALLET CALLBACK sent: completion=%s context=%s balance=%s error=%s return=%s" %
                            (payload.get("completionCount"),payload.get("context"),
                             payload.get("balance"),payload.get("errorCode"),
                             payload.get("result")))
                    else:
                        log("origin", "WALLET CALLBACK failed: status=%s context=%s error=%s" %
                            (payload.get("status"),payload.get("context"),
                             payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-friends-query":
                    friend_count=int(payload.get("callCount") or 0)
                    if friend_count <= 3:
                        log("origin", "LOCAL FRIENDS QUERY #%s sdk=%s user=%s callback=%s context=%s handle=%s callback_status=%s callback_scheduled=%s return=%s caller=%s" %
                            (payload.get("callCount"),payload.get("sdk"),payload.get("user"),
                             payload.get("callback"),payload.get("context"),payload.get("handle"),
                             payload.get("callbackStatus"),payload.get("completionScheduled"),
                             payload.get("result"),payload.get("returnAddress")))
                    elif friend_count == 4:
                        log("origin", "PERIODIC FRIENDS QUERY: additional lines suppressed; callbacks continue")
                elif isinstance(payload, dict) and payload.get("event") == "origin-friends-callback":
                    if payload.get("status") == "sent":
                        completion_count=int(payload.get("completionCount") or 0)
                        if completion_count <= 3:
                            log("origin", "EMPTY FRIENDS CALLBACK sent: completion=%s context=%s handle=%s total=%s available=%s error=%s return=%s" %
                                (payload.get("completionCount"),payload.get("context"),
                                 payload.get("handle"),payload.get("total"),
                                 payload.get("available"),payload.get("errorCode"),
                                 payload.get("result")))
                    else:
                        log("origin", "FRIENDS CALLBACK failed: status=%s context=%s handle=%s error=%s" %
                            (payload.get("status"),payload.get("context"),
                             payload.get("handle"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-blocked-users-query":
                    log("origin", "LOCAL BLOCKED-USERS QUERY #%s sdk=%s user=%s context=%s handle=%s callback_scheduled=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("sdk"),payload.get("user"),
                         payload.get("context"),payload.get("handle"),
                         payload.get("completionScheduled"),payload.get("result"),
                         payload.get("returnAddress")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-blocked-users-callback":
                    if payload.get("status") == "sent":
                        log("origin", "EMPTY BLOCKED-USERS CALLBACK sent: completion=%s context=%s handle=%s total=%s available=%s error=%s return=%s" %
                            (payload.get("completionCount"),payload.get("context"),
                             payload.get("handle"),payload.get("total"),
                             payload.get("available"),payload.get("errorCode"),
                             payload.get("result")))
                    else:
                        log("origin", "BLOCKED-USERS CALLBACK failed: status=%s context=%s handle=%s error=%s" %
                            (payload.get("status"),payload.get("context"),
                             payload.get("handle"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-auth-code-sync":
                    log("origin", "LOCAL AUTH CODE #%s request_null=%s written=%s length_written=%s length=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("requestNull"),
                         payload.get("codeWritten"),
                         payload.get("lengthWritten"),
                         payload.get("length"),payload.get("result"),
                         payload.get("returnAddress")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-eligibility-decision":
                    log("origin", "ELIGIBILITY DECISION #%s sdk_error=%s text_ptr=%s text=%r fallback_applied=%s state=%s mapped_error=%s rbx=%s r15=%s" %
                        (payload.get("callCount"),payload.get("sdkError"),
                         payload.get("textPointer"),payload.get("textValue"),
                         payload.get("fallbackApplied"),payload.get("state"),payload.get("mappedError"),
                         payload.get("rbx"),payload.get("r15")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-eligibility-fallback":
                    log("origin", "ELIGIBILITY FALLBACK #%s applied: sdk_error=%s original_text=%s replacement=%r ptr=%s" %
                        (payload.get("callCount"),payload.get("sdkError"),
                         payload.get("originalTextPointer"),payload.get("replacementText"),
                         payload.get("replacementTextPointer")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-eligibility-fallback-error":
                    log("origin", "ELIGIBILITY FALLBACK #%s failed: sdk_error=%s error=%s" %
                        (payload.get("callCount"),payload.get("sdkError"),
                         payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-identity-credentials":
                    log("origin", "IDENTITY FIELD GUARD #%s client_id_missing=%s client_id_fallback=%s client_secret_missing=%s client_secret_fallback=%s redirect_uri_missing=%s redirect_uri_fallback=%s" %
                        (payload.get("callCount"),payload.get("clientIdWasMissing"),
                         payload.get("clientIdFallbackApplied"),payload.get("clientSecretWasMissing"),
                         payload.get("clientSecretFallbackApplied"),payload.get("redirectUriWasMissing"),
                         payload.get("redirectUriFallbackApplied")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-underage-branch":
                    log("origin", "OSDK_UNDERAGE_ERROR BRANCH #%s taken: sdk_error=%s rbx=%s" %
                        (payload.get("callCount"),payload.get("sdkError"),
                         payload.get("rbx")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-entitlements-callback":
                    if payload.get("status") == "sent":
                        log("origin", "EMPTY ENTITLEMENTS CALLBACK sent: source=%s completion=%s handle=%s total=%s available=%s error=%s return=%s" %
                            (payload.get("source"),payload.get("completionCount"),payload.get("handle"),
                             payload.get("total"),payload.get("available"),
                             payload.get("errorCode"),payload.get("result")))
                    else:
                        log("origin", "ENTITLEMENTS CALLBACK failed: source=%s status=%s handle=%s error=%s" %
                            (payload.get("source"),payload.get("status"),
                             payload.get("handle"),payload.get("error")))
                elif isinstance(payload, dict) and payload.get("event") == "origin-empty-enumeration-read":
                    log("origin", "EMPTY ENUMERATION READ #%s handle=%s buffer=%s size=%s ignored_fourth=%s index=%s itemsRead=%s zeroed=%s return=%s caller=%s" %
                        (payload.get("callCount"),payload.get("handle"),
                         payload.get("buffer"),payload.get("bufferSize"),
                         payload.get("ignoredFourth"),payload.get("startIndex"),
                         payload.get("itemsRead"),
                         payload.get("itemsReadWritten"),payload.get("result"),
                         payload.get("returnAddress")))
                else:
                    log("hook", "script message PID %d: %s" %
                        (attached_pid, repr(payload)[:1000]))
            elif message.get("type") == "error":
                log("hook", "script error PID %d: %s" %
                    (attached_pid, message.get("stack") or repr(message)))
        return on_message

    def make_detached_handler(attached_pid):
        # The factory prevents event arguments from overwriting the PID.
        def on_detached(reason, crash=None, *_extra):
            if crash is None:
                log("hook", "DETACHED FIFA19.exe PID %d: %s (no Frida crash report)" %
                    (attached_pid, reason))
            else:
                summary = getattr(crash, "summary", None) or repr(crash)
                log("hook", "DETACHED FIFA19.exe PID %d: %s crash=%s" %
                    (attached_pid, reason, str(summary)[:1200]))
                report = getattr(crash, "report", None)
                if report:
                    for line in str(report).splitlines()[:60]:
                        log("crash", line[:500])
            sessions.pop(attached_pid, None)
        return on_detached

    while True:
        try:
            procs = device.enumerate_processes()
            found=None
            for p in procs:
                if p.name.lower()=="fifa19.exe": found=p.pid; break
            if found and found not in sessions:
                inspection=build_inspections.get(found)
                if inspection is None:
                    inspection=inspect_process_build(found)
                    build_inspections[found]=inspection
                    summary=fingerprint_summary(inspection)
                    if inspection.supported:
                        log("compat","recognized %s profile=%s nativeAdapter=%s%s" %
                            (inspection.profile.display_name,
                             inspection.profile.profile_id,
                             inspection.profile.native_adapter_id,
                             " fingerprints="+summary if summary else ""))
                    else:
                        log("compat","REFUSED FIFA19.exe PID %d: %s%s" %
                            (found,inspection.reason,
                             " fingerprints="+summary if summary else ""))
                if inspection.supported:
                    native_adapter=inspection.profile.native_adapter_id
                    script_source=agent_sources.get(native_adapter)
                    if script_source is None:
                        log("compat","REFUSED FIFA19.exe PID %d: no native agent for adapter %s" %
                            (found,native_adapter))
                    else:
                        pending_session=None
                        try:
                            pending_session=frida.attach(found)
                            sc=pending_session.create_script(script_source)
                            sc.on("message", make_script_message_handler(found))
                            sc.load()
                            pending_session.on("detached", make_detached_handler(found))
                            sessions[found]=(pending_session,sc)
                            pending_session=None
                            log("hook","ATTACHED to FIFA19.exe (PID %d) - profile %s active" %
                                (found,inspection.profile.profile_id))
                        except Exception as e:
                            if pending_session is not None:
                                try:
                                    pending_session.detach()
                                except Exception as cleanup_error:
                                    log("hook","failed attach cleanup: %r" % cleanup_error)
                            log("hook","attach failed: %r" % e)
            # Remove dead PIDs.
            alive={p.pid for p in procs}
            for pid in list(sessions):
                if pid not in alive:
                    sessions.pop(pid,None)
            for pid in list(build_inspections):
                if pid not in alive: build_inspections.pop(pid,None)
        except Exception as e:
            log("hook","loop error: %r" % e)
        time.sleep(3)

def main():
    print("="*60)
    print(" FIFA 19 LOCAL FUT - SERVER OFFLINE")
    print("="*60)
    try:
        runtime_profile=configure_runtime_adapter("legacy-full")
    except RuntimeError as exc:
        log("compat","REFUSED server startup: %s" % exc)
        return 5
    occupied=occupied_service_ports()
    if occupied:
        log("main","ERROR: an older instance is still active on ports %s" %
            ", ".join(str(x) for x in occupied))
        log("main","Close other Local FUT/Python windows and restart this file.")
        return 4
    # A clean launch owns all service ports, so its log is authoritative.
    # Reset only here: a mistakenly launched duplicate must not erase the
    # diagnostics still being written by the active server instance.
    reset_server_log()
    log("compat","runtime profile=%s native=%s routes=%s dto=%s" % (
        runtime_profile.profile_id,runtime_profile.native_adapter_id,
        runtime_profile.route_adapter_id,runtime_profile.dto_adapter_id))
    log("compat","data namespace=%s root=%s" % (
        runtime_profile.data_namespace,DATA_ROOT))
    ensure_cert()
    for fn in (start_redirector, start_blaze, start_fut, frida_loop):
        threading.Thread(target=fn, daemon=True).start()
        time.sleep(0.3)
    log("main","all services started.")
    log("main","START FIFA 19 and enter Ultimate Team. Press Ctrl+C to stop.")
    stop_file=str(os.environ.get("LOCALFUT19_STOP_FILE","") or "").strip()
    try:
        while True:
            if stop_file and os.path.exists(stop_file):
                log("main","safe stop requested.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        log("main","stopping.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

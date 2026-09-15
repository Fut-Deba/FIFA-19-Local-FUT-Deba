#!/usr/bin/env python3
"""Draw the Draft AWAY club the server chose, not the client's fallback.

Every Draft round shows Manchester City and then Arsenal whatever CreateMatch
publishes. The 2026-09-07 23:12 run named the reason: the Draft's Offline
Select controller carries mode 1002 with owner 0/0 and selected squad 0, its
`FUT_IB_PACK_SELECT_DP` (0x7548) dependency check returns 0 and the
participant provider returns NULL, so the front end falls back to a built-in
club.

The identity the front end reads comes from two script accessors, registered
next to their names in the binding table: `GetOpponentFutTeamId` at
`CardsDLL+0x139ae0` (registration at `+0x137dfb`) and `GetOpponentBadgeTeamID`
at `CardsDLL+0x13bed0` (registration at `+0x13b8fc`). Both are function starts
with unwind records and sixteen-byte prologues unique in the image.

This guard replaces only their return value, and only while the local server
reports a live Draft match. Nothing else is written and no module memory is
patched.

An earlier attempt put the same hooks in the server's own embedded agent. That
agent is never injected on this profile: all 83 captured EA App sessions start
`network-only services started` and none logs `ATTACHED to FIFA19.exe`. The
guarded runner is what attaches here, so the hook belongs beside the other
guards.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_PRODUCT_VERSION = "19.0.4052077.0"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)

ACCESSORS = (
    ("GetOpponentFutTeamId", 0x139AE0,
     "4883ec28488b0de5622f00488d1556e3"),
    ("GetOpponentBadgeTeamID", 0x13BED0,
     "48895c2408574883ec20488b0d7f4a2f"),
)

OPPONENT_URL = "http://127.0.0.1:8199/localfut19/draft/opponent"
HTTP_TIMEOUT_SECONDS = 2.0
# One host round trip per this many milliseconds, not one per call: the front
# end asks for the badge on every frame of Match Preview.
REFRESH_INTERVAL_MS = 2000
MAX_EVENTS = 16
REQUEST_EVENT = "draft-away-club-request"
REPLY_TYPE = "draft-away-club-result"


def read_draft_opponent(_payload=None) -> dict:
    """Return the club the live Draft round must show, or zero when none."""
    try:
        with urllib.request.urlopen(OPPONENT_URL,
                                    timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return {"teamId": 0, "round": 0, "name": ""}
    if not isinstance(body, dict):
        return {"teamId": 0, "round": 0, "name": ""}
    try:
        team_id = int(body.get("teamId", 0) or 0)
    except (TypeError, ValueError):
        team_id = 0
    # A team id outside the FIFA range is never published.
    if not 0 < team_id <= 200000:
        team_id = 0
    return {"teamId": team_id,
            "round": int(body.get("round", 0) or 0),
            "name": str(body.get("name", "") or "")}


def agent_source() -> str:
    """Return the two-accessor, fingerprint-gated AWAY club guard."""
    return r"""
'use strict';
const moduleName = %s;
const accessors = %s;
const refreshIntervalMs = %d;
const maxEvents = %d;
let teamId = 0;
let round = 0;
let checkedAt = 0;
let events = 0;

function emit(payload) {
  if (events >= maxEvents) return;
  events++;
  send(payload);
}

function matches(address, expected) {
  try {
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let i = 0; i < expected.length; i++)
      if (actual[i] !== expected[i]) return false;
    return true;
  } catch (_) { return false; }
}

function refresh() {
  const now = Date.now();
  if (now - checkedAt < refreshIntervalMs) return;
  checkedAt = now;
  let answer = null;
  const pending = recv('%s', function (message) {
    answer = message.payload || {};
  });
  send({event: '%s'});
  pending.wait();
  if (answer === null) return;
  const next = Number(answer.teamId || 0);
  if (next !== teamId)
    emit({event: 'draft-away-club', status: 'published', teamId: next,
      round: Number(answer.round || 0), name: String(answer.name || '')});
  teamId = next;
  round = Number(answer.round || 0);
}

function attach(cards) {
  for (let i = 0; i < accessors.length; i++) {
    const entry = accessors[i];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused', reason: 'accessor-signature-mismatch',
        accessor: entry.name, rva: entry.rva});
      return;
    }
  }
  for (let i = 0; i < accessors.length; i++) {
    const entry = accessors[i];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter() { refresh(); },
      onLeave(retval) {
        if (teamId <= 0) return;
        const before = retval.toInt32();
        if (before === teamId) return;
        retval.replace(ptr(teamId));
        emit({event: 'draft-away-club', status: 'applied',
          accessor: entry.name, round: round, from: before, to: teamId});
      }
    });
  }
  send({event: 'armed', guard: 'draft-away-club', module: cards.name,
    accessors: accessors.length});
}

let pollsLeft = 900;
function waitForModule() {
  let cards = null;
  try { cards = Process.findModuleByName(moduleName); }
  catch (_) { cards = null; }
  if (cards !== null) { attach(cards); return; }
  pollsLeft--;
  if (pollsLeft <= 0) {
    send({event: 'refused', reason: 'CardsDLL module absent'});
    return;
  }
  setTimeout(waitForModule, 1000);
}

send({event: 'armed', guard: 'draft-away-club', module: moduleName,
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps([{"name": name, "rva": rva,
                     "signature": list(bytes.fromhex(signature))}
                    for name, rva, signature in ACCESSORS]),
        REFRESH_INTERVAL_MS,
        MAX_EVENTS,
        REQUEST_EVENT,
        REQUEST_EVENT,
    )


def image_errors(image: bytes, product_version: str) -> list[str]:
    """Return the reasons this guard must not be armed against an image."""
    import hashlib
    import struct

    errors: list[str] = []
    if product_version != EXPECTED_PRODUCT_VERSION:
        errors.append("CardsDLL product version mismatch")
    if hashlib.sha256(image).hexdigest() != EXPECTED_CARDS_SHA256:
        return errors + ["CardsDLL fingerprint mismatch"]
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    count = struct.unpack_from("<H", image, pe_offset + 6)[0]
    table = pe_offset + 24 + struct.unpack_from("<H", image, pe_offset + 20)[0]

    def offset_of(rva):
        for index in range(count):
            entry = table + index * 40
            size, address, raw, pointer = struct.unpack_from(
                "<IIII", image, entry + 8)
            if address <= rva < address + max(size, raw):
                return pointer + rva - address
        raise ValueError("RVA 0x%x is outside every section" % rva)

    for name, rva, signature in ACCESSORS:
        wanted = bytes.fromhex(signature)
        offset = offset_of(rva)
        if image[offset:offset + len(wanted)] != wanted:
            errors.append("%s signature mismatch at 0x%x" % (name, rva))
        if image.count(wanted) != 1:
            errors.append("%s signature is not unique" % name)
    return errors

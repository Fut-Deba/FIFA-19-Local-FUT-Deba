#!/usr/bin/env python3
"""Publish the Draft AWAY club through the native OPPONENT_CLUBS provider.

Draft Offline Select runs as FEMode 1002. ``createDataProvider`` at
``CardsDLL+0x472c0`` answers the opponent-clubs key ``0x755f`` only for modes
1007, 1008 and 1013 (gate ``+0x47314``); 1002 returns NULL, so the front end
keeps the club it drew last (Manchester City, then Arsenal). RC123 showed FIFA
asks for ``0x755f`` right after the Draft ``POST /match``, when the server
already knows the round's club.

While the server reports a live Draft match, the 1002 gate takes the Squad
Battles branch instead: at ``+0x47327`` ``ecx`` holds 1002 - 1008 = -6 and is
set to 5, so the native builder ``+0x48ff0`` runs (it never reads the
controller). At that builder's only serializer call (return ``+0x49466``) the
stack record receives the Draft club's teamId, name and abbreviation and a
zero PUBLIC byte, and its original bytes are restored when the serializer
returns, before the record's destructor runs. Nothing is patched and no other
mode is touched.
"""

from __future__ import annotations

import json

from guard_eaapp_draft_away_club import read_draft_opponent  # noqa: F401


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_PRODUCT_VERSION = "19.0.4052077.0"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)

GATE_RVA = 0x47327
SERIALIZER_RVA = 0x49DA0
BUILDER_RETURN_RVA = 0x49466
SITES = (
    ("opponent-clubs-mode-gate", 0x47314,
     "8b8b3001000081e9ef030000744e83e901741c83f9050f8505020000"),
    ("squad-battle-serializer-call", 0x49442,
     "c744242801000000c7442420010000004533c94c8d442470"
     "488d95b8000000e83a090000"),
    ("club-serializer-entry", 0x49DA0,
     "405556574154415541564157488d6c24e94881ec"),
)
REFRESH_INTERVAL_MS = 2000
MAX_EVENTS = 16
REQUEST_EVENT = "draft-opponent-clubs-request"
REPLY_TYPE = "draft-opponent-clubs-result"


def agent_source() -> str:
    """Return the fingerprint-gated Draft opponent-clubs guard."""
    return r"""
'use strict';
const moduleName = %s;
const sites = %s;
const gateRva = %d;
const serializerRva = %d;
const builderReturnRva = %d;
const refreshIntervalMs = %d;
const maxEvents = %d;
let teamId = 0;
let round = 0;
let name = '';
let checkedAt = 0;
let events = 0;
let redirectThread = -1;
const strings = {};

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

// Runs only on the mode-1002 gate, so only a Draft ever waits for the host.
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
  if (answer === null || answer.status === 'error') return;
  teamId = Number(answer.teamId || 0);
  round = Number(answer.round || 0);
  name = String(answer.name || '');
}

// Heap form of the native small string: pointer at +0, length at +8 and the
// sign bit of byte +0xf set. The caller restores the original bytes.
function heapString(field, text) {
  if (!(text in strings)) strings[text] = Memory.allocUtf8String(text);
  const buffer = strings[text];
  let length = 0;
  while (buffer.add(length).readU8() !== 0) length++;
  field.writePointer(buffer);
  field.add(8).writeU32(length);
  field.add(12).writeU32((length | 0x80000000) >>> 0);
}

// ponytail: three ASCII letters of the name; a real abbreviation table when
// the pools carry one.
function abbreviation(text) {
  const letters = text.replace(/[^A-Za-z]/g, '').slice(0, 3).toUpperCase();
  return letters || 'CPU';
}

function attach(cards) {
  for (let i = 0; i < sites.length; i++) {
    const site = sites[i];
    if (!matches(cards.base.add(site.rva), site.signature)) {
      send({event: 'refused', reason: 'site-signature-mismatch',
        site: site.name, rva: site.rva});
      return;
    }
  }
  const builderReturn = cards.base.add(builderReturnRva);
  Interceptor.attach(cards.base.add(gateRva), {
    onEnter() {
      if (this.context.rcx.toInt32() !== -6) return;
      if (this.context.rbx.add(0x130).readS32() !== 1002) return;
      refresh();
      if (teamId <= 0 || name === '') return;
      this.context.rcx = ptr(5);
      redirectThread = this.threadId;
      emit({event: 'draft-opponent-clubs', status: 'redirected',
        teamId: teamId, round: round});
    }
  });
  Interceptor.attach(cards.base.add(serializerRva), {
    onEnter() {
      this.record = null;
      if (redirectThread !== this.threadId) return;
      if (!this.returnAddress.equals(builderReturn)) return;
      redirectThread = -1;
      const record = this.context.r8;
      this.saved = record.readByteArray(0x98);
      this.record = record;
      record.add(0x30).writeU8(0);
      heapString(record.add(0x38), name);
      heapString(record.add(0x60), abbreviation(name));
      record.add(0x94).writeS32(teamId);
      emit({event: 'draft-opponent-clubs', status: 'applied',
        teamId: teamId, round: round, name: name});
    },
    onLeave() {
      if (this.record !== null) this.record.writeByteArray(this.saved);
    }
  });
  send({event: 'armed', guard: 'draft-opponent-clubs', module: cards.name,
    sites: sites.length});
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

send({event: 'armed', guard: 'draft-opponent-clubs', module: moduleName,
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps([{"name": name, "rva": rva,
                     "signature": list(bytes.fromhex(signature))}
                    for name, rva, signature in SITES]),
        GATE_RVA,
        SERIALIZER_RVA,
        BUILDER_RETURN_RVA,
        REFRESH_INTERVAL_MS,
        MAX_EVENTS,
        REPLY_TYPE,
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

    for name, rva, signature in SITES:
        wanted = bytes.fromhex(signature)
        offset = offset_of(rva)
        if image[offset:offset + len(wanted)] != wanted:
            errors.append("%s signature mismatch at 0x%x" % (name, rva))
        if image.count(wanted) != 1:
            errors.append("%s signature is not unique" % name)
    return errors

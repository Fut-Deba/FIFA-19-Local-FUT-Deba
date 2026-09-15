#!/usr/bin/env python3
"""Record which club record the Offline Select AWAY search accepts.

`CardsDLL+0x48b50` builds the post-Side-Select AWAY participant and its cache
search at `+0x48cae` accepts only a 0xf0-byte GetClubInfo record whose
classification byte at `+0x30` is nonzero. The project already marks that byte
for Offline Select mode 1008 (TOTW); every other mode fails closed, and the
Draft is mode 1002 - its controller carries owner 0/0 and selected squad 0 and
its participant provider returns NULL, which is why every round shows
Manchester City and then Arsenal.

Two things are still unknown and neither can be guessed: whether this search
even runs for a Draft round, and what the record cache holds when it does.
This observer answers both. It reads the controller's mode, the owner it is
matching, the selected squad id, and the owner, classification byte, name,
abbreviation, badge and team id of each cached record. It writes nothing.

The equivalent report was first added to the server's own embedded agent,
which is never injected on this profile: all 84 captured EA App sessions log
`network-only services started` and none logs `ATTACHED to FIFA19.exe`.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)

# v1 0x4883e -> EA App 0x48cae, the same +0x470 displacement the
# whole 0x4xxxx region carries; the v1 signature occurs exactly once
# in the EA App image and 0x48cae is inside the builder that v1 has
# at 0x486e0 and the EA App at 0x48b50.
CACHE_SEARCH_RVA = 0x48CAE
CACHE_SEARCH_SIGNATURE = "488d97207100004c8b42304c3b4238"
TOTW_MODE = 1008
MAX_REPORTS = 8
MAX_ENTRIES = 8

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "writeU8", "writeU32", "writeS32", "writePointer",
    "writeByteArray", "retval.replace", "NativeFunction", "Stalker.",
)


def agent_source() -> str:
    """Return the read-only Offline Select AWAY search observer."""
    return r"""
'use strict';
const moduleName = %s;
const searchRva = %d;
const signature = %s;
const totwMode = %d;
const maxReports = %d;
const maxEntries = %d;
let reports = 0;

function readable(address, size) {
  try {
    if (address === null || address.isNull()) return false;
    const first = Process.findRangeByAddress(address);
    const last = Process.findRangeByAddress(address.add(Math.max(0, size - 1)));
    return first !== null && last !== null &&
      first.protection.indexOf('r') !== -1 &&
      last.protection.indexOf('r') !== -1;
  } catch (_) { return false; }
}

function text(address, maximum) {
  try {
    if (!readable(address, maximum)) return null;
    let out = '';
    for (let i = 0; i < maximum; i++) {
      const value = address.add(i).readU8();
      if (value === 0) break;
      if (value < 0x20 || value > 0x7e) return null;
      out += String.fromCharCode(value);
    }
    return out;
  } catch (_) { return null; }
}

function attach(cards) {
  const site = cards.base.add(searchRva);
  let ok = false;
  try {
    const actual = new Uint8Array(site.readByteArray(signature.length));
    ok = actual.length === signature.length;
    for (let i = 0; ok && i < signature.length; i++)
      if (actual[i] !== signature[i]) ok = false;
  } catch (_) { ok = false; }
  if (!ok) {
    send({event: 'refused', reason: 'cache-search-signature-mismatch',
      rva: searchRva});
    return;
  }
  Interceptor.attach(site, {
    onEnter() {
      if (reports >= maxReports) return;
      const controller = this.context.rbx;
      const service = this.context.rdi;
      if (!readable(controller, 0x140)) return;
      let mode = null;
      try { mode = controller.add(0x130).readS32(); } catch (_) { return; }
      // TOTW already has its own marking path; only the unexplained modes
      // are worth a report.
      if (mode === totwMode) return;
      reports++;
      const report = {event: 'offline-select-away-search', mode: mode,
        ownerHigh: null, ownerLow: null, selectedSquadId: null,
        records: null, entries: []};
      try {
        report.ownerHigh = controller.add(0x134).readU32();
        report.ownerLow = controller.add(0x138).readU32();
        report.selectedSquadId = controller.add(0x13c).readU32();
        if (!readable(service, 0x7160)) {
          report.records = -2; send(report); return;
        }
        const vector = service.add(0x7120);
        const begin = vector.add(0x30).readPointer();
        const end = vector.add(0x38).readPointer();
        if (begin.isNull() || end.isNull() || end.compare(begin) < 0) {
          report.records = -1; send(report); return;
        }
        const bytes = end.sub(begin).toInt32();
        if (bytes < 0 || bytes %% 0xf0 !== 0 || bytes > 0xf0 * 64 ||
            !readable(begin, bytes)) {
          report.records = -3; send(report); return;
        }
        report.records = bytes / 0xf0;
        for (let offset = 0; offset < bytes &&
             report.entries.length < maxEntries; offset += 0xf0) {
          const record = begin.add(offset);
          report.entries.push({
            ownerLow: record.readU32(), ownerHigh: record.add(4).readU32(),
            classified: record.add(0x30).readU8(),
            name: text(record.add(0x38), 24),
            abbreviation: text(record.add(0x60), 8),
            badgeAssetId: record.add(0x8c).readU32(),
            teamId: record.add(0x94).readU32()});
        }
      } catch (error) {
        report.error = String(error);
      }
      send(report);
    }
  });
  send({event: 'armed', observer: 'offline-select-away', module: cards.name});
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

send({event: 'armed', observer: 'offline-select-away',
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        CACHE_SEARCH_RVA,
        json.dumps(list(bytes.fromhex(CACHE_SEARCH_SIGNATURE))),
        TOTW_MODE,
        MAX_REPORTS,
        MAX_ENTRIES,
    )


def self_test_errors() -> list[str]:
    """Return the reasons this observer must not be armed, if any."""
    errors = []
    source = agent_source()
    for token in FORBIDDEN_AGENT_TOKENS:
        if token in source:
            errors.append("forbidden agent primitive: %s" % token)
    if MAX_REPORTS <= 0 or MAX_ENTRIES <= 0:
        errors.append("unbounded report budget")
    return errors

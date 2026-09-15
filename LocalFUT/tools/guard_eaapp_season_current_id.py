#!/usr/bin/env python3
"""Recover the one enrolled local Season when CardsDLL loses its UI id.

The Season user parser assigns internal id 200000 to its first record, while
the competition manager can still return -1 after the mode is reopened.  The
guard changes only that return value, only for one parsed supported local
Season.  It does not write game memory or alter a valid native selection.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
GET_CURRENT_SEASON_ID_RVA = 0x1D9650
GET_CURRENT_SEASON_ID_SIGNATURE = tuple(bytes.fromhex(
    "40555657415641574883ec5048c7442430feffffff48899c2480000000"
    "beffffffff4533ff8d6e02488b05717225004885c00f8557010000"))
SEASON_RECORD_STORE_RVA = 0x28A322
SEASON_RECORD_STORE_SIGNATURE = tuple(bytes.fromhex(
    "488b0dd7651a00488b01488b90800b0000"))
SEASON_RECORDS_SINGLETON_RVA = 0x430900
SEASON_RECORDS_OFFSET = 0x6FF0
RECORD_START_OFFSET = 0x30
RECORD_END_OFFSET = 0x38
RECORD_STRIDE = 0x98
INTERNAL_ID_OFFSET = 0x24
SEASON_ID_OFFSET = 0x84
FIRST_INTERNAL_ID = 200_000
SUPPORTED_BASES = (19_000, 29_000, 39_000, 49_000, 59_000, 69_000)
MAX_EVENTS = 8
MODULE_POLL_ATTEMPTS = 900
MODULE_POLL_INTERVAL_MS = 1000


def agent_source() -> str:
    """Return the signature-gated current-id fallback."""
    return r"""
'use strict';
const moduleName = %s;
const getterRva = %d;
const getterSignature = %s;
const storeRva = %d;
const storeSignature = %s;
const singletonRva = %d;
const recordsOffset = %d;
const recordStartOffset = %d;
const recordEndOffset = %d;
const recordStride = %d;
const internalIdOffset = %d;
const seasonIdOffset = %d;
const firstInternalId = %d;
const supportedBases = %s;
const maxEvents = %d;
const pollAttempts = %d;
const pollIntervalMs = %d;
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

function attach(cards) {
  const getter = cards.base.add(getterRva);
  const store = cards.base.add(storeRva);
  if (!matches(getter, getterSignature) || !matches(store, storeSignature)) {
    send({event: 'refused', reason: 'season-current-id-signature-mismatch'});
    return;
  }
  Interceptor.attach(getter, {
    onLeave(retval) {
      if (retval.toInt32() >= 0) return;
      try {
        const singleton = cards.base.add(singletonRva).readPointer();
        if (singleton.isNull()) return;
        const records = singleton.add(recordsOffset);
        const start = records.add(recordStartOffset).readPointer();
        const end = records.add(recordEndOffset).readPointer();
        if (start.isNull() || end.sub(start).toUInt32() !== recordStride)
          return;
        const record = start;
        const internalId = record.add(internalIdOffset).readS32();
        const seasonId = record.add(seasonIdOffset).readS32();
        const division = seasonId %% 100;
        const base = seasonId - division;
        if (internalId !== firstInternalId || division < 1 || division > 10 ||
            supportedBases.indexOf(base) < 0)
          return;
        retval.replace(ptr(internalId));
        emit({event: 'season-current-id-applied', internalId: internalId,
          seasonId: seasonId});
      } catch (_) {}
    }
  });
  send({event: 'armed', guard: 'season-current-id', module: cards.name});
}

let pollsLeft = pollAttempts;
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
  setTimeout(waitForModule, pollIntervalMs);
}

send({event: 'armed', guard: 'season-current-id', module: moduleName,
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        GET_CURRENT_SEASON_ID_RVA,
        json.dumps(list(GET_CURRENT_SEASON_ID_SIGNATURE)),
        SEASON_RECORD_STORE_RVA,
        json.dumps(list(SEASON_RECORD_STORE_SIGNATURE)),
        SEASON_RECORDS_SINGLETON_RVA,
        SEASON_RECORDS_OFFSET,
        RECORD_START_OFFSET,
        RECORD_END_OFFSET,
        RECORD_STRIDE,
        INTERNAL_ID_OFFSET,
        SEASON_ID_OFFSET,
        FIRST_INTERNAL_ID,
        json.dumps(list(SUPPORTED_BASES)),
        MAX_EVENTS,
        MODULE_POLL_ATTEMPTS,
        MODULE_POLL_INTERVAL_MS,
    )

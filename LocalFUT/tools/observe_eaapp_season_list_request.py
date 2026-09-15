#!/usr/bin/env python3
"""Observe which divisions FIFA asks for in `season/list`, and who fills them.

The request serializer `CardsDLL+0x28b2f0` writes the query. For offline
seasons (`[request+0x40] == 4`) it encodes every entry of the int16 vector at
`[request+0x10]..[request+0x18]` as `11 - value` (`+0x28b3ca..+0x28b3f1`).

Live that vector holds 1 when FUT enters Seasons (query `divisionList=10`)
and 0 after every match (query `divisionList=11`, a division that cannot
exist). Answering that request with another division's rows, with an empty
list, and publishing `11 - division` were all disproved live, so the value's
source has to be read instead of guessed.

This observer only reads. It reports the vector, the request type and the
return address of the caller that built it, which is the function to map
statically afterwards.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)
# Hooked: the serializer entry, where rcx is the request.
SERIALIZER_RVA = 0x28B2F0
SERIALIZER_SIGNATURE = tuple(bytes.fromhex(
    "4055565741564157488bec4883ec7048c745e0feffffff48"))
# Validated only: the `11 - division` encoding this observer explains.
DIVISION_LOOP_RVA = 0x28B3CA
DIVISION_LOOP_SIGNATURE = tuple(bytes.fromhex(
    "837b40047526488b4310b90b00000066"))
# Hooked: the copy that writes the competition division (+0x38c) from a
# parsed record (+0x13c). rcx destination, rdx source begin, r8 source end.
RECORD_COPY_RVA = 0x284E30
RECORD_COPY_SIGNATURE = tuple(bytes.fromhex(
    "4053555657415641574883ec4848c7442420feffffff498b"))
RECORD_STRIDE = 0x250
RECORD_DIVISION_OFFSET = 0x13C
MAX_RECORDS = 4
# Separate budget: the copy fires on every catalogue load and must not
# consume the reports reserved for the post-match request.
MAX_RECORD_REPORTS = 6
VECTOR_BEGIN_OFFSET = 0x10
VECTOR_END_OFFSET = 0x18
TYPE_OFFSET = 0x40
REQUEST_SIZE = 0x48
OFFLINE_TYPE = 4
MAX_DIVISIONS = 16
MAX_REPORTS = 16
# RC164 live: the immediate caller is the generic request sender +0x2a39b0,
# which 63 sites share, so the function that fills the vector is further up.
MAX_FRAMES = 8
MODULE_POLL_ATTEMPTS = 900
MODULE_POLL_INTERVAL_MS = 1000

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "writeU8", "writeU32", "writeS32", "writeS16",
    "writePointer", "writeByteArray", "retval.replace", "NativeFunction",
    "Stalker.",
)


def agent_source() -> str:
    """Return the bounded, signature-gated read-only observer."""
    return r"""
'use strict';
const moduleName = %s;
const sites = %s;
const vectorBeginOffset = %d;
const vectorEndOffset = %d;
const typeOffset = %d;
const requestSize = %d;
const offlineType = %d;
const maxDivisions = %d;
const maxReports = %d;
const maxFrames = %d;
const recordStride = %d;
const recordDivisionOffset = %d;
const maxRecords = %d;
const maxRecordReports = %d;
const pollAttempts = %d;
const pollIntervalMs = %d;
let reports = 0;
let recordReports = 0;

function emit(payload) {
  if (reports >= maxReports) return;
  reports++;
  send(payload);
}

function emitRecord(payload) {
  if (recordReports >= maxRecordReports) return;
  recordReports++;
  send(payload);
}

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

function matches(address, expected) {
  try {
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++)
      if (actual[index] !== expected[index]) return false;
    return true;
  } catch (_) { return false; }
}

function rva(cards, address) {
  try {
    if (address.compare(cards.base) >= 0 &&
        address.compare(cards.base.add(cards.size)) < 0)
      return address.sub(cards.base).toInt32();
  } catch (_) {}
  return null;
}

function callChain(cards, context) {
  // Read-only stack walk: the sender is shared, so the Season-specific
  // builder is one of the frames above it.
  try {
    return Thread.backtrace(context, Backtracer.ACCURATE)
      .slice(0, maxFrames)
      .map(function (frame) { return rva(cards, frame); });
  } catch (error) { return [String(error)]; }
}

function recordSnapshot(begin, end) {
  const result = {};
  try {
    if (begin.isNull() || end.isNull() || end.compare(begin) < 0) {
      result.error = 'record-range-invalid';
      return result;
    }
    const count = end.sub(begin).toInt32() / recordStride;
    result.count = count;
    if (count < 0 || count > maxRecords * 16) return result;
    const values = [];
    for (let index = 0; index < Math.min(count, maxRecords); index++) {
      const record = begin.add(index * recordStride);
      if (!readable(record.add(recordDivisionOffset), 4)) break;
      values.push(record.add(recordDivisionOffset).readS32());
    }
    result.divisions = values;
  } catch (error) { result.error = String(error); }
  return result;
}

function requestSnapshot(request) {
  const result = {address: String(request)};
  try {
    if (!readable(request, requestSize)) {
      result.error = 'request-unreadable';
      return result;
    }
    result.type = request.add(typeOffset).readS32();
    result.offline = result.type === offlineType;
    const begin = request.add(vectorBeginOffset).readPointer();
    const end = request.add(vectorEndOffset).readPointer();
    if (begin.isNull() || end.isNull()) {
      result.divisions = [];
      return result;
    }
    const count = end.sub(begin).toInt32() / 2;
    if (count < 0 || count > maxDivisions || !readable(begin, count * 2)) {
      result.error = 'vector-out-of-range';
      result.count = count;
      return result;
    }
    const divisions = [];
    const query = [];
    for (let index = 0; index < count; index++) {
      const value = begin.add(index * 2).readS16();
      divisions.push(value);
      query.push(11 - value);
    }
    // `divisions` is what the client holds; `query` is what reaches the URL.
    result.divisions = divisions;
    result.query = query;
  } catch (error) { result.error = String(error); }
  return result;
}

function attach(cards) {
  for (let index = 0; index < sites.length; index++) {
    if (!matches(cards.base.add(sites[index].rva), sites[index].signature)) {
      send({event: 'refused', reason: 'season-list-signature-mismatch',
        rva: sites[index].rva});
      return;
    }
  }
  Interceptor.attach(cards.base.add(sites[0].rva), {
    onEnter(args) {
      emit({event: 'season-list-request',
        request: requestSnapshot(args[0]),
        callerRva: rva(cards, this.returnAddress),
        caller: String(this.returnAddress),
        frames: callChain(cards, this.context)});
    }
  });
  Interceptor.attach(cards.base.add(sites[2].rva), {
    onEnter(args) {
      emitRecord({event: 'season-record-copy',
        source: recordSnapshot(args[1], args[2]),
        callerRva: rva(cards, this.returnAddress),
        frames: callChain(cards, this.context)});
    }
  });
  send({event: 'armed', observer: 'season-list-request', module: cards.name});
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

send({event: 'armed', observer: 'season-list-request',
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps([
            {"rva": rva, "signature": list(signature)}
            for rva, signature in (
                (SERIALIZER_RVA, SERIALIZER_SIGNATURE),
                (DIVISION_LOOP_RVA, DIVISION_LOOP_SIGNATURE),
                (RECORD_COPY_RVA, RECORD_COPY_SIGNATURE),
            )
        ]),
        VECTOR_BEGIN_OFFSET,
        VECTOR_END_OFFSET,
        TYPE_OFFSET,
        REQUEST_SIZE,
        OFFLINE_TYPE,
        MAX_DIVISIONS,
        MAX_REPORTS,
        MAX_FRAMES,
        RECORD_STRIDE,
        RECORD_DIVISION_OFFSET,
        MAX_RECORDS,
        MAX_RECORD_REPORTS,
        MODULE_POLL_ATTEMPTS,
        MODULE_POLL_INTERVAL_MS,
    )


def self_test_errors() -> list[str]:
    """Return any reason this observer must not be armed."""
    errors = []
    source = agent_source()
    for token in FORBIDDEN_AGENT_TOKENS:
        if token in source:
            errors.append("forbidden agent primitive: %s" % token)
    if (MAX_REPORTS <= 0 or MAX_DIVISIONS <= 0 or MAX_FRAMES <= 0 or
            MAX_RECORDS <= 0 or MAX_RECORD_REPORTS <= 0):
        errors.append("unbounded report budget")
    return errors

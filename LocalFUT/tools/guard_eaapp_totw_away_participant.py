#!/usr/bin/env python3
"""Expose the proven TOTW AWAY participant fix in the EA App runner.

The launcher runs the server in network-only mode, so the equivalent legacy
embedded hook is not injected. Offline Select mode 1008 otherwise discards the
complete public TOTW record because its private classification byte remains
zero. This guard marks only the uniquely matched selected TOTW record before
the retail cache search.
"""

from __future__ import annotations

import hashlib
import json
import struct


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_PRODUCT_VERSION = "19.0.4052077.0"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)
BUILDER_RVA = 0x48B50
CACHE_SEARCH_RVA = 0x48CAE
BUILDER_SIGNATURE = (
    "40555657415641574883ec5048c7442430feffffff48899c2480000000488bd9"
)
CACHE_SEARCH_SIGNATURE = (
    "488d97207100004c8b42304c3b42387443498b08488bc148c1e82041807830007410"
    "3983340100007508398b3801000074094981c0f0000000ebd0894c24288944242045"
    "33c9488d942490000000e89f100000"
)
MAX_EVENTS = 12


def agent_source() -> str:
    """Return the fingerprint-gated, mode-1008 record marker."""
    return r"""
'use strict';
const moduleName = %s;
const builderRva = %d;
const cacheSearchRva = %d;
const builderSignature = %s;
const cacheSearchSignature = %s;
const maxEvents = %d;
let events = 0;

function emit(payload) {
  if (events >= maxEvents) return;
  events++;
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

function writable(address) {
  try {
    const range = Process.findRangeByAddress(address);
    return range !== null && range.protection.indexOf('w') !== -1;
  } catch (_) { return false; }
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
  const builder = cards.base.add(builderRva);
  const cacheSearch = cards.base.add(cacheSearchRva);
  const builderOk = matches(builder, builderSignature);
  const cacheSearchOk = matches(cacheSearch, cacheSearchSignature);
  if (!builderOk || !cacheSearchOk) {
    send({event: 'refused', reason: 'signature-mismatch',
      builderOk: builderOk, cacheSearchOk: cacheSearchOk});
    return;
  }
  Interceptor.attach(cacheSearch, {
    onEnter() {
      try {
        const controller = this.context.rbx;
        const service = this.context.rdi;
        if (!readable(controller, 0x140) || !readable(service, 0x7160)) return;
        const mode = controller.add(0x130).readS32();
        if (mode !== 1008) return;
        const ownerHigh = controller.add(0x134).readU32();
        const ownerLow = controller.add(0x138).readU32();
        const ownerIsPublic = ownerHigh === 0x7fffffff && ownerLow === 0xffffffff;
        const ownerIsLocal = ownerHigh === 0 && ownerLow === 1000019;
        const selectedSquadId = controller.add(0x13c).readU32();
        if ((!ownerIsPublic && !ownerIsLocal) || selectedSquadId < 500001 ||
            selectedSquadId > 500099) return;

        const vector = service.add(0x7120);
        const begin = vector.add(0x30).readPointer();
        const end = vector.add(0x38).readPointer();
        if (begin.isNull() || end.isNull() || end.compare(begin) < 0) return;
        const bytes = end.sub(begin).toInt32();
        if (bytes <= 0 || bytes %% 0xf0 !== 0 || bytes > 0xf0 * 64 ||
            !readable(begin, bytes)) return;

        const publicMatches = [];
        const localMatches = [];
        for (let offset = 0; offset < bytes; offset += 0xf0) {
          const record = begin.add(offset);
          const recordOwnerLow = record.readU32();
          const recordOwnerHigh = record.add(4).readU32();
          const recordIsPublic = recordOwnerHigh === 0x7fffffff &&
            recordOwnerLow === 0xffffffff;
          const recordIsLocal = recordOwnerHigh === 0 &&
            recordOwnerLow === 1000019;
          if (!recordIsPublic && !recordIsLocal) continue;
          if (record.add(0x94).readU32() !== 1 ||
              record.add(0x8c).readU32() !== 1 ||
              record.add(0x98).readU32() !== 14 ||
              record.add(0xa8).readU32() !== 15) continue;

          const summariesBegin = record.add(0xb8).readPointer();
          const summariesEnd = record.add(0xc0).readPointer();
          if (summariesBegin.isNull() || summariesEnd.isNull() ||
              summariesEnd.compare(summariesBegin) < 0) continue;
          const summaryBytes = summariesEnd.sub(summariesBegin).toInt32();
          if (summaryBytes <= 0 || summaryBytes %% 0x40 !== 0 ||
              summaryBytes > 0x40 * 64 ||
              !readable(summariesBegin, summaryBytes)) continue;
          let found = false;
          for (let summaryOffset = 0; summaryOffset < summaryBytes;
               summaryOffset += 0x40) {
            const summary = summariesBegin.add(summaryOffset);
            const rating = summary.add(0x30).readU32();
            if (summary.readU32() === selectedSquadId && rating >= 70 &&
                rating <= 99 && summary.add(0x34).readU32() === 100) {
              found = true;
              break;
            }
          }
          if (found)
            (recordIsPublic ? publicMatches : localMatches).push(record);
        }

        let selected = null;
        if (publicMatches.length === 1) selected = publicMatches[0];
        else if (publicMatches.length === 0 && localMatches.length === 1)
          selected = localMatches[0];
        if (selected === null) {
          emit({event: 'totw-away-participant', status: 'record-mismatch',
            selectedSquadId: selectedSquadId,
            publicMatches: publicMatches.length,
            localMatches: localMatches.length, records: bytes / 0xf0});
          return;
        }

        const target = selected.add(0x30);
        const previous = target.readU8();
        if (previous === 0) {
          if (!writable(target)) {
            emit({event: 'totw-away-participant', status: 'not-writable',
              selectedSquadId: selectedSquadId});
            return;
          }
          target.writeU8(1);
        }
        emit({event: 'totw-away-participant',
          status: previous === 0 ? 'applied' : 'already-classified',
          selectedSquadId: selectedSquadId, previous: previous,
          current: target.readU8(), records: bytes / 0xf0});
      } catch (error) {
        emit({event: 'totw-away-participant', status: 'record-error',
          error: String(error)});
      }
    }
  });
  send({event: 'armed', guard: 'totw-away-participant', module: cards.name});
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

send({event: 'armed', guard: 'totw-away-participant',
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        BUILDER_RVA,
        CACHE_SEARCH_RVA,
        json.dumps(list(bytes.fromhex(BUILDER_SIGNATURE))),
        json.dumps(list(bytes.fromhex(CACHE_SEARCH_SIGNATURE))),
        MAX_EVENTS,
    )


def image_errors(image: bytes, product_version: str) -> list[str]:
    """Return why this guard must not arm against an image."""
    errors = []
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

    for name, rva, signature in (
        ("builder", BUILDER_RVA, BUILDER_SIGNATURE),
        ("cache search", CACHE_SEARCH_RVA, CACHE_SEARCH_SIGNATURE),
    ):
        wanted = bytes.fromhex(signature)
        offset = offset_of(rva)
        if image[offset:offset + len(wanted)] != wanted:
            errors.append("%s signature mismatch at 0x%x" % (name, rva))
        if image.count(wanted) != 1:
            errors.append("%s signature is not unique" % name)
    return errors

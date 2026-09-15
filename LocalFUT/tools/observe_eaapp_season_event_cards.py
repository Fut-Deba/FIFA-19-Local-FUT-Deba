#!/usr/bin/env python3
"""Observe the native Season event-card lookup without changing the client.

RC138 proved that all six Season rows decode while their titles stay blank.
The tile builder resolves a Season/type pair through the registry scanned at
``CardsDLL+0x1c88a2`` and stores the resolved event-card at ``+0x1c8a7b``.
This bounded observer records those ids and the event-card asset id so the
next title correction is based on the client's real registry, not item 0.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)
LOOKUP_RVA = 0x1C88A2
LOOKUP_SIGNATURE = "498b88980100004d8b88a0010000493b"
RESOLVED_RVA = 0x1C8A7B
RESOLVED_SIGNATURE = "498947104883c478415f415e415d415c"
MAX_REPORTS = 24
MAX_ENTRIES = 32

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "writeU8", "writeU32", "writeS32", "writePointer",
    "writeByteArray", "retval.replace", "NativeFunction", "Stalker.",
)


def agent_source() -> str:
    """Return the signature-gated read-only Season event-card observer."""
    return r"""
'use strict';
const moduleName = %s;
const lookupRva = %d;
const lookupSignature = %s;
const resolvedRva = %d;
const resolvedSignature = %s;
const maxReports = %d;
const maxEntries = %d;
const pending = {};
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

function signatureMatches(site, expected) {
  try {
    const actual = new Uint8Array(site.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++)
      if (actual[index] !== expected[index]) return false;
    return true;
  } catch (_) { return false; }
}

function attach(cards) {
  const lookup = cards.base.add(lookupRva);
  const resolved = cards.base.add(resolvedRva);
  if (!signatureMatches(lookup, lookupSignature) ||
      !signatureMatches(resolved, resolvedSignature)) {
    send({event: 'refused', reason: 'season-event-card-signature-mismatch'});
    return;
  }
  Interceptor.attach(lookup, {
    onEnter() {
      if (reports >= maxReports) return;
      const registry = this.context.r8;
      const request = this.context.r15;
      if (!readable(registry, 0x1a8) || !readable(request, 12)) return;
      const report = {event: 'season-event-card-registry',
        requestedSeasonId: request.add(8).readU32(),
        requestedType: this.context.rbp.toInt32(), entries: [], matched: false};
      try {
        const begin = registry.add(0x198).readPointer();
        const end = registry.add(0x1a0).readPointer();
        const bytes = end.sub(begin).toInt32();
        const pointerSize = Process.pointerSize;
        if (bytes < 0 || bytes %% pointerSize !== 0 ||
            bytes > pointerSize * 256 || !readable(begin, bytes)) {
          report.registryCount = -1;
        } else {
          report.registryCount = bytes / pointerSize;
          for (let offset = 0; offset < bytes &&
               report.entries.length < maxEntries; offset += pointerSize) {
            const record = begin.add(offset).readPointer();
            if (!readable(record, 0xa0)) continue;
            const row = {seasonId: record.readU32(),
              type: record.add(4).readU32(),
              assetId: record.add(0x9c).readU32()};
            report.entries.push(row);
            if (row.seasonId === report.requestedSeasonId &&
                row.type === report.requestedType) report.matched = true;
          }
        }
      } catch (error) { report.error = String(error); }
      reports++;
      pending[String(Process.getCurrentThreadId())] = report;
      send(report);
    }
  });
  Interceptor.attach(resolved, {
    onEnter() {
      const key = String(Process.getCurrentThreadId());
      const request = pending[key];
      if (!request) return;
      delete pending[key];
      const record = this.context.rax;
      const result = {event: 'season-event-card-resolved',
        requestedSeasonId: request.requestedSeasonId,
        requestedType: request.requestedType,
        fallback: !request.matched, resolvedSeasonId: null,
        resolvedType: null, assetId: null};
      if (readable(record, 0xa0)) {
        result.resolvedSeasonId = record.readU32();
        result.resolvedType = record.add(4).readU32();
        result.assetId = record.add(0x9c).readU32();
      }
      send(result);
    }
  });
  send({event: 'armed', observer: 'season-event-cards', module: cards.name});
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

send({event: 'armed', observer: 'season-event-cards',
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME), LOOKUP_RVA,
        json.dumps(list(bytes.fromhex(LOOKUP_SIGNATURE))), RESOLVED_RVA,
        json.dumps(list(bytes.fromhex(RESOLVED_SIGNATURE))), MAX_REPORTS,
        MAX_ENTRIES,
    )


def self_test_errors() -> list[str]:
    errors = []
    source = agent_source()
    for token in FORBIDDEN_AGENT_TOKENS:
        if token in source:
            errors.append("forbidden agent primitive: %s" % token)
    if MAX_REPORTS <= 0 or MAX_ENTRIES <= 0:
        errors.append("unbounded report budget")
    return errors

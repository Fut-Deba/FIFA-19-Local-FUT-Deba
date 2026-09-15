#!/usr/bin/env python3
"""Observe the EA App offline-Season load response without changing it."""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)
FACTORY_RVA = 0x23ADF0
FACTORY_SIGNATURE = tuple(bytes.fromhex(
    "48894c24084883ec3848c7442420feffffff33c089442448488d442448"
    "4889442450488b4908488b014533c94c8d0585451400418d5170ff5010"
    "48894424584885c0740e488bc8e8c45ee2ff"
))
LOAD_HANDLER_RVA = 0x23AE50
LOAD_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "4c8bdc4881ec9800000049c74388feffffffc64138008b420889442448"
    "488d05842c1200498943a88b4210894424500fb7421466894424548b4218"
    "89442458488d05fa010000"
))
COPY_RVA = 0x23AF20
COPY_SIGNATURE = tuple(bytes.fromhex(
    "4154415641574883ec3048c7442420feffffff48895c245048896c2458"
    "488974246048897c2468488bea488bf1488b420848894108488b421048894110"
    "8b42188941188b421c89411c8b42208941200fb64224884124"
))
PARSER_RVA = 0x23B2C0
PARSER_SIGNATURE = tuple(bytes.fromhex(
    "405556574154415541564157488d6c24904881ec7001000048c7442430"
    "feffffff48899c24c0010000488b05180d1f004833c4"
))
COMPLETION_RVA = 0x2A4840
COMPLETION_SIGNATURE = tuple(bytes.fromhex(
    "4055535657415441564157488d6c24e14881ec9000000048c745dffeffffff"
))
DATA_START_OFFSET = 0x28
DATA_END_OFFSET = 0x30
DIVISION_ID_OFFSET = 0x58
SEASON_ID_OFFSET = 0x5C
ROUND_OFFSET = 0x60
USER_POINTS_OFFSET = 0x64
DATA_VERSION_OFFSET = 0x68
RESPONSE_SIZE = 0x70
MAX_DATA_BYTES = 256
MAX_REPORTS = 16
MODULE_POLL_ATTEMPTS = 900
MODULE_POLL_INTERVAL_MS = 1000

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "writeU8", "writeU32", "writeS32",
    "writePointer", "writeByteArray", "retval.replace", "NativeFunction",
    "Stalker.",
)


def agent_source() -> str:
    """Return the bounded, signature-gated read-only observer."""
    return r"""
'use strict';
const moduleName = %s;
const sites = %s;
const dataStartOffset = %d;
const dataEndOffset = %d;
const divisionIdOffset = %d;
const seasonIdOffset = %d;
const roundOffset = %d;
const userPointsOffset = %d;
const dataVersionOffset = %d;
const responseSize = %d;
const maxDataBytes = %d;
const maxReports = %d;
const pollAttempts = %d;
const pollIntervalMs = %d;
let reports = 0;

function emit(payload) {
  if (reports >= maxReports) return;
  reports++;
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

function responseSnapshot(response) {
  const result = {address: String(response)};
  try {
    if (!readable(response, responseSize)) {
      result.error = 'response-unreadable';
      return result;
    }
    const start = response.add(dataStartOffset).readPointer();
    const end = response.add(dataEndOffset).readPointer();
    let dataBytes = null;
    if (start.isNull() && end.isNull()) dataBytes = 0;
    else if (!start.isNull() && end.compare(start) >= 0) {
      const size = end.sub(start).toInt32();
      if (size >= 0 && size <= maxDataBytes &&
          (size === 0 || readable(start, size))) dataBytes = size;
    }
    result.dataBytes = dataBytes;
    result.divisionId = response.add(divisionIdOffset).readS16();
    result.seasonId = response.add(seasonIdOffset).readS32();
    result.internalRound = response.add(roundOffset).readS16();
    result.wireRound = result.internalRound >= 0 ? result.internalRound + 1 : null;
    result.userPoints = response.add(userPointsOffset).readS32();
    result.dataVersion = response.add(dataVersionOffset).readU8();
  } catch (error) { result.error = String(error); }
  return result;
}

function attach(cards) {
  for (let index = 0; index < sites.length; index++) {
    if (!matches(cards.base.add(sites[index].rva), sites[index].signature)) {
      send({event: 'refused', reason: 'season-load-signature-mismatch',
        rva: sites[index].rva});
      return;
    }
  }
  Interceptor.attach(cards.base.add(sites[0].rva), {
    onLeave(retval) {
      emit({event: 'season-load-response-created',
        response: responseSnapshot(retval)});
    }
  });
  Interceptor.attach(cards.base.add(sites[1].rva), {
    onEnter(args) {
      emit({event: 'season-load-handler-enter', handler: String(args[0]),
        eventData: String(args[1]), returnRva: rva(cards, this.returnAddress)});
    }
  });
  Interceptor.attach(cards.base.add(sites[2].rva), {
    onEnter(args) {
      this.destination = args[0];
      this.source = args[1];
    },
    onLeave() {
      emit({event: 'season-load-response-copied',
        source: responseSnapshot(this.source),
        destination: responseSnapshot(this.destination)});
    }
  });
  Interceptor.attach(cards.base.add(sites[3].rva), {
    onEnter(args) {
      this.response = args[0];
      this.returnRva = rva(cards, this.returnAddress);
    },
    onLeave(retval) {
      emit({event: 'season-load-response-parsed', result: retval.toInt32(),
        returnRva: this.returnRva,
        response: responseSnapshot(this.response)});
    }
  });
  Interceptor.attach(cards.base.add(sites[4].rva), {
    onEnter(args) {
      emit({event: 'season-load-completion-enter', controller: String(args[0]),
        responseState: String(args[1]), result: args[3].toInt32(),
        returnRva: rva(cards, this.returnAddress)});
    }
  });
  send({event: 'armed', observer: 'season-selection', module: cards.name});
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

send({event: 'armed', observer: 'season-selection',
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps([
            {"rva": rva, "signature": list(signature)}
            for rva, signature in (
                (FACTORY_RVA, FACTORY_SIGNATURE),
                (LOAD_HANDLER_RVA, LOAD_HANDLER_SIGNATURE),
                (COPY_RVA, COPY_SIGNATURE),
                (PARSER_RVA, PARSER_SIGNATURE),
                (COMPLETION_RVA, COMPLETION_SIGNATURE),
            )
        ]),
        DATA_START_OFFSET,
        DATA_END_OFFSET,
        DIVISION_ID_OFFSET,
        SEASON_ID_OFFSET,
        ROUND_OFFSET,
        USER_POINTS_OFFSET,
        DATA_VERSION_OFFSET,
        RESPONSE_SIZE,
        MAX_DATA_BYTES,
        MAX_REPORTS,
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
    if MAX_REPORTS <= 0 or MAX_DATA_BYTES <= 0:
        errors.append("unbounded report budget")
    return errors

#!/usr/bin/env python3
"""Keep the proved EA-App FUT Champions request on the CPU match path.

RC106 captured the mode-1012 builder setting request byte ``+0x2D`` to one;
the serializer then emits ``type=ONLINE`` and FIFA enters Blaze matchmaking.
This guard owns only the instruction immediately after that Champions-only
write.  A non-mutating host receipt must confirm the local run before the
request byte is changed to the native offline value.  Module code is never
patched.
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

# +0x1F8642 is reached only by the captured Champions request branch and sets
# request+0x2D to ONLINE.  Hooking the following instruction means the stock
# write has completed, while +0x2F has not yet been changed by that instruction.
MATCH_TYPE_WRITE_RVA = 0x1F8642
MATCH_TYPE_GUARD_RVA = 0x1F8646
MATCH_TYPE_GUARD_SIGNATURE = tuple(bytes.fromhex(
    "c6472f01488b05574623004c8b4030"
))
REQUEST_TYPE_OFFSET = 0x2D
REQUEST_SECONDARY_FLAG_OFFSET = 0x2F
ONLINE_TYPE_VALUE = 1
OFFLINE_TYPE_VALUE = 0
SECONDARY_FLAG_BEFORE_WRITE = 0

MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900
MAX_EVENTS = 12


def _pe_layout(image: bytes) -> tuple[int, int]:
    """Return the section count and section-table offset of a PE image."""
    try:
        pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
        if image[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        section_count = struct.unpack_from("<H", image, pe_offset + 6)[0]
        optional_size = struct.unpack_from("<H", image, pe_offset + 20)[0]
        return section_count, pe_offset + 24 + optional_size
    except (IndexError, struct.error) as error:
        raise ValueError("invalid CardsDLL PE image") from error


def _file_offset(image: bytes, rva: int) -> int:
    section_count, table = _pe_layout(image)
    try:
        for index in range(section_count):
            entry = table + index * 40
            virtual_size, virtual_address, raw_size, raw_pointer = (
                struct.unpack_from("<IIII", image, entry + 8)
            )
            if virtual_address <= rva < virtual_address + max(
                    virtual_size, raw_size):
                return raw_pointer + rva - virtual_address
    except struct.error as error:
        raise ValueError("invalid CardsDLL section table") from error
    raise ValueError("CardsDLL RVA 0x%x is outside every section" % rva)


def _bytes_at(image: bytes, rva: int, size: int) -> bytes:
    offset = _file_offset(image, rva)
    value = image[offset:offset + size]
    if len(value) != size:
        raise ValueError("CardsDLL RVA 0x%x is truncated" % rva)
    return value


def validate_cards_image(image: bytes, product_version: str) -> None:
    """Refuse every image outside the one live-observed request contract."""
    if product_version != EXPECTED_PRODUCT_VERSION:
        raise ValueError("CardsDLL product version mismatch")
    if hashlib.sha256(image).hexdigest() != EXPECTED_CARDS_SHA256:
        raise ValueError("CardsDLL fingerprint mismatch")
    signature = bytes(MATCH_TYPE_GUARD_SIGNATURE)
    if _bytes_at(image, MATCH_TYPE_GUARD_RVA, len(signature)) != signature:
        raise ValueError("Champions CPU match guard signature mismatch")
    if image.count(signature) != 1:
        raise ValueError("Champions CPU match guard signature is not unique")


def guarded_match_type(current: int, secondary: int,
                       receipt: dict | None) -> int:
    """Model the fail-closed request-byte decision used by the live guard."""
    result = dict(receipt or {})
    valid_receipt = (
        result.get("status") == "confirmed" and
        result.get("offlineRegistered") is True and
        str(result.get("state", "")).upper() == "READY_FOR_MATCH" and
        int(result.get("difficulty", 0) or 0) in range(1, 8)
    )
    if (int(current) == ONLINE_TYPE_VALUE and
            int(secondary) == SECONDARY_FLAG_BEFORE_WRITE and valid_receipt):
        return OFFLINE_TYPE_VALUE
    return int(current)


def agent_source() -> str:
    """Return the exact-site, host-confirmed Frida request guard."""
    return r"""
'use strict';
const moduleName = %s;
const guardRva = %d;
const guardSignature = %s;
const requestTypeOffset = %d;
const secondaryFlagOffset = %d;
const onlineTypeValue = %d;
const offlineTypeValue = %d;
const secondaryFlagBeforeWrite = %d;
const pollIntervalMs = %d;
const pollAttempts = %d;
const maxEvents = %d;
let nextRequestId = 1;
let events = 0;

function matches(address, expected) {
  try {
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++)
      if (actual[index] !== expected[index]) return false;
    return true;
  } catch (_) {
    return false;
  }
}

function emit(payload) {
  if (events >= maxEvents) return;
  events++;
  send(payload);
}

function attach(cards) {
  const site = cards.base.add(guardRva);
  if (!matches(site, guardSignature)) {
    send({event: 'refused',
      reason: 'Champions CPU match guard signature mismatch'});
    return;
  }
  Interceptor.attach(site, {
    onEnter() {
      const request = this.context.rdi;
      let current = null;
      let secondary = null;
      try {
        current = request.add(requestTypeOffset).readU8();
        secondary = request.add(secondaryFlagOffset).readU8();
      } catch (_) {
        emit({event: 'fut-champions-cpu-match-refused',
          reason: 'request-flags-unreadable'});
        return;
      }
      // The hook sits between the two stock byte writes. Refuse any hit that
      // is not in exactly that transient state, even on the fingerprinted DLL.
      if (current !== onlineTypeValue ||
          secondary !== secondaryFlagBeforeWrite) {
        emit({event: 'fut-champions-cpu-match-refused',
          reason: 'unexpected-request-flags', current: current,
          secondary: secondary});
        return;
      }

      const requestId = nextRequestId++;
      let response = null;
      const pending = recv('fut-champions-cpu-match-result',
        function (message) {
          const candidate = message.payload || {};
          if (Number(candidate.requestId || 0) === requestId)
            response = candidate;
        });
      send({event: 'fut-champions-cpu-match-request',
        requestId: requestId, current: current, secondary: secondary});
      // The native UI thread may continue only after the local server has
      // confirmed the already-persisted offline run without mutating it.
      pending.wait();
      const confirmed = response !== null &&
        String(response.status || '') === 'confirmed' &&
        response.offlineRegistered === true &&
        String(response.state || '').toUpperCase() === 'READY_FOR_MATCH' &&
        Number(response.difficulty || 0) >= 1 &&
        Number(response.difficulty || 0) <= 7;
      if (!confirmed) {
        emit({event: 'fut-champions-cpu-match-refused', requestId: requestId,
          reason: response === null ? 'missing-host-reply' :
            String(response.reason || 'invalid-host-receipt')});
        return;
      }

      let beforeWrite = null;
      let secondaryBeforeWrite = null;
      try {
        beforeWrite = request.add(requestTypeOffset).readU8();
        secondaryBeforeWrite = request.add(secondaryFlagOffset).readU8();
      } catch (_) {
        emit({event: 'fut-champions-cpu-match-refused', requestId: requestId,
          reason: 'request-flags-unreadable-after-reply'});
        return;
      }
      if (beforeWrite !== onlineTypeValue ||
          secondaryBeforeWrite !== secondaryFlagBeforeWrite) {
        emit({event: 'fut-champions-cpu-match-refused', requestId: requestId,
          reason: 'request-flags-changed-while-waiting',
          current: beforeWrite, secondary: secondaryBeforeWrite});
        return;
      }
      try {
        request.add(requestTypeOffset).writeU8(offlineTypeValue);
        const verified = request.add(requestTypeOffset).readU8();
        if (verified !== offlineTypeValue) {
          emit({event: 'fut-champions-cpu-match-refused',
            requestId: requestId, reason: 'request-write-verification-failed',
            current: verified});
          return;
        }
        emit({event: 'fut-champions-cpu-match-applied',
          requestId: requestId, eventId: Number(response.eventId || 0),
          difficulty: Number(response.difficulty || 0),
          from: onlineTypeValue, to: offlineTypeValue});
      } catch (_) {
        emit({event: 'fut-champions-cpu-match-refused', requestId: requestId,
          reason: 'request-write-failed'});
      }
    }
  });
  send({event: 'fut-champions-cpu-match-guard-attached',
    module: moduleName, guardRva: '0x' + guardRva.toString(16)});
}

let pollsLeft = pollAttempts;
function waitForModule() {
  let cards = null;
  try { cards = Process.findModuleByName(moduleName); }
  catch (_) { cards = null; }
  if (cards !== null) {
    attach(cards);
    return;
  }
  pollsLeft--;
  if (pollsLeft <= 0) {
    send({event: 'refused', reason: 'CardsDLL module absent',
      module: moduleName});
    return;
  }
  setTimeout(waitForModule, pollIntervalMs);
}

send({event: 'armed', guard: 'fut-champions-cpu-match',
  module: moduleName, state: 'waiting-for-module',
  pollIntervalMs: pollIntervalMs, pollAttempts: pollAttempts});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        MATCH_TYPE_GUARD_RVA,
        json.dumps(list(MATCH_TYPE_GUARD_SIGNATURE)),
        REQUEST_TYPE_OFFSET,
        REQUEST_SECONDARY_FLAG_OFFSET,
        ONLINE_TYPE_VALUE,
        OFFLINE_TYPE_VALUE,
        SECONDARY_FLAG_BEFORE_WRITE,
        MODULE_POLL_INTERVAL_MS,
        MODULE_POLL_ATTEMPTS,
        MAX_EVENTS,
    )

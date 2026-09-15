#!/usr/bin/env python3
"""Observe the EA-App FUT Champions CreateMatch-to-match transition boundary.

RC105 proves that the local server returns opponent one and then receives
Blaze c4/k16, but none of the earlier action/provider/matchmaking probes fires.
This observer therefore also records the exact mode-1012 request flag,
champion-id copy and state-commit boundaries.  It is intentionally separate
from the Registration observer because the active Registration guard already
owns its navigation instructions.
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

CHAMPIONS_MODE = 1012
CREATE_MATCH_PROVIDER = 0x7564
MAX_CAPTURES = 24
MAX_REQUEST_BOUNDARY_CAPTURES = 12
MAX_RESPONSE_BOUNDARY_CAPTURES = 24
CHAMPIONS_WINDOW_MS = 15000
MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900

# futonlineselectteamviewmodel's secondary event handler is reached through
# vtable slot 0x36A5F0.  Its mode-1012 generic branch invokes controller action
# 2 at 0x128690 before the CreateMatch provider is requested.
ONLINE_EVENT_HANDLER_RVA = 0x128260
ONLINE_EVENT_HANDLER_POINTER_SLOT_RVA = 0x36A5F0
ONLINE_EVENT_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "40555657415641574883ec6048c7442420feffffff48899c2490000000488bf1"
))
ONLINE_ACTION_TWO_RVA = 0x128690
ONLINE_ACTION_TWO_SIGNATURE = tuple(bytes.fromhex(
    "488b8ee8feffff488b014533c0418d50"
))

# The primary vtable handler dispatches provider 0x7564 and publishes the
# shared MATCH_CREATED property.  The offline publisher is recorded only as a
# comparison point; it is never invoked or redirected by this observer.
ONLINE_PROVIDER_HANDLER_RVA = 0x1291A0
ONLINE_PROVIDER_HANDLER_POINTER_SLOT_RVA = 0x36A5B8
ONLINE_PROVIDER_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "488bc4574881ec6002000048c7442438"
))
ONLINE_MATCH_CREATED_RVA = 0x1292E1
ONLINE_MATCH_CREATED_SIGNATURE = tuple(bytes.fromhex(
    "488d1518272300488bcbff503890488b"
))
OFFLINE_MATCH_CREATED_RVA = 0x47504
OFFLINE_MATCH_CREATED_SIGNATURE = tuple(bytes.fromhex(
    "488d15f5443100488bcfff503890488b"
))
MATCH_CREATED_STRING_RVA = 0x35BA00
MATCH_CREATED_STRING = "MATCH_CREATED"

# Search Opponent is represented by futmatchmakingviewmodel.  Its registered
# factory at 0x83A60 calls the constructor at 0x830D0.  Capturing both with a
# bounded CardsDLL-relative backtrace identifies the first native transition
# after action 2 without guessing a navigation route.
MATCHMAKING_NAME_RVA = 0x359D18
MATCHMAKING_NAME = "futmatchmakingviewmodel"
MATCHMAKING_FACTORY_RVA = 0x83A60
MATCHMAKING_FACTORY_SIGNATURE = tuple(bytes.fromhex(
    "4c89442418574883ec4048c7442420feffffff48895c24504d8bd0488bda"
    "488bf933c089442468488d4424684889442428498b0041b9010000004c8d"
    "05a73a2d"
))
MATCHMAKING_CONSTRUCTOR_RVA = 0x830D0
MATCHMAKING_CONSTRUCTOR_SIGNATURE = tuple(bytes.fromhex(
    "48894c240856574154415641574883ec3048c7442420feffffff48895c2468"
    "48896c2470488be9e8"
))
MATCHMAKING_FACTORY_CALL_RVA = 0x83ABF
MATCHMAKING_FACTORY_CALL_SIGNATURE = tuple(bytes.fromhex("e80cf6ffff"))

# The mode-1012 request builder writes its two flags, copies championId from
# context +0xFA8 into request +0x1C, then commits the request flags into the
# owner state.  These instruction sites are observed directly because RC105
# received POST /match without crossing any of the older transition hooks.
CHAMPIONS_REQUEST_FLAGS_RVA = 0x1F8642
CHAMPIONS_REQUEST_FLAGS_SIGNATURE = tuple(bytes.fromhex(
    "c6472d01c6472f01488b05574623004c"
))
CHAMPIONS_CONTEXT_READ_RVA = 0x1F86B5
CHAMPIONS_CONTEXT_READ_SIGNATURE = tuple(bytes.fromhex(
    "8b80a80f000089471c488b03"
))
CHAMPIONS_STATE_COMMIT_RVA = 0x1F87B6
CHAMPIONS_STATE_COMMIT_SIGNATURE = tuple(bytes.fromhex(
    "0fb6472d418887326e00000fb6472e"
))

# The mode-1012 request uses the generic CreateMatch service whose primary
# vtable starts at 0x37B998.  Its response factory installs the separate
# FutCreateMatchServerResponse vtable at 0x37BA58.  The parser and service
# completion are distinct stages; observing both is necessary because RC106
# proved that the valid HTTP response is followed by online matchmaking but
# did not keep its response-side observer window open.
CREATE_MATCH_SERVICE_VTABLE_RVA = 0x37B998
CREATE_MATCH_SERVICE_EVENT_HANDLER_SLOT_RVA = 0x37B9E0
CREATE_MATCH_SERVICE_RESPONSE_FACTORY_SLOT_RVA = 0x37BA20
CREATE_MATCH_SERVICE_COMPLETION_SLOT_RVA = 0x37BA30
CREATE_MATCH_RESPONSE_VTABLE_RVA = 0x37BA58
CREATE_MATCH_RESPONSE_PARSER_SLOT_RVA = 0x37BA60
CREATE_MATCH_RESPONSE_FACTORY_RVA = 0x211510
CREATE_MATCH_SERVICE_COMPLETION_RVA = 0x211570
CREATE_MATCH_SERVICE_EVENT_HANDLER_RVA = 0x2115D0
CREATE_MATCH_SERVICE_PUBLISH_RVA = 0x2116B9
CREATE_MATCH_RESPONSE_PARSER_RVA = 0x212170
CREATE_MATCH_RESPONSE_FACTORY_SIGNATURE = tuple(bytes.fromhex(
    "4883ec28488b49084c8d05b9a316004533c9488b01418d51"
))
CREATE_MATCH_SERVICE_COMPLETION_SIGNATURE = tuple(bytes.fromhex(
    "48895c2408574883ec20488b01488bfab201488bd9ff5070"
))
CREATE_MATCH_SERVICE_EVENT_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "48895c240848896c24104889742418574883ec20488b018bda33d2488bf9ff50"
))
CREATE_MATCH_SERVICE_PUBLISH_SIGNATURE = tuple(bytes.fromhex(
    "ba48750000488b01ff501048ffc7488d"
))
CREATE_MATCH_RESPONSE_PARSER_SIGNATURE = tuple(bytes.fromhex(
    "405556574154415541564157488d6c24804881ec8001000048c7442428feffff"
))

def _pe_layout(image: bytes) -> tuple[int, int, int]:
    try:
        pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
        if image[pe_offset:pe_offset + 4] != b"PE\0\0":
            raise ValueError("missing PE signature")
        section_count = struct.unpack_from("<H", image, pe_offset + 6)[0]
        optional_size = struct.unpack_from("<H", image, pe_offset + 20)[0]
        optional = pe_offset + 24
        image_base = struct.unpack_from("<Q", image, optional + 24)[0]
        return image_base, section_count, optional + optional_size
    except (IndexError, struct.error) as error:
        raise ValueError("invalid CardsDLL PE image") from error


def _file_offset(image: bytes, rva: int) -> int:
    _, section_count, table = _pe_layout(image)
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
    """Refuse any image outside the exact RC97 live-tested client build."""
    if product_version != EXPECTED_PRODUCT_VERSION:
        raise ValueError("CardsDLL product version mismatch")
    if hashlib.sha256(image).hexdigest() != EXPECTED_CARDS_SHA256:
        raise ValueError("CardsDLL fingerprint mismatch")

    sites = (
        (ONLINE_EVENT_HANDLER_RVA, ONLINE_EVENT_HANDLER_SIGNATURE,
         "online event handler"),
        (ONLINE_ACTION_TWO_RVA, ONLINE_ACTION_TWO_SIGNATURE,
         "online action 2"),
        (ONLINE_PROVIDER_HANDLER_RVA, ONLINE_PROVIDER_HANDLER_SIGNATURE,
         "online provider handler"),
        (ONLINE_MATCH_CREATED_RVA, ONLINE_MATCH_CREATED_SIGNATURE,
         "online MATCH_CREATED"),
        (OFFLINE_MATCH_CREATED_RVA, OFFLINE_MATCH_CREATED_SIGNATURE,
         "offline MATCH_CREATED"),
        (MATCHMAKING_FACTORY_RVA, MATCHMAKING_FACTORY_SIGNATURE,
         "matchmaking factory"),
        (MATCHMAKING_CONSTRUCTOR_RVA, MATCHMAKING_CONSTRUCTOR_SIGNATURE,
         "matchmaking constructor"),
        (MATCHMAKING_FACTORY_CALL_RVA, MATCHMAKING_FACTORY_CALL_SIGNATURE,
         "matchmaking constructor call"),
        (CHAMPIONS_REQUEST_FLAGS_RVA, CHAMPIONS_REQUEST_FLAGS_SIGNATURE,
         "request flags"),
        (CHAMPIONS_CONTEXT_READ_RVA, CHAMPIONS_CONTEXT_READ_SIGNATURE,
         "context read"),
            (CHAMPIONS_STATE_COMMIT_RVA, CHAMPIONS_STATE_COMMIT_SIGNATURE,
             "state commit"),
            (CREATE_MATCH_RESPONSE_FACTORY_RVA,
             CREATE_MATCH_RESPONSE_FACTORY_SIGNATURE,
             "CreateMatch response factory"),
            (CREATE_MATCH_SERVICE_COMPLETION_RVA,
             CREATE_MATCH_SERVICE_COMPLETION_SIGNATURE,
             "CreateMatch service completion"),
            (CREATE_MATCH_SERVICE_EVENT_HANDLER_RVA,
             CREATE_MATCH_SERVICE_EVENT_HANDLER_SIGNATURE,
             "CreateMatch service event handler"),
            (CREATE_MATCH_SERVICE_PUBLISH_RVA,
             CREATE_MATCH_SERVICE_PUBLISH_SIGNATURE,
             "CreateMatch service 0x7548 publish"),
            (CREATE_MATCH_RESPONSE_PARSER_RVA,
             CREATE_MATCH_RESPONSE_PARSER_SIGNATURE,
             "CreateMatch response parser"),
    )
    for rva, expected, label in sites:
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError("Champions %s signature mismatch" % label)
        if len(signature) >= 12 and image.count(signature) != 1:
            raise ValueError("Champions %s signature is not unique" % label)

    for rva, value in (
            (MATCH_CREATED_STRING_RVA, MATCH_CREATED_STRING),
            (MATCHMAKING_NAME_RVA, MATCHMAKING_NAME)):
        encoded = value.encode("ascii") + b"\0"
        if _bytes_at(image, rva, len(encoded)) != encoded:
            raise ValueError("Champions string mismatch at RVA 0x%x" % rva)

    image_base, _, _ = _pe_layout(image)
    for slot, target, label in (
            (ONLINE_EVENT_HANDLER_POINTER_SLOT_RVA,
             ONLINE_EVENT_HANDLER_RVA, "online event handler"),
            (ONLINE_PROVIDER_HANDLER_POINTER_SLOT_RVA,
             ONLINE_PROVIDER_HANDLER_RVA, "online provider handler")):
        pointer = struct.unpack("<Q", _bytes_at(image, slot, 8))[0]
        if pointer - image_base != target:
            raise ValueError("Champions %s vtable mismatch" % label)

    for slot, target, label in (
            (CREATE_MATCH_SERVICE_EVENT_HANDLER_SLOT_RVA,
             CREATE_MATCH_SERVICE_EVENT_HANDLER_RVA,
             "CreateMatch service event handler"),
            (CREATE_MATCH_SERVICE_RESPONSE_FACTORY_SLOT_RVA,
             CREATE_MATCH_RESPONSE_FACTORY_RVA,
             "CreateMatch response factory"),
            (CREATE_MATCH_SERVICE_COMPLETION_SLOT_RVA,
             CREATE_MATCH_SERVICE_COMPLETION_RVA,
             "CreateMatch service completion"),
            (CREATE_MATCH_RESPONSE_PARSER_SLOT_RVA,
             CREATE_MATCH_RESPONSE_PARSER_RVA,
             "CreateMatch response parser")):
        pointer = struct.unpack("<Q", _bytes_at(image, slot, 8))[0]
        if pointer - image_base != target:
            raise ValueError("Champions %s vtable mismatch" % label)


def agent_source() -> str:
    """Return a fingerprinted, bounded and observation-only Frida agent."""
    sites = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in (
            ("online-action-two", ONLINE_ACTION_TWO_RVA,
             ONLINE_ACTION_TWO_SIGNATURE),
            ("online-match-created", ONLINE_MATCH_CREATED_RVA,
             ONLINE_MATCH_CREATED_SIGNATURE),
            ("offline-match-created", OFFLINE_MATCH_CREATED_RVA,
             OFFLINE_MATCH_CREATED_SIGNATURE),
            ("matchmaking-factory", MATCHMAKING_FACTORY_RVA,
             MATCHMAKING_FACTORY_SIGNATURE),
            ("matchmaking-constructor", MATCHMAKING_CONSTRUCTOR_RVA,
             MATCHMAKING_CONSTRUCTOR_SIGNATURE),
        )
    ])
    request_boundary_sites = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in (
            ("context-read", CHAMPIONS_CONTEXT_READ_RVA,
             CHAMPIONS_CONTEXT_READ_SIGNATURE),
            ("state-commit", CHAMPIONS_STATE_COMMIT_RVA,
             CHAMPIONS_STATE_COMMIT_SIGNATURE),
        )
    ])
    response_boundary_sites = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in (
            ("response-factory", CREATE_MATCH_RESPONSE_FACTORY_RVA,
             CREATE_MATCH_RESPONSE_FACTORY_SIGNATURE),
            ("response-parser", CREATE_MATCH_RESPONSE_PARSER_RVA,
             CREATE_MATCH_RESPONSE_PARSER_SIGNATURE),
            ("service-completion", CREATE_MATCH_SERVICE_COMPLETION_RVA,
             CREATE_MATCH_SERVICE_COMPLETION_SIGNATURE),
            ("service-event-handler", CREATE_MATCH_SERVICE_EVENT_HANDLER_RVA,
             CREATE_MATCH_SERVICE_EVENT_HANDLER_SIGNATURE),
            ("service-publish-7548", CREATE_MATCH_SERVICE_PUBLISH_RVA,
             CREATE_MATCH_SERVICE_PUBLISH_SIGNATURE),
        )
    ])
    strings = json.dumps([
        {"rva": rva, "value": value,
         "bytes": list(value.encode("ascii") + b"\0")}
        for rva, value in (
            (MATCH_CREATED_STRING_RVA, MATCH_CREATED_STRING),
            (MATCHMAKING_NAME_RVA, MATCHMAKING_NAME),
        )
    ])
    return r"""
'use strict';
const moduleName = %s;
const championMode = %d;
const createMatchProvider = %d;
const onlineProviderHandlerRva = %d;
const onlineProviderHandlerSignature = %s;
const sites = %s;
const requestBoundarySites = %s;
const responseBoundarySites = %s;
const strings = %s;
const maxCaptures = %d;
const maxRequestBoundaryCaptures = %d;
const maxResponseBoundaryCaptures = %d;
const championsWindowMs = %d;
const pollIntervalMs = %d;
const pollAttempts = %d;

let captures = 0;
let requestBoundaryCaptures = 0;
let responseBoundaryCaptures = 0;
let lastChampionsMs = 0;
let activeChampionsRequest = null;
let activeCreateMatchResponse = null;
let activeCreateMatchService = null;

function matches(address, expected) {
  try {
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++)
      if (actual[index] !== expected[index]) return false;
    return true;
  } catch (_) { return false; }
}

function pointerText(value) {
  try { return value.isNull() ? null : value.toString(); }
  catch (_) { return null; }
}

function cardsAddressText(cards, value) {
  try {
    if (value.isNull()) return null;
    if (value.compare(cards.base) >= 0 &&
        value.compare(cards.base.add(cards.size)) < 0)
      return 'CardsDLL+0x' + value.sub(cards.base).toString(16);
    return DebugSymbol.fromAddress(value).toString();
  } catch (_) { return pointerText(value); }
}

function cardsBacktrace(cards, context) {
  try {
    return Thread.backtrace(context, Backtracer.ACCURATE).slice(0, 20).map(
      function (address) {
        if (address.compare(cards.base) >= 0 &&
            address.compare(cards.base.add(cards.size)) < 0)
          return 'CardsDLL+0x' + address.sub(cards.base).toString(16);
        return DebugSymbol.fromAddress(address).toString();
      });
  } catch (error) { return ['backtrace-error: ' + String(error)]; }
}

function requestBacktrace(cards, context) {
  try {
    return Thread.backtrace(context, Backtracer.ACCURATE).slice(0, 12).map(
      function (address) {
        if (address.compare(cards.base) >= 0 &&
            address.compare(cards.base.add(cards.size)) < 0)
          return 'CardsDLL+0x' + address.sub(cards.base).toString(16);
        return DebugSymbol.fromAddress(address).toString();
      });
  } catch (error) { return ['backtrace-error: ' + String(error)]; }
}

function emit(result) {
  if (captures >= maxCaptures) return;
  result.capture = ++captures;
  send(result);
}

function emitRequestBoundary(result) {
  if (requestBoundaryCaptures >= maxRequestBoundaryCaptures) return;
  result.capture = ++requestBoundaryCaptures;
  send(result);
}

function emitResponseBoundary(result) {
  if (responseBoundaryCaptures >= maxResponseBoundaryCaptures) return;
  result.capture = ++responseBoundaryCaptures;
  send(result);
}

function inChampionsWindow() {
  return lastChampionsMs !== 0 &&
    (Date.now() - lastChampionsMs) <= championsWindowMs;
}

function attach(cards) {
  if (!matches(cards.base.add(onlineProviderHandlerRva),
               onlineProviderHandlerSignature)) {
    send({event: 'refused',
      reason: 'Champions online provider handler signature mismatch',
      rva: '0x' + onlineProviderHandlerRva.toString(16)});
    return;
  }
  for (let index = 0; index < strings.length; index++) {
    const entry = strings[index];
    if (!matches(cards.base.add(entry.rva), entry.bytes)) {
      send({event: 'refused', reason: 'Champions transition string mismatch',
        rva: '0x' + entry.rva.toString(16), expected: entry.value});
      return;
    }
  }
  for (let index = 0; index < sites.length; index++) {
    const entry = sites[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused', reason: 'Champions transition site mismatch',
        site: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }
  for (let index = 0; index < requestBoundarySites.length; index++) {
    const entry = requestBoundarySites[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused',
        reason: 'Champions request boundary signature mismatch',
        site: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }
  for (let index = 0; index < responseBoundarySites.length; index++) {
    const entry = responseBoundarySites[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused',
        reason: 'Champions response boundary signature mismatch',
        site: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }

  Interceptor.attach(cards.base.add(onlineProviderHandlerRva), {
    onEnter(args) {
      if (args[1].toUInt32() !== createMatchProvider) return;
      let mode = null;
      try { mode = args[0].add(0x130).readS32(); } catch (_) {}
      if (mode !== championMode) return;
      lastChampionsMs = Date.now();
      emit({event: 'fut-champions-create-match-provider', mode: mode,
        providerId: createMatchProvider, viewModel: pointerText(args[0]),
        backtrace: cardsBacktrace(cards, this.context)});
    }
  });

  for (let index = 0; index < sites.length; index++) {
    const entry = sites[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter(args) {
        if (entry.name === 'online-action-two') {
          let mode = null;
          let controller = null;
          let controllerVtable = null;
          let actionTarget = null;
          try { mode = this.context.rsi.add(8).readS32(); } catch (_) {}
          if (mode !== championMode) return;
          try {
            controller = this.context.rsi.sub(0x118).readPointer();
            controllerVtable = controller.readPointer();
            actionTarget = controllerVtable.add(0x20).readPointer();
          } catch (_) {}
          lastChampionsMs = Date.now();
          emit({event: 'fut-champions-online-action-two', mode: mode,
            owner: pointerText(this.context.rsi.sub(0x128)),
            controller: pointerText(controller),
            controllerVtable: cardsAddressText(cards, controllerVtable),
            actionTarget: cardsAddressText(cards, actionTarget),
            backtrace: cardsBacktrace(cards, this.context)});
          return;
        }
        if (entry.name === 'online-match-created') {
          if (!inChampionsWindow()) return;
          emit({event: 'fut-champions-match-created', publisher: 'online',
            backtrace: cardsBacktrace(cards, this.context)});
          return;
        }
        if (entry.name === 'offline-match-created') {
          emit({event: 'fut-offline-match-created', publisher: 'offline',
            backtrace: cardsBacktrace(cards, this.context)});
          return;
        }
        if (!inChampionsWindow()) return;
        emit({event: 'fut-champions-matchmaking-viewmodel',
          stage: entry.name === 'matchmaking-factory' ? 'factory' :
            'constructor',
          object: pointerText(args[0]), argument1: pointerText(args[1]),
          argument2: pointerText(args[2]),
          backtrace: cardsBacktrace(cards, this.context)});
      }
    });
  }

  for (let index = 0; index < requestBoundarySites.length; index++) {
    const entry = requestBoundarySites[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter(args) {
        if (entry.name === 'context-read') {
          const source = this.context.rax;
          const request = this.context.rdi;
          // The active CPU-match guard owns +0x1F8646, so the observer must
          // not attach to the overlapping +0x1F8642 instruction. This unique
          // later site is still inside the same proved Champions branch and
          // safely opens the response window without competing for code.
          lastChampionsMs = Date.now();
          activeChampionsRequest = request;
          let sourceChampionId = null;
          let requestChampionIdBefore = null;
          try { sourceChampionId = source.add(0xfa8).readS32(); } catch (_) {}
          try {
            requestChampionIdBefore = request.add(0x1c).readS32();
          } catch (_) {}
          emitRequestBoundary({
            event: 'fut-champions-request-champion-id-copy',
            stage: 'before-read-and-commit', source: pointerText(source),
            request: pointerText(request),
            sourceChampionId: sourceChampionId,
            requestChampionIdBefore: requestChampionIdBefore,
            backtrace: requestBacktrace(cards, this.context)});
          return;
        }
        const request = this.context.rdi;
        const state = this.context.r15;
        if (!inChampionsWindow()) return;
        if (activeChampionsRequest === null ||
            !request.equals(activeChampionsRequest)) return;
        let requestFlag2d = null;
        let requestFlag2e = null;
        let requestChampionId = null;
        let stateFlag6e32Before = null;
        let stateFlag6e33Before = null;
        try { requestFlag2d = request.add(0x2d).readU8(); } catch (_) {}
        try { requestFlag2e = request.add(0x2e).readU8(); } catch (_) {}
        try { requestChampionId = request.add(0x1c).readS32(); } catch (_) {}
        try { stateFlag6e32Before = state.add(0x6e32).readU8(); } catch (_) {}
        try { stateFlag6e33Before = state.add(0x6e33).readU8(); } catch (_) {}
        emitRequestBoundary({event: 'fut-champions-request-state-commit',
          stage: 'before-write', request: pointerText(request),
          state: pointerText(state), requestFlag2d: requestFlag2d,
          requestFlag2e: requestFlag2e,
          requestChampionId: requestChampionId,
          stateFlag6e32Before: stateFlag6e32Before,
          stateFlag6e33Before: stateFlag6e33Before,
          backtrace: requestBacktrace(cards, this.context)});
        activeChampionsRequest = null;
        // Request commit precedes the HTTP response.  Keep the bounded window
        // open so parser/completion/provider/matchmaking events from this exact
        // mode-1012 request remain observable.
        lastChampionsMs = Date.now();
      }
    });
  }

  for (let index = 0; index < responseBoundarySites.length; index++) {
    const entry = responseBoundarySites[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter(args) {
        if (!inChampionsWindow()) return;
        this.recordResponseBoundary = true;
        if (entry.name === 'response-factory') {
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'factory-enter', owner: pointerText(args[0]),
            backtrace: requestBacktrace(cards, this.context)});
          return;
        }
        if (entry.name === 'response-parser') {
          activeCreateMatchResponse = args[0];
          let responseVtable = null;
          try { responseVtable = args[0].readPointer(); } catch (_) {}
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'parser-enter', response: pointerText(args[0]),
            responseVtable: cardsAddressText(cards, responseVtable),
            reader: pointerText(args[1]),
            backtrace: requestBacktrace(cards, this.context)});
          return;
        }
        if (entry.name === 'service-completion') {
          if (activeCreateMatchResponse !== null &&
              !args[1].equals(activeCreateMatchResponse)) return;
          activeCreateMatchService = args[0];
          let serviceVtable = null;
          let callbackInline = null;
          let callbackFallback = null;
          try { serviceVtable = args[0].readPointer(); } catch (_) {}
          try { callbackInline = args[0].add(0x90).readPointer(); } catch (_) {}
          try { callbackFallback = args[0].add(0xa0).readPointer(); } catch (_) {}
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'service-completion-enter', service: pointerText(args[0]),
            serviceVtable: cardsAddressText(cards, serviceVtable),
            response: pointerText(args[1]),
            callbackInline: cardsAddressText(cards, callbackInline),
            callbackFallback: cardsAddressText(cards, callbackFallback),
            backtrace: requestBacktrace(cards, this.context)});
          return;
        }
        if (entry.name === 'service-event-handler') {
          if (activeCreateMatchService !== null &&
              !args[0].equals(activeCreateMatchService)) return;
          emitResponseBoundary({event: 'fut-champions-create-match-service',
            stage: 'event-handler-enter', service: pointerText(args[0]),
            eventId: args[1].toUInt32(),
            backtrace: requestBacktrace(cards, this.context)});
          return;
        }
        emitResponseBoundary({event: 'fut-champions-create-match-service',
          stage: 'publish-7548', listener: pointerText(this.context.rcx),
          providerId: this.context.rdx.toUInt32(),
          backtrace: requestBacktrace(cards, this.context)});
      },
      onLeave(retval) {
        if (!this.recordResponseBoundary) return;
        if (entry.name === 'response-factory') {
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'factory-leave', response: pointerText(retval)});
        } else if (entry.name === 'response-parser') {
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'parser-leave', response: pointerText(activeCreateMatchResponse),
            result: retval.toInt32()});
        } else if (entry.name === 'service-completion') {
          emitResponseBoundary({event: 'fut-champions-create-match-response',
            stage: 'service-completion-leave',
            service: pointerText(activeCreateMatchService),
            response: pointerText(activeCreateMatchResponse)});
        }
      }
    });
  }
  send({event: 'fut-champions-match-transition-observer-attached',
    module: cards.name, base: cards.base.toString(),
    championMode: championMode, createMatchProvider: createMatchProvider,
    maxCaptures: maxCaptures,
    maxRequestBoundaryCaptures: maxRequestBoundaryCaptures,
    maxResponseBoundaryCaptures: maxResponseBoundaryCaptures,
    championsWindowMs: championsWindowMs,
    sites: sites.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    }), requestBoundarySites: requestBoundarySites.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    }), responseBoundarySites: responseBoundarySites.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    })});
}

let pollsLeft = pollAttempts;
function waitForModule() {
  let cards = null;
  try { cards = Process.findModuleByName(moduleName); } catch (_) {}
  if (cards !== null) { attach(cards); return; }
  pollsLeft--;
  if (pollsLeft <= 0) {
    send({event: 'fut-champions-match-transition-module-absent',
      module: moduleName});
    return;
  }
  setTimeout(waitForModule, pollIntervalMs);
}

send({event: 'armed', observer: 'fut-champions-match-transition',
  module: moduleName, state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        CHAMPIONS_MODE,
        CREATE_MATCH_PROVIDER,
        ONLINE_PROVIDER_HANDLER_RVA,
        json.dumps(list(ONLINE_PROVIDER_HANDLER_SIGNATURE)),
        sites,
        request_boundary_sites,
        response_boundary_sites,
        strings,
        MAX_CAPTURES,
        MAX_REQUEST_BOUNDARY_CAPTURES,
        MAX_RESPONSE_BOUNDARY_CAPTURES,
        CHAMPIONS_WINDOW_MS,
        MODULE_POLL_INTERVAL_MS,
        MODULE_POLL_ATTEMPTS,
    )

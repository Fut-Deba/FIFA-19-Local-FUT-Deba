#!/usr/bin/env python3
"""Observe the Purchased Items path used after a Transfer Market purchase.

RC129 proved that an SBC Buy Now settles successfully, fills the Purchased
Items cache and then misses every previously suspected cache consumer. This
bounded observer records the exact Purchased Items response lifecycle, claim
branch and two relevant view-model factories. It writes no native state and
changes no return value.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900
MAX_PARSER_CAPTURES = 16
MAX_LIFECYCLE_CAPTURES = 48

# Exact EA App 19.0.4052077.0 CardsDLL sites. The parser has only the two
# direct callers listed here; their return RVAs identify which path invoked it.
PURCHASED_ITEMS_PARSER_RVA = 0x27E2E0
PURCHASED_ITEMS_PARSER_SIGNATURE = tuple(bytes.fromhex(
    "488bc4554154415541564157488bec4883ec7048c745b0feffffff48895810"
    "4889701848897820488bf941bf8d040000e86b30f2ff488bf00f57c0f30f7f45d0"
))
PURCHASED_ITEMS_READY_RVA = 0x27E5A6
PURCHASED_ITEMS_READY_SIGNATURE = tuple(bytes.fromhex(
    "41c6442428014885ff74174c2bf74983e6e0488b064d8bc6488bd7488bceff50"
))
PURCHASED_ITEMS_CALLER_RETURNS = {
    0x20EEA4: "caller-20ee9f",
    0x2A2AEE: "caller-2a2ae9",
}
PURCHASED_ITEMS_SERVICE_VTABLE_RVA = 0x37B3C8
PURCHASED_ITEMS_RESPONSE_VTABLE_RVA = 0x356A38
PURCHASED_ITEMS_LIFECYCLE_SITES = (
    ("response-handoff", 0x20E2C0, "function",
     "48895c24084889742410574883ec20488b3a488bda488b71"),
    ("service-event-handler", 0x20EB80, "function",
     "48895c240848896c24104889742418574883ec20488b01488d2da25b09008bda"),
    ("response-factory", 0x20ED20, "function",
     "4883ec28488b49084c8d0569c616004533c9488b01418d51"),
    ("callback-dispatcher", 0x20ED80, "function",
     "4c8b81900000004d85c0740a4881c19000000049ffe0488b"),
    ("response-parser", 0x20EDD0, "function",
     "40574881ec6001000048c7442420feffffff48899c2470010000488b0517d221"),
    ("claim-cache-check", 0x2DD852, "instruction",
     "488d87286d0000488b4838482b483048b8abaaaaaaaaaaaa2a48f7e948c1fa02"
     "488bc248c1e83f4803d085d20f8e4401"),
    ("claim-popup-key", 0x2DD8CA, "instruction",
     "4c8d057f0a0b00488d542438ff90b8010000488b0d85"),
    ("sbc-squads-viewmodel", 0x9E640, "function",
     "4c89442418574883ec4048c7442420feffffff48895c24504d8bd0488bda488bf9"
     "33c089442468488d4424684889442428498b0041b9010000004c8d05c78e2b"),
    ("new-items-viewmodel", 0x7EC90, "function",
     "4c8bdc4d8943185556574883ec5049c743c0feffffff4989"),
)
PURCHASED_ITEMS_STRINGS = (
    (0x37B39C, "FutGetPurchasedItemsServerResponse"),
    (0x38E350, "FUT_CLAIM_NEW_ITEM_POPUP"),
    (0x35A190, "futsbcsquadsviewmodel"),
    (0x35A0A8, "futnewitemsviewmodel"),
)

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "Memory.write", "writePointer", "writeU8",
    "writeU32", "writeByteArray", "Memory.patchCode", "retval.replace",
    "NativeFunction", "Stalker.", "context.rax =", "context.rip =",
)


def agent_source() -> str:
    """Return the signature-gated, observation-only Frida agent."""
    callers = {str(rva): name
               for rva, name in PURCHASED_ITEMS_CALLER_RETURNS.items()}
    return r"""
'use strict';
const moduleName = __MODULE_NAME__;
const parserRva = __PARSER_RVA__;
const parserSignature = __PARSER_SIGNATURE__;
const readyRva = __READY_RVA__;
const readySignature = __READY_SIGNATURE__;
const callerReturns = __CALLER_RETURNS__;
const lifecycleSites = __LIFECYCLE_SITES__;
const expectedStrings = __EXPECTED_STRINGS__;
const serviceVtableRva = __SERVICE_VTABLE_RVA__;
const responseVtableRva = __RESPONSE_VTABLE_RVA__;
const maxCaptures = __MAX_CAPTURES__;
const maxLifecycleCaptures = __MAX_LIFECYCLE_CAPTURES__;
const pollIntervalMs = __POLL_INTERVAL__;
const pollAttempts = __POLL_ATTEMPTS__;

let captures = 0;
let lifecycleCaptures = 0;
let sequence = 0;
const activeByThread = Object.create(null);

function emit(event, fields) {
  const payload = fields || {};
  payload.event = event;
  payload.sequence = ++sequence;
  send(payload);
}

function readable(address, size) {
  try {
    if (address === null || address.isNull()) return false;
    const first = Process.findRangeByAddress(address);
    const last = Process.findRangeByAddress(address.add(Math.max(0, size - 1)));
    return first !== null && last !== null && first.base.equals(last.base) &&
      first.protection.indexOf('r') !== -1;
  } catch (_) { return false; }
}

function matches(address, expected) {
  try {
    if (!readable(address, expected.length)) return false;
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++)
      if (actual[index] !== expected[index]) return false;
    return true;
  } catch (_) { return false; }
}

function safePointer(address) {
  try { return readable(address, Process.pointerSize) ? address.readPointer() : null; }
  catch (_) { return null; }
}

function safeU32(address) {
  try { return readable(address, 4) ? address.readU32() : null; }
  catch (_) { return null; }
}

function safeU8(address) {
  try { return readable(address, 1) ? address.readU8() : null; }
  catch (_) { return null; }
}

function safeBytes(address, size) {
  try {
    if (!readable(address, size)) return null;
    return Array.from(new Uint8Array(address.readByteArray(size)))
      .map(function (value) { return ('0' + value.toString(16)).slice(-2); })
      .join('');
  } catch (_) { return null; }
}

function pointerText(value) {
  try { return value === null || value.isNull() ? null : value.toString(); }
  catch (_) { return null; }
}

function moduleRva(cards, value) {
  try {
    if (value === null || value.isNull()) return null;
    const owner = Process.findModuleByAddress(value);
    return owner !== null && owner.base.equals(cards.base) ?
      value.sub(cards.base).toUInt32() : null;
  } catch (_) { return null; }
}

function currentState() {
  const stack = activeByThread[String(Process.getCurrentThreadId())];
  return stack && stack.length ? stack[stack.length - 1] : null;
}

function cacheState(cache) {
  return {address: pointerText(cache), ready: safeU8(cache.add(0x28)),
    begin: pointerText(safePointer(cache.add(0x30))),
    end: pointerText(safePointer(cache.add(0x38))),
    capacity: pointerText(safePointer(cache.add(0x40))),
    bytes: safeBytes(cache.add(0x28), 0x40)};
}

function attach(cards) {
  const invalidLifecycle = lifecycleSites.find(function (definition) {
    return !matches(cards.base.add(definition.rva), definition.signature);
  });
  const invalidString = expectedStrings.find(function (definition) {
    return !matches(cards.base.add(definition.rva), definition.bytes);
  });
  if (!matches(cards.base.add(parserRva), parserSignature) ||
      !matches(cards.base.add(readyRva), readySignature) ||
      invalidLifecycle !== undefined || invalidString !== undefined) {
    send({event: 'refused', reason: 'purchased-items-signature-mismatch',
      parserRva: '0x' + parserRva.toString(16),
      readyRva: '0x' + readyRva.toString(16),
      lifecycle: invalidLifecycle === undefined ? null : invalidLifecycle.name,
      string: invalidString === undefined ? null : invalidString.value});
    return;
  }

  Interceptor.attach(cards.base.add(parserRva), {
    onEnter(args) {
      if (captures >= maxCaptures) { this.tracked = false; return; }
      this.tracked = true;
      const threadId = Process.getCurrentThreadId();
      const key = String(threadId);
      const response = args[0];
      let callerRva = null;
      try {
        const owner = Process.findModuleByAddress(this.returnAddress);
        if (owner !== null && owner.base.equals(cards.base))
          callerRva = this.returnAddress.sub(cards.base).toUInt32();
      } catch (_) { callerRva = null; }
      const state = {capture: ++captures, threadId: threadId,
        response: pointerText(response), callerRva: callerRva,
        caller: callerRva === null ? null :
          (callerReturns[String(callerRva)] || 'unexpected'),
        responseState: safeU32(response.add(0xd0)),
        responseMember: pointerText(safePointer(response.add(0xf8))),
        readySeen: false, cache: null, readyBefore: null,
        begin: null, end: null, capacity: null, itemCount: null, items: []};
      if (!activeByThread[key]) activeByThread[key] = [];
      activeByThread[key].push(state);
      emit('purchased-items-parser-enter', state);
    },
    onLeave(retval) {
      if (!this.tracked) return;
      const key = String(Process.getCurrentThreadId());
      const stack = activeByThread[key] || [];
      const state = stack.length ? stack.pop() : null;
      if (!stack.length) delete activeByThread[key];
      if (state === null) return;
      state.returnCode = retval.toInt32();
      emit('purchased-items-parser-exit', state);
    }
  });

  Interceptor.attach(cards.base.add(readyRva), {
    onEnter() {
      const state = currentState();
      if (state === null) return;
      const cache = this.context.r12;
      const begin = safePointer(cache.add(0x30));
      const end = safePointer(cache.add(0x38));
      const capacity = safePointer(cache.add(0x40));
      state.readySeen = true;
      state.cache = pointerText(cache);
      state.readyBefore = safeU8(cache.add(0x28));
      state.begin = pointerText(begin);
      state.end = pointerText(end);
      state.capacity = pointerText(capacity);
      if (begin !== null && end !== null && end.compare(begin) >= 0) {
        const span = end.sub(begin).toInt32();
        if (span >= 0 && span <= 0x18 * 64 &&
            Math.floor(span / 0x18) * 0x18 === span &&
            (span === 0 || readable(begin, span))) {
          state.itemCount = span / 0x18;
          for (let index = 0; index < state.itemCount && index < 8; index++) {
            const item = begin.add(index * 0x18);
            state.items.push({index: index,
              word0: pointerText(safePointer(item)),
              word1: pointerText(safePointer(item.add(8))),
              word2: pointerText(safePointer(item.add(0x10)))});
          }
        }
      }
      emit('purchased-items-cache-ready', state);
    }
  });

  for (const definition of lifecycleSites) {
    Interceptor.attach(cards.base.add(definition.rva), {
      onEnter(args) {
        let service = null;
        let serviceVtable = null;
        let response = null;
        let responseVtable = null;
        if (definition.name === 'response-handoff' ||
            definition.name === 'service-event-handler' ||
            definition.name === 'response-factory' ||
            definition.name === 'callback-dispatcher') {
          service = args[0];
          serviceVtable = safePointer(service);
          if (serviceVtable === null ||
              !serviceVtable.equals(cards.base.add(serviceVtableRva))) {
            this.tracked = false;
            return;
          }
        }
        if (definition.name === 'response-handoff') {
          response = safePointer(args[1]);
          responseVtable = response === null ? null : safePointer(response);
          if (responseVtable === null ||
              !responseVtable.equals(cards.base.add(responseVtableRva))) {
            this.tracked = false;
            return;
          }
        } else if (definition.name === 'response-parser') {
          response = args[0];
          responseVtable = safePointer(response);
          if (responseVtable === null ||
              !responseVtable.equals(cards.base.add(responseVtableRva))) {
            this.tracked = false;
            return;
          }
        }
        if (lifecycleCaptures >= maxLifecycleCaptures) {
          this.tracked = false;
          return;
        }
        this.tracked = true;
        let callerRva = null;
        try {
          const owner = Process.findModuleByAddress(this.returnAddress);
          if (owner !== null && owner.base.equals(cards.base))
            callerRva = this.returnAddress.sub(cards.base).toUInt32();
        } catch (_) { callerRva = null; }
        const state = {capture: ++lifecycleCaptures, site: definition.name,
          siteRva: definition.rva, kind: definition.kind,
          threadId: Process.getCurrentThreadId(), callerRva: callerRva,
          caller: pointerText(this.returnAddress),
          arg0: pointerText(args[0]), arg1: pointerText(args[1]),
          arg2: pointerText(args[2])};
        if (service !== null) {
          state.service = pointerText(service);
          state.serviceVtableRva = moduleRva(cards, serviceVtable);
          state.serviceVtableMatches = true;
        }
        if (definition.name === 'service-event-handler')
          state.eventId = args[1].toUInt32();
        if (definition.name === 'response-parser') {
          state.response = pointerText(response);
          state.reader = pointerText(args[1]);
          state.responseVtableRva = moduleRva(cards, responseVtable);
          state.responseVtableMatches = true;
          state.responseBytes = safeBytes(response, 0x28);
        }
        if (definition.name === 'response-handoff') {
          const receiver = safePointer(service.add(8));
          const receiverVtable = receiver === null ? null :
            safePointer(receiver);
          const receiverTarget = receiverVtable === null ? null :
            safePointer(receiverVtable.add(0x18));
          state.responseHolder = pointerText(args[1]);
          state.response = pointerText(response);
          state.responseVtableRva = moduleRva(cards, responseVtable);
          state.responseVtableMatches = true;
          state.responseBytes = safeBytes(response, 0x28);
          state.receiver = pointerText(receiver);
          state.receiverVtable = pointerText(receiverVtable);
          state.receiverVtableRva = moduleRva(cards, receiverVtable);
          state.receiverTarget = pointerText(receiverTarget);
          state.receiverTargetRva = moduleRva(cards, receiverTarget);
        }
        if (definition.name === 'claim-cache-check' ||
            definition.name === 'claim-popup-key') {
          state.service = pointerText(this.context.rdi);
          state.cache = cacheState(this.context.rdi.add(0x6d28));
          state.viewModel = pointerText(this.context.rsi);
        }
        this.state = state;
        emit('purchased-items-lifecycle-enter', state);
      },
      onLeave(retval) {
        if (!this.tracked || definition.kind !== 'function') return;
        this.state.returnValue = pointerText(retval);
        if (definition.name === 'response-factory' ||
            definition.name === 'sbc-squads-viewmodel' ||
            definition.name === 'new-items-viewmodel') {
          const vtable = safePointer(retval);
          this.state.created = pointerText(retval);
          this.state.createdVtableRva = moduleRva(cards, vtable);
          this.state.createdBytes = safeBytes(retval, 0x30);
          if (definition.name === 'response-factory')
            this.state.createdVtableMatches = vtable !== null &&
              vtable.equals(cards.base.add(responseVtableRva));
        }
        emit('purchased-items-lifecycle-exit', this.state);
      }
    });
  }

  send({event: 'armed', observer: 'sbc-market-return', state: 'attached',
    module: cards.name, parserRva: '0x' + parserRva.toString(16),
    readyRva: '0x' + readyRva.toString(16), maxCaptures: maxCaptures,
    maxLifecycleCaptures: maxLifecycleCaptures});
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

send({event: 'armed', observer: 'sbc-market-return',
  state: 'waiting-for-module'});
waitForModule();
""".replace("__MODULE_NAME__", json.dumps(MODULE_NAME)) \
        .replace("__PARSER_RVA__", str(PURCHASED_ITEMS_PARSER_RVA)) \
        .replace("__PARSER_SIGNATURE__", json.dumps(
            list(PURCHASED_ITEMS_PARSER_SIGNATURE))) \
        .replace("__READY_RVA__", str(PURCHASED_ITEMS_READY_RVA)) \
        .replace("__READY_SIGNATURE__", json.dumps(
            list(PURCHASED_ITEMS_READY_SIGNATURE))) \
        .replace("__CALLER_RETURNS__", json.dumps(callers)) \
        .replace("__LIFECYCLE_SITES__", json.dumps([
            {"name": name, "rva": rva, "kind": kind,
             "signature": list(bytes.fromhex(signature))}
            for name, rva, kind, signature in PURCHASED_ITEMS_LIFECYCLE_SITES
        ], separators=(",", ":"))) \
        .replace("__EXPECTED_STRINGS__", json.dumps([
            {"rva": rva, "value": value,
             "bytes": list(value.encode("ascii") + b"\0")}
            for rva, value in PURCHASED_ITEMS_STRINGS
        ], separators=(",", ":"))) \
        .replace("__SERVICE_VTABLE_RVA__",
                 str(PURCHASED_ITEMS_SERVICE_VTABLE_RVA)) \
        .replace("__RESPONSE_VTABLE_RVA__",
                 str(PURCHASED_ITEMS_RESPONSE_VTABLE_RVA)) \
        .replace("__MAX_CAPTURES__", str(MAX_PARSER_CAPTURES)) \
        .replace("__MAX_LIFECYCLE_CAPTURES__",
                 str(MAX_LIFECYCLE_CAPTURES)) \
        .replace("__POLL_INTERVAL__", str(MODULE_POLL_INTERVAL_MS)) \
        .replace("__POLL_ATTEMPTS__", str(MODULE_POLL_ATTEMPTS))


def self_test_errors() -> list[str]:
    """Return reasons this observer must not be armed."""
    source = agent_source()
    errors = ["forbidden agent primitive: " + token
              for token in FORBIDDEN_AGENT_TOKENS if token in source]
    if MAX_PARSER_CAPTURES <= 0:
        errors.append("unbounded parser capture budget")
    if MAX_LIFECYCLE_CAPTURES <= 0:
        errors.append("unbounded lifecycle capture budget")
    return errors

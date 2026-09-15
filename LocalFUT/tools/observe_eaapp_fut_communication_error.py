#!/usr/bin/env python3
"""Build a passive observer for FUT error exits with no failing HTTP request.

Paying a Draft token and entering raises `There was a problem communicating
with the FIFA Ultimate Team servers` while every request in the session
answers HTTP 200, no native exception fires and the client sends nothing
between the last response and the popup. Three payload shapes have already
been changed on that route and eliminated, so the failing call must be named
rather than guessed.

`CARDS_CB_ERR_COMMUNICATION_FAILURE` is the popup's localisation key at RVA
`0x374fa0`. It has exactly one code reference, the two-instruction accessor at
`CardsDLL+0x1a1df1` which loads the key and returns. Whatever raises that
popup resolves the key through this accessor, so its return address chain
names the caller.

The observer records only the accessor's own backtrace. It calls no client
method, writes no register, memory or return value, and refuses to attach
unless the exact accessor bytes are present.

RC115 armed this observer and it refused live with `accessor-signature-mismatch`
at RVA 0x1a1df1 (`eaapp-full-server-guarded-20260907-190819.jsonl`). The image
was never the problem: the installed DLL does carry `488d05a8311d00c3488d`
there. The agent called `Memory.readByteArray`, which Frida 17 removed, the
throw was swallowed by the signature guard, and the guard reported a mismatch
it had not measured. The reads now go through the pointer methods every other
agent in this project uses, and a refusal now carries the observed bytes.

RC124's SBC Player Search Buy Now instead reaches the stock
`FUT_NON_CRITICAL_ERROR|Ok|quit` path after Buy Now, trade status and Purchased
Items all return HTTP 200. `CardsDLL+0xbd000` is the shared response-error
handler which opens `UNASSIGNED_ITEM_POPUP`; observing its direct caller names
the exact rejected client branch without changing the purchased item.

RC125 proved that handler is not reached by this popup. The same stock popup
has only one other emitter, `CardsDLL+0x99510`, so observing that function now
names the response controller which rejects the otherwise successful SBC save.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"

# Verified read-only against CardsDLL_Win64_retail.dll, product version
# 19.0.4052077.0, sha256
# 8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a.
# The message key lives at RVA 0x374fa0 and this accessor is its only code
# reference; the ten-byte signature occurs exactly once in the image.
ERROR_KEY_RVA = 0x374FA0
ERROR_KEY = "CARDS_CB_ERR_COMMUNICATION_FAILURE"
ACCESSOR_RVA = 0x1A1DF1
ACCESSOR_SIGNATURE = tuple(bytes.fromhex("488d05a8311d00c3488d"))

# The accessor is one entry in a dense block of eight-byte `lea rax,[rip+X];
# ret` thunks reached through the jump table at RVA 0x1a1ec8. It is not a
# function start in the exception directory, so:
#   * an inline hook that needed more than eight bytes would overwrite the
#     next thunk, and
#   * `Backtracer.ACCURATE` has no unwind record to walk from here.
# The observer therefore verifies the neighbour is still intact after the hook
# is written and detaches if it is not, and it reports the return address --
# which names the caller on its own -- alongside both backtracers.
NEIGHBOUR_RVA = 0x1A1DF9
NEIGHBOUR_SIGNATURE = tuple(bytes.fromhex("488d05002e1d00c3"))

# This whole function has unwind metadata, so its direct return address and an
# accurate backtrace identify which response controller requested the visible
# `Ok|quit` popup. The signature occurs exactly once in the EA App image.
RESPONSE_ERROR_RVA = 0xBD000
RESPONSE_ERROR_SIGNATURE = tuple(bytes.fromhex(
    "488bc455488d68a14881ec9000000048c745f7feffffff"))
RESPONSE_ERROR_LITERALS = (
    (0x364220, "OnServerResponseError()"),
    (0x341F48, "UNASSIGNED_ITEM_POPUP"),
    (0x3620D0, "FUT_NON_CRITICAL_ERROR|Ok|quit"),
)

# RC125 produced the visible FUT_NON_CRITICAL_ERROR popup without entering
# RESPONSE_ERROR_RVA. This is the only other xref to the exact popup command;
# its 32-byte entry signature occurs once in the installed EA App image.
NON_CRITICAL_POPUP_RVA = 0x99510
NON_CRITICAL_POPUP_SIGNATURE = tuple(bytes.fromhex(
    "40574883ec6048c7442430feffffff48895c2478488bd9488b4910488b01488d"))

MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900
MAX_CAPTURES = 8
MAX_RESPONSE_CAPTURES = 8
MAX_NON_CRITICAL_CAPTURES = 8
MAX_FRAMES = 16

# A guard would need these; an observer must never contain them.
FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "Memory.write", "Memory.protect", "writePointer",
    "writeS32", "writeU32", "writeByteArray", "NativeFunction", "Stalker.",
    "context.rax =", "context.rcx =", "context.rdx =", "retval.replace",
)

# Frida 17 removed these module-level readers. An agent that calls one throws,
# the throw is swallowed by the signature guard, and the observer refuses with
# a mismatch it never actually measured - which is exactly what happened on
# 2026-09-07 at 19:08 and cost a live Draft entry.
REMOVED_FRIDA_APIS = (
    "Memory.readByteArray", "Memory.readUtf8String", "Memory.readAnsiString",
    "Memory.readCString", "Memory.readPointer", "Memory.readU8",
    "Memory.readU32", "Memory.readS32", "Memory.readU64", "Memory.readS64",
)


def agent_source() -> str:
    """Return the signature-gated, observation-only Frida agent."""
    return """
'use strict';
const moduleName = %s;
const accessorRva = %d;
const accessorSignature = %s;
const neighbourRva = %d;
const neighbourSignature = %s;
const responseErrorRva = %d;
const responseErrorSignature = %s;
const responseErrorLiterals = %s;
const errorKeyRva = %d;
const errorKey = %s;
const pollIntervalMs = %d;
const pollAttempts = %d;
const maxCaptures = %d;
const maxResponseCaptures = %d;
const nonCriticalPopupRva = %d;
const nonCriticalPopupSignature = %s;
const maxNonCriticalCaptures = %d;
const maxFrames = %d;

let captures = 0;
let responseCaptures = 0;
let nonCriticalCaptures = 0;
let sequence = 0;
let attached = false;

function emit(event, fields) {
  const payload = fields || {};
  payload.event = event;
  payload.sequence = ++sequence;
  send(payload);
}

function readBytes(address, length) {
  // Frida 17 removed the legacy module-level readers, so every read here
  // goes through the pointer methods each other agent in this project uses.
  // A refusal must never be an API mistake dressed up as a mismatch.
  try {
    const raw = address.readByteArray(length);
    return raw === null ? null : new Uint8Array(raw);
  } catch (error) {
    return null;
  }
}

function hex(bytes) {
  if (bytes === null) return null;
  let text = '';
  for (let index = 0; index < bytes.length; index += 1) {
    text += ('0' + bytes[index].toString(16)).slice(-2);
  }
  return text;
}

function matches(bytes, expected) {
  if (bytes === null || bytes.length !== expected.length) return false;
  for (let index = 0; index < expected.length; index += 1) {
    if (bytes[index] !== expected[index]) return false;
  }
  return true;
}

function readKey(base) {
  try {
    return base.add(errorKeyRva).readUtf8String(errorKey.length);
  } catch (error) {
    return null;
  }
}

function readLiteral(base, rva, length) {
  try {
    return base.add(rva).readUtf8String(length);
  } catch (error) {
    return null;
  }
}

function attach(module) {
  if (attached) return;
  const accessor = module.base.add(accessorRva);
  const observed = readBytes(accessor, accessorSignature.length);
  if (!matches(observed, accessorSignature)) {
    // Report what was actually there: a refusal without the observed bytes
    // costs a whole live session to diagnose.
    emit('refused', {reason: 'accessor-signature-mismatch',
      module: module.name, rva: accessorRva, observed: hex(observed),
      expected: hex(new Uint8Array(accessorSignature))});
    return;
  }
  const key = readKey(module.base);
  if (key !== errorKey) {
    emit('refused', {reason: 'error-key-mismatch', module: module.name,
      observed: key});
    return;
  }
  const responseError = module.base.add(responseErrorRva);
  const responseObserved = readBytes(responseError,
                                     responseErrorSignature.length);
  if (!matches(responseObserved, responseErrorSignature)) {
    emit('refused', {reason: 'response-error-signature-mismatch',
      module: module.name, rva: responseErrorRva,
      observed: hex(responseObserved),
      expected: hex(new Uint8Array(responseErrorSignature))});
    return;
  }
  for (let index = 0; index < responseErrorLiterals.length; index += 1) {
    const literal = responseErrorLiterals[index];
    const observed = readLiteral(module.base, literal[0], literal[1].length);
    if (observed !== literal[1]) {
      emit('refused', {reason: 'response-error-literal-mismatch',
        module: module.name, rva: literal[0], observed: observed,
        expected: literal[1]});
      return;
    }
  }
  const nonCriticalPopup = module.base.add(nonCriticalPopupRva);
  const nonCriticalObserved = readBytes(
    nonCriticalPopup, nonCriticalPopupSignature.length);
  if (!matches(nonCriticalObserved, nonCriticalPopupSignature)) {
    emit('refused', {reason: 'non-critical-popup-signature-mismatch',
      module: module.name, rva: nonCriticalPopupRva,
      observed: hex(nonCriticalObserved),
      expected: hex(new Uint8Array(nonCriticalPopupSignature))});
    return;
  }
  attached = true;
  const moduleBase = module.base;
  function walk(context, backtracer) {
    const frames = [];
    try {
      const trace = Thread.backtrace(context, backtracer);
      for (let index = 0; index < trace.length && index < maxFrames;
           index += 1) {
        const address = trace[index];
        frames.push({address: address.toString(),
          rva: address.sub(moduleBase).toString(),
          symbol: DebugSymbol.fromAddress(address).toString()});
      }
    } catch (error) {
      return [];
    }
    return frames;
  }
  const listener = Interceptor.attach(accessor, {
    onEnter() {
      if (captures >= maxCaptures) return;
      captures += 1;
      // The thunk has no unwind record, so ACCURATE can legitimately come
      // back empty. The return address alone already names the caller.
      const accurate = walk(this.context, Backtracer.ACCURATE);
      const fuzzy = accurate.length ? [] :
        walk(this.context, Backtracer.FUZZY);
      const returnAddress = this.returnAddress;
      emit('fut-communication-error', {
        key: errorKey,
        capture: captures,
        threadId: Process.getCurrentThreadId(),
        returnAddress: returnAddress.toString(),
        returnRva: returnAddress.sub(moduleBase).toString(),
        frames: accurate,
        fuzzyFrames: fuzzy});
    }
  });
  Interceptor.flush();
  // The hook is written into an eight-byte thunk. If it needed more room the
  // next thunk is now corrupt, and this session must not continue.
  const neighbour = readBytes(module.base.add(neighbourRva),
                              neighbourSignature.length);
  if (!matches(neighbour, neighbourSignature)) {
    // Detach only this listener: other guards and observers share the
    // process and must not be torn down by our refusal.
    listener.detach();
    Interceptor.flush();
    attached = false;
    emit('refused', {reason: 'hook-overran-the-neighbouring-thunk',
      module: module.name, rva: neighbourRva, observed: hex(neighbour),
      expected: hex(new Uint8Array(neighbourSignature))});
    return;
  }
  Interceptor.attach(responseError, {
    onEnter() {
      if (responseCaptures >= maxResponseCaptures) return;
      responseCaptures += 1;
      const accurate = walk(this.context, Backtracer.ACCURATE);
      const fuzzy = accurate.length ? [] :
        walk(this.context, Backtracer.FUZZY);
      const returnAddress = this.returnAddress;
      emit('unassigned-response-error', {
        capture: responseCaptures,
        threadId: Process.getCurrentThreadId(),
        controller: this.context.rcx.toString(),
        returnAddress: returnAddress.toString(),
        returnRva: returnAddress.sub(moduleBase).toString(),
        frames: accurate,
        fuzzyFrames: fuzzy});
    }
  });
  Interceptor.flush();
  Interceptor.attach(nonCriticalPopup, {
    onEnter() {
      if (nonCriticalCaptures >= maxNonCriticalCaptures) return;
      nonCriticalCaptures += 1;
      const accurate = walk(this.context, Backtracer.ACCURATE);
      const fuzzy = accurate.length ? [] :
        walk(this.context, Backtracer.FUZZY);
      let responseCode = null;
      try {
        responseCode = this.context.rdx.add(0x1c).readU32();
      } catch (error) {}
      const returnAddress = this.returnAddress;
      emit('non-critical-popup', {
        capture: nonCriticalCaptures,
        threadId: Process.getCurrentThreadId(),
        controller: this.context.rcx.toString(),
        response: this.context.rdx.toString(),
        responseCode: responseCode,
        returnAddress: returnAddress.toString(),
        returnRva: returnAddress.sub(moduleBase).toString(),
        frames: accurate,
        fuzzyFrames: fuzzy});
    }
  });
  Interceptor.flush();
  emit('armed', {state: 'attached', module: module.name,
    base: module.base.toString(), maxCaptures: maxCaptures,
    maxResponseCaptures: maxResponseCaptures,
    maxNonCriticalCaptures: maxNonCriticalCaptures});
}

let remaining = pollAttempts;
function poll() {
  const module = Process.findModuleByName(moduleName);
  if (module !== null) { attach(module); return; }
  remaining -= 1;
  if (remaining <= 0) {
    emit('refused', {reason: 'module-absent', module: moduleName});
    return;
  }
  setTimeout(poll, pollIntervalMs);
}

emit('armed', {state: 'waiting-for-module', module: moduleName});
poll();
""" % (json.dumps(MODULE_NAME), ACCESSOR_RVA, json.dumps(list(ACCESSOR_SIGNATURE)),
       NEIGHBOUR_RVA, json.dumps(list(NEIGHBOUR_SIGNATURE)),
       RESPONSE_ERROR_RVA, json.dumps(list(RESPONSE_ERROR_SIGNATURE)),
       json.dumps(RESPONSE_ERROR_LITERALS),
       ERROR_KEY_RVA, json.dumps(ERROR_KEY), MODULE_POLL_INTERVAL_MS,
       MODULE_POLL_ATTEMPTS, MAX_CAPTURES, MAX_RESPONSE_CAPTURES,
       NON_CRITICAL_POPUP_RVA,
       json.dumps(list(NON_CRITICAL_POPUP_SIGNATURE)),
       MAX_NON_CRITICAL_CAPTURES, MAX_FRAMES)


def self_test_errors() -> list[str]:
    """Return the reasons this observer must not be armed, if any."""
    errors: list[str] = []
    if ACCESSOR_RVA <= 0 or len(ACCESSOR_SIGNATURE) < 8:
        errors.append("invalid accessor site")
    if ERROR_KEY_RVA <= 0 or not ERROR_KEY:
        errors.append("invalid error key site")
    if MAX_CAPTURES <= 0 or MAX_RESPONSE_CAPTURES <= 0 or MAX_FRAMES <= 0:
        errors.append("unbounded capture budget")
    if (NON_CRITICAL_POPUP_RVA <= 0 or
            len(NON_CRITICAL_POPUP_SIGNATURE) < 16 or
            MAX_NON_CRITICAL_CAPTURES <= 0):
        errors.append("invalid non-critical popup observer")
    source = agent_source()
    for token in FORBIDDEN_AGENT_TOKENS:
        if token in source:
            errors.append("forbidden agent primitive: %s" % token)
    for token in REMOVED_FRIDA_APIS:
        if token in source:
            errors.append("removed Frida API: %s" % token)
    return errors

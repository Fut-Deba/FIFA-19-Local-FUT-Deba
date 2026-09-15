#!/usr/bin/env python3
"""Observe where SBC-player skill moves become zero in Player Bio.

The server publishes five skill moves for Flashback Ibrahimovic and FUTmas
Rashford. CardsDLL enriches the native item from its player database and writes
the result to item byte ``+0x15e`` before Player Bio republishes that byte.
This observer records that database result, its missing-row fallback and the
published value for only those two asset ids. It changes no client state.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
TARGET_ASSETS = {41236: "Ibrahimovic", 231677: "Rashford"}

# Verified against CardsDLL 19.0.4052077.0, SHA-256
# 8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a.
# Each signature occurs exactly once in that image.
DB_RESULT_RVA = 0x26FCB0
DB_RESULT_SIGNATURE = tuple(bytes.fromhex(
    "88835e010000eb1b418bc688835e0100"))
DB_FALLBACK_RVA = 0x26FCCC
DB_FALLBACK_SIGNATURE = tuple(bytes.fromhex(
    "c6835e010000004885c97406488b01ff"))
BIO_PUBLISH_RVA = 0x1B3013
BIO_PUBLISH_SIGNATURE = tuple(bytes.fromhex(
    "440fb6805e010000488d15ce321c0048"))

MAX_DB_RESULTS = 12
MAX_DB_FALLBACKS = 12
MAX_BIO_PUBLISHES = 16
MODULE_POLL_ATTEMPTS = 900
MODULE_POLL_INTERVAL_MS = 1000

FORBIDDEN_AGENT_TOKENS = (
    "Interceptor.replace", "Memory.write", "Memory.protect", "writeU8",
    "writeU32", "writePointer", "writeByteArray", "retval.replace",
    "NativeFunction", "Stalker.", "context.rax =", "context.rbx =",
)


def agent_source() -> str:
    """Return the bounded, read-only Frida observer."""
    return r"""
'use strict';
const moduleName = %s;
const targetAssets = %s;
const dbResultRva = %d;
const dbResultSignature = %s;
const dbFallbackRva = %d;
const dbFallbackSignature = %s;
const bioPublishRva = %d;
const bioPublishSignature = %s;
const maxDbResults = %d;
const maxDbFallbacks = %d;
const maxBioPublishes = %d;
const pollAttempts = %d;
const pollIntervalMs = %d;

let dbResults = 0;
let dbFallbacks = 0;
let bioPublishes = 0;
let sequence = 0;

function emit(event, fields) {
  const payload = fields || {};
  payload.event = event;
  payload.sequence = ++sequence;
  send(payload);
}

function observedBytes(address, length) {
  try {
    const bytes = address.readByteArray(length);
    return bytes === null ? null : new Uint8Array(bytes);
  } catch (_) { return null; }
}

function hex(bytes) {
  if (bytes === null) return null;
  let result = '';
  for (let index = 0; index < bytes.length; index++)
    result += ('0' + bytes[index].toString(16)).slice(-2);
  return result;
}

function matches(actual, expected) {
  if (actual === null || actual.length !== expected.length) return false;
  for (let index = 0; index < expected.length; index++)
    if (actual[index] !== expected[index]) return false;
  return true;
}

function target(object) {
  try {
    if (object === null || object.isNull()) return null;
    const identity = object.add(0x18).readU32();
    const assetId = identity & 0xffffff;
    const name = targetAssets[String(assetId)];
    if (name === undefined) return null;
    return {object: object.toString(), identity: identity,
      assetId: assetId, player: name};
  } catch (_) { return null; }
}

function verify(cards, name, rva, signature) {
  const actual = observedBytes(cards.base.add(rva), signature.length);
  if (matches(actual, signature)) return true;
  emit('refused', {reason: name + '-signature-mismatch', rva: rva,
    observed: hex(actual), expected: hex(new Uint8Array(signature))});
  return false;
}

function attach(cards) {
  if (!verify(cards, 'db-result', dbResultRva, dbResultSignature) ||
      !verify(cards, 'db-fallback', dbFallbackRva, dbFallbackSignature) ||
      !verify(cards, 'bio-publish', bioPublishRva, bioPublishSignature)) return;

  Interceptor.attach(cards.base.add(dbResultRva), {
    onEnter() {
      if (dbResults >= maxDbResults) return;
      const item = target(this.context.rbx);
      if (item === null) return;
      dbResults++;
      item.databaseValue = this.context.rax.toUInt32() & 0xff;
      try { item.valueBeforeWrite = this.context.rbx.add(0x15e).readU8(); }
      catch (_) { item.valueBeforeWrite = null; }
      emit('player-skill-db-result', item);
    }
  });

  Interceptor.attach(cards.base.add(dbFallbackRva), {
    onEnter() {
      if (dbFallbacks >= maxDbFallbacks) return;
      const item = target(this.context.rbx);
      if (item === null) return;
      dbFallbacks++;
      emit('player-skill-db-fallback', item);
    }
  });

  Interceptor.attach(cards.base.add(bioPublishRva), {
    onEnter() {
      if (bioPublishes >= maxBioPublishes) return;
      const item = target(this.context.rax);
      if (item === null) return;
      bioPublishes++;
      try { item.publishedValue = this.context.rax.add(0x15e).readU8(); }
      catch (_) { item.publishedValue = null; }
      emit('player-skill-bio-publish', item);
    }
  });

  emit('armed', {state: 'attached', module: cards.name,
    targets: targetAssets, maxDbResults: maxDbResults,
    maxDbFallbacks: maxDbFallbacks, maxBioPublishes: maxBioPublishes});
}

let pollsLeft = pollAttempts;
function waitForModule() {
  let cards = null;
  try { cards = Process.findModuleByName(moduleName); }
  catch (_) { cards = null; }
  if (cards !== null) { attach(cards); return; }
  pollsLeft--;
  if (pollsLeft <= 0) {
    emit('refused', {reason: 'CardsDLL module absent'});
    return;
  }
  setTimeout(waitForModule, pollIntervalMs);
}

emit('armed', {state: 'waiting-for-module', module: moduleName});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps({str(key): value for key, value in TARGET_ASSETS.items()}),
        DB_RESULT_RVA,
        json.dumps(list(DB_RESULT_SIGNATURE)),
        DB_FALLBACK_RVA,
        json.dumps(list(DB_FALLBACK_SIGNATURE)),
        BIO_PUBLISH_RVA,
        json.dumps(list(BIO_PUBLISH_SIGNATURE)),
        MAX_DB_RESULTS,
        MAX_DB_FALLBACKS,
        MAX_BIO_PUBLISHES,
        MODULE_POLL_ATTEMPTS,
        MODULE_POLL_INTERVAL_MS,
    )


def self_test_errors() -> list[str]:
    """Return reasons this observer must not be armed."""
    source = agent_source()
    errors = ["forbidden agent primitive: %s" % token
              for token in FORBIDDEN_AGENT_TOKENS if token in source]
    if min(MAX_DB_RESULTS, MAX_DB_FALLBACKS, MAX_BIO_PUBLISHES) <= 0:
        errors.append("unbounded capture budget")
    return errors

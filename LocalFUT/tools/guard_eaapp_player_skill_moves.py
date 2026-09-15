#!/usr/bin/env python3
"""Publish the sourced skill moves for two SBC reward cards.

The server already sends five for Flashback Ibrahimovic and FUTmas Rashford,
but Player Bio publishes byte ``item+0x15e`` at CardsDLL+0x1b3013 instead.
This guard changes only the outgoing ``skillmoves`` argument, only for those
two exact resource identities, after the native load and before the setter.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"
TARGET_RESOURCES = {
    # FIFA stores one-to-five stars as zero-to-four; five is rejected by the
    # native field and Player Bio consequently renders an empty star row.
    50372884: {"player": "Ibrahimovic", "skillMoves": 4},
    67340541: {"player": "Rashford", "skillMoves": 4},
}

# `movzx r8d,[rax+0x15e]` completes at +0x1b301b. Hooking the following LEA
# leaves rax on the item and lets us replace only the setter's integer argument.
PUBLISH_ARGUMENT_RVA = 0x1B301B
PUBLISH_ARGUMENT_SIGNATURE = tuple(bytes.fromhex(
    "488d15ce321c00488bcf41ff5110488b"))
MAX_EVENTS = 12
MODULE_POLL_ATTEMPTS = 900
MODULE_POLL_INTERVAL_MS = 1000


def agent_source() -> str:
    """Return the exact-card, signature-gated Frida guard."""
    return r"""
'use strict';
const moduleName = %s;
const targets = %s;
const publishRva = %d;
const signature = %s;
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
  const site = cards.base.add(publishRva);
  if (!matches(site, signature)) {
    send({event: 'refused', reason: 'player-skill-publish-signature-mismatch',
      rva: publishRva});
    return;
  }
  Interceptor.attach(site, {
    onEnter() {
      let identity = 0;
      try { identity = this.context.rax.add(0x18).readU32(); }
      catch (_) { return; }
      const target = targets[String(identity)];
      if (target === undefined) return;
      const before = this.context.r8.toUInt32();
      const wanted = Number(target.skillMoves);
      if (before === wanted) return;
      this.context.r8 = ptr(wanted);
      emit({event: 'player-skill-moves-applied', resourceId: identity,
        player: target.player, from: before, to: wanted});
    }
  });
  send({event: 'armed', guard: 'player-skill-moves', module: cards.name,
    resources: Object.keys(targets).map(Number)});
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

send({event: 'armed', guard: 'player-skill-moves', module: moduleName,
  state: 'waiting-for-module'});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        json.dumps({str(key): value
                    for key, value in TARGET_RESOURCES.items()}),
        PUBLISH_ARGUMENT_RVA,
        json.dumps(list(PUBLISH_ARGUMENT_SIGNATURE)),
        MAX_EVENTS,
        MODULE_POLL_ATTEMPTS,
        MODULE_POLL_INTERVAL_MS,
    )

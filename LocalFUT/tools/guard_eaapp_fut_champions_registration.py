#!/usr/bin/env python3
"""Build the exact EA App offline FUT Champions entry navigation guard.

The stock Registration view was live-proved to POST successfully, but then
forces regional-leaderboard and online-privacy screens which do not belong in
the offline competition.  At either proved PLAY FUT CHAMPIONS destination,
this guard waits for the host's explicit local enrollment/difficulty receipt.
Success selects native ``GotoSquadSetup``; cancellation or failure selects
native ``GotoChampionsSchedule``.  It never patches module memory.
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

# BFCFF loads GotoRegistration, BFCF0 loads GotoSquadSetup, and BFD06 performs
# their shared virtual navigation call. RC96 live proved that a legacy stock
# registration in PICK_DIFFICULTY takes the Squad Setup load directly, so the
# country-free selector must guard both mutually exclusive entry loads.
#
# RC95 live proved that Frida cannot attach to the three-byte virtual call,
# while the passive observer attached to BFCFF and saw its destination.
# RC100 then proved that changing PC/RDX at an instruction Interceptor does not
# skip Frida's relocated LEA: /pidinfo was still fetched. RC101 attached the
# target only from that LEA and the same native invocation never reached the
# newly installed target hook. Pre-arm it once RDI contains the router, before
# the action-0x10 branch can select either destination, then replace RDX only at
# that function's entry after the stock destination LEA has executed.
CALL_SITE_RVA = 0xBFD06
CALL_SITE_SIGNATURE = tuple(bytes.fromhex("ff5078e92a010000"))
ROUTER_READY_RVA = 0xBFB70
ROUTER_READY_SIGNATURE = tuple(bytes.fromhex(
    "4889bc2490000000488b0d810d3700"
))
REGISTRATION_LOAD_RVA = 0xBFCFF
REGISTRATION_LOAD_SIGNATURE = tuple(bytes.fromhex(
    "488d151a472a00ff5078"
))
SQUAD_SETUP_LOAD_RVA = 0xBFCF0
SQUAD_SETUP_LOAD_SIGNATURE = tuple(bytes.fromhex(
    "488d1541472a00ff5078"
))
REGISTRATION_DESTINATION_RVA = 0x364420
REGISTRATION_DESTINATION = "GotoRegistration"
SQUAD_SETUP_DESTINATION_RVA = 0x364438
SQUAD_SETUP_DESTINATION = "GotoSquadSetup"
SCHEDULE_LOAD_RVA = 0xC008A
SCHEDULE_LOAD_SIGNATURE = tuple(bytes.fromhex("488d1567442a00"))
SCHEDULE_DESTINATION_RVA = 0x3644F8
SCHEDULE_DESTINATION = "GotoChampionsSchedule"

MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900
MAX_DIAGNOSTIC_EVENTS = 8


def _pe_layout(image: bytes) -> tuple[int, int]:
    """Return section count and section-table offset for a PE image."""
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
                return raw_pointer + (rva - virtual_address)
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
    """Refuse every disk image outside the live-proved navigation contract."""
    if product_version != EXPECTED_PRODUCT_VERSION:
        raise ValueError("CardsDLL product version mismatch")
    if hashlib.sha256(image).hexdigest() != EXPECTED_CARDS_SHA256:
        raise ValueError("CardsDLL fingerprint mismatch")

    sites = (
        (CALL_SITE_RVA, CALL_SITE_SIGNATURE, "call site"),
        (ROUTER_READY_RVA, ROUTER_READY_SIGNATURE, "router-ready site"),
        (REGISTRATION_LOAD_RVA, REGISTRATION_LOAD_SIGNATURE,
         "Registration load"),
        (SQUAD_SETUP_LOAD_RVA, SQUAD_SETUP_LOAD_SIGNATURE,
         "Squad Setup load"),
        (SCHEDULE_LOAD_RVA, SCHEDULE_LOAD_SIGNATURE,
         "Champions Schedule load"),
    )
    for rva, expected, label in sites:
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError("Champions %s signature mismatch" % label)
        if image.count(signature) != 1:
            raise ValueError("Champions %s signature is not unique" % label)

    destinations = (
        (REGISTRATION_DESTINATION_RVA, REGISTRATION_DESTINATION),
        (SQUAD_SETUP_DESTINATION_RVA, SQUAD_SETUP_DESTINATION),
        (SCHEDULE_DESTINATION_RVA, SCHEDULE_DESTINATION),
    )
    for rva, value in destinations:
        encoded = value.encode("ascii") + b"\0"
        if _bytes_at(image, rva, len(encoded)) != encoded:
            raise ValueError(
                "Champions destination mismatch at RVA 0x%x" % rva)
        if image.count(encoded) != 1:
            raise ValueError(
                "Champions destination is not unique at RVA 0x%x" % rva)


def redirect_destination_rva(destination_rva: int) -> int:
    """Model the successful Registration destination substitution."""
    if destination_rva == REGISTRATION_DESTINATION_RVA:
        return SQUAD_SETUP_DESTINATION_RVA
    return destination_rva


def entry_destination_rva(status: str) -> int:
    """Model the two fail-closed destinations chosen after the host reply."""
    if str(status) in ("registered", "resume"):
        return SQUAD_SETUP_DESTINATION_RVA
    return SCHEDULE_DESTINATION_RVA


def agent_source() -> str:
    """Return the delayed, signature-gated Frida register guard."""
    return r"""
'use strict';
const moduleName = %s;
  const callSiteRva = %d;
  const callSiteSignature = %s;
  const routerReadyRva = %d;
  const routerReadySignature = %s;
const registrationLoadRva = %d;
const registrationLoadSignature = %s;
const squadSetupLoadRva = %d;
const squadSetupLoadSignature = %s;
const registrationDestinationRva = %d;
const registrationDestinationBytes = %s;
const squadSetupDestinationRva = %d;
const squadSetupDestinationBytes = %s;
const scheduleDestinationRva = %d;
const scheduleDestinationBytes = %s;
const pollIntervalMs = %d;
const pollAttempts = %d;
const maximumDiagnosticEvents = %d;
let diagnosticEvents = 0;
let nextRequestId = 1;
  let routerPrearmEvents = 0;

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

function attach(cards) {
  // The passive Champions observer also hooks the Schedule LEA. Both scripts
  // poll for CardsDLL independently, so validating that live prologue here can
  // race the observer's Interceptor trampoline. RC104 hit exactly that race:
  // the guard refused before arming and stock Registration then opened. The
  // host already validates the untouched on-disk Schedule signature before
  // either script loads, so keep the live guard checks to its own hook sites
  // and immutable destination strings.
  const checks = [
    { address: cards.base.add(callSiteRva),
      expected: callSiteSignature, label: 'call site' },
    { address: cards.base.add(routerReadyRva),
      expected: routerReadySignature, label: 'router-ready site' },
    { address: cards.base.add(registrationLoadRva),
      expected: registrationLoadSignature, label: 'Registration load' },
    { address: cards.base.add(squadSetupLoadRva),
      expected: squadSetupLoadSignature, label: 'Squad Setup load' },
    { address: cards.base.add(registrationDestinationRva),
      expected: registrationDestinationBytes,
      label: 'GotoRegistration destination' },
    { address: cards.base.add(squadSetupDestinationRva),
      expected: squadSetupDestinationBytes,
      label: 'GotoSquadSetup destination' },
    { address: cards.base.add(scheduleDestinationRva),
      expected: scheduleDestinationBytes,
      label: 'GotoChampionsSchedule destination' }
  ];
  for (let index = 0; index < checks.length; index++) {
    const check = checks[index];
    if (!matches(check.address, check.expected)) {
      send({event: 'refused',
        reason: 'Champions Registration guard signature mismatch',
        check: check.label});
      return;
    }
  }

  const registrationDestination = cards.base.add(
    registrationDestinationRva);
  const squadSetupDestination = cards.base.add(squadSetupDestinationRva);
  const scheduleDestination = cards.base.add(scheduleDestinationRva);
  const pendingDestinations = new Map();
  const attachedNavigationTargets = new Set();

  function armNavigationTarget(context) {
    let navigationTarget = null;
    let range = null;
    try {
      const navigationVtable = context.rdi.readPointer();
      navigationTarget = navigationVtable.add(0x78).readPointer();
      range = Process.findRangeByAddress(navigationTarget);
    } catch (_) {
      navigationTarget = null;
    }
    if (navigationTarget === null || navigationTarget.isNull() ||
        range === null || range.protection.indexOf('x') === -1) {
      if (diagnosticEvents < maximumDiagnosticEvents) {
        diagnosticEvents++;
        send({event: 'fut-champions-navigation-target-refused',
          reason: 'router-target-not-executable'});
      }
      return null;
    }
    const key = navigationTarget.toString();
    if (attachedNavigationTargets.has(key)) return navigationTarget;
    try {
      Interceptor.attach(navigationTarget, {
        onEnter() {
          const threadId = Process.getCurrentThreadId();
          const override = pendingDestinations.get(threadId);
          if (override === undefined) return;
          pendingDestinations.delete(threadId);
          const original = this.context.rdx;
          this.context.rdx = override.destination;
          const applied = this.context.rdx.equals(override.destination);
          if (diagnosticEvents < maximumDiagnosticEvents) {
            diagnosticEvents++;
            send({event: applied ?
              'fut-champions-offline-entry-applied' :
              'fut-champions-offline-entry-failed',
              requestId: override.requestId, trigger: override.trigger,
              status: override.status,
              reason: applied ? null : 'router-argument-verification-failed',
              navigationTarget: navigationTarget.toString(),
              originalDestination: original.toString(),
              destination: this.context.rdx.toString(),
              threadId: threadId});
          }
        }
      });
      attachedNavigationTargets.add(key);
      if (diagnosticEvents < maximumDiagnosticEvents) {
        diagnosticEvents++;
        send({event: 'fut-champions-navigation-target-attached',
          navigationTarget: navigationTarget.toString(),
          module: range.file === undefined || range.file === null ?
            null : range.file.path});
      }
    } catch (error) {
      if (diagnosticEvents < maximumDiagnosticEvents) {
        diagnosticEvents++;
        send({event: 'fut-champions-navigation-target-refused',
          reason: String(error), navigationTarget: key});
      }
      return null;
    }
    return navigationTarget;
  }

  function resolveEntry(context, trigger) {
    const navigationTarget = armNavigationTarget(context);
    if (navigationTarget === null) return;
    const requestId = nextRequestId++;
    let response = null;
    const pending = recv('fut-champions-offline-entry-result',
      function (message) {
        const candidate = message.payload || {};
        if (Number(candidate.requestId || 0) === requestId)
          response = candidate;
      });
    send({event: 'fut-champions-offline-entry-request',
      requestId: requestId, trigger: trigger});
    // Enrollment mutates server state, so native navigation may continue only
    // after the host has returned the matching committed receipt.
    pending.wait();
    const status = response === null ? 'error' : String(response.status);
    const accepted = status === 'registered' || status === 'resume';
    const destination = accepted ? squadSetupDestination : scheduleDestination;
    const threadId = Process.getCurrentThreadId();
    pendingDestinations.set(threadId, {destination: destination,
      requestId: requestId, trigger: trigger, status: status});
    // The call follows the guarded LEA immediately. Expire a missed override
    // so it can never affect an unrelated later navigation on this thread.
    setTimeout(function () {
      const queued = pendingDestinations.get(threadId);
      if (queued !== undefined && queued.requestId === requestId) {
        pendingDestinations.delete(threadId);
        if (diagnosticEvents < maximumDiagnosticEvents) {
          diagnosticEvents++;
          send({event: 'fut-champions-offline-entry-failed',
            requestId: requestId, trigger: trigger, status: status,
            reason: 'router-call-not-observed', threadId: threadId});
        }
      }
    }, 1000);
    if (diagnosticEvents < maximumDiagnosticEvents) {
      diagnosticEvents++;
      send({event: 'fut-champions-offline-entry-queued',
        requestId: requestId, trigger: trigger, status: status,
        navigationTarget: navigationTarget.toString(),
        destination: destination.toString(), threadId: threadId});
    }
  }

  // RC101 proved that installing the target hook from either destination LEA
  // is too late for that same native call. At BFB70 RDI already owns the
  // router and ESI still owns the action id, so action 0x10 can pre-arm the
  // exact virtual target before the client evaluates country or destination.
  Interceptor.attach(cards.base.add(routerReadyRva), {
    onEnter() {
      if (this.context.rsi.toInt32() !== 0x10) return;
      if (routerPrearmEvents++ < 8)
        send({event: 'fut-champions-router-prearm-hit', action: 0x10,
          threadId: Process.getCurrentThreadId(),
          pc: this.context.pc.toString()});
      resolveEntry(this.context, 'action-0x10');
    }
  });
  send({event: 'fut-champions-registration-guard-attached',
    module: moduleName,
    hookSiteRvas: ['0x' + routerReadyRva.toString(16)],
    callSiteRva: '0x' + callSiteRva.toString(16),
    registrationDestinationRva:
      '0x' + registrationDestinationRva.toString(16),
    squadSetupDestinationRva:
      '0x' + squadSetupDestinationRva.toString(16)});
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

send({event: 'armed', guard: 'fut-champions-registration',
  module: moduleName, state: 'waiting-for-module',
  pollIntervalMs: pollIntervalMs, pollAttempts: pollAttempts});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        CALL_SITE_RVA,
        json.dumps(list(CALL_SITE_SIGNATURE)),
        ROUTER_READY_RVA,
        json.dumps(list(ROUTER_READY_SIGNATURE)),
        REGISTRATION_LOAD_RVA,
        json.dumps(list(REGISTRATION_LOAD_SIGNATURE)),
        SQUAD_SETUP_LOAD_RVA,
        json.dumps(list(SQUAD_SETUP_LOAD_SIGNATURE)),
        REGISTRATION_DESTINATION_RVA,
        json.dumps(list(REGISTRATION_DESTINATION.encode("ascii") + b"\0")),
        SQUAD_SETUP_DESTINATION_RVA,
        json.dumps(list(SQUAD_SETUP_DESTINATION.encode("ascii") + b"\0")),
        SCHEDULE_DESTINATION_RVA,
        json.dumps(list(SCHEDULE_DESTINATION.encode("ascii") + b"\0")),
        MODULE_POLL_INTERVAL_MS,
        MODULE_POLL_ATTEMPTS,
        MAX_DIAGNOSTIC_EVENTS,
    )

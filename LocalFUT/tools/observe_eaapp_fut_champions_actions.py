#!/usr/bin/env python3
"""Build a passive observer for FUT Champions navigation and Leaderboard.

The competition core cannot accept a difficulty choice until the client entry
path is demonstrated.  This observer records only the action id in EDX and the
opaque payload pointer in R8 when the verified Champions handler is entered.
The action-0x10 branch contains three verified navigation literals.  Recording
which literal-loading instruction CardsDLL reaches distinguishes Registration,
Squad Setup and Schedule without dereferencing model state or calling client
code.  The controller delegates its tile action to the gate router at RVA
0xBFB00.  That router's action-0x10 branch reads four small, statically proved
fields before navigating; observing those reads distinguishes a malformed Hub
projection from a missing click.  The observer writes no process state and
changes no control flow.  The Leaderboard view has a separate handler and data
provider dispatch, so each receives its own bounded counter; otherwise the
normal Hub/Schedule event burst can consume the primary budget before a
Leaderboard failure is reached.
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

# Verified against the exact EA App image above.  The 16-byte prologue is
# unique in the installed DLL; the primary Champions vtable points here from
# RVA 0x3647f8.
HANDLER_RVA = 0xBFFA0
HANDLER_POINTER_SLOT_RVA = 0x3647F8
HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "488bc4574881eca001000048c7442430"
))

# The Leaderboard viewmodel factory at RVA 0xC69A0 installs the vtable at
# 0x364E00.  Its action slot points to this handler from 0x364E08.  Action
# 0x35 enters the ranking path; the adjacent provider dispatcher receives
# FUT_LEADERBOARD_DP (0x7574) or FUT_LEADERBOARD_ENTRY_DATA_DP (0x7575).
LEADERBOARD_HANDLER_RVA = 0xC3FB0
LEADERBOARD_HANDLER_POINTER_SLOT_RVA = 0x364E08
LEADERBOARD_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "48895c24104889742420574881ec3001"
))
LEADERBOARD_PROVIDER_DISPATCH_RVA = 0xC43E0
LEADERBOARD_PROVIDER_DISPATCH_SIGNATURE = tuple(bytes.fromhex(
    "81ea8b270000741981eae94d0000740c"
))

# The primary handler calls this gate router at RVA 0xC04F2 after translating
# the UI event.  Its action-0x10 branch is the only code which reaches the
# Registration/Squad Setup destinations below.
GATE_ROUTER_RVA = 0xBFB00
GATE_ROUTER_SIGNATURE = tuple(bytes.fromhex(
    "4c89442418555657415641574883ec50"
))
GATE_ROUTER_CALL_SITE_RVA = 0xC04F2
GATE_ROUTER_CALL_SIGNATURE = tuple(bytes.fromhex("e809f6ffffeb71"))

# These class and action destinations bound the probe to the Champions UI
# family whose handler is referenced by the vtable above.
CHAMPIONS_STRINGS = (
    (0x359E60, "futonlinechampionsviewmodel"),
    (0x359EC0, "futchampionsscheduleviewmodel"),
    (0x359F00, "futchampionsregistrationviewmodel"),
    (0x359F28, "futchampionsleaderboardsviewmodel"),
    (0x3644F8, "GotoChampionsSchedule"),
    (0x364420, "GotoRegistration"),
    (0x364438, "GotoSquadSetup"),
    (0x35C750, "GotoNewItems"),
)

MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900
MAX_CAPTURES = 256
MAX_DESTINATION_CAPTURES = 8
MAX_GATE_CAPTURES = 8
MAX_LEADERBOARD_CAPTURES = 16
MAX_REGISTRATION_CAPTURES = 12

# RC101 requested /user/pidinfo twice without reaching either guarded PLAY
# destination.  The Registration viewmodel factory is the earlier independent
# boundary: it installs the vtable at 0x367C78, binds the 0x7530 endpoint and
# immediately starts GetChampionsUserCountry.  The endpoint's path method is
# the only code reference to /pidinfo.  Observing both boundaries distinguishes
# a registry-created Registration viewmodel from the already disproved PLAY
# destination loads without changing either path.
REGISTRATION_FACTORY_RVA = 0xFA250
REGISTRATION_FACTORY_SIGNATURE = tuple(bytes.fromhex(
    "4c89442418555657488bec4881ec8000000048c745e8feff"
))
PIDINFO_PATH_METHOD_RVA = 0x2A9060
PIDINFO_PATH_METHOD_POINTER_SLOT_RVA = 0x3874F0
PIDINFO_PATH_METHOD_SIGNATURE = tuple(bytes.fromhex(
    "488b05f96a1800488bca458bc0488d1534e40d0048ffa0b8000000"
))
MAX_REGISTRATION_FACTORY_CAPTURES = 4
MAX_PIDINFO_CAPTURES = 6
MAX_BACKTRACE_FRAMES = 12

# The Registration viewmodel vtable begins at RVA 0x367C78.  Its action slot
# points to 0xF9D10. Action 3 accepts YES only when the UI event contains
# NATION_ISO: 0xF9D5C holds the property-test result in AL and 0xF9E4B is the
# proved POST executor call.  These probes observe that boundary without
# synthesising a click, property or request.
REGISTRATION_HANDLER_RVA = 0xF9D10
REGISTRATION_HANDLER_POINTER_SLOT_RVA = 0x367C80
REGISTRATION_HANDLER_SIGNATURE = tuple(bytes.fromhex(
    "488bc455488d68a14881ecc000000048c74527fe"
))
REGISTRATION_SITES = (
    (
        "nationIsoProperty",
        0xF9D5C,
        tuple(bytes.fromhex("84c00f8427010000")),
    ),
    (
        "registrationPost",
        0xF9E4B,
        tuple(bytes.fromhex("e8e0a41a0090")),
    ),
)

# GetChampionsUserCountry builds /pidinfo at 0x2A906D. Its response parser
# dispatches member 0xBD (competitionCountryCode in the exact member table)
# at 0x2A91C3 and stores that string in the country object consumed by the
# Registration viewmodel.
PIDINFO_PATH_RVA = 0x3874A8
PIDINFO_PATH = "/pidinfo"
PIDINFO_PATH_XREF_RVA = 0x2A906D
PIDINFO_PATH_XREF_SIGNATURE = tuple(bytes.fromhex(
    "488d1534e40d0048ffa0b8000000"
))
PIDINFO_COUNTRY_DISPATCH_RVA = 0x2A91C3
PIDINFO_COUNTRY_DISPATCH_SIGNATURE = tuple(bytes.fromhex(
    "81ffbd000000740c488d542430"
))

# These probes sit exactly on the reads performed by action 0x10.  At 0xBFBFB
# RAX is the Hub projection base; at 0xBFC6C RBP is the selected-event base;
# at 0xBFCE9 AL is the result of comparing competitionCountryCode with empty.
GATE_SITES = (
    (
        "hubReady",
        0xBFBFB,
        tuple(bytes.fromhex("80b8b05f000000488b059fd03600")),
    ),
    (
        "eventProjection",
        0xBFC6C,
        tuple(bytes.fromhex("807d29000f84c2010000")),
    ),
    (
        "countryNonEmpty",
        0xBFCE9,
        tuple(bytes.fromhex("84c0488b07740f")),
    ),
)

# Reaching one of these literal-loading instructions proves the destination
# selected by CardsDLL's own action-0x10 state checks.  Hook the LEA rather than
# the following virtual call so the observer never calls or alters UI code.
DESTINATION_SITES = (
    (
        "GotoSquadSetup",
        0xBFCF0,
        tuple(bytes.fromhex("488d1541472a00ff5078")),
    ),
    (
        "GotoRegistration",
        0xBFCFF,
        tuple(bytes.fromhex("488d151a472a00ff5078")),
    ),
    (
        "GotoChampionsSchedule",
        0xC008A,
        tuple(bytes.fromhex("488d1567442a00488bcbff5078")),
    ),
)

# The active Registration guard owns these two instruction boundaries before
# this observer loads. Frida replaces their live prologues, so re-validating
# them in a second script made RC101's only route observer refuse before it
# could attach to the untouched action and gate sites. The disk-image verifier
# still checks all three exact signatures; runtime observation keeps only the
# non-overlapping Schedule destination.
PASSIVE_DESTINATION_SITES = tuple(
    entry for entry in DESTINATION_SITES
    if entry[0] not in {"GotoSquadSetup","GotoRegistration"}
)


def _pe_layout(image: bytes) -> tuple[int, int, int]:
    """Return image base, section count and section-table offset."""
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
    """Refuse any disk image outside the exact observed Champions contract."""
    if product_version != EXPECTED_PRODUCT_VERSION:
        raise ValueError("CardsDLL product version mismatch")
    if hashlib.sha256(image).hexdigest() != EXPECTED_CARDS_SHA256:
        raise ValueError("CardsDLL fingerprint mismatch")

    signature = bytes(HANDLER_SIGNATURE)
    if _bytes_at(image, HANDLER_RVA, len(signature)) != signature:
        raise ValueError("Champions action handler signature mismatch")
    if image.count(signature) != 1:
        raise ValueError("Champions action handler signature is not unique")

    leaderboard_signature = bytes(LEADERBOARD_HANDLER_SIGNATURE)
    if _bytes_at(image, LEADERBOARD_HANDLER_RVA, len(
            leaderboard_signature)) != leaderboard_signature:
        raise ValueError("Champions Leaderboard handler signature mismatch")
    if image.count(leaderboard_signature) != 1:
        raise ValueError(
            "Champions Leaderboard handler signature is not unique")
    provider_signature = bytes(LEADERBOARD_PROVIDER_DISPATCH_SIGNATURE)
    if _bytes_at(image, LEADERBOARD_PROVIDER_DISPATCH_RVA, len(
            provider_signature)) != provider_signature:
        raise ValueError(
            "Champions Leaderboard provider signature mismatch")
    if image.count(provider_signature) != 1:
        raise ValueError(
            "Champions Leaderboard provider signature is not unique")

    registration_signature = bytes(REGISTRATION_HANDLER_SIGNATURE)
    if _bytes_at(image, REGISTRATION_HANDLER_RVA, len(
            registration_signature)) != registration_signature:
        raise ValueError("Champions Registration handler signature mismatch")
    if image.count(registration_signature) != 1:
        raise ValueError(
            "Champions Registration handler signature is not unique")

    router_signature = bytes(GATE_ROUTER_SIGNATURE)
    if _bytes_at(image, GATE_ROUTER_RVA, len(
            router_signature)) != router_signature:
        raise ValueError("Champions gate router signature mismatch")
    if image.count(router_signature) != 1:
        raise ValueError("Champions gate router signature is not unique")
    if _bytes_at(image, GATE_ROUTER_CALL_SITE_RVA, len(
            GATE_ROUTER_CALL_SIGNATURE)) != bytes(GATE_ROUTER_CALL_SIGNATURE):
        raise ValueError("Champions gate router call-site mismatch")

    image_base, _, _ = _pe_layout(image)
    pointer = struct.unpack(
        "<Q", _bytes_at(image, HANDLER_POINTER_SLOT_RVA, 8)
    )[0]
    if pointer - image_base != HANDLER_RVA:
        raise ValueError("Champions action handler vtable mismatch")
    leaderboard_pointer = struct.unpack(
        "<Q", _bytes_at(
            image, LEADERBOARD_HANDLER_POINTER_SLOT_RVA, 8)
    )[0]
    if leaderboard_pointer - image_base != LEADERBOARD_HANDLER_RVA:
        raise ValueError("Champions Leaderboard handler vtable mismatch")
    registration_pointer = struct.unpack(
        "<Q", _bytes_at(
            image, REGISTRATION_HANDLER_POINTER_SLOT_RVA, 8)
    )[0]
    if registration_pointer - image_base != REGISTRATION_HANDLER_RVA:
        raise ValueError("Champions Registration handler vtable mismatch")

    factory_signature = bytes(REGISTRATION_FACTORY_SIGNATURE)
    if _bytes_at(image, REGISTRATION_FACTORY_RVA, len(
            factory_signature)) != factory_signature:
        raise ValueError("Champions Registration factory signature mismatch")
    if image.count(factory_signature) != 1:
        raise ValueError(
            "Champions Registration factory signature is not unique")

    pidinfo_method_signature = bytes(PIDINFO_PATH_METHOD_SIGNATURE)
    if _bytes_at(image, PIDINFO_PATH_METHOD_RVA, len(
            pidinfo_method_signature)) != pidinfo_method_signature:
        raise ValueError("Champions pidinfo path method signature mismatch")
    if image.count(pidinfo_method_signature) != 1:
        raise ValueError("Champions pidinfo path method is not unique")
    pidinfo_method_pointer = struct.unpack(
        "<Q", _bytes_at(
            image, PIDINFO_PATH_METHOD_POINTER_SLOT_RVA, 8)
    )[0]
    if pidinfo_method_pointer - image_base != PIDINFO_PATH_METHOD_RVA:
        raise ValueError("Champions pidinfo path method vtable mismatch")

    for rva, expected in CHAMPIONS_STRINGS:
        encoded = expected.encode("ascii") + b"\0"
        if _bytes_at(image, rva, len(encoded)) != encoded:
            raise ValueError(
                "Champions string mismatch at RVA 0x%x" % rva)
    for name, rva, expected in DESTINATION_SITES:
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError(
                "Champions destination %s signature mismatch" % name)
    for name, rva, expected in GATE_SITES:
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError(
                "Champions gate %s signature mismatch" % name)
    for name, rva, expected in REGISTRATION_SITES:
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError(
                "Champions Registration %s signature mismatch" % name)
    pidinfo_path = PIDINFO_PATH.encode("ascii") + b"\0"
    if _bytes_at(image, PIDINFO_PATH_RVA, len(pidinfo_path)) != pidinfo_path:
        raise ValueError("Champions pidinfo path mismatch")
    for rva, expected, label in (
            (PIDINFO_PATH_XREF_RVA, PIDINFO_PATH_XREF_SIGNATURE,
             "path xref"),
            (PIDINFO_COUNTRY_DISPATCH_RVA,
             PIDINFO_COUNTRY_DISPATCH_SIGNATURE,
             "country dispatch")):
        signature = bytes(expected)
        if _bytes_at(image, rva, len(signature)) != signature:
            raise ValueError("Champions pidinfo %s mismatch" % label)


def agent_source() -> str:
    """Return the signature-gated, observation-only Frida agent."""
    strings = json.dumps([
        {
            "rva": rva,
            "value": value,
            "bytes": list(value.encode("ascii") + b"\0"),
        }
        for rva, value in CHAMPIONS_STRINGS
    ])
    destinations = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in PASSIVE_DESTINATION_SITES
    ])
    gates = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in GATE_SITES
    ])
    registration_sites = json.dumps([
        {"name": name, "rva": rva, "signature": list(signature)}
        for name, rva, signature in REGISTRATION_SITES
    ])
    return """
'use strict';
const moduleName = %s;
const handlerRva = %d;
const handlerSignature = %s;
const leaderboardHandlerRva = %d;
const leaderboardHandlerSignature = %s;
const leaderboardProviderDispatchRva = %d;
const leaderboardProviderDispatchSignature = %s;
const registrationHandlerRva = %d;
const registrationHandlerSignature = %s;
const registrationFactoryRva = %d;
const registrationFactorySignature = %s;
const pidinfoPathMethodRva = %d;
const pidinfoPathMethodSignature = %s;
const gateRouterRva = %d;
const gateRouterSignature = %s;
const championsStrings = %s;
const destinations = %s;
const gates = %s;
const registrationSites = %s;
const pollIntervalMs = %d;
const pollAttempts = %d;
const maxCaptures = %d;
const maxDestinationCaptures = %d;
const maxGateCaptures = %d;
const maxLeaderboardCaptures = %d;
const maxRegistrationCaptures = %d;
const maxRegistrationFactoryCaptures = %d;
const maxPidinfoCaptures = %d;
const maxBacktraceFrames = %d;

let captures = 0;
let destinationCaptures = 0;
let gateCaptures = 0;
let leaderboardCaptures = 0;
let registrationCaptures = 0;
let registrationFactoryCaptures = 0;
let pidinfoCaptures = 0;

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

function pointerText(value) {
  try { return value.isNull() ? null : value.toString(); }
  catch (_) { return null; }
}

function cardsBacktrace(cards, context) {
  try {
    return Thread.backtrace(context, Backtracer.ACCURATE).slice(
      0, maxBacktraceFrames).map(function (address) {
        if (address.compare(cards.base) >= 0 &&
            address.compare(cards.base.add(cards.size)) < 0)
          return 'CardsDLL+0x' + address.sub(cards.base).toString(16);
        return DebugSymbol.fromAddress(address).toString();
      });
  } catch (error) {
    return ['backtrace-error: ' + String(error)];
  }
}

function attach(cards) {
  if (!matches(cards.base.add(handlerRva), handlerSignature)) {
    send({event: 'refused',
      reason: 'Champions action handler signature mismatch',
      rva: '0x' + handlerRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(gateRouterRva), gateRouterSignature)) {
    send({event: 'refused',
      reason: 'Champions gate router signature mismatch',
      rva: '0x' + gateRouterRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(
      leaderboardHandlerRva), leaderboardHandlerSignature)) {
    send({event: 'refused',
      reason: 'Champions Leaderboard handler signature mismatch',
      rva: '0x' + leaderboardHandlerRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(
      leaderboardProviderDispatchRva),
      leaderboardProviderDispatchSignature)) {
    send({event: 'refused',
      reason: 'Champions Leaderboard provider signature mismatch',
      rva: '0x' + leaderboardProviderDispatchRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(
      registrationHandlerRva), registrationHandlerSignature)) {
    send({event: 'refused',
      reason: 'Champions Registration handler signature mismatch',
      rva: '0x' + registrationHandlerRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(
      registrationFactoryRva), registrationFactorySignature)) {
    send({event: 'refused',
      reason: 'Champions Registration factory signature mismatch',
      rva: '0x' + registrationFactoryRva.toString(16)});
    return;
  }
  if (!matches(cards.base.add(
      pidinfoPathMethodRva), pidinfoPathMethodSignature)) {
    send({event: 'refused',
      reason: 'Champions pidinfo path method signature mismatch',
      rva: '0x' + pidinfoPathMethodRva.toString(16)});
    return;
  }
  for (let index = 0; index < championsStrings.length; index++) {
    const entry = championsStrings[index];
    if (!matches(cards.base.add(entry.rva), entry.bytes)) {
      send({event: 'refused', reason: 'Champions class/string mismatch',
        rva: '0x' + entry.rva.toString(16), expected: entry.value});
      return;
    }
  }
  for (let index = 0; index < destinations.length; index++) {
    const entry = destinations[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused',
        reason: 'Champions destination signature mismatch',
        destination: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }
  for (let index = 0; index < gates.length; index++) {
    const entry = gates[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused', reason: 'Champions gate signature mismatch',
        gate: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }
  for (let index = 0; index < registrationSites.length; index++) {
    const entry = registrationSites[index];
    if (!matches(cards.base.add(entry.rva), entry.signature)) {
      send({event: 'refused',
        reason: 'Champions Registration site signature mismatch',
        site: entry.name, rva: '0x' + entry.rva.toString(16)});
      return;
    }
  }

  Interceptor.attach(cards.base.add(handlerRva), {
    onEnter(args) {
      if (captures >= maxCaptures) return;
      send({event: 'fut-champions-action', capture: ++captures,
        actionId: args[1].toUInt32(), payload: pointerText(args[2])});
    }
  });
  Interceptor.attach(cards.base.add(gateRouterRva), {
    onEnter(args) {
      if (captures >= maxCaptures) return;
      send({event: 'fut-champions-gate-action', capture: ++captures,
        actionId: args[1].toUInt32(), payload: pointerText(args[2])});
    }
  });
  Interceptor.attach(cards.base.add(registrationHandlerRva), {
    onEnter(args) {
      if (registrationCaptures >= maxRegistrationCaptures) return;
      send({event: 'fut-champions-registration-action',
        capture: ++registrationCaptures,
        actionId: args[1].toUInt32(), payload: pointerText(args[2])});
    }
  });
  Interceptor.attach(cards.base.add(registrationFactoryRva), {
    onEnter(args) {
      if (registrationFactoryCaptures >=
          maxRegistrationFactoryCaptures) return;
      this.captureRegistrationFactory = ++registrationFactoryCaptures;
      send({event: 'fut-champions-registration-factory-enter',
        capture: this.captureRegistrationFactory,
        argument0: pointerText(args[0]), argument1: pointerText(args[1]),
        argument2: pointerText(args[2]),
        backtrace: cardsBacktrace(cards, this.context)});
    },
    onLeave(retval) {
      if (!this.captureRegistrationFactory) return;
      send({event: 'fut-champions-registration-factory-leave',
        capture: this.captureRegistrationFactory,
        result: pointerText(retval)});
    }
  });
  Interceptor.attach(cards.base.add(pidinfoPathMethodRva), {
    onEnter(args) {
      if (pidinfoCaptures >= maxPidinfoCaptures) return;
      send({event: 'fut-champions-pidinfo-path-method',
        capture: ++pidinfoCaptures,
        argument0: pointerText(args[0]), argument1: pointerText(args[1]),
        argument2: pointerText(args[2]),
        backtrace: cardsBacktrace(cards, this.context)});
    }
  });
  Interceptor.attach(cards.base.add(leaderboardHandlerRva), {
    onEnter(args) {
      if (leaderboardCaptures >= maxLeaderboardCaptures) return;
      this.captureLeaderboard = true;
      this.leaderboardActionId = args[1].toUInt32();
      this.leaderboardViewModel = args[0];
      const result = {event: 'fut-champions-leaderboard-handler-enter',
        capture: ++leaderboardCaptures,
        actionId: this.leaderboardActionId,
        payload: pointerText(args[2])};
      try { result.viewState = args[0].add(0x150).readS32(); }
      catch (error) { result.readError = String(error); }
      send(result);
    },
    onLeave(retval) {
      if (!this.captureLeaderboard) return;
      const result = {event: 'fut-champions-leaderboard-handler-leave',
        capture: leaderboardCaptures,
        actionId: this.leaderboardActionId,
        returnValue: pointerText(retval)};
      try {
        result.viewStateAfter = this.leaderboardViewModel.add(0x150).readS32();
      } catch (error) {
        result.readError = String(error);
      }
      send(result);
    }
  });
  Interceptor.attach(cards.base.add(leaderboardProviderDispatchRva), {
    onEnter(args) {
      if (leaderboardCaptures >= maxLeaderboardCaptures) return;
      send({event: 'fut-champions-leaderboard-provider',
        capture: ++leaderboardCaptures,
        providerId: args[1].toUInt32(), payload: pointerText(args[2])});
    }
  });
  for (let index = 0; index < gates.length; index++) {
    const entry = gates[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter() {
        if (gateCaptures >= maxGateCaptures) return;
        const result = {event: 'fut-champions-gate',
          capture: ++gateCaptures, gate: entry.name,
          rva: '0x' + entry.rva.toString(16)};
        try {
          if (entry.name === 'hubReady') {
            result.value = this.context.rax.add(0x5fb0).readU8();
          } else if (entry.name === 'eventProjection') {
            const selected = this.context.rbp;
            result.active = selected.add(0x29).readU8();
            result.state = selected.add(0x10).readS32();
            result.qualified = selected.add(0x28).readU8();
            result.remaining = selected.add(0x14).readS32();
          } else if (entry.name === 'countryNonEmpty') {
            result.value = this.context.rax.toUInt32() & 0xff;
          }
        } catch (error) {
          result.readError = String(error);
        }
        send(result);
      }
    });
  }
  for (let index = 0; index < destinations.length; index++) {
    const entry = destinations[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter() {
        if (destinationCaptures >= maxDestinationCaptures) return;
        send({event: 'fut-champions-destination',
          capture: ++destinationCaptures, destination: entry.name,
          rva: '0x' + entry.rva.toString(16)});
      }
    });
  }
  for (let index = 0; index < registrationSites.length; index++) {
    const entry = registrationSites[index];
    Interceptor.attach(cards.base.add(entry.rva), {
      onEnter() {
        if (registrationCaptures >= maxRegistrationCaptures) return;
        const result = {event: 'fut-champions-registration-site',
          capture: ++registrationCaptures, site: entry.name,
          rva: '0x' + entry.rva.toString(16)};
        if (entry.name === 'nationIsoProperty')
          result.present = (this.context.rax.toUInt32() & 0xff) !== 0;
        send(result);
      }
    });
  }
  send({event: 'fut-champions-actions-observer-attached',
    module: cards.name, base: cards.base.toString(),
    handlerRva: '0x' + handlerRva.toString(16),
    leaderboardHandlerRva: '0x' + leaderboardHandlerRva.toString(16),
    leaderboardProviderDispatchRva:
      '0x' + leaderboardProviderDispatchRva.toString(16),
    maxLeaderboardCaptures: maxLeaderboardCaptures,
    registrationHandlerRva:
      '0x' + registrationHandlerRva.toString(16),
    registrationFactoryRva:
      '0x' + registrationFactoryRva.toString(16),
    pidinfoPathMethodRva:
      '0x' + pidinfoPathMethodRva.toString(16),
    registrationSites: registrationSites.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    }), maxRegistrationCaptures: maxRegistrationCaptures,
    maxRegistrationFactoryCaptures: maxRegistrationFactoryCaptures,
    maxPidinfoCaptures: maxPidinfoCaptures,
    maxBacktraceFrames: maxBacktraceFrames,
    gateRouterRva: '0x' + gateRouterRva.toString(16),
    maxCaptures: maxCaptures,
    destinations: destinations.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    }), maxDestinationCaptures: maxDestinationCaptures,
    gates: gates.map(function (entry) {
      return {name: entry.name, rva: '0x' + entry.rva.toString(16)};
    }), maxGateCaptures: maxGateCaptures});
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
    send({event: 'fut-champions-actions-observer-module-absent',
      module: moduleName});
    return;
  }
  setTimeout(waitForModule, pollIntervalMs);
}

send({event: 'armed', observer: 'fut-champions-actions', module: moduleName,
  state: 'waiting-for-module', pollIntervalMs: pollIntervalMs,
  pollAttempts: pollAttempts});
waitForModule();
""" % (
        json.dumps(MODULE_NAME),
        HANDLER_RVA,
        json.dumps(list(HANDLER_SIGNATURE)),
        LEADERBOARD_HANDLER_RVA,
        json.dumps(list(LEADERBOARD_HANDLER_SIGNATURE)),
        LEADERBOARD_PROVIDER_DISPATCH_RVA,
        json.dumps(list(LEADERBOARD_PROVIDER_DISPATCH_SIGNATURE)),
        REGISTRATION_HANDLER_RVA,
        json.dumps(list(REGISTRATION_HANDLER_SIGNATURE)),
        REGISTRATION_FACTORY_RVA,
        json.dumps(list(REGISTRATION_FACTORY_SIGNATURE)),
        PIDINFO_PATH_METHOD_RVA,
        json.dumps(list(PIDINFO_PATH_METHOD_SIGNATURE)),
        GATE_ROUTER_RVA,
        json.dumps(list(GATE_ROUTER_SIGNATURE)),
        strings,
        destinations,
        gates,
        registration_sites,
        MODULE_POLL_INTERVAL_MS,
        MODULE_POLL_ATTEMPTS,
        MAX_CAPTURES,
        MAX_DESTINATION_CAPTURES,
        MAX_GATE_CAPTURES,
        MAX_LEADERBOARD_CAPTURES,
        MAX_REGISTRATION_CAPTURES,
        MAX_REGISTRATION_FACTORY_CAPTURES,
        MAX_PIDINFO_CAPTURES,
        MAX_BACKTRACE_FRAMES,
    )

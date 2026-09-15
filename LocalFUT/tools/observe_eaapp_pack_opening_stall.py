#!/usr/bin/env python3
"""Build a passive observer for the EA App native pack-opening path.

Seven consecutive live pack openings on the EA App build returned HTTP 200 and
then produced no further client request and no native evidence.  The guarded
runner already attaches to FIFA, but it loads only the certificate,
eligibility and Origin-session guards, so a stall inside the native
create-pack path is invisible.

This observer reports where the client stops.  It attaches to three verified
sites in ``CardsDLL_Win64_retail.dll``:

* the create-pack response factory, which allocates the
  ``RS4:FutCreatePackServerResponse`` object and installs its vtable;
* that response object's single virtual method, which consumes the server
  response;
* the response object destructor.

Together they answer the open question directly.  If the factory runs but the
virtual method is never entered, the response never reached the parser.  If
the method is entered and never left, the stall is inside response decoding.
If it is entered and left normally and nothing follows, the stall is in the
native presentation stage instead of the response contract.

A bounded watchdog additionally samples thread program counters and fuzzy
backtraces after the request is issued, so a deadlock is distinguishable from
a fault, and a bounded exception reporter records native exceptions without
suppressing them.

The observer is strictly read-only.  It never writes registers, memory,
process state or control flow, never changes a return value, and never
suppresses an exception.  Every site is verified against an exact byte
signature and against the response vtable layout before anything is attached,
so the observer refuses to arm on any build other than the fingerprinted EA
App one.
"""

from __future__ import annotations

import json


MODULE_NAME = "CardsDLL_Win64_retail.dll"

# Verified read-only against CardsDLL_Win64_retail.dll, product version
# 19.0.4052077.0, sha256
# 8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a.
RESPONSE_FACTORY_RVA = 0x216720
RESPONSE_PARSE_RVA = 0x216C60
RESPONSE_DTOR_RVA = 0x216680
RESPONSE_VTABLE_RVA = 0x37BE50

# Minimal byte prefixes that are unique inside the fingerprinted image.
RESPONSE_FACTORY_SIGNATURE = (
    0x48, 0x83, 0xEC, 0x28, 0x48, 0x8B, 0x49, 0x08,
    0x4C, 0x8D, 0x05, 0x01, 0x57, 0x16, 0x00, 0x45,
)
RESPONSE_DTOR_SIGNATURE = (
    0x48, 0x89, 0x5C, 0x24, 0x08, 0x57, 0x48, 0x83,
    0xEC, 0x20, 0x48, 0x8B, 0xF9, 0x8B, 0xDA, 0x48,
    0x83, 0xC1, 0x30, 0xE8, 0x38, 0x14, 0xE4, 0xFF,
)
RESPONSE_PARSE_SIGNATURE = (
    0x40, 0x55, 0x56, 0x57, 0x41, 0x54, 0x41, 0x55,
    0x41, 0x56, 0x41, 0x57, 0x48, 0x8D, 0xAC, 0x24,
    0x50, 0xFF, 0xFF, 0xFF, 0x48, 0x81, 0xEC, 0xB0,
    0x01, 0x00, 0x00, 0x48, 0xC7, 0x44, 0x24, 0x70,
    0xFE, 0xFF, 0xFF, 0xFF, 0x48, 0x89, 0x9C, 0x24,
    0x00, 0x02, 0x00, 0x00, 0x48, 0x8B, 0x05, 0x75,
)

# CardsDLL is not loaded while FIFA sits on the main menu, so the observer
# arms immediately and attaches as soon as the module appears.
MODULE_POLL_INTERVAL_MS = 1000
MODULE_POLL_ATTEMPTS = 900

MAX_STAGE_EVENTS = 24
MAX_EXCEPTION_EVENTS = 8
# A hard process termination kills the agent before an asynchronous send() can
# be delivered, which is exactly what happened to the 2026-09-02 Store promo
# crash. Every exception is therefore also written straight to disk, flushed
# and closed on the faulting thread before the handler returns.
MAX_EXCEPTION_RECORDS = 64
# Sampling starts when the create-pack request is issued and stops well before
# a normal opening would have finished, so a completed animation stays quiet.
SAMPLE_DELAYS_MS = (4000, 10000, 20000, 40000, 70000, 110000)
MAX_SAMPLE_THREADS = 12
MAX_SAMPLE_FRAMES = 14


def _js_bytes(values: tuple[int, ...]) -> str:
    return ",".join("0x%02x" % value for value in values)


def agent_source(crash_report_path: str | None = None) -> str:
    """Build the observer agent.

    ``crash_report_path`` receives one JSON record per native exception,
    written and flushed on the faulting thread so the evidence survives a hard
    process termination. When omitted the agent still reports exceptions
    through ``send()`` only.
    """
    return """
'use strict';
const moduleName = '%s';
const factoryRva = %d;
const parseRva = %d;
const dtorRva = %d;
const vtableRva = %d;
const factorySignature = [%s];
const dtorSignature = [%s];
const parseSignature = [%s];
const pollIntervalMs = %d;
const pollAttempts = %d;
const maxStageEvents = %d;
const maxExceptionEvents = %d;
const maxExceptionRecords = %d;
const crashReportPath = %s;
const sampleDelaysMs = [%s];
const maxSampleThreads = %d;
const maxSampleFrames = %d;

let stageEvents = 0;
let exceptionEvents = 0;
let exceptionRecords = 0;
let sampleRound = 0;
let watchdogStarted = false;
let parseDepth = 0;
let parseEnteredAt = null;
let parseThreadId = null;
let sampleTimers = [];

function stage(event, fields) {
  if (stageEvents >= maxStageEvents) return;
  stageEvents++;
  const payload = fields || {};
  payload.event = event;
  payload.sequence = stageEvents;
  send(payload);
}

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

function describe(address) {
  try {
    if (address === null || address.isNull()) return null;
    const owner = Process.findModuleByAddress(address);
    if (owner === null) return address.toString();
    return owner.name + '+0x' + address.sub(owner.base).toString(16);
  } catch (_) {
    return null;
  }
}

// Read-only stack walk of a foreign thread. A fuzzy backtrace can contain
// false frames; it is reported as evidence, never used for a decision.
function backtraceOf(context) {
  const frames = [];
  try {
    const raw = Thread.backtrace(context, Backtracer.FUZZY);
    for (let index = 0; index < raw.length && index < maxSampleFrames; index++) {
      const resolved = describe(raw[index]);
      if (resolved !== null) frames.push(resolved);
    }
  } catch (_) {}
  return frames;
}

function cancelWatchdog() {
  for (let index = 0; index < sampleTimers.length; index++) {
    try { clearTimeout(sampleTimers[index]); } catch (_) {}
  }
  sampleTimers = [];
}

function sampleThreads(cards) {
  // Sampling exists only to locate a stall. Walking the stack of a live,
  // running thread is unsafe: the CPU context is a snapshot while the thread
  // keeps rewriting its stack, so a heuristic walk can dereference stale
  // pointers and take the process down. Two healthy 2026-09-02 sessions were
  // killed that way at the 70s sample, while the frozen session survived all
  // six because its stack was static. Never sample once the parse returned.
  if (parseDepth <= 0) {
    cancelWatchdog();
    return;
  }
  sampleRound++;
  let threads = [];
  try {
    threads = Process.enumerateThreads();
  } catch (_) {
    return;
  }
  // Report the threads standing inside the pack-opening code first so the
  // per-sample cap never hides the interesting one.
  const scored = [];
  for (let index = 0; index < threads.length; index++) {
    const thread = threads[index];
    let pc = null;
    try {
      pc = thread.context.pc;
    } catch (_) {
      continue;
    }
    let rank = 2;
    try {
      const owner = Process.findModuleByAddress(pc);
      if (owner !== null && owner.name === cards.name) rank = 0;
      else if (owner !== null && owner.name === 'FIFA19.exe') rank = 1;
    } catch (_) {}
    scored.push({ rank: rank, thread: thread, pc: pc });
  }
  scored.sort(function (left, right) { return left.rank - right.rank; });
  const reported = [];
  for (let index = 0; index < scored.length && index < maxSampleThreads; index++) {
    const entry = scored[index];
    // Only the thread stuck inside the parse is stack-walked. Every other
    // thread reports its program counter alone, which is enough to tell a
    // deadlock from a spin and cannot fault.
    const isStalled = parseThreadId !== null && entry.thread.id === parseThreadId;
    reported.push({
      id: entry.thread.id,
      state: entry.thread.state,
      pc: describe(entry.pc),
      stalled: isStalled,
      backtrace: isStalled ? backtraceOf(entry.thread.context) : []
    });
  }
  stage('pack-opening-thread-sample', {
    round: sampleRound,
    parseDepth: parseDepth,
    threadCount: threads.length,
    threads: reported
  });
}

function startWatchdog(cards) {
  if (watchdogStarted) return;
  watchdogStarted = true;
  for (let index = 0; index < sampleDelaysMs.length; index++) {
    sampleTimers.push(
      setTimeout(function () { sampleThreads(cards); }, sampleDelaysMs[index]));
  }
}

function attach(cards) {
  const factory = cards.base.add(factoryRva);
  const parse = cards.base.add(parseRva);
  const dtor = cards.base.add(dtorRva);
  if (!matches(factory, factorySignature) ||
      !matches(parse, parseSignature) ||
      !matches(dtor, dtorSignature)) {
    send({ event: 'refused', reason: 'pack-opening signature mismatch',
      module: cards.name, factoryRva: '0x' + factoryRva.toString(16),
      parseRva: '0x' + parseRva.toString(16),
      dtorRva: '0x' + dtorRva.toString(16) });
    return;
  }
  // The response object's vtable must hold exactly the destructor and the
  // virtual method observed here, otherwise this is not the same layout.
  let vtableOk = false;
  try {
    const vtable = cards.base.add(vtableRva);
    vtableOk = vtable.readPointer().equals(dtor) &&
               vtable.add(Process.pointerSize).readPointer().equals(parse);
  } catch (_) {
    vtableOk = false;
  }
  if (!vtableOk) {
    send({ event: 'refused', reason: 'pack-opening response vtable mismatch',
      module: cards.name, vtableRva: '0x' + vtableRva.toString(16) });
    return;
  }

  Interceptor.attach(factory, {
    onLeave(retval) {
      const response = ptr(retval.toString());
      stage('pack-opening-response-allocated', {
        threadId: Process.getCurrentThreadId(),
        responseNull: response.isNull(),
        response: response.isNull() ? null : response.toString()
      });
      startWatchdog(cards);
    }
  });
  Interceptor.attach(parse, {
    onEnter() {
      parseDepth++;
      parseEnteredAt = Date.now();
      parseThreadId = Process.getCurrentThreadId();
      stage('pack-opening-response-parse-enter', {
        threadId: parseThreadId,
        depth: parseDepth
      });
    },
    onLeave(retval) {
      const elapsed = parseEnteredAt === null ? null : Date.now() - parseEnteredAt;
      if (parseDepth > 0) parseDepth--;
      if (parseDepth === 0) {
        // The opening is healthy. Stop the watchdog before it can touch a
        // running thread's stack.
        parseThreadId = null;
        cancelWatchdog();
      }
      stage('pack-opening-response-parse-leave', {
        threadId: Process.getCurrentThreadId(),
        depth: parseDepth,
        elapsedMs: elapsed,
        returnValue: retval.toString()
      });
    }
  });
  Interceptor.attach(dtor, {
    onEnter() {
      stage('pack-opening-response-destroyed', {
        threadId: Process.getCurrentThreadId()
      });
    }
  });

  // Exceptions are reported and always handed back to the process unchanged.
  try {
    Process.setExceptionHandler(function (details) {
      let memoryAddress = null;
      let operation = null;
      try {
        if (details.memory) {
          operation = details.memory.operation;
          memoryAddress = details.memory.address === null
            ? null : details.memory.address.toString();
        }
      } catch (_) {}
      const record = {
        event: 'pack-opening-native-exception',
        type: details.type,
        address: describe(details.address),
        operation: operation,
        memoryAddress: memoryAddress,
        threadId: Process.getCurrentThreadId(),
        parseDepth: parseDepth,
        backtrace: backtraceOf(details.context)
      };
      // Durable first: a crash can terminate the process before an
      // asynchronous send() reaches the host.
      if (crashReportPath !== null && exceptionRecords < maxExceptionRecords) {
        exceptionRecords++;
        record.record = exceptionRecords;
        try {
          const handle = new File(crashReportPath, 'a');
          handle.write(JSON.stringify(record) + '\\n');
          handle.flush();
          handle.close();
        } catch (_) {}
      }
      if (exceptionEvents < maxExceptionEvents) {
        exceptionEvents++;
        record.callCount = exceptionEvents;
        send(record);
      }
      return false;
    });
  } catch (_) {}

  send({ event: 'pack-opening-observer-attached',
    module: cards.name,
    base: cards.base.toString(),
    factoryRva: '0x' + factoryRva.toString(16),
    parseRva: '0x' + parseRva.toString(16),
    dtorRva: '0x' + dtorRva.toString(16),
    vtableRva: '0x' + vtableRva.toString(16),
    crashReportPath: crashReportPath,
    sampleDelaysMs: sampleDelaysMs });
}

let pollsLeft = pollAttempts;
function waitForModule() {
  let cards = null;
  try {
    cards = Process.findModuleByName(moduleName);
  } catch (_) {
    cards = null;
  }
  if (cards !== null) {
    attach(cards);
    return;
  }
  pollsLeft--;
  if (pollsLeft <= 0) {
    send({ event: 'pack-opening-observer-module-absent', module: moduleName });
    return;
  }
  setTimeout(waitForModule, pollIntervalMs);
}

// FUT loads CardsDLL after the main menu, so arming completes immediately and
// the attach happens as soon as the module is present.
send({ event: 'armed', observer: 'pack-opening', module: moduleName,
  state: 'waiting-for-module', pollIntervalMs: pollIntervalMs,
  pollAttempts: pollAttempts });
waitForModule();
""" % (
        MODULE_NAME,
        RESPONSE_FACTORY_RVA,
        RESPONSE_PARSE_RVA,
        RESPONSE_DTOR_RVA,
        RESPONSE_VTABLE_RVA,
        _js_bytes(RESPONSE_FACTORY_SIGNATURE),
        _js_bytes(RESPONSE_DTOR_SIGNATURE),
        _js_bytes(RESPONSE_PARSE_SIGNATURE),
        MODULE_POLL_INTERVAL_MS,
        MODULE_POLL_ATTEMPTS,
        MAX_STAGE_EVENTS,
        MAX_EXCEPTION_EVENTS,
        MAX_EXCEPTION_RECORDS,
        # JSON encoding escapes Windows backslashes and yields null when the
        # caller supplies no path.
        json.dumps(crash_report_path) if crash_report_path else "null",
        ",".join(str(value) for value in SAMPLE_DELAYS_MS),
        MAX_SAMPLE_THREADS,
        MAX_SAMPLE_FRAMES,
    )

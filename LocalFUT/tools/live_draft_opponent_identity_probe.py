#!/usr/bin/env python3
"""Passively record the native values published by Draft Match Preview.

RC130 narrowed the Draft response lifecycle without finding the identity or
difficulty source. These four intact instructions are the final publishers
of the fields visible on Match Preview, so observing them answers the native
boundary directly without changing process state.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PROFILE = "eaapp-4052077"
PROFILES = {
    DEFAULT_PROFILE: {
        "dllSha256": (
            "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
        ),
        "targets": (
            ("difficulty", "DIFFICULTY", "text", 0x1135AA,
             "4c 8d 05 ff 23 25 00 41 8b d4 49 8b cf ff 50 78"),
            ("home-name", "HOME_NAME", "text", 0x11361F,
             "4c 8d 05 ca fd 24 00 41 8b d4 49 8b cf ff 50 78"),
            ("away-name", "AWAY_NAME", "text", 0x113644,
             "4c 8d 05 7d fd 24 00 41 8b d4 49 8b cf ff 50 78"),
            ("away-badge-id", "AWAY_BADGE_ID", "uint32", 0x113976,
             "4c 8d 05 33 db 24 00 41 8b d4 49 8b cf ff 50 70"),
        ),
    },
}

FORBIDDEN_AGENT_TOKENS = (
    "Memory.scan", "Memory.write", "writePointer", "writeS32", "writeU32",
    "writeU8", "NativeFunction", "Stalker.", "Process.enumerateRanges",
    "Process.enumerateMallocRanges", "Thread.backtrace",
    "context.rcx =", "context.rdx =", "context.rax =",
)


def targets(profile: str = DEFAULT_PROFILE) -> tuple[tuple, ...]:
    """Return the four publishers for one exact CardsDLL image."""
    return PROFILES[profile]["targets"]


TARGETS = targets()


def _target_rows(profile: str = DEFAULT_PROFILE) -> list[dict]:
    return [
        {"name": name, "property": prop, "kind": kind, "rva": rva,
         "signature": [int(value, 16) for value in signature.split()]}
        for name, prop, kind, rva, signature in targets(profile)
    ]


def build_agent(max_events: int, profile: str = DEFAULT_PROFILE) -> str:
    return r"""
'use strict';
const definitions = __TARGETS__;
const maxEvents = __MAX_EVENTS__;
let installed = false;
let emitted = 0;
let limitReported = false;
let cards = null;
const counts = {};

function emit(event, details) {
  if (emitted >= maxEvents) {
    if (!limitReported) {
      limitReported = true;
      send({event: 'probe-event-limit', maxEvents: maxEvents});
    }
    return;
  }
  emitted++;
  const payload = details || {};
  payload.event = event;
  payload.sequence = emitted;
  payload.timestampMs = Date.now();
  payload.threadId = Process.getCurrentThreadId();
  send(payload);
}

function limited(name, maximum) {
  counts[name] = (counts[name] || 0) + 1;
  return counts[name] <= maximum;
}

function readable(address, size) {
  try {
    if (address === null || address.isNull()) return false;
    const lastAddress = address.add(Math.max(0, size - 1));
    const first = Process.findRangeByAddress(address);
    const last = Process.findRangeByAddress(lastAddress);
    return first !== null && last !== null && first.base.equals(last.base) &&
      first.protection.indexOf('r') >= 0;
  } catch (_) { return false; }
}

function matches(address, expected) {
  try {
    if (!readable(address, expected.length)) return false;
    for (let index = 0; index < expected.length; index++)
      if (address.add(index).readU8() !== expected[index]) return false;
    return true;
  } catch (_) { return false; }
}

function safePointer(address) {
  try { return readable(address, Process.pointerSize) ? address.readPointer() : null; }
  catch (_) { return null; }
}

function safeU8(address) {
  try { return readable(address, 1) ? address.readU8() : null; }
  catch (_) { return null; }
}

function safeU32(address) {
  try { return readable(address, 4) ? address.readU32() : null; }
  catch (_) { return null; }
}

function safeBytes(address, size) {
  try {
    if (!readable(address, size)) return null;
    return Array.from(new Uint8Array(address.readByteArray(size)))
      .map(function (value) {
        return ('0' + value.toString(16)).slice(-2);
      }).join('');
  } catch (_) { return null; }
}

function safeText(address) {
  try { return readable(address, 1) ? address.readUtf8String(96) : null; }
  catch (_) { return null; }
}

function pointerText(value) {
  return value === null ? null : value.toString();
}

function moduleRva(value) {
  try {
    if (value === null || value.isNull()) return null;
    const owner = Process.findModuleByAddress(value);
    return owner !== null && owner.base.equals(cards.base) ?
      value.sub(cards.base).toString() : null;
  } catch (_) { return null; }
}

function install(module) {
  if (installed) return;
  installed = true;
  cards = module;
  const signatureChecks = definitions.map(function (definition) {
    const address = cards.base.add(definition.rva);
    return {name: definition.name, rva: definition.rva,
      valid: matches(address, definition.signature),
      observed: safeBytes(address, definition.signature.length)};
  });
  if (signatureChecks.some(function (check) { return !check.valid; })) {
    emit('signature-mismatch', {module: cards.name,
      signatureChecks: signatureChecks});
    send({event: 'refused', reason: 'signature-mismatch'});
    return;
  }

  const errors = [];
  for (const definition of definitions) {
    try {
      Interceptor.attach(cards.base.add(definition.rva), {onEnter() {
        if (!limited(definition.name, 8)) return;
        const record = this.context.rdi;
        const publisher = this.context.r15;
        const vtable = safePointer(publisher);
        emit('draft-match-preview-publisher', {
          stage: definition.name,
          property: definition.property,
          rva: definition.rva,
          index: this.context.r12.toInt32(),
          valuePointer: definition.kind === 'text' ?
            pointerText(this.context.r9) : null,
          value: definition.kind === 'text' ?
            safeText(this.context.r9) : this.context.r9.toUInt32(),
          record: pointerText(record),
          recordBytes: safeBytes(record, 0x10),
          recordTeamId: safeU32(record),
          recordDifficulty: safeU8(record.add(4)),
          publisher: pointerText(publisher),
          publisherVtable: pointerText(vtable),
          publisherVtableRva: moduleRva(vtable)
        });
      }});
    } catch (error) {
      errors.push({target: definition.name, error: String(error)});
    }
  }

  emit('probe-ready', {module: cards.name, base: cards.base.toString(),
    hooks: definitions.length - errors.length, hookErrors: errors,
    signatureChecks: signatureChecks});
  send({event: 'armed', state: 'attached'});
}

Process.attachModuleObserver({onAdded(module) {
  if (module.name.toLowerCase() === 'cardsdll_win64_retail.dll') install(module);
}});
const loaded = Process.findModuleByName('CardsDLL_Win64_retail.dll');
if (loaded !== null) install(loaded);
if (!installed) {
  emit('module-waiting', {module: 'CardsDLL_Win64_retail.dll'});
  send({event: 'armed', state: 'waiting-for-module'});
}
""".replace("__TARGETS__", json.dumps(
        _target_rows(profile), separators=(",", ":"))) \
        .replace("__MAX_EVENTS__", str(max(1, int(max_events))))


def self_test_errors(profile: str = DEFAULT_PROFILE) -> list[str]:
    """Return reasons this exact, read-only publisher probe is invalid."""
    if profile not in PROFILES:
        return ["unknown profile: %s" % profile]
    rows = targets(profile)
    errors: list[str] = []
    if len(rows) != 4:
        errors.append("expected four Match Preview publishers")
    if len({row[0] for row in rows}) != len(rows):
        errors.append("duplicate target name")
    if len({row[3] for row in rows}) != len(rows):
        errors.append("duplicate target RVA")
    for name, prop, kind, rva, signature in rows:
        if not name or not prop or kind not in ("text", "uint32"):
            errors.append("invalid target %s" % name)
        if rva <= 0 or not signature:
            errors.append("invalid target %s" % name)
    source = build_agent(32, profile)
    for token in FORBIDDEN_AGENT_TOKENS:
        if token in source:
            errors.append("forbidden agent primitive: %s" % token)
    return errors


def _image_offset(image: bytes, rva: int) -> int:
    import struct

    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    section_count = struct.unpack_from("<H", image, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", image, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    for index in range(section_count):
        entry = table + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = (
            struct.unpack_from("<IIII", image, entry + 8))
        if virtual_address <= rva < virtual_address + max(virtual_size,
                                                          raw_size):
            return raw_pointer + rva - virtual_address
    raise ValueError("RVA 0x%x is outside every section" % rva)


def image_errors(image: bytes, profile: str = DEFAULT_PROFILE) -> list[str]:
    """Reject a CardsDLL unless all four publishers match byte for byte."""
    import hashlib

    if profile not in PROFILES:
        return ["unknown profile: %s" % profile]
    if hashlib.sha256(image).hexdigest() != PROFILES[profile]["dllSha256"]:
        return ["image is not %s" % profile]
    errors: list[str] = []
    for name, _prop, _kind, rva, signature in targets(profile):
        wanted = bytes(int(value, 16) for value in signature.split())
        offset = _image_offset(image, rva)
        if image[offset:offset + len(wanted)] != wanted:
            errors.append("%s signature mismatch at 0x%x" % (name, rva))
        if image.count(wanted) != 1:
            errors.append("%s signature is not unique" % name)
    return errors


def _default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("diagnostics/live") / f"draft_match_preview_{stamp}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int,
                        help="verified FIFA19.exe PID; required for a live run")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-events", type=int, default=64)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    errors = self_test_errors()
    if args.self_test:
        print(json.dumps({"ok": not errors, "errors": errors,
                          "targets": len(TARGETS)}, indent=2))
        return 0 if not errors else 1
    if errors:
        raise RuntimeError("probe definition rejected: " + "; ".join(errors))
    if args.pid is None:
        parser.error("--pid is required unless --self-test is used")

    import frida

    output = args.output or _default_output()
    output.parent.mkdir(parents=True, exist_ok=True)
    finished = threading.Event()
    with output.open("a", encoding="utf-8", buffering=1) as stream:
        def record(payload: dict) -> None:
            row = {"hostTime": datetime.now(timezone.utc).isoformat(),
                   **payload}
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")

        def on_message(message, data) -> None:
            if message.get("type") == "send":
                payload = message.get("payload") or {}
                record(payload)
                if payload.get("event") == "signature-mismatch":
                    finished.set()
            else:
                record({"event": "frida-message", "message": message})
                if message.get("type") == "error":
                    finished.set()

        session = frida.get_local_device().attach(args.pid)
        script = session.create_script(build_agent(args.max_events))
        script.on("message", on_message)
        script.load()
        print("Passive Draft Match Preview probe ready: %s" % output)
        interrupted = False
        try:
            finished.wait(max(0.1, args.timeout))
        except KeyboardInterrupt:
            record({"event": "probe-interrupted"})
            stream.flush()
            interrupted = True
        finally:
            if not interrupted:
                try:
                    script.unload()
                finally:
                    session.detach()
        if interrupted:
            os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

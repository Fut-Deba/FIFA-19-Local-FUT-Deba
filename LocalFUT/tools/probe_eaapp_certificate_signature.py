#!/usr/bin/env python3
"""Bounded one-pass runtime signature probe for the observed EA App build.

This diagnostic refuses every other EXE/CardsDLL fingerprint. It attaches only
long enough to scan one bounded executable window for one old certificate-
handler signature, performs no writes or hooks, and always detaches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from fut_compat import fingerprint_file, process_image_path


EXPECTED_EXE_SHA256 = (
    "e03e3b28a8128da382c7c88618171d7e648af88771991d24c8e32722c75f1615"
)
EXPECTED_CARDS_SHA256 = (
    "8e7403f93eb5117917f0c706b4712d7862f038788e6f495c0d7176ded2f9021a"
)
EXPECTED_PRODUCT_VERSION = "19.0.4052077.0"
SCAN_START_RVA = 0x1000
SCAN_SIZE = 0x46D4A00
TIMEOUT_SECONDS = 20.0
MAX_MATCHES = 32

# Old handler prologue. Only the relative call and RIP-relative load are
# wildcarded. A match is a relocation candidate, never an approved hook.
PATTERN = (
    "40 53 55 56 57 41 55 41 56 41 57 b8 a0 11 00 00 "
    "e8 ?? ?? ?? ?? 48 2b e0 48 8b 05 ?? ?? ?? ?? 48 33 c4"
)


def verify_target(pid: int) -> str:
    executable = process_image_path(pid)
    exe = fingerprint_file(executable, "FIFA19.exe")
    cards_path = os.path.join(
        os.path.dirname(executable), "CardsDLL_Win64_retail.dll"
    )
    cards = fingerprint_file(cards_path, "CardsDLL_Win64_retail.dll")
    if (
        exe.sha256 != EXPECTED_EXE_SHA256
        or cards.sha256 != EXPECTED_CARDS_SHA256
        or exe.product_version != EXPECTED_PRODUCT_VERSION
        or cards.product_version != EXPECTED_PRODUCT_VERSION
    ):
        raise RuntimeError("refusing unrecognized or mixed FIFA 19 build")
    return executable


def agent_source() -> str:
    return """
'use strict';
const module = Process.getModuleByName('FIFA19.exe');
const windowStart = module.base.add(%d);
const windowEnd = windowStart.add(%d);
const pattern = %s;
const maximumBytes = %d;
const maximumMatches = %d;

const ranges = [];
let totalBytes = 0;
for (const range of module.enumerateRanges('r-x')) {
  const rangeEnd = range.base.add(range.size);
  if (rangeEnd.compare(windowStart) <= 0 ||
      range.base.compare(windowEnd) >= 0) continue;
  const start = range.base.compare(windowStart) < 0 ? windowStart : range.base;
  const end = rangeEnd.compare(windowEnd) > 0 ? windowEnd : rangeEnd;
  const size = end.sub(start).toUInt32();
  if (size <= 0) continue;
  ranges.push({ base: start, size: size });
  totalBytes += size;
}

if (totalBytes <= 0 || totalBytes > maximumBytes) {
  send({ event: 'refused', reason: 'bounded RX window unavailable',
         totalBytes: totalBytes });
} else {
  const matches = [];
  let completed = false;
  function finish(event, extra) {
    if (completed) return;
    completed = true;
    send(Object.assign({ event: event, scannedBytes: totalBytes,
                         matches: matches }, extra || {}));
  }
  function scanRange(index) {
    if (index >= ranges.length || matches.length >= maximumMatches) {
      finish('complete');
      return;
    }
    const range = ranges[index];
    Memory.scan(range.base, range.size, pattern, {
      onMatch(address) {
        matches.push('0x' + address.sub(module.base).toString(16));
        if (matches.length >= maximumMatches) return 'stop';
      },
      onError(reason) {
        finish('scan-error', { reason: String(reason) });
      },
      onComplete() {
        if (!completed) scanRange(index + 1);
      }
    });
  }
  scanRange(0);
}
""" % (
        SCAN_START_RVA,
        SCAN_SIZE,
        json.dumps(PATTERN),
        SCAN_SIZE,
        MAX_MATCHES,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    executable = verify_target(args.pid)
    print(
        json.dumps(
            {
                "event": "preflight-ok",
                "pid": args.pid,
                "imageName": os.path.basename(executable),
                "productVersion": EXPECTED_PRODUCT_VERSION,
                "maximumScanBytes": SCAN_SIZE,
            },
            sort_keys=True,
        )
    )

    import frida

    finished = threading.Event()
    result: dict[str, object] = {}
    session = None
    script = None

    def on_message(message, _data):
        if message.get("type") == "send":
            payload = message.get("payload") or {}
            result.update(payload)
        else:
            result.update(
                {"event": "agent-error", "description": message.get("description")}
            )
        finished.set()

    try:
        session = frida.get_local_device().attach(args.pid)
        script = session.create_script(agent_source())
        script.on("message", on_message)
        script.load()
        if not finished.wait(TIMEOUT_SECONDS):
            result.update({"event": "timeout", "timeoutSeconds": TIMEOUT_SECONDS})
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("event") == "complete" else 2
    finally:
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

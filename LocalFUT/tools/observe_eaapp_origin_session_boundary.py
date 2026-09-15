#!/usr/bin/env python3
"""Build a passive EA App Origin/session-boundary observer.

The observer is restricted to two fixed candidates in the fingerprint-gated
EA App FIFA 19 build.  Each candidate is verified against its complete masked
instruction signature before any Interceptor is installed.  It records only
call counts, caller RVAs, return codes and pointer nullness.  It never reads
request strings, credentials, auth codes, tokens, output buffers or payloads,
and it never replaces a function, register, return value or process state.
"""

from __future__ import annotations

import json


# Both candidates preserve their exact distance from already independently
# mapped neighbours.  The signatures below remain authoritative: a distance or
# candidate RVA alone is never accepted.
AUTH_CODE_SYNC_RVA = 0x1751090
IDENTITY_CREDENTIAL_USE_RVA = 0x1573FBE
MAX_CALLS = 8

AUTH_CODE_SYNC_PATTERN = (
    "4c 89 4c 24 20 55 56 57 41 54 41 55 41 56 41 57 48 83 ec 40 "
    "48 c7 44 24 30 fe ff ff ff 48 89 9c 24 80 00 00 00 49 8b f0 "
    "48 8b e9 48 85 d2 0f 84 ?? ?? ?? ?? 48 3b 91 b0 04 00 00 "
    "0f 85 ?? ?? ?? ??"
)

IDENTITY_CREDENTIAL_USE_PATTERN = (
    "4c 8b c6 48 8d 15 ?? ?? ?? ?? 48 8d 4c 24 60 e8 ?? ?? ?? ?? "
    "4d 8b c6 48 8d 15 ?? ?? ?? ?? 48 8d 4c 24 60 e8 ?? ?? ?? ??"
)


def _signature_arrays(pattern: str) -> tuple[list[int], list[bool]]:
    expected: list[int] = []
    keep: list[bool] = []
    for token in pattern.split():
        if token == "??":
            expected.append(0)
            keep.append(False)
        else:
            expected.append(int(token, 16))
            keep.append(True)
    return expected, keep


def agent_source() -> str:
    auth_expected, auth_keep = _signature_arrays(AUTH_CODE_SYNC_PATTERN)
    identity_expected, identity_keep = _signature_arrays(
        IDENTITY_CREDENTIAL_USE_PATTERN
    )
    return """
'use strict';
const fifa = Process.getModuleByName('FIFA19.exe');
const fifaEnd = fifa.base.add(fifa.size);
const authTarget = fifa.base.add(%d);
const identitySite = fifa.base.add(%d);
const authExpected = %s;
const authKeep = %s;
const identityExpected = %s;
const identityKeep = %s;
const maximumCalls = %d;
const listeners = [];
let authCalls = 0;
let identityCalls = 0;
let exitCalls = 0;

function signatureMatches(address, expected, keep) {
  try {
    const actual = new Uint8Array(address.readByteArray(expected.length));
    if (actual.length !== expected.length) return false;
    for (let index = 0; index < expected.length; index++) {
      if (keep[index] && actual[index] !== expected[index]) return false;
    }
    return true;
  } catch (_) {
    return false;
  }
}

function callerSummary(address) {
  if (address.compare(fifa.base) >= 0 && address.compare(fifaEnd) < 0) {
    return { module: 'FIFA19.exe',
      rva: '0x' + address.sub(fifa.base).toString(16) };
  }
  const owner = Process.findModuleByAddress(address);
  if (owner === null) return { module: null, rva: null };
  return { module: owner.name,
    rva: '0x' + address.sub(owner.base).toString(16) };
}

if (!signatureMatches(authTarget, authExpected, authKeep)) {
  send({ event: 'refused', reason: 'OriginRequestAuthCodeSync signature mismatch',
    authCodeSyncRva: '0x%x' });
} else if (!signatureMatches(
    identitySite, identityExpected, identityKeep)) {
  send({ event: 'refused', reason: 'Identity credential-use signature mismatch',
    identityCredentialUseRva: '0x%x' });
} else {
  listeners.push(Interceptor.attach(authTarget, {
    onEnter(args) {
      authCalls++;
      this.observed = authCalls <= maximumCalls;
      if (!this.observed) return;
      this.callCount = authCalls;
      this.caller = callerSummary(this.returnAddress);
      this.sdkNull = args[0].isNull();
      this.userNull = args[1].isNull();
      this.requestNull = args[2].isNull();
      this.outputCodeSlotNull = args[4].isNull();
      this.outputLengthSlotNull = args[5].isNull();
      this.backendNull = null;
      if (!args[0].isNull()) {
        try {
          const field = args[0].add(0x4c0);
          const range = Process.findRangeByAddress(field);
          if (range !== null && range.protection.indexOf('r') !== -1)
            this.backendNull = field.readPointer().isNull();
        } catch (_) {}
      }
    },
    onLeave(retval) {
      if (!this.observed) return;
      send({ event: 'origin-auth-code-sync-call', callCount: this.callCount,
        callerModule: this.caller.module, callerRva: this.caller.rva,
        sdkNull: this.sdkNull, userNull: this.userNull,
        requestNull: this.requestNull, backendNull: this.backendNull,
        outputCodeSlotNull: this.outputCodeSlotNull,
        outputLengthSlotNull: this.outputLengthSlotNull,
        result: retval.toString() });
    }
  }));

  listeners.push(Interceptor.attach(identitySite, {
    onEnter() {
      identityCalls++;
      if (identityCalls > maximumCalls) return;
      const caller = callerSummary(this.returnAddress);
      send({ event: 'origin-identity-credentials-observed',
        callCount: identityCalls, callerModule: caller.module,
        callerRva: caller.rva, clientIdNull: this.context.rsi.isNull(),
        clientSecretNull: this.context.r14.isNull(),
        redirectUriNull: this.context.r15.isNull() });
    }
  }));

  function attachExitObserver(moduleName, exportName, exitCodeArgument) {
    try {
      const owner = Process.getModuleByName(moduleName);
      const target = owner.findExportByName(exportName);
      if (target === null) return false;
      listeners.push(Interceptor.attach(target, {
        onEnter(args) {
          exitCalls++;
          if (exitCalls > maximumCalls) return;
          const caller = callerSummary(this.returnAddress);
          send({ event: 'process-exit-call', callCount: exitCalls,
            api: exportName, exitCode: args[exitCodeArgument].toUInt32(),
            callerModule: caller.module, callerRva: caller.rva });
        }
      }));
      return true;
    } catch (_) {
      return false;
    }
  }

  const exitObservers = [];
  if (attachExitObserver('kernel32.dll', 'ExitProcess', 0))
    exitObservers.push('ExitProcess');
  if (attachExitObserver('kernel32.dll', 'TerminateProcess', 1))
    exitObservers.push('TerminateProcess');
  if (attachExitObserver('ntdll.dll', 'RtlExitUserProcess', 0))
    exitObservers.push('RtlExitUserProcess');

  send({ event: 'armed', authCodeSyncRva: '0x%x',
    identityCredentialUseRva: '0x%x', maximumCalls: maximumCalls,
    exitObservers: exitObservers });
}
""" % (
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
        json.dumps(auth_expected),
        json.dumps(auth_keep),
        json.dumps(identity_expected),
        json.dumps(identity_keep),
        MAX_CALLS,
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
    )

#!/usr/bin/env python3
"""Build the exact-tuple EA App Origin/session missing-value guard.

The guard is restricted to the two independently signature-verified EA App
candidates.  It changes only the observed failure tuple: AuthCodeSync receives
a null request and returns ``0xA2000004`` with valid output slots, while the
later Identity builder receives three null credential pointers.  The guard
publishes fixed loopback-only synthetic values, reads no input string or real
credential, and fails closed for every non-matching call or signature.
"""

from __future__ import annotations

import json

from observe_eaapp_origin_session_boundary import (
    AUTH_CODE_SYNC_PATTERN,
    AUTH_CODE_SYNC_RVA,
    IDENTITY_CREDENTIAL_USE_PATTERN,
    IDENTITY_CREDENTIAL_USE_RVA,
    _signature_arrays,
)


EXPECTED_FAILURE = 0xA2000004
VERIFIED_SUCCESS = 0
MAX_AUTH_REPLACEMENTS = 4
MAX_IDENTITY_REPLACEMENTS = 2


def agent_source() -> str:
    auth_expected, auth_keep = _signature_arrays(AUTH_CODE_SYNC_PATTERN)
    identity_expected, identity_keep = _signature_arrays(
        IDENTITY_CREDENTIAL_USE_PATTERN
    )
    return """
'use strict';
const fifa = Process.getModuleByName('FIFA19.exe');
const authTarget = fifa.base.add(%d);
const identitySite = fifa.base.add(%d);
const authExpected = %s;
const authKeep = %s;
const identityExpected = %s;
const identityKeep = %s;
const expectedFailure = 0x%x;
const verifiedSuccess = 0x%x;
const maximumAuthReplacements = %d;
const maximumIdentityReplacements = %d;
const localClientId = Memory.allocUtf8String('fifa19-local-client');
const localClientSecret = Memory.allocUtf8String('localfut19-client-secret');
const localRedirectUri = Memory.allocUtf8String(
  'http://127.0.0.1:8199/identity/callback');
const localAuthCodeValue = 'localfut19-auth-code';
const localAuthCode = Memory.allocUtf8String(localAuthCodeValue);
const listeners = [];
let authCalls = 0;
let authReplacements = 0;
let identityCalls = 0;
let identityReplacements = 0;

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

function readablePointer(address) {
  if (address.isNull()) return false;
  try {
    const range = Process.findRangeByAddress(address);
    return range !== null && range.protection.indexOf('r') !== -1 &&
      address.add(Process.pointerSize).compare(range.base.add(range.size)) <= 0;
  } catch (_) {
    return false;
  }
}

function writable(address, size) {
  if (address.isNull()) return false;
  try {
    const range = Process.findRangeByAddress(address);
    return range !== null && range.protection.indexOf('w') !== -1 &&
      address.add(size).compare(range.base.add(range.size)) <= 0;
  } catch (_) {
    return false;
  }
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
      this.callCount = authCalls;
      this.outputCode = args[4];
      this.outputLength = args[5];
      this.sdkNull = args[0].isNull();
      this.userNull = args[1].isNull();
      this.requestNull = args[2].isNull();
      this.outputCodeWritable = writable(args[4], Process.pointerSize);
      this.outputLengthWritable = writable(args[5], 8);
      this.backendNull = null;
      if (!args[0].isNull() && readablePointer(args[0].add(0x4c0))) {
        try { this.backendNull = args[0].add(0x4c0).readPointer().isNull(); }
        catch (_) {}
      }
      this.eligible = authReplacements < maximumAuthReplacements &&
        !this.sdkNull && !this.userNull && this.requestNull &&
        this.backendNull === false && this.outputCodeWritable &&
        this.outputLengthWritable;
    },
    onLeave(retval) {
      const original = retval.toUInt32();
      if (!this.eligible || original !== expectedFailure) {
        if (this.callCount <= maximumAuthReplacements) {
          send({ event: 'origin-auth-code-fallback-skipped',
            callCount: this.callCount,
            original: '0x' + original.toString(16), sdkNull: this.sdkNull,
            userNull: this.userNull, requestNull: this.requestNull,
            backendNull: this.backendNull,
            outputCodeWritable: this.outputCodeWritable,
            outputLengthWritable: this.outputLengthWritable });
        }
        return;
      }
      let codeWritten = false;
      let lengthWritten = false;
      try {
        this.outputCode.writePointer(localAuthCode);
        codeWritten = this.outputCode.readPointer().equals(localAuthCode);
      } catch (_) {}
      try {
        this.outputLength.writeU64(localAuthCodeValue.length);
        lengthWritten = this.outputLength.readU64().toString() ===
          String(localAuthCodeValue.length);
      } catch (_) {}
      if (!codeWritten || !lengthWritten) {
        send({ event: 'origin-auth-code-fallback-skipped',
          callCount: this.callCount, original: '0x' + original.toString(16),
          skipReason: 'output verification failed', codeWritten: codeWritten,
          lengthWritten: lengthWritten });
        return;
      }
      authReplacements++;
      retval.replace(ptr(verifiedSuccess));
      send({ event: 'origin-auth-code-fallback-applied',
        callCount: this.callCount, replacementCount: authReplacements,
        original: '0x' + original.toString(16),
        replacement: '0x' + verifiedSuccess.toString(16),
        requestNull: this.requestNull, backendNull: this.backendNull,
        replacementLength: localAuthCodeValue.length });
    }
  }));

  listeners.push(Interceptor.attach(identitySite, {
    onEnter() {
      identityCalls++;
      const clientIdNull = this.context.rsi.isNull();
      const clientSecretNull = this.context.r14.isNull();
      const redirectUriNull = this.context.r15.isNull();
      if (identityReplacements >= maximumIdentityReplacements ||
          !clientIdNull || !clientSecretNull || !redirectUriNull) {
        if (identityCalls <= maximumIdentityReplacements) {
          send({ event: 'origin-identity-fallback-skipped',
            callCount: identityCalls, clientIdNull: clientIdNull,
            clientSecretNull: clientSecretNull,
            redirectUriNull: redirectUriNull });
        }
        return;
      }
      this.context.rsi = localClientId;
      this.context.r14 = localClientSecret;
      this.context.r15 = localRedirectUri;
      const applied = this.context.rsi.equals(localClientId) &&
        this.context.r14.equals(localClientSecret) &&
        this.context.r15.equals(localRedirectUri);
      if (applied) identityReplacements++;
      send({ event: applied ? 'origin-identity-fallback-applied' :
          'origin-identity-fallback-skipped',
        callCount: identityCalls, replacementCount: identityReplacements,
        clientIdWasMissing: clientIdNull,
        clientSecretWasMissing: clientSecretNull,
        redirectUriWasMissing: redirectUriNull,
        skipReason: applied ? null : 'register verification failed' });
    }
  }));

  send({ event: 'armed', authCodeSyncRva: '0x%x',
    identityCredentialUseRva: '0x%x', expectedFailure:
      '0x' + expectedFailure.toString(16), replacement:
      '0x' + verifiedSuccess.toString(16),
    maximumAuthReplacements: maximumAuthReplacements,
    maximumIdentityReplacements: maximumIdentityReplacements });
}
""" % (
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
        json.dumps(auth_expected),
        json.dumps(auth_keep),
        json.dumps(identity_expected),
        json.dumps(identity_keep),
        EXPECTED_FAILURE,
        VERIFIED_SUCCESS,
        MAX_AUTH_REPLACEMENTS,
        MAX_IDENTITY_REPLACEMENTS,
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
        AUTH_CODE_SYNC_RVA,
        IDENTITY_CREDENTIAL_USE_RVA,
    )

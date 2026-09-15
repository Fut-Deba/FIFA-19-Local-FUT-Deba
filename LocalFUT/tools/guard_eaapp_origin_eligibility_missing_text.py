#!/usr/bin/env python3
"""Build the minimal EA App eligibility missing-text guard.

This guard is intentionally limited to the exact FIFA 19 EA App build whose
fingerprint is checked by the caller.  It does not change the Origin SDK error,
the mapped error, the eligibility state, registers, or control flow.  At the
verified decision site it fills only the missing text pointer for the one live
state already observed: Origin error ``0xA2000004``, state ``1`` and mapped
error ``0x01000001``.  A real underage error is always left untouched.
"""

from __future__ import annotations


DECISION_RVA = 0x1573B38
POPUP_BRANCH_RVA = 0x1573B65
UNDERAGE_ERROR = 0xA2000012
EXPECTED_SDK_ERROR = 0xA2000004
EXPECTED_STATE = 1
EXPECTED_MAPPED_ERROR = 0x01000001
MAX_EVENTS = 4
FALLBACK_TEXT = "Login Success"


def agent_source() -> str:
    return r"""
'use strict';
const module = Process.getModuleByName('FIFA19.exe');
const decision = module.base.add(%d);
const popupBranch = module.base.add(%d);
const underageError = 0x%x;
const expectedSdkError = 0x%x;
const expectedState = %d;
const expectedMappedError = 0x%x;
const decisionSignature = [0x3d,0x12,0x00,0x00,0xa2,0x74,0x26,
  0x48,0x8b,0x54,0x24,0x40,0x48,0x85,0xd2,0x74,0x1c,
  0x44,0x38,0x2a,0x74,0x17];
const branchSignature = [0x48,0x8d,0x05,0x6c,0x69,0xbb,0x03,
  0x48,0x89,0x83,0x50,0x05,0x00,0x00,0x8b,0x83,0x1c,0x05,0x00,0x00];
let decisionCount = 0;
let branchCount = 0;

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

function stateSummary(rbx) {
  let state = null;
  let mappedError = null;
  try {
    if (!rbx.isNull()) {
      const range = Process.findRangeByAddress(rbx.add(0x54c));
      if (range !== null && range.protection.indexOf('r') !== -1) {
        state = rbx.add(0x51c).readS32();
        mappedError = rbx.add(0x54c).readU32();
      }
    }
  } catch (_) {}
  return { state: state, mappedError: mappedError };
}

if (!matches(decision, decisionSignature) ||
    !matches(popupBranch, branchSignature)) {
  send({ event: 'refused', reason: 'eligibility signature mismatch',
    decisionRva: '0x%x', popupBranchRva: '0x%x' });
} else {
  const fallbackText = Memory.allocUtf8String('%s');
  Interceptor.attach(decision, {
    onEnter() {
      decisionCount++;
      if (decisionCount > %d) return;
      const sdkError = this.context.rax.toUInt32();
      const summary = stateSummary(this.context.rbx);
      const textSlot = this.context.rsp.add(0x40);
      let textPointer = ptr(0);
      let textPointerReadable = false;
      let textSlotWritable = false;
      let textMissing = false;
      try {
        const slotRange = Process.findRangeByAddress(textSlot);
        if (slotRange !== null && slotRange.protection.indexOf('r') !== -1) {
          textPointer = textSlot.readPointer();
          textPointerReadable = true;
          textSlotWritable = slotRange.protection.indexOf('w') !== -1;
          if (textPointer.isNull()) {
            textMissing = true;
          } else {
            const textRange = Process.findRangeByAddress(textPointer);
            if (textRange !== null && textRange.protection.indexOf('r') !== -1)
              textMissing = textPointer.readU8() === 0;
          }
        }
      } catch (_) {}

      let fallbackApplied = false;
      let skipReason = null;
      if (sdkError === underageError) {
        skipReason = 'real-underage-code';
      } else if (sdkError !== expectedSdkError) {
        skipReason = 'unexpected-sdk-error';
      } else if (summary.state !== expectedState ||
                 summary.mappedError !== expectedMappedError) {
        skipReason = 'unexpected-eligibility-state';
      } else if (!textPointerReadable || !textSlotWritable) {
        skipReason = 'text-slot-not-readable-writable';
      } else if (!textMissing) {
        skipReason = 'text-already-present-or-unreadable';
      } else {
        try {
          textSlot.writePointer(fallbackText);
          fallbackApplied = textSlot.readPointer().equals(fallbackText);
          if (!fallbackApplied) skipReason = 'write-verification-failed';
        } catch (error) {
          skipReason = 'write-error:' + String(error);
        }
      }

      const event = fallbackApplied
        ? 'origin-eligibility-fallback-applied'
        : 'origin-eligibility-fallback-skipped';
      send({ event: event, callCount: decisionCount,
        sdkError: '0x' + sdkError.toString(16),
        isUnderageCode: sdkError === underageError,
        state: summary.state,
        mappedError: summary.mappedError === null ? null :
          '0x' + summary.mappedError.toString(16),
        textPointerNull: textPointerReadable ? textPointer.isNull() : null,
        textMissing: textMissing, fallbackApplied: fallbackApplied,
        skipReason: skipReason });
    }
  });
  Interceptor.attach(popupBranch, {
    onEnter() {
      branchCount++;
      if (branchCount > %d) return;
      const sdkError = this.context.rax.toUInt32();
      const summary = stateSummary(this.context.rbx);
      send({ event: 'origin-eligibility-popup-branch',
        callCount: branchCount,
        sdkError: '0x' + sdkError.toString(16),
        isUnderageCode: sdkError === underageError,
        state: summary.state,
        mappedError: summary.mappedError === null ? null :
          '0x' + summary.mappedError.toString(16) });
    }
  });
  send({ event: 'armed', decisionRva: '0x%x',
    popupBranchRva: '0x%x', expectedSdkError: '0x%x',
    underageError: '0x%x', expectedState: %d,
    expectedMappedError: '0x%x', maximumEvents: %d });
}
""" % (
        DECISION_RVA,
        POPUP_BRANCH_RVA,
        UNDERAGE_ERROR,
        EXPECTED_SDK_ERROR,
        EXPECTED_STATE,
        EXPECTED_MAPPED_ERROR,
        DECISION_RVA,
        POPUP_BRANCH_RVA,
        FALLBACK_TEXT,
        MAX_EVENTS,
        MAX_EVENTS,
        DECISION_RVA,
        POPUP_BRANCH_RVA,
        EXPECTED_SDK_ERROR,
        UNDERAGE_ERROR,
        EXPECTED_STATE,
        EXPECTED_MAPPED_ERROR,
        MAX_EVENTS,
    )

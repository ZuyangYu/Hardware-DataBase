import { describe, expect, it } from 'vitest';

import type { MemoryConsentView } from '../api/types';
import { visibleMemoryConsents } from './memoryViewModel';

const activeConsent: MemoryConsentView = {
  consent_event_id: 'active-consent',
  session_id: 10,
  source_count: 2,
  manifest_hash: 'hash-active',
  policy_version: 'v1',
  revoke_generation: 0,
  status: 'active',
  granted_at: '2026-09-03T00:00:00Z',
  revoked_at: null,
};

const revokedConsent: MemoryConsentView = {
  ...activeConsent,
  consent_event_id: 'revoked-consent',
  status: 'revoked',
  revoked_at: '2026-09-03T00:01:00Z',
};

describe('memoryViewModel', () => {
  it('hides revoked consent rows while retaining active consent rows', () => {
    expect(visibleMemoryConsents([activeConsent, revokedConsent])).toEqual([activeConsent]);
  });
});

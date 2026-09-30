import { describe, expect, it } from 'vitest';

import {
  ADVANCED_KEYS,
  BOOLEAN_KEYS,
  GROUPS,
  NUMBER_FIELDS,
  RESTART_REQUIRED_KEYS,
  booleanDraftValue,
} from './ConfigPage';

describe('ConfigPage MCP group', () => {
  it('exposes one switch set; platform prerequisites are not user-facing settings', () => {
    const group = GROUPS.find((item) => item.title.includes('MCP'));
    expect(group).toBeTruthy();
    const keys = (group?.fields ?? []).map(([key]) => key);
    for (const key of ['MCP_ENABLED', 'MCP_WRITE_ENABLED', 'MCP_WRITE_MAX_ATTEMPTS']) {
      expect(keys).toContain(key);
    }
    for (const key of ['CONVERSATION_UNIFIED_TOOLS_ENABLED', 'RESOURCE_GOVERNOR_ENABLED']) {
      expect(keys).not.toContain(key);
      expect(BOOLEAN_KEYS.has(key)).toBe(false);
    }
    for (const key of [
      'MCP_ENABLED',
      'MCP_WRITE_ENABLED',
      'MCP_ALLOW_INSECURE_HTTP',
      'MCP_ALLOW_PRIVATE_HOSTS',
    ]) {
      expect(BOOLEAN_KEYS.has(key)).toBe(true);
    }
    // The credentials key stays environment-only and must never be configurable.
    expect(keys).not.toContain('MCP_CREDENTIALS_KEY');
    expect(ADVANCED_KEYS.has('MCP_CALL_TIMEOUT_SECONDS')).toBe(true);
    expect(ADVANCED_KEYS.has('MCP_ENABLED')).toBe(false);
  });

  it('marks every restart-required UI key', () => {
    const keys = GROUPS.flatMap((group) => group.fields.map(([key]) => key));
    for (const key of RESTART_REQUIRED_KEYS) {
      expect(keys).toContain(key);
    }
    expect(RESTART_REQUIRED_KEYS.has('AUTH_DB_PATH')).toBe(true);
  });

  it('validates numeric MCP fields and normalizes boolean drafts', () => {
    expect(NUMBER_FIELDS.MCP_CALL_TIMEOUT_SECONDS?.integer).toBe(true);
    expect(NUMBER_FIELDS.MCP_MAX_ARTIFACT_BYTES?.min).toBeGreaterThanOrEqual(1024);
    expect(NUMBER_FIELDS.RESOURCE_MCP_MAX_CONCURRENT?.integer).toBe(true);
    expect(booleanDraftValue('on')).toBe('true');
    expect(booleanDraftValue('TRUE')).toBe('true');
    expect(booleanDraftValue('false')).toBe('false');
    expect(booleanDraftValue('')).toBe('false');
    expect(booleanDraftValue(undefined)).toBe('false');
  });
});

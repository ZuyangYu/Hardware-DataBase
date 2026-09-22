import { describe, expect, it } from 'vitest';

import { BOOLEAN_KEYS, GROUPS, NUMBER_FIELDS, booleanDraftValue } from './ConfigPage';

describe('ConfigPage MCP group', () => {
  it('exposes the MCP switch and its prerequisites as boolean toggles', () => {
    const group = GROUPS.find((item) => item.title.includes('MCP'));
    expect(group).toBeTruthy();
    const keys = (group?.fields ?? []).map(([key]) => key);
    for (const key of [
      'MCP_ENABLED',
      'MCP_WRITE_ENABLED',
      'CONVERSATION_UNIFIED_TOOLS_ENABLED',
      'RESOURCE_GOVERNOR_ENABLED',
      'MCP_WRITE_MAX_ATTEMPTS',
    ]) {
      expect(keys).toContain(key);
    }
    for (const key of [
      'MCP_ENABLED',
      'MCP_WRITE_ENABLED',
      'CONVERSATION_UNIFIED_TOOLS_ENABLED',
      'RESOURCE_GOVERNOR_ENABLED',
    ]) {
      expect(BOOLEAN_KEYS.has(key)).toBe(true);
    }
    // The credentials key stays environment-only and must never be configurable.
    expect(keys).not.toContain('MCP_CREDENTIALS_KEY');
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

import { describe, expect, it } from 'vitest';

import {
  parseDocumentGenerationDeepLink,
  resolveDocumentGenerationDeepLink,
  type DeepLinkRefs,
} from './documentGenerationDeepLink';

function refs(overrides: Partial<DeepLinkRefs> = {}): DeepLinkRefs {
  return { kb: '', task: '', session: '', workOrder: '', ...overrides };
}

describe('documentGenerationDeepLink', () => {
  it('parses percent-encoded params from a URLSearchParams', () => {
    const params = new URLSearchParams(
      'kb=%E7%A1%AC%E4%BB%B6&task=document-task-1&unused=keep',
    );
    expect(parseDocumentGenerationDeepLink(params)).toEqual(
      refs({ kb: '硬件', task: 'document-task-1' }),
    );
  });

  it('prefers the task reference as the authoritative identity', () => {
    const resolved = resolveDocumentGenerationDeepLink(
      refs({ task: 't-1', session: 's-1', workOrder: 'wo-1' }),
    );
    expect(resolved).toEqual({ kind: 'task', taskId: 't-1' });
  });

  it('resolves a session-only link', () => {
    expect(resolveDocumentGenerationDeepLink(refs({ session: 's-1' }))).toEqual({
      kind: 'session',
      sessionId: 's-1',
    });
  });

  it('resolves a work-order-only link', () => {
    expect(resolveDocumentGenerationDeepLink(refs({ workOrder: 'wo-1' }))).toEqual({
      kind: 'workOrder',
      workOrderId: 'wo-1',
    });
  });

  it('reports a scoped conflict when session and work order are both present without a task', () => {
    const resolved = resolveDocumentGenerationDeepLink(refs({ session: 's-1', workOrder: 'wo-1' }));
    expect(resolved.kind).toBe('conflict');
  });

  it('returns none when no identity reference is present', () => {
    expect(resolveDocumentGenerationDeepLink(refs({ kb: 'hardware' })).kind).toBe('none');
  });
});

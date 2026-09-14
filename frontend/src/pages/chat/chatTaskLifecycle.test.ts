import { describe, expect, it } from 'vitest';

import { shouldCancelServerTurn, type ChatStreamDetachReason } from './chatTaskLifecycle';

describe('chat task lifecycle', () => {
  it.each([
    ['component_unmount', false],
    ['route_navigation', false],
    ['knowledge_base_switch', false],
    ['user_stop', true],
  ] as Array<[ChatStreamDetachReason, boolean]>)('cancels the server turn only for %s', (reason, expected) => {
    expect(shouldCancelServerTurn(reason)).toBe(expected);
  });
});

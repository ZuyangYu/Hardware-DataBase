import { describe, expect, it } from 'vitest';

import { CHAT_MESSAGES_CLASS } from './chatPageStyles';

describe('chat message column', () => {
  it('scrolls vertically without exposing a horizontal scrollbar', () => {
    expect(CHAT_MESSAGES_CLASS).toContain('overflow-y-auto');
    expect(CHAT_MESSAGES_CLASS).toContain('overflow-x-hidden');
  });
});

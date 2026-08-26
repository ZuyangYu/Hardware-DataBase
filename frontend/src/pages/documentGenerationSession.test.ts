import { describe, expect, it } from 'vitest';
import type { GenerationSession } from '../api/types';

import {
  confirmedBriefItems,
  latestClarificationQuestion,
  pendingBriefItems,
  sessionMessagesForWorkbench,
} from './documentGenerationSession';

const session: GenerationSession = {
  session_id: 'generation-session-1',
  status: 'needs_clarification',
  brief: {
    purpose: '生成硬件评审表',
    scope: { revision: '当前发布版本' },
    output_policy: { format: 'xlsx' },
    source_policy: {},
    missing_data_policy: null,
    inference_policy: null,
    confirmed: false,
    confidence: 0.33,
  },
  messages: [
    {
      message_id: 'm1',
      role: 'assistant' as const,
      content: '检索不到可靠资料的字段应如何处理？',
      question_id: 'missing_data_policy',
      options: ['标记未提供', '保留空白'],
    },
  ],
};

describe('document generation session adapter', () => {
  it('projects persisted messages and the active question into the workbench', () => {
    expect(sessionMessagesForWorkbench(session)[0]).toEqual({
      id: 'm1',
      role: 'assistant',
      content: '检索不到可靠资料的字段应如何处理？',
      options: ['标记未提供', '保留空白'],
    });
    expect(latestClarificationQuestion(session)?.question_id).toBe('missing_data_policy');
  });

  it('separates confirmed and pending brief decisions', () => {
    expect(confirmedBriefItems(session.brief)).toContain('项目版本：当前发布版本');
    expect(pendingBriefItems(session.brief)).toEqual(['缺失数据处理', 'AI 推断策略']);
  });
});

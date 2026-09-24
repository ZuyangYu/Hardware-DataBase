import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ChatsPanel, ChatMessageBubble } from './KbFilesPage';
import type { ExternalConversationDetailResponse, ExternalConversationListItem } from '../api/types';

const items: ExternalConversationListItem[] = [
  {
    conversation_id: 'c1',
    title: '电源设计讨论',
    source_file: 'chat.md',
    origin: 'upload',
    source_group: '外部数据',
    turn_count: 2,
    block_count: 0,
    status: 'indexed',
    created_at: '2026-08-25',
  },
];

const detail: ExternalConversationDetailResponse = {
  ...items[0],
  turns: [
    { role: 'user', content: 'LDO 压差是多少?', ts: '', start_offset: 0, end_offset: 10 },
    { role: 'assistant', content: '最大压差 0.3V。', ts: '', start_offset: 10, end_offset: 24 },
  ],
  blocks: [],
  preview: '用户: LDO 压差是多少?',
  summary: '讨论了 LDO 压差要求与静态电流结论。',
  key_points: ['最大压差 0.3V', '静态电流典型 12uA'],
  summary_generated_at: '2026-08-25',
};

describe('kb files workbench chats panel', () => {
  it('renders the 外部对话 panel with conversation list and message preview', () => {
    const html = renderToStaticMarkup(
      <ChatsPanel
        items={items}
        loading={false}
        selectedId="c1"
        detail={detail}
        canWrite
        summaryGenerating={false}
        onSelectedIdChange={() => undefined}
        onRefresh={() => undefined}
        onDelete={() => undefined}
        onRegenerateSummary={() => undefined}
      />,
    );

    expect(html).toContain('外部对话浏览');
    expect(html).toContain('电源设计讨论');
    expect(html).toContain('对话内容');
    expect(html).toContain('LDO 压差是多少?');
    expect(html).toContain('最大压差 0.3V。');
    expect(html).toContain('AI 提取摘要');
    expect(html).toContain('最大压差 0.3V');
    expect(html).toContain('生成/刷新 AI 摘要');
    expect(html).toContain('删除');
  });

  it('shows empty state without conversations', () => {
    const html = renderToStaticMarkup(
      <ChatsPanel
        items={[]}
        loading={false}
        selectedId=""
        detail={null}
        canWrite={false}
        summaryGenerating={false}
        onSelectedIdChange={() => undefined}
        onRefresh={() => undefined}
        onDelete={() => undefined}
        onRegenerateSummary={() => undefined}
      />,
    );

    expect(html).toContain('当前知识库尚未上传外部对话记录');
    expect(html).not.toContain('>删除<');
  });
});

describe('chat message bubble collapse', () => {
  const longText = '这是一段很长的对话内容。'.repeat(30);

  it('long messages collapse by default with an expand hint', () => {
    const html = renderToStaticMarkup(<ChatMessageBubble role="user" content={longText} />);
    expect(html).toContain('展开');
    expect(html).toContain('line-clamp-3'); // CSS-clamped by default
  });

  it('short messages render fully without hint', () => {
    const html = renderToStaticMarkup(<ChatMessageBubble role="assistant" content="最大压差 0.3V。" />);
    expect(html).toContain('最大压差 0.3V。');
    expect(html).not.toContain('展开');
    expect(html).not.toContain('line-clamp-3');
  });
});

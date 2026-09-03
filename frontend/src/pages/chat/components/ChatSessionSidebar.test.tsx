import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import ChatSessionSidebar from './ChatSessionSidebar';

describe('ChatSessionSidebar background turns', () => {
  it('keeps new conversation available while another session is streaming', () => {
    const markup = renderToStaticMarkup(
      <ChatSessionSidebar
        kbName="hardware"
        sessions={[{
          id: 1,
          user_id: 7,
          kb_name: 'hardware',
          title: '正在生成的会话',
          created_at: '2026-09-02T00:00:00Z',
          updated_at: '2026-09-02T00:00:00Z',
        }]}
        sessionsLoaded
        activeSessionId={1}
        streaming
        onSelect={() => undefined}
        onNew={() => undefined}
        onDelete={() => undefined}
      />,
    );

    expect(markup).toMatch(/aria-label="新对话"/);
    const newButton = markup.match(/<button[^>]*aria-label="新对话"[^>]*>/)?.[0] ?? '';
    expect(newButton).not.toContain('disabled');
    expect(markup).toContain('正在生成的会话');
  });

  it('anchors the history rail on the left and keeps its resize handle on the right', () => {
    const markup = renderToStaticMarkup(
      <ChatSessionSidebar
        kbName="hardware"
        sessions={[]}
        sessionsLoaded
        activeSessionId={null}
        streaming={false}
        onSelect={() => undefined}
        onNew={() => undefined}
        onDelete={() => undefined}
      />,
    );

    expect(markup).toContain('border-r');
    expect(markup).not.toContain('border-l');
    expect(markup).toContain('right-0');
    expect(markup).not.toContain('absolute left-0');
    expect(markup).toContain('title="新建会话"');
  });
});

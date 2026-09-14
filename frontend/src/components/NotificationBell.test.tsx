import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { NotificationBellView, type AppNotification } from './NotificationBell';

const item: AppNotification = {
  notification_id: 3,
  kind: 'attachment_release_approved',
  target_type: 'attachment_revision',
  target_id: 'attachment-revision-1',
  title: '附件候选已通过正式发布',
  body: '审核通过，已生成独立的正式文件，可从会话附件中下载。',
  payload: { session_id: 7 },
  read: false,
  created_at: '2026-09-13T10:00:00+00:00',
};

describe('NotificationBellView', () => {
  it('renders the unread badge and notification items', () => {
    const markup = renderToStaticMarkup(
      <NotificationBellView
        unreadCount={3}
        items={[item]}
        open
        onToggle={() => undefined}
        onSelect={() => undefined}
        onMarkAll={() => undefined}
      />,
    );

    expect(markup).toContain('data-notification-bell');
    expect(markup).toContain('data-notification-badge');
    expect(markup).toContain('>3<');
    expect(markup).toContain('data-notification-panel');
    expect(markup).toContain('附件候选已通过正式发布');
    expect(markup).toContain('data-notification-id="3"');
    expect(markup).toContain('全部已读');
  });

  it('renders the empty state without a badge', () => {
    const markup = renderToStaticMarkup(
      <NotificationBellView
        unreadCount={0}
        items={[]}
        open
        onToggle={() => undefined}
        onSelect={() => undefined}
        onMarkAll={() => undefined}
      />,
    );

    expect(markup).toContain('暂无通知');
    expect(markup).not.toContain('data-notification-badge');
    expect(markup).not.toContain('全部已读');
  });
});

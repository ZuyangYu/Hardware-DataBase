import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  AttachmentReleasesView,
  type PendingAttachmentRelease,
} from './AttachmentReleasesPage';

const item: PendingAttachmentRelease = {
  revision_id: 'attachment-revision-abc',
  session_id: 7,
  kb_name: 'ADAS',
  session_title: '完善 HSI 文档',
  owner_user_id: 11,
  candidate_filename: 'hsi.revision-abc.docx',
  parent_filename: 'hsi.docx',
  operation: 'replace_section',
  change_summary: '替换章节「共享和独占的硬件资源」；移除 11 段；新增 6 段',
  release_state: 'pending',
  release_comment: '请审核发布',
  release_requested_at: '2026-09-11T10:00:00+00:00',
  candidate_download_url:
    '/api/v1/conversations/7/attachments/att-child/download',
};

describe('AttachmentReleasesView', () => {
  it('renders the candidate, change summary and review actions', () => {
    const markup = renderToStaticMarkup(
      <AttachmentReleasesView
        items={[item]}
        loading={false}
        onApprove={() => undefined}
        onReject={() => undefined}
        onDownload={() => undefined}
        onRefresh={() => undefined}
      />,
    );

    expect(markup).toContain('data-attachment-release-queue');
    expect(markup).toContain('data-revision-id="attachment-revision-abc"');
    expect(markup).toContain('hsi.revision-abc.docx');
    expect(markup).toContain('基于 hsi.docx');
    expect(markup).toContain('替换章节');
    expect(markup).toContain('申请说明：请审核发布');
    expect(markup).toContain('aria-label="下载候选 hsi.revision-abc.docx"');
    expect(markup).toContain('aria-label="通过 hsi.revision-abc.docx"');
    expect(markup).toContain('aria-label="驳回 hsi.revision-abc.docx"');
    expect(markup).toContain('候选与正式发布严格分离');
  });

  it('renders the empty state without review buttons', () => {
    const markup = renderToStaticMarkup(
      <AttachmentReleasesView
        items={[]}
        loading={false}
        onApprove={() => undefined}
        onReject={() => undefined}
        onDownload={() => undefined}
        onRefresh={() => undefined}
      />,
    );

    expect(markup).toContain('当前没有待审核的附件发布申请');
    expect(markup).not.toContain('aria-label="通过');
  });
});

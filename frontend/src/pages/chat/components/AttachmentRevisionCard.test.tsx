import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import AttachmentRevisionCardView from './AttachmentRevisionCard';

describe('AttachmentRevisionCard', () => {
  it('renders the candidate download and release state', () => {
    const markup = renderToStaticMarkup(
      <AttachmentRevisionCardView
        card={{
          kind: 'attachment_revision',
          content: '已创建附件修订候选',
          release_state: 'pending',
          artifacts: [{ artifact_id: 'att-2', output_format: 'docx', download_url: '/api/v1/attachments/att-2/download' }],
        }}
      />,
    );
    expect(markup).toContain('附件修订候选');
    expect(markup).toContain('/api/v1/attachments/att-2/download');
    expect(markup).toContain('DOCX');
    expect(markup).toContain('等待审核人处理');
  });
});

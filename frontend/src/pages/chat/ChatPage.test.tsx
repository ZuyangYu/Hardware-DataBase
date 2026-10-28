import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import type { AttachmentView, KbView } from '../../api/types';
import ChatPage, {
  defaultSelectedAttachmentIds,
  isChatAttachmentsUiEnabled,
  retainExistingAttachmentIds,
} from './ChatPage';
import Composer, { kbOptionLabel } from './components/Composer';

const auth: AuthSession = {
  token: 'test-token',
  user: {
    username: 'alice',
    role: 'employee',
    department_id: null,
    department_name: null,
  },
};

const writableKb: KbView = {
  name: 'shared',
  kb_id: 1,
  department_id: 1,
  department_name: '研发部',
  permission: 'write',
  registered: true,
  owner_user_id: 1,
  is_shared: true,
  is_owner: false,
};

function renderChat(availableKbs: KbView[] = [writableKb]) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ChatPage
        auth={auth}
        kbName="shared"
        availableKbs={availableKbs}
        onLogout={() => undefined}
      />
    </MemoryRouter>,
  );
}

describe('session attachment selection stays mounted', () => {
  const ready = { attachment_id: 'a1', parse_status: 'ready' } as unknown as AttachmentView;
  const failed = { attachment_id: 'a2', parse_status: 'failed' } as unknown as AttachmentView;
  const queued = { attachment_id: 'a3', parse_status: 'queued' } as unknown as AttachmentView;

  it('selects parseable attachments and skips failed parses', () => {
    expect(defaultSelectedAttachmentIds([ready, failed, queued])).toEqual(['a1', 'a3']);
    expect(defaultSelectedAttachmentIds([])).toEqual([]);
  });

  it('retains only existing selections in their original order', () => {
    expect(
      retainExistingAttachmentIds(['a3', 'gone', 'a1'], [ready, failed, queued]),
    ).toEqual(['a3', 'a1']);
    expect(retainExistingAttachmentIds(['a3'], [])).toEqual([]);
  });
});

describe('isChatAttachmentsUiEnabled flag resolution (opt-out semantics)', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    delete (import.meta.env as Record<string, unknown>).VITE_CHAT_ATTACHMENTS_ENABLED;
  });

  it('defaults to enabled when the attachment env var is absent', () => {
    expect(isChatAttachmentsUiEnabled()).toBe(true);
  });

  it('allows an explicit false value to disable the attachment UI', () => {
    vi.stubEnv('VITE_CHAT_ATTACHMENTS_ENABLED', 'false');
    expect(isChatAttachmentsUiEnabled()).toBe(false);
  });
});

describe('ChatPage chat entry', () => {
  it('uses the unified attachment entry without a dedicated template upload', () => {
    const markup = renderChat();

    expect(markup).toContain('添加附件');
    expect(markup).toContain('chat-attachment-upload');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });

  it('keeps the unified attachment entry for a read-only knowledge base', () => {
    const markup = renderChat([{ ...writableKb, permission: 'read' }]);

    expect(markup).toContain('添加附件');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });

  it('shows only the single knowledge-base selector', () => {
    const markup = renderChat([
      writableKb,
      { ...writableKb, name: 'reference', kb_id: 2, permission: 'read' },
    ]);

    expect(markup).toContain('检索范围');
    expect(markup).toContain('aria-label="选择会话锚定库"');
    expect(markup).not.toContain('aria-label="添加知识库"');
    expect(markup).not.toContain('参考库');
    expect(markup).not.toContain('用途');
    expect(markup).not.toContain('aria-label="知识库范围"');
    expect(markup).not.toContain('挂载知识库');
  });
});

type ComposerProps = Parameters<typeof Composer>[0];

function renderComposer(overrides: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    kbName: 'shared',
    input: '',
    setInput: () => undefined,
    streaming: false,
    onSend: () => undefined,
    onStop: () => undefined,
    ...overrides,
  };
  return renderToStaticMarkup(<Composer {...props} />);
}

describe('Composer knowledge-base selector', () => {
  it('disables the primary knowledge-base selector while a turn is streaming', () => {
    const markup = renderComposer({ streaming: true });

    expect(markup).toMatch(/aria-label="选择会话锚定库"/);
    expect(markup).toMatch(/<button[^>]*\bdisabled(?:=""|(?=\s|>))[^>]*aria-label="选择会话锚定库"/);
  });

  it('labels the selector as the retrieval scope', () => {
    const markup = renderComposer();

    expect(markup).toContain('检索范围');
    expect(markup).toContain('会话锚定库');
  });

  it('does not render a multi-knowledge-base picker', () => {
    const markup = renderComposer({
      availableKbs: [
        writableKb,
        { ...writableKb, name: 'reference', kb_id: 2, permission: 'read' },
      ],
    });

    expect(markup).toContain('aria-label="选择会话锚定库"');
    expect(markup).not.toContain('aria-label="添加知识库"');
    expect(markup).not.toContain('参考库');
    expect(markup).not.toContain('用途');
    expect(markup).not.toContain('检索用途');
  });

  it('shows an explicit primary-knowledge-base prompt when nothing is mounted', () => {
    const markup = renderComposer({ kbName: '' });

    expect(markup).toContain('选择会话锚定库');
    expect(markup).toContain('未选择知识库');
  });

  it('hides the department suffix when all knowledge bases share one department', () => {
    expect(kbOptionLabel(writableKb, false)).toBe('shared');
    expect(kbOptionLabel(writableKb, true)).toBe('shared · 研发部');
  });

  it('does not render document-authoring controls', () => {
    const markup = renderComposer();

    expect(markup).toContain('添加附件');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('文档生成模式');
    expect(markup).not.toContain('chat-document-flow-toggle');
    expect(markup).not.toContain('文档快捷回复');
  });
});

describe('Composer attachment status', () => {
  it('shows the failure reason and retry affordance for a failed attachment', () => {
    const failedAttachment: AttachmentView = {
      attachment_id: 'att-failed',
      asset_id: 'asset-failed',
      filename: 'broken.pdf',
      media_type: 'application/pdf',
      extension: '.pdf',
      size_bytes: 128,
      sha256: 'hash',
      usage_hint: 'reference',
      status: 'active',
      parse_status: 'failed',
      error_code: 'parse_failed',
      error_message: 'PDF 内容无法读取',
      created_at: '2026-09-04T00:00:00Z',
    };
    const markup = renderComposer({
      attachmentsEnabled: true,
      draftAttachments: [failedAttachment],
      onRetryAttachment: () => undefined,
    });

    expect(markup).toContain('PDF 内容无法读取');
    expect(markup).toContain('重试解析');
  });

  it('does not show a template conversion action on uploaded attachments', () => {
    const attachment: AttachmentView = {
      attachment_id: 'att-template',
      asset_id: 'asset-template',
      filename: 'review.xlsx',
      media_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      extension: '.xlsx',
      size_bytes: 128,
      sha256: 'hash',
      usage_hint: 'reference',
      status: 'active',
      parse_status: 'ready',
      created_at: '2026-09-04T00:00:00Z',
    };
    const markup = renderComposer({
      attachmentsEnabled: true,
      draftAttachments: [attachment],
    });

    expect(markup).toContain('aria-label="添加附件"');
    expect(markup).toContain('chat-attachment-upload');
    expect(markup).toContain('会话锚定库');
    expect(markup).toContain('检索范围');
    expect(markup).toContain('review.xlsx');
    expect(markup).not.toContain('作为模板');
    expect(markup).not.toContain('将附件转换为文档模板');
  });
});

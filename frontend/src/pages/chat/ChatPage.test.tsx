import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import type { AttachmentView, DocumentContext, KbView } from '../../api/types';
import ChatPage, {
  isChatAttachmentsUiEnabled,
  isDocumentAuthoringChatEnabled,
  isTemplateGenerationChatIntent,
  listTemplateCandidateAttachments,
  pickAutoTemplateAttachment,
  resolveTemplateAttachmentRoute,
} from './ChatPage';
import Composer from './components/Composer';

const auth: AuthSession = {
  token: 'test-token',
  user: {
    username: 'alice',
    role: 'user',
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
};

function renderChat(documentAuthoringEnabled?: boolean, availableKbs: KbView[] = [writableKb]) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ChatPage
        auth={auth}
        kbName="shared"
        availableKbs={availableKbs}
        onLogout={() => undefined}
        {...(documentAuthoringEnabled === undefined ? {} : { documentAuthoringEnabled })}
      />
    </MemoryRouter>,
  );
}

describe('isDocumentAuthoringChatEnabled flag resolution (opt-out semantics)', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    // 每个用例从"两个变量都不存在"的基线出发,不依赖本地 .env 状态。
    delete (import.meta.env as Record<string, unknown>).VITE_AGENT_DOCUMENT_TOOLS_ENABLED;
    delete (import.meta.env as Record<string, unknown>).VITE_DOCUMENT_AUTHORING_CHAT_ENABLED;
  });

  it('defaults to enabled when both env vars are absent', () => {
    expect(isDocumentAuthoringChatEnabled()).toBe(true);
  });

  it('lets a deployment opt out with a falsy string', () => {
    vi.stubEnv('VITE_AGENT_DOCUMENT_TOOLS_ENABLED', 'false');
    expect(isDocumentAuthoringChatEnabled()).toBe(false);
  });

  it('stays enabled on truthy strings', () => {
    vi.stubEnv('VITE_AGENT_DOCUMENT_TOOLS_ENABLED', 'true');
    expect(isDocumentAuthoringChatEnabled()).toBe(true);
  });

  it('falls back to VITE_DOCUMENT_AUTHORING_CHAT_ENABLED', () => {
    vi.stubEnv('VITE_DOCUMENT_AUTHORING_CHAT_ENABLED', 'false');
    expect(isDocumentAuthoringChatEnabled()).toBe(false);
  });

  it('lets the explicit override beat the env value', () => {
    vi.stubEnv('VITE_AGENT_DOCUMENT_TOOLS_ENABLED', 'true');
    expect(isDocumentAuthoringChatEnabled(false)).toBe(false);
    vi.stubEnv('VITE_AGENT_DOCUMENT_TOOLS_ENABLED', 'false');
    expect(isDocumentAuthoringChatEnabled(true)).toBe(true);
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

describe('natural-language template generation routing', () => {
  const readyDocx: AttachmentView = {
    attachment_id: 'att-docx',
    asset_id: 'asset-docx',
    filename: 'icd-template.docx',
    media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    extension: '.docx',
    size_bytes: 10,
    sha256: 'hash',
    usage_hint: 'reference',
    status: 'active',
    parse_status: 'ready',
    created_at: new Date().toISOString(),
  };

  it('recognizes template fill commands but not ordinary document questions', () => {
    expect(isTemplateGenerationChatIntent('参考模板，根据知识库生成新的 ICD 文档')).toBe(true);
    expect(isTemplateGenerationChatIntent('将模板按照知识库内容回填')).toBe(true);
    expect(isTemplateGenerationChatIntent('参考模板，比较知识库和附件中的 HSI 文档差异')).toBe(false);
    expect(isTemplateGenerationChatIntent('这个 PDF 里有哪些接口？')).toBe(false);
  });

  it('auto-selects one ready office attachment and stays fail-closed for ambiguity', () => {
    expect(pickAutoTemplateAttachment('参考模板生成 ICD 文档', [readyDocx])).toEqual(readyDocx);
    const candidates = listTemplateCandidateAttachments('参考模板生成 ICD 文档', [
      readyDocx,
      { ...readyDocx, attachment_id: 'att-other', filename: 'other.xlsx', extension: '.xlsx' },
    ]);
    expect(candidates).toHaveLength(2);
    expect(pickAutoTemplateAttachment('参考模板生成 ICD 文档', candidates)).toBeNull();
    expect(resolveTemplateAttachmentRoute('参考模板生成 ICD 文档', candidates)).toMatchObject({
      requiresSelection: true,
      requiresTemplate: false,
      autoTemplate: null,
      authority: 'backend',
    });
    expect(resolveTemplateAttachmentRoute('参考模板生成 ICD 文档', candidates, true)).toMatchObject({
      requiresSelection: false,
      requiresTemplate: false,
      autoTemplate: null,
      authority: 'backend',
    });
  });

  it('keeps ambiguous and missing-template results as backend hints instead of blockers', () => {
    const ambiguous = resolveTemplateAttachmentRoute('参考模板生成 ICD 文档', [
      readyDocx,
      { ...readyDocx, attachment_id: 'att-other', filename: 'other.xlsx', extension: '.xlsx' },
    ]);
    const missing = resolveTemplateAttachmentRoute('参考模板生成 ICD 文档', []);

    expect(ambiguous.authority).toBe('backend');
    expect(ambiguous.requiresSelection).toBe(true);
    expect(missing.authority).toBe('backend');
    expect(missing.requiresTemplate).toBe(true);
  });

  it('fails closed while an intended template attachment is still parsing', () => {
    const pending = resolveTemplateAttachmentRoute('参考模板生成 ICD 文档', [{
      ...readyDocx,
      parse_status: 'queued',
    }]);

    expect(pending.waitingForTemplate).toBe(true);
    expect(pending.pendingCandidates).toHaveLength(1);
    expect(pending.autoTemplate).toBeNull();
  });
});

describe('ChatPage document authoring bridge', () => {
  it('keeps the legacy composer unchanged when the bridge is not enabled', () => {
    // 显式传 false:不依赖 VITE_AGENT_DOCUMENT_TOOLS_ENABLED 的本地默认值。
    const markup = renderChat(false);

    expect(markup).toContain('Enter 发送 / Shift+Enter 换行');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });

  it('uses the unified attachment entry instead of a dedicated template upload', () => {
    const markup = renderChat(true);

    expect(markup).toContain('添加附件');
    expect(markup).toContain('chat-attachment-upload');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });

  it('does not reintroduce a dedicated template upload for a read-only knowledge base', () => {
    const markup = renderChat(true, [{ ...writableKb, permission: 'read' }]);

    expect(markup).toContain('添加附件');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });
});

const attachedContext: DocumentContext = {
  analysis_id: 'analysis-1',
  template_version_id: 'template-v1',
  knowledge_base_name: 'shared',
  version: 1,
  expiry: new Date(Date.now() + 60_000).toISOString(),
  client_request_id: 'upload-key-1',
};

type ComposerProps = Parameters<typeof Composer>[0];

function renderComposer(overrides: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    kbName: 'shared',
    input: '',
    setInput: () => undefined,
    streaming: false,
    onSend: () => undefined,
    onStop: () => undefined,
    documentAuthoringEnabled: true,
    ...overrides,
  };
  return renderToStaticMarkup(<Composer {...props} />);
}

describe('Composer document generation toggle', () => {
  it('renders document quick replies beside the main conversation input', () => {
    const markup = renderComposer({ quickReplies: ['确认生成', '修改计划'] });

    expect(markup).toContain('aria-label="文档快捷回复"');
    expect(markup).toContain('确认生成');
    expect(markup).toContain('修改计划');
  });

  it('keeps the knowledge-base selector usable while a turn is streaming', () => {
    const markup = renderComposer({ streaming: true });

    expect(markup).toMatch(/aria-label="挂载知识库"/);
    expect(markup).not.toMatch(/<button[^>]*\bdisabled(?:=""|(?=\s|>))[^>]*aria-label="挂载知识库"/);
  });

  it('hides the generation toggle when no document context is attached', () => {
    const markup = renderComposer({ onToggleDocumentFlow: () => undefined });

    expect(markup).toContain('添加附件');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('文档生成模式');
    expect(markup).not.toContain('chat-document-flow-toggle');
  });

  it('renders the generation toggle checked by default with context attached', () => {
    const markup = renderComposer({
      documentContext: attachedContext,
      documentContextLabel: '评审表.xlsx',
      documentFlowEnabled: true,
      onToggleDocumentFlow: () => undefined,
    });

    expect(markup).toContain('文档生成模式');
    expect(markup).toContain('chat-document-flow-toggle');
    expect(markup).toMatch(/id="chat-document-flow-toggle"[^>]*checked/);
  });

  it('renders the generation toggle unchecked when the mode is turned off', () => {
    const markup = renderComposer({
      documentContext: attachedContext,
      documentFlowEnabled: false,
      onToggleDocumentFlow: () => undefined,
    });

    expect(markup).toContain('文档生成模式');
    expect(markup).not.toMatch(/id="chat-document-flow-toggle"[^>]*checked/);
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

  it('offers template conversion only for an eligible ready attachment with write access', () => {
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
      documentAuthoringEnabled: true,
      canUploadDocumentTemplate: true,
      draftAttachments: [attachment],
      onUseAsTemplate: () => undefined,
    });

    expect(markup).toContain('作为模板');
  });
});

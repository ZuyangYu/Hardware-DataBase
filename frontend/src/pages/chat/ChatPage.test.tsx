import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import type { DocumentContext, KbView } from '../../api/types';
import ChatPage, { isDocumentAuthoringChatEnabled } from './ChatPage';
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

describe('ChatPage document authoring bridge', () => {
  it('keeps the legacy composer unchanged when the bridge is not enabled', () => {
    // 显式传 false:不依赖 VITE_AGENT_DOCUMENT_TOOLS_ENABLED 的本地默认值。
    const markup = renderChat(false);

    expect(markup).toContain('Enter 发送 / Shift+Enter 换行');
    expect(markup).not.toContain('上传模板');
    expect(markup).not.toContain('chat-document-template-upload');
  });

  it('exposes the template upload entry only after explicit opt-in', () => {
    const markup = renderChat(true);

    expect(markup).toContain('上传模板');
    expect(markup).toContain('chat-document-template-upload');
    expect(markup).toContain('.xlsx,.xlsm,.docx');
  });

  it('keeps upload disabled for a read-only knowledge base', () => {
    const markup = renderChat(true, [{ ...writableKb, permission: 'read' }]);

    expect(markup).toContain('需 KB 写权限');
    expect(markup).toMatch(/id="chat-document-template-upload"[^>]*disabled/);
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
  it('hides the generation toggle when no document context is attached', () => {
    const markup = renderComposer({ onToggleDocumentFlow: () => undefined });

    expect(markup).toContain('上传模板');
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

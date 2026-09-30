import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { McpListFlags, McpServerView, McpToolView } from '../../api/types';
import { AdminExtensionsView, PluginsView, ServerFormFields } from './PluginsPage';

const flags: McpListFlags = {
  enabled: true,
  write_enabled: false,
  prerequisites_met: true,
  prerequisites_reason: '',
};

const server: McpServerView = {
  slug: 'kicad',
  name: 'KiCad 格式示例生成器（参考服务）',
  description: '原理图工具',
  transport: 'streamable_http',
  url: 'https://mcp.internal/kicad',
  headers: { Authorization: '••••' },
  has_credentials: true,
  timeout_s: 15,
  sse_read_timeout_s: null,
  enabled: true,
  department_scope: 'departments',
  department_ids: [7],
  disabled_tools: ['echo'],
  write_tools: ['writer'],
  tags: ['EDA'],
  icon: '',
  connection_fingerprint: 'fp1',
  last_test_fingerprint: 'fp1',
  last_test_ok_at: '2026-09-21T00:00:00+00:00',
  last_test_error: '',
  credentials_error: '',
  test_passed_for_current_config: true,
  created_by: 'admin',
  updated_by: 'admin',
  created_at: '2026-09-20',
  updated_at: '2026-09-21',
};

const tools: McpToolView[] = [
  {
    name: 'read_schematic',
    description: '读取原理图',
    read_only: true,
    disabled: false,
    write_allowed: false,
    blocked_reason: '',
  },
  {
    name: 'writer',
    description: '生成原理图',
    read_only: false,
    disabled: false,
    write_allowed: true,
    blocked_reason: 'write_disabled',
  },
];

function view(overrides: Partial<Parameters<typeof PluginsView>[0]> = {}) {
  return renderToStaticMarkup(
    <PluginsView
      flags={flags}
      servers={[server]}
      loaded
      busySlug={null}
      toolsFor="kicad"
      tools={tools}
      toolsLoading={false}
      onRefresh={() => undefined}
      onCreate={() => undefined}
      onEdit={() => undefined}
      onDelete={() => undefined}
      onTest={() => undefined}
      onToggleEnabled={() => undefined}
      onOpenTools={() => undefined}
      onCloseTools={() => undefined}
      onRefreshTools={() => undefined}
      onToggleTool={() => undefined}
      onToggleWriteTool={() => undefined}
      {...overrides}
    />,
  );
}

describe('PluginsView', () => {
  it('renders the registry, masked credentials and tool governance', () => {
    const markup = view();
    expect(markup).toContain('KiCad 格式示例生成器（参考服务）');
    expect(markup).toContain('kicad');
    expect(markup).toContain('已配置凭据');
    expect(markup).toContain('启用中');
    expect(markup).toContain('连接测试通过');
    expect(markup).toContain('data-mcp-tools-panel="kicad"');
    expect(markup).toContain('data-mcp-tool="read_schematic"');
    // Read-only tools have no write-whitelist checkbox.
    expect(markup).not.toContain('data-mcp-write-tool="read_schematic"');
    expect(markup).toContain('data-mcp-write-tool="writer"');
    expect(markup).toContain('写通道未启用');
  });

  it('surfaces the global switch and prerequisites banners', () => {
    expect(view({ flags: { ...flags, enabled: false } })).toContain('data-mcp-switch-off');
    expect(
      view({
        flags: { ...flags, prerequisites_met: false, prerequisites_reason: 'governor_disabled' },
      }),
    ).toContain('governor_disabled');
  });

  it('keeps registration available while the global switch is off', () => {
    const markup = view({ flags: { ...flags, enabled: false } });
    expect(markup).toContain('data-mcp-switch-off');
    expect(markup).toContain('MCP_ENABLED');
    expect(markup).toContain('data-mcp-create');
    const createTag = markup.slice(markup.indexOf('data-mcp-create'));
    expect(createTag.slice(0, createTag.indexOf('>'))).not.toContain('disabled');
  });

  it('renders the empty state without servers', () => {
    const markup = view({ servers: [], toolsFor: null });
    expect(markup).toContain('暂无登记的服务');
    expect(markup).not.toContain('data-mcp-tools-panel');
  });
});

describe('AdminExtensionsView', () => {
  function renderSection(activeSection: 'mcp' | 'skills') {
    return renderToStaticMarkup(
      <MemoryRouter>
        <AdminExtensionsView
          activeSection={activeSection}
          onSectionChange={() => undefined}
          mcpContent={<div data-mcp-content>远程服务目录</div>}
          skillContent={<div data-skill-content>Skill 审核目录</div>}
        />
      </MemoryRouter>,
    );
  }

  it('shows MCP and Skill as peer navigation choices, with only the selected page visible', () => {
    const mcp = renderSection('mcp');
    expect(mcp).toContain('MCP 登记服务');
    expect(mcp).toContain('Skill 上传与审核');
    expect(mcp).toContain('data-mcp-content');
    expect(mcp).not.toContain('data-skill-content');
    expect(mcp).toContain('id="admin-extensions-mcp-tab" type="button" aria-controls="admin-extensions-mcp-panel" aria-pressed="true"');

    const skills = renderSection('skills');
    expect(skills).toContain('data-skill-content');
    expect(skills).not.toContain('data-mcp-content');
    expect(skills).toContain('id="admin-extensions-skills-tab" type="button" aria-controls="admin-extensions-skills-panel" aria-pressed="true"');
  });
});

describe('ServerFormFields', () => {
  it('keeps stored credentials unless the admin clears them', () => {
    const markup = renderToStaticMarkup(
      <ServerFormFields
        initial={server}
        departments={[{ id: 7, name: '硬件部', member_count: 3 } as never]}
        submitting={false}
        onSubmit={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(markup).toContain('编辑时留空会保留已保存凭据');
    expect(markup).toContain('data-mcp-clear-headers');
    // The credential value is never echoed back into the form.
    expect(markup).not.toContain('secret-value');
    // Edit mode locks the slug.
    expect(markup).toContain('disabled');
  });

  it('renders a create form without the clear-credentials control', () => {
    const markup = renderToStaticMarkup(
      <ServerFormFields
        initial={null}
        departments={[]}
        submitting={false}
        onSubmit={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(markup).toContain('登记');
    expect(markup).not.toContain('data-mcp-clear-headers');
    expect(markup).toContain('data-mcp-advanced');
    expect(markup).toContain('data-mcp-form-scroll');
    expect(markup).toContain('data-mcp-form-footer');
    expect(markup).toContain('服务名称');
    expect(markup).toContain('服务地址');
    expect(markup).toContain('访问凭据');
    expect(markup).not.toContain('SSE（旧服务兼容，已弃用）');
  });

  it('only offers deprecated SSE as an existing-service compatibility option', () => {
    const markup = renderToStaticMarkup(
      <ServerFormFields
        initial={{ ...server, transport: 'sse' }}
        departments={[]}
        submitting={false}
        onSubmit={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(markup).toContain('SSE（旧服务兼容，已弃用）');
    expect(markup).toContain('data-mcp-advanced');
  });
});

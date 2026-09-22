import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { McpListFlags, McpServerView, McpToolView } from '../../api/types';
import { PluginsView, ServerFormFields } from './PluginsPage';

const flags: McpListFlags = {
  enabled: true,
  write_enabled: false,
  prerequisites_met: true,
  prerequisites_reason: '',
};

const server: McpServerView = {
  slug: 'kicad',
  name: '内网 KiCad',
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
    <MemoryRouter>
      <PluginsView
        flags={flags}
        servers={[server]}
        loaded
        busySlug={null}
        toolsFor="kicad"
        tools={tools}
        toolsLoading={false}
        userName="admin"
        onLogout={() => undefined}
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
      />
    </MemoryRouter>,
  );
}

describe('PluginsView', () => {
  it('renders the registry, masked credentials and tool governance', () => {
    const markup = view();
    expect(markup).toContain('内网 KiCad');
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
    expect(markup).toContain('留空即保留已保存凭据');
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
  });
});

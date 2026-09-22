import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import type { McpCatalogItem, McpListFlags } from '../api/types';
import { ExtensionsView } from './ExtensionsPage';

const flags: McpListFlags = {
  enabled: true,
  write_enabled: true,
  prerequisites_met: true,
  prerequisites_reason: '',
};

const subscribed: McpCatalogItem = {
  slug: 'kicad',
  name: '内网 KiCad',
  description: '原理图生成',
  icon: '',
  tags: ['EDA'],
  tool_count: 5,
  read_only_tool_count: 4,
  subscribed: true,
  allow_write: false,
  writable: true,
};

const available: McpCatalogItem = {
  ...subscribed,
  slug: 'datasheet',
  name: '器件手册',
  subscribed: false,
  allow_write: false,
  writable: false,
  tool_count: 2,
  read_only_tool_count: 2,
};

function view(overrides: Partial<Parameters<typeof ExtensionsView>[0]> = {}) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <ExtensionsView
        flags={flags}
        items={[subscribed, available]}
        loaded
        busySlug={null}
        userName="user1"
        onLogout={() => undefined}
        onRefresh={() => undefined}
        onSubscribe={() => undefined}
        onUnsubscribe={() => undefined}
        onToggleWrite={() => undefined}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

describe('ExtensionsView', () => {
  it('renders the egress notice, catalog and write consent only for writable subscriptions', () => {
    const markup = view();
    expect(markup).toContain('data-mcp-egress-notice');
    expect(markup).toContain('不会进入知识库证据链');
    expect(markup).toContain('内网 KiCad');
    expect(markup).toContain('器件手册');
    expect(markup).toContain('data-mcp-subscribed="kicad"');
    expect(markup).toContain('data-mcp-write-consent="kicad"');
    // Non-subscribed / non-writable servers never show write consent.
    expect(markup).not.toContain('data-mcp-write-consent="datasheet"');
  });

  it('hides write consent while the write channel is off', () => {
    const markup = view({ flags: { ...flags, write_enabled: false } });
    expect(markup).not.toContain('data-mcp-write-consent="kicad"');
    expect(markup).toContain('写通道当前未启用');
  });

  it('renders disabled and empty states', () => {
    const disabled = view({ flags: { ...flags, enabled: false }, items: [] });
    expect(disabled).toContain('data-mcp-disabled');
    expect(disabled).toContain('data-mcp-empty');
    expect(disabled).toContain('扩展能力未启用');
  });
});

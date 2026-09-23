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

  it('shows only the reviewed bundle invocation and committed output downloads', () => {
    const markup = view({
      skills: [{ skill_ref: 'skill/report', version: 'v1', bundle_ref: 'bundle/report',
        input_schema: { type: 'object', required: ['title'] } }],
      skillParameters: { 'skill/report': '{"title":"review"}' },
      skillTask: { task_id: 'task-reviewed', status: 'succeeded' },
      skillArtifacts: [
        { artifact_id: 'art-reviewed', filename: 'report.txt', state: 'committed' },
        { artifact_id: 'art-pending', filename: 'pending.txt', state: 'staged' },
      ],
      onRunSkill: () => undefined,
      onDownloadSkillArtifact: () => undefined,
    });
    expect(markup).toContain('data-sandbox-skills');
    expect(markup).toContain('skill/report');
    expect(markup).toContain('bundle/report');
    expect(markup).toContain('运行固定任务');
    expect(markup).toContain('下载 report.txt');
    expect(markup).not.toContain('下载 pending.txt');
  });
});

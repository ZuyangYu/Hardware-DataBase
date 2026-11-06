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
  name: 'KiCad 格式示例生成器（参考服务）',
  description: '只按参数返回示例摘要并生成简单 KiCad 格式候选文件；不读取或分析真实原理图，不调用 KiCad，也不运行 ERC。',
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
    expect(markup).toContain('id="extensions-skills-tab"');
    expect(markup).toContain('aria-selected="true"');
    expect(markup).toContain('id="extensions-mcp-panel"');
    expect(markup).toContain('hidden=""');
    expect(markup).toContain('data-mcp-egress-notice');
    expect(markup).toContain('不会进入知识库证据链');
    expect(markup).toContain('KiCad 格式示例生成器（参考服务）');
    expect(markup).toContain('不读取或分析真实原理图');
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
    expect(disabled).toContain('MCP 服务未启用');
  });

  it('keeps execution inputs out of the compact card and shows only committed output downloads', () => {
    const markup = view({
      skills: [{ skill_ref: 'skill/report', version: 'v1', bundle_ref: 'bundle/report',
        input_schema: { type: 'object', required: ['title'] }, subscribed: true }],
      skillParameters: { 'skill/report': '{"title":"review"}' },
      skillTask: { task_id: 'task-reviewed', status: 'succeeded' },
      skillArtifacts: [
        { artifact_id: 'art-reviewed', filename: 'report.txt', state: 'committed' },
        { artifact_id: 'art-pending', filename: 'pending.txt', state: 'staged' },
      ],
      onToggleSkill: () => undefined,
      onRunSkill: () => undefined,
      onDownloadSkillArtifact: () => undefined,
    });
    expect(markup).toContain('data-sandbox-skills');
    expect(markup).toContain('skill/report');
    expect(markup).toContain('bundle/report');
    expect(markup).toContain('已添加');
    expect(markup).toContain('测试 Skill');
    expect(markup).not.toContain('JSON 参数');
    expect(markup).not.toContain('在沙箱中运行');
    expect(markup).toContain('下载 report.txt');
    expect(markup).not.toContain('下载 pending.txt');
  });

  it('lets an employee add a scoped text Skill without offering execution controls', () => {
    const markup = view({ skills: [{ skill_ref: 'skill/bom-review', version: 'v1',
      bundle_ref: '', input_schema: {}, kind: 'text', description: '---\nname: bom-review\ndescription: 核对位号\n---\n# BOM 人工复核清单\n请检查冲突。' }],
      onToggleSkill: () => undefined });
    expect(markup).toContain('skill/bom-review');
    expect(markup).toContain('BOM 人工复核清单');
    expect(markup).toContain('核对位号');
    expect(markup).toContain('文本参考');
    expect(markup).toContain('添加 Skill');
    expect(markup).not.toContain('测试 Skill');
    expect(markup).not.toContain('在沙箱中运行');
    expect(markup).not.toContain('JSON 参数');
  });

  it('does not render write proposals inside personal extensions', () => {
    const markup = view();
    expect(markup).not.toContain('data-mcp-write-proposals');
    expect(markup).not.toContain('写入提案');
  });

  it('shows personal review state and offers department sharing only after approval', () => {
    const personal = { skill_ref: 'skill/personal/71/bom-guide', version: '1.0.0',
      approval_state: 'draft', share_scope: 'private', summary: '核对 BOM',
      kind: 'text' as const, package_build_state: '', department_id: '47', created_at: '2026-09-24' };
    const waiting = view({ personalSkills: [personal], userDepartmentId: '47', onUploadPersonal: async () => true });
    expect(waiting).toContain('上传个人 Skill');
    expect(waiting).toContain('待管理员审核');
    expect(waiting).not.toContain('共享到部门');
    const enabled = view({ personalSkills: [{ ...personal, approval_state: 'enabled' }],
      userDepartmentId: '47', onSharePersonal: () => undefined });
    expect(enabled).toContain('共享到部门');
    const shared = view({ personalSkills: [{ ...personal, approval_state: 'enabled', share_scope: 'department' }],
      userDepartmentId: '47', onSharePersonal: () => undefined });
    expect(shared).toContain('停止部门共享');
    expect(view({ personalSkills: [{ ...personal, approval_state: 'enabled' }],
      userDepartmentId: '48', onSharePersonal: () => undefined })).not.toContain('共享到部门');
    const executable = view({ personalSkills: [{ ...personal, kind: 'executable', package_build_state: 'uploaded' }],
      userDepartmentId: '47', onUploadPersonal: async () => true });
    expect(executable).toContain('执行型');
    expect(executable).toContain('等待管理员审查源包');
  });
});

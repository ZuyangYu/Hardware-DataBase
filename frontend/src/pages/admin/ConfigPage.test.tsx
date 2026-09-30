import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ConfigGroupsPanel, GROUPS, groupDirty } from './ConfigPage';

const noop = () => {};

function renderPanel(activeGroup: string, extra: { draft?: Record<string, string>; snapshot?: Record<string, string>; showAdvanced?: boolean } = {}) {
  return renderToStaticMarkup(
    <ConfigGroupsPanel
      groups={GROUPS}
      activeGroup={activeGroup}
      draft={extra.draft ?? {}}
      snapshot={extra.snapshot ?? {}}
      showAdvanced={extra.showAdvanced ?? false}
      onSelectGroup={noop}
      onFieldChange={noop}
    />,
  );
}

describe('ConfigPage category navigation', () => {
  it('renders only the selected category panel', () => {
    const index = GROUPS.findIndex((group) => group.title === '模型与提示词');
    const markup = renderPanel('模型与提示词', { showAdvanced: true });
    expect(markup).toContain(`id="config-group-${index}-panel"`);
    expect(markup).toContain(`aria-labelledby="config-group-${index}-tab"`);
    expect(markup).toContain('AGENT_LLM_PROVIDER');
    expect(markup).not.toContain('RAGFLOW_BASE_URL');
    expect(markup.match(/aria-pressed="true"/g)).toHaveLength(1);
  });

  it('keeps built-in observability out of the settings surface', () => {
    expect(GROUPS.map((group) => group.title)).toEqual([
      '检索',
      '账号与会话',
      '模型与提示词',
      '扩展（MCP，受管控）',
    ]);
    const keys = GROUPS.flatMap((group) => group.fields.map(([key]) => key));
    expect(keys.some((key) => key.startsWith('OBS_') || key.startsWith('OTEL_'))).toBe(false);
  });

  it('hides advanced tuning fields until the toggle is on', () => {
    const hidden = renderPanel('扩展（MCP，受管控）');
    expect(hidden).not.toContain('MCP_CALL_TIMEOUT_SECONDS');
    expect(hidden).toContain('MCP_ENABLED');
    const shown = renderPanel('扩展（MCP，受管控）', { showAdvanced: true });
    expect(shown).toContain('MCP_CALL_TIMEOUT_SECONDS');
  });

  it('renders prompt fields as full-width textareas', () => {
    const markup = renderPanel('模型与提示词', { showAdvanced: true });
    expect(markup).toContain('SYSTEM_PROMPT');
    expect(markup).toContain('<textarea');
  });

  it('labels fields that need a restart', () => {
    const markup = renderPanel('账号与会话', { showAdvanced: true });
    expect(markup).toContain('重启生效');
  });

  it('marks the category that has unsaved edits', () => {
    const markup = renderPanel('检索', {
      draft: { RAGFLOW_GOVERNANCE_DATASET_NAME: 'changed' },
      snapshot: { RAGFLOW_GOVERNANCE_DATASET_NAME: 'old' },
    });
    expect(markup).toContain('有未保存修改');
  });

  it('treats masked secrets as unchanged', () => {
    const group = GROUPS.find((item) => item.fields.some(([key]) => key === 'AUTH_DEFAULT_ADMIN_PASSWORD'));
    if (!group) throw new Error('auth group is missing');
    expect(groupDirty(group, { AUTH_DEFAULT_ADMIN_PASSWORD: '***' }, { AUTH_DEFAULT_ADMIN_PASSWORD: 'secret' })).toBe(false);
    expect(groupDirty(group, { AUTH_DEFAULT_ADMIN_PASSWORD: 'new' }, { AUTH_DEFAULT_ADMIN_PASSWORD: 'secret' })).toBe(true);
  });
});

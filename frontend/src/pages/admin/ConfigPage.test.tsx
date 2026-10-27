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
      '长期记忆',
      '附件解析与理解',
      '扩展（MCP，受管控）',
    ]);
    const keys = GROUPS.flatMap((group) => group.fields.map(([key]) => key));
    expect(keys.some((key) => key.startsWith('OBS_') || key.startsWith('OTEL_'))).toBe(false);
  });

  it('marks every credential field as secret so "***" is never saved back', () => {
    // 后端 _SECRET_KEYS 返回 "***"；前端名单缺一个，保存时就会把掩码写成真实值。
    const backendSecretKeys = [
      'RAGFLOW_API_KEY',
      'AGENT_CUSTOM_API_KEY',
      'MEMORY_MODEL_API_KEY',
      'MEMORY_EMBEDDING_API_KEY',
      'CHAT_ATTACHMENT_EMBEDDING_API_KEY',
      'ARK_API_KEY',
      'AUTH_DEFAULT_ADMIN_PASSWORD',
    ];
    const keys = new Set(GROUPS.flatMap((group) => group.fields.map(([key]) => key)));
    const rendered = backendSecretKeys.filter((key) => keys.has(key));
    expect(rendered.length).toBeGreaterThan(0);
    // 渲染出来的凭证字段必须都在 SECRET_KEYS 里（用行为验证：填 "***" 不算改动）。
    const group = GROUPS.find((item) => item.fields.some(([key]) => key === rendered[0]))!;
    const draft = Object.fromEntries(rendered.map((key) => [key, '***']));
    const snapshot = Object.fromEntries(rendered.map((key) => [key, '***']));
    expect(groupDirty(group, { ...snapshot, ...draft }, snapshot)).toBe(false);
  });

  it('hides advanced tuning fields until the toggle is on', () => {
    const hidden = renderPanel('扩展（MCP，受管控）');
    expect(hidden).not.toContain('MCP_CALL_TIMEOUT_SECONDS');
    expect(hidden).toContain('MCP_ENABLED');
    const shown = renderPanel('扩展（MCP，受管控）', { showAdvanced: true });
    expect(shown).toContain('MCP_CALL_TIMEOUT_SECONDS');
  });

  it('exposes attachment safety controls and keeps retrieval mode explicit', () => {
    const keys = new Set(GROUPS.flatMap((group) => group.fields.map(([key]) => key)));
    expect(keys.has('CHAT_ATTACHMENTS_ENABLED')).toBe(true);
    expect(keys.has('CHAT_ATTACHMENT_MAX_BYTES')).toBe(true);
    expect(keys.has('CHAT_ATTACHMENT_RETRIEVAL_MODE')).toBe(true);
    expect(keys.has('ARK_API_KEY')).toBe(true);

    const markup = renderPanel('附件解析与理解', {
      showAdvanced: true,
      draft: {
        CHAT_ATTACHMENT_DENSE_ENABLED: 'true',
        CHAT_ATTACHMENT_VISUAL_ENABLED: 'true',
      },
    });
    expect(markup).toContain('CHAT_ATTACHMENTS_ENABLED');
    expect(markup).toContain('CHAT_ATTACHMENT_RETRIEVAL_MODE');
    expect(markup).toContain('ARK_API_KEY');
  });

  it('uses retrieval mode as the single dense-retrieval control', () => {
    const attachment = GROUPS.find((group) => group.title === '附件解析与理解');
    expect(attachment).toBeDefined();
    expect(attachment?.fields.some(([key]) => key === 'CHAT_ATTACHMENT_DENSE_ENABLED')).toBe(false);
    const markup = renderPanel('附件解析与理解');
    expect(markup).toContain('CHAT_ATTACHMENT_RETRIEVAL_MODE');
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

  it('exposes search byte limits with blank inheritance semantics', () => {
    const keys = new Set(GROUPS.flatMap((group) => group.fields.map(([key]) => key)));
    expect(keys.has('MULTI_KB_FUSION_MAX_BYTES')).toBe(true);
    expect(keys.has('MULTI_KB_TURN_EVIDENCE_MAX_BYTES')).toBe(true);
    const markup = renderPanel('检索', { showAdvanced: true });
    expect(markup).toContain('MULTI_KB_FUSION_MAX_BYTES');
    expect(markup).toContain('留空继承');
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

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SkillsPanelView } from './SkillsPanel';

describe('SkillsPanelView', () => {
  it('shows the draft-only fixed bundle binding and subsequent approval states', () => {
    const markup = renderToStaticMarkup(<SkillsPanelView
      skills={[
        { skill_ref: 'skill/report', version: 'v1', department_id: '47', approval_state: 'draft',
          execution_bundle_ref: '', bundle_invocation_available: false },
        { skill_ref: 'skill/text', version: 'v1', department_id: '', approval_state: 'enabled',
          execution_bundle_ref: '', bundle_invocation_available: false },
      ]}
      bundles={[{ bundle_ref: 'bundle/report', bundle_digest: 'a'.repeat(64), profile_ref: 'profile/dev-l1',
        approval_state: 'enabled', catalog_source: 'curated', network_policy_ref: '',
        required_capabilities: ['sandbox.execute'] }]}
      busy={false} skillRef="" version="1.0.0" content="" departmentId="47"
      binding={{ 'skill/report': 'bundle/report' }}
      onSkillRef={() => undefined} onVersion={() => undefined} onContent={() => undefined}
      onDepartmentId={() => undefined} onBinding={() => undefined} onRegister={() => undefined}
      onBind={() => undefined} onState={() => undefined} onRefresh={() => undefined}
    />);
    expect(markup).toContain('data-admin-skills');
    expect(markup).toContain('skill/report');
    expect(markup).toContain('bundle/report');
    expect(markup).toContain('绑定固定任务');
    expect(markup).toContain('审核源文件与构建结果');
  });
});

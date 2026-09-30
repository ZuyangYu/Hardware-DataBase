import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SkillDepartmentSelect, SkillsPanelView, type SkillsPanelViewProps } from './SkillsPanel';

const baseProps: SkillsPanelViewProps = {
  skills: [],
  bundles: [],
  departments: [],
  busy: false,
  skillRef: '',
  version: '1.0.0',
  departmentId: '',
  binding: {},
  onSkillRef: () => undefined,
  onVersion: () => undefined,
  onDepartmentId: () => undefined,
  onBinding: () => undefined,
  onRegister: () => undefined,
  textSkillRef: '',
  textVersion: '1.0.0',
  textContent: '',
  textDepartmentId: '',
  onTextSkillRef: () => undefined,
  onTextVersion: () => undefined,
  onTextContent: () => undefined,
  onTextDepartmentId: () => undefined,
  onBind: () => undefined,
  onState: () => undefined,
  onRefresh: () => undefined,
  onBundleState: () => undefined,
};

function view(overrides: Partial<SkillsPanelViewProps> = {}) {
  return renderToStaticMarkup(<SkillsPanelView {...baseProps} {...overrides} />);
}

describe('SkillsPanelView', () => {
  it('uses named departments instead of asking the administrator to type an ID', () => {
    const markup = renderToStaticMarkup(<SkillDepartmentSelect id="skill-department" value="47"
      departments={[{ id: 1, name: 'system' }, { id: 47, name: '研发部' }]}
      onChange={() => undefined} />);
    expect(markup).toContain('可见部门');
    expect(markup).toContain('全部员工（所有部门）');
    expect(markup).toContain('研发部（ID 47）');
    expect(markup).toContain('value="47" selected=""');
    expect(markup).not.toContain('system');
    expect(markup).not.toContain('type="number"');
  });
  it('keeps creation controls out of the review workspace and selects a pending Skill', () => {
    const markup = view({
      skills: [
        { skill_ref: 'skill/report', version: 'v1', department_id: '47', approval_state: 'draft',
          execution_bundle_ref: '', bundle_invocation_available: false },
        { skill_ref: 'skill/text', version: 'v1', department_id: '', approval_state: 'enabled',
          execution_bundle_ref: '', bundle_invocation_available: false },
      ],
      bundles: [{ bundle_ref: 'bundle/report', bundle_digest: 'a'.repeat(64), profile_ref: 'profile/dev-l1',
        approval_state: 'enabled', catalog_source: 'curated', network_policy_ref: '',
        required_capabilities: ['sandbox.execute'] }],
      departmentId: '47',
      binding: { 'skill/report': 'bundle/report' },
    });

    expect(markup).toContain('data-admin-skills');
    expect(markup).toContain('data-skill-inventory');
    expect(markup).toContain('data-skill-detail');
    expect(markup).toContain('skill/report');
    expect(markup).toContain('aria-pressed="true"');
    expect(markup).toContain('上传执行型 Skill');
    expect(markup).toContain('导入文本 Skill');
    expect(markup).not.toContain('id="skill-package"');
    expect(markup).not.toContain('id="text-skill-content"');
    expect(markup).toContain('bundle/report');
    expect(markup).toContain('绑定固定任务');
    expect(markup).toContain('审核 Skill');
    expect(markup).toContain('已启用');
  });

  it('shows uploaded source review and build image approval only for the selected Skill', () => {
    const markup = view({
      skills: [{
        skill_ref: 'skill/export',
        version: '2.1.0',
        department_id: '42',
        approval_state: 'draft',
        execution_bundle_ref: 'bundle/skill-export',
        bundle_invocation_available: false,
        package_archive_digest: 'b'.repeat(64),
        package_files: ['SKILL.md', 'entrypoint.py'],
        package_review_flags: ['execution_markers_present'],
        package_build_state: 'built',
      }, {
        skill_ref: 'skill/other', version: '1.0.0', department_id: '13',
        approval_state: 'enabled', execution_bundle_ref: '',
        bundle_invocation_available: false, package_archive_digest: 'd'.repeat(64),
        package_build_state: 'built',
      }],
      bundles: [{
        bundle_ref: 'bundle/skill-export',
        bundle_digest: 'c'.repeat(64),
        profile_ref: 'profile/dev-l1',
        approval_state: 'draft',
        catalog_source: 'curated',
        network_policy_ref: '',
        required_capabilities: ['sandbox.execute'],
        metadata: { image_digest: 'sha256:image-id' },
      }],
    });

    expect(markup).toContain('data-skill-source-review="skill/export"');
    expect(markup).toContain('data-skill-build-review="bundle/skill-export"');
    expect(markup).toContain('源包 SHA-256');
    expect(markup).toContain('文本规则提示');
    expect(markup).not.toContain('data-skill-source-review="skill/other"');
    expect(markup).toContain('审核构建镜像');
    expect(markup).toContain('审核 Skill');
    expect(markup).toContain('管理员人工标记');
    expect(markup).toContain('data-skill-next-action');
    expect(markup).toContain('data-skill-more-actions');
  });

  it('offers deletion only on revoked Skills', () => {
    const skill = { skill_ref: 'skill/revoked', version: 'v1', department_id: '42',
      approval_state: 'revoked', execution_bundle_ref: '', bundle_invocation_available: false };
    expect(view({ skills: [skill], onDelete: async () => true })).toContain('删除 Skill');
    expect(view({ skills: [{ ...skill, approval_state: 'enabled' }], onDelete: async () => true })).not.toContain('删除 Skill');
  });

  it('displays imported Markdown files as a text-only review source', () => {
    const markup = view({
      skills: [{ skill_ref: 'skill/bom', version: '1.0.0', approval_state: 'draft',
        department_id: '47', execution_bundle_ref: '', bundle_invocation_available: false,
        text_archive_digest: 'a'.repeat(64), text_files: ['SKILL.md', 'references/checks.md'] }],
    });
    expect(markup).toContain('data-text-skill-source-review="skill/bom"');
    expect(markup).toContain('references/checks.md');
    expect(markup).toContain('下载 ZIP 审核');
    expect(markup).not.toContain('data-skill-build-review');
  });

  it('identifies employee submissions without offering execution binding', () => {
    const markup = view({
      skills: [{ skill_ref: 'skill/personal/71/bom-guide', version: '1.0.0',
        approval_state: 'draft', department_id: '47', owner_id: '71',
        catalog_source: 'employee', share_scope: 'private', execution_bundle_ref: '',
        bundle_invocation_available: false, text_archive_digest: 'a'.repeat(64),
        text_files: ['SKILL.md'] }],
    });
    expect(markup).toContain('员工个人上传');
    expect(markup).toContain('仅上传者（审核通过后可共享部门）');
    expect(markup).toContain('下载 ZIP 审核');
    expect(markup).toContain('审核 Skill');
    expect(markup).not.toContain('绑定固定任务');
  });

  it('requires source approval before an employee executable can be built', () => {
    const skill = { skill_ref: 'skill/personal/81/calculator', version: '1.0.0',
      approval_state: 'draft', department_id: '47', owner_id: '81',
      catalog_source: 'employee', share_scope: 'private', execution_bundle_ref: '',
      bundle_invocation_available: false, package_archive_digest: 'a'.repeat(64),
      package_files: ['SKILL.md', 'entrypoint.py'], package_build_state: 'uploaded',
      metadata: { submitted_input_schema: { type: 'object', properties: { voltage_v: { type: 'number' } }, additionalProperties: false } } };
    const pending = view({ skills: [skill], onApproveSource: () => undefined, onRetryBuild: () => undefined });
    expect(pending).toContain('审核源包并允许构建');
    expect(pending).not.toContain('>构建镜像</button>');
    expect(pending).toContain('查看员工提交的参数 Schema');
    expect(pending).toContain('voltage_v');
    const approved = view({ skills: [{ ...skill, package_build_state: 'source_reviewed' }],
      onApproveSource: () => undefined, onRetryBuild: () => undefined });
    expect(approved).toContain('源包已审核 · 待构建');
    expect(approved).toContain('>构建镜像</button>');
    expect(approved).not.toContain('审核源包并允许构建');
  });
});

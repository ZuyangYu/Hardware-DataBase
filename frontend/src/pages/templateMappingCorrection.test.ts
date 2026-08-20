import { describe, expect, it } from 'vitest';

import { buildTemplateCorrectionRequest } from './templateMappingCorrection';

const review = {
  analysis_id: 'analysis-1',
  template_version_id: 'template-1',
  content_hash: 'a'.repeat(64),
  format: 'xlsx',
  status: 'requires_human',
  units: [
    {
      unit_id: 'sheet:Review!A1',
      label: '固定标题',
      writable: true,
      structural_role_hint: 'fixed_label',
      candidate_for_auto_fill: false,
    },
    {
      unit_id: 'sheet:Review!B1',
      label: '项目名称',
      writable: true,
      structural_role_hint: 'placeholder',
      candidate_for_auto_fill: false,
    },
    {
      unit_id: 'sheet:Review!C1',
      label: '示例版本',
      writable: true,
      structural_role_hint: 'sample_value',
      candidate_for_auto_fill: false,
    },
  ],
  suggestions: [
    {
      semantic_unit_id: 'project-name',
      label: '项目名称',
      target_unit_ids: ['sheet:Review!B1'],
      retrieval_terms: ['project name'],
      confidence: 0.98,
      value_shape: 'scalar' as const,
      overwrite_basis: 'placeholder' as const,
    },
    {
      semantic_unit_id: 'revision',
      label: '版本号',
      target_unit_ids: ['sheet:Review!C1'],
      retrieval_terms: ['revision'],
      confidence: 0.98,
      value_shape: 'scalar' as const,
      overwrite_basis: 'sample_value' as const,
    },
  ],
  locked_unit_ids: [],
  reason_codes: ['fixed_label_target'],
};

describe('template mapping correction request', () => {
  it('submits only selected suggestions and removes a mapping when its target is locked', () => {
    expect(buildTemplateCorrectionRequest(
      review,
      new Set(['project-name', 'revision']),
      new Set(['sheet:Review!B1']),
      '锁定固定标题，仅保留示例值字段。',
    )).toEqual({
      expected_content_hash: 'a'.repeat(64),
      selected_suggestion_ids: ['revision'],
      locked_unit_ids: ['sheet:Review!B1'],
      comment: '锁定固定标题，仅保留示例值字段。',
    });
  });

  it('uses server suggestion ids rather than sending editable mapping content', () => {
    const invalidReview = {
      ...review,
      suggestions: [{
        ...review.suggestions[0],
        overwrite_basis: 'sample_value' as const,
      }],
    };

    expect(buildTemplateCorrectionRequest(
      invalidReview,
      new Set(['project-name']),
      new Set(),
      '保留占位符字段。',
    )).toEqual({
      expected_content_hash: 'a'.repeat(64),
      selected_suggestion_ids: ['project-name'],
      locked_unit_ids: [],
      comment: '保留占位符字段。',
    });
  });
});

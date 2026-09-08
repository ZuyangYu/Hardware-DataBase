import { describe, expect, it } from 'vitest';

import {
  describeDocumentUnitStatus,
  describeRevisionDiffSummary,
  describeWorkOrderStatus,
  describeHarnessProgress,
  documentCoverageBuckets,
  documentUnitStatusTone,
  hasDocumentGenerationWritePermission,
  nextActionsForStatus,
  resolveDeepLinkKb,
  resolveDocumentPhase,
} from './documentGenerationModel';
import type { DocumentCoverage } from '../api/types';

describe('document generation status model', () => {
  it('maps renderer blocking to an actionable Chinese status', () => {
    expect(describeWorkOrderStatus('blocked')).toEqual({
      label: '生成被阻止',
      tone: 'danger',
      action: '查看原因并重试',
    });
    expect(nextActionsForStatus('retrieving')).toContain('refresh');
  });

  it('normalizes the legacy complete status to the downloadable completed state', () => {
    expect(describeWorkOrderStatus('complete')).toEqual({
      label: '文档已生成',
      tone: 'success',
      action: '预览或下载文档',
    });
    expect(nextActionsForStatus('complete')).toEqual(['view_result']);
  });

  it('treats a completed harness with a renderer error as blocked', () => {
    expect(resolveDocumentPhase({
      work_order_id: 'wo-1',
      status: 'retrieving',
      scope_type: 'knowledge_base',
      unit_statuses: {},
      harness_run: {
        status: 'waiting_human',
        current_node: 'complete',
        error: 'abnormal duplicate long value fan-out is not allowed',
      },
      artifacts: [],
    })).toBe('blocked');
  });

  it('only enables generation changes for write-capable knowledge bases', () => {
    expect(hasDocumentGenerationWritePermission('read')).toBe(false);
    expect(hasDocumentGenerationWritePermission('write')).toBe(true);
    expect(hasDocumentGenerationWritePermission('admin')).toBe(true);
  });

  it('maps paused to a resumable Chinese state', () => {
    expect(describeWorkOrderStatus('paused')).toEqual({
      label: '任务已暂停',
      tone: 'warning',
      action: '可继续生成或取消任务',
    });
  });

  it('formats completed parallel units for the technical progress panel', () => {
    expect(describeHarnessProgress({ completed_units: 7, total_units: 66 })).toBe('已完成单元：7 / 66');
    expect(describeHarnessProgress({ completed_units: 0, total_units: 0 })).toBeNull();
  });

  it('keeps a deep-link kb preselect only when it exists in the accessible list', () => {
    const kbs = [{ name: '硬件知识库' }, { name: 'shared' }];
    expect(resolveDeepLinkKb('硬件知识库', kbs)).toBe('硬件知识库');
    expect(resolveDeepLinkKb('shared', kbs)).toBe('shared');
    expect(resolveDeepLinkKb('nonexistent', kbs)).toBe('');
    expect(resolveDeepLinkKb('', kbs)).toBe('');
    expect(resolveDeepLinkKb('硬件知识库', [])).toBe('');
    expect(resolveDeepLinkKb('硬件知识库', ['硬件知识库', 'other'])).toBe('硬件知识库');
    expect(resolveDeepLinkKb('Shared', kbs)).toBe('');
  });
});

describe('coverage panel helpers', () => {
  const coverage: DocumentCoverage = {
    fields: [
      { kind: 'field', field_id: 'rated_current', unit_id: 'field:rated_current', label: '额定电流', required: true, status: 'ready_to_render', coverage_status: 'supported', display_value: '10 A', evidence_count: 2 },
      { kind: 'field', field_id: 'pin_map', unit_id: 'field:pin_map', label: '管脚定义', required: false, status: 'conflicting', evidence_count: 0 },
      { kind: 'field', field_id: 'pinout', unit_id: 'field:pinout', label: 'pinout', required: false, status: 'planned', evidence_count: 0 },
      { kind: 'review', field_id: 'unit_check', unit_id: 'review:unit_check', label: '单位一致性', required: false, status: 'retrieval_failed', evidence_count: 0 },
    ],
    summary: { covered: 1, missing: 0, conflicting: 1, failed: 1, pending: 1 },
    total: 4,
  };

  it('speaks the user language for every execution unit status', () => {
    expect(describeDocumentUnitStatus('ready_to_render')).toBe('已完成');
    expect(describeDocumentUnitStatus('insufficient_evidence')).toBe('缺证据');
    expect(describeDocumentUnitStatus('conflicting')).toBe('冲突');
    expect(describeDocumentUnitStatus('tbd')).toBe('未提供');
    expect(describeDocumentUnitStatus('retrieval_failed')).toBe('检索失败');
    expect(describeDocumentUnitStatus('mystery')).toBe('mystery');
    expect(documentUnitStatusTone('ready_to_render')).toBe('success');
    expect(documentUnitStatusTone('insufficient_evidence')).toBe('warning');
    expect(documentUnitStatusTone('conflicting')).toBe('danger');
    expect(documentUnitStatusTone('planned')).toBe('neutral');
  });

  it('summarizes only non-empty buckets and hides the panel without data', () => {
    expect(documentCoverageBuckets(coverage)).toEqual([
      { key: 'covered', label: '已完成', tone: 'success', count: 1 },
      { key: 'conflicting', label: '冲突', tone: 'danger', count: 1 },
      { key: 'failed', label: '失败', tone: 'danger', count: 1 },
      { key: 'pending', label: '待处理', tone: 'neutral', count: 1 },
    ]);
    expect(documentCoverageBuckets(undefined)).toEqual([]);
    expect(documentCoverageBuckets({ fields: [], summary: { covered: 0, missing: 0, conflicting: 0, failed: 0, pending: 0 }, total: 0 })).toEqual([]);
  });

  it('describes the revision diff report without leaking internals', () => {
    expect(describeRevisionDiffSummary(undefined)).toBeNull();
    expect(describeRevisionDiffSummary({})).toBeNull();
    expect(describeRevisionDiffSummary({
      diff_report: { summary: { changed: 2, added: 1, removed: 0, unchanged: 40 }, truncated: false },
    })).toBe('差异报告：变更 2；新增 1；删除 0；保持不变 40');
    expect(describeRevisionDiffSummary({
      diff_report: { summary: { changed: 300 }, truncated: true },
    })).toContain('明细已截断');
    expect(describeRevisionDiffSummary({
      diff_report: { error: 'diff unavailable: missing file' },
    })).toBe('差异报告不可用：diff unavailable: missing file');
  });
});

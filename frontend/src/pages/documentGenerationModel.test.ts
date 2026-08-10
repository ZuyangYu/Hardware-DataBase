import { describe, expect, it } from 'vitest';

import {
  describeWorkOrderStatus,
  describeHarnessProgress,
  hasDocumentGenerationWritePermission,
  nextActionsForStatus,
  resolveDocumentPhase,
} from './documentGenerationModel';

describe('document generation status model', () => {
  it('maps renderer blocking to an actionable Chinese status', () => {
    expect(describeWorkOrderStatus('blocked')).toEqual({
      label: '生成被阻止',
      tone: 'danger',
      action: '查看原因并重试',
    });
    expect(nextActionsForStatus('retrieving')).toContain('refresh');
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
});

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { ExportJobView } from '@/api/types';
import ExportTaskCenter, { countActiveExportJobs, isExportActive } from './ExportTaskCenter';

const job = (status: string): ExportJobView => ({
  export_job_id: `job-${status}`,
  snapshot_id: 'snapshot-1',
  session_id: 3,
  turn_id: 'turn-1',
  format: 'pdf',
  content_shape: 'report',
  status,
  attempt: 1,
  error_message: '',
  artifact: null,
  created_at: '2026-09-02T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z',
  completed_at: null,
  tenant_id: 'default',
  department_id: null,
  knowledge_base_name: 'shared',
});

describe('ExportTaskCenter', () => {
  it('counts durable work independently from the current route', () => {
    expect(isExportActive('queued')).toBe(true);
    expect(isExportActive('running')).toBe(true);
    expect(isExportActive('succeeded')).toBe(false);
    expect(countActiveExportJobs([job('queued'), job('running'), job('failed')])).toBe(2);
  });

  it('renders a global task-center affordance', () => {
    const markup = renderToStaticMarkup(<ExportTaskCenter />);
    expect(markup).toContain('导出任务中心');
    expect(markup).toContain('查看后台导出任务');
  });
});

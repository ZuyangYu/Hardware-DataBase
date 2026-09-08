import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({
  api: { get: vi.fn(), post: vi.fn() },
  uploadFiles: vi.fn(),
  uploadFilesWithProgress: vi.fn(),
}));

import {
  analyzeTemplate,
  analyzeTemplateFromAttachment,
  buildDocumentContext,
  buildClarificationAnswerRequest,
  buildTemplateAnalyzeRequest,
  completeDocumentRevision,
  createDocumentRevision,
  fetchGenerationSession,
  fetchDocumentTaskProjection,
  isDocumentContextExpired,
  requestDocumentArtifactConversion,
  resumeDocumentTask,
  submitDocumentReviewDecision,
} from './documentAuthoring';
import { api, uploadFiles, uploadFilesWithProgress } from './client';
import type { DocumentAnalysis } from './types';

const analysis: DocumentAnalysis = {
  analysis_id: 'analysis-1',
  template_version_id: 'template-v1',
  format: 'xlsx',
  status: 'ready_for_confirmation',
  units: [],
  suggestions: [],
};

function templateFile(name = 'demo.xlsx'): File {
  return new File(['workbook'], name, { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
}

beforeEach(() => {
  vi.mocked(uploadFiles).mockReset();
  vi.mocked(uploadFilesWithProgress).mockReset();
  vi.mocked(api.get).mockReset();
  vi.mocked(api.post).mockReset();
});

describe('analyzeTemplate', () => {
  it('posts multipart to /document-generation/templates/analyze and returns analysis', async () => {
    const file = templateFile();
    vi.mocked(uploadFiles).mockResolvedValue(analysis);

    const result = await analyzeTemplate('shared', file, 'demo.xlsx');

    expect(result).toBe(analysis);
    expect(uploadFiles).toHaveBeenCalledTimes(1);
    expect(uploadFilesWithProgress).not.toHaveBeenCalled();
    const [path, form] = vi.mocked(uploadFiles).mock.calls[0];
    expect(path).toBe('/api/v1/document-generation/templates/analyze?kb=shared');
    expect(form).toBeInstanceOf(FormData);
    expect(form.get('file')).toBe(file);
    expect(form.get('template_name')).toBe('demo.xlsx');
    expect(form.has('client_request_id')).toBe(false);
  });

  it('url-encodes the kb query parameter', () => {
    const { path } = buildTemplateAnalyzeRequest('kb 空格/a', templateFile(), 'demo.xlsx');
    expect(path).toBe('/api/v1/document-generation/templates/analyze?kb=kb%20%E7%A9%BA%E6%A0%BC%2Fa');
  });

  it('routes progress callbacks through uploadFilesWithProgress and forwards client_request_id', async () => {
    const file = templateFile();
    vi.mocked(uploadFilesWithProgress).mockResolvedValue(analysis);
    const onProgress = vi.fn();

    const result = await analyzeTemplate('shared', file, file.name, {
      clientRequestId: 'req-1',
      onProgress,
    });

    expect(result).toBe(analysis);
    expect(uploadFiles).not.toHaveBeenCalled();
    expect(uploadFilesWithProgress).toHaveBeenCalledTimes(1);
    const [path, form, progress] = vi.mocked(uploadFilesWithProgress).mock.calls[0];
    expect(path).toBe('/api/v1/document-generation/templates/analyze?kb=shared');
    expect(form.get('file')).toBe(file);
    expect(form.get('template_name')).toBe('demo.xlsx');
    expect(form.get('client_request_id')).toBe('req-1');
    expect(progress).toBe(onProgress);
  });
  it('sends a custom template_name that differs from the file name', async () => {
    const file = templateFile('demo.xlsx');
    vi.mocked(uploadFiles).mockResolvedValue(analysis);

    await analyzeTemplate('shared', file, '自定义模板名');

    const [, form] = vi.mocked(uploadFiles).mock.calls[0];
    expect(form.get('template_name')).toBe('自定义模板名');
  });

  it('falls back to the file name when name is empty (workbench path)', async () => {
    const file = templateFile('demo.xlsx');
    vi.mocked(uploadFiles).mockResolvedValue(analysis);

    await analyzeTemplate('shared', file, '');

    const [, form] = vi.mocked(uploadFiles).mock.calls[0];
    expect(form.get('template_name')).toBe('demo.xlsx');
  });

  it('routes onProgress through uploadFilesWithProgress without client_request_id', async () => {
    const file = templateFile();
    vi.mocked(uploadFilesWithProgress).mockResolvedValue(analysis);
    const onProgress = vi.fn();

    await analyzeTemplate('shared', file, file.name, { onProgress });

    expect(uploadFiles).not.toHaveBeenCalled();
    expect(uploadFilesWithProgress).toHaveBeenCalledTimes(1);
    const [path, form, progress] = vi.mocked(uploadFilesWithProgress).mock.calls[0];
    expect(path).toBe('/api/v1/document-generation/templates/analyze?kb=shared');
    expect(form.has('client_request_id')).toBe(false);
    expect(progress).toBe(onProgress);
  });
});

describe('analyzeTemplateFromAttachment', () => {
  it('posts the session-scoped attachment reference without re-uploading bytes', async () => {
    vi.mocked(api.post).mockResolvedValue(analysis);

    const result = await analyzeTemplateFromAttachment('shared', 42, 'att-1', '评审表');

    expect(result).toBe(analysis);
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/document-generation/templates/analyze-from-attachment',
      {
        kb: 'shared',
        session_id: 42,
        attachment_id: 'att-1',
        template_name: '评审表',
      },
    );
  });
});

describe('requestDocumentArtifactConversion', () => {
  it('queues a semantic PDF/PPTX conversion with encoded identifiers', async () => {
    const job = {
      job_id: 'job-1',
      operation: 'convert_artifact' as const,
      status: 'queued',
      source_artifact_id: 'artifact-1',
      target_format: 'pdf' as const,
    };
    vi.mocked(api.post).mockResolvedValue(job);

    await expect(requestDocumentArtifactConversion('硬件 KB', 'artifact/a', 'pdf')).resolves.toBe(job);
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/document-generation/artifacts/artifact%2Fa/convert?kb=%E7%A1%AC%E4%BB%B6%20KB',
      { target_format: 'pdf' },
    );
  });
});

describe('buildClarificationAnswerRequest', () => {
  it('builds an idempotent session answer request with encoded identifiers', () => {
    expect(buildClarificationAnswerRequest(
      'session/1',
      'kb 空格',
      'missing_data_policy',
      '标记未提供',
      'answer-key-1',
    )).toEqual({
      path: '/api/v1/document-generation/sessions/session%2F1/messages?kb=kb%20%E7%A9%BA%E6%A0%BC',
      body: {
        question_id: 'missing_data_policy',
        answer: '标记未提供',
        client_request_id: 'answer-key-1',
      },
    });
  });

  it('fetches a generation session for restored clarification cards', async () => {
    const session = { session_id: 'session-1' };
    vi.mocked(api.get).mockResolvedValue(session);

    await expect(fetchGenerationSession('硬件 KB', 'session/1')).resolves.toBe(session);
    expect(api.get).toHaveBeenCalledWith(
      '/api/v1/document-generation/sessions/session%2F1?kb=%E7%A1%AC%E4%BB%B6%20KB',
    );
  });
});

describe('DocumentTask projection and revision requests', () => {
  it('fetches the task aggregate through the shared projection endpoint', async () => {
    const projection = { task_id: 'task-1' };
    vi.mocked(api.get).mockResolvedValue(projection);

    await expect(fetchDocumentTaskProjection('硬件 KB', 'task/a')).resolves.toBe(projection);
    expect(api.get).toHaveBeenCalledWith(
      '/api/v1/document-generation/tasks/task%2Fa/projection?kb=%E7%A1%AC%E4%BB%B6%20KB',
    );
  });

  it('creates a task-bound revision with its caller idempotency key', async () => {
    const revision = { revision_id: 'revision-1' };
    vi.mocked(api.post).mockResolvedValue(revision);
    const body = {
      parent_artifact_id: 'artifact-1',
      request_type: 'section_update' as const,
      request: '更新评审结论',
      client_request_id: 'revision-key-1',
    };

    await expect(createDocumentRevision('硬件 KB', 'task/a', body)).resolves.toBe(revision);
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/document-generation/tasks/task%2Fa/revisions?kb=%E7%A1%AC%E4%BB%B6%20KB',
      body,
    );
  });

  it('commits a worker-produced revision child with revalidation metadata', async () => {
    const revision = { revision_id: 'revision-1', status: 'revalidated' };
    vi.mocked(api.post).mockResolvedValue(revision);
    const body = {
      child_artifact_id: 'artifact-child',
      revalidation_status: 'passed' as const,
      revalidation_result: { report_id: 'report-child' },
    };

    await expect(completeDocumentRevision('硬件 KB', 'revision/a', body)).resolves.toBe(revision);
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/document-generation/revisions/revision%2Fa/complete?kb=%E7%A1%AC%E4%BB%B6%20KB',
      body,
    );
  });

  it('submits a hash-bound review decision and can resume by task identity', async () => {
    const decision = { review_id: 'review-1', status: 'approved' };
    vi.mocked(api.post).mockResolvedValueOnce(decision).mockResolvedValueOnce({
      task_id: 'task-1',
      status: 'queued',
    });

    await expect(submitDocumentReviewDecision('硬件 KB', 'review/a', {
      subject_hash: 'subject-hash',
      decision: { outcome: 'approved' },
      status: 'approved',
      client_request_id: 'decision-key-1',
    })).resolves.toBe(decision);
    await expect(resumeDocumentTask('硬件 KB', 'task/a')).resolves.toEqual({
      task_id: 'task-1',
      status: 'queued',
    });
    expect(api.post).toHaveBeenNthCalledWith(
      1,
      '/api/v1/document-generation/reviews/review%2Fa/decision?kb=%E7%A1%AC%E4%BB%B6%20KB',
      {
        subject_hash: 'subject-hash',
        decision: { outcome: 'approved' },
        status: 'approved',
        client_request_id: 'decision-key-1',
      },
    );
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/api/v1/document-generation/tasks/task%2Fa/resume?kb=%E7%A1%AC%E4%BB%B6%20KB',
      {},
    );
  });
});

describe('document context helpers (migrated to api/documentAuthoring)', () => {
  it('buildDocumentContext keeps the structured wire shape', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'req-1',
      Date.UTC(2026, 7, 31, 0, 0, 0),
    );
    expect(context).toEqual({
      analysis_id: 'analysis-1',
      template_version_id: 'template-v1',
      knowledge_base_name: 'shared',
      version: 1,
      expiry: '2026-08-31T00:30:00.000Z',
      client_request_id: 'req-1',
    });
  });

  it('isDocumentContextExpired fails closed for malformed expiry', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'req-1',
      Date.UTC(2026, 7, 31, 0, 0, 0),
    );
    expect(isDocumentContextExpired({ ...context!, expiry: 'not-a-date' })).toBe(true);
    expect(isDocumentContextExpired(context, Date.parse('2026-08-31T00:29:59.000Z'))).toBe(false);
  });
});

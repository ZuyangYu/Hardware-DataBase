import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({
  uploadFiles: vi.fn(),
  uploadFilesWithProgress: vi.fn(),
}));

import {
  analyzeTemplate,
  buildDocumentContext,
  buildTemplateAnalyzeRequest,
  isDocumentContextExpired,
} from './documentAuthoring';
import { uploadFiles, uploadFilesWithProgress } from './client';
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

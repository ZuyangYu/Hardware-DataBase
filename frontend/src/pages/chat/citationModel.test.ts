import { describe, expect, it } from 'vitest';
import { citationReadRequest, citationReadError, citationLocator } from './citationModel';
import { ApiError } from '@/api/client';

describe('citation source identity', () => {
  it('uses the cited KB and local record even when the file and remote chunk names collide', () => {
    expect(citationReadRequest({ source_name: 'same.pdf', locator: { chunk_id: 'shared' }, metadata: {
      kb_id: 205, local_record_id: 7, source_content_hash: 'hash-b',
      index_revision: 3, processing_artifact_id: 'parse-3', processor_version: 'parser-2', evidence_text_hash: 'b'.repeat(64),
      knowledge_source: { kb_id: 205, kb_name: 'B' },
    } })).toEqual({
      kb_id: 205, local_record_id: 7, source_content_hash: 'hash-b', locator: { chunk_id: 'shared' },
      index_revision: 3, processing_artifact_id: 'parse-3', processor_version: 'parser-2', evidence_text_hash: 'b'.repeat(64),
    });
  });

  it('preserves existing zero and empty processing stamps without inventing replacements', () => {
    expect(citationReadRequest({ metadata: {
      kb_id: 205, local_record_id: 7, source_content_hash: 'hash-b',
      index_revision: 0, processing_artifact_id: '', processor_version: '', evidence_text_hash: 'b'.repeat(64),
    } })).toMatchObject({ index_revision: 0, processing_artifact_id: '', processor_version: '', evidence_text_hash: 'b'.repeat(64) });
  });

  it.each(['index_revision', 'processing_artifact_id', 'processor_version', 'evidence_text_hash'])(
    'requires a new retrieval for legacy citations missing %s', (missing) => {
      const metadata: Record<string, unknown> = {
        kb_id: 205, local_record_id: 7, source_content_hash: 'hash-b',
        index_revision: 3, processing_artifact_id: 'parse-3', processor_version: 'parser-2', evidence_text_hash: 'b'.repeat(64),
      };
      delete metadata[missing];
      expect(citationReadRequest({ metadata })).toBeNull();
    },
  );

  it('refuses unstamped legacy citations instead of binding them to the current parse', () => {
    expect(citationReadRequest({ metadata: { kb_id: 205, local_record_id: 7, source_content_hash: 'hash-b' } })).toBeNull();
  });

  it('refuses file-name fallback or unbound source identity', () => {
    expect(citationReadRequest({ source_name: 'same.pdf', document_id: '7' })).toBeNull();
    expect(citationReadRequest({ metadata: { kb_id: 205, local_record_id: 7 } })).toBeNull();
  });

  it('explains revoked and changed sources without offering cached evidence', () => {
    expect(citationReadError(new ApiError(403, '', 'Forbidden'))).toContain('访问权限');
    expect(citationReadError(new ApiError(409, '', 'Conflict'))).toContain('已变化');
  });

  it('shows spreadsheet row numbers using the parser one-based coordinate', () => {
    expect(citationLocator({ locator: { sheet_name: 'Power', row_index: 2 } })).toBe('定位：Power · 第 2 行');
    expect(citationLocator({ locator: { sheet_name: 'Power', cell_ref: 'B2' } })).toBe('定位：Power · B2');
  });
});

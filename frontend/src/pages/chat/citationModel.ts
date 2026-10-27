import type { EvidenceItem } from '@/api/types';
import { ApiError } from '@/api/client';

export interface CitationReadRequest {
  kb_id: number;
  local_record_id: number;
  source_content_hash: string;
  index_revision: number;
  processing_artifact_id: string;
  processor_version: string;
  evidence_text_hash: string;
  locator: Record<string, unknown>;
}

export interface CitationReadback extends CitationReadRequest {
  kb_name: string;
  source_name: string;
  content: string;
}

export function citationReadRequest(item: EvidenceItem): CitationReadRequest | null {
  const metadata = item.metadata ?? {};
  const kbId = metadata.kb_id;
  const recordId = metadata.local_record_id;
  const hash = metadata.source_content_hash;
  const indexRevision = metadata.index_revision;
  const artifactId = metadata.processing_artifact_id;
  const processorVersion = metadata.processor_version;
  const evidenceHash = metadata.evidence_text_hash;
  if (typeof kbId !== 'number' || !Number.isSafeInteger(kbId) || kbId <= 0
    || typeof recordId !== 'number' || !Number.isSafeInteger(recordId) || recordId <= 0
    || typeof hash !== 'string' || !hash
    || typeof indexRevision !== 'number' || !Number.isSafeInteger(indexRevision) || indexRevision < 0
    || typeof artifactId !== 'string' || typeof processorVersion !== 'string'
    || typeof evidenceHash !== 'string' || !/^[0-9a-f]{64}$/.test(evidenceHash)) return null;
  // Older citations remain visible, but cannot bind themselves to a new parse.
  return {
    kb_id: kbId, local_record_id: recordId, source_content_hash: hash, locator: item.locator ?? {},
    index_revision: indexRevision, processing_artifact_id: artifactId,
    processor_version: processorVersion, evidence_text_hash: evidenceHash,
  };
}

export function citationReadError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return '来源访问权限已变化，无法读取原文。';
    if (error.status === 409) return '来源版本已变化，请重新检索后查看原文。';
    if (error.status === 404) return '该来源或引用位置已不可用，请重新检索。';
    if (error.status === 422) return '此来源暂不支持原文定位。';
  }
  return error instanceof Error ? error.message : '读取来源失败，请重试。';
}

export function citationLocator(item: EvidenceItem): string {
  const locator = item.locator ?? {};
  const sheet = String(locator.sheet_name ?? '');
  const cell = String(locator.cell_ref ?? '');
  const row = locator.row_index;
  if (sheet && cell) return `定位：${sheet} · ${cell}`;
  if (sheet && row !== undefined && row !== null) return `定位：${sheet} · 第 ${Number(row)} 行`;
  const page = locator.page ?? item.metadata?.page ?? item.metadata?.page_number;
  if (page !== undefined && page !== null && String(page)) return `定位：第 ${page} 页`;
  const chunk = locator.chunk_id ?? item.chunk_id;
  if (chunk) return `定位：片段 ${chunk}`;
  const design = locator.circuit_id ?? locator.design_id ?? item.metadata?.design_id;
  if (design) return `定位：设计 ${design}`;
  const conversation = locator.conversation_id;
  if (conversation) return `定位：对话 ${conversation} · 第 ${Number(locator.turn_index ?? 0) + 1} 轮`;
  return '';
}

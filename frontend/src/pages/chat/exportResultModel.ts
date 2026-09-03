import type { ExportFormat, ExportJobView } from '@/api/types';

export const EXPORT_FORMAT_LABELS: Record<ExportFormat, string> = {
  md: 'Markdown',
  xlsx: 'Excel',
  docx: 'Word',
  pdf: 'PDF',
  pptx: 'PowerPoint',
};

export function exportFormatLabel(format: string): string {
  return EXPORT_FORMAT_LABELS[format as ExportFormat] ?? format.toUpperCase();
}

const EXPORT_INTENT_PATTERN = /(导出|输出|生成|下载|保存为|转换为|整理成|export|output|generate|download|save as|convert)/i;
const FORMAT_PATTERNS: Array<{ format: ExportFormat; pattern: RegExp }> = [
  { format: 'md', pattern: /markdown|\bmd\b/i },
  { format: 'xlsx', pattern: /excel|xlsx|电子表格|表格/i },
  { format: 'docx', pattern: /word|docx|文档/i },
  { format: 'pdf', pattern: /pdf/i },
  { format: 'pptx', pattern: /power\s*point|pptx?|演示文稿|幻灯片/i },
];

/**
 * Recognize only explicit file-output language.  A bare mention such as
 * "PDF 中的电压" remains a normal retrieval question.
 */
export function requestedExportFormats(query: string): ExportFormat[] {
  const text = String(query || '').trim();
  if (!EXPORT_INTENT_PATTERN.test(text)) return [];
  if (/(不要|无需|不需要|别|禁止).{0,6}(导出|输出|生成|下载)/i.test(text)) return [];
  return FORMAT_PATTERNS.filter(({ pattern }) => pattern.test(text)).map(({ format }) => format);
}

export function buildExportRequest(
  turnId: string,
  format: ExportFormat,
  clientRequestId: string,
): {
  source_ref: { kind: 'turn'; id: string };
  formats: ExportFormat[];
  content_shape: 'report' | 'data';
  client_request_id: string;
  options?: { theme: 'light'; include_charts: true };
} {
  const request: {
    source_ref: { kind: 'turn'; id: string };
    formats: ExportFormat[];
    content_shape: 'report' | 'data';
    client_request_id: string;
  } = {
    source_ref: { kind: 'turn', id: turnId },
    formats: [format],
    content_shape: format === 'xlsx' ? 'data' : 'report',
    client_request_id: clientRequestId,
  };
  if (format === 'pptx') {
    return { ...request, options: { theme: 'light', include_charts: true } };
  }
  return request;
}

export function isExportTerminal(status: string): boolean {
  return ['succeeded', 'failed', 'cancelled', 'dead_letter'].includes(status);
}

export function mergeExportJob(current: ExportJobView[], next: ExportJobView): ExportJobView[] {
  const index = current.findIndex((item) => item.export_job_id === next.export_job_id);
  if (index < 0) return [...current, next];
  return current.map((item, itemIndex) => (itemIndex === index ? next : item));
}

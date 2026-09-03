import type { ChangeEvent } from 'react';

import type { ExportFormat } from '@/api/types';

const RELEASED_EXPORT_FORMATS: Array<{ value: ExportFormat; label: string }> = [
  { value: 'md', label: 'Markdown' },
  { value: 'xlsx', label: 'Excel' },
  { value: 'docx', label: 'Word' },
  { value: 'pdf', label: 'PDF' },
  { value: 'pptx', label: 'PowerPoint' },
];

export function exportFormatFromValue(value: string): ExportFormat | null {
  return RELEASED_EXPORT_FORMATS.some((item) => item.value === value)
    ? (value as ExportFormat)
    : null;
}

export function exportJobStatusLabel(status: string): string {
  switch (status) {
    case 'queued':
      return '排队中';
    case 'running':
      return '生成中';
    case 'succeeded':
      return '可下载';
    case 'cancelled':
      return '已取消';
    case 'dead_letter':
      return '处理失败';
    case 'failed':
      return '生成失败';
    default:
      return status || '未知状态';
  }
}

type Props = {
  disabled?: boolean;
  formats?: ExportFormat[];
  onExport: (format: ExportFormat) => void;
};

export default function ExportMenu({ disabled = false, formats, onExport }: Props) {
  const visibleFormats = formats
    ? RELEASED_EXPORT_FORMATS.filter((item) => formats.includes(item.value))
    : RELEASED_EXPORT_FORMATS;

  function handleChange(event: ChangeEvent<HTMLSelectElement>) {
    const format = exportFormatFromValue(event.target.value);
    event.target.value = '';
    if (format) onExport(format);
  }

  return (
    <div className="flex items-center gap-[7px] text-[11px] text-[#858b9c]">
      <span aria-hidden="true">导出结果</span>
      <select
        aria-label="导出格式"
        defaultValue=""
        disabled={disabled}
        onChange={handleChange}
        className="rounded-[7px] border border-[#e3e7f1] bg-white px-[7px] py-[3px] text-[11px] font-medium text-[#464c5e] outline-none transition-colors hover:border-[#c9d2e4] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <option value="" disabled>选择格式</option>
        {visibleFormats.map((item) => (
          <option key={item.value} value={item.value}>{item.label}</option>
        ))}
      </select>
    </div>
  );
}

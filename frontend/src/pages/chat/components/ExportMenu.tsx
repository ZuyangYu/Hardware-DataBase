import type { ExportFormat } from '@/api/types';
import AppIcon from '@/components/AppIcon';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

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

export function visibleExportFormats(
  formats?: ExportFormat[],
): Array<{ value: ExportFormat; label: string }> {
  return formats
    ? RELEASED_EXPORT_FORMATS.filter((item) => formats.includes(item.value))
    : RELEASED_EXPORT_FORMATS;
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
  const visibleFormats = visibleExportFormats(formats);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          aria-label="导出对话"
          title="导出本轮对话"
          className="inline-flex items-center gap-[5px] border-0 bg-transparent p-0 text-[11px] text-[#858b9c] transition-colors hover:text-[#18181a] hover:underline disabled:cursor-not-allowed disabled:opacity-45"
        >
          <AppIcon name="download" size={13} />
          导出对话
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" side="bottom" sideOffset={6} className="min-w-[180px]">
        <DropdownMenuLabel className="text-[11px] text-[#858b9c]">选择导出格式</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {visibleFormats.map((item) => (
          <DropdownMenuItem
            key={item.value}
            className="gap-[8px] text-[12px]"
            onSelect={() => onExport(item.value)}
          >
            <AppIcon name="file" size={14} className="text-[#68728a]" />
            {item.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

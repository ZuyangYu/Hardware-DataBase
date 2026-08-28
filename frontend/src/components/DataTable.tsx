import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from './ui';

export type DataTableColumn<T> = {
  /** Unique column key. */
  key: string;
  /** Header cell content. */
  title: ReactNode;
  /** Cell renderer. Falls back to `row[dataIndex]` when omitted. */
  render?: (row: T, index: number) => ReactNode;
  /** Shortcut for reading a plain field value when no `render` is provided. */
  dataIndex?: keyof T;
  /** Fixed column width (px number or any CSS width). */
  width?: number | string;
  align?: 'left' | 'center' | 'right';
  /** Extra classes for the body cell. */
  className?: string;
  /** Extra classes for the header cell. */
  headClassName?: string;
};

export type DataTableProps<T> = {
  columns: DataTableColumn<T>[];
  data: T[];
  rowKey: (row: T, index: number) => string | number;
  loading?: boolean;
  emptyText?: ReactNode;
  loadingText?: ReactNode;
  onRowClick?: (row: T, index: number) => void;
  /** Body row height. `default` = 64px, `compact` = 46px. */
  size?: 'default' | 'compact';
  /** Zebra striping: even rows get a subtle `#fbfbfb` fill. */
  striped?: boolean;
  /** Full grid: every cell is bordered instead of row-only dividers. */
  bordered?: boolean;
  /** Extra classes for the outer rounded container. */
  className?: string;
  'aria-label'?: string;
};

const ALIGN_CLASS = {
  left: 'text-left',
  center: 'text-center',
  right: 'text-right',
} as const;

const HEAD_CELL_CLASS =
  'h-[36px] bg-[#f2f3f7] px-[16px] py-[12px] align-middle text-[12px] font-normal text-[#464c5e]';
const BODY_CELL_CLASS = 'px-[16px] py-[12px] align-middle text-[12px] text-[#858b9c]';
const BODY_HEIGHT = {
  default: 'min-h-[64px]',
  compact: 'min-h-[46px]',
} as const;
const CELL_BORDER = 'border border-[#f2f3f7]';

/**
 * 业务数据表:圆角 `#f2f3f7` 外框、灰表头、白行 + 发丝分隔。
 * 基于 shadcn Table 原语,但持有产品专属样式。照搬自企业风 UI。
 */
export function DataTable<T>({
  columns,
  data,
  rowKey,
  loading = false,
  emptyText = '暂无数据',
  loadingText = '加载中…',
  onRowClick,
  size = 'default',
  striped = false,
  bordered = false,
  className,
  'aria-label': ariaLabel,
}: DataTableProps<T>) {
  const hasData = data.length > 0;
  const minTableWidth = columns.reduce((total, column) => {
    if (typeof column.width === 'number') return total + column.width;
    if (typeof column.width === 'string') return total + 140;
    return total + 160;
  }, 0);

  return (
    <div
      className={cn(
        'overflow-hidden rounded-[14px] border border-[#f2f3f7]',
        className,
      )}
    >
      <div className="overflow-x-auto">
        <Table
          className="w-full min-w-full table-auto text-[12px]"
          style={{ minWidth: Math.max(minTableWidth, 480) }}
          aria-label={ariaLabel}
        >
          <TableHeader>
            <TableRow className="border-0 hover:bg-transparent">
              {columns.map((column) => (
                <TableHead
                  key={column.key}
                  style={column.width ? { width: column.width } : undefined}
                  className={cn(
                    HEAD_CELL_CLASS,
                    bordered && CELL_BORDER,
                    ALIGN_CLASS[column.align ?? 'left'],
                    column.headClassName,
                  )}
                >
                  {column.title}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {hasData ? (
              data.map((row, index) => (
                <TableRow
                  key={rowKey(row, index)}
                  onClick={onRowClick ? () => onRowClick(row, index) : undefined}
                  className={cn(
                    'has-aria-expanded:bg-transparent',
                    bordered
                      ? 'border-0'
                      : 'border-b border-[#f2f3f7] last:border-0',
                    striped
                      ? index % 2 === 1
                        ? 'bg-[#fbfbfb] hover:bg-[#f2f3f7]'
                        : 'bg-white hover:bg-[#f2f3f7]'
                      : 'hover:bg-[#fafbfc]',
                    onRowClick && 'cursor-pointer',
                  )}
                >
                  {columns.map((column) => (
                    <TableCell
                      key={column.key}
                      style={column.width ? { width: column.width } : undefined}
                      className={cn(
                        BODY_CELL_CLASS,
                        BODY_HEIGHT[size],
                        bordered && CELL_BORDER,
                        ALIGN_CLASS[column.align ?? 'left'],
                        column.className,
                      )}
                    >
                      {column.render
                        ? column.render(row, index)
                        : column.dataIndex != null
                          ? (row[column.dataIndex] as ReactNode)
                          : null}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={columns.length} className="h-[160px] text-center align-middle text-[13px] text-[#858b9c]">
                  {loading ? loadingText : emptyText}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

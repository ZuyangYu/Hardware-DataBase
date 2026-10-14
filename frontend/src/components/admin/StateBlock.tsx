import type { ReactNode } from 'react';

import { Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';

/** 首次加载:明确的加载中状态。不渲染"暂无数据"假空状态。 */
export function LoadingState({ label = '正在加载…', className }: { label?: string; className?: string }) {
  return (
    <div className={cn('flex items-center gap-2 p-6 text-sm text-muted-foreground', className)} role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

/**
 * 刷新中:轻量标记,叠在已有内容上使用。
 * 刷新期间保留旧数据 + 本标记,切忌清空列表显示"暂无案例/暂无调用"。
 */
export function RefreshingBadge({ label = '正在更新' }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border bg-muted px-2 py-0.5 text-xs text-muted-foreground" role="status">
      <Loader2 className="size-3 animate-spin" aria-hidden />
      {label}
    </span>
  );
}

/** Errors are not empty results; preserve existing data and offer a read-only retry. */
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800 break-words">
    {message}
    {onRetry && <button type="button" className="ml-2 underline" onClick={onRetry}>重试读取</button>}
  </div>;
}

/** 确实无数据:空状态(与"未知/无数据" `—` 语义区分,不显示成 0)。 */
export function EmptyState({
  title,
  children,
  className,
}: {
  title: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('p-6 text-center text-sm text-muted-foreground', className)}>
      <p className="font-medium text-foreground">{title}</p>
      {children != null && <p className="mt-1 text-xs">{children}</p>}
    </div>
  );
}

/** 表格空数据行:保持表格结构,避免"白屏"或假空列表。 */
export function TableEmptyRow({
  colSpan,
  title = '暂无数据',
  children,
}: {
  colSpan: number;
  title?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="p-8 text-center text-sm text-muted-foreground">
        <p className="font-medium text-foreground">{title}</p>
        {children != null && <p className="mt-1 text-xs">{children}</p>}
      </td>
    </tr>
  );
}

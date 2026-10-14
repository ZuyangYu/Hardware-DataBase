import type { ReactNode } from 'react';

import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

export type StatusTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral' | 'readonly';

/**
 * 色调 -> 浅色底 + 深色字(浅色主题),保证 >= 4.5:1 对比度。
 * readonly 为虚线边框,表示"只读/不可操作",与空状态、普通中性状态区分。
 */
const TONE_CLASS: Record<StatusTone, string> = {
  success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  danger: 'border-red-200 bg-red-50 text-red-700',
  info: 'border-teal-200 bg-teal-50 text-teal-800',
  neutral: 'border-border bg-muted text-muted-foreground',
  readonly: 'border-dashed border-border bg-transparent text-muted-foreground',
};

/**
 * 统一状态徽章。颜色仅作辅助,文案(中文标签)始终存在,
 * 不单独依赖颜色表达状态(WCAG 1.4.1)。
 */
export function StatusBadge({
  tone = 'neutral',
  children,
  className,
  title,
}: {
  tone?: StatusTone;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <Badge variant="outline" className={cn('gap-1 border font-medium', TONE_CLASS[tone], className)} title={title}>
      {children}
    </Badge>
  );
}

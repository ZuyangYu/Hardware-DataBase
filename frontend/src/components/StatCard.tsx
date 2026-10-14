import type { ReactNode } from 'react';

import { cn } from '@/lib/utils';

export type StatCardTone = 'default' | 'green' | 'red';

const SURFACE_CLASS: Record<StatCardTone, string> = {
  default: 'bg-[#f6f6f6]',
  green: 'bg-[#e9f7ef]',
  red: 'bg-[#fce7e7]',
};
const VALUE_CLASS: Record<StatCardTone, string> = {
  default: 'text-[#18181a]',
  green: 'text-[#2cb360]',
  red: 'text-[#d20b0b]',
};
const LABEL_CLASS: Record<StatCardTone, string> = {
  default: 'text-[#464c5e]',
  green: 'text-[#2cb360]',
  red: 'text-[#d20b0b]',
};

export type StatCardProps = {
  value: ReactNode;
  label: ReactNode;
  /** 色调。`default` = 中性灰卡,`green`/`red` = 着色。 */
  tone?: StatCardTone;
  valueClassName?: string;
  className?: string;
  /** 整卡悬浮说明(如统计口径),避免截断信息只能靠 tooltip。 */
  title?: string;
};

/**
 * 指标卡:圆角着色面 + 大数字 + 尾随标签。照搬自企业风 UI。
 * 长金额/长标签不再截断:高度改为 min-h 允许内容增高,值与标签均可
 * 收缩换行;固定短标签视觉与旧版一致。
 */
export function StatCard({ value, label, tone = 'default', valueClassName, className, title }: StatCardProps) {
  return (
    <div
      title={title}
      className={cn(
        'flex min-h-[70px] flex-1 basis-[180px] items-center rounded-[14px] px-[24px] py-[8px]',
        SURFACE_CLASS[tone],
        className,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-end gap-[6px]">
        <span className={cn('min-w-0 break-words text-[26px] font-semibold leading-tight', VALUE_CLASS[tone], valueClassName)}>
          {value}
        </span>
        <span className={cn('min-w-0 text-[14px] leading-snug', LABEL_CLASS[tone])}>{label}</span>
      </div>
    </div>
  );
}

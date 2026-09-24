import { cn } from '@/lib/utils';

export type BrandLogoProps = {
  /** 只显示 mark,隐藏 wordmark。 */
  markOnly?: boolean;
  /** mark 方块边长(px)。 */
  markSize?: number;
  className?: string;
  /** wordmark 包裹层的额外 class(如折叠态响应式隐藏)。 */
  wordmarkClassName?: string;
};

/**
 * 品牌字标:纯文本锁定,不再使用图形 logo。
 * markOnly 时显示一个紧凑的文本徽记,展开态显示中英文品牌名。
 */
export default function BrandLogo({ markOnly = false, markSize = 28, className, wordmarkClassName }: BrandLogoProps) {
  if (markOnly) {
    return (
      <span
        className={cn(
          'grid place-items-center rounded-[10px] border border-[#dfe5f2] bg-white px-[10px] text-[12px] font-semibold tracking-[0.12em] text-[#18181a]',
          className,
        )}
        style={{ minWidth: markSize + 10, height: markSize + 6 }}
      >
        HD
      </span>
    );
  }

  return (
    <span className={cn('flex items-center overflow-hidden p-[4px]', className)}>
      <span className={cn('flex min-w-0 flex-col items-start gap-[2px] leading-none', wordmarkClassName)}>
        <strong className="text-[16px] font-semibold leading-none text-[#18181a]">硬件数据平台</strong>
        <span className="text-[11px] leading-none text-[#858b9c]">Hardware DataBase</span>
      </span>
    </span>
  );
}

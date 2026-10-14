import { useState } from 'react';

import { Check, Copy } from 'lucide-react';

import { cn } from '@/lib/utils';

/** 复制到剪贴板;失败(非安全上下文/权限拒绝)时静默忽略,文本仍可手动选中。 */
async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    return false;
  }
}

/** 独立复制按钮:hover/focus 出现,复制成功短暂显示对勾。 */
export function CopyIconButton({ value, label }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={copied ? '已复制到剪贴板' : `复制${label ?? '值'} ${value}`}
      className={cn(
        'inline-flex shrink-0 items-center rounded p-0.5 align-middle text-muted-foreground transition-colors',
        'opacity-0 hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100',
      )}
      onClick={() => {
        void copyText(value).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
    </button>
  );
}

/**
 * 技术值(长 ID / revision / trace / 价格版本)统一展示:
 * 等宽字体 + 省略截断 + hover/focus 出现的复制按钮 + 复制成功反馈。
 * title 属性保留完整值,键盘与读屏用户均可获取。
 */
export function CopyableTechnicalValue({
  value,
  display,
  label,
  className,
}: {
  value: string;
  display?: string;
  label?: string;
  className?: string;
}) {
  const text = display ?? value;

  return (
    <span className={cn('group inline-flex min-w-0 max-w-full items-center gap-1 align-middle', className)}>
      <code className="truncate font-mono text-xs" title={value}>
        {label != null && <span className="mr-1 text-muted-foreground">{label}</span>}
        {text}
      </code>
      <CopyIconButton value={value} label={label} />
    </span>
  );
}

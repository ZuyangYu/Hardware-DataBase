import { useState } from 'react';

import { Check, Copy } from 'lucide-react';

import { cn } from '@/lib/utils';

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
  const [copied, setCopied] = useState(false);
  const text = display ?? value;

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 非安全上下文或权限拒绝时静默忽略:文本仍可手动选中复制 */
    }
  }

  return (
    <span className={cn('group inline-flex min-w-0 max-w-full items-center gap-1 align-middle', className)}>
      <code className="truncate font-mono text-xs" title={value}>
        {label != null && <span className="mr-1 text-muted-foreground">{label}</span>}
        {text}
      </code>
      <button
        type="button"
        aria-label={copied ? '已复制到剪贴板' : `复制${label ?? '值'} ${value}`}
        className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100"
        onClick={() => void copy()}
      >
        {copied ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
      </button>
    </span>
  );
}

import type { ReactNode } from 'react';
import { toast, type ExternalToast } from 'sonner';
import { CheckCircle2, XCircle } from 'lucide-react';

import { cn } from '@/lib/utils';

type ToastVariant = 'success' | 'error';

// 照搬 的 中性消息 pill 配色(成功绿/失败红)
const VARIANTS: Record<
  ToastVariant,
  { container: string; icon: string; Icon: typeof CheckCircle2 }
> = {
  success: {
    container: 'border-[#96d9b0] bg-[#e9f7ef] text-[#018434]',
    icon: 'text-[#2cb360]',
    Icon: CheckCircle2,
  },
  error: {
    container: 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]',
    icon: 'text-[#d20b0b]',
    Icon: XCircle,
  },
};

function ToastPill({ variant, message }: { variant: ToastVariant; message: ReactNode }) {
  const { container, icon, Icon } = VARIANTS[variant];
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'pointer-events-auto flex max-w-full items-center gap-[12px] rounded-[14px] border border-solid px-[24px] py-[10px] shadow-[0px_12px_32px_rgba(0,0,0,0.12)]',
        container,
      )}
    >
      <Icon className={cn('size-[16px] shrink-0', icon)} aria-hidden="true" />
      <span className="text-[14px] leading-[normal] wrap-anywhere">{message}</span>
    </div>
  );
}

export type AppToastOptions = Omit<
  ExternalToast,
  'icon' | 'className' | 'style' | 'unstyled' | 'descriptionClassName'
>;

function showVariant(variant: ToastVariant, message: ReactNode, options?: AppToastOptions) {
  return toast.custom(() => <ToastPill variant={variant} message={message} />, {
    duration: variant === 'success' ? 3200 : 4800,
    unstyled: true,
    className: 'flex w-full justify-center',
    ...options,
  });
}

/** 全局 toast helper(对齐 的 notify) */
export const notify = {
  success: (message: ReactNode, options?: AppToastOptions) =>
    showVariant('success', message, options),
  error: (message: ReactNode, options?: AppToastOptions) => showVariant('error', message, options),
  warning: (message: ReactNode, options?: AppToastOptions) => toast.warning(message, options),
  info: (message: ReactNode, options?: AppToastOptions) => toast.info(message, options),
  loading: (message: ReactNode, options?: AppToastOptions) => toast.loading(message, options),
  dismiss: (id?: string | number) => toast.dismiss(id),
};

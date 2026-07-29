import type { ReactNode } from 'react';
import { AlertDialog as AlertDialogPrimitive } from 'radix-ui';
import { TriangleAlert } from 'lucide-react';

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export type ConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 标题,支持富文本(如把目标名放进 `<strong>`)。 */
  title: ReactNode;
  /** 标题下方的补充说明。 */
  description?: ReactNode;
  confirmText?: string;
  cancelText?: string;
  onConfirm: () => void;
  /** true 时按钮禁用、遮罩/Esc 不可关闭。 */
  loading?: boolean;
  /** 危险(红色)确认按钮。默认 true(匹配删除流程)。 */
  destructive?: boolean;
  /** 覆盖头部前置图标;传 `null` 隐藏。 */
  icon?: ReactNode;
};

/**
 * 确认弹窗(中性灰设计):警告图标 + 标题、muted 说明、右对齐取消/确认。
 * 基于 Radix AlertDialog,焦点陷阱与 a11y 由原语处理。照搬自企业风 UI。
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmText = '删除',
  cancelText = '取消',
  onConfirm,
  loading = false,
  destructive = true,
  icon,
}: ConfirmDialogProps) {
  const leadingIcon =
    icon === undefined ? (
      <TriangleAlert className="mt-px size-[16px] shrink-0 fill-[#ff7f00] text-[#ff7f00]" />
    ) : (
      icon
    );

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (loading && !next) return;
        onOpenChange(next);
      }}
    >
      <AlertDialogContent className="gap-0 overflow-hidden rounded-[16px] p-0">
        <div className="flex items-start gap-[8px] px-[16px] pt-[16px] pb-[12px]">
          {leadingIcon}
          <AlertDialogTitle className="min-w-0 flex-1 text-[14px] leading-[normal] font-medium text-[#18181a] [word-break:break-word]">
            {title}
          </AlertDialogTitle>
        </div>
        {description != null && (
          <div className="px-[24px] pb-[12px]">
            <AlertDialogDescription className="text-[14px] leading-[20px] text-[#4f5669] [word-break:break-word]">
              {description}
            </AlertDialogDescription>
          </div>
        )}
        <div className="flex items-center justify-end gap-[8px] pt-[12px] pr-[16px] pb-[16px] pl-[12px]">
          <AlertDialogPrimitive.Cancel asChild>
            <Button
              variant="outline"
              disabled={loading}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              {cancelText}
            </Button>
          </AlertDialogPrimitive.Cancel>
          <AlertDialogPrimitive.Action asChild>
            <Button
              disabled={loading}
              className={cn(
                'h-[32px] w-[80px] rounded-[10px] px-[12px] py-[8px] text-[14px] font-normal',
                destructive
                  ? 'bg-[#d20b0b] text-white hover:bg-[#b80909]'
                  : 'bg-[#18181a] text-white hover:bg-[#303030]',
              )}
              onClick={(event) => {
                event.preventDefault();
                onConfirm();
              }}
            >
              {confirmText}
            </Button>
          </AlertDialogPrimitive.Action>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}

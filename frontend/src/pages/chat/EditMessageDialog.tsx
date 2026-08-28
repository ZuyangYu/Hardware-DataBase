/**
 * 编辑用户消息对话框:修改原文(PATCH content)或脱敏删除原文(PATCH redact)。
 * 受控组件,提交态由父级持有;对话框本身即明确意图界面,不做二次确认。
 */
import { useEffect, useState } from 'react';

import type { MessageView } from '@/api/types';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

type Props = {
  open: boolean;
  /** 编辑目标;null 仅在关闭时出现。 */
  message: MessageView | null;
  /** 任一提交动作正在进行。 */
  submitting: boolean;
  onClose: () => void;
  /** 保存修改 -> PATCH { content }。 */
  onSaveEdit: (content: string, reason: string) => void;
  /** 脱敏并删除原文 -> PATCH { redact: true }。 */
  onRedact: (reason: string) => void;
};

export default function EditMessageDialog({
  open,
  message,
  submitting,
  onClose,
  onSaveEdit,
  onRedact,
}: Props) {
  const [content, setContent] = useState('');
  const [reason, setReason] = useState('');
  const hasContent = content.trim().length > 0;

  useEffect(() => {
    if (open) {
      setContent(message?.content ?? '');
      setReason('');
    }
  }, [open, message]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent className="max-w-[calc(100%-2rem)] gap-0 rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[520px]">
        <DialogHeader className="gap-[4px] px-[20px] pt-[18px] pb-[10px]">
          <DialogTitle className="text-[15px] leading-[normal] font-semibold text-[#18181a]">
            编辑消息
          </DialogTitle>
          <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
            修改将保留消息的对话位置；脱敏会用占位文本替换原内容且不可恢复。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-[14px] px-[20px] py-[6px]">
          <div className="grid gap-[6px]">
            <Label htmlFor="edit-message-content" className="text-[12px] text-[#464c5e]">
              消息内容
            </Label>
            <Textarea
              id="edit-message-content"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              disabled={submitting}
              rows={5}
              className="min-h-[120px] resize-y rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[13px] leading-[20px] text-[#18181a]"
            />
          </div>
          <div className="grid gap-[6px]">
            <Label htmlFor="edit-message-reason" className="text-[12px] text-[#464c5e]">
              变更原因（可选）
            </Label>
            <Input
              id="edit-message-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              disabled={submitting}
              placeholder="例如：修正设备型号笔误"
              className="h-[34px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px] text-[#18181a]"
            />
          </div>
        </div>
        <div className="flex items-center justify-end gap-[8px] px-[16px] py-[16px]">
          <Button
            type="button"
            variant="outline"
            disabled={submitting}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] shadow-none hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={submitting}
            onClick={() => onRedact(reason.trim())}
            title="用“[已脱敏]”占位文本替换原内容"
            className={cn(
              'h-[32px] rounded-[10px] border-[#d20b0b] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#d20b0b] shadow-none',
              'hover:bg-[#fce7e7] hover:text-[#d20b0b]',
            )}
          >
            {submitting ? '处理中…' : '脱敏并删除原文'}
          </Button>
          <Button
            type="button"
            disabled={!hasContent || submitting}
            onClick={() => onSaveEdit(content.trim(), reason.trim())}
            className="h-[32px] rounded-[10px] bg-[#18181a] px-[12px] py-[8px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            保存修改
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

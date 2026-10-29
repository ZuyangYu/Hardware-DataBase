/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 检索范围下拉框固定在输入框左上角(文本域上方)。
 * 附件入口收敛为输入框左侧的"+"按钮:悬停提示"添加附件",菜单内上传/重试/移除;
 * 检索范围统一在输入区设置:只选择当前会话使用的知识库。
 * 发送后的附件只出现在消息气泡中。
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';

import type { AttachmentView, KbView } from '@/api/types';
import { CHAT_ATTACHMENT_ACCEPT } from '@/api/chatAttachments';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
  CHAT_COMPOSER_ATTACHMENT_COPY_CLASS,
  CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
  CHAT_COMPOSER_ATTACHMENT_NAME_CLASS,
  CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS,
  CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS,
  CHAT_COMPOSER_ATTACHMENTS_CLASS,
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_HINT_CLASS,
  CHAT_COMPOSER_MODEL_BTN_CLASS,
  CHAT_COMPOSER_PLUS_BTN_CLASS,
  CHAT_COMPOSER_SEND_BTN_CLASS,
  CHAT_COMPOSER_STAGE_CLASS,
  CHAT_COMPOSER_STOP_BTN_CLASS,
  CHAT_COMPOSER_TEXTAREA_CLASS,
  CHAT_COMPOSER_TOOLBAR_CLASS,
  CHAT_INPUT_SHELL_CLASS,
} from '../chatPageStyles';

type Props = {
  kbName: string;
  availableKbs?: KbView[];
  input: string;
  setInput: (value: string) => void;
  streaming: boolean;
  disabled?: boolean;
  onKbChange?: (kbName: string) => void;
  onSend: () => void;
  onStop: () => void;
  /** 会话附件(默认开):后端 CHAT_ATTACHMENTS_ENABLED 打开时由 ChatPage 传入。 */
  attachmentsEnabled?: boolean;
  draftAttachments?: AttachmentView[];
  attachmentUploadPending?: boolean;
  attachmentRetryingId?: string | null;
  onUploadAttachment?: (file: File) => void | Promise<void>;
  onRetryAttachment?: (attachmentId: string) => void | Promise<void>;
  onRemoveAttachment?: (attachmentId: string) => void;
};

/** 与后端 CHAT_ATTACHMENT_MAX_FILES_PER_SESSION 对齐(单会话活动附件上限)。 */
const ATTACHMENT_MAX_FILES = (() => {
  const configured = Number(import.meta.env.VITE_CHAT_ATTACHMENT_MAX_FILES ?? '');
  return Number.isFinite(configured) && configured > 0 ? configured : 20;
})();

export function kbOptionLabel(kb: KbView, showDepartment: boolean): string {
  return showDepartment && kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
}

function attachmentStateLabel(attachment: AttachmentView): string {
  if (attachment.parse_status === 'ready') return '已就绪';
  if (attachment.parse_status === 'degraded') return '部分可读';
  if (attachment.parse_status === 'failed') return '解析失败';
  return '解析中…';
}

export default function Composer({
  kbName,
  availableKbs = [],
  input,
  setInput,
  streaming,
  disabled = false,
  onKbChange = () => undefined,
  onSend,
  onStop,
  // Attachment support is on by default; ChatPage passes the explicit
  // environment-controlled value when an installation opts out.
  attachmentsEnabled = true,
  draftAttachments = [],
  attachmentUploadPending = false,
  attachmentRetryingId = null,
  onUploadAttachment,
  onRetryAttachment,
  onRemoveAttachment,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const failedAttachments = draftAttachments.filter((item) => item.parse_status === 'failed');
  const attachmentCount = draftAttachments.length;
  const attachmentLimitReached = attachmentCount >= ATTACHMENT_MAX_FILES;
  const uploadBlocked = streaming || disabled || attachmentUploadPending;
  const showDepartmentInKbOptions = new Set(
    availableKbs.map((item) => item.department_name).filter(Boolean),
  ).size > 1;
  const scopeDisabled = disabled || streaming;

  // 自适应高度
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSend();
  };

  return (
    <div className={CHAT_INPUT_SHELL_CLASS}>
      <div className={CHAT_COMPOSER_STAGE_CLASS}>
        <form className={CHAT_COMPOSER_FORM_CLASS} onSubmit={handleSubmit}>
          <div className="mb-[8px] flex flex-wrap items-center gap-x-[8px] gap-y-[6px]">
            <span className="sr-only">检索范围 · 会话锚定库</span>
            <Select
              value={kbName || '__none__'}
              onValueChange={(value) => onKbChange(value === '__none__' ? '' : value)}
              disabled={scopeDisabled}
            >
              <SelectTrigger
                aria-label="选择会话锚定库"
                title={kbName ? `当前会话锚定库：${kbName}` : '选择会话锚定库'}
                className={cn(
                  CHAT_COMPOSER_MODEL_BTN_CLASS,
                  'min-w-[190px] max-w-[min(340px,calc(100vw-48px))]',
                )}
              >
                <span className="grid size-[14px] shrink-0 place-items-center" aria-hidden>
                  <AppIcon name="database" size={12} />
                </span>
                {kbName ? (
                  <SelectValue className="min-w-0 truncate" />
                ) : (
                  <span className="min-w-0 truncate">选择会话锚定库</span>
                )}
              </SelectTrigger>
              <SelectContent
                position="popper"
                align="start"
                sideOffset={6}
                className="min-w-[240px] max-w-[min(560px,calc(100vw-48px))]"
              >
                <SelectItem value="__none__">不使用知识库（仅附件/普通对话）</SelectItem>
                {availableKbs.map((kb) => {
                  const label = kbOptionLabel(kb, showDepartmentInKbOptions);
                  return (
                    <SelectItem key={kb.kb_id ?? `${kb.department_id ?? 'none'}:${kb.name}`} value={kb.name}>
                      <span className="block max-w-[480px] truncate" title={label}>{label}</span>
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-[8px]">
            {attachmentsEnabled && attachmentCount > 0 && (
              <div className={CHAT_COMPOSER_ATTACHMENTS_CLASS} aria-label="会话附件">
                {draftAttachments.map((attachment) => {
                  return (
                    <div
                      key={attachment.attachment_id}
                      className={cn(
                        CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
                        attachment.parse_status === 'failed' && CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
                      )}
                      title={attachment.error_message || attachment.filename}
                    >
                      <span className={CHAT_COMPOSER_ATTACHMENT_NAME_CLASS}>{attachment.filename}</span>
                      <span className={CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS}>
                        {attachmentStateLabel(attachment)}
                      </span>
                      {attachment.parse_status === 'failed' && onRetryAttachment && (
                        <button
                          type="button"
                          onClick={() => void onRetryAttachment(attachment.attachment_id)}
                          disabled={streaming || disabled || attachmentRetryingId !== null}
                          aria-label={`重试解析附件 ${attachment.filename}`}
                          title="重试解析"
                          className="shrink-0 border-0 bg-transparent p-0 text-[11px] text-[#1d4ed8] hover:underline disabled:cursor-not-allowed disabled:opacity-45"
                        >
                          {attachmentRetryingId === attachment.attachment_id ? '重试中…' : '重试'}
                        </button>
                      )}
                      {onRemoveAttachment && (
                        <button
                          type="button"
                          onClick={() => void onRemoveAttachment(attachment.attachment_id)}
                          disabled={streaming || disabled}
                          aria-label={`移除附件 ${attachment.filename}`}
                          title="移除附件"
                          className={CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS}
                        >
                          ×
                        </button>
                      )}
                    </div>
                  );
                })}
                <span className="shrink-0 text-[10px] text-[#9aa1b1]">
                  {attachmentCount}/{ATTACHMENT_MAX_FILES}
                </span>
              </div>
            )}
          </div>
          <div className="flex min-w-0 items-end gap-[8px]">

            <input
              ref={attachmentInputRef}
              id="chat-attachment-upload"
              type="file"
              aria-label="上传附件"
              accept={CHAT_ATTACHMENT_ACCEPT}
              className="sr-only"
              disabled={uploadBlocked}
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                event.currentTarget.value = '';
                if (file && onUploadAttachment) void onUploadAttachment(file);
              }}
            />
            <textarea
              ref={textareaRef}
              aria-label="聊天输入"
              aria-describedby="chat-composer-hint"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={kbName ? `向「${kbName}」提问,Enter 发送 / Shift+Enter 换行` : '未选择知识库，直接提问'}
              className={cn(CHAT_COMPOSER_TEXTAREA_CLASS, 'flex-1')}
              rows={2}
              disabled={streaming || disabled}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  onSend();
                }
              }}
            />
            {streaming ? (
              <button
                type="button"
                onClick={onStop}
                aria-label="停止生成"
                title="停止生成"
                className={cn(CHAT_COMPOSER_SEND_BTN_CLASS, CHAT_COMPOSER_STOP_BTN_CLASS)}
              >
                <AppIcon name="stop" size={18} />
              </button>
            ) : (
              <button
                type="submit"
                aria-label="发送"
                title="发送"
                disabled={!input.trim() || disabled}
                className={CHAT_COMPOSER_SEND_BTN_CLASS}
              >
                <AppIcon name="send" size={18} />
              </button>
            )}
          </div>
          {failedAttachments.length > 0 && (
            <div
              className="flex min-w-0 flex-wrap items-center gap-[6px] rounded-[10px] border border-[#f2c7c7] bg-[#fdf3f3] px-[9px] py-[6px] text-[11px] text-[#b42318]"
              role="status"
            >
              {failedAttachments.map((attachment) => (
                <span key={attachment.attachment_id} className="inline-flex min-w-0 items-center gap-[6px]">
                  <AppIcon name="warning" size={13} className="shrink-0" />
                  <span className="min-w-0 max-w-[260px] truncate" title={`${attachment.filename}：${attachment.error_message || '解析失败'}`}>
                    {attachment.filename}
                    {attachment.error_message ? ` · ${attachment.error_message}` : ''}
                  </span>
                  {onRetryAttachment && (
                    <button
                      type="button"
                      onClick={() => void onRetryAttachment(attachment.attachment_id)}
                      disabled={streaming || disabled || attachmentRetryingId !== null}
                      className="shrink-0 border-0 bg-transparent p-0 text-[11px] text-[#b42318] underline disabled:cursor-not-allowed disabled:opacity-45"
                    >
                      {attachmentRetryingId === attachment.attachment_id ? '重试中…' : '重试解析'}
                    </button>
                  )}
                </span>
              ))}
            </div>
          )}
          <div className={CHAT_COMPOSER_TOOLBAR_CLASS}>
            <div className="flex min-w-0 items-center gap-[2px]">
              {attachmentsEnabled && (
                <TooltipProvider delayDuration={200}>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        aria-label="添加附件"
                        title={
                          attachmentLimitReached
                            ? `单会话最多 ${ATTACHMENT_MAX_FILES} 个附件，请先移除部分附件`
                            : attachmentUploadPending
                              ? '上传中…'
                              : '添加附件'
                        }
                        disabled={uploadBlocked || attachmentLimitReached}
                        onClick={() => attachmentInputRef.current?.click()}
                        className={CHAT_COMPOSER_PLUS_BTN_CLASS}
                      >
                        <AppIcon name={attachmentUploadPending ? 'refresh' : 'plus'} size={16} />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent side="top">添加附件</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
              )}
              <span id="chat-composer-hint" className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 挂载知识库下拉框固定在输入框左上角(文本域上方)。
 * 附件入口收敛为输入框左侧的"+"按钮:悬停提示"添加附件",菜单内上传/重试/移除/作为模板;
 * 检索范围固定自动(由后端按用户意图路由),不再在输入区展示;发送后的附件只出现在消息气泡中。
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';

import type { AttachmentView, DocumentContext } from '@/api/types';
import { CHAT_ATTACHMENT_ACCEPT } from '@/api/chatAttachments';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import type { KbView } from '@/api/types';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { isDocumentContextExpired } from '../useKbChat';
import {
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
  quickReplies?: string[];
  /** The bridge is opt-in; false keeps the legacy composer unchanged. */
  documentAuthoringEnabled?: boolean;
  canUploadDocumentTemplate?: boolean;
  documentContext?: DocumentContext | null;
  documentContextLabel?: string;
  documentUploadPending?: boolean;
  onClearDocumentContext?: () => void;
  /** 会话附件(默认开):后端 CHAT_ATTACHMENTS_ENABLED 打开时由 ChatPage 传入。 */
  attachmentsEnabled?: boolean;
  draftAttachments?: AttachmentView[];
  attachmentUploadPending?: boolean;
  attachmentRetryingId?: string | null;
  onUploadAttachment?: (file: File) => void | Promise<void>;
  onRetryAttachment?: (attachmentId: string) => void | Promise<void>;
  onUseAsTemplate?: (attachment: AttachmentView) => void | Promise<void>;
  onRemoveAttachment?: (attachmentId: string) => void;
};

const TEMPLATE_ATTACHMENT_EXTENSIONS = new Set(['.docx', '.xlsx', '.xlsm']);

function kbOptionLabel(kb: KbView): string {
  return kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
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
  quickReplies = [],
  documentAuthoringEnabled = false,
  canUploadDocumentTemplate = false,
  documentContext = null,
  documentContextLabel,
  documentUploadPending = false,
  onClearDocumentContext,
  // Attachment support is on by default; ChatPage passes the explicit
  // environment-controlled value when an installation opts out.
  attachmentsEnabled = true,
  draftAttachments = [],
  attachmentUploadPending = false,
  attachmentRetryingId = null,
  onUploadAttachment,
  onRetryAttachment,
  onUseAsTemplate,
  onRemoveAttachment,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const [attachmentMenuOpen, setAttachmentMenuOpen] = useState(false);
  const contextExpired = isDocumentContextExpired(documentContext);
  const contextReference = documentContext
    ? documentContextLabel
      || documentContext.template_version_id
      || documentContext.generation_session_id
      || (documentContext.version === 'v2' && documentContext.task_id
        ? documentContext.task_id
        : undefined)
      || (documentContext.version === 'v2' && documentContext.attachment_ids?.length
        ? '会话附件'
        : '文档会话')
    : '';
  const showDocumentControls = documentAuthoringEnabled && documentContext;
  const failedAttachments = draftAttachments.filter((item) => item.parse_status === 'failed');
  const uploadBlocked = streaming || disabled || attachmentUploadPending;

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
          <div className="flex min-w-0 items-center gap-[8px]">
            <Select
              value={kbName || '__none__'}
              onValueChange={(value) => onKbChange(value === '__none__' ? '' : value)}
              disabled={disabled}
            >
              <SelectTrigger
                aria-label="挂载知识库"
                title="挂载知识库"
                className={cn(CHAT_COMPOSER_MODEL_BTN_CLASS)}
              >
                <span className="grid size-[14px] shrink-0 place-items-center" aria-hidden>
                  <AppIcon name="database" size={14} />
                </span>
                <SelectValue className="min-w-0 truncate" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">不挂载</SelectItem>
                {availableKbs.map((kb) => (
                  <SelectItem key={kb.kb_id ?? `${kb.department_id ?? 'none'}:${kb.name}`} value={kb.name}>
                    {kbOptionLabel(kb)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {showDocumentControls && (
            <div
              className="flex min-w-0 items-center gap-[8px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[6px] text-[12px] text-[#464c5e]"
              aria-label="当前文档模板引用"
            >
              <AppIcon name="file" size={16} className="shrink-0 text-[#68728a]" />
              <span className="min-w-0 flex-1 truncate" title={contextReference}>
                {documentContext.template_version_id ? '模板引用' : '文档范围'}：{contextReference}
              </span>
              <span className={cn('shrink-0 text-[11px]', contextExpired ? 'text-[#b45309]' : 'text-[#858b9c')}>
                {contextExpired ? '已过期，仅可读取历史状态' : '已附加'}
              </span>
              {onClearDocumentContext && (
                <button
                  type="button"
                  onClick={onClearDocumentContext}
                  aria-label="清除文档模板引用"
                  title="清除文档模板引用"
                  className="inline-grid size-[20px] shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 text-[15px] leading-none text-[#a2a8b8] hover:text-[#18181a]"
                >
                  ×
                </button>
              )}
            </div>
          )}
          {quickReplies.length > 0 && !streaming && (
            <div className="flex flex-wrap gap-[6px]" aria-label="文档快捷回复">
              {quickReplies.slice(0, 6).map((reply) => (
                <button
                  key={reply}
                  type="button"
                  onClick={() => {
                    setInput(reply);
                    textareaRef.current?.focus();
                  }}
                  disabled={disabled}
                  className="rounded-full border border-[#c8d8f5] bg-[#f4f7ff] px-[9px] py-[3px] text-[11px] text-[#315da8] hover:bg-[#eaf1ff] disabled:opacity-45"
                >
                  {reply}
                </button>
              ))}
            </div>
          )}
          <div className="flex min-w-0 items-end gap-[8px]">
            {attachmentsEnabled && (
              <DropdownMenu open={attachmentMenuOpen} onOpenChange={setAttachmentMenuOpen}>
                <TooltipProvider delayDuration={200}>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          aria-label="添加附件"
                          title="添加附件"
                          disabled={uploadBlocked}
                          className={CHAT_COMPOSER_PLUS_BTN_CLASS}
                        >
                          <AppIcon name="plus" size={18} />
                        </button>
                      </DropdownMenuTrigger>
                    </TooltipTrigger>
                    <TooltipContent side="top">添加附件</TooltipContent>
                  </Tooltip>
                </TooltipProvider>
                <DropdownMenuContent align="start" side="top" className="w-[300px]">
                  <DropdownMenuItem
                    className="gap-[8px]"
                    onSelect={(event) => {
                      event.preventDefault();
                      attachmentInputRef.current?.click();
                    }}
                    disabled={uploadBlocked}
                  >
                    <AppIcon name="file" size={14} />
                    {attachmentUploadPending ? '上传中…' : '上传新附件'}
                  </DropdownMenuItem>
                  {draftAttachments.length > 0 && <DropdownMenuSeparator />}
                  {draftAttachments.length > 0 && (
                    <DropdownMenuLabel className="text-[11px] text-[#858b9c]">
                      会话附件（发送时自动挂载）
                    </DropdownMenuLabel>
                  )}
                  {draftAttachments.map((attachment) => {
                    const eligibleTemplate =
                      documentAuthoringEnabled &&
                      canUploadDocumentTemplate &&
                      onUseAsTemplate &&
                      TEMPLATE_ATTACHMENT_EXTENSIONS.has(attachment.extension.toLowerCase()) &&
                      (attachment.parse_status === 'ready' || attachment.parse_status === 'degraded');
                    return (
                      <div
                        key={attachment.attachment_id}
                        className="flex min-w-0 items-center gap-[6px] rounded-[8px] px-[8px] py-[6px] text-[12px] text-[#464c5e] hover:bg-[#f6f6f6]"
                        aria-label={`附件 ${attachment.filename}`}
                      >
                        <AppIcon name="file" size={14} className="shrink-0 text-[#68728a]" />
                        <span className="min-w-0 flex-1 truncate" title={attachment.filename}>
                          {attachment.filename}
                        </span>
                        <span
                          className={cn(
                            'shrink-0 text-[11px]',
                            attachment.parse_status === 'failed'
                              ? 'text-[#d20b0b]'
                              : attachment.parse_status === 'ready'
                                ? 'text-[#166534]'
                                : 'text-[#858b9c]',
                          )}
                        >
                          {attachmentStateLabel(attachment)}
                        </span>
                        {attachment.parse_status === 'failed' && onRetryAttachment && (
                          <button
                            type="button"
                            onClick={() => void onRetryAttachment(attachment.attachment_id)}
                            disabled={
                              streaming ||
                              disabled ||
                              (attachmentRetryingId !== null && attachmentRetryingId !== attachment.attachment_id)
                            }
                            aria-label={`重试解析附件 ${attachment.filename}`}
                            title="重试解析"
                            className="shrink-0 border-0 bg-transparent p-0 text-[11px] text-[#1d4ed8] hover:underline disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            {attachmentRetryingId === attachment.attachment_id ? '重试中…' : '重试解析'}
                          </button>
                        )}
                        {eligibleTemplate && (
                          <button
                            type="button"
                            onClick={() => void onUseAsTemplate(attachment)}
                            disabled={streaming || disabled || documentUploadPending}
                            aria-label={`将附件 ${attachment.filename} 作为模板`}
                            title="将附件转换为模板"
                            className="shrink-0 border-0 bg-transparent p-0 text-[11px] text-[#1d4ed8] hover:underline disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            作为模板
                          </button>
                        )}
                        {onRemoveAttachment && (
                          <button
                            type="button"
                            onClick={() => void onRemoveAttachment(attachment.attachment_id)}
                            disabled={streaming || disabled}
                            aria-label={`移除附件 ${attachment.filename}`}
                            title="移除附件"
                            className="inline-grid size-[18px] shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 text-[14px] leading-none text-[#a2a8b8] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-45"
                          >
                            ×
                          </button>
                        )}
                      </div>
                    );
                  })}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <input
              ref={attachmentInputRef}
              id="chat-attachment-upload"
              type="file"
              accept={CHAT_ATTACHMENT_ACCEPT}
              className="sr-only"
              disabled={uploadBlocked}
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                event.currentTarget.value = '';
                setAttachmentMenuOpen(false);
                if (file && onUploadAttachment) void onUploadAttachment(file);
              }}
            />
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder={kbName ? `向「${kbName}」提问,Enter 发送 / Shift+Enter 换行` : '未挂载知识库,直接提问'}
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
            <span className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
            {documentUploadPending && (
              <span className="shrink-0 text-[11px] text-[#68728a]" role="status" aria-live="polite">
                模板分析中…（结构分析 + 分批 LLM 语义建议，大文档可能数分钟）
              </span>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}

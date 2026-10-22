/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 检索范围下拉框固定在输入框左上角(文本域上方)。
 * 附件入口收敛为输入框左侧的"+"按钮:悬停提示"添加附件",菜单内上传/重试/移除/作为模板;
 * 检索范围统一在输入区设置:先选主知识库,再按需添加参考库和用途;发送后的附件只出现在消息气泡中。
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Popover } from 'radix-ui';

import type { AttachmentView, DocumentContext, KnowledgePurpose, KbView } from '@/api/types';
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
} from '@/components/ui/dropdown-menu';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { isDocumentContextExpired } from '../useKbChat';
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
  knowledgePurpose?: KnowledgePurpose | 'auto';
  selectedKnowledgeBaseIds?: number[];
  onKnowledgeScopeChange?: (purpose: KnowledgePurpose | 'auto', kbIds: number[]) => void;
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
  knowledgePurpose = 'auto',
  selectedKnowledgeBaseIds = [],
  onKnowledgeScopeChange,
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
  const attachmentCount = draftAttachments.length;
  const attachmentLimitReached = attachmentCount >= ATTACHMENT_MAX_FILES;
  const uploadBlocked = streaming || disabled || attachmentUploadPending;
  const showDepartmentInKbOptions = new Set(
    availableKbs.map((item) => item.department_name).filter(Boolean),
  ).size > 1;
  const selectableKbs = availableKbs.filter((kb) => kb.permission && kb.kb_id != null);
  const primaryKb = availableKbs.find((kb) => kb.name === kbName) ?? null;
  const primaryKbId = primaryKb?.kb_id != null ? Number(primaryKb.kb_id) : null;
  const effectiveSelectedIds = primaryKbId != null && !selectedKnowledgeBaseIds.includes(primaryKbId)
    ? [primaryKbId, ...selectedKnowledgeBaseIds]
    : selectedKnowledgeBaseIds;
  const selectedKbs = selectableKbs.filter((kb) => effectiveSelectedIds.includes(Number(kb.kb_id)));
  const referenceKbs = selectedKbs.filter((kb) => Number(kb.kb_id) !== primaryKbId);
  const referenceCandidates = selectableKbs.filter((kb) => Number(kb.kb_id) !== primaryKbId);
  const scopeDisabled = disabled || streaming;
  const showReferencePicker = Boolean(onKnowledgeScopeChange);
  const referencePickerDisabled = scopeDisabled
    || primaryKbId == null
    || referenceCandidates.length === 0;

  const updateReferences = (kbId: number, checked: boolean) => {
    if (primaryKbId == null) return;
    const referenceIds = effectiveSelectedIds.filter((value) => value !== primaryKbId);
    const nextReferenceIds = checked
      ? [...referenceIds, kbId]
      : referenceIds.filter((value) => value !== kbId);
    onKnowledgeScopeChange?.(knowledgePurpose, [primaryKbId, ...new Set(nextReferenceIds)]);
  };

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
          <div className="mb-[6px] flex flex-wrap items-center gap-x-[12px] gap-y-[6px]">
            <span className="text-[11px] font-medium text-[#5b6378]">检索范围</span>
            <div className="flex items-center gap-[6px]">
              <span className="text-[11px] text-[#858b9c]">主知识库</span>
              <Select
                value={kbName || '__none__'}
                onValueChange={(value) => onKbChange(value === '__none__' ? '' : value)}
                disabled={scopeDisabled}
              >
                <SelectTrigger
                  aria-label="选择主知识库"
                  title={kbName ? `当前主知识库：${kbName}` : '选择主知识库'}
                  className={cn(
                    CHAT_COMPOSER_MODEL_BTN_CLASS,
                    'min-w-[220px] max-w-[min(360px,calc(100vw-48px))]',
                  )}
                >
                  <span className="grid size-[14px] shrink-0 place-items-center" aria-hidden>
                    <AppIcon name="database" size={14} />
                  </span>
                  {kbName ? (
                    <SelectValue className="min-w-0 truncate" />
                  ) : (
                    <span className="min-w-0 truncate">选择主知识库</span>
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
            {showReferencePicker && (
              <div className="flex min-w-0 flex-wrap items-center gap-[6px]">
                <span className="text-[11px] text-[#858b9c]">参考知识库</span>
                <Popover.Root>
                  <Popover.Trigger asChild>
                    <button
                      type="button"
                      aria-label="添加参考知识库"
                      title={
                        primaryKbId == null
                          ? '请先选择主知识库'
                          : referenceCandidates.length === 0
                            ? '暂无其他可用知识库'
                            : referenceKbs.length > 0
                              ? `已添加 ${referenceKbs.length} 个参考知识库，点击继续添加`
                              : '添加参考知识库'
                      }
                      disabled={referencePickerDisabled}
                      className={cn(
                        CHAT_COMPOSER_MODEL_BTN_CLASS,
                        'min-w-[132px] max-w-[200px] px-[10px] text-[11px]',
                      )}
                    >
                      <span className="text-[14px] leading-none" aria-hidden>+</span>
                      <span className="min-w-0 truncate">
                        {referenceKbs.length > 0 ? `参考知识库 ${referenceKbs.length}` : '添加参考知识库'}
                      </span>
                    </button>
                  </Popover.Trigger>
                  <Popover.Portal>
                    <Popover.Content
                      aria-label="选择参考知识库"
                      align="start"
                      sideOffset={6}
                      collisionPadding={12}
                      className="z-50 w-[320px] max-w-[calc(100vw-24px)] rounded-[10px] border border-[#e5e8f0] bg-white p-[10px] shadow-lg"
                    >
                      <div className="mb-[6px] flex items-center justify-between gap-[8px]">
                        <span className="text-[12px] font-medium text-[#18181a]">添加参考知识库</span>
                        <span className="text-[10px] text-[#8a92a5]">已选 {referenceKbs.length}</span>
                      </div>
                      <p className="mb-[6px] text-[10px] leading-4 text-[#8a92a5]">
                        可多选；主知识库不会重复添加。
                      </p>
                      {referenceCandidates.length > 0 ? (
                        <div className="grid max-h-[220px] gap-[2px] overflow-y-auto">
                          {referenceCandidates.map((kb) => {
                            const id = Number(kb.kb_id);
                            const checked = referenceKbs.some((item) => Number(item.kb_id) === id);
                            const label = kbOptionLabel(kb, showDepartmentInKbOptions);
                            return (
                              <label
                                key={id}
                                className="flex cursor-pointer items-center gap-[8px] rounded-[6px] px-[8px] py-[6px] text-[11px] text-[#464c5e] hover:bg-[#f6f7fa]"
                              >
                                <input
                                  type="checkbox"
                                  aria-label={`选择参考知识库 ${kb.name}`}
                                  checked={checked}
                                  disabled={scopeDisabled}
                                  onChange={(event) => updateReferences(id, event.target.checked)}
                                  className="size-[13px] accent-[#18181a]"
                                />
                                <span className="min-w-0 flex-1 truncate" title={label}>{label}</span>
                              </label>
                            );
                          })}
                        </div>
                      ) : (
                        <p className="rounded-[6px] border border-dashed border-[#e5e8f0] px-[8px] py-[7px] text-[11px] text-[#a2a8b8]">
                          暂无其他可用知识库
                        </p>
                      )}
                    </Popover.Content>
                  </Popover.Portal>
                </Popover.Root>
              </div>
            )}
            <label className="flex items-center gap-[6px] text-[11px] text-[#858b9c]">
              用途
              <select
                aria-label="检索用途"
                value={knowledgePurpose}
                disabled={scopeDisabled}
                onChange={(event) => onKnowledgeScopeChange?.(
                  event.target.value as KnowledgePurpose | 'auto',
                  effectiveSelectedIds,
                )}
                className="rounded border border-[#e5e8f0] px-[5px] py-[3px] text-[11px] text-[#464c5e]"
              >
                <option value="auto">自动（推荐）</option>
                <option value="answer">问答</option>
                <option value="compare">对比</option>
                <option value="compose">编写文档</option>
              </select>
            </label>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-[8px]">
            {attachmentsEnabled && attachmentCount > 0 && (
              <div className={CHAT_COMPOSER_ATTACHMENTS_CLASS} aria-label="会话附件">
                {draftAttachments.map((attachment) => {
                  const eligibleTemplate =
                    documentAuthoringEnabled &&
                    canUploadDocumentTemplate &&
                    Boolean(onUseAsTemplate) &&
                    TEMPLATE_ATTACHMENT_EXTENSIONS.has(attachment.extension.toLowerCase()) &&
                    (attachment.parse_status === 'ready' || attachment.parse_status === 'degraded');
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
                      {eligibleTemplate && (
                        <button
                          type="button"
                          onClick={() => void onUseAsTemplate?.(attachment)}
                          disabled={streaming || disabled || documentUploadPending}
                          aria-label={`将附件 ${attachment.filename} 作为模板`}
                          title="将附件转换为文档模板"
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
                if (file && onUploadAttachment) void onUploadAttachment(file);
              }}
            />
            <textarea
              ref={textareaRef}
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
              <span className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
            </div>
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

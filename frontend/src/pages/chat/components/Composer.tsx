/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 挂载知识库下拉框固定在输入框左上角(文本域上方)。
 */
import { useEffect, useRef, type FormEvent } from 'react';

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
import { isDocumentContextExpired } from '../useKbChat';
import {
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_HINT_CLASS,
  CHAT_COMPOSER_MODEL_BTN_CLASS,
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
  /** The bridge is opt-in; false keeps the legacy composer unchanged. */
  documentAuthoringEnabled?: boolean;
  canUploadDocumentTemplate?: boolean;
  documentContext?: DocumentContext | null;
  documentContextLabel?: string;
  /** 本次对话是否驱动文档生成(默认开);仅当模板上下文已附加时渲染。 */
  documentFlowEnabled?: boolean;
  onToggleDocumentFlow?: (enabled: boolean) => void;
  documentUploadPending?: boolean;
  documentUploadProgress?: number;
  onUploadTemplate?: (file: File) => void | Promise<void>;
  onClearDocumentContext?: () => void;
  /** 会话附件(默认开):后端 CHAT_ATTACHMENTS_ENABLED 打开时由 ChatPage 传入。 */
  attachmentsEnabled?: boolean;
  draftAttachments?: AttachmentView[];
  selectedAttachmentIds?: string[];
  attachmentUploadPending?: boolean;
  attachmentRetryingId?: string | null;
  sourceScope?: 'auto' | 'attachment_only' | 'knowledge_base_only' | 'attachment_and_knowledge_base';
  onSourceScopeChange?: (scope: 'auto' | 'attachment_only' | 'knowledge_base_only' | 'attachment_and_knowledge_base') => void;
  onUploadAttachment?: (file: File) => void | Promise<void>;
  onToggleAttachment?: (attachmentId: string) => void;
  onRetryAttachment?: (attachmentId: string) => void | Promise<void>;
  onUseAsTemplate?: (attachment: AttachmentView) => void | Promise<void>;
  onRemoveAttachment?: (attachmentId: string) => void;
};

const TEMPLATE_ATTACHMENT_EXTENSIONS = new Set(['.docx', '.xlsx', '.xlsm']);

function kbOptionLabel(kb: KbView): string {
  return kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
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
  documentAuthoringEnabled = false,
  canUploadDocumentTemplate = false,
  documentContext = null,
  documentContextLabel,
  documentFlowEnabled = true,
  onToggleDocumentFlow,
  documentUploadPending = false,
  documentUploadProgress = 0,
  onUploadTemplate,
  onClearDocumentContext,
  // Attachment support is on by default; ChatPage passes the explicit
  // environment-controlled value when an installation opts out.
  attachmentsEnabled = true,
  draftAttachments = [],
  selectedAttachmentIds,
  attachmentUploadPending = false,
  attachmentRetryingId = null,
  sourceScope = 'auto',
  onSourceScopeChange,
  onUploadAttachment,
  onToggleAttachment,
  onRetryAttachment,
  onUseAsTemplate,
  onRemoveAttachment,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const templateInputRef = useRef<HTMLInputElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const selectedIds = selectedAttachmentIds ?? draftAttachments.map((attachment) => attachment.attachment_id);
  const contextExpired = isDocumentContextExpired(documentContext);
  const showDocumentControls = documentAuthoringEnabled && documentContext;

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
              <span className="min-w-0 flex-1 truncate" title={documentContextLabel || documentContext.template_version_id}>
                模板引用：{documentContextLabel || documentContext.template_version_id}
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
          {showDocumentControls && onToggleDocumentFlow && (
            <label
              title="开启后本次对话将驱动文档生成流程；关闭后本次回答不会进入文档流程"
              className="flex w-fit cursor-pointer select-none items-center gap-[7px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[6px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4]"
            >
              <input
                id="chat-document-flow-toggle"
                type="checkbox"
                checked={documentFlowEnabled}
                disabled={streaming || disabled}
                onChange={(event) => onToggleDocumentFlow(event.currentTarget.checked)}
                aria-label="文档生成模式"
              />
              <span>文档生成模式</span>
              <span
                className={cn(
                  'shrink-0 rounded-full px-[7px] py-[1px] text-[10px] font-semibold',
                  documentFlowEnabled ? 'bg-[#eef8f0] text-[#166534]' : 'bg-[#eef0f4] text-[#858b9c]',
                )}
              >
                {documentFlowEnabled ? '开' : '关'}
              </span>
            </label>
          )}
          {attachmentsEnabled && (
            <div className="flex min-w-0 flex-col gap-[6px]">
              {draftAttachments.length > 0 && (
                <div className="flex min-w-0 flex-wrap items-center gap-[6px]">
                  {draftAttachments.map((attachment) => {
                    const selected = selectedIds.includes(attachment.attachment_id);
                    const stateLabel =
                      attachment.parse_status === 'ready'
                        ? '已就绪'
                        : attachment.parse_status === 'degraded'
                          ? '部分可读'
                          : attachment.parse_status === 'failed'
                            ? '解析失败'
                            : '解析中…';
                    return (
                      <span
                        key={attachment.attachment_id}
                        className="flex min-w-0 items-center gap-[8px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[6px] text-[12px] text-[#464c5e]"
                        aria-label={`附件 ${attachment.filename}`}
                      >
                        <AppIcon name="file" size={14} className="shrink-0 text-[#68728a]" />
                        <span className="min-w-0 max-w-[220px] truncate" title={attachment.filename}>
                          {attachment.filename}
                        </span>
                        {onToggleAttachment && (
                          <button
                            type="button"
                            onClick={() => onToggleAttachment(attachment.attachment_id)}
                            aria-pressed={selected}
                            aria-label={`${selected ? '取消选择' : '选择'}附件 ${attachment.filename}`}
                            className={cn(
                              'shrink-0 rounded-[6px] border px-[5px] py-[1px] text-[10px] transition-colors',
                              selected
                                ? 'border-[#c8d8f5] bg-[#eef4ff] text-[#1d4ed8]'
                                : 'border-[#e3e7f1] bg-white text-[#858b9c]',
                            )}
                          >
                            {selected ? '已选' : '未选'}
                          </button>
                        )}
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
                          {stateLabel}
                        </span>
                        {attachment.parse_status === 'failed' && attachment.error_message && (
                          <span
                            className="min-w-0 max-w-[240px] truncate text-[11px] text-[#b42318]"
                            title={attachment.error_message}
                          >
                            · {attachment.error_message}
                          </span>
                        )}
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
                        {documentAuthoringEnabled &&
                          canUploadDocumentTemplate &&
                          onUseAsTemplate &&
                          TEMPLATE_ATTACHMENT_EXTENSIONS.has(attachment.extension.toLowerCase()) &&
                          (attachment.parse_status === 'ready' || attachment.parse_status === 'degraded') && (
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
                            onClick={() => onRemoveAttachment(attachment.attachment_id)}
                            aria-label={`移除附件 ${attachment.filename}`}
                            className="inline-grid size-[20px] shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 text-[15px] leading-none text-[#a2a8b8] hover:text-[#18181a]"
                          >
                            ×
                          </button>
                        )}
                      </span>
                    );
                  })}
                </div>
              )}
              <div className="flex min-w-0 items-center gap-[8px]">
                <input
                  ref={attachmentInputRef}
                  id="chat-attachment-upload"
                  type="file"
                  accept={CHAT_ATTACHMENT_ACCEPT}
                  className="sr-only"
                  disabled={streaming || disabled || attachmentUploadPending}
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    event.currentTarget.value = '';
                    if (file && onUploadAttachment) void onUploadAttachment(file);
                  }}
                />
                <label
                  htmlFor="chat-attachment-upload"
                  aria-disabled={streaming || disabled || attachmentUploadPending}
                  title="添加会话附件"
                  className={cn(
                    'inline-flex h-[30px] shrink-0 cursor-pointer items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[9px] text-[12px] text-[#68728a] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]',
                    (streaming || disabled || attachmentUploadPending) &&
                      'pointer-events-none cursor-not-allowed opacity-45',
                  )}
                >
                  <AppIcon name="file" size={14} />
                  {attachmentUploadPending ? '上传中…' : '添加附件'}
                </label>
                {selectedIds.length > 0 && onSourceScopeChange && (
                  <label className="flex min-w-0 items-center gap-[6px] text-[11px] text-[#68728a]">
                    检索范围
                    <select
                      value={sourceScope}
                      onChange={(event) =>
                        onSourceScopeChange(
                          event.currentTarget.value as 'auto' | 'attachment_only' | 'knowledge_base_only' | 'attachment_and_knowledge_base',
                        )
                      }
                      disabled={streaming || disabled}
                      className="h-[26px] rounded-[8px] border border-[#e3e7f1] bg-white px-[6px] text-[11px] text-[#464c5e]"
                    >
                      <option value="auto">自动</option>
                      <option value="attachment_only">仅附件</option>
                      <option value="knowledge_base_only">仅知识库</option>
                      <option value="attachment_and_knowledge_base">附件 + 知识库</option>
                    </select>
                  </label>
                )}
              </div>
            </div>
          )}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={kbName ? `向「${kbName}」提问,Enter 发送 / Shift+Enter 换行` : '未挂载知识库,直接提问'}
            className={CHAT_COMPOSER_TEXTAREA_CLASS}
            rows={2}
            disabled={streaming || disabled}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                onSend();
              }
            }}
          />
          <div className={CHAT_COMPOSER_TOOLBAR_CLASS}>
            {documentAuthoringEnabled ? (
              <div className="flex min-w-0 items-center gap-[8px]">
                <input
                  ref={templateInputRef}
                  id="chat-document-template-upload"
                  type="file"
                  accept=".xlsx,.xlsm,.docx"
                  className="sr-only"
                  disabled={streaming || disabled || documentUploadPending || !canUploadDocumentTemplate}
                  onChange={(event) => {
                    const file = event.currentTarget.files?.[0];
                    event.currentTarget.value = '';
                    if (file && onUploadTemplate) void onUploadTemplate(file);
                  }}
                />
                <label
                  htmlFor="chat-document-template-upload"
                  aria-disabled={streaming || disabled || documentUploadPending || !canUploadDocumentTemplate}
                  title={canUploadDocumentTemplate ? '上传并分析文档模板' : '需要该知识库的写权限才能上传模板'}
                  className={cn(
                    'inline-flex h-[30px] shrink-0 cursor-pointer items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[9px] text-[12px] text-[#68728a] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]',
                    (streaming || disabled || documentUploadPending || !canUploadDocumentTemplate) &&
                      'pointer-events-none cursor-not-allowed opacity-45',
                  )}
                >
                  <AppIcon name="file" size={14} />
                  上传模板
                </label>
                {documentUploadPending && (
                  <span className="shrink-0 text-[11px] text-[#68728a]" role="status" aria-live="polite">
                    分析中 {Math.max(0, Math.min(100, documentUploadProgress))}%
                  </span>
                )}
                {!canUploadDocumentTemplate && !documentUploadPending && (
                  <span className="hidden text-[11px] text-[#b45309] md:inline">需 KB 写权限</span>
                )}
                <span className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
              </div>
            ) : (
              <span className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
            )}
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
        </form>
      </div>
    </div>
  );
}

/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 挂载知识库下拉框固定在输入框左上角(文本域上方)。
 */
import { useEffect, useRef, type FormEvent } from 'react';

import type { DocumentContext } from '@/api/types';
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
};

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
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const templateInputRef = useRef<HTMLInputElement>(null);
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

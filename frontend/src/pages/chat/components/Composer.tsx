/**
 * 输入区(对齐 Composer 结构,裁掉业务件)。
 * 保留:自适应高度 textarea、Enter 发送 / Shift+Enter 换行、发送按钮、流式中停止按钮、hint。
 * 挂载知识库下拉框固定在输入框左上角(文本域上方)。
 */
import { useEffect, useRef, type FormEvent } from 'react';

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
  availableKbs: KbView[];
  input: string;
  setInput: (value: string) => void;
  streaming: boolean;
  disabled?: boolean;
  onKbChange: (kbName: string) => void;
  onSend: () => void;
  onStop: () => void;
};

function kbOptionLabel(kb: KbView): string {
  return kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
}

export default function Composer({
  kbName,
  availableKbs,
  input,
  setInput,
  streaming,
  disabled = false,
  onKbChange,
  onSend,
  onStop,
}: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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
              disabled={streaming || disabled}
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
            <span className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</span>
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

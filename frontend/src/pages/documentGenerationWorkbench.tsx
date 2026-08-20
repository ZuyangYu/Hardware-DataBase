import type { ReactNode } from 'react';

import type { WorkOrderStatus } from '../api/types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import {
  DOCUMENT_PHASES,
  describeWorkOrderStatus,
  nextActionsForStatus,
  resolveDocumentPhase,
  type DocumentGenerationPhase,
  type DocumentStatusTone,
} from './documentGenerationModel';

export type ClarificationMessageView = {
  id: string;
  role: 'assistant' | 'user' | 'system';
  content: string;
  options?: string[];
};

type WorkbenchProps = {
  activePhase: DocumentGenerationPhase;
  children: ReactNode;
  inspector?: ReactNode;
};

const TONE_CLASSES: Record<DocumentStatusTone, string> = {
  neutral: 'border-border bg-muted/30 text-foreground',
  info: 'border-blue-200 bg-blue-50 text-blue-800',
  warning: 'border-amber-200 bg-amber-50 text-amber-900',
  success: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  danger: 'border-red-200 bg-red-50 text-red-800',
};

function PhaseRail({ activePhase }: { activePhase: DocumentGenerationPhase }) {
  const activeIndex = DOCUMENT_PHASES.findIndex((phase) => phase.key === activePhase);
  const activeDescription = describeWorkOrderStatus(activePhase);

  return (
    <aside className="rounded-xl border bg-card p-4 shadow-sm" aria-label="文档生成流程">
      <div className="mb-5">
        <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">生成流程</p>
        <p className="mt-2 text-sm font-semibold">{activeDescription.label}</p>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">{activeDescription.action}</p>
      </div>
      <ol className="space-y-1">
        {DOCUMENT_PHASES.map((phase, index) => {
          const active = phase.key === activePhase
            || (activePhase === 'ready_to_generate' && phase.key === 'needs_clarification');
          const completed = activeIndex >= 0 && index < activeIndex;
          return (
            <li
              key={phase.key}
              className={`flex items-center gap-3 rounded-lg px-2 py-2.5 text-sm ${
                active ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
              }`}
            >
              <span className={`grid h-6 w-6 place-items-center rounded-full border text-xs ${
                completed ? 'border-emerald-500 bg-emerald-500 text-white' : active ? 'border-white/60' : 'border-border'
              }`}>
                {completed ? '✓' : index + 1}
              </span>
              <span className={active ? 'font-medium' : ''}>{phase.label}</span>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}

export function DocumentGenerationWorkbench({ activePhase, children, inspector }: WorkbenchProps) {
  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-[220px_minmax(0,1fr)_300px]">
      <PhaseRail activePhase={activePhase} />
      <main className="min-w-0 space-y-4">{children}</main>
      <aside className="min-w-0 space-y-4 lg:block" aria-label="模板与证据摘要">
        {inspector ?? (
          <Card>
            <CardHeader><CardTitle className="text-base">任务摘要</CardTitle></CardHeader>
            <CardContent className="text-sm text-muted-foreground">选择模板后显示字段、来源和风险。</CardContent>
          </Card>
        )}
      </aside>
    </div>
  );
}

type ClarificationPanelProps = {
  messages: ClarificationMessageView[];
  reply: string;
  confirmedItems?: string[];
  pendingItems?: string[];
  readyToConfirm?: boolean;
  sending?: boolean;
  disabled?: boolean;
  onReplyChange?: (value: string) => void;
  onSend?: () => void;
  onSelectOption?: (value: string) => void;
  onConfirm?: () => void;
};

export function ClarificationPanel({
  messages,
  reply,
  confirmedItems = [],
  pendingItems = [],
  readyToConfirm = false,
  sending = false,
  disabled = false,
  onReplyChange,
  onSend,
  onSelectOption,
  onConfirm,
}: ClarificationPanelProps) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b bg-muted/20">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>AI 需求澄清</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">逐项确认后，系统才会冻结来源并开始生成。</p>
          </div>
          <Badge variant="outline">聊天式确认</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-5 p-4 sm:p-6">
        <div className="max-h-[420px] space-y-3 overflow-y-auto" aria-live="polite">
          {messages.map((message) => (
            <div
              key={message.id}
              className={`max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-6 ${
                message.role === 'user'
                  ? 'ml-auto bg-primary text-primary-foreground'
                  : 'border bg-muted/35 text-foreground'
              }`}
            >
              <p>{message.content}</p>
              {message.options && message.options.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-2">
                  {message.options.map((option) => (
                    <Button
                      key={option}
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={disabled || sending}
                      onClick={() => onSelectOption?.(option)}
                    >
                      {option}
                    </Button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        {(confirmedItems.length > 0 || pendingItems.length > 0) && (
          <div className="grid gap-3 rounded-xl border bg-muted/20 p-3 sm:grid-cols-2">
            <div>
              <p className="text-xs font-semibold text-emerald-700">已确认</p>
              <ul className="mt-2 space-y-1 text-sm">
                {confirmedItems.map((item) => <li key={item}>✓ {item}</li>)}
              </ul>
            </div>
            <div>
              <p className="text-xs font-semibold text-amber-700">仍待确认</p>
              <ul className="mt-2 space-y-1 text-sm">
                {pendingItems.map((item) => <li key={item}>· {item}</li>)}
              </ul>
            </div>
          </div>
        )}

        {readyToConfirm ? (
          <Button className="w-full" disabled={disabled || sending} onClick={onConfirm}>确认需求并开始生成</Button>
        ) : (
          <div className="space-y-2">
            <Textarea
              aria-label="回复 AI"
              placeholder="回复当前问题，或输入“修改上一项”……"
              value={reply}
              readOnly={disabled || !onReplyChange}
              onChange={(event) => onReplyChange?.(event.target.value)}
            />
            <div className="flex justify-end">
              <Button disabled={disabled || !reply.trim() || sending} onClick={onSend}>
                {sending ? '发送中…' : '发送'}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

type RunStatusPanelProps = {
  status: WorkOrderStatus;
  actionBusy?: boolean;
  onPause?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
  onDelete?: () => void;
};

export function RunStatusPanel({
  status,
  actionBusy = false,
  onPause,
  onResume,
  onCancel,
  onDelete,
}: RunStatusPanelProps) {
  const phase = resolveDocumentPhase(status);
  const description = describeWorkOrderStatus(phase);
  const actions = status.next_actions?.length ? status.next_actions : nextActionsForStatus(phase);
  const error = status.error_message || status.harness_run?.error;
  const progress = typeof status.progress === 'number' ? Math.max(0, Math.min(100, status.progress)) : null;

  return (
    <div className={`space-y-3 rounded-xl border p-4 ${TONE_CLASSES[description.tone]}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-semibold">{status.display_label || description.label}</p>
          <p className="mt-1 text-sm opacity-80">{description.action}</p>
        </div>
        <Badge variant="outline">{status.target_format ?? '文档'}</Badge>
      </div>
      {progress !== null && (
        <div className="h-2 overflow-hidden rounded-full bg-white/70">
          <div className="h-full rounded-full bg-current opacity-70" style={{ width: `${progress}%` }} />
        </div>
      )}
      {status.current_unit && <p className="text-sm">当前字段：{status.current_unit}</p>}
      {error && (
        <div className="rounded-lg border border-current/20 bg-white/55 p-3 text-sm">
          <p className="font-medium">{status.error_code ? `错误：${status.error_code}` : '任务未能继续'}</p>
          <p className="mt-1 break-words opacity-90">{String(error)}</p>
        </div>
      )}
      {actions.length > 0 && (
        <div className="flex flex-wrap gap-2 text-xs opacity-80">
          {actions.map((action) => <span key={String(action)} className="rounded-full border border-current/20 px-2 py-1">{String(action)}</span>)}
        </div>
      )}
      {(status.can_pause || status.can_resume || status.can_cancel || status.can_delete) && (
        <div className="flex flex-wrap gap-2 border-t border-current/15 pt-3">
          {status.can_pause && <Button size="sm" disabled={actionBusy} onClick={onPause}>暂停任务</Button>}
          {status.can_resume && <Button size="sm" disabled={actionBusy} onClick={onResume}>继续生成</Button>}
          {status.can_cancel && <Button size="sm" variant="outline" disabled={actionBusy} onClick={onCancel}>取消任务</Button>}
          {status.can_delete && <Button size="sm" variant="destructive" disabled={actionBusy} onClick={onDelete}>删除任务</Button>}
        </div>
      )}
    </div>
  );
}

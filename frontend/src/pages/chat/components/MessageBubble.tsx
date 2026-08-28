/**
 * 消息气泡(对齐 MessageBubble 结构,裁掉业务件)。
 * 保留:用户气泡(plain text)、助手气泡(MarkdownMessage)、流式无文本时流光占位、
 * 参考来源(证据)面板。删掉:ExecutionRecord(trace)、ScheduledDraftCard、attachments、feedback。
 */
import { memo, useState, type ReactNode } from 'react';

import type { EvidenceItem, MessageView, QueryTraceStep } from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { MarkdownMessage, citationDisplayTitle } from '../chatHelpers';
import {
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATIONS_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';

type Props = {
  msg: MessageView;
  /** 助手消息的参考来源(证据)。 */
  evidence?: EvidenceItem[];
  /** 流式中的临时气泡:有文本走 markdown,无文本走流光占位。 */
  streaming?: boolean;
  streamingText?: string;
  traceSteps?: QueryTraceStep[];
  /** 降级提醒(fail-open 发生时)。 */
  degradedNotes?: Array<{ stage: string; reason: string }>;
};

function CollapsibleSection({
  title,
  badge,
  defaultOpen = false,
  className,
  children,
}: {
  title: string;
  badge?: string;
  defaultOpen?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="flex items-center gap-[6px] text-[11px] text-[#9aa1b1] hover:text-[#464c5e]"
      >
        <span className={cn('inline-block transition-transform', open && 'rotate-90')}>▸</span>
        <span>{title}</span>
        {badge != null && <span className="text-[#c3c8d4]">{badge}</span>}
      </button>
      {open && <div className="mt-[6px] space-y-[6px]">{children}</div>}
    </div>
  );
}

// Agent steps from the agent loop's tool_started / tool_result events (keyed
// by tool name; label already humanized on the hook side). Collapsible and
// collapsed by default on completed messages; open while streaming so live
// progress stays visible.
function StepGroup({ steps, defaultOpen = false }: { steps: QueryTraceStep[]; defaultOpen?: boolean }) {
  if (steps.length === 0) return null;
  const runningCount = steps.filter((s) => s.status === 'running').length;
  return (
    <CollapsibleSection
      title="执行轨迹"
      badge={`(${steps.length}${runningCount > 0 ? ` · ${runningCount} running` : ''})`}
      defaultOpen={defaultOpen}
    >
      {steps.map((s, idx) => (
        <div key={`${s.key}-${idx}`} className="flex items-start gap-[6px] text-[12px]">
          <span
            className={cn(
              'mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full',
              s.status === 'running' ? 'animate-pulse bg-[#1d4ed8]' : s.status === 'done' ? 'bg-[#16a34a]' : s.status === 'error' ? 'bg-[#d20b0b]' : 'bg-[#d1d5db]',
            )}
          />
          <div className="min-w-0 flex-1 whitespace-pre-wrap break-words">
            <span className={cn(s.status === 'error' ? 'text-[#d20b0b]' : 'text-[#464c5e]')}>{s.label}</span>
            {s.detail && <span className="text-[#9aa1b1]"> · {s.detail}</span>}
          </div>
        </div>
      ))}
    </CollapsibleSection>
  );
}

/** Quiet spinner while the agent is busy with no visible step yet. */
function WaitSpinner() {
  return (
    <div className="flex items-center gap-[8px] text-[12px] text-[#9aa1b1]">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-[#cbd5e1] border-t-[#1d4ed8]" />
      <span className="thinking-shimmer">正在思考…</span>
    </div>
  );
}

function DegradedBanner({ notes }: { notes: Array<{ stage: string; reason: string }> }) {
  if (notes.length === 0) return null;
  return (
    <div className="rounded-[8px] border border-[#f0d28a] bg-[#fff8e6] px-[12px] py-[8px] text-[12px] leading-[18px] text-[#8a6a1f]">
      <div className="mb-[2px] font-semibold">⚠️ 本次回答存在降级</div>
      {notes.map((n, idx) => (
        <div key={idx}>
          · {n.stage ? `${n.stage}：` : ''}
          {n.reason}
        </div>
      ))}
    </div>
  );
}

/** 参考来源(证据)面板:贴在助手气泡底部,用 citations 样式。 */
function EvidenceBlock({ evidence }: { evidence: EvidenceItem[] }) {
  const [open, setOpen] = useState(false);
  if (evidence.length === 0) return null;
  return (
    <div className={CHAT_CITATIONS_CLASS}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(CHAT_CITATION_HEADING_CLASS, 'cursor-pointer')}
      >
        <AppIcon name="file" size={14} />
        <span>参考来源({evidence.length})</span>
      </button>
      {open && (
        <div className="grid gap-[8px]">
          {evidence.map((item, index) => {
            const fileName = citationDisplayTitle(item);
            const preview = citationPreview(item);
            const locator = citationLocator(item);
            const sourceKind = item.content_kind || item.source_type || '';
            return (
              <div
                key={index}
                className="rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[10px] py-[8px]"
              >
                <div className="mb-[4px] flex items-center gap-[6px] text-[12px] font-semibold text-[#18181a]">
                  <span className="text-[#757f9c]">[{index + 1}]</span>
                  <span className="min-w-0 truncate">{fileName}</span>
                  {sourceKind && (
                    <span className="ml-auto shrink-0 text-[11px] font-normal text-[#858b9c]">
                      {sourceKind}
                    </span>
                  )}
                </div>
                {preview && (
                  <div className="line-clamp-3 whitespace-pre-wrap text-[12px] leading-[18px] text-[#858b9c]">
                    {preview}
                  </div>
                )}
                {locator && <div className="mt-[5px] text-[11px] leading-[16px] text-[#9aa1b1]">{locator}</div>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function citationPreview(item: EvidenceItem): string {
  const metadata = item.metadata ?? {};
  const candidates = [
    item.text_preview,
    item.text,
    item.content,
    typeof metadata.raw_text === 'string' ? metadata.raw_text : '',
    typeof metadata.text_preview === 'string' ? metadata.text_preview : '',
  ];
  return candidates.find((value) => typeof value === 'string' && value.trim())?.trim() ?? '';
}

function citationLocator(item: EvidenceItem): string {
  const locator = item.locator ?? {};
  const sheet = String(locator.sheet_name ?? '');
  const cell = String(locator.cell_ref ?? '');
  const row = locator.row_index;
  if (sheet && cell) return `定位：${sheet} · ${cell}`;
  if (sheet && row !== undefined && row !== null) return `定位：${sheet} · 第 ${Number(row) + 1} 行`;
  const page = locator.page ?? item.metadata?.page ?? item.metadata?.page_number;
  if (page !== undefined && page !== null && String(page)) return `定位：第 ${page} 页`;
  const chunk = locator.chunk_id ?? item.chunk_id;
  if (chunk) return `定位：片段 ${chunk}`;
  const design = locator.design_id ?? item.metadata?.design_id;
  if (design) return `定位：设计 ${design}`;
  return '';
}

function StreamingText({ content }: { content: string }) {
  // 生成中不解析不断增长的整段 Markdown，避免每个增量都阻塞主线程。
  return <div className="whitespace-pre-wrap break-words text-[14px] leading-[24px] text-[#2d3140]">{content}</div>;
}

function MessageBubble({
  msg,
  evidence,
  streaming,
  streamingText,
  traceSteps = [],
  degradedNotes = [],
}: Props) {
  // 流式临时气泡
  if (streaming) {
    const generating = !!streamingText;
    return (
      <div className={CHAT_MESSAGE_ITEM_CLASS}>
        <div className={chatRowClass('assistant')}>
          <div className={chatBubbleClass('assistant')}>
            <div className="grid gap-[12px]">
              {degradedNotes.length > 0 && <DegradedBanner notes={degradedNotes} />}
              {!generating && traceSteps.length === 0 && <WaitSpinner />}
              {traceSteps.length > 0 && <StepGroup steps={traceSteps} defaultOpen />}
              {generating && (
                <div data-i18n-ignore>
                  <StreamingText content={streamingText} />
                  <span className="text-[#1d4ed8]">▍</span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const isError = msg.content.startsWith('⚠️');
  return (
    <div className={CHAT_MESSAGE_ITEM_CLASS}>
      <div className={chatRowClass(msg.role)}>
        <div className={chatBubbleClass(msg.role, isError)}>
          {msg.role === 'assistant' ? (
            <div className="grid gap-[12px]" data-i18n-ignore>
              {traceSteps.length > 0 && <StepGroup steps={traceSteps} />}
              <MarkdownMessage content={msg.content} />
              {msg.footer && (
                <CollapsibleSection title="检索概览" className="border-t border-[#e3e7f1] pt-[10px]">
                  <MarkdownMessage content={msg.footer} />
                </CollapsibleSection>
              )}
            </div>
          ) : (
            <div className={CHAT_PLAIN_ANSWER_CLASS}>{msg.content}</div>
          )}
          {msg.role === 'assistant' && evidence && evidence.length > 0 && (
            <EvidenceBlock evidence={evidence} />
          )}
        </div>
      </div>
    </div>
  );
}

// 流式更新时消息列表仍会刷新，但历史消息无需重新解析 Markdown。
export default memo(MessageBubble);

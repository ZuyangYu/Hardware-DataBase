/**
 * 消息气泡(对齐 MessageBubble 结构,裁掉业务件)。
 * 保留:用户气泡(plain text)、助手气泡(MarkdownMessage)、流式无文本时流光占位、
 * 参考来源(证据)面板。删掉:ExecutionRecord(trace)、ScheduledDraftCard、attachments、feedback。
 */
import { memo, useState } from 'react';

import type { EvidenceItem, MessageView, QueryTraceStep } from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { MarkdownMessage, citationDisplayTitle } from '../chatHelpers';
import {
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATIONS_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_STREAM_TRACE_CLASS,
  CHAT_STREAM_TRACE_ITEM_CLASS,
  CHAT_STREAM_TRACE_HEAD_CLASS,
  CHAT_STREAM_TRACE_DOT_CLASS,
  CHAT_STREAM_TRACE_DOT_RUNNING_CLASS,
  CHAT_STREAM_TRACE_DETAIL_CLASS,
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
};

const TRACE_DETAIL_FALLBACKS: Record<string, string> = {
  route: '正在判断是否需要检索知识库',
  route_query: '正在判断是否需要检索知识库',
  analyze: '正在拆解问题、实体和子问题',
  question_analysis_agent: '正在拆解问题、实体和子问题',
  plan: '正在规划可用来源和检索工具',
  retrieve: '正在召回相关硬件资料',
  merge: '正在合并多源证据',
  draft: '正在生成中间草稿',
  judge: '正在判断证据是否足够',
  verify: '正在校验答案来源',
};

const HIDDEN_TRACE_KEYS = new Set(['permission', 'generate']);

function fallbackTraceDetail(step: QueryTraceStep): string {
  if (step.key === 'analyze' && step.status === 'done') {
    return '已完成问题范围分析';
  }
  const detail = step.detail?.trim();
  if (detail) return detail;
  return TRACE_DETAIL_FALLBACKS[step.key] || TRACE_DETAIL_FALLBACKS[step.label] || '';
}

function TraceStatus({ status }: { status: QueryTraceStep['status'] }) {
  if (status === 'done') return null;
  const text = status === 'running' ? '进行中' : status === 'error' ? '错误' : '等待';
  const className = cn(
    'shrink-0 text-[10px] font-medium leading-none',
    status === 'running' && 'animate-pulse text-[#1d4ed8]',
    status === 'error' && 'text-[#b42318]',
    status === 'pending' && 'text-[#858b9c]',
  );
  return <span className={className}>{text}</span>;
}

function StreamingTraceBlock({ traceSteps }: { traceSteps: QueryTraceStep[] }) {
  const visibleSteps = traceSteps.filter((step) => !HIDDEN_TRACE_KEYS.has(step.key));
  if (visibleSteps.length === 0) return null;
  return (
    <div className={CHAT_STREAM_TRACE_CLASS}>
      {visibleSteps.map((step, index) => {
        const isLast = index === visibleSteps.length - 1;
        const detail = fallbackTraceDetail(step);
        return (
          <div key={`${step.key}-${index}`} className={CHAT_STREAM_TRACE_ITEM_CLASS}>
            <div className={CHAT_STREAM_TRACE_HEAD_CLASS}>
              <span
                className={cn(
                  CHAT_STREAM_TRACE_DOT_CLASS,
                  step.status === 'running' && CHAT_STREAM_TRACE_DOT_RUNNING_CLASS,
                )}
              />
              <span className="min-w-0 truncate text-[#464c5e]">{step.label}</span>
              <TraceStatus status={step.status} />
            </div>
            {detail && <div className={CHAT_STREAM_TRACE_DETAIL_CLASS}>{detail}</div>}
            {isLast && step.status === 'running' && !detail && (
              <div className={CHAT_STREAM_TRACE_DETAIL_CLASS}>正在展开下一步链路…</div>
            )}
          </div>
        );
      })}
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

function MessageBubble({ msg, evidence, streaming, streamingText, traceSteps = [] }: Props) {
  // 流式临时气泡
  if (streaming) {
    return (
      <div className={CHAT_MESSAGE_ITEM_CLASS}>
        <div className={chatRowClass('assistant')}>
          <div className={chatBubbleClass('assistant')}>
            <div className="grid gap-[12px]">
              <StreamingTraceBlock traceSteps={traceSteps} />
              {streamingText && (
                <div data-i18n-ignore>
                  <StreamingText content={streamingText} />
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
              <MarkdownMessage content={msg.content} />
              {msg.footer && (
                <div className="border-t border-[#e3e7f1] pt-[10px] text-[13px] text-[#646b7d]">
                  <MarkdownMessage content={msg.footer} />
                </div>
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

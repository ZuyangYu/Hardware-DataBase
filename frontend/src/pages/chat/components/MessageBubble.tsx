/**
 * 消息气泡(对齐 MessageBubble 结构,裁掉业务件)。
 * 保留:用户气泡(plain text)、助手气泡(MarkdownMessage)、流式无文本时流光占位、
 * 参考来源(证据)与记忆上下文面板。删掉:ExecutionRecord(trace)、ScheduledDraftCard、attachments、feedback。
 */
import { memo, useState } from 'react';

import type { EvidenceItem, MemoryContextItem, MessageView, QueryTraceStep } from '@/api/types';
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
  /** 为当前用户把一条已完成消息提交到明确的个人记忆授权确认流。 */
  onCreateMemory?: (messageId: number) => void;
  /** 打开消息编辑对话框(仅本人 user 消息)。 */
  onEditMessage?: (messageId: number) => void;
};

// Humanize the backend's stage keys into short one-line step labels.
const STEP_SHORT: Record<string, string> = {
  short_term_memory: '短期记忆',
  route: '识别问题',
  route_query: '识别问题',
  analyze: '分析问题',
  question_analysis_agent: '分析问题',
  catalog: '读取目录',
  scan_kb_catalog: '读取目录',
  plan: '规划来源',
  retrieval_planner_agent: '规划来源',
  retrieve: '检索资料',
  merge: '整理证据',
  merge_evidence: '整理证据',
  evaluate: '评估覆盖',
  score_and_compare_evidence: '评估覆盖',
  draft: '起草',
  judge: '判断充分性',
  judge_sufficiency: '判断充分性',
  plan_next_retrieval: '规划补检',
  verify: '校验来源',
  verify_grounding: '校验来源',
};
const HIDDEN_STEPS = new Set(['route', 'route_query', 'generate']);

function stepLabel(key: string, fallback: string): string {
  return STEP_SHORT[key] || fallback;
}

/** Agent steps shown inline, no background, always fully visible. */
function StepGroup({ steps }: { steps: QueryTraceStep[] }) {
  const visible = steps.filter((s) => !HIDDEN_STEPS.has(s.key));
  if (visible.length === 0) return null;
  return (
    <div className="space-y-[6px]">
      {visible.map((s, idx) => (
        <div key={`${s.key}-${idx}`} className="flex items-start gap-[6px] text-[12px]">
          <span
            className={cn(
              'mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full',
              s.status === 'running' ? 'animate-pulse bg-[#1d4ed8]' : s.status === 'done' ? 'bg-[#16a34a]' : 'bg-[#d1d5db]',
            )}
          />
          <div className="min-w-0 flex-1 whitespace-pre-wrap break-words">
            <span className="text-[#464c5e]">{stepLabel(s.key, s.label)}</span>
            {s.detail && <span className="text-[#9aa1b1]"> · {s.detail}</span>}
          </div>
        </div>
      ))}
    </div>
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

function memoryScopeLabel(scope?: string): string {
  if (scope === 'user') return '个人记忆';
  if (scope === 'project') return '项目记忆';
  return scope || '长期记忆';
}

function memoryStatusLabel(status?: string): string {
  if (status === 'verified') return '已验证';
  if (status === 'verification_pending') return '待审核';
  if (status === 'candidate') return '候选';
  return status || '候选';
}

/** 长期记忆上下文面板:与正式参考来源分开,只展示检索到的历史线索。 */
function MemoryContextBlock({ memories }: { memories: MemoryContextItem[] }) {
  const [open, setOpen] = useState(false);
  const visible = memories.filter((item) => item && (item.title || item.content));
  if (visible.length === 0) return null;

  return (
    <div className="border-t border-[#e3e7f1] pt-[10px]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full cursor-pointer items-center gap-[6px] text-left text-[12px] font-semibold text-[#464c5e]"
      >
        <AppIcon name="history" size={14} />
        <span>记忆上下文（长期记忆 {visible.length}）</span>
        <span className="ml-auto text-[11px] font-normal text-[#858b9c]">{open ? '收起' : '展开'}</span>
      </button>
      {open && (
        <div className="mt-[8px] grid gap-[8px]">
          <div className="text-[11px] leading-[16px] text-[#858b9c]">
            本次检索到的记忆上下文可用于辅助回答；它不是正式知识库证据，未必全部被最终回答采用。
          </div>
          {visible.map((item, index) => (
            <div
              key={String(item.id || `${item.scope}-${item.title}-${index}`)}
              className="rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[10px] py-[8px]"
            >
              <div className="mb-[4px] flex items-center gap-[6px] text-[12px] font-semibold text-[#18181a]">
                <span className="text-[#757f9c]">[M{index + 1}]</span>
                <span className="min-w-0 truncate">{item.title || '未命名记忆'}</span>
                <span className="ml-auto shrink-0 text-[11px] font-normal text-[#858b9c]">
                  {memoryScopeLabel(item.scope)} · {memoryStatusLabel(item.status)}
                </span>
              </div>
              {item.content && (
                <div className="whitespace-pre-wrap break-words text-[12px] leading-[18px] text-[#858b9c]">
                  {item.content}
                </div>
              )}
              {!!item.source_count && (
                <div className="mt-[5px] text-[11px] leading-[16px] text-[#9aa1b1]">关联来源 {item.source_count} 条</div>
              )}
            </div>
          ))}
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
  onCreateMemory,
  onEditMessage,
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
              {traceSteps.length > 0 && !generating && <StepGroup steps={traceSteps} />}
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
              <MarkdownMessage content={msg.content} />
              {msg.footer && (
                <div className="border-t border-[#e3e7f1] pt-[10px] text-[13px] text-[#646b7d]">
                  <MarkdownMessage content={msg.footer} />
                </div>
              )}
              {msg.memory_context && msg.memory_context.length > 0 && (
                <MemoryContextBlock memories={msg.memory_context} />
              )}
            </div>
          ) : (
            <div className="grid gap-[7px]">
              {msg.redacted ? (
                <div className="flex flex-col items-end whitespace-pre-wrap">
                  <span className="italic text-[#9aa1b1]">{msg.content || '[已脱敏]'}</span>
                </div>
              ) : (
                <div className={CHAT_PLAIN_ANSWER_CLASS}>{msg.content}</div>
              )}
              {msg.role === 'user' && msg.id > 0 && !msg.redacted && (
                <div className="flex items-center justify-end gap-[10px]">
                  {onEditMessage && (
                    <button
                      type="button"
                      onClick={() => onEditMessage(msg.id)}
                      className="w-fit text-[11px] text-[#858b9c] underline-offset-2 transition-colors hover:text-[#1d4ed8] hover:underline"
                    >
                      编辑
                    </button>
                  )}
                  {onCreateMemory && (
                    <button
                      type="button"
                      onClick={() => onCreateMemory(msg.id)}
                      className="w-fit text-[11px] text-[#858b9c] underline-offset-2 transition-colors hover:text-[#1d4ed8] hover:underline"
                    >
                      创建个人记忆
                    </button>
                  )}
                </div>
              )}
            </div>
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

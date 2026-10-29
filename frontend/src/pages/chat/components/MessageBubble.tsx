/**
 * 消息气泡(对齐 MessageBubble 结构,裁掉业务件)。
 * 保留:用户气泡(plain text)、助手气泡(MarkdownMessage)、流式无文本时流光占位、
 * 参考来源(证据)与记忆上下文面板。删掉:ExecutionRecord(trace)、ScheduledDraftCard、attachments、feedback。
 */
import { memo, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '@/api/client';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { citationLocator, citationReadError, citationReadRequest, type CitationReadback } from '../citationModel';

import type {
  EvidenceItem,
  ExportArtifactView,
  ExportFormat,
  ExportJobView,
  MemoryContextItem,
  MessageView,
  QueryTraceStep,
  TurnBudget,
  TurnPlanState,
  TurnSubagent,
  TurnVerification,
} from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { MarkdownMessage, citationDisplayTitle } from '../chatHelpers';
import ExportMenu, { exportJobStatusLabel } from './ExportMenu';
import FeedbackActions from './FeedbackActions';
import { exportFormatLabel } from '../exportResultModel';
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
  /** 复杂任务执行计划（实时气泡传入；历史消息从 msg.plan 读取）。 */
  plan?: TurnPlanState | null;
  /** 本轮 agent 循环预算。 */
  budget?: TurnBudget | null;
  /** 系统校验结果。 */
  verification?: TurnVerification | null;
  /** 检索子 agent 摘要（不含原始上下文）。 */
  subagents?: TurnSubagent[];
  /** 预算耗尽后的继续操作。 */
  onContinue?: () => void;
  /** 预算耗尽后的缩小范围操作。 */
  onNarrowScope?: () => void;
  /** 降级提醒(fail-open 发生时)。 */
  degradedNotes?: Array<{ stage: string; reason: string }>;
  /** 通用对话不展示内部执行轨迹和检索概要。 */
  showDiagnostics?: boolean;
  /** 为当前用户把一条已完成消息提交到明确的个人记忆授权确认流。 */
  onCreateMemory?: (messageIds: number[]) => void;
  /** 把本轮问答主动提交为项目记忆提炼（结果先进入候选状态）。 */
  onCreateProjectMemory?: (messageIds: number[]) => void;
  /** 本轮问答的消息 id(提问在前、输出在后);个人记忆授权来源必须包含输出。 */
  memorySourceMessageIds?: number[];
  /** 打开消息编辑对话框(仅本人 user 消息)。 */
  onEditMessage?: (messageId: number) => void;
  /** 为已完成助手轮次创建一个后台导出任务。 */
  onExport?: (message: MessageView, format: ExportFormat) => void;
  /** 通过带认证的 API 下载已完成 Artifact。 */
  onDownload?: (job: ExportJobView) => void;
  /** 通过带认证的 API 读取安全的 Artifact 预览。 */
  onPreview?: (job: ExportJobView) => void;
  exportJobs?: ExportJobView[];
  exportPreviews?: Record<string, ExportArtifactView>;
  exportFormats?: ExportFormat[];
};

function CollapsibleSection({
  title,
  badge,
  icon,
  defaultOpen = false,
  className,
  children,
}: {
  title: string;
  badge?: string;
  icon?: ReactNode;
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
        className="flex w-full items-center gap-[7px] text-left text-[11px] font-medium text-[#6b7280] transition-colors hover:text-[#18181a]"
      >
        <span className={cn('inline-block text-[10px] text-[#a1a7b5] transition-transform', open && 'rotate-90')}>▸</span>
        {icon && <span className="inline-grid size-[18px] place-items-center rounded-[6px] bg-[#f1f3f7] text-[#5b6273]">{icon}</span>}
        <span>{title}</span>
        {badge != null && <span className="font-normal text-[#a1a7b5]">{badge}</span>}
      </button>
      {open && <div className="mt-[9px] space-y-[6px]">{children}</div>}
    </div>
  );
}

function AssistantIdentity({ live = false }: { live?: boolean }) {
  return (
    <div className="mb-[2px] flex items-center gap-[7px]">
      <span className="inline-grid size-[20px] place-items-center rounded-[7px] bg-[#18181a] text-white shadow-[0_2px_6px_rgba(24,24,26,0.16)]">
        <AppIcon name="database" size={11} />
      </span>
      <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-[#9aa1b1]">
        {live ? '正在检索' : '知识库回答'}
      </span>
      <span className="h-px flex-1 bg-[linear-gradient(90deg,#eceef3,transparent)]" aria-hidden />
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

const PLAN_STATUS_META: Record<string, { icon: string; label: string; className: string }> = {
  completed: { icon: '✓', label: '已完成', className: 'text-[#15803d]' },
  in_progress: { icon: '◐', label: '进行中', className: 'text-[#1d4ed8]' },
  pending: { icon: '○', label: '待开始', className: 'text-[#9aa1b1]' },
};

function PlanPanel({ plan, defaultOpen = false }: { plan: TurnPlanState; defaultOpen?: boolean }) {
  if (!plan.items?.length) return null;
  const completed = plan.items.filter((item) => item.status === 'completed').length;
  const percent = Math.round((completed / plan.items.length) * 100);
  return (
    <CollapsibleSection
      title="执行计划"
      badge={`${completed}/${plan.items.length}`}
      icon={<AppIcon name="check-list" size={11} />}
      defaultOpen={defaultOpen}
      className="rounded-[12px] border border-[#e6e9f0] bg-white px-[12px] py-[10px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]"
    >
      <div className="mb-[9px] h-[3px] overflow-hidden rounded-full bg-[#eef1f6]">
        <div
          className="h-full rounded-full bg-[#18181a] transition-all duration-300"
          style={{ width: `${Math.max(0, Math.min(100, percent))}%` }}
        />
      </div>
      <div className="grid gap-[7px]">
        {plan.items.map((item, index) => {
          const meta = PLAN_STATUS_META[item.status] ?? PLAN_STATUS_META.pending;
          return (
            <div key={`${item.content}-${index}`} className="flex items-start gap-[8px] text-[12px] leading-[18px]">
              <span className={cn('mt-[1px] inline-grid size-[16px] shrink-0 place-items-center rounded-full border border-[#eceef3] bg-[#fafbfc] text-[9px]', meta.className)}>
                {meta.icon}
              </span>
              <span className={cn('min-w-0 flex-1 break-words', item.status === 'completed' ? 'text-[#858b9c]' : 'text-[#464c5e]')}>
                {item.content}
              </span>
              <span className={cn('mt-[1px] shrink-0 text-[10px]', meta.className)}>{meta.label}</span>
            </div>
          );
        })}
      </div>
    </CollapsibleSection>
  );
}

function BudgetPanel({
  budget,
  onContinue,
  onNarrowScope,
}: {
  budget: TurnBudget;
  onContinue?: () => void;
  onNarrowScope?: () => void;
}) {
  const exhausted = budget.exhausted === true || budget.status === 'exhausted';
  const partial = budget.partial_answer === true || budget.status === 'partial';
  const contextWindow = budget.context_window;
  if (!exhausted && !partial && !contextWindow) return null;
  const tone = exhausted || partial
    ? 'border-[#f0d28a] bg-[#fff8e6] text-[#8a6a1f]'
    : 'border-[#dbe3f0] bg-[#f8faff] text-[#46506b]';
  const complexity = budget.complexity;
  const compressed = budget.context_compressed;
  const limit = Number(budget.recursion_limit ?? 0);
  const calls = Number(budget.tool_calls ?? 0);
  const usagePercent = limit > 0 ? Math.min(100, Math.round((calls / limit) * 100)) : 0;
  return (
    <div className={cn('rounded-[12px] border px-[12px] py-[10px] text-[12px] leading-[18px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]', tone)}>
      {exhausted || partial ? (
        <>
          <div className="mb-[3px] flex items-center gap-[6px] font-semibold">
            <AppIcon name="chart" size={12} />
            {exhausted ? '本轮执行预算已用完' : '本轮为部分交付'}
          </div>
          <div>
            已执行 {budget.tool_calls ?? 0} 次工具调用，登记 {budget.evidence_count ?? 0} 条证据；
            {budget.partial_answer ? '已基于已有证据生成本轮可交付的部分结果。' : '未能生成可交付结果。'}
          </div>
          {limit > 0 && (
            <div className="mt-[8px]">
              <div className="h-[3px] overflow-hidden rounded-full bg-white/70">
                <div
                  className={cn('h-full rounded-full transition-all', exhausted ? 'bg-[#c98a16]' : 'bg-[#8b93a7]')}
                  style={{ width: `${Math.max(3, usagePercent)}%` }}
                />
              </div>
              <div className="mt-[3px] text-[10px] opacity-70">工具调用 {calls}/{limit}</div>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="mb-[3px] flex items-center gap-[6px] font-semibold">
            <AppIcon name="history" size={12} />
            上下文预算
          </div>
          <div>
            本轮最大输入约 {contextWindow?.max_prompt_tokens ?? 0} tokens
            {contextWindow?.window_tokens ? `（模型窗口 ${contextWindow.window_tokens}）` : ''}；
            长对话由框架摘要中间件保留近期消息与关键事实。
          </div>
        </>
      )}
      {complexity && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[6px] text-[10px] opacity-80">
          <span className="rounded-full border border-white/50 bg-white/55 px-[7px] py-[2px]">
            复杂度 {complexity.level ?? 'unknown'}
            {typeof complexity.score === 'number' ? ` · ${complexity.score} 分` : ''}
          </span>
          {typeof budget.recursion_limit === 'number' && (
            <span className="rounded-full border border-white/50 bg-white/55 px-[7px] py-[2px]">
              步数上限 {budget.recursion_limit}
            </span>
          )}
        </div>
      )}
      {compressed && (
        <div className="mt-[5px] text-[10px] opacity-75">
          收尾时已将 {compressed.evidence_count ?? 0} 条证据压缩为有界上下文
          {compressed.compressed_bytes ? `（${Math.max(1, Math.round(compressed.compressed_bytes / 1024))} KB）` : ''}。
        </div>
      )}
      {(onContinue || onNarrowScope) && (
        <div className="mt-[7px] flex flex-wrap gap-[8px]">
          {onContinue && (
            <button
              type="button"
              onClick={onContinue}
              className="rounded-[6px] border border-[#d9b95c] bg-white px-[9px] py-[3px] text-[11px] font-medium text-[#7a5a12] hover:bg-[#fffdf5]"
            >
              继续完成
            </button>
          )}
          {onNarrowScope && (
            <button
              type="button"
              onClick={onNarrowScope}
              className="rounded-[6px] border border-[#e4d6b0] bg-transparent px-[9px] py-[3px] text-[11px] text-[#8a6a1f] hover:bg-white/60"
            >
              缩小范围
            </button>
          )}
        </div>
      )}
    </div>
  );
}

const VERIFICATION_STATUS_META: Record<string, { label: string; className: string; badge: string }> = {
  pass: { label: '通过', className: 'border-[#bbf7d0] bg-[#f0fdf4] text-[#166534]', badge: '✓' },
  warn: { label: '有缺口', className: 'border-[#f0d28a] bg-[#fff8e6] text-[#8a6a1f]', badge: '!' },
  fail: { label: '未通过', className: 'border-[#fecaca] bg-[#fef2f2] text-[#b42318]', badge: '×' },
  partial: { label: '部分完成', className: 'border-[#f0d28a] bg-[#fff8e6] text-[#8a6a1f]', badge: '◐' },
  unknown: { label: '未评估', className: 'border-[#e3e7f1] bg-[#fafbfc] text-[#697187]', badge: '?' },
};

const CHECK_STATUS_META: Record<string, { icon: string; className: string }> = {
  pass: { icon: '✓', className: 'text-[#15803d]' },
  warn: { icon: '!', className: 'text-[#b45309]' },
  fail: { icon: '×', className: 'text-[#b42318]' },
  unknown: { icon: '?', className: 'text-[#858b9c]' },
};

function VerificationPanel({
  verification,
  defaultOpen = false,
}: {
  verification: TurnVerification;
  defaultOpen?: boolean;
}) {
  if (!verification.status) return null;
  const meta = VERIFICATION_STATUS_META[verification.status] ?? VERIFICATION_STATUS_META.unknown;
  const checks = verification.checks ?? [];
  const gaps = verification.gaps ?? [];
  return (
    <div className="rounded-[12px] border border-[#e6e9f0] bg-white px-[12px] py-[10px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]">
      <CollapsibleSection
        title="系统校验"
        badge={`${meta.badge} ${meta.label}${checks.length ? ` · ${checks.length} 项` : ''}`}
        icon={<AppIcon name="check-list" size={11} />}
        defaultOpen={defaultOpen}
      >
        <div className={cn('mb-[8px] inline-flex w-fit items-center gap-[5px] rounded-full border px-[8px] py-[2px] text-[10px] font-medium', meta.className)}>
          <span>{meta.badge}</span>
          <span>{meta.label}</span>
        </div>
        <div className="grid gap-[6px]">
          {checks.map((check, index) => {
            const checkMeta = CHECK_STATUS_META[check.status] ?? CHECK_STATUS_META.unknown;
            return (
              <div key={`${check.key}-${index}`} className="flex items-start gap-[7px] text-[12px] leading-[18px]">
                <span className={cn('mt-[1px] inline-grid size-[15px] shrink-0 place-items-center rounded-full bg-[#fafbfc] text-[10px] font-semibold', checkMeta.className)}>
                  {checkMeta.icon}
                </span>
                <div className="min-w-0 flex-1">
                  <span className="text-[#464c5e]">{check.label}</span>
                  {check.source === 'model_rubric' && (
                    <span className="ml-[5px] rounded-[4px] bg-[#eef2ff] px-[4px] py-[0px] text-[9px] text-[#4f46e5]">模型</span>
                  )}
                  {check.detail && <span className="text-[#9aa1b1]"> · {check.detail}</span>}
                </div>
              </div>
            );
          })}
        </div>
        {gaps.length > 0 && (
          <div className="mt-[8px] rounded-[8px] border border-[#f0f2f6] bg-[#fafbfc] px-[9px] py-[7px] text-[11px] leading-[17px] text-[#697187]">
            <div className="mb-[3px] text-[10px] font-semibold text-[#5b6273]">缺口与待确认</div>
            {gaps.map((gap, index) => <div key={index} className="flex gap-[5px]"><span className="text-[#b3b8c4]">·</span><span>{gap}</span></div>)}
          </div>
        )}
        {verification.semantic_verification?.status === 'unknown' && (
          <div className="mt-[4px] text-[10px] text-[#9aa1b1]">独立模型校验未完成；以上为结构与引用检查。</div>
        )}
      </CollapsibleSection>
    </div>
  );
}

function SubagentPanel({ subagents, defaultOpen = false }: { subagents: TurnSubagent[]; defaultOpen?: boolean }) {
  if (!subagents.length) return null;
  return (
    <CollapsibleSection
      title="检索子任务"
      badge={`${subagents.length}`}
      icon={<AppIcon name="search" size={11} />}
      defaultOpen={defaultOpen}
      className="rounded-[12px] border border-[#e6e9f0] bg-white px-[12px] py-[10px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]"
    >
      <div className="grid gap-[5px]">
        {subagents.map((subagent, index) => {
          const status = subagent.status ?? 'running';
          const label = status === 'completed' ? '已完成'
            : status === 'failed' ? '失败'
              : status === 'abandoned' ? '已中止'
                : '进行中';
          const className = status === 'completed' ? 'text-[#15803d]'
            : status === 'failed' ? 'text-[#b42318]'
              : status === 'abandoned' ? 'text-[#858b9c]'
                : 'text-[#1d4ed8]';
          return (
            <div key={subagent.tool_call_id ?? `${subagent.name}-${index}`} className="flex items-start gap-[6px] text-[12px] leading-[18px]">
              <span className={cn('mt-[4px] h-1.5 w-1.5 shrink-0 rounded-full', status === 'running' ? 'animate-pulse bg-[#1d4ed8]' : status === 'completed' ? 'bg-[#16a34a]' : status === 'failed' ? 'bg-[#d20b0b]' : 'bg-[#d1d5db]')} />
              <div className="min-w-0 flex-1">
                <span className="text-[#464c5e]">{subagent.name || 'research'}</span>
                <span className={cn('ml-[6px] text-[10px]', className)}>{label}</span>
                {subagent.description && (
                  <div className="truncate text-[11px] text-[#9aa1b1]" title={subagent.description}>
                    {subagent.description}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </CollapsibleSection>
  );
}

/** 参考来源(证据)面板:贴在助手气泡底部,用 citations 样式。 */
function EvidenceBlock({ evidence }: { evidence: EvidenceItem[] }) {
  const [open, setOpen] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [reading, setReading] = useState(false);
  const [source, setSource] = useState<CitationReadback | null>(null);
  const [sourceError, setSourceError] = useState('');
  const sourceRequest = useRef(0);
  useEffect(() => () => { sourceRequest.current += 1; }, []);

  async function readSource(item: EvidenceItem) {
    const request = citationReadRequest(item);
    if (!request) return;
    const requestId = ++sourceRequest.current;
    setSource(null);
    setSourceError('');
    setReading(true);
    setSourceOpen(true);
    try {
      const result = await api.post<CitationReadback>('/api/v1/citations/resolve', request);
      if (sourceRequest.current === requestId) setSource(result);
    } catch (error) {
      if (sourceRequest.current !== requestId) return;
      setSource(null);
      setSourceError(citationReadError(error));
    } finally {
      if (sourceRequest.current === requestId) setReading(false);
    }
  }
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
        <div className="grid max-h-[420px] gap-[8px] overflow-y-auto pr-[2px]">
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
                {citationReadRequest(item) && (
                  <button type="button" onClick={() => void readSource(item)}
                    className="mt-[6px] text-[12px] text-[#0b6cf5] hover:underline"
                    aria-label={`查看原文 ${index + 1}：${fileName}`}>
                    查看原文
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
      <Dialog open={sourceOpen} onOpenChange={(value) => {
        setSourceOpen(value);
        if (!value) { sourceRequest.current += 1; setSource(null); setReading(false); }
      }}>
        <DialogContent className="max-h-[80dvh] overflow-auto">
          <DialogHeader><DialogTitle>{source ? `${source.kb_name} · ${source.source_name}` : '引用原文'}</DialogTitle></DialogHeader>
          {reading && <p role="status">正在核验来源并读取原文…</p>}
          {sourceError && <p role="alert">{sourceError}</p>}
          {source && <div className="whitespace-pre-wrap break-words text-[13px] leading-[21px]">{source.content}</div>}
        </DialogContent>
      </Dialog>
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

function ExportPreview({ artifact }: { artifact: ExportArtifactView }) {
  const preview = artifact.preview ?? {};
  const textPreview = typeof preview.text_preview === 'string' ? preview.text_preview : '';
  const paragraphs = Array.isArray(preview.paragraphs) ? preview.paragraphs : [];
  const sheets = Array.isArray(preview.sheets) ? preview.sheets : [];
  const warnings = Array.isArray(preview.warnings) ? preview.warnings : [];
  return (
    <div className="mt-[6px] w-full rounded-[8px] bg-[#f8fafc] px-[8px] py-[7px] text-[11px] text-[#697187]">
      {textPreview && <pre className="max-h-[180px] overflow-auto whitespace-pre-wrap break-words font-sans leading-[17px]">{textPreview}</pre>}
      {paragraphs.length > 0 && (
        <div className="space-y-[3px]">
          {paragraphs.slice(0, 8).map((paragraph, index) => <p key={index}>{String(paragraph)}</p>)}
        </div>
      )}
      {sheets.length > 0 && (
        <div className="space-y-[3px]">
          {sheets.slice(0, 5).map((sheet, index) => {
            const rowCount = sheet && typeof sheet === 'object' && Array.isArray((sheet as { rows?: unknown[] }).rows)
              ? (sheet as { rows: unknown[] }).rows.length
              : 0;
            const name = sheet && typeof sheet === 'object' ? String((sheet as { name?: unknown }).name ?? '') : '';
            return <p key={index}>工作表：{name}（{rowCount} 行）</p>;
          })}
        </div>
      )}
      {warnings.map((warning, index) => <p key={`warning-${index}`} className="text-[#b42318]">{String(warning)}</p>)}
      {preview.truncated === true && <p>预览已截断，下载文件可查看完整内容。</p>}
      {!textPreview && paragraphs.length === 0 && sheets.length === 0 && warnings.length === 0 && (
        <p>已生成 {artifact.filename}，当前格式仅提供摘要预览。</p>
      )}
    </div>
  );
}

function ExportJobs({
  jobs,
  onDownload,
  onPreview,
  previews = {},
}: {
  jobs: ExportJobView[];
  onDownload?: (job: ExportJobView) => void;
  onPreview?: (job: ExportJobView) => void;
  previews?: Record<string, ExportArtifactView>;
}) {
  if (jobs.length === 0) return null;
  return (
    <div className="border-t border-[#e3e7f1] pt-[10px]">
      <div className="mb-[6px] text-[11px] font-semibold text-[#858b9c]">导出任务</div>
      <div className="grid gap-[5px]">
        {jobs.map((job) => (
          <div key={job.export_job_id} className="flex flex-wrap items-center gap-[7px] text-[11px] text-[#858b9c]">
            <span>{exportFormatLabel(job.format)}</span>
            <span>{exportJobStatusLabel(job.status)}</span>
            {job.status === 'succeeded' && job.artifact && onDownload && (
              <button
                type="button"
                onClick={() => onDownload(job)}
                className="font-medium text-[#0b6cf5] underline-offset-2 hover:underline"
              >
                下载 {job.artifact.filename}
              </button>
            )}
            {job.status === 'succeeded' && job.artifact && onPreview && (
              <button
                type="button"
                onClick={() => onPreview(job)}
                className="font-medium text-[#0b6cf5] underline-offset-2 hover:underline"
              >
                {previews[job.export_job_id] ? '刷新预览' : '预览'}
              </button>
            )}
            {job.error_message && <span className="text-[#b42318]">· {job.error_message}</span>}
            {previews[job.export_job_id] && <ExportPreview artifact={previews[job.export_job_id]} />}
          </div>
        ))}
      </div>
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
  plan,
  budget,
  verification,
  subagents = [],
  onContinue,
  onNarrowScope,
  degradedNotes = [],
  showDiagnostics = true,
  onCreateMemory,
  onCreateProjectMemory,
  memorySourceMessageIds,
  onEditMessage,
  onExport,
  onDownload,
  onPreview,
  exportJobs = [],
  exportPreviews = {},
  exportFormats,
}: Props) {
  const resolvedPlan = plan ?? msg.plan ?? null;
  const resolvedBudget = budget ?? msg.budget ?? null;
  const resolvedVerification = verification ?? msg.verification ?? null;
  const resolvedSubagents = subagents.length > 0 ? subagents : (msg.subagents ?? []);
  // 流式临时气泡
  if (streaming) {
    const generating = !!streamingText;
    return (
      <div className={CHAT_MESSAGE_ITEM_CLASS} data-message-role="assistant" data-streaming="true">
        <div className={chatRowClass('assistant')}>
          <div className={chatBubbleClass('assistant')}>
            <div className="grid gap-[12px]">
              {showDiagnostics && <AssistantIdentity live />}
              {degradedNotes.length > 0 && <DegradedBanner notes={degradedNotes} />}
              {!generating && (!showDiagnostics || traceSteps.length === 0) && <WaitSpinner />}
              {showDiagnostics && resolvedPlan && <PlanPanel plan={resolvedPlan} defaultOpen />}
              {showDiagnostics && resolvedSubagents.length > 0 && <SubagentPanel subagents={resolvedSubagents} defaultOpen />}
              {showDiagnostics && traceSteps.length > 0 && <StepGroup steps={traceSteps} defaultOpen />}
              {showDiagnostics && resolvedBudget && (
                <BudgetPanel budget={resolvedBudget} />
              )}
              {showDiagnostics && resolvedVerification && (
                <VerificationPanel verification={resolvedVerification} defaultOpen />
              )}
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
    <div className={CHAT_MESSAGE_ITEM_CLASS} data-message-id={msg.id} data-message-role={msg.role}>
      <div className={chatRowClass(msg.role)}>
        <div className={chatBubbleClass(msg.role, isError)}>
          {msg.role === 'assistant' ? (
            <div className="grid gap-[12px]" data-i18n-ignore>
              {showDiagnostics && !isError && <AssistantIdentity />}
              {showDiagnostics && resolvedPlan && <PlanPanel plan={resolvedPlan} />}
              {showDiagnostics && resolvedSubagents.length > 0 && <SubagentPanel subagents={resolvedSubagents} />}
              {showDiagnostics && traceSteps.length > 0 && <StepGroup steps={traceSteps} />}
              <MarkdownMessage content={msg.content} />
              {showDiagnostics && resolvedBudget && (
                <BudgetPanel budget={resolvedBudget} onContinue={onContinue} onNarrowScope={onNarrowScope} />
              )}
              {showDiagnostics && resolvedVerification && (
                <VerificationPanel
                  verification={resolvedVerification}
                  defaultOpen={resolvedVerification.status === 'fail' || resolvedVerification.status === 'partial'}
                />
              )}
              <FeedbackActions msg={msg} />
              {msg.memory_context && msg.memory_context.length > 0 && (
                <MemoryContextBlock memories={msg.memory_context} />
              )}
              {((onCreateMemory && memorySourceMessageIds?.length) ||
                (onCreateProjectMemory && memorySourceMessageIds?.length) ||
                (onExport && msg.turn_id)) && (
                <div className="flex flex-wrap items-center gap-[14px]">
                  {onCreateMemory && memorySourceMessageIds?.length ? (
                    <button
                      type="button"
                      onClick={() => onCreateMemory(memorySourceMessageIds)}
                      title="将本轮提问记录为个人记忆来源"
                      className="inline-flex items-center gap-[5px] border-0 bg-transparent p-0 text-[11px] text-[#858b9c] transition-colors hover:text-[#18181a] hover:underline"
                    >
                      <AppIcon name="user" size={13} />
                      创建个人记忆
                    </button>
                  ) : null}
                  {onCreateProjectMemory && memorySourceMessageIds?.length ? (
                    <button
                      type="button"
                      onClick={() => onCreateProjectMemory(memorySourceMessageIds)}
                      title="把本轮问答提炼为项目记忆（先进入候选状态待审核）"
                      className="inline-flex items-center gap-[5px] border-0 bg-transparent p-0 text-[11px] text-[#858b9c] transition-colors hover:text-[#18181a] hover:underline"
                    >
                      <AppIcon name="database" size={13} />
                      记入项目记忆
                    </button>
                  ) : null}
                  {onExport && msg.turn_id && (
                    <ExportMenu formats={exportFormats} onExport={(format) => onExport(msg, format)} />
                  )}
                </div>
              )}
              {showDiagnostics && msg.footer && (
                <CollapsibleSection title="检索概览" className="border-t border-[#e3e7f1] pt-[10px]">
                  <MarkdownMessage content={msg.footer} />
                </CollapsibleSection>
              )}
              <ExportJobs
                jobs={exportJobs ?? []}
                onDownload={onDownload}
                onPreview={onPreview}
                previews={exportPreviews}
              />
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
              {msg.attachments && msg.attachments.length > 0 && (
                <div className="flex flex-wrap items-center justify-end gap-[6px]">
                  {msg.attachments.map((attachment) => {
                    const deleted = Boolean(attachment.deleted);
                    const stateLabel =
                      deleted
                        ? '已删除'
                        : attachment.parse_status === 'ready'
                          ? '已就绪'
                          : attachment.parse_status === 'degraded'
                            ? '部分可读'
                            : attachment.parse_status === 'failed'
                              ? '解析失败'
                              : '解析中…';
                    return (
                      <span
                        key={attachment.attachment_id}
                        title={`${attachment.filename} · ${stateLabel}`}
                        className="inline-flex min-w-0 items-center gap-[6px] rounded-full border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[2px] text-[11px] text-[#464c5e]"
                      >
                        <span className="min-w-0 max-w-[200px] truncate">
                          {attachment.filename}
                          {deleted ? '（已删除）' : ''}
                        </span>
                        <span
                          className={cn(
                            'shrink-0 text-[10px]',
                            deleted || attachment.parse_status === 'failed'
                              ? 'text-[#b45309]'
                              : attachment.parse_status === 'ready'
                                ? 'text-[#166534]'
                                : 'text-[#858b9c]',
                          )}
                        >
                          {stateLabel}
                        </span>
                      </span>
                    );
                  })}
                </div>
              )}
              {msg.role === 'user' && msg.id > 0 && !msg.redacted && onEditMessage && (
                <div className="flex items-center justify-end gap-[10px]">
                  <button
                    type="button"
                    onClick={() => onEditMessage(msg.id)}
                    className="w-fit text-[11px] text-[#858b9c] underline-offset-2 transition-colors hover:text-[#1d4ed8] hover:underline"
                  >
                    编辑
                  </button>
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

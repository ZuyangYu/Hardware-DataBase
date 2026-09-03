/**
 * 消息气泡(对齐 MessageBubble 结构,裁掉业务件)。
 * 保留:用户气泡(plain text)、助手气泡(MarkdownMessage)、流式无文本时流光占位、
 * 参考来源(证据)与记忆上下文面板。删掉:ExecutionRecord(trace)、ScheduledDraftCard、attachments、feedback。
 */
import { memo, useState, type ReactNode } from 'react';

import type {
  EvidenceItem,
  ExportArtifactView,
  ExportFormat,
  ExportJobView,
  MemoryContextItem,
  MessageView,
  QueryTraceStep,
} from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { MarkdownMessage, citationDisplayTitle } from '../chatHelpers';
import ExportMenu, { exportJobStatusLabel } from './ExportMenu';
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
  /** 降级提醒(fail-open 发生时)。 */
  degradedNotes?: Array<{ stage: string; reason: string }>;
  /** 通用对话不展示内部执行轨迹和检索概要。 */
  showDiagnostics?: boolean;
  /** 为当前用户把一条已完成消息提交到明确的个人记忆授权确认流。 */
  onCreateMemory?: (messageId: number) => void;
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
  showDiagnostics = true,
  onCreateMemory,
  onEditMessage,
  onExport,
  onDownload,
  onPreview,
  exportJobs = [],
  exportPreviews = {},
  exportFormats,
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
              {!generating && (!showDiagnostics || traceSteps.length === 0) && <WaitSpinner />}
              {showDiagnostics && traceSteps.length > 0 && <StepGroup steps={traceSteps} defaultOpen />}
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
              {showDiagnostics && traceSteps.length > 0 && <StepGroup steps={traceSteps} />}
              <MarkdownMessage content={msg.content} />
              {showDiagnostics && msg.footer && (
                <CollapsibleSection title="检索概览" className="border-t border-[#e3e7f1] pt-[10px]">
                  <MarkdownMessage content={msg.footer} />
                </CollapsibleSection>
              )}
              {msg.memory_context && msg.memory_context.length > 0 && (
                <MemoryContextBlock memories={msg.memory_context} />
              )}
              {onExport && msg.turn_id && (
                <div className="border-t border-[#e3e7f1] pt-[10px]">
                  <ExportMenu formats={exportFormats} onExport={(format) => onExport(msg, format)} />
                </div>
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

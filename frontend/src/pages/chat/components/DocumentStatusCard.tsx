/**
 * DocumentStatusCard -- 消息流内的文档生成状态卡片(纯渲染,逻辑在 documentCardModel)。
 *
 * 消费 document_card SSE 事件(线格式 {"type":"document_card","payload":{"card":{...}}}),
 * 只渲染后端约定的不可变引用与状态枚举:kind / status / next_actions / kb_name /
 * task_id / work_order_id / generation_session_id / target_format / artifacts(仅 artifact_id+stage)。
 * 澄清和计划确认统一回到主对话；刷新状态与产物下载仍走 REST 直链，
 * URL 与文件名由 documentCardModel 前端拼装。
 */
import { cn } from '@/lib/utils';
import { notify } from '@/components/ui/app-toast';
import {
  documentCardLifecycleLabel,
  documentCardLifecycleTone,
  canRequestDocumentRelease,
  documentCardReviewCandidateArtifact,
  documentCardTitle,
  downloadDocumentArtifact,
  downloadDocumentArtifactAudit,
  downloadDocumentArtifactBundle,
  type DocumentCardData,
  type DocumentStatusTone,
} from './documentCardModel';
import OutputSpecConfirmationCard from './OutputSpecConfirmationCard';
import ConflictReviewPanel from './ConflictReviewPanel';

// 兼容再导出:ChatPage 只用 identity 生成 React key,import 路径保持不变。
export { documentCardIdentity } from './documentCardModel';

const STATUS_BADGE_TONE_CLASS: Record<DocumentStatusTone, string> = {
  neutral: 'bg-[#eef0f4] text-[#858b9c]',
  info: 'bg-[#edf2ff] text-[#1d4ed8]',
  success: 'bg-[#eef8f0] text-[#166534]',
  warning: 'bg-[#fff8e6] text-[#8a6a1f]',
  danger: 'bg-[#fce7e7] text-[#b42318]',
};

const PROGRESS_NODE_LABELS: Record<string, string> = {
  load_context: '加载生成上下文',
  plan_units: '规划字段',
  retrieve_evidence: '检索证据',
  generate_draft: '字段生成进度',
  fill_fields: '字段生成进度',
  validate_draft: '校验字段',
  validate_cross_unit: '交叉校验',
  persist_draft: '保存草稿',
  await_human: '等待人工审核',
  finalize: '生成文档',
  complete: '生成完成',
};

function artifactStageLabel(stage: string): string {
  if (stage === 'review_candidate') return '候选文档（待审核）';
  if (stage === 'approved_release') return '正式文档';
  if (stage === 'draft_preview') return '草稿预览';
  return stage || '文档产物';
}

function progressNodeLabel(card: DocumentCardData): string {
  // A harness may reach its terminal ``complete`` node and then fail a
  // deterministic release gate.  The node name is an execution detail; the
  // user-facing label must follow the authoritative card status so a blocked
  // run never looks like a completed document.
  if ((card.status === 'blocked' || card.status === 'failed')
    && card.progress?.currentNode === 'complete') return '执行进度';
  return PROGRESS_NODE_LABELS[card.progress?.currentNode ?? ''] ?? '文档生成进度';
}

type Props = {
  card: DocumentCardData;
  refreshing?: boolean;
  onRefreshStatus?: (card: DocumentCardData) => void;
  requestingRelease?: boolean;
  onRequestRelease?: (card: DocumentCardData, artifactId: string) => void;
  stale?: boolean;
};

export default function DocumentStatusCard({
  card,
  refreshing = false,
  onRefreshStatus,
  requestingRelease = false,
  onRequestRelease,
  stale = false,
}: Props) {
  const workOrderId = card.work_order_id?.trim() ?? '';
  const tone = documentCardLifecycleTone(card);
  const reviewCandidate = documentCardReviewCandidateArtifact(card);
  const canRequestRelease = Boolean(onRequestRelease)
    && canRequestDocumentRelease(card)
    && Boolean(reviewCandidate);
  const pendingGroups = card.pendingDecisions ?? [];
  const groupedPending = pendingGroups
    .map((group) => `${group.group_key}（${group.count} 项）`)
    .join('、');
  const uniqueRefdes = (card.gateReview?.scopeExceptions ?? [])
    .map((exception) => exception.refdes?.trim() ?? '')
    .filter((value) => value !== '')
    .filter((value, index, values) => values.indexOf(value) === index);
  const pendingRefdes = groupedPending || uniqueRefdes.join('、') || '（未指定位号）';
  const hasCandidateArtifact = card.candidateState === 'available' || (card.artifacts ?? []).some((artifact) => {
    const stage = (artifact.stage || '').trim().toLowerCase();
    return stage === 'review_candidate' || stage === 'draft_preview';
  });
  const suggestedRefdes = (card.gateReview?.scopeExceptions ?? [])
    .flatMap((exception) => exception.suggested_refdes ?? [])
    .map((value) => value.trim())
    .filter((value) => value !== '')
    .filter((value, index, values) => values.indexOf(value) === index)
    .join('、');
  // Clarification text and options are conversation content.  The status
  // card remains a progress/status surface only; quick replies may still read
  // `card.options` in ChatPage and render in the composer.
  const showConversationContent = card.status !== 'needs_clarification';
  if (card.kind === 'output_spec_confirmation') {
    return (
      <OutputSpecConfirmationCard
        card={card}
        stale={stale}
      />
    );
  }
  return (
    <div
      className="rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] px-[10px] py-[8px]"
      data-document-card={card.kind}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-[6px]">
        <span className="text-[12px] font-semibold text-[#18181a]">{documentCardTitle(card.kind)}</span>
        <span
          className={cn(
            'inline-flex items-center gap-[4px] rounded-full px-[8px] py-[1px] text-[11px] font-medium',
            STATUS_BADGE_TONE_CLASS[tone],
          )}
        >
          <span aria-hidden="true" className="size-[6px] rounded-full bg-current" />
          {documentCardLifecycleLabel(card)}
        </span>
        {card.kb_name && <span className="min-w-0 truncate text-[11px] text-[#757f9c]">知识库 {card.kb_name}</span>}
      </div>
      {workOrderId && (
        <div className="mt-[3px] text-[11px] text-[#858b9c]">
          工单 <span className="font-mono text-[#464c5e]">{workOrderId}</span>
        </div>
      )}
      {card.progress && (
        <div className="mt-[7px]" aria-label="文档生成进度">
          <div className="mb-[4px] flex items-center justify-between gap-[8px] text-[11px] text-[#68728a]">
            <span>{progressNodeLabel(card)} {card.progress.completedUnits} / {card.progress.totalUnits}</span>
            <span>{card.progress.percent}%</span>
          </div>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={card.progress.percent}
            className="h-[5px] overflow-hidden rounded-full bg-[#e8edf5]"
          >
            <div
              className="h-full rounded-full bg-[#0b6cf5] transition-[width] duration-300"
              style={{ width: `${card.progress.percent}%` }}
            />
          </div>
        </div>
      )}
      {card.errorMessage && (
        <div className="mt-[7px] rounded-md border border-[#f2c7c7] bg-[#fff7f7] px-[8px] py-[6px] text-[11px] text-[#b42318]" role="alert">
          {card.errorMessage}
        </div>
      )}
      {card.gateReview && (
        <div
          className="mt-[7px] rounded-md border border-[#f2dcb0] bg-[#fffaf0] px-[8px] py-[6px] text-[11px] text-[#7a5b16]"
          data-document-gate={card.gateReview.reviewKind}
        >
          <p className="font-medium">
            {card.gateReview.reviewKind === 'icd_scope'
              ? 'ICD 范围异常待办'
              : `待处理人工审核：${card.gateReview.reviewKind}`}
          </p>
          {pendingGroups.length > 0 ? (
            <ul className="mt-[4px] list-disc pl-[16px]">
              {pendingGroups.map((group) => (
                <li key={group.decision_id || `${group.kind}:${group.group_key}`}>
                  {group.title}（{group.count} 项）
                </li>
              ))}
            </ul>
          ) : card.gateReview.scopeExceptions.length > 0 && (
            <ul className="mt-[4px] list-disc pl-[16px]">
              {card.gateReview.scopeExceptions.map((exception, index) => (
                <li key={`${exception.refdes ?? exception.kind ?? 'exception'}-${index}`}>
                  {exception.refdes ? `${exception.refdes}：` : ''}
                  {exception.user_instruction ?? exception.kind ?? '范围异常'}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-[4px]">
            {card.gateReview.reviewKind !== 'icd_scope'
              ? '请在文档工作台完成审核，或在对话中说明处理意见。'
              : card.gateReview.scopeBlocking
                ? suggestedRefdes
                  ? `冻结 EDF 中的实际位号为 ${suggestedRefdes}。可直接说“按 EDF 实际位号生成”，由助手替换模板示例位号并继续生成；若实际目标不同，请说明目标位号或模块。`
                  : `请确认实际目标接插件后按你的要求重新生成：当前系统解析到的待处理位号为 ${pendingRefdes}。若这些位号应包含，请补齐对应的 EDF 管脚映射并重新解析；若实际目标不是这些位号，请在对话中明确目标位号或模块。`
                : `待处理：${pendingRefdes}。可按实际情况直接用自然语言说明，例如“${pendingGroups[0]?.group_key ?? uniqueRefdes[0] ?? '这组'} 全部包含”或“全部排除”；也可以逐项说明处理意见。`}
          </p>
        </div>
      )}
      {card.status === 'blocked' && (
        <p className="mt-[6px] text-[11px] text-[#68728a]">
          {hasCandidateArtifact
            ? '候选文档已生成，可在上方下载；正式发布被门禁阻止，处理完待办后即可申请发布。'
            : card.errorCode === 'document_job_failed' || card.progress?.completedUnits === 0
              ? '字段填充阶段未完成，请稍后刷新或重试；暂不需要你手工补齐字段。'
              : '请在下方对话中补充或确认缺失信息；工作台仅用于查看候选稿和审核详情。'}
        </p>
      )}
      {card.status === 'needs_review' && !card.gateReview && (
        <p className="mt-[6px] text-[11px] text-[#68728a]">
          候选文档已生成，审核通过后才会发布正式文件。
        </p>
      )}
      {workOrderId && card.kb_name && ['needs_review', 'waiting_human', 'blocked'].includes(card.status) && (
        <ConflictReviewPanel
          key={`${card.kb_name}:${workOrderId}`}
          kbName={card.kb_name}
          workOrderId={workOrderId}
          onDecisionSaved={onRefreshStatus ? () => onRefreshStatus(card) : undefined}
        />
      )}
      {showConversationContent && card.content && (
        <div className="mt-[6px] rounded-md border border-[#e3e7f1] bg-white px-[8px] py-[6px] text-[12px] text-[#464c5e]">
          <p>{card.content}</p>
          {card.options && card.options.length > 0 && (
            <div className="mt-[5px] flex flex-wrap gap-[4px]" aria-label="可选回答">
              {card.options.map((option) => (
                <span
                  key={option}
                  className="rounded-full bg-[#f1f5ff] px-[7px] py-[2px] text-[11px] text-[#315da8]"
                >
                  {option}
                </span>
              ))}
            </div>
          )}
          {card.status === 'needs_clarification' && card.question_id && (
            <p className="mt-[7px] text-[11px] text-[#757f9c]">
              请在下方对话输入框回复，系统会继续完善文档计划。
            </p>
          )}
        </div>
      )}
      {((onRefreshStatus && (workOrderId || card.generation_session_id)) || canRequestRelease) && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[8px]">
          {onRefreshStatus && (workOrderId || card.generation_session_id) && (
            <button
              type="button"
              aria-label={workOrderId ? `刷新工单状态 ${workOrderId}` : '刷新澄清问题'}
              disabled={refreshing}
              onClick={() => onRefreshStatus(card)}
              className="rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] py-[3px] text-[12px] font-medium text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {refreshing ? '刷新中…' : workOrderId ? '刷新状态' : '刷新问题'}
            </button>
          )}
          {canRequestRelease && reviewCandidate && (
            <button
              type="button"
              aria-label={`申请正式发布审核（工单 ${workOrderId}）`}
              disabled={requestingRelease}
              onClick={() => onRequestRelease?.(card, reviewCandidate.artifact_id)}
              className="rounded-[8px] border border-[#0b6cf5] bg-[#0b6cf5] px-[10px] py-[3px] text-[12px] font-medium text-white transition-colors hover:bg-[#095ccc] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {requestingRelease ? '申请中…' : '申请正式发布审核'}
            </button>
          )}
        </div>
      )}
      {card.kind === 'attachment_revision' && (
        <p className="mt-[6px] text-[11px] text-[#68728a]" data-attachment-release-state={card.releaseState ?? 'not_requested'}>
          {card.releaseState === 'pending'
            ? '已提交正式发布审核，等待审核人处理；候选仍可下载。'
            : card.releaseState === 'rejected'
              ? '发布申请被拒绝，候选仍可下载；可在对话中修改后重新申请。'
              : card.releaseState === 'approved'
                ? '已通过审核：正式文件与候选分开保存。'
                : '候选未发布；可在对话中申请正式发布审核。'}
        </p>
      )}
      {(workOrderId || card.kind === 'attachment_revision')
        && card.artifacts
        && card.artifacts.length > 0 && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[6px]">
          <span className="text-[11px] text-[#858b9c]">
            {card.kind === 'attachment_revision' ? '候选附件' : '文档产物'}
          </span>
          {card.artifacts.map((artifact) => (
            <span key={artifact.artifact_id} className="inline-flex flex-wrap gap-1">
            <button
              type="button"
              aria-label={
                card.kind === 'attachment_revision'
                  ? `下载 ${artifactStageLabel(artifact.stage)}`
                  : `下载 ${artifactStageLabel(artifact.stage)}（工单 ${workOrderId}）`
              }
              onClick={() => void downloadDocumentArtifact(card, artifact)}
              className="rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] py-[2px] text-[11px] font-medium text-[#0b6cf5] transition-colors hover:border-[#c9d2e4] hover:bg-[#f4f8ff]"
            >
              下载 {artifact.output_format ? `${artifactStageLabel(artifact.stage)}（${artifact.output_format.toUpperCase()}）` : artifactStageLabel(artifact.stage)}
            </button>
            {workOrderId && (
              <>
              <button
                type="button"
                aria-label={`下载来源与决策记录（产物 ${artifact.artifact_id}）`}
                className="rounded-md border border-[#e3e7f1] bg-white px-2 py-0.5 text-[11px] text-[#464c5e]"
                onClick={() => void downloadDocumentArtifactAudit(card, artifact).catch((error) => notify.error(error instanceof Error ? error.message : '记录下载失败'))}
              >来源与决策记录</button>
              <button
                type="button"
                aria-label={`下载含审计附录包（产物 ${artifact.artifact_id}）`}
                className="rounded-md border border-[#e3e7f1] bg-white px-2 py-0.5 text-[11px] text-[#464c5e]"
                onClick={() => void downloadDocumentArtifactBundle(card, artifact).catch((error) => notify.error(error instanceof Error ? error.message : '审计包下载失败'))}
              >含审计附录包</button>
              </>
            )}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

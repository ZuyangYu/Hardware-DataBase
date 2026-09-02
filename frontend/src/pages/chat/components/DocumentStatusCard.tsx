/**
 * DocumentStatusCard -- 消息流内的文档生成状态卡片(纯渲染,逻辑在 documentCardModel)。
 *
 * 消费 document_card SSE 事件(线格式 {"type":"document_card","payload":{"card":{...}}}),
 * 只渲染后端约定的不可变引用与状态枚举:kind / status / next_actions / kb_name /
 * work_order_id / generation_session_id / target_format / artifacts(仅 artifact_id+stage)。
 * 刷新状态按钮直调 REST 状态接口;人工门按钮一律深链到文档生成工作台;
 * 产物下载按钮走 REST 直链,URL 与文件名由 documentCardModel 前端拼装。
 */
import { cn } from '@/lib/utils';
import type { DocumentStatusTone } from '../../documentGenerationModel';
import {
  documentCardStatusTone,
  documentCardStatusLabel,
  documentCardTitle,
  documentCardWorkbenchActions,
  downloadDocumentArtifact,
  type DocumentCardData,
} from './documentCardModel';

// 兼容再导出:ChatPage 只用 identity 生成 React key,import 路径保持不变。
export { documentCardIdentity } from './documentCardModel';

const STATUS_BADGE_TONE_CLASS: Record<DocumentStatusTone, string> = {
  neutral: 'bg-[#eef0f4] text-[#858b9c]',
  info: 'bg-[#edf2ff] text-[#1d4ed8]',
  success: 'bg-[#eef8f0] text-[#166534]',
  warning: 'bg-[#fff8e6] text-[#8a6a1f]',
  danger: 'bg-[#fce7e7] text-[#b42318]',
};

type Props = {
  card: DocumentCardData;
  refreshing?: boolean;
  onRefreshStatus?: (card: DocumentCardData) => void;
};

export default function DocumentStatusCard({ card, refreshing = false, onRefreshStatus }: Props) {
  const workOrderId = card.work_order_id?.trim() ?? '';
  const tone = documentCardStatusTone(card.status);
  const workbenchActions = documentCardWorkbenchActions(card);
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
          {documentCardStatusLabel(card.status)}
        </span>
        {card.kb_name && <span className="min-w-0 truncate text-[11px] text-[#757f9c]">知识库 {card.kb_name}</span>}
      </div>
      {workOrderId && (
        <div className="mt-[3px] text-[11px] text-[#858b9c]">
          工单 <span className="font-mono text-[#464c5e]">{workOrderId}</span>
        </div>
      )}
      {((onRefreshStatus && workOrderId) || workbenchActions.length > 0) && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[8px]">
          {onRefreshStatus && workOrderId && (
            <button
              type="button"
              aria-label={`刷新工单状态 ${workOrderId}`}
              disabled={refreshing}
              onClick={() => onRefreshStatus(card)}
              className="rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] py-[3px] text-[12px] font-medium text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {refreshing ? '刷新中…' : '刷新状态'}
            </button>
          )}
          {workbenchActions.map((action) => (
            <a
              key={action.action}
              href={action.href}
              aria-label={`${action.label}（工单 ${workOrderId}）`}
              className="text-[12px] font-medium text-[#0b6cf5] underline-offset-2 hover:underline"
            >
              {action.label}
            </a>
          ))}
        </div>
      )}
      {workOrderId && card.artifacts && card.artifacts.length > 0 && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[6px]">
          <span className="text-[11px] text-[#858b9c]">文档产物</span>
          {card.artifacts.map((artifact) => (
            <button
              key={artifact.artifact_id}
              type="button"
              aria-label={`下载 ${artifact.stage}（工单 ${workOrderId}）`}
              onClick={() => void downloadDocumentArtifact(card, artifact)}
              className="rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] py-[2px] text-[11px] font-medium text-[#0b6cf5] transition-colors hover:border-[#c9d2e4] hover:bg-[#f4f8ff]"
            >
              下载 {artifact.stage}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

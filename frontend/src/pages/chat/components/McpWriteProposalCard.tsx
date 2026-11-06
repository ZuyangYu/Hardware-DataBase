import type { McpWriteProposalCard as ProposalCard } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

type Props = {
  card: ProposalCard;
  busy?: boolean;
  onConfirm?: (card: ProposalCard) => void;
  onReject?: (card: ProposalCard) => void;
  onDownload?: (card: ProposalCard) => void;
};

const STATUS_LABEL: Record<string, string> = {
  proposed: '待确认',
  confirmed: '已确认',
  running: '生成中',
  completed: '可下载',
  failed: '生成失败',
  rejected: '已拒绝',
  expired: '已过期',
};

/** Inline human-in-the-loop card for the governed external write channel. */
export default function McpWriteProposalCard({
  card,
  busy = false,
  onConfirm,
  onReject,
  onDownload,
}: Props) {
  const status = STATUS_LABEL[card.status] ?? card.status;
  const filename = card.artifact_filename || '由外部服务返回';
  return (
    <div
      data-mcp-write-card={card.proposal_id}
      className="rounded-[12px] border border-[#ead9b4] bg-[#fffaf0] p-[14px]"
    >
      <div className="flex flex-wrap items-start justify-between gap-[8px]">
        <div>
          <div className="text-[14px] font-semibold text-[#18181a]">
            {card.status === 'proposed' ? '需要你确认：生成外部文件' : '外部文件'}
          </div>
          <div className="mt-[3px] text-[12px] text-[#6b7280]">
            {card.server_name || card.server} · {card.tool}
          </div>
        </div>
        <Badge>{status}</Badge>
      </div>

      <div className="mt-[9px] rounded-[9px] bg-white/70 px-[10px] py-[8px] text-[12px] leading-[1.6] text-[#464c5e]">
        <div>文件：<span className="font-medium text-[#273a59]">{filename}</span></div>
        {card.status === 'proposed' && (
          <div className="mt-[2px] text-[#7a5a12]">
            确认后才会调用外部服务；确认前不会修改任何原件。
          </div>
        )}
        {card.error_code && (
          <div className="mt-[2px] text-[#b5352d]">失败原因：{card.error_code}</div>
        )}
      </div>

      <div className="mt-[11px] flex flex-wrap items-center gap-[8px]">
        {card.status === 'proposed' && onConfirm && (
          <Button disabled={busy} onClick={() => onConfirm(card)}>确认生成文件</Button>
        )}
        {card.status === 'proposed' && onReject && (
          <Button variant="outline" disabled={busy} onClick={() => onReject(card)}>暂不生成</Button>
        )}
        {(card.status === 'confirmed' || card.status === 'running') && (
          <span className="text-[12px] text-[#7a5a12]">正在生成，完成后会自动出现下载按钮。</span>
        )}
        {card.status === 'completed' && card.artifact_filename && onDownload && (
          <Button disabled={busy} onClick={() => onDownload(card)}>下载文件</Button>
        )}
      </div>
    </div>
  );
}

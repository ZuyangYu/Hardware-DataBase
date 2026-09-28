import type { McpWriteProposalCard as ProposalCard } from '../useKbChat';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

type Props = {
  card: ProposalCard;
  busy?: boolean;
  onConfirm?: (card: ProposalCard) => void;
  onReject?: (card: ProposalCard) => void;
  onDownload?: (card: ProposalCard) => void;
};

/** Inline confirmation card for the governed external write channel. */
export default function McpWriteProposalCard({
  card,
  busy = false,
  onConfirm,
  onReject,
  onDownload,
}: Props) {
  return (
    <div data-mcp-write-card={card.proposal_id} className="rounded-[12px] border border-[#ead9b4] bg-[#fffaf0] p-[14px]">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <div className="text-[14px] font-semibold text-[#18181a]">外部插件候选产物</div>
        <Badge>{card.status}</Badge>
      </div>
      <div className="mt-[8px] text-[13px] text-[#464c5e]">
        {card.server_name || card.server} / {card.tool}
      </div>
      <div className="mt-[4px] text-[12px] leading-[1.6] text-[#6b7280]">
        预计产物：{card.artifact_hint || '由外部服务声明'} · 参数摘要：{card.arguments_digest.slice(0, 12)}…
      </div>
      <div className="mt-[4px] text-[12px] text-[#858b9c]">确认有效期至 {card.expires_at || '未声明'}</div>
      {card.error_code && <div className="mt-[6px] text-[12px] text-[#d20b0b]">执行失败：{card.error_code}</div>}
      <div className="mt-[12px] flex flex-wrap gap-[8px]">
        {card.status === 'proposed' && onConfirm && (
          <Button disabled={busy} onClick={() => onConfirm(card)}>确认生成候选产物</Button>
        )}
        {card.status === 'proposed' && onReject && (
          <Button variant="outline" disabled={busy} onClick={() => onReject(card)}>拒绝</Button>
        )}
        {card.status === 'completed' && card.artifact_filename && onDownload && (
          <Button variant="outline" disabled={busy} onClick={() => onDownload(card)}>下载候选产物</Button>
        )}
      </div>
    </div>
  );
}

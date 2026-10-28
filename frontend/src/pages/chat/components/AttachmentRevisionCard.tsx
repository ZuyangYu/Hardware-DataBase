import type { AttachmentRevisionCard } from '@/api/types';

const RELEASE_STATE_TEXT: Record<string, string> = {
  pending: '已提交正式发布审核，等待审核人处理；候选仍可下载。',
  rejected: '发布申请被拒绝，候选仍可下载；可继续修改后重新申请。',
  approved: '已通过审核：正式文件与候选分开保存。',
};

/** Renders the attachment-revision candidate emitted by the attachment worker. */
export default function AttachmentRevisionCardView({ card }: { card: AttachmentRevisionCard }) {
  const artifacts = (card.artifacts ?? []).filter((item) => item.download_url);
  return (
    <div
      className="ml-auto w-full max-w-[820px] rounded-[10px] border border-[#e3e7f1] bg-white px-[14px] py-[10px]"
      data-attachment-revision-card="true"
    >
      <div className="text-[13px] font-medium text-[#18181a]">附件修订候选</div>
      {card.content && <p className="mt-[4px] text-[12px] leading-[18px] text-[#464c5e]">{card.content}</p>}
      {artifacts.length > 0 && (
        <div className="mt-[6px] flex flex-wrap items-center gap-[6px]">
          <span className="text-[11px] text-[#858b9c]">候选附件</span>
          {artifacts.map((artifact) => (
            <a
              key={artifact.artifact_id}
              href={artifact.download_url}
              download
              className="rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] py-[2px] text-[11px] font-medium text-[#0b6cf5] hover:border-[#c9d2e4] hover:bg-[#f4f8ff]"
            >
              下载候选附件{artifact.output_format ? `（${artifact.output_format.toUpperCase()}）` : ''}
            </a>
          ))}
        </div>
      )}
      <p className="mt-[6px] text-[11px] text-[#68728a]">
        {RELEASE_STATE_TEXT[card.release_state ?? ''] ?? '候选未发布；可在对话中申请正式发布审核。'}
      </p>
    </div>
  );
}

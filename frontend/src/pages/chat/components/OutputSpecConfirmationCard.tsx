/**
 * OutputSpecConfirmationCard -- Gate 1 计划确认卡。
 *
 * 只渲染服务器下发的安全提案摘要(格式/章节数/表格数/警告/阻塞)。
 * 用户确认统一通过主对话输入框完成，避免卡片与对话维护两套交互状态；
 * 哈希值本身不出现在界面上。
 */
import type { DocumentCardData } from './documentCardModel';

type Props = {
  card: DocumentCardData;
  stale?: boolean;
};

function formatLabel(format: string): string {
  // 交付格式保持服务端原值(如 xlsx/docx),不猜测大小写语义。
  return format;
}

export default function OutputSpecConfirmationCard({
  card,
  stale = false,
}: Props) {
  const proposal = card.proposal;
  const blockers = proposal?.blockers ?? [];
  const warnings = proposal?.warnings ?? [];
  const deliverables = proposal?.deliverables ?? [];
  const primary = deliverables.find((item) => item.role === 'primary');

  return (
    <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-slate-800">
      <div className="mb-1 font-semibold">计划确认</div>
      {stale ? (
        <div className="mb-2 rounded border border-orange-300 bg-orange-50 px-2 py-1 text-xs text-orange-700">
          计划已更新，请确认最新提案后重新确认；系统不会自动重试。
        </div>
      ) : null}
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {primary ? <span>交付格式：{formatLabel(primary.format)}</span> : null}
        {typeof proposal?.outline_count === 'number' ? <span>章节 {proposal.outline_count}</span> : null}
        {typeof proposal?.table_count === 'number' ? <span>表格 {proposal.table_count}</span> : null}
        {proposal?.executable === false ? <span className="text-rose-600">提案暂不可执行</span> : null}
      </div>
      {warnings.length > 0 ? (
        <ul className="mb-2 list-disc pl-4 text-xs text-amber-700">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}
      {blockers.length > 0 ? (
        <ul className="mb-2 list-disc pl-4 text-xs text-rose-600">
          {blockers.map((blocker, index) => (
            <li key={index}>{typeof blocker === 'string' ? blocker : JSON.stringify(blocker)}</li>
          ))}
        </ul>
      ) : null}
      <div className="rounded border border-amber-200 bg-white/70 px-2 py-1.5 text-xs text-slate-600">
        {blockers.length > 0
          ? '请在下方对话输入框补充或修改需求。'
          : '请在下方对话输入“确认生成”，或直接说明需要修改的内容。'}
      </div>
    </div>
  );
}

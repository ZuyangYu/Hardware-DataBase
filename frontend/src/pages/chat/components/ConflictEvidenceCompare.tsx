import type { DocumentConflict } from '@/api/types';

const ROLES: Record<string, string> = { primary: '主库', reference: '参考', standard: '规范', peer: '对比' };

function display(value: unknown): string {
  return value == null || value === '' ? '未提供' : typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export default function ConflictEvidenceCompare({ claims }: { claims: NonNullable<DocumentConflict['claims']> }) {
  return (
    <div className="overflow-x-auto rounded-md border border-[#e3e7f1]">
      <table className="w-full text-left text-[12px]" aria-label="冲突来源证据对照">
        <thead className="bg-[#f1f5ff]"><tr>
          {['来源 / 角色', '参数与条件', '版本与审批', '证据与定位'].map((title) => <th key={title} className="p-2">{title}</th>)}
        </tr></thead>
        <tbody>{claims.map((claim, index) => (
          <tr key={String(claim.claim_id || index)} className="border-t border-[#e3e7f1] align-top">
            <td className="p-2">知识库 {display(claim.source_kb_id)}<br />{ROLES[String(claim.source_role)] || '未知角色'}</td>
            <td className="p-2">{display(claim.display_value || claim.value)}<br />条件：{display(claim.condition)}</td>
            <td className="max-w-[220px] break-all p-2">
              版本：{display(claim.source_version_id)}<br />指纹：{display(claim.source_content_hash)}<br />审批：{display(claim.approval_status)}
            </td>
            <td className="max-w-[220px] break-all p-2">
              {display(claim.evidence_ids)}<br />{display(claim.locator)}
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

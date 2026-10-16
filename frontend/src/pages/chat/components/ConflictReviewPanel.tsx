import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/api/client';
import { createClientRequestId, fetchDocumentConflict, fetchDocumentConflicts, submitDocumentConflictDecision } from '@/api/documentAuthoring';
import type { DocumentConflict, DocumentConflictDecisionInput } from '@/api/types';
import ConflictEvidenceCompare from './ConflictEvidenceCompare';
import { buildConflictDecision, CONFLICT_ACTION_LABELS, CONFLICT_STATUS_LABELS, conflictActionDescription, conflictActionNeedsClaim } from './conflictReviewModel';

type Props = { kbName: string; workOrderId: string; onDecisionSaved?: () => void };
const buttonClass = 'rounded-md border border-[#d5ddeb] bg-white px-3 py-1.5 text-[12px] disabled:opacity-50';

export default function ConflictReviewPanel({ kbName, workOrderId, onDecisionSaved }: Props) {
  const [opened, setOpened] = useState(false);
  const [cases, setCases] = useState<DocumentConflict[]>([]);
  const [selected, setSelected] = useState<DocumentConflict | null>(null);
  const [action, setAction] = useState('');
  const [claimId, setClaimId] = useState('');
  const [rationale, setRationale] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const lock = useRef(false);
  const mounted = useRef(true);
  const pending = useRef<{ signature: string; body: DocumentConflictDecisionInput } | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  function failed(err: unknown) {
    if (!mounted.current) return;
    if (err instanceof ApiError && [401, 403, 404, 409].includes(err.status)) {
      // Revoke old previews as well as the form. A retry must reload the
      // authoritative version instead of applying a choice to new evidence.
      setCases([]); setSelected(null); pending.current = null;
    }
    setError(err instanceof ApiError && err.status === 409
      ? '来源或决策版本已变化，请重新加载并审阅。'
      : err instanceof ApiError && [401, 403].includes(err.status)
        ? '当前无权查看或处理这些来源，请重新核验访问权限。'
        : err instanceof Error ? err.message : '加载或保存失败，请重试');
  }

  async function perform(task: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try { await task(); } catch (err) { failed(err); }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }

  async function reload() {
    setOpened(true); setSelected(null); setCases([]); setNotice('');
    await perform(async () => {
      const rows = await fetchDocumentConflicts(kbName, workOrderId);
      if (mounted.current) setCases(rows);
    });
  }

  async function select(conflictId: string) {
    setSelected(null); setNotice('');
    await perform(async () => {
      const detail = await fetchDocumentConflict(kbName, workOrderId, conflictId);
      if (!mounted.current) return;
      setSelected(detail); setAction(''); setClaimId(''); setRationale(''); pending.current = null;
    });
  }

  async function submit() {
    if (!selected) return;
    await perform(async () => {
      const signature = JSON.stringify([selected.conflict_id, selected.row_version, action, claimId, rationale]);
      if (pending.current?.signature !== signature) {
        pending.current = { signature, body: buildConflictDecision(selected, action, claimId, rationale, createClientRequestId()) };
      }
      const result = await submitDocumentConflictDecision(kbName, workOrderId, selected.conflict_id, pending.current.body);
      if (!mounted.current) return;
      // Clear the old form before any follow-up read. Network failures may
      // safely retry the exact body/key, but never submit a second decision.
      setSelected(null); pending.current = null;
      setNotice(result.status === 'approval_pending' ? '设计建议已保存，仍待正式审批。'
        : result.status === 'deferred' ? '已暂缓，相关章节继续等待。'
          : result.next_action === 'resume_work_order' ? '决策已保存，文档任务已排队继续生成。' : '决策已保存。');
      onDecisionSaved?.();
      const rows = await fetchDocumentConflicts(kbName, workOrderId);
      if (mounted.current) setCases(rows);
    });
  }

  const actions = (selected?.allowed_actions || []).filter((value) => Boolean(CONFLICT_ACTION_LABELS[value]));
  const editable = selected && !['resolved', 'dismissed', 'blocked', 'approval_pending'].includes(selected.status || '');
  return (
    <section className="mt-2 space-y-2 text-[#464c5e]" data-conflict-review-panel={workOrderId}>
      <button type="button" className={buttonClass} disabled={busy} onClick={() => void reload()}>
        {busy ? '正在处理…' : opened ? '重新加载来源冲突' : '查看来源冲突'}
      </button>
      {error && <p role="alert" className="text-[12px] text-[#b42318]">{error}</p>}
      {notice && <p role="status" className="text-[12px]">{notice}</p>}
      {opened && !busy && !error && cases.length === 0 && <p className="text-[12px]">没有待处理的来源冲突。</p>}
      {opened && cases.length > 0 && <div className="space-y-2" aria-label="来源冲突审阅">
        <p className="text-[12px] font-medium">待处理 {cases.length} 项，请逐项核对依据和影响章节。</p>
        <ul className="space-y-1">{cases.map((item) => <li key={item.conflict_id}>
          <button type="button" className={buttonClass} disabled={busy} onClick={() => void select(item.conflict_id)}>
            {item.field_key || item.conflict_id} · {CONFLICT_STATUS_LABELS[item.status || 'open'] || item.status}
          </button>
        </li>)}</ul>
      </div>}
      {selected && <div className="space-y-2 rounded-md border border-[#e3e7f1] bg-white p-3">
        <p className="text-[12px]">{selected.subject} · {selected.field_key} · {selected.severity}</p>
        <p className="text-[12px]">影响章节：{selected.affected_unit_ids?.join('、') || '未指定'}</p>
        <ConflictEvidenceCompare claims={selected.claims || []} />
        {(selected.decisions || []).length > 0 && <ul aria-label="决策历史" className="text-[12px]">
          {selected.decisions!.map((item, index) => {
            const decision = item.decision as { action?: string; rationale?: string } | undefined;
            return <li key={String(item.decision_id || index)}>{String(item.actor_id || '')} · {String(item.created_at || '')} · {CONFLICT_ACTION_LABELS[decision?.action || ''] || decision?.action} · {decision?.rationale} · {CONFLICT_STATUS_LABELS[String(item.status)] || String(item.status || '')}</li>;
          })}
        </ul>}
        {editable && <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
          <label className="block text-[12px]">处理方式
            <select aria-label="冲突处理方式" className="mt-1 block w-full rounded-md border p-2" value={action} disabled={busy} required onChange={(event) => setAction(event.target.value)}>
              <option value="">请选择处理方式</option>
              {actions.map((value) => <option key={value} value={value}>{CONFLICT_ACTION_LABELS[value]}</option>)}
            </select>
          </label>
          {conflictActionNeedsClaim(action) && <label className="block text-[12px]">证据候选
            <select aria-label="证据候选" className="mt-1 block w-full rounded-md border p-2" value={claimId} disabled={busy} required onChange={(event) => setClaimId(event.target.value)}>
              <option value="">请选择证据候选</option>
              {(selected.claims || []).map((claim) => <option key={String(claim.claim_id)} value={String(claim.claim_id)}>知识库 {String(claim.source_kb_id)} · {String(claim.display_value || claim.value || claim.claim_id)}</option>)}
            </select>
          </label>}
          <label className="block text-[12px]">处理理由
            <textarea aria-label="冲突处理理由" className="mt-1 block w-full rounded-md border p-2" maxLength={4000} value={rationale} disabled={busy} onChange={(event) => setRationale(event.target.value)} />
          </label>
          {action && <p className="text-[12px]">{conflictActionDescription(action)}</p>}
          <button type="submit" className={buttonClass} disabled={busy || !action || (conflictActionNeedsClaim(action) && !claimId)}>保存本项决策</button>
        </form>}
      </div>}
    </section>
  );
}

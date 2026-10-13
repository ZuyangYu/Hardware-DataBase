import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '@/api/client';
import { createClientRequestId } from '@/api/documentAuthoring';
import type { QualityCase } from '@/api/types';
import type { QualityReport } from '@/api/workbenchTypes';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { AdminSection } from '@/components/admin/AdminPageShell';
import { EmptyState, ErrorState, LoadingState, RefreshingBadge } from '@/components/admin/StateBlock';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';
import { formatAbsoluteTime } from '@/lib/formatters';
import { QUALITY_RECOMMENDATION_LABELS, ROOT_CAUSE_FIXES, WORKFLOW_STAGES } from '@/lib/qualityLabels';
import { workbenchErrorMessage } from '@/lib/workbenchErrors';

export { default as ReviewRequestApproval } from '@/components/quality/ReviewRequestApproval';

export function AdminRemediationGuide({ detail }: { detail: QualityCase }) {
  const review = detail.reviews.filter((item) => !detail.reviews.some((next) => next.supersedes === item.review_id)).slice(-1)[0];
  const fix = ROOT_CAUSE_FIXES[review?.root_cause ?? 'unknown'] ?? ROOT_CAUSE_FIXES.unknown;
  const stage = WORKFLOW_STAGES[detail.status] ?? { label: detail.status, next: '刷新案例并联系原回答 owner 确认当前处理阶段。' };
  const pendingRequest = detail.review_requests?.find((item) => item.status === 'pending');
  const [requesting, setRequesting] = useState(false);
  const [requested, setRequested] = useState<{ caseId: string; requestId: string } | null>(null);
  const requestBody = useRef<{ row_version: number; client_request_id: string; message: string } | null>(null);
  const waiting = pendingRequest || (requested?.caseId === detail.case_id && !detail.review_requests?.some((item) => item.request_id === requested.requestId));
  const lastRequest = detail.review_requests?.[0];
  async function requestReview() {
    if (requesting) return;
    setRequesting(true);
    try {
      requestBody.current ??= {
        row_version: detail.row_version,
        client_request_id: createClientRequestId(),
        message: '请确认是否共享固定提问、回答和必要证据，安排同部门员工完成复核。',
      };
      const result = await api.post<{ request_id: string }>(`/api/v1/quality/cases/${detail.case_id}/review-requests`, requestBody.current);
      requestBody.current = null;
      setRequested({ caseId: detail.case_id, requestId: result.request_id });
      notify.success('已向原回答用户发起复核请求');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) requestBody.current = null;
      notify.error(error instanceof Error ? error.message : '发起复核请求失败');
    }
    finally { setRequesting(false); }
  }
  return <section className="grid gap-3 rounded-md border border-blue-200 bg-blue-50/60 p-3">
    <h3 className="font-semibold">管理员修复管理</h3>
    <div className="grid gap-1 text-sm">
      <p><span className="font-medium">当前阶段：</span>{stage.label}</p>
      <p><span className="font-medium">下一步：</span>{stage.next}</p>
      <p><span className="font-medium">根因对应动作：</span>{fix.label}：{fix.action}</p>
    </div>
    <div className="grid gap-1 border-t border-blue-200 pt-3 text-sm">
      <p className="font-medium">完成标准</p>
      <ol className="list-decimal space-y-1 pl-5">
        <li>原回答 owner 或同部门授权员工确认参考答案、评分要点和固定证据。</li>
        <li>用同一冻结问题运行基线与候选评测；候选必须通过质量门禁。</li>
        <li>关联修改引用、基线/候选 run ID；发布后用同一问题回访。</li>
        <li>线上回访确认正确后，案例才会进入“已上线验证”。</li>
      </ol>
    </div>
    {detail.content_access === 'hidden' && <div className="grid gap-2 border-t border-blue-200 pt-3 text-sm">
      <p className="font-medium">授权复核</p>
      {lastRequest?.status === 'approved' && <p>上次请求已批准，授权员工可复核；管理员仍只看元数据。</p>}
      {lastRequest?.status === 'rejected' && <p>上次请求已关闭，未新增材料授权。</p>}
      {waiting
        ? <p className="text-blue-800">复核请求已发出，等待原回答用户确认共享范围。</p>
        : <Button variant="outline" disabled={requesting || !detail.source_valid || detail.manifest.content_available === false} onClick={() => void requestReview()}>{requesting ? '发起中…' : '向原回答用户发起复核请求'}</Button>}
    </div>}
    <p className="text-xs text-muted-foreground">管理员负责查看队列、协调负责人、监督门禁和报告；由于正文权限限制，管理员不能直接修改知识库、提交复核或代替 owner 发起正文评测。</p>
  </section>;
}

export function QualityReports() {
  const [reports, setReports] = useState<QualityReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const generationKey = useRef<string | null>(null);
  const load = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.get<QualityReport[]>('/api/v1/quality/reports');
      if (request === sequence.current) setReports(result);
    } catch (failure) {
      if (request === sequence.current) setError(workbenchErrorMessage(failure, '读取报告失败'));
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void load();
    return () => { sequence.current += 1; };
  }, [load]);
  async function generate() {
    setBusy(true);
    setError('');
    try {
      generationKey.current ??= createClientRequestId();
      await api.post('/api/v1/quality/analysis-runs', { client_request_id: generationKey.current });
      generationKey.current = null;
      await load();
    }
    catch (failure) { setError(workbenchErrorMessage(failure, '生成报告失败')); }
    finally { setBusy(false); }
  }
  return <AdminSection
    title="质量与费用分析报告"
    description="首版由代码冻结当前权限范围内的全部历史统计；诊断建议待实验验证。报告未读取 Phoenix，也未调用 LLM 生成说明。"
    actions={<><Button variant="outline" disabled={busy || loading} onClick={() => void load()}>刷新报告</Button><Button variant="outline" disabled={busy || loading} onClick={() => void generate()}>{busy ? '生成中…' : '生成统计报告'}</Button></>}
    bodyClassName="gap-3"
  >
    {error && <ErrorState message={error} onRetry={() => void load()} />}
    {busy && <RefreshingBadge label="正在生成冻结统计报告" />}
    {loading && (!reports.length ? <LoadingState label="正在读取统计报告…" /> : <RefreshingBadge label="正在更新报告，保留上次结果" />)}
    {reports.map((r, index) => <details key={r.report_id} open={index === 0} className="min-w-0 rounded-lg border p-3">
      <summary className="cursor-pointer text-sm">{formatAbsoluteTime(r.created_at)} · {r.verified_statistics.case_count} 案例 · {r.verified_statistics.costs.calls} 调用 · {r.verified_statistics.costs.unknown_cost_calls} 未知费用 <StatusBadge tone="info">已生成（冻结统计）</StatusBadge></summary>
      <div className="my-2 grid min-w-0 gap-2">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">输入版本<CopyableTechnicalValue value={r.input_manifest_sha256} /></div>
        {r.recommendations.map((item) => <div className="my-1 grid gap-1 text-sm break-words" key={item.recommendation_id}>
          <p>{item.hypothesis} <StatusBadge tone={item.status === 'verified' ? 'success' : item.status === 'rejected' ? 'danger' : 'warning'}>{QUALITY_RECOMMENDATION_LABELS[item.status] ?? item.status}</StatusBadge></p>
          <p className="text-xs text-muted-foreground">验证：{item.experiment_plan}</p>
        </div>)}
        {!r.recommendations.length && <EmptyState title="本报告无诊断建议">冻结统计数据不含待验证假设时为空</EmptyState>}
      </div>
    </details>)}
    {!reports.length && !busy && !loading && !error && <EmptyState title="暂无统计报告">点击"生成统计报告"创建首份报告</EmptyState>}
  </AdminSection>;
}

export function CaseWorkflow({ detail, onRefresh }: { detail: QualityCase; onRefresh: () => Promise<void> }) {
  const [versionId, setVersionId] = useState('');
  const [material, setMaterial] = useState<{ samples: unknown[]; snapshots: unknown[] } | null>(null);
  const [changeRef, setChangeRef] = useState('');
  const [baseline, setBaseline] = useState('');
  const [candidate, setCandidate] = useState('');
  const [releaseRef, setReleaseRef] = useState('');
  const [deployedAt, setDeployedAt] = useState('');
  const [validationTurnId, setValidationTurnId] = useState('');
  const [run, setRun] = useState<{ run_id: string; status: string; summary?: unknown; results?: unknown } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setVersionId(''); setMaterial(null); setRun(null); }, [detail.case_id]);
  const review = detail.reviews.filter((r) => r.judgment === 'confirmed' && r.content.reference_answer && !detail.reviews.some((next) => next.supersedes === r.review_id)).slice(-1)[0];
  async function freeze() {
    if (!review) return;
    setBusy(true);
    try {
      const value = await api.post<{ version_id: string }>('/api/v1/quality/datasets/production-repair/versions', { client_request_id: createClientRequestId(), cases: [{ case_id: detail.case_id, review_id: review.review_id, critical: true, split: 'repair' }] });
      setVersionId(value.version_id); setMaterial(null); notify.success('已冻结数据集来源版本');
    } catch (error) { notify.error(error instanceof Error ? error.message : '冻结数据集失败'); }
    finally { setBusy(false); }
  }
  async function readMaterial() {
    setBusy(true);
    try { setMaterial(await api.get(`/api/v1/quality/dataset-versions/${encodeURIComponent(versionId)}?materialize=true`)); }
    catch (error) { setMaterial(null); notify.error(error instanceof Error ? error.message : '固定材料授权已失效'); }
    finally { setBusy(false); }
  }
  async function link() {
    setBusy(true);
    try {
      await api.post(`/api/v1/quality/cases/${detail.case_id}/changes`, { row_version: detail.row_version, dataset_version_id: versionId, change_ref: changeRef, baseline_run_id: baseline, candidate_run_id: candidate, release_ref: releaseRef, deployed_at: deployedAt ? new Date(deployedAt).toISOString() : null, validation_turn_id: validationTurnId || null });
      await onRefresh(); notify.success('已校验并关联回归证据');
    } catch (error) { notify.error(error instanceof Error ? error.message : '关联实验失败'); }
    finally { setBusy(false); }
  }
  async function evaluate(mode: 'offline' | 'online') {
    setBusy(true);
    try {
      const created = await api.post<{ run_id: string; status: string }>(`/api/v1/quality/dataset-versions/${encodeURIComponent(versionId)}/evaluation-runs`, { mode });
      setRun(created);
      if (mode === 'offline') setBaseline(created.run_id); else setCandidate(created.run_id);
      notify.success('已提交固定材料评测');
    } catch (error) { notify.error(error instanceof Error ? error.message : '提交评测失败'); }
    finally { setBusy(false); }
  }
  async function refreshRun() {
    if (!run) return;
    try { setRun(await api.get(`/api/v1/quality/evaluation-runs/${encodeURIComponent(run.run_id)}`)); }
    catch (error) { setRun(null); notify.error(error instanceof Error ? error.message : '读取评测失败'); }
  }
  return <section className="grid gap-3 border-t pt-4">
    <h3 className="font-semibold">固定数据集与修复实验</h3>
    <Button variant="outline" disabled={busy || !review || detail.content.query == null} onClick={() => void freeze()}>冻结已确认案例版本</Button>
    <label className="grid gap-1">数据集版本 ID<input className="rounded border p-2" value={versionId} onChange={(e) => { setVersionId(e.target.value); setMaterial(null); }} /></label>
    <Button variant="outline" disabled={!versionId || busy} onClick={() => void readMaterial()}>读取当前授权的固定评测材料</Button>
    {material && <details><summary>评测样本 / 生产回答快照</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">{JSON.stringify(material, null, 2)}</pre></details>}
    <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={!versionId || busy} onClick={() => void evaluate('offline')}>评分固定生产回答</Button><Button variant="outline" disabled={!versionId || busy || detail.content_access !== 'owner'} onClick={() => void evaluate('online')}>固定证据生成候选回答</Button></div>
    <p className="text-xs text-muted-foreground">候选实验只使用已冻结检索证据，用于回答 Prompt / 模型修复；原始 owner 才能发起。完整检索 / Agent 的来源隔离实验需单独配置。</p>
    {run && <section className="grid gap-2 rounded border p-3"><p className="break-all text-sm">{run.run_id} · {run.status}</p><Button variant="outline" onClick={() => void refreshRun()}>刷新评测进度</Button>{run.status === 'collected' && <Button onClick={async () => { try { await api.post(`/api/v1/quality/evaluation-runs/${run.run_id}/controls/score`, {}); await refreshRun(); } catch (error) { notify.error(error instanceof Error ? error.message : '启动评分失败'); } }}>开始候选评分</Button>}{run.summary != null && <details><summary>门禁与逐样本结果</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ summary: run.summary, results: run.results }, null, 2)}</pre></details>}</section>}
    <label className="grid gap-1">修改引用（commit / issue）<input className="rounded border p-2" value={changeRef} onChange={(e) => setChangeRef(e.target.value)} /></label>
    <label className="grid gap-1">基线 run ID<input className="rounded border p-2" value={baseline} onChange={(e) => setBaseline(e.target.value)} /></label>
    <label className="grid gap-1">候选 run ID<input className="rounded border p-2" value={candidate} onChange={(e) => setCandidate(e.target.value)} /></label>
    <label className="grid gap-1">发布代码 revision（可选）<input className="rounded border p-2" value={releaseRef} onChange={(e) => setReleaseRef(e.target.value)} /></label>
    <label className="grid gap-1">已部署时间（可选）<input type="datetime-local" className="rounded border p-2" value={deployedAt} onChange={(e) => setDeployedAt(e.target.value)} /></label>
    <label className="grid gap-1">上线验证 turn ID（可选）<input className="rounded border p-2" value={validationTurnId} onChange={(e) => setValidationTurnId(e.target.value)} /></label>
    <Button disabled={busy || !versionId || !changeRef || !baseline || !candidate} onClick={() => void link()}>校验并关联回归证据</Button>
  </section>;
}

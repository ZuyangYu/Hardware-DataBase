import { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { createClientRequestId } from '@/api/documentAuthoring';
import type { QualityCase } from '@/api/types';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { AdminSection } from '@/components/admin/AdminPageShell';
import { EmptyState } from '@/components/admin/StateBlock';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';
import { formatAbsoluteTime } from '@/lib/formatters';

type Report = { report_id: string; created_at: string; input_manifest_sha256: string; verified_statistics: { case_count: number; costs: { calls: number; unknown_cost_calls: number } }; recommendations: { recommendation_id: string; hypothesis: string; experiment_plan: string; status: string }[] };

const ROOT_CAUSE_FIXES: Record<string, { label: string; action: string }> = {
  retrieval: { label: '检索', action: '检查召回范围、证据排序和无关材料过滤，修复后用同一固定问题重新检索验证。' },
  parsing: { label: '解析', action: '检查文件解析、表格/原理图索引和字段映射，修复解析结果后重新生成证据。' },
  missing_source: { label: '资料缺失', action: '补充或更新授权知识库资料，记录资料版本，再用同一问题重新回答。' },
  prompt: { label: 'Prompt', action: '调整回答约束、格式要求或缺失信息披露规则，再运行固定数据集对比。' },
  model: { label: '模型', action: '调整模型或参数，并用相同冻结输入执行基线/候选配对评测。' },
  tool: { label: '工具', action: '检查工具调用参数、错误处理和返回内容，修复后重新执行受影响任务。' },
  permission: { label: '权限', action: '检查知识库、部门和共享授权，确认修复后不会扩大数据访问范围。' },
  presentation: { label: '前端呈现', action: '检查回答格式、引用展示、字段完整性和前端渲染规则，再用同一回答标准回归。' },
  unknown: { label: '待确定', action: '先由原用户或同部门授权员工查看固定提问、回答和证据，补充复核结论后再确定修复方向。' },
};
const WORKFLOW_STAGES: Record<string, { label: string; next: string }> = {
  new: { label: '待复核', next: '由原用户共享选定材料，安排同部门员工完成问题判断和根因复核。' },
  needs_information: { label: '待补充材料', next: '联系原用户补充提问、回答或证据授权，然后重新提交复核。' },
  reviewed: { label: '已确认问题', next: '原回答 owner 冻结案例，实施修复并运行基线/候选配对评测。' },
  regression_verified: { label: '回归已验证', next: '确认发布 revision 和部署时间，在线上版本执行同一问题回访。' },
  closed: { label: '已上线验证', next: '闭环完成；继续通过统计报告观察同类反馈和费用影响。' },
  rejected: { label: '复核未发现问题', next: '保留复核依据；如用户仍有异议，由原用户补充新的反馈材料。' },
};

export function AdminRemediationGuide({ detail }: { detail: QualityCase }) {
  const review = detail.reviews.filter((item) => !detail.reviews.some((next) => next.supersedes === item.review_id)).slice(-1)[0];
  const fix = ROOT_CAUSE_FIXES[review?.root_cause ?? 'unknown'] ?? ROOT_CAUSE_FIXES.unknown;
  const stage = WORKFLOW_STAGES[detail.status] ?? { label: detail.status, next: '刷新案例并联系原回答 owner 确认当前处理阶段。' };
  return <section className="grid gap-3 rounded-md border border-blue-200 bg-blue-50/60 p-3 dark:border-blue-900 dark:bg-blue-950/20">
    <h3 className="font-semibold">管理员修复管理</h3>
    <div className="grid gap-1 text-sm">
      <p><span className="font-medium">当前阶段：</span>{stage.label}</p>
      <p><span className="font-medium">下一步：</span>{stage.next}</p>
      <p><span className="font-medium">根因对应动作：</span>{fix.label}：{fix.action}</p>
    </div>
    <div className="grid gap-1 border-t border-blue-200 pt-3 text-sm dark:border-blue-900">
      <p className="font-medium">完成标准</p>
      <ol className="list-decimal space-y-1 pl-5">
        <li>原回答 owner 或同部门授权员工确认参考答案、评分要点和固定证据。</li>
        <li>用同一冻结问题运行基线与候选评测；候选必须通过质量门禁。</li>
        <li>关联修改引用、基线/候选 run ID；发布后用同一问题回访。</li>
        <li>线上回访确认正确后，案例才会进入“已上线验证”。</li>
      </ol>
    </div>
    <p className="text-xs text-muted-foreground">管理员负责查看队列、协调负责人、监督门禁和报告；由于正文权限限制，管理员不能直接修改知识库、提交复核或代替 owner 发起正文评测。</p>
  </section>;
}

export function QualityReports() {
  const [reports, setReports] = useState<Report[]>([]);
  const [busy, setBusy] = useState(false);
  async function load() {
    try { setReports(await api.get('/api/v1/quality/reports')); }
    catch (error) { notify.error(error instanceof Error ? error.message : '读取报告失败'); }
  }
  useEffect(() => { void load(); }, []);
  async function generate() {
    setBusy(true);
    try { await api.post('/api/v1/quality/analysis-runs', { client_request_id: createClientRequestId() }); await load(); }
    catch (error) { notify.error(error instanceof Error ? error.message : '生成报告失败'); }
    finally { setBusy(false); }
  }
  return <AdminSection
    title="质量与费用分析报告"
    description="首版由代码冻结当前权限范围内的全部历史统计；诊断建议待实验验证。报告未读取 Phoenix，也未调用 LLM 生成说明。"
    actions={<Button variant="outline" disabled={busy} onClick={() => void generate()}>{busy ? '生成中…' : '生成统计报告'}</Button>}
    bodyClassName="gap-3"
  >
    {reports.map((r, index) => <details key={r.report_id} open={index === 0} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm">{formatAbsoluteTime(r.created_at)} · {r.verified_statistics.case_count} 案例 · {r.verified_statistics.costs.calls} 调用 · {r.verified_statistics.costs.unknown_cost_calls} 未知费用</summary><div className="my-2 grid gap-2"><div className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">输入版本<CopyableTechnicalValue value={r.input_manifest_sha256} /></div>{r.recommendations.map((item) => <div className="my-1 grid gap-1 text-sm" key={item.recommendation_id}><p>{item.hypothesis} <StatusBadge tone={({ verified: 'success', in_progress: 'warning', rejected: 'danger' } as const)[item.status] ?? 'neutral'}>{item.status}</StatusBadge></p><p className="text-xs text-muted-foreground">验证：{item.experiment_plan}</p></div>)}{!r.recommendations.length && <EmptyState title="本报告无诊断建议">冻结统计数据不含待验证假设时为空</EmptyState>}</div></details>)}
    {!reports.length && !busy && <EmptyState title="暂无统计报告">点击"生成统计报告"创建首份报告</EmptyState>}
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

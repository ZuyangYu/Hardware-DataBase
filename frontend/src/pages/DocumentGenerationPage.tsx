import { useCallback, useEffect, useState } from 'react';

import { api, apiDownload, uploadFiles } from '../api/client';
import type {
  CreateWorkOrderResult,
  DocumentAnalysis,
  GenerationOptions,
  IcdScopeReview,
  TemplateVersion,
  WorkOrder,
  WorkOrderStatus,
} from '../api/types';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import type { AuthSession } from '../auth';

type Props = { auth: AuthSession; onLogout: () => void };

const RUNNING = ['retrieving', 'ready_to_draft', 'drafting', 'validating', 'rendering'];

export default function DocumentGenerationPage({ auth, onLogout }: Props) {
  const [section, setSection] = useState<'templates' | 'create' | 'runs'>('templates');
  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex gap-2">
        {(['templates', 'create', 'runs'] as const).map((key) => (
          <Button key={key} variant={section === key ? 'default' : 'outline'} onClick={() => setSection(key)}>
            {key === 'templates' ? '上传模板' : key === 'create' ? '新建生成任务' : '任务与下载'}
          </Button>
        ))}
      </div>
      {section === 'templates' && <TemplateSection />}
      {section === 'create' && <CreateSection />}
      {section === 'runs' && <RunsSection />}
    </div>
  );
}

function TemplateSection() {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [analysis, setAnalysis] = useState<DocumentAnalysis | null>(null);
  const [kb, setKb] = useState('');

  async function analyze() {
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    form.append('template_name', name || file.name);
    try {
      const a = await uploadFiles<DocumentAnalysis>(
        `/api/v1/document-generation/templates/analyze?kb=${encodeURIComponent(kb)}`,
        form,
      );
      setAnalysis(a);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '分析模板失败');
    }
  }

  async function confirm() {
    if (!analysis) return;
    try {
      await api.post(`/api/v1/document-generation/templates/${analysis.analysis_id}/confirm?kb=${encodeURIComponent(kb)}`, {
        display_name: name || analysis.format,
      });
      notify.success('已启用受控模板');
      setAnalysis(null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '启用模板失败');
    }
  }

  return (
    <Card>
      <CardHeader><CardTitle>上传并分析受控模板</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <Input placeholder="知识库名称" value={kb} onChange={(e) => setKb(e.target.value)} />
        <Input type="file" accept=".xlsx,.xlsm,.docx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <Input placeholder="模板名称" value={name} onChange={(e) => setName(e.target.value)} />
        <Button onClick={analyze} disabled={!file || !kb}>分析模板</Button>
        {analysis && (
          <div className="space-y-2 rounded-md border p-3 text-sm">
            <p>分析 ID：{analysis.analysis_id}；格式：{analysis.format}；状态：{analysis.status}</p>
            {analysis.status === 'ready_for_confirmation' && (
              <Button onClick={confirm}>确认并启用模板</Button>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function CreateSection() {
  const [kb, setKb] = useState('');
  const [options, setOptions] = useState<GenerationOptions | null>(null);
  const [templateId, setTemplateId] = useState('');
  const [schemaKey, setSchemaKey] = useState('');

  useEffect(() => {
    if (!kb) return;
    let cancelled = false;
    api.get<GenerationOptions>(`/api/v1/document-generation/options?kb=${encodeURIComponent(kb)}`)
      .then((o) => { if (!cancelled) setOptions(o); })
      .catch((e) => notify.error(e instanceof Error ? e.message : '读取配置失败'));
    return () => { cancelled = true; };
  }, [kb]);

  const templates = options?.templates ?? [];
  const selected = templates.find((t) => t.template_version_id === templateId);
  const schemas = (options?.schemas ?? []).filter(
    (s) => selected && s.document_schema_id === selected.template_schema_id && s.version === selected.template_schema_version,
  );

  async function create() {
    const [docSchemaId, docSchemaVersion] = (schemaKey || '').split('@');
    if (!templateId || !docSchemaId) return;
    try {
      const result = await api.post<CreateWorkOrderResult>(
        `/api/v1/document-generation/work-orders?kb=${encodeURIComponent(kb)}`,
        { template_version_id: templateId, document_schema_id: docSchemaId, document_schema_version: docSchemaVersion },
      );
      if (result.stage === 'ready') {
        await api.post(`/api/v1/document-generation/work-orders/${result.work_order_id}/generate?kb=${encodeURIComponent(kb)}`, {});
        notify.success(`已创建并开始生成：${result.work_order_id}`);
      } else {
        notify.info(`已创建工作单 ${result.work_order_id}，需人工处理（${result.stage}）`);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建任务失败');
    }
  }

  return (
    <Card>
      <CardHeader><CardTitle>新建生成任务</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <Input placeholder="知识库名称" value={kb} onChange={(e) => setKb(e.target.value)} />
        {templates.length > 0 && (
          <select className="w-full rounded-md border px-3 py-2" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
            <option value="">选择模板</option>
            {templates.map((t) => <option key={t.template_version_id} value={t.template_version_id}>{t.template_id}</option>)}
          </select>
        )}
        {schemas.length > 0 && (
          <select className="w-full rounded-md border px-3 py-2" value={schemaKey} onChange={(e) => setSchemaKey(e.target.value)}>
            <option value="">选择 Schema</option>
            {schemas.map((s) => <option key={`${s.document_schema_id}@${s.version}`} value={`${s.document_schema_id}@${s.version}`}>{s.document_schema_id}@{s.version}</option>)}
          </select>
        )}
        <Button onClick={create} disabled={!templateId || !schemaKey || !kb}>创建生成任务</Button>
      </CardContent>
    </Card>
  );
}

function RunsSection() {
  const [kb, setKb] = useState('');
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [selected, setSelected] = useState('');
  const [status, setStatus] = useState<WorkOrderStatus | null>(null);
  const [review, setReview] = useState<IcdScopeReview>({ exceptions: [], status: '' });

  const loadOrders = useCallback(() => {
    if (!kb) return;
    api.get<WorkOrder[]>(`/api/v1/document-generation/work-orders?kb=${encodeURIComponent(kb)}`)
      .then(setOrders)
      .catch((e) => notify.error(e instanceof Error ? e.message : '加载工作单失败'));
  }, [kb]);

  useEffect(() => { loadOrders(); }, [loadOrders]);

  const running = status?.status ? RUNNING.includes(status.status) : false;

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = () => {
      api.get<WorkOrderStatus>(`/api/v1/document-generation/work-orders/${selected}/status?kb=${encodeURIComponent(kb)}`)
        .then((s) => {
          if (cancelled) return;
          setStatus(s);
          if (s.status && RUNNING.includes(s.status)) timer = window.setTimeout(poll, 2000);
        })
        .catch((e) => notify.error(e instanceof Error ? e.message : '读取状态失败'));
    };
    poll();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [selected, kb]);

  useEffect(() => {
    if (!selected) return;
    api.get<IcdScopeReview>(`/api/v1/document-generation/work-orders/${selected}/icd-scope-review?kb=${encodeURIComponent(kb)}`)
      .then(setReview)
      .catch(() => setReview(null));
  }, [selected, kb]);

  return (
    <Card>
      <CardHeader><CardTitle>任务与下载</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <Input placeholder="知识库名称" value={kb} onChange={(e) => setKb(e.target.value)} />
        {orders.length > 0 && (
          <select className="w-full rounded-md border px-3 py-2" value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">选择工作单</option>
            {orders.map((o) => <option key={o.work_order_id} value={o.work_order_id}>{o.work_order_id}（{o.status}）</option>)}
          </select>
        )}
        {status && <StatusView status={status} kb={kb} />}
        {review && (review.exceptions?.length ?? 0) > 0 && <ScopeReviewView review={review} kb={kb} workOrderId={selected} />}
        {running && <p className="text-sm text-muted-foreground">正在生成，自动刷新中…</p>}
      </CardContent>
    </Card>
  );
}

function StatusView({ status, kb }: { status: WorkOrderStatus; kb: string }) {
  return (
    <div className="space-y-2 rounded-md border p-3 text-sm">
      <p>状态：{status.status}；格式：{status.target_format ?? '-'}</p>
      {status.harness_run && (
        <p>节点：{status.harness_run.current_node ?? '-'}；错误：{status.harness_run.error ?? '-'}</p>
      )}
      {status.artifacts.map((a) => (
        <div key={a.artifact_id} className="flex items-center gap-2">
          <span>{String(a.artifact_id)}</span>
          <Button size="sm" onClick={() => void apiDownload.blob(
            `/api/v1/document-generation/artifacts/${a.artifact_id}/download?kb=${encodeURIComponent(kb)}`,
            `${a.artifact_id}.${status.target_format ?? 'bin'}`,
          )}>下载</Button>
        </div>
      ))}
    </div>
  );
}

function ScopeReviewView({ review, kb, workOrderId }: { review: IcdScopeReview; kb: string; workOrderId: string }) {
  const [comment, setComment] = useState('');
  const exceptions = Array.isArray(review?.exceptions) ? review.exceptions : [];
  if (exceptions.length === 0) return null;
  return (
    <div className="space-y-2 rounded-md border p-3 text-sm">
      <p className="font-semibold">ICD 范围异常待办</p>
      {exceptions.map((ex, i) => (
        <div key={ex.exception_id ?? i}>发现：{ex.kind ?? '-'}</div>
      ))}
      <Label>处理说明</Label>
      <Textarea value={comment} onChange={(e) => setComment(e.target.value)} />
      <Button size="sm" onClick={() => {
        const resolutions = exceptions.map((ex) => ({ exception_id: ex.exception_id, action: 'include' }));
        void api.post(
          `/api/v1/document-generation/work-orders/${workOrderId}/icd-scope-resolution?kb=${encodeURIComponent(kb)}`,
          { resolutions, comment },
        ).then(() => notify.success('已应用范围处理并继续生成'))
          .catch((e) => notify.error(e instanceof Error ? e.message : '应用范围处理失败'));
      }}>应用处理结果并继续生成</Button>
    </div>
  );
}
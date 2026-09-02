import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { api, apiDownload } from '../api/client';
import { analyzeTemplate } from '../api/documentAuthoring';
import type {
  CreateWorkOrderResult,
  DocumentAnalysis,
  GenerationSession,
  GenerationOptions,
  IcdScopeReview,
  KbView,
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
import {
  ClarificationPanel,
  DocumentGenerationWorkbench,
  RunStatusPanel,
} from './documentGenerationWorkbench';
import {
  describeHarnessProgress,
  hasDocumentGenerationWritePermission,
  resolveDeepLinkKb,
  resolveDocumentPhase,
  type DocumentGenerationPhase,
} from './documentGenerationModel';
import {
  confirmedBriefItems,
  latestClarificationQuestion,
  pendingBriefItems,
  sessionMessagesForWorkbench,
} from './documentGenerationSession';

type Props = { auth: AuthSession; kbs: KbView[]; onLogout: () => void };

const RUNNING = ['retrieving', 'ready_to_draft', 'drafting', 'validating', 'rendering'];

const PERMISSION_LABELS: Record<string, string> = {
  read: '可读',
  write: '可写',
  admin: '可管理',
};

const REASON_LABELS: Record<string, string> = {
  fixed_label_target: '固定标签不能作为填充目标',
  table_header_target: '表头不能作为填充目标',
  missing_semantic_context: '未能自动获取足够语义上下文',
  low_mapping_confidence: '部分字段映射置信度偏低',
  layout_blank_target: '存在空白布局目标单元',
  nonempty_target_not_placeholder: '部分目标单元格非空且非占位',
  destructive_target_ratio: '目标覆盖比例较高',
  mapping_conflict: '存在映射冲突',
  scalar_target_fanout: '标量字段存在多目标映射',
  repeating_table_requires_schema: '重复表格需要显式 schema',
};

/**
 * 知识库下拉选择:候选项来自 /api/v1/kbs(后端按用户权限过滤),
 * 替代原先的手填"知识库名称",避免拼写错误与越权猜测。
 */
function KbSelect({ kbs, value, onChange }: { kbs: KbView[]; value: string; onChange: (v: string) => void }) {
  return (
    <select
      className="w-full rounded-md border px-3 py-2"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={kbs.length === 0}
    >
      <option value="">{kbs.length === 0 ? '暂无可访问知识库' : '选择知识库'}</option>
      {kbs.map((kb) => (
        <option key={`${kb.kb_id ?? 'none'}:${kb.name}`} value={kb.name}>
          {kb.name}{kb.permission ? `（${PERMISSION_LABELS[kb.permission] ?? kb.permission}）` : ''}
        </option>
      ))}
    </select>
  );
}

/**
 * 深链 kb 预选清理:仅当本地选中值仍等于深链预选(用户未改过)且 kbs 已
 * 加载成功非空时,预选不可用才清空。提示由页面层统一发出,避免重复 toast。
 */
function useDropInvalidDeepLinkKb(
  kb: string,
  setKb: (v: string) => void,
  initialKb: string,
  kbs: KbView[],
) {
  useEffect(() => {
    if (!kb || kb !== initialKb || kbs.length === 0) return;
    if (resolveDeepLinkKb(kb, kbs)) return;
    setKb('');
  }, [kb, setKb, initialKb, kbs]);
}

export default function DocumentGenerationPage({ auth, kbs, onLogout }: Props) {
  const [searchParams, setSearchParams] = useSearchParams();
  // 深链 /document-generation?kb=...&workOrder=... 仅在首次挂载消费一次,
  // 随后清掉查询参数,避免刷新页面时重放预选。
  const [deepLink] = useState(() => ({
    kb: searchParams.get('kb') ?? '',
    workOrder: searchParams.get('workOrder') ?? '',
  }));
  const [section, setSection] = useState<'templates' | 'create' | 'runs'>(() =>
    deepLink.workOrder ? 'runs' : 'templates',
  );
  const [runPhase, setRunPhase] = useState<DocumentGenerationPhase>('retrieving');
  const hasDeepLink = Boolean(deepLink.kb || deepLink.workOrder);
  useEffect(() => {
    if (!hasDeepLink) return;
    // 只清掉深链自己的 kb/workOrder 两个键,未来无关的查询参数保留不动。
    const next = new URLSearchParams(searchParams);
    next.delete('kb');
    next.delete('workOrder');
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // 深链 kb 校验:kbs 加载成功且非空后校验一次,不可用则提示并忽略预选;
  // kbs 为空(加载中或加载失败)时保留预选,避免误清。实际清空在各分区的
  // useDropInvalidDeepLinkKb 中进行(那里持有 kb 状态),提示在这里只发一次。
  useEffect(() => {
    if (!deepLink.kb || kbs.length === 0) return;
    if (resolveDeepLinkKb(deepLink.kb, kbs)) return;
    notify.warning('深链指定的知识库不可用，已忽略预选');
  }, [deepLink.kb, kbs]);
  const activePhase: DocumentGenerationPhase = section === 'templates'
    ? 'analyzing_template'
    : section === 'create'
      ? 'needs_clarification'
      : runPhase;
  return (
    <div className="mx-auto max-w-[1500px] space-y-5 p-4 sm:p-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">Document authoring</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">文档生成工作台</h1>
          <p className="mt-1 text-sm text-muted-foreground">从模板分析、需求确认到检索生成和安全交付，全程可恢复。</p>
        </div>
        <div className="flex flex-wrap gap-2 rounded-xl border bg-card p-1.5 shadow-sm" aria-label="工作台视图">
          {(['templates', 'create', 'runs'] as const).map((key) => (
            <Button key={key} size="sm" variant={section === key ? 'default' : 'ghost'} onClick={() => setSection(key)}>
              {key === 'templates' ? '上传模板' : key === 'create' ? '需求与生成' : '任务与下载'}
            </Button>
          ))}
        </div>
      </header>

      <DocumentGenerationWorkbench
        activePhase={activePhase}
        inspector={(
          <Card>
            <CardHeader><CardTitle className="text-base">模板与证据摘要</CardTitle></CardHeader>
            <CardContent className="space-y-3 text-sm">
              <div className="rounded-lg border bg-muted/20 p-3">
                <p className="font-medium">当前工作区</p>
                <p className="mt-1 text-muted-foreground">
                  {section === 'templates' && '上传模板后显示结构、字段和映射风险。'}
                  {section === 'create' && '确认模板、生成范围和缺失数据策略。'}
                  {section === 'runs' && '选择任务后显示字段进度、证据与校验结果。'}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-lg border p-2"><p className="text-muted-foreground">知识库</p><p className="mt-1 font-medium">{kbs.length} 个可访问</p></div>
                <div className="rounded-lg border p-2"><p className="text-muted-foreground">自动策略</p><p className="mt-1 font-medium">校验通过后继续</p></div>
              </div>
            </CardContent>
          </Card>
        )}
      >
        {section === 'templates' && <TemplateSection kbs={kbs} initialKb={deepLink.kb} />}
        {section === 'create' && <CreateSection kbs={kbs} initialKb={deepLink.kb} />}
        {section === 'runs' && (
          <RunsSection kbs={kbs} onPhaseChange={setRunPhase} initialKb={deepLink.kb} initialWorkOrderId={deepLink.workOrder} />
        )}
      </DocumentGenerationWorkbench>
    </div>
  );
}

function TemplateSection({ kbs, initialKb = '' }: { kbs: KbView[]; initialKb?: string }) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [analysis, setAnalysis] = useState<DocumentAnalysis | null>(null);
  const [kb, setKb] = useState(initialKb);
  const [analyzing, setAnalyzing] = useState(false);

  // 只有一个可访问知识库时自动选中，避免漏选导致按钮一直 disabled、点击无反应。
  useEffect(() => {
    if (!kb && kbs.length === 1) setKb(kbs[0].name);
  }, [kb, kbs]);
  useDropInvalidDeepLinkKb(kb, setKb, initialKb, kbs);

  async function analyze() {
    if (!file || analyzing) return;
    setAnalyzing(true);
    try {
      const a = await analyzeTemplate(kb, file, name);
      setAnalysis(a);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '分析模板失败');
    } finally {
      setAnalyzing(false);
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
        <KbSelect kbs={kbs} value={kb} onChange={setKb} />
        <Input type="file" accept=".xlsx,.xlsm,.docx" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <Input placeholder="模板名称" value={name} onChange={(e) => setName(e.target.value)} />
        <Button onClick={analyze} disabled={!file || !kb || analyzing}>
          {analyzing ? '分析中…（结构分析 + 分批 LLM 语义建议，大模板可能数分钟）' : '分析模板'}
        </Button>
        {!kb && (
          <p className="text-sm text-muted-foreground">
            {kbs.length === 0 ? '当前账号无可访问的知识库，请联系管理员授权后再试。' : '请先选择知识库。'}
          </p>
        )}
        {kb && !file && <p className="text-sm text-muted-foreground">请先选择模板文件（.xlsx/.xlsm/.docx）。</p>}
        {analysis && (
          <div className="space-y-2 rounded-md border p-3 text-sm">
            <p>分析 ID：{analysis.analysis_id}；格式：{analysis.format}；状态：{analysis.status}</p>
            {analysis.auto_activated && <p className="text-green-700">已采纳 AI 推荐并自动启用模板；分析告警已留存审计记录。</p>}
            {analysis.status === 'ready_for_confirmation' && !analysis.auto_activated && (
              <Button onClick={confirm}>确认并启用模板</Button>
            )}
            {analysis.status === 'requires_human' && (
              <div className="space-y-2">
                <p className="text-destructive">
                  AI 未生成可自动启用的字段映射：
                  {(analysis.reason_codes ?? []).map((c) => REASON_LABELS[c] ?? c).join('；') || '原因未知'}
                </p>
                <p className="text-muted-foreground">
                  已保留分析记录；请重试分析或调整模板后重新上传。
                </p>
                <Button variant="outline" onClick={analyze} disabled={!file || !kb || analyzing}>
                  {analyzing ? '分析中…' : '重试分析'}
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function CreateSection({ kbs, initialKb = '' }: { kbs: KbView[]; initialKb?: string }) {
  const [kb, setKb] = useState(initialKb);
  const [options, setOptions] = useState<GenerationOptions | null>(null);
  const [templateId, setTemplateId] = useState('');
  const [schemaKey, setSchemaKey] = useState('');
  const [creating, setCreating] = useState(false);
  const [session, setSession] = useState<GenerationSession | null>(null);
  const [reply, setReply] = useState('');

  // 只有一个可访问知识库时自动选中，避免漏选导致按钮一直 disabled、点击无反应。
  useEffect(() => {
    if (!kb && kbs.length === 1) setKb(kbs[0].name);
  }, [kb, kbs]);
  useDropInvalidDeepLinkKb(kb, setKb, initialKb, kbs);

  useEffect(() => {
    if (!kb) return;
    let cancelled = false;
    api.get<GenerationOptions>(`/api/v1/document-generation/options?kb=${encodeURIComponent(kb)}`)
      .then((o) => { if (!cancelled) setOptions(o); })
      .catch((e) => notify.error(e instanceof Error ? e.message : '读取配置失败'));
    return () => { cancelled = true; };
  }, [kb]);

  useEffect(() => {
    setSession(null);
    setReply('');
  }, [kb, templateId, schemaKey]);

  const templates = options?.templates ?? [];
  const selected = templates.find((t) => t.template_version_id === templateId);
  const canChangeGeneration = hasDocumentGenerationWritePermission(
    kbs.find((item) => item.name === kb)?.permission,
  );
  const schemas = (options?.schemas ?? []).filter(
    (s) => selected && s.document_schema_id === selected.template_schema_id && s.version === selected.template_schema_version,
  );

  async function startClarification() {
    if (!canChangeGeneration || !kb || !templateId || !schemaKey || !reply.trim() || creating) return;
    setCreating(true);
    try {
      const created = await api.post<GenerationSession>(
        `/api/v1/document-generation/sessions?kb=${encodeURIComponent(kb)}`,
        { template_version_id: templateId, purpose: reply.trim() },
      );
      setSession(created);
      setReply('');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '启动需求澄清失败');
    } finally {
      setCreating(false);
    }
  }

  async function answerClarification(selectedAnswer?: string) {
    if (!canChangeGeneration || !session || creating) return;
    const question = latestClarificationQuestion(session);
    const answer = (selectedAnswer ?? reply).trim();
    if (!question?.question_id || !answer) return;
    setCreating(true);
    try {
      const updated = await api.post<GenerationSession>(
        `/api/v1/document-generation/sessions/${session.session_id}/messages?kb=${encodeURIComponent(kb)}`,
        { question_id: question.question_id, answer },
      );
      setSession(updated);
      setReply('');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交需求回答失败');
    } finally {
      setCreating(false);
    }
  }

  async function confirmAndCreate() {
    const [docSchemaId, docSchemaVersion] = (schemaKey || '').split('@');
    if (!canChangeGeneration || !session || !templateId || !docSchemaId || creating) return;
    setCreating(true);
    try {
      const confirmed = await api.post<GenerationSession>(
        `/api/v1/document-generation/sessions/${session.session_id}/confirm?kb=${encodeURIComponent(kb)}`,
        {},
      );
      setSession(confirmed);
      const result = await api.post<CreateWorkOrderResult>(
        `/api/v1/document-generation/work-orders?kb=${encodeURIComponent(kb)}`,
        {
          template_version_id: templateId,
          document_schema_id: docSchemaId,
          document_schema_version: docSchemaVersion,
          generation_session_id: confirmed.session_id,
        },
      );
      if (result.stage === 'ready') {
        await api.post(`/api/v1/document-generation/work-orders/${result.work_order_id}/generate?kb=${encodeURIComponent(kb)}`, {});
        notify.success(`需求已确认，开始生成：${result.work_order_id}`);
      } else {
        notify.info(`已创建工作单 ${result.work_order_id}，生成前需处理：${result.stage}`);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建任务失败');
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader><CardTitle>生成范围</CardTitle></CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-3">
          <KbSelect kbs={kbs} value={kb} onChange={setKb} />
          <select
            className="w-full rounded-md border px-3 py-2"
            value={templateId}
            onChange={(e) => setTemplateId(e.target.value)}
            disabled={templates.length === 0}
          >
            <option value="">{templates.length === 0 ? '暂无已启用模板' : '选择模板'}</option>
            {templates.map((t) => <option key={t.template_version_id} value={t.template_version_id}>{t.template_id}</option>)}
          </select>
          <select
            className="w-full rounded-md border px-3 py-2"
            value={schemaKey}
            onChange={(e) => setSchemaKey(e.target.value)}
            disabled={schemas.length === 0}
          >
            <option value="">{schemas.length === 0 ? '请先选择匹配模板' : '选择 Schema'}</option>
            {schemas.map((s) => <option key={`${s.document_schema_id}@${s.version}`} value={`${s.document_schema_id}@${s.version}`}>{s.document_schema_id}@{s.version}</option>)}
          </select>
        </CardContent>
      </Card>

      <ClarificationPanel
        messages={session
          ? sessionMessagesForWorkbench(session)
          : [{
              id: 'clarification-welcome',
              role: 'assistant',
              content: '选择模板和 Schema 后，请描述文档用途。我会逐项确认版本范围、缺失数据处理和 AI 推断边界。',
            }]}
        reply={reply}
        confirmedItems={session ? confirmedBriefItems(session.brief) : []}
        pendingItems={session
          ? pendingBriefItems(session.brief)
          : ['模板与 Schema', '文档用途', '项目版本', '缺失数据处理', 'AI 推断策略']}
        readyToConfirm={Boolean(
          session
          && session.status === 'needs_clarification'
          && pendingBriefItems(session.brief).length === 0
        )}
        sending={creating}
        disabled={Boolean(kb) && !canChangeGeneration}
        onReplyChange={canChangeGeneration ? setReply : undefined}
        onSend={canChangeGeneration ? (session ? () => void answerClarification() : () => void startClarification()) : undefined}
        onSelectOption={canChangeGeneration ? (value) => void answerClarification(value) : undefined}
        onConfirm={canChangeGeneration ? () => void confirmAndCreate() : undefined}
      />
      {!kb && <p className="text-sm text-muted-foreground">请先选择知识库。</p>}
      {kb && !canChangeGeneration && (
        <p className="text-sm text-amber-700">当前账号对该知识库仅可查看；请申请“可写”权限后再进行需求澄清和文档生成。</p>
      )}
      {kb && (!templateId || !schemaKey) && (
        <p className="text-sm text-muted-foreground">选择模板和匹配的 Schema 后即可开始需求对话。</p>
      )}
    </div>
  );
}

function RunsSection({
  kbs,
  onPhaseChange,
  initialKb = '',
  initialWorkOrderId = '',
}: {
  kbs: KbView[];
  onPhaseChange: (phase: DocumentGenerationPhase) => void;
  initialKb?: string;
  initialWorkOrderId?: string;
}) {
  const [kb, setKb] = useState(initialKb);
  const [orders, setOrders] = useState<WorkOrder[]>([]);
  const [selected, setSelected] = useState(initialWorkOrderId);
  const [status, setStatus] = useState<WorkOrderStatus | null>(null);
  const [review, setReview] = useState<IcdScopeReview>({ exceptions: [], status: '' });
  const [actionBusy, setActionBusy] = useState(false);

  // 只有一个可访问知识库时自动选中，否则 loadOrders 因 !kb 直接 return，工作单列表恒为空。
  useEffect(() => {
    if (!kb && kbs.length === 1) setKb(kbs[0].name);
  }, [kb, kbs]);
  useDropInvalidDeepLinkKb(kb, setKb, initialKb, kbs);

  const loadOrders = useCallback(() => {
    if (!kb) return;
    api.get<WorkOrder[]>(`/api/v1/document-generation/work-orders?kb=${encodeURIComponent(kb)}`)
      .then(setOrders)
      .catch((e) => notify.error(e instanceof Error ? e.message : '加载工作单失败'));
  }, [kb]);

  useEffect(() => { loadOrders(); }, [loadOrders]);

  const loadStatus = useCallback(async () => {
    if (!selected || !kb) return null;
    const next = await api.get<WorkOrderStatus>(
      `/api/v1/document-generation/work-orders/${selected}/status?kb=${encodeURIComponent(kb)}`,
    );
    setStatus(next);
    onPhaseChange(resolveDocumentPhase(next));
    return next;
  }, [kb, onPhaseChange, selected]);

  const running = status?.status ? RUNNING.includes(status.status) : false;

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = () => {
      loadStatus()
        .then((s) => {
          if (cancelled) return;
          if (s?.status && RUNNING.includes(s.status)) timer = window.setTimeout(poll, 2000);
        })
        .catch((e) => notify.error(e instanceof Error ? e.message : '读取状态失败'));
    };
    poll();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [loadStatus, selected]);

  async function runLifecycleAction(action: 'pause' | 'resume' | 'cancel' | 'delete') {
    if (!status || actionBusy) return;
    const runId = status.harness_run?.run_id;
    if ((action === 'pause' || action === 'cancel') && !runId) return;
    if (action === 'cancel' && !window.confirm('确认取消任务？已生成内容、证据和日志会保留，可用于排查或重新运行。')) return;
    if (action === 'delete' && !window.confirm('确认删除任务？将删除生成文件和中间数据，但保留最小审计记录；此操作不可恢复。')) return;
    setActionBusy(true);
    try {
      if (action === 'pause') {
        await api.post(`/api/v1/document-generation/harness-runs/${runId}/pause?kb=${encodeURIComponent(kb)}`, {});
      } else if (action === 'resume') {
        await api.post(`/api/v1/document-generation/work-orders/${status.work_order_id}/resume?kb=${encodeURIComponent(kb)}`, {});
      } else if (action === 'cancel') {
        await api.post(`/api/v1/document-generation/harness-runs/${runId}/cancel?kb=${encodeURIComponent(kb)}`, {});
      } else {
        await api.delete(`/api/v1/document-generation/work-orders/${status.work_order_id}?kb=${encodeURIComponent(kb)}`, { reason: '用户确认删除' });
        setSelected('');
        setStatus(null);
        onPhaseChange('draft');
      }
      await loadOrders();
      if (action !== 'delete') await loadStatus();
      notify.success(action === 'pause' ? '任务已暂停，可随时继续。' : action === 'resume' ? '任务已继续生成。' : action === 'cancel' ? '任务已取消，已保留生成记录。' : '任务及其生成数据已删除，审计记录已保留。');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '任务操作失败');
    } finally {
      setActionBusy(false);
    }
  }

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
        <KbSelect kbs={kbs} value={kb} onChange={setKb} />
        {!kb && (
          <p className="text-sm text-muted-foreground">
            {kbs.length === 0 ? '当前账号无可访问的知识库，请联系管理员授权后再试。' : '请先选择知识库以加载工作单。'}
          </p>
        )}
        {selected && <p className="text-sm text-muted-foreground">已从会话预选工作单 {selected}</p>}
        {orders.length > 0 && (
          <select className="w-full rounded-md border px-3 py-2" value={selected} onChange={(e) => setSelected(e.target.value)}>
            <option value="">选择工作单</option>
            {orders.map((o) => <option key={o.work_order_id} value={o.work_order_id}>{o.work_order_id}（{o.status}）</option>)}
          </select>
        )}
        {status && <StatusView
          status={status}
          kb={kb}
          actionBusy={actionBusy}
          onPause={() => void runLifecycleAction('pause')}
          onResume={() => void runLifecycleAction('resume')}
          onCancel={() => void runLifecycleAction('cancel')}
          onDelete={() => void runLifecycleAction('delete')}
        />}
        {review && (review.exceptions?.length ?? 0) > 0 && <ScopeReviewView review={review} kb={kb} workOrderId={selected} />}
        {running && <p className="text-sm text-muted-foreground">正在生成，自动刷新中…</p>}
      </CardContent>
    </Card>
  );
}

export function StatusView({
  status,
  kb,
  actionBusy = false,
  onPause,
  onResume,
  onCancel,
  onDelete,
}: {
  status: WorkOrderStatus;
  kb: string;
  actionBusy?: boolean;
  onPause?: () => void;
  onResume?: () => void;
  onCancel?: () => void;
  onDelete?: () => void;
}) {
  const [previews, setPreviews] = useState<Record<string, unknown>>({});
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const [approve, setApprove] = useState<Record<string, string>>({});

  async function loadPreview(artifact_id: string) {
    if (previews[artifact_id]) return;
    try {
      const p = await api.get<Record<string, unknown>>(
        `/api/v1/document-generation/artifacts/${artifact_id}/preview?kb=${encodeURIComponent(kb)}`,
      );
      setPreviews((prev) => ({ ...prev, [artifact_id]: p }));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载预览失败');
    }
  }

  function submitFeedback(artifact_id: string) {
    void api
      .post(
        `/api/v1/document-generation/artifacts/${artifact_id}/feedback?kb=${encodeURIComponent(kb)}`,
        { comment: feedback[artifact_id] ?? '' },
      )
      .then(() => notify.success('反馈已保存；候选文件仍未发布。'))
      .catch((e) => notify.error(e instanceof Error ? e.message : '提交反馈失败'));
  }

  function submitApprove(artifact_id: string) {
    void api
      .post(
        `/api/v1/document-generation/artifacts/${artifact_id}/approve?kb=${encodeURIComponent(kb)}`,
        { comment: approve[artifact_id] ?? '' },
      )
      .then(() => notify.success('已批准并发布。'))
      .catch((e) => notify.error(e instanceof Error ? e.message : '批准失败'));
  }

  return (
    <div className="space-y-3 text-sm">
      <RunStatusPanel
        status={status}
        actionBusy={actionBusy}
        onPause={onPause}
        onResume={onResume}
        onCancel={onCancel}
        onDelete={onDelete}
      />
      {status.harness_run && (
        <details className="rounded-lg border bg-muted/20 p-3">
          <summary className="font-medium">运行技术详情</summary>
          <div className="mt-2 space-y-1 text-muted-foreground">
            <p>当前节点：{status.harness_run.current_node ?? '-'}</p>
            <p>Harness 状态：{status.harness_run.status ?? '-'}</p>
            <p>步骤数：{String(status.harness_run.step_count ?? '-')}</p>
            {describeHarnessProgress(status.harness_run) && (
              <p>{describeHarnessProgress(status.harness_run)}</p>
            )}
          </div>
        </details>
      )}
      {status.artifacts.map((a) => {
        const preview = previews[a.artifact_id] as Record<string, unknown> | undefined;
        return (
          <div key={a.artifact_id} className="space-y-2 rounded-md border p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span>{String(a.artifact_id)}（{String(a.stage ?? '')}）</span>
              <Button size="sm" onClick={() => void loadPreview(a.artifact_id)}>预览</Button>
              <Button size="sm" onClick={() => void apiDownload.blob(
                `/api/v1/document-generation/artifacts/${a.artifact_id}/download?kb=${encodeURIComponent(kb)}`,
                `${a.artifact_id}.${status.target_format ?? 'bin'}`,
              )}>下载</Button>
            </div>
            {preview && (
              <div className="space-y-1 rounded bg-muted p-2">
                <p>格式：{String(preview.format ?? '')}</p>
                {Array.isArray(preview.sheets) &&
                  (preview.sheets as Array<{ name?: string; rows?: unknown[] }>).map((s, i) => (
                    <p key={i}>工作表：{s.name ?? ''}（{s.rows?.length ?? 0} 行）</p>
                  ))}
                {preview.truncated ? <p>预览已截断；下载候选文件可查看完整内容。</p> : null}
              </div>
            )}
            {String(a.stage ?? '') === 'review_candidate' && (
              <div className="space-y-2">
                <Input
                  placeholder="反馈说明"
                  value={feedback[a.artifact_id] ?? ''}
                  onChange={(e) => setFeedback((prev) => ({ ...prev, [a.artifact_id]: e.target.value }))}
                />
                <Button size="sm" variant="outline" onClick={() => submitFeedback(a.artifact_id)}>提交反馈</Button>
                <Input
                  placeholder="批准说明"
                  value={approve[a.artifact_id] ?? ''}
                  onChange={(e) => setApprove((prev) => ({ ...prev, [a.artifact_id]: e.target.value }))}
                />
                <Button size="sm" onClick={() => submitApprove(a.artifact_id)}>批准并发布</Button>
              </div>
            )}
          </div>
        );
      })}
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

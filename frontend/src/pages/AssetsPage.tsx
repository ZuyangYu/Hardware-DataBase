import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api } from '../api/client';
import type { AssetCandidateView, AssetDetailView, AssetSourceLinkView, AssetView, KbView, OkResponse } from '../api/types';
import { isDeptAdmin, type AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS, formatDateTime } from '@/lib/enterprise-ui';

const ASSET_TYPE_LABEL: Record<AssetView['asset_type'], string> = {
  device: '设备',
  board: '板卡',
  component: '器件',
  firmware: '固件',
  other: '其他',
};

const SOURCE_CATEGORY_LABEL: Record<AssetSourceLinkView['source_category'], string> = {
  circuit_design: '电路设计',
  structured_table: '结构化表格',
  hardware_requirement: '硬件需求',
  hardware_architecture: '硬件架构',
  document_rag: 'RAG 文档',
};

type Props = {
  auth: AuthSession;
  onLogout: () => void;
  kbs: KbView[];
};

type CandidateForm = Pick<AssetCandidateView, 'asset_type' | 'name' | 'model' | 'manufacturer' | 'version'>;

export default function AssetsPage({ auth, onLogout, kbs }: Props) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedKb = searchParams.get('kb') ?? '';
  const selectedKb = kbs.find((kb) => kb.name === requestedKb) ?? kbs[0];
  const kbName = selectedKb?.name ?? '';
  const canWrite = Boolean(
    selectedKb && (selectedKb.permission === 'write' || selectedKb.permission === 'admin' || isDeptAdmin(auth.user)),
  );
  const [assets, setAssets] = useState<AssetView[]>([]);
  const [candidates, setCandidates] = useState<AssetCandidateView[]>([]);
  const [sources, setSources] = useState<AssetSourceLinkView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [candidateToConfirm, setCandidateToConfirm] = useState<AssetCandidateView | null>(null);
  const [candidateForm, setCandidateForm] = useState<CandidateForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [assetDetail, setAssetDetail] = useState<AssetDetailView | null>(null);

  const load = useCallback(() => {
    if (!kbName) {
      setAssets([]);
      setCandidates([]);
      setSources([]);
      setLoaded(true);
      return () => undefined;
    }
    let cancelled = false;
    setLoaded(false);
    Promise.all([
      api.get<AssetView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/assets`),
      api.get<AssetCandidateView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/asset-candidates`),
      api.get<AssetSourceLinkView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/asset-sources`),
    ])
      .then(([assetRows, candidateRows, sourceRows]) => {
        if (cancelled) return;
        setAssets(assetRows);
        setCandidates(candidateRows);
        setSources(sourceRows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载资产数据失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kbName]);

  useEffect(() => load(), [load]);

  const completedSources = useMemo(() => sources.filter((source) => source.file_status === 'completed'), [sources]);

  const assetColumns: DataTableColumn<AssetView>[] = useMemo(
    () => [
      {
        key: 'name',
        title: '资产',
        render: (asset) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{asset.name}</span>
            <span className="truncate text-[11px] text-[#858b9c]">{ASSET_TYPE_LABEL[asset.asset_type]}</span>
          </div>
        ),
      },
      { key: 'model', title: '型号', width: 150, render: (asset) => <span className="text-[#464c5e]">{asset.model || '-'}</span> },
      { key: 'manufacturer', title: '厂商', width: 130, render: (asset) => <span className="text-[#464c5e]">{asset.manufacturer || '-'}</span> },
      { key: 'version', title: '版本', width: 90, render: (asset) => <span>{asset.version || '-'}</span> },
      { key: 'evidence', title: '证据', width: 80, align: 'right', render: (asset) => <span className="text-[#464c5e]">{asset.evidence_count}</span> },
      { key: 'updated', title: '更新时间', width: 160, render: (asset) => <span>{formatDateTime(asset.updated_at)}</span> },
    ],
    [],
  );

  const candidateColumns: DataTableColumn<AssetCandidateView>[] = useMemo(
    () => [
      {
        key: 'candidate', title: 'AI 候选', render: (candidate) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{candidate.name}</span>
            <span className="truncate text-[11px] text-[#858b9c]">{candidate.file_name}</span>
          </div>
        ),
      },
      { key: 'model', title: '型号', width: 140, render: (candidate) => <span>{candidate.model || '-'}</span> },
      {
        key: 'source', title: '提取', width: 100, render: (candidate) => (
          <span className={candidate.extraction_method === 'llm' ? 'text-[#2b7a57]' : 'text-[#858b9c]'}>
            {candidate.extraction_method === 'llm' ? '模型提取' : '规则候选'}
          </span>
        ),
      },
      { key: 'confidence', title: '置信度', width: 90, align: 'right', render: (candidate) => <span>{Math.round(candidate.confidence * 100)}%</span> },
      {
        key: 'actions', title: '确认', width: 146, align: 'right', render: (candidate) => (
          <div className="flex justify-end gap-[6px]">
            <button
              type="button"
              disabled={!canWrite}
              onClick={(event) => { event.stopPropagation(); openConfirm(candidate); }}
              className="inline-flex h-[28px] items-center rounded-[8px] bg-[#18181a] px-[10px] text-[12px] text-white disabled:cursor-not-allowed disabled:opacity-40"
            >
              确认
            </button>
            <button
              type="button"
              disabled={!canWrite}
              onClick={(event) => { event.stopPropagation(); void rejectCandidate(candidate); }}
              className="inline-flex h-[28px] items-center rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#757f9c] disabled:cursor-not-allowed disabled:opacity-40"
            >
              忽略
            </button>
          </div>
        ),
      },
    ],
    [canWrite],
  );

  const sourceColumns: DataTableColumn<AssetSourceLinkView>[] = useMemo(
    () => [
      {
        key: 'file', title: '已接入资料', render: (source) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{source.file_name}</span>
            <span className="truncate text-[11px] text-[#858b9c]">{source.processor_kind || source.dataset_kind || '文档资料'}</span>
          </div>
        ),
      },
      { key: 'category', title: '资料类型', width: 110, render: (source) => <span className="text-[#464c5e]">{SOURCE_CATEGORY_LABEL[source.source_category]}</span> },
      {
        key: 'status', title: '资产关联', width: 180, render: (source) => {
          if (source.link_status === 'linked') return <span className="text-[#2b7a57]">已关联 · {source.asset_name}</span>;
          if (source.link_status === 'pending_review') return <span className="text-[#b45309]">候选待确认</span>;
          if (source.link_status === 'ignored') return <span className="text-[#858b9c]">已忽略</span>;
          return <span className="text-[#858b9c]">待 AI 提取</span>;
        },
      },
      {
        key: 'parse', title: '解析状态', width: 100, render: (source) => (
          <span className={source.file_status === 'completed' ? 'text-[#2b7a57]' : 'text-[#858b9c]'}>
            {source.file_status === 'completed' ? '已解析' : source.file_status || '-'}
          </span>
        ),
      },
      {
        key: 'action', title: '操作', width: 110, align: 'right', render: (source) => (
          <button
            type="button"
            disabled={source.asset_eligible && (!canWrite || source.file_status !== 'completed' || generating)}
            onClick={(event) => {
              event.stopPropagation();
              if (source.asset_eligible) void generateCandidate(source.file_id);
              else navigate(`/kbs/${encodeURIComponent(kbName)}/files`);
            }}
            className="inline-flex h-[28px] items-center rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] disabled:cursor-not-allowed disabled:opacity-40"
          >
            {source.asset_eligible
              ? (source.link_status === 'pending_review' ? '重新提取' : source.link_status === 'linked' ? '再次提取' : '生成候选')
              : '查看资料'}
          </button>
        ),
      },
    ],
    [canWrite, generating, kbName, navigate],
  );

  function changeKb(value: string) {
    navigate(`/assets?kb=${encodeURIComponent(value)}`);
  }

  async function generateCandidate(fileId: string) {
    if (!kbName || !fileId) return;
    setGenerating(true);
    try {
      const candidate = await api.post<AssetCandidateView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/asset-candidates/generate`, { file_id: fileId });
      setCandidates((rows) => [candidate, ...rows.filter((row) => row.id !== candidate.id)]);
      setSources((rows) => rows.map((source) => source.file_id === fileId ? { ...source, link_status: 'pending_review', candidate_id: candidate.id } : source));
      notify.success(candidate.extraction_method === 'llm' ? '已生成 AI 候选，等待确认' : '已生成规则候选，模型当前不可用');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成候选失败');
    } finally {
      setGenerating(false);
    }
  }

  function openConfirm(candidate: AssetCandidateView) {
    setCandidateToConfirm(candidate);
    setCandidateForm({
      asset_type: candidate.asset_type,
      name: candidate.name,
      model: candidate.model,
      manufacturer: candidate.manufacturer,
      version: candidate.version,
    });
  }

  async function confirmCandidate() {
    if (!candidateToConfirm || !candidateForm || !kbName) return;
    setSaving(true);
    try {
      const created = await api.post<AssetView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/asset-candidates/${candidateToConfirm.id}/accept`,
        candidateForm,
      );
      setAssets((rows) => [created, ...rows]);
      setCandidates((rows) => rows.filter((candidate) => candidate.id !== candidateToConfirm.id));
      setSources((rows) => rows.map((source) => source.file_id === candidateToConfirm.file_id ? { ...source, link_status: 'linked', asset_id: created.id, asset_name: created.name } : source));
      setCandidateToConfirm(null);
      setCandidateForm(null);
      notify.success('资产已确认并写入主数据');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '确认资产失败');
    } finally {
      setSaving(false);
    }
  }

  async function rejectCandidate(candidate: AssetCandidateView) {
    if (!kbName) return;
    try {
      await api.post<OkResponse>(`/api/v1/kbs/${encodeURIComponent(kbName)}/asset-candidates/${candidate.id}/reject`);
      setCandidates((rows) => rows.filter((item) => item.id !== candidate.id));
      setSources((rows) => rows.map((source) => source.file_id === candidate.file_id ? { ...source, link_status: 'ignored' } : source));
      notify.success('候选已忽略');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '操作失败');
    }
  }

  async function openAsset(asset: AssetView) {
    if (!kbName) return;
    try {
      setAssetDetail(await api.get<AssetDetailView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/assets/${asset.id}`));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载资产详情失败');
    }
  }

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="资产中心"
        description="AI 从已解析资料提出资产候选；确认后才会成为可追溯的硬件主数据。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] flex flex-wrap items-center justify-between gap-[12px]">
        <label className="flex min-w-[220px] items-center gap-[8px] text-[12px] text-[#757f9c]">
          知识库
          <select
            value={kbName}
            onChange={(event) => changeKb(event.target.value)}
            className="h-[34px] min-w-0 flex-1 rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]"
          >
            {kbs.length === 0 ? <option value="">暂无可访问知识库</option> : kbs.map((kb) => <option key={`${kb.kb_id}:${kb.name}`} value={kb.name}>{kb.name}</option>)}
          </select>
        </label>
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => load()}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
      </div>

      {!kbName ? (
        <div className="mt-[24px] border-y border-[#edf0f5] py-[56px] text-center text-[13px] text-[#858b9c]">请先挂载一个知识库，再开始构建资产主数据。</div>
      ) : (
        <>
          <div className="mt-[18px] flex flex-wrap gap-[16px]">
            <StatCard label="已确认资产" value={assets.length} />
            <StatCard label="待确认候选" value={candidates.length} tone={candidates.length ? 'red' : 'green'} />
            <StatCard label="已解析资料" value={completedSources.length} />
          </div>

          <section className="mt-[28px] border-t border-[#edf0f5] pt-[20px]">
            <div>
              <h2 className="text-[15px] font-medium text-[#18181a]">资料来源</h2>
              <p className="mt-[3px] text-[12px] text-[#858b9c]">知识库中的已解析文件会自动接入这里；从具体资料发起 AI 提取后，可查看它与资产的关联状态。</p>
            </div>
            <div className="mt-[14px]">
              {!loaded ? <LoadingRows /> : <DataTable columns={sourceColumns} data={sources} rowKey={(row) => row.file_id} size="compact" emptyText="知识库暂无文件" />}
            </div>
          </section>

          <section className="mt-[30px] border-t border-[#edf0f5] pt-[20px]">
            <div>
              <h2 className="text-[15px] font-medium text-[#18181a]">AI 解析候选</h2>
              <p className="mt-[3px] text-[12px] text-[#858b9c]">模型只提出候选和证据，确认操作由有写入权限的人员完成。</p>
            </div>
            <div className="mt-[14px]">
              {!loaded ? <LoadingRows /> : <DataTable columns={candidateColumns} data={candidates} rowKey={(row) => row.id} size="compact" emptyText="从上方资料来源选择“生成候选”。" />}
            </div>
          </section>

          <section className="mt-[30px] border-t border-[#edf0f5] pt-[20px]">
            <div>
              <h2 className="text-[15px] font-medium text-[#18181a]">可信资产</h2>
              <p className="mt-[3px] text-[12px] text-[#858b9c]">每条资产保留来源证据，后续可在此基础上建立设备、板卡和器件关系。</p>
            </div>
            <div className="mt-[14px]">
              {!loaded ? <LoadingRows /> : <DataTable columns={assetColumns} data={assets} rowKey={(row) => row.id} size="compact" emptyText="暂无已确认资产" onRowClick={(asset) => void openAsset(asset)} />}
            </div>
          </section>
        </>
      )}

      <Dialog open={Boolean(candidateToConfirm)} onOpenChange={(open) => { if (!open && !saving) { setCandidateToConfirm(null); setCandidateForm(null); } }}>
        <DialogContent className="max-w-[560px] gap-[16px] rounded-[10px] p-[24px]">
          <DialogHeader><DialogTitle>确认资产候选</DialogTitle></DialogHeader>
          {candidateToConfirm && candidateForm && (
            <div className="grid gap-[14px]">
              <p className="text-[12px] leading-[1.7] text-[#757f9c]">来源：{candidateToConfirm.file_name} · {candidateToConfirm.extraction_method === 'llm' ? '模型提取' : '规则候选'}</p>
              <Field label="资产名称"><Input value={candidateForm.name} onChange={(event) => setCandidateForm({ ...candidateForm, name: event.target.value })} /></Field>
              <div className="grid grid-cols-2 gap-[12px] max-[520px]:grid-cols-1">
                <Field label="类型"><select value={candidateForm.asset_type} onChange={(event) => setCandidateForm({ ...candidateForm, asset_type: event.target.value as AssetView['asset_type'] })} className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"><option value="device">设备</option><option value="board">板卡</option><option value="component">器件</option><option value="firmware">固件</option><option value="other">其他</option></select></Field>
                <Field label="型号"><Input value={candidateForm.model} onChange={(event) => setCandidateForm({ ...candidateForm, model: event.target.value })} /></Field>
                <Field label="厂商"><Input value={candidateForm.manufacturer} onChange={(event) => setCandidateForm({ ...candidateForm, manufacturer: event.target.value })} /></Field>
                <Field label="版本"><Input value={candidateForm.version} onChange={(event) => setCandidateForm({ ...candidateForm, version: event.target.value })} /></Field>
              </div>
              {candidateToConfirm.evidence_excerpt && <div className="max-h-[120px] overflow-auto border-l-2 border-[#d8e2d8] bg-[#f7faf7] px-[12px] py-[9px] text-[12px] leading-[1.7] text-[#526052]">{candidateToConfirm.evidence_excerpt}</div>}
              <div className="flex justify-end gap-[8px] pt-[4px]">
                <Button variant="outline" disabled={saving} onClick={() => { setCandidateToConfirm(null); setCandidateForm(null); }}>取消</Button>
                <Button disabled={saving || !candidateForm.name.trim()} onClick={() => void confirmCandidate()} className="bg-[#18181a] text-white hover:bg-[#303030]">{saving ? '写入中' : '确认并写入'}</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(assetDetail)} onOpenChange={(open) => { if (!open) setAssetDetail(null); }}>
        <DialogContent className="max-w-[640px] gap-[16px] rounded-[10px] p-[24px]">
          <DialogHeader><DialogTitle>{assetDetail?.name ?? '资产详情'}</DialogTitle></DialogHeader>
          {assetDetail && (
            <div className="grid gap-[16px]">
              <div className="grid grid-cols-2 gap-x-[24px] gap-y-[10px] text-[13px] max-[520px]:grid-cols-1">
                <Detail label="类型" value={ASSET_TYPE_LABEL[assetDetail.asset_type]} /><Detail label="型号" value={assetDetail.model || '-'} />
                <Detail label="厂商" value={assetDetail.manufacturer || '-'} /><Detail label="版本" value={assetDetail.version || '-'} />
              </div>
              <div className="border-t border-[#edf0f5] pt-[12px]"><h3 className="text-[13px] font-medium text-[#18181a]">来源证据</h3><div className="mt-[8px] grid gap-[8px]">{assetDetail.evidence.map((evidence) => <div key={evidence.id} className="border-l-2 border-[#d8e2d8] bg-[#f7faf7] px-[12px] py-[9px]"><p className="text-[12px] font-medium text-[#526052]">{evidence.file_name}{evidence.locator ? ` · ${evidence.locator}` : ''}</p>{evidence.excerpt && <p className="mt-[4px] whitespace-pre-wrap text-[12px] leading-[1.6] text-[#757f9c]">{evidence.excerpt}</p>}</div>)}</div></div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="grid gap-[5px]"><Label className="text-[12px] text-[#757f9c]">{label}</Label>{children}</label>;
}

function Detail({ label, value }: { label: string; value: string }) {
  return <div><p className="text-[11px] text-[#858b9c]">{label}</p><p className="mt-[2px] text-[#464c5e]">{value}</p></div>;
}

function LoadingRows() {
  return <div className="grid gap-[8px]">{[0, 1, 2].map((index) => <Skeleton key={index} className="h-[46px] rounded-[8px]" />)}</div>;
}

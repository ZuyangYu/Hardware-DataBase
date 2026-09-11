import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api } from '../api/client';
import type {
  DocAssetDetailView,
  DocAssetView,
  DocCategory,
  DocLifecycleStatus,
  DocRelationType,
  FileView,
  KbView,
  WikiGraphView,
  WikiPageType,
} from '../api/types';
import { isEmployee, type AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import WikiGraph from '@/components/WikiGraph';
import WikiPage from '@/pages/WikiPage';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS, formatDateTime, formatDateTimeShort } from '@/lib/enterprise-ui';

const LIFECYCLE_LABEL: Record<DocLifecycleStatus, string> = {
  draft: '草稿',
  effective: '生效',
  in_revision: '修订中',
  obsolete: '已作废',
  archived: '已归档',
};

const LIFECYCLE_TONE: Record<DocLifecycleStatus, string> = {
  draft: 'border-[#e3e7f1] bg-[#f6f7fb] text-[#5b6478]',
  effective: 'border-[#d8e2d8] bg-[#eef6ef] text-[#2b7a57]',
  in_revision: 'border-[#dbe4f5] bg-[#eef2fa] text-[#3d5a91]',
  obsolete: 'border-[#f0dfc0] bg-[#fdf6ea] text-[#b45309]',
  archived: 'border-[#e3e7f1] bg-[#f6f7fb] text-[#a5adc0]',
};

const CATEGORY_LABEL: Record<DocCategory, string> = {
  design_doc: '设计文档',
  schematic: '原理图图纸',
  bom: 'BOM 清单',
  netlist: '网表',
  test_report: '测试报告',
  requirement: '需求文档',
  standard: '标准规范',
  other: '其他',
};

const GRAPH_TYPE_LABEL: Record<WikiPageType, string> = {
  summary: '文档摘要',
  entity: '实体',
  concept: '概念',
  index: '索引',
};

const RELATION_LABEL: Record<DocRelationType, string> = {
  derived_from: '衍生自',
  companion: '配套',
  references: '引用',
  verified_by: '验证',
};

const EVENT_LABEL: Record<string, string> = {
  created: '建档',
  version_added: '新增版本',
  released: '发布生效',
  superseded: '版本被替代',
  revision_started: '发起修订',
  info_updated: '信息更新',
};

const VERSION_STATE_LABEL: Record<string, string> = {
  draft: '待发布',
  effective: '生效',
  superseded: '已替代',
  deleted: '源文件已删除',
};

const SHADOW_TAG = '影子档';
const SHADOW_DESCRIPTION_MARK = '自动补录的影子档';
const SHADOW_VERSION_NOTE = '批量补录';

function visibleTags(tags: string[]): string[] {
  return tags.filter((tag) => tag !== SHADOW_TAG);
}

type Props = {
  auth: AuthSession;
  onLogout: () => void;
  kbs: KbView[];
};


type AssetForm = {
  title: string;
  doc_no: string;
  category: DocCategory;
  project: string;
  description: string;
  tagsText: string;
}

function parseTags(text: string): string[] {
  return text
    .split(/[,，;；\s]+/)
    .map((tag) => tag.trim())
    .filter(Boolean)
    .slice(0, 20);
}

function normalizeNameTokens(text: string): string[] {
  const cleaned = text.replace(/\.(xlsx|xls|docx|doc|pdf|edf|edif)$/i, '');
  return cleaned
    .split(/[^A-Za-z\u4e00-\u9fff]+/)
    .map((token) => token.toLowerCase())
    .filter((token) => token.length >= 2 && !/^\d+$/.test(token));
}

export default function DocumentAssetsPage({ auth, onLogout, kbs }: Props) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedKb = searchParams.get('kb') ?? '';
  const tabParam = searchParams.get('tab');
  const tab = tabParam === 'wiki' || tabParam === 'graph' ? tabParam : 'assets';
  const selectedKb = kbs.find((kb) => kb.name === requestedKb) ?? kbs[0];
  const kbName = selectedKb?.name ?? '';
  const canWrite = Boolean(
    selectedKb && (selectedKb.permission === 'write' || selectedKb.permission === 'admin' || isEmployee(auth.user)),
  );

  const [assets, setAssets] = useState<DocAssetView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [categoryFilter, setCategoryFilter] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [searchApplied, setSearchApplied] = useState('');

  const [detail, setDetail] = useState<DocAssetDetailView | null>(null);
  const [saving, setSaving] = useState(false);
  const [files, setFiles] = useState<FileView[]>([]);
  const [editForm, setEditForm] = useState<AssetForm | null>(null);
  const [versionDialog, setVersionDialog] = useState<{ fileId: string; note: string } | null>(null);
  const [linkDialog, setLinkDialog] = useState<{ targetId: string; relType: DocRelationType; note: string } | null>(null);
  const [linkTargets, setLinkTargets] = useState<DocAssetView[]>([]);
  const [eventsOpen, setEventsOpen] = useState(false);
  const [graph, setGraph] = useState<WikiGraphView | null>(null);
  const [graphLoaded, setGraphLoaded] = useState(false);
  const [graphTypeFilter, setGraphTypeFilter] = useState<'' | WikiPageType>('');

  const load = useCallback(() => {
    if (!kbName) {
      setAssets([]);
      setLoaded(true);
      return () => undefined;
    }
    let cancelled = false;
    setLoaded(false);
    const params = new URLSearchParams();
    if (searchApplied) params.set('query', searchApplied);
    if (categoryFilter) params.set('category', categoryFilter);
    api
      .get<DocAssetView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets?${params.toString()}`)
      .then((rows) => {
        if (!cancelled) setAssets(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载文档资产失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kbName, searchApplied, categoryFilter]);

  useEffect(() => load(), [load]);

  const loadGraph = useCallback(() => {
    if (!kbName) {
      setGraph(null);
      setGraphLoaded(true);
      return;
    }
    setGraphLoaded(false);
    api
      .get<WikiGraphView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/graph?limit=200`)
      .then((data) => setGraph(data))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载图谱失败'))
      .finally(() => setGraphLoaded(true));
  }, [kbName]);

  useEffect(() => {
    if (tab === 'graph') loadGraph();
  }, [tab, loadGraph]);

  /** 图谱跟随类型筛选: 只保留该类节点及两端都在的边 */
  const visibleGraph = useMemo<WikiGraphView | null>(() => {
    if (!graph) return null;
    if (!graphTypeFilter) return graph;
    const nodes = graph.nodes.filter((node) => node.page_type === graphTypeFilter);
    const alive = new Set(nodes.map((node) => node.slug));
    return {
      nodes,
      edges: graph.edges.filter((edge) => alive.has(edge.source) && alive.has(edge.target)),
      total: nodes.length,
      truncated: graph.truncated,
    };
  }, [graph, graphTypeFilter]);

  function openGraphNode(slug: string) {
    if (!kbName) return;
    navigate(`/assets?tab=wiki&kb=${encodeURIComponent(kbName)}&open=${encodeURIComponent(slug)}`);
  }

  const loadFiles = useCallback(async () => {
    if (!kbName) return;
    try {
      const rows = await api.get<FileView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/files`);
      setFiles(rows.filter((file) => (file.status || '').toLowerCase() === 'completed'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载知识库文件失败');
    }
  }, [kbName]);

  async function openDetail(asset: DocAssetView) {
    if (!kbName) return;
    try {
      setDetail(await api.get<DocAssetDetailView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${asset.id}`));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载资产详情失败');
    }
  }

  async function refreshDetail(assetId: number) {
    if (!kbName) return;
    try {
      setDetail(await api.get<DocAssetDetailView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${assetId}`));
    } catch {
      setDetail(null);
    }
    load();
  }

  function changeKb(value: string) {
    navigate(`/assets?tab=${tab}&kb=${encodeURIComponent(value)}`);
  }

  function switchTab(next: 'assets' | 'wiki' | 'graph') {
    navigate(`/assets?tab=${next}${kbName ? `&kb=${encodeURIComponent(kbName)}` : ''}`);
  }


  async function saveInfo() {
    if (!kbName || !detail || !editForm) return;
    setSaving(true);
    try {
      const updated = await api.patch<DocAssetView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${detail.id}`,
        {
          title: editForm.title,
          doc_no: editForm.doc_no,
          category: editForm.category,
          project: editForm.project,
          description: editForm.description,
          tags: parseTags(editForm.tagsText),
        },
      );
      notify.success('资产信息已更新');
      setEditForm(null);
      setDetail({ ...detail, ...updated });
      load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新失败');
    } finally {
      setSaving(false);
    }
  }

  async function addVersion() {
    if (!kbName || !detail || !versionDialog?.fileId) return;
    setSaving(true);
    try {
      await api.post<DocAssetView>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${detail.id}/versions`, {
        file_id: versionDialog.fileId,
        note: versionDialog.note,
      });
      notify.success('新版本已登记并生效');
      setVersionDialog(null);
      await refreshDetail(detail.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '登记版本失败');
    } finally {
      setSaving(false);
    }
  }

  async function addLink() {
    if (!kbName || !detail || !linkDialog?.targetId) return;
    setSaving(true);
    try {
      await api.post<unknown>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${detail.id}/links`, {
        to_asset_id: Number(linkDialog.targetId),
        rel_type: linkDialog.relType,
        note: linkDialog.note,
      });
      notify.success('关联已建立');
      setLinkDialog(null);
      await refreshDetail(detail.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '建立关联失败');
    } finally {
      setSaving(false);
    }
  }

  async function removeLink(linkId: number) {
    if (!kbName || !detail) return;
    try {
      await api.delete<OkShape>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets/${detail.id}/links/${linkId}`);
      notify.success('关联已移除');
      await refreshDetail(detail.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '移除关联失败');
    }
  }

  const assetColumns: DataTableColumn<DocAssetView>[] = useMemo(
    () => [
      {
        key: 'title',
        title: '文档资产',
        render: (asset) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{asset.title}</span>
            <span className="truncate text-[11px] text-[#858b9c]">{asset.doc_no || CATEGORY_LABEL[asset.category]}</span>
          </div>
        ),
      },
      { key: 'category', title: '类别', width: 84, render: (asset) => <span className="text-[#464c5e]">{CATEGORY_LABEL[asset.category]}</span> },
      { key: 'project', title: '项目', width: 120, render: (asset) => <span className="block truncate text-[#464c5e]">{asset.project || '-'}</span> },
      {
        key: 'status',
        title: '状态',
        width: 76,
        render: (asset) => (
          <span className={`inline-flex h-[22px] items-center whitespace-nowrap rounded-full border px-[8px] text-[11px] ${LIFECYCLE_TONE[asset.lifecycle_status]}`}>
            {LIFECYCLE_LABEL[asset.lifecycle_status]}
          </span>
        ),
      },
      {
        key: 'versions',
        title: '版本',
        width: 150,
        align: 'right',
        render: (asset) => (
          <span className="text-[#464c5e]">
            {asset.effective_version_no ? `v${asset.effective_version_no} 生效 · ` : ''}
            {asset.version_count} 个版本
          </span>
        ),
      },
      { key: 'updated', title: '更新时间', width: 160, render: (asset) => <span>{formatDateTime(asset.updated_at)}</span> },
    ],
    [],
  );

  const usedFileIds = useMemo(() => new Set((detail?.versions ?? []).map((version) => version.file_id)), [detail]);
  const linkableFiles = files.filter((file) => !usedFileIds.has(file.id));

  const hintAssets = useMemo(() => {
    if (!detail) return [];
    return assets.map((asset) => ({
      id: asset.id,
      title: asset.title,
      tokens: normalizeNameTokens(asset.title),
    }));
  }, [assets, detail]);

  function fileHint(file: FileView): string {
    const fileTokens = normalizeNameTokens(file.name);
    if (fileTokens.length === 0) return '';
    for (const asset of hintAssets) {
      const shared = fileTokens.filter((token) => asset.tokens.includes(token));
      if (shared.length >= 1) {
        const ratio = shared.length / Math.max(fileTokens.length, 1);
        if (ratio >= 0.4 || shared.some((token) => token.length >= 3)) {
          return `★ 疑似同文档:${asset.title}`;
        }
      }
    }
    return '';
  }

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="资产中心"
        description="文档资产:一文档一档,版本链与生命周期全程留痕;词条 Wiki 由文档蒸馏生成,出处可回溯。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] flex flex-wrap items-center gap-[12px]">
        <div className="flex gap-[8px]">
          {([['assets', '文档资产'], ['wiki', '知识 Wiki'], ['graph', '知识图谱']] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => switchTab(key)}
              className={`h-[34px] rounded-[8px] border px-[16px] text-[13px] transition-colors ${
                tab === key
                  ? 'border-[#18181a] bg-[#18181a] text-white'
                  : 'border-[#e3e7f1] bg-white text-[#5b6478] hover:border-[#9cabc8]'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <label className="ml-auto flex min-w-[200px] items-center gap-[8px] text-[12px] text-[#757f9c]">
          知识库
          <select
            value={kbName}
            onChange={(event) => changeKb(event.target.value)}
            className="h-[34px] min-w-0 flex-1 rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]"
          >
            {kbs.length === 0 ? <option value="">暂无可访问知识库</option> : kbs.map((kb) => <option key={`${kb.kb_id}:${kb.name}`} value={kb.name}>{kb.name}</option>)}
          </select>
        </label>
        {tab === 'assets' && (
          <div className="flex items-center gap-[8px]">
            <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => load()}>
              <AppIcon name="refresh" size={14} />
              刷新
            </Button>
          </div>
        )}
      </div>

      {tab === 'wiki' ? (
        <WikiPage auth={auth} onLogout={onLogout} kbs={kbs} embedded />
      ) : tab === 'graph' ? (
        <>
          <div className="mt-[16px] flex flex-wrap items-center gap-[8px]">
            <button
              type="button"
              onClick={() => setGraphTypeFilter('')}
              className={`h-[30px] rounded-full border px-[12px] text-[12px] ${graphTypeFilter === '' ? 'border-[#18181a] bg-[#18181a] text-white' : 'border-[#e3e7f1] bg-white text-[#5b6478] hover:border-[#9cabc8]'}`}
            >
              全部
            </button>
            {(Object.keys(GRAPH_TYPE_LABEL) as WikiPageType[]).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setGraphTypeFilter(t)}
                className={`h-[30px] rounded-full border px-[12px] text-[12px] ${graphTypeFilter === t ? 'border-[#18181a] bg-[#18181a] text-white' : 'border-[#e3e7f1] bg-white text-[#5b6478] hover:border-[#9cabc8]'}`}
              >
                {GRAPH_TYPE_LABEL[t]}
              </button>
            ))}
            <div className="ml-auto">
              <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => loadGraph()}>
                <AppIcon name="refresh" size={14} />
                刷新
              </Button>
            </div>
          </div>
          <div className="mt-[18px]">
            {!kbName ? (
              <div className="border-y border-[#edf0f5] py-[56px] text-center text-[13px] text-[#858b9c]">请先挂载一个知识库,再查看图谱。</div>
            ) : !graphLoaded ? (
              <div className="grid gap-[8px]">{[0, 1].map((i) => <Skeleton key={i} className="h-[120px] rounded-[8px]" />)}</div>
            ) : graph && visibleGraph ? (
              <WikiGraph graph={visibleGraph} onSelect={openGraphNode} />
            ) : null}
          </div>
        </>
      ) : (
        <>
          <div className="mt-[16px] flex flex-wrap items-center gap-[8px]">
        <select
          value={categoryFilter}
          onChange={(event) => setCategoryFilter(event.target.value)}
          className="h-[30px] rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] text-[12px] text-[#18181a] outline-none focus:border-[#9cabc8]"
        >
          <option value="">全部类别</option>
          {(Object.keys(CATEGORY_LABEL) as DocCategory[]).map((key) => (
            <option key={key} value={key}>{CATEGORY_LABEL[key]}</option>
          ))}
        </select>
        <div className="relative ml-auto">
          <input
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') setSearchApplied(searchInput.trim());
            }}
            placeholder="搜索标题 / 编号 / 项目,回车确认"
            className="h-[30px] w-[240px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none placeholder:text-[#a5adc0] focus:border-[#9cabc8]"
          />
        </div>
      </div>

      {!kbName ? (
        <div className="mt-[24px] border-y border-[#edf0f5] py-[56px] text-center text-[13px] text-[#858b9c]">请先挂载一个知识库,再开始整理文档资产。</div>
      ) : (
        <div className="mt-[18px]">
          {!loaded ? (
            <LoadingRows />
          ) : (
            <DataTable
              columns={assetColumns}
              data={assets}
              rowKey={(row) => row.id}
              size="compact"
              emptyText="暂无文档资产;可从知识库文件建档,或手动新建。"
              onRowClick={(asset) => void openDetail(asset)}
            />
          )}
        </div>
      )}
        </>
      )}

      <Dialog open={Boolean(detail)} onOpenChange={(open) => { if (!open && !saving) setDetail(null); }}>
        <DialogContent className="nice-scroll flex max-h-[88vh] w-[min(960px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-auto rounded-[12px] p-0 sm:max-w-none">
          {detail && (
            <>
              {/* 头部:标题区 + 动作区(右端留 56px 给 32px 弹窗关闭 × 让位) */}
              <div className="flex flex-wrap items-start gap-[12px] pb-[2px] pl-[24px] pr-[56px] pt-[18px]">
                <div className="min-w-0 flex-1">
                  <div className="text-[11px] text-[#9aa2b5]">{CATEGORY_LABEL[detail.category]}</div>
                  <div className="mt-[2px] flex flex-wrap items-center gap-[10px]">
                    <DialogTitle className="min-w-0 truncate text-[17px] font-semibold text-[#18181a]">{detail.title}</DialogTitle>
                    <span className={`inline-flex h-[21px] items-center rounded-full border px-[8px] text-[11px] ${LIFECYCLE_TONE[detail.lifecycle_status] ?? LIFECYCLE_TONE.effective}`}>
                      {LIFECYCLE_LABEL[detail.lifecycle_status] ?? detail.lifecycle_status}
                    </span>
                    {visibleTags(detail.tags).map((tag) => (
                      <span key={tag} className="rounded-[4px] bg-[#f4f5f9] px-[7px] py-[2px] text-[11px] text-[#5b6478]">{tag}</span>
                    ))}
                  </div>
                  <div className="mt-[6px] flex flex-wrap items-center gap-x-[14px] gap-y-[3px] text-[12px] text-[#9aa2b5]">
                    {detail.doc_no && <span>{detail.doc_no}</span>}
                    {detail.project && <span>项目 · {detail.project}</span>}
                    <span>
                      {detail.effective_version_no ? `v${detail.effective_version_no} 生效` : '无生效版本'}
                      <span className="mx-[6px] text-[#d9deeb]">/</span>
                      {detail.version_count} 个版本
                    </span>
                    <span>建档 {formatDateTime(detail.created_at)}</span>
                  </div>
                </div>
                <div className="ml-auto flex shrink-0 flex-wrap items-center gap-[8px] pt-[2px]">
                  {canWrite && (
                    <Button
                      disabled={saving}
                      onClick={() => {
                        void loadFiles();
                        setVersionDialog({ fileId: '', note: '' });
                      }}
                      className="h-[30px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] text-white hover:bg-[#303030]"
                    >
                      登记新版本
                    </Button>
                  )}
                  {canWrite && (
                    <Button
                      variant="outline"
                      className={OUTLINE_ACTION_BUTTON_CLASS}
                      onClick={() => {
                        setEditForm({
                          title: detail.title,
                          doc_no: detail.doc_no,
                          category: detail.category,
                          project: detail.project,
                          description: detail.description,
                          tagsText: visibleTags(detail.tags).join(' '),
                        });
                      }}
                    >
                      编辑信息
                    </Button>
                  )}
                </div>
              </div>

              {/* 说明(仅人工填写时展示) */}
              {detail.description && !detail.description.includes(SHADOW_DESCRIPTION_MARK) && (
                <div className="border-b border-[#eef0f4] bg-[#fafbfd] px-[24px] py-[10px] text-[12px] leading-[1.7] text-[#5b6478]">
                  {detail.description}
                </div>
              )}

              <div className="grid gap-0 min-[760px]:grid-cols-[minmax(0,1fr)_292px]">
                {/* 主列: 当前版本 + 历史 */}
                <div className="min-w-0 border-[#eef0f4] px-[24px] py-[18px] min-[760px]:border-r">
                  <div className="text-[12px] font-medium text-[#757f9c]">当前版本</div>
                  {(() => {
                    const current = detail.versions.find((version) => version.state === 'effective');
                    if (!current) {
                      return (
                        <p className="mt-[10px] rounded-[8px] border border-dashed border-[#e3e7f1] px-[12px] py-[14px] text-[12px] text-[#858b9c]">
                          暂无生效版本;点击「登记新版本」从知识库选择文件。
                        </p>
                      );
                    }
                    return (
                      <div className="mt-[10px] flex items-start gap-[12px] border-l-[3px] border-[#66b788] bg-[#f6fbf6] px-[14px] py-[11px]">
                        <span className="pt-[1px] text-[18px] font-semibold leading-[1.1] text-[#2b7a57]">v{current.version_no}</span>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[13px] font-medium text-[#18181a]">{current.file_name || current.file_id || '未关联文件'}</p>
                          <div className="mt-[3px] flex flex-wrap items-center gap-x-[12px] gap-y-[2px] text-[11px] text-[#858b9c]">
                            {current.content_hash && <span className="font-mono">{current.content_hash.slice(0, 12)}</span>}
                            <span>{formatDateTime(current.uploaded_at)}</span>
                            {current.note && current.note !== SHADOW_VERSION_NOTE && <span className="break-words">{current.note}</span>}
                          </div>
                        </div>
                      </div>
                    );
                  })()}

                  {detail.versions.some((version) => version.state !== 'effective') && (
                    <>
                      <div className="mt-[18px] text-[12px] font-medium text-[#757f9c]">
                        历史版本
                        <span className="ml-[6px] text-[11px] font-normal text-[#a5adc0]">{detail.versions.filter((version) => version.state !== 'effective').length}</span>
                      </div>
                      <div className="relative mt-[8px]">
                        <span className="absolute bottom-[10px] left-[3px] top-[10px] w-px bg-[#e8ebf2]" />
                        {detail.versions.filter((version) => version.state !== 'effective').map((version) => (
                          <div key={version.id} className="relative flex items-center gap-[10px] py-[7px]">
                            <span className={`relative z-[1] ml-0 size-[7px] shrink-0 rounded-full border-2 bg-white ${version.state === 'superseded' ? 'border-[#c3cad9]' : 'border-[#e0b184]'}`} />
                            <span className="w-[30px] shrink-0 text-right text-[12px] text-[#5b6478]">v{version.version_no}</span>
                            <span className={`w-[46px] shrink-0 text-[11px] ${version.state === 'superseded' ? 'text-[#a5adc0]' : 'text-[#b45309]'}`}>
                              {VERSION_STATE_LABEL[version.state] ?? version.state}
                            </span>
                            <span className="min-w-0 truncate text-[12px] text-[#757f9c]">{version.file_name || version.file_id}</span>
                            <span className="ml-auto shrink-0 text-[11px] text-[#b6bdcf]">{formatDateTimeShort(version.uploaded_at)}</span>
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </div>

                {/* 侧列: 关联 + 变更 */}
                <div className="grid content-start gap-0 border-t border-[#eef0f4] bg-[#fbfcfe] px-[20px] py-[18px] min-[760px]:border-t-0">
                  <div>
                    <div className="flex items-center justify-between">
                      <div className="text-[12px] font-medium text-[#757f9c]">
                        关联文档
                        <span className="ml-[6px] text-[11px] font-normal text-[#a5adc0]">出 {detail.links_out.length} · 入 {detail.links_in.length}</span>
                      </div>
                      {canWrite && (
                        <button
                          type="button"
                          onClick={() => {
                            setLinkTargets([]);
                            setLinkDialog({ targetId: '', relType: 'companion', note: '' });
                          }}
                          className="text-[12px] text-[#2b7a57] hover:underline"
                        >
                          + 添加
                        </button>
                      )}
                    </div>
                    <div className="mt-[8px] grid gap-[6px]">
                      {detail.links_out.length === 0 && detail.links_in.length === 0 && (
                        <p className="rounded-[8px] border border-dashed border-[#e3e7f1] px-[10px] py-[12px] text-[12px] leading-[1.6] text-[#858b9c]">
                          暂无关联。可建立 配套 / 引用 / 衍生 / 验证 四类关系。
                        </p>
                      )}
                      {detail.links_out.map((link) => (
                        <div key={`out-${link.id}`} className="min-w-0">
                          <p className="truncate text-[12px] text-[#18181a]">
                            <span className="mr-[6px] rounded-[4px] bg-[#eef2fa] px-[5px] py-[1px] text-[10px] text-[#3d5a91]">{RELATION_LABEL[link.rel_type]} →</span>
                            {link.to_title}
                          </p>
                          {link.note && <p className="mt-[1px] truncate pl-[2px] text-[11px] text-[#a5adc0]">{link.note}</p>}
                        </div>
                      ))}
                      {detail.links_in.map((link) => (
                        <div key={`in-${link.id}`} className="min-w-0">
                          <p className="truncate text-[12px]">
                            <span className="mr-[6px] rounded-[4px] bg-[#eaf2ec] px-[5px] py-[1px] text-[10px] text-[#2b7a57]">← 被{RELATION_LABEL[link.rel_type]}</span>
                            <span className="text-[#5b6478]">{link.to_title}</span>
                          </p>
                          {link.note && <p className="mt-[1px] truncate pl-[2px] text-[11px] text-[#a5adc0]">{link.note}</p>}
                        </div>
                      ))}
                    </div>
                  </div>

                  <div className="mt-[18px] border-t border-[#eef0f4] pt-[14px]">
                    <button
                      type="button"
                      onClick={() => setEventsOpen(!eventsOpen)}
                      className="flex w-full items-center justify-between text-left"
                    >
                      <span className="text-[12px] font-medium text-[#757f9c]">
                        变更记录
                        <span className="ml-[6px] text-[11px] font-normal text-[#a5adc0]">{detail.events.length}</span>
                      </span>
                      <span className="text-[11px] text-[#858b9c]">{eventsOpen ? '收起 ▲' : '展开 ▼'}</span>
                    </button>
                    {eventsOpen && (
                      <div className="mt-[8px] grid gap-[5px]">
                        {detail.events.map((event) => (
                          <div key={event.id} className="flex items-baseline gap-[8px] text-[12px]">
                            <span className="w-[74px] shrink-0 text-[#5b6478]">{EVENT_LABEL[event.event] || event.event}</span>
                            <span className="min-w-0 flex-1 break-words text-[#858b9c]">{event.comment}</span>
                            <span className="shrink-0 text-[10px] text-[#b6bdcf]">{formatDateTimeShort(event.created_at)}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(editForm)} onOpenChange={(open) => { if (!open && !saving) setEditForm(null); }}>
        <DialogContent className="w-[min(560px,calc(100vw-32px))] max-w-none gap-[14px] rounded-[10px] p-[24px] sm:max-w-none">
          <DialogHeader><DialogTitle>编辑资产信息</DialogTitle></DialogHeader>
          {editForm && (
            <div className="grid gap-[12px]">
              <Field label="标题">
                <Input value={editForm.title} onChange={(event) => setEditForm({ ...editForm, title: event.target.value })} />
              </Field>
              <div className="grid grid-cols-2 gap-[12px] max-[520px]:grid-cols-1">
                <Field label="类别">
                  <SelectCell value={editForm.category} onChange={(value) => setEditForm({ ...editForm, category: value as DocCategory })} options={Object.keys(CATEGORY_LABEL) as DocCategory[]} labelMap={CATEGORY_LABEL} />
                </Field>
                <Field label="编号"><Input value={editForm.doc_no} onChange={(event) => setEditForm({ ...editForm, doc_no: event.target.value })} /></Field>
                <Field label="项目"><Input value={editForm.project} onChange={(event) => setEditForm({ ...editForm, project: event.target.value })} /></Field>
                <Field label="标签(空格分隔)"><Input value={editForm.tagsText} onChange={(event) => setEditForm({ ...editForm, tagsText: event.target.value })} /></Field>
              </div>
              <Field label="说明"><Input value={editForm.description} onChange={(event) => setEditForm({ ...editForm, description: event.target.value })} /></Field>
              <div className="flex justify-end gap-[8px] pt-[4px]">
                <Button variant="outline" disabled={saving} onClick={() => setEditForm(null)}>取消</Button>
                <Button disabled={saving || !editForm.title.trim()} onClick={() => void saveInfo()} className="bg-[#18181a] text-white hover:bg-[#303030]">{saving ? '保存中' : '保存'}</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(versionDialog)} onOpenChange={(open) => { if (!open && !saving) setVersionDialog(null); }}>
        <DialogContent className="w-[min(520px,calc(100vw-32px))] max-w-none gap-[14px] rounded-[10px] p-[24px] sm:max-w-none">
          <DialogHeader><DialogTitle>登记新版本</DialogTitle></DialogHeader>
          {versionDialog && (
            <div className="grid gap-[12px]">
              <p className="text-[12px] leading-[1.7] text-[#757f9c]">从知识库选择一个已解析文件登记为新版本;登记即生效,旧版本自动标记为已替代。</p>
              <Field label="知识库文件">
                <select
                  value={versionDialog.fileId}
                  onChange={(event) => setVersionDialog({ ...versionDialog, fileId: event.target.value })}
                  className="h-[34px] w-full rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]"
                >
                  <option value="">请选择文件</option>
                  {[...linkableFiles]
                    .sort((a, b) => (fileHint(b) ? 1 : 0) - (fileHint(a) ? 1 : 0))
                    .map((file) => {
                      const hint = fileHint(file);
                      return (
                        <option key={file.id} value={file.id}>
                          {hint ? `★ ${file.name} — ${hint}` : file.name}
                        </option>
                      );
                    })}
                </select>
              </Field>
              {linkableFiles.length === 0 && <p className="text-[12px] text-[#b45309]">没有可用的已解析文件(或都已被登记为版本);先到「知识库」页上传并解析。</p>}
              <Field label="版本说明(可选)">
                <Input value={versionDialog.note} onChange={(event) => setVersionDialog({ ...versionDialog, note: event.target.value })} placeholder="如:按评审意见更新第 3 章" />
              </Field>
              <div className="flex justify-end gap-[8px] pt-[4px]">
                <Button variant="outline" disabled={saving} onClick={() => setVersionDialog(null)}>取消</Button>
                <Button disabled={saving || !versionDialog.fileId} onClick={() => void addVersion()} className="bg-[#18181a] text-white hover:bg-[#303030]">{saving ? '登记中' : '登记版本'}</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(linkDialog)} onOpenChange={(open) => { if (!open && !saving) setLinkDialog(null); }}>
        <DialogContent className="w-[min(520px,calc(100vw-32px))] max-w-none gap-[14px] rounded-[10px] p-[24px] sm:max-w-none">
          <DialogHeader><DialogTitle>添加关联文档</DialogTitle></DialogHeader>
          {linkDialog && (
            <div className="grid gap-[12px]">
              <Field label="关系类型">
                <SelectCell value={linkDialog.relType} onChange={(value) => setLinkDialog({ ...linkDialog, relType: value as DocRelationType })} options={Object.keys(RELATION_LABEL) as DocRelationType[]} labelMap={RELATION_LABEL} />
              </Field>
              <Field label="目标文档(当前知识库)">
                <select
                  value={linkDialog.targetId}
                  onFocus={() => {
                    api
                      .get<DocAssetView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/doc-assets`)
                      .then((rows) => setLinkTargets(rows.filter((row) => row.id !== detail?.id)))
                      .catch((error) => notify.error(error instanceof Error ? error.message : '加载文档列表失败'));
                  }}
                  onChange={(event) => setLinkDialog({ ...linkDialog, targetId: event.target.value })}
                  className="h-[34px] w-full rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]"
                >
                  <option value="">请选择(点击加载列表)</option>
                  {linkTargets.map((target) => (
                    <option key={target.id} value={target.id}>{target.title}{target.doc_no ? ` · ${target.doc_no}` : ''}</option>
                  ))}
                </select>
              </Field>
              <Field label="备注(可选)">
                <Input value={linkDialog.note} onChange={(event) => setLinkDialog({ ...linkDialog, note: event.target.value })} />
              </Field>
              <div className="flex justify-end gap-[8px] pt-[4px]">
                <Button variant="outline" disabled={saving} onClick={() => setLinkDialog(null)}>取消</Button>
                <Button disabled={saving || !linkDialog.targetId} onClick={() => void addLink()} className="bg-[#18181a] text-white hover:bg-[#303030]">{saving ? '建立中' : '建立关联'}</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

    </div>
  );
}

type OkShape = { ok: boolean; message?: string };

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="grid gap-[5px]">
      <Label className="text-[12px] text-[#757f9c]">{label}</Label>
      {children}
    </label>
  );
}

function SelectCell({ value, onChange, options, labelMap }: { value: string; onChange: (value: string) => void; options: string[]; labelMap: Record<string, string> }) {
  return (
    <select value={value} onChange={(event) => onChange(event.target.value)} className="h-[34px] w-full rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]">
      {options.map((key) => (
        <option key={key} value={key}>{labelMap[key]}</option>
      ))}
    </select>
  );
}


function LoadingRows() {
  return <div className="grid gap-[8px]">{[0, 1, 2].map((index) => <Skeleton key={index} className="h-[46px] rounded-[8px]" />)}</div>;
}

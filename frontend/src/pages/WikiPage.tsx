import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import { api } from '../api/client';
import type { KbView, WikiJobState, WikiPageDetailView, WikiPageType, WikiPageView } from '../api/types';
import { isEmployee, type AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS, formatDateTime } from '@/lib/enterprise-ui';

const TYPE_LABEL: Record<WikiPageType, string> = {
  summary: '文档摘要',
  entity: '实体',
  concept: '概念',
  index: '索引',
};

const TYPE_TONE: Record<WikiPageType, string> = {
  summary: 'border-[#d8e2d8] bg-[#eef6ef] text-[#2b7a57]',
  entity: 'border-[#dbe4f5] bg-[#eef2fa] text-[#3d5a91]',
  concept: 'border-[#f0dfc0] bg-[#fdf6ea] text-[#b45309]',
  index: 'border-[#e3e7f1] bg-[#f6f7fb] text-[#5b6478]',
};

const EDIT_SOURCE_LABEL: Record<string, string> = {
  pipeline: '管线生成',
  agent: 'Agent',
  user: '人工',
  revert: '回滚',
};

type Props = {
  auth: AuthSession;
  onLogout: () => void;
  kbs: KbView[];
  /** 资产中心 Tab 模式: 隐藏页头与知识库选择器(由外壳提供), 路由写回 /assets?tab=wiki */
  embedded?: boolean;
};

export default function WikiPage({ auth, onLogout, kbs, embedded = false }: Props) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedKb = searchParams.get('kb') ?? '';
  const openSlug = searchParams.get('open') ?? '';
  const selectedKb = kbs.find((kb) => kb.name === requestedKb) ?? kbs[0];
  const kbName = selectedKb?.name ?? '';
  const canWrite = Boolean(
    selectedKb && (selectedKb.permission === 'write' || selectedKb.permission === 'admin' || isEmployee(auth.user)),
  );
  const kbRoute = (value: string) =>
    embedded ? `/assets?tab=wiki&kb=${encodeURIComponent(value)}` : `/wiki?kb=${encodeURIComponent(value)}`;

  const [pages, setPages] = useState<WikiPageView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [typeFilter, setTypeFilter] = useState<'' | WikiPageType>('');
  const [searchInput, setSearchInput] = useState('');
  const [searchApplied, setSearchApplied] = useState('');
  const [statusFilter, setStatusFilter] = useState<'published' | 'archived'>('published');
  const [maxPagesInput, setMaxPagesInput] = useState('');
  const [job, setJob] = useState<WikiJobState | null>(null);
  const [detail, setDetail] = useState<WikiPageDetailView | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState<{ title: string; content: string; summary: string; aliasesText: string; version: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [archiveTarget, setArchiveTarget] = useState<WikiPageView | null>(null);
  const [revertVersion, setRevertVersion] = useState<number | null>(null);

  const load = useCallback(() => {
    if (!kbName) {
      setPages([]);
      setLoaded(true);
      return () => undefined;
    }
    let cancelled = false;
    setLoaded(false);
    const params = new URLSearchParams();
    if (searchApplied) params.set('query', searchApplied);
    if (typeFilter) params.set('page_type', typeFilter);
    params.set('status', statusFilter);
    api
      .get<WikiPageView[]>(`/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages?${params.toString()}`)
      .then((rows) => {
        if (!cancelled) setPages(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载 Wiki 失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kbName, typeFilter, searchApplied, statusFilter]);

  useEffect(() => load(), [load]);

  // 图谱页跳过来的直达详情: ?open=<slug>, 打开后吃掉参数(免回退重复弹)
  useEffect(() => {
    if (openSlug && kbName) {
      void openDetail(openSlug);
      navigate(kbRoute(kbName), { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openSlug, kbName]);

  const pollJob = useCallback(() => {
    if (!kbName) return;
    api
      .get<WikiJobState>(`/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/status`)
      .then((state) => {
        setJob(state);
        if (state.running) {
          setTimeout(pollJob, 4000);
        } else if (state.stage === '完成') {
          load();
        }
      })
      .catch(() => undefined);
  }, [kbName, load]);

  useEffect(() => {
    pollJob();
  }, [pollJob]);

  const startIngest = useCallback(
    (granularity: 'focused' | 'standard' | 'exhaustive') => {
      if (!kbName) return;
      const parsed = Number.parseInt(maxPagesInput.trim(), 10);
      const maxPages = Number.isFinite(parsed) ? Math.max(0, Math.min(2000, parsed)) : 0;
      api
        .post(`/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/ingest`, { granularity, max_pages_per_ingest: maxPages })
        .then(() => {
          notify.success('Wiki 生成已启动');
          setJob({ running: true, stage: '排队中' });
          setTimeout(pollJob, 1500);
        })
        .catch((error) => notify.error(error instanceof Error ? error.message : '启动失败'));
    },
    [kbName, pollJob, maxPagesInput],
  );

  async function openDetail(slug: string) {
    if (!kbName) return;
    try {
      const page = await api.get<WikiPageDetailView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages/${slug.split('/').map(encodeURIComponent).join('/')}`,
      );
      setDetail(page);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载页面失败');
    }
  }

  async function saveEdit() {
    if (!kbName || !detail || !editForm) return;
    setSaving(true);
    try {
      const updated = await api.patch<WikiPageView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages/${detail.slug.split('/').map(encodeURIComponent).join('/')}`,
        {
          title: editForm.title,
          content: editForm.content,
          summary: editForm.summary,
          aliases: editForm.aliasesText.split(/[\s,，]+/).filter(Boolean),
          version: editForm.version,
        },
      );
      notify.success('页面已保存');
      setEditOpen(false);
      setDetail({ ...detail, ...updated, content: updated.version ? detail.content : detail.content });
      load();
      void openDetail(detail.slug);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function doArchive() {
    if (!kbName || !archiveTarget) return;
    setSaving(true);
    try {
      await api.post(`/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages/${archiveTarget.slug.split('/').map(encodeURIComponent).join('/')}/archive`, {});
      notify.success('已归档');
      setArchiveTarget(null);
      setDetail(null);
      load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '归档失败');
    } finally {
      setSaving(false);
    }
  }

  async function doRestore() {
    if (!kbName || !detail) return;
    setSaving(true);
    try {
      const updated = await api.patch<WikiPageView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages/${detail.slug.split('/').map(encodeURIComponent).join('/')}`,
        { status: 'published' },
      );
      notify.success('已重新发布');
      setDetail({ ...detail, ...updated });
      load();
      void openDetail(detail.slug);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重新发布失败');
    } finally {
      setSaving(false);
    }
  }

  async function doRevert() {
    if (!kbName || !detail || revertVersion == null) return;
    setSaving(true);
    try {
      const updated = await api.post<WikiPageView>(
        `/api/v1/kbs/${encodeURIComponent(kbName)}/wiki/pages/${detail.slug.split('/').map(encodeURIComponent).join('/')}/revert?version=${revertVersion}`,
        {},
      );
      notify.success(`已回滚到 v${revertVersion}`);
      setRevertVersion(null);
      setDetail({ ...detail, ...updated });
      load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '回滚失败');
    } finally {
      setSaving(false);
    }
  }

  const columns: DataTableColumn<WikiPageView>[] = useMemo(
    () => [
      {
        key: 'title',
        title: '页面',
        render: (page) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{page.title}</span>
            <span className="truncate text-[11px] text-[#858b9c]">{page.slug}</span>
          </div>
        ),
      },
      {
        key: 'type',
        title: '类型',
        width: 100,
        render: (page) => (
          <span className={`inline-flex h-[22px] items-center whitespace-nowrap rounded-full border px-[8px] text-[11px] ${TYPE_TONE[page.page_type] ?? TYPE_TONE.entity}`}>
            {TYPE_LABEL[page.page_type] ?? page.page_type}
          </span>
        ),
      },
      { key: 'summary', title: '摘要', width: 300, render: (page) => <span className="block max-w-[300px] line-clamp-2 text-[12px] text-[#464c5e]">{page.summary || '-'}</span> },
      {
        key: 'links',
        title: '链接',
        width: 110,
        align: 'right',
        render: (page) => <span className="text-[#464c5e]">{page.in_links.length} 入 / {page.out_links.length} 出</span>,
      },
      { key: 'version', title: '版本', width: 70, align: 'right', render: (page) => <span>v{page.version}</span> },
      { key: 'updated', title: '更新时间', width: 150, render: (page) => <span>{formatDateTime(page.updated_at)}</span> },
    ],
    [],
  );

  function renderContent(content: string): ReactNode {
    const lines = content.split('\n');
    return lines.map((line, index) => {
      if (line.startsWith('## ')) {
        return <p key={index} className="mt-[12px] text-[13px] font-semibold text-[#18181a]">{line.slice(3)}</p>;
      }
      if (line.startsWith('# ')) {
        return <p key={index} className="text-[15px] font-semibold text-[#18181a]">{line.slice(2)}</p>;
      }
      if (line.startsWith('- ')) {
        return (
          <p key={index} className="ml-[14px] text-[13px] leading-[1.8] text-[#464c5e]">
            · {renderLinks(line.slice(2), index)}
          </p>
        );
      }
      if (!line.trim()) return <div key={index} className="h-[4px]" />;
      return <p key={index} className="text-[13px] leading-[1.8] text-[#464c5e]">{renderLinks(line, index)}</p>;
    });
  }

  function renderLinks(text: string, keyBase: number): ReactNode {
    const parts = text.split(/(\[\[[^\]]+\]\])/);
    return parts.map((part, index) => {
      const m = part.match(/^\[\[([^\]]+)\]\]$/);
      if (m) {
        return (
          <button
            key={`${keyBase}-${index}`}
            type="button"
            onClick={() => void openDetail(m[1])}
            className="text-[#2b7a57] underline decoration-dotted hover:opacity-80"
          >
            {m[1].split('/').pop()}
          </button>
        );
      }
      return <span key={`${keyBase}-${index}`}>{part}</span>;
    });
  }

  return (
    <div className={embedded ? 'min-h-full' : 'min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]'}>
      {!embedded && (
        <AppHeader
          title="知识 Wiki"
          description="管线把知识库文档蒸馏成互链词条(summary/entity/concept),全部页面保留原文出处与修订历史。"
          userName={auth.user.username}
          onLogout={onLogout}
        />
      )}

      <div className={`flex flex-wrap items-center gap-[8px] ${embedded ? 'mt-[16px]' : 'mt-[20px]'}`}>
        {!embedded && (
          <label className="flex min-w-[200px] items-center gap-[8px] text-[12px] text-[#757f9c]">
            知识库
            <select
              value={kbName}
              onChange={(event) => navigate(kbRoute(event.target.value))}
              className="h-[34px] min-w-0 flex-1 rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#18181a] outline-none focus:border-[#9cabc8]"
            >
              {kbs.length === 0 ? <option value="">暂无可访问知识库</option> : kbs.map((kb) => <option key={`${kb.kb_id}:${kb.name}`} value={kb.name}>{kb.name}</option>)}
            </select>
          </label>
        )}
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as 'published' | 'archived')}
          className="h-[30px] rounded-full border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none focus:border-[#9cabc8]"
        >
          <option value="published">已发布</option>
          <option value="archived">已归档</option>
        </select>
        <button
          type="button"
          onClick={() => setTypeFilter('')}
          className={`h-[30px] rounded-full border px-[12px] text-[12px] ${typeFilter === '' ? 'border-[#18181a] bg-[#18181a] text-white' : 'border-[#e3e7f1] bg-white text-[#5b6478] hover:border-[#9cabc8]'}`}
        >
          全部
        </button>
        {(Object.keys(TYPE_LABEL) as WikiPageType[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTypeFilter(t)}
            className={`h-[30px] rounded-full border px-[12px] text-[12px] ${typeFilter === t ? 'border-[#18181a] bg-[#18181a] text-white' : 'border-[#e3e7f1] bg-white text-[#5b6478] hover:border-[#9cabc8]'}`}
          >
            {TYPE_LABEL[t]}
          </button>
        ))}
        <div className="ml-auto flex flex-wrap items-center gap-[8px]">
          <input
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') setSearchApplied(searchInput.trim());
            }}
            placeholder="搜索标题/别名/摘要, 回车确认"
            className="h-[30px] w-[200px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none placeholder:text-[#a5adc0] focus:border-[#9cabc8]"
          />
          {canWrite && (
            <>
              <input
                value={maxPagesInput}
                onChange={(event) => setMaxPagesInput(event.target.value.replace(/[^\d]/g, '').slice(0, 4))}
                placeholder="页上限"
                title="词条页上限(0 或空 = 不限, 最多 2000; 摘要页不受限)"
                className="h-[30px] w-[64px] rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] text-[12px] text-[#18181a] outline-none placeholder:text-[#a5adc0] focus:border-[#9cabc8]"
              />
              <Button
                disabled={Boolean(job?.running)}
                onClick={() => startIngest('focused')}
                className={OUTLINE_ACTION_BUTTON_CLASS}
                variant="outline"
              >
                生成(聚焦)
              </Button>
              <Button
                disabled={Boolean(job?.running)}
                onClick={() => startIngest('standard')}
                className="h-[34px] rounded-[8px] bg-[#18181a] px-[14px] text-[13px] text-white hover:bg-[#303030] disabled:cursor-not-allowed disabled:opacity-40"
              >
                <AppIcon name="refresh" size={14} />
                {job?.running ? `生成中: ${job.stage}` : '生成 Wiki(标准)'}
              </Button>
              <Button
                disabled={Boolean(job?.running)}
                onClick={() => startIngest('exhaustive')}
                className={OUTLINE_ACTION_BUTTON_CLASS}
                variant="outline"
                title="抽取所有被点名的实体/概念，建议配合页上限使用"
              >
                生成(穷举)
              </Button>
            </>
          )}
        </div>
      </div>

      {job?.running && (
        <div className="mt-[14px] rounded-[8px] border border-[#dbe4f5] bg-[#eef2fa] px-[14px] py-[10px] text-[12px] text-[#3d5a91]">
          Wiki 生成中: {job.stage}…(大模型蒸馏需要一些时间, 页面就绪后自动刷新)
        </div>
      )}
      {job && !job.running && job.stage === '失败' && (
        <div className="mt-[14px] rounded-[8px] border border-[#f0cfc8] bg-[#fdefec] px-[14px] py-[10px] text-[12px] text-[#b3261e]">
          生成失败: {job.error}
        </div>
      )}
      {job && !job.running && job.stats && (
        <div className="mt-[14px] rounded-[8px] border border-[#d8e2d8] bg-[#eef6ef] px-[14px] py-[10px] text-[12px] text-[#2b7a57]">
          上次生成: {job.stats.documents} 份文档 → {job.stats.candidates} 个候选 → {job.stats.pages_written} 个页面
          {job.stats.errors.length ? `(失败 ${job.stats.errors.length})` : ''}
          {(job.stats.skipped_user_edited ?? 0) > 0 ? `(跳过人工页 ${job.stats.skipped_user_edited})` : ''}
        </div>
      )}

      {!kbName ? (
        <div className="mt-[24px] border-y border-[#edf0f5] py-[56px] text-center text-[13px] text-[#858b9c]">请先挂载一个知识库,再生成 Wiki。</div>
      ) : (
        <div className="mt-[18px]">
          {!loaded ? (
            <div className="grid gap-[8px]">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-[46px] rounded-[8px]" />)}</div>
          ) : (
            <DataTable
              columns={columns}
              data={pages}
              rowKey={(row) => row.id}
              size="compact"
              emptyText="还没有 Wiki 页面;点击「生成 Wiki」由管线蒸馏生成。"
              onRowClick={(page) => void openDetail(page.slug)}
            />
          )}
        </div>
      )}

      <Dialog open={Boolean(detail)} onOpenChange={(open) => { if (!open && !saving) setDetail(null); }}>
        <DialogContent className="nice-scroll flex max-h-[88vh] w-[min(880px,calc(100vw-32px))] max-w-none flex-col gap-[14px] overflow-auto rounded-[10px] p-[24px] sm:max-w-none">
          {detail && (
            <>
              <div className="flex flex-wrap items-start justify-between gap-[12px] pr-[28px]">
                <div className="min-w-0">
                  <DialogHeader>
                    <DialogTitle className="flex items-center gap-[10px]">
                      <span className="min-w-0 truncate">{detail.title}</span>
                      <span className={`inline-flex h-[22px] shrink-0 items-center whitespace-nowrap rounded-full border px-[8px] text-[11px] ${TYPE_TONE[detail.page_type]}`}>{TYPE_LABEL[detail.page_type]}</span>
                      <span className="text-[11px] font-normal text-[#a5adc0]">v{detail.version} · {EDIT_SOURCE_LABEL[detail.last_edit_source] ?? detail.last_edit_source}</span>
                    </DialogTitle>
                  </DialogHeader>
                  <p className="mt-[3px] text-[12px] text-[#858b9c]">{detail.slug}</p>
                </div>
                {canWrite && (
                  <div className="flex gap-[8px]">
                    <Button
                      variant="outline"
                      className={OUTLINE_ACTION_BUTTON_CLASS}
                      onClick={() => {
                        setEditForm({
                          title: detail.title,
                          content: detail.content,
                          summary: detail.summary,
                          aliasesText: detail.aliases.join(' '),
                          version: detail.version,
                        });
                        setEditOpen(true);
                      }}
                    >
                      编辑
                      </Button>
                      {detail.page_type !== 'index' && detail.status === 'archived' && (
                        <Button disabled={saving} onClick={() => void doRestore()} className="h-[34px] rounded-[8px] bg-[#18181a] px-[14px] text-[13px] text-white hover:bg-[#303030]">
                          重新发布
                        </Button>
                      )}
                      {detail.page_type !== 'index' && detail.status !== 'archived' && (
                        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => setArchiveTarget(detail)}>归档</Button>
                      )}
                  </div>
                )}
              </div>

              <div className="rounded-[8px] border border-[#edf0f5] bg-[#fafbfd] px-[14px] py-[12px]">
                {renderContent(detail.content || '(空页面)')}
              </div>

              <div className="grid gap-[10px] text-[12px] max-[560px]:grid-cols-1 sm:grid-cols-2">
                <div>
                  <p className="text-[11px] text-[#858b9c]">出链</p>
                  <div className="mt-[4px] flex flex-wrap gap-[6px]">
                    {detail.out_links.length === 0 && <span className="text-[#a5adc0]">无</span>}
                    {detail.out_links.map((slug) => (
                      <button key={slug} type="button" onClick={() => void openDetail(slug)} className="rounded-full border border-[#e3e7f1] bg-white px-[9px] py-[2px] text-[11px] text-[#3d5a91] hover:border-[#9cabc8]">
                        {slug.split('/').pop()}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="text-[11px] text-[#858b9c]">反链</p>
                  <div className="mt-[4px] flex flex-wrap gap-[6px]">
                    {detail.backlinks.length === 0 && <span className="text-[#a5adc0]">无</span>}
                    {detail.backlinks.map((b) => (
                      <button key={b.slug} type="button" onClick={() => void openDetail(b.slug)} className="rounded-full border border-[#e3e7f1] bg-white px-[9px] py-[2px] text-[11px] text-[#2b7a57] hover:border-[#9cabc8]">
                        {b.title}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="text-[11px] text-[#858b9c]">来源文档</p>
                  <div className="mt-[4px] flex flex-wrap gap-[6px]">
                    {detail.source_refs.length === 0 && <span className="text-[#a5adc0]">无</span>}
                    {detail.source_refs.map((ref) => (
                      <span key={ref} className="rounded-full border border-[#e3e7f1] bg-[#f6f7fb] px-[9px] py-[2px] text-[11px] text-[#5b6478]">
                        {ref.includes('|') ? ref.split('|')[1] : ref}
                      </span>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="text-[11px] text-[#858b9c]">出处锚点 ({detail.chunk_refs.length})</p>
                  <p className="mt-[4px] break-words text-[11px] text-[#a5adc0]">{detail.chunk_refs.join(' · ') || '无'}</p>
                </div>
              </div>

              {detail.revisions.length > 0 && (
                <div className="border-t border-[#edf0f5] pt-[12px]">
                  <h3 className="text-[13px] font-medium text-[#18181a]">修订历史</h3>
                  <div className="mt-[8px] grid gap-[6px]">
                    {detail.revisions.map((rev) => (
                      <div key={rev.version} className="flex items-center gap-[10px] text-[12px]">
                        <span className="w-[36px] shrink-0 text-[#5b6478]">v{rev.version}</span>
                        <span className="w-[70px] shrink-0 text-[#858b9c]">{EDIT_SOURCE_LABEL[rev.edit_source] ?? rev.edit_source}</span>
                        <span className="min-w-0 flex-1 truncate text-[#a5adc0]">{formatDateTime(rev.edited_at)}</span>
                        {canWrite && (
                          <button type="button" disabled={saving} onClick={() => setRevertVersion(rev.version)} className="shrink-0 text-[11px] text-[#2b7a57] hover:underline">
                            回滚到此版
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={editOpen} onOpenChange={(open) => { if (!open && !saving) setEditOpen(false); }}>
        <DialogContent className="flex max-h-[86vh] w-[min(720px,calc(100vw-32px))] max-w-none flex-col gap-[12px] overflow-auto rounded-[10px] p-[24px] sm:max-w-none">
          <DialogHeader><DialogTitle>编辑页面</DialogTitle></DialogHeader>
          {editForm && (
            <div className="grid gap-[12px]">
              <label className="grid gap-[5px]"><Label className="text-[12px] text-[#757f9c]">标题</Label>
                <Input value={editForm.title} onChange={(event) => setEditForm({ ...editForm, title: event.target.value })} />
              </label>
              <label className="grid gap-[5px]"><Label className="text-[12px] text-[#757f9c]">摘要</Label>
                <Input value={editForm.summary} onChange={(event) => setEditForm({ ...editForm, summary: event.target.value })} />
              </label>
              <label className="grid gap-[5px]"><Label className="text-[12px] text-[#757f9c]">别名(空格分隔)</Label>
                <Input value={editForm.aliasesText} onChange={(event) => setEditForm({ ...editForm, aliasesText: event.target.value })} />
              </label>
              <label className="grid gap-[5px]"><Label className="text-[12px] text-[#757f9c]">内容(Markdown, [[slug]] 互链)</Label>
                <textarea
                  value={editForm.content}
                  onChange={(event) => setEditForm({ ...editForm, content: event.target.value })}
                  className="min-h-[280px] w-full rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] py-[10px] font-mono text-[12px] leading-[1.7] text-[#18181a] outline-none focus:border-[#9cabc8]"
                />
              </label>
              <div className="flex justify-end gap-[8px] pt-[4px]">
                <Button variant="outline" disabled={saving} onClick={() => setEditOpen(false)}>取消</Button>
                <Button disabled={saving || !editForm.title.trim()} onClick={() => void saveEdit()} className="bg-[#18181a] text-white hover:bg-[#303030]">{saving ? '保存中' : '保存'}</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(archiveTarget)}
        onOpenChange={(open) => { if (!open) setArchiveTarget(null); }}
        title={<>归档「{archiveTarget?.title ?? ''}」?</>}
        description="归档后页面从浏览与图谱中隐藏, 可在「已归档」筛选中找到并恢复。"
        confirmText="归档"
        destructive={false}
        loading={saving}
        onConfirm={() => void doArchive()}
      />
      <ConfirmDialog
        open={revertVersion != null}
        onOpenChange={(open) => { if (!open) setRevertVersion(null); }}
        title={`回滚到 v${revertVersion ?? ''}?`}
        description="当前内容会先保存为一个新修订, 随时可以再回滚回来。"
        confirmText="回滚"
        destructive={false}
        loading={saving}
        onConfirm={() => void doRevert()}
      />
    </div>
  );
}

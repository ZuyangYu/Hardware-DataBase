import { useCallback, useEffect, useState } from 'react';
import { Plus, RefreshCw, Settings2, Upload } from 'lucide-react';

import { api, apiDownload } from '../../api/client';
import type { DepartmentView } from '../../api/types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label, Textarea } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { ConfirmDialog } from '@/components/ConfirmDialog';

type Skill = {
  skill_ref: string;
  version: string;
  approval_state: string;
  department_id: string;
  owner_id?: string;
  catalog_source?: string;
  share_scope?: string;
  execution_bundle_ref: string;
  bundle_invocation_available: boolean;
  package_archive_digest?: string;
  package_files?: string[];
  package_review_flags?: string[];
  package_build_state?: string;
  text_archive_digest?: string;
  text_files?: string[];
  review_flags?: string[];
  metadata?: { summary?: string; skill_name?: string; submitted_input_schema?: Record<string, unknown>; submitted_output_schema?: Record<string, unknown> };
};
type Bundle = {
  bundle_ref: string;
  bundle_digest: string;
  profile_ref: string;
  approval_state: string;
  catalog_source: string;
  network_policy_ref: string;
  required_capabilities: string[];
  metadata?: { image_digest?: string };
};

export type SkillsPanelViewProps = {
  skills: Skill[];
  bundles: Bundle[];
  departments: DepartmentView[];
  busy: boolean;
  skillRef: string;
  version: string;
  departmentId: string;
  binding: Record<string, string>;
  onSkillRef: (value: string) => void;
  onVersion: (value: string) => void;
  onDepartmentId: (value: string) => void;
  onBinding: (ref: string, bundleRef: string) => void;
  onRegister: () => Promise<boolean> | void;
  textSkillRef: string;
  textVersion: string;
  textContent: string;
  textDepartmentId: string;
  onTextSkillRef: (value: string) => void;
  onTextVersion: (value: string) => void;
  onTextContent: (value: string) => void;
  onTextDepartmentId: (value: string) => void;
  onBind: (skill: Skill) => void;
  onState: (skill: Skill, action: string) => void;
  onDelete?: (skill: Skill) => Promise<boolean> | void;
  onRefresh: () => void;
  packageFile?: File | null;
  inputSchema?: string;
  onPackageFile?: (file: File | null) => void;
  onInputSchema?: (value: string) => void;
  onUploadPackage?: () => Promise<boolean> | void;
  textPackageFile?: File | null;
  onTextPackageFile?: (file: File | null) => void;
  onUploadTextPackage?: () => Promise<string | false>;
  onApproveSource?: (skill: Skill) => void;
  onRetryBuild?: (skill: Skill) => void;
  onBundleState?: (bundle: Bundle, action: string) => void;
  onDownloadSource?: (skill: Skill) => void;
  pipIndex?: string;
  pipAllowedHosts?: string;
  onPipIndex?: (value: string) => void;
  onSavePipIndex?: () => Promise<boolean> | void;
};

type SkillFilter = 'all' | 'pending' | 'enabled' | 'revoked';

export function SkillDepartmentSelect({ id, value, departments, onChange }: {
  id: string;
  value: string;
  departments: DepartmentView[];
  onChange: (value: string) => void;
}) {
  const options = departments.filter((department) => department.name !== 'system');
  return (
    <div>
      <Label htmlFor={id}>可见部门</Label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}
        className="mt-[6px] h-[36px] w-full rounded-[8px] border border-[#e1e5ee] bg-white px-[10px] text-[13px] text-[#464c5e]">
        <option value="">请选择部门</option>
        <option value="*">全部员工（所有部门）</option>
        {options.map((department) => (
          <option key={department.id} value={String(department.id)}>{department.name}（ID {department.id}）</option>
        ))}
      </select>
      <p className="mt-[5px] text-[11px] text-[#858b9c]">
        {options.length ? '可指定一个部门或全部员工；仍需员工订阅，执行型还需沙箱授权。' : '暂无部门；如需指定部门，请先在部门管理创建。'}
      </p>
    </div>
  );
}

const APPROVAL_LABELS: Record<string, string> = {
  draft: '草稿待审核',
  reviewed: '已审核',
  tested: '待启用',
  enabled: '已启用',
  revoked: '已撤销',
};

const BUILD_LABELS: Record<string, string> = {
  uploaded: '等待构建 / 可重试',
  source_reviewed: '源包已审核 · 待构建',
  building: '构建中',
  built: '构建完成',
  failed: '构建失败',
};

const REVIEW_FLAG_LABELS: Record<string, string> = {
  execution_markers_present: '包含命令或执行相关关键词，需人工确认',
  prompt_injection_pattern: '包含类似提示注入的文本，需人工确认',
  pseudo_system_marker: '包含伪系统提示标记，需人工确认',
};

function approvalVariant(state: string): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (state === 'enabled') return 'default';
  if (state === 'revoked') return 'destructive';
  if (state === 'tested') return 'secondary';
  return 'outline';
}

function SkillBundleCard({ bundle, busy, onBundleState }: {
  bundle: Bundle;
  busy: boolean;
  onBundleState?: (bundle: Bundle, action: string) => void;
}) {
  const nextAction: Record<string, { action: string; label: string }> = {
    draft: { action: 'review', label: '审核构建镜像' },
    reviewed: { action: 'tested', label: '标记镜像测试通过' },
    tested: { action: 'enable', label: '启用构建镜像' },
    enabled: { action: 'revoke', label: '撤销构建镜像' },
  };
  const next = nextAction[bundle.approval_state];

  return (
    <section data-skill-build-review={bundle.bundle_ref} className="border-t border-[#edf0f4] pt-[20px]">
      <div className="flex flex-wrap items-start justify-between gap-[10px]">
        <div className="min-w-0">
          <h4 className="text-[14px] font-semibold text-[#18181a]">构建镜像</h4>
          <div className="mt-[5px] break-all font-mono text-[12px] text-[#697084]">{bundle.bundle_ref}</div>
        </div>
        <Badge variant={approvalVariant(bundle.approval_state)}>
          {APPROVAL_LABELS[bundle.approval_state] ?? bundle.approval_state}
        </Badge>
      </div>
      <p className="mt-[8px] text-[12px] text-[#697084]">构建结果须单独审核、人工标记测试通过并启用。</p>
      <details className="mt-[10px] text-[12px] text-[#697084]">
        <summary className="cursor-pointer font-medium text-[#596174]">查看镜像资料</summary>
        <div className="mt-[10px] space-y-[6px] rounded-[10px] bg-[#f8f9fb] p-[12px]">
          <div>运行档位：{bundle.profile_ref || '未指定'}</div>
          <div>目录来源：{bundle.catalog_source === 'curated' ? '已审核模板' : bundle.catalog_source}</div>
          <div>网络策略：{bundle.network_policy_ref || '未附加策略引用'}</div>
          <div>所需能力：{bundle.required_capabilities.join('、') || '无'}</div>
          <div className="break-all"><span className="font-medium">Bundle SHA-256：</span>{bundle.bundle_digest}</div>
          {bundle.metadata?.image_digest && (
            <div className="break-all"><span className="font-medium">镜像 ID：</span>{bundle.metadata.image_digest}</div>
          )}
        </div>
      </details>
      {next && onBundleState && (
        <div className="mt-[14px] flex justify-end">
          <Button variant={next.action === 'revoke' ? 'destructive' : 'default'} size="sm"
            disabled={busy} onClick={() => onBundleState(bundle, next.action)}>
            {next.label}
          </Button>
        </div>
      )}
    </section>
  );
}

export function SkillsPanelView({ skills, bundles, departments, busy, skillRef, version, departmentId,
  binding, onSkillRef, onVersion, onDepartmentId, onBinding,
  onRegister, textSkillRef, textVersion, textContent, textDepartmentId,
  onTextSkillRef, onTextVersion, onTextContent, onTextDepartmentId,
  onBind, onState, onDelete, onRefresh, packageFile, inputSchema, onPackageFile,
  onInputSchema, onUploadPackage, textPackageFile, onTextPackageFile, onUploadTextPackage,
  onApproveSource, onRetryBuild, onBundleState, onDownloadSource,
  pipIndex, pipAllowedHosts, onPipIndex, onSavePipIndex }: SkillsPanelViewProps) {
  const [filter, setFilter] = useState<SkillFilter>('all');
  const [search, setSearch] = useState('');
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [dialog, setDialog] = useState<'upload' | 'text' | 'text-manual' | 'settings' | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null);
  const selectableDepartments = departments.filter((department) => department.name !== 'system');
  const departmentExists = (value: string) => value === '*' || selectableDepartments.some((department) => String(department.id) === value);
  const pendingCount = skills.filter((skill) => !['enabled', 'revoked'].includes(skill.approval_state)).length;
  const filteredSkills = [...skills].sort((a, b) => {
    const priority = (skill: Skill) => skill.approval_state === 'draft' ? 0
      : skill.approval_state === 'reviewed' ? 1 : skill.approval_state === 'tested' ? 2
        : skill.approval_state === 'enabled' ? 3 : 4;
    return priority(a) - priority(b) || a.skill_ref.localeCompare(b.skill_ref);
  }).filter((skill) => {
    const matchesFilter = filter === 'all'
      || (filter === 'pending' && !['enabled', 'revoked'].includes(skill.approval_state))
      || (filter === 'enabled' && skill.approval_state === 'enabled')
      || (filter === 'revoked' && skill.approval_state === 'revoked');
    const query = search.trim().toLowerCase();
    const matchesSearch = !query || [skill.skill_ref, skill.version, skill.department_id, skill.execution_bundle_ref]
      .some((value) => value.toLowerCase().includes(query));
    return matchesFilter && matchesSearch;
  });
  const linkedBundleRefs = new Set(skills.map((skill) => skill.execution_bundle_ref).filter(Boolean));
  const unlinkedBundles = bundles.filter((bundle) =>
    bundle.bundle_ref.startsWith('bundle/skill-') && !linkedBundleRefs.has(bundle.bundle_ref),
  );
  const selectedSkill = filteredSkills.find((skill) => skill.skill_ref === selectedRef)
    ?? filteredSkills[0] ?? null;
  const selectedBundle = bundles.find((bundle) => bundle.bundle_ref === selectedSkill?.execution_bundle_ref);
  const selectedIsPackage = Boolean(selectedSkill?.package_archive_digest);
  const nextSkillAction: Record<string, { action: string; label: string }> = {
    draft: { action: 'review', label: '审核 Skill' },
    reviewed: { action: 'tested', label: '标记测试通过' },
    tested: { action: 'enable', label: '启用 Skill' },
  };
  const next = selectedSkill && nextSkillAction[selectedSkill.approval_state];
  const nextActionBlocked = Boolean(
    selectedSkill && (
      busy
      || (selectedIsPackage && selectedSkill.approval_state === 'draft'
        && (selectedSkill.package_build_state !== 'built' || !selectedSkill.execution_bundle_ref))
      || (selectedSkill.approval_state === 'tested'
        && !!selectedSkill.execution_bundle_ref
        && selectedBundle?.approval_state !== 'enabled')
    ),
  );
  const nextActionHint = selectedSkill?.approval_state === 'draft' && selectedIsPackage
    && (selectedSkill.package_build_state !== 'built' || !selectedSkill.execution_bundle_ref)
    ? '请先完成源包审核并构建镜像。'
    : selectedSkill?.approval_state === 'tested' && !!selectedSkill.execution_bundle_ref
      && selectedBundle?.approval_state !== 'enabled'
      ? '请先启用关联的构建镜像。'
      : selectedSkill?.approval_state === 'tested'
        ? '管理员确认测试结果后即可启用。'
        : '确认内容与适用范围后推进审核状态。';

  async function submitAndClose(action?: () => Promise<boolean> | void) {
    if (action && await action()) {
      if (dialog === 'upload' || dialog === 'text' || dialog === 'text-manual') {
        setSelectedRef(dialog === 'upload' ? skillRef.trim() : textSkillRef.trim());
        setFilter('all');
        setSearch('');
      }
      setDialog(null);
    }
  }

  async function uploadTextAndClose() {
    const ref = await onUploadTextPackage?.();
    if (!ref) return;
    setSelectedRef(ref);
    setFilter('all');
    setSearch('');
    setDialog(null);
  }

  return (
    <section data-admin-skills>
      <header className="flex flex-wrap items-center justify-between gap-[16px] border-b border-[#e8ebf2] pb-[20px]">
        <div>
          <h2 className="text-[16px] font-semibold tracking-[-0.02em] text-[#18181a]">Skill 上传与审核</h2>
          <p className="mt-[4px] text-[12px] text-[#697084]">管理上传、构建和审核。选择一项查看详情与下一步操作。</p>
        </div>
        <div className="flex flex-wrap items-center gap-[8px]">
          <Button variant="outline" size="sm" onClick={() => setDialog('settings')}><Settings2 />依赖源</Button>
          <Button variant="outline" size="sm" onClick={() => setDialog('text')}><Plus />导入文本 Skill</Button>
          <Button size="sm" onClick={() => setDialog('upload')}><Upload />上传执行型 Skill</Button>
        </div>
      </header>

      <div className="mt-[20px] grid items-stretch gap-[18px] lg:h-[min(68dvh,680px)] lg:grid-cols-[minmax(280px,320px)_minmax(0,1fr)]">
      <section data-skill-inventory className="flex min-h-0 min-w-0 flex-col rounded-[14px] border border-[#e8ebf2] bg-white shadow-[0_2px_10px_rgba(0,0,0,0.04)] lg:h-full">
        <div className="border-b border-[#edf0f4] px-[16px] py-[14px]">
          <div className="flex items-center justify-between gap-[8px]">
            <h3 className="text-[14px] font-semibold text-[#18181a]">Skill 目录</h3>
            <Button variant="ghost" size="icon-sm" aria-label="刷新 Skill 目录" disabled={busy} onClick={onRefresh}><RefreshCw /></Button>
          </div>
          <div className="mt-[8px] text-[12px] text-[#858b9c]">{skills.length} 项 · {pendingCount} 项待处理</div>
          <Input id="skill-search" className="mt-[14px]" type="search" aria-label="搜索 Skill" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索名称、版本或部门" />
          <select aria-label="按审核状态筛选" value={filter} onChange={(event) => setFilter(event.target.value as SkillFilter)} className="mt-[8px] h-[34px] w-full rounded-[8px] border border-[#e1e5ee] bg-white px-[10px] text-[12px] text-[#464c5e]">
            <option value="all">全部状态</option>
            <option value="pending">待处理</option>
            <option value="enabled">已启用</option>
            <option value="revoked">已撤销</option>
          </select>
        </div>
        {filteredSkills.length === 0 ? (
           <p className="px-[16px] py-[32px] text-center text-[12px] text-[#858b9c] lg:flex lg:min-h-0 lg:flex-1 lg:items-center lg:justify-center">{skills.length ? '没有匹配的 Skill' : '还没有登记 Skill'}</p>
         ) : (
           <div className="min-h-0 max-h-[300px] overflow-y-auto py-[6px] lg:flex-1 lg:max-h-none" aria-label="Skill 列表">
            {filteredSkills.map((skill) => (
              <button key={skill.skill_ref} type="button" data-skill-item={skill.skill_ref}
                aria-controls="skill-review-detail"
                aria-pressed={skill.skill_ref === selectedSkill?.skill_ref}
                onClick={() => setSelectedRef(skill.skill_ref)}
                className={`w-full border-l-[3px] px-[14px] py-[12px] text-left transition-colors hover:bg-[#f8f9fc] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#315d91] ${
                  skill.skill_ref === selectedSkill?.skill_ref ? 'border-[#315d91] bg-[#f4f7fb]' : 'border-transparent'
                }`}>
                <span className="flex items-start justify-between gap-[8px]">
                  <span className="min-w-0 break-all text-[13px] font-medium text-[#202633]">{skill.skill_ref}</span>
                  <Badge variant={approvalVariant(skill.approval_state)} className="text-[10px]">{APPROVAL_LABELS[skill.approval_state] ?? skill.approval_state}</Badge>
                </span>
                <span className="mt-[5px] block text-[11px] text-[#858b9c]">
                   {skill.catalog_source === 'employee' ? `员工个人上传 · ${skill.package_archive_digest ? '执行型' : '文本型'}` : skill.package_archive_digest ? '执行型' : skill.execution_bundle_ref ? '固定任务' : '文本型'} · v{skill.version}{skill.department_id === '*' ? ' · 全部员工' : skill.department_id ? ` · 部门 ${skill.department_id}` : ''}
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
       <section id="skill-review-detail" data-skill-detail className="min-h-0 min-w-0 overflow-y-auto rounded-[14px] border border-[#e8ebf2] bg-white p-[20px] shadow-[0_2px_10px_rgba(0,0,0,0.04)] max-[600px]:p-[16px] lg:h-full">
        {selectedSkill ? (
          <>
            <div className="flex flex-wrap items-start justify-between gap-[12px]">
              <div className="min-w-0">
                <div className="text-[11px] font-medium text-[#858b9c]">当前选中 · {selectedIsPackage ? '执行型 Skill' : selectedSkill.execution_bundle_ref ? '固定任务 Skill' : '文本 Skill'}</div>
                <h3 className="mt-[6px] break-all text-[18px] font-semibold text-[#18181a]">{selectedSkill.skill_ref}</h3>
                {selectedSkill.metadata?.summary && <p className="mt-[6px] text-[12px] text-[#697084]">{selectedSkill.metadata.summary}</p>}
                {selectedSkill.catalog_source === 'employee' && <p className="mt-[6px] text-[12px] text-[#476a9f]">员工个人上传 · 员工 ID {selectedSkill.owner_id} · {selectedSkill.share_scope === 'department' ? '已共享至部门' : '仅上传者可见'}</p>}
              </div>
              <Badge variant={approvalVariant(selectedSkill.approval_state)}>{APPROVAL_LABELS[selectedSkill.approval_state] ?? selectedSkill.approval_state}</Badge>
            </div>
            <div className="mt-[18px] grid gap-[12px] border-b border-[#edf0f4] pb-[18px] sm:grid-cols-3">
              <div><div className="text-[11px] text-[#858b9c]">版本</div><div className="mt-[3px] text-[13px] text-[#464c5e]">{selectedSkill.version}</div></div>
              <div><div className="text-[11px] text-[#858b9c]">可见范围</div><div className="mt-[3px] text-[13px] text-[#464c5e]">{selectedSkill.catalog_source === 'employee' ? selectedSkill.share_scope === 'department' ? `部门 ID ${selectedSkill.department_id}（上传者已共享）` : '仅上传者（审核通过后可共享部门）' : selectedSkill.department_id === '*' ? '全部员工（所有部门）' : selectedSkill.department_id ? `部门 ID ${selectedSkill.department_id}` : '未限定部门'}</div></div>
              <div><div className="text-[11px] text-[#858b9c]">构建状态</div><div className="mt-[3px] text-[13px] text-[#464c5e]">{selectedIsPackage ? BUILD_LABELS[selectedSkill.package_build_state ?? ''] ?? selectedSkill.package_build_state : '不适用'}</div></div>
            </div>

            <section data-skill-next-action className="mt-[18px] flex flex-wrap items-center justify-between gap-[12px] rounded-[12px] border border-[#dce7fb] bg-[#f7faff] px-[14px] py-[13px]">
              <div className="min-w-0">
                <div className="text-[11px] font-medium text-[#476a9f]">当前下一步</div>
                <p className="mt-[3px] text-[13px] font-medium text-[#243e68]">{next ? next.label : selectedSkill.approval_state === 'revoked' ? 'Skill 已撤销' : '当前状态无需操作'}</p>
                <p className="mt-[3px] text-[11px] leading-[1.5] text-[#687894]">{nextActionHint}“测试通过”由管理员人工标记，不表示系统自动完成了功能测试。</p>
              </div>
              {next && <Button size="sm" disabled={nextActionBlocked} onClick={() => onState(selectedSkill, next.action)}>{next.label}</Button>}
            </section>

            {selectedIsPackage && (
              <div data-skill-source-review={selectedSkill.skill_ref} className="border-b border-[#edf0f4] py-[20px]">
                <div className="flex flex-wrap items-center justify-between gap-[10px]">
                  <h4 className="text-[14px] font-semibold text-[#18181a]">源包审核</h4>
                  <div className="flex flex-wrap gap-[8px]">
                    <Button variant="outline" size="sm" disabled={busy} onClick={() => onDownloadSource?.(selectedSkill)}>下载源包审核</Button>
                    {selectedSkill.catalog_source === 'employee' && selectedSkill.package_build_state === 'uploaded' && selectedSkill.approval_state === 'draft' && onApproveSource && (
                      <Button variant="outline" size="sm" disabled={busy} onClick={() => onApproveSource(selectedSkill)}>审核源包并允许构建</Button>
                    )}
                    {selectedSkill.approval_state === 'draft' && onRetryBuild && (
                      selectedSkill.catalog_source === 'employee' ? selectedSkill.package_build_state === 'source_reviewed' : selectedSkill.package_build_state === 'uploaded'
                    ) && <Button variant="outline" size="sm" disabled={busy} onClick={() => onRetryBuild(selectedSkill)}>{selectedSkill.catalog_source === 'employee' ? '构建镜像' : '重试构建'}</Button>}
                  </div>
                </div>
                <p className="mt-[7px] text-[12px] text-[#697084]">先检查 ZIP 源文件，再审核 Skill 与构建镜像。</p>
                {Boolean(selectedSkill.package_review_flags?.length) && (
                  <div className="mt-[12px] rounded-[9px] bg-[#fff8ed] px-[12px] py-[10px] text-[12px] text-[#885c22]">
                    文本规则提示（需人工复核，不是安全结论）：{selectedSkill.package_review_flags?.map((flag) => REVIEW_FLAG_LABELS[flag] ?? flag).join('；')}
                  </div>
                )}
                <details className="mt-[10px] text-[12px] text-[#697084]">
                  <summary className="cursor-pointer font-medium text-[#596174]">查看文件清单与源包 SHA-256</summary>
                  <div className="mt-[8px] break-all">文件：{selectedSkill.package_files?.join('、') || '未返回文件清单'}<br />源包 SHA-256：{selectedSkill.package_archive_digest}</div>
                </details>
                {selectedSkill.catalog_source === 'employee' && (
                  <details className="mt-[10px] text-[12px] text-[#697084]">
                    <summary className="cursor-pointer font-medium text-[#596174]">查看员工提交的参数 Schema</summary>
                    <div className="mt-[8px]">输入参数</div>
                    <pre className="mt-[5px] max-h-[220px] overflow-auto whitespace-pre-wrap break-all rounded-[8px] bg-[#f8f9fb] p-[10px] font-mono">{JSON.stringify(selectedSkill.metadata?.submitted_input_schema ?? {}, null, 2)}</pre>
                    <div className="mt-[8px]">输出参数</div>
                    <pre className="mt-[5px] max-h-[220px] overflow-auto whitespace-pre-wrap break-all rounded-[8px] bg-[#f8f9fb] p-[10px] font-mono">{JSON.stringify(selectedSkill.metadata?.submitted_output_schema ?? {}, null, 2)}</pre>
                  </details>
                )}
              </div>
            )}

            {selectedSkill.text_archive_digest && (
              <div data-text-skill-source-review={selectedSkill.skill_ref} className="border-b border-[#edf0f4] py-[20px]">
                <div className="flex flex-wrap items-center justify-between gap-[10px]">
                  <h4 className="text-[14px] font-semibold text-[#18181a]">文本源包审核</h4>
                  <Button variant="outline" size="sm" disabled={busy} onClick={() => onDownloadSource?.(selectedSkill)}>下载 ZIP 审核</Button>
                </div>
                <p className="mt-[7px] text-[12px] text-[#697084]">SKILL.md 与随附 Markdown 已合并为审核文本；文件不会作为脚本运行。</p>
                {Boolean(selectedSkill.review_flags?.length) && (
                  <p className="mt-[10px] rounded-[9px] bg-[#fff8ed] px-[12px] py-[10px] text-[12px] text-[#885c22]">
                    文本规则提示（需人工复核）：{selectedSkill.review_flags?.map((flag) => REVIEW_FLAG_LABELS[flag] ?? flag).join('；')}
                  </p>
                )}
                <details className="mt-[10px] text-[12px] text-[#697084]">
                  <summary className="cursor-pointer font-medium text-[#596174]">查看文件清单与 SHA-256</summary>
                  <div className="mt-[8px] break-all">文件：{selectedSkill.text_files?.join('、') || '未返回文件清单'}<br />ZIP SHA-256：{selectedSkill.text_archive_digest}</div>
                </details>
              </div>
            )}

            {selectedSkill.execution_bundle_ref && !selectedBundle && (
              <p className="mt-[16px] text-[12px] text-[#a15c00]">关联镜像未在目录中找到，请刷新目录并检查构建状态。</p>
            )}

            {!selectedIsPackage && selectedSkill.catalog_source !== 'employee' && selectedSkill.approval_state === 'draft' && !selectedSkill.execution_bundle_ref && (
              <details className="border-b border-[#edf0f4] py-[18px]">
                <summary className="cursor-pointer text-[12px] font-medium text-[#596174]">绑定固定任务（可选）</summary>
                <p className="mt-[8px] text-[12px] text-[#697084]">留空则仅作为文本提示；绑定后需要部门 ID 和已启用的固定任务。</p>
                <div className="mt-[10px] flex flex-wrap gap-[8px]">
                  <select aria-label={`${selectedSkill.skill_ref} 固定任务`} value={binding[selectedSkill.skill_ref] ?? ''}
                    onChange={(event) => onBinding(selectedSkill.skill_ref, event.target.value)}
                    className="h-[34px] min-w-[180px] flex-1 rounded-[8px] border border-[#e1e5ee] bg-white px-[10px] text-[12px]">
                    <option value="">仅文本 / 不绑定</option>
                    {bundles.filter((item) => item.approval_state === 'enabled' && item.catalog_source === 'curated' && !item.network_policy_ref).map((item) => <option key={item.bundle_ref} value={item.bundle_ref}>{item.bundle_ref}</option>)}
                  </select>
                  <Button variant="outline" size="sm" disabled={busy || !binding[selectedSkill.skill_ref] || !selectedSkill.department_id} onClick={() => onBind(selectedSkill)}>绑定固定任务</Button>
                </div>
                {!selectedSkill.department_id && <p className="mt-[6px] text-[11px] text-[#a15c00]">此 Skill 未设置部门 ID，无法绑定执行任务。</p>}
              </details>
            )}

            {selectedBundle && <div className="mt-[18px]"><SkillBundleCard bundle={selectedBundle} busy={busy} onBundleState={onBundleState} /></div>}

            <details className="mt-[18px] border-t border-[#edf0f4] pt-[16px]" data-skill-more-actions>
              <summary className="cursor-pointer text-[12px] font-medium text-[#596174]">更多审核操作</summary>
              <div className="mt-[10px] rounded-[10px] bg-[#fafbfc] p-[12px]">
                <p className="text-[11px] leading-[1.6] text-[#697084]">{selectedIsPackage ? '源包和构建结果需分别审核。' : '确认内容与适用范围后推进状态。'}撤销或删除属于高影响操作，请确认后再执行。</p>
                <div className="mt-[10px] flex flex-wrap gap-[8px]">
                  {selectedSkill.approval_state !== 'revoked' && <Button variant="destructive" size="sm" disabled={busy} onClick={() => onState(selectedSkill, 'revoke')}>撤销 Skill</Button>}
                  {selectedSkill.approval_state === 'revoked' && onDelete && <Button variant="destructive" size="sm" disabled={busy} onClick={() => setDeleteTarget(selectedSkill)}>删除 Skill</Button>}
                </div>
              </div>
            </details>
          </>
        ) : (
          <div className="flex min-h-[280px] flex-col items-center justify-center text-center">
            <p className="text-[14px] font-medium text-[#464c5e]">{skills.length ? '没有匹配的 Skill' : '选择上传或新建文本 Skill'}</p>
            <p className="mt-[5px] text-[12px] text-[#858b9c]">{skills.length ? '调整左侧搜索或筛选条件。' : '创建后在这里查看审核状态与构建结果。'}</p>
          </div>
        )}
         {unlinkedBundles.length > 0 && (
           <details className="mt-[18px] rounded-[12px] border border-[#e8ebf2] bg-[#fbfcfe] px-[14px] py-[12px]">
             <summary className="cursor-pointer text-[12px] font-medium text-[#596174]">未关联 Skill 的构建镜像（{unlinkedBundles.length}）</summary>
             <div className="mt-[14px] grid gap-[18px]">
               {unlinkedBundles.map((bundle) => <SkillBundleCard key={bundle.bundle_ref} bundle={bundle} busy={busy} onBundleState={onBundleState} />)}
             </div>
           </details>
         )}
       </section>
       </div>
      <Dialog open={dialog !== null} onOpenChange={(open) => { if (!open) setDialog(null); }}>
        <DialogContent className="flex max-h-[88dvh] w-[min(680px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[18px] p-0 sm:max-w-none">
          <DialogHeader className="shrink-0 border-b border-[#e8ebf2] px-[24px] py-[18px] pr-[56px]">
            <DialogTitle className="text-[16px] font-semibold text-[#18181a]">{dialog === 'upload' ? '上传执行型 Skill' : dialog === 'text' ? '导入文本 Skill' : dialog === 'text-manual' ? '快速创建文本 Skill' : '构建依赖源'}</DialogTitle>
            <p className="text-[12px] leading-[1.6] text-[#758098]">
              {dialog === 'upload' ? '上传源包后进入构建与审核流程。' : dialog === 'text' ? '导入 Markdown 内容，生成待审核文本 Skill。' : dialog === 'text-manual' ? '快速登记一段文本说明，内容不会作为脚本执行。' : '配置构建时使用的依赖源。'}
            </p>
          </DialogHeader>

          <div className="min-h-0 overflow-y-auto px-[24px] py-[20px]">

          {dialog === 'upload' && (
            <div data-skill-upload className="space-y-[16px]">
              <p className="text-[12px] leading-[1.6] text-[#697084]">上传 ZIP 后自动构建镜像；脚本只会在后续授权运行时进入独立沙箱。</p>
              <div className="grid gap-[12px] sm:grid-cols-2">
                <div><Label htmlFor="skill-ref">Skill 引用</Label><Input id="skill-ref" className="mt-[6px]" value={skillRef} onChange={(event) => onSkillRef(event.target.value)} placeholder="skill/demo-ohms-law" /><p className="mt-[5px] text-[11px] text-[#858b9c]">内部唯一标识，建议 skill/英文短名；不能包含空格。</p></div>
                <div><Label htmlFor="skill-version">版本</Label><Input id="skill-version" className="mt-[6px]" value={version} onChange={(event) => onVersion(event.target.value)} placeholder="1.0.0" /></div>
              </div>
              <SkillDepartmentSelect id="skill-department" value={departmentId} departments={departments} onChange={onDepartmentId} />
              <div className="rounded-[10px] border border-dashed border-[#ccd4e0] bg-[#f8fafc] p-[14px]">
                <Label htmlFor="skill-package">Skill ZIP <span className="font-normal text-[#858b9c]">· 最大 8 MiB</span></Label>
                <Input id="skill-package" className="mt-[8px] bg-white" type="file" accept=".zip,application/zip" onChange={(event) => onPackageFile?.(event.target.files?.[0] ?? null)} />
                <p className="mt-[7px] break-all text-[11px] text-[#697084]">{packageFile ? `已选择：${packageFile.name} · ${(packageFile.size / (1024 * 1024)).toFixed(2)} MiB` : '根目录需含 SKILL.md 与 entrypoint.py。'}</p>
              </div>
              <details className="rounded-[10px] border border-[#e8ebf2] px-[12px] py-[10px]">
                <summary className="cursor-pointer text-[12px] font-medium text-[#596174]">输入输出约定与参数 Schema</summary>
                <p className="mt-[10px] text-[11px] leading-[1.6] text-[#697084]">脚本读取 /work/in/parameters.json，产物写入 /work/out。可选 requirements.lock，需锁定版本及 wheel SHA-256。</p>
                <Label htmlFor="skill-schema" className="mt-[10px]">参数 JSON Schema</Label>
                <Textarea id="skill-schema" rows={6} className="mt-[6px] font-mono text-[12px]" value={inputSchema ?? ''} onChange={(event) => onInputSchema?.(event.target.value)} />
              </details>
              <div className="sticky bottom-0 z-10 flex justify-end gap-[8px] border-t border-[#edf0f4] bg-white/95 pt-[14px] backdrop-blur-sm">
                <Button variant="outline" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
                <Button disabled={busy || !packageFile || !skillRef.trim() || !departmentExists(departmentId) || !version.trim()} onClick={() => void submitAndClose(onUploadPackage)}>上传并构建</Button>
              </div>
            </div>
          )}

          {dialog === 'text' && (
            <div data-skill-text-draft className="space-y-[16px]">
              <p className="text-[12px] leading-[1.6] text-[#697084]">上传包含 SKILL.md 的文件夹 ZIP；可附带 references/、scripts/ 等 UTF-8 文本资源。系统从 YAML 中读取名称、说明与可选版本，生成待审核草稿。</p>
              <SkillDepartmentSelect id="text-skill-department" value={textDepartmentId} departments={departments} onChange={onTextDepartmentId} />
              <div data-text-skill-import className="rounded-[10px] border border-dashed border-[#ccd4e0] bg-[#f8fafc] p-[14px]">
                <Label htmlFor="text-skill-package">文本 Skill ZIP <span className="font-normal text-[#858b9c]">· 最大 8 MiB</span></Label>
                <Input id="text-skill-package" className="mt-[8px] bg-white" type="file" accept=".zip,application/zip" onChange={(event) => onTextPackageFile?.(event.target.files?.[0] ?? null)} />
                <p className="mt-[7px] break-all text-[11px] text-[#697084]">{textPackageFile ? `已选择：${textPackageFile.name}` : 'SKILL.md 需包含 YAML name / description；可直接打包内容或打包单个同名文件夹。支持 UTF-8 文本资源。'}</p>
              </div>
              <p className="text-[11px] text-[#858b9c]">随附文件按原路径保留，审核后只读挂载、按需读取；脚本作为文本参考保存，不会被执行。</p>
              <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-[8px] border-t border-[#edf0f4] bg-white/95 pt-[14px] backdrop-blur-sm">
                <Button variant="ghost" disabled={busy} onClick={() => setDialog('text-manual')}>没有 ZIP？快速创建</Button>
                <div className="flex gap-[8px]">
                <Button variant="outline" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
                <Button disabled={busy || !textPackageFile || !departmentExists(textDepartmentId)} onClick={() => void uploadTextAndClose()}>上传并创建草稿</Button>
                </div>
              </div>
            </div>
          )}

          {dialog === 'text-manual' && (
            <div data-skill-text-manual className="space-y-[16px]">
              <p className="text-[12px] leading-[1.6] text-[#697084]">适合快速登记一段文本说明。内容会进入待审核草稿，不会作为脚本执行。</p>
              <SkillDepartmentSelect id="text-manual-department" value={textDepartmentId} departments={departments} onChange={onTextDepartmentId} />
              <div className="grid gap-[12px] sm:grid-cols-2">
                <div><Label htmlFor="text-skill-ref">Skill 引用</Label><Input id="text-skill-ref" className="mt-[6px]" value={textSkillRef} onChange={(event) => onTextSkillRef(event.target.value)} placeholder="skill/demo-bom-review" /><p className="mt-[5px] text-[11px] text-[#858b9c]">内部唯一标识，建议 skill/英文短名。</p></div>
                <div><Label htmlFor="text-skill-version">版本</Label><Input id="text-skill-version" className="mt-[6px]" value={textVersion} onChange={(event) => onTextVersion(event.target.value)} placeholder="1.0.0" /></div>
              </div>
              <div><Label htmlFor="text-skill-content">提示内容</Label><Textarea id="text-skill-content" className="mt-[6px]" rows={8} value={textContent} onChange={(event) => onTextContent(event.target.value)} placeholder="写明用途、适用场景与建议步骤。" /></div>
              <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-[8px] border-t border-[#edf0f4] bg-white/95 pt-[14px] backdrop-blur-sm">
                <Button variant="ghost" disabled={busy} onClick={() => setDialog('text')}>返回 ZIP 导入</Button>
                <div className="flex gap-[8px]">
                  <Button variant="outline" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
                  <Button disabled={busy || !textSkillRef.trim() || !textVersion.trim() || !textContent.trim() || !departmentExists(textDepartmentId)} onClick={() => void submitAndClose(onRegister)}>登记文本草稿</Button>
                </div>
              </div>
            </div>
          )}

          {dialog === 'settings' && (
            <div data-skill-dependencies className="space-y-[14px]">
              <p className="text-[12px] leading-[1.6] text-[#697084]">仅从运维批准的 HTTPS 主机获取固定版本与哈希的依赖；依赖在镜像构建时安装。</p>
              <div><Label htmlFor="skill-pip-index">pip 镜像源</Label><Input id="skill-pip-index" className="mt-[6px]" value={pipIndex ?? ''} onChange={(event) => onPipIndex?.(event.target.value)} placeholder="https://pypi.org/simple/" /></div>
              <p className="break-all text-[11px] text-[#858b9c]">允许的主机：{pipAllowedHosts || '未配置'}</p>
              <div className="sticky bottom-0 z-10 flex justify-end gap-[8px] border-t border-[#edf0f4] bg-white/95 pt-[14px] backdrop-blur-sm">
                <Button variant="outline" disabled={busy} onClick={() => setDialog(null)}>取消</Button>
                <Button disabled={busy || !onSavePipIndex} onClick={() => void submitAndClose(onSavePipIndex)}>保存依赖源</Button>
              </div>
            </div>
          )}
          </div>
        </DialogContent>
      </Dialog>
      <ConfirmDialog open={deleteTarget !== null} onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
        title={`删除 ${deleteTarget?.skill_ref ?? ''}？`}
        description="仅可删除已撤销的 Skill；将清除目录登记及订阅。若有关联构建镜像，须先撤销镜像。历史任务、审核记录及已上传的源包留存。"
        loading={busy} onConfirm={() => {
          if (!deleteTarget || !onDelete) return;
          void Promise.resolve(onDelete(deleteTarget)).then((success) => { if (success) setDeleteTarget(null); });
        }} />
    </section>
  );
}

export default function SkillsPanel({ departments }: { departments: DepartmentView[] }) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [bundles, setBundles] = useState<Bundle[]>([]);
  const [busy, setBusy] = useState(false);
  const [skillRef, setSkillRef] = useState('');
  const [version, setVersion] = useState('1.0.0');
  const [textSkillRef, setTextSkillRef] = useState('');
  const [textVersion, setTextVersion] = useState('1.0.0');
  const [textContent, setTextContent] = useState('');
  const [textDepartmentId, setTextDepartmentId] = useState('');
  const [departmentId, setDepartmentId] = useState('');
  const [binding, setBinding] = useState<Record<string, string>>({});
  const [packageFile, setPackageFile] = useState<File | null>(null);
  const [textPackageFile, setTextPackageFile] = useState<File | null>(null);
  const [inputSchema, setInputSchema] = useState(`{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}`);
  const [pipIndex, setPipIndex] = useState('');
  const [pipAllowedHosts, setPipAllowedHosts] = useState('');

  const load = useCallback(async () => {
    try {
      const [skillResult, bundleResult, pipResult] = await Promise.all([
        api.get<{ items: Skill[] }>('/api/v1/sandbox/skills'),
        api.get<{ items: Bundle[] }>('/api/v1/sandbox/bundles'),
        api.get<{ index_url: string; allowed_hosts: string }>('/api/v1/sandbox/skill-dependencies'),
      ]);
      setSkills(skillResult.items);
      setBundles(bundleResult.items);
      setPipIndex(pipResult.index_url);
      setPipAllowedHosts(pipResult.allowed_hosts);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : 'Skill 目录读取失败');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function act(action: () => Promise<unknown>, message: string): Promise<boolean> {
    setBusy(true);
    return Promise.resolve().then(action).then(() => { notify.success(message); void load(); return true; })
      .catch((error) => { notify.error(error instanceof Error ? error.message : '操作失败'); void load(); return false; })
      .finally(() => setBusy(false));
  }

  async function encodePackage(file: File): Promise<string> {
    if (file.size > 8 * 1024 * 1024) throw new Error('Skill ZIP 超过 8 MiB');
    const data = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result ?? '').split(',')[1] ?? '');
      reader.onerror = () => reject(new Error('无法读取 Skill ZIP'));
      reader.readAsDataURL(file);
    });
    return data;
  }

  const buildPayload = () => ({
    profile_ref: 'profile/dev-l1', base_bundle_ref: 'bundle/report',
    input_schema: JSON.parse(inputSchema) as Record<string, unknown>,
  });

  return <SkillsPanelView skills={skills} bundles={bundles} departments={departments} busy={busy}
    skillRef={skillRef} version={version} departmentId={departmentId} binding={binding}
    onSkillRef={setSkillRef} onVersion={setVersion} onDepartmentId={setDepartmentId}
    onBinding={(ref, value) => setBinding((current) => ({ ...current, [ref]: value }))}
    packageFile={packageFile} inputSchema={inputSchema} onPackageFile={setPackageFile} onInputSchema={setInputSchema}
    textPackageFile={textPackageFile} onTextPackageFile={setTextPackageFile}
    onUploadTextPackage={async () => {
      let createdRef = '';
      const success = await act(async () => {
        if (!textPackageFile) throw new Error('请选择文本 Skill ZIP');
        const result = await api.post<{ skill: Skill }>('/api/v1/sandbox/skills/text-packages', {
          department_id: textDepartmentId.trim(), package_base64: await encodePackage(textPackageFile),
        });
        createdRef = result.skill.skill_ref;
        setTextPackageFile(null);
      }, '文本 Skill 已导入为待审核草稿');
      return success ? createdRef : false;
    }}
    pipIndex={pipIndex} pipAllowedHosts={pipAllowedHosts} onPipIndex={setPipIndex}
    onSavePipIndex={() => act(() => api.put('/api/v1/sandbox/skill-dependencies', { index_url: pipIndex.trim() }), '依赖源已更新')}
    onUploadPackage={() => act(async () => {
      if (!packageFile) throw new Error('请选择 Skill ZIP');
      const payload = buildPayload();
      await api.post('/api/v1/sandbox/skills/packages', {
        skill_ref: skillRef.trim(), version: version.trim(), department_id: departmentId.trim(),
        package_base64: await encodePackage(packageFile), input_schema: payload.input_schema,
      });
      setPackageFile(null);
    }, 'Skill 源包已保存并构建，请审核构建产物')}
    onApproveSource={(skill) => act(() => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/approve-source`), '源包已审核，可以构建镜像')}
    onRetryBuild={(skill) => act(async () => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/build`,
      skill.catalog_source === 'employee' ? {
        profile_ref: 'profile/dev-l1', base_bundle_ref: 'bundle/report',
        input_schema: skill.metadata?.submitted_input_schema ?? {},
        output_schema: skill.metadata?.submitted_output_schema ?? {},
      } : buildPayload()), '构建完成')}
    onDownloadSource={(skill) => act(() => apiDownload.blob(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/source`, 'skill-source.zip'), '源包下载已开始')}
    onBundleState={(bundle, action) => act(() => api.post(`/api/v1/sandbox/bundles/${encodeURIComponent(bundle.bundle_ref)}/${action}`), '构建结果状态已更新')}
    textSkillRef={textSkillRef} textVersion={textVersion} textContent={textContent} textDepartmentId={textDepartmentId}
    onTextSkillRef={setTextSkillRef} onTextVersion={setTextVersion} onTextContent={setTextContent} onTextDepartmentId={setTextDepartmentId}
    onRegister={() => act(async () => {
      await api.post('/api/v1/sandbox/skills', {
        skill_ref: textSkillRef.trim(), version: textVersion.trim(), content: textContent, department_id: textDepartmentId.trim(),
      });
      setTextSkillRef('');
      setTextContent('');
    }, 'Skill 草稿已登记')}
    onBind={(skill) => {
      const bundle = bundles.find((item) => item.bundle_ref === binding[skill.skill_ref] && item.approval_state === 'enabled');
      if (bundle) act(() => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/bind-execution`, {
        bundle_ref: bundle.bundle_ref, bundle_digest: bundle.bundle_digest, profile_ref: bundle.profile_ref,
      }), '固定任务绑定完成，请审核 Skill');
    }}
    onState={(skill, action) => act(() => api.post(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/${action}`), `Skill ${action} 完成`)}
    onDelete={(skill) => act(() => api.delete(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}`), 'Skill 已删除')}
    onRefresh={() => void load()}
  />;
}

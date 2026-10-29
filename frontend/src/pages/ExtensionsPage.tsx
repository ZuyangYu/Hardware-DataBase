import { useCallback, useEffect, useState } from 'react';
import { Blocks, Check, Code2, Download, FileText, Play, PlugZap, RefreshCw, Search, ShieldCheck, Upload } from 'lucide-react';

import { api, apiDownload } from '../api/client';
import type {
  McpCatalogItem,
  McpCatalogResponse,
  McpListFlags,
  McpWriteProposal,
  McpWriteProposalListResponse,
} from '../api/types';
import type { AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

type Props = { auth: AuthSession };

type ExecutableSkill = {
  skill_ref: string;
  version: string;
  bundle_ref: string;
  input_schema: Record<string, unknown>;
  subscribed?: boolean;
  description?: string;
  kind?: 'text' | 'executable';
  is_owner?: boolean;
  share_scope?: string;
};
type SkillTask = { task_id: string; status: string; error_code?: string };
type SkillArtifact = { artifact_id: string; filename: string; state: string };
type PersonalSkill = {
  skill_ref: string;
  version: string;
  approval_state: string;
  share_scope: string;
  summary: string;
  kind: 'text' | 'executable';
  package_build_state: string;
  department_id: string;
  created_at: string;
};
type PersonalUploadOptions = { kind: 'text' | 'executable'; name: string; version: string; inputSchema: string };

const PERSONAL_STATUS: Record<string, string> = {
  draft: '待管理员审核', reviewed: '已审核 · 待测试', tested: '待启用',
  enabled: '已启用', revoked: '已撤销',
};

const EMPTY_FLAGS: McpListFlags = {
  enabled: false,
  write_enabled: false,
  prerequisites_met: false,
  prerequisites_reason: '',
};

export type ExtensionsViewProps = {
  flags: McpListFlags;
  items: McpCatalogItem[];
  loaded: boolean;
  skillsLoaded?: boolean;
  busySlug: string | null;
  userDepartmentId?: string;
  onRefresh: () => void;
  onSubscribe: (item: McpCatalogItem) => void;
  onUnsubscribe: (item: McpCatalogItem) => void;
  onToggleWrite: (item: McpCatalogItem, allow: boolean) => void;
  proposals?: McpWriteProposal[];
  proposalBusyId?: string | null;
  onConfirmProposal?: (proposal: McpWriteProposal) => void;
  onRejectProposal?: (proposal: McpWriteProposal) => void;
  onDownloadProposal?: (proposal: McpWriteProposal) => void;
  skills?: ExecutableSkill[];
  personalSkills?: PersonalSkill[];
  personalLoaded?: boolean;
  personalBusy?: boolean;
  sharingRef?: string | null;
  onUploadPersonal?: (file: File, options: PersonalUploadOptions) => Promise<boolean>;
  onSharePersonal?: (skill: PersonalSkill, share: boolean) => void;
  skillParameters?: Record<string, string>;
  skillBusy?: string | null;
  skillTask?: SkillTask | null;
  skillArtifacts?: SkillArtifact[];
  onSkillParametersChange?: (ref: string, value: string) => void;
  onRunSkill?: (item: ExecutableSkill) => void;
  onToggleSkill?: (item: ExecutableSkill) => void;
  onRefreshSkillTask?: () => void;
  onDownloadSkillArtifact?: (artifact: SkillArtifact) => void;
};

type ExtensionSection = 'skills' | 'mcp';

function skillDisplay(skill: ExecutableSkill) {
  const content = (skill.description || '').replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, '');
  const title = content.match(/^#\s+(.+)$/m)?.[1]?.trim() || skill.skill_ref.replace(/^skill\//, '');
  const summary = skill.description?.match(/^description:\s*(.+)$/m)?.[1]?.trim()
    || content.split('\n').map((line) => line.trim()).find((line) => line && !line.startsWith('#') && !line.startsWith('---'))
    || '管理员审核的扩展能力，添加后即可按授权使用。';
  return { title, summary };
}

/** Pure view: employee catalog with subscription and write-consent controls. */
export function ExtensionsView({
  flags,
  items,
  loaded,
  skillsLoaded = true,
  busySlug,
  userDepartmentId = '',
  onRefresh,
  onSubscribe,
  onUnsubscribe,
  onToggleWrite,
  proposals = [],
  proposalBusyId = null,
  onConfirmProposal,
  onRejectProposal,
  onDownloadProposal,
  skills = [],
  personalSkills = [],
  personalLoaded = true,
  personalBusy = false,
  sharingRef = null,
  onUploadPersonal,
  onSharePersonal,
  skillParameters = {},
  skillBusy = null,
  skillTask = null,
  skillArtifacts = [],
  onSkillParametersChange,
  onRunSkill,
  onToggleSkill,
  onRefreshSkillTask,
  onDownloadSkillArtifact,
}: ExtensionsViewProps) {
  const [activeSection, setActiveSection] = useState<ExtensionSection>('skills');
  const [skillQuery, setSkillQuery] = useState('');
  const [mcpQuery, setMcpQuery] = useState('');
  const [onlyAdded, setOnlyAdded] = useState(false);
  const [testingSkill, setTestingSkill] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [personalFile, setPersonalFile] = useState<File | null>(null);
  const [personalKind, setPersonalKind] = useState<'text' | 'executable'>('text');
  const [personalName, setPersonalName] = useState('');
  const [personalVersion, setPersonalVersion] = useState('1.0.0');
  const [personalSchema, setPersonalSchema] = useState('{\n  "type": "object",\n  "properties": {},\n  "additionalProperties": false\n}');
  const addedSkills = skills.filter((skill) => skill.subscribed).length;
  const subscribedServices = items.filter((item) => item.subscribed).length;
  const visibleSkills = skills.filter((skill) => {
    const display = skillDisplay(skill);
    return (!onlyAdded || skill.subscribed) &&
      `${skill.skill_ref} ${display.title} ${display.summary}`.toLocaleLowerCase().includes(skillQuery.trim().toLocaleLowerCase());
  }).sort((a, b) => Number(Boolean(b.subscribed)) - Number(Boolean(a.subscribed)));
  const visibleServices = items.filter((item) =>
    (!onlyAdded || item.subscribed) &&
    `${item.name} ${item.slug} ${item.description || ''} ${item.tags.join(' ')}`.toLocaleLowerCase().includes(mcpQuery.trim().toLocaleLowerCase()),
  ).sort((a, b) => Number(Boolean(b.subscribed)) - Number(Boolean(a.subscribed)));

  return (
    <div className="mx-auto min-h-full max-w-[1380px] px-[48px] pb-[56px] pt-[32px] max-[900px]:px-[16px]">
      <AppHeader
        title="个人扩展"
        description="订阅已审核的 Skill 与 MCP 服务，按需添加到你的对话中。"
      />

      <div className="mt-[26px] grid gap-[12px] sm:grid-cols-2">
        <div className="flex items-center gap-[14px] rounded-[18px] border border-[#dce7fb] bg-gradient-to-r from-[#eef4ff] to-white px-[20px] py-[17px]">
          <div className="flex size-[42px] shrink-0 items-center justify-center rounded-[12px] bg-white text-[#2862c4] shadow-sm"><Blocks className="size-[20px]" /></div>
          <div className="min-w-0">
            <p className="text-[12px] text-[#60708c]">我的 Skill</p>
            <p className="mt-[2px] text-[19px] font-semibold leading-tight text-[#1c3156]">{skillsLoaded ? addedSkills : '—'}<span className="ml-[5px] text-[12px] font-normal text-[#73819a]">/ {skillsLoaded ? skills.length : '—'} 已添加</span></p>
          </div>
        </div>
        <div className="flex items-center gap-[14px] rounded-[18px] border border-[#e7eaf0] bg-white px-[20px] py-[17px]">
          <div className="flex size-[42px] shrink-0 items-center justify-center rounded-[12px] bg-[#f2f5f9] text-[#55657d]"><PlugZap className="size-[20px]" /></div>
          <div className="min-w-0">
            <p className="text-[12px] text-[#60708c]">我的 MCP 服务</p>
            <p className="mt-[2px] text-[19px] font-semibold leading-tight text-[#1c3156]">{loaded ? subscribedServices : '—'}<span className="ml-[5px] text-[12px] font-normal text-[#73819a]">/ {loaded ? items.length : '—'} 已订阅</span></p>
          </div>
        </div>
      </div>

      <div className="mt-[28px] flex flex-wrap items-end justify-between gap-[12px] border-b border-[#e6eaf1]">
        <div role="tablist" aria-label="扩展类型" className="flex gap-[22px]">
          {([
            { id: 'skills', label: 'Skill 库', count: skills.length, icon: Blocks },
            { id: 'mcp', label: 'MCP 服务', count: items.length, icon: PlugZap },
          ] as const).map(({ id, label, count, icon: Icon }) => (
            <button key={id} id={`extensions-${id}-tab`} type="button" role="tab"
              aria-selected={activeSection === id} aria-controls={`extensions-${id}-panel`}
              tabIndex={activeSection === id ? 0 : -1}
              onClick={() => { setActiveSection(id); setOnlyAdded(false); }}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight' && event.key !== 'Home' && event.key !== 'End') return;
                event.preventDefault();
                const next: ExtensionSection = event.key === 'Home' ? 'skills' : event.key === 'End' ? 'mcp' : id === 'skills' ? 'mcp' : 'skills';
                setActiveSection(next);
                setOnlyAdded(false);
                document.getElementById(`extensions-${next}-tab`)?.focus();
              }}
              className={`-mb-px flex items-center gap-[7px] border-b-2 px-[2px] pb-[13px] text-[13px] font-medium transition-colors focus-visible:rounded focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#1d4ed8] ${activeSection === id ? 'border-[#2563eb] text-[#1d4ed8]' : 'border-transparent text-[#7c879a] hover:text-[#34445d]'}`}>
              <Icon className="size-[16px]" />{label}<span className={`rounded-full px-[7px] py-[1px] text-[11px] ${activeSection === id ? 'bg-[#e8f0ff] text-[#1d4ed8]' : 'bg-[#f1f3f6] text-[#798499]'}`}>{count}</span>
            </button>
          ))}
        </div>
        <Button variant="outline" size="sm" className="mb-[9px]" onClick={onRefresh} disabled={!loaded || !skillsLoaded || !personalLoaded}>
          <RefreshCw className="size-[14px]" />刷新
        </Button>
      </div>

      <section id="extensions-skills-panel" role="tabpanel" aria-labelledby="extensions-skills-tab" hidden={activeSection !== 'skills'} data-sandbox-skills className="pt-[24px]">
        <div className="flex flex-wrap items-end justify-between gap-[16px]">
          <div>
            <h2 className="text-[18px] font-semibold text-[#1d2a40]">已审核的 Skill</h2>
            <p className="mt-[5px] text-[13px] text-[#758098]">文本参考会在对话中使用；执行型 Skill 可在独立沙箱中测试。</p>
          </div>
          <Button variant="outline" onClick={() => setUploadOpen(true)} disabled={!onUploadPersonal || !userDepartmentId}><Upload className="size-[15px]" />上传个人 Skill</Button>
        </div>
        {personalLoaded && personalSkills.length > 0 && (
          <div className="mt-[18px] rounded-[16px] border border-[#dce7fb] bg-[#f7faff] p-[16px]">
            <div className="flex flex-wrap items-center justify-between gap-[6px]">
              <h3 className="text-[13px] font-semibold text-[#274470]">我上传的 Skill</h3>
              <span className="text-[11px] text-[#73819a]">审核启用后可添加；共享后同部门员工可自行订阅</span>
            </div>
            <div className="mt-[10px] grid gap-[8px]">
              {personalSkills.map((skill) => (
                <div key={skill.skill_ref} data-personal-skill={skill.skill_ref} className="flex flex-wrap items-center gap-[10px] rounded-[10px] border border-[#e2eafa] bg-white px-[13px] py-[10px]">
                  <div className="min-w-0 flex-1">
                    <p className="break-all text-[12px] font-medium text-[#273a59]">{skill.skill_ref} <span className="text-[11px] font-normal text-[#8995a9]">v{skill.version} · {skill.kind === 'executable' ? '执行型' : '文本型'}</span></p>
                    {skill.summary && <p className="mt-[3px] line-clamp-1 text-[11px] text-[#758098]">{skill.summary}</p>}
                    {skill.kind === 'executable' && skill.approval_state === 'draft' && <p className="mt-[3px] text-[11px] text-[#758098]">{skill.package_build_state === 'source_reviewed' ? '源包已审核，等待构建' : skill.package_build_state === 'built' ? '镜像已构建，等待审核' : '等待管理员审查源包'}</p>}
                  </div>
                  <Badge variant={skill.approval_state === 'enabled' ? 'secondary' : 'outline'}>{PERSONAL_STATUS[skill.approval_state] ?? skill.approval_state}</Badge>
                  {skill.approval_state === 'enabled' && skill.department_id === userDepartmentId && (
                    <Button variant="outline" size="sm" disabled={sharingRef !== null || !onSharePersonal}
                      onClick={() => onSharePersonal?.(skill, skill.share_scope !== 'department')}>
                      {skill.share_scope === 'department' ? '停止部门共享' : '共享到部门'}
                    </Button>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="mt-[20px] flex flex-wrap items-center justify-between gap-[10px]">
          <p className="text-[12px] text-[#7c879a]">可用目录 · {skills.length} 项</p>
          <div className="flex flex-wrap items-center gap-[10px]">
            <label className="relative block max-[500px]:w-full">
              <Search className="pointer-events-none absolute left-[11px] top-1/2 size-[15px] -translate-y-1/2 text-[#97a2b4]" />
              <span className="sr-only">搜索 Skill</span>
              <input type="search" value={skillQuery} onChange={(event) => setSkillQuery(event.target.value)} placeholder="搜索 Skill"
                className="h-[36px] w-[210px] rounded-[9px] border border-[#e0e5ee] bg-white pl-[34px] pr-[10px] text-[13px] text-[#34445d] outline-none placeholder:text-[#9ba5b5] focus:border-[#6b9ae8] focus:ring-2 focus:ring-[#e5efff] max-[500px]:w-full" />
            </label>
            <Button variant={onlyAdded ? 'secondary' : 'outline'} size="sm" onClick={() => setOnlyAdded((value) => !value)} aria-pressed={onlyAdded}>仅看已添加</Button>
          </div>
        </div>
        {!skillsLoaded ? (
          <div className="mt-[20px] grid gap-[14px] lg:grid-cols-2"><Skeleton className="h-[198px] rounded-[16px]" /><Skeleton className="h-[198px] rounded-[16px]" /></div>
        ) : visibleSkills.length === 0 ? (
          <div className="mt-[20px] rounded-[16px] border border-dashed border-[#dce2eb] bg-white px-[24px] py-[48px] text-center text-[13px] text-[#858b9c]">
            {skills.length === 0 ? '暂无可用的 Skill，管理员审核启用后会显示在这里。' : '没有找到符合条件的 Skill，试试其他关键词或取消筛选。'}
          </div>
        ) : (
          <div className="mt-[20px] grid items-start gap-[14px] lg:grid-cols-2">
            {visibleSkills.map((skill) => {
              const isText = skill.kind === 'text';
              const { title, summary } = skillDisplay(skill);
              const isTesting = testingSkill === skill.skill_ref;
              return (
                <article key={skill.skill_ref} data-skill-catalog-item={skill.skill_ref} className="min-w-0 rounded-[16px] border border-[#e6eaf1] bg-white p-[20px] shadow-[0_3px_16px_rgba(28,45,75,0.04)]">
                  <div className="flex items-start gap-[12px]">
                    <div className={`flex size-[40px] shrink-0 items-center justify-center rounded-[11px] ${isText ? 'bg-[#ecf4ff] text-[#2864c7]' : 'bg-[#eef8f4] text-[#228666]'}`}>
                      {isText ? <FileText className="size-[19px]" /> : <Code2 className="size-[19px]" />}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-start justify-between gap-[6px]">
                        <h3 className="min-w-0 break-words text-[15px] font-semibold leading-[1.5] text-[#1d2a40]">{title}</h3>
                        {skill.subscribed && <Badge variant="secondary" className="bg-[#eaf6ef] text-[#237a51]"><Check className="size-[12px]" />已添加</Badge>}
                      </div>
                      <p className="mt-[2px] break-all font-mono text-[11px] text-[#919bad]">{skill.skill_ref} · {skill.version}</p>
                    </div>
                  </div>
                  <p className="mt-[14px] line-clamp-2 min-h-[42px] break-words text-[13px] leading-[1.65] text-[#536077]">{summary}</p>
                  <div className="mt-[12px] flex flex-wrap items-center gap-[7px] text-[11px]">
                    <span className={`rounded-full px-[9px] py-[4px] ${isText ? 'bg-[#f0f5ff] text-[#3768ae]' : 'bg-[#edf8f3] text-[#287a60]'}`}>{isText ? '文本参考' : '沙箱执行'}</span>
                    <span className="text-[#858fa2]">{isText ? '添加后用于对话' : '添加后可在对话中调用或在此测试'}</span>
                    {skill.is_owner && <span className="rounded-full bg-[#eef2fb] px-[9px] py-[4px] text-[#476a9f]">我的上传</span>}
                    {skill.share_scope === 'department' && <span className="rounded-full bg-[#eef2fb] px-[9px] py-[4px] text-[#476a9f]">部门共享</span>}
                  </div>
                  {!isText && <p className="mt-[7px] truncate font-mono text-[11px] text-[#919bad]" title={skill.bundle_ref}>固定任务：{skill.bundle_ref}</p>}
                  <div className="mt-[16px] flex flex-wrap items-center gap-[8px] border-t border-[#eef0f4] pt-[15px]">
                    <Button variant={skill.subscribed ? 'outline' : 'default'} disabled={skillBusy !== null || !onToggleSkill} onClick={() => onToggleSkill?.(skill)}>
                      {skill.subscribed ? '移除 Skill' : '添加 Skill'}
                    </Button>
                    {!isText && skill.subscribed && (
                      <Button variant="outline" aria-expanded={isTesting} onClick={() => setTestingSkill(isTesting ? null : skill.skill_ref)}>
                        <Play className="size-[14px]" />{isTesting ? '收起测试' : '测试 Skill'}
                      </Button>
                    )}
                    {skill.description && <details className="w-full text-[12px] text-[#536077]"><summary className="w-fit cursor-pointer py-[3px] text-[#476a9f] hover:text-[#1d4ed8]">查看完整说明</summary><pre className="mt-[6px] max-h-[200px] overflow-auto whitespace-pre-wrap break-words rounded-[9px] bg-[#f7f9fc] p-[12px] font-sans leading-[1.6]">{skill.description}</pre></details>}
                  </div>
                  {!isText && skill.subscribed && isTesting && (
                    <div className="mt-[14px] rounded-[12px] border border-[#e3e9f2] bg-[#f8faff] p-[14px]">
                      <p className="text-[12px] text-[#64718a]">固定任务：<span className="break-all font-mono text-[#34445d]">{skill.bundle_ref}</span></p>
                      <details className="mt-[10px] text-[12px] text-[#536077]"><summary className="w-fit cursor-pointer text-[#476a9f]">查看参数格式</summary><pre className="mt-[7px] max-h-[180px] overflow-auto whitespace-pre-wrap break-all rounded-[8px] bg-white p-[10px] font-mono">{JSON.stringify(skill.input_schema, null, 2)}</pre></details>
                      <label className="mt-[12px] block text-[12px] font-medium text-[#464c5e]">JSON 参数
                        <textarea data-skill-parameters={skill.skill_ref} spellCheck={false} value={skillParameters[skill.skill_ref] ?? '{}'} onChange={(event) => onSkillParametersChange?.(skill.skill_ref, event.target.value)}
                          className="mt-[6px] block min-h-[90px] w-full rounded-[8px] border border-[#dfe5ef] bg-white p-[10px] font-mono text-[12px] outline-none focus:border-[#6b9ae8] focus:ring-2 focus:ring-[#e5efff]" />
                      </label>
                      <Button className="mt-[10px]" disabled={skillBusy !== null || !onRunSkill} onClick={() => onRunSkill?.(skill)}><Play className="size-[14px]" />在沙箱中运行</Button>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
        {skillTask && (
          <div className="mt-[18px] rounded-[16px] border border-[#dce6f7] bg-[#f6f9ff] p-[18px]" aria-live="polite">
            <div className="text-[13px] font-medium text-[#243e68]">最近任务 · {skillTask.status}</div>
            <div className="mt-[4px] break-all font-mono text-[11px] text-[#687894]">{skillTask.task_id}{skillTask.error_code ? ` · ${skillTask.error_code}` : ''}</div>
            <div className="mt-[12px] flex flex-wrap gap-[8px]">
              <Button variant="outline" size="sm" onClick={onRefreshSkillTask}><RefreshCw className="size-[13px]" />刷新状态</Button>
              {skillArtifacts.filter((item) => item.state === 'committed').map((item) => (
                <Button key={item.artifact_id} variant="outline" size="sm" onClick={() => onDownloadSkillArtifact?.(item)}><Download className="size-[13px]" />下载 {item.filename}</Button>
              ))}
            </div>
          </div>
        )}
      </section>

      <section id="extensions-mcp-panel" role="tabpanel" aria-labelledby="extensions-mcp-tab" hidden={activeSection !== 'mcp'} className="pt-[24px]">
        <div className="flex flex-wrap items-end justify-between gap-[16px]">
          <div>
            <h2 className="text-[18px] font-semibold text-[#1d2a40]">MCP 服务</h2>
            <p className="mt-[5px] text-[13px] text-[#758098]">订阅管理员登记的外部工具，按授权在对话中使用。</p>
          </div>
          <div className="flex flex-wrap items-center gap-[10px]">
            <label className="relative block max-[500px]:w-full"><Search className="pointer-events-none absolute left-[11px] top-1/2 size-[15px] -translate-y-1/2 text-[#97a2b4]" /><span className="sr-only">搜索 MCP 服务</span>
              <input type="search" value={mcpQuery} onChange={(event) => setMcpQuery(event.target.value)} placeholder="搜索服务"
                className="h-[36px] w-[210px] rounded-[9px] border border-[#e0e5ee] bg-white pl-[34px] pr-[10px] text-[13px] text-[#34445d] outline-none placeholder:text-[#9ba5b5] focus:border-[#6b9ae8] focus:ring-2 focus:ring-[#e5efff] max-[500px]:w-full" />
            </label>
            <Button variant={onlyAdded ? 'secondary' : 'outline'} size="sm" onClick={() => setOnlyAdded((value) => !value)} aria-pressed={onlyAdded}>仅看已订阅</Button>
          </div>
        </div>
        <div data-mcp-egress-notice className="mt-[18px] flex gap-[12px] rounded-[12px] border border-[#f0dfb8] bg-[#fffbf2] px-[15px] py-[12px] text-[12px] leading-[1.6] text-[#785e2b]">
          <ShieldCheck className="mt-[2px] size-[17px] shrink-0" />
          <p><span className="font-semibold">数据外发说明：</span>调用外部服务时，查询内容会离开本系统并交给管理员登记的服务提供方；返回内容是不可信数据，不会进入知识库证据链，也不会获得引用编号。</p>
        </div>
        {!flags.enabled && <div data-mcp-disabled className="mt-[14px] rounded-[10px] bg-[#f3f5f9] px-[14px] py-[11px] text-[12px] text-[#64718a]">MCP 服务未启用（MCP_ENABLED=false），当前没有可订阅的 MCP 服务。</div>}
        {flags.enabled && !flags.prerequisites_met && <div data-mcp-prerequisite className="mt-[14px] rounded-[10px] bg-[#fff4f2] px-[14px] py-[11px] text-[12px] text-[#b5352d]">扩展已开启但前置条件未满足：{flags.prerequisites_reason || '未知原因'}</div>}

        {!loaded ? (
          <div className="mt-[20px] grid gap-[14px] lg:grid-cols-2"><Skeleton className="h-[190px] rounded-[16px]" /><Skeleton className="h-[190px] rounded-[16px]" /></div>
        ) : visibleServices.length === 0 ? (
          <div data-mcp-empty className="mt-[20px] rounded-[16px] border border-dashed border-[#dce2eb] bg-white px-[24px] py-[48px] text-center text-[13px] text-[#858b9c]">
            {items.length === 0 ? '暂无可订阅的扩展服务。管理员登记并启用服务后会出现在这里。' : '没有找到符合条件的服务，试试其他关键词或取消筛选。'}
          </div>
        ) : (
          <div className="mt-[20px] grid items-start gap-[14px] lg:grid-cols-2">
            {visibleServices.map((item) => (
              <article key={item.slug} data-mcp-catalog-item={item.slug} className="min-w-0 rounded-[16px] border border-[#e6eaf1] bg-white p-[20px] shadow-[0_3px_16px_rgba(28,45,75,0.04)]">
                <div className="flex items-start gap-[12px]">
                  <div className="flex size-[40px] shrink-0 items-center justify-center rounded-[11px] bg-[#eef3fa] text-[#51729d]">{item.icon ? <span className="text-[19px]">{item.icon}</span> : <PlugZap className="size-[19px]" />}</div>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-start justify-between gap-[6px]">
                      <h3 className="min-w-0 break-words text-[15px] font-semibold leading-[1.5] text-[#1d2a40]">{item.name}</h3>
                      {item.subscribed && <Badge data-mcp-subscribed={item.slug} variant="secondary" className="bg-[#eaf6ef] text-[#237a51]"><Check className="size-[12px]" />已订阅</Badge>}
                    </div>
                    <p className="mt-[2px] break-all font-mono text-[11px] text-[#919bad]">{item.slug}</p>
                  </div>
                </div>
                {item.description && <p className="mt-[14px] line-clamp-3 break-words text-[13px] leading-[1.65] text-[#536077]">{item.description}</p>}
                <div className="mt-[13px] flex flex-wrap items-center gap-[7px] text-[11px] text-[#798499]">
                  <span className="rounded-full bg-[#f2f5f9] px-[9px] py-[4px]">工具 {item.tool_count} 个 · 只读 {item.read_only_tool_count} 个</span>
                  {item.tags.map((tag) => <span key={tag} className="rounded-full bg-[#f2f5f9] px-[9px] py-[4px] text-[#536077]">{tag}</span>)}
                </div>
                <div className="mt-[16px] flex flex-wrap items-center gap-[12px] border-t border-[#eef0f4] pt-[15px]">
                  {item.subscribed ? (
                    <Button variant="outline" disabled={busySlug === item.slug} onClick={() => onUnsubscribe(item)}>退订</Button>
                  ) : (
                    <Button disabled={busySlug === item.slug || !flags.enabled} onClick={() => onSubscribe(item)}>订阅</Button>
                  )}
                  {item.subscribed && item.writable && !flags.write_enabled && <span className="text-[12px] text-[#858b9c]">该服务包含写工具，写通道当前未启用。</span>}
                  {item.subscribed && item.writable && flags.write_enabled && (
                    <label className="flex cursor-pointer items-center gap-[7px] text-[12px] text-[#464c5e]">
                      <input type="checkbox" data-mcp-write-consent={item.slug} checked={item.allow_write} disabled={busySlug === item.slug} onChange={(event) => onToggleWrite(item, event.target.checked)} className="accent-[#2563eb]" />允许写操作（需管理员白名单）
                    </label>
                  )}
                  {item.description && item.description.length > 140 && (
                    <details className="w-full text-[12px] text-[#536077]"><summary className="w-fit cursor-pointer py-[3px] text-[#476a9f] hover:text-[#1d4ed8]">查看完整说明</summary><p className="mt-[6px] whitespace-pre-wrap break-words rounded-[9px] bg-[#f7f9fc] p-[12px] leading-[1.6]">{item.description}</p></details>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}

        {proposals.length > 0 && (
          <section data-mcp-write-proposals className="mt-[24px] rounded-[16px] border border-[#e6eaf1] bg-white p-[20px]">
            <div className="flex flex-wrap items-center justify-between gap-[10px]"><div><h3 className="text-[15px] font-semibold text-[#1d2a40]">写入提案</h3><p className="mt-[4px] text-[12px] text-[#858b9c]">候选产物必须先确认；确认前不会调用外部写工具，也不会修改原件。</p></div><Badge variant="secondary">{proposals.length} 条</Badge></div>
            <div className="mt-[14px] grid gap-[10px]">
              {proposals.map((proposal) => {
                const busy = proposalBusyId === proposal.proposal_id;
                return (
                  <div key={proposal.proposal_id} data-mcp-write-proposal={proposal.proposal_id} className="rounded-[12px] border border-[#e7eaf1] p-[14px]">
                    <div className="flex flex-wrap items-center justify-between gap-[8px]"><div className="break-all text-[13px] font-medium text-[#1d2a40]">{proposal.slug} / {proposal.tool_name}</div><Badge variant="outline">{proposal.status}</Badge></div>
                    <div className="mt-[7px] break-all text-[12px] text-[#858b9c]">产物提示：{proposal.artifact_hint || '未声明'} · 参数摘要：{proposal.arguments_digest.slice(0, 12)}…</div>
                    {proposal.error_code && <div className="mt-[6px] text-[12px] text-[#b5352d]">错误：{proposal.error_code}</div>}
                    <div className="mt-[11px] flex flex-wrap gap-[8px]">
                      {proposal.status === 'proposed' && onConfirmProposal && <Button disabled={busy || !flags.write_enabled} onClick={() => onConfirmProposal(proposal)}>确认生成</Button>}
                      {proposal.status === 'proposed' && onRejectProposal && <Button variant="outline" disabled={busy} onClick={() => onRejectProposal(proposal)}>拒绝</Button>}
                      {proposal.status === 'completed' && proposal.artifact_filename && onDownloadProposal && <Button variant="outline" disabled={busy} onClick={() => onDownloadProposal(proposal)}><Download className="size-[14px]" />下载候选产物</Button>}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}
      </section>

      <Dialog open={uploadOpen} onOpenChange={(open) => { if (!open && !personalBusy) { setUploadOpen(false); setPersonalFile(null); } }}>
        <DialogContent className="flex max-h-[88dvh] w-[min(560px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[18px] p-0 sm:max-w-none">
          <DialogHeader className="shrink-0 border-b border-[#e8ebf2] px-[24px] py-[18px] pr-[56px]">
            <DialogTitle className="text-[16px] font-semibold text-[#18181a]">上传个人 Skill</DialogTitle>
            <p className="text-[12px] leading-[1.6] text-[#758098]">提交后先由管理员审核；启用后可添加到对话，也可以共享给所属部门。</p>
          </DialogHeader>
          <div data-personal-upload className="min-h-0 overflow-y-auto px-[24px] py-[20px]">
            <div className="rounded-[12px] border border-[#e5eaf3] bg-[#f8faff] p-[12px]">
              <div className="text-[11px] font-medium text-[#476a9f]">第一步 · 选择类型</div>
              <div className="mt-[9px] grid gap-[8px] sm:grid-cols-2" role="group" aria-label="Skill 类型">
                <Button variant={personalKind === 'text' ? 'secondary' : 'outline'} className="justify-start" aria-pressed={personalKind === 'text'} disabled={personalBusy} onClick={() => { setPersonalKind('text'); setPersonalFile(null); }}>文本型<span className="ml-auto text-[11px] font-normal opacity-70">Markdown 参考</span></Button>
                <Button variant={personalKind === 'executable' ? 'secondary' : 'outline'} className="justify-start" aria-pressed={personalKind === 'executable'} disabled={personalBusy} onClick={() => { setPersonalKind('executable'); setPersonalFile(null); }}>执行型<span className="ml-auto text-[11px] font-normal opacity-70">独立沙箱</span></Button>
              </div>
            </div>
            {personalKind === 'executable' && (
              <div className="mt-[16px] space-y-[12px]">
                <div className="grid gap-[10px] sm:grid-cols-2">
                  <div><Label htmlFor="personal-skill-name">Skill 名称</Label><Input id="personal-skill-name" className="mt-[6px]" value={personalName} onChange={(event) => setPersonalName(event.target.value)} placeholder="my-calculator" /><p className="mt-[4px] text-[11px] text-[#858b9c]">英文小写、数字及连字符</p></div>
                  <div><Label htmlFor="personal-skill-version">版本</Label><Input id="personal-skill-version" className="mt-[6px]" value={personalVersion} onChange={(event) => setPersonalVersion(event.target.value)} placeholder="1.0.0" /></div>
                </div>
                <details data-personal-schema className="rounded-[10px] border border-[#e8ebf2] px-[12px] py-[10px]">
                  <summary className="cursor-pointer text-[12px] font-medium text-[#596174]">高级：输入参数 JSON Schema</summary>
                  <p className="mt-[9px] text-[11px] leading-[1.6] text-[#73819a]">不填写业务参数时可保留默认对象 Schema；执行时会将参数写入 /work/in/parameters.json。</p>
                  <textarea id="personal-skill-schema" value={personalSchema} onChange={(event) => setPersonalSchema(event.target.value)} rows={6} spellCheck={false} className="mt-[8px] w-full rounded-[8px] border border-[#e0e5ee] bg-white p-[9px] font-mono text-[12px] focus:border-[#6b9ae8] focus:outline-none" />
                </details>
              </div>
            )}
            <div className="mt-[16px] rounded-[12px] border border-dashed border-[#cbd8ed] bg-[#f8faff] p-[16px]">
              <div className="text-[11px] font-medium text-[#476a9f]">第二步 · 上传源包</div>
              <Label htmlFor="personal-skill-zip" className="mt-[9px] block">{personalKind === 'text' ? '文本 Skill ZIP' : '执行型 Skill ZIP'} · 不超过 8 MiB</Label>
              <Input key={personalKind} id="personal-skill-zip" className="mt-[9px] bg-white" type="file" accept=".zip,application/zip"
                onChange={(event) => setPersonalFile(event.target.files?.[0] ?? null)} />
              <p className="mt-[8px] text-[11px] leading-[1.6] text-[#73819a]">{personalKind === 'text' ? '包含 SKILL.md（YAML name / description）和可选 Markdown 参考资料。' : '根目录包含 SKILL.md 与 entrypoint.py；可选 requirements.lock 须固定版本和 wheel SHA-256。'}同名 Skill 不能重复上传。</p>
            </div>
            {personalKind === 'executable' && <p className="mt-[12px] rounded-[9px] bg-[#fff9ed] px-[11px] py-[9px] text-[11px] leading-[1.6] text-[#785e2b]">代码不会在上传时构建或运行；管理员审查源包后，才可交由独立 Runner 构建并继续审核镜像。</p>}
            <div className="sticky bottom-0 z-10 mt-[20px] flex justify-end gap-[8px] border-t border-[#edf0f4] bg-white/95 pt-[14px] backdrop-blur-sm">
              <Button variant="outline" disabled={personalBusy} onClick={() => { setUploadOpen(false); setPersonalFile(null); }}>取消</Button>
              <Button disabled={personalBusy || !personalFile || !onUploadPersonal} onClick={() => {
                if (personalFile && onUploadPersonal) void onUploadPersonal(personalFile, {
                  kind: personalKind, name: personalName.trim(), version: personalVersion.trim(), inputSchema: personalSchema,
                }).then((success) => {
                  if (success) { setUploadOpen(false); setPersonalFile(null); }
                });
              }}>{personalBusy ? '正在上传…' : '提交审核'}</Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function ExtensionsPage({ auth }: Props) {
  const [flags, setFlags] = useState<McpListFlags>(EMPTY_FLAGS);
  const [items, setItems] = useState<McpCatalogItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [proposals, setProposals] = useState<McpWriteProposal[]>([]);
  const [proposalBusyId, setProposalBusyId] = useState<string | null>(null);
  const [skills, setSkills] = useState<ExecutableSkill[]>([]);
  const [personalSkills, setPersonalSkills] = useState<PersonalSkill[]>([]);
  const [personalLoaded, setPersonalLoaded] = useState(false);
  const [personalBusy, setPersonalBusy] = useState(false);
  const [sharingRef, setSharingRef] = useState<string | null>(null);
  const [skillParameters, setSkillParameters] = useState<Record<string, string>>({});
  const [skillBusy, setSkillBusy] = useState<string | null>(null);
  const [skillTask, setSkillTask] = useState<SkillTask | null>(null);
  const [skillArtifacts, setSkillArtifacts] = useState<SkillArtifact[]>([]);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    setSkillsLoaded(false);
    setPersonalLoaded(false);
    api
      .get<McpCatalogResponse>('/api/v1/plugins/mcp')
      .then((payload) => {
        if (cancelled) return;
        setFlags({
          enabled: payload.enabled,
          write_enabled: payload.write_enabled,
          prerequisites_met: payload.prerequisites_met,
          prerequisites_reason: payload.prerequisites_reason,
        });
        setItems(payload.items);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载扩展失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    api
      .get<McpWriteProposalListResponse>('/api/v1/plugins/mcp/write-proposals')
      .then((payload) => {
        if (!cancelled) setProposals(payload.items);
      })
      .catch(() => {
        if (!cancelled) setProposals([]);
      });
    api.get<{ items: ExecutableSkill[] }>('/api/v1/sandbox/skills/available')
      .then((payload) => { if (!cancelled) setSkills(payload.items); })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载 Skill 失败');
      })
      .finally(() => { if (!cancelled) setSkillsLoaded(true); });
    api.get<{ items: PersonalSkill[] }>('/api/v1/sandbox/skills/mine')
      .then((payload) => { if (!cancelled) setPersonalSkills(payload.items); })
      .catch((error) => { if (!cancelled) notify.error(error instanceof Error ? error.message : '加载个人 Skill 失败'); })
      .finally(() => { if (!cancelled) setPersonalLoaded(true); });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  const path = (slug: string) => `/api/v1/plugins/mcp/${encodeURIComponent(slug)}`;

  const refreshSkillTask = useCallback(async (taskId: string) => {
    const response = await api.get<{ task: SkillTask }>(`/api/v1/sandbox/tasks/${encodeURIComponent(taskId)}`);
    setSkillTask(response.task);
    if (response.task.status === 'succeeded') {
      const artifacts = await api.get<{ items: SkillArtifact[] }>(`/api/v1/sandbox/tasks/${encodeURIComponent(taskId)}/artifacts`);
      setSkillArtifacts(artifacts.items);
    }
  }, []);

  useEffect(() => {
    if (!skillTask || ['succeeded', 'failed', 'cancelled', 'rejected', 'expired', 'reconcile_required'].includes(skillTask.status)) return;
    const poll = () => {
      if (!document.hidden) void refreshSkillTask(skillTask.task_id).catch(() => {});
    };
    const timer = window.setInterval(poll, 2500);
    document.addEventListener('visibilitychange', poll);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', poll);
    };
  }, [skillTask?.task_id, skillTask?.status, refreshSkillTask]);

  function run(slug: string, action: () => Promise<unknown>, message: string) {
    setBusySlug(slug);
    action()
      .then(() => {
        notify.success(message);
        load();
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '操作失败'))
      .finally(() => setBusySlug(null));
  }

  function runProposal(proposal: McpWriteProposal, action: () => Promise<unknown>, message: string) {
    setProposalBusyId(proposal.proposal_id);
    action()
      .then(() => {
        notify.success(message);
        void load();
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '操作失败'))
      .finally(() => setProposalBusyId(null));
  }

  return (
    <ExtensionsView
      flags={flags}
      items={items}
      loaded={loaded}
      skillsLoaded={skillsLoaded}
      personalLoaded={personalLoaded}
      busySlug={busySlug}
      userDepartmentId={String(auth.user.department_id ?? '')}
      onRefresh={() => void load()}
      onSubscribe={(item) => run(item.slug, () => api.post(path(item.slug) + '/subscribe', {}), '已订阅')}
      onUnsubscribe={(item) => run(item.slug, () => api.delete(path(item.slug) + '/subscribe'), '已退订')}
      onToggleWrite={(item, allow) =>
        run(
          item.slug,
          () => api.post(path(item.slug) + '/write-consent', { allow_write: allow }),
          allow ? '已允许写操作' : '已撤销写操作同意',
        )
      }
      proposals={proposals}
      proposalBusyId={proposalBusyId}
      onConfirmProposal={(proposal) =>
        runProposal(
          proposal,
          () => api.post(`/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(proposal.proposal_id)}/confirm`),
          '已确认，候选产物将由 worker 生成',
        )
      }
      onRejectProposal={(proposal) =>
        runProposal(
          proposal,
          () => api.post(`/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(proposal.proposal_id)}/reject`),
          '已拒绝写入提案',
        )
      }
      onDownloadProposal={(proposal) =>
        runProposal(
          proposal,
          () => apiDownload.blob(
            `/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(proposal.proposal_id)}/artifact`,
            proposal.artifact_filename || `${proposal.proposal_id}.artifact`,
          ),
          '下载已开始',
        )
      }
      skills={skills}
      personalSkills={personalSkills}
      personalBusy={personalBusy}
      sharingRef={sharingRef}
      onUploadPersonal={async (file, options) => {
        if (file.size > 8 * 1024 * 1024) { notify.error('Skill ZIP 超过 8 MiB'); return false; }
        let inputSchema: Record<string, unknown> = {};
        if (options.kind === 'executable') {
          if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(options.name) || !options.version) {
            notify.error('请输入有效的 Skill 名称与版本'); return false;
          }
          try {
            const parsed: unknown = JSON.parse(options.inputSchema);
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('参数 Schema 必须是 JSON 对象');
            inputSchema = parsed as Record<string, unknown>;
          } catch (error) { notify.error(error instanceof Error ? error.message : '参数 Schema 不是有效 JSON'); return false; }
        }
        setPersonalBusy(true);
        try {
          const encoded = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result ?? '').split(',')[1] ?? '');
            reader.onerror = () => reject(new Error('无法读取 Skill ZIP'));
            reader.readAsDataURL(file);
          });
          await api.post(options.kind === 'text' ? '/api/v1/sandbox/skills/personal/text-packages' : '/api/v1/sandbox/skills/personal/packages',
            options.kind === 'text' ? { package_base64: encoded } : {
              package_base64: encoded, skill_name: options.name, version: options.version, input_schema: inputSchema,
            });
          notify.success('个人 Skill 已提交，等待管理员审核');
          load();
          return true;
        } catch (error) {
          notify.error(error instanceof Error ? error.message : '上传失败');
          return false;
        } finally { setPersonalBusy(false); }
      }}
      onSharePersonal={(skill, share) => {
        setSharingRef(skill.skill_ref);
        void api.post(`/api/v1/sandbox/skills/personal/${encodeURIComponent(skill.skill_ref)}/share`, {
          share_to_department: share,
        }).then(() => { notify.success(share ? '已共享给部门，成员可自行添加' : '已停止共享'); load(); })
          .catch((error) => notify.error(error instanceof Error ? error.message : '共享设置失败'))
          .finally(() => setSharingRef(null));
      }}
      skillParameters={skillParameters}
      skillBusy={skillBusy}
      skillTask={skillTask}
      skillArtifacts={skillArtifacts}
      onSkillParametersChange={(ref, value) => setSkillParameters((previous) => ({ ...previous, [ref]: value }))}
      onToggleSkill={(skill) => {
        setSkillBusy(skill.skill_ref);
        const path = `/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/subscribe`;
        const action = skill.subscribed ? api.delete(path) : api.post(path, {});
        void action.then(() => { notify.success(skill.subscribed ? '已移除 Skill' : '已添加 Skill'); void load(); })
          .catch((error) => notify.error(error instanceof Error ? error.message : 'Skill 操作失败'))
          .finally(() => setSkillBusy(null));
      }}
      onRunSkill={(skill) => {
        let parameters: Record<string, unknown>;
        try {
          const parsed: unknown = JSON.parse(skillParameters[skill.skill_ref] ?? '{}');
          if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('参数必须是 JSON 对象');
          parameters = parsed as Record<string, unknown>;
        } catch (error) {
          notify.error(error instanceof Error ? error.message : '参数不是有效 JSON');
          return;
        }
        setSkillBusy(skill.skill_ref);
        api.post<{ task: SkillTask }>(`/api/v1/sandbox/skills/${encodeURIComponent(skill.skill_ref)}/run`, {
          idempotency_key: globalThis.crypto?.randomUUID?.() ?? `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`,
          parameters,
        })
          .then((response) => {
            setSkillTask(response.task);
            setSkillArtifacts([]);
            notify.success('任务已提交到沙箱');
          })
          .catch((error) => notify.error(error instanceof Error ? error.message : 'Skill 任务提交失败'))
          .finally(() => setSkillBusy(null));
      }}
      onRefreshSkillTask={() => {
        if (skillTask) void refreshSkillTask(skillTask.task_id).catch((error) => notify.error(error instanceof Error ? error.message : '状态读取失败'));
      }}
      onDownloadSkillArtifact={(artifact) => {
        void apiDownload.blob(`/api/v1/sandbox/artifacts/${encodeURIComponent(artifact.artifact_id)}`, artifact.filename)
          .catch((error) => notify.error(error instanceof Error ? error.message : '下载失败'));
      }}
    />
  );
}

import { useCallback, useEffect, useState } from 'react';

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
import { notify } from '@/components/ui/app-toast';

type Props = { auth: AuthSession; onLogout: () => void };

type ExecutableSkill = {
  skill_ref: string;
  version: string;
  bundle_ref: string;
  input_schema: Record<string, unknown>;
  subscribed?: boolean;
  description?: string;
};
type SkillTask = { task_id: string; status: string; error_code?: string };
type SkillArtifact = { artifact_id: string; filename: string; state: string };

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
  busySlug: string | null;
  userName: string;
  onLogout: () => void;
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

/** Pure view: employee catalog with subscription and write-consent controls. */
export function ExtensionsView({
  flags,
  items,
  loaded,
  busySlug,
  userName,
  onLogout,
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
  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="扩展"
        description="管理员登记的外部服务；订阅后才能在对话中使用。"
        userName={userName}
        onLogout={onLogout}
      />

      <div
        data-mcp-egress-notice
        className="mt-[20px] rounded-[14px] border border-[#f0d8a8] bg-[#fffaf0] p-[16px] text-[13px] leading-[1.7] text-[#7a5a12]"
      >
        <div className="font-semibold">数据外发说明</div>
        <div className="mt-[4px]">
          调用外部服务时，你发送的查询内容会离开本系统并交给管理员登记的外部服务提供方；
          外部返回内容属于不可信数据，不会进入知识库证据链，也不会获得引用编号。
        </div>
      </div>

      {!flags.enabled && (
        <div data-mcp-disabled className="mt-[16px] text-[13px] text-[#858b9c]">
          扩展能力未启用（MCP_ENABLED=false），当前没有可订阅的服务。
        </div>
      )}
      {flags.enabled && !flags.prerequisites_met && (
        <div data-mcp-prerequisite className="mt-[16px] text-[13px] text-[#d20b0b]">
          扩展已开启但前置条件未满足：{flags.prerequisites_reason || '未知原因'}
        </div>
      )}

      <div className="mt-[16px] flex items-center gap-[10px]">
        <Button variant="outline" onClick={onRefresh} disabled={!loaded}>
          刷新
        </Button>
        <span className="text-[13px] text-[#858b9c]">
          共 {items.length} 个服务，已订阅 {items.filter((item) => item.subscribed).length} 个
        </span>
      </div>

      {!loaded ? (
        <div className="mt-[16px] grid gap-[12px] md:grid-cols-2">
          <Skeleton className="h-[140px] rounded-[16px]" />
          <Skeleton className="h-[140px] rounded-[16px]" />
        </div>
      ) : items.length === 0 ? (
        <div
          data-mcp-empty
          className="mt-[16px] rounded-[16px] bg-white p-[24px] text-[13px] text-[#858b9c] shadow-[0_2px_10px_rgba(0,0,0,0.05)]"
        >
          暂无可订阅的扩展服务。管理员登记并启用服务后会出现在这里。
        </div>
      ) : (
        <div className="mt-[16px] grid gap-[12px] md:grid-cols-2">
          {items.map((item) => (
            <div
              key={item.slug}
              data-mcp-catalog-item={item.slug}
              className="flex flex-col rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]"
            >
              <div className="flex items-start justify-between gap-[12px]">
                <div className="min-w-0">
                  <div className="truncate text-[15px] font-semibold text-[#18181a]">
                    {item.icon ? `${item.icon} ` : ''}
                    {item.name}
                  </div>
                  <div className="mt-[2px] truncate text-[12px] text-[#858b9c]">{item.slug}</div>
                </div>
                {item.subscribed && <Badge data-mcp-subscribed={item.slug}>已订阅</Badge>}
              </div>

              {item.description && (
                <p className="mt-[10px] text-[13px] leading-[1.6] text-[#464c5e]">
                  {item.description}
                </p>
              )}

              <div className="mt-[10px] text-[12px] text-[#858b9c]">
                工具 {item.tool_count} 个 · 只读 {item.read_only_tool_count} 个
              </div>
              {item.tags.length > 0 && (
                <div className="mt-[8px] flex flex-wrap gap-[6px]">
                  {item.tags.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-full bg-[#f2f3f7] px-[8px] py-[2px] text-[11px] text-[#464c5e]"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              )}

              <div className="mt-[14px] flex flex-wrap items-center gap-[10px]">
                {item.subscribed ? (
                  <Button
                    variant="outline"
                    disabled={busySlug === item.slug}
                    onClick={() => onUnsubscribe(item)}
                  >
                    退订
                  </Button>
                ) : (
                  <Button
                    disabled={busySlug === item.slug || !flags.enabled}
                    onClick={() => onSubscribe(item)}
                  >
                    订阅
                  </Button>
                )}
                {item.subscribed && item.writable && !flags.write_enabled && (
                  <span className="text-[12px] text-[#858b9c]">
                    该服务包含写工具，写通道当前未启用。
                  </span>
                )}
                {item.subscribed && item.writable && flags.write_enabled && (
                  <label className="flex cursor-pointer items-center gap-[6px] text-[13px] text-[#464c5e]">
                    <input
                      type="checkbox"
                      data-mcp-write-consent={item.slug}
                      checked={item.allow_write}
                      onChange={(event) => onToggleWrite(item, event.target.checked)}
                    />
                    允许写操作（需管理员白名单）
                  </label>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <section
        data-mcp-write-proposals
        className="mt-[20px] rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]"
      >
        <div className="flex items-center justify-between gap-[12px]">
          <div>
            <h2 className="text-[15px] font-semibold text-[#18181a]">写入提案</h2>
            <p className="mt-[4px] text-[12px] text-[#858b9c]">
              候选产物必须先确认；确认前不会调用外部写工具，也不会修改原件。
            </p>
          </div>
          <Badge>{proposals.length} 条</Badge>
        </div>
        {proposals.length === 0 ? (
          <div className="mt-[12px] text-[13px] text-[#858b9c]">暂无待处理或已完成的写入提案。</div>
        ) : (
          <div className="mt-[12px] grid gap-[10px]">
            {proposals.map((proposal) => {
              const busy = proposalBusyId === proposal.proposal_id;
              return (
                <div
                  key={proposal.proposal_id}
                  data-mcp-write-proposal={proposal.proposal_id}
                  className="rounded-[12px] border border-[#eceef3] p-[12px]"
                >
                  <div className="flex flex-wrap items-center justify-between gap-[8px]">
                    <div className="text-[13px] font-medium text-[#18181a]">
                      {proposal.slug} / {proposal.tool_name}
                    </div>
                    <Badge>{proposal.status}</Badge>
                  </div>
                  <div className="mt-[6px] text-[12px] text-[#858b9c]">
                    产物提示：{proposal.artifact_hint || '未声明'} · 参数摘要：{proposal.arguments_digest.slice(0, 12)}…
                  </div>
                  {proposal.error_code && (
                    <div className="mt-[6px] text-[12px] text-[#d20b0b]">错误：{proposal.error_code}</div>
                  )}
                  <div className="mt-[10px] flex flex-wrap gap-[8px]">
                    {proposal.status === 'proposed' && onConfirmProposal && (
                      <Button disabled={busy || !flags.write_enabled} onClick={() => onConfirmProposal(proposal)}>
                        确认生成
                      </Button>
                    )}
                    {proposal.status === 'proposed' && onRejectProposal && (
                      <Button variant="outline" disabled={busy} onClick={() => onRejectProposal(proposal)}>
                        拒绝
                      </Button>
                    )}
                    {proposal.status === 'completed' && proposal.artifact_filename && onDownloadProposal && (
                      <Button variant="outline" disabled={busy} onClick={() => onDownloadProposal(proposal)}>
                        下载候选产物
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section data-sandbox-skills className="mt-[20px] rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]">
        <h2 className="text-[15px] font-semibold text-[#18181a]">已审核的执行 Skill</h2>
        <p className="mt-[4px] text-[12px] text-[#858b9c]">
          添加审核通过的 Skill 后，可在临时沙箱中运行其构建镜像。
        </p>
        {skills.length === 0 ? (
          <p className="mt-[12px] text-[13px] text-[#858b9c]">暂无可用的执行 Skill。</p>
        ) : (
          <div className="mt-[12px] grid gap-[12px]">
            {skills.map((skill) => (
              <div key={skill.skill_ref} className="rounded-[12px] border border-[#eceef3] p-[12px]">
                <div className="text-[13px] font-medium">{skill.skill_ref} · {skill.version}</div>
                {skill.description && <p className="mt-[6px] whitespace-pre-wrap break-words text-[12px] text-[#464c5e]">{skill.description}</p>}
                <div className="mt-[3px] text-[12px] text-[#858b9c]">固定任务：{skill.bundle_ref}</div>
                <Button variant="outline" className="mt-[8px]" onClick={() => onToggleSkill?.(skill)}>
                  {skill.subscribed ? '移除 Skill' : '添加 Skill'}
                </Button>
                {skill.subscribed && <Badge className="ml-[8px]">已添加</Badge>}
                <details className="mt-[8px] text-[12px] text-[#464c5e]">
                  <summary>查看参数格式</summary>
                  <pre className="mt-[6px] max-h-[180px] overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(skill.input_schema, null, 2)}</pre>
                </details>
                <label className="mt-[8px] block text-[12px] text-[#464c5e]">
                  JSON 参数
                  <textarea
                    data-skill-parameters={skill.skill_ref}
                    className="mt-[4px] block min-h-[64px] w-full rounded-[8px] border border-[#eceef3] p-[8px] font-mono"
                    value={skillParameters[skill.skill_ref] ?? '{}'}
                    onChange={(event) => onSkillParametersChange?.(skill.skill_ref, event.target.value)}
                  />
                </label>
                <Button className="mt-[8px]" disabled={skillBusy !== null || !skill.subscribed || !onRunSkill} onClick={() => onRunSkill?.(skill)}>
                  在沙箱中运行
                </Button>
              </div>
            ))}
          </div>
        )}
        {skillTask && (
          <div className="mt-[12px] rounded-[12px] border border-[#eceef3] p-[12px] text-[12px]">
            <div>任务 {skillTask.task_id} · {skillTask.status}{skillTask.error_code ? ` · ${skillTask.error_code}` : ''}</div>
            <Button variant="outline" className="mt-[8px]" onClick={onRefreshSkillTask}>刷新状态</Button>
            {skillArtifacts.filter((item) => item.state === 'committed').map((item) => (
              <Button key={item.artifact_id} variant="outline" className="ml-[8px] mt-[8px]" onClick={() => onDownloadSkillArtifact?.(item)}>
                下载 {item.filename}
              </Button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export default function ExtensionsPage({ auth, onLogout }: Props) {
  const [flags, setFlags] = useState<McpListFlags>(EMPTY_FLAGS);
  const [items, setItems] = useState<McpCatalogItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [proposals, setProposals] = useState<McpWriteProposal[]>([]);
  const [proposalBusyId, setProposalBusyId] = useState<string | null>(null);
  const [skills, setSkills] = useState<ExecutableSkill[]>([]);
  const [skillParameters, setSkillParameters] = useState<Record<string, string>>({});
  const [skillBusy, setSkillBusy] = useState<string | null>(null);
  const [skillTask, setSkillTask] = useState<SkillTask | null>(null);
  const [skillArtifacts, setSkillArtifacts] = useState<SkillArtifact[]>([]);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
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
      .catch(() => { if (!cancelled) setSkills([]); });
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
    const timer = window.setInterval(() => {
      void refreshSkillTask(skillTask.task_id).catch(() => {});
    }, 2500);
    return () => window.clearInterval(timer);
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
      busySlug={busySlug}
      userName={auth.user.username}
      onLogout={onLogout}
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

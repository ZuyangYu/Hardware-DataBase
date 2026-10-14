import { useCallback, useEffect, useState, type ReactNode } from 'react';

import { api } from '../../api/client';
import type {
  DepartmentView,
  McpAdminListResponse,
  McpCreatePayload,
  McpListFlags,
  McpServerResponse,
  McpServerView,
  McpTestResponse,
  McpToolView,
  McpToolsResponse,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { AdminPageFrame } from '@/components/admin/AdminPageShell';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import SkillsPanel from './SkillsPanel';

type Props = { auth: AuthSession };
type ExtensionSection = 'mcp' | 'skills';

type AdminExtensionsViewProps = {
  activeSection: ExtensionSection;
  onSectionChange: (section: ExtensionSection) => void;
  mcpContent: ReactNode;
  skillContent: ReactNode;
};

/** One admin page with two peer management sections, only one visible at a time. */
export function AdminExtensionsView({ activeSection, onSectionChange,
  mcpContent, skillContent }: AdminExtensionsViewProps) {
  return (
    <AdminPageFrame>
      <AppHeader
        title="平台扩展"
        description="登记、审核和配置远程 MCP 服务与可执行 Skill；员工按授权选择添加。"
      />
        <nav aria-label="平台扩展管理" className="mt-[20px] flex gap-[20px] border-b border-[#eceef3]">
          {([
            { id: 'mcp', label: 'MCP 登记服务' },
            { id: 'skills', label: 'Skill 上传与审核' },
          ] as const).map(({ id, label }) => (
            <button
              key={id}
              id={`admin-extensions-${id}-tab`}
              type="button"
              aria-controls={`admin-extensions-${id}-panel`}
              aria-pressed={activeSection === id}
              onClick={() => onSectionChange(id)}
              className={`border-b-2 px-[4px] py-[10px] text-[13px] font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#1d4ed8] ${
                activeSection === id
                  ? 'border-[#1d4ed8] text-[#1d4ed8]'
                  : 'border-transparent text-[#858b9c] hover:text-[#464c5e]'
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
      {activeSection === 'mcp' ? (
        <div id="admin-extensions-mcp-panel" role="region" aria-labelledby="admin-extensions-mcp-tab" className="mt-[20px]">
          {mcpContent}
        </div>
      ) : (
        <div id="admin-extensions-skills-panel" role="region" aria-labelledby="admin-extensions-skills-tab" className="mt-[20px]">
          {skillContent}
        </div>
      )}
    </AdminPageFrame>
  );
}

const EMPTY_FLAGS: McpListFlags = {
  enabled: false,
  write_enabled: false,
  prerequisites_met: false,
  prerequisites_reason: '',
};

const SCOPE_LABELS: Record<string, string> = {
  none: '不可见',
  departments: '指定部门',
  all: '全部员工',
};

const BLOCKED_LABELS: Record<string, string> = {
  not_read_only: '非只读，未加入写白名单',
  disabled: '已停用',
  write_disabled: '写通道未启用',
};

export type PluginsViewProps = {
  flags: McpListFlags;
  servers: McpServerView[];
  loaded: boolean;
  busySlug: string | null;
  toolsFor: string | null;
  tools: McpToolView[];
  toolsLoading: boolean;
  onRefresh: () => void;
  onCreate: () => void;
  onEdit: (server: McpServerView) => void;
  onDelete: (server: McpServerView) => void;
  onTest: (server: McpServerView) => void;
  onToggleEnabled: (server: McpServerView) => void;
  onOpenTools: (server: McpServerView) => void;
  onCloseTools: () => void;
  onRefreshTools: (server: McpServerView) => void;
  onToggleTool: (server: McpServerView, tool: McpToolView) => void;
  onToggleWriteTool: (server: McpServerView, tool: McpToolView) => void;
};

/** Pure admin view: server registry, connection state and tool governance. */
export function PluginsView({
  flags,
  servers,
  loaded,
  busySlug,
  toolsFor,
  tools,
  toolsLoading,
  onRefresh,
  onCreate,
  onEdit,
  onDelete,
  onTest,
  onToggleEnabled,
  onOpenTools,
  onCloseTools,
  onRefreshTools,
  onToggleTool,
  onToggleWriteTool,
}: PluginsViewProps) {
  const columns: DataTableColumn<McpServerView>[] = [
    {
      key: 'name',
      title: '服务',
      render: (server) => (
        <div className="min-w-0">
          <div className="truncate text-[13px] font-medium text-[#18181a]">{server.name}</div>
          <div className="truncate text-[12px] text-[#858b9c]">{server.slug}</div>
        </div>
      ),
    },
    {
      key: 'connection',
      title: '连接',
      render: (server) => (
        <div className="min-w-0">
          <div className="text-[12px] text-[#464c5e]">{server.transport}</div>
          <div className="max-w-[260px] truncate text-[12px] text-[#858b9c]" title={server.url}>
            {server.url}
          </div>
          <div className="text-[12px] text-[#858b9c]">
            {server.has_credentials ? '已配置凭据' : '无凭据'}
            {server.credentials_error ? ` · ${server.credentials_error}` : ''}
          </div>
        </div>
      ),
    },
    {
      key: 'scope',
      title: '部门范围',
      render: (server) => (
        <div className="text-[12px] text-[#464c5e]">
          {SCOPE_LABELS[server.department_scope] ?? server.department_scope}
          {server.department_scope === 'departments' && server.department_ids.length > 0 && (
            <div className="text-[12px] text-[#858b9c]">部门 {server.department_ids.join(', ')}</div>
          )}
        </div>
      ),
    },
    {
      key: 'status',
      title: '状态',
      render: (server) => (
        <div className="flex flex-col gap-[4px]">
          <Badge variant={server.enabled ? 'default' : 'secondary'}>
            {server.enabled ? '启用中' : '已停用'}
          </Badge>
          <span className="text-[12px] text-[#858b9c]">
            {server.test_passed_for_current_config ? '连接测试通过' : '未通过当前配置测试'}
          </span>
          {server.last_test_error && (
            <span className="max-w-[220px] truncate text-[12px] text-[#d20b0b]" title={server.last_test_error}>
              {server.last_test_error}
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'tools',
      title: '工具治理',
      render: (server) => (
        <div className="text-[12px] text-[#464c5e]">
          停用 {server.disabled_tools.length} 个
          <div className="text-[12px] text-[#858b9c]">
            写白名单 {server.write_tools.length} 个
          </div>
        </div>
      ),
    },
    {
      key: 'actions',
      title: '操作',
      render: (server) => (
        <div className="flex flex-wrap gap-[6px]">
          <Button
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_CLASS}
            disabled={busySlug === server.slug}
            onClick={() => onTest(server)}
          >
            测试连接
          </Button>
          <Button
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_CLASS}
            disabled={busySlug === server.slug}
            onClick={() => onToggleEnabled(server)}
          >
            {server.enabled ? '停用' : '启用'}
          </Button>
          <Button
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_CLASS}
            onClick={() => onOpenTools(server)}
          >
            工具
          </Button>
          <Button
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_CLASS}
            onClick={() => onEdit(server)}
          >
            编辑
          </Button>
          <Button
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_CLASS}
            disabled={busySlug === server.slug}
            onClick={() => onDelete(server)}
          >
            删除
          </Button>
        </div>
      ),
    },
  ];

  const active = servers.find((server) => server.slug === toolsFor) ?? null;

  return (
    <div>
      <h2 className="text-[16px] font-semibold text-[#18181a]">MCP 登记服务</h2>
      <p className="mt-[6px] text-[12px] text-[#858b9c]">
        登记远程 MCP 服务；默认停用，测试连接通过后方可启用。外部结果不进入知识库证据链。
      </p>

      {!flags.enabled && (
        <div data-mcp-switch-off className="mt-[20px] text-[13px] text-[#7a5a12]">
          MCP 总开关未启用（MCP_ENABLED=false）：仍可登记、编辑、删除服务；测试连接与工具刷新
          不会发起外部连接，也不能启用服务。启用前请在「系统配置 → 外部 MCP 扩展」把 MCP_ENABLED
          设为 true（需同时开启统一工具路径与资源 governor），或修改 .env 后重启/热加载。
        </div>
      )}
      {flags.enabled && !flags.prerequisites_met && (
        <div data-mcp-prerequisite className="mt-[20px] text-[13px] text-[#d20b0b]">
          前置条件未满足：{flags.prerequisites_reason || '未知原因'}
        </div>
      )}

      <div className="mt-[16px] flex items-center gap-[10px]">
        <Button data-mcp-create onClick={onCreate}>
          登记服务
        </Button>
        <Button variant="outline" onClick={onRefresh}>
          刷新
        </Button>
        <span className="text-[13px] text-[#858b9c]">
          共 {servers.length} 个服务，启用 {servers.filter((server) => server.enabled).length} 个
          {flags.write_enabled ? ' · 写通道已开启' : ' · 写通道未开启'}
        </span>
      </div>

      <div className="mt-[16px]">
        <DataTable
          columns={columns}
          data={servers}
          rowKey={(server) => server.slug}
          loading={!loaded}
          emptyText="暂无登记的服务"
          aria-label="MCP 服务列表"
        />
      </div>

      {active && (
        <div
          data-mcp-tools-panel={active.slug}
          className="mt-[16px] rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]"
        >
          <div className="flex items-center justify-between">
            <div className="text-[14px] font-semibold text-[#18181a]">
              {active.name} · 工具治理
            </div>
            <div className="flex gap-[8px]">
              <Button
                variant="outline"
                disabled={busySlug === active.slug || toolsLoading}
                onClick={() => onRefreshTools(active)}
              >
                刷新工具
              </Button>
              <Button variant="outline" onClick={onCloseTools}>
                收起
              </Button>
            </div>
          </div>
          <div className="mt-[6px] text-[12px] text-[#858b9c]">
            只有声明为只读的工具会进入对话；写工具需要先加入写白名单，且写通道开启、员工同意后才可执行（二期）。
          </div>
          {toolsLoading ? (
            <div className="mt-[12px] text-[13px] text-[#858b9c]">工具加载中…</div>
          ) : tools.length === 0 ? (
            <div className="mt-[12px] text-[13px] text-[#858b9c]">未发现工具</div>
          ) : (
            <div className="mt-[12px] flex flex-col gap-[8px]">
              {tools.map((tool) => (
                <div
                  key={tool.name}
                  data-mcp-tool={tool.name}
                  className="flex flex-wrap items-center justify-between gap-[10px] rounded-[12px] bg-[#f7f8fa] p-[12px]"
                >
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium text-[#18181a]">
                      {tool.name}
                      <span className="ml-[8px] text-[12px] text-[#858b9c]">
                        {tool.read_only ? '只读' : '写工具'}
                      </span>
                    </div>
                    <div className="max-w-[520px] truncate text-[12px] text-[#858b9c]" title={tool.description}>
                      {tool.description}
                    </div>
                    {tool.blocked_reason && (
                      <div className="text-[12px] text-[#7a5a12]">
                        {BLOCKED_LABELS[tool.blocked_reason] ?? tool.blocked_reason}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-[10px]">
                    {!tool.read_only && (
                      <label className="flex cursor-pointer items-center gap-[6px] text-[12px] text-[#464c5e]">
                        <input
                          type="checkbox"
                          data-mcp-write-tool={tool.name}
                          checked={tool.write_allowed}
                          onChange={() => onToggleWriteTool(active, tool)}
                        />
                        写白名单
                      </label>
                    )}
                    <Button
                      variant="outline"
                      className={OUTLINE_ACTION_BUTTON_CLASS}
                      disabled={busySlug === active.slug}
                      onClick={() => onToggleTool(active, tool)}
                    >
                      {tool.disabled ? '启用工具' : '停用工具'}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export type ServerFormFieldsProps = {
  initial: McpServerView | null;
  departments: DepartmentView[];
  submitting: boolean;
  onSubmit: (payload: McpCreatePayload & { clear_headers?: boolean }) => void;
  onCancel: () => void;
};

function parseHeaders(text: string): Record<string, string> | null {
  const headers: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const index = line.indexOf(':');
    if (index <= 0) continue;
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    if (key && value) headers[key] = value;
  }
  return Object.keys(headers).length > 0 ? headers : null;
}

function slugFromName(value: string): string {
  const ascii = value
    .normalize('NFKD')
    .replace(/[^\x00-\x7F]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^[^a-z0-9]+|[-_]+$/g, '')
    .slice(0, 50);
  if (ascii.length >= 2) return ascii;
  const codePoints = Array.from(value.trim())
    .map((character) => character.codePointAt(0)?.toString(16) ?? '')
    .filter(Boolean)
    .join('-')
    .slice(0, 44)
    .replace(/[-_]+$/g, '');
  return `mcp-${codePoints || 'service'}`.slice(0, 50);
}

/** Pure create/edit form fields; the credential is never echoed back. */
export function ServerFormFields({
  initial,
  departments,
  submitting,
  onSubmit,
  onCancel,
}: ServerFormFieldsProps) {
  const editing = initial != null;
  const [slug, setSlug] = useState(initial?.slug ?? '');
  const [slugEdited, setSlugEdited] = useState(initial != null);
  const [name, setName] = useState(initial?.name ?? '');
  const [transport, setTransport] = useState(initial?.transport ?? 'streamable_http');
  const [url, setUrl] = useState(initial?.url ?? '');
  const [description, setDescription] = useState(initial?.description ?? '');
  const [headersText, setHeadersText] = useState('');
  const [clearHeaders, setClearHeaders] = useState(false);
  const [timeoutS, setTimeoutS] = useState(initial?.timeout_s ? String(initial.timeout_s) : '');
  const [scope, setScope] = useState(initial?.department_scope ?? 'none');
  const [departmentIds, setDepartmentIds] = useState<number[]>(initial?.department_ids ?? []);
  const [tagsText, setTagsText] = useState((initial?.tags ?? []).join(', '));
  const [icon, setIcon] = useState(initial?.icon ?? '');

  function toggleDepartment(id: number) {
    setDepartmentIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );
  }

  function submit() {
    const headers = parseHeaders(headersText);
    const payload: McpCreatePayload & { clear_headers?: boolean } = {
      slug: slug.trim(),
      name: name.trim(),
      transport,
      url: url.trim(),
      description: description.trim(),
      timeout_s: timeoutS.trim() ? Number(timeoutS) : null,
      department_scope: scope,
      department_ids: scope === 'departments' ? departmentIds : [],
      tags: tagsText
        .split(',')
        .map((tag) => tag.trim())
        .filter(Boolean),
      icon: icon.trim(),
    };
    if (headers) payload.headers = headers;
    if (editing && clearHeaders) payload.clear_headers = true;
    onSubmit(payload);
  }

  return (
    <div className="flex min-h-0 flex-col">
      <div data-mcp-form-scroll className="max-h-[calc(88dvh-164px)] space-y-[16px] overflow-y-auto px-[24px] py-[20px]">
        <section className="rounded-[14px] border border-[#e8ebf2] bg-white p-[16px]">
          <div className="mb-[12px]">
            <h3 className="text-[13px] font-semibold text-[#252b37]">基本信息</h3>
            <p className="mt-[3px] text-[11px] text-[#858b9c]">名称用于在服务目录中展示；标识会根据名称自动生成。</p>
          </div>
          <Label htmlFor="mcp-name">服务名称</Label>
          <Input
            id="mcp-name"
            className="mt-[6px]"
            value={name}
            onChange={(event) => {
              const nextName = event.target.value;
              setName(nextName);
              if (!slugEdited) setSlug(slugFromName(nextName));
            }}
            placeholder="例如：KiCad 原理图工具"
          />
          <p className="mt-[6px] text-[11px] text-[#858b9c]">
            服务标识：<span className="font-mono text-[#596174]">{slug || '填写名称后自动生成'}</span>
          </p>
        </section>

        <section className="rounded-[14px] border border-[#e8ebf2] bg-white p-[16px]">
          <div className="mb-[12px]">
            <h3 className="text-[13px] font-semibold text-[#252b37]">连接信息</h3>
            <p className="mt-[3px] text-[11px] text-[#858b9c]">凭据加密保存，不会在编辑时回显。</p>
          </div>
          <div className="space-y-[12px]">
            <div>
              <Label htmlFor="mcp-url">服务地址</Label>
              <Input
                id="mcp-url"
                className="mt-[6px]"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://mcp.example.com/mcp"
              />
            </div>
            <div>
              <Label htmlFor="mcp-headers">访问凭据</Label>
              <Textarea
                id="mcp-headers"
                className="mt-[6px] font-mono text-[12px]"
                value={headersText}
                onChange={(event) => setHeadersText(event.target.value)}
                rows={2}
                autoComplete="off"
                spellCheck={false}
                placeholder={editing && initial?.has_credentials ? '已保存凭据，留空即保留' : 'Authorization: Bearer …'}
              />
              <p className="mt-[5px] text-[11px] text-[#858b9c]">
                每行一个请求头，格式为 Key: Value{editing ? '；编辑时留空会保留已保存凭据' : ''}。
              </p>
              {editing && (
                <label className="mt-[9px] flex cursor-pointer items-center gap-[7px] text-[12px] text-[#464c5e]">
                  <input
                    type="checkbox"
                    data-mcp-clear-headers
                    checked={clearHeaders}
                    onChange={(event) => setClearHeaders(event.target.checked)}
                  />
                  清除已保存凭据
                </label>
              )}
            </div>
          </div>
        </section>

        <details className="rounded-[14px] border border-[#e8ebf2] bg-[#fbfcfe] p-[16px]" data-mcp-advanced>
          <summary className="cursor-pointer list-none text-[13px] font-semibold text-[#3f4a5d] marker:hidden">
            <span className="flex flex-col items-start gap-[3px] sm:flex-row sm:items-center sm:justify-between sm:gap-[12px]">
              <span className="min-w-0">授权与高级设置</span>
              <span className="min-w-0 text-[11px] font-normal text-[#858b9c]">说明、传输协议、超时、部门范围与标签</span>
            </span>
          </summary>
          <div className="mt-[16px] space-y-[16px]">
            <section className="rounded-[12px] border border-[#e8ebf2] bg-white p-[14px]">
              <h4 className="mb-[12px] text-[12px] font-semibold text-[#596174]">连接选项</h4>
              {!editing && (
                <div className="mb-[12px]">
                  <Label htmlFor="mcp-slug">服务标识（可修改）</Label>
                  <Input
                    id="mcp-slug"
                    className="mt-[6px] font-mono"
                    value={slug}
                    onChange={(event) => {
                      setSlugEdited(true);
                      setSlug(event.target.value);
                    }}
                    placeholder="kicad-local"
                  />
                  <p className="mt-[5px] text-[11px] text-[#858b9c]">只能用小写字母、数字、连字符和下划线；保存后不可修改。</p>
                </div>
              )}
              {editing && (
                <div className="mb-[12px]">
                  <Label htmlFor="mcp-slug">服务标识</Label>
                  <Input id="mcp-slug" className="mt-[6px] font-mono" value={slug} disabled />
                </div>
              )}
              <div className="grid gap-[12px] sm:grid-cols-2">
                <div>
                  <Label htmlFor="mcp-transport">传输协议</Label>
                  <select
                    id="mcp-transport"
                    className="mt-[6px] h-[36px] w-full rounded-[10px] border border-[#e1e5ee] bg-white px-[10px] text-[13px]"
                    value={transport}
                    onChange={(event) => setTransport(event.target.value)}
                  >
                    <option value="streamable_http">Streamable HTTP（推荐）</option>
                    {editing && initial?.transport === 'sse' && (
                      <option value="sse">SSE（旧服务兼容，已弃用）</option>
                    )}
                  </select>
                </div>
                <div>
                  <Label htmlFor="mcp-timeout">连接超时（秒）</Label>
                  <Input
                    id="mcp-timeout"
                    className="mt-[6px]"
                    type="number"
                    min={1}
                    max={300}
                    value={timeoutS}
                    onChange={(event) => setTimeoutS(event.target.value)}
                    placeholder="15"
                  />
                </div>
              </div>
              <div className="mt-[12px]">
                <Label htmlFor="mcp-description">服务说明（可选）</Label>
                <Textarea
                  id="mcp-description"
                  className="mt-[6px]"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  rows={2}
                  placeholder="描述服务用途和适用场景"
                />
              </div>
            </section>

            <section className="rounded-[12px] border border-[#e8ebf2] bg-white p-[14px]">
              <h4 className="mb-[12px] text-[12px] font-semibold text-[#596174]">可见范围</h4>
              <div className="grid gap-[12px] sm:grid-cols-2">
                <div>
                  <Label htmlFor="mcp-scope">员工可见范围</Label>
                  <select
                    id="mcp-scope"
                    className="mt-[6px] h-[36px] w-full rounded-[10px] border border-[#e1e5ee] bg-white px-[10px] text-[13px]"
                    value={scope}
                    onChange={(event) => setScope(event.target.value)}
                  >
                    <option value="none">暂不对员工开放</option>
                    <option value="departments">指定部门</option>
                    <option value="all">全部员工</option>
                  </select>
                  <p className="mt-[5px] text-[11px] text-[#858b9c]">服务启用后，符合范围的员工仍需自行订阅。</p>
                  {scope === 'departments' && (
                    <div className="mt-[10px] max-h-[150px] overflow-y-auto rounded-[9px] border border-[#edf0f4] p-[9px]" data-mcp-department-picker>
                      {departments.length === 0 ? (
                        <span className="text-[12px] text-[#858b9c]">暂无部门</span>
                      ) : (
                        <div className="flex flex-col gap-[8px]">
                          {departments.map((department) => (
                            <label
                              key={department.id}
                              className="flex cursor-pointer items-center gap-[7px] text-[12px] text-[#464c5e]"
                            >
                              <input
                                type="checkbox"
                                data-mcp-department={department.id}
                                checked={departmentIds.includes(department.id)}
                                onChange={() => toggleDepartment(department.id)}
                              />
                              {department.name}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
                <div className="space-y-[12px]">
                  <div>
                    <Label htmlFor="mcp-tags">目录标签（可选）</Label>
                    <Input
                      id="mcp-tags"
                      className="mt-[6px]"
                      value={tagsText}
                      onChange={(event) => setTagsText(event.target.value)}
                      placeholder="设计, EDA"
                    />
                  </div>
                  <div>
                    <Label htmlFor="mcp-icon">目录图标（可选）</Label>
                    <Input
                      id="mcp-icon"
                      className="mt-[6px]"
                      value={icon}
                      onChange={(event) => setIcon(event.target.value)}
                      maxLength={1}
                      placeholder="🔌"
                    />
                  </div>
                </div>
              </div>
            </section>
          </div>
        </details>
      </div>

      <div data-mcp-form-footer className="flex shrink-0 justify-end gap-[8px] border-t border-[#e8ebf2] bg-white px-[24px] py-[14px]">
        <Button type="button" variant="outline" onClick={onCancel}>
          取消
        </Button>
        <Button type="button" disabled={submitting || !slug.trim() || !name.trim() || !url.trim()} onClick={submit}>
          {editing ? '保存' : '登记'}
        </Button>
      </div>
    </div>
  );
}

export default function PluginsPage({ auth }: Props) {
  const [activeSection, setActiveSection] = useState<ExtensionSection>('mcp');
  const [flags, setFlags] = useState<McpListFlags>(EMPTY_FLAGS);
  const [servers, setServers] = useState<McpServerView[]>([]);
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<McpServerView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<McpServerView | null>(null);
  const [toolsFor, setToolsFor] = useState<string | null>(null);
  const [tools, setTools] = useState<McpToolView[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .get<McpAdminListResponse>('/api/v1/plugins/mcp')
      .then((payload) => {
        if (cancelled) return;
        setFlags({
          enabled: payload.enabled,
          write_enabled: payload.write_enabled,
          prerequisites_met: payload.prerequisites_met,
          prerequisites_reason: payload.prerequisites_reason,
        });
        setServers(payload.items);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载扩展失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    api
      .get<DepartmentView[]>('/api/v1/departments')
      .then((rows) => {
        if (!cancelled) setDepartments(rows);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (activeSection !== 'mcp') return;
    const cancel = load();
    return cancel;
  }, [activeSection, load]);

  const path = (slug: string) => `/api/v1/plugins/mcp/${encodeURIComponent(slug)}`;

  function run(slug: string, action: () => Promise<unknown>, message: string) {
    setBusySlug(slug);
    action()
      .then(() => {
        notify.success(message);
        load();
        return undefined;
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '操作失败'))
      .finally(() => setBusySlug(null));
  }

  const loadTools = useCallback((slug: string) => {
    setToolsFor(slug);
    setToolsLoading(true);
    api
      .get<McpToolsResponse>(`/api/v1/plugins/mcp/${encodeURIComponent(slug)}/tools`)
      .then((payload) => setTools(payload.items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '工具加载失败'))
      .finally(() => setToolsLoading(false));
  }, []);

  function submitForm(payload: McpCreatePayload & { clear_headers?: boolean }) {
    setSubmitting(true);
    const request = editing
      ? api.put<McpServerResponse>(path(editing.slug), payload)
      : api.post<McpServerResponse>('/api/v1/plugins/mcp', payload);
    request
      .then(() => {
        notify.success(editing ? '已保存' : '已登记，请测试连接后启用');
        setFormOpen(false);
        setEditing(null);
        load();
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '保存失败'))
      .finally(() => setSubmitting(false));
  }

  const mcpContent = (
    <PluginsView
        flags={flags}
        servers={servers}
        loaded={loaded}
        busySlug={busySlug}
        toolsFor={toolsFor}
        tools={tools}
        toolsLoading={toolsLoading}
        onRefresh={() => void load()}
        onCreate={() => {
          setEditing(null);
          setFormOpen(true);
        }}
        onEdit={(server) => {
          setEditing(server);
          setFormOpen(true);
        }}
        onDelete={setDeleteTarget}
        onTest={(server) =>
          run(
            server.slug,
            () =>
              api
                .post<McpTestResponse>(path(server.slug) + '/test')
                .then((result) => {
                  notify.success(
                    `连接成功：${result.tool_count} 个工具（只读 ${result.read_only_tool_count}），${result.latency_ms}ms`,
                  );
                }),
            '连接测试完成',
          )
        }
        onToggleEnabled={(server) =>
          run(
            server.slug,
            () =>
              api.put<McpServerResponse>(path(server.slug) + '/status', {
                enabled: !server.enabled,
              }),
            server.enabled ? '已停用' : '已启用',
          )
        }
        onOpenTools={(server) => loadTools(server.slug)}
        onCloseTools={() => setToolsFor(null)}
        onRefreshTools={(server) =>
          run(
            server.slug,
            () =>
              api
                .post<McpToolsResponse>(path(server.slug) + '/refresh')
                .then((payload) => setTools(payload.items)),
            '工具已刷新',
          )
        }
        onToggleTool={(server, tool) =>
          run(
            server.slug,
            () =>
              api
                .put<McpToolsResponse>(
                  path(server.slug) + `/tools/${encodeURIComponent(tool.name)}/toggle`,
                  {},
                )
                .then((payload) => setTools(payload.items)),
            tool.disabled ? '已启用工具' : '已停用工具',
          )
        }
        onToggleWriteTool={(server, tool) => {
          const next = tool.write_allowed
            ? server.write_tools.filter((name) => name !== tool.name)
            : [...server.write_tools, tool.name];
          run(
            server.slug,
            () =>
              api.put<McpServerResponse>(path(server.slug) + '/write-tools', {
                write_tools: next,
              }),
            tool.write_allowed ? '已移出写白名单' : '已加入写白名单',
          );
          setTools((current) =>
            current.map((item) =>
              item.name === tool.name
                ? { ...item, write_allowed: !tool.write_allowed }
                : item,
            ),
          );
        }}
    />
  );

  return (
    <>
      <AdminExtensionsView
        activeSection={activeSection}
        onSectionChange={setActiveSection}
        mcpContent={mcpContent}
        skillContent={<SkillsPanel departments={departments} />}
      />

      <Dialog
        open={formOpen}
        onOpenChange={(open) => {
          setFormOpen(open);
          if (!open) setEditing(null);
        }}
      >
        <DialogContent className="flex max-h-[88dvh] w-[min(720px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[18px] p-0 sm:max-w-none">
          <DialogHeader className="shrink-0 border-b border-[#e8ebf2] px-[24px] py-[18px] pr-[56px]">
            <DialogTitle className="text-[16px] font-semibold text-[#18181a]">
              {editing ? `编辑 ${editing.name}` : '登记 MCP 服务'}
            </DialogTitle>
            <p className="text-[12px] leading-[1.6] text-[#758098]">
              填写服务名称、连接地址和访问凭据；授权范围等选项可稍后设置。
            </p>
          </DialogHeader>
          <ServerFormFields
            initial={editing}
            departments={departments}
            submitting={submitting}
            onSubmit={submitForm}
            onCancel={() => {
              setFormOpen(false);
              setEditing(null);
            }}
          />
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget != null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title={`删除 ${deleteTarget?.name ?? ''}`}
        description="将同时清理所有员工的订阅记录；该操作不可撤销。"
        confirmText="删除"
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target) run(target.slug, () => api.delete(path(target.slug)), '已删除');
        }}
      />
    </>
  );
}

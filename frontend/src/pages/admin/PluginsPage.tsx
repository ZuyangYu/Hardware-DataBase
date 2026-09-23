import { useCallback, useEffect, useState } from 'react';

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
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';

type Props = { auth: AuthSession; onLogout: () => void };

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
  userName: string;
  onLogout: () => void;
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
  userName,
  onLogout,
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
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="扩展插件"
        description="登记远程 MCP 服务；默认停用，测试连接通过后方可启用。外部结果不进入知识库证据链。"
        userName={userName}
        onLogout={onLogout}
      />

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
    <div className="flex flex-col gap-[12px]">
      <div className="grid gap-[12px] md:grid-cols-2">
        <div>
          <Label htmlFor="mcp-slug">标识（slug）</Label>
          <Input
            id="mcp-slug"
            value={slug}
            disabled={editing}
            onChange={(event) => setSlug(event.target.value)}
            placeholder="kicad-local"
          />
        </div>
        <div>
          <Label htmlFor="mcp-name">名称</Label>
          <Input id="mcp-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
      </div>

      <div className="grid gap-[12px] md:grid-cols-2">
        <div>
          <Label htmlFor="mcp-transport">传输</Label>
          <select
            id="mcp-transport"
            className="h-[36px] w-full rounded-[10px] border border-[#e1e5ee] bg-white px-[10px] text-[13px]"
            value={transport}
            onChange={(event) => setTransport(event.target.value)}
          >
            <option value="streamable_http">streamable_http</option>
            <option value="sse">sse</option>
          </select>
        </div>
        <div>
          <Label htmlFor="mcp-timeout">超时（秒，可空）</Label>
          <Input
            id="mcp-timeout"
            value={timeoutS}
            onChange={(event) => setTimeoutS(event.target.value)}
            placeholder="15"
          />
        </div>
      </div>

      <div>
        <Label htmlFor="mcp-url">服务地址</Label>
        <Input
          id="mcp-url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://mcp.example.com/mcp"
        />
      </div>

      <div>
        <Label htmlFor="mcp-description">说明</Label>
        <Textarea
          id="mcp-description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          rows={2}
        />
      </div>

      <div>
        <Label htmlFor="mcp-headers">
          请求头（每行 Key: Value{editing ? '；留空即保留已保存凭据' : ''}）
        </Label>
        <Textarea
          id="mcp-headers"
          value={headersText}
          onChange={(event) => setHeadersText(event.target.value)}
          rows={2}
          placeholder={editing && initial?.has_credentials ? '已保存凭据，留空即保留' : 'Authorization: Bearer …'}
        />
        {editing && (
          <label className="mt-[6px] flex cursor-pointer items-center gap-[6px] text-[12px] text-[#464c5e]">
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

      <div className="grid gap-[12px] md:grid-cols-2">
        <div>
          <Label htmlFor="mcp-scope">部门范围</Label>
          <select
            id="mcp-scope"
            className="h-[36px] w-full rounded-[10px] border border-[#e1e5ee] bg-white px-[10px] text-[13px]"
            value={scope}
            onChange={(event) => setScope(event.target.value)}
          >
            <option value="none">不可见</option>
            <option value="departments">指定部门</option>
            <option value="all">全部员工</option>
          </select>
          {scope === 'departments' && (
            <div className="mt-[8px] flex flex-col gap-[6px]" data-mcp-department-picker>
              {departments.length === 0 && (
                <span className="text-[12px] text-[#858b9c]">暂无部门</span>
              )}
              {departments.map((department) => (
                <label
                  key={department.id}
                  className="flex cursor-pointer items-center gap-[6px] text-[12px] text-[#464c5e]"
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
        <div>
          <Label htmlFor="mcp-tags">标签（逗号分隔）</Label>
          <Input
            id="mcp-tags"
            value={tagsText}
            onChange={(event) => setTagsText(event.target.value)}
            placeholder="设计, EDA"
          />
          <Label htmlFor="mcp-icon" className="mt-[8px]">
            图标（1 个字符，可空）
          </Label>
          <Input id="mcp-icon" value={icon} onChange={(event) => setIcon(event.target.value)} />
        </div>
      </div>

      <div className="mt-[4px] flex justify-end gap-[8px]">
        <Button variant="outline" onClick={onCancel}>
          取消
        </Button>
        <Button disabled={submitting || !slug.trim() || !name.trim() || !url.trim()} onClick={submit}>
          {editing ? '保存' : '登记'}
        </Button>
      </div>
    </div>
  );
}

export default function PluginsPage({ auth, onLogout }: Props) {
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
    const cancel = load();
    return cancel;
  }, [load]);

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

  return (
    <>
      <PluginsView
        flags={flags}
        servers={servers}
        loaded={loaded}
        busySlug={busySlug}
        toolsFor={toolsFor}
        tools={tools}
        toolsLoading={toolsLoading}
        userName={auth.user.username}
        onLogout={onLogout}
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

      <Dialog
        open={formOpen}
        onOpenChange={(open) => {
          setFormOpen(open);
          if (!open) setEditing(null);
        }}
      >
        <DialogContent className="max-h-[86vh] max-w-[640px] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{editing ? `编辑 ${editing.name}` : '登记 MCP 服务'}</DialogTitle>
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

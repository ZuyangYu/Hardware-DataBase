import { useEffect, useState } from 'react';

import { api } from '../api/client';
import type { AuthSession } from '../auth';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  auth: AuthSession;
};

type McpTokenStatus = {
  has_token: boolean;
  token_id: string;
  name: string;
  masked_token: string;
  created_at: string;
  expires_at: string;
  last_used_at: string;
  api_base: string;
  mcp_remote_url?: string;
};

type McpTokenCreated = McpTokenStatus & {
  token: string;
  command: string;
};

function formatDate(value: string): string {
  if (!value) return '-';
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(parsed));
}

async function copyText(value: string, label: string) {
  try {
    await navigator.clipboard.writeText(value);
    notify.success(`${label}已复制`);
  } catch {
    notify.error('复制失败，请手动选择文本');
  }
}

export default function McpSettingsDialog({ open, onOpenChange, auth }: Props) {
  const [status, setStatus] = useState<McpTokenStatus | null>(null);
  const [created, setCreated] = useState<McpTokenCreated | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const data = await api.get<McpTokenStatus>('/api/v1/me/mcp-token');
      setStatus(data);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载 MCP 设置失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    setCreated(null);
    void load();
  }, [open]);

  async function generate() {
    setBusy(true);
    try {
      const data = await api.post<McpTokenCreated>('/api/v1/me/mcp-token', { name: '个人 MCP' });
      setCreated(data);
      setStatus(data);
      notify.success('个人 MCP 凭证已生成');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成 MCP 凭证失败');
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    if (!window.confirm('撤销后，使用这个凭证的外部 Agent 会立即断开。继续？')) return;
    setBusy(true);
    try {
      await api.delete('/api/v1/me/mcp-token');
      setCreated(null);
      setStatus({
        has_token: false,
        token_id: '',
        name: '',
        masked_token: '',
        created_at: '',
        expires_at: '',
        last_used_at: '',
        api_base: status?.api_base || '',
      });
      notify.success('个人 MCP 凭证已撤销');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销 MCP 凭证失败');
    } finally {
      setBusy(false);
    }
  }

  const active = created || (status?.has_token ? status : null);
  const remoteUrl = created?.mcp_remote_url || status?.mcp_remote_url || '';
  const remotePaste = created
    ? [
        'MCP 远程接入（标准 streamable-http）',
        `URL: ${remoteUrl}`,
        `请求头：X-HDB-MCP-Token: ${created.token}`,
        '工具名前缀：hdb_（Agent 自动加载）',
      ].join('\n')
    : '';


  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-mcp-settings-dialog
        className="flex max-h-[88vh] flex-col gap-0 rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[980px]"
      >
        <DialogHeader className="gap-[4px] px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">MCP 接入</DialogTitle>
          <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
            给 OpenCode / Claude Code 等外部 Agent 使用的个人凭证。凭证长期有效，可随时撤销；平台审计仍按你本人记录。
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-[16px] overflow-y-auto px-[24px] py-[16px]">
          <div className="grid gap-[16px] xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <section className="rounded-[14px] border border-[#e3e7f1] bg-white px-[18px] py-[16px]">
          <div className="flex items-start justify-between gap-[12px]">
            <div>
              <h2 className="text-[14px] font-semibold text-[#18181a]">个人凭证</h2>
              <p className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]">
                默认有效期 365 天。Token 只在生成时显示一次，请立即复制或保存到密码管理器。
              </p>
            </div>
            <span className={`shrink-0 rounded-full px-[9px] py-[3px] text-[11px] ${active ? 'bg-[#e8f7ee] text-[#28784f]' : 'bg-[#f1f3f7] text-[#6b7486]'}`}>
              {active ? '已生成' : '未生成'}
            </span>
          </div>

          {loading ? (
            <p className="mt-[18px] text-[12px] text-[#757f9c]">加载中…</p>
          ) : active ? (
            <dl className="mt-[16px] grid gap-[9px] text-[12px]">
              <div className="flex items-start justify-between gap-[16px]">
                <dt className="shrink-0 text-[#757f9c]">凭证</dt>
                <dd className="break-all text-right font-mono text-[12px] text-[#18181a]">
                  {created?.token || active.masked_token || '已生成'}
                </dd>
              </div>
              <div className="flex justify-between gap-[16px]">
                <dt className="text-[#757f9c]">到期时间</dt>
                <dd className="text-right text-[#18181a]">{formatDate(active.expires_at)}</dd>
              </div>
              <div className="flex justify-between gap-[16px]">
                <dt className="text-[#757f9c]">最近使用</dt>
                <dd className="text-right text-[#18181a]">{active.last_used_at ? formatDate(active.last_used_at) : '未使用'}</dd>
              </div>
              <div className="flex justify-between gap-[16px]">
                <dt className="text-[#757f9c]">工具名前缀</dt>
                <dd className="text-right font-mono text-[#18181a]">hdb_（自动加载）</dd>
              </div>
            </dl>
          ) : (
            <p className="mt-[18px] text-[12px] leading-[18px] text-[#757f9c]">
              还没有个人 MCP 凭证。生成后，外部 Agent 会用你的身份访问你有权限的知识库和工具。
            </p>
          )}

          <div className="mt-[18px] flex flex-wrap gap-[8px]">
            {active ? (
              <>
                <Button
                  disabled={busy}
                  className="h-[34px] rounded-[10px] bg-[#18181a] text-[13px] text-white hover:bg-[#303030]"
                  onClick={() => void generate()}
                >
                  {busy ? '生成中' : '重新生成'}
                </Button>
                <Button
                  variant="outline"
                  disabled={busy}
                  className="h-[34px] rounded-[10px] border-[#e3e7f1] text-[13px] text-[#b42318]"
                  onClick={() => void revoke()}
                >
                  撤销
                </Button>
              </>
            ) : (
              <Button
                disabled={busy}
                className="h-[34px] rounded-[10px] bg-[#18181a] text-[13px] text-white hover:bg-[#303030]"
                onClick={() => void generate()}
              >
                {busy ? '生成中' : '生成长期凭证'}
              </Button>
            )}
          </div>

          {created ? (
            <div className="mt-[14px] rounded-[10px] border border-[#f0d7a8] bg-[#fff8e8] px-[12px] py-[9px] text-[12px] leading-[18px] text-[#8a5a00]">
              Token 只显示这一次。关闭或刷新页面后，只能重新生成，旧凭证会立即失效。
            </div>
          ) : null}
        </section>

        <section className="rounded-[14px] border border-[#e3e7f1] bg-white px-[18px] py-[16px]">
          <h2 className="text-[14px] font-semibold text-[#18181a]">给外部 Agent 的接入说明（推荐：远程 MCP）</h2>
          <p className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]">
            远程 MCP 是标准 streamable-http，任何支持 MCP 的 Agent（OpenCode / Claude / Cursor
            等）直接连 URL，带上你的 hdbmcp_* 令牌即可，无需在本机启动子进程。
          </p>

          <div className="mt-[14px] grid gap-[10px] text-[12px]">
            <div className="grid gap-[4px]">
              <span className="text-[#757f9c]">远程 MCP URL（streamable-http）</span>
              <code className="break-all rounded-[8px] border border-[#e3e7f1] bg-[#f8fafc] px-[10px] py-[8px] font-mono text-[12px] text-[#18181a]">
                {remoteUrl || '生成凭证后显示'}
              </code>
            </div>
            <div className="grid gap-[4px]">
              <span className="text-[#757f9c]">请求头（工具调用时的门禁）</span>
              <code className="break-all rounded-[8px] border border-[#e3e7f1] bg-[#f8fafc] px-[10px] py-[8px] font-mono text-[12px] text-[#18181a]">
                X-HDB-MCP-Token: {created?.token || active?.masked_token || '<你的 hdbmcp_*>'}
              </code>
            </div>
          </div>

          <div className="mt-[14px] grid gap-[6px]">
            <div className="flex items-center justify-between gap-[8px]">
              <span className="text-[12px] text-[#454b5c]">可复制接入说明</span>
              {created ? (
                <Button
                  variant="outline"
                  className="h-[30px] rounded-[9px] border-[#e3e7f1] px-[10px] text-[12px] text-[#464c5e]"
                  onClick={() => void copyText(remotePaste, '远程接入说明')}
                >
                  复制全部
                </Button>
              ) : null}
            </div>
            <pre className="max-h-[260px] overflow-auto whitespace-pre-wrap break-all rounded-[10px] border border-[#e3e7f1] bg-[#f8fafc] px-[12px] py-[10px] text-[12px] leading-[20px] text-[#18181a]">
              {created ? remotePaste : '生成个人凭证后，这里会显示远程 URL、X-HDB-MCP-Token 请求头和工具名前缀。'}
            </pre>

          </div>

          {created ? (
            <div className="mt-[12px] flex flex-wrap gap-[8px]">
              <Button
                variant="outline"
                className="h-[30px] rounded-[9px] border-[#e3e7f1] px-[10px] text-[12px] text-[#464c5e]"
                onClick={() => void copyText(created.token, 'Token')}
              >
                复制 Token
              </Button>

            </div>
          ) : null}
        </section>
      </div>

      <section className="mt-[16px] rounded-[14px] border border-[#e3e7f1] bg-white px-[18px] py-[16px]">
        <h2 className="text-[14px] font-semibold text-[#18181a]">使用说明</h2>
        <ol className="mt-[10px] grid gap-[8px] text-[12px] leading-[19px] text-[#5f687a]">
          <li>1. 生成个人凭证，复制"可复制接入说明"里的远程 URL 与请求头。</li>
          <li>2. 在 Agent（OpenCode / Claude / Cursor 等）的 MCP 设置里登记该 URL，并配置 <code>X-HDB-MCP-Token</code> 请求头。</li>
          <li>3. 远程 MCP 本身常驻服务端，无需本机启动子进程。Agent 会自动加载 hdb 工具（工具名形如 <code>hdb_list_kbs</code>），直接用自然语言提问即可。</li>
          <li>4. 凭证泄露或人员变动时，立即在本页撤销并重新生成。</li>
        </ol>
        <p className="mt-[10px] text-[11px] leading-[18px] text-[#8a93a5]">
          你当前登录账号：{auth.user.username}。所有通过该凭证的调用都会记到你的账号名下。
        </p>
      </section>
        </div>

        <div className="flex justify-end border-t border-[#eef1f6] px-[24px] py-[12px]">
          <Button
            variant="outline"
            className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[13px] text-[#464c5e] hover:bg-[#f6f6f6]"
            onClick={() => onOpenChange(false)}
          >
            关闭
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

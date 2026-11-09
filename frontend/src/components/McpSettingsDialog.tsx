import { useEffect, useState } from 'react';

import { api } from '../api/client';
import type { AuthSession } from '../auth';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { copyTextToClipboard } from '../lib/clipboard';

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
  token?: string;
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
  if (!value) {
    notify.error('暂无可复制内容');
    return;
  }
  const copied = await copyTextToClipboard(value);
  if (copied) {
    notify.success(`${label}已复制`);
    return;
  }
  notify.error('自动复制失败，请在弹出的窗口中手动复制');
  window.prompt(`自动复制失败，请手动复制${label}：`, value);
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
  const tokenValue = created?.token || active?.token || '';
  const hasToken = Boolean(tokenValue);
  // 让通用 Agent 自己接线并验证的一段话（小白直接复制粘贴）
  const tokenForPrompt = tokenValue || '<你的 hdbmcp_* 令牌>';
  const agentPrompt = [
    '请帮我把 HDB 知识库 MCP 接入你当前的运行环境，并完成一次验证。',
    '这是一个标准 MCP 服务（streamable-http），只读知识库检索，不需要 API Key。',
    '',
    'MCP 名称：hdb',
    `服务地址：${remoteUrl || '<HDB MCP 地址>'}`,
    `请求头：X-HDB-MCP-Token: ${tokenForPrompt}`,
    '',
    '请按你实际的客户端类型选择一种操作：',
    'A. 如果你是 OpenCode：',
    '   编辑 ~/.config/opencode/config.json，把下面这段合并进已有的 mcp 字段（不要覆盖其它 mcp 项）：',
    '   {',
    '     "hdb": {',
    '       "type": "remote",',
    `       "url": "${remoteUrl || '<HDB MCP 地址>'}",`,
    '       "headers": { "X-HDB-MCP-Token": "' + tokenForPrompt + '" }',
    '     }',
    '   }',
    'B. 如果你是 Claude Code：',
    '   执行：claude mcp add --transport http hdb ' + (remoteUrl || '<HDB MCP 地址>') + ' --header "X-HDB-MCP-Token: ' + tokenForPrompt + '"',
    'C. 其它支持远程 MCP 的客户端：新建远程 MCP，名称 hdb，地址同上，认证/请求头填 X-HDB-MCP-Token。',
    '',
    '保存或执行后重启/新开会话，先调用 hdb_list_kbs 验证连接；成功应看到 hdb_ 前缀的工具（共 16 个）。',
    '然后调用 hdb_search_knowledge_base，搜索“项目中关于休眠电流的要求”，把命中的文件与 chunk 位置告诉我。',
  ].join('\n');


  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        data-mcp-settings-dialog
        className="flex max-h-[96vh] flex-col gap-0 rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[980px] xl:h-[690px]"
      >
        <DialogHeader className="gap-[4px] px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">MCP 接入</DialogTitle>
          <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
            给 OpenCode / Claude Code 等外部 Agent 使用的个人凭证。凭证长期有效，可随时撤销；平台审计仍按你本人记录。
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-[16px] overflow-y-auto px-[24px] py-[16px] xl:overflow-hidden">
          <div className="grid gap-[16px] xl:min-h-0 xl:flex-1 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <section className="flex min-h-0 flex-col rounded-[14px] border border-[#e3e7f1] bg-white px-[18px] py-[16px]">
          <div className="flex items-start justify-between gap-[12px]">
            <div>
              <h2 className="text-[14px] font-semibold text-[#18181a]">个人凭证</h2>
              <p className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]">
                默认有效期 365 天。凭证保存在服务端，可随时查看和复制；如怀疑泄露请立即撤销。
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
                <dd className="min-w-0 break-all text-right font-mono text-[12px] text-[#18181a]">
                  {tokenValue || active.masked_token || '已生成'}
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
                {hasToken ? (
                  <Button
                    variant="outline"
                    className="h-[34px] rounded-[10px] border-[#e3e7f1] text-[13px] text-[#464c5e]"
                    onClick={() => void copyText(tokenValue, 'Token')}
                  >
                    复制 Token
                  </Button>
                ) : null}
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

          {active && !hasToken ? (
            <p className="mt-[10px] text-[11px] leading-[17px] text-[#8a93a5]">
              当前凭证由旧版本生成，令牌无法回显。点“重新生成”会换发新令牌，此后可随时查看和复制。
            </p>
          ) : null}

          {created ? (
            <div className="mt-[14px] rounded-[10px] border border-[#f0d7a8] bg-[#fff8e8] px-[12px] py-[9px] text-[12px] leading-[18px] text-[#8a5a00]">
              新 Token 已生成，旧凭证已立即失效。此令牌保存在服务端，可随时回到本页查看和复制。
            </div>
          ) : null}

          <div className="mt-[14px] border-t border-[#eef1f6] pt-[12px]">
            <h3 className="text-[13px] font-semibold text-[#18181a]">使用说明</h3>
            <ol className="mt-[8px] grid gap-[6px] text-[12px] leading-[18px] text-[#5f687a]">
              <li>1. 生成个人凭证。</li>
              <li>2. 点“复制给 Agent”，把整段话原样粘贴给你正在使用的 Agent。</li>
              <li>3. Agent 会自己完成登记并调用 <code>hdb_list_kbs</code> 验证；之后直接用自然语言提问即可。</li>
              <li>4. 凭证泄露或人员变动时，立即在本页撤销并重新生成。</li>
            </ol>
            <p className="mt-[8px] text-[11px] leading-[17px] text-[#8a93a5]">
              你当前登录账号：{auth.user.username}。所有通过该凭证的调用都会记到你的账号名下。
            </p>
          </div>
        </section>

        <section className="flex min-h-0 flex-col rounded-[14px] border border-[#e3e7f1] bg-white px-[18px] py-[16px]">
          <h2 className="text-[14px] font-semibold text-[#18181a]">给外部 Agent 的接入说明</h2>
          <p className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]">
            复制下面这段话，直接发给你正在使用的 Agent（OpenCode / Claude Code / Cursor 等）。
            这段话已经包含服务地址、你的令牌、对应客户端的登记方法和验证步骤，Agent 会自己完成接入。
          </p>


          <div className="mt-[14px] flex min-h-0 flex-1 flex-col gap-[6px]">
            <div className="flex items-center justify-between gap-[8px]">
              <span className="text-[12px] text-[#454b5c]">复制给 Agent（含地址、令牌、登记方法与验证步骤）</span>
              {hasToken ? (
                <Button
                  variant="outline"
                  className="h-[30px] rounded-[9px] border-[#e3e7f1] px-[10px] text-[12px] text-[#464c5e]"
                  onClick={() => void copyText(agentPrompt, 'Agent 自助接入说明')}
                >
                  复制给 Agent
                </Button>
              ) : null}
            </div>
            <pre className="min-h-[120px] flex-1 overflow-auto whitespace-pre-wrap break-all rounded-[10px] border border-[#e3e7f1] bg-[#f8fafc] px-[12px] py-[10px] text-[12px] leading-[20px] text-[#18181a]">
              {hasToken
                ? agentPrompt
                : active
                  ? '当前凭证由旧版本生成，令牌无法回显。请先点“重新生成”换发一次新令牌。'
                  : '生成个人凭证后，这里会显示一段可直接发给 Agent 的完整接入说明。'}
            </pre>
          </div>
        </section>
      </div>
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

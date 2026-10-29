import { useEffect, useRef, useState } from 'react';

import { api } from '../api/client';
import { ROLE_LABELS, type AuthSession } from '../auth';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import PasswordInput from '@/components/PasswordInput';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  auth: AuthSession;
  onLogout: () => void;
};

type ApiKeyView = {
  key_id: string;
  name: string;
  scopes: string[];
  kb_access_mode: string;
  kb_refs: string[];
  created_at: string;
  expires_at: string;
  status: string;
  masked_key: string;
};

type ApiKeyKbOption = {
  kb_ref: string;
  name: string;
  department_name?: string | null;
};

type CreatedApiKey = ApiKeyView & { api_key: string };
type SettingsSection = 'account' | 'api-key' | 'security';
type ApiKeyMode = 'list' | 'create';
type ApiKeyAction = 'revoke' | 'rotate';

const API_KEY_MAX_TTL_DAYS = 365;
const API_KEY_SCOPE_LABELS: Record<string, string> = {
  read: '读取',
  invoke: '检索 / 查询',
  write: '上传',
};
const API_KEY_STATUS_LABELS: Record<string, string> = {
  active: '启用中',
  expired: '已过期',
  revoked: '已撤销',
};

function formatApiKeyDate(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return value || '未知';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp));
}

function apiKeyScopeLabel(scope: string): string {
  return API_KEY_SCOPE_LABELS[scope] || scope;
}

function apiKeyStatusLabel(status: string): string {
  return API_KEY_STATUS_LABELS[status] || status;
}

function copyTextFallback(value: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '-9999px';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }
  document.body.removeChild(textarea);
  return copied;
}

/**
 * 个人设置弹窗:账号信息 + 修改密码 + 界面缩放 + 退出登录。
 *
 * 从头像菜单点开,不离开当前页面;修改密码需要当前密码,成功后服务端吊销
 * 该账号全部会话,这里直接走退出登录,让用户用新密码重新进入。
 */
export default function AccountSettingsDialog({ open, onOpenChange, auth, onLogout }: Props) {
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [apiKeys, setApiKeys] = useState<ApiKeyView[]>([]);
  const [apiKeyKbs, setApiKeyKbs] = useState<ApiKeyKbOption[]>([]);
  const [apiKeyLoading, setApiKeyLoading] = useState(false);
  const [apiKeyListError, setApiKeyListError] = useState<string | null>(null);
  const [apiKeyKbLoadError, setApiKeyKbLoadError] = useState<string | null>(null);
  const [apiKeyName, setApiKeyName] = useState('');
  const [apiKeyExpiresInDays, setApiKeyExpiresInDays] = useState('90');
  const [apiKeyScopes, setApiKeyScopes] = useState<string[]>(['read', 'invoke']);
  const [selectedKbRefs, setSelectedKbRefs] = useState<string[]>([]);
  const [apiKeySubmitting, setApiKeySubmitting] = useState(false);
  const [newApiKey, setNewApiKey] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState<SettingsSection>('account');
  const [apiKeyMode, setApiKeyMode] = useState<ApiKeyMode>('list');
  const [showRetiredKeys, setShowRetiredKeys] = useState(false);
  const [apiKeyConfirmTarget, setApiKeyConfirmTarget] = useState<{ keyId: string; action: ApiKeyAction } | null>(null);
  const [apiKeyActionBusy, setApiKeyActionBusy] = useState(false);
  const apiKeyContentRef = useRef<HTMLDivElement>(null);

  const isEmployee = auth.user.role === 'employee';

  async function loadApiKeyData({ notifyOnError = true }: { notifyOnError?: boolean } = {}) {
    if (!isEmployee) return;
    setApiKeyLoading(true);
    setApiKeyListError(null);
    setApiKeyKbLoadError(null);

    // These are independent resources. A transient KB-options failure should
    // not hide Keys that were already created, and vice versa.
    const [keysResult, optionsResult] = await Promise.allSettled([
      api.get<ApiKeyView[]>('/api/v1/me/api-keys'),
      api.get<ApiKeyKbOption[]>('/api/v1/me/api-keys/available-kbs'),
    ]);
    const errors: string[] = [];
    if (keysResult.status === 'fulfilled') {
      setApiKeys(keysResult.value);
    } else {
      const message = keysResult.reason instanceof Error ? keysResult.reason.message : '加载已创建的 API Key 失败';
      setApiKeyListError(message);
      errors.push(message);
    }
    if (optionsResult.status === 'fulfilled') {
      const options = optionsResult.value;
      setApiKeyKbs(options);
      setSelectedKbRefs((current) => {
        const allowed = new Set(options.map((option) => option.kb_ref));
        // Do not pre-select the whole allowlist: production keys must be an
        // explicit choice and must not auto-expand with newly accessible KBs.
        return current.filter((ref) => allowed.has(ref));
      });
    } else {
      const message = optionsResult.reason instanceof Error ? optionsResult.reason.message : '加载可绑定知识库失败';
      setApiKeyKbLoadError(message);
      errors.push(message);
    }
    if (notifyOnError && errors.length > 0) notify.error(errors[0]);
    setApiKeyLoading(false);
  }

  useEffect(() => {
    if (open) void loadApiKeyData();
  }, [open, isEmployee]);

  useEffect(() => {
    if (open) {
      setActiveSection('account');
      setApiKeyMode('list');
    }
  }, [open]);

  useEffect(() => {
    if (!open || activeSection !== 'api-key' || apiKeyMode !== 'create') return;
    const focusTimer = window.setTimeout(() => {
      document.getElementById('api-key-name')?.focus();
    }, 80);
    return () => {
      window.clearTimeout(focusTimer);
    };
  }, [open, activeSection, apiKeyMode]);

  function toggleApiKeyScope(scope: string) {
    setApiKeyScopes((current) =>
      current.includes(scope) ? current.filter((item) => item !== scope) : [...current, scope],
    );
  }

  function toggleKbRef(kbRef: string) {
    setSelectedKbRefs((current) =>
      current.includes(kbRef) ? current.filter((item) => item !== kbRef) : [...current, kbRef],
    );
  }

  function selectAllKbRefs() {
    setSelectedKbRefs(apiKeyKbs.map((option) => option.kb_ref));
  }

  function clearKbRefs() {
    setSelectedKbRefs([]);
  }

  function normalizeExpiryInput() {
    const days = Number(apiKeyExpiresInDays);
    if (!Number.isInteger(days) || days < 1) {
      setApiKeyExpiresInDays('90');
      return;
    }
    setApiKeyExpiresInDays(String(Math.min(days, API_KEY_MAX_TTL_DAYS)));
  }

  async function createApiKey() {
    const expiresInDays = Number(apiKeyExpiresInDays);
    if (!apiKeyName.trim() || apiKeyScopes.length === 0 || selectedKbRefs.length === 0) {
      notify.error('请填写 Key 名称、至少一个权限并选择至少一个知识库');
      return;
    }
    if (!Number.isSafeInteger(expiresInDays) || expiresInDays < 1 || expiresInDays > API_KEY_MAX_TTL_DAYS) {
      notify.error(`有效期必须是 1 到 ${API_KEY_MAX_TTL_DAYS} 天`);
      return;
    }
    setApiKeySubmitting(true);
    try {
      const created = await api.post<CreatedApiKey>('/api/v1/me/api-keys', {
        name: apiKeyName.trim(),
        scopes: apiKeyScopes,
        kb_access_mode: 'selected_kbs',
        kb_refs: selectedKbRefs,
        expires_in_days: expiresInDays,
      });
      setNewApiKey(created.api_key);
      const { api_key: _apiKey, ...createdView } = created;
      setApiKeys((current) => [
        createdView,
        ...current.filter((item) => item.key_id !== createdView.key_id),
      ]);
      setApiKeyName('');
      setSelectedKbRefs([]);
      setApiKeyMode('list');
      await loadApiKeyData({ notifyOnError: false });
      requestAnimationFrame(() => apiKeyContentRef.current?.scrollTo({ top: 0, behavior: 'smooth' }));
      notify.success('API Key 已创建；完整凭证只显示这一次');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建 API Key 失败');
    } finally {
      setApiKeySubmitting(false);
    }
  }

  async function revokeApiKey(keyId: string) {
    setApiKeyConfirmTarget({ keyId, action: 'revoke' });
  }

  async function performRevokeApiKey(keyId: string) {
    try {
      await api.delete(`/api/v1/me/api-keys/${encodeURIComponent(keyId)}`);
      setApiKeys((current) => current.map((item) => (
        item.key_id === keyId ? { ...item, status: 'revoked' } : item
      )));
      await loadApiKeyData();
      notify.success('API Key 已停用；历史记录仍保留用于审计');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销 API Key 失败');
    }
  }

  async function rotateApiKey(keyId: string) {
    setApiKeyConfirmTarget({ keyId, action: 'rotate' });
  }

  async function performRotateApiKey(keyId: string) {
    try {
      const created = await api.post<CreatedApiKey>(
        `/api/v1/me/api-keys/${encodeURIComponent(keyId)}/rotate`,
        {},
      );
      setNewApiKey(created.api_key);
      const { api_key: _apiKey, ...createdView } = created;
      setApiKeys((current) => [
        createdView,
        ...current
          .filter((item) => item.key_id !== createdView.key_id)
          .map((item) => item.key_id === keyId ? { ...item, status: 'revoked' } : item),
      ]);
      await loadApiKeyData({ notifyOnError: false });
      notify.success('API Key 已轮换；旧 Key 已停用，请立即复制新 Key');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '轮换 API Key 失败');
    }
  }

  async function confirmApiKeyAction() {
    if (!apiKeyConfirmTarget) return;
    setApiKeyActionBusy(true);
    try {
      if (apiKeyConfirmTarget.action === 'revoke') {
        await performRevokeApiKey(apiKeyConfirmTarget.keyId);
      } else {
        await performRotateApiKey(apiKeyConfirmTarget.keyId);
      }
    } finally {
      setApiKeyActionBusy(false);
      setApiKeyConfirmTarget(null);
    }
  }

  async function copyApiKey() {
    if (!newApiKey) return;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(newApiKey);
        notify.success('API Key 已复制');
        return;
      }
    } catch {
      // Some browsers expose the API but reject it outside a secure context.
    }

    if (copyTextFallback(newApiKey)) {
      notify.success('API Key 已复制');
      return;
    }

    notify.error('浏览器阻止了自动复制，请点击 Key 输入框后使用 Ctrl/Cmd+C');
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) setNewApiKey(null);
    onOpenChange(nextOpen);
  }

  function startCreateApiKey() {
    setNewApiKey(null);
    setApiKeyMode('create');
    requestAnimationFrame(() => {
      apiKeyContentRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  function cancelCreateApiKey() {
    setApiKeyMode('list');
    requestAnimationFrame(() => apiKeyContentRef.current?.scrollTo({ top: 0, behavior: 'smooth' }));
  }

  const settingsSections: Array<{
    id: SettingsSection;
    label: string;
    description: string;
  }> = [
    { id: 'account', label: '账号信息', description: '身份与所属部门' },
    ...(isEmployee
      ? [{ id: 'api-key' as const, label: 'API Key', description: '外部访问凭证' }]
      : []),
    { id: 'security', label: '安全设置', description: '密码与登录安全' },
  ];
  const activeApiKeys = apiKeys.filter((key) => key.status === 'active');
  const retiredApiKeys = apiKeys.filter((key) => key.status !== 'active');
  const canCreateApiKey = Boolean(
    apiKeyName.trim()
      && apiKeyScopes.length > 0
      && selectedKbRefs.length > 0
      && Number.isSafeInteger(Number(apiKeyExpiresInDays))
      && Number(apiKeyExpiresInDays) >= 1
      && Number(apiKeyExpiresInDays) <= API_KEY_MAX_TTL_DAYS,
  );
  const createApiKeyHint = apiKeyLoading
    ? '正在加载可用知识库…'
    : apiKeyKbs.length === 0
      ? '当前没有可绑定的知识库'
      : selectedKbRefs.length === 0
        ? '至少选择一个知识库后才能创建'
        : !apiKeyName.trim()
          ? '请先填写 Key 名称'
          : apiKeyScopes.length === 0
            ? '至少选择一个权限'
            : '创建后完整凭证只显示一次';

  async function submit() {
    if (!currentPassword || !newPassword) {
      notify.error('请填写当前密码和新密码');
      return;
    }
    if (newPassword.length < 8) {
      notify.error('密码长度不能少于 8 位');
      return;
    }
    if (newPassword === currentPassword) {
      notify.error('新密码不能与当前密码相同');
      return;
    }
    if (newPassword !== confirmPassword) {
      notify.error('两次输入的新密码不一致');
      return;
    }
    setSubmitting(true);
    try {
      await api.put('/api/v1/users/me/password', {
        current_password: currentPassword,
        new_password: newPassword,
      });
      notify.success('密码已修改，请用新密码重新登录');
      onLogout();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '修改密码失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="!flex h-[88vh] max-h-[calc(100vh-32px)] min-h-0 flex-col gap-0 overflow-hidden rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[860px]">
        <DialogHeader className="shrink-0 gap-[4px] px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">个人设置</DialogTitle>
          <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
            管理个人资料、外部访问凭证和登录安全。
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-hidden px-[24px] py-[16px]">
          <div className="flex h-full min-h-0 flex-col gap-[16px] md:flex-row">
            <nav aria-label="个人设置导航" className="shrink-0 md:w-[184px]">
              <p className="mb-[8px] px-[10px] text-[11px] font-semibold uppercase tracking-[0.08em] text-[#a0a7b8]">设置</p>
              <div className="flex gap-[6px] overflow-x-auto pb-[2px] md:grid md:overflow-visible">
                {settingsSections.map((section) => {
                  const active = activeSection === section.id;
                  return (
                    <button
                      key={section.id}
                      type="button"
                      aria-current={active ? 'page' : undefined}
                      onClick={() => setActiveSection(section.id)}
                      className={`min-w-[132px] rounded-[10px] border px-[10px] py-[9px] text-left transition-colors md:min-w-0 ${
                        active
                          ? 'border-[#c9d5f2] bg-[#f1f5ff] text-[#31559d]'
                          : 'border-transparent text-[#626b82] hover:border-[#e3e7f1] hover:bg-[#f7f8fb]'
                      }`}
                    >
                      <span className="block text-[12px] font-semibold">{section.label}</span>
                      <span className={`mt-[2px] block whitespace-nowrap text-[10px] ${active ? 'text-[#6680bd]' : 'text-[#a0a7b8]'}`}>
                        {section.description}
                      </span>
                    </button>
                  );
                })}
              </div>
            </nav>

            <div ref={apiKeyContentRef} className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain pr-[2px] [scrollbar-gutter:stable]">
              {activeSection === 'account' ? (
          <section className="rounded-[12px] border border-[#e3e7f1] px-[16px] py-[14px]">
            <div className="flex items-start gap-[12px]">
              <div className="flex h-[40px] w-[40px] shrink-0 items-center justify-center rounded-[12px] bg-[#eef3ff] text-[#4967a8]">
                <AppIcon name="user" size={20} />
              </div>
              <div>
                <h3 className="text-[13px] font-semibold text-[#18181a]">账号信息</h3>
                <p className="mt-[3px] text-[12px] leading-[18px] text-[#757f9c]">查看当前登录身份和组织归属。</p>
              </div>
            </div>
            <dl className="mt-[18px] grid gap-[12px] text-[13px] sm:grid-cols-2">
              <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                <dt className="text-[#757f9c]">用户名</dt>
                <dd className="break-all text-[#18181a]">{auth.user.username}</dd>
              </div>
              <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                <dt className="text-[#757f9c]">角色</dt>
                <dd className="break-all text-[#18181a]">{ROLE_LABELS[auth.user.role]}</dd>
              </div>
              {auth.user.department_name ? (
                <div className="grid grid-cols-[64px_1fr] items-center gap-[8px]">
                  <dt className="text-[#757f9c]">部门</dt>
                <dd className="break-all text-[#18181a]">{auth.user.department_name}</dd>
              </div>
            ) : null}
            </dl>
          </section>
              ) : null}

              {activeSection === 'api-key' && isEmployee ? (
            <section className="rounded-[12px] border border-[#e3e7f1] bg-white px-[16px] py-[14px]">
              <div className="flex items-start justify-between gap-[12px]">
                <div>
                  {apiKeyMode === 'create' ? (
                    <button
                      type="button"
                      onClick={cancelCreateApiKey}
                      className="mb-[7px] text-[12px] font-medium text-[#4967a8] hover:underline"
                    >
                      ← 返回 Key 列表
                    </button>
                  ) : null}
                  <div className="flex items-center gap-[8px]">
                    <h3 className="text-[13px] font-semibold text-[#18181a]">
                      {apiKeyMode === 'create' ? '创建 API Key' : 'API Key'}
                    </h3>
                    {apiKeyMode === 'list' ? (
                      <span className="rounded-full bg-[#eef3ff] px-[8px] py-[2px] text-[11px] text-[#4967a8]">员工自主管理</span>
                    ) : null}
                  </div>
                  <p className="mt-[4px] text-[12px] leading-[18px] text-[#757f9c]">
                    {apiKeyMode === 'create'
                      ? '为一个系统或用途创建独立凭证，按最小权限绑定知识库。'
                      : 'Key 只允许访问你选择的知识库。完整凭证只展示一次，关闭窗口后只能查看掩码。'}
                  </p>
                </div>
                {apiKeyMode === 'list' ? (
                <div className="flex shrink-0 items-center gap-[8px]">
                  {apiKeyLoading ? <span className="text-[12px] text-[#757f9c]">同步中…</span> : null}
                  <Button
                    variant="outline"
                    onClick={startCreateApiKey}
                    className="h-[32px] rounded-[9px] border-[#c9d5f2] px-[10px] text-[12px] text-[#31559d] hover:bg-[#f1f5ff]"
                  >
                    创建新 Key
                  </Button>
                </div>
                ) : null}
              </div>

              {apiKeyMode === 'list' && apiKeyListError ? (
                <div className="mt-[12px] flex items-center justify-between gap-[10px] rounded-[9px] border border-[#f2caca] bg-[#fff5f5] px-[10px] py-[8px] text-[12px] text-[#a33a3a]">
                  <span>已创建的 Key 列表加载失败：{apiKeyListError}</span>
                  <button type="button" onClick={() => void loadApiKeyData()} className="shrink-0 font-medium text-[#8c3030] hover:underline">重新加载</button>
                </div>
              ) : null}

              {apiKeyMode === 'list' && newApiKey ? (
                <div className="mt-[12px] rounded-[10px] border border-[#e9c46a] bg-[#fff9eb] p-[12px]">
                  <div className="flex items-start justify-between gap-[12px]">
                    <div>
                      <p className="text-[12px] font-semibold text-[#7a5310]">API Key 已创建</p>
                      <p className="mt-[3px] text-[12px] leading-[18px] text-[#966f27]">请立即复制完整凭证；关闭此窗口后无法恢复。</p>
                    </div>
                    <Button onClick={() => void copyApiKey()} className="h-[30px] shrink-0 rounded-[8px] bg-[#18181a] px-[10px] text-[12px] text-white">
                      复制完整 Key
                    </Button>
                  </div>
                  <Input
                    readOnly
                    value={newApiKey}
                    aria-label="新建 API Key"
                    onFocus={(event) => event.currentTarget.select()}
                    onClick={(event) => event.currentTarget.select()}
                    className="mt-[8px] font-mono text-[12px]"
                  />
                </div>
              ) : null}

              {apiKeyMode === 'list' ? (
              <>
              <div className="mt-[14px] rounded-[10px] bg-[#f7f8fb] p-[12px]">
                <div className="flex items-center justify-between gap-[8px]">
                  <div>
                    <h4 className="text-[12px] font-semibold text-[#18181a]">启用中的 Key</h4>
                    <p className="mt-[2px] text-[11px] text-[#858b9c]">可同时创建多个 Key，按用途分别管理。</p>
                  </div>
                  <span className="rounded-full bg-white px-[8px] py-[3px] text-[11px] text-[#757f9c]">{activeApiKeys.length} 个</span>
                </div>
                <div className="mt-[8px] grid gap-[8px]">
                {activeApiKeys.length === 0 ? (
                  <div className="rounded-[9px] border border-dashed border-[#d8deeb] bg-white px-[12px] py-[14px] text-[12px] text-[#858b9c]">
                    暂无启用中的 API Key，点击右上角“创建新 Key”开始使用。
                  </div>
                ) : activeApiKeys.map((key) => (
                  <div key={key.key_id} className="flex flex-col gap-[9px] rounded-[9px] border border-[#e3e7f1] bg-white px-[10px] py-[10px] sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-[7px]">
                        <span className="truncate text-[12px] font-semibold text-[#18181a]">{key.name}</span>
                        <span className={`rounded-full px-[7px] py-[2px] text-[10px] ${key.status === 'active' ? 'bg-[#eaf7ef] text-[#267545]' : 'bg-[#eef0f4] text-[#757f9c]'}`}>
                          {apiKeyStatusLabel(key.status)}
                        </span>
                      </div>
                      <div className="mt-[4px] truncate font-mono text-[11px] text-[#757f9c]">{key.masked_key}</div>
                      <div className="mt-[5px] flex flex-wrap gap-x-[12px] gap-y-[3px] text-[11px] text-[#757f9c]">
                        <span>权限：{key.scopes.map((scope) => apiKeyScopeLabel(scope)).join('、')}</span>
                        <span>知识库：{key.kb_refs.length} 个</span>
                        <span>有效期至：{formatApiKeyDate(key.expires_at)}</span>
                      </div>
                    </div>
                    {key.status === 'active' ? (
                      <div className="flex shrink-0 items-center gap-[6px]">
                        <Button variant="outline" onClick={() => void rotateApiKey(key.key_id)} className="h-[28px] rounded-[8px] border-[#e3e7f1] px-[8px] text-[12px] text-[#464c5e]">
                          轮换
                        </Button>
                        <Button variant="outline" onClick={() => void revokeApiKey(key.key_id)} className="h-[28px] rounded-[8px] border-[#e3e7f1] px-[8px] text-[12px] text-[#a33a3a]">
                          停用
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ))}
                </div>
              </div>

              {retiredApiKeys.length > 0 ? (
                <div className="mt-[12px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] p-[12px]">
                  <button
                    type="button"
                    onClick={() => setShowRetiredKeys((current) => !current)}
                    aria-expanded={showRetiredKeys}
                    className="flex w-full items-center justify-between gap-[8px] text-left"
                  >
                    <span>
                      <span className="block text-[12px] font-semibold text-[#464c5e]">历史 Key</span>
                      <span className="mt-[2px] block text-[11px] text-[#858b9c]">已撤销或已过期的凭证仅保留审计记录，不能恢复。</span>
                    </span>
                    <span className="shrink-0 text-[11px] text-[#757f9c]">{retiredApiKeys.length} 个 {showRetiredKeys ? '收起' : '查看'}</span>
                  </button>
                  {showRetiredKeys ? (
                    <div className="mt-[8px] grid gap-[6px]">
                      {retiredApiKeys.map((key) => (
                        <div key={key.key_id} className="flex flex-wrap items-center justify-between gap-[8px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] py-[8px] text-[11px]">
                          <div className="min-w-0">
                            <span className="font-medium text-[#626b82]">{key.name}</span>
                            <span className="ml-[8px] font-mono text-[#a0a7b8]">{key.masked_key}</span>
                          </div>
                          <span className="shrink-0 text-[#858b9c]">{apiKeyStatusLabel(key.status)} · {formatApiKeyDate(key.expires_at)}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
              </>
              ) : (
                <div className="mt-[16px] rounded-[10px] border border-[#e3e7f1] bg-[#fbfcfe] p-[14px]">
                  <div className="grid gap-[12px]">
                    <div className="grid gap-[6px] sm:grid-cols-[minmax(0,1fr)_180px]">
                      <div className="grid gap-[6px]">
                        <Label htmlFor="api-key-name" className="text-[12px] text-[#464c5e]">名称</Label>
                        <Input
                          id="api-key-name"
                          value={apiKeyName}
                          onChange={(event) => setApiKeyName(event.target.value)}
                          placeholder="例如：WCCA 文档生成"
                          aria-describedby="api-key-name-help"
                        />
                        <span id="api-key-name-help" className="text-[11px] text-[#858b9c]">用于识别用途，不会影响权限。</span>
                      </div>
                      <div className="grid gap-[6px]">
                        <Label htmlFor="api-key-expiry" className="text-[12px] text-[#464c5e]">有效期（天）</Label>
                        <Input
                          id="api-key-expiry"
                          type="number"
                          min={1}
                          max={API_KEY_MAX_TTL_DAYS}
                          step={1}
                          inputMode="numeric"
                          value={apiKeyExpiresInDays}
                          onChange={(event) => setApiKeyExpiresInDays(event.target.value)}
                          onBlur={normalizeExpiryInput}
                          aria-describedby="api-key-expiry-help"
                        />
                        <span id="api-key-expiry-help" className="text-[11px] text-[#858b9c]">1–{API_KEY_MAX_TTL_DAYS} 天，到期后自动失效。</span>
                      </div>
                    </div>

                    <fieldset>
                      <legend className="text-[12px] text-[#464c5e]">权限</legend>
                      <div className="mt-[6px] grid gap-[6px] sm:grid-cols-3">
                        {(['read', 'invoke', 'write'] as const).map((scope) => (
                          <label key={scope} className={`flex cursor-pointer items-start gap-[7px] rounded-[8px] border px-[9px] py-[8px] ${apiKeyScopes.includes(scope) ? 'border-[#b8c8ed] bg-[#f4f7ff]' : 'border-[#e3e7f1] bg-white'}`}>
                            <input type="checkbox" className="mt-[2px]" checked={apiKeyScopes.includes(scope)} onChange={() => toggleApiKeyScope(scope)} />
                            <span>
                              <span className="block text-[12px] font-medium text-[#464c5e]">{apiKeyScopeLabel(scope)}</span>
                              <span className="mt-[2px] block text-[10px] leading-[14px] text-[#858b9c]">{scope === 'read' ? '查看、下载和读取证据' : scope === 'invoke' ? '检索、表格和电路查询' : '向所选知识库上传文件'}</span>
                            </span>
                          </label>
                        ))}
                      </div>
                      {apiKeyScopes.includes('write') ? (
                        <p className="mt-[6px] text-[11px] leading-[17px] text-[#9a6a12]">上传权限不包含删除知识库、文件或修改共享设置。</p>
                      ) : null}
                    </fieldset>

                    <div>
                      <div className="flex items-center justify-between gap-[8px]">
                        <div>
                          <p className="text-[12px] text-[#464c5e]">可访问知识库</p>
                          <p className="mt-[2px] text-[11px] text-[#858b9c]">只允许访问你明确选择的知识库。已选 {selectedKbRefs.length} / {apiKeyKbs.length}</p>
                        </div>
                        {apiKeyKbs.length > 0 ? (
                          <div className="flex shrink-0 gap-[8px] text-[11px]">
                            <button type="button" onClick={selectAllKbRefs} className="text-[#4967a8] hover:underline">全选</button>
                            <button type="button" onClick={clearKbRefs} className="text-[#757f9c] hover:underline">清空</button>
                          </div>
                        ) : null}
                      </div>
                      {apiKeyKbLoadError ? (
                        <div className="mt-[6px] flex items-center justify-between gap-[10px] rounded-[8px] border border-[#f2caca] bg-[#fff5f5] px-[9px] py-[8px] text-[11px] text-[#a33a3a]">
                          <span>知识库列表加载失败：{apiKeyKbLoadError}</span>
                          <button type="button" onClick={() => void loadApiKeyData()} className="shrink-0 font-medium text-[#8c3030] hover:underline">重新加载</button>
                        </div>
                      ) : apiKeyKbs.length === 0 ? (
                        <div className="mt-[6px] rounded-[8px] bg-[#f7f8fb] px-[9px] py-[8px] text-[11px] text-[#a0a7b8]">当前没有可绑定的知识库</div>
                      ) : (
                        <div className="mt-[6px] grid max-h-[180px] gap-[5px] overflow-y-auto rounded-[8px] border border-[#e3e7f1] bg-white p-[8px] sm:grid-cols-2">
                          {apiKeyKbs.map((kb) => (
                            <label key={kb.kb_ref} className="flex min-w-0 cursor-pointer items-center gap-[8px] rounded-[6px] px-[6px] py-[5px] text-[11px] text-[#464c5e] hover:bg-[#f7f8fb]">
                              <input type="checkbox" checked={selectedKbRefs.includes(kb.kb_ref)} onChange={() => toggleKbRef(kb.kb_ref)} />
                              <span className="min-w-0 truncate">{kb.name}</span>
                              {kb.department_name ? <span className="shrink-0 text-[#a0a7b8]">({kb.department_name})</span> : null}
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="sticky bottom-0 mt-[16px] -mx-[14px] flex flex-wrap items-center justify-between gap-[10px] border-t border-[#e3e7f1] bg-[#fbfcfe]/95 px-[14px] py-[12px] backdrop-blur-sm">
                    <span className="text-[11px] text-[#858b9c]" role="status">{createApiKeyHint}</span>
                    <div className="flex shrink-0 items-center gap-[8px]">
                      <Button variant="outline" onClick={cancelCreateApiKey} className="h-[32px] rounded-[9px] border-[#e3e7f1] px-[11px] text-[12px] text-[#464c5e]">
                        取消
                      </Button>
                      <Button onClick={() => void createApiKey()} disabled={!canCreateApiKey || apiKeySubmitting || apiKeyLoading} className="h-[32px] rounded-[9px] bg-[#18181a] px-[12px] text-[12px] text-white hover:bg-[#303030]">
                        {apiKeySubmitting ? '创建中…' : '创建 Key'}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

            </section>
              ) : null}

              {activeSection === 'security' ? (
          <section className="rounded-[12px] border border-[#e3e7f1] px-[16px] py-[14px]">
            <div className="flex items-start gap-[12px]">
              <div className="flex h-[40px] w-[40px] shrink-0 items-center justify-center rounded-[12px] bg-[#fff5e7] text-[#a06b18]">
                <AppIcon name="lock" size={20} />
              </div>
              <div>
                <h3 className="text-[13px] font-semibold text-[#18181a]">修改密码</h3>
                <p className="mt-[3px] text-[12px] leading-[18px] text-[#757f9c]">修改后所有已有会话失效，需要使用新密码重新登录。</p>
              </div>
            </div>
            <div className="mt-[18px] grid max-w-[560px] gap-[10px]">
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">当前密码</Label>
                <PasswordInput
                  autoComplete="current-password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  placeholder="输入当前密码"
                />
              </div>
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">新密码</Label>
                <PasswordInput
                  autoComplete="new-password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  placeholder="至少 8 位"
                />
              </div>
              <div className="grid gap-[6px]">
                <Label className="text-[12px] text-[#464c5e]">确认新密码</Label>
                <PasswordInput
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="再次输入新密码"
                />
              </div>
              <div className="flex justify-end">
                <Button
                  onClick={submit}
                  disabled={submitting}
                  className="h-[32px] min-w-[96px] rounded-[10px] bg-[#18181a] text-[14px] text-white hover:bg-[#303030]"
                >
                  {submitting ? '提交中' : '修改密码'}
                </Button>
              </div>
            </div>
          </section>
              ) : null}
            </div>
          </div>

        </div>

        <div className="shrink-0 flex items-center justify-between gap-[8px] border-t border-[#eef1f6] px-[24px] py-[14px]">
          <span className="text-[12px] text-[#757f9c]">退出当前账号，需要重新登录。</span>
          <div className="flex items-center gap-[8px]">
            <Button
              variant="outline"
              onClick={() => onOpenChange(false)}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              关闭
            </Button>
            <Button
              variant="outline"
              onClick={onLogout}
              className="h-[32px] rounded-[10px] border-[#e3e7f1] text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              <AppIcon name="logout" size={16} />
              退出登录
            </Button>
          </div>
        </div>
      </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={apiKeyConfirmTarget !== null}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && !apiKeyActionBusy) setApiKeyConfirmTarget(null);
        }}
        title={apiKeyConfirmTarget?.action === 'rotate' ? '轮换 API Key？' : '停用 API Key？'}
        description={apiKeyConfirmTarget?.action === 'rotate'
          ? '旧 Key 会立即失效，新 Key 只显示一次，请确认后立即复制。'
          : '停用后该 Key 将立即失效，历史记录会保留用于审计。'}
        confirmText={apiKeyConfirmTarget?.action === 'rotate' ? '确认轮换' : '停用 Key'}
        destructive={apiKeyConfirmTarget?.action !== 'rotate'}
        loading={apiKeyActionBusy}
        onConfirm={() => void confirmApiKeyAction()}
      />
    </>
  );
}

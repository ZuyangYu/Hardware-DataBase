import { useCallback, useEffect, useState } from 'react';

import { api } from '../../api/client';
import type {
  ConfigResponse,
  LlmHealthResponse,
  OkResponse,
  RagflowHealthResponse,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { Button } from '@/components/ui/button';
import { Input, Label, Textarea } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

// 分组(对齐 settings.DEFAULT_VALUES)。值是 [key, 标签]。
type FieldDef = [string, string];
type Group = { title: string; fields: FieldDef[]; textarea?: boolean };

const GROUPS: Group[] = [
  {
    title: 'RAGFlow 检索',
    fields: [
      ['RAGFLOW_BASE_URL', 'Base URL'],
      ['RAGFLOW_API_KEY', 'API Key'],
      ['RAGFLOW_GOVERNANCE_DATASET_NAME', '治理数据集'],
      ['RAGFLOW_DESIGN_DATASET_NAME', '设计数据集'],
      ['RAGFLOW_TIMEOUT_SECONDS', '超时(秒)'],
      ['RAGFLOW_SIMILARITY_THRESHOLD', '相似度阈值'],
      ['RAGFLOW_VECTOR_WEIGHT', '向量权重'],
    ],
  },
  {
    title: '认证与会话',
    fields: [
      ['AUTH_DB_PATH', 'Auth DB 路径'],
      ['AUTH_DEFAULT_ADMIN_USERNAME', '默认管理员'],
      ['AUTH_DEFAULT_ADMIN_PASSWORD', '默认管理员密码'],
      ['AUTH_SESSION_TTL_HOURS', '会话 TTL(小时)'],
    ],
  },
  {
    title: 'Agent 模型',
    fields: [
      ['AGENT_LLM_PROVIDER', 'Provider (ollama/custom)'],
      ['AGENT_OLLAMA_BASE_URL', 'Ollama Base URL'],
      ['AGENT_OLLAMA_MODEL', 'Ollama Model'],
      ['AGENT_CUSTOM_API_KEY', 'Custom API Key'],
      ['AGENT_CUSTOM_BASE_URL', 'Custom Base URL'],
      ['AGENT_CUSTOM_MODEL', 'Custom Model'],
      ['AGENT_CUSTOM_MAX_TOKENS', 'Custom Max Tokens'],
      ['AGENT_TEMPERATURE', 'Temperature'],
      ['AGENT_TIMEOUT_SECONDS', '超时(秒)'],
      ['AGENT_RATE_LIMIT_MAX_RETRIES', '限流重试次数'],
      ['AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS', '限流初始延迟(秒)'],
      ['AGENT_RATE_LIMIT_MAX_DELAY_SECONDS', '限流最大延迟(秒)'],
    ],
  },
  {
    title: 'Agent 检索',
    fields: [
      ['FINAL_TOP_K', 'Final Top K'],
      ['AGENT_MAX_RETRIEVAL_ROUNDS', '最大检索轮数'],
    ],
  },
  {
    title: '可观测性',
    fields: [
      ['OBS_ENABLED', '启用观测'],
      ['OTEL_SERVICE_NAME', '服务名'],
      ['OTEL_EXPORTER_OTLP_ENDPOINT', 'OTLP Endpoint'],
      ['OBS_ENVIRONMENT', '环境'],
      ['OBS_SERVICE_VERSION', '服务版本'],
      ['OBS_TRACE_SAMPLE_RATIO', 'Trace 采样率'],
      ['OBS_CAPTURE_CONTENT', '允许采集内容'],
      ['OBS_CAPTURE_QUERY', '允许采集问题'],
      ['OBS_CAPTURE_EVIDENCE', '允许采集证据'],
      ['OBS_CAPTURE_LLM_CONTENT', '允许采集 LLM 内容'],
      ['OBS_CONTENT_MAX_CHARS', '内容最大长度'],
      ['OBS_LOG_FORMAT', '日志格式'],
      ['OBS_METRICS_ENABLED', '启用 Metrics'],
      ['OBS_TRACES_ENABLED', '启用 Traces'],
      ['OBS_LOGS_ENABLED', '启用 Logs'],
      ['OBS_PHOENIX_PROJECT', 'Phoenix 项目'],
      ['OBS_GRAFANA_BASE_URL', 'Grafana 地址'],
      ['OBS_PHOENIX_BASE_URL', 'Phoenix 地址'],
      ['OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS', 'Worker 心跳间隔(秒)'],
      ['OBS_WORKER_STALE_SECONDS', 'Worker 过期阈值(秒)'],
      ['OBS_DEPENDENCY_TIMEOUT_SECONDS', '依赖探测超时(秒)'],
    ],
  },
  {
    title: '系统提示词',
    textarea: true,
    fields: [
      ['SYSTEM_PROMPT', 'System Prompt'],
      ['NO_CONTEXT_PROMPT', 'No-Context Prompt'],
    ],
  },
];

const SECRET_KEYS = new Set(['RAGFLOW_API_KEY', 'AGENT_CUSTOM_API_KEY', 'AUTH_DEFAULT_ADMIN_PASSWORD']);
const NUMBER_FIELDS: Record<string, { min: number; max?: number; step?: number; integer?: boolean }> = {
  RAGFLOW_TIMEOUT_SECONDS: { min: 10, max: 600, integer: true },
  RAGFLOW_SIMILARITY_THRESHOLD: { min: 0, max: 1, step: 0.05 },
  RAGFLOW_VECTOR_WEIGHT: { min: 0, max: 1, step: 0.05 },
  AUTH_SESSION_TTL_HOURS: { min: 1, max: 720, integer: true },
  AGENT_CUSTOM_MAX_TOKENS: { min: 256, max: 65536, step: 256, integer: true },
  AGENT_TEMPERATURE: { min: 0, max: 2, step: 0.1 },
  AGENT_TIMEOUT_SECONDS: { min: 10, max: 600, step: 10, integer: true },
  AGENT_RATE_LIMIT_MAX_RETRIES: { min: 0, max: 20, integer: true },
  AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS: { min: 0, max: 120 },
  AGENT_RATE_LIMIT_MAX_DELAY_SECONDS: { min: 0, max: 600 },
  FINAL_TOP_K: { min: 1, max: 50, integer: true },
  AGENT_MAX_RETRIEVAL_ROUNDS: { min: 1, max: 20, integer: true },
  OBS_TRACE_SAMPLE_RATIO: { min: 0, max: 1, step: 0.05 },
  OBS_CONTENT_MAX_CHARS: { min: 1000, max: 200000, integer: true },
  OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS: { min: 1, max: 300 },
  OBS_WORKER_STALE_SECONDS: { min: 5, max: 3600, integer: true },
  OBS_DEPENDENCY_TIMEOUT_SECONDS: { min: 0.1, max: 60 },
};

export default function ConfigPage({ auth, onLogout }: Props) {
  const [snapshot, setSnapshot] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ragflowHealth, setRagflowHealth] = useState<RagflowHealthResponse | null>(null);
  const [llmHealth, setLlmHealth] = useState<LlmHealthResponse | null>(null);
  const [probing, setProbing] = useState<string | null>(null);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .get<ConfigResponse>('/api/v1/config')
      .then((resp) => {
        if (cancelled) return;
        const snap: Record<string, string> = {};
        for (const [k, v] of Object.entries(resp.settings)) {
          snap[k] = v == null ? '' : String(v);
        }
        setSnapshot(snap);
        setDraft(snap);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载配置失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  function setField(key: string, value: string) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  function visibleGroups(): Group[] {
    const provider = String(draft.AGENT_LLM_PROVIDER || 'ollama');
    return GROUPS.map((group) => {
      if (group.title !== 'Agent 模型') return group;
      return {
        ...group,
        fields: group.fields.filter(([key]) => {
          if (key === 'AGENT_LLM_PROVIDER') return true;
          if (provider === 'ollama') return key.startsWith('AGENT_OLLAMA');
          return !key.startsWith('AGENT_OLLAMA');
        }),
      };
    });
  }

  function validateDraft(changes: Record<string, string>): string[] {
    const view = { ...snapshot, ...draft, ...changes };
    const errors: string[] = [];
    const provider = String(view.AGENT_LLM_PROVIDER || 'ollama');
    if (!['ollama', 'custom'].includes(provider)) errors.push('AGENT_LLM_PROVIDER 必须为 ollama 或 custom');
    if (provider === 'ollama') {
      if (!String(view.AGENT_OLLAMA_BASE_URL || '').trim()) errors.push('Agent Ollama Base URL 不能为空');
      if (!String(view.AGENT_OLLAMA_MODEL || '').trim()) errors.push('Agent Ollama 模型不能为空');
    } else {
      if (!String(view.AGENT_CUSTOM_API_KEY || '').trim()) errors.push('Agent API Key 不能为空');
      if (!String(view.AGENT_CUSTOM_BASE_URL || '').trim()) errors.push('Agent Base URL 不能为空');
      if (!String(view.AGENT_CUSTOM_MODEL || '').trim()) errors.push('Agent LLM 模型不能为空');
    }
    if (!String(view.RAGFLOW_BASE_URL || '').trim()) errors.push('RAGFlow Base URL 不能为空');
    if (!String(view.RAGFLOW_API_KEY || '').trim()) errors.push('RAGFlow API Key 不能为空');
    for (const [key, rule] of Object.entries(NUMBER_FIELDS)) {
      const raw = String(view[key] ?? '').trim();
      if (raw === '') continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        errors.push(`${key} 不是有效数字`);
        continue;
      }
      if (rule.integer && !Number.isInteger(value)) errors.push(`${key} 必须为整数`);
      if (value < rule.min) errors.push(`${key} 不能小于 ${rule.min}`);
      if (rule.max != null && value > rule.max) errors.push(`${key} 不能大于 ${rule.max}`);
    }
    return errors;
  }

  // 只发改动过的 key(值与快照不同)。secret 类若仍是 *** 视为未改动。
  function changedKeys(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(draft)) {
      const snap = snapshot[k];
      if (SECRET_KEYS.has(k) && v === '***') continue; // 未改动的密钥
      if (v !== snap) out[k] = v;
    }
    return out;
  }

  async function handleSave() {
    const changes = changedKeys();
    if (Object.keys(changes).length === 0) {
      notify.info('没有改动');
      return;
    }
    const errors = validateDraft(changes);
    if (errors.length > 0) {
      notify.error(errors[0]);
      return;
    }
    setSaving(true);
    try {
      await api.put<OkResponse>('/api/v1/config', { settings: changes });
      notify.success('配置已保存并热加载');
      load(); // 重新拉快照(secrets 又变回 ***)
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function probeRagflow() {
    setProbing('ragflow');
    try {
      const r = await api.get<RagflowHealthResponse>('/api/v1/health/ragflow');
      setRagflowHealth(r);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '探测失败');
    } finally {
      setProbing(null);
    }
  }

  async function probeLlm() {
    setProbing('llm');
    try {
      const r = await api.get<LlmHealthResponse>('/api/v1/health/llm');
      setLlmHealth(r);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '探测失败');
    } finally {
      setProbing(null);
    }
  }

  const dirty = Object.keys(changedKeys()).length > 0;

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="系统配置"
        description="编辑运行时配置(写入 .env 并热加载)。仅白名单键可修改。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap items-center justify-between gap-[12px]">
        <div className="flex items-center gap-[8px]">
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={probeRagflow} disabled={probing === 'ragflow'}>
            <AppIcon name="refresh" size={13} />
            探测 RAGFlow
          </Button>
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={probeLlm} disabled={probing === 'llm'}>
            <AppIcon name="refresh" size={13} />
            探测 LLM
          </Button>
        </div>
        <div className="flex items-center gap-[8px]">
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={() => load()}>
            重置
          </Button>
          <Button
            onClick={handleSave}
            disabled={!dirty || saving}
            className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030] disabled:opacity-40"
          >
            {saving ? '保存中' : '保存并热加载'}
          </Button>
        </div>
      </div>

      {/* 健康探针结果 */}
      {(ragflowHealth || llmHealth) && (
        <div className="mb-[16px] flex flex-wrap gap-[10px]">
          {ragflowHealth && (
            <div
              className={cn(
                'rounded-[10px] border px-[14px] py-[8px] text-[12px]',
                ragflowHealth.reachable
                  ? 'border-[#bfe6cd] bg-[#e9f7ef] text-[#138a55]'
                  : 'border-[#f3b0b0] bg-[#fce7e7] text-[#d20b0b]',
              )}
            >
              RAGFlow:{ragflowHealth.reachable ? ' ✓' : ' ✗'} {ragflowHealth.message}
              {ragflowHealth.missing_datasets.length > 0 && ` (缺: ${ragflowHealth.missing_datasets.join(', ')})`}
            </div>
          )}
          {llmHealth && (
            <div
              className={cn(
                'rounded-[10px] border px-[14px] py-[8px] text-[12px]',
                llmHealth.reachable
                  ? 'border-[#bfe6cd] bg-[#e9f7ef] text-[#138a55]'
                  : 'border-[#f3b0b0] bg-[#fce7e7] text-[#d20b0b]',
              )}
            >
              LLM({llmHealth.provider}):{llmHealth.reachable ? ' ✓' : ' ✗'} {llmHealth.message}
            </div>
          )}
        </div>
      )}

      {!loaded ? (
        <div className="grid gap-[10px]">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-[64px] rounded-[12px]" />
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          {visibleGroups().map((group) => (
            <div key={group.title} className="flex flex-col gap-[12px]">
              <h3 className="text-[14px] font-semibold text-[#18181a]">{group.title}</h3>
              <div className={cn('grid gap-[12px]', group.textarea ? 'grid-cols-1' : 'grid-cols-1 sm:grid-cols-2')}>
                {group.fields.map(([key, label]) => (
                  <div key={key} className="grid gap-[4px]">
                    <Label className="text-[11px] text-[#858b9c]">
                      {label} <span className="text-[#b3b8c4]">({key})</span>
                    </Label>
                    {key === 'AGENT_LLM_PROVIDER' ? (
                      <Select value={String(draft[key] ?? 'ollama')} onValueChange={(value) => setField(key, value)}>
                        <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="ollama">ollama</SelectItem>
                          <SelectItem value="custom">custom</SelectItem>
                        </SelectContent>
                      </Select>
                    ) : group.textarea ? (
                      <Textarea
                        value={draft[key] ?? ''}
                        onChange={(e) => setField(key, e.target.value)}
                        className="min-h-[100px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
                      />
                    ) : (
                      <Input
                        type={NUMBER_FIELDS[key] ? 'number' : 'text'}
                        min={NUMBER_FIELDS[key]?.min}
                        max={NUMBER_FIELDS[key]?.max}
                        step={NUMBER_FIELDS[key]?.step ?? (NUMBER_FIELDS[key]?.integer ? 1 : undefined)}
                        value={draft[key] ?? ''}
                        onChange={(e) => setField(key, e.target.value)}
                        className="h-[36px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

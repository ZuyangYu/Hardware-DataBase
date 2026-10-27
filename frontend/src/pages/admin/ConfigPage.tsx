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
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { AdminPageFrame } from '@/components/admin/AdminPageShell';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

// 分组(对齐 settings.DEFAULT_VALUES)。值是 [key, 标签, 可选渲染选项]。
export type FieldDef = [string, string, { textarea?: boolean }?];
export type Group = { title: string; fields: FieldDef[]; textarea?: boolean; hint?: string };

export const GROUPS: Group[] = [
  {
    title: '检索',
    fields: [
      ['RAGFLOW_BASE_URL', 'Base URL'],
      ['RAGFLOW_API_KEY', 'API Key'],
      ['RAGFLOW_GOVERNANCE_DATASET_NAME', '治理数据集'],
      ['RAGFLOW_DESIGN_DATASET_NAME', '设计数据集'],
      ['RAGFLOW_TIMEOUT_SECONDS', '超时(秒)'],
      ['RAGFLOW_SIMILARITY_THRESHOLD', '相似度阈值'],
      ['RAGFLOW_VECTOR_WEIGHT', '向量权重'],
      ['MULTI_KB_FUSION_MAX_BYTES', '多库单次证据空间(字节，留空继承)'],
      ['MULTI_KB_TURN_EVIDENCE_MAX_BYTES', '多库整轮证据空间(字节，留空继承)'],
      ['EVIDENCE_SOURCE_AWARE_ENABLED', '来源感知证据准入（附件+知识库）'],
      ['EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES', '证据全局上下文上限(字节)'],
      ['EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES', '系统提示预留(字节)'],
      ['EVIDENCE_ANSWER_RESERVE_BYTES', '最终回答预留(字节)'],
      ['EVIDENCE_ATTACHMENT_MAX_BYTES', '附件证据上限(字节)'],
      ['EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES', '知识库证据上限(字节)'],
      ['EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES', '附件必要预留(字节)'],
      ['EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES', '知识库必要预留(字节)'],
      ['FINAL_TOP_K', 'Final Top K'],
      ['AGENT_MAX_RETRIEVAL_ROUNDS', '最大检索轮数'],
    ],
  },
  {
    title: '账号与会话',
    fields: [
      ['AUTH_DB_PATH', 'Auth DB 路径'],
      ['AUTH_DEFAULT_ADMIN_USERNAME', '默认管理员'],
      ['AUTH_DEFAULT_ADMIN_PASSWORD', '默认管理员密码'],
      ['AUTH_SESSION_TTL_HOURS', '会话 TTL(小时)'],
    ],
  },
  {
    title: '模型与提示词',
    fields: [
      ['AGENT_LLM_PROVIDER', 'Provider (ollama/custom)'],
      ['AGENT_OLLAMA_BASE_URL', 'Ollama Base URL'],
      ['AGENT_OLLAMA_MODEL', 'Ollama Model'],
      ['AGENT_CUSTOM_API_KEY', 'Custom API Key'],
      ['AGENT_CUSTOM_BASE_URL', 'Custom Base URL'],
      ['AGENT_CUSTOM_MODEL', 'Custom Model'],
      ['AGENT_CUSTOM_MAX_TOKENS', 'Custom Max Tokens'],
      ['AGENT_MODEL_MAX_INPUT_TOKENS', '模型输入上下文上限'],
      ['AGENT_TEMPERATURE', 'Temperature'],
      ['AGENT_TIMEOUT_SECONDS', '超时(秒)'],
      ['AGENT_RATE_LIMIT_MAX_RETRIES', '限流重试次数'],
      ['AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS', '限流初始延迟(秒)'],
      ['AGENT_RATE_LIMIT_MAX_DELAY_SECONDS', '限流最大延迟(秒)'],
      ['LLM_MAX_CONCURRENCY', 'LLM 最大并发'],
      ['LLM_BATCH_MAX_CONCURRENCY', '批量任务并发上限'],
      ['LLM_QUEUE_TIMEOUT_SECONDS', '排队超时(秒)'],
      ['LLM_QUEUE_WARN_SECONDS', '排队告警阈值(秒)'],
      ['LLM_STREAM_IDLE_TIMEOUT_SECONDS', '流空闲超时(秒, 0=关闭)'],
      ['WORKER_TURN_CONCURRENCY', 'Worker 轮次并发'],
      ['SYSTEM_PROMPT', 'System Prompt', { textarea: true }],
      ['NO_CONTEXT_PROMPT', 'No-Context Prompt', { textarea: true }],
    ],
  },
  {
    title: '长期记忆',
    hint: '记忆默认开启，模型留空时复用上面的 Agent 模型；语义检索（Embedding）留空则关闭，走目录/关键词检索。',
    fields: [
      ['MEMORY_ENABLED', '启用长期记忆'],
      ['MEMORY_MODEL_PROVIDER', '记忆模型 Provider（留空=复用 Agent）'],
      ['MEMORY_MODEL', '记忆模型'],
      ['MEMORY_MODEL_BASE_URL', '记忆模型 Base URL'],
      ['MEMORY_MODEL_API_KEY', '记忆模型 API Key'],
      ['MEMORY_EMBEDDING_PROVIDER', 'Embedding Provider（留空=关闭语义检索）'],
      ['MEMORY_EMBEDDING_MODEL', 'Embedding 模型'],
      ['MEMORY_EMBEDDING_BASE_URL', 'Embedding Base URL'],
      ['MEMORY_EMBEDDING_API_KEY', 'Embedding API Key'],
      ['MEMORY_EMBEDDING_DIMS', 'Embedding 维度'],
    ],
  },
  {
    title: '附件解析与理解',
    hint: '附件默认开启：词法检索 + OCR（本地 tesseract，不外发）。先选择检索或视觉开关，再配置对应模型；远程推理仍需单独允许。',
    fields: [
      ['CHAT_ATTACHMENTS_ENABLED', '启用会话附件'],
      ['CHAT_ATTACHMENT_MAX_BYTES', '单文件大小上限(字节)'],
      ['CHAT_ATTACHMENT_MAX_FILES_PER_SESSION', '单会话文件数上限'],
      ['CHAT_ATTACHMENT_OCR_ENABLED', 'OCR 识别'],
      ['CHAT_ATTACHMENT_RETRIEVAL_MODE', '检索模式'],
      ['CHAT_ATTACHMENT_EMBEDDING_PROVIDER', 'Embedding Provider'],
      ['CHAT_ATTACHMENT_EMBEDDING_BASE_URL', 'Embedding Base URL'],
      ['CHAT_ATTACHMENT_EMBEDDING_MODEL', 'Embedding 模型'],
      ['CHAT_ATTACHMENT_EMBEDDING_API_KEY', 'Embedding API Key'],
      ['CHAT_ATTACHMENT_VISUAL_ENABLED', '图像理解（视觉模型）'],
      ['CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED', '允许远程推理（图像/OCR 外发）'],
      ['CHAT_ATTACHMENT_VISUAL_PROVIDER', '视觉模型 Provider'],
      ['CHAT_ATTACHMENT_VISUAL_BASE_URL', '视觉模型 Base URL'],
      ['CHAT_ATTACHMENT_VISUAL_MODEL', '视觉模型'],
      ['ARK_API_KEY', '视觉模型 API Key'],
      ['CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN', '视觉模型每轮最大页数'],
      ['CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES', '单张图片大小上限(字节)'],
      ['CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS', '视觉模型超时(秒)'],
    ],
  },
  {
    title: '扩展（MCP，受管控）',
    hint: '远程 MCP 默认开启；没有启用的服务时不发起任何连接，本开关仅作应急停用。平台前置自动开启，无需手动配置；凭据密钥 MCP_CREDENTIALS_KEY 只能写在 .env，配置接口不会返回或写入。',
    fields: [
      ['MCP_ENABLED', '启用外部 MCP 扩展'],
      ['MCP_WRITE_ENABLED', '启用写入通道（候选产物）'],
      ['MCP_ALLOW_INSECURE_HTTP', '允许 http://（非 https）'],
      ['MCP_ALLOW_PRIVATE_HOSTS', '允许内网地址'],
      ['MCP_CONNECT_TIMEOUT_SECONDS', '连接超时(秒)'],
      ['MCP_CALL_TIMEOUT_SECONDS', '调用超时(秒)'],
      ['MCP_SSE_READ_TIMEOUT_SECONDS', 'SSE 读取超时(秒)'],
      ['MCP_MAX_RESULT_CHARS', '结果字符上限'],
      ['MCP_MAX_RESULT_BYTES', '结果字节上限'],
      ['MCP_MAX_ARGUMENT_CHARS', '参数上限(字符)'],
      ['MCP_MAX_TOOLS_PER_SERVER', '单服务工具上限'],
      ['MCP_MAX_SERVERS_PER_RUN', '单回合服务上限'],
      ['MCP_CALLS_PER_TURN', '单回合调用上限'],
      ['MCP_FAILURE_COOLDOWN_SECONDS', '失败冷却(秒)'],
      ['MCP_WRITE_CONFIRM_TTL_SECONDS', '写入提案有效期(秒)'],
      ['MCP_MAX_ARTIFACT_BYTES', '候选产物大小上限(字节)'],
      ['MCP_ARTIFACT_ALLOWED_EXTENSIONS', '候选产物扩展名白名单'],
      ['MCP_WRITE_MAX_ATTEMPTS', '写入最大尝试次数'],
      ['RESOURCE_MCP_MAX_CONCURRENT', 'MCP 最大并发'],
      ['RESOURCE_MCP_RESERVED_INTERACTIVE', 'MCP 交互保留并发'],
    ],
  },
];

const SECRET_KEYS = new Set([
  'RAGFLOW_API_KEY',
  'AGENT_CUSTOM_API_KEY',
  'AUTH_DEFAULT_ADMIN_PASSWORD',
  // 与后端 _SECRET_KEYS 对齐：不在这个名单里的 "***" 会被当成真实值写回 .env。
  'MEMORY_MODEL_API_KEY',
  'MEMORY_EMBEDDING_API_KEY',
  'CHAT_ATTACHMENT_EMBEDDING_API_KEY',
  'ARK_API_KEY',
]);

/** 受控布尔开关:渲染 true/false 下拉,避免手输字符串。 */
export const BOOLEAN_KEYS = new Set([
  'MCP_ENABLED',
  'MCP_WRITE_ENABLED',
  'MCP_ALLOW_INSECURE_HTTP',
  'MCP_ALLOW_PRIVATE_HOSTS',
  'MEMORY_ENABLED',
  'CHAT_ATTACHMENTS_ENABLED',
  'CHAT_ATTACHMENT_OCR_ENABLED',
  'CHAT_ATTACHMENT_DENSE_ENABLED',
  'CHAT_ATTACHMENT_VISUAL_ENABLED',
  'CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED',
  'EVIDENCE_SOURCE_AWARE_ENABLED',
]);

/** 调参项：默认隐藏，避免把"永远用默认值"的旋钮摊在主视图。 */
export const ADVANCED_KEYS = new Set([
  'RAGFLOW_TIMEOUT_SECONDS',
  'RAGFLOW_SIMILARITY_THRESHOLD',
  'RAGFLOW_VECTOR_WEIGHT',
  'MULTI_KB_FUSION_MAX_BYTES',
  'MULTI_KB_TURN_EVIDENCE_MAX_BYTES',
  'EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES',
  'EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES',
  'EVIDENCE_ANSWER_RESERVE_BYTES',
  'EVIDENCE_ATTACHMENT_MAX_BYTES',
  'EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES',
  'EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES',
  'EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES',
  'AUTH_DB_PATH',
  'AGENT_CUSTOM_MAX_TOKENS',
  'AGENT_MODEL_MAX_INPUT_TOKENS',
  'AGENT_TEMPERATURE',
  'AGENT_TIMEOUT_SECONDS',
  'AGENT_RATE_LIMIT_MAX_RETRIES',
  'AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS',
  'AGENT_RATE_LIMIT_MAX_DELAY_SECONDS',
  'LLM_MAX_CONCURRENCY',
  'LLM_BATCH_MAX_CONCURRENCY',
  'LLM_QUEUE_TIMEOUT_SECONDS',
  'LLM_QUEUE_WARN_SECONDS',
  'LLM_STREAM_IDLE_TIMEOUT_SECONDS',
  'WORKER_TURN_CONCURRENCY',
  'AGENT_MAX_RETRIEVAL_ROUNDS',
  'MCP_CONNECT_TIMEOUT_SECONDS',
  'MCP_CALL_TIMEOUT_SECONDS',
  'MCP_SSE_READ_TIMEOUT_SECONDS',
  'MCP_MAX_RESULT_CHARS',
  'MCP_MAX_RESULT_BYTES',
  'MCP_MAX_ARGUMENT_CHARS',
  'MCP_MAX_TOOLS_PER_SERVER',
  'MCP_MAX_SERVERS_PER_RUN',
  'MCP_CALLS_PER_TURN',
  'MCP_FAILURE_COOLDOWN_SECONDS',
  'MCP_WRITE_CONFIRM_TTL_SECONDS',
  'MCP_MAX_ARTIFACT_BYTES',
  'MCP_ARTIFACT_ALLOWED_EXTENSIONS',
  'MCP_WRITE_MAX_ATTEMPTS',
  'RESOURCE_MCP_MAX_CONCURRENT',
  'RESOURCE_MCP_RESERVED_INTERACTIVE',
  'CHAT_ATTACHMENT_MAX_FILES_PER_SESSION',
  'CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN',
  'CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES',
  'CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS',
]);

/** 保存后需重启进程才生效的键（与后端 RESTART_REQUIRED_SETTINGS 子集对齐）。 */
export const RESTART_REQUIRED_KEYS = new Set(['AUTH_DB_PATH']);

const TRUTHY = new Set(['1', 'true', 'yes', 'on']);

export function booleanDraftValue(raw: unknown): 'true' | 'false' {
  return TRUTHY.has(String(raw ?? '').trim().toLowerCase()) ? 'true' : 'false';
}

export const NUMBER_FIELDS: Record<string, { min: number; max?: number; step?: number; integer?: boolean }> = {
  RAGFLOW_TIMEOUT_SECONDS: { min: 10, max: 600, integer: true },
  RAGFLOW_SIMILARITY_THRESHOLD: { min: 0, max: 1, step: 0.05 },
  RAGFLOW_VECTOR_WEIGHT: { min: 0, max: 1, step: 0.05 },
  MULTI_KB_FUSION_MAX_BYTES: { min: 1, max: 1000000, step: 1000, integer: true },
  MULTI_KB_TURN_EVIDENCE_MAX_BYTES: { min: 1, max: 1000000, step: 1000, integer: true },
  EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES: { min: 1, max: 1000000, step: 1000, integer: true },
  EVIDENCE_SYSTEM_PROMPT_RESERVE_BYTES: { min: 1, max: 500000, step: 1000, integer: true },
  EVIDENCE_ANSWER_RESERVE_BYTES: { min: 1, max: 500000, step: 1000, integer: true },
  EVIDENCE_ATTACHMENT_MAX_BYTES: { min: 1, max: 1000000, step: 1000, integer: true },
  EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES: { min: 1, max: 1000000, step: 1000, integer: true },
  EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES: { min: 1, max: 500000, step: 1000, integer: true },
  EVIDENCE_KNOWLEDGE_BASE_REQUIRED_RESERVE_BYTES: { min: 1, max: 500000, step: 1000, integer: true },
  AUTH_SESSION_TTL_HOURS: { min: 1, max: 720, integer: true },
  AGENT_CUSTOM_MAX_TOKENS: { min: 256, max: 65536, step: 256, integer: true },
  AGENT_MODEL_MAX_INPUT_TOKENS: { min: 0, max: 1000000, step: 1024, integer: true },
  AGENT_TEMPERATURE: { min: 0, max: 2, step: 0.1 },
  AGENT_TIMEOUT_SECONDS: { min: 10, max: 600, step: 10, integer: true },
  AGENT_RATE_LIMIT_MAX_RETRIES: { min: 0, max: 20, integer: true },
  AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS: { min: 0, max: 120 },
  AGENT_RATE_LIMIT_MAX_DELAY_SECONDS: { min: 0, max: 600 },
  LLM_MAX_CONCURRENCY: { min: 1, max: 64, integer: true },
  LLM_STREAM_IDLE_TIMEOUT_SECONDS: { min: 0, max: 3600, integer: true },
  WORKER_TURN_CONCURRENCY: { min: 1, max: 16, integer: true },
  LLM_BATCH_MAX_CONCURRENCY: { min: 0, max: 64, integer: true },
  LLM_QUEUE_TIMEOUT_SECONDS: { min: 0, max: 3600, integer: true },
  LLM_QUEUE_WARN_SECONDS: { min: 0, max: 600, integer: true },
  FINAL_TOP_K: { min: 1, max: 50, integer: true },
  AGENT_MAX_RETRIEVAL_ROUNDS: { min: 1, max: 20, integer: true },
  DOCUMENT_AUTHORING_JOB_LEASE_SECONDS: { min: 15, max: 3600, integer: true },
  DOCUMENT_AUTHORING_JOB_BATCH_SIZE: { min: 1, max: 100, integer: true },
  DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS: { min: 0.1, max: 600, step: 0.5 },
  OBS_TRACE_SAMPLE_RATIO: { min: 0, max: 1, step: 0.05 },
  OBS_CONTENT_MAX_CHARS: { min: 1000, max: 200000, integer: true },
  OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS: { min: 1, max: 300 },
  OBS_WORKER_STALE_SECONDS: { min: 5, max: 3600, integer: true },
  OBS_DEPENDENCY_TIMEOUT_SECONDS: { min: 0.1, max: 60 },
  MCP_CONNECT_TIMEOUT_SECONDS: { min: 1, max: 300, integer: true },
  MCP_CALL_TIMEOUT_SECONDS: { min: 1, max: 3600, integer: true },
  MCP_SSE_READ_TIMEOUT_SECONDS: { min: 1, max: 3600, integer: true },
  MCP_MAX_RESULT_CHARS: { min: 1000, max: 500000, integer: true },
  MCP_MAX_RESULT_BYTES: { min: 1024, max: 10485760, integer: true },
  MCP_MAX_ARGUMENT_CHARS: { min: 100, max: 1000000, integer: true },
  MCP_MAX_TOOLS_PER_SERVER: { min: 1, max: 500, integer: true },
  MCP_MAX_SERVERS_PER_RUN: { min: 1, max: 50, integer: true },
  MCP_CALLS_PER_TURN: { min: 1, max: 100, integer: true },
  MCP_FAILURE_COOLDOWN_SECONDS: { min: 0, max: 86400, integer: true },
  MCP_WRITE_CONFIRM_TTL_SECONDS: { min: 60, max: 86400, integer: true },
  MCP_MAX_ARTIFACT_BYTES: { min: 1024, max: 1073741824, integer: true },
  MCP_WRITE_MAX_ATTEMPTS: { min: 1, max: 10, integer: true },
  RESOURCE_MCP_MAX_CONCURRENT: { min: 1, max: 64, integer: true },
  RESOURCE_MCP_RESERVED_INTERACTIVE: { min: 0, max: 64, integer: true },
  MEMORY_EMBEDDING_DIMS: { min: 0, max: 8192, integer: true },
  CHAT_ATTACHMENT_MAX_BYTES: { min: 1, max: 1073741824, integer: true },
  CHAT_ATTACHMENT_MAX_FILES_PER_SESSION: { min: 1, max: 100, integer: true },
  CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN: { min: 1, max: 20, integer: true },
  CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES: { min: 1024, max: 67108864, integer: true },
  CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS: { min: 1, max: 600, integer: true },
};

/** 分类内是否有未保存改动；掩码后的密钥视为未改动。 */
export function groupDirty(
  group: Group,
  draft: Record<string, string>,
  snapshot: Record<string, string>,
): boolean {
  return group.fields.some(([key]) => {
    const value = draft[key] ?? '';
    if (SECRET_KEYS.has(key) && value === '***') return false;
    return value !== (snapshot[key] ?? '');
  });
}

type ConfigGroupsPanelProps = {
  groups: Group[];
  activeGroup: string;
  draft: Record<string, string>;
  snapshot: Record<string, string>;
  showAdvanced: boolean;
  onSelectGroup: (title: string) => void;
  onFieldChange: (key: string, value: string) => void;
};

/** 分类导航 + 单类面板；所有字段仍属于同一草稿，保存语义不变。 */
export function ConfigGroupsPanel({
  groups,
  activeGroup,
  draft,
  snapshot,
  showAdvanced,
  onSelectGroup,
  onFieldChange,
}: ConfigGroupsPanelProps) {
  const enabled = (key: string) => booleanDraftValue(draft[key]) === 'true';
  const isVisibleField = (key: string) => {
    if (!showAdvanced && ADVANCED_KEYS.has(key)) return false;
    if (key.startsWith('MEMORY_') && key !== 'MEMORY_ENABLED' && !enabled('MEMORY_ENABLED')) return false;
    const denseFields = new Set([
      'CHAT_ATTACHMENT_EMBEDDING_PROVIDER',
      'CHAT_ATTACHMENT_EMBEDDING_BASE_URL',
      'CHAT_ATTACHMENT_EMBEDDING_MODEL',
      'CHAT_ATTACHMENT_EMBEDDING_API_KEY',
    ]);
    if (denseFields.has(key) && String(draft.CHAT_ATTACHMENT_RETRIEVAL_MODE ?? 'lexical').trim().toLowerCase() !== 'hybrid') {
      return false;
    }
    const visualFields = new Set([
      'CHAT_ATTACHMENT_VISUAL_PROVIDER',
      'CHAT_ATTACHMENT_VISUAL_BASE_URL',
      'CHAT_ATTACHMENT_VISUAL_MODEL',
      'ARK_API_KEY',
      'CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN',
      'CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES',
      'CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS',
    ]);
    if (visualFields.has(key) && !enabled('CHAT_ATTACHMENT_VISUAL_ENABLED')) return false;
    return true;
  };
  return (
    <div className="flex flex-col">
      <nav
        aria-label="配置分类"
        className="flex gap-[2px] overflow-x-auto border-b border-[#eceef3]"
      >
        {groups.map((group, index) => {
          const selected = group.title === activeGroup;
          return (
            <button
              key={group.title}
              id={`config-group-${index}-tab`}
              type="button"
              aria-controls={`config-group-${index}-panel`}
              aria-pressed={selected}
              onClick={() => onSelectGroup(group.title)}
              className={cn(
                'relative shrink-0 whitespace-nowrap border-b-2 px-[12px] py-[9px] text-[13px] font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#1d4ed8]',
                selected
                  ? 'border-[#1d4ed8] text-[#1d4ed8]'
                  : 'border-transparent text-[#858b9c] hover:text-[#464c5e]',
              )}
            >
              {group.title}
              {groupDirty(group, draft, snapshot) && (
                <span
                  aria-label="有未保存修改"
                  title="有未保存修改"
                  className="absolute right-[3px] top-[5px] size-[6px] rounded-full bg-[#e0871a]"
                />
              )}
            </button>
          );
        })}
      </nav>
      {groups.map((group, index) => {
        if (group.title !== activeGroup) return null;
        return (
          <div
            key={group.title}
            id={`config-group-${index}-panel`}
            role="region"
            aria-labelledby={`config-group-${index}-tab`}
            className="flex min-w-0 flex-col gap-[12px] pt-[16px]"
          >
            {group.hint && <p className="text-[12px] text-[#858b9c]">{group.hint}</p>}
            <div className="grid grid-cols-1 gap-[12px] sm:grid-cols-2 xl:grid-cols-3">
              {group.fields
                .filter(([key]) => isVisibleField(key))
                .map(([key, label, fieldOptions]) => (
                <div
                  key={key}
                  className={cn('grid gap-[4px]', (group.textarea || fieldOptions?.textarea) && 'sm:col-span-2 xl:col-span-3')}
                >
                  <Label className="flex items-center gap-[6px] text-[11px] text-[#858b9c]">
                    <span>
                      {label} <span className="text-[#b3b8c4]">({key})</span>
                    </span>
                    {RESTART_REQUIRED_KEYS.has(key) && (
                      <span className="rounded-[4px] bg-[#fdf3e3] px-[5px] py-[1px] text-[10px] text-[#a9701b]">
                        重启生效
                      </span>
                    )}
                  </Label>
                  {BOOLEAN_KEYS.has(key) ? (
                    <Select
                      value={booleanDraftValue(draft[key])}
                      onValueChange={(value) => onFieldChange(key, value)}
                    >
                      <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="true">true（启用）</SelectItem>
                        <SelectItem value="false">false（关闭）</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : key === 'AGENT_LLM_PROVIDER' ? (
                    <Select value={String(draft[key] ?? 'ollama')} onValueChange={(value) => onFieldChange(key, value)}>
                      <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="ollama">ollama</SelectItem>
                        <SelectItem value="custom">custom</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : key === 'CHAT_ATTACHMENT_RETRIEVAL_MODE' ? (
                    <Select
                      value={String(draft[key] ?? 'lexical').toLowerCase() === 'hybrid' ? 'hybrid' : 'lexical'}
                      onValueChange={(value) => {
                        onFieldChange(key, value);
                        // Keep the legacy backend flag synchronized while the
                        // visible mode remains the single user-facing switch.
                        onFieldChange('CHAT_ATTACHMENT_DENSE_ENABLED', value === 'hybrid' ? 'true' : 'false');
                      }}
                    >
                      <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="lexical">lexical（关键词）</SelectItem>
                        <SelectItem value="hybrid">hybrid（关键词+语义）</SelectItem>
                      </SelectContent>
                    </Select>
                  ) : group.textarea || fieldOptions?.textarea ? (
                    <Textarea
                      value={draft[key] ?? ''}
                      onChange={(e) => onFieldChange(key, e.target.value)}
                      className="min-h-[100px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
                    />
                  ) : (
                    <Input
                      type={NUMBER_FIELDS[key] ? 'number' : SECRET_KEYS.has(key) ? 'password' : 'text'}
                      min={NUMBER_FIELDS[key]?.min}
                      max={NUMBER_FIELDS[key]?.max}
                      step={NUMBER_FIELDS[key]?.step ?? (NUMBER_FIELDS[key]?.integer ? 1 : undefined)}
                      value={draft[key] ?? ''}
                      onChange={(e) => onFieldChange(key, e.target.value)}
                      className="h-[36px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
                    />
                  )}
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function ConfigPage({ auth, onLogout }: Props) {
  const [snapshot, setSnapshot] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [activeGroup, setActiveGroup] = useState<string>(GROUPS[0]?.title ?? '');
  const [showAdvanced, setShowAdvanced] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem('hdb.config.showAdvanced') === 'true';
    } catch {
      return false;
    }
  });
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ragflowHealth, setRagflowHealth] = useState<RagflowHealthResponse | null>(null);
  const [llmHealth, setLlmHealth] = useState<LlmHealthResponse | null>(null);
  const [probing, setProbing] = useState<string | null>(null);
  const [confirmResetOpen, setConfirmResetOpen] = useState(false);

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

  function toggleAdvanced() {
    setShowAdvanced((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem('hdb.config.showAdvanced', String(next));
      } catch {
        // 隐私模式下 localStorage 不可用时忽略，仅影响记忆偏好。
      }
      return next;
    });
  }

  function visibleGroups(): Group[] {
    const provider = String(draft.AGENT_LLM_PROVIDER || 'ollama');
    return GROUPS.map((group) => {
      // 按字段定位模型分组，避免与展示标题耦合。
      if (!group.fields.some(([key]) => key === 'AGENT_LLM_PROVIDER')) return group;
      return {
        ...group,
        fields: group.fields.filter(([key]) => {
          if (key === 'AGENT_LLM_PROVIDER') return true;
          if (key.startsWith('AGENT_OLLAMA')) return provider === 'ollama';
          if (key.startsWith('AGENT_CUSTOM')) return provider !== 'ollama';
          return true;
        }),
      };
    });
  }

  function validateDraft(changes: Record<string, string>): { key: string; message: string }[] {
    const view = { ...snapshot, ...draft, ...changes };
    const errors: { key: string; message: string }[] = [];
    const provider = String(view.AGENT_LLM_PROVIDER || 'ollama');
    if (!['ollama', 'custom'].includes(provider)) {
      errors.push({ key: 'AGENT_LLM_PROVIDER', message: 'AGENT_LLM_PROVIDER 必须为 ollama 或 custom' });
    }
    if (provider === 'ollama') {
      if (!String(view.AGENT_OLLAMA_BASE_URL || '').trim()) {
        errors.push({ key: 'AGENT_OLLAMA_BASE_URL', message: 'Agent Ollama Base URL 不能为空' });
      }
      if (!String(view.AGENT_OLLAMA_MODEL || '').trim()) {
        errors.push({ key: 'AGENT_OLLAMA_MODEL', message: 'Agent Ollama 模型不能为空' });
      }
    } else {
      if (!String(view.AGENT_CUSTOM_API_KEY || '').trim()) {
        errors.push({ key: 'AGENT_CUSTOM_API_KEY', message: 'Agent API Key 不能为空' });
      }
      if (!String(view.AGENT_CUSTOM_BASE_URL || '').trim()) {
        errors.push({ key: 'AGENT_CUSTOM_BASE_URL', message: 'Agent Base URL 不能为空' });
      }
      if (!String(view.AGENT_CUSTOM_MODEL || '').trim()) {
        errors.push({ key: 'AGENT_CUSTOM_MODEL', message: 'Agent LLM 模型不能为空' });
      }
    }
    if (!String(view.RAGFLOW_BASE_URL || '').trim()) {
      errors.push({ key: 'RAGFLOW_BASE_URL', message: 'RAGFlow Base URL 不能为空' });
    }
    if (!String(view.RAGFLOW_API_KEY || '').trim()) {
      errors.push({ key: 'RAGFLOW_API_KEY', message: 'RAGFlow API Key 不能为空' });
    }
    for (const [key, rule] of Object.entries(NUMBER_FIELDS)) {
      const raw = String(view[key] ?? '').trim();
      if (raw === '') continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        errors.push({ key, message: `${key} 不是有效数字` });
        continue;
      }
      if (rule.integer && !Number.isInteger(value)) errors.push({ key, message: `${key} 必须为整数` });
      if (value < rule.min) errors.push({ key, message: `${key} 不能小于 ${rule.min}` });
      if (rule.max != null && value > rule.max) errors.push({ key, message: `${key} 不能大于 ${rule.max}` });
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
    // The backend owns the hidden MCP platform prerequisites. Keep them out of
    // the request entirely; the ordinary config API deliberately rejects
    // those internal keys.
    const changes = changedKeys();
    if (Object.keys(changes).length === 0) {
      notify.info('没有改动');
      return;
    }
    const errors = validateDraft(changes);
    if (errors.length > 0) {
      const first = errors[0];
      // 校验错误可能落在其他分类:自动切换到对应分类，避免看不到出错的字段。
      const target = visibleGroups().find((group) => group.fields.some(([key]) => key === first.key));
      if (target) setActiveGroup(target.title);
      notify.error(first.message);
      return;
    }
    setSaving(true);
    try {
      await api.put<OkResponse>('/api/v1/config', { settings: changes });
      // 只把本次提交的键同步进快照与草稿(密钥回显为 *** 与服务端一致)，
      // 其余分组未保存的草稿保持不动，不再整页重拉覆盖。
      const applied: Record<string, string> = { ...changes };
      for (const key of Object.keys(applied)) {
        if (SECRET_KEYS.has(key)) applied[key] = '***';
      }
      setSnapshot((prev) => ({ ...prev, ...applied }));
      setDraft((prev) => ({ ...prev, ...applied }));
      notify.success('配置已保存并热加载');
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
  const groups = visibleGroups();

  return (
    <AdminPageFrame>
      <AppHeader
        title="系统配置"
        description="编辑运行时配置(写入 .env 并热加载)；标有「重启生效」的项需重启进程。仅白名单键可修改，调参项可点「显示高级设置」。"
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
          <Button
            variant="outline"
            aria-pressed={showAdvanced}
            className={cn(
              OUTLINE_ACTION_BUTTON_CLASS,
              'h-[34px]',
              showAdvanced && 'border-[#1d4ed8] text-[#1d4ed8] hover:text-[#1d4ed8]',
            )}
            onClick={toggleAdvanced}
          >
            {showAdvanced ? '隐藏高级设置' : '显示高级设置'}
          </Button>
        </div>
        <div className="flex items-center gap-[8px]">
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={() => setConfirmResetOpen(true)}>
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
        <div className="rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          <ConfigGroupsPanel
            groups={groups}
            activeGroup={activeGroup}
            draft={draft}
            snapshot={snapshot}
            showAdvanced={showAdvanced}
            onSelectGroup={setActiveGroup}
            onFieldChange={setField}
          />
        </div>
      )}
      <ConfirmDialog
        open={confirmResetOpen}
        onOpenChange={setConfirmResetOpen}
        title="重置配置编辑"
        description="将放弃所有分组中未保存的修改，恢复为服务器当前配置。此操作无法撤销。"
        confirmText="重置"
        onConfirm={() => {
          setConfirmResetOpen(false);
          load();
        }}
      />
    </AdminPageFrame>
  );
}

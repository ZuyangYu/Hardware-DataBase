/**
 * 聊天头部(对齐 ChatHeader 结构:会话标题 + 右侧用户菜单下拉退出)。
 * 删掉重命名按钮/员工 handoff;保留会话标题 + KB 名 + 记忆提炼信息 + 退出。
 * 另含本会话授权台账入口(查看/撤销个人记忆授权)与长期记忆页链接。
 */
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Popover } from 'radix-ui';

import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';
import type { KbView, KnowledgePurpose, MemoryConsentView, SessionMemorySummary } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  CHAT_HEADER_CLASS,
  CHAT_HEADER_TITLE_NAME_CLASS,
  CHAT_HEADER_TITLE_STACK_CLASS,
  CHAT_HEADER_TITLE_META_CLASS,
} from '../chatPageStyles';

/** 撤销原因固定文案;request_id 由 useKbChat 内的 requestUuid 生成。 */
const CONSENT_REVOKE_REASON = '用户在会话页撤销';

type Props = {
  title: string;
  kbName: string;
  onExtractMemory?: () => void;
  extractDisabled?: boolean;
  memorySummary?: SessionMemorySummary | null;
  /** 切换自动提炼开关;返回是否成功(供调用方结束 pending)。 */
  onToggleAutoExtract?: (enabled: boolean) => Promise<boolean>;
  /** 本会话授权台账;null 表示无会话或未加载。 */
  sessionConsents?: MemoryConsentView[] | null;
  /** 撤销一条授权;返回是否成功。 */
  onRevokeConsent?: (consentEventId: string, reason: string) => Promise<boolean>;
  /** Wiki 辅助开关(仅挂载 KB 时显示): 检索知识 Wiki 蒸馏页。 */
  wikiAssist?: boolean;
  onWikiAssistChange?: (enabled: boolean) => void;
  /** Wiki 是否有内容; false 时禁用开关并提示。null/undefined = 未知(不禁用)。 */
  wikiAvailable?: boolean | null;
  /** 页面级附加操作(如嵌入的导出任务中心),渲染在用户菜单之前。 */
  actions?: ReactNode;
  /** Optional multi-KB selector; the selected set is fixed when a session starts. */
  availableKbs?: KbView[];
  knowledgePurpose?: KnowledgePurpose;
  selectedKnowledgeBaseIds?: number[];
  onKnowledgeScopeChange?: (purpose: KnowledgePurpose, kbIds: number[]) => void;
  onPrimaryKnowledgeBaseChange?: (kbName: string) => void;
  knowledgeScopeDisabled?: boolean;
};

function HeaderToggle({
  enabled,
  disabled,
  onToggle,
  ariaLabel,
  title,
}: {
  enabled: boolean;
  disabled: boolean;
  onToggle: (enabled: boolean) => void;
  ariaLabel: string;
  title: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onToggle(!enabled)}
      title={title}
      className={cn(
        'relative inline-flex h-[16px] w-[28px] shrink-0 items-center rounded-full border border-[#e3e7f1] transition-colors',
        'disabled:cursor-not-allowed disabled:opacity-45',
        enabled ? 'bg-[#18181a]' : 'bg-[#eef0f4]',
      )}
    >
      <span
        className={cn(
          'inline-block size-[11px] rounded-full bg-white shadow-[0_1px_2px_rgba(24,24,26,0.25)] transition-transform',
          enabled ? 'translate-x-[14px]' : 'translate-x-[2px]',
        )}
      />
    </button>
  );
}

export default function ChatHeader({
  title,
  kbName,
  onExtractMemory,
  extractDisabled = false,
  memorySummary = null,
  onToggleAutoExtract,
  sessionConsents = null,
  onRevokeConsent,
  wikiAssist = false,
  onWikiAssistChange,
  wikiAvailable,
  actions = null,
  availableKbs = [],
  knowledgePurpose = 'answer',
  selectedKnowledgeBaseIds = [],
  onKnowledgeScopeChange,
  onPrimaryKnowledgeBaseChange,
  knowledgeScopeDisabled = false,
}: Props) {
  const [toggling, setToggling] = useState(false);
  const [consentsOpen, setConsentsOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<MemoryConsentView | null>(null);
  const [revoking, setRevoking] = useState(false);
  const extracted = memorySummary?.extracted_memories ?? 0;
  const activeCount = sessionConsents?.filter((consent) => consent.status === 'active').length ?? 0;
  const selectableKbs = availableKbs.filter((kb) => kb.permission && kb.kb_id != null);
  const selectedKbs = selectableKbs.filter((kb) => selectedKnowledgeBaseIds.includes(Number(kb.kb_id)));
  const remainingKbs = selectableKbs.filter((kb) => !selectedKnowledgeBaseIds.includes(Number(kb.kb_id)));
  // 展示按授权时间倒序,最新在前
  const sortedConsents = [...(sessionConsents ?? [])].sort((a, b) =>
    a.granted_at < b.granted_at ? 1 : -1,
  );

  async function handleToggle(nextEnabled: boolean) {
    if (!onToggleAutoExtract || toggling) return;
    setToggling(true);
    try {
      await onToggleAutoExtract(nextEnabled);
    } finally {
      setToggling(false);
    }
  }

  async function handleRevokeConfirm() {
    if (!revokeTarget || !onRevokeConsent) return;
    setRevoking(true);
    try {
      const ok = await onRevokeConsent(revokeTarget.consent_event_id, CONSENT_REVOKE_REASON);
      setRevokeTarget(null);
      if (ok) setConsentsOpen(false);
    } finally {
      setRevoking(false);
    }
  }

  return (
    <div className={cn(CHAT_HEADER_CLASS, 'h-auto min-h-[56px] flex-wrap py-[8px]')}>
      <div className={CHAT_HEADER_TITLE_STACK_CLASS}>
        <span className={CHAT_HEADER_TITLE_NAME_CLASS}>{title || '新对话'}</span>
        <span className={CHAT_HEADER_TITLE_META_CLASS}>{kbName}</span>
      </div>
      <div className="flex max-w-full flex-wrap items-center gap-[8px] [&>*]:shrink-0">
        {onKnowledgeScopeChange && selectableKbs.length > 1 && selectableKbs.some((kb) => kb.name === kbName) && (
          <Popover.Root>
            <Popover.Trigger asChild>
              <button type="button" className="shrink-0 whitespace-nowrap rounded-[8px] px-[8px] py-[4px] text-[11px] text-[#68728a] hover:bg-[#f6f7fa]" title={`主库：${kbName}；点击添加或管理参考知识库`}>
                挂载知识库 · {selectedKbs.length}
              </button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content aria-label="知识库范围" align="end" sideOffset={6} collisionPadding={12} className="z-50 w-[320px] max-w-[calc(100vw-24px)] rounded-[10px] border border-[#e5e8f0] bg-white p-[10px] shadow-lg">
                <label className="mb-[8px] flex items-center justify-between gap-[8px] text-[11px] text-[#464c5e]">
                  用途
                  <select
                    aria-label="知识库用途"
                    value={knowledgePurpose}
                    disabled={knowledgeScopeDisabled}
                    onChange={(event) => onKnowledgeScopeChange(
                      event.target.value as KnowledgePurpose,
                      selectedKnowledgeBaseIds,
                    )}
                    className="rounded border border-[#e5e8f0] px-[5px] py-[3px] text-[11px]"
                  >
                    <option value="answer">问答</option>
                    <option value="compare">对比</option>
                    <option value="compose">编写文档</option>
                  </select>
                </label>
                <ul aria-label="已选知识库" className="mb-[8px] max-h-[200px] space-y-[4px] overflow-y-auto">
                  {selectedKbs.map((kb) => {
                    const id = Number(kb.kb_id);
                    const anchor = kb.name === kbName;
                    return (
                      <li key={id} className="flex items-center gap-[7px] rounded-[6px] bg-[#f6f7fa] px-[7px] py-[5px] text-[11px] text-[#464c5e]">
                        <span className="min-w-0 flex-1 truncate" title={kb.name}>{kb.name}</span>
                        {anchor ? <span className="shrink-0 text-[10px] text-[#8a92a5]">主库</span> : (
                          <>
                            {onPrimaryKnowledgeBaseChange ? (
                              <button
                                type="button"
                                aria-label={`设为主库 ${kb.name}`}
                                disabled={knowledgeScopeDisabled}
                                onClick={() => onPrimaryKnowledgeBaseChange(kb.name)}
                                className="shrink-0 rounded px-[4px] text-[#68728a] hover:bg-[#e5e8f0] disabled:opacity-45"
                              >
                                设为主库
                              </button>
                            ) : null}
                            <button
                              type="button"
                              aria-label={`移除知识库 ${kb.name}`}
                              disabled={knowledgeScopeDisabled}
                              onClick={() => onKnowledgeScopeChange(knowledgePurpose, selectedKnowledgeBaseIds.filter((value) => value !== id))}
                              className="shrink-0 rounded px-[4px] text-[#68728a] hover:bg-[#e5e8f0] disabled:opacity-45"
                            >
                              移除
                            </button>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
                <Select
                  value=""
                  disabled={knowledgeScopeDisabled || remainingKbs.length === 0}
                  onValueChange={(value) => onKnowledgeScopeChange(knowledgePurpose, [...selectedKnowledgeBaseIds, Number(value)])}
                >
                  <SelectTrigger aria-label="添加知识库" className="w-full text-[11px]">
                    <SelectValue placeholder={remainingKbs.length ? '选择一个知识库添加' : '已添加所有可用知识库'} />
                  </SelectTrigger>
                  <SelectContent position="popper" align="start" className="max-w-[calc(100vw-24px)]">
                    {remainingKbs.map((kb) => (
                      <SelectItem key={kb.kb_id} value={String(kb.kb_id)}>
                        <span className="block max-w-[280px] truncate" title={kb.name}>{kb.name}</span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="mt-[8px] text-[10px] leading-4 text-[#8a92a5]">
                  仅选主库即可单库检索；逐个添加其他知识库进行联合检索。范围变化后新建会话。
                </p>
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        )}
        {kbName && onWikiAssistChange && (
          <label
            className={cn(
              'flex items-center gap-[6px] rounded-[8px] px-[8px] py-[4px] text-[11px] transition-colors',
              wikiAvailable === false ? 'cursor-not-allowed' : 'cursor-pointer hover:bg-[#f6f7fa]',
            )}
            title={
              wikiAvailable === false
                ? '该知识库还没有 Wiki 内容: 请先到「资产中心 → 知识 Wiki」生成'
                : '开启后本轮问答会检索知识 Wiki 蒸馏页(摘要/实体/概念);默认关闭'
            }
          >
            <HeaderToggle
              enabled={Boolean(wikiAssist)}
              disabled={wikiAvailable === false}
              onToggle={onWikiAssistChange}
              ariaLabel="Wiki 辅助"
              title={
                wikiAvailable === false
                  ? '该知识库还没有 Wiki 内容: 请先到「资产中心 → 知识 Wiki」生成'
                  : '开启后本轮问答会检索知识 Wiki 蒸馏页(摘要/实体/概念);默认关闭'
              }
            />
            <span className={cn('select-none whitespace-nowrap', wikiAvailable === false && 'text-[#a5adc0]')}>
              Wiki 辅助
            </span>
            {wikiAvailable === false && (
              <span className="select-none whitespace-nowrap text-[11px] text-[#b45309]">暂无内容</span>
            )}
          </label>
        )}
        {actions}
        {memorySummary && (
          <label
            className="flex cursor-pointer items-center gap-[6px] rounded-[8px] px-[8px] py-[4px] text-[11px] transition-colors hover:bg-[#f6f7fa]"
            title="开启后每轮对话结束将自动后台提炼项目长期记忆"
          >
            <HeaderToggle
              enabled={memorySummary.auto_extract_enabled}
              disabled={toggling}
              onToggle={(next) => void handleToggle(next)}
              ariaLabel="自动提炼项目记忆"
              title="开启后每轮对话结束将自动后台提炼项目长期记忆"
            />
            <span className="select-none whitespace-nowrap">自动提炼</span>
          </label>
        )}
        {memorySummary && (
          <span
            className={cn(
              'hidden whitespace-nowrap md:inline',
              extracted > 0 ? 'text-[12px] text-[#464c5e]' : 'text-[11px] italic text-[#a2a8b8]',
            )}
          >
            {extracted > 0 ? `已提炼 ${extracted} 条记忆` : '本会话暂无提炼记忆'}
          </span>
        )}
        {sessionConsents != null && (
          <button
            type="button"
            onClick={() => setConsentsOpen(true)}
            title="查看本会话的个人记忆授权台账"
            className={cn(
              'inline-flex whitespace-nowrap rounded-[8px] px-[8px] py-[4px] transition-colors hover:bg-[#f6f7fa]',
              activeCount > 0
                ? 'text-[12px] text-[#464c5e]'
                : 'text-[11px] italic text-[#a2a8b8]',
            )}
          >
            本会话授权 {activeCount} 条
          </button>
        )}
        <Link
          to="/memory"
          title="查看与管理个人长期记忆"
          className="inline-flex h-[30px] items-center gap-[5px] rounded-[8px] px-[9px] text-[11px] text-[#68728a] transition-colors hover:bg-[#f6f7fa] hover:text-[#18181a]"
        >
          <AppIcon name="database" size={13} />
          我的长期记忆
        </Link>
        {onExtractMemory && (
          <button
            type="button"
            onClick={onExtractMemory}
            disabled={extractDisabled}
            className="inline-flex h-[30px] items-center gap-[5px] rounded-[8px] border border-[#e3e7f1] px-[9px] text-[11px] text-[#68728a] transition-colors hover:bg-[#f6f7fa] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-40"
            title="重新提炼当前项目会话中的工程记忆"
          >
            <AppIcon name="history" size={13} />
            重新提炼
          </button>
        )}
      </div>
      <Dialog
        open={consentsOpen}
        onOpenChange={(next) => {
          if (!next) setConsentsOpen(false);
        }}
      >
        <DialogContent className="max-w-[calc(100%-2rem)] gap-0 rounded-[16px] p-0 ring-1 ring-[#e3e7f1] sm:max-w-[560px]">
          <DialogHeader className="gap-[4px] px-[20px] pt-[18px] pb-[10px]">
            <DialogTitle className="text-[15px] leading-[normal] font-semibold text-[#18181a]">
              本会话授权记录
            </DialogTitle>
            <DialogDescription className="text-[12px] leading-[18px] text-[#757f9c]">
              本会话中创建的个人记忆授权；撤销后相关记忆会立即下线，不会再被检索或重放。
            </DialogDescription>
          </DialogHeader>
          <div className="grid max-h-[320px] gap-[8px] overflow-y-auto px-[20px] py-[6px]">
            {sortedConsents.length === 0 ? (
              <div className="rounded-[10px] bg-[#fafbfc] px-[12px] py-[14px] text-center text-[12px] text-[#858b9c]">
                本会话还没有个人记忆授权记录。
              </div>
            ) : (
              sortedConsents.map((consent) => (
                <div
                  key={consent.consent_event_id}
                  className="flex flex-wrap items-center gap-[8px] rounded-[10px] bg-[#fafbfc] px-[12px] py-[8px] text-[12px] text-[#464c5e]"
                >
                  <span className="whitespace-nowrap">{formatDateTime(consent.granted_at)}</span>
                  <span className="whitespace-nowrap">来源 {consent.source_count} 条</span>
                  <span
                    className={cn(
                      'rounded-full px-[8px] py-[1px] text-[11px]',
                      consent.status === 'active'
                        ? 'bg-[#e8f7ee] text-[#28784f]'
                        : 'bg-[#eef1f7] text-[#858b9c]',
                    )}
                  >
                    {consent.status === 'active' ? '有效' : '已撤销'}
                  </span>
                  {consent.status === 'active' && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      className="ml-auto h-[26px] rounded-[8px] px-[10px] text-[12px]"
                      onClick={() => setRevokeTarget(consent)}
                    >
                      撤销
                    </Button>
                  )}
                </div>
              ))
            )}
          </div>
          <div className="flex items-center justify-end px-[16px] py-[16px]">
            <Button
              type="button"
              variant="outline"
              onClick={() => setConsentsOpen(false)}
              className="h-[32px] min-w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] shadow-none hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              关闭
            </Button>
          </div>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(next) => {
          if (!next && !revoking) setRevokeTarget(null);
        }}
        title="撤销这条个人记忆授权？"
        description="将取消未执行的个人记忆任务并下线相关记忆。"
        confirmText="撤销授权"
        destructive={false}
        loading={revoking}
        onConfirm={() => void handleRevokeConfirm()}
      />
    </div>
  );
}

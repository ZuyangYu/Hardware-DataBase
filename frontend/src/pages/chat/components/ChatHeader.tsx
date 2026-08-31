/**
 * 聊天头部(对齐 ChatHeader 结构:会话标题 + 右侧用户菜单下拉退出)。
 * 删掉重命名按钮/员工 handoff;保留会话标题 + KB 名 + 记忆提炼信息 + 退出。
 * 另含本会话授权台账入口(查看/撤销个人记忆授权)与长期记忆页链接。
 */
import { useState } from 'react';
import { Link } from 'react-router-dom';

import UserMenu from '@/components/UserMenu';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cn } from '@/lib/utils';
import { formatDateTime } from '@/lib/enterprise-ui';
import type { MemoryConsentView, SessionMemorySummary } from '@/api/types';
import { Button } from '@/components/ui/button';
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
  userName: string;
  onLogout: () => void;
  onExtractMemory?: () => void;
  extractDisabled?: boolean;
  memorySummary?: SessionMemorySummary | null;
  /** 切换自动提炼开关;返回是否成功(供调用方结束 pending)。 */
  onToggleAutoExtract?: (enabled: boolean) => Promise<boolean>;
  /** 本会话授权台账;null 表示无会话或未加载。 */
  sessionConsents?: MemoryConsentView[] | null;
  /** 撤销一条授权;返回是否成功。 */
  onRevokeConsent?: (consentEventId: string, reason: string) => Promise<boolean>;
};

function MemoryToggle({
  enabled,
  disabled,
  onToggle,
}: {
  enabled: boolean;
  disabled: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label="自动提炼项目记忆"
      disabled={disabled}
      onClick={() => onToggle(!enabled)}
      title="开启后每轮对话结束将自动后台提炼项目长期记忆"
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
  userName,
  onLogout,
  onExtractMemory,
  extractDisabled = false,
  memorySummary = null,
  onToggleAutoExtract,
  sessionConsents = null,
  onRevokeConsent,
}: Props) {
  const [toggling, setToggling] = useState(false);
  const [consentsOpen, setConsentsOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<MemoryConsentView | null>(null);
  const [revoking, setRevoking] = useState(false);
  const extracted = memorySummary?.extracted_memories ?? 0;
  const activeCount = sessionConsents?.filter((consent) => consent.status === 'active').length ?? 0;
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
    <div className={CHAT_HEADER_CLASS}>
      <div className={CHAT_HEADER_TITLE_STACK_CLASS}>
        <span className={CHAT_HEADER_TITLE_NAME_CLASS}>{title || '新对话'}</span>
        <span className={CHAT_HEADER_TITLE_META_CLASS}>{kbName}</span>
      </div>
      <div className="flex items-center gap-[8px]">
        {memorySummary && (
          <label
            className="flex cursor-pointer items-center gap-[6px] rounded-[8px] px-[8px] py-[4px] text-[11px] transition-colors hover:bg-[#f6f7fa]"
            title="开启后每轮对话结束将自动后台提炼项目长期记忆"
          >
            <MemoryToggle
              enabled={memorySummary.auto_extract_enabled}
              disabled={toggling}
              onToggle={(next) => void handleToggle(next)}
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
        <UserMenu userName={userName} onLogout={onLogout} />
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
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] shadow-none hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
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

/**
 * 聊天会话侧栏(对齐 chat 全屏布局左侧的会话列:回链 + KB 名 + 新建 + 会话列表 + 删除)。
 * 会话侧栏本绑定 agent 业务;我们裁成纯会话 CRUD。
 */
import { useNavigate } from 'react-router-dom';

import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cn } from '@/lib/utils';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { KbView, SessionView } from '@/api/types';
import { useState } from 'react';

type Props = {
  kbName: string;
  availableKbs: KbView[];
  sessions: SessionView[];
  sessionsLoaded: boolean;
  activeSessionId: number | null;
  streaming: boolean;
  onKbChange: (kbName: string) => void;
  onSelect: (sessionId: number) => void;
  onNew: () => void;
  onDelete: (sessionId: number) => void;
};

export default function ChatSessionSidebar({
  kbName,
  availableKbs,
  sessions,
  sessionsLoaded,
  activeSessionId,
  streaming,
  onKbChange,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  const navigate = useNavigate();
  const [deleteTarget, setDeleteTarget] = useState<SessionView | null>(null);
  function kbOptionLabel(kb: KbView): string {
    return kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
  }
  return (
    <aside className="flex w-[240px] shrink-0 flex-col border-r border-[#f4f4f4] bg-white">
      <div className="border-b border-[#f4f4f4] p-[12px]">
        <button
          type="button"
          onClick={() => navigate('/kbs')}
          className="flex items-center gap-[6px] text-[12px] text-[#858b9c] transition-colors hover:text-[#18181a]"
        >
          <AppIcon name="arrow" size={14} style={{ transform: 'rotate(180deg)' }} />
          全部知识库
        </button>
        <div className="mt-[10px] grid gap-[5px]">
          <span className="text-[11px] text-[#858b9c]">挂载知识库</span>
          <Select value={kbName || '__none__'} onValueChange={(value) => onKbChange(value === '__none__' ? '' : value)} disabled={streaming}>
            <SelectTrigger className="h-[32px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[12px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">不挂载</SelectItem>
              {availableKbs.map((kb) => (
                <SelectItem key={kb.kb_id ?? `${kb.department_id ?? 'none'}:${kb.name}`} value={kb.name}>
                  {kbOptionLabel(kb)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="mt-[10px] flex items-center justify-between gap-[8px]">
          <span className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
            {kbName || '通用对话'}
          </span>
          <button
            type="button"
            onClick={onNew}
            disabled={streaming}
            aria-label="新对话"
            className="inline-grid size-[28px] shrink-0 place-items-center rounded-[8px] text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]"
          >
            <AppIcon name="plus" size={16} />
          </button>
        </div>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="grid gap-[2px] p-[8px]">
          {!sessionsLoaded && [0, 1, 2].map((i) => <Skeleton key={i} className="h-[36px] rounded-[8px]" />)}
          {sessionsLoaded && sessions.length === 0 && (
            <div className="px-[8px] py-[16px] text-center text-[12px] text-[#858b9c]">
              暂无会话,点击 + 开始
            </div>
          )}
          {sessions.map((session) => {
            const active = session.id === activeSessionId;
            return (
              <div
                key={session.id}
                className={cn(
                  'group flex items-center gap-[6px] rounded-[8px] px-[10px] py-[8px] text-[13px] transition-colors',
                  !streaming && 'cursor-pointer',
                  active
                    ? 'bg-[#f6f6f6] text-[#18181a]'
                    : 'text-[#858b9c] hover:bg-[#f6f6f6] hover:text-[#18181a]',
                )}
                onClick={() => !streaming && onSelect(session.id)}
              >
                <span className="min-w-0 flex-1 truncate">{session.title}</span>
                <button
                  type="button"
                  aria-label="删除会话"
                  disabled={streaming}
                  className="hidden shrink-0 text-[#858b9c] hover:text-[#d20b0b] group-hover:block disabled:cursor-not-allowed disabled:opacity-40"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteTarget(session);
                  }}
                >
                  <AppIcon name="trash" size={14} />
                </button>
              </div>
            );
          })}
        </div>
      </ScrollArea>
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title={<>删除会话「{deleteTarget?.title}」</>}
        description="删除后该会话中的历史消息将不可恢复。"
        confirmText="删除"
        destructive
        onConfirm={() => {
          if (!deleteTarget) return;
          onDelete(deleteTarget.id);
          setDeleteTarget(null);
        }}
      />
    </aside>
  );
}

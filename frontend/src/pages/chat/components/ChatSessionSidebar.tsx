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
import { useEffect, useRef, useState } from 'react';

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
  const [collapsed, setCollapsed] = useState(false);
  const [width, setWidth] = useState(240);
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!dragRef.current) return;
      const { startX, startW } = dragRef.current;
      setWidth(Math.min(420, Math.max(180, startW + e.clientX - startX)));
    }
    function onUp() {
      dragRef.current = null;
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  function kbOptionLabel(kb: KbView): string {
    return kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name;
  }
  if (collapsed) {
    return (
      <aside className="flex w-[44px] shrink-0 flex-col items-center border-r border-[#f4f4f4] bg-white py-[12px]">
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          aria-label="展开会话侧栏"
          title="展开会话侧栏"
          className="inline-grid size-[28px] place-items-center rounded-[8px] text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]"
        >
          <AppIcon name="arrow" size={16} />
        </button>
      </aside>
    );
  }
  return (
    <aside className="group relative flex shrink-0 flex-col border-r border-[#f4f4f4] bg-white" style={{ width }}>
      <div
        onMouseDown={(e) => {
          dragRef.current = { startX: e.clientX, startW: width };
          e.preventDefault();
        }}
        className="absolute right-0 top-0 z-10 h-full w-[3px] cursor-col-resize bg-transparent opacity-0 transition-opacity group-hover:bg-[#d0d5dd] group-hover:opacity-100 active:!opacity-100"
        title="拖动调整宽度"
      />
      <div className="border-b border-[#f4f4f4] p-[12px]">
        <div className="flex items-center justify-between gap-[8px]">
          <button
            type="button"
            onClick={() => navigate('/kbs')}
            className="flex items-center gap-[6px] text-[12px] text-[#858b9c] transition-colors hover:text-[#18181a]"
          >
            <AppIcon name="arrow" size={14} style={{ transform: 'rotate(180deg)' }} />
            全部知识库
          </button>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            aria-label="收起会话侧栏"
            title="收起会话侧栏"
            className="inline-grid size-[24px] shrink-0 place-items-center rounded-[6px] text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]"
          >
            <AppIcon name="arrow" size={13} style={{ transform: 'rotate(180deg)' }} />
          </button>
        </div>
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
                  'group flex min-w-0 items-center gap-[6px] overflow-hidden rounded-[8px] px-[10px] py-[8px] text-[13px] transition-colors',
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
                  className="ml-1 mr-[2px] shrink-0 rounded-[6px] bg-[#f0f1f3] p-1 text-[#6b7280] hover:bg-[#fdeaea] hover:text-[#d20b0b] disabled:cursor-not-allowed disabled:opacity-40"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteTarget(session);
                  }}
                >
                  <AppIcon name="trash" size={15} className="text-[#6b7280]" />
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

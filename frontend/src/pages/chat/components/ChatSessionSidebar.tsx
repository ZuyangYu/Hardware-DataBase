/**
 * 聊天会话侧栏(布局在聊天主区右侧:KB 名 + 新建 + 会话列表 + 删除)。
 * 会话侧栏本绑定 agent 业务;我们裁成纯会话 CRUD。
 * 检索范围下拉框在输入区上方(Composer)。
 */
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cn } from '@/lib/utils';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import type { SessionView } from '@/api/types';
import { useEffect, useRef, useState } from 'react';

type Props = {
  kbName: string;
  sessions: SessionView[];
  sessionsLoaded: boolean;
  activeSessionId: number | null;
  streaming: boolean;
  onSelect: (sessionId: number) => void;
  onNew: () => void;
  onDelete: (sessionId: number) => void;
};

export default function ChatSessionSidebar({
  kbName,
  sessions,
  sessionsLoaded,
  activeSessionId,
  streaming,
  onSelect,
  onNew,
  onDelete,
}: Props) {
  const [deleteTarget, setDeleteTarget] = useState<SessionView | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [width, setWidth] = useState(240);
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  useEffect(() => {
    function onMove(e: MouseEvent) {
      if (!dragRef.current) return;
      const { startX, startW } = dragRef.current;
      // 侧栏在右侧:向左拖动加宽。
      setWidth(Math.min(420, Math.max(180, startW - (e.clientX - startX))));
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

  const mobileLauncher = !mobileOpen && (
    <button
      type="button"
      onClick={() => {
        setCollapsed(false);
        setMobileOpen(true);
      }}
      aria-label="打开会话列表"
      title="打开会话列表"
      className="fixed right-[12px] top-[68px] z-30 hidden size-[32px] place-items-center rounded-[9px] border border-[#e3e7f1] bg-white text-[#757f9c] shadow-[0_4px_14px_rgba(24,24,26,0.12)] hover:text-[#18181a] max-[700px]:inline-grid"
    >
      <AppIcon name="arrow" size={15} style={{ transform: 'rotate(180deg)' }} />
    </button>
  );
  const mobileScrim = mobileOpen && (
    <button
      type="button"
      aria-label="关闭会话列表"
      onClick={() => setMobileOpen(false)}
      className="fixed inset-0 z-40 hidden bg-black/15 max-[700px]:block"
    />
  );

  if (collapsed) {
    return <>
      {mobileLauncher}
      <aside className="flex w-[44px] shrink-0 flex-col items-center border-l border-[#eceef3] bg-[#fbfbfd] py-[12px] max-[700px]:hidden">
        <button
          type="button"
          onClick={() => {
            setCollapsed(false);
            setMobileOpen(true);
          }}
          aria-label="展开会话侧栏"
          title="展开会话侧栏"
          className="inline-grid size-[28px] place-items-center rounded-[8px] text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]"
        >
          <AppIcon name="arrow" size={16} style={{ transform: 'rotate(180deg)' }} />
        </button>
      </aside>
    </>;
  }
  return (
    <>
      {mobileLauncher}
      {mobileScrim}
      <aside className={cn(
        'group relative flex shrink-0 flex-col border-l border-[#eceef3] bg-[#fbfbfd]',
        mobileOpen ? 'max-[700px]:fixed max-[700px]:right-0 max-[700px]:top-0 max-[700px]:z-50 max-[700px]:h-full max-[700px]:!w-[min(320px,calc(100vw-24px))]' : 'max-[700px]:hidden',
      )} style={{ width }}>
      <div
        onMouseDown={(e) => {
          dragRef.current = { startX: e.clientX, startW: width };
          e.preventDefault();
        }}
        className="absolute left-0 top-0 z-10 h-full w-[3px] cursor-col-resize bg-transparent opacity-0 transition-opacity group-hover:bg-[#d0d5dd] group-hover:opacity-100 active:!opacity-100"
        title="拖动调整宽度"
      />
      <div className="border-b border-[#eceef3] px-[14px] pb-[12px] pt-[14px]">
        <div className="flex items-start justify-between gap-[8px]">
          <div className="min-w-0">
            <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-[#9aa1b1]">
              会话
            </div>
            <div className="mt-[3px] flex min-w-0 items-center gap-[6px]">
              <span className="min-w-0 truncate text-[14px] font-semibold text-[#18181a]">
                {kbName || '通用对话'}
              </span>
              <span className="shrink-0 rounded-full bg-[#f0f2f6] px-[6px] py-[1px] text-[10px] tabular-nums text-[#6b7280]">
                {sessions.length}
              </span>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-[2px]">
            <button
              type="button"
              onClick={() => {
                setCollapsed(true);
                setMobileOpen(false);
              }}
              aria-label="收起会话侧栏"
              title="收起会话侧栏"
              className="inline-grid size-[26px] shrink-0 place-items-center rounded-[7px] text-[#8b93a7] transition-colors hover:bg-white hover:text-[#18181a]"
            >
              <AppIcon name="arrow" size={13} />
            </button>
            <button
              type="button"
              onClick={onNew}
              aria-label="新对话"
              title="新建会话"
              className="inline-grid size-[28px] shrink-0 place-items-center rounded-[8px] border border-[#e6e9f0] bg-white text-[#5b6273] shadow-[0_1px_2px_rgba(24,24,26,0.03)] transition-colors hover:border-[#cfd5e2] hover:text-[#18181a]"
            >
              <AppIcon name="plus" size={15} />
            </button>
          </div>
        </div>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="grid gap-[6px] p-[10px]">
          {!sessionsLoaded && [0, 1, 2].map((i) => <Skeleton key={i} className="h-[38px] rounded-[10px]" />)}
          {sessionsLoaded && sessions.length === 0 && (
            <div className="rounded-[10px] border border-dashed border-[#e6e9f0] px-[10px] py-[18px] text-center text-[11px] leading-[17px] text-[#9aa1b1]">
              暂无会话
              <br />
              点击右上角 + 开始
            </div>
          )}
          {sessions.map((session) => {
            const active = session.id === activeSessionId;
            return (
              <div
                key={session.id}
                className={cn(
                  'group relative flex min-w-0 items-center gap-[6px] overflow-hidden rounded-[10px] border px-[5px] py-[5px] text-[12px] transition-all',
                  active
                    ? 'border-[#e6e9f0] bg-white text-[#18181a] shadow-[0_1px_3px_rgba(24,24,26,0.05)]'
                    : 'border-transparent text-[#6b7280] hover:border-[#eceef3] hover:bg-white/70 hover:text-[#18181a]',
                )}
              >
                <button
                  type="button"
                  aria-label={`打开会话 ${session.title}`}
                  aria-pressed={active}
                  onClick={() => onSelect(session.id)}
                  className="relative flex min-w-0 flex-1 items-center gap-[6px] rounded-[7px] px-[6px] py-[4px] text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring"
                >
                  <span
                    className={cn(
                      'absolute bottom-[4px] left-0 top-[4px] w-[2px] rounded-full bg-[#18181a] transition-opacity',
                      active ? 'opacity-100' : 'opacity-0',
                    )}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1 truncate pl-[2px]">{session.title}</span>
                </button>
                <button
                  type="button"
                  aria-label="删除会话"
                  disabled={streaming}
                  className={cn(
                    'ml-1 shrink-0 rounded-[6px] p-[5px] text-[#8b93a7] transition-all hover:bg-[#fdeaea] hover:text-[#d20b0b] disabled:cursor-not-allowed disabled:opacity-40',
                    active ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 max-[700px]:opacity-100',
                  )}
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteTarget(session);
                  }}
                >
                  <AppIcon name="trash" size={13} />
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
    </>
  );
}
